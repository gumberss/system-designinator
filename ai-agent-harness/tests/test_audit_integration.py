from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_harness.adapters.postgres import PostgresStore, PostgresWriter
from agent_harness.domain import AuthorizationError, Execution, HarnessError, Status
from agent_harness.runtime import Runtime
from agent_harness.state import LeaseLost
from test_postgres_integration import store as store
from test_postgres_integration import expire_lease
from test_runtime import ScriptedProvider, answer, make_agent, principal_for
from test_tools import call
from test_tool_runtime import tool_response
from test_tools import build_harness


pytestmark = pytest.mark.integration


async def test_execution_audit_admission_attempts_tools_final_and_authorized_pagination(store: PostgresStore) -> None:
    harness = build_harness()
    session = await store.create_session(harness.principal)
    identifier = await store.submit(harness.agent, harness.principal, session, "private-input", "audit-test")
    assert await store.submit(harness.agent, harness.principal, session, "private-input", "audit-test") == identifier
    lease = await store.claim(uuid4(), tenant_id=harness.agent.tenant_id)
    assert lease
    provider = ScriptedProvider(tool_response(call()), answer("private-result"))
    result = await Runtime(
        {"test-double": provider}, tools=harness.executor,
    ).execute(
        harness.agent, harness.principal, session, "private-input", resume=lease.envelope.execution,
        checkpoint=PostgresWriter(store, lease),
    )
    assert result.status == Status.SUCCEEDED
    first = await store.audit(harness.principal, identifier, limit=1)
    assert len(first) == 1 and first[0].kind == "execution.admitted"
    rest = await store.audit(harness.principal, identifier, after=first[0].sequence)
    assert all(entry.sequence > first[0].sequence for entry in rest)
    kinds = [entry.kind for entry in first + rest]
    assert kinds.count("execution.admitted") == 1
    assert kinds.count("model.running") == 2 and kinds.count("model.succeeded") == 2
    assert "tool.prepared" in kinds and "tool.succeeded" in kinds and "tool_attempt.succeeded" in kinds
    assert "private-input" not in repr(first + rest) and "private-result" not in repr(first + rest)
    assert rest[-1].kind in {"execution.checkpoint", "model.succeeded"}
    assert any(entry.data.get("status") == "succeeded" for entry in rest if entry.kind == "execution.checkpoint")
    for intruder in (
        replace(harness.principal, tenant_id=uuid4()), replace(harness.principal, subject="other"),
    ):
        with pytest.raises(AuthorizationError):
            await store.audit(intruder, identifier)


async def test_audit_failure_rolls_back_checkpoint_and_lease_version(store: PostgresStore, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, worker = make_agent(), uuid4()
    principal = principal_for(agent)
    session = await store.create_session(principal)
    identifier = await store.submit(agent, principal, session, "task", "rollback-test")
    lease = await store.claim(worker, tenant_id=agent.tenant_id)
    assert lease
    old_version = lease.version
    original = store._audit_insert

    async def fail(*args: object, **kwargs: object) -> None:
        raise HarnessError("audit_unavailable", "Safe failure")

    monkeypatch.setattr(store, "_audit_insert", fail)
    execution = lease.envelope.execution
    execution.status = Status.SUCCEEDED
    execution.result = "must-not-commit"
    with pytest.raises(HarnessError):
        await store.save(lease, execution)
    assert lease.version == old_version
    monkeypatch.setattr(store, "_audit_insert", original)
    persisted = await store.get(principal, identifier)
    assert persisted.execution.status == Status.QUEUED and persisted.execution.result is None
    assert len((await store.context(principal, session)).history) == 0
    assert all(entry.kind != "execution.checkpoint" for entry in await store.audit(principal, identifier))


async def test_cancel_is_audited_once_and_stale_worker_cannot_append_checkpoint(store: PostgresStore) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    session = await store.create_session(principal)
    identifier = await store.submit(agent, principal, session, "task", "cancel-test")
    await store.cancel(principal, identifier)
    await store.cancel(principal, identifier)
    lease = await store.claim(uuid4(), tenant_id=agent.tenant_id)
    assert lease
    await expire_lease(store, lease)
    replacement = await store.claim(uuid4(), tenant_id=agent.tenant_id)
    assert replacement
    with pytest.raises(LeaseLost):
        await store.save(lease, lease.envelope.execution)
    entries = await store.audit(principal, identifier)
    assert sum(entry.kind == "execution.cancel_requested" for entry in entries) == 1
    assert any(entry.kind == "execution.recovered" for entry in entries)
    assert not any(entry.kind == "execution.checkpoint" for entry in entries)


@pytest.mark.parametrize("operation", [
    "UPDATE execution_audit SET kind='changed' WHERE false", "DELETE FROM execution_audit WHERE false",
    "TRUNCATE execution_audit",
])
async def test_audit_table_rejects_mutation(store: PostgresStore, operation: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        async with store.transaction() as connection:
            await connection.execute(operation)
