from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def as_message_value(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, separators=(",", ":")),
            },
        }


@dataclass(frozen=True)
class ToolCompletion:
    content: str
    tool_calls: list[ToolCall]


class OpenAIProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        chat_model: str,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> None:
        self.client = client
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self.client.post(
            "/embeddings",
            json={
                "model": self.embedding_model,
                "input": texts,
                "dimensions": self.embedding_dimensions,
            },
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("OpenAI returned an incomplete embedding response")
        indices = [item.get("index") if isinstance(item, dict) else None for item in data]
        if any(not isinstance(index, int) or isinstance(index, bool) for index in indices):
            raise RuntimeError("OpenAI returned invalid embedding indexes")
        if sorted(indices) != list(range(len(texts))):
            raise RuntimeError("OpenAI returned invalid embedding indexes")
        ordered = sorted(data, key=lambda item: item.get("index", -1))
        embeddings = [item.get("embedding") for item in ordered]
        if any(
            not isinstance(vector, list)
            or len(vector) != self.embedding_dimensions
            or not all(isinstance(value, (int, float)) for value in vector)
            for vector in embeddings
        ):
            raise RuntimeError("OpenAI returned an embedding with an invalid dimension")
        return embeddings

    async def complete(self, messages: list[dict[str, str]]) -> str:
        response = await self.client.post(
            "/chat/completions",
            json={
                "model": self.chat_model,
                "messages": messages,
                "max_completion_tokens": 700,
                "temperature": 0.2,
            },
        )
        response.raise_for_status()
        choices = response.json().get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenAI returned no chat completion choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenAI returned an empty chat completion")
        return content.strip()

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ToolCompletion:
        body: dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "max_completion_tokens": 700,
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        response = await self.client.post("/chat/completions", json=body)
        response.raise_for_status()
        choices = response.json().get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenAI returned no chat completion choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("OpenAI returned an invalid chat completion")
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise RuntimeError("OpenAI returned invalid chat completion content")
        raw_tool_calls = message.get("tool_calls", [])
        if not isinstance(raw_tool_calls, list):
            raise RuntimeError("OpenAI returned invalid tool calls")
        tool_calls: list[ToolCall] = []
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, dict):
                raise RuntimeError("OpenAI returned an invalid tool call")
            function = raw_call.get("function")
            if not isinstance(function, dict):
                raise RuntimeError("OpenAI returned an invalid tool call")
            call_id = raw_call.get("id")
            name = function.get("name")
            arguments_text = function.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise RuntimeError("OpenAI returned an invalid tool call")
            if not isinstance(arguments_text, str):
                raise RuntimeError("OpenAI returned invalid tool arguments")
            try:
                arguments = json.loads(arguments_text)
            except json.JSONDecodeError as error:
                raise RuntimeError("OpenAI returned invalid tool arguments") from error
            if not isinstance(arguments, dict):
                raise RuntimeError("OpenAI tool arguments must be a JSON object")
            tool_calls.append(ToolCall(call_id, name, arguments))
        if not content.strip() and not tool_calls:
            raise RuntimeError("OpenAI returned an empty chat completion")
        return ToolCompletion(content=content.strip(), tool_calls=tool_calls)


def qdrant_collection_body(dimensions: int) -> dict[str, Any]:
    return {
        "vectors": {
            "size": dimensions,
            "distance": "Cosine",
        }
    }


def qdrant_query_body(
    vector: list[float], *, limit: int
) -> dict[str, Any]:
    return {
        "query": vector,
        "limit": limit,
        "with_payload": True,
        "score_threshold": 0.2,
    }
