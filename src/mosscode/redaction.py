"""Process-wide secret value redaction and credential transport URL guards."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

PLACEHOLDER = "[已隐藏的密钥]"
MIN_SECRET = 8  # Shorter values are too likely to occur by chance to hide.
# Header names that carry a credential; other headers (versions, beta flags) are left alone.
SECRET_HEADER = re.compile(r"auth|key|token|secret", re.IGNORECASE)
CLEARTEXT_CREDENTIAL_URL_ERROR = (
    "为防止密钥明文传输，带凭据的远程服务 base_url 必须使用 https://（仅本机回环地址允许 http://）。"
)

_values: set[str] = set()


def is_loopback_url(url: str | None) -> bool:
    """True when `url` is an HTTP(S) URL without userinfo pointing to loopback (localhost, 127.0.0.0/8, ::1)."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        return False
    normalized = parsed.hostname.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def has_secret_headers(headers: Mapping[str, str] | None) -> bool:
    """True when any header carries a non-empty value under a credential-like name."""
    if not headers:
        return False
    return any(
        isinstance(k, str) and isinstance(v, str) and SECRET_HEADER.search(k) and v.strip() for k, v in headers.items()
    )


def is_cleartext_remote_credential_url(
    url: str | None,
    *,
    auth_type: str = "api_key",
    headers: Mapping[str, str] | None = None,
    has_api_key: bool = True,
) -> bool:
    """True when credentials would be sent over cleartext HTTP to a non-loopback endpoint.

    Loopback endpoints (localhost, 127.0.0.0/8, ::1 without userinfo) are exempted so local
    relays (such as CC Switch) and local servers can listen on loopback HTTP.
    """
    if not url:
        return False
    sends_credentials = (auth_type != "none" and has_api_key) or has_secret_headers(headers)
    if not sends_credentials:
        return False
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return True
    return scheme != "https" and not is_loopback_url(url)


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
