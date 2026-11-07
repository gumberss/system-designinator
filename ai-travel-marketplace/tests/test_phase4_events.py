from __future__ import annotations

import asyncio
from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from packages.common.events import (
    EventEnvelope,
    OutboxBase,
    OutboxEvent,
    emit_event,
    serialize_event,
)
from services.search.app import (
    ConsumerFailure,
    InboxEvent,
    ReservationProjection,
    SearchBase,
    SearchDocument,
    _consume_message,
    process_event,
    rebuild_projection,
)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'phase4.db'}",
        connect_args={"check_same_thread": False},
    )
    SearchBase.metadata.create_all(engine)
    OutboxBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def catalog_event(
    *,
    event_id: str = "catalog-event-1",
    aggregate_version: int = 1,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": "CatalogSnapshot",
        "schema_version": 1,
        "aggregate_type": "catalog",
        "aggregate_id": "flight",
        "aggregate_version": aggregate_version,
        "occurred_at": datetime(2026, 11, 5).isoformat(),
        "producer": "flight",
        "causation_id": None,
        "payload": {
            "documents": [
                {
                    "id": "offer-001",
                    "kind": "flight",
                    "name": "Tokyo-Kyoto",
                    "origin_city_code": "TYO",
                    "destination_city_code": "KYO",
                }
            ]
        },
    }


def reservation_event(
    event_id: str, version: int, status: str
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": "ReservationStateChanged",
        "schema_version": 1,
        "aggregate_type": "reservation",
        "aggregate_id": "reservation-001",
        "aggregate_version": version,
        "occurred_at": datetime(2026, 11, 5).isoformat(),
        "producer": "flight",
        "causation_id": None,
        "payload": {
            "reservation_id": "reservation-001",
            "resource_type": "flight",
            "resource_id": "offer-001",
            "status": status,
        },
    }


def test_search_projection_deduplicates_versions_and_rebuilds(session_factory):
    with session_factory() as session:
        assert process_event(session, catalog_event())
        assert not process_event(session, catalog_event())
        assert process_event(session, reservation_event("reservation-held", 1, "held"))
        assert process_event(
            session, reservation_event("reservation-confirmed", 2, "confirmed")
        )
        assert not process_event(
            session, reservation_event("reservation-delayed", 1, "held")
        )

        document = session.scalar(select(SearchDocument))
        reservation = session.get(ReservationProjection, "reservation-001")
        assert document is not None
        assert document.document["id"] == "offer-001"
        assert reservation is not None
        assert reservation.status == "confirmed"
        assert reservation.aggregate_version == 2
        assert session.scalar(select(func.count(InboxEvent.id))) == 4

        session.execute(SearchDocument.__table__.delete())
        session.commit()
        assert rebuild_projection(session) == 4
        rebuilt = session.scalar(select(SearchDocument))
        assert rebuilt is not None and rebuilt.document["name"] == "Tokyo-Kyoto"


def test_projection_rejects_version_gaps_and_unknown_schema(session_factory):
    with session_factory() as session:
        with pytest.raises(ValueError, match="version gap"):
            process_event(
                session,
                reservation_event("reservation-v2", 2, "confirmed"),
            )
        unsupported = catalog_event()
        unsupported["schema_version"] = 2
        with pytest.raises(ValueError, match="Unsupported event schema version"):
            process_event(session, unsupported)


def test_outbox_event_is_versioned_json_and_transactional(session_factory):
    with session_factory() as session:
        emit_event(
            session,
            producer="booking",
            event_type="BookingStateChanged",
            aggregate_type="booking",
            aggregate_id="booking-001",
            aggregate_version=3,
            payload={"booking_id": "booking-001", "status": "completed"},
        )
        session.rollback()
        assert session.scalar(select(func.count(OutboxEvent.event_id))) == 0

        emit_event(
            session,
            producer="booking",
            event_type="BookingStateChanged",
            aggregate_type="booking",
            aggregate_id="booking-001",
            aggregate_version=3,
            payload={"booking_id": "booking-001", "status": "completed"},
        )
        session.commit()
        stored = session.scalar(select(OutboxEvent))
        assert stored is not None
        encoded = serialize_event(stored)
        decoded = EventEnvelope.model_validate(json.loads(encoded))
        assert decoded.aggregate_version == 3
        assert decoded.payload["status"] == "completed"


def test_consumer_dead_letters_after_maximum_attempts(session_factory, monkeypatch):
    class FakeProducer:
        def __init__(self):
            self.messages = []

        async def send_and_wait(self, topic, value, key):
            self.messages.append((topic, json.loads(value), key))

    class FakeConsumer:
        def __init__(self):
            self.committed = None

        async def commit(self, offsets):
            self.committed = offsets

        async def end_offsets(self, partitions):
            return {partitions[0]: 10}

        async def position(self, partition):
            assert partition.partition == 0
            return 4

    async def no_wait(delay):
        assert delay >= 0
        return None

    monkeypatch.setattr("services.search.app.asyncio.sleep", no_wait)
    event = reservation_event("reservation-gap", 2, "confirmed")
    message = SimpleNamespace(
        topic="marketplace.events.v1",
        partition=0,
        offset=3,
        value=json.dumps(event).encode(),
        key=b"reservation-001",
    )
    producer = FakeProducer()
    consumer = FakeConsumer()
    runtime = {}

    asyncio.run(
        _consume_message(
            session_factory,
            producer,
            consumer,
            message,
            runtime,
            "marketplace.events.dlq.v1",
        )
    )

    assert len(producer.messages) == 1
    topic, dead_letter, key = producer.messages[0]
    assert topic == "marketplace.events.dlq.v1"
    assert dead_letter["attempts"] == 5
    assert key == b"reservation-001"
    assert consumer.committed is not None
    with session_factory() as session:
        failure = session.get(ConsumerFailure, "reservation-gap")
        assert failure is not None
        assert failure.attempts == 5
        assert failure.dead_lettered_at is not None
