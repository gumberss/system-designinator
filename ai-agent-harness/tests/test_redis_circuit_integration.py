import asyncio
import hashlib
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from agent_harness.adapters.redis_circuit import RedisCircuitBreaker
from agent_harness.resilience import BreakerPolicy, CircuitOpen, Permit


pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def redis_client() -> Redis:
    url = os.environ.get("HARNESS_TEST_REDIS_URL")
    if not url:
        pytest.skip("HARNESS_TEST_REDIS_URL is required for real Redis circuit integration.")
    if urlparse(url).path != "/15":
        pytest.fail("Circuit integration tests require isolated Redis database 15.")
    return Redis.from_url(url, decode_responses=True, socket_timeout=3)


async def test_atomic_cross_worker_half_open_and_fenced_probe(redis_client: Redis) -> None:
    key = "test-circuit:" + uuid4().hex
    policy = BreakerPolicy(failure_threshold=1, recovery_seconds=0.05, probe_timeout_seconds=2)
    a, b = RedisCircuitBreaker(redis_client, policy), RedisCircuitBreaker(redis_client, policy)
    try:
        stale = await a.acquire(key)
        await a.finish(await a.acquire(key), "failure")
        await a.finish(stale, "success")
        with pytest.raises(CircuitOpen):
            await b.acquire(key)
        await asyncio.sleep(0.06)
        attempts = await asyncio.gather(
            *(instance.acquire(key) for instance in (a, b) * 8), return_exceptions=True,
        )
        probes = [item for item in attempts if isinstance(item, Permit)]
        assert len(probes) == 1 and probes[0].probe
        await asyncio.sleep(2.01)
        replacement = await b.acquire(key)
        await a.finish(probes[0], "success")
        with pytest.raises(CircuitOpen):
            await a.acquire(key)
        await b.finish(replacement, "success")
        assert not (await a.acquire(key)).probe
    finally:
        await redis_client.delete("harness:circuit:" + hashlib.sha256(key.encode()).hexdigest())
        await redis_client.aclose()


async def test_redis_cooldown_does_not_block_an_independent_dependency(redis_client: Redis) -> None:
    keys = ("test:" + uuid4().hex, "test:" + uuid4().hex)
    breaker = RedisCircuitBreaker(redis_client, BreakerPolicy(recovery_seconds=0.02))
    try:
        await breaker.finish(await breaker.acquire(keys[0]), "failure", cooldown=0.1)
        with pytest.raises(CircuitOpen):
            await breaker.acquire(keys[0])
        assert not (await breaker.acquire(keys[1])).probe
        await asyncio.sleep(0.11)
        assert (await breaker.acquire(keys[0])).probe
    finally:
        await redis_client.delete(*(
            "harness:circuit:" + hashlib.sha256(key.encode()).hexdigest() for key in keys
        ))
        await redis_client.aclose()
