"""API keys: reading them from the system credential manager, and keeping them out of what commands and the model see.

Two process-wide sets are filled once the configuration is read: the variable names that hold keys, which child
processes do not inherit, and the key values, which are hidden wherever they appear in tool output or in messages
joining the history. A key the model never sees cannot end up in a reply, a transcript or the service's logs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

SERVICE = "mosscode"  # The service name every entry is saved under.
PLACEHOLDER = "[已隐藏的密钥]"
MIN_SECRET = 8  # Shorter values are too likely to occur by chance to hide.
# Header names that carry a credential; other headers (versions, beta flags) are left alone.
SECRET_HEADER = re.compile(r"auth|key|token|secret", re.IGNORECASE)

_names: set[str] = set()
_values: set[str] = set()


class KeyringError(Exception):
    """The credential manager is not available or refused the operation; the message says why in Chinese."""


def _fold(name: str) -> str:
    return name.upper() if os.name == "nt" else name  # Windows variable names ignore case.


def hide_variable(name: str | None) -> None:
    if name:
        _names.add(_fold(name))


def remember(value: str | None) -> None:
    if value and len(value) >= MIN_SECRET:
        _values.add(value)


def forget_all() -> None:
    """Tests only: start from an empty set."""
    _names.clear()
    _values.clear()


def protect(settings) -> None:
    """Register the keys a configuration names; keys in the credential manager are read now so all are known."""
    from .command_env import configure  # It imports this module.

    configure(settings.command_env_keep, settings.command_env_drop)
    for config in (*settings.providers, *((settings.web_search,) if settings.web_search else ())):
        hide_variable(config.api_key_env)
        if config.api_key_env:
            remember(os.environ.get(config.api_key_env))
        remember(config.api_key)
        if config.api_key_keyring:
            try:
                remember(keyring_get(config.api_key_keyring))
            except KeyringError:
                pass  # Reported when the key is needed.
        for header, value in getattr(config, "headers", {}).items():
            if SECRET_HEADER.search(header):
                remember(value)


def child_environment(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """env (or this process's environment for None) without the variables that hold keys."""
    if not _names:
        return None if env is None else dict(env)
    source = os.environ if env is None else env
    return {name: value for name, value in source.items() if _fold(name) not in _names}


def redact(text: str) -> str:
    if not _values or not text:
        return text
    for value in sorted(_values, key=len, reverse=True):
        if value in text:
            text = text.replace(value, PLACEHOLDER)
    return text


def redact_value(value: Any) -> Any:
    """redact through the strings of a tool result's details."""
    if not _values:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(item) for item in value)
    return value


def placeholder_problem(text: str) -> str | None:
    if PLACEHOLDER not in text:
        return None
    return (
        f"内容包含 {PLACEHOLDER}：这是读取结果中替换掉的真实密钥，写入会用占位符覆盖它。"
        "请只修改其他部分，edit_file 的 new_text 不要包含这段占位符。"
    )


def _backend():
    import keyring
    from keyring.errors import KeyringError as Failure
    from keyring.errors import NoKeyringError

    return keyring, Failure, NoKeyringError


NO_BACKEND = "这台机器没有可用的系统凭据管理器（Linux 需要 Secret Service，例如 GNOME Keyring）；请改用 api_key_env"


def _call(action: str, *args):
    keyring, failure, missing = _backend()
    try:
        return getattr(keyring, action)(SERVICE, *args)
    except missing:
        raise KeyringError(NO_BACKEND) from None
    except failure as exc:
        raise KeyringError(f"系统凭据管理器拒绝了操作：{exc}") from None


def keyring_get(name: str) -> str | None:
    value = _call("get_password", name)
    remember(value)
    return value or None


def keyring_set(name: str, value: str) -> None:
    remember(value)
    _call("set_password", name, value)


def keyring_delete(name: str) -> bool:
    """False when there was no such entry."""
    if keyring_get(name) is None:
        return False
    _call("delete_password", name)
    return True


def keyring_problem() -> str | None:
    """Why the credential manager cannot be used, or None."""
    keyring, _, _ = _backend()
    from keyring.backends.fail import Keyring as Fail

    return NO_BACKEND if isinstance(keyring.get_keyring(), Fail) else None


def keyring_label() -> str:
    """The credential store's name for messages, e.g. Windows 凭据管理器."""
    keyring, _, _ = _backend()
    module = type(keyring.get_keyring()).__module__
    for part, label in (
        ("Windows", "Windows 凭据管理器"),
        ("macOS", "macOS 钥匙串"),
        ("SecretService", "Secret Service"),
    ):
        if part in module:
            return label
    return "系统凭据管理器"


def plaintext_notice(settings, root) -> str | None:
    """A key written in plain text in a configuration inside the project, where the model's tools can read it."""
    source = settings.source
    # A following provider's key stays in the followed tool's file; this one does not hold it.
    plain = [config.name for config in settings.providers if config.api_key and not config.follow] + (
        ["web_search"] if settings.web_search and settings.web_search.api_key else []
    )
    if source is None or not plain:
        return None
    try:
        inside = source.resolve().is_relative_to(root.resolve())
    except OSError:
        return None
    if not inside:
        return None
    name = plain[0]
    return (
        f"配置文件 {source.name} 在项目目录中，并明文保存了 {'、'.join(plain)} 的密钥。模型读到时会隐藏密钥，"
        f"但更稳妥的做法是运行 moss key set {name} 保存到系统凭据管理器，并把配置中的 api_key 换成 api_key_keyring: {name}。"
    )
