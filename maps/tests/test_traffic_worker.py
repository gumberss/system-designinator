import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from aiokafka.errors import KafkaError, TopicAlreadyExistsError

from mapinator.traffic_store import TrafficStore
from mapinator.traffic_worker import initialize_topics, relay


def test_existing_topics_get_retention_configured_and_admin_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[str] = []
    closed = []

    @dataclass
    class Response:
        resources: list[tuple[int, str, int, str]]

    class Admin:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            pass

        async def create_topics(self, topics: object) -> None:
            raise TopicAlreadyExistsError()

        async def alter_configs(self, resources: list[object]) -> list[Response]:
            configured.append("configured")
            return [Response([(0, "", 2, "topic")])]

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("mapinator.traffic_worker.AIOKafkaAdminClient", Admin)
    asyncio.run(initialize_topics("test-only-broker"))
    assert len(configured) == 3 and closed == [True]


def test_topic_config_error_is_not_success_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class Response:
        resources: list[tuple[int, str, int, str]]

    class Admin:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            pass

        async def create_topics(self, topics: object) -> None:
            pass

        async def alter_configs(self, resources: list[object]) -> list[Response]:
            return [Response([(42, "failed", 2, "topic")])]

        async def close(self) -> None:
            pass

    monkeypatch.setattr("mapinator.traffic_worker.AIOKafkaAdminClient", Admin)
    with pytest.raises(KafkaError, match="retention"):
        asyncio.run(initialize_topics("test-only-broker"))


def test_publish_retry_starts_at_failure_not_claim_time(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed_at = datetime(2026, 11, 15, tzinfo=UTC)
    failed_at = claimed_at + timedelta(seconds=10)
    clock = Mock()
    clock.now.side_effect = [claimed_at, failed_at]
    monkeypatch.setattr("mapinator.traffic_worker.datetime", clock)
    producer = Mock()
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock(side_effect=KafkaError("simulated send failure"))
    monkeypatch.setattr("mapinator.traffic_worker.AIOKafkaProducer", Mock(return_value=producer))
    store = Mock(spec=TrafficStore)
    store.claim.return_value = {
        "event_id": uuid4(),
        "lease_id": uuid4(),
        "topic": "fixture",
        "partition_key": "fixture",
        "payload": {},
    }
    store.failed.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(relay(store, "test-only-broker"))
    producer.stop.assert_awaited_once()
    assert store.failed.call_args.args[2] == failed_at
