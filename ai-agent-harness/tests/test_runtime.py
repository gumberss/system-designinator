import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.domain import (
    Agent,
    AuthorizationError,
    Decision,
    HarnessError,
    Limits,
    ModelRequest,
    ModelResponse,
    ModelSelection,
    Principal,
    ProviderError,
    SessionConflict,
    Status,
    Usage,
)
from agent_harness.runtime import Runtime


def make_agent(**limits: object) -> Agent:
    return Agent(
        id=uuid4(),
        tenant_id=uuid4(),
        instructions="Solve the task.",
        selection=ModelSelection(
            provider="test-double",
            model="deterministic",
            input_usd_per_million=Decimal("1.5"),
            output_usd_per_million=Decimal("6"),
        ),
        limits=Limits.model_validate(limits),
    )


def principal_for(agent: Agent) -> Principal:
    return Principal(agent.tenant_id, "test-user", frozenset({agent.id}))


def answer(content: str = "done", *, complete: bool = True) -> ModelResponse:
    return ModelResponse(
        Decision(status="completed" if complete else "continue", content=content),
        Usage(input_tokens=100, output_tokens=20),
        "request-test",
    )


class ScriptedProvider:
    def __init__(self, *responses: ModelResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BlockingProvider:
    def __init__(self, expected_calls: int = 1) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()
        self.expected_calls = expected_calls
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == self.expected_calls:
            self.started.set()
        try:
            await self.release.wait()
            return answer()
        finally:
            self.stopped.set()


async def test_multiple_steps_track_usage_cost_and_model_selection() -> None:
    agent = make_agent()
    provider = ScriptedProvider(answer("public progress", complete=False), answer("final"))
    other = ScriptedProvider()
    execution = await Runtime({"test-double": provider, "other": other}).execute(
        agent, principal_for(agent), uuid4(), "question"
    )
    assert execution.status == Status.SUCCEEDED
    assert execution.result == "final"
    assert execution.input_tokens == 200
    assert execution.output_tokens == 40
    assert execution.estimated_cost_usd == Decimal("0.00054")
    assert execution.usage_complete
    assert [step.index for step in execution.steps] == [1, 2]
    assert all(step.latency_seconds >= 0 for step in execution.steps)
    assert execution.finished_at is not None
    assert execution.finished_at >= execution.created_at
    assert len(provider.requests[0].messages) == 3
    assert len(provider.requests[1].messages) == 5
    assert provider.requests[0].model == agent.selection.model
    assert provider.requests[0].max_output_tokens == agent.limits.max_output_tokens
    assert not other.requests


async def test_iteration_limit_is_failure_not_partial_success() -> None:
    agent = make_agent(max_steps=2)
    provider = ScriptedProvider(answer(complete=False), answer(complete=False))
    result = await Runtime({"test-double": provider}).execute(
        agent, principal_for(agent), uuid4(), "task"
    )
    assert result.status == Status.FAILED
    assert result.failure is not None and result.failure.code == "iteration_limit"
    assert result.result is None
    assert len(provider.requests) == 2


@pytest.mark.parametrize("wrong_tenant", [True, False])
async def test_unauthorized_execution_never_calls_provider(wrong_tenant: bool) -> None:
    agent = make_agent()
    provider = ScriptedProvider()
    principal = Principal(
        uuid4() if wrong_tenant else agent.tenant_id,
        "caller",
        frozenset({agent.id}) if wrong_tenant else frozenset(),
    )
    with pytest.raises(AuthorizationError):
        await Runtime({"test-double": provider}).execute(agent, principal, uuid4(), "task")
    assert not provider.requests


@pytest.mark.parametrize("user_input", ["", "  ", "12345", "\u00e9\u00e9\u00e9"])
async def test_input_limit_counts_utf8_bytes(user_input: str) -> None:
    agent = make_agent(max_input_bytes=4)
    provider = ScriptedProvider()
    with pytest.raises(HarnessError, match="Input"):
        await Runtime({"test-double": provider}).execute(
            agent, principal_for(agent), uuid4(), user_input
        )
    assert not provider.requests


async def test_missing_provider_is_explicit() -> None:
    agent = make_agent()
    with pytest.raises(HarnessError) as error:
        await Runtime({}).execute(agent, principal_for(agent), uuid4(), "task")
    assert error.value.code == "provider_not_configured"


@pytest.mark.parametrize(
    "values",
    [
        {"max_steps": 0},
        {"max_steps": 101},
        {"execution_timeout_seconds": float("inf")},
        {"execution_timeout_seconds": float("nan")},
        {"call_timeout_seconds": 0},
        {"max_output_tokens": -1},
        {"unknown": True},
    ],
)
def test_invalid_limits_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Limits.model_validate(values)


async def test_pre_cancelled_execution_never_calls_provider() -> None:
    agent = make_agent()
    provider = ScriptedProvider()
    cancellation = asyncio.Event()
    cancellation.set()
    execution = await Runtime({"test-double": provider}).execute(
        agent, principal_for(agent), uuid4(), "task", cancellation=cancellation
    )
    assert execution.status == Status.CANCELLED
    assert execution.usage_complete
    assert not provider.requests


async def test_cancellation_interrupts_provider_and_releases_session() -> None:
    agent = make_agent()
    provider = BlockingProvider()
    runtime = Runtime({"test-double": provider})
    session_id = uuid4()
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        runtime.execute(agent, principal_for(agent), session_id, "task", cancellation=cancellation)
    )
    await asyncio.wait_for(provider.started.wait(), 1)
    cancellation.set()
    result = await asyncio.wait_for(task, 1)
    assert result.status == Status.CANCELLED
    assert not result.usage_complete
    assert provider.stopped.is_set()
    provider.release.set()
    second = await runtime.execute(agent, principal_for(agent), session_id, "retry")
    assert second.status == Status.SUCCEEDED


async def test_task_cancellation_propagates_and_cleans_up_children() -> None:
    agent = make_agent()
    provider = BlockingProvider()
    runtime = Runtime({"test-double": provider})
    session_id = uuid4()
    task = asyncio.create_task(runtime.execute(agent, principal_for(agent), session_id, "task"))
    await asyncio.wait_for(provider.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.stopped.is_set()
    provider.release.set()
    result = await runtime.execute(agent, principal_for(agent), session_id, "retry")
    assert result.status == Status.SUCCEEDED


@pytest.mark.parametrize("total_timeout", [True, False])
async def test_deadline_and_call_timeout_cancel_inflight_io(total_timeout: bool) -> None:
    agent = make_agent(
        execution_timeout_seconds=0.05 if total_timeout else 10,
        call_timeout_seconds=10 if total_timeout else 0.05,
    )
    provider = BlockingProvider()
    result = await asyncio.wait_for(
        Runtime({"test-double": provider}).execute(agent, principal_for(agent), uuid4(), "task"), 2
    )
    assert result.status == (Status.EXPIRED if total_timeout else Status.FAILED)
    assert result.failure is not None
    assert result.failure.code == ("deadline_exceeded" if total_timeout else "provider_timeout")
    assert provider.stopped.is_set()
    assert not result.usage_complete


async def test_same_session_rejected_until_previous_execution_finishes() -> None:
    agent = make_agent()
    provider = BlockingProvider()
    runtime = Runtime({"test-double": provider})
    session_id = uuid4()
    first = asyncio.create_task(runtime.execute(agent, principal_for(agent), session_id, "one"))
    await asyncio.wait_for(provider.started.wait(), 1)
    try:
        with pytest.raises(SessionConflict):
            await runtime.execute(agent, principal_for(agent), session_id, "two")
    finally:
        provider.release.set()
        await first
    result = await runtime.execute(agent, principal_for(agent), session_id, "three")
    assert result.status == Status.SUCCEEDED


async def test_different_sessions_execute_concurrently_without_context_leakage() -> None:
    agent = make_agent()
    provider = BlockingProvider(expected_calls=2)
    runtime = Runtime({"test-double": provider})
    tasks = [
        asyncio.create_task(runtime.execute(agent, principal_for(agent), uuid4(), content))
        for content in ("session-one-secret", "session-two-secret")
    ]
    try:
        await asyncio.wait_for(provider.started.wait(), 1)
    finally:
        provider.release.set()
        results = await asyncio.gather(*tasks)
    assert all(result.status == Status.SUCCEEDED for result in results)
    first, second = provider.requests
    assert first.messages[-1].content == "session-one-secret"
    assert second.messages[-1].content == "session-two-secret"
    assert all("session-two-secret" not in msg.content for msg in first.messages)
    assert all("session-one-secret" not in msg.content for msg in second.messages)


@pytest.mark.parametrize("known_usage", [True, False])
async def test_provider_failures_are_classified_and_usage_not_silently_zeroed(
    known_usage: bool,
) -> None:
    agent = make_agent()
    usage = Usage(input_tokens=100, output_tokens=20) if known_usage else None
    provider = ScriptedProvider(
        answer(complete=False),
        ProviderError("provider_invalid_output", "Invalid model decision.", usage=usage),
    )
    result = await Runtime({"test-double": provider}).execute(
        agent, principal_for(agent), uuid4(), "task"
    )
    assert result.status == Status.FAILED
    assert result.failure is not None and result.failure.code == "provider_invalid_output"
    assert result.input_tokens == (200 if known_usage else 100)
    assert result.usage_complete == known_usage


async def test_unexpected_bug_is_not_hidden_and_session_is_released() -> None:
    agent = make_agent()
    provider = ScriptedProvider(RuntimeError("implementation bug"), answer())
    runtime = Runtime({"test-double": provider})
    session = uuid4()
    with pytest.raises(RuntimeError, match="implementation bug"):
        await runtime.execute(agent, principal_for(agent), session, "task")
    result = await runtime.execute(agent, principal_for(agent), session, "retry")
    assert result.status == Status.SUCCEEDED
