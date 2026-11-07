from __future__ import annotations

import os
import asyncio
import logging
from collections.abc import Callable, Generator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from packages.common.events import OutboxBase, publish_outbox

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **options)


def add_missing_columns(
    engine: Engine, table_name: str, columns: Mapping[str, str]
) -> None:
    """Add nullable columns needed by a service's forward-only local migration."""
    existing = {column["name"] for column in inspect(engine).get_columns(table_name)}
    missing = {name: ddl for name, ddl in columns.items() if name not in existing}
    if not missing:
        return

    preparer = engine.dialect.identifier_preparer
    table = preparer.quote(table_name)
    with engine.begin() as connection:
        for name, ddl in missing.items():
            column = preparer.quote(name)
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def session_dependency(request: Request) -> Generator[Session, None, None]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


def service_lifespan(
    *,
    base: type[DeclarativeBase],
    seed: Callable[[Session], None],
    default_database_url: str,
    migrate: Callable[[Engine], None] | None = None,
    events_producer: str | None = None,
    bootstrap_events: Callable[[Session], None] | None = None,
) -> Callable[..., Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Generator[None, None, None]:
        database_url = os.getenv("DATABASE_URL", default_database_url)
        engine = build_engine(database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        app.state.engine = engine
        app.state.session_factory = factory
        base.metadata.create_all(engine)
        if events_producer is not None:
            OutboxBase.metadata.create_all(engine)
        if migrate is not None:
            migrate(engine)
        with factory() as session:
            seed(session)
            if bootstrap_events is not None:
                bootstrap_events(session)
        publisher_task = None
        if events_producer is not None:
            if os.getenv("KAFKA_BOOTSTRAP_SERVERS"):
                publisher_task = asyncio.create_task(
                    publish_outbox(factory, events_producer)
                )
            else:
                logger.warning(
                    "Event publishing for %s is disabled: KAFKA_BOOTSTRAP_SERVERS is unset",
                    events_producer,
                )
        try:
            yield
        finally:
            if publisher_task is not None:
                publisher_task.cancel()
                try:
                    await publisher_task
                except asyncio.CancelledError:
                    pass
            engine.dispose()

    return lifespan
