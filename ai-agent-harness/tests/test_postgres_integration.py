import asyncio
import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from psycopg.conninfo import conninfo_to_dict

from agent_harness.adapters.postgres import PostgresStore, PostgresWriter
from agent_harness.adapters.redis_signals import RedisSignals
from agent_harness.adapters.workflow_postgres import WorkflowStore
from agent_harness.domain import (
    AuthorizationError, HarnessError, ModelRequest, ModelResponse, SessionConflict, Status,
)
from agent_harness.runtime import Runtime
from agent_harness.state import Lease, LeaseLost, Summary
from agent_harness.worker import Worker
from agent_harness.workflows import Condition, WorkflowDefinition, WorkflowNode
from test_runtime import ScriptedProvider, answer, make_agent, principal_for
from test_tool_runtime import tool_response
from test_tools import build_harness, call


pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def store() -> PostgresStore:
    dsn = os.environ.get("HARNESS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("HARNESS_TEST_DATABASE_URL is required for real PostgreSQL integration tests.")
    database_name = conninfo_to_dict(dsn).get("dbname")
    if not isinstance(database_name, str) or not database_name.endswith("_test"):
        pytest.fail("Integration database name must end with _test; never target production.")
    database = PostgresStore(dsn)
    await database.migrate()
    return database


async def expire_lease(store: PostgresStore, lease: Lease) -> None:
    async with store.transaction() as connection:
        await connection.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND execution_id=%s",
            (lease.tenant_id, lease.execution_id),
        )


async def test_postgres_statement_timeout_is_enforced_by_server(store: PostgresStore) -> None:
    bounded = PostgresStore(os.environ["HARNESS_TEST_DATABASE_URL"], statement_timeout_ms=20)
    with pytest.raises(HarnessError) as error:
        async with bounded.transaction() as connection:
            await connection.execute("SELECT pg_sleep(0.2)")
    assert error.value.code == "storage_unavailable"


async def test_migrations_admission_owner_isolation_and_idempotency(store: PostgresStore) -> None:
    await store.migrate()
    agent = make_agent()
    principal = principal_for(agent)
    session = await store.create_session(principal)
    identifier = await store.submit(agent, principal, session, "task", "same")
    assert await store.submit(agent, principal, session, "task", "same") == identifier
    with pytest.raises(HarnessError) as error:
        await store.submit(agent, principal, session, "different", "same")
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(SessionConflict):
        await store.submit(agent, principal, session, "task", "other")
    for intruder in (replace(principal, tenant_id=uuid4()), replace(principal, subject="other")):
        with pytest.raises(AuthorizationError):
            await store.get(intruder, identifier)
        with pytest.raises(AuthorizationError):
            await store.context(intruder, session)
        with pytest.raises(AuthorizationError):
            await store.put_memory(intruder, session, "key", "secret")


async def test_claim_is_exclusive_and_stale_worker_cannot_write(store: PostgresStore) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    session = await store.create_session(principal)
    identifier = await store.submit(agent, principal, session, "task", "one")
    claims = await asyncio.gather(
        store.claim(uuid4(), tenant_id=agent.tenant_id),
        store.claim(uuid4(), tenant_id=agent.tenant_id),
    )
    live = [claim for claim in claims if claim is not None]
    assert len(live) == 1
    first = live[0]
    await expire_lease(store, first)
    second = await store.claim(uuid4(), tenant_id=agent.tenant_id)
    assert second is not None and second.token > first.token
    with pytest.raises(LeaseLost):
        await store.save(first, first.envelope.execution)
    with pytest.raises(LeaseLost):
        await store.heartbeat(first, 30)
    execution = second.envelope.execution
    execution.status, execution.result = Status.SUCCEEDED, "final"
    await store.save(second, execution)
    assert (await store.get(principal, identifier)).execution.result == "final"
    assert len((await store.context(principal, session)).history) == 2
    await store.submit(agent, principal, session, "next", "two")


async def test_context_survives_worker_recreation_and_summary_keeps_originals(store: PostgresStore) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    session = await store.create_session(principal)
    for index in range(2):
        await store.submit(agent, principal, session, f"question {index}", str(index))
        provider = ScriptedProvider(answer(f"response {index}"))
        result = await Worker(
            store, Runtime({"test-double": provider}), {"test-double": provider},
            tenant_id=agent.tenant_id,
        ).run_once()
        assert result is not None and result.status == Status.SUCCEEDED
        if index:
            assert any(message.content == "response 0" for message in provider.requests[0].messages)
    await store.submit(agent, principal, session, "third", "third")
    lease = await store.claim(uuid4(), tenant_id=agent.tenant_id)
    assert lease is not None
    summary = Summary(2, "first turn summary", 1, "test-double", "deterministic", 1)
    await store.save_summary(lease, summary)
    restored = await store.context(principal, session)
    assert restored.summary == summary
    assert [item.seq for item in restored.history] == [3, 4]
    async with store.transaction() as connection:
        cursor = await connection.execute(
            "SELECT count(*) AS total FROM messages WHERE tenant_id=%s AND session_id=%s",
            (agent.tenant_id, session),
        )
        row = await cursor.fetchone()
        assert row is not None and row["total"] == 4
    await expire_lease(store, lease)
    with pytest.raises(LeaseLost):
        await store.save_summary(lease, summary)


async def test_memory_is_persistent_session_scoped_and_expires(store: PostgresStore) -> None:
    principal = principal_for(make_agent())
    first, second = await store.create_session(principal), await store.create_session(principal)
    await store.put_memory(principal, first, "preference", "private preference")
    assert (await store.context(principal, first)).memories == ("private preference",)
    assert not (await store.context(principal, second)).memories
    async with store.transaction() as connection:
        await connection.execute(
            "UPDATE memories SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND session_id=%s", (principal.tenant_id, first),
        )
    assert not (await store.context(principal, first)).memories


async def test_queued_cancel_is_persistent_and_never_calls_model(store: PostgresStore) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    session = await store.create_session(principal)
    identifier = await store.submit(agent, principal, session, "task", "cancel")
    await store.cancel(principal, identifier)
    provider = ScriptedProvider()
    result = await Worker(
        store, Runtime({"test-double": provider}), {"test-double": provider}, tenant_id=agent.tenant_id
    ).run_once()
    assert result is not None and result.status == Status.CANCELLED
    assert not provider.requests
    assert (await store.get(principal, identifier)).execution.status == Status.CANCELLED


@pytest.mark.parametrize("side_effects", [True, False])
async def test_interrupted_tool_recovery_fences_original_worker(
    store: PostgresStore, side_effects: bool,
) -> None:
    harness = build_harness(side_effects=side_effects)
    session = await store.create_session(harness.principal)
    identifier = await store.submit(harness.agent, harness.principal, session, "task", "recovery")
    first = await store.claim(uuid4(), tenant_id=harness.agent.tenant_id)
    assert first is not None
    harness.handler.block = True
    old_provider = ScriptedProvider(tool_response(call()))
    old_runtime = Runtime({"test-double": old_provider}, tools=harness.executor)
    old = asyncio.create_task(old_runtime.execute(
        harness.agent, harness.principal, session, "task",
        resume=first.envelope.execution, checkpoint=PostgresWriter(store, first),
    ))
    await asyncio.wait_for(harness.handler.started.wait(), 10)
    persisted = await store.get(harness.principal, identifier)
    assert persisted.execution.tool_invocations[0].status == "running"
    await expire_lease(store, first)
    harness.handler.block = False
    replacement_provider = ScriptedProvider(answer("recovered"))
    try:
        recovered = await Worker(
            store, Runtime({"test-double": replacement_provider}, tools=harness.executor),
            {"test-double": replacement_provider}, tenant_id=harness.agent.tenant_id,
        ).run_once()
    finally:
        old.cancel()
        with pytest.raises((LeaseLost, asyncio.CancelledError)):
            await old
    assert recovered is not None
    if side_effects:
        assert recovered.status == Status.FAILED
        assert recovered.failure is not None and recovered.failure.code == "tool_outcome_unknown"
        assert len(harness.handler.contexts) == 1
        assert not replacement_provider.requests
    else:
        assert recovered.status == Status.SUCCEEDED
        assert len(harness.handler.contexts) == 2
        assert len(replacement_provider.requests) == 1
    assert (await store.get(harness.principal, identifier)).execution.status == recovered.status


async def test_workflow_parallel_join_conditional_and_explicit_handoff(store: PostgresStore) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    definition = WorkflowDefinition(nodes=(
        WorkflowNode(id="a", agent=agent, input="root-a"),
        WorkflowNode(id="b", agent=agent, input="private-root-b"),
        WorkflowNode(id="join", agent=agent, input="join", depends_on=("a", "b"),
                     handoff_from=("a",), condition=Condition(source="a", equals="yes")),
        WorkflowNode(id="skip", agent=agent, input="never", depends_on=("a",),
                     condition=Condition(source="a", equals="no")),
    ))
    workflows = WorkflowStore(store)
    identifier = await workflows.submit(definition, principal)
    requests: list[ModelRequest] = []

    class Provider:
        async def generate(self, request: ModelRequest) -> ModelResponse:
            requests.append(request)
            content = request.messages[-1].content
            if content == "root-a":
                return answer("yes")
            if content == "private-root-b":
                return answer("private-result-b")
            assert '"handoff": {"a": "yes"}' in content
            assert "private-result-b" not in str(request.messages)
            return answer("joined")

    provider = Provider()
    workers = [Worker(
        store, Runtime({"test-double": provider}), {"test-double": provider}, tenant_id=agent.tenant_id
    ) for _ in range(2)]
    await asyncio.gather(*(worker.run_once() for worker in workers))
    for _ in range(4):
        if (await workflows.get(principal, identifier)).status != "running":
            break
        await workers[0].run_once()
    run = await workflows.get(principal, identifier)
    assert run.status == "succeeded"
    assert run.nodes["join"].result == "joined" and run.nodes["skip"].status == "skipped"
    assert len(requests) == 3
    children = [node.execution_id for node in run.nodes.values() if node.execution_id]
    sessions = {(await store.get(principal, child)).execution.session_id for child in children}
    assert len(sessions) == 3
    with pytest.raises(AuthorizationError):
        await workflows.get(replace(principal, tenant_id=uuid4()), identifier)


async def test_workflow_reconciliation_is_idempotent_after_coordinator_restart(store: PostgresStore) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    definition = WorkflowDefinition(nodes=(WorkflowNode(id="only", agent=agent, input="task"),))
    identifier = uuid4()
    workflows = WorkflowStore(store)
    assert await workflows.submit(definition, principal, identifier) == identifier
    assert await workflows.submit(definition, principal, identifier) == identifier
    await asyncio.gather(
        workflows.advance(agent.tenant_id, identifier),
        WorkflowStore(store).advance(agent.tenant_id, identifier),
    )
    run = await workflows.get(principal, identifier)
    assert run.nodes["only"].execution_id is not None
    async with store.transaction() as connection:
        cursor = await connection.execute(
            "SELECT count(*) AS total FROM jobs WHERE tenant_id=%s", (agent.tenant_id,)
        )
        row = await cursor.fetchone()
        assert row is not None and row["total"] == 1


async def test_real_redis_signal_loss_does_not_lose_database_job(store: PostgresStore) -> None:
    url = os.environ.get("HARNESS_TEST_REDIS_URL")
    if not url or not url.rstrip("/").endswith("/15"):
        pytest.skip("HARNESS_TEST_REDIS_URL must select isolated Redis database 15.")
    signals = RedisSignals(url)
    agent = make_agent()
    principal = principal_for(agent)
    try:
        assert await signals.notify()
        assert await signals.wait()
        session = await store.create_session(principal)
        await store.submit(agent, principal, session, "task", "signal-loss")
        await signals.client.delete("harness:wake")
        provider = ScriptedProvider(answer("polled"))
        result = await Worker(
            store, Runtime({"test-double": provider}), {"test-double": provider},
            signals=signals, tenant_id=agent.tenant_id,
        ).run_once()
        assert result is not None and result.result == "polled"
    finally:
        await signals.close()
