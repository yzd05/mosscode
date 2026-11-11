"""Layer 1: oversized tool results go to the session directory; the history keeps a preview.

Decided once, before a result enters the history, and never revisited, so requests already sent
keep a byte-identical prefix for prompt caching.
"""

from collections.abc import Sequence
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
# A batch of reads over the combined budget is cut to shorter pages instead.
EXEMPT = frozenset({"read_file"})
READ_FLOOR = 2_000  # Tokens every read of a batch may keep, whatever the batch adds up to.
NOTE_TOKENS = 60  # Room for the closing note of a shortened read.


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


def share(sizes: Sequence[int], available: int) -> int:
    """The largest cap that fits the sizes into available: small ones whole, the large ones cut alike."""
    remaining, left = available, len(sizes)
    for size in sorted(sizes):
        if size * left > remaining:
            return max(remaining // left, 0)
        remaining, left = remaining - size, left - 1
    return max(sizes, default=0)


def shorten_read(result: ToolResult, tokens: int) -> ToolResult:
    """A read_file page cut to about tokens at a line boundary, ending with where to read on."""
    lines = result.output.split("\n")
    if result.details.get("truncated"):
        lines.pop()  # The page's own note about the next offset.
    kept, used = 0, 0
    for line in lines:
        cost = estimate_text(line) + 1
        if kept and used + cost > tokens:
            break
        kept, used = kept + 1, used + cost
    number = lines[kept - 1].split("\t", 1)[0].strip() if kept else ""
    if kept == len(lines) or not number.isdigit():
        return result
    last = int(number)
    note = (
        f"（共 {result.details.get('total_lines', '?')} 行；同一批读取的内容合计超出上下文预算，"
        f"此文件只保留到第 {last} 行，请从 offset={last + 1} 继续读取）"
    )
    # budgeted: decided, like a stored result, and never shortened again.
    details = {**result.details, "lines": kept, "truncated": True, "next_offset": last + 1, "budgeted": True}
    return replace(result, output="\n".join([*lines[:kept], note]), details=details)


def bound_message(message: Message, session: Session | None) -> Message:
    """One batch over the combined budget: reads are cut to shorter pages, then other results stored, largest first."""
    results = list(message.tool_results)
    sizes = [estimate_text(r.model_text()) for r in results]
    total = sum(sizes)
    if total <= MESSAGE_TOKENS:
        return message
    # Text pages only: an image read has no lines to page through.
    reads = [
        i
        for i, r in enumerate(results)
        if r.name in EXEMPT and not r.is_error and "lines" in r.details and not r.details.get("budgeted")
    ]
    if reads:
        # Shortening a read costs nothing but another page request, so it comes before storing anything.
        others = total - sum(sizes[i] for i in reads)
        cap = max(share([sizes[i] for i in reads], MESSAGE_TOKENS - others), READ_FLOOR)
        for index in reads:
            if sizes[index] > cap:
                results[index] = shorten_read(results[index], cap - NOTE_TOKENS)
                updated = estimate_text(results[index].model_text())
                total += updated - sizes[index]
                sizes[index] = updated
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
