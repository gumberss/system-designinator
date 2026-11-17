import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID
from dataclasses import asdict

import httpx
from pydantic import Field, ValidationError

from agent_harness.adapters.calculator import calculator_tool
from agent_harness.adapters.redis_circuit import RedisCircuitBreaker
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.redis_signals import RedisSignals
from agent_harness.adapters.workflow_postgres import WorkflowStore
from agent_harness.domain import Agent, Contract, HarnessError, Principal
from agent_harness.hosting import configure_providers
from agent_harness.models import ModelExecutor
from agent_harness.resilience import BreakerPolicy
from agent_harness.runtime import Runtime
from agent_harness.state import ENVELOPE, LeaseLost
from agent_harness.tools import ToolExecutor, ToolRegistry
from agent_harness.worker import Worker
from agent_harness.workflows import WORKFLOW, WorkflowDefinition
from agent_harness.audit import execution_metrics
from agent_harness.telemetry import telemetry_environment


class HostConfig(Contract):
    tenant_id: UUID
    subject: str = Field(min_length=1)
    agents: tuple[Agent, ...] = Field(min_length=1)
    allowed_tools: frozenset[str] = frozenset()
    circuit_breaker: BreakerPolicy = Field(default_factory=BreakerPolicy)


async def run(args: argparse.Namespace) -> None:
    store = PostgresStore(os.environ["HARNESS_DATABASE_URL"])
    if args.command == "migrate":
        await store.migrate()
        print("Migrations 1-5 applied or already current.")
        return
    config = HostConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
    if any(agent.tenant_id != config.tenant_id for agent in config.agents):
        raise HarnessError("invalid_config", "All configured agents must belong to the configured tenant.")
    principal = Principal(
        config.tenant_id, config.subject, frozenset(agent.id for agent in config.agents),
        config.allowed_tools,
    )
    if args.command == "session":
        print(await store.create_session(principal))
    elif args.command == "status":
        print(ENVELOPE.dump_json(await store.get(principal, args.id), indent=2).decode())
    elif args.command == "metrics":
        print(json.dumps(execution_metrics((await store.get(principal, args.id)).execution), indent=2))
    elif args.command == "audit":
        entries = await store.audit(principal, args.id, after=args.after, limit=args.limit)
        print(json.dumps([asdict(entry) for entry in entries], default=str, indent=2))
    elif args.command == "cancel":
        await store.cancel(principal, args.id)
        print("Cancellation requested.")
    elif args.command == "workflow-status":
        print(WORKFLOW.dump_json(await WorkflowStore(store).get(principal, args.id), indent=2).decode())
    elif args.command == "memory":
        await store.put_memory(principal, args.session, args.key, args.content, args.ttl)
        print("Session memory saved.")
    else:
        signals = RedisSignals(os.environ["HARNESS_REDIS_URL"])
        try:
            if args.command == "submit":
                agent = next((agent for agent in config.agents if agent.id == args.agent), None)
                if agent is None:
                    raise HarnessError("agent_not_configured", "Agent is not configured in this host.")
                identifier = await store.submit(
                    agent, principal, args.session, args.input, args.idempotency_key
                )
                print(identifier)
                print(f"notification_delivered={await signals.notify()}")
            elif args.command == "workflow":
                definition = WorkflowDefinition.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
                identifier = await WorkflowStore(store).submit(definition, principal, args.id)
                print(identifier)
                print(f"notification_delivered={await signals.notify()}")
            elif args.command == "worker":
                registry = ToolRegistry()
                if config.allowed_tools:
                    tool = calculator_tool()
                    if not config.allowed_tools <= {tool.definition.name}:
                        raise HarnessError("tool_not_installed", "The host only installs the calculator tool.")
                    registry.register(tool)
                    registry.set_tenant_grant(config.tenant_id, tool.definition.name, True)
                async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                    providers, scopes = configure_providers(client, (
                        selection for agent in config.agents
                        for selection in (agent.selection, *agent.fallbacks)
                    ))
                    breaker = RedisCircuitBreaker(signals.client, config.circuit_breaker)
                    runtime = Runtime(
                        providers, tools=ToolExecutor(registry, breaker=breaker),
                        models=ModelExecutor(providers, breaker=breaker, dependency_scopes=scopes),
                    )
                    async with asyncio.TaskGroup() as group:
                        for _ in range(args.concurrency):
                            group.create_task(Worker(
                                store, runtime, providers, signals=signals,
                                tenant_id=config.tenant_id,
                            ).run_forever())
        finally:
            await signals.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted local durable harness host (not a public API).")
    parser.add_argument("--config", help="Trusted JSON host configuration; required except for migrate.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("session")
    submit = commands.add_parser("submit")
    submit.add_argument("--agent", type=UUID, required=True)
    submit.add_argument("--session", type=UUID, required=True)
    submit.add_argument("--input", required=True)
    submit.add_argument("--idempotency-key", required=True)
    for name in ("status", "cancel", "workflow-status", "metrics"):
        commands.add_parser(name).add_argument("id", type=UUID)
    audit = commands.add_parser("audit")
    audit.add_argument("id", type=UUID)
    audit.add_argument("--after", type=int, default=0)
    audit.add_argument("--limit", type=int, default=100, choices=range(1, 501))
    workflow = commands.add_parser("workflow")
    workflow.add_argument("file")
    workflow.add_argument("--id", type=UUID, default=None)
    worker = commands.add_parser("worker")
    worker.add_argument("--concurrency", type=int, default=2, choices=range(1, 17))
    memory = commands.add_parser("memory")
    memory.add_argument("--session", type=UUID, required=True)
    memory.add_argument("--key", required=True)
    memory.add_argument("--content", required=True)
    memory.add_argument("--ttl", type=int, default=86400)
    args = parser.parse_args()
    required = ["HARNESS_DATABASE_URL"]
    if args.command != "migrate" and args.config is None:
        parser.error("--config is required for this operation.")
    if args.command in ("worker", "submit", "workflow"):
        required.append("HARNESS_REDIS_URL")
    if any(not os.environ.get(key) for key in required):
        parser.error("Configure the required environment variables: " + ", ".join(required))
    try:
        with telemetry_environment():
            if sys.platform == "win32":
                asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
            else:
                asyncio.run(run(args))
    except ValidationError:
        parser.exit(2, "Configuration does not match the required schema.\n")
    except HarnessError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except LeaseLost:
        parser.exit(1, "lease_lost: this worker no longer owns the execution.\n")
    except KeyboardInterrupt:
        parser.exit(130, "Worker interrupted.\n")
