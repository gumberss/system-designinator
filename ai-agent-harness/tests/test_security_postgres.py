import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.adapters.workflow_postgres import WorkflowStore
from agent_harness.domain import Agent, HarnessError, Principal, ProviderError, Status
from agent_harness.models import ModelExecutor
from agent_harness.runtime import Runtime
from agent_harness.security import reservation
from agent_harness.worker import Worker
from agent_harness.workflows import WorkflowDefinition, WorkflowNode
from test_postgres_integration import store as store
from test_runtime import ScriptedProvider, answer, make_agent, principal_for


pytestmark = pytest.mark.integration


@pytest.fixture
async def restricted(store: PostgresStore) -> AsyncIterator[tuple[PostgresStore, PostgresStore]]:
    logins = ["harness_test_" + uuid4().hex for _ in range(2)]
    stores: list[PostgresStore] = []
    try:
        for name, group in zip(logins, ("harness_api", "harness_worker"), strict=True):
            password = secrets.token_urlsafe(32)
            async with store.transaction() as connection:
                await connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(name), sql.Literal(password),
                ))
                await connection.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(name)))
            stores.append(PostgresStore(make_conninfo(os.environ["HARNESS_TEST_DATABASE_URL"], user=name, password=password)))
        yield stores[0], stores[1]
    finally:
        async with store.transaction() as connection:
            for name in logins:
                await connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


async def provision(store: PostgresStore, agent: Agent, principal: Principal, active: int = 1) -> None:
    async with store.transaction() as connection:
        await connection.execute(
            "INSERT INTO memberships(tenant_id,subject,role,allowed_agents,allowed_tools) VALUES (%s,%s,'admin',%s,%s)",
            (agent.tenant_id, principal.subject, [agent.id], list(principal.allowed_tools)),
        )
        await connection.execute(
            "INSERT INTO agent_catalog(tenant_id,id,version,document) VALUES (%s,%s,1,%s)",
            (agent.tenant_id, agent.id, Jsonb(agent.model_dump(mode="json"))),
        )
        await connection.execute("INSERT INTO quota_policy VALUES (%s,%s,100,1000000000,100000)", (agent.tenant_id, active))


async def test_real_logins_force_rls_without_scope_and_cross_tenant(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    api, worker = restricted
    await SecurityRepository(api).verify_api_role()
    await SecurityRepository(worker).verify_api_role(worker=True)
    with pytest.raises(HarnessError):
        await SecurityRepository(store).verify_api_role()
    with pytest.raises(HarnessError):
        await SecurityRepository(worker).verify_api_role()
    first, second = make_agent(), make_agent()
    a, b = principal_for(first), principal_for(second)
    session_a, session_b = await store.create_session(a), await store.create_session(b)
    async with api.transaction() as connection:
        cursor = await connection.execute("SELECT id FROM sessions WHERE id=ANY(%s)", ([session_a, session_b],))
        assert await cursor.fetchall() == []
    async with api.scoped(a.tenant_id).transaction() as connection:
        cursor = await connection.execute("SELECT id FROM sessions WHERE tenant_id=%s", (b.tenant_id,))
        assert await cursor.fetchall() == []
        cursor = await connection.execute("UPDATE sessions SET owner='intruder' WHERE tenant_id=%s", (b.tenant_id,))
        assert cursor.rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with api.scoped(a.tenant_id).transaction() as connection:
            await connection.execute("INSERT INTO sessions(tenant_id,id,owner) VALUES (%s,%s,'intruder')", (b.tenant_id, uuid4()))
    async with api.transaction() as connection:
        cursor = await connection.execute("SELECT current_setting('harness.tenant_id',true) AS scope")
        row = await cursor.fetchone()
        assert row is not None and row["scope"] in (None, "")
    assert (await api.scoped(a.tenant_id).context(a, session_a)).history == ()


async def test_atomic_quota_idempotency_terminal_reconciliation_and_known_usage(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    api, worker = restricted
    agent = make_agent()
    principal = principal_for(agent)
    await provision(store, agent, principal)
    scoped = api.scoped(agent.tenant_id)
    sessions = await asyncio.gather(scoped.create_session(principal), scoped.create_session(principal))
    admitted = await asyncio.gather(*(
        scoped.submit(agent, principal, session, "task", f"quota-{i}") for i, session in enumerate(sessions)
    ), return_exceptions=True)
    assert sum(isinstance(item, HarnessError) and item.code == "quota_exceeded" for item in admitted) == 1
    winner = next(i for i, item in enumerate(admitted) if not isinstance(item, BaseException))
    identifier = await scoped.submit(agent, principal, sessions[winner], "task", f"quota-{winner}")
    assert identifier == admitted[winner]
    security = SecurityRepository(worker)
    provider = ScriptedProvider(answer())
    runtime = Runtime({"test-double": provider}, models=ModelExecutor(
        {"test-double": provider}, authorization=security.authorize_execution,
    ))
    await Worker(worker, runtime, {"test-double": provider}, tenant_id=agent.tenant_id,
                 security=security, managed_only=True).run_once()
    result = (await scoped.get(principal, identifier)).execution
    assert result.status == Status.SUCCEEDED and result.managed
    async with scoped.transaction() as connection:
        cursor = await connection.execute("SELECT * FROM quota_days WHERE tenant_id=%s", (agent.tenant_id,))
        day = await cursor.fetchone()
        assert day is not None and day["executions"] == 1 and day["tokens"] == 120
        assert day["cost"] == Decimal("0.00027")
        cursor = await connection.execute("SELECT * FROM quota_reservations WHERE execution_id=%s", (identifier,))
        record = await cursor.fetchone()
        assert record is not None and record["active"] is False and record["usage_complete"] is True


async def test_unknown_provider_consumption_retains_reservation_and_revocation_prevents_dispatch(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    api, worker = restricted
    agent = make_agent()
    principal = principal_for(agent)
    await provision(store, agent, principal)
    scoped = api.scoped(agent.tenant_id)
    session = await scoped.create_session(principal)
    identifier = await scoped.submit(agent, principal, session, "task", "unknown")
    security = SecurityRepository(worker)
    provider = ScriptedProvider(ProviderError("provider_unavailable", "Unavailable."))
    runtime = Runtime({"test-double": provider}, models=ModelExecutor(
        {"test-double": provider}, authorization=security.authorize_execution,
    ))
    host = Worker(worker, runtime, {"test-double": provider}, tenant_id=agent.tenant_id,
                  security=security, managed_only=True)
    await host.run_once()
    async with scoped.transaction() as connection:
        cursor = await connection.execute("SELECT * FROM quota_reservations WHERE execution_id=%s", (identifier,))
        record = await cursor.fetchone()
        assert record is not None and record["tokens"] == reservation(agent)[0]
        assert record["active"] is False and record["usage_complete"] is False
    other_session = await scoped.create_session(principal)
    other = await scoped.submit(agent, principal, other_session, "task", "revoked")
    async with store.transaction() as connection:
        await connection.execute("UPDATE memberships SET active=false WHERE tenant_id=%s", (agent.tenant_id,))
    await host.run_once()
    result = (await scoped.get(principal, other)).execution
    assert result.failure is not None and result.failure.code == "forbidden"
    assert len(provider.requests) == 1 and result.usage_complete


async def test_managed_workflow_quota_failure_is_durable_not_scheduler_crash(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    api, worker = restricted
    agent = make_agent()
    principal = principal_for(agent)
    await provision(store, agent, principal)
    workflow_api = WorkflowStore(api.scoped(agent.tenant_id))
    definition = WorkflowDefinition(nodes=(
        WorkflowNode(id="a", agent=agent, input="first"),
        WorkflowNode(id="b", agent=agent, input="second"),
    ))
    identifier = await workflow_api.submit(definition, principal)
    await WorkflowStore(worker).advance(agent.tenant_id, identifier)
    run = await workflow_api.get(principal, identifier)
    assert run.failure is not None and run.failure.code == "quota_exceeded"
    assert run.nodes["b"].status == "failed"
    assert run.nodes["b"].execution_id is None
    assert run.nodes["a"].execution_id is not None
    child = await api.scoped(agent.tenant_id).get(principal, run.nodes["a"].execution_id)
    assert child.execution.managed
