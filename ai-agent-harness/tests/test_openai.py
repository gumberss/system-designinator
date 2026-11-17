import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from agent_harness.adapters.openai import OpenAIProvider
from agent_harness.domain import Message, ModelRequest, ProviderError


def request(*, max_bytes: int = 4096) -> ModelRequest:
    return ModelRequest(
        "gpt-4.1-mini",
        (Message("system", "Instructions"), Message("user", "question")),
        128,
        max_bytes,
    )


def envelope(
    content: str = '{"status":"completed","content":"answer"}',
    *,
    finish: str = "stop",
    tool_calls: bool = False,
    refusal: str | None = None,
) -> dict[str, object]:
    return {
        "choices": [{
            "finish_reason": finish,
            "message": {
                "content": content,
                "refusal": refusal,
                "tool_calls": [{"id": "unsafe"}] if tool_calls else [],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def generate_with(
    handler: Callable[[httpx.Request], httpx.Response], *, max_bytes: int = 4096
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await OpenAIProvider(client, SecretStr("test-secret")).generate(request(max_bytes=max_bytes))


async def test_real_adapter_serializes_contract_and_normalizes_response() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json=envelope(), headers={"x-request-id": "request-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAIProvider(client, SecretStr("test-secret")).generate(request())
    assert result.decision is not None
    assert result.decision.status == "completed"
    assert result.decision.content == "answer"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.request_id == "request-1"
    assert captured[0].url == "https://api.openai.com/v1/chat/completions"
    assert captured[0].headers["Authorization"] == "Bearer test-secret"
    payload = json.loads(captured[0].content)
    assert payload["model"] == "gpt-4.1-mini"
    assert payload["max_completion_tokens"] == 128
    schema = payload["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert "tools" not in payload


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "provider_rate_limited"),
        (401, "provider_authentication"),
        (403, "provider_authentication"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
        (400, "provider_request_rejected"),
        (307, "provider_request_rejected"),
    ],
)
async def test_http_failures_are_sanitized_without_retry_or_redirect(status: int, code: str) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="private-provider-body",
            headers={"Location": "https://untrusted.invalid/"},
        )

    with pytest.raises(ProviderError) as error:
        await generate_with(handler)
    assert error.value.code == code
    assert "private-provider-body" not in str(error.value)
    assert "test-secret" not in str(error.value)
    assert calls == 1


@pytest.mark.parametrize(
    ("error_type", "code"),
    [(httpx.ReadTimeout, "provider_timeout"), (httpx.ConnectError, "provider_unavailable")],
)
async def test_transport_failures_are_sanitized(
    error_type: type[httpx.RequestError], code: str
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise error_type("private transport diagnostic", request=req)

    with pytest.raises(ProviderError) as error:
        await generate_with(handler)
    assert error.value.code == code
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"status":"completed","content":"ok","unknown":true}',
        '{"status":"completed","content":12}',
        '{"status":"completed","content":""}',
        '{"status":"completed","status":"continue","content":"x"}',
        '{"status":"invalid","content":"x"}',
    ],
)
async def test_invalid_decisions_fail_explicitly_but_preserve_known_usage(content: str) -> None:
    with pytest.raises(ProviderError) as error:
        await generate_with(lambda req: httpx.Response(200, json=envelope(content)))
    assert error.value.code == "provider_invalid_output"
    assert error.value.usage is not None and error.value.usage.input_tokens == 10


@pytest.mark.parametrize(
    ("finish", "tools", "refusal", "code"),
    [
        ("length", False, None, "output_limit_exceeded"),
        ("stop", True, None, "tools_not_available"),
        ("tool_calls", False, None, "tools_not_available"),
        ("content_filter", False, None, "provider_refusal"),
        ("stop", False, "refused", "provider_refusal"),
    ],
)
async def test_non_success_finishes_are_not_accepted(
    finish: str, tools: bool, refusal: str | None, code: str
) -> None:
    with pytest.raises(ProviderError) as error:
        await generate_with(
            lambda req: httpx.Response(
                200, json=envelope(finish=finish, tool_calls=tools, refusal=refusal)
            )
        )
    assert error.value.code == code
    assert error.value.usage is not None


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"choices":[]}',
        b'{"choices":[],"usage":{"prompt_tokens":0,"completion_tokens":0}}',
        b'{"choices":[{"finish_reason":"stop","message":{"content":"x"}}]}',
        b'{"choices":[{"finish_reason":"stop","message":{"content":"x"}}],'
        b'"usage":{"prompt_tokens":true,"completion_tokens":1}}',
    ],
)
async def test_malformed_envelopes_do_not_invent_usage(body: bytes) -> None:
    with pytest.raises(ProviderError) as error:
        await generate_with(lambda req: httpx.Response(200, content=body))
    assert error.value.code == "provider_invalid_response"
    assert error.value.usage is None


async def test_response_body_has_byte_limit() -> None:
    with pytest.raises(ProviderError) as error:
        await generate_with(lambda req: httpx.Response(200, content=b"x" * 100), max_bytes=99)
    assert error.value.code == "provider_response_too_large"


def test_blank_secret_rejected_without_leaking_it() -> None:
    with pytest.raises(ValueError, match="API key"):
        OpenAIProvider(httpx.AsyncClient(), SecretStr("  "))
