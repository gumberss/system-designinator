import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from agent_harness.adapters.anthropic import AnthropicProvider
from agent_harness.adapters.openai import OpenAIProvider
from agent_harness.domain import Message, ModelRequest, ProviderError, ToolCall
from agent_harness.ports import ModelProvider
from agent_harness.runtime import Runtime
from agent_harness.domain import RetryPolicy, Status
from test_model_reliability import with_fallback
from test_tools import build_harness
from uuid import uuid4


VENDORS = ("openai", "anthropic")


def build_provider(vendor: str, client: httpx.AsyncClient) -> ModelProvider:
    if vendor == "openai":
        return OpenAIProvider(client, SecretStr("contract-test-only"))
    return AnthropicProvider(client, SecretStr("contract-test-only"))


def model_request() -> ModelRequest:
    return ModelRequest(
        "configured-model", (Message("system", "instructions"), Message("user", "task")), 128, 8192,
    )


def envelope(vendor: str, content: str = '{"status":"completed","content":"done"}') -> dict[str, object]:
    if vendor == "openai":
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    return {
        "type": "message", "role": "assistant", "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.mark.parametrize("vendor", VENDORS)
async def test_shared_provider_contract_schema_usage_model_and_secret_headers(vendor: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "configured-model"
        assert request.method == "POST"
        assert "contract-test-only" not in request.content.decode()
        if vendor == "openai":
            assert request.headers["authorization"] == "Bearer contract-test-only"
            schema = body["response_format"]["json_schema"]["schema"]
        else:
            assert request.headers["x-api-key"] == "contract-test-only"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert body["system"] == "instructions"
            schema = body["output_config"]["format"]["schema"]
            assert "minLength" not in schema["properties"]["content"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"status", "content"}
        return httpx.Response(200, json=envelope(vendor))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await build_provider(vendor, client).generate(model_request())
    assert response.decision is not None and response.decision.content == "done"
    assert response.usage.input_tokens == 10 and response.usage.output_tokens == 5


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize(("status", "code"), [
    (429, "provider_rate_limited"), (503, "provider_unavailable"),
    (408, "provider_unavailable"), (401, "provider_authentication"),
    (400, "provider_request_rejected"), (302, "provider_request_rejected"),
])
async def test_shared_http_classification_retry_after_and_no_redirects(
    vendor: str, status: int, code: str,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, text="private-provider-payload", headers={
                "retry-after": "2", "location": "https://must-not-follow.invalid",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(ProviderError) as error:
            await build_provider(vendor, client).generate(model_request())
    assert error.value.code == code and len(requests) == 1
    assert "private-provider-payload" not in str(error.value)
    if status in {429, 503, 408}:
        assert error.value.retry_after_seconds == 2


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("content", [
    '{"status":"completed","content":"x","extra":1}', '{"status":"completed","content":""}',
])
async def test_shared_invalid_output_preserves_known_usage(vendor: str, content: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=envelope(vendor, content)),
    )) as client:
        with pytest.raises(ProviderError) as error:
            await build_provider(vendor, client).generate(model_request())
    assert error.value.code == "provider_invalid_output"
    assert error.value.usage is not None and error.value.usage.input_tokens == 10


@pytest.mark.parametrize("vendor", VENDORS)
async def test_shared_response_size_and_transport_timeout(vendor: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"x" * 100),
    )) as client:
        with pytest.raises(ProviderError) as error:
            await build_provider(vendor, client).generate(replace(model_request(), max_response_bytes=32))
    assert error.value.code == "provider_response_too_large"

    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport diagnostic", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ProviderError) as error:
            await build_provider(vendor, client).generate(model_request())
    assert error.value.code == "provider_timeout"
    assert "private transport diagnostic" not in str(error.value)


@pytest.mark.parametrize("vendor", VENDORS)
async def test_shared_cancel_drains_underlying_http_operation(vendor: str) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
            return httpx.Response(200, json=envelope(vendor))
        finally:
            stopped.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(build_provider(vendor, client).generate(model_request()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stopped.is_set()


async def test_anthropic_native_tools_preserve_transcript_and_merge_tool_results() -> None:
    harness = build_harness()
    request = replace(model_request(), tools=(harness.tool.describe(),))
    body = envelope("anthropic")
    body.update({
        "stop_reason": "tool_use", "content": [
            {"type": "text", "text": "Using the authorized tool."},
            {"type": "tool_use", "id": "tool_1", "name": "add_v1", "input": {"left": 2, "right": 3}},
        ],
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=body),
    )) as client:
        response = await AnthropicProvider(client, SecretStr("test-only")).generate(request)
    assert response.decision is None and response.tool_calls[0].name == "add_v1"
    assert response.assistant_content == "Using the authorized tool."
    calls = (
        response.tool_calls[0],
        ToolCall(id="tool_2", name="add_v1", arguments='{"left":4,"right":5}'),
    )
    transcript = request.messages + (
        Message("assistant", response.assistant_content, tool_calls=calls),
        Message("tool", '{"trust":"untrusted_tool_data","result":{"total":5}}', tool_call_id="tool_1"),
        Message("tool", '{"trust":"untrusted_tool_data","result":{"total":9}}', tool_call_id="tool_2"),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        assert payload["tools"][0]["input_schema"] == harness.tool.describe().input_schema
        messages = payload["messages"]
        assert messages[-2]["role"] == "assistant"
        assert messages[-2]["content"][1]["type"] == "tool_use"
        assert len(messages[-1]["content"]) == 2
        assert messages[-1]["content"][1]["tool_use_id"] == "tool_2"
        assert "untrusted_tool_data" in messages[-1]["content"][0]["content"]
        return httpx.Response(200, json=envelope("anthropic"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        final = await AnthropicProvider(client, SecretStr("test-only")).generate(
            replace(request, messages=transcript),
        )
    assert final.decision is not None and final.decision.status == "completed"


async def test_runtime_http_fallback_keeps_native_tool_history_between_real_adapters() -> None:
    harness = build_harness()
    configured = with_fallback()
    agent = harness.agent.model_copy(update={
        "selection": configured.selection.model_copy(update={"provider": "openai"}),
        "fallbacks": (configured.fallbacks[0].model_copy(update={"provider": "anthropic"}),),
        "retry": RetryPolicy(max_attempts=1),
    })
    counts = {"api.openai.com": 0, "api.anthropic.com": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        counts[req.url.host] += 1
        payload = json.loads(req.content)
        if req.url.host == "api.openai.com":
            return httpx.Response(503)
        if counts[req.url.host] == 1:
            body = envelope("anthropic")
            body.update({"stop_reason": "tool_use", "content": [{
                "type": "tool_use", "id": "native_1", "name": "add_v1", "input": {"left": 2, "right": 3},
            }]})
            return httpx.Response(200, json=body)
        result = payload["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "native_1"
        assert json.loads(result["content"])["result"] == {"total": 5}
        return httpx.Response(200, json=envelope("anthropic"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Runtime({
            "openai": OpenAIProvider(client, SecretStr("test")),
            "anthropic": AnthropicProvider(client, SecretStr("test")),
        }, tools=harness.executor).execute(agent, harness.principal, uuid4(), "task")
    assert result.status == Status.SUCCEEDED and result.model_calls == 4
    assert len(harness.handler.contexts) == 1 and result.unaccounted_calls == 2
    assert counts == {"api.openai.com": 2, "api.anthropic.com": 2}


@pytest.mark.parametrize(("stop", "code"), [
    ("max_tokens", "output_limit_exceeded"), ("refusal", "provider_refusal"),
    ("pause_turn", "provider_request_rejected"),
])
async def test_anthropic_noncompleted_turns_do_not_become_success(stop: str, code: str) -> None:
    body = envelope("anthropic")
    body["stop_reason"] = stop
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=body),
    )) as client:
        with pytest.raises(ProviderError) as error:
            await AnthropicProvider(client, SecretStr("test")).generate(model_request())
    assert error.value.code == code and error.value.usage is not None


async def test_anthropic_cache_consumption_counts_all_input_categories() -> None:
    body = envelope("anthropic")
    body["usage"] = {
        "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 20,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=body),
    )) as client:
        result = await AnthropicProvider(client, SecretStr("test")).generate(model_request())
    assert result.usage.input_tokens == 80


@pytest.mark.parametrize("mutate", [
    lambda body: body.update({"content": [{"type": "tool_use", "id": "x", "name": "forbidden", "input": {}}]}),
    lambda body: body.update({"content": [{"type": "tool_use", "id": "x", "name": "add_v1", "input": {}}]}),
])
async def test_anthropic_rejects_unoffered_or_inconsistent_tools(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    body = envelope("anthropic")
    mutate(body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=body),
    )) as client:
        with pytest.raises(ProviderError) as error:
            await AnthropicProvider(client, SecretStr("test")).generate(model_request())
    assert error.value.code == "provider_invalid_tool_calls"
