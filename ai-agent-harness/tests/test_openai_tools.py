import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from agent_harness.domain import ModelRequest, ModelResponse, ProviderError, Status
from agent_harness.adapters.openai import OpenAIProvider
from agent_harness.runtime import Runtime
from test_openai import request
from test_tools import build_harness


def native_call(call_id: str = "call_1", name: str = "add_v1") -> dict[str, object]:
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": '{"left":2,"right":3}'},
    }


def native_envelope(
    calls: list[dict[str, object]], *, finish: str = "tool_calls", content: str | None = None,
) -> dict[str, object]:
    return {
        "choices": [{
            "finish_reason": finish,
            "message": {"content": content, "tool_calls": calls},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def normalize(
    body: dict[str, object], *, max_calls: int = 8,
) -> ModelResponse:
    harness = build_harness()
    base = request()
    model_request = ModelRequest(
        base.model, base.messages, base.max_output_tokens, base.max_response_bytes,
        harness.registry.discover(harness.agent, harness.principal), max_calls,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    ) as client:
        return await OpenAIProvider(client, SecretStr("test-only")).generate(model_request)


async def test_normalizes_native_tool_calls_without_decision() -> None:
    response = await normalize(native_envelope([native_call()]))
    assert response.decision is None
    assert response.tool_calls[0].name == "add_v1"
    assert response.tool_calls[0].arguments == '{"left":2,"right":3}'
    assert response.usage.input_tokens == 10


@pytest.mark.parametrize(
    "body",
    [
        native_envelope([]),
        native_envelope([native_call(), native_call()]),
        native_envelope([native_call(name="unoffered")]),
        native_envelope([native_call()], finish="stop"),
        native_envelope([native_call()], content="ambiguous content"),
        native_envelope([{"id": "call_1"}]),
        native_envelope([native_call(call_id="invalid id")]),
    ],
)
async def test_invalid_native_batches_are_rejected_with_usage(body: dict[str, object]) -> None:
    with pytest.raises(ProviderError) as error:
        await normalize(body)
    assert error.value.code == "provider_invalid_tool_calls"
    assert error.value.usage is not None


async def test_adapter_caps_number_of_tool_calls() -> None:
    with pytest.raises(ProviderError) as error:
        await normalize(native_envelope([native_call("first"), native_call("second")]), max_calls=1)
    assert error.value.code == "provider_invalid_tool_calls"


async def test_duplicate_native_fields_cannot_change_tool_identity_silently() -> None:
    harness = build_harness()
    base = request()
    model_request = ModelRequest(
        base.model, base.messages, base.max_output_tokens, base.max_response_bytes,
        harness.registry.discover(harness.agent, harness.principal),
    )
    body = (
        b'{"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":['
        b'{"id":"one","id":"two","type":"function",'
        b'"function":{"name":"add_v1","arguments":"{}"}}]}}],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5}}'
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await OpenAIProvider(client, SecretStr("test-only")).generate(model_request)
    assert error.value.code == "provider_invalid_response"


async def test_full_runtime_openai_tool_result_round_trip() -> None:
    harness = build_harness()
    payloads: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        payloads.append(payload)
        if len(payloads) == 1:
            assert payload["tools"][0]["function"]["name"] == "add_v1"
            assert payload["tools"][0]["function"]["parameters"]["additionalProperties"] is False
            assert payload["parallel_tool_calls"] is False
            return httpx.Response(200, json=native_envelope([native_call()]))
        messages = payload["messages"]
        assert messages[-2]["role"] == "assistant"
        assert messages[-2]["tool_calls"] == [native_call()]
        assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "call_1"
        assert json.loads(messages[-1]["content"])["result"] == {"total": 5}
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"completed","content":"5"}'},
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = Runtime(
            {"test-double": OpenAIProvider(client, SecretStr("test-only"))},
            tools=harness.executor,
        )
        result = await runtime.execute(harness.agent, harness.principal, uuid4(), "Add 2 and 3.")
    assert result.status == Status.SUCCEEDED and result.result == "5"
    assert len(payloads) == 2 and len(harness.handler.contexts) == 1
    assert result.input_tokens == 22 and result.output_tokens == 8
    assert result.tool_invocations[0].result == '{"total":5}'
