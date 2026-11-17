import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agent_harness.adapters.postgres import PostgresStore
from agent_harness.domain import AuthorizationError, Failure, HarnessError, Principal, Status
from agent_harness.json_codec import decode_object
from agent_harness.state import ENVELOPE
from agent_harness.workflows import WORKFLOW, NodeRun, WorkflowDefinition, WorkflowRun, plan_nodes
from agent_harness.audit import AuditEvent
from agent_harness.telemetry import telemetry, traceparent


class WorkflowStore:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    async def submit(
        self, definition: WorkflowDefinition, principal: Principal, identifier: UUID | None = None,
    ) -> UUID:
        with telemetry().operation("admission") as observation:
            result = await self._submit(definition, principal, identifier)
            observation.span.set_attribute("harness.workflow.id", str(result))
            return result

    async def _submit(
        self, definition: WorkflowDefinition, principal: Principal, identifier: UUID | None = None,
    ) -> UUID:
        if not principal.subject.strip() or any(
            node.agent.tenant_id != principal.tenant_id or node.agent.id not in principal.allowed_agent_ids
            for node in definition.nodes
        ):
            raise AuthorizationError()
        run = WorkflowRun(
            definition, principal, id=identifier or uuid4(), traceparent=traceparent(),
            managed=self.store.enforce_security,
        )
        run.nodes = {node.id: NodeRun() for node in definition.nodes}
        async with self.store.transaction() as connection:
            await connection.execute(
                "INSERT INTO workflows(tenant_id,id,owner,status,document) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id,id) DO NOTHING",
                (principal.tenant_id, run.id, principal.subject, run.status,
                 Jsonb(decode_object(WORKFLOW.dump_json(run).decode()))),
            )
            cursor = await connection.execute(
                "SELECT document FROM workflows WHERE tenant_id=%s AND id=%s",
                (principal.tenant_id, run.id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise HarnessError("workflow_missing", "Workflow admission failed.")
            existing = WORKFLOW.validate_python(row["document"])
            if existing.principal.subject != principal.subject:
                raise AuthorizationError()
            if existing.definition != definition or existing.principal != principal:
                raise HarnessError("workflow_conflict", "Workflow ID has different configuration.")
        return run.id

    async def get(self, principal: Principal, identifier: UUID) -> WorkflowRun:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT document FROM workflows WHERE tenant_id=%s AND id=%s AND owner=%s",
                (principal.tenant_id, identifier, principal.subject),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AuthorizationError()
            return WORKFLOW.validate_python(row["document"])

    async def tick(self, tenant_id: UUID | None = None, *, managed_only: bool = False) -> None:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT tenant_id,id FROM workflows WHERE status='running' "
                "AND (%s::uuid IS NULL OR tenant_id=%s) "
                "AND (NOT %s OR COALESCE((document->>'managed')::boolean,false)) LIMIT 100",
                (tenant_id, tenant_id, managed_only),
            )
            identifiers = [(UUID(str(row["tenant_id"])), UUID(str(row["id"]))) for row in await cursor.fetchall()]
        for tenant_id, identifier in identifiers:
            await self.advance(tenant_id, identifier)

    async def advance(self, tenant_id: UUID, identifier: UUID) -> None:
        with telemetry().operation("admission"):
            await self._advance(tenant_id, identifier)

    async def _advance(self, tenant_id: UUID, identifier: UUID) -> None:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT document FROM workflows WHERE tenant_id=%s AND id=%s AND status='running' "
                "FOR UPDATE SKIP LOCKED", (tenant_id, identifier),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            run = WORKFLOW.validate_python(row["document"])
            for node in run.nodes.values():
                if node.execution_id is None or node.status != "queued":
                    continue
                cursor = await connection.execute(
                    "SELECT checkpoint FROM executions WHERE tenant_id=%s AND id=%s",
                    (tenant_id, node.execution_id),
                )
                child = await cursor.fetchone()
                if child is None:
                    raise HarnessError("workflow_corrupt", "Workflow child execution is missing.")
                execution = ENVELOPE.validate_python(child["checkpoint"]).execution
                if execution.status == Status.SUCCEEDED:
                    node.status, node.result = "succeeded", execution.result
                elif execution.status in (Status.FAILED, Status.EXPIRED, Status.CANCELLED):
                    node.status = "failed"
                    run.failure = execution.failure or Failure("child_cancelled", "A workflow child was cancelled.")
            ready = plan_nodes(run, datetime.now(UTC))
            if run.failure is None:
                for spec in ready:
                    node = run.nodes[spec.id]
                    session_id = uuid4()
                    payload = spec.input
                    if spec.handoff_from:
                        payload = json.dumps({
                            "task": spec.input, "trust": "untrusted_agent_handoff",
                            "handoff": {name: run.nodes[name].result for name in spec.handoff_from},
                        })
                    if len(payload.encode("utf-8")) > spec.agent.limits.max_input_bytes:
                        node.status = "failed"
                        run.failure = Failure("workflow_handoff_too_large", "Workflow handoff exceeds the input limit.")
                        break
                    try:
                        async with connection.transaction():
                            await connection.execute(
                                "INSERT INTO sessions(tenant_id,id,owner) VALUES (%s,%s,%s)",
                                (tenant_id, session_id, run.principal.subject),
                            )
                            node.execution_id = await self.store.enqueue(
                                connection, spec.agent, run.principal, session_id, payload,
                                f"workflow:{run.id}:{spec.id}",
                                deadline_at=run.created_at + timedelta(seconds=run.definition.timeout_seconds),
                                trace_link=run.traceparent, managed=run.managed,
                            )
                    except HarnessError as exc:
                        if exc.code not in {"quota_exceeded", "quota_not_configured", "forbidden", "access_denied"}:
                            raise
                        node.status = "failed"
                        run.failure = Failure(exc.code, exc.message)
                        break
                    node.status = "queued"
            if run.failure is not None:
                plan_nodes(run, datetime.now(UTC))
                for node in run.nodes.values():
                    if node.status == "queued":
                        await connection.execute(
                            "SELECT execution_id FROM jobs WHERE tenant_id=%s AND execution_id=%s FOR UPDATE",
                            (tenant_id, node.execution_id),
                        )
                        cursor = await connection.execute(
                            "UPDATE executions SET cancel_requested=true WHERE tenant_id=%s AND id=%s "
                            "RETURNING checkpoint", (tenant_id, node.execution_id),
                        )
                        cancelled = await cursor.fetchone()
                        if cancelled is None:
                            raise HarnessError("workflow_corrupt", "Workflow cancellation target is missing.")
                        await connection.execute(
                            "UPDATE jobs SET state='queued',available_at=clock_timestamp(),lease_until=NULL "
                            "WHERE tenant_id=%s AND execution_id=%s AND state='waiting'",
                            (tenant_id, node.execution_id),
                        )
                        await self.store._audit_insert(
                            connection, ENVELOPE.validate_python(cancelled["checkpoint"]).execution,
                            f"workflow:{run.id}", (
                                AuditEvent(f"workflow_cancel:{run.id}", "execution.cancel_requested",
                                           {"workflow_id": str(run.id)}),
                            ),
                        )
            await connection.execute(
                "UPDATE workflows SET status=%s,document=%s WHERE tenant_id=%s AND id=%s",
                (run.status, Jsonb(decode_object(WORKFLOW.dump_json(run).decode())), tenant_id, identifier),
            )
