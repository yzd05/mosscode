"""Process-wide secret value redaction for model messages and tool outputs."""

from __future__ import annotations

from typing import Any

PLACEHOLDER = "[已隐藏的密钥]"
MIN_SECRET = 8  # Shorter values are too likely to occur by chance to hide.

_values: set[str] = set()


def remember(value: str | None) -> None:
    if value and len(value) >= MIN_SECRET:
        _values.add(value)


def forget_secrets() -> None:
    """Tests only: clear all remembered secret values."""
    _values.clear()


def redact(text: str) -> str:
    if not _values or not text:
        return text
    for value in sorted(_values, key=len, reverse=True):
        if value in text:
            text = text.replace(value, PLACEHOLDER)
    return text


def redact_value(value: Any) -> Any:
    """Redact remembered secrets recursively through strings in structured details."""
    if not _values:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(item) for item in value)
    return value
