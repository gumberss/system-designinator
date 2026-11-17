import asyncio
import os
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from agent_harness.adapters.rate_limit import RateLimiter
from agent_harness.domain import HarnessError
from agent_harness.security import Identity


@pytest.mark.parametrize("reply,code", [
    (1, None), (0, "rate_limit_exceeded"), (2, "coordination_unavailable"),
    ("1", "coordination_unavailable"), (True, "coordination_unavailable"),
])
async def test_rate_limit_contract_is_strict_and_keys_do_not_contain_subject(reply: object, code: str | None) -> None:
    client = AsyncMock()
    client.eval.return_value = reply
    identity = Identity(uuid4(), "sensitive@example.invalid")
    limiter = RateLimiter(client, 600, 120)
    if code is None:
        await limiter.check(identity)
    else:
        with pytest.raises(HarnessError) as error:
            await limiter.check(identity)
        assert error.value.code == code
    args = client.eval.call_args.args
    assert identity.subject not in str(args)
    assert args[1] == 2 and "{" + str(identity.tenant_id) + "}" in args[2]


async def test_rate_limit_dependency_outage_is_fail_closed() -> None:
    client = AsyncMock()
    client.eval.side_effect = ConnectionError("private Redis password diagnostic")
    with pytest.raises(HarnessError) as error:
        await RateLimiter(client, 600, 120).check(Identity(uuid4(), "alice"))
    assert error.value.code == "coordination_unavailable"
    assert "password" not in error.value.message


@pytest.mark.integration
@pytest.mark.parametrize("tenant_limit,subject_limit", [(7, 100), (100, 3)])
async def test_real_redis_concurrent_fixed_window_limits(tenant_limit: int, subject_limit: int) -> None:
    url = os.environ.get("HARNESS_TEST_REDIS_URL")
    if not url:
        pytest.skip("HARNESS_TEST_REDIS_URL is required for real Redis rate-limit integration.")
    if urlparse(url).path != "/15":
        pytest.fail("Use only isolated Redis database 15.")
    client = Redis.from_url(url, decode_responses=True, socket_timeout=3)
    identity = Identity(uuid4(), "rate-test")
    prefix = "harness:rate:{" + str(identity.tenant_id) + "}"
    keys: list[str] = []
    try:
        limiter = RateLimiter(client, tenant_limit, subject_limit)
        results = await asyncio.gather(*(limiter.check(identity) for _ in range(40)), return_exceptions=True)
        assert all(item is None or isinstance(item, HarnessError) and item.code == "rate_limit_exceeded" for item in results)
        keys = [key async for key in client.scan_iter(match=prefix + "*")]
        tenant_total = 0
        for key in keys:
            stored = await client.get(key)
            assert isinstance(stored, str)
            value = int(stored)
            if key.count(":") == prefix.count(":") + 1:
                assert 0 < value <= tenant_limit
                tenant_total += value
            else:
                assert 0 < value <= subject_limit
            assert 0 < await client.ttl(key) <= 120
        assert tenant_total == sum(item is None for item in results) > 0
    finally:
        if not keys:
            keys = [key async for key in client.scan_iter(match=prefix + "*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
