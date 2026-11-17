from typing import Literal

import httpx
from pydantic import Field, SecretStr, TypeAdapter, ValidationError
from agent_harness.adapters.wire import WireModel as WireModel

from agent_harness.domain import (
    Decision,
    ModelRequest,
    ModelResponse,
    ProviderError,
    Message,
    ToolCall,
    Usage,
)
from agent_harness.json_codec import decode_object
from agent_harness.resilience import retry_after


class WireMessage(WireModel):
    content: str | None = None
    refusal: str | None = None
    tool_calls: list[dict[str, object]] = Field(default_factory=list)


class WireChoice(WireModel):
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"]
    message: WireMessage


class WireFunction(WireModel):
    name: str
    arguments: str


class WireToolCall(WireModel):
    id: str
    type: Literal["function"]
    function: WireFunction


class WireUsage(WireModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class WireResponse(WireModel):
    choices: list[WireChoice] = Field(min_length=1, max_length=1)
    usage: WireUsage


class OpenAIProvider:
    """Chat Completions adapter; the caller owns the AsyncClient lifecycle."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("An OpenAI API key is required.")
        self._client = client
        self._api_key = api_key

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [_message_payload(message) for message in request.messages],
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_decision",
                    "strict": True,
                    "schema": Decision.model_json_schema(),
                },
            },
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": descriptor.definition.name,
                        "description": descriptor.definition.description,
                        "parameters": descriptor.input_schema,
                        "strict": False,
                    },
                }
                for descriptor in request.tools
            ]
            payload["parallel_tool_calls"] = False
        try:
            async with self._client.stream(
                "POST",
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                follow_redirects=False,
            ) as response:
                self._validate_status(response.status_code, response.headers)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > request.max_response_bytes:
                        raise ProviderError(
                            "provider_response_too_large", "Provider response exceeds the byte limit."
                        )
                    body.extend(chunk)
                request_id = response.headers.get("x-request-id")
        except httpx.TimeoutException:
            raise ProviderError("provider_timeout", "Provider request timed out.") from None
        except httpx.RequestError:
            raise ProviderError("provider_unavailable", "Provider transport failed.") from None
        try:
            parsed = WireResponse.model_validate(decode_object(body.decode("utf-8")))
        except ValueError:
            raise ProviderError(
                "provider_invalid_response", "Provider returned an invalid response envelope."
            ) from None
        usage = Usage(
            input_tokens=parsed.usage.prompt_tokens,
            output_tokens=parsed.usage.completion_tokens,
        )
        choice = parsed.choices[0]
        if choice.message.tool_calls or choice.finish_reason == "tool_calls":
            if not request.tools:
                raise ProviderError(
                    "tools_not_available", "Tools were not offered in this request.", usage=usage
                )
        if choice.message.refusal is not None or choice.finish_reason == "content_filter":
            raise ProviderError("provider_refusal", "Provider declined the request.", usage=usage)
        if choice.finish_reason == "length":
            raise ProviderError(
                "output_limit_exceeded", "Model output token limit reached.", usage=usage
            )
        if choice.message.tool_calls or choice.finish_reason == "tool_calls":
            if (
                choice.finish_reason != "tool_calls"
                or not choice.message.tool_calls
                or len(choice.message.tool_calls) > request.max_tool_calls
                or choice.message.content not in (None, "")
            ):
                raise ProviderError(
                    "provider_invalid_tool_calls", "Provider returned an invalid tool call batch.", usage=usage
                )
            try:
                wire_calls = TypeAdapter(list[WireToolCall]).validate_python(choice.message.tool_calls)
                calls = tuple(
                    ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
                    for call in wire_calls
                )
            except ValidationError:
                raise ProviderError(
                    "provider_invalid_tool_calls", "Provider returned malformed tool calls.", usage=usage
                ) from None
            if (
                len({call.id for call in calls}) != len(calls)
                or any(call.name not in {tool.definition.name for tool in request.tools} for call in calls)
            ):
                raise ProviderError(
                    "provider_invalid_tool_calls", "Provider returned duplicate or unoffered tool calls.", usage=usage
                )
            return ModelResponse(None, usage, request_id, calls)
        if choice.message.content is None:
            raise ProviderError(
                "provider_invalid_response", "Provider response has no content.", usage=usage
            )
        try:
            content = decode_object(choice.message.content)
            decision = Decision.model_validate(content, strict=True)
        except (ValueError, ValidationError):
            raise ProviderError(
                "provider_invalid_output", "Model output violates the decision schema.", usage=usage
            ) from None
        return ModelResponse(decision, usage, request_id)

    @staticmethod
    def _validate_status(status: int, headers: httpx.Headers | None = None) -> None:
        delay = retry_after(headers.get("retry-after")) if headers is not None else None
        if status == 429:
            raise ProviderError(
                "provider_rate_limited", "Provider rate limit reached.", retry_after_seconds=delay,
            )
        if status in (401, 403):
            raise ProviderError("provider_authentication", "Provider credentials were rejected.")
        if status >= 500 or status == 408:
            raise ProviderError(
                "provider_unavailable", "Provider is temporarily unavailable.", retry_after_seconds=delay,
            )
        if not 200 <= status < 300:
            raise ProviderError("provider_request_rejected", "Provider rejected the request.")


def _message_payload(message: Message) -> dict[str, object]:
    result: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["content"] = message.content or None
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        result["tool_call_id"] = message.tool_call_id
    return result
