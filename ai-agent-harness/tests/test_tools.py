import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.adapters.calculator import AddArguments, AddResult
from agent_harness.async_utils import OperationCancelled
from agent_harness.domain import (
    Agent,
    Execution,
    HarnessError,
    Principal,
    Status,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
)
from agent_harness.tools import ToolExecutor, ToolRegistry, TypedTool
from test_runtime import make_agent


class Handler:
    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()
        self.block = False
        self.invalid_output = False
        self.fail = False
        self.bug = False
        self.dependency_timeout = False

    async def __call__(self, arguments: AddArguments, context: ToolContext) -> AddResult:
        self.contexts.append(context)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            if self.fail:
                raise HarnessError("tool_failed", "The operation failed.")
            if self.bug:
                raise RuntimeError("implementation bug")
            if self.dependency_timeout:
                raise TimeoutError("private dependency diagnostic")
            if self.invalid_output:
                return AddResult(total=0).model_copy(update={"total": "private-invalid-result"})
            return AddResult(total=arguments.left + arguments.right)
        finally:
            self.stopped.set()


@dataclass
class ToolHarness:
    agent: Agent
    principal: Principal
    execution: Execution
    registry: ToolRegistry
    executor: ToolExecutor
    handler: Handler
    tool: TypedTool[AddArguments, AddResult]

    async def invoke(
        self, *calls: ToolCall, cancellation: asyncio.Event | None = None
    ) -> tuple[ToolInvocation, ...]:
        return await self.executor.invoke_batch(
            calls, self.agent, self.principal, self.execution,
            cancellation if cancellation is not None else asyncio.Event(),
            self.registry.discover(self.agent, self.principal),
        )


def build_harness(
    *, side_effects: bool = False, idempotency: bool = False,
    timeout: float = 10, approval: bool = False, **limits: object,
) -> ToolHarness:
    agent = make_agent(**limits).model_copy(update={"allowed_tools": frozenset({"add_v1"})})
    principal = Principal(agent.tenant_id, "caller", frozenset({agent.id}), frozenset({"add_v1"}))
    execution = Execution(
        agent.tenant_id, principal.subject, agent.id, agent.version, uuid4(), status=Status.RUNNING
    )
    handler = Handler()
    tool = TypedTool(
        ToolDefinition(
            name="add_v1", description="Add integers.", timeout_seconds=timeout,
            side_effects=side_effects, supports_idempotency=idempotency,
            requires_approval=approval,
        ),
        AddArguments, AddResult, handler,
    )
    registry = ToolRegistry()
    registry.register(tool)
    registry.set_tenant_grant(agent.tenant_id, "add_v1", True)
    return ToolHarness(agent, principal, execution, registry, ToolExecutor(registry), handler, tool)


def call(call_id: str = "call_1", arguments: str = '{"left":2,"right":3}') -> ToolCall:
    return ToolCall(id=call_id, name="add_v1", arguments=arguments)


async def test_validation_records_result_and_scoped_context() -> None:
    harness = build_harness(idempotency=True)
    (record,) = await harness.invoke(call())
    assert record.status == "succeeded"
    assert record.result == '{"total":5}'
    assert record.version == 1
    assert record.arguments_sha256 is not None and len(record.arguments_sha256) == 64
    assert record.started_at is not None and record.finished_at is not None
    assert record.finished_at >= record.started_at
    assert record.latency_seconds >= 0
    context = harness.handler.contexts[0]
    assert context.tenant_id == harness.agent.tenant_id
    assert context.subject == harness.principal.subject
    assert context.execution_id == harness.execution.id
    assert context.session_id == harness.execution.session_id
    assert context.invocation_id == record.id
    assert context.deadline <= harness.execution.created_at + timedelta(
        seconds=harness.agent.limits.execution_timeout_seconds
    )
    assert context.idempotency_key is not None and len(context.idempotency_key) == 64


def test_registry_discovers_installed_tools_without_exposing_mutable_schemas() -> None:
    harness = build_harness()
    (descriptor,) = harness.registry.list_installed()
    assert descriptor.input_schema["additionalProperties"] is False
    descriptor.input_schema.clear()
    assert harness.registry.discover(harness.agent, harness.principal)[0].input_schema
    with pytest.raises(HarnessError) as error:
        harness.registry.register(harness.tool)
    assert error.value.code == "tool_already_registered"
    assert harness.registry.resolve("add_v1") is harness.tool
    with pytest.raises(HarnessError, match="not installed"):
        harness.registry.resolve("missing")


@pytest.mark.parametrize(
    "revoke", ["agent", "principal", "tenant", "disabled", "wrong_tenant", "wrong_agent", "blank_subject"]
)
async def test_permission_intersection_denies_before_any_handler_call(revoke: str) -> None:
    harness = build_harness()
    if revoke == "agent":
        harness.agent = harness.agent.model_copy(update={"allowed_tools": frozenset()})
    elif revoke == "principal":
        harness.principal = replace(harness.principal, allowed_tools=frozenset())
    elif revoke == "tenant":
        harness.registry.set_tenant_grant(harness.agent.tenant_id, "add_v1", False)
    elif revoke == "disabled":
        harness.registry.set_enabled("add_v1", False)
    elif revoke == "wrong_tenant":
        harness.principal = replace(harness.principal, tenant_id=uuid4())
    elif revoke == "wrong_agent":
        harness.principal = replace(harness.principal, allowed_agent_ids=frozenset())
    else:
        harness.principal = replace(harness.principal, subject=" ")
    assert not harness.registry.discover(harness.agent, harness.principal)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_access_denied"
    assert not harness.handler.contexts


@pytest.mark.parametrize(
    "arguments",
    [
        "invalid-json", "[]", "null", '{"left":true,"right":2}',
        '{"left":"2","right":3}', '{"left":2,"right":3,"private-extra":"secret"}',
        '{"left":2}', '{"left":2,"left":3,"right":4}',
        '{"left":NaN,"right":3}', '{"left":1e999,"right":3}',
        '{"left":1000000000001,"right":0}',
        '{"left":' + "[" * 40 + "0" + "]" * 40 + ',"right":3}',
    ],
)
async def test_malformed_arguments_are_rejected_and_sanitized(arguments: str) -> None:
    harness = build_harness()
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call(arguments=arguments))
    assert error.value.code == "tool_invalid_arguments"
    assert "secret" not in str(error.value)
    assert not harness.handler.contexts
    assert harness.execution.tool_invocations[-1].status == "rejected"


def test_invalid_unicode_is_rejected_at_the_tool_call_boundary() -> None:
    with pytest.raises(ValidationError):
        call(arguments='{"left":"\ud800","right":0}')


async def test_argument_byte_limit_and_result_byte_limit() -> None:
    harness = build_harness(max_tool_argument_bytes=5)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_arguments_too_large"
    assert not harness.handler.contexts
    harness = build_harness(max_tool_result_bytes=10)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_result_too_large"
    assert harness.execution.tool_invocations[0].result is None


async def test_output_is_revalidated_without_leaking_pydantic_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = build_harness()
    harness.handler.invalid_output = True
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_invalid_result"
    assert "private-invalid-result" not in str(error.value)
    assert not capsys.readouterr().err
    assert harness.execution.tool_invocations[0].status == "failed"


async def test_batch_preflight_validates_every_call_before_effects() -> None:
    harness = build_harness(side_effects=True)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call("first"), call("second", '{"left":"bad","right":1}'))
    assert error.value.code == "tool_invalid_arguments"
    assert not harness.handler.contexts
    first, second = harness.execution.tool_invocations
    assert first.status == "cancelled" and second.status == "rejected"


async def test_duplicate_batch_ids_are_rejected_before_effects() -> None:
    harness = build_harness()
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call(), call())
    assert error.value.code == "duplicate_tool_call_id"
    assert not harness.handler.contexts


@pytest.mark.parametrize("per_step", [True, False])
async def test_tool_call_limits_are_enforced(per_step: bool) -> None:
    harness = build_harness(
        max_tool_calls_per_step=1 if per_step else 8,
        max_tool_calls=32 if per_step else 1,
    )
    calls: tuple[ToolCall, ...]
    if per_step:
        calls = (call("one"), call("two"))
    else:
        await harness.invoke(call("one"))
        calls = (call("two"),)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(*calls)
    assert error.value.code == "tool_call_limit"
    assert len(harness.handler.contexts) == (0 if per_step else 1)


async def test_replay_is_scoped_canonical_and_never_reruns_effects() -> None:
    harness = build_harness(idempotency=True, side_effects=True)
    (first,) = await harness.invoke(call())
    (replay,) = await harness.invoke(call(arguments='{ "right":3, "left":2 }'))
    assert first is replay
    assert first.replay_count == 1
    assert len(harness.handler.contexts) == 1
    await harness.invoke(call("new_intent"))
    assert len(harness.handler.contexts) == 2
    assert harness.handler.contexts[0].idempotency_key != harness.handler.contexts[1].idempotency_key
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call(arguments='{"left":4,"right":3}'))
    assert error.value.code == "tool_idempotency_conflict"
    assert len(harness.handler.contexts) == 2


async def test_replay_rechecks_permissions_before_returning_cached_output() -> None:
    harness = build_harness()
    await harness.invoke(call())
    harness.registry.set_tenant_grant(harness.agent.tenant_id, "add_v1", False)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_access_denied"
    assert len(harness.handler.contexts) == 1


async def test_concurrent_replay_cannot_dispatch_twice() -> None:
    harness = build_harness(side_effects=True)
    harness.handler.block = True
    first = asyncio.create_task(harness.invoke(call()))
    await asyncio.wait_for(harness.handler.started.wait(), 1)
    try:
        with pytest.raises(HarnessError) as error:
            await harness.invoke(call())
        assert error.value.code == "tool_replay_blocked"
    finally:
        harness.handler.release.set()
        await first
    assert len(harness.handler.contexts) == 1
    assert harness.handler.contexts[0].idempotency_key is None


@pytest.mark.parametrize("side_effects", [True, False])
async def test_timeout_marks_unknown_effects_and_never_retries(side_effects: bool) -> None:
    harness = build_harness(side_effects=side_effects, timeout=0.03)
    harness.handler.block = True
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == ("tool_outcome_unknown" if side_effects else "tool_timeout")
    assert harness.handler.stopped.is_set()
    assert harness.execution.tool_invocations[0].status == ("outcome_unknown" if side_effects else "failed")
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_replay_blocked"
    assert len(harness.handler.contexts) == 1


@pytest.mark.parametrize("side_effects", [True, False])
async def test_cancellation_stops_await_and_preserves_effect_uncertainty(side_effects: bool) -> None:
    harness = build_harness(side_effects=side_effects)
    harness.handler.block = True
    signal = asyncio.Event()
    task = asyncio.create_task(harness.invoke(call(), cancellation=signal))
    await asyncio.wait_for(harness.handler.started.wait(), 1)
    signal.set()
    with pytest.raises(OperationCancelled):
        await asyncio.wait_for(task, 1)
    record = harness.execution.tool_invocations[0]
    assert record.status == ("outcome_unknown" if side_effects else "cancelled")
    assert harness.handler.stopped.is_set()


async def test_pre_cancelled_call_never_starts_effect() -> None:
    harness = build_harness(side_effects=True)
    signal = asyncio.Event()
    signal.set()
    with pytest.raises(OperationCancelled):
        await harness.invoke(call(), cancellation=signal)
    assert not harness.handler.contexts
    assert harness.execution.tool_invocations[0].status == "cancelled"


async def test_approval_required_tool_is_blocked_until_workflow_exists() -> None:
    harness = build_harness(approval=True)
    assert not harness.registry.discover(harness.agent, harness.principal)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "approval_required"
    assert not harness.handler.contexts


async def test_unoffered_tool_is_not_dispatched_even_with_grants() -> None:
    harness = build_harness()
    with pytest.raises(HarnessError) as error:
        await harness.executor.invoke_batch(
            (call(),), harness.agent, harness.principal, harness.execution, asyncio.Event(), ()
        )
    assert error.value.code == "tool_not_offered"
    assert not harness.handler.contexts


async def test_execution_scope_is_checked_before_cached_output() -> None:
    harness = build_harness()
    await harness.invoke(call())
    harness.execution.session_id = uuid4()
    harness.execution.tenant_id = uuid4()
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_access_denied"
    assert len(harness.handler.contexts) == 1


async def test_unexpected_implementation_bug_propagates_with_failed_record() -> None:
    harness = build_harness()
    harness.handler.bug = True
    with pytest.raises(RuntimeError, match="implementation bug"):
        await harness.invoke(call())
    record = harness.execution.tool_invocations[0]
    assert record.status == "failed"
    assert record.failure is not None and record.failure.code == "tool_internal_error"


async def test_expired_execution_cannot_start_a_tool_effect() -> None:
    harness = build_harness(side_effects=True)
    harness.execution.created_at = datetime.now(UTC) - timedelta(hours=2)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "deadline_exceeded"
    assert not harness.handler.contexts
    assert harness.execution.tool_invocations[0].status == "failed"


@pytest.mark.parametrize("status", [Status.SUCCEEDED, Status.FAILED, Status.CANCELLED, Status.EXPIRED])
async def test_terminal_execution_cannot_dispatch_tools(status: Status) -> None:
    harness = build_harness()
    harness.execution.status = status
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_access_denied"
    assert not harness.handler.contexts


async def test_tool_dependency_timeout_is_classified_and_sanitized() -> None:
    harness = build_harness()
    harness.handler.dependency_timeout = True
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_timeout"
    assert "private" not in str(error.value)


async def test_recorded_results_cannot_leak_across_execution_instances() -> None:
    harness = build_harness(idempotency=True)
    await harness.invoke(call())
    first_context = harness.handler.contexts[0]
    harness.execution = replace(
        harness.execution, id=uuid4(), tool_invocations=[], tool_call_attempts=0
    )
    await harness.invoke(call())
    assert len(harness.handler.contexts) == 2
    assert harness.handler.contexts[1].idempotency_key != first_context.idempotency_key


async def test_permissions_are_rechecked_between_calls_in_the_same_batch() -> None:
    harness = build_harness()

    async def revoke(arguments: AddArguments, context: ToolContext) -> AddResult:
        harness.registry.set_enabled("add_v1", False)
        return await harness.handler(arguments, context)

    registry = ToolRegistry()
    registry.register(TypedTool(
        harness.tool.definition, AddArguments, AddResult, revoke
    ))
    registry.set_tenant_grant(harness.agent.tenant_id, "add_v1", True)
    harness.registry = registry
    harness.executor = ToolExecutor(registry)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call("first"), call("second"))
    assert error.value.code == "tool_access_denied"
    assert len(harness.handler.contexts) == 1
    assert harness.execution.tool_invocations[0].status == "succeeded"
    assert harness.execution.tool_invocations[1].result is None
