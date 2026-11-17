import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
from aiokafka.errors import KafkaError, TopicAlreadyExistsError
from aiokafka.structs import OffsetAndMetadata, TopicPartition
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from mapinator.domain import DomainError
from mapinator.postgres import PostgresRepository
from mapinator.routing import Graph, build_graph
from mapinator.traffic import TrafficEvent
from mapinator.traffic_store import DLQ_TOPIC, LOCATION_TOPIC, UPDATE_TOPIC, TrafficStore

logger = logging.getLogger("mapinator")


async def initialize_topics(bootstrap_servers: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers, request_timeout_ms=5000)
    try:
        await admin.start()
        for topic in (LOCATION_TOPIC, UPDATE_TOPIC, DLQ_TOPIC):
            try:
                await admin.create_topics(
                    [
                        NewTopic(
                            topic,
                            num_partitions=3,
                            replication_factor=1,
                            topic_configs={
                                "retention.ms": "900000",
                                "segment.ms": "60000",
                                "cleanup.policy": "delete",
                            },
                        )
                    ]
                )
            except TopicAlreadyExistsError:
                logger.info(json.dumps({"event": "topic_exists", "topic": topic}))
            responses = await admin.alter_configs(
                [
                    ConfigResource(
                        ConfigResourceType.TOPIC,
                        topic,
                        configs={
                            "retention.ms": "900000",
                            "segment.ms": "60000",
                            "cleanup.policy": "delete",
                        },
                    )
                ]
            )
            for response in responses:
                if any(resource[0] != 0 for resource in response.resources):
                    raise KafkaError("Traffic topic retention configuration failed.")
    finally:
        await admin.close()


async def relay(store: TrafficStore, bootstrap_servers: str) -> None:
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        enable_idempotence=True,
        request_timeout_ms=5000,
    )
    last_heartbeat = 0.0
    try:
        await producer.start()
        while True:
            now = datetime.now(UTC)
            record = await asyncio.to_thread(store.claim, now)
            if record:
                identifier = record["event_id"]
                lease = record["lease_id"]
                from uuid import UUID

                if not isinstance(identifier, UUID) or not isinstance(lease, UUID):
                    raise ValueError("Invalid outbox lease.")
                try:
                    await asyncio.wait_for(
                        producer.send_and_wait(
                            str(record["topic"]),
                            json.dumps(record["payload"]).encode(),
                            key=str(record["partition_key"]).encode(),
                        ),
                        timeout=10,
                    )
                except (KafkaError, TimeoutError) as exc:
                    logger.error(
                        json.dumps({"event": "traffic_publish_failed", "error": type(exc).__name__})
                    )
                    await asyncio.to_thread(
                        store.failed, identifier, lease, datetime.now(UTC), type(exc).__name__
                    )
                else:
                    await asyncio.to_thread(store.delivered, identifier, lease)
            else:
                await asyncio.sleep(0.5)
            if asyncio.get_running_loop().time() - last_heartbeat >= 10:
                await asyncio.to_thread(store.heartbeat, "relay", datetime.now(UTC), 0)
                last_heartbeat = asyncio.get_running_loop().time()
    finally:
        await producer.stop()


async def analytics(
    store: TrafficStore, repository: PostgresRepository, bootstrap_servers: str
) -> None:
    consumer = AIOKafkaConsumer(
        LOCATION_TOPIC,
        bootstrap_servers=bootstrap_servers,
        group_id="mapinator-traffic-v1",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        request_timeout_ms=10000,
    )
    graph: Graph | None = None
    version = ""
    last_tick = 0.0
    lag = 0.0
    try:
        await consumer.start()
        while True:
            try:
                record = await asyncio.wait_for(consumer.getone(), timeout=1)
            except TimeoutError:
                record = None
            if record is not None:
                partition = TopicPartition(record.topic, record.partition)
                now = datetime.now(UTC)
                try:
                    try:
                        event = TrafficEvent.model_validate_json(record.value)
                    except ValidationError:
                        identifier = uuid5(
                            NAMESPACE_URL, f"{record.topic}:{record.partition}:{record.offset}"
                        )
                        await asyncio.to_thread(
                            store.reject, identifier, "invalid_event_schema", now
                        )
                    else:
                        _, dataset = await asyncio.to_thread(repository.routing_dataset)
                        if graph is None or version != dataset.checksum:
                            graph = await asyncio.to_thread(build_graph, dataset)
                            version = dataset.checksum
                        result = await asyncio.to_thread(store.consume, event, graph, version, now)
                        lag = max(0, (now - event.observed_at).total_seconds())
                        logger.info(
                            json.dumps(
                                {"event": "traffic_consumed", "result": result, "lag_s": lag}
                            )
                        )
                    await consumer.commit({partition: OffsetAndMetadata(record.offset + 1, "")})
                except (SQLAlchemyError, DomainError, KafkaError) as exc:
                    consumer.seek(partition, record.offset)
                    logger.error(
                        json.dumps({"event": "traffic_retry", "error": type(exc).__name__})
                    )
                    await asyncio.sleep(2)
            if asyncio.get_running_loop().time() - last_tick >= 30:
                _, dataset = await asyncio.to_thread(repository.routing_dataset)
                if graph is None or version != dataset.checksum:
                    graph = await asyncio.to_thread(build_graph, dataset)
                    version = dataset.checksum
                now = datetime.now(UTC)
                published = await asyncio.to_thread(store.aggregate_window, graph, version, now)
                await asyncio.to_thread(repository.cleanup, now)
                await asyncio.to_thread(store.heartbeat, "analytics", now, lag)
                logger.info(json.dumps({"event": "traffic_window", "updates": published}))
                last_tick = asyncio.get_running_loop().time()
    finally:
        await consumer.stop()


async def run_worker(role: str, repository: PostgresRepository, bootstrap_servers: str) -> None:
    store = TrafficStore(repository.engine)
    failures = 0
    while True:
        try:
            await asyncio.to_thread(repository.cleanup, datetime.now(UTC))
            if role == "relay":
                await relay(store, bootstrap_servers)
            else:
                await analytics(store, repository, bootstrap_servers)
        except (KafkaError, SQLAlchemyError, DomainError, OSError, TimeoutError) as exc:
            failures += 1
            delay = min(30, 2 ** min(failures, 5)) + random.random()
            logger.error(
                json.dumps(
                    {
                        "event": "traffic_worker_restart",
                        "role": role,
                        "error": type(exc).__name__,
                        "backoff_s": delay,
                    }
                )
            )
            await asyncio.sleep(delay)
