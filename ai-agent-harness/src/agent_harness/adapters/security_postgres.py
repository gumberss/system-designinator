from datetime import date
from decimal import Decimal
from typing import TypeAlias
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from agent_harness.adapters.postgres import PostgresStore, number
from agent_harness.domain import Agent, AuthorizationError, Execution, HarnessError, Principal
from agent_harness.security import Access, Identity, reservation


Connection: TypeAlias = psycopg.AsyncConnection[dict[str, object]]


def decimal_value(row: dict[str, object], key: str) -> Decimal:
    value = row[key]
    if not isinstance(value, Decimal):
        raise TypeError("Expected a PostgreSQL numeric field.")
    return value


async def reserve_quota(connection: Connection, agent: Agent, execution: Execution) -> None:
    cursor = await connection.execute(
        "SELECT *,(clock_timestamp() AT TIME ZONE 'UTC')::date AS day FROM quota_policy WHERE tenant_id=%s FOR UPDATE",
        (execution.tenant_id,),
    )
    policy = await cursor.fetchone()
    if policy is None:
        raise HarnessError("quota_not_configured", "Tenant execution quota is not configured.")
    cursor = await connection.execute(
        "SELECT count(*) AS total FROM quota_reservations WHERE tenant_id=%s AND active",
        (execution.tenant_id,),
    )
    active = await cursor.fetchone()
    if active is None or number(active, "total") >= number(policy, "max_active"):
        raise HarnessError("quota_exceeded", "Concurrent execution quota is exhausted.")
    day = policy["day"]
    if not isinstance(day, date):
        raise TypeError("Expected a database quota day.")
    await connection.execute(
        "INSERT INTO quota_days(tenant_id,day) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (execution.tenant_id, day),
    )
    cursor = await connection.execute(
        "SELECT * FROM quota_days WHERE tenant_id=%s AND day=%s FOR UPDATE", (execution.tenant_id, day),
    )
    usage = await cursor.fetchone()
    if usage is None:
        raise HarnessError("quota_unavailable", "Quota ledger is unavailable.")
    tokens, cost = reservation(agent)
    if (
        number(usage, "executions") + 1 > number(policy, "max_daily_executions")
        or number(usage, "tokens") + tokens > number(policy, "max_daily_tokens")
        or decimal_value(usage, "cost") + cost > decimal_value(policy, "max_daily_cost")
    ):
        raise HarnessError("quota_exceeded", "Daily execution budget is exhausted.")
    await connection.execute(
        "UPDATE quota_days SET executions=executions+1,tokens=tokens+%s,cost=cost+%s WHERE tenant_id=%s AND day=%s",
        (tokens, cost, execution.tenant_id, day),
    )
    await connection.execute(
        "INSERT INTO quota_reservations(tenant_id,execution_id,day,tokens,cost) VALUES (%s,%s,%s,%s,%s)",
        (execution.tenant_id, execution.id, day, tokens, cost),
    )


async def reconcile_quota(connection: Connection, execution: Execution) -> None:
    await connection.execute("SELECT tenant_id FROM quota_policy WHERE tenant_id=%s FOR UPDATE", (execution.tenant_id,))
    cursor = await connection.execute(
        "SELECT * FROM quota_reservations WHERE tenant_id=%s AND execution_id=%s AND active FOR UPDATE",
        (execution.tenant_id, execution.id),
    )
    record = await cursor.fetchone()
    if record is None:
        raise HarnessError("quota_unavailable", "Active execution reservation is missing.")
    tokens = execution.input_tokens + execution.output_tokens
    cost = execution.estimated_cost_usd
    if not execution.usage_complete:
        tokens, cost = max(tokens, number(record, "tokens")), max(cost, decimal_value(record, "cost"))
    await connection.execute(
        "UPDATE quota_days SET tokens=tokens+%s,cost=cost+%s WHERE tenant_id=%s AND day=%s",
        (tokens - number(record, "tokens"), cost - decimal_value(record, "cost"), execution.tenant_id, record["day"]),
    )
    await connection.execute(
        "UPDATE quota_reservations SET active=false,tokens=%s,cost=%s,usage_complete=%s "
        "WHERE tenant_id=%s AND execution_id=%s",
        (tokens, cost, execution.usage_complete, execution.tenant_id, execution.id),
    )


class SecurityRepository:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store

    async def verify_api_role(self, *, worker: bool = False) -> None:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT rolsuper,rolbypassrls,pg_has_role(current_user,'harness_worker','member') AS worker,"
                "pg_has_role(current_user,'harness_api','member') AS api,"
                "EXISTS (SELECT 1 FROM pg_roles elevated WHERE "
                "(elevated.rolsuper OR elevated.rolbypassrls OR elevated.rolcreaterole OR elevated.rolcreatedb "
                "OR elevated.rolreplication OR elevated.rolname NOT IN(current_user,'harness_api','harness_worker')) "
                "AND pg_has_role(current_user,elevated.oid,'member')) AS elevated "
                "FROM pg_roles WHERE rolname=current_user"
            )
            role = await cursor.fetchone()
            if (
                role is None or role["rolsuper"] or role["rolbypassrls"] or role["elevated"]
                or (not worker and (role["worker"] or not role["api"]))
                or (worker and (not role["worker"] or role["api"]))
            ):
                raise HarnessError("unsafe_database_role", "API requires a restricted harness_api database login.")
            cursor = await connection.execute(
                "SELECT count(*) AS total FROM pg_class WHERE relnamespace='public'::regnamespace "
                "AND relname=ANY(%s) AND relrowsecurity AND relforcerowsecurity "
                "AND NOT pg_has_role(current_user,relowner,'member')",
                (["sessions", "executions", "memberships", "agent_catalog", "tool_catalog", "quota_policy",
                  "quota_days", "quota_reservations", "execution_audit", "jobs", "messages", "memories",
                  "execution_events", "workflows", "approvals", "human_evaluations"],),
            )
            state = await cursor.fetchone()
            if state is None or number(state, "total") != 16:
                raise HarnessError("unsafe_database_role", "Migrate RLS tables with a separate non-API owner.")

    async def access(self, identity: Identity) -> Access:
        async with self.store.scoped(identity.tenant_id).transaction() as connection:
            cursor = await connection.execute(
                "SELECT role,allowed_agents,allowed_tools FROM memberships "
                "WHERE tenant_id=%s AND subject=%s AND active", (identity.tenant_id, identity.subject),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HarnessError("forbidden", "No active tenant membership exists.")
        return TypeAdapter(Access).validate_python({"principal": Principal(
            identity.tenant_id, identity.subject,
            frozenset(TypeAdapter(list[UUID]).validate_python(row["allowed_agents"])),
            frozenset(TypeAdapter(list[str]).validate_python(row["allowed_tools"])),
        ), "role": row["role"]})

    async def agent(self, principal: Principal, identifier: UUID, version: int | None = None) -> Agent:
        if identifier not in principal.allowed_agent_ids:
            raise AuthorizationError()
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT document,enabled FROM agent_catalog WHERE tenant_id=%s AND id=%s "
                "AND (%s::integer IS NULL OR version=%s) ORDER BY version DESC LIMIT 1",
                (principal.tenant_id, identifier, version, version),
            )
            row = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT enabled FROM agent_catalog WHERE tenant_id=%s AND id=%s ORDER BY version DESC LIMIT 1",
                (principal.tenant_id, identifier),
            )
            latest = await cursor.fetchone()
        if row is None or not row["enabled"] or latest is None or not latest["enabled"]:
            raise AuthorizationError()
        return Agent.model_validate(row["document"])

    async def save_agent(self, principal: Principal, agent: Agent, expected: int | None) -> None:
        if agent.id not in principal.allowed_agent_ids or agent.tenant_id != principal.tenant_id:
            raise AuthorizationError()
        async with self.store.transaction() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"agent:{agent.tenant_id}:{agent.id}",),
            )
            cursor = await connection.execute(
                "SELECT version FROM agent_catalog WHERE tenant_id=%s AND id=%s ORDER BY version DESC LIMIT 1 FOR UPDATE",
                (agent.tenant_id, agent.id),
            )
            previous = await cursor.fetchone()
            if (previous is None and expected is not None) or (
                previous is not None and (expected is None or number(previous, "version") != expected)
            ):
                raise HarnessError("precondition_failed", "The resource version has changed.")
            await connection.execute(
                "INSERT INTO agent_catalog(tenant_id,id,version,document) VALUES (%s,%s,%s,%s)",
                (agent.tenant_id, agent.id, agent.version, Jsonb(agent.model_dump(mode="json"))),
            )

    async def disable_agent(self, principal: Principal, identifier: UUID, expected: int) -> None:
        agent = await self.agent(principal, identifier)
        if agent.version != expected:
            raise HarnessError("precondition_failed", "The resource version has changed.")
        async with self.store.transaction() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"agent:{principal.tenant_id}:{identifier}",),
            )
            cursor = await connection.execute(
                "SELECT max(version) AS version FROM agent_catalog WHERE tenant_id=%s AND id=%s",
                (principal.tenant_id, identifier),
            )
            current = await cursor.fetchone()
            if current is None or current["version"] != expected:
                raise HarnessError("precondition_failed", "The resource version has changed.")
            await connection.execute(
                "UPDATE agent_catalog SET enabled=false WHERE tenant_id=%s AND id=%s",
                (principal.tenant_id, identifier),
            )

    async def agents(self, principal: Principal, after: UUID, limit: int) -> tuple[Agent, ...]:
        async with self.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT document FROM (SELECT DISTINCT ON(id) id,document,enabled FROM agent_catalog "
                "WHERE tenant_id=%s AND id=ANY(%s) AND id>%s ORDER BY id,version DESC) latest "
                "WHERE enabled ORDER BY id LIMIT %s",
                (principal.tenant_id, list(principal.allowed_agent_ids), after, limit),
            )
            return tuple(Agent.model_validate(row["document"]) for row in await cursor.fetchall() if row["enabled"])

    async def tool_grants(self, tenant_id: UUID) -> dict[str, tuple[bool, int]]:
        async with self.store.transaction() as connection:
            cursor = await connection.execute("SELECT * FROM tool_catalog WHERE tenant_id=%s", (tenant_id,))
            return {str(row["name"]): (bool(row["enabled"]), number(row, "revision")) for row in await cursor.fetchall()}

    async def set_tool(self, tenant_id: UUID, name: str, enabled: bool, expected: int | None) -> int:
        async with self.store.transaction() as connection:
            if expected is None:
                cursor = await connection.execute(
                    "INSERT INTO tool_catalog(tenant_id,name,enabled) VALUES (%s,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING revision", (tenant_id, name, enabled),
                )
            else:
                cursor = await connection.execute(
                    "UPDATE tool_catalog SET enabled=%s,revision=revision+1 "
                    "WHERE tenant_id=%s AND name=%s AND revision=%s RETURNING revision",
                    (enabled, tenant_id, name, expected),
                )
            row = await cursor.fetchone()
            if row is None:
                raise HarnessError("precondition_failed", "The resource version has changed.")
            return number(row, "revision")

    async def authorize_execution(self, execution: Execution, tool_name: str | None = None) -> None:
        if not execution.managed:
            return
        access = await self.access(Identity(execution.tenant_id, execution.subject))
        access.require("execution:create")
        repo = SecurityRepository(self.store.scoped(execution.tenant_id))
        agent = await repo.agent(access.principal, execution.agent_id, execution.agent_version)
        if tool_name is not None:
            grants = await repo.tool_grants(execution.tenant_id)
            if (
                tool_name not in access.principal.allowed_tools or tool_name not in agent.allowed_tools
                or not grants.get(tool_name, (False, 0))[0]
            ):
                raise HarnessError("tool_access_denied", "Tool authorization was revoked.")
