"""Unified diffs for people (approval and transcript); the model gets a summary."""

import difflib

DIFF_LIMIT = 200
DIFF_INPUT_LIMIT = 20000  # Lines per side; larger files skip the quadratic matcher.


def unified_diff(before: str, after: str, name: str, *, limit: int = DIFF_LIMIT) -> str:
    old, new = before.splitlines(), after.splitlines()
    if max(len(old), len(new)) > DIFF_INPUT_LIMIT:
        return f"（文件超过 {DIFF_INPUT_LIMIT} 行，未生成 diff）"
    lines = list(difflib.unified_diff(old, new, f"a/{name}", f"b/{name}", lineterm="", n=3))
    if len(lines) > limit:
        lines = [*lines[:limit], f"…（diff 共 {len(lines)} 行，仅显示前 {limit} 行）"]
    return "\n".join(lines)
