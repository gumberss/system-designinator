import asyncio
import hashlib
import json
from dataclasses import replace
from typing import NoReturn

import httpx
import psycopg
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from agent_harness.adapters.calculator import AddArguments, AddResult
from agent_harness.adapters.postgres import PostgresStore, request_fingerprints
from agent_harness.adapters.redis_circuit import RedisCircuitBreaker
from agent_harness.adapters.redis_signals import RedisSignals
from agent_harness.domain import HarnessError, RetryPolicy, ToolContext, ToolDefinition
from agent_harness.hosting import configure_providers
from agent_harness.resilience import BreakerPolicy, CircuitBreaker, LocalCircuitBreaker, Permit
from agent_harness.runtime import Runtime
from agent_harness.state import LeaseLost
from agent_harness.tools import ToolExecutor, ToolRegistry
from agent_harness.worker import Worker
from test_model_reliability import with_fallback
from test_runtime import make_agent
from test_tools import ToolHarness, build_harness, call
from uuid import uuid4


def configure_tool(harness: ToolHarness, *, breaker: CircuitBreaker | None = None) -> None:
    harness.registry = ToolRegistry()
    harness.registry.register(harness.tool)
    harness.registry.set_tenant_grant(harness.agent.tenant_id, "add_v1", True)
    harness.executor = ToolExecutor(harness.registry, breaker=breaker)


async def test_read_only_tool_can_retry_with_same_invocation_and_rechecked_grants() -> None:
    harness = build_harness()
    count = 0

    async def handler(args: AddArguments, context: ToolContext) -> AddResult:
        nonlocal count
        count += 1
        if count == 1:
            raise HarnessError("tool_unavailable", "Safe dependency error")
        return AddResult(total=args.left + args.right)

    harness.tool = replace(
        harness.tool, handler=handler, definition=harness.tool.definition.model_copy(update={
            "retry": RetryPolicy(max_attempts=3, base_delay_seconds=0),
        }),
    )
    configure_tool(harness)
    (record,) = await harness.invoke(call())
    assert count == 2 and record.attempts == 2 and record.status == "succeeded"
    assert len(harness.execution.tool_invocations) == 1


async def test_tool_retry_rechecks_revoked_permissions_before_second_callback() -> None:
    harness = build_harness()
    count = 0

    async def handler(args: AddArguments, context: ToolContext) -> AddResult:
        nonlocal count
        count += 1
        harness.registry.set_tenant_grant(harness.agent.tenant_id, "add_v1", False)
        raise HarnessError("tool_unavailable", "Safe dependency error")

    harness.tool = replace(
        harness.tool, handler=handler, definition=harness.tool.definition.model_copy(update={
            "retry": RetryPolicy(max_attempts=3, base_delay_seconds=0),
        }),
    )
    configure_tool(harness)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_access_denied" and count == 1


async def test_read_only_retries_cannot_bypass_physical_tool_dispatch_limit() -> None:
    harness = build_harness(max_tool_attempts=1)
    harness.handler.dependency_timeout = True
    harness.tool = replace(
        harness.tool, definition=harness.tool.definition.model_copy(update={
            "retry": RetryPolicy(max_attempts=3, base_delay_seconds=0),
        }),
    )
    configure_tool(harness)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "tool_attempt_limit"
    assert len(harness.handler.contexts) == 1 and harness.execution.tool_dispatches == 1


def test_side_effecting_tools_cannot_enable_automatic_retries_even_with_idempotency() -> None:
    with pytest.raises(ValidationError):
        ToolDefinition(
            name="effect", description="Changes external state", side_effects=True,
            supports_idempotency=True, retry=RetryPolicy(max_attempts=2),
        )


def test_legacy_defaults_do_not_enable_retries_or_reduce_original_tool_timeout() -> None:
    agent = make_agent()
    assert agent.retry.max_attempts == 1
    assert agent.retry.attempt_timeout_seconds >= agent.limits.call_timeout_seconds
    tool = ToolDefinition(name="read", description="Read only", side_effects=False, timeout_seconds=120)
    assert tool.retry.max_attempts == 1 and tool.retry.attempt_timeout_seconds >= tool.timeout_seconds


def test_phase_four_request_fingerprint_remains_idempotent_only_without_policy_changes() -> None:
    agent, session = make_agent(), uuid4()
    legacy = {
        "id": str(agent.id), "tenant_id": str(agent.tenant_id), "version": 1,
        "instructions": "Solve the task.", "allowed_tools": [],
        "selection": {
            "provider": "test-double", "model": "deterministic",
            "input_usd_per_million": "1.5", "output_usd_per_million": "6",
        },
        "limits": {
            "max_steps": 8, "execution_timeout_seconds": 120.0, "call_timeout_seconds": 30.0,
            "max_output_tokens": 1024, "max_input_bytes": 65536, "max_response_bytes": 262144,
            "max_tool_calls": 32, "max_tool_calls_per_step": 8, "max_tool_argument_bytes": 16384,
            "max_tool_result_bytes": 65536, "context_window_tokens": 32768,
        },
    }
    original = hashlib.sha256(json.dumps({
        "agent": legacy, "session": str(session), "input": "task", "deadline": None,
    }, sort_keys=True).encode()).hexdigest()
    current, compatible = request_fingerprints(agent, session, "task", None)
    assert original != current and {current, original} <= compatible
    changed = agent.model_copy(update={"retry": RetryPolicy()})
    _, changed_fingerprints = request_fingerprints(changed, session, "task", None)
    assert original not in changed_fingerprints
    changed = agent.model_copy(update={"limits": agent.limits.model_copy(update={"max_model_calls": 2})})
    assert original not in request_fingerprints(changed, session, "task", None)[1]


async def test_open_tool_circuit_fails_before_external_effect() -> None:
    harness = build_harness(side_effects=True)
    breaker = LocalCircuitBreaker(BreakerPolicy(failure_threshold=1))
    key = f"tool:{harness.agent.tenant_id}:add_v1:1"
    await breaker.finish(await breaker.acquire(key), "failure")
    configure_tool(harness, breaker=breaker)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "circuit_open" and not harness.handler.contexts
    assert harness.execution.tool_invocations[0].status == "failed"


async def test_tools_can_share_an_external_dependency_circuit_within_their_tenant() -> None:
    harness = build_harness(side_effects=True)
    harness.tool = replace(
        harness.tool, definition=harness.tool.definition.model_copy(update={"dependency_key": "shared-backend"}),
    )
    breaker = LocalCircuitBreaker(BreakerPolicy(failure_threshold=1))
    await breaker.finish(
        await breaker.acquire(f"tool:{harness.agent.tenant_id}:shared-backend"), "failure",
    )
    configure_tool(harness, breaker=breaker)
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "circuit_open" and not harness.handler.contexts


class FailingCircuit:
    async def acquire(self, key: str) -> Permit:
        return Permit(key, 0, "test", False)

    async def finish(self, permit: Permit, outcome: str, cooldown: float | None = None) -> None:
        raise HarnessError("coordination_unavailable", "Safe coordination error")


async def test_known_tool_success_survives_circuit_update_failure_and_is_replayable() -> None:
    harness = build_harness(side_effects=True)
    configure_tool(harness, breaker=FailingCircuit())
    with pytest.raises(HarnessError) as error:
        await harness.invoke(call())
    assert error.value.code == "coordination_unavailable"
    record = harness.execution.tool_invocations[0]
    assert record.status == "succeeded" and record.result == '{"total":5}'
    (replayed,) = await harness.invoke(call())
    assert replayed.id == record.id and len(harness.handler.contexts) == 1


async def test_postgres_operational_failures_open_local_breaker_without_retrying_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 0

    async def fail(*args: object, **kwargs: object) -> NoReturn:
        nonlocal count
        count += 1
        assert kwargs["connect_timeout"] == 5
        assert kwargs["options"] == (
            "-c statement_timeout=5000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=10000 "
            "-c search_path=public"
        )
        raise psycopg.OperationalError("private database connection details")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fail)
    store = PostgresStore("unused-private-dsn")
    for i in range(6):
        with pytest.raises(HarnessError) as error:
            async with store.transaction():
                pytest.fail("No transaction should have been entered.")
        assert error.value.code == ("storage_unavailable" if i < 5 else "circuit_open")
        assert "private" not in str(error.value)
    assert count == 5


async def test_redis_coordination_fails_closed_and_notification_degrades_explicitly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    signals = RedisSignals("redis://127.0.0.1:1/15")

    async def fail(*args: object, **kwargs: object) -> NoReturn:
        raise RedisConnectionError("private redis credential")

    monkeypatch.setattr(signals.client, "eval", fail)
    monkeypatch.setattr(signals.client, "blpop", fail)
    try:
        with pytest.raises(HarnessError) as error:
            await RedisCircuitBreaker(signals.client).acquire("test")
        assert error.value.code == "coordination_unavailable" and "private" not in str(error.value)
        for _ in range(6):
            assert not await signals.wait()
        assert "redis_signal_circuit_open_using_postgres_polling" in caplog.text
        assert "private redis credential" not in caplog.text
    finally:
        await signals.close()


async def test_host_bootstraps_only_required_providers_and_scopes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = make_agent().selection.model_copy(update={"provider": "anthropic"})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret-a")
    async with httpx.AsyncClient() as client:
        providers, scopes = configure_providers(client, (selection,))
        assert set(providers) == {"anthropic"}
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret-b")
        _, different = configure_providers(client, (selection,))
        assert scopes != different and "synthetic-secret" not in str(scopes)
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        with pytest.raises(HarnessError) as error:
            configure_providers(client, (selection,))
        assert error.value.code == "missing_provider_credentials"


async def test_global_public_host_does_not_claim_regional_residency() -> None:
    agent = with_fallback()
    caps = agent.selection.capabilities
    assert caps is not None
    selection = agent.selection.model_copy(update={
        "provider": "openai", "capabilities": caps.model_copy(update={"residency": "eu"}),
    })
    async with httpx.AsyncClient() as client:
        with pytest.raises(HarnessError) as error:
            configure_providers(client, (selection,))
    assert error.value.code == "unsupported_residency"


async def test_worker_recovers_from_storage_outage_and_lease_loss_without_hiding_other_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    worker = Worker(PostgresStore("unused"), Runtime({}), {})
    calls = 0

    async def once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HarnessError("storage_unavailable", "Safe failure")
        if calls == 2:
            raise LeaseLost()
        raise RuntimeError("implementation bug")

    monkeypatch.setattr(worker, "run_once", once)
    monkeypatch.setattr("agent_harness.worker.retry_delay", lambda *args: 0)
    with pytest.raises(RuntimeError, match="implementation bug"):
        await worker.run_forever()
    assert calls == 3 and "worker_dependency_unavailable" in caplog.text and "worker_lease_lost" in caplog.text
