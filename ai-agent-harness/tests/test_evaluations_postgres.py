import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest

from agent_harness.adapters.evaluations_postgres import EvaluationRepository
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.domain import HarnessError
from agent_harness.evaluation import HumanEvaluationCreate
from agent_harness.models import ModelExecutor
from agent_harness.runtime import Runtime
from agent_harness.security import Identity
from agent_harness.worker import Worker
from test_postgres_integration import store as store
from test_runtime import ScriptedProvider, answer, make_agent, principal_for
from test_security_postgres import provision, restricted as restricted


pytestmark = pytest.mark.integration


async def test_real_human_review_rls_append_only_identity_and_concurrent_idempotency(
    store: PostgresStore, restricted: tuple[PostgresStore, PostgresStore],
) -> None:
    api, worker = restricted
    agent = make_agent()
    owner = principal_for(agent)
    await provision(store, agent, owner)
    async with store.transaction() as connection:
        await connection.execute(
            "INSERT INTO memberships(tenant_id,subject,role,allowed_agents) VALUES (%s,'reviewer','evaluator',%s)",
            (agent.tenant_id, [agent.id]),
        )
    scoped = api.scoped(agent.tenant_id)
    security = SecurityRepository(worker)
    requester = await SecurityRepository(scoped).access(Identity(agent.tenant_id, owner.subject))
    reviewer = await SecurityRepository(scoped).access(Identity(agent.tenant_id, "reviewer"))
    session = await scoped.create_session(owner)
    identifier = await scoped.submit(agent, owner, session, "Synthetic review task.", "human-review")
    provider = ScriptedProvider(answer("done"))
    runtime = Runtime({"test-double": provider}, models=ModelExecutor({"test-double": provider}, authorization=security.authorize_execution))
    await Worker(worker, runtime, {"test-double": provider}, tenant_id=agent.tenant_id, security=security, managed_only=True).run_once()
    repository = EvaluationRepository(scoped)
    artifact = await repository.artifact(reviewer, identifier)
    body = HumanEvaluationCreate(
        id=uuid4(), artifact_sha256=artifact.fingerprint(), scores={"correctness": 5, "relevance": 4, "safety": 5}, rationale="Synthetic review.",
    )
    with pytest.raises(HarnessError) as error:
        await repository.create(requester, identifier, body)
    assert error.value.code == "evaluation_self_forbidden"
    reviews = await asyncio.gather(repository.create(reviewer, identifier, body), repository.create(reviewer, identifier, body))
    assert reviews[0] == reviews[1] and reviews[0].evaluator == "reviewer"
    assert len(await repository.list(requester, identifier, UUID(int=0), 50)) == 1
    with pytest.raises(HarnessError):
        await repository.create(reviewer, identifier, body.model_copy(update={"rationale": "Cannot replace."}))
    with pytest.raises(HarnessError):
        await repository.create(reviewer, identifier, body.model_copy(update={"id": uuid4(), "artifact_sha256": "0" * 64}))
    async with api.scoped(uuid4()).transaction() as connection:
        cursor = await connection.execute("SELECT id FROM human_evaluations WHERE id=%s", (body.id,))
        assert await cursor.fetchall() == []
    with pytest.raises(psycopg.Error):
        async with scoped.transaction() as connection:
            await connection.execute("UPDATE human_evaluations SET document=document WHERE id=%s", (body.id,))
    with pytest.raises(psycopg.Error):
        async with store.transaction() as connection:
            await connection.execute("DELETE FROM human_evaluations WHERE id=%s", (body.id,))
    audit = await scoped.audit(owner, identifier, limit=100)
    assert sum(entry.kind == "evaluation.created" for entry in audit) == 1
