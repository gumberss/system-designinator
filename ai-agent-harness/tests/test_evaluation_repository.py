from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_harness.adapters.evaluations_postgres import EvaluationRepository
from agent_harness.domain import HarnessError, Principal, Status
from agent_harness.evaluation import ExecutionArtifact, HumanEvaluation, HumanEvaluationCreate
from agent_harness.security import Access
from agent_harness.state import ENVELOPE
from test_approval_repository import RepositoryCase, case as case, cursor


def finished(case: RepositoryCase) -> tuple[Access, HumanEvaluationCreate]:
    case.execution.status = Status.SUCCEEDED
    case.execution.result = "5"
    case.execution.finished_at = datetime.now(UTC)
    access = Access(case.reviewer.principal, "evaluator")
    artifact = ExecutionArtifact.from_execution(case.execution, case.harness.agent, user_input=case.lease.envelope.user_input)
    body = HumanEvaluationCreate(
        id=uuid4(), artifact_sha256=artifact.fingerprint(),
        scores={"correctness": 5, "relevance": 4, "safety": 5}, rationale="Checked against the task.",
    )
    return access, body


async def test_production_review_insert_is_artifact_bound_and_audited_without_rationale(case: RepositoryCase) -> None:
    access, body = finished(case)
    now = datetime.now(UTC)
    case.connection.execute.side_effect = [
        cursor({"checkpoint": ENVELOPE.dump_python(case.lease.envelope, mode="json"), "now": now}),
        cursor({"id": body.id}),
    ]
    review = await EvaluationRepository(case.store).create(access, case.execution.id, body)
    assert review.evaluator == "reviewer" and review.created_at == now and all(case.commits)
    audit = case.store._audit_insert
    assert isinstance(audit, AsyncMock)
    event = audit.call_args.args[3][0]
    assert body.rationale not in str(event.data)


@pytest.mark.parametrize("problem,code", [
    ("self", "evaluation_self_forbidden"), ("role", "forbidden"),
    ("acl", "access_denied"), ("tenant", "access_denied"),
    ("hash", "evaluation_artifact_changed"), ("running", "evaluation_not_ready"),
])
async def test_production_review_rejects_unauthorized_or_changed_artifacts(case: RepositoryCase, problem: str, code: str) -> None:
    access, body = finished(case)
    if problem == "self":
        access = Access(case.harness.principal, "admin")
    elif problem == "role":
        access = Access(access.principal, "approver")
    elif problem == "acl":
        access = Access(Principal(access.principal.tenant_id, "reviewer", frozenset()), "evaluator")
    elif problem == "tenant":
        access = Access(Principal(uuid4(), "reviewer", frozenset({case.harness.agent.id})), "evaluator")
    elif problem == "hash":
        body = body.model_copy(update={"artifact_sha256": "0" * 64})
    else:
        case.execution.status = Status.RUNNING
    case.connection.execute.side_effect = [
        cursor({"checkpoint": ENVELOPE.dump_python(case.lease.envelope, mode="json"), "now": datetime.now(UTC)}),
    ]
    with pytest.raises(HarnessError) as error:
        await EvaluationRepository(case.store).create(access, case.execution.id, body)
    assert error.value.code == code
    assert len(case.connection.execute.call_args_list) == 1


@pytest.mark.parametrize("same", [True, False])
async def test_review_id_is_idempotent_but_cannot_overwrite_previous_evaluation(case: RepositoryCase, same: bool) -> None:
    access, body = finished(case)
    original = HumanEvaluation(
        **body.model_dump(), execution_id=case.execution.id, evaluator="reviewer", created_at=datetime.now(UTC),
    )
    payload = body if same else body.model_copy(update={"rationale": "Different review."})
    case.connection.execute.side_effect = [
        cursor({"checkpoint": ENVELOPE.dump_python(case.lease.envelope, mode="json"), "now": datetime.now(UTC)}),
        cursor(), cursor({"document": original.model_dump(mode="json")}),
    ]
    if same:
        assert await EvaluationRepository(case.store).create(access, case.execution.id, payload) == original
    else:
        with pytest.raises(HarnessError) as error:
            await EvaluationRepository(case.store).create(access, case.execution.id, payload)
        assert error.value.code == "evaluation_conflict"


def test_artifact_read_does_not_grant_peer_access_to_ordinary_viewer(case: RepositoryCase) -> None:
    access, _ = finished(case)
    EvaluationRepository.authorize(Access(case.harness.principal, "viewer"), case.lease.envelope)
    EvaluationRepository.authorize(access, case.lease.envelope)
    with pytest.raises(HarnessError):
        EvaluationRepository.authorize(Access(access.principal, "viewer"), case.lease.envelope)
