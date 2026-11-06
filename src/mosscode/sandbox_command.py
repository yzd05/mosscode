"""`moss sandbox cleanup`: remove the access-control entries the Windows restricted-token sandbox added."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main(argv: list[str] | None = None, *, home: Path | None = None) -> int:
    command = argparse.ArgumentParser(prog="moss sandbox", description="管理沙箱留在系统中的设置。")
    actions = command.add_subparsers(dest="action", required=True, metavar="cleanup")
    text = (
        "移除 Windows 受限令牌后端加在项目目录上的访问控制项（记录在 ~/.mosscode/sandbox-acl.json）。"
        "先关闭正在使用沙箱的 MossCode 会话。"
    )
    clean = actions.add_parser("cleanup", help=text, description=text)
    clean.add_argument("project", nargs="?", help="只清理这个项目目录；省略时清理全部")
    args = command.parse_args(argv)
    if os.name != "nt":
        print("只有 Windows 的受限令牌后端会修改访问控制，这个系统上没有需要清理的设置。")
        return 0
    from .sandbox import windows_token

    root = Path(args.project).expanduser().resolve() if args.project else None
    if root is not None and not any(Path(key) == root for key in windows_token.load(home)["roots"]):
        print(f"{root} 没有访问控制记录。")
        return 1
    lines = windows_token.cleanup(root, home=home)
    if not lines:
        print("没有需要清理的访问控制。")
        return 0
    print("\n".join(lines))
    return 1 if any(line.startswith("未能") for line in lines) else 0
