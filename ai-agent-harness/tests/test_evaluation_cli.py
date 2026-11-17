import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.domain import HarnessError
from agent_harness.evaluation import evaluate
from agent_harness.evaluation_cli import main, run_case, run_live, validate_url
from test_evaluation import sample_case
from test_runtime import make_agent


@pytest.mark.parametrize("value", ["http://example.com", "https://user:password@example.com", "https://example.com/?token=secret", "ftp://localhost", "https://example.com/api"])
def test_runner_rejects_unsafe_token_destination(value: str) -> None:
    with pytest.raises(HarnessError):
        validate_url(value)


async def test_live_runner_preflights_budget_before_any_execution(tmp_path: Path) -> None:
    case, _ = sample_case()
    agent = make_agent().model_copy(update={"id": case.agent_id})
    methods = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.url.params["version"] == "1"
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost") as client:
        with pytest.raises(HarnessError) as error:
            await run_live(client, (case,), repetitions=2, budget=Decimal(".00000001"), timeout=1, artifacts=tmp_path / "samples.jsonl")
    assert error.value.code == "evaluation_budget_exceeded" and methods == ["GET"]
    assert not (tmp_path / "samples.jsonl").exists()


async def test_live_runner_uses_managed_api_actual_memory_route_fresh_sessions_and_artifacts(tmp_path: Path) -> None:
    case, sample = sample_case()
    case = case.model_copy(update={"memories": {"note": "Untrusted fixture."}})
    agent = make_agent().model_copy(update={"id": case.agent_id, "tenant_id": sample.artifact.tenant_id})
    sessions = []
    executions = []
    artifact_requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal artifact_requests
        path = request.url.path
        if path.startswith("/v1/agents/"):
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        if path == "/v1/sessions":
            session = uuid4()
            sessions.append(session)
            return httpx.Response(201, json={"id": str(session)})
        if "/memory/" in path:
            assert path.endswith("/memory/note")
            return httpx.Response(204)
        if path == "/v1/executions":
            assert request.headers["Idempotency-Key"].startswith("evaluation-")
            assert json.loads(request.content)["limits"]["max_model_calls"] <= 8
            identifier = uuid4()
            executions.append(identifier)
            return httpx.Response(202, json={"execution_id": str(identifier)})
        if path.endswith("/evaluation-artifact"):
            artifact_requests += 1
            return httpx.Response(200, json=sample.artifact.model_copy(update={"execution_id": executions[-1], "session_id": sessions[-1]}).model_dump(mode="json"))
        return httpx.Response(200, json={"status": "succeeded"})

    path = tmp_path / "samples.jsonl"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost") as client:
        report = await run_live(client, (case,), repetitions=2, budget=Decimal("10"), timeout=2, artifacts=path)
    assert report.passed and report.source == "live_api" and len(report.samples) == artifact_requests == 2
    assert len(set(sessions)) == 2 and len(path.read_text().splitlines()) == 2


async def test_runner_cancels_known_execution_on_timeout_without_auto_approval() -> None:
    case, _ = sample_case()
    cancelled = []
    identifier = uuid4()

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert "approvals" not in path
        if path == "/v1/sessions":
            return httpx.Response(201, json={"id": str(uuid4())})
        if path == "/v1/executions":
            return httpx.Response(202, json={"execution_id": str(identifier)})
        if path.endswith("/cancel"):
            cancelled.append(path)
            return httpx.Response(202, json={"status": "waiting_approval"})
        return httpx.Response(200, json={"status": "waiting_approval"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost") as client:
        with pytest.raises(HarnessError) as error:
            await run_case(client, case, make_agent(), 1, .01)
    assert error.value.code == "evaluation_timeout" and cancelled == [f"/v1/executions/{identifier}/cancel"]


async def test_runner_stops_on_unknown_spend_and_preserves_partial_artifacts(tmp_path: Path) -> None:
    case, sample = sample_case()
    agent = make_agent().model_copy(update={"id": case.agent_id, "tenant_id": sample.artifact.tenant_id})

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/agents/"):
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        if path == "/v1/sessions":
            return httpx.Response(201, json={"id": str(sample.artifact.session_id)})
        if path == "/v1/executions":
            return httpx.Response(202, json={"execution_id": str(sample.artifact.execution_id)})
        if path.endswith("/evaluation-artifact"):
            return httpx.Response(200, json=sample.artifact.model_copy(update={"usage_complete": False}).model_dump(mode="json"))
        return httpx.Response(200, json={"status": "succeeded"})

    path = tmp_path / "partial.jsonl"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://localhost") as client:
        with pytest.raises(HarnessError) as error:
            await run_live(client, (case,), repetitions=2, budget=Decimal(10), timeout=1, artifacts=path)
    assert error.value.code == "evaluation_budget_uncertain" and len(path.read_text().splitlines()) == 1


def test_cli_scores_compares_and_never_overwrites_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    case, sample = sample_case()
    dataset, artifacts, output = (tmp_path / name for name in ("cases.jsonl", "samples.jsonl", "report.json"))
    dataset.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    artifacts.write_text(sample.model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["eval", "score", "--dataset", str(dataset), "--artifacts", str(artifacts), "--out", str(output)])
    main()
    assert json.loads(capsys.readouterr().out)["passed"]
    original = output.read_text()
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and output.read_text() == original
    monkeypatch.setattr(sys, "argv", ["eval", "compare", "--baseline", str(output), "--candidate", str(output)])
    main()
    assert json.loads(capsys.readouterr().out)["passed"]


def test_cli_does_not_run_paid_calls_without_consent_or_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case, _ = sample_case()
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.delenv("HARNESS_EVAL_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "eval", "run", "--dataset", str(dataset), "--artifacts", str(tmp_path / "samples.jsonl"),
        "--out", str(tmp_path / "report.json"), "--base-url", "http://localhost", "--max-cost-usd", "1",
    ])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and not (tmp_path / "report.json").exists()
