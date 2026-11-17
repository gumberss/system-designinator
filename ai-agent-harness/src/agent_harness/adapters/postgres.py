import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_harness.domain import (
    Agent, AuthorizationError, Execution, HarnessError, Message, Principal, SessionConflict, Status,
    RetryPolicy, execution_deadline,
)
from agent_harness.json_codec import decode_object
from agent_harness.resilience import LocalCircuitBreaker, Outcome
from agent_harness.audit import AuditEntry, AuditEvent, checkpoint_events, execution_metrics
from agent_harness.telemetry import telemetry, traceparent
from datetime import UTC
from copy import copy
from agent_harness.state import (
    ENVELOPE, MESSAGE, SUMMARY, Envelope, HistoryMessage, Lease, LeaseLost, SessionContext, Summary,
)


TERMINAL = frozenset({Status.SUCCEEDED, Status.FAILED, Status.CANCELLED, Status.EXPIRED})


def json_document(envelope: Envelope) -> Jsonb:
    return Jsonb(decode_object(ENVELOPE.dump_json(envelope).decode("utf-8")))


def number(row: dict[str, object], key: str) -> int:
    value = row[key]
    if not isinstance(value, int):
        raise TypeError(f"Database field {key} must be an integer.")
    return value


def request_fingerprints(
    agent: Agent, session_id: UUID, user_input: str, deadline_at: datetime | None,
) -> tuple[str, frozenset[str]]:
    document = agent.model_dump(mode="json")
    document["allowed_tools"] = sorted(agent.allowed_tools)
    if agent.approval_policy is not None:
        document["approval_policy"]["required_tools"] = sorted(agent.approval_policy.required_tools)

    def fingerprint(value: dict[str, object]) -> str:
        return hashlib.sha256(json.dumps({
            "agent": value, "session": str(session_id), "input": user_input,
            "deadline": deadline_at.isoformat() if deadline_at else None,
        }, sort_keys=True).encode()).hexdigest()

    current = fingerprint(document)
    fingerprints = {current}
    if agent.approval_policy is None:
        before_approvals = dict(document)
        before_approvals.pop("approval_policy")
        fingerprints.add(fingerprint(before_approvals))
    if agent.result_schema is None:
        before_output_policy = dict(document)
        before_output_policy.pop("result_schema")
        if agent.approval_policy is None:
            before_output_policy.pop("approval_policy")
        fingerprints.add(fingerprint(before_output_policy))
    if (
        not agent.fallbacks and agent.selection.capabilities is None
        and agent.result_schema is None
        and agent.approval_policy is None
        and agent.retry == RetryPolicy(max_attempts=1, attempt_timeout_seconds=300)
        and agent.limits.max_model_calls == 256 and agent.limits.max_tool_attempts == 256
    ):
        legacy = agent.model_dump(mode="json")
        legacy["allowed_tools"] = sorted(agent.allowed_tools)
        legacy.pop("retry")
        legacy.pop("fallbacks")
        legacy.pop("result_schema")
        legacy.pop("approval_policy")
        legacy["selection"].pop("capabilities")
        legacy["limits"].pop("max_model_calls")
        legacy["limits"].pop("max_tool_attempts")
        fingerprints.add(fingerprint(legacy))
    return current, frozenset(fingerprints)


class PostgresStore:
    def __init__(self, dsn: str, *, statement_timeout_ms: int = 5000) -> None:
        if not 1 <= statement_timeout_ms <= 300000:
            raise ValueError("Statement timeout must be in [1, 300000] milliseconds.")
        self._dsn = dsn
        self._options = (
            f"-c statement_timeout={statement_timeout_ms} "
            f"-c lock_timeout={min(statement_timeout_ms, 3000)} "
            "-c idle_in_transaction_session_timeout=10000 -c search_path=public"
        )
        self._breaker = LocalCircuitBreaker()
        self.tenant_id: UUID | None = None
        self.enforce_security = False

    def scoped(self, tenant_id: UUID, *, enforce_security: bool = True) -> "PostgresStore":
        store = copy(self)
        store.tenant_id, store.enforce_security = tenant_id, enforce_security
        return store

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection[dict[str, object]]]:
        permit = await self._breaker.acquire("postgres")
        outcome: Outcome = "neutral"
        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, row_factory=dict_row, connect_timeout=5, options=self._options,
            ) as connection:
                outcome = "success"
                if self.tenant_id is not None:
                    await connection.execute(
                        "SELECT set_config('harness.tenant_id',%s,true)", (str(self.tenant_id),),
                    )
                yield connection
        except psycopg.OperationalError:
            outcome = "failure"
            raise HarnessError("storage_unavailable", "PostgreSQL is unavailable.") from None
        finally:
            await self._breaker.finish(permit, outcome)

    async def migrate(self) -> None:
        async with self.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(64732040)")
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS harness_schema_migrations (version integer PRIMARY KEY)"
            )
            cursor = await connection.execute("SELECT version FROM harness_schema_migrations")
            versions = {number(row, "version") for row in await cursor.fetchall()}
            if versions - {1, 2, 3, 4, 5}:
                raise HarnessError("schema_incompatible", "Database schema is newer than this application.")
            if 1 not in versions:
                await connection.execute(files("agent_harness.adapters").joinpath("schema.sql").read_text())
                await connection.execute("INSERT INTO harness_schema_migrations VALUES (1)")
            if 2 not in versions:
                await connection.execute(files("agent_harness.adapters").joinpath("observability.sql").read_text())
                await connection.execute("INSERT INTO harness_schema_migrations VALUES (2)")
            if 3 not in versions:
                await connection.execute(files("agent_harness.adapters").joinpath("security.sql").read_text())
                await connection.execute("INSERT INTO harness_schema_migrations VALUES (3)")
            if 4 not in versions:
                await connection.execute(files("agent_harness.adapters").joinpath("approvals.sql").read_text())
                await connection.execute("INSERT INTO harness_schema_migrations VALUES (4)")
            if 5 not in versions:
                await connection.execute(files("agent_harness.adapters").joinpath("evaluations.sql").read_text())
                await connection.execute("INSERT INTO harness_schema_migrations VALUES (5)")

    async def create_session(self, principal: Principal, session_id: UUID | None = None) -> UUID:
        if not principal.subject.strip():
            raise AuthorizationError()
        identifier = session_id or uuid4()
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO sessions(tenant_id,id,owner) VALUES (%s,%s,%s)",
                (principal.tenant_id, identifier, principal.subject),
            )
        return identifier

    async def _session(
        self, connection: psycopg.AsyncConnection[dict[str, object]], principal: Principal,
        session_id: UUID, *, lock: bool = False,
    ) -> dict[str, object]:
        query = "SELECT * FROM sessions WHERE tenant_id=%s AND id=%s AND owner=%s"
        if lock:
            query += " FOR UPDATE"
        cursor = await connection.execute(query, (principal.tenant_id, session_id, principal.subject))
        row = await cursor.fetchone()
        if row is None:
            raise AuthorizationError()
        return row

    async def submit(
        self, agent: Agent, principal: Principal, session_id: UUID,
        user_input: str, idempotency_key: str,
    ) -> UUID:
        with telemetry().operation("admission") as observation:
            identifier = await self._submit(agent, principal, session_id, user_input, idempotency_key)
            observation.span.set_attribute("harness.execution.id", str(identifier))
            return identifier

    async def _submit(
        self, agent: Agent, principal: Principal, session_id: UUID,
        user_input: str, idempotency_key: str,
    ) -> UUID:
        async with self.transaction() as connection:
            return await self.enqueue(
                connection, agent, principal, session_id, user_input, idempotency_key
            )

    async def enqueue(
        self, connection: psycopg.AsyncConnection[dict[str, object]], agent: Agent,
        principal: Principal, session_id: UUID, user_input: str, idempotency_key: str,
        *, deadline_at: datetime | None = None,
        trace_link: str | None = None,
        managed: bool | None = None,
    ) -> UUID:
        if (
            principal.tenant_id != agent.tenant_id or agent.id not in principal.allowed_agent_ids
            or not principal.subject.strip()
        ):
            raise AuthorizationError()
        if (
            not user_input.strip() or len(user_input.encode("utf-8")) > agent.limits.max_input_bytes
            or not 1 <= len(idempotency_key) <= 128
        ):
            raise HarnessError("invalid_input", "Input or idempotency key is invalid.")
        fingerprint, compatible_fingerprints = request_fingerprints(
            agent, session_id, user_input, deadline_at,
        )
        session = await self._session(connection, principal, session_id, lock=True)
        cursor = await connection.execute(
            "SELECT id,request_hash FROM executions WHERE tenant_id=%s AND owner=%s AND idempotency_key=%s",
            (principal.tenant_id, principal.subject, idempotency_key),
        )
        previous = await cursor.fetchone()
        if previous:
            if previous["request_hash"] not in compatible_fingerprints:
                raise HarnessError("idempotency_conflict", "Idempotency key has a different request.")
            return UUID(str(previous["id"]))
        if session["active_execution_id"] is not None:
            raise SessionConflict()
        execution = Execution(principal.tenant_id, principal.subject, agent.id, agent.version, session_id)
        execution.managed = self.enforce_security if managed is None else managed
        execution.traceparent = trace_link or traceparent()
        execution.deadline_at = deadline_at
        execution.deadline_at = execution_deadline(execution, agent)
        envelope = Envelope(agent, principal, user_input, execution)
        await connection.execute(
            "INSERT INTO executions(tenant_id,id,session_id,owner,idempotency_key,request_hash,status,checkpoint) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (principal.tenant_id, execution.id, session_id, principal.subject,
             idempotency_key, fingerprint, execution.status.value, json_document(envelope)),
        )
        await connection.execute(
            "INSERT INTO jobs(tenant_id,execution_id) VALUES (%s,%s)", (principal.tenant_id, execution.id)
        )
        if execution.managed:
            from agent_harness.adapters.security_postgres import SecurityRepository, reserve_quota
            await SecurityRepository(self.scoped(execution.tenant_id)).authorize_execution(execution)
            await reserve_quota(connection, agent, execution)
        await connection.execute(
            "UPDATE sessions SET active_execution_id=%s WHERE tenant_id=%s AND id=%s",
            (execution.id, principal.tenant_id, session_id),
        )
        await self._audit_insert(connection, execution, principal.subject, (
            AuditEvent("admitted", "execution.admitted", {"agent_id": str(agent.id), "agent_version": agent.version}),
        ))
        return execution.id

    async def get(self, principal: Principal, execution_id: UUID) -> Envelope:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT checkpoint FROM executions WHERE tenant_id=%s AND id=%s AND owner=%s",
                (principal.tenant_id, execution_id, principal.subject),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AuthorizationError()
            return ENVELOPE.validate_python(row["checkpoint"])

    async def cancel(self, principal: Principal, execution_id: UUID) -> None:
        with telemetry().operation("cancel") as observation:
            observation.span.set_attribute("harness.execution.id", str(execution_id))
            await self._cancel(principal, execution_id)

    async def _cancel(self, principal: Principal, execution_id: UUID) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "SELECT j.execution_id FROM jobs j JOIN executions e ON "
                "(e.tenant_id,e.id)=(j.tenant_id,j.execution_id) "
                "WHERE e.tenant_id=%s AND e.id=%s AND e.owner=%s FOR UPDATE OF j",
                (principal.tenant_id, execution_id, principal.subject),
            )
            cursor = await connection.execute(
                "UPDATE executions SET cancel_requested=true WHERE tenant_id=%s AND id=%s AND owner=%s "
                "RETURNING checkpoint", (principal.tenant_id, execution_id, principal.subject),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AuthorizationError()
            await connection.execute(
                "UPDATE jobs SET state='queued',available_at=clock_timestamp(),lease_until=NULL "
                "WHERE tenant_id=%s AND execution_id=%s AND state='waiting'",
                (principal.tenant_id, execution_id),
            )
            execution = ENVELOPE.validate_python(row["checkpoint"]).execution
            await self._audit_insert(connection, execution, principal.subject, (
                AuditEvent("cancel_requested", "execution.cancel_requested", {}),
            ))

    async def claim(
        self, worker_id: UUID, lease_seconds: float = 30, tenant_id: UUID | None = None,
        *, managed_only: bool = False,
    ) -> Lease | None:
        if not 0 < lease_seconds <= 300:
            raise ValueError("Lease duration must be in (0, 300].")
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT j.tenant_id,j.execution_id,e.checkpoint,e.version,e.cancel_requested,"
                "EXISTS(SELECT 1 FROM execution_audit a WHERE a.tenant_id=e.tenant_id "
                "AND a.execution_id=e.id) AS has_audit "
                "FROM jobs j JOIN executions e ON (e.tenant_id,e.id)=(j.tenant_id,j.execution_id) "
                "WHERE j.state<>'done' AND j.available_at<=clock_timestamp() "
                "AND (%s::uuid IS NULL OR j.tenant_id=%s) "
                "AND (NOT %s OR COALESCE((e.checkpoint->'execution'->>'managed')::boolean,false)) "
                "AND (j.state IN ('queued','waiting') OR j.lease_until<=clock_timestamp()) "
                "ORDER BY j.available_at,j.execution_id FOR UPDATE OF j SKIP LOCKED LIMIT 1",
                (tenant_id, tenant_id, managed_only),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await connection.execute(
                "UPDATE jobs SET state='running',worker_id=%s,fencing_token=fencing_token+1,"
                "lease_until=clock_timestamp()+%s*interval '1 second' "
                "WHERE tenant_id=%s AND execution_id=%s RETURNING fencing_token",
                (worker_id, lease_seconds, row["tenant_id"], row["execution_id"]),
            )
            token = await cursor.fetchone()
            if token is None:
                raise LeaseLost()
            lease = Lease(
                UUID(str(row["tenant_id"])), UUID(str(row["execution_id"])), worker_id,
                number(token, "fencing_token"), number(row, "version"),
                ENVELOPE.validate_python(row["checkpoint"]), bool(row["cancel_requested"]),
            )
            execution = lease.envelope.execution
            await self._audit_insert(connection, execution, f"worker:{worker_id}", (
                AuditEvent(
                    f"claimed:{lease.token}", "execution.recovered" if lease.token > 1 else "execution.claimed",
                    {"fencing_token": lease.token, "version": lease.version},
                ),
            ))
            if not row["has_audit"]:
                await self._audit_insert(
                    connection, execution, f"worker:{worker_id}",
                    (AuditEvent("baseline", "execution.baseline", execution_metrics(execution)),)
                    + tuple(AuditEvent(
                        "baseline:" + event.key, "baseline." + event.kind, event.data,
                    ) for event in checkpoint_events(execution)),
                )
        telemetry().queue_age.record(
            max(0, (datetime.now(UTC) - execution.created_at).total_seconds()), {"recovery": lease.token > 1},
        )
        if lease.token > 1:
            telemetry().recoveries.add(1)
        return lease

    async def observe_queue(self, tenant_id: UUID | None) -> None:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT state,count(*) AS total FROM jobs WHERE state<>'done' "
                "AND (%s::uuid IS NULL OR tenant_id=%s) GROUP BY state",
                (tenant_id, tenant_id),
            )
            counts = {str(row["state"]): number(row, "total") for row in await cursor.fetchall()}
        for state in ("queued", "running", "waiting"):
            telemetry().queue_depth.set(counts.get(state, 0), {"state": state})

    async def _check_lease(
        self, connection: psycopg.AsyncConnection[dict[str, object]], lease: Lease,
    ) -> None:
        cursor = await connection.execute(
            "SELECT fencing_token FROM jobs WHERE tenant_id=%s AND execution_id=%s AND worker_id=%s "
            "AND fencing_token=%s AND state='running' AND lease_until>clock_timestamp() FOR UPDATE",
            (lease.tenant_id, lease.execution_id, lease.worker_id, lease.token),
        )
        if await cursor.fetchone() is None:
            raise LeaseLost()

    async def heartbeat(self, lease: Lease, lease_seconds: float) -> bool:
        async with self.transaction() as connection:
            await self._check_lease(connection, lease)
            await connection.execute(
                "UPDATE jobs SET lease_until=clock_timestamp()+%s*interval '1 second' "
                "WHERE tenant_id=%s AND execution_id=%s",
                (lease_seconds, lease.tenant_id, lease.execution_id),
            )
            cursor = await connection.execute(
                "SELECT cancel_requested FROM executions WHERE tenant_id=%s AND id=%s",
                (lease.tenant_id, lease.execution_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LeaseLost()
            return bool(row["cancel_requested"])

    async def save(self, lease: Lease, execution: Execution) -> None:
        with telemetry().operation("checkpoint", execution=execution):
            await self._save(lease, execution)

    async def _save(self, lease: Lease, execution: Execution) -> None:
        if execution.id != lease.execution_id or execution.tenant_id != lease.tenant_id:
            raise AuthorizationError()
        lease.envelope.execution = execution
        async with self.transaction() as connection:
            await self._check_lease(connection, lease)
            cursor = await connection.execute(
                "SELECT checkpoint,cancel_requested FROM executions WHERE tenant_id=%s AND id=%s",
                (lease.tenant_id, lease.execution_id),
            )
            previous = await cursor.fetchone()
            if previous is None:
                raise LeaseLost()
            if execution.status == Status.WAITING_APPROVAL and previous["cancel_requested"]:
                execution.status, execution.finished_at = Status.CANCELLED, datetime.now(UTC)
            if execution.approvals:
                from agent_harness.adapters.approvals_postgres import persist_requests
                await persist_requests(connection, self, execution, f"worker:{lease.worker_id}")
            old_events = {
                event.key for event in checkpoint_events(ENVELOPE.validate_python(previous["checkpoint"]).execution)
            }
            cursor = await connection.execute(
                "UPDATE executions SET checkpoint=%s,status=%s,version=version+1 "
                "WHERE tenant_id=%s AND id=%s AND version=%s RETURNING version",
                (json_document(lease.envelope), execution.status.value,
                 lease.tenant_id, lease.execution_id, lease.version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LeaseLost()
            version = number(row, "version")
            await connection.execute(
                "INSERT INTO execution_events(tenant_id,execution_id,version,status) VALUES (%s,%s,%s,%s)",
                (lease.tenant_id, lease.execution_id, version, execution.status.value),
            )
            await self._audit_insert(
                connection, execution, f"worker:{lease.worker_id}",
                (AuditEvent(
                    f"checkpoint:{version}", "execution.checkpoint",
                    {**execution_metrics(execution), "version": version, "fencing_token": lease.token},
                ),) + tuple(event for event in checkpoint_events(execution) if event.key not in old_events),
            )
            if execution.status == Status.WAITING_APPROVAL:
                deadlines = [item.expires_at for item in execution.approvals if item.status in ("pending", "approved")]
                if not deadlines:
                    raise HarnessError("approval_invalid_state", "Waiting requires an unresolved approval.")
                await connection.execute(
                    "UPDATE jobs SET state='waiting',worker_id=NULL,lease_until=NULL,available_at=%s "
                    "WHERE tenant_id=%s AND execution_id=%s",
                    (min(deadlines), execution.tenant_id, execution.id),
                )
            if execution.status in TERMINAL:
                if execution.managed:
                    from agent_harness.adapters.security_postgres import reconcile_quota
                    await reconcile_quota(connection, execution)
                session = await self._session(
                    connection, lease.envelope.principal, execution.session_id, lock=True
                )
                if session["active_execution_id"] != execution.id:
                    raise LeaseLost()
                seq = number(session, "next_seq")
                response = execution.result if execution.result is not None else (
                    f"Execution {execution.status}: "
                    f"{execution.failure.code if execution.failure else 'cancelled'}"
                )
                for offset, message in enumerate(
                    (Message("user", lease.envelope.user_input), Message("assistant", response)), 1
                ):
                    await connection.execute(
                        "INSERT INTO messages(tenant_id,session_id,seq,payload) VALUES (%s,%s,%s,%s)",
                        (lease.tenant_id, execution.session_id, seq + offset,
                         Jsonb(decode_object(MESSAGE.dump_json(message).decode()))),
                    )
                await connection.execute(
                    "UPDATE sessions SET next_seq=%s,active_execution_id=NULL WHERE tenant_id=%s AND id=%s",
                    (seq + 2, lease.tenant_id, execution.session_id),
                )
                await connection.execute(
                    "UPDATE jobs SET state='done',lease_until=NULL WHERE tenant_id=%s AND execution_id=%s",
                    (lease.tenant_id, execution.id),
                )
        lease.version = version

    async def _audit_insert(
        self, connection: psycopg.AsyncConnection[dict[str, object]], execution: Execution,
        actor: str, events: tuple[AuditEvent, ...],
    ) -> None:
        if not events:
            return
        await connection.execute(
            "INSERT INTO execution_audit(tenant_id,execution_id,event_key,kind,actor,data) "
            "SELECT %s,%s,event_key,kind,%s,data FROM jsonb_to_recordset(%s) "
            "AS event(event_key text,kind text,data jsonb) "
            "ON CONFLICT (tenant_id,execution_id,event_key) DO NOTHING",
            (execution.tenant_id, execution.id, actor, Jsonb([
                {"event_key": event.key, "kind": event.kind, "data": event.data} for event in events
            ])),
        )

    async def audit(
        self, principal: Principal, execution_id: UUID, *, after: int = 0, limit: int = 100,
    ) -> tuple[AuditEntry, ...]:
        if after < 0 or not 1 <= limit <= 500:
            raise HarnessError("invalid_pagination", "Audit cursor must be nonnegative and limit in [1, 500].")
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id FROM executions WHERE tenant_id=%s AND id=%s AND owner=%s",
                (principal.tenant_id, execution_id, principal.subject),
            )
            if await cursor.fetchone() is None:
                raise AuthorizationError()
            cursor = await connection.execute(
                "SELECT sequence,kind,actor,created_at,data FROM execution_audit "
                "WHERE tenant_id=%s AND execution_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                (principal.tenant_id, execution_id, after, limit),
            )
            entries = []
            for row in await cursor.fetchall():
                created_at, data = row["created_at"], row["data"]
                if not isinstance(created_at, datetime) or not isinstance(data, dict):
                    raise HarnessError("invalid_audit_record", "Stored audit record violates its schema.")
                entries.append(AuditEntry(
                    number(row, "sequence"), str(row["kind"]), str(row["actor"]), created_at, data,
                ))
            return tuple(entries)

    async def context(self, principal: Principal, session_id: UUID) -> SessionContext:
        async with self.transaction() as connection:
            session = await self._session(connection, principal, session_id)
            summary = SUMMARY.validate_python(session["summary"]) if session["summary"] else None
            cursor = await connection.execute(
                "SELECT seq,payload FROM messages WHERE tenant_id=%s AND session_id=%s AND seq>%s ORDER BY seq",
                (principal.tenant_id, session_id, summary.through_seq if summary else 0),
            )
            history = tuple(
                HistoryMessage(number(row, "seq"), MESSAGE.validate_python(row["payload"]))
                for row in await cursor.fetchall()
            )
            cursor = await connection.execute(
                "SELECT content FROM memories WHERE tenant_id=%s AND session_id=%s "
                "AND expires_at>clock_timestamp() ORDER BY key LIMIT 65",
                (principal.tenant_id, session_id),
            )
            memories = tuple(str(row["content"]) for row in await cursor.fetchall())
            if len(memories) > 64:
                raise HarnessError("memory_limit", "Session memory item limit exceeded.")
            return SessionContext(history, summary, memories)

    async def save_summary(self, lease: Lease, summary: Summary) -> None:
        async with self.transaction() as connection:
            await self._check_lease(connection, lease)
            await connection.execute(
                "UPDATE sessions SET summary=%s WHERE tenant_id=%s AND id=%s AND owner=%s",
                (Jsonb(decode_object(SUMMARY.dump_json(summary).decode())), lease.tenant_id,
                 lease.envelope.execution.session_id, lease.envelope.principal.subject),
            )

    async def put_memory(
        self, principal: Principal, session_id: UUID, key: str, content: str, ttl_seconds: int = 86400,
    ) -> None:
        if not 1 <= len(key) <= 128 or not 1 <= len(content.encode()) <= 8192 or not 0 < ttl_seconds <= 2592000:
            raise HarnessError("invalid_memory", "Memory key, content or expiration is invalid.")
        async with self.transaction() as connection:
            await self._session(connection, principal, session_id, lock=True)
            cursor = await connection.execute(
                "SELECT count(*) AS total FROM memories WHERE tenant_id=%s AND session_id=%s "
                "AND key<>%s AND expires_at>clock_timestamp()",
                (principal.tenant_id, session_id, key),
            )
            row = await cursor.fetchone()
            if row is None or number(row, "total") >= 64:
                raise HarnessError("memory_limit", "Session memory item limit exceeded.")
            await connection.execute(
                "INSERT INTO memories(tenant_id,session_id,key,content,expires_at) "
                "VALUES (%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second') "
                "ON CONFLICT (tenant_id,session_id,key) DO UPDATE SET "
                "content=EXCLUDED.content,expires_at=EXCLUDED.expires_at",
                (principal.tenant_id, session_id, key, content, ttl_seconds),
            )


class PostgresWriter:
    def __init__(self, store: PostgresStore, lease: Lease) -> None:
        self.store, self.lease = store, lease

    async def save(self, execution: Execution) -> None:
        await self.store.save(self.lease, execution)
