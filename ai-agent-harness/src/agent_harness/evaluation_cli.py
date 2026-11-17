import argparse
import asyncio
import ipaddress
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from agent_harness.domain import Agent, HarnessError
from agent_harness.evaluation import (
    EvaluationCase, EvaluationReport, EvaluationSample, ExecutionArtifact, Gates, TERMINAL,
    evaluate, load_dataset, read_jsonl, regressions,
)
from agent_harness.json_codec import decode_object
from agent_harness.security import reservation


async def request(client: httpx.AsyncClient, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    try:
        async with client.stream(method, path, json=body) as response:
            if response.status_code not in (200, 201, 202, 204):
                raise HarnessError("evaluation_http_error", "Managed API rejected an evaluation operation.")
            if response.status_code == 204:
                return {}
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1048576:
                    raise HarnessError("evaluation_http_error", "Managed API response exceeds the limit.")
            return decode_object(content.decode("utf-8"))
    except httpx.RequestError:
        raise HarnessError("evaluation_http_error", "Managed API request failed.") from None


def bounded_agent(agent: Agent) -> Agent:
    return agent.model_copy(update={"limits": agent.limits.model_copy(update={
        "max_model_calls": min(8, agent.limits.max_model_calls),
        "max_steps": min(8, agent.limits.max_steps),
        "context_window_tokens": min(8192, agent.limits.context_window_tokens),
        "max_output_tokens": min(256, agent.limits.max_output_tokens),
    })})


async def run_case(client: httpx.AsyncClient, case: EvaluationCase, agent: Agent, repetition: int, timeout: float) -> EvaluationSample:
    identifier: UUID | None = None
    completed = False
    try:
        async with asyncio.timeout(timeout):
            session = UUID(str((await request(client, "POST", "/v1/sessions"))["id"]))
            for key, content in case.memories.items():
                await request(client, "PUT", f"/v1/sessions/{session}/memory/{key}", {"content": content, "ttl_seconds": 3600})
            accepted = await client.post("/v1/executions", json={
                "agent_id": str(case.agent_id), "agent_version": case.agent_version, "session_id": str(session),
                "input": case.input, "limits": agent.limits.model_dump(mode="json"),
                **({"output_schema": case.output_schema} if case.output_schema is not None else {}),
            }, headers={"Idempotency-Key": "evaluation-" + uuid4().hex})
            if accepted.status_code != 202:
                raise HarnessError("evaluation_http_error", "Execution admission failed.")
            identifier = UUID(str(decode_object(accepted.text)["execution_id"]))
            while True:
                value = await request(client, "GET", f"/v1/executions/{identifier}")
                if value["status"] in TERMINAL:
                    artifact = ExecutionArtifact.model_validate(await request(
                        client, "GET", f"/v1/executions/{identifier}/evaluation-artifact",
                    ))
                    if (
                        artifact.execution_id != identifier or artifact.session_id != session
                        or artifact.tenant_id != agent.tenant_id
                        or (artifact.agent_id, artifact.agent_version) != (case.agent_id, case.agent_version)
                    ):
                        raise HarnessError("evaluation_target_changed", "Managed API returned another execution scope.")
                    completed = True
                    return EvaluationSample(case_id=case.case_id, repetition=repetition, artifact=artifact)
                await asyncio.sleep(.5)
    except TimeoutError:
        raise HarnessError("evaluation_timeout", "Evaluation timed out; cancellation was requested when execution ID was known.") from None
    finally:
        if identifier is not None and not completed:
            await request(client, "POST", f"/v1/executions/{identifier}/cancel")


def validate_url(value: str) -> str:
    url = httpx.URL(value)
    loopback = url.host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(url.host).is_loopback
    except ValueError:
        pass
    if (
        not url.host or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")
        or url.scheme not in ("http", "https") or url.scheme == "http" and not loopback
    ):
        raise HarnessError("invalid_evaluation", "Use HTTPS or loopback HTTP without URL credentials or query.")
    return str(url)


async def run_live(
    client: httpx.AsyncClient, cases: tuple[EvaluationCase, ...], *, repetitions: int,
    budget: Decimal, timeout: float, artifacts: Path, gates: Gates = Gates(), allow_controlled_fixtures: bool = False,
) -> EvaluationReport:
    if not cases or len(cases) > 100 or not budget.is_finite() or budget <= 0 or not 1 <= timeout <= 3600 or not 1 <= repetitions <= 10:
        raise HarnessError("invalid_evaluation", "Live evaluation requires bounded budget, repetitions and timeout.")
    if not allow_controlled_fixtures and any("controlled_fixture" in case.tags for case in cases):
        raise HarnessError("invalid_evaluation", "Dataset requires explicitly provisioned controlled fixtures.")
    agents: dict[tuple[UUID, int], Agent] = {}
    projected = Decimal(0)
    for case in cases:
        key = case.agent_id, case.agent_version
        if key not in agents:
            agent = Agent.model_validate(await request(client, "GET", f"/v1/agents/{case.agent_id}?version={case.agent_version}"))
            if (agent.id, agent.version) != key:
                raise HarnessError("evaluation_target_changed", "Managed API returned another agent version.")
            agents[key] = bounded_agent(agent)
        agent = agents[key]
        if case.output_schema is not None and agent.result_schema not in (None, case.output_schema):
            raise HarnessError("invalid_evaluation", "Dataset cannot replace agent output policy.")
        projected += reservation(agent)[1] * repetitions
    if projected > budget:
        raise HarnessError("evaluation_budget_exceeded", "Pessimistic reservation exceeds the evaluation budget.")
    samples = []
    with artifacts.open("x", encoding="utf-8") as output:
        for case in cases:
            for repetition in range(1, repetitions + 1):
                sample = await run_case(client, case, agents[(case.agent_id, case.agent_version)], repetition, timeout)
                output.write(sample.model_dump_json() + "\n")
                output.flush()
                samples.append(sample)
                if not sample.artifact.usage_complete or sum(
                    (item.artifact.estimated_cost_usd for item in samples), Decimal(0)
                ) > budget:
                    raise HarnessError("evaluation_budget_uncertain", "Usage is incomplete or reported spend exceeds budget; evaluation stopped.")
    return evaluate(cases, tuple(samples), repetitions=repetitions, gates=gates, source="live_api")


def read_report(path: Path) -> EvaluationReport:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise HarnessError("invalid_evaluation", "Report exceeds the byte limit.")
    return EvaluationReport.model_validate(decode_object(path.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description="Score recorded executions or explicitly run budgeted managed API evaluations.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--dataset", type=Path, required=True)
    for name in ("score", "run"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", type=Path, required=True)
        command.add_argument("--artifacts", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--repetitions", type=int, default=1)
        command.add_argument("--tool-f1-min", type=float, default=.95)
        command.add_argument("--agent-version", type=int)
        if name == "run":
            command.add_argument("--base-url", required=True)
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--max-cost-usd", type=Decimal, required=True)
            command.add_argument("--timeout-seconds", type=float, default=120)
            command.add_argument("--allow-controlled-fixtures", action="store_true")
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "compare":
            failures = regressions(read_report(args.baseline), read_report(args.candidate))
            print(json.dumps({"passed": not failures, "regressions": failures}))
            if failures:
                parser.exit(1)
            return
        cases = load_dataset(args.dataset)
        if args.command == "validate":
            print(json.dumps({"valid": True, "cases": len(cases)}))
            return
        if args.agent_version is not None:
            if args.agent_version < 1:
                raise HarnessError("invalid_evaluation", "Agent version must be positive.")
            cases = tuple(case.model_copy(update={"agent_version": args.agent_version}) for case in cases)
        if args.out.exists() or args.out.resolve() in (args.artifacts.resolve(), args.dataset.resolve()):
            raise HarnessError("invalid_evaluation", "Output must be a new file distinct from dataset and artifacts.")
        gates = Gates(tool_f1_min=args.tool_f1_min)
        if args.command == "score":
            samples = tuple(EvaluationSample.model_validate(row) for row in read_jsonl(args.artifacts))
            report = evaluate(cases, samples, repetitions=args.repetitions, gates=gates)
        else:
            token = os.environ.get("HARNESS_EVAL_ACCESS_TOKEN")
            if not args.allow_live or not token or args.artifacts.exists():
                raise HarnessError("invalid_evaluation", "Live execution requires explicit consent, token environment and a new artifact file.")
            base_url = validate_url(args.base_url)

            async def run() -> EvaluationReport:
                async with httpx.AsyncClient(
                    base_url=base_url, headers={"Authorization": "Bearer " + token},
                    follow_redirects=False, trust_env=False, timeout=10,
                ) as client:
                    return await run_live(
                        client, cases, repetitions=args.repetitions, budget=args.max_cost_usd,
                        timeout=args.timeout_seconds, artifacts=args.artifacts, gates=gates,
                        allow_controlled_fixtures=args.allow_controlled_fixtures,
                    )

            report = asyncio.run(run())
        with args.out.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2) + "\n")
        print(json.dumps({"passed": report.passed, "samples": len(report.samples), "tool_f1": report.tool_f1}))
        if not report.passed:
            parser.exit(1)
    except (OSError, ValueError, ValidationError, HarnessError, httpx.HTTPError, httpx.InvalidURL) as error:
        code = error.code if isinstance(error, HarnessError) else "invalid_evaluation"
        parser.exit(2, json.dumps({"error": code}) + "\n")


if __name__ == "__main__":
    main()
