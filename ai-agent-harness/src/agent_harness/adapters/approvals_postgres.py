from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from agent_harness.adapters.postgres import PostgresStore, TERMINAL, json_document, number
from agent_harness.adapters.security_postgres import Connection, SecurityRepository
from agent_harness.approvals import definition_digest
from agent_harness.async_utils import OperationCancelled
from agent_harness.audit import AuditEvent
from agent_harness.domain import (
    ApprovalRequest, AuthorizationError, Execution, HarnessError, Status, ToolDescriptor, ToolInvocation,
    execution_deadline,
)
from agent_harness.security import Access, Identity, ROLE_PERMISSIONS
from agent_harness.state import ENVELOPE, Envelope, Lease


APPROVAL = TypeAdapter(ApprovalRequest)


def document(request: ApprovalRequest) -> Jsonb:
    return Jsonb(APPROVAL.dump_python(request, mode="json"))


def same_action(a: ApprovalRequest, b: ApprovalRequest) -> bool:
    fields = (
        "id", "execution_id", "agent_id", "agent_version", "invocation_id", "call_id", "tool_name",
        "tool_version", "arguments", "arguments_sha256", "definition_sha256", "requested_by", "expires_at", "created_at",
    )
    return all(getattr(a, name) == getattr(b, name) for name in fields)


async def update_request(connection: Connection, tenant: UUID, request: ApprovalRequest) -> None:
    version = request.version
    request.version += 1
    cursor = await connection.execute(
        "UPDATE approvals SET status=%s,version=%s,document=%s WHERE tenant_id=%s AND id=%s AND version=%s RETURNING id",
        (request.status, request.version, document(request), tenant, request.id, version),
    )
    if await cursor.fetchone() is None:
        raise HarnessError("approval_conflict", "Approval state changed concurrently.")


async def persist_requests(
    connection: Connection, store: PostgresStore, execution: Execution, actor: str,
) -> None:
    for index, proposed in enumerate(execution.approvals):
        if (
            proposed.execution_id != execution.id or proposed.agent_id != execution.agent_id
            or proposed.agent_version != execution.agent_version or proposed.requested_by != execution.subject
        ):
            raise HarnessError("approval_action_changed", "Approval scope does not match the execution.")
        cursor = await connection.execute(
            "SELECT document FROM approvals WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (execution.tenant_id, proposed.id),
        )
        row = await cursor.fetchone()
        if row is None:
            if proposed.status != "pending" or proposed.decision is not None or proposed.version != 1:
                raise HarnessError("approval_invalid_state", "New approvals must begin undecided.")
            await connection.execute(
                "INSERT INTO approvals VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (execution.tenant_id, proposed.id, execution.id, proposed.invocation_id, execution.subject,
                 proposed.agent_id, proposed.tool_name, proposed.status, proposed.expires_at, proposed.version, document(proposed)),
            )
            current = proposed
            await store._audit_insert(connection, execution, actor, (
                AuditEvent(f"approval:{current.id}:requested", "approval.requested", {"approval_id": str(current.id)}),
            ))
        else:
            current = APPROVAL.validate_python(row["document"])
            if not same_action(proposed, current):
                raise HarnessError("approval_action_changed", "Approval action is immutable.")
        if execution.status in TERMINAL and current.status in ("pending", "approved"):
            current.status = "expired" if execution.status == Status.EXPIRED or current.expires_at <= datetime.now(UTC) else "cancelled"
            await update_request(connection, execution.tenant_id, current)
            await store._audit_insert(connection, execution, actor, (
                AuditEvent(f"approval:{current.id}:{current.status}", "approval." + current.status,
                           {"approval_id": str(current.id)}),
            ))
        execution.approvals[index] = current


class ApprovalRepository:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    @staticmethod
    def visible(access: Access, request: ApprovalRequest) -> bool:
        return request.requested_by == access.principal.subject or (
            "approval:decide" in ROLE_PERMISSIONS[access.role]
            and request.agent_id in access.principal.allowed_agent_ids
            and request.tool_name in access.principal.allowed_tools
        )

    async def get(self, access: Access, identifier: UUID) -> ApprovalRequest:
        access.require("approval:read")
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT document FROM approvals WHERE tenant_id=%s AND id=%s",
                (access.principal.tenant_id, identifier),
            )
            row = await cursor.fetchone()
        if row is None:
            raise AuthorizationError()
        request = APPROVAL.validate_python(row["document"])
        if not self.visible(access, request):
            raise AuthorizationError()
        return effective(request)

    async def list(
        self, access: Access, after: UUID, limit: int, status: str | None,
    ) -> tuple[ApprovalRequest, ...]:
        access.require("approval:read")
        if not 1 <= limit <= 100:
            raise HarnessError("invalid_pagination", "Approval page size is invalid.")
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT document FROM approvals WHERE tenant_id=%s AND id>%s "
                "AND (requested_by=%s OR (%s AND agent_id=ANY(%s) AND tool_name=ANY(%s))) "
                "AND (%s::text IS NULL OR CASE WHEN status IN ('pending','approved') "
                "AND expires_at<=clock_timestamp() THEN 'expired' ELSE status END=%s) ORDER BY id LIMIT %s",
                (access.principal.tenant_id, after, access.principal.subject,
                 "approval:decide" in ROLE_PERMISSIONS[access.role], list(access.principal.allowed_agent_ids),
                 list(access.principal.allowed_tools), status, status, limit),
            )
            return tuple(effective(APPROVAL.validate_python(row["document"])) for row in await cursor.fetchall())

    async def check_reviewer(self, access: Access, request: ApprovalRequest) -> None:
        access.require("approval:decide")
        if request.requested_by == access.principal.subject:
            raise HarnessError("approval_self_forbidden", "Self approval is forbidden.")
        if not self.visible(access, request):
            raise AuthorizationError()
        security = SecurityRepository(self.store.scoped(access.principal.tenant_id))
        await security.agent(access.principal, request.agent_id, request.agent_version)
        grants = await security.tool_grants(access.principal.tenant_id)
        if not grants.get(request.tool_name, (False, 0))[0]:
            raise AuthorizationError()

    async def decide(
        self, access: Access, identifier: UUID, decision: Literal["approve", "reject"],
        reason: str, expected: int, descriptor: ToolDescriptor,
    ) -> ApprovalRequest:
        access.require("approval:decide")
        request = await self.get(access, identifier)
        await self.check_reviewer(access, request)
        expired = False
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT state FROM jobs WHERE tenant_id=%s AND execution_id=%s FOR UPDATE",
                (access.principal.tenant_id, request.execution_id),
            )
            job = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT a.document,e.checkpoint,e.cancel_requested,clock_timestamp() AS now "
                "FROM approvals a JOIN executions e ON (e.tenant_id,e.id)=(a.tenant_id,a.execution_id) "
                "WHERE a.tenant_id=%s AND a.id=%s FOR UPDATE OF a",
                (access.principal.tenant_id, identifier),
            )
            row = await cursor.fetchone()
            if row is None or job is None:
                raise AuthorizationError()
            request = APPROVAL.validate_python(row["document"])
            envelope = ENVELOPE.validate_python(row["checkpoint"])
            if request.decision is not None:
                if request.decided_by == access.principal.subject and request.decision == decision and request.reason == reason:
                    return effective(request)
                raise HarnessError("approval_conflict", "A different decision has already been recorded.")
            if request.version != expected:
                raise HarnessError("precondition_failed", "Approval version changed.")
            if row["cancel_requested"] or job["state"] != "waiting":
                raise HarnessError("approval_conflict", "Execution is not awaiting a decision.")
            if definition_digest(descriptor) != request.definition_sha256:
                raise HarnessError("approval_action_changed", "Installed tool differs from the proposed action.")
            now = timestamp(row)
            if now >= min(request.expires_at, execution_deadline(envelope.execution, envelope.agent)):
                request.status, expired = "expired", True
            else:
                await SecurityRepository(self.store).authorize_execution(envelope.execution, request.tool_name)
                request.decision, request.reason = decision, reason
                request.decided_by, request.decided_at = access.principal.subject, now
                request.status = "approved" if decision == "approve" else "rejected"
            await update_request(connection, access.principal.tenant_id, request)
            await self._wake(connection, envelope)
            await self.store._audit_insert(connection, envelope.execution, access.principal.subject, (
                AuditEvent(f"approval:{request.id}:{request.version}", "approval." + request.status,
                           {"approval_id": str(request.id)}),
            ))
        if expired:
            raise HarnessError("approval_expired", "Approval deadline has elapsed.")
        return request

    async def _wake(self, connection: Connection, envelope: Envelope) -> None:
        execution = envelope.execution
        cursor = await connection.execute(
            "SELECT document FROM approvals WHERE tenant_id=%s AND execution_id=%s ORDER BY id",
            (execution.tenant_id, execution.id),
        )
        execution.approvals = [APPROVAL.validate_python(row["document"]) for row in await cursor.fetchall()]
        ready = not any(item.status == "pending" for item in execution.approvals) or any(
            item.status in ("rejected", "expired", "cancelled") for item in execution.approvals
        )
        execution.status = Status.QUEUED if ready else Status.WAITING_APPROVAL
        cursor = await connection.execute(
            "UPDATE executions SET checkpoint=%s,status=%s,version=version+1 WHERE tenant_id=%s AND id=%s RETURNING version",
            (json_document(envelope), execution.status.value, execution.tenant_id, execution.id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AuthorizationError()
        await connection.execute(
            "INSERT INTO execution_events VALUES (%s,%s,%s,%s,clock_timestamp())",
            (execution.tenant_id, execution.id, number(row, "version"), execution.status.value),
        )
        if ready:
            await connection.execute(
                "UPDATE jobs SET state='queued',worker_id=NULL,lease_until=NULL,available_at=clock_timestamp() "
                "WHERE tenant_id=%s AND execution_id=%s", (execution.tenant_id, execution.id),
            )

    async def consume(
        self, lease: Lease, execution: Execution, invocation: ToolInvocation, descriptor: ToolDescriptor,
    ) -> None:
        if execution.id != lease.execution_id or execution.tenant_id != lease.tenant_id:
            raise AuthorizationError()
        async with self.store.transaction() as connection:
            await self.store._check_lease(connection, lease)
            cursor = await connection.execute(
                "SELECT a.document,e.cancel_requested,clock_timestamp() AS now FROM approvals a "
                "JOIN executions e ON (e.tenant_id,e.id)=(a.tenant_id,a.execution_id) "
                "WHERE a.tenant_id=%s AND a.execution_id=%s AND a.invocation_id=%s FOR UPDATE OF a",
                (execution.tenant_id, execution.id, invocation.id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise HarnessError("approval_required", "A persisted approval is required.")
            if row["cancel_requested"]:
                raise OperationCancelled()
            current = APPROVAL.validate_python(row["document"])
            local = next((item for item in execution.approvals if item.id == current.id), None)
            if (
                local is None or not same_action(local, current) or current.definition_sha256 != definition_digest(descriptor)
                or current.arguments_sha256 != invocation.arguments_sha256 or current.tool_name != invocation.name
                or current.tool_version != invocation.version or current.call_id != invocation.call_id
                or current.requested_by != execution.subject or current.agent_id != execution.agent_id
                or current.agent_version != execution.agent_version or current.execution_id != execution.id
            ):
                raise HarnessError("approval_action_changed", "Approval does not match the dispatch.")
            if timestamp(row) >= current.expires_at:
                raise HarnessError("approval_expired", "Approval has expired before dispatch.")
            if current.status not in ("approved", "consumed") or current.decided_by is None or current.decision != "approve":
                raise HarnessError("approval_required", "An approved human decision is required.")
            if current.status == "consumed" and invocation.side_effects and not invocation.supports_idempotency:
                raise HarnessError("tool_outcome_unknown", "A consumed side-effect approval cannot be replayed.")
            security = SecurityRepository(self.store.scoped(execution.tenant_id))
            reviewer = await security.access(Identity(execution.tenant_id, current.decided_by))
            await self.check_reviewer(reviewer, current)
            await security.authorize_execution(execution, current.tool_name)
            if current.status == "approved":
                current.status = "consumed"
                await update_request(connection, execution.tenant_id, current)
                await self.store._audit_insert(connection, execution, current.decided_by, (
                    AuditEvent(f"approval:{current.id}:consumed", "approval.consumed", {"approval_id": str(current.id)}),
                ))
            execution.approvals[execution.approvals.index(local)] = current


class PostgresApprovalAuthorizer:
    def __init__(self, store: PostgresStore, lease: Lease) -> None:
        self.repository, self.lease = ApprovalRepository(store), lease

    async def consume(self, execution: Execution, invocation: ToolInvocation, descriptor: ToolDescriptor) -> None:
        await self.repository.consume(self.lease, execution, invocation, descriptor)


def effective(request: ApprovalRequest) -> ApprovalRequest:
    if request.status in ("pending", "approved") and request.expires_at <= datetime.now(UTC):
        return replace(request, status="expired")
    return request


def timestamp(row: dict[str, object]) -> datetime:
    value = row["now"]
    if not isinstance(value, datetime):
        raise TypeError("Expected a PostgreSQL timestamp.")
    return value
