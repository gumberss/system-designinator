from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from agent_harness.adapters.calculator import calculator_tool
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.rate_limit import RateLimiter
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.api import create_app
from agent_harness.domain import Agent, AuthorizationError, HarnessError, Principal
from agent_harness.domain import ApprovalRequest
from agent_harness.adapters.approvals_postgres import ApprovalRepository
from agent_harness.security import Access, Identity, JWKSClient, OIDCConfig, ServerConfig
from agent_harness.state import SessionContext
from agent_harness.tools import ToolRegistry
from agent_harness.workflows import WorkflowDefinition, WorkflowNode, WorkflowRun
from agent_harness.adapters.workflow_postgres import WorkflowStore
from test_runtime import make_agent
from test_security import jwks, oidc as oidc, public_dns, signed_token, signing_key as signing_key


@pytest.fixture
async def api(
    signing_key: RSAPrivateKey, oidc: OIDCConfig, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, Agent, PostgresStore]]:
    agent = make_agent().model_copy(update={"allowed_tools": frozenset({"add_integers_v1"})})
    store = PostgresStore("host=127.0.0.1 port=1 dbname=harness_test")
    registry = ToolRegistry()
    registry.register(calculator_tool())
    limiter = RateLimiter(AsyncMock(), 600, 120)
    monkeypatch.setattr(limiter, "check", AsyncMock())

    async def access(self: SecurityRepository, identity: Identity) -> Access:
        assert self.store.tenant_id == identity.tenant_id
        assert self.store.enforce_security
        if identity.tenant_id != agent.tenant_id:
            raise HarnessError("forbidden", "Membership is missing.")
        return Access(
            Principal(identity.tenant_id, identity.subject, frozenset({agent.id}), frozenset({"add_integers_v1"})),
            "viewer" if identity.subject == "viewer" else "approver" if identity.subject == "reviewer"
            else "evaluator" if identity.subject == "evaluator" else "admin",
        )

    monkeypatch.setattr(SecurityRepository, "access", access)
    monkeypatch.setattr(SecurityRepository, "save_agent", AsyncMock())
    monkeypatch.setattr(SecurityRepository, "agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(SecurityRepository, "tool_grants", AsyncMock(return_value={"add_integers_v1": (True, 1)}))
    monkeypatch.setattr(SecurityRepository, "set_tool", AsyncMock(return_value=2))
    monkeypatch.setattr(PostgresStore, "create_session", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(PostgresStore, "submit", AsyncMock(return_value=uuid4()))
    config = ServerConfig(oidc=oidc, models={"primary": agent.selection})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(signing_key)))) as keys:
        identity = JWKSClient(keys, oidc, resolver=public_dns)
        app = create_app(config, store, identity, limiter, registry, verify_database=False)
        assert app.openapi()["paths"]["/v1/executions"]["post"]["responses"]["202"]
        assert "application/problem+json" in app.openapi()["paths"]["/v1/executions"]["post"]["responses"]["422"]["content"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["authorization"] = "Bearer " + signed_token(signing_key, oidc, tenant_id=str(agent.tenant_id))
            yield client, agent, store


async def test_authentication_and_server_membership_precede_session_access(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], signing_key: RSAPrivateKey, oidc: OIDCConfig,
) -> None:
    client, agent, _ = api
    valid = await client.post("/v1/sessions")
    assert valid.status_code == 201 and UUID(valid.json()["id"])
    assert valid.headers["cache-control"] == "no-store"
    assert UUID(valid.headers["x-correlation-id"])
    assert valid.headers["x-content-type-options"] == "nosniff"
    client.headers["authorization"] = "Bearer " + signed_token(signing_key, oidc)
    assert (await client.post("/v1/sessions")).status_code == 403
    del client.headers["authorization"]
    missing = await client.post("/v1/sessions")
    assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"


async def test_signed_roles_cannot_override_server_rbac(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], signing_key: RSAPrivateKey, oidc: OIDCConfig,
) -> None:
    client, agent, _ = api
    client.headers["authorization"] = "Bearer " + signed_token(
        signing_key, oidc, tenant_id=str(agent.tenant_id), sub="viewer", role="admin",
    )
    assert (await client.post("/v1/sessions")).status_code == 403
    assert (await client.get("/v1/tool-implementations")).status_code == 403


async def test_agent_creation_catalog_constraints_and_etags(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore],
) -> None:
    client, agent, _ = api
    body = {"id": str(agent.id), "instructions": "Solve.", "model_key": "primary", "allowed_tools": ["add_integers_v1"]}
    created = await client.post("/v1/agents", json=body)
    assert created.status_code == 201 and created.headers["etag"] == '"1"'
    assert created.json()["tenant_id"] == str(agent.tenant_id)
    invalid = await client.post("/v1/agents", json={**body, "tenant_id": str(uuid4())})
    assert invalid.status_code == 422
    assert (await client.post("/v1/agents", json={**body, "model_key": "https://attacker.example"})).status_code == 400
    assert (await client.post("/v1/agents", json={**body, "allowed_tools": ["shell"]})).status_code == 403
    del body["id"]
    assert (await client.patch(f"/v1/agents/{agent.id}", json=body)).status_code == 428
    updated = await client.patch(f"/v1/agents/{agent.id}", json=body, headers={"If-Match": '"1"'})
    assert updated.status_code == 200 and updated.headers["etag"] == '"2"'


async def test_execution_requires_idempotency_and_cannot_replace_agent_policy(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, agent, _ = api
    body = {"agent_id": str(agent.id), "session_id": str(uuid4()), "input": "task"}
    assert (await client.post("/v1/executions", json=body)).status_code == 422
    accepted = await client.post("/v1/executions", json=body, headers={"Idempotency-Key": "safe-replay"})
    assert accepted.status_code == 202
    assert accepted.headers["location"] == accepted.json()["status_url"]
    assert (await client.post("/v1/executions", json={**body, "managed": False},
                              headers={"Idempotency-Key": "no-bypass"})).status_code == 422
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    monkeypatch.setattr(SecurityRepository, "agent", AsyncMock(return_value=agent.model_copy(update={"result_schema": schema})))
    assert (await client.post("/v1/executions", json={**body, "output_schema": {"type": "object"}},
                              headers={"Idempotency-Key": "no-weaken"})).status_code == 403


async def test_no_arbitrary_code_or_egress_in_tool_registration(api: tuple[httpx.AsyncClient, Agent, PostgresStore]) -> None:
    client, _, _ = api
    assert (await client.post("/v1/tools", json={"implementation_key": "add_integers_v1", "url": "http://127.0.0.1"})).status_code == 422
    assert (await client.post("/v1/tools", json={"implementation_key": "shell"})).status_code == 400
    updated = await client.patch("/v1/tools/add_integers_v1", json={"enabled": False}, headers={"If-Match": '"1"'})
    assert updated.status_code == 200 and updated.json()["enabled"] is False
    assert (await client.patch("/v1/tools/add_integers_v1", json={"enabled": True})).status_code == 428


@pytest.mark.parametrize("body,status", [
    (b'{"input":"a","input":"b"}', 400), (b'{"input":NaN}', 400),
    (b'{"secret":"never-echo-me", "extra":true}', 422),
    (b"\xff", 400), (b"x" * 262145, 413),
    (b'{"nested":' * 140 + b"{}" + b"}" * 140, 400),
])
async def test_hostile_request_bodies_are_bounded_and_not_echoed(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], body: bytes, status: int,
) -> None:
    client, _, _ = api
    response = await client.post("/v1/executions", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == status
    assert "never-echo-me" not in response.text
    assert response.headers["content-type"] == "application/problem+json"


async def test_private_failures_and_cross_owner_lookups_are_sanitized(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api
    monkeypatch.setattr(PostgresStore, "get", AsyncMock(side_effect=AuthorizationError()))
    assert (await client.get(f"/v1/executions/{uuid4()}")).status_code == 404
    monkeypatch.setattr(PostgresStore, "create_session", AsyncMock(side_effect=HarnessError(
        "storage_unavailable", "postgresql://password-secret@private-host private SQL",
    )))
    response = await client.post("/v1/sessions")
    assert response.status_code == 503
    assert "password-secret" not in response.text and "private-host" not in response.text


async def test_context_and_workflow_have_explicit_private_safe_response_contracts(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, agent, _ = api
    monkeypatch.setattr(PostgresStore, "context", AsyncMock(return_value=SessionContext((), None, ("memory",))))
    context = await client.get(f"/v1/sessions/{uuid4()}")
    assert context.status_code == 200 and context.json() == {"history": [], "summary": None, "memories": ["memory"]}
    run = WorkflowRun(
        WorkflowDefinition(nodes=(WorkflowNode(id="a", agent=agent, input="private task"),)),
        Principal(agent.tenant_id, "private-subject", frozenset({agent.id})),
    )
    monkeypatch.setattr(WorkflowStore, "get", AsyncMock(return_value=run))
    response = await client.get(f"/v1/workflows/{run.id}")
    assert response.status_code == 200
    assert "private-subject" not in response.text and "private task" not in response.text
    assert set(response.json()) == {"id", "status", "created_at", "nodes", "failure_category"}


async def test_chunked_body_and_router_errors_follow_problem_contract(api: tuple[httpx.AsyncClient, Agent, PostgresStore]) -> None:
    client, _, _ = api

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b" " * 100000

    oversized = await client.post("/v1/executions", content=chunks())
    assert oversized.status_code == 413 and oversized.headers["cache-control"] == "no-store"
    missing = await client.get("/v1/not-a-route")
    assert missing.status_code == 404 and missing.headers["content-type"] == "application/problem+json"


async def test_approval_endpoints_have_typed_payloads_and_version_preconditions(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], monkeypatch: pytest.MonkeyPatch,
    signing_key: RSAPrivateKey, oidc: OIDCConfig,
) -> None:
    from copy import deepcopy
    from datetime import UTC, datetime, timedelta
    client, agent, _ = api
    request = ApprovalRequest(
        uuid4(), agent.id, 1, uuid4(), "call_1", "add_integers_v1", 1,
        '{"left":2,"right":3}', "digest", "definition", "alice", datetime.now(UTC) + timedelta(seconds=60),
    )
    monkeypatch.setattr(ApprovalRepository, "get", AsyncMock(return_value=request))
    monkeypatch.setattr(ApprovalRepository, "list", AsyncMock(return_value=(request,)))
    client.headers["authorization"] = "Bearer " + signed_token(
        signing_key, oidc, tenant_id=str(agent.tenant_id), sub="reviewer",
    )
    listed = await client.get("/v1/approvals?status=pending")
    assert listed.status_code == 200 and listed.json()["items"][0]["id"] == str(request.id)
    value = await client.get(f"/v1/approvals/{request.id}")
    assert value.status_code == 200 and value.headers["etag"] == '"1"'
    assert value.json()["arguments"] == request.arguments
    path = f"/v1/approvals/{request.id}/decision"
    body = {"decision": "approve", "reason": "Reviewed."}
    assert (await client.post(path, json=body)).status_code == 428
    assert (await client.post(path, json={**body, "decided_by": "forged"}, headers={"If-Match": '"1"'})).status_code == 422
    approved = deepcopy(request)
    approved.status, approved.decision, approved.decided_by, approved.version = "approved", "approve", "reviewer", 2
    decide = AsyncMock(return_value=approved)
    monkeypatch.setattr(ApprovalRepository, "decide", decide)
    for reason in (" ", "\n\t"):
        invalid = await client.post(path, json={**body, "reason": reason}, headers={"If-Match": '"1"'})
        assert invalid.status_code == 422
    decide.assert_not_awaited()
    response = await client.post(path, json=body, headers={"If-Match": '"1"'})
    assert response.status_code == 200 and response.headers["etag"] == '"2"'
    assert response.json()["decided_by"] == "reviewer"
    access = decide.call_args.args[0]
    assert access.role == "approver" and access.principal.subject == "reviewer"
    assert (await client.post("/v1/sessions")).status_code == 403


async def test_approval_self_decision_and_unprivileged_role_stay_forbidden(
    api: tuple[httpx.AsyncClient, Agent, PostgresStore], monkeypatch: pytest.MonkeyPatch,
    signing_key: RSAPrivateKey, oidc: OIDCConfig,
) -> None:
    from datetime import UTC, datetime, timedelta
    client, agent, _ = api
    request = ApprovalRequest(
        uuid4(), agent.id, 1, uuid4(), "call_1", "add_integers_v1", 1,
        '{"left":2,"right":3}', "digest", "definition", "alice", datetime.now(UTC) + timedelta(seconds=60),
    )
    monkeypatch.setattr(ApprovalRepository, "get", AsyncMock(return_value=request))
    path = f"/v1/approvals/{request.id}/decision"
    response = await client.post(path, json={"decision": "approve", "reason": "My action."}, headers={"If-Match": '"1"'})
    assert response.status_code == 403 and response.json()["code"] == "approval_self_forbidden"
    client.headers["authorization"] = "Bearer " + signed_token(
        signing_key, oidc, tenant_id=str(agent.tenant_id), sub="viewer", role="approver",
    )
    assert (await client.post(path, json={"decision": "approve", "reason": "Forged role."},
                              headers={"If-Match": '"1"'})).status_code == 403
