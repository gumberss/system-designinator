import asyncio
import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol
from uuid import uuid4

from agent_harness.domain import Contract, HarnessError, RetryPolicy
from agent_harness.telemetry import telemetry
from pydantic import Field


TRANSIENT_PROVIDER_ERRORS = frozenset({
    "provider_rate_limited", "provider_timeout", "provider_unavailable",
})
logger = logging.getLogger(__name__)


def retry_after(value: str | None, *, now: float | None = None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = date.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            logger.warning("provider_retry_after_invalid")
            return None
    if not math.isfinite(seconds):
        logger.warning("provider_retry_after_invalid")
        return None
    return max(0, seconds)


def retry_delay(
    policy: RetryPolicy, failed_attempt: int, minimum: float | None = None,
    jitter: Callable[[], float] = random.random,
) -> float:
    sample = jitter()
    if not 0 <= sample <= 1:
        raise ValueError("Jitter must be in [0, 1].")
    return max(
        minimum or 0,
        sample * min(policy.max_delay_seconds, policy.base_delay_seconds * 2.0 ** (failed_attempt - 1)),
    )


class BreakerPolicy(Contract):
    failure_threshold: int = Field(default=5, ge=1, le=100)
    recovery_seconds: float = Field(default=30, gt=0, le=3600)
    probe_timeout_seconds: float = Field(default=310, gt=0, le=3600)


@dataclass(frozen=True)
class Permit:
    key: str
    generation: int
    token: str
    probe: bool


Outcome = Literal["success", "failure", "neutral"]


class CircuitOpen(HarnessError):
    def __init__(self) -> None:
        super().__init__("circuit_open", "Dependency circuit is open; retry after recovery.")


class CircuitBreaker(Protocol):
    async def acquire(self, key: str) -> Permit: ...

    async def finish(
        self, permit: Permit, outcome: Outcome, cooldown: float | None = None,
    ) -> None: ...


async def abandon_probe(breaker: CircuitBreaker, permit: Permit, remaining: float) -> None:
    if not permit.probe:
        return
    if remaining <= 0:
        logger.warning("circuit_probe_abandoned_until_lease_expiry")
        return
    try:
        async with asyncio.timeout(min(0.25, remaining)):
            await breaker.finish(permit, "neutral")
    except (HarnessError, TimeoutError):
        logger.warning("circuit_probe_cleanup_unavailable_until_lease_expiry")


@dataclass
class CircuitState:
    failures: int = 0
    generation: int = 0
    opened_until: float = 0
    probe_token: str | None = None
    probe_until: float = 0


class LocalCircuitBreaker:
    """Single-event-loop protection only; not distributed coordination."""

    def __init__(
        self, policy: BreakerPolicy | None = None, *, clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy or BreakerPolicy()
        self.clock = clock
        self._states: dict[str, CircuitState] = {}

    async def acquire(self, key: str) -> Permit:
        state = self._states.setdefault(key, CircuitState())
        now = self.clock()
        if state.opened_until > now or state.probe_until > now:
            telemetry().circuit("local", "blocked")
            raise CircuitOpen()
        probe = state.opened_until != 0
        token = uuid4().hex
        if probe:
            state.generation += 1
            state.probe_token = token
            state.probe_until = now + self.policy.probe_timeout_seconds
        telemetry().circuit("local", "probe" if probe else "granted")
        return Permit(key, state.generation, token, probe)

    async def finish(
        self, permit: Permit, outcome: Outcome, cooldown: float | None = None,
    ) -> None:
        state = self._states[permit.key]
        if state.generation != permit.generation or (
            permit.probe and (
                state.probe_token != permit.token or self.clock() >= state.probe_until
            )
        ):
            return
        if outcome == "success":
            state.failures = 0
            state.opened_until = 0
            state.probe_until = 0
            state.probe_token = None
        elif outcome == "failure":
            state.failures += 1
            if permit.probe or cooldown is not None or state.failures >= self.policy.failure_threshold:
                state.generation += 1
                state.opened_until = self.clock() + (
                    cooldown if cooldown is not None else self.policy.recovery_seconds
                )
                state.probe_until = 0
                state.probe_token = None
        elif permit.probe:
            state.generation += 1
            state.opened_until = self.clock() + self.policy.recovery_seconds
            state.probe_until = 0
            state.probe_token = None
