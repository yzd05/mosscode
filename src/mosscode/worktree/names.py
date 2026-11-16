"""Worktree names. A name may come from a command, so it is checked before it reaches a path or git."""

from __future__ import annotations

import re
import secrets

NAME_LIMIT = 64
SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
EPHEMERAL = re.compile(r"agent-[0-9a-f]{8}")
BRANCH_PREFIX = "mosscode/"
# Names Windows reserves for devices, with or without an extension.
RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def validate_name(name: str) -> str | None:
    """None when the name is usable, otherwise the reason."""
    if not name:
        return "名称不能为空"
    if len(name) > NAME_LIMIT:
        return f"名称最长 {NAME_LIMIT} 个字符"
    for segment in name.split("/"):
        if not segment:
            return "名称中不能有空段（// 或开头、结尾的 /）"
        if segment in {".", ".."}:
            return "名称中不能有 . 或 .. 段"
        if ".." in segment:  # git refuses it in a branch name.
            return "名称中不能有连续的 .."
        if not SEGMENT.fullmatch(segment):
            return f"段 {segment!r} 只能含字母、数字、.、_ 和 -"
        if segment[0] in ".-":
            return f"段 {segment!r} 不能以 . 或 - 开头"
        if segment.endswith(".") or segment.lower().endswith(".lock"):
            return f"段 {segment!r} 不能以 . 或 .lock 结尾"
        if segment.split(".", 1)[0].lower() in RESERVED:
            return f"段 {segment!r} 是 Windows 保留的设备名"
    return None


def flatten(name: str) -> str:
    """One directory level and one branch level, so a/b never collides with a branch a (git D/F)."""
    return name.replace("/", "+")


def branch_of(flat: str) -> str:
    return BRANCH_PREFIX + flat


def ephemeral_name() -> str:
    return f"agent-{secrets.token_hex(4)}"
