import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from agent_harness.adapters.openai import OpenAIProvider
from agent_harness.domain import Principal, Status
from agent_harness.runtime import Runtime
from test_runtime import make_agent


@pytest.mark.parametrize("invalid_final", [True, False])
async def test_runtime_and_real_adapter_contract_over_controlled_http(
    invalid_final: bool,
) -> None:
    agent = make_agent()
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(req.content)
        assert payload["model"] == agent.selection.model
        if calls == 1:
            content = '{"status":"continue","content":"Public progress."}'
        elif invalid_final:
            content = '{"status":"completed","content":false}'
        else:
            content = '{"status":"completed","content":"Final answer."}'
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    principal = Principal(agent.tenant_id, "integration-test", frozenset({agent.id}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(client, SecretStr("test-only-secret"))
        execution = await Runtime({"test-double": provider}).execute(
            agent, principal, uuid4(), "Complete the task."
        )
    assert calls == 2
    assert execution.input_tokens == 20
    assert execution.output_tokens == 10
    assert execution.usage_complete
    if invalid_final:
        assert execution.status == Status.FAILED
        assert execution.failure is not None
        assert execution.failure.code == "provider_invalid_output"
        assert execution.result is None
    else:
        assert execution.status == Status.SUCCEEDED
        assert execution.result == "Final answer."
