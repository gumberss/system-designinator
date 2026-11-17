import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from agent_harness.domain import RetryPolicy
from agent_harness.async_utils import run_interruptible
from agent_harness.domain import HarnessError
from agent_harness.resilience import (
    BreakerPolicy, CircuitOpen, LocalCircuitBreaker, retry_after, retry_delay,
)


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), ("2", 2), ("0", 0), ("-2", 0), ("NaN", None), ("Infinity", None),
    ("invalid", None), ("1.5", 1.5),
])
def test_retry_after_numeric_and_invalid(value: str | None, expected: float | None) -> None:
    assert retry_after(value, now=100) == expected


def test_retry_after_http_date() -> None:
    header = format_datetime(datetime.fromtimestamp(120, UTC), usegmt=True)
    assert retry_after(header, now=100) == 20
    assert retry_after(header, now=130) == 0


def test_full_jitter_is_exponential_capped_and_respects_server_minimum() -> None:
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=3)
    assert [retry_delay(policy, i, jitter=lambda: 0.5) for i in range(1, 5)] == [0.5, 1, 1.5, 1.5]
    assert retry_delay(policy, 1, minimum=12, jitter=lambda: 0) == 12
    with pytest.raises(ValueError):
        retry_delay(policy, 1, jitter=lambda: 2)


@pytest.mark.parametrize("timeout", [0, -1])
async def test_expired_operation_never_starts_even_before_first_event_loop_yield(timeout: float) -> None:
    called = False

    async def effect() -> None:
        nonlocal called
        called = True

    with pytest.raises(HarnessError) as error:
        await run_interruptible(
            effect, asyncio.Event(), timeout, HarnessError("expired", "Already expired"),
        )
    assert error.value.code == "expired" and not called


async def test_breaker_opens_and_allows_only_one_half_open_probe() -> None:
    clock = [100.0]
    breaker = LocalCircuitBreaker(
        BreakerPolicy(failure_threshold=2, recovery_seconds=5), clock=lambda: clock[0],
    )
    for _ in range(2):
        await breaker.finish(await breaker.acquire("provider:a"), "failure")
    with pytest.raises(CircuitOpen):
        await breaker.acquire("provider:a")
    assert not (await breaker.acquire("provider:b")).probe
    clock[0] += 5
    probes = await asyncio.gather(
        *(breaker.acquire("provider:a") for _ in range(16)), return_exceptions=True,
    )
    allowed = [probe for probe in probes if not isinstance(probe, BaseException)]
    assert len(allowed) == 1 and allowed[0].probe
    assert sum(isinstance(probe, CircuitOpen) for probe in probes) == 15
    await breaker.finish(allowed[0], "success")
    assert not (await breaker.acquire("provider:a")).probe


async def test_stale_success_and_expired_probe_cannot_close_a_new_circuit() -> None:
    clock = [100.0]
    breaker = LocalCircuitBreaker(
        BreakerPolicy(failure_threshold=1, recovery_seconds=2, probe_timeout_seconds=3),
        clock=lambda: clock[0],
    )
    old = await breaker.acquire("service")
    await breaker.finish(await breaker.acquire("service"), "failure")
    await breaker.finish(old, "success")
    with pytest.raises(CircuitOpen):
        await breaker.acquire("service")
    clock[0] += 2
    first = await breaker.acquire("service")
    clock[0] += 3
    await breaker.finish(first, "success")
    replacement = await breaker.acquire("service")
    assert replacement.probe and replacement.generation > first.generation
    await breaker.finish(first, "success")
    with pytest.raises(CircuitOpen):
        await breaker.acquire("service")
    await breaker.finish(replacement, "failure")
    with pytest.raises(CircuitOpen):
        await breaker.acquire("service")


async def test_retry_after_cooldown_and_cancelled_probe() -> None:
    clock = [100.0]
    breaker = LocalCircuitBreaker(clock=lambda: clock[0])
    await breaker.finish(await breaker.acquire("service"), "failure", cooldown=2)
    clock[0] += 1
    with pytest.raises(CircuitOpen):
        await breaker.acquire("service")
    clock[0] += 1
    probe = await breaker.acquire("service")
    await breaker.finish(probe, "neutral")
    with pytest.raises(CircuitOpen):
        await breaker.acquire("service")
