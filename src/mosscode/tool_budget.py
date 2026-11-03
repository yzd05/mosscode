"""Layer 1: oversized tool results go to the session directory; the history keeps a preview.

Decided once, before a result enters the history, and never revisited, so requests already sent
keep a byte-identical prefix for prompt caching.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

from .context import estimate_text
from .messages import Message, ToolResult
from .session import Session

RESULT_TOKENS = 12_500
MESSAGE_TOKENS = 50_000
HEAD, TAIL = 2_000, 1_000
# read_file is paged and reads files already on disk; storing it would loop on the stored copy.
EXEMPT = frozenset({"read_file"})


def preview(text: str, tokens: int, path: Path | None) -> str:
    size = f"输出过大（约 {tokens} Token，{len(text)} 字符）"
    if path is None:
        lead = f"{size}，本次未能保存完整内容。"
    else:
        lead = (
            f"{size}，完整内容已保存到：\n{path}\n"
            "需要完整内容时用 read_file 读取这个文件（可用 offset 和 limit 分页）。"
        )
    omitted = len(text) - HEAD - TAIL
    return (
        f"{lead}\n以下是开头 {HEAD} 字符和末尾 {TAIL} 字符：\n{text[:HEAD]}\n"
        f"…（中间省略 {omitted} 字符）…\n{text[-TAIL:]}"
    )


def store(text: str, details: dict[str, Any], session: Session | None, call_id: str) -> tuple[str, dict[str, Any]]:
    tokens = estimate_text(text)
    path = None
    if session is not None:
        try:
            path = session.persist_result(call_id, text)
        except OSError:
            path = None
    extra = {"persisted": str(path)} if path else {"truncated": True}
    extra["budgeted"] = True  # Already a preview: never stored again.
    return preview(text, tokens, path), {**details, **extra}


def bound_result(
    name: str, call_id: str, text: str, details: dict[str, Any], session: Session | None
) -> tuple[str, dict[str, Any]]:
    """One result over the threshold is stored."""
    if name in EXEMPT or estimate_text(text) <= RESULT_TOKENS:
        return text, details
    return store(text, details, session, call_id)


def bound_message(message: Message, session: Session | None) -> Message:
    """Results of one batch over the combined budget are stored, largest first."""
    results = list(message.tool_results)
    sizes = [estimate_text(r.model_text()) for r in results]
    total = sum(sizes)
    if total <= MESSAGE_TOKENS:
        return message
    candidates = sorted(
        (i for i, r in enumerate(results) if r.name not in EXEMPT and not r.details.get("budgeted")),
        key=lambda i: sizes[i],
        reverse=True,
    )
    for index in candidates:
        if total <= MESSAGE_TOKENS:
            break
        result = results[index]
        output, details = store(result.output, result.details, session, result.call_id)
        results[index] = replace(result, output=output, details=details)
        updated = estimate_text(results[index].model_text())
        total += updated - sizes[index]
        sizes[index] = updated
    return replace(message, tool_results=tuple(results))


def persisted_note(result: ToolResult) -> str:
    path = result.details.get("persisted")
    return f"完整结果已存盘：{path}" if path else ""
