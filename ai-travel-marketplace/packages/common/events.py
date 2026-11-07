from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from datetime import date
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, JSON, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from packages.common.booking import utc_now

logger = logging.getLogger(__name__)
EVENTS_TOPIC = "marketplace.events.v1"
EVENTS_DLQ_TOPIC = "marketplace.events.dlq.v1"
MAX_DELIVERY_ATTEMPTS = 5


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=36)
    event_type: str = Field(min_length=1, max_length=120)
    schema_version: int = Field(ge=1)
    aggregate_type: str = Field(min_length=1, max_length=80)
    aggregate_id: str = Field(min_length=1, max_length=180)
    aggregate_version: int = Field(ge=1)
    occurred_at: datetime
    producer: str = Field(min_length=1, max_length=80)
    causation_id: str | None = Field(default=None, max_length=180)
    payload: dict[str, Any]


class OutboxBase(DeclarativeBase):
    pass


class OutboxEvent(OutboxBase):
    __tablename__ = "event_outbox"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    aggregate_type: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[str] = mapped_column(String(180), index=True)
    aggregate_version: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    producer: Mapped[str] = mapped_column(String(80))
    causation_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def emit_event(
    session: Session,
    *,
    producer: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    payload: dict[str, Any],
    causation_id: str | None = None,
    event_id: str | None = None,
) -> OutboxEvent:
    envelope = EventEnvelope(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        schema_version=1,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        occurred_at=utc_now(),
        producer=producer,
        causation_id=causation_id,
        payload=payload,
    )
    event_data = envelope.model_dump()
    event_data["payload"] = json.loads(
        json.dumps(event_data["payload"], default=lambda value: value.isoformat() if isinstance(value, (date, datetime)) else str(value))
    )
    event = OutboxEvent(**event_data)
    session.add(event)
    return event


def emit_catalog_snapshot(
    session: Session,
    *,
    producer: str,
    documents: list[dict[str, Any]],
) -> None:
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"marketplace:{producer}:catalog:v1"))
    if session.get(OutboxEvent, event_id) is not None:
        return
    emit_event(
        session,
        producer=producer,
        event_type="CatalogSnapshot",
        aggregate_type="catalog",
        aggregate_id=producer,
        aggregate_version=1,
        payload={"documents": documents},
        event_id=event_id,
    )
    session.commit()


def emit_reservation_state(
    session: Session,
    *,
    producer: str,
    reservation_id: str,
    resource_type: str,
    resource_id: str,
    status: str,
    version: int,
) -> None:
    emit_event(
        session,
        producer=producer,
        event_type="ReservationStateChanged",
        aggregate_type="reservation",
        aggregate_id=reservation_id,
        aggregate_version=version,
        payload={
            "reservation_id": reservation_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "status": status,
        },
    )


def serialize_event(event: OutboxEvent) -> bytes:
    envelope = EventEnvelope(
        event_id=event.event_id,
        event_type=event.event_type,
        schema_version=event.schema_version,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        occurred_at=event.occurred_at,
        producer=event.producer,
        causation_id=event.causation_id,
        payload=event.payload,
    )
    return json.dumps(envelope.model_dump(mode="json"), separators=(",", ":")).encode(
        "utf-8"
    )


async def publish_outbox(
    factory: sessionmaker[Session], producer_name: str
) -> None:
    from aiokafka import AIOKafkaProducer

    brokers = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    if not brokers:
        raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS is required for event publishing")
    producer = AIOKafkaProducer(
        bootstrap_servers=brokers.split(","),
        client_id=f"{producer_name}-outbox",
        enable_idempotence=True,
    )
    await producer.start()
    try:
        while True:
            with factory() as session:
                event = session.scalar(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.dead_lettered_at.is_(None),
                    )
                    .order_by(
                        OutboxEvent.occurred_at,
                        OutboxEvent.aggregate_id,
                        OutboxEvent.aggregate_version,
                    )
                    .limit(1)
                )
                if event is None:
                    event_data = None
                else:
                    event_data = {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "schema_version": event.schema_version,
                        "aggregate_id": event.aggregate_id,
                        "payload": event.payload,
                    }
            if event_data is None:
                await asyncio.sleep(0.5)
                continue
            try:
                with factory() as session:
                    event = session.get(OutboxEvent, event_data["event_id"])
                    if event is None:
                        continue
                    value = serialize_event(event)
                    key = event.aggregate_id.encode("utf-8")
                    attempts = event.attempts
                if attempts < MAX_DELIVERY_ATTEMPTS:
                    await producer.send_and_wait(
                        os.getenv("EVENTS_TOPIC", EVENTS_TOPIC), value=value, key=key
                    )
                    with factory() as session:
                        event = session.get(OutboxEvent, event_data["event_id"])
                        if event is not None:
                            event.attempts += 1
                            event.published_at = utc_now()
                            event.last_error = None
                            session.commit()
                    continue
                dlq_value = json.dumps(
                    {
                        "event": json.loads(value),
                        "failure": {
                            "reason": "maximum delivery attempts exceeded",
                            "attempts": attempts,
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                await producer.send_and_wait(
                    os.getenv("EVENTS_DLQ_TOPIC", EVENTS_DLQ_TOPIC),
                    value=dlq_value,
                    key=key,
                )
                with factory() as session:
                    event = session.get(OutboxEvent, event_data["event_id"])
                    if event is not None:
                        event.dead_lettered_at = utc_now()
                        event.last_error = "maximum delivery attempts exceeded"
                        session.commit()
            except Exception as error:
                logger.exception("Outbox delivery failed for event %s", event_data["event_id"])
                with factory() as session:
                    event = session.get(OutboxEvent, event_data["event_id"])
                    if event is not None:
                        event.attempts += 1
                        event.last_error = str(error)[:1000]
                        session.commit()
                        delay = min(0.5 * (2 ** min(event.attempts, 6)), 30.0)
                    else:
                        delay = 1.0
                await asyncio.sleep(delay * random.uniform(0.75, 1.25))
    finally:
        await producer.stop()
