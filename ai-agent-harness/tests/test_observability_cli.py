import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_harness.adapters.postgres import PostgresStore
from agent_harness.audit import AuditEntry
from agent_harness.domain import Execution, Principal
from agent_harness.durable_cli import HostConfig, run
from agent_harness.state import Envelope
from test_runtime import make_agent, principal_for


@pytest.mark.parametrize("command", ["metrics", "audit"])
async def test_authorized_observability_cli_uses_host_scope_and_pagination(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    agent = make_agent()
    principal = principal_for(agent)
    execution = Execution(agent.tenant_id, principal.subject, agent.id, 1, uuid4())
    config = HostConfig(tenant_id=principal.tenant_id, subject=principal.subject, agents=(agent,))
    path = tmp_path / "host.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("HARNESS_DATABASE_URL", "synthetic-dsn-not-used")

    async def get(store: PostgresStore, caller: Principal, identifier: UUID) -> Envelope:
        assert caller == principal and identifier == execution.id
        return Envelope(agent, caller, "private-input", execution)

    async def audit(
        store: PostgresStore, caller: Principal, identifier: UUID, *, after: int, limit: int,
    ) -> tuple[AuditEntry, ...]:
        assert caller == principal and identifier == execution.id and after == 5 and limit == 2
        return (AuditEntry(6, "execution.admitted", principal.subject, datetime.now(UTC), {}),)

    monkeypatch.setattr(PostgresStore, "get", get)
    monkeypatch.setattr(PostgresStore, "audit", audit)
    await run(argparse.Namespace(
        command=command, config=str(path), id=execution.id, after=5, limit=2,
    ))
    output = capsys.readouterr().out
    data = json.loads(output)
    assert "private-input" not in output
    if command == "metrics":
        assert data["execution_id"] == str(execution.id)
        assert data["estimated_cost_usd"] == "0" and data["usage_complete"] is True
    else:
        assert data[0]["sequence"] == 6 and data[0]["kind"] == "execution.admitted"
