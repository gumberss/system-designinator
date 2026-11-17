import json


def decode_object(raw: str) -> dict[str, object]:
    try:
        value: object = json.loads(
            raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except RecursionError:
        raise ValueError("JSON nesting exceeds the limit.") from None
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required.")
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON nesting exceeds the limit.")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return {str(key): item for key, item in value.items()}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("Non-finite JSON numbers are not allowed.")
