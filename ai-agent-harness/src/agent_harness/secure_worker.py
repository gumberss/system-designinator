import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

from agent_harness.adapters.calculator import calculator_tool
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.redis_circuit import RedisCircuitBreaker
from agent_harness.adapters.redis_signals import RedisSignals
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.domain import HarnessError
from agent_harness.hosting import configure_providers
from agent_harness.models import ModelExecutor
from agent_harness.runtime import Runtime
from agent_harness.security import ServerConfig
from agent_harness.telemetry import telemetry_environment
from agent_harness.tools import ToolExecutor, ToolRegistry
from agent_harness.worker import Worker


async def run(concurrency: int) -> None:
    config = ServerConfig.model_validate_json(Path(os.environ["HARNESS_SERVER_CONFIG"]).read_text(encoding="utf-8"))
    store = PostgresStore(os.environ["HARNESS_WORKER_DATABASE_URL"])
    security = SecurityRepository(store)
    await security.verify_api_role(worker=True)
    signals = RedisSignals(os.environ["HARNESS_REDIS_URL"])
    try:
        registry = ToolRegistry()
        registry.register(calculator_tool())
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            providers, scopes = configure_providers(client, config.models.values())
            breaker = RedisCircuitBreaker(signals.client)
            runtime = Runtime(
                providers, tools=ToolExecutor(
                    registry, breaker=breaker, authorization=security.authorize_execution,
                    approvals_enabled=True,
                ),
                models=ModelExecutor(providers, breaker=breaker, dependency_scopes=scopes,
                                     authorization=security.authorize_execution),
            )
            async with asyncio.TaskGroup() as group:
                for _ in range(concurrency):
                    group.create_task(Worker(
                        store, runtime, providers, signals=signals, security=security, managed_only=True,
                    ).run_forever())
    finally:
        await signals.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed worker with live authorization and pessimistic quotas.")
    parser.add_argument("--concurrency", type=int, default=2, choices=range(1, 17))
    args = parser.parse_args()
    required = ("HARNESS_WORKER_DATABASE_URL", "HARNESS_REDIS_URL", "HARNESS_SERVER_CONFIG")
    if any(not os.environ.get(key) for key in required):
        parser.error("Configure " + ", ".join(required))
    try:
        with telemetry_environment():
            if sys.platform == "win32":
                asyncio.run(run(args.concurrency), loop_factory=asyncio.SelectorEventLoop)
            else:
                asyncio.run(run(args.concurrency))
    except (HarnessError, ValidationError, OSError):
        parser.exit(1, "Managed worker configuration or dependency validation failed.\n")
    except KeyboardInterrupt:
        parser.exit(130, "Worker interrupted.\n")
