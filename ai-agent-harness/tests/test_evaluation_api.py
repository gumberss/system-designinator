from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from agent_harness.adapters.evaluations_postgres import EvaluationRepository
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.domain import Agent, Execution, HarnessError, Status
from agent_harness.evaluation import ExecutionArtifact, HumanEvaluation, HumanEvaluationCreate
from agent_harness.security import OIDCConfig
from test_api import api as api
from test_security import oidc as oidc, signed_token, signing_key as signing_key


async def test_evaluation_routes_have_authenticated_typed_independent_review_contract(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], monkeypatch: pytest.MonkeyPatch,
    signing_key: RSAPrivateKey, oidc: OIDCConfig,
) -> None:
    client, agent, _ = api
    execution = Execution(agent.tenant_id, "alice", agent.id, 1, uuid4(), status=Status.SUCCEEDED, result="5", finished_at=datetime.now(UTC))
    artifact = ExecutionArtifact.from_execution(execution, agent)
    monkeypatch.setattr(EvaluationRepository, "artifact", AsyncMock(return_value=artifact))
    value = await client.get(f"/v1/executions/{execution.id}/evaluation-artifact")
    assert value.status_code == 200 and value.headers["etag"] == '"' + artifact.fingerprint() + '"'
    client.headers["authorization"] = "Bearer " + signed_token(signing_key, oidc, tenant_id=str(agent.tenant_id), sub="evaluator")
    rubric = await client.get("/v1/evaluation-rubrics")
    assert rubric.status_code == 200 and set(rubric.json()["dimensions"]) == {"correctness", "relevance", "safety"}
    body = HumanEvaluationCreate(
        id=uuid4(), artifact_sha256=artifact.fingerprint(), scores={"correctness": 5, "relevance": 4, "safety": 5}, rationale="Checked.",
    )
    review = HumanEvaluation(**body.model_dump(), execution_id=execution.id, evaluator="evaluator", created_at=datetime.now(UTC))
    create = AsyncMock(return_value=review)
    monkeypatch.setattr(EvaluationRepository, "create", create)
    monkeypatch.setattr(EvaluationRepository, "list", AsyncMock(return_value=(review,)))
    path = f"/v1/executions/{execution.id}/evaluations"
    response = await client.post(path, json=body.model_dump(mode="json"))
    assert response.status_code == 201 and response.json()["evaluator"] == "evaluator"
    assert create.call_args.args[0].role == "evaluator"
    assert (await client.get(path)).json()["items"][0]["id"] == str(body.id)
    assert (await client.post(path, json={**body.model_dump(mode="json"), "evaluator": "forged"})).status_code == 422
    assert (await client.post("/v1/sessions")).status_code == 403
    for code, status in (("evaluation_self_forbidden", 403), ("evaluation_artifact_changed", 409), ("evaluation_not_ready", 409)):
        monkeypatch.setattr(EvaluationRepository, "create", AsyncMock(side_effect=HarnessError(code, "Private rationale must not be echoed.")))
        failed = await client.post(path, json=body.model_dump(mode="json"))
        assert failed.status_code == status and "Private rationale" not in failed.text


async def test_signed_evaluator_role_does_not_override_membership(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], signing_key: RSAPrivateKey, oidc: OIDCConfig,
) -> None:
    client, agent, _ = api
    client.headers["authorization"] = "Bearer " + signed_token(signing_key, oidc, tenant_id=str(agent.tenant_id), sub="viewer", role="evaluator")
    assert (await client.get("/v1/evaluation-rubrics")).status_code == 403
    del client.headers["authorization"]
    assert (await client.get("/v1/evaluation-rubrics")).status_code == 401
