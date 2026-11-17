import argparse
import asyncio
import os
import re
import sys
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import Field, ValidationError

from agent_harness.adapters.postgres import PostgresStore
from agent_harness.domain import Contract, HarnessError


class QuotaPolicy(Contract):
    max_active: int = Field(default=4, ge=1, le=1000)
    max_daily_executions: int = Field(default=100, ge=1, le=1000000)
    max_daily_tokens: int = Field(default=100000000, ge=1, le=10**12)
    max_daily_cost: Decimal = Field(default=Decimal("1000"), gt=0, le=1000000)


async def run(args: argparse.Namespace) -> None:
    store = PostgresStore(os.environ["HARNESS_ADMIN_DATABASE_URL"])
    if args.command == "migrate":
        await store.migrate()
        print("Migrations 1-5 applied.")
        return
    async with store.transaction() as connection:
        if args.command == "member":
            if (
                not args.subject.strip() or len(args.subject) > 256 or len(args.agent) > 1000
                or len(args.tool) > 1000 or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) for name in args.tool)
            ):
                raise HarnessError("invalid_config", "Membership identity or grants violate the bounded contract.")
            await connection.execute(
                "INSERT INTO memberships(tenant_id,subject,role,active,allowed_agents,allowed_tools) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,subject) DO UPDATE SET "
                "role=EXCLUDED.role,active=EXCLUDED.active,allowed_agents=EXCLUDED.allowed_agents,"
                "allowed_tools=EXCLUDED.allowed_tools",
                (args.tenant, args.subject, args.role, not args.inactive, args.agent, args.tool),
            )
        elif args.command == "quota":
            policy = QuotaPolicy(
                max_active=args.active, max_daily_executions=args.executions,
                max_daily_tokens=args.tokens, max_daily_cost=args.cost,
            )
            await connection.execute(
                "INSERT INTO quota_policy VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id) DO UPDATE SET "
                "max_active=EXCLUDED.max_active,max_daily_executions=EXCLUDED.max_daily_executions,"
                "max_daily_tokens=EXCLUDED.max_daily_tokens,max_daily_cost=EXCLUDED.max_daily_cost",
                (args.tenant, policy.max_active, policy.max_daily_executions, policy.max_daily_tokens, policy.max_daily_cost),
            )
        elif args.command == "login":
            password = os.environ.get("HARNESS_NEW_LOGIN_PASSWORD")
            if not password or len(password) < 24:
                raise HarnessError("invalid_config", "Configure a unique login password of at least 24 characters.")
            if not args.name.startswith("harness_") or len(args.name) > 63 or not args.name.replace("_", "").isalnum():
                raise HarnessError("invalid_config", "Use a bounded harness_ login identifier.")
            await connection.execute(sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(args.name), sql.Literal(password)))
            await connection.execute(sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(args.role), sql.Identifier(args.name),
            ))
    print("Administrative change committed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted offline provisioning; never expose as an HTTP endpoint.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    member = commands.add_parser("member")
    member.add_argument("--tenant", type=UUID, required=True)
    member.add_argument("--subject", required=True)
    member.add_argument("--role", choices=("viewer", "operator", "designer", "admin", "approver", "evaluator"), required=True)
    member.add_argument("--agent", action="append", type=UUID, default=[])
    member.add_argument("--tool", action="append", default=[])
    member.add_argument("--inactive", action="store_true")
    quota = commands.add_parser("quota")
    quota.add_argument("--tenant", type=UUID, required=True)
    quota.add_argument("--active", type=int, default=4)
    quota.add_argument("--executions", type=int, default=100)
    quota.add_argument("--tokens", type=int, default=100000000)
    quota.add_argument("--cost", default="1000")
    login = commands.add_parser("login")
    login.add_argument("--name", required=True)
    login.add_argument("--role", choices=("harness_api", "harness_worker"), required=True)
    args = parser.parse_args()
    if not os.environ.get("HARNESS_ADMIN_DATABASE_URL"):
        parser.error("Configure HARNESS_ADMIN_DATABASE_URL with a separate migration/provisioning login.")
    try:
        if sys.platform == "win32":
            asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
        else:
            asyncio.run(run(args))
    except (psycopg.Error, HarnessError, ValidationError):
        parser.exit(1, "Administrative operation failed; verify input, permissions and database availability.\n")
