import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_harness.adapters.approvals_postgres import ApprovalRepository, effective
from agent_harness.adapters.postgres import PostgresStore, request_fingerprints
from agent_harness.approvals import definition_digest
from agent_harness.domain import (
    ApprovalPolicy, ApprovalRequest, Execution, HarnessError, Principal, Status, ToolDescriptor, ToolInvocation,
)
from agent_harness.runtime import Runtime
from agent_harness.security import Access
from agent_harness.state import ENVELOPE, Envelope
from agent_harness.tools import ToolExecutor
from test_runtime import ScriptedProvider, answer
from test_tool_runtime import tool_response
from test_tools import ToolHarness, build_harness, call


class MemoryWriter:
    def __init__(self) -> None:
        self.saved: list[Execution] = []

    async def save(self, execution: Execution) -> None:
        self.saved.append(deepcopy(execution))


class TestAuthorizer:
    __test__ = False

    def __init__(self) -> None:
        self.consumed: list[object] = []
        self.denied = False

    async def consume(self, execution: Execution, invocation: ToolInvocation, descriptor: ToolDescriptor) -> None:
        request = next(item for item in execution.approvals if item.invocation_id == invocation.id)
        if self.denied:
            raise HarnessError("forbidden", "Approver permission revoked.")
        if request.status not in ("approved", "consumed") or request.decision != "approve":
            raise HarnessError("approval_required", "A human decision is required.")
        assert request.definition_sha256 == definition_digest(descriptor)
        self.consumed.append(request.id)
        request.status = "consumed"


def configured() -> ToolHarness:
    harness = build_harness(side_effects=True, approval=True)
    harness.executor = ToolExecutor(harness.registry, approvals_enabled=True)
    return harness


async def pause(harness: ToolHarness) -> tuple[Execution, MemoryWriter, TestAuthorizer, ScriptedProvider]:
    writer, gate = MemoryWriter(), TestAuthorizer()
    provider = ScriptedProvider(tool_response(call()))
    execution = await Runtime({"test-double": provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, harness.execution.session_id, "Add two and three.",
        checkpoint=writer, approval_authorizer=gate,
    )
    assert execution.status == Status.WAITING_APPROVAL and execution.finished_at is None
    assert not harness.handler.contexts and not gate.consumed
    assert len(execution.approvals) == 1
    return execution, writer, gate, provider


def approve(execution: Execution) -> None:
    request = execution.approvals[0]
    request.status, request.decision = "approved", "approve"
    request.decided_by, request.decided_at, request.reason = "independent-reviewer", datetime.now(UTC), "Reviewed."
    request.version += 1
    execution.status = Status.QUEUED


async def test_pause_checkpoint_restart_and_resume_without_repeating_model_or_tool() -> None:
    harness = configured()
    execution, writer, gate, first = await pause(harness)
    assert writer.saved[-1].status == Status.WAITING_APPROVAL
    request = execution.approvals[0]
    assert request.arguments == '{"left":2,"right":3}' and request.arguments_sha256
    assert request.execution_id == execution.id and request.invocation_id == execution.tool_invocations[0].id
    assert request.expires_at <= execution.created_at + timedelta(seconds=harness.agent.limits.execution_timeout_seconds)
    envelope = ENVELOPE.validate_json(ENVELOPE.dump_json(Envelope(harness.agent, harness.principal, "Add two and three.", execution)))
    approve(envelope.execution)
    restarted_provider = ScriptedProvider(answer("Five."))
    result = await Runtime({"test-double": restarted_provider}, tools=harness.executor).execute(
        envelope.agent, envelope.principal, execution.session_id, envelope.user_input,
        resume=envelope.execution, checkpoint=writer, approval_authorizer=gate,
    )
    assert result.status == Status.SUCCEEDED and result.result == "Five."
    assert len(first.requests) == len(restarted_provider.requests) == len(harness.handler.contexts) == 1
    assert result.model_attempts == 2 and result.approvals[0].status == "consumed"
    assert result.tool_call_attempts == 1


@pytest.mark.parametrize("state,code", [("rejected", "approval_rejected"), ("expired", "approval_expired")])
async def test_rejection_or_expiration_prevents_all_dispatches(state: str, code: str) -> None:
    harness = configured()
    execution, writer, gate, _ = await pause(harness)
    if state == "rejected":
        execution.approvals[0].status = "rejected"
        execution.approvals[0].decision = "reject"
    else:
        execution.approvals[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    execution.status = Status.QUEUED
    provider = ScriptedProvider()
    result = await Runtime({"test-double": provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, execution.session_id, "task", resume=execution,
        checkpoint=writer, approval_authorizer=gate,
    )
    assert result.failure is not None and result.failure.code == code
    assert not harness.handler.contexts and not provider.requests and not gate.consumed


async def test_original_deadline_and_cancel_are_not_paused_for_human_wait() -> None:
    for cancelled in (True, False):
        harness = configured()
        execution, writer, gate, _ = await pause(harness)
        approve(execution)
        if not cancelled:
            execution.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        signal = asyncio.Event()
        if cancelled:
            signal.set()
        result = await Runtime({"test-double": ScriptedProvider()}, tools=harness.executor).execute(
            harness.agent, harness.principal, execution.session_id, "task", resume=execution,
            checkpoint=writer, approval_authorizer=gate, cancellation=signal,
        )
        assert result.status == (Status.CANCELLED if cancelled else Status.EXPIRED)
        assert not harness.handler.contexts and not gate.consumed


async def test_changed_arguments_or_tool_contract_never_reuse_approval() -> None:
    for change_arguments in (True, False):
        harness = configured()
        execution, writer, gate, _ = await pause(harness)
        approve(execution)
        if change_arguments:
            execution.pending_tool_calls = (call(arguments='{"left":100,"right":200}'),)
        else:
            execution.approvals[0].definition_sha256 = "0" * 64
        result = await Runtime({"test-double": ScriptedProvider()}, tools=harness.executor).execute(
            harness.agent, harness.principal, execution.session_id, "task", resume=execution,
            checkpoint=writer, approval_authorizer=gate,
        )
        assert result.failure is not None
        assert result.failure.code in {"tool_idempotency_conflict", "approval_action_changed"}
        assert not harness.handler.contexts and not gate.consumed


async def test_approver_revocation_before_callback_is_known_not_dispatched() -> None:
    harness = configured()
    execution, writer, gate, _ = await pause(harness)
    approve(execution)
    gate.denied = True
    result = await Runtime({"test-double": ScriptedProvider()}, tools=harness.executor).execute(
        harness.agent, harness.principal, execution.session_id, "task", resume=execution,
        checkpoint=writer, approval_authorizer=gate,
    )
    assert result.failure is not None and result.failure.code == "forbidden"
    assert result.tool_invocations[0].status == "failed"
    assert not harness.handler.contexts and result.usage_complete


async def test_approval_enabled_runtime_without_checkpoint_stays_fail_closed() -> None:
    harness = configured()
    provider = ScriptedProvider(tool_response(call()))
    result = await Runtime({"test-double": provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, uuid4(), "task", approval_authorizer=TestAuthorizer(),
    )
    assert result.failure is not None and result.failure.code == "approval_required"
    assert not harness.handler.contexts


async def test_batch_is_validated_and_paused_before_any_effect() -> None:
    harness = configured()
    writer, gate = MemoryWriter(), TestAuthorizer()
    provider = ScriptedProvider(tool_response(call("one"), call("two")))
    result = await Runtime({"test-double": provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, uuid4(), "task", checkpoint=writer, approval_authorizer=gate,
    )
    assert result.status == Status.WAITING_APPROVAL and len(result.approvals) == 2
    assert not harness.handler.contexts
    for request in result.approvals:
        assert request.status == "pending"


async def test_agent_policy_can_require_approval_for_unflagged_real_tools() -> None:
    harness = build_harness()
    harness.agent = harness.agent.model_copy(update={
        "approval_policy": ApprovalPolicy(required_tools=frozenset({"add_v1"}), timeout_seconds=5),
    })
    harness.executor = ToolExecutor(harness.registry, approvals_enabled=True)
    execution, _, _, _ = await pause(harness)
    assert execution.approvals[0].expires_at <= datetime.now(UTC) + timedelta(seconds=5)


async def test_independent_reviewer_role_and_acl_and_no_self_approval() -> None:
    harness = configured()
    execution, _, _, _ = await pause(harness)
    request = execution.approvals[0]
    repository = ApprovalRepository(PostgresStore("dbname=not_connected"))
    owner = Access(harness.principal, "admin")
    assert repository.visible(owner, request)
    with pytest.raises(HarnessError) as error:
        await repository.check_reviewer(owner, request)
    assert error.value.code == "approval_self_forbidden"
    unauthorized = Access(Principal(harness.agent.tenant_id, "reviewer", frozenset(), frozenset()), "approver")
    assert not repository.visible(unauthorized, request)
    with pytest.raises(HarnessError):
        await repository.check_reviewer(unauthorized, request)
    for role in ("viewer", "operator", "designer"):
        from pydantic import TypeAdapter
        access = TypeAdapter(Access).validate_python({"principal": unauthorized.principal, "role": role})
        with pytest.raises(HarnessError):
            access.require("approval:decide")


def test_policy_is_part_of_fingerprint_but_none_preserves_phase_seven() -> None:
    harness = build_harness()
    agent = harness.agent
    before = agent.model_dump(mode="json", exclude={"approval_policy"})
    before["allowed_tools"] = sorted(agent.allowed_tools)
    session = uuid4()
    old = hashlib.sha256(json.dumps({
        "agent": before, "session": str(session), "input": "task", "deadline": None,
    }, sort_keys=True).encode()).hexdigest()
    assert old in request_fingerprints(agent, session, "task", None)[1]
    changed = agent.model_copy(update={"approval_policy": ApprovalPolicy(require_side_effects=True)})
    assert old not in request_fingerprints(changed, session, "task", None)[1]


async def test_effective_expiration_does_not_mutate_persisted_request() -> None:
    harness = configured()
    execution, _, _, _ = await pause(harness)
    request: ApprovalRequest = execution.approvals[0]
    request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert effective(request).status == "expired"
    assert request.status == "pending"
