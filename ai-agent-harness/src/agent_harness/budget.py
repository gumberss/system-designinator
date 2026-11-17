import json

from agent_harness.domain import ModelRequest


def estimate_context(request: ModelRequest) -> int:
    messages = sum(
        len(message.content.encode("utf-8")) + 32
        + sum(len(call.model_dump_json().encode("utf-8")) for call in message.tool_calls)
        for message in request.messages
    )
    schemas = sum(
        len(json.dumps(tool.input_schema).encode("utf-8"))
        + len(tool.definition.description.encode("utf-8")) + 128
        for tool in request.tools
    )
    return messages + schemas + request.max_output_tokens
