from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import psycopg
import pytest

from agent_harness.adapters.approvals_postgres import APPROVAL, ApprovalRepository
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.approvals import definition_digest
from agent_harness.async_utils import OperationCancelled
from agent_harness.domain import ApprovalRequest, Execution, HarnessError, Principal, Status, ToolDescriptor, ToolInvocation
from agent_harness.security import Access
from agent_harness.state import ENVELOPE, Envelope, Lease
from test_tools import ToolHarness, build_harness


@dataclass
class RepositoryCase:
    store: PostgresStore
    connection: MagicMock
    harness: ToolHarness
    execution: Execution
    request: ApprovalRequest
    reviewer: Access
    lease: Lease
    descriptor: ToolDescriptor
    commits: list[bool]


def cursor(one: dict[str, object] | None = None, many: list[dict[str, object]] | None = None) -> MagicMock:
    result = MagicMock(spec=psycopg.AsyncCursor)
    result.fetchone = AsyncMock(return_value=one)
    result.fetchall = AsyncMock(return_value=many or [])
    return result


@pytest.fixture
def case(monkeypatch: pytest.MonkeyPatch) -> RepositoryCase:
    harness = build_harness(side_effects=True, approval=True)
    execution = harness.execution
    execution.managed = True
    execution.status = Status.WAITING_APPROVAL
    descriptor = harness.tool.describe()
    request = ApprovalRequest(
        execution.id, harness.agent.id, 1, uuid4(), "call_1", "add_v1", 1,
        '{"left":2,"right":3}', "digest", definition_digest(descriptor), execution.subject,
        datetime.now(UTC) + timedelta(seconds=60),
    )
    execution.approvals.append(request)
    store = PostgresStore("dbname=controlled_contract_no_network")
    connection = MagicMock(spec=psycopg.AsyncConnection)
    connection.execute = AsyncMock()
    commits: list[bool] = []

    @asynccontextmanager
    async def transaction(self: PostgresStore) -> AsyncIterator[psycopg.AsyncConnection[dict[str, object]]]:
        try:
            yield connection
        except BaseException:
            commits.append(False)
            raise
        else:
            commits.append(True)

    monkeypatch.setattr(PostgresStore, "transaction", transaction)
    monkeypatch.setattr(store, "_check_lease", AsyncMock())
    monkeypatch.setattr(store, "_audit_insert", AsyncMock())
    reviewer = Access(Principal(execution.tenant_id, "reviewer", frozenset({harness.agent.id}), frozenset({"add_v1"})), "approver")
    monkeypatch.setattr(SecurityRepository, "access", AsyncMock(return_value=reviewer))
    monkeypatch.setattr(SecurityRepository, "agent", AsyncMock(return_value=harness.agent))
    monkeypatch.setattr(SecurityRepository, "tool_grants", AsyncMock(return_value={"add_v1": (True, 1)}))
    monkeypatch.setattr(SecurityRepository, "authorize_execution", AsyncMock())
    lease = Lease(execution.tenant_id, execution.id, uuid4(), 1, 0,
                  Envelope(harness.agent, harness.principal, "task", execution), False)
    return RepositoryCase(store, connection, harness, execution, request, reviewer, lease, descriptor, commits)


def row(case: RepositoryCase, *, cancelled: bool = False, now: datetime | None = None) -> dict[str, object]:
    return {
        "document": APPROVAL.dump_python(case.request, mode="json"),
        "checkpoint": ENVELOPE.dump_python(case.lease.envelope, mode="json"),
        "cancel_requested": cancelled, "now": now or datetime.now(UTC),
    }


@pytest.mark.parametrize("decision,pending,ready", [
    ("approve", False, True), ("reject", False, True),
    ("approve", True, False), ("reject", True, True),
])
async def test_production_decision_updates_checkpoint_queue_and_audit_atomically(
    case: RepositoryCase, decision: Literal["approve", "reject"], pending: bool, ready: bool,
) -> None:
    action = decision
    now = datetime.now(UTC)
    decided = deepcopy(case.request)
    decided.status = "approved" if action == "approve" else "rejected"
    decided.decision, decided.reason, decided.decided_by, decided.decided_at = action, "Reviewed.", "reviewer", now
    decided.version = 2
    requests = [decided]
    if pending:
        second = deepcopy(case.request)
        second.id, second.invocation_id, second.call_id = uuid4(), uuid4(), "call_2"
        requests.append(second)
    case.connection.execute.side_effect = [
        cursor({"document": APPROVAL.dump_python(case.request, mode="json")}),
        cursor({"state": "waiting"}), cursor(row(case, now=now)), cursor({"id": case.request.id}),
        cursor(many=[{"document": APPROVAL.dump_python(item, mode="json")} for item in requests]),
        cursor({"version": 3}), cursor(),
    ] + ([cursor()] if ready else [])
    result = await ApprovalRepository(case.store).decide(
        case.reviewer, case.request.id, action, "Reviewed.", 1, case.descriptor,
    )
    assert result.decision == action and result.decided_by == "reviewer" and result.version == 2
    assert all(case.commits)
    queries = [item.args[0] for item in case.connection.execute.call_args_list]
    assert any("FOR UPDATE" in query and "jobs" in query for query in queries)
    assert any("UPDATE executions" in query for query in queries)
    assert any("UPDATE jobs SET state='queued'" in query for query in queries) is ready
    update = next(item for item in case.connection.execute.call_args_list if "UPDATE executions" in item.args[0])
    assert update.args[1][1] == ("queued" if ready else "waiting_approval")


@pytest.mark.parametrize("same", [True, False])
async def test_production_decision_replay_is_idempotent_and_conflicts_are_explicit(case: RepositoryCase, same: bool) -> None:
    case.request.status, case.request.decision = "approved", "approve"
    case.request.decided_by, case.request.reason, case.request.version = "reviewer", "Reviewed.", 2
    case.connection.execute.side_effect = [
        cursor({"document": APPROVAL.dump_python(case.request, mode="json")}),
        cursor({"state": "queued"}), cursor(row(case)),
    ]
    repository = ApprovalRepository(case.store)
    if same:
        result = await repository.decide(case.reviewer, case.request.id, "approve", "Reviewed.", 1, case.descriptor)
        assert result.version == 2
    else:
        with pytest.raises(HarnessError) as error:
            await repository.decide(case.reviewer, case.request.id, "reject", "Reviewed.", 1, case.descriptor)
        assert error.value.code == "approval_conflict"
    assert not any("UPDATE" in item.args[0] and "FOR UPDATE" not in item.args[0] for item in case.connection.execute.call_args_list)


@pytest.mark.parametrize("problem,code", [
    ("cancelled", "approval_conflict"), ("running", "approval_conflict"),
    ("version", "precondition_failed"), ("definition", "approval_action_changed"),
])
async def test_production_decision_rejects_stale_or_cancelled_actions(case: RepositoryCase, problem: str, code: str) -> None:
    descriptor = case.descriptor
    if problem == "definition":
        case.request.definition_sha256 = "changed"
    case.connection.execute.side_effect = [
        cursor({"document": APPROVAL.dump_python(case.request, mode="json")}),
        cursor({"state": "running" if problem == "running" else "waiting"}),
        cursor(row(case, cancelled=problem == "cancelled")),
    ]
    with pytest.raises(HarnessError) as error:
        await ApprovalRepository(case.store).decide(
            case.reviewer, case.request.id, "approve", "Reviewed.", 99 if problem == "version" else 1, descriptor,
        )
    assert error.value.code == code


async def test_expiration_is_committed_before_http_error_not_rolled_back(case: RepositoryCase) -> None:
    expired = deepcopy(case.request)
    expired.status, expired.version = "expired", 2
    case.connection.execute.side_effect = [
        cursor({"document": APPROVAL.dump_python(case.request, mode="json")}),
        cursor({"state": "waiting"}), cursor(row(case, now=case.request.expires_at + timedelta(seconds=1))),
        cursor({"id": case.request.id}), cursor(many=[{"document": APPROVAL.dump_python(expired, mode="json")}]),
        cursor({"version": 3}), cursor(), cursor(),
    ]
    with pytest.raises(HarnessError) as error:
        await ApprovalRepository(case.store).decide(case.reviewer, case.request.id, "approve", "Reviewed.", 1, case.descriptor)
    assert error.value.code == "approval_expired" and case.commits[-1] is True


@pytest.mark.parametrize("problem,code", [
    ("pending", "approval_required"), ("expired", "approval_expired"),
    ("changed", "approval_action_changed"), ("consumed", "tool_outcome_unknown"),
    ("reviewer_revoked", "forbidden"), ("decision_missing", "approval_required"),
])
async def test_production_consume_revalidates_action_reviewer_and_expiration(
    case: RepositoryCase, monkeypatch: pytest.MonkeyPatch, problem: str, code: str,
) -> None:
    case.request.status, case.request.decision, case.request.decided_by = "approved", "approve", "reviewer"
    if problem == "pending":
        case.request.status = "pending"
    if problem == "expired":
        case.request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if problem == "changed":
        case.request.definition_sha256 = "bad"
    if problem == "consumed":
        case.request.status = "consumed"
    if problem == "decision_missing":
        case.request.decision = None
    if problem == "reviewer_revoked":
        monkeypatch.setattr(SecurityRepository, "access", AsyncMock(return_value=Access(case.reviewer.principal, "viewer")))
    case.connection.execute.side_effect = [cursor(row(case))]
    invocation = ToolInvocation("call_1", "add_v1", version=1, id=case.request.invocation_id, arguments_sha256="digest")
    with pytest.raises(HarnessError) as error:
        await ApprovalRepository(case.store).consume(case.lease, case.execution, invocation, case.descriptor)
    assert error.value.code == code
    assert case.commits[-1] is False


async def test_production_consume_records_single_use_under_fenced_lease(case: RepositoryCase) -> None:
    case.request.status, case.request.decision, case.request.decided_by = "approved", "approve", "reviewer"
    case.connection.execute.side_effect = [cursor(row(case)), cursor({"id": case.request.id})]
    invocation = ToolInvocation("call_1", "add_v1", version=1, id=case.request.invocation_id, arguments_sha256="digest")
    await ApprovalRepository(case.store).consume(case.lease, case.execution, invocation, case.descriptor)
    assert case.execution.approvals[0].status == "consumed"
    assert case.execution.approvals[0].version == 2
    checker = case.store._check_lease
    assert isinstance(checker, AsyncMock)
    checker.assert_awaited_once_with(case.connection, case.lease)
    assert case.commits == [True]


async def test_production_consume_checks_cancel_flag_before_authorizing_effect(case: RepositoryCase) -> None:
    case.connection.execute.side_effect = [cursor(row(case, cancelled=True))]
    invocation = ToolInvocation("call_1", "add_v1", id=case.request.invocation_id)
    with pytest.raises(OperationCancelled):
        await ApprovalRepository(case.store).consume(case.lease, case.execution, invocation, case.descriptor)
