from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def as_input_item(self) -> dict[str, Any]:
        return {
            "type": "function_call",
            "call_id": self.call_id,
            "name": self.name,
            "arguments": json.dumps(self.arguments, separators=(",", ":")),
        }


@dataclass(frozen=True)
class ToolCompletion:
    content: str
    tool_calls: list[ToolCall]
    output_items: list[dict[str, Any]] = field(default_factory=list)

    def as_input_items(self) -> list[dict[str, Any]]:
        if self.output_items:
            return self.output_items
        items = [call.as_input_item() for call in self.tool_calls]
        if self.content:
            items.insert(0, {"role": "assistant", "content": self.content})
        return items


class OpenAIProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        chat_model: str,
        embedding_model: str,
        embedding_dimensions: int,
        reasoning_effort: str | None = None,
    ) -> None:
        self.client = client
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.reasoning_effort = reasoning_effort

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
        completion = await self.complete_with_tools(
            [{"role": message["role"], "content": message["content"]} for message in messages],
            [],
        )
        if completion.tool_calls or not completion.content:
            raise RuntimeError("OpenAI returned no final answer")
        return completion.content

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ToolCompletion:
        body: dict[str, Any] = {
            "model": self.chat_model,
            "input": messages,
            "max_output_tokens": 4096,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self.reasoning_effort is not None:
            body["reasoning"] = {"effort": self.reasoning_effort}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        response = await self.client.post("/responses", json=body)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("status") != "completed":
            raise RuntimeError("OpenAI returned an incomplete or failed response")
        output = data.get("output")
        if not isinstance(output, list):
            raise RuntimeError("OpenAI returned invalid response output")
        output_items: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in output:
            if not isinstance(item, dict):
                raise RuntimeError("OpenAI returned an invalid output item")
            output_items.append(item)
            if item.get("type") == "reasoning":
                continue
            if item.get("type") == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    raise RuntimeError("OpenAI returned invalid response content")
                for part in content:
                    if (
                        not isinstance(part, dict)
                        or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)
                    ):
                        raise RuntimeError("OpenAI returned unsupported response content")
                    text_parts.append(part["text"])
                continue
            if item.get("type") != "function_call":
                raise RuntimeError("OpenAI returned an unsupported output item")
            call_id = item.get("call_id")
            name = item.get("name")
            arguments_text = item.get("arguments")
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
        content = "\n".join(text_parts).strip()
        if not content and not tool_calls:
            raise RuntimeError("OpenAI returned an empty response")
        return ToolCompletion(
            content=content, tool_calls=tool_calls, output_items=output_items,
        )


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
