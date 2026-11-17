import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.context import ContextManager, estimate_context
from agent_harness.domain import (
    AuthorizationError, Execution, HarnessError, Message, ModelRequest, ProviderError, Status, Usage,
)
from agent_harness.runtime import Runtime
from agent_harness.state import ENVELOPE, Envelope, HistoryMessage, SessionContext, Summary
from agent_harness.workflows import Condition, WorkflowDefinition, WorkflowNode
from test_runtime import ScriptedProvider, answer, make_agent, principal_for
from test_tool_runtime import tool_response
from test_tools import build_harness, call


class RecordingWriter:
    def __init__(self) -> None:
        self.saved: list[Execution] = []

    async def save(self, execution: Execution) -> None:
        self.saved.append(deepcopy(execution))


async def test_context_summarizes_old_messages_but_keeps_recent_and_memory_as_data() -> None:
    agent = make_agent(context_window_tokens=1800, max_output_tokens=128)
    execution = Execution(agent.tenant_id, "caller", agent.id, 1, uuid4())
    context = SessionContext(
        tuple(HistoryMessage(index, Message("user" if index % 2 else "assistant", "x" * 250))
              for index in range(1, 13)),
        None, ("private-session-memory",),
    )
    provider = ScriptedProvider(*(answer("brief summary") for _ in range(8)))
    writer = RecordingWriter()
    history, summary = await ContextManager(provider).prepare(
        agent, execution, context, "new task", (Message("system", "instructions"),),
        writer, asyncio.Event(),
    )
    assert summary is not None and summary.through_seq >= 1
    assert summary.provider == agent.selection.provider and summary.model == agent.selection.model
    assert all(message.role != "system" for message in history)
    assert history[-2:] == tuple(item.message for item in context.history[-2:])
    assert "private-session-memory" in history[0].content
    assert "untrusted_conversation_summary" in history[1].content
    assert estimate_context(ModelRequest(
        agent.selection.model, (Message("system", "instructions"),) + history + (Message("user", "new task"),),
        128, 4096,
    )) <= 1800
    assert execution.input_tokens == 100 * len(provider.requests)
    assert execution.usage_complete
    assert len(context.history) == 12


async def test_context_does_not_silently_truncate_unfittable_recent_messages() -> None:
    agent = make_agent(context_window_tokens=128, max_output_tokens=128)
    execution = Execution(agent.tenant_id, "caller", agent.id, 1, uuid4())
    provider = ScriptedProvider()
    with pytest.raises(HarnessError) as error:
        await ContextManager(provider).prepare(
            agent, execution, SessionContext((), None, ()), "task",
            (Message("system", "instructions"),), RecordingWriter(), asyncio.Event(),
        )
    assert error.value.code == "context_budget_exceeded"
    assert not provider.requests


async def test_checkpoint_round_trip_resumes_without_repeating_completed_model_steps() -> None:
    harness = build_harness()
    provider = ScriptedProvider(tool_response(call()), answer("final"))
    writer = RecordingWriter()
    result = await Runtime({"test-double": provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, uuid4(), "task", checkpoint=writer
    )
    running_tool = next(
        saved for saved in writer.saved
        if saved.tool_invocations and saved.tool_invocations[0].status == "running"
    )
    envelope = Envelope(harness.agent, harness.principal, "task", running_tool)
    restored = ENVELOPE.validate_json(ENVELOPE.dump_json(envelope))
    recovery_provider = ScriptedProvider(answer("recovered"))
    resumed = await Runtime({"test-double": recovery_provider}, tools=harness.executor).execute(
        restored.agent, restored.principal, restored.execution.session_id, "task",
        resume=restored.execution, checkpoint=RecordingWriter(),
    )
    assert result.status == Status.SUCCEEDED and resumed.status == Status.SUCCEEDED
    assert resumed.model_attempts == 2
    assert len(recovery_provider.requests) == 1
    assert resumed.tool_invocations[0].id == running_tool.tool_invocations[0].id
    assert resumed.tool_call_attempts == 1


async def test_unknown_external_effect_is_never_replayed_after_checkpoint_recovery() -> None:
    harness = build_harness(side_effects=True)
    provider = ScriptedProvider(tool_response(call()), answer())
    writer = RecordingWriter()
    await Runtime({"test-double": provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, uuid4(), "task", checkpoint=writer
    )
    interrupted = next(
        saved for saved in writer.saved
        if saved.tool_invocations and saved.tool_invocations[0].status == "running"
    )
    recovery_provider = ScriptedProvider()
    result = await Runtime({"test-double": recovery_provider}, tools=harness.executor).execute(
        harness.agent, harness.principal, interrupted.session_id, "task", resume=interrupted
    )
    assert result.status == Status.FAILED
    assert result.failure is not None and result.failure.code == "tool_outcome_unknown"
    assert len(harness.handler.contexts) == 1
    assert not recovery_provider.requests


async def test_provider_call_interrupted_by_crash_keeps_unknown_usage_on_resume() -> None:
    agent = make_agent()
    principal = principal_for(agent)
    interrupted = Execution(agent.tenant_id, principal.subject, agent.id, 1, uuid4())
    interrupted.model_attempts = 1
    interrupted.unaccounted_calls = 1
    interrupted.usage_complete = False
    result = await Runtime({"test-double": ScriptedProvider(answer())}).execute(
        agent, principal, interrupted.session_id, "task", resume=interrupted
    )
    assert result.status == Status.SUCCEEDED
    assert result.model_attempts == 2 and result.unaccounted_calls == 1
    assert not result.usage_complete and result.input_tokens == 100


async def test_terminal_resume_is_immutable_and_cross_session_resume_is_denied() -> None:
    agent = make_agent()
    principal = principal_for(agent)
    runtime = Runtime({"test-double": ScriptedProvider(answer())})
    result = await runtime.execute(agent, principal, uuid4(), "task")
    saved = deepcopy(result)
    assert await runtime.execute(
        agent, principal, result.session_id, "task", resume=result
    ) == saved
    with pytest.raises(AuthorizationError):
        await runtime.execute(agent, principal, uuid4(), "task", resume=result)
    assert result == saved


def test_checkpoint_schema_version_fails_closed() -> None:
    agent = make_agent()
    principal = principal_for(agent)
    execution = Execution(agent.tenant_id, principal.subject, agent.id, 1, uuid4())
    envelope = Envelope(agent, principal, "task", execution)
    raw = ENVELOPE.dump_json(envelope).replace(b'"schema_version":1', b'"schema_version":999')
    with pytest.raises(ValidationError):
        ENVELOPE.validate_json(raw)


@pytest.mark.parametrize("kind", ["cycle", "unknown", "handoff", "condition", "duplicate"])
def test_workflow_graph_validation(kind: str) -> None:
    agent = make_agent()
    first = WorkflowNode(id="first", agent=agent, input="task")
    nodes: tuple[WorkflowNode, ...]
    if kind == "cycle":
        nodes = (
            first.model_copy(update={"depends_on": ("second",)}),
            WorkflowNode(id="second", agent=agent, input="task", depends_on=("first",)),
        )
    elif kind == "unknown":
        nodes = (first.model_copy(update={"depends_on": ("missing",)}),)
    elif kind == "handoff":
        nodes = (first.model_copy(update={"handoff_from": ("second",)}),)
    elif kind == "condition":
        nodes = (first.model_copy(update={"condition": Condition(source="missing", equals="x")}),)
    else:
        nodes = (first, first)
    with pytest.raises(ValidationError):
        WorkflowDefinition(nodes=nodes)


def test_cached_summary_remains_untrusted_and_retains_source_interval() -> None:
    summary = Summary(4, "facts", 2, "provider", "model", 1)
    assert replace(summary, version=3).source_start_seq == 1


async def test_failed_summary_retains_known_usage_and_never_uses_invalid_summary() -> None:
    agent = make_agent(context_window_tokens=1800, max_output_tokens=128)
    execution = Execution(agent.tenant_id, "caller", agent.id, 1, uuid4())
    context = SessionContext(
        tuple(HistoryMessage(index, Message("user", "x" * 250)) for index in range(1, 13)),
        None, (),
    )
    provider = ScriptedProvider(ProviderError(
        "provider_invalid_output", "Invalid output.", usage=Usage(input_tokens=50, output_tokens=5)
    ))
    with pytest.raises(HarnessError):
        await ContextManager(provider).prepare(
            agent, execution, context, "task", (Message("system", "instructions"),),
            RecordingWriter(), asyncio.Event(),
        )
    assert execution.input_tokens == 50 and execution.output_tokens == 5
    assert execution.usage_complete


async def test_absolute_workflow_deadline_is_enforced_before_model_call() -> None:
    agent = make_agent()
    principal = principal_for(agent)
    execution = Execution(agent.tenant_id, principal.subject, agent.id, 1, uuid4())
    execution.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    provider = ScriptedProvider()
    result = await Runtime({"test-double": provider}).execute(
        agent, principal, execution.session_id, "task", resume=execution
    )
    assert result.status == Status.EXPIRED
    assert not provider.requests
