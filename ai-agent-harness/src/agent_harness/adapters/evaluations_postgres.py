from uuid import UUID

from psycopg.types.json import Jsonb

from agent_harness.adapters.postgres import PostgresStore
from agent_harness.audit import AuditEvent
from agent_harness.domain import AuthorizationError, HarnessError
from agent_harness.evaluation import ExecutionArtifact, HumanEvaluation, HumanEvaluationCreate
from agent_harness.security import Access
from agent_harness.state import ENVELOPE, Envelope


class EvaluationRepository:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    @staticmethod
    def authorize(access: Access, envelope: Envelope, *, write: bool = False) -> None:
        execution = envelope.execution
        if execution.tenant_id != access.principal.tenant_id:
            raise AuthorizationError()
        if write:
            access.require("evaluation:write")
            if execution.subject == access.principal.subject:
                raise HarnessError("evaluation_self_forbidden", "An independent evaluator is required.")
        elif execution.subject == access.principal.subject:
            access.require("execution:read")
            return
        else:
            access.require("evaluation:read")
        if (
            execution.agent_id not in access.principal.allowed_agent_ids
            or not envelope.agent.allowed_tools <= access.principal.allowed_tools
        ):
            raise AuthorizationError()

    async def artifact(self, access: Access, identifier: UUID) -> ExecutionArtifact:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT checkpoint FROM executions WHERE tenant_id=%s AND id=%s",
                (access.principal.tenant_id, identifier),
            )
            row = await cursor.fetchone()
        if row is None:
            raise AuthorizationError()
        envelope = ENVELOPE.validate_python(row["checkpoint"])
        self.authorize(access, envelope)
        return ExecutionArtifact.from_execution(envelope.execution, envelope.agent, user_input=envelope.user_input)

    async def create(self, access: Access, identifier: UUID, body: HumanEvaluationCreate) -> HumanEvaluation:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT checkpoint,clock_timestamp() AS now FROM executions WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (access.principal.tenant_id, identifier),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AuthorizationError()
            envelope = ENVELOPE.validate_python(row["checkpoint"])
            self.authorize(access, envelope, write=True)
            artifact = ExecutionArtifact.from_execution(envelope.execution, envelope.agent, user_input=envelope.user_input)
            if artifact.fingerprint() != body.artifact_sha256:
                raise HarnessError("evaluation_artifact_changed", "Review does not match the execution artifact.")
            review = HumanEvaluation.model_validate({
                **body.model_dump(), "execution_id": identifier, "evaluator": access.principal.subject,
                "created_at": row["now"],
            })
            cursor = await connection.execute(
                "INSERT INTO human_evaluations(tenant_id,id,execution_id,document) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (access.principal.tenant_id, review.id, identifier, Jsonb(review.model_dump(mode="json"))),
            )
            if await cursor.fetchone() is None:
                cursor = await connection.execute(
                    "SELECT document FROM human_evaluations WHERE tenant_id=%s AND id=%s",
                    (access.principal.tenant_id, review.id),
                )
                previous = await cursor.fetchone()
                if previous is None:
                    raise HarnessError("evaluation_conflict", "Review identifier is already in use.")
                original = HumanEvaluation.model_validate(previous["document"])
                if original.model_copy(update={"created_at": review.created_at}) != review:
                    raise HarnessError("evaluation_conflict", "Review identifier cannot replace a previous review.")
                return original
            await self.store._audit_insert(connection, envelope.execution, access.principal.subject, (
                AuditEvent(f"evaluation:{review.id}", "evaluation.created", {
                    "evaluation_id": str(review.id), "rubric_version": review.rubric_version,
                    "artifact_sha256": review.artifact_sha256,
                }),
            ))
            return review

    async def list(self, access: Access, identifier: UUID, cursor: UUID, limit: int) -> tuple[HumanEvaluation, ...]:
        await self.artifact(access, identifier)
        async with self.store.transaction() as connection:
            rows = await connection.execute(
                "SELECT document FROM human_evaluations WHERE tenant_id=%s AND execution_id=%s AND id>%s "
                "ORDER BY id LIMIT %s", (access.principal.tenant_id, identifier, cursor, limit),
            )
            return tuple(HumanEvaluation.model_validate(row["document"]) for row in await rows.fetchall())
