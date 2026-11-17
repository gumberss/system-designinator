import argparse
import asyncio
import os
from uuid import uuid4

import httpx
from pydantic import ValidationError

from agent_harness.adapters.calculator import calculator_tool
from agent_harness.domain import Agent, HarnessError, ModelSelection, Principal, RetryPolicy, Status
from agent_harness.hosting import configure_providers
from agent_harness.models import ModelExecutor
from agent_harness.runtime import Runtime
from agent_harness.tools import ToolExecutor, ToolRegistry
from agent_harness.telemetry import telemetry_environment


def main() -> None:
    parser = argparse.ArgumentParser(description="Local single-process agent execution.")
    parser.add_argument("input", help="Task to send to the provider (avoid sensitive data).")
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--enable-calculator", action="store_true",
        help="Explicitly grant the local read-only integer addition tool.",
    )
    args = parser.parse_args()
    required = (
        args.provider.upper() + "_API_KEY", "MODEL_INPUT_USD_PER_MILLION", "MODEL_OUTPUT_USD_PER_MILLION",
    )
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        parser.error("Missing environment variables: " + ", ".join(missing))
    try:
        selection = ModelSelection(
            provider=args.provider,
            model=args.model or (
                "gpt-4.1-mini" if args.provider == "openai" else "claude-sonnet-4-5-20250929"
            ),
            input_usd_per_million=os.environ["MODEL_INPUT_USD_PER_MILLION"],
            output_usd_per_million=os.environ["MODEL_OUTPUT_USD_PER_MILLION"],
        )
    except ValidationError:
        parser.error("Model and nonnegative finite USD prices must be configured correctly.")
    try:
        with telemetry_environment():
            raise SystemExit(asyncio.run(_run(args.input, selection, calculator=args.enable_calculator)))
    except HarnessError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Execution interrupted.\n")


async def _run(user_input: str, selection: ModelSelection, *, calculator: bool = False) -> int:
    tenant_id, agent_id = uuid4(), uuid4()
    registry = ToolRegistry()
    allowed_tools: frozenset[str] = frozenset()
    if calculator:
        tool = calculator_tool()
        registry.register(tool)
        registry.set_tenant_grant(tenant_id, tool.definition.name, True)
        allowed_tools = frozenset({tool.definition.name})
    agent = Agent(
        id=agent_id,
        tenant_id=tenant_id,
        instructions="Complete the user's task accurately and concisely.",
        selection=selection,
        retry=RetryPolicy(),
        allowed_tools=allowed_tools,
    )
    principal = Principal(tenant_id, "local-cli", frozenset({agent_id}), allowed_tools)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        providers, scopes = configure_providers(client, (selection,))
        runtime = Runtime(
            providers, models=ModelExecutor(providers, dependency_scopes=scopes),
            tools=ToolExecutor(registry),
        )
        execution = await runtime.execute(agent, principal, uuid4(), user_input)
    if execution.status == Status.SUCCEEDED:
        print(execution.result)
    else:
        failure = execution.failure
        print(f"{execution.status}: {failure.code if failure else 'cancelled'}")
    print(
        f"input_tokens={execution.input_tokens} output_tokens={execution.output_tokens} "
        f"estimated_cost_usd={execution.estimated_cost_usd} "
        f"usage_complete={execution.usage_complete}"
    )
    return 0 if execution.status == Status.SUCCEEDED else 1
