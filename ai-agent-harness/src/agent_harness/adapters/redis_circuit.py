import hashlib
from collections.abc import Awaitable
from importlib.resources import files

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent_harness.domain import HarnessError
from agent_harness.resilience import BreakerPolicy, CircuitOpen, Outcome, Permit
from uuid import uuid4
from agent_harness.telemetry import telemetry


class RedisCircuitBreaker:
    """Atomic circuit transitions use Redis server time and fenced probe permits."""

    def __init__(self, client: Redis, policy: BreakerPolicy | None = None) -> None:
        self.client = client
        self.policy = policy or BreakerPolicy()
        self.script = files("agent_harness.adapters").joinpath("circuit.lua").read_text()

    async def _run(
        self, key: str, action: str, generation: int, token: str,
        probe: bool, cooldown: float | None = None,
    ) -> tuple[int, int, int]:
        name = "harness:circuit:" + hashlib.sha256(key.encode()).hexdigest()
        try:
            pending = self.client.eval(
                self.script, 1, name, action, generation, token, int(probe),
                self.policy.failure_threshold, self.policy.recovery_seconds,
                self.policy.probe_timeout_seconds,
                -1 if cooldown is None else cooldown,
            )
            if not isinstance(pending, Awaitable):
                raise TypeError("An asynchronous Redis client is required.")
            result = await pending
        except RedisError:
            raise HarnessError(
                "coordination_unavailable", "Distributed circuit coordination is unavailable.",
            ) from None
        try:
            return TypeAdapter(tuple[int, int, int]).validate_python(result, strict=False)
        except ValidationError:
            raise HarnessError("coordination_invalid_response", "Circuit coordination returned invalid data.") from None

    async def acquire(self, key: str) -> Permit:
        token = uuid4().hex
        allowed, generation, probe = await self._run(key, "acquire", 0, token, False)
        if allowed != 1:
            telemetry().circuit("redis", "blocked")
            raise CircuitOpen()
        telemetry().circuit("redis", "probe" if probe == 1 else "granted")
        return Permit(key, generation, token, probe == 1)

    async def finish(
        self, permit: Permit, outcome: Outcome, cooldown: float | None = None,
    ) -> None:
        await self._run(permit.key, outcome, permit.generation, permit.token, permit.probe, cooldown)
