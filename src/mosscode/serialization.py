"""Messages to and from JSON for the session transcript; loading is strict so a bad line can be skipped."""

from pathlib import PurePath
from typing import Any

from .messages import ContentBlock, Message, ToolCall, ToolResult

ROLES = {"user", "assistant", "tool"}
KINDS = {"text", "thinking"}


def jsonable(value: Any) -> Any:
    """Plain JSON data; paths and other objects become strings, unconvertible keys are dropped."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        data = {}
        for key, item in value.items():
            try:
                data[str(key)] = jsonable(item)
            except Exception:
                continue
        return data
    if isinstance(value, PurePath):
        return str(value)
    return str(value)


def message_to_json(message: Message) -> dict:
    data: dict[str, Any] = {"role": message.role,
                            "content": [{"kind": b.kind, "text": b.text} for b in message.content]}
    if message.protocol is not None:
        data["protocol"] = message.protocol
    if message.native:
        data["native"] = [jsonable(item) for item in message.native]
    if message.tool_calls:
        data["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls]
    if message.tool_results:
        data["tool_results"] = [{"call_id": r.call_id, "name": r.name, "output": r.output, "is_error": r.is_error,
                                 "code": r.code, "details": jsonable(r.details)} for r in message.tool_results]
    if message.synthetic is not None:
        data["synthetic"] = message.synthetic
    return data


def _check(condition: bool, field: str) -> None:
    if not condition:
        raise ValueError(f"字段不合法：{field}")


def _string(value: Any, field: str, *, optional: bool = False) -> str | None:
    _check(isinstance(value, str) or (optional and value is None), field)
    return value


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    _check(isinstance(value, list), key)
    return value


def message_from_json(data: Any) -> Message:
    _check(isinstance(data, dict), "message")
    role = data.get("role")
    _check(role in ROLES, "role")
    content = []
    for block in _list(data, "content"):
        _check(isinstance(block, dict) and block.get("kind") in KINDS, "content.kind")
        content.append(ContentBlock(block["kind"], _string(block.get("text"), "content.text")))
    native = _list(data, "native")
    _check(all(isinstance(item, dict) for item in native), "native")
    calls = []
    for item in _list(data, "tool_calls"):
        _check(isinstance(item, dict), "tool_calls")
        calls.append(ToolCall(_string(item.get("id"), "tool_calls.id"), _string(item.get("name"), "tool_calls.name"),
                              _string(item.get("arguments"), "tool_calls.arguments")))
    results = []
    for item in _list(data, "tool_results"):
        _check(isinstance(item, dict) and isinstance(item.get("is_error"), bool), "tool_results")
        details = item.get("details", {})
        _check(isinstance(details, dict), "tool_results.details")
        results.append(ToolResult(_string(item.get("call_id"), "tool_results.call_id"),
                                  _string(item.get("name"), "tool_results.name"),
                                  _string(item.get("output"), "tool_results.output"), item["is_error"],
                                  _string(item.get("code"), "tool_results.code", optional=True), details))
    return Message(role, tuple(content), _string(data.get("protocol"), "protocol", optional=True), tuple(native),
                   tuple(calls), tuple(results), _string(data.get("synthetic"), "synthetic", optional=True))
