import asyncio
from dataclasses import replace
from decimal import Decimal
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.context import ContextManager
from agent_harness.domain import (
    Agent, Execution, HarnessError, Message, ModelCapabilities, ModelRequest, ModelSelection,
    ProviderError, RetryPolicy, Status, Usage,
)
from agent_harness.models import ModelExecutor
from agent_harness.resilience import BreakerPolicy, LocalCircuitBreaker
from agent_harness.runtime import Runtime
from agent_harness.state import ENVELOPE, Envelope, HistoryMessage, SessionContext
from test_context_persistence import RecordingWriter
from test_runtime import BlockingProvider, ScriptedProvider, answer, make_agent, principal_for
from test_tools import call


def with_fallback(*, tools: bool = True, context: int = 32768) -> Agent:
    agent = make_agent()
    caps = ModelCapabilities(
        tools=True, structured_output=True, context_window_tokens=32768,
        max_output_tokens=4096, residency="global", policy_tags=frozenset({"approved"}),
    )
    primary = agent.selection.model_copy(update={"capabilities": caps})
    fallback = ModelSelection(
        provider="second", model="different", input_usd_per_million=10, output_usd_per_million=20,
        capabilities=caps.model_copy(update={"tools": tools, "context_window_tokens": context}),
    )
    return agent.model_copy(update={
        "selection": primary, "fallbacks": (fallback,),
        "retry": RetryPolicy(max_attempts=1),
    })


async def no_sleep(seconds: float) -> None:
    assert seconds >= 0


async def test_retry_accounts_every_failed_attempt_and_checkpoints_before_io() -> None:
    agent = make_agent().model_copy(update={"retry": RetryPolicy()})
    writer = RecordingWriter()
    provider = ScriptedProvider(
        ProviderError("provider_unavailable", "Safe message", usage=Usage(input_tokens=10, output_tokens=2)),
        ProviderError("provider_rate_limited", "Safe message"),
        answer(),
    )
    providers = {agent.selection.provider: provider}
    runtime = Runtime(providers, models=ModelExecutor(providers, sleep=no_sleep))
    result = await runtime.execute(agent, principal_for(agent), uuid4(), "task", checkpoint=writer)
    assert result.status == Status.SUCCEEDED and result.model_attempts == 1 and result.model_calls == 3
    assert result.input_tokens == 110 and result.output_tokens == 22
    assert result.estimated_cost_usd == Decimal("0.000297")
    assert not result.usage_complete and result.unaccounted_calls == 1
    assert [r.status for r in result.model_call_records] == ["failed", "failed", "succeeded"]
    assert all(any(
        len(snapshot.model_call_records) >= index
        and snapshot.model_call_records[index - 1].status == "running"
        and snapshot.usage_complete is False
        for snapshot in writer.saved
    ) for index in range(1, 4))


async def test_retry_bound_and_physical_call_quota() -> None:
    for cap, expected in ((256, "provider_unavailable"), (2, "model_call_limit")):
        agent = make_agent(max_model_calls=cap).model_copy(update={"retry": RetryPolicy()})
        provider = ScriptedProvider(*(
            ProviderError("provider_unavailable", "Safe error") for _ in range(3)
        ))
        providers = {agent.selection.provider: provider}
        result = await Runtime(
            providers, models=ModelExecutor(providers, sleep=no_sleep),
        ).execute(agent, principal_for(agent), uuid4(), "task")
        assert result.failure is not None and result.failure.code == expected
        assert len(provider.requests) == min(3, cap)


@pytest.mark.parametrize("code", [
    "provider_authentication", "provider_request_rejected", "provider_refusal",
    "provider_invalid_output", "provider_invalid_tool_calls",
])
async def test_permanent_errors_do_not_retry_or_fallback(code: str) -> None:
    agent = with_fallback()
    primary, secondary = ScriptedProvider(ProviderError(code, "Safe error")), ScriptedProvider(answer())
    result = await Runtime({agent.selection.provider: primary, "second": secondary}).execute(
        agent, principal_for(agent), uuid4(), "task",
    )
    assert result.failure is not None and result.failure.code == code
    assert len(primary.requests) == 1 and not secondary.requests


async def test_fallback_tracks_actual_model_prices_and_request_model() -> None:
    agent = with_fallback()
    first = ScriptedProvider(ProviderError(
        "provider_unavailable", "Safe message", usage=Usage(input_tokens=10, output_tokens=2),
    ))
    second = ScriptedProvider(answer())
    result = await Runtime({agent.selection.provider: first, "second": second}).execute(
        agent, principal_for(agent), uuid4(), "task",
    )
    assert result.status == Status.SUCCEEDED and result.usage_complete
    assert result.estimated_cost_usd == Decimal("0.001427")
    assert result.steps[0].estimated_cost_usd == Decimal("0.0014")
    assert result.model_call_records[-1].selection == agent.fallbacks[0]
    assert second.requests[0].model == "different"
    restored = ENVELOPE.validate_json(ENVELOPE.dump_json(Envelope(
        agent, principal_for(agent), "task", result,
    )))
    assert restored.execution.model_call_records == result.model_call_records


@pytest.mark.parametrize("changes", [
    {"capabilities": None},
    {"capabilities": ModelCapabilities(
        tools=True, structured_output=True, context_window_tokens=32768,
        max_output_tokens=4096, residency="other",
    )},
])
def test_fallback_requires_explicit_equivalent_policy(changes: dict[str, object]) -> None:
    agent = with_fallback()
    document = agent.model_dump()
    document["fallbacks"] = [agent.fallbacks[0].model_copy(update=changes)]
    with pytest.raises(ValidationError):
        Agent.model_validate(document)


async def test_incompatible_context_fallback_is_recorded_and_never_called() -> None:
    agent = with_fallback(context=128)
    second = ScriptedProvider(answer())
    result = await Runtime({
        agent.selection.provider: ScriptedProvider(ProviderError("provider_unavailable", "Safe")),
        "second": second,
    }).execute(agent, principal_for(agent), uuid4(), "task")
    assert result.failure is not None and result.failure.code == "fallback_incompatible"
    assert not second.requests and result.model_call_records[-1].status == "blocked"


def test_tool_history_requires_tools_capability_even_without_offered_tools() -> None:
    agent = with_fallback(tools=False)
    request = ModelRequest(
        "different", (Message("assistant", "", tool_calls=(call(),)),), 100, 4096,
    )
    assert not ModelExecutor.compatible(agent.fallbacks[0], request)


async def test_open_primary_circuit_can_use_compatible_fallback_without_call() -> None:
    agent = with_fallback()
    breaker = LocalCircuitBreaker(BreakerPolicy(failure_threshold=1))
    await breaker.finish(await breaker.acquire("provider:" + agent.selection.provider), "failure")
    first, second = ScriptedProvider(), ScriptedProvider(answer())
    providers = {agent.selection.provider: first, "second": second}
    result = await Runtime(
        providers, models=ModelExecutor(providers, breaker=breaker),
    ).execute(agent, principal_for(agent), uuid4(), "task")
    assert result.status == Status.SUCCEEDED and not first.requests
    assert result.model_calls == 1 and result.model_call_records[0].error_code == "circuit_open"


async def test_cancellation_during_retry_wait_does_not_dispatch_again() -> None:
    agent = make_agent().model_copy(update={"retry": RetryPolicy()})
    entered, cancellation = asyncio.Event(), asyncio.Event()

    async def blocked_sleep(seconds: float) -> None:
        entered.set()
        await asyncio.Event().wait()

    provider = ScriptedProvider(ProviderError("provider_rate_limited", "Safe"), answer())
    providers = {agent.selection.provider: provider}
    running = asyncio.create_task(Runtime(
        providers, models=ModelExecutor(providers, sleep=blocked_sleep),
    ).execute(agent, principal_for(agent), uuid4(), "task", cancellation=cancellation))
    await asyncio.wait_for(entered.wait(), 1)
    cancellation.set()
    result = await asyncio.wait_for(running, 1)
    assert result.status == Status.CANCELLED and len(provider.requests) == 1


async def test_cancelling_half_open_model_releases_probe_without_waiting_for_lease_expiry() -> None:
    agent = make_agent()
    now = [time.time()]
    breaker = LocalCircuitBreaker(
        BreakerPolicy(recovery_seconds=2, probe_timeout_seconds=310), clock=lambda: now[0],
    )
    key = "provider:" + agent.selection.provider
    await breaker.finish(await breaker.acquire(key), "failure", cooldown=0)
    provider, cancellation = BlockingProvider(), asyncio.Event()
    providers = {agent.selection.provider: provider}
    running = asyncio.create_task(Runtime(
        providers, models=ModelExecutor(providers, breaker=breaker, clock=lambda: now[0]),
    ).execute(agent, principal_for(agent), uuid4(), "task", cancellation=cancellation))
    await asyncio.wait_for(provider.started.wait(), 1)
    cancellation.set()
    result = await asyncio.wait_for(running, 1)
    assert result.status == Status.CANCELLED and result.model_call_records[0].status == "interrupted"
    now[0] += 2
    assert (await breaker.acquire(key)).probe


async def test_attempt_timeout_retries_and_cancels_each_underlying_call() -> None:
    agent = make_agent(call_timeout_seconds=0.2)
    agent = agent.model_copy(update={"retry": RetryPolicy(
        max_attempts=2, attempt_timeout_seconds=0.01, base_delay_seconds=0,
    )})
    provider = BlockingProvider(expected_calls=2)
    providers = {agent.selection.provider: provider}
    result = await Runtime(providers).execute(agent, principal_for(agent), uuid4(), "task")
    assert result.failure is not None and result.failure.code == "provider_timeout"
    assert len(provider.requests) == 2 and provider.stopped.is_set()
    assert result.unaccounted_calls == 2


async def test_retry_after_larger_than_deadline_does_not_sleep_or_retry() -> None:
    agent = make_agent(call_timeout_seconds=0.05).model_copy(update={"retry": RetryPolicy()})
    provider = ScriptedProvider(ProviderError(
        "provider_rate_limited", "Safe", retry_after_seconds=3600,
    ))
    providers = {agent.selection.provider: provider}
    result = await asyncio.wait_for(
        Runtime(providers).execute(agent, principal_for(agent), uuid4(), "task"), 1,
    )
    assert result.failure is not None and result.failure.code == "provider_rate_limited"
    assert result.model_calls == 1


async def test_retry_after_is_waited_before_half_open_retry_without_real_sleep() -> None:
    agent = make_agent().model_copy(update={"retry": RetryPolicy()})
    now = [time.time()]
    waits: list[float] = []

    async def advance(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    provider = ScriptedProvider(
        ProviderError("provider_rate_limited", "Safe", retry_after_seconds=2), answer(),
    )
    providers = {agent.selection.provider: provider}
    breaker = LocalCircuitBreaker(clock=lambda: now[0])
    models = ModelExecutor(
        providers, breaker=breaker, clock=lambda: now[0], sleep=advance, jitter=lambda: 0,
    )
    result = await Runtime(providers, models=models).execute(
        agent, principal_for(agent), uuid4(), "task",
    )
    assert result.status == Status.SUCCEEDED and waits == [2] and len(provider.requests) == 2


async def test_context_summary_uses_fallback_and_records_its_provenance() -> None:
    agent = with_fallback()
    agent = agent.model_copy(update={"limits": agent.limits.model_copy(update={
        "context_window_tokens": 1800, "max_output_tokens": 128,
    })})
    first = ScriptedProvider(*(ProviderError("provider_unavailable", "Safe") for _ in range(8)))
    second = ScriptedProvider(*(answer("brief summary") for _ in range(8)))
    execution = Execution(agent.tenant_id, "caller", agent.id, 1, uuid4())
    context = SessionContext(tuple(
        HistoryMessage(i, Message("user", "x" * 250)) for i in range(1, 13)
    ), None, ())
    _, summary = await ContextManager(first, models=ModelExecutor({
        agent.selection.provider: first, "second": second,
    })).prepare(
        agent, execution, context, "task", (Message("system", "instructions"),),
        RecordingWriter(), asyncio.Event(),
    )
    assert summary is not None and summary.provider == "second" and summary.model == "different"
    assert all(record.purpose == "summary" for record in execution.model_call_records)


async def test_resumed_unknown_model_attempt_is_not_repriced_or_erased() -> None:
    agent = make_agent()
    provider = BlockingProvider()
    writer = RecordingWriter()
    original = asyncio.create_task(Runtime({agent.selection.provider: provider}).execute(
        agent, principal_for(agent), uuid4(), "task", checkpoint=writer,
    ))
    await asyncio.wait_for(provider.started.wait(), 1)
    interrupted = replace(writer.saved[-1])
    original.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original
    assert interrupted.model_call_records[0].status == "running"
    result = await Runtime({agent.selection.provider: ScriptedProvider(answer())}).execute(
        agent, principal_for(agent), interrupted.session_id, "task", resume=interrupted,
    )
    assert result.model_call_records[0].status == "interrupted"
    assert result.status == Status.SUCCEEDED and result.model_calls == 2
    assert result.unaccounted_calls == 1 and not result.usage_complete
