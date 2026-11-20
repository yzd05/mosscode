"""`moss login`: browser OAuth PKCE (OpenRouter) and Device Flow (GitHub Copilot) onboarding."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .auth import (
    AUTH_PROVIDERS,
    AuthError,
    GitHubCopilotAuth,
    OpenRouterPKCEAuth,
    persist_oauth_result,
)
from .credentials import KeyringError, keyring_delete, keyring_label, keyring_problem


def _choose_provider(read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> str | None:
    keys = list(AUTH_PROVIDERS)
    write("选择浏览器登录服务：")
    for idx, key in enumerate(keys, 1):
        write(f"  {idx}. {AUTH_PROVIDERS[key]}")
    while True:
        try:
            answer = read(f"请选择 [1-{len(keys)}]（回车选 1）：").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return keys[0]
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        if answer in AUTH_PROVIDERS:
            return answer
        write(f"  请输入 1–{len(keys)} 之间的数字或服务名称（{' / '.join(keys)}）。")


def _status_report(home: Path | None, config_path: Path | None, write: Callable[[str], None]) -> int:
    from .config import ConfigError, find_config, load_settings

    try:
        path = config_path or find_config(home=home)
        settings = load_settings(path, home)
    except ConfigError as exc:
        write(f"moss login status：{exc}")
        return 1
    write(f"配置文件：{path}")
    for provider in settings.providers:
        if provider.auth_type == "github-copilot":
            cred = f"GitHub Copilot 设备码令牌（{provider.api_key_keyring or provider.api_key_env or '配置文件'}）"
        elif provider.api_key_keyring:
            cred = f"系统凭据管理器（{provider.api_key_keyring}）"
        elif provider.api_key_env:
            cred = f"环境变量（{provider.api_key_env}）"
        elif provider.auth_type == "none":
            cred = "免密钥（本机回环）"
        elif provider.follow:
            cred = f"跟随 {provider.follow}"
        else:
            cred = "配置文件明文密钥"
        mark = " [默认]" if provider.name == settings.default_provider else ""
        write(f"  - {provider.name}{mark} · {provider.model} · {cred}")
    return 0


def _logout(
    name: str,
    write: Callable[[str], None],
    err_write: Callable[[str], None],
    *,
    vault: Any = None,
) -> int:
    problem = (
        vault.problem() if (vault is not None and callable(getattr(vault, "problem", None))) else keyring_problem()
    )
    if problem:
        err_write(f"moss login logout：{problem}。")
        return 1
    try:
        if vault is not None and callable(getattr(vault, "delete", None)):
            existed = vault.exists(name) if callable(getattr(vault, "exists", None)) else True
            if existed:
                vault.delete(name)
            removed = bool(existed)
        else:
            removed = keyring_delete(name)
    except KeyringError as exc:
        err_write(f"moss login logout：{exc}。")
        return 1
    label = vault.label() if (vault is not None and callable(getattr(vault, "label", None))) else keyring_label()
    if not removed:
        write(f"{label} 中未找到名为 {name} 的凭据条目。")
        return 1
    from .auth import copilot_token_manager

    copilot_token_manager.invalidate()
    write(f"已从 {label} 删除凭据条目 {name}（配置文件本身未修改）。")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    home: Path | None = None,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    err_write: Callable[[str], None] | None = None,
    open_browser: Callable[[str], Any] | None = webbrowser.open,
    transport: Any = None,
    vault: Any = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> int:
    err = err_write or (lambda msg: print(msg, file=sys.stderr))
    command = argparse.ArgumentParser(
        prog="moss login",
        description="通过浏览器 OAuth PKCE（OpenRouter）或设备码（GitHub Copilot）一键登录并写入用户配置。",
    )
    command.add_argument(
        "target",
        nargs="?",
        help="登录目标：openrouter | copilot | status | logout",
    )
    command.add_argument(
        "logout_name",
        nargs="?",
        help="与 logout 搭配使用：要从系统凭据管理器删除的条目名称",
    )
    command.add_argument("--name", help="写入配置中的 Provider 名称（默认同目标名）")
    command.add_argument("--model", help="默认模型 ID（留空使用厂商预设默认模型）")
    command.add_argument("--default", action="store_true", help="同时设为全局默认 Provider")
    command.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，仅打印授权地址")
    command.add_argument(
        "--plaintext",
        action="store_true",
        help="仅当系统凭据管理器不可用时，显式允许明文写入 ~/.mosscode/config.yaml",
    )
    command.add_argument("--timeout", type=float, default=180.0, help="等待授权完成的超时秒数（默认 180）")
    command.add_argument("--config", help="status 子命令查看的配置文件路径")
    args = command.parse_args(argv)

    if args.target == "status":
        return _status_report(home, Path(args.config) if args.config else None, write)
    if args.target == "logout":
        if not args.logout_name:
            err("moss login logout 需要指定凭据条目名称，例如 moss login logout copilot。")
            return 2
        return _logout(args.logout_name, write, err, vault=vault)

    target = args.target
    if target is None:
        target = _choose_provider(read=read, write=write)
        if target is None:
            err("已取消登录。")
            return 130
    if target not in AUTH_PROVIDERS:
        err(f"不支持的登录服务：{target}。可选：{'、'.join(AUTH_PROVIDERS)}、status、logout。")
        return 2
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        err("moss login：--timeout 必须是有限的正数秒数。")
        return 2

    # Fail closed BEFORE opening the browser if keyring is unavailable and plaintext was not explicitly requested.
    problem = (
        vault.problem() if (vault is not None and callable(getattr(vault, "problem", None))) else keyring_problem()
    )
    if problem is not None and not args.plaintext:
        import os

        if os.environ.get("MOSSCODE_ALLOW_PLAINTEXT_OAUTH", "").strip() != "1":
            err(
                f"moss login：系统凭据管理器不可用（{problem}），默认拒绝明文保存 OAuth 凭据。"
                "请先配置系统凭据管理器，或显式传入 --plaintext。"
            )
            return 1

    provider = (
        OpenRouterPKCEAuth(transport=transport)
        if target == "openrouter"
        else GitHubCopilotAuth(transport=transport, sleep=sleep)
    )
    browser_fn = None if args.no_browser else open_browser
    try:
        result = asyncio.run(
            provider.login(
                on_notice=write,
                open_browser=browser_fn,
                timeout=max(5.0, float(args.timeout)),
            )
        )
        saved_path, saved_name = persist_oauth_result(
            result,
            home=home,
            name=args.name,
            model=args.model,
            set_default=bool(args.default),
            allow_plaintext=bool(args.plaintext),
            vault=vault,
        )
    except KeyboardInterrupt:
        err("\n已取消登录，未写入任何配置。")
        return 130
    except AuthError as exc:
        err(f"moss login：{exc}")
        return 1

    write(f"✓ {result.detail}")
    write(f"✓ 已保存 Provider {saved_name} 到 {saved_path}。")
    if args.default:
        write(f"✓ 已将 {saved_name} 设为默认模型。")
    return 0
