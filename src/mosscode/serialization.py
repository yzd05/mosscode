"""Messages to and from JSON for the session transcript; loading is strict so a bad line can be skipped."""

import re
from pathlib import Path, PurePath
from typing import Any

from .images import EXTENSIONS, ImageRef
from .messages import ContentBlock, Message, ToolCall, ToolResult

ROLES = {"user", "assistant", "tool"}
KINDS = {"text", "thinking", "image"}
# A transcript names a file in the images directory: nothing that could point elsewhere.
IMAGE_NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif|webp)")


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


def image_to_json(image: ImageRef) -> dict:
    """A reference only; the bytes stay in the session's images directory."""
    return {
        "name": image.name,
        "media_type": image.media_type,
        "width": image.width,
        "height": image.height,
        "size": image.size,
        "alt": image.alt,
    }


def _block_to_json(block: ContentBlock) -> dict:
    data = {"kind": block.kind, "text": block.text}
    if block.image is not None:
        data["image"] = image_to_json(block.image)
    return data


def message_to_json(message: Message) -> dict:
    data: dict[str, Any] = {
        "role": message.role,
        "content": [_block_to_json(b) for b in message.content],
    }
    if message.protocol is not None:
        data["protocol"] = message.protocol
    if message.native:
        data["native"] = [jsonable(item) for item in message.native]
    if message.tool_calls:
        data["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls]
    if message.tool_results:
        data["tool_results"] = [
            {
                "call_id": r.call_id,
                "name": r.name,
                "output": r.output,
                "is_error": r.is_error,
                "code": r.code,
                "details": jsonable(r.details),
            }
            | ({"images": [image_to_json(image) for image in r.images]} if r.images else {})
            for r in message.tool_results
        ]
    if message.synthetic is not None:
        data["synthetic"] = message.synthetic
    if message.command is not None:
        data["command"] = message.command
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


def _size(value: Any, field: str) -> int:
    _check(type(value) is int and value >= 0, field)
    return value


def image_from_json(data: Any, images: Path | None = None) -> ImageRef:
    """images is the session's images directory; without it the reference has no bytes (display only)."""
    _check(isinstance(data, dict), "image")
    name = _string(data.get("name"), "image.name")
    _check(IMAGE_NAME.fullmatch(name) is not None, "image.name")
    media_type = _string(data.get("media_type"), "image.media_type")
    _check(EXTENSIONS.get(media_type) == name.rsplit(".", 1)[1], "image.media_type")
    return ImageRef(
        name,
        media_type,
        _size(data.get("width"), "image.width"),
        _size(data.get("height"), "image.height"),
        _size(data.get("size"), "image.size"),
        _string(data.get("alt"), "image.alt"),
        path=None if images is None else images / name,
    )


def _lost(image: ImageRef) -> str:
    return f"[图片已丢失：{image.alt}]"


def _missing(image: ImageRef, lost: list | None) -> bool:
    if image.path is None or image.path.is_file():
        return False
    if lost is not None:
        lost.append(image.name)
    return True


def message_from_json(data: Any, images: Path | None = None, lost: list | None = None) -> Message:
    """An image whose file is missing from images becomes text and is noted in lost."""
    _check(isinstance(data, dict), "message")
    role = data.get("role")
    _check(role in ROLES, "role")
    content = []
    for block in _list(data, "content"):
        _check(isinstance(block, dict) and block.get("kind") in KINDS, "content.kind")
        text = _string(block.get("text"), "content.text")
        if block["kind"] != "image":
            content.append(ContentBlock(block["kind"], text))
            continue
        image = image_from_json(block.get("image"), images)
        _check(image.alt == text, "content.text")
        content.append(
            ContentBlock("text", _lost(image)) if _missing(image, lost) else ContentBlock("image", text, image)
        )
    native = _list(data, "native")
    _check(all(isinstance(item, dict) for item in native), "native")
    calls = []
    for item in _list(data, "tool_calls"):
        _check(isinstance(item, dict), "tool_calls")
        calls.append(
            ToolCall(
                _string(item.get("id"), "tool_calls.id"),
                _string(item.get("name"), "tool_calls.name"),
                _string(item.get("arguments"), "tool_calls.arguments"),
            )
        )
    results = []
    for item in _list(data, "tool_results"):
        _check(isinstance(item, dict) and isinstance(item.get("is_error"), bool), "tool_results")
        details = item.get("details", {})
        _check(isinstance(details, dict), "tool_results.details")
        output = _string(item.get("output"), "tool_results.output")
        kept = []
        for image in (image_from_json(value, images) for value in _list(item, "images")):
            if _missing(image, lost):
                output += "\n" + _lost(image)
            else:
                kept.append(image)
        results.append(
            ToolResult(
                _string(item.get("call_id"), "tool_results.call_id"),
                _string(item.get("name"), "tool_results.name"),
                output,
                item["is_error"],
                _string(item.get("code"), "tool_results.code", optional=True),
                details,
                tuple(kept),
            )
        )
    return Message(
        role,
        tuple(content),
        _string(data.get("protocol"), "protocol", optional=True),
        tuple(native),
        tuple(calls),
        tuple(results),
        _string(data.get("synthetic"), "synthetic", optional=True),
        _string(data.get("command"), "command", optional=True),
    )
