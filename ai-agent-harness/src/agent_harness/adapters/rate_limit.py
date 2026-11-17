import hashlib
from collections.abc import Awaitable

from redis.asyncio import Redis
from redis.exceptions import RedisError
from pydantic import TypeAdapter, ValidationError

from agent_harness.domain import HarnessError
from agent_harness.security import Identity
from agent_harness.resilience import LocalCircuitBreaker, Outcome


SCRIPT = """
local now = redis.call('TIME')
local window = math.floor(tonumber(now[1]) / 60)
local tenant = KEYS[1] .. ':' .. window
local subject = KEYS[2] .. ':' .. window
local a = tonumber(redis.call('GET',tenant)) or 0
local b = tonumber(redis.call('GET',subject)) or 0
if a >= tonumber(ARGV[1]) or b >= tonumber(ARGV[2]) then return 0 end
redis.call('INCR',tenant)
redis.call('INCR',subject)
redis.call('EXPIRE',tenant,120)
redis.call('EXPIRE',subject,120)
return 1
"""


class RateLimiter:
    def __init__(self, client: Redis, tenant_limit: int, subject_limit: int) -> None:
        self.client, self.tenant_limit, self.subject_limit = client, tenant_limit, subject_limit
        self.breaker = LocalCircuitBreaker()

    async def check(self, identity: Identity) -> None:
        key = "harness:rate:{" + str(identity.tenant_id) + "}"
        subject = hashlib.sha256(identity.subject.encode()).hexdigest()
        permit = await self.breaker.acquire("redis:rate")
        outcome: Outcome = "failure"
        try:
            pending = self.client.eval(SCRIPT, 2, key, key + ":" + subject, self.tenant_limit, self.subject_limit)
            if not isinstance(pending, Awaitable):
                raise TypeError("An asynchronous Redis client is required.")
            accepted = TypeAdapter(int).validate_python(await pending, strict=True)
            if accepted in (0, 1):
                outcome = "success"
        except RedisError:
            raise HarnessError("coordination_unavailable", "Request limit coordination is unavailable.") from None
        except ValidationError:
            raise HarnessError("coordination_unavailable", "Request limit coordination returned invalid data.") from None
        finally:
            await self.breaker.finish(permit, outcome)
        if accepted == 0:
            raise HarnessError("rate_limit_exceeded", "Request rate limit exceeded.")
        if accepted != 1:
            raise HarnessError("coordination_unavailable", "Request limit coordination returned invalid data.")
