import json
from typing import Literal

import httpx
from pydantic import Field, SecretStr, ValidationError

from agent_harness.adapters.wire import WireModel
from agent_harness.domain import Decision, Message, ModelRequest, ModelResponse, ProviderError, ToolCall, Usage
from agent_harness.json_codec import decode_object
from agent_harness.resilience import retry_after


class AnthropicUsage(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)


class TextBlock(WireModel):
    type: Literal["text"]
    text: str


class ToolBlock(WireModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, object]


class AnthropicResponse(WireModel):
    type: Literal["message"]
    role: Literal["assistant"]
    content: list[TextBlock | ToolBlock] = Field(min_length=1)
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"]
    usage: AnthropicUsage


class AnthropicProvider:
    """Direct Messages API with native tools and JSON-schema final decisions."""

    def __init__(self, client: httpx.AsyncClient, api_key: SecretStr) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("An Anthropic API key is required.")
        self.client, self.api_key = client, api_key

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": "\n\n".join(m.content for m in request.messages if m.role == "system"),
            "messages": _messages(request.messages),
            "output_config": {"format": {"type": "json_schema", "schema": _decision_schema()}},
        }
        if request.tools:
            payload["tools"] = [{
                "name": tool.definition.name, "description": tool.definition.description,
                "input_schema": tool.input_schema,
            } for tool in request.tools]
            payload["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        try:
            async with self.client.stream(
                "POST", "https://api.anthropic.com/v1/messages", json=payload,
                headers={
                    "x-api-key": self.api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                }, follow_redirects=False,
            ) as response:
                delay = retry_after(response.headers.get("retry-after"))
                if response.status_code in {429, 408} or response.status_code >= 500:
                    raise ProviderError(
                        "provider_rate_limited" if response.status_code == 429 else "provider_unavailable",
                        "Anthropic is temporarily unavailable.", retry_after_seconds=delay,
                    )
                if response.status_code in {401, 403}:
                    raise ProviderError("provider_authentication", "Anthropic credentials were rejected.")
                if not 200 <= response.status_code < 300:
                    raise ProviderError("provider_request_rejected", "Anthropic rejected the request.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > request.max_response_bytes:
                        raise ProviderError("provider_response_too_large", "Provider response exceeds the byte limit.")
                    body.extend(chunk)
                request_id = response.headers.get("request-id")
        except httpx.TimeoutException:
            raise ProviderError("provider_timeout", "Anthropic request timed out.") from None
        except httpx.RequestError:
            raise ProviderError("provider_unavailable", "Anthropic transport failed.") from None
        try:
            parsed = AnthropicResponse.model_validate(decode_object(body.decode("utf-8")))
        except ValueError:
            raise ProviderError("provider_invalid_response", "Anthropic returned an invalid envelope.") from None
        usage = Usage(
            input_tokens=parsed.usage.input_tokens + parsed.usage.cache_creation_input_tokens
            + parsed.usage.cache_read_input_tokens,
            output_tokens=parsed.usage.output_tokens,
        )
        if parsed.stop_reason in {"max_tokens", "refusal", "pause_turn"}:
            codes = {
                "max_tokens": "output_limit_exceeded", "refusal": "provider_refusal",
                "pause_turn": "provider_request_rejected",
            }
            raise ProviderError(codes[parsed.stop_reason], "Anthropic did not complete the model turn.", usage=usage)
        text = "".join(block.text for block in parsed.content if isinstance(block, TextBlock))
        try:
            calls = tuple(
                ToolCall(
                    id=block.id, name=block.name,
                    arguments=json.dumps(block.input, allow_nan=False, separators=(",", ":")),
                ) for block in parsed.content if isinstance(block, ToolBlock)
            )
        except (ValueError, ValidationError):
            raise ProviderError("provider_invalid_tool_calls", "Anthropic returned invalid tools.", usage=usage) from None
        if calls or parsed.stop_reason == "tool_use":
            if (
                not calls or not request.tools or parsed.stop_reason != "tool_use"
                or len(calls) > request.max_tool_calls or len({call.id for call in calls}) != len(calls)
                or any(call.name not in {tool.definition.name for tool in request.tools} for call in calls)
            ):
                raise ProviderError("provider_invalid_tool_calls", "Anthropic returned unoffered tools.", usage=usage)
            return ModelResponse(None, usage, request_id, calls, assistant_content=text)
        try:
            decision = Decision.model_validate(decode_object(text), strict=True)
        except ValueError:
            raise ProviderError("provider_invalid_output", "Anthropic output violates the decision schema.", usage=usage) from None
        return ModelResponse(decision, usage, request_id)


def _message(message: Message) -> dict[str, object]:
    if message.role == "tool":
        return {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": message.tool_call_id,
            "content": message.content,
        }]}
    blocks: list[dict[str, object]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        blocks.append({
            "type": "tool_use", "id": call.id, "name": call.name,
            "input": decode_object(call.arguments),
        })
    return {"role": message.role, "content": blocks}


def _decision_schema() -> dict[str, object]:
    schema = Decision.model_json_schema()
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(properties.get("content"), dict):
        raise TypeError("Decision schema must define a content object.")
    content = properties["content"]
    # Anthropic does not accept minLength on the wire; Decision still validates it locally.
    content.pop("minLength", None)
    content["description"] = "Non-empty public content, never private reasoning."
    return schema


def _messages(messages: tuple[Message, ...]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        if message.role == "system":
            continue
        item = _message(message)
        if result and result[-1]["role"] == item["role"]:
            previous, current = result[-1]["content"], item["content"]
            if not isinstance(previous, list) or not isinstance(current, list):
                raise TypeError("Anthropic messages must contain block lists.")
            previous.extend(current)
        else:
            result.append(item)
    return result
