import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from uuid import uuid4

import pytest
from sqlalchemy import text

from mapinator.discovery import Criteria
from mapinator.discovery_store import PostgresConversationStore
from mapinator.domain import DomainError
from mapinator.postgres import PostgresRepository, make_engine, migrate

pytestmark = pytest.mark.integration


def test_opt_in_postgres_owner_revision_transaction_ttl_and_cleanup() -> None:
    url = os.environ.get("MAPINATOR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Pending SQL: set MAPINATOR_TEST_DATABASE_URL to an isolated PostGIS database.")
    engine = make_engine(url)
    repository = PostgresRepository(engine)
    cleanup_owner = None
    try:
        migrate(engine)
        with engine.begin() as connection:
            if not connection.execute(
                text("SELECT 1 FROM mapinator_schema_versions WHERE version=5")
            ).scalar_one_or_none():
                sql = files("mapinator").joinpath("migrations", "005_discovery.sql").read_text()
                for statement in sql.split(";"):
                    if statement.strip():
                        connection.exec_driver_sql(statement)
        now = datetime.now(UTC)
        identity = repository.create_identity(str(uuid4()), str(uuid4()), now)
        owner_id = identity.owner_id
        cleanup_owner = owner_id
        store = PostgresConversationStore(engine, ttl_s=60)
        original = store.create(owner_id, Criteria(categories=["cafe"], wifi=True), now)
        identifier = original.conversation_id
        assert store.get(owner_id, identifier, now) == original
        assert store.get(uuid4(), identifier, now) is None
        assert store.get(owner_id, identifier, now + timedelta(seconds=60)) is None
        with pytest.raises(DomainError) as denied:
            store.update(uuid4(), identifier, 1, Criteria(), now)
        assert denied.value.status == 404

        def concurrent_update(radius: float) -> int:
            try:
                return store.update(
                    owner_id,
                    identifier,
                    1,
                    Criteria(categories=["cafe"], radius_m=radius),
                    now + timedelta(seconds=1),
                ).revision
            except DomainError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(concurrent_update, (1000.0, 1500.0))) == [2, 409]
        saved = store.get(owner_id, identifier, now)
        assert saved is not None and saved.revision == 2
        with pytest.raises(DomainError) as conflict:
            store.update(owner_id, identifier, 1, Criteria(query="should not persist"), now)
        assert conflict.value.status == 409
        assert store.get(owner_id, identifier, now) == saved
        with engine.connect() as connection:
            data: dict[str, object] = connection.execute(
                text("""
                SELECT criteria FROM discovery_conversations WHERE conversation_id=:id
            """),
                {"id": identifier},
            ).scalar_one()
            assert "center" not in data and "message" not in data
        store.cleanup(now + timedelta(seconds=62))
        assert store.get(owner_id, identifier, now) is None
        cascade = store.create(owner_id, Criteria(), now)
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM browser_sessions WHERE owner_id=:owner"), {"owner": owner_id}
            )
        assert store.get(owner_id, cascade.conversation_id, now) is None
    finally:
        if cleanup_owner is not None:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM browser_sessions WHERE owner_id=:owner"),
                    {"owner": cleanup_owner},
                )
        repository.close()
