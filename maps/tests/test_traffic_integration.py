import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mapinator.domain import Observation
from mapinator.osm import parse_osm
from mapinator.postgres import PostgresRepository, import_dataset, make_engine, migrate
from mapinator.routing import build_graph
from mapinator.traffic import TrafficEvent
from mapinator.traffic_store import DLQ_TOPIC, TrafficStore

pytestmark = pytest.mark.integration


def test_real_traffic_outbox_inbox_aggregation_and_revocation(tmp_path: Path) -> None:
    url = os.environ.get("MAPINATOR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires isolated disposable PostgreSQL/PostGIS URL.")
    engine = make_engine(url)
    repository = PostgresRepository(engine)
    store = TrafficStore(engine)
    try:
        migrate(engine)
        now = datetime.now(UTC)
        repository.cleanup(now + timedelta(days=2))
        path = tmp_path / "synthetic-traffic.osm"
        path.write_text(
            '<osm><node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="0.004"/>'
            '<way id="10"><nd ref="1"/><nd ref="2"/>'
            '<tag k="highway" v="residential"/><tag k="oneway" v="yes"/></way></osm>'
        )
        dataset = parse_osm(path)
        import_dataset(engine, dataset, "https://example.invalid/synthetic-traffic-fixture")
        graph = build_graph(dataset)
        owners = []
        for _ in range(5):
            identity = repository.create_identity(str(uuid4()), str(uuid4()), now)
            owners.append(identity.owner_id)
            tracker = repository.start_tracking(identity.owner_id, now)
            first = Observation(
                uuid4(), tracker.session_id, 1, now - timedelta(seconds=2), 0, 0.002, 5, 90, 3
            )
            assert repository.save_location(identity.owner_id, first, now)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("""
                    SELECT count(*) FROM traffic_outbox WHERE payload->>'session_id'=:session
                """),
                        {"session": str(tracker.session_id)},
                    ).scalar_one()
                    == 0
                )
            repository.traffic_consent(identity.owner_id, tracker.session_id, True, now)
            second = Observation(
                uuid4(), tracker.session_id, 2, now - timedelta(seconds=1), 0, 0.002, 5, 90, 3
            )
            assert repository.save_location(identity.owner_id, second, now)
            assert not repository.save_location(identity.owner_id, second, now)
            record = store.claim(now)
            assert record is not None
            event = TrafficEvent.model_validate(record["payload"])
            assert store.consume(event, graph, dataset.checksum, now) == "matched"
            assert store.consume(event, graph, dataset.checksum, now) == "duplicate"
            lease = record["lease_id"]
            assert isinstance(lease, UUID)
            store.delivered(event.event_id, lease)
        assert store.aggregate_window(graph, dataset.checksum, now) == 1
        snapshot = store.snapshot(dataset.checksum, now)
        estimate = snapshot.estimates["10:0:0:1"]
        assert estimate.contributors == 5 and estimate.speed_mps == 3
        assert store.aggregate_window(graph, dataset.checksum, now + timedelta(seconds=31)) == 0
        repository.stop_tracking(owners[0])
        assert store.aggregate_window(graph, dataset.checksum, now + timedelta(seconds=32)) == 0
        assert not store.snapshot(dataset.checksum, now).estimates
        repository.cleanup(now + timedelta(days=2))
    finally:
        repository.close()


def test_real_kafka_publish_ack_and_retention_configuration() -> None:
    bootstrap = os.environ.get("MAPINATOR_TEST_KAFKA_BOOTSTRAP_SERVERS")
    if not bootstrap:
        pytest.skip("Requires an isolated disposable Kafka-compatible broker URL.")

    async def verify() -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
        from aiokafka.structs import OffsetAndMetadata, TopicPartition

        from mapinator.traffic_worker import initialize_topics

        await initialize_topics(bootstrap)
        consumer = AIOKafkaConsumer(
            DLQ_TOPIC,
            bootstrap_servers=bootstrap,
            group_id=f"mapinator-test-{uuid4()}",
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        producer = AIOKafkaProducer(bootstrap_servers=bootstrap, enable_idempotence=True)
        identifier = str(uuid4())
        try:
            await consumer.start()
            await producer.start()
            await producer.send_and_wait(
                DLQ_TOPIC,
                json.dumps(
                    {
                        "event_id": identifier,
                        "code": "synthetic-integration-check",
                    }
                ).encode(),
                key=identifier.encode(),
            )
            async with asyncio.timeout(15):
                while True:
                    record = await consumer.getone()
                    if json.loads(record.value)["event_id"] == identifier:
                        await consumer.commit(
                            {
                                TopicPartition(record.topic, record.partition): OffsetAndMetadata(
                                    record.offset + 1, ""
                                ),
                            }
                        )
                        break
        finally:
            await producer.stop()
            await consumer.stop()

    asyncio.run(verify())


def test_real_workers_end_to_end(tmp_path: Path) -> None:
    url = os.environ.get("MAPINATOR_TEST_DATABASE_URL")
    bootstrap = os.environ.get("MAPINATOR_TEST_KAFKA_BOOTSTRAP_SERVERS")
    if not url or not bootstrap:
        pytest.skip("Requires both isolated PostGIS and Kafka endpoints for real worker execution.")
    from mapinator.traffic_worker import analytics, initialize_topics, relay

    engine = make_engine(url)
    repository = PostgresRepository(engine)
    store = TrafficStore(engine)
    now = datetime.now(UTC)
    try:
        migrate(engine)
        repository.cleanup(now + timedelta(days=2))
        path = tmp_path / "synthetic-e2e.osm"
        path.write_text(
            '<osm><node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="0.004"/>'
            '<way id="10"><nd ref="1"/><nd ref="2"/>'
            '<tag k="highway" v="residential"/><tag k="oneway" v="yes"/></way></osm>'
        )
        dataset = parse_osm(path)
        import_dataset(engine, dataset, "https://example.invalid/synthetic-e2e-fixture")
        for _ in range(5):
            identity = repository.create_identity(str(uuid4()), str(uuid4()), now)
            tracking = repository.start_tracking(identity.owner_id, now)
            repository.traffic_consent(identity.owner_id, tracking.session_id, True, now)
            repository.save_location(
                identity.owner_id,
                Observation(
                    uuid4(), tracking.session_id, 1, now - timedelta(seconds=1), 0, 0.002, 5, 90, 3
                ),
                now,
            )

        async def verify() -> None:
            await initialize_topics(bootstrap)
            tasks = [
                asyncio.create_task(analytics(store, repository, bootstrap)),
                asyncio.create_task(relay(store, bootstrap)),
            ]
            try:
                async with asyncio.timeout(50):
                    while True:
                        for task in tasks:
                            if task.done():
                                await task
                                raise AssertionError("Worker exited before publishing traffic.")
                        snapshot = await asyncio.to_thread(
                            store.snapshot, dataset.checksum, datetime.now(UTC)
                        )
                        estimate = snapshot.estimates.get("10:0:0:1")
                        if estimate and estimate.contributors == 5:
                            assert estimate.speed_mps == 3
                            break
                        await asyncio.sleep(0.5)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        asyncio.run(verify())
        repository.cleanup(now + timedelta(days=2))
    finally:
        repository.close()
