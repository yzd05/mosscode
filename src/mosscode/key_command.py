"""`moss key`: save an API key in the system credential manager, or delete it; the key itself is never shown."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

from .credentials import SERVICE, KeyringError, keyring_delete, keyring_label, keyring_problem, keyring_set


def read_key(name: str, stdin=None, secret: Callable[[str], str] = getpass.getpass) -> str:
    """Typed without echo in a terminal; the first line of piped input otherwise."""
    from .launcher import is_terminal

    stdin = sys.stdin if stdin is None else stdin
    if is_terminal(stdin):
        return secret(f"{name} 的密钥（输入时不显示）：").strip()
    return stdin.readline().strip()


def main(argv: list[str] | None = None, *, stdin=None, secret: Callable[[str], str] = getpass.getpass) -> int:
    command = argparse.ArgumentParser(
        prog="moss key",
        description="在系统凭据管理器（Windows 凭据管理器、macOS 钥匙串、Linux Secret Service）中保存或删除 API 密钥。"
        "配置中用 api_key_keyring: 名称 引用它。",
    )
    actions = command.add_subparsers(dest="action", required=True, metavar="set | delete")
    for action, text in (("set", "保存或替换密钥：终端中不回显地输入，或从管道读取第一行"), ("delete", "删除密钥")):
        actions.add_parser(action, help=text, description=text).add_argument("name", help="条目名称，例如 deepseek")
    args = command.parse_args(argv)
    try:
        if problem := keyring_problem():
            print(f"moss key：{problem}。", file=sys.stderr)
            return 1
        place = f"{keyring_label()}（{SERVICE} / {args.name}）"
        if args.action == "delete":
            if not keyring_delete(args.name):
                print(f"{place}中没有这个条目。")
                return 1
            print(f"已从{place}删除。")
            return 0
        try:
            key = read_key(args.name, stdin, secret)
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，没有保存。", file=sys.stderr)
            return 130
        if not key:
            print("moss key：密钥为空，没有保存。", file=sys.stderr)
            return 1
        keyring_set(args.name, key)
    except KeyringError as exc:
        print(f"moss key：{exc}。", file=sys.stderr)
        return 1
    print(f"已保存到{place}。在配置的 Provider 中写 api_key_keyring: {args.name}（并删除 api_key 或 api_key_env）。")
    return 0
