import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from agent_harness.domain import HarnessError


T = TypeVar("T")


class OperationCancelled(HarnessError):
    def __init__(self) -> None:
        super().__init__("cancelled", "Execution was cancelled.")


async def run_interruptible(
    operation: Callable[[], Awaitable[T]],
    cancellation: asyncio.Event,
    timeout: float,
    timeout_error: HarnessError,
) -> T:
    if cancellation.is_set():
        raise OperationCancelled()
    if timeout <= 0:
        raise timeout_error
    running = asyncio.ensure_future(operation())
    cancelled = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            (running, cancelled), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if running in done:
            return await running
        if cancelled in done:
            raise OperationCancelled()
        raise timeout_error
    finally:
        running.cancel()
        cancelled.cancel()
        await asyncio.gather(running, cancelled, return_exceptions=True)
