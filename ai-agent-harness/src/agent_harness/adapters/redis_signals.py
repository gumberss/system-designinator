import logging
from collections.abc import Awaitable

from redis.asyncio import Redis
from redis.exceptions import ConnectionError, TimeoutError
from agent_harness.resilience import CircuitOpen, LocalCircuitBreaker


logger = logging.getLogger(__name__)


class RedisSignals:
    def __init__(self, url: str) -> None:
        self.client = Redis.from_url(
            url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3,
        )
        self._breaker = LocalCircuitBreaker()

    async def notify(self) -> bool:
        try:
            permit = await self._breaker.acquire("redis_signals")
            async with self.client.pipeline() as pipeline:
                pipeline.lpush("harness:wake", "work")
                pipeline.ltrim("harness:wake", 0, 1023)
                pipeline.expire("harness:wake", 60)
                await pipeline.execute()
            await self._breaker.finish(permit, "success")
            return True
        except (ConnectionError, TimeoutError):
            await self._breaker.finish(permit, "failure")
            logger.warning("redis_notification_unavailable")
            return False
        except CircuitOpen:
            logger.warning("redis_signal_circuit_open")
            return False

    async def wait(self) -> bool:
        try:
            permit = await self._breaker.acquire("redis_signals")
            pending = self.client.blpop("harness:wake", timeout=1)
            if not isinstance(pending, Awaitable):
                raise TypeError("The configured Redis client must be asynchronous.")
            delivered = await pending is not None
            await self._breaker.finish(permit, "success")
            return delivered
        except (ConnectionError, TimeoutError):
            await self._breaker.finish(permit, "failure")
            logger.warning("redis_signal_unavailable_using_postgres_polling")
            return False
        except CircuitOpen:
            logger.warning("redis_signal_circuit_open_using_postgres_polling")
            return False

    async def close(self) -> None:
        await self.client.aclose()
