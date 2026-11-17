import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_harness.adapters.approvals_postgres import ApprovalRepository, PostgresApprovalAuthorizer
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.domain import ApprovalPolicy, ApprovalRequest, HarnessError, Principal, Status
from agent_harness.models import ModelExecutor
from agent_harness.runtime import Runtime
from agent_harness.security import Access, Identity
from agent_harness.state import LeaseLost
from agent_harness.tools import ToolExecutor
from agent_harness.worker import Worker
from test_postgres_integration import expire_lease, store as store
from test_security_postgres import provision, restricted as restricted
from test_runtime import ScriptedProvider, answer
from test_tool_runtime import tool_response
from test_tools import ToolHarness, build_harness, call


pytestmark = pytest.mark.integration


@dataclass
class Scenario:
    admin: PostgresStore
    api: PostgresStore
    worker: PostgresStore
    harness: ToolHarness
    requester: Access
    reviewer: Access
    request: ApprovalRequest

    def host(self, provider: ScriptedProvider) -> Worker:
        security = SecurityRepository(self.worker)
        executor = ToolExecutor(
            self.harness.registry, authorization=security.authorize_execution, approvals_enabled=True,
        )
        runtime = Runtime(
            {"test-double": provider}, tools=executor,
            models=ModelExecutor({"test-double": provider}, authorization=security.authorize_execution),
        )
        return Worker(self.worker, runtime, {"test-double": provider}, security=security,
                      tenant_id=self.harness.agent.tenant_id, managed_only=True)


async def scenario(store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore], *, ttl: int = 60) -> Scenario:
    api_root, worker = restricted
    harness = build_harness(side_effects=True, approval=True)
    harness.agent = harness.agent.model_copy(update={"approval_policy": ApprovalPolicy(timeout_seconds=ttl)})
    await provision(store, harness.agent, harness.principal)
    async with store.transaction() as connection:
        await connection.execute(
            "INSERT INTO tool_catalog(tenant_id,name) VALUES (%s,'add_v1')", (harness.agent.tenant_id,),
        )
        await connection.execute(
            "INSERT INTO memberships(tenant_id,subject,role,allowed_agents,allowed_tools) "
            "VALUES (%s,'reviewer','approver',%s,%s)",
            (harness.agent.tenant_id, [harness.agent.id], ["add_v1"]),
        )
    api = api_root.scoped(harness.agent.tenant_id)
    security = SecurityRepository(api)
    requester = await security.access(Identity(harness.agent.tenant_id, harness.principal.subject))
    reviewer = await security.access(Identity(harness.agent.tenant_id, "reviewer"))
    session = await api.create_session(harness.principal)
    identifier = await api.submit(harness.agent, harness.principal, session, "Add two and three.", "approval-test")
    placeholder = ApprovalRequest(
        identifier, harness.agent.id, 1, uuid4(), "unused", "add_v1", 1, "{}", "", "",
        harness.principal.subject, datetime.now(UTC),
    )
    result = Scenario(store, api, worker, harness, requester, reviewer, placeholder)
    paused = await result.host(ScriptedProvider(tool_response(call()))).run_once()
    assert paused is not None and paused.status == Status.WAITING_APPROVAL and paused.finished_at is None
    assert not harness.handler.contexts and len(paused.approvals) == 1
    result.request = paused.approvals[0]
    return result


async def decide(case: Scenario, *, approve: bool = True) -> ApprovalRequest:
    return await ApprovalRepository(case.api).decide(
        case.reviewer, case.request.id, "approve" if approve else "reject",
        "Reviewed exact arguments.", 1, case.harness.tool.describe(),
    )


async def test_real_pause_holds_session_and_quota_then_restart_resumes_exact_action(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    assert await case.worker.claim(uuid4(), tenant_id=case.harness.agent.tenant_id, managed_only=True) is None
    async with case.api.transaction() as connection:
        cursor = await connection.execute("SELECT active FROM quota_reservations WHERE execution_id=%s", (case.request.execution_id,))
        row = await cursor.fetchone()
        assert row is not None and row["active"] is True
    await decide(case)
    provider = ScriptedProvider(answer("Five."))
    result = await case.host(provider).run_once()
    assert result is not None and result.status == Status.SUCCEEDED and result.model_attempts == 2
    assert len(provider.requests) == len(case.harness.handler.contexts) == 1
    approval = await ApprovalRepository(case.api).get(case.reviewer, case.request.id)
    assert approval.status == "consumed" and approval.decided_by == "reviewer"
    async with case.api.transaction() as connection:
        cursor = await connection.execute("SELECT active,tokens FROM quota_reservations WHERE execution_id=%s", (result.id,))
        row = await cursor.fetchone()
        assert row is not None and row["active"] is False and row["tokens"] == 135
    events = await case.api.audit(case.harness.principal, result.id)
    assert {item.kind for item in events} >= {"approval.requested", "approval.approved", "approval.consumed"}


async def test_real_self_approval_rejected_and_human_rejection_does_not_dispatch(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    repository = ApprovalRepository(case.api)
    with pytest.raises(HarnessError) as error:
        await repository.decide(case.requester, case.request.id, "approve", "Self.", 1, case.harness.tool.describe())
    assert error.value.code == "approval_self_forbidden"
    await decide(case, approve=False)
    result = await case.host(ScriptedProvider()).run_once()
    assert result is not None and result.failure is not None and result.failure.code == "approval_rejected"
    assert not case.harness.handler.contexts


async def test_real_concurrent_conflicting_decisions_commit_one_winner(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    results = await asyncio.gather(decide(case), decide(case, approve=False), return_exceptions=True)
    assert sum(isinstance(item, ApprovalRequest) for item in results) == 1
    assert sum(isinstance(item, HarnessError) and item.code == "approval_conflict" for item in results) == 1
    recorded = await ApprovalRepository(case.api).get(case.reviewer, case.request.id)
    result = await case.host(ScriptedProvider(answer())).run_once()
    assert result is not None
    assert len(case.harness.handler.contexts) == (1 if recorded.decision == "approve" else 0)


async def test_real_duplicate_approval_does_not_requeue_or_repeat_side_effect(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    first = await decide(case)
    assert (await decide(case)).version == first.version
    result = await case.host(ScriptedProvider(answer())).run_once()
    assert result is not None and result.status == Status.SUCCEEDED
    assert (await decide(case)).status == "consumed"
    assert await case.host(ScriptedProvider()).run_once() is None
    assert len(case.harness.handler.contexts) == 1


async def test_real_approver_revocation_after_decision_prevents_callback(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    await decide(case)
    async with store.transaction() as connection:
        await connection.execute("UPDATE memberships SET active=false WHERE tenant_id=%s AND subject='reviewer'",
                                 (case.harness.agent.tenant_id,))
    result = await case.host(ScriptedProvider()).run_once()
    assert result is not None and result.failure is not None and result.failure.code == "forbidden"
    assert not case.harness.handler.contexts
    assert all(item.status != "outcome_unknown" for item in result.tool_invocations)


async def test_real_cancel_waiting_wakes_job_and_blocks_decision(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    await case.api.cancel(case.harness.principal, case.request.execution_id)
    with pytest.raises(HarnessError):
        await decide(case)
    result = await case.host(ScriptedProvider()).run_once()
    assert result is not None and result.status == Status.CANCELLED
    assert not case.harness.handler.contexts
    assert (await ApprovalRepository(case.api).get(case.requester, case.request.id)).status == "cancelled"


@pytest.mark.parametrize("by_decision", [True, False])
async def test_real_expiration_wakes_without_approval_or_effect(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore], by_decision: bool,
) -> None:
    case = await scenario(store, restricted, ttl=1)
    await asyncio.sleep(1.05)
    if by_decision:
        with pytest.raises(HarnessError) as error:
            await decide(case)
        assert error.value.code == "approval_expired"
    result = await case.host(ScriptedProvider()).run_once()
    assert result is not None and result.failure is not None and result.failure.code == "approval_expired"
    assert not case.harness.handler.contexts
    assert (await ApprovalRepository(case.api).get(case.requester, case.request.id)).status == "expired"


async def test_real_stale_lease_cannot_consume_approval_and_cross_tenant_rls_hides_request(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    case = await scenario(store, restricted)
    await decide(case)
    first = await case.worker.claim(uuid4(), tenant_id=case.harness.agent.tenant_id, managed_only=True)
    assert first is not None
    await expire_lease(store, first)
    second = await case.worker.claim(uuid4(), tenant_id=case.harness.agent.tenant_id, managed_only=True)
    assert second is not None and second.token > first.token
    invocation = first.envelope.execution.tool_invocations[0]
    with pytest.raises(LeaseLost):
        await PostgresApprovalAuthorizer(case.worker, first).consume(first.envelope.execution, invocation, case.harness.tool.describe())
    foreign = case.api.scoped(uuid4())
    async with foreign.transaction() as connection:
        cursor = await connection.execute("SELECT id FROM approvals WHERE id=%s", (case.request.id,))
        assert await cursor.fetchall() == []
    assert not case.harness.handler.contexts
