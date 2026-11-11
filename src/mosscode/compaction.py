"""Layer 2: summarize older messages in one tool-free request; keep recent ones verbatim.

Pure with respect to the agent: returns a new history or raises CompactionError, never mutates the input.
"""

import math
import re
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .context import ContextBudget, estimate_messages, estimate_text
from .images import ImageRef
from .messages import Completed, ContentBlock, Message, ProviderError, ToolResult
from .session import Session

KEEP_TOKENS = 10_000
KEEP_MESSAGES = 5
KEEP_SHARE = 0.4  # Of the auto threshold: the kept tail never crowds out the summary's benefit.
QUOTE_TOKENS = 8_000
QUOTE_CLIP = 200
RESULT_CLIP = 2_000
SHRINK_STEPS = 3
DROP_SHARE = 0.2
BOUNDARY = "compact"
QUOTE = "\n\n▸ "  # Prefix of each verbatim user quote block in a boundary message.
CLIPPED = " …（较长，原文见记录文件）"
NOTHING = "没有可压缩的内容：对话中只有最近一轮。"
# Results the model can get again: files are read again, the rest is stored in the session directory.
CLEARABLE = frozenset({"read_file", "grep", "glob", "execute_command", "web_fetch", "web_search", "task_output"})
CLEAR_MINIMUM = 300  # Tokens: a smaller result saves too little once the note replaces it.
CLEAR_TARGET = 0.5  # Of the auto threshold: clearing alone is enough when the context ends up under it.
CLEAR_NOTE = 100  # Tokens a note is assumed to cost when sizing up what clearing would free.

SYSTEM = "你是对话摘要器。禁止调用任何工具，只输出纯文本；任何工具调用都会被拒绝，本次任务会失败。"

SECTIONS = """1. 主要请求和意图：用户要完成什么，包括后来补充或修改的要求
2. 关键技术概念：涉及的技术、框架、约定
3. 文件和代码段：查看或修改过的文件，各自的原因和改动；关键代码可以简短引用
4. 错误和修复：遇到的错误、原因和修复方式，以及用户对修复的反馈
5. 问题解决过程：已解决的问题和仍在排查的问题
6. 用户的偏好与约束：用户明确提出的做法、禁止事项和风格要求
7. 待办任务：用户要求但尚未完成的事项
8. 当前工作：压缩前正在做什么、做到哪一步、涉及的文件和代码（最详细的一部分）
9. 下一步：与当前工作直接相关的下一步；没有明确的下一步就写“无”"""


class CompactionError(Exception):
    """A safe, user-facing reason. code "nothing" means there was nothing to compact (not a failure)."""

    def __init__(self, reason: str, code: str = "failed"):
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class Compacted:
    history: list[Message]
    removed: int
    summary_tokens: int
    record: Path | None


def _group_start(history: Sequence[Message], index: int) -> int:
    # Never start on tool results: keep the assistant call that produced them.
    while index > 0 and history[index].role == "tool":
        index -= 1
    return index


def _only_boundary(messages: Sequence[Message]) -> bool:
    # Re-summarizing a lone summary gains nothing.
    return all(m.synthetic == BOUNDARY for m in messages)


def split_point(history: Sequence[Message], threshold: int) -> int:
    """Index where the kept tail starts; 0 means nothing can be compacted."""
    if not history:
        return 0
    start = _group_start(history, len(history) - 1)
    tokens = estimate_messages(history[start:])
    cap = int(threshold * KEEP_SHARE)
    while start > 0 and (tokens < KEEP_TOKENS or len(history) - start < KEEP_MESSAGES):
        earlier = _group_start(history, start - 1)
        extra = estimate_messages(history[earlier:start])
        if tokens + extra > cap:
            break
        start, tokens = earlier, tokens + extra
    return 0 if _only_boundary(history[:start]) else start


def cut_point(history: Sequence[Message], threshold: int, *, manual: bool = False) -> int:
    """split_point; a manual /compact on a short history keeps only the last group instead."""
    cut = split_point(history, threshold)
    if cut or not manual or not history:
        return cut
    start = _group_start(history, len(history) - 1)
    return 0 if _only_boundary(history[:start]) else start


@dataclass(frozen=True)
class Cleared:
    history: list[Message]
    results: dict[str, ToolResult]  # By call ID, as they read now.
    tokens: int  # Freed, by estimate.
    paths: tuple[str, ...]  # Files none of whose reads is left in the history.


def _clearable(result: ToolResult) -> bool:
    if result.details.get("cleared") or result.images:
        return False
    if result.name not in CLEARABLE and not result.name.startswith("mcp__"):
        return False
    return estimate_text(result.model_text()) > CLEAR_MINIMUM


def clearable_tokens(history: Sequence[Message], cut: int) -> int:
    """About how much clearing the results before cut would free; nothing is written."""
    sizes = [estimate_text(r.model_text()) for m in history[:cut] for r in m.tool_results if _clearable(r)]
    return sum(max(size - CLEAR_NOTE, 0) for size in sizes)


def _is_read(result: ToolResult) -> bool:
    return result.name == "read_file" and not result.is_error and "path" in result.details


def _cleared(result: ToolResult, session: Session | None) -> ToolResult | None:
    """The note that replaces a result, or None when its content could not be kept anywhere."""
    lead = f"（较早的工具结果已清理以节省上下文，原有约 {estimate_text(result.output)} Token。"
    if _is_read(result):
        note = f"{lead}需要其中的内容时重新读取这个文件，不要凭记忆补写。）"
        return replace(result, output=note, details={**result.details, "cleared": True})
    path = result.details.get("persisted")
    if not path:
        if session is None:
            return None
        try:
            path = str(session.persist_result(result.call_id, result.output))
        except OSError:
            return None
    note = f"{lead}完整内容保存在：\n{path}\n需要其中的内容时用 read_file 读取这个文件，不要凭记忆补写。）"
    return replace(result, output=note, details={**result.details, "persisted": path, "cleared": True})


def clear_results(history: Sequence[Message], cut: int, session: Session | None) -> Cleared:
    """The history with the larger tool results before cut replaced by notes; no message is added or removed."""
    changed: dict[str, ToolResult] = {}
    cleared_paths: list[str] = []
    freed = 0
    updated = list(history)
    for index, message in enumerate(history[:cut]):
        results = list(message.tool_results)
        for position, result in enumerate(results):
            note = _cleared(result, session) if _clearable(result) else None
            if note is None:
                continue
            results[position] = changed[result.call_id] = note
            freed += estimate_text(result.model_text()) - estimate_text(note.model_text())
            if _is_read(result):
                cleared_paths.append(result.details["path"])
        if results != list(message.tool_results):
            updated[index] = replace(message, tool_results=tuple(results))
    held = {r.details["path"] for m in updated for r in m.tool_results if _is_read(r) and not r.details.get("cleared")}
    paths = tuple(dict.fromkeys(path for path in cleared_paths if path not in held))
    return Cleared(updated, changed, freed, paths)


def carried_quotes(message: Message) -> list[str]:
    return [b.text[len(QUOTE) :] for b in message.content if b.text.startswith(QUOTE)]


def collect_quotes(removed: Sequence[Message]) -> list[str]:
    """User input in time order, including quotes a previous compaction attached."""
    quotes = []
    for message in removed:
        if message.synthetic == BOUNDARY:
            quotes.extend(carried_quotes(message))
        elif message.role == "user" and message.synthetic is None and message.text.strip():
            quotes.append(message.text)
    return quotes


def fit_quotes(quotes: Sequence[str], budget: int = QUOTE_TOKENS) -> tuple[list[str], int]:
    """Newest first: whole if it fits, else the first 200 characters, else omitted."""
    kept, used, omitted = [], 0, 0
    for quote in reversed(quotes):
        for candidate in (quote, quote if quote.endswith(CLIPPED) else quote[:QUOTE_CLIP].rstrip() + CLIPPED):
            cost = estimate_text(candidate)
            if used + cost <= budget:
                kept.append(candidate)
                used += cost
                break
        else:
            omitted += 1
    kept.reverse()
    return kept, omitted


def _image(image: ImageRef) -> str:
    """The summary keeps what an image was, never its pixels."""
    return image.placeholder(f"图片 {image.width}×{image.height}")


def render(message: Message, shrink: bool = False) -> str:
    """One message as role-tagged plain text, so no protocol ever sees unpaired tool calls."""
    if message.synthetic == BOUNDARY:
        return "[之前的压缩摘要]\n" + message.text.strip()
    if message.role == "user":
        content = (_image(b.image) if b.image is not None else b.text for b in message.content if b.kind != "thinking")
        return "[用户]\n" + "".join(content)
    if message.role == "assistant":
        parts = ["[助手]\n" + message.text] if message.text.strip() else []
        parts += [f"[工具调用] {call.name} {call.arguments}" for call in message.tool_calls]
        return "\n".join(parts) or "[助手]（无文本）"
    parts = []
    for result in message.tool_results:
        text = result.model_text()
        if shrink and len(text) > RESULT_CLIP:
            text = text[:RESULT_CLIP] + f"\n…（已缩短，原文 {len(text)} 字符）"
        text = "\n".join([text, *map(_image, result.images)])
        parts.append(f"[工具结果 · {result.name}]\n{text}")
    return "\n".join(parts)


def transcript(messages: Sequence[Message], *, shrink: bool = False, dropped: int = 0) -> str:
    lead = [f"（更早的 {dropped} 条消息因篇幅省略）"] if dropped else []
    return "\n\n".join(lead + [render(m, shrink) for m in messages])


def summary_request(text: str, instructions: str = "") -> str:
    extra = f"\n用户对本次摘要的额外要求：{instructions.strip()}" if instructions.strip() else ""
    return (
        f"{SYSTEM}\n\n下面是编程助手与用户较早的一段对话记录，请压缩成摘要，供助手在后续工作中代替这段记录使用。\n\n"
        f"<transcript>\n{text}\n</transcript>\n\n"
        "先在 <analysis> 中按时间顺序梳理：用户每次要求了什么，做了哪些修改，涉及哪些文件和代码，"
        "遇到了什么错误、怎么修复的。\n"
        f"再在 <summary> 中按以下 9 部分写正式摘要：\n{SECTIONS}\n"
        "用户原话会由程序另外附上，摘要中不必逐字复述，只需总结其中的要求和约束。"
        f"{extra}\n\n再次提醒：不要调用任何工具，只输出 <analysis> 和 <summary>。"
    )


def parse_summary(response: Completed) -> str:
    """The <summary> body, or a failure reason."""
    if response.message.tool_calls:
        raise CompactionError("摘要回复包含工具调用。")
    if response.stop_reason == "max_tokens":
        raise CompactionError("摘要回复达到输出上限被截断。")
    found = re.findall(r"<summary>(.*?)</summary>", response.message.text, re.DOTALL)
    summary = found[-1].strip() if found else ""
    if not summary:
        raise CompactionError("摘要回复中没有有效的 <summary>。")
    return summary


def record_text(removed: Sequence[Message]) -> str:
    stamp = datetime.now().isoformat(timespec="seconds")
    return f"# MossCode 压缩记录\n\n压缩时间：{stamp}\n被压缩的消息：{len(removed)} 条\n\n{transcript(removed)}\n"


def boundary(summary: str, quotes: Sequence[str], omitted: int, record: Path | None) -> Message:
    """Replaces the compacted messages; quotes stay separate blocks so the next compaction carries them."""
    blocks = [
        ContentBlock("text", "（上下文已压缩：较早的对话由下面的摘要代替，之后的消息保留原文。）"),
        ContentBlock("text", f"\n\n摘要：\n{summary}"),
    ]
    if quotes:
        blocks.append(ContentBlock("text", "\n\n用户原话（按时间顺序，由程序原样附上）："))
        blocks += [ContentBlock("text", QUOTE + quote) for quote in quotes]
    if omitted:
        blocks.append(ContentBlock("text", f"\n\n（另有 {omitted} 条更早的用户消息未附上，原文见记录文件。）"))
    where = f"压缩前的完整记录：{record}\n" if record else "（本次未能保存压缩前的完整记录。）\n"
    blocks.append(
        ContentBlock(
            "text",
            f"\n\n{where}需要具体代码、报错或输出时用 read_file 读取记录文件；"
            "文件可能已变化，修改前必须重新读取，不要依据摘要补写代码。",
        )
    )
    return Message("user", tuple(blocks), synthetic=BOUNDARY)


async def _ask(provider, prompt: str) -> Completed:
    async with aclosing(
        provider.stream([Message("user", (ContentBlock("text", prompt),))], SYSTEM, (), allow_tools=False)
    ) as events:
        async for event in events:
            if isinstance(event, Completed):
                return event
    raise ProviderError("摘要回复在完成信号前中断。")


async def compact_history(
    provider,
    history: Sequence[Message],
    budget: ContextBudget,
    *,
    session: Session | None = None,
    instructions: str = "",
    manual: bool = False,
) -> Compacted:
    cut = cut_point(history, budget.auto_threshold, manual=manual)
    if cut == 0:
        raise CompactionError(NOTHING, "nothing")
    removed, tail = list(history[:cut]), list(history[cut:])
    limit = budget.manual_limit
    shrink, dropped = False, 0
    for step in range(SHRINK_STEPS + 1):
        prompt = summary_request(transcript(removed[dropped:], shrink=shrink, dropped=dropped), instructions)
        response = None
        if estimate_text(SYSTEM) + estimate_text(prompt) <= limit:
            try:
                response = await _ask(provider, prompt)
            except ProviderError as exc:
                if exc.code != "context_overflow":
                    raise CompactionError(f"摘要请求失败：{exc}") from None
        if response is not None:
            break
        if step == SHRINK_STEPS:
            raise CompactionError(f"摘要请求缩减 {SHRINK_STEPS} 次后仍超出上下文窗口。")
        # Shorten tool results first, then drop the earliest 20% of messages each time.
        if not shrink:
            shrink = True
            continue
        remaining = len(removed) - dropped
        if remaining <= 1:
            raise CompactionError("摘要请求超出上下文窗口，且无法继续缩减。")
        dropped += min(remaining - 1, max(1, math.ceil(remaining * DROP_SHARE)))
    summary = parse_summary(response)
    quotes, omitted = fit_quotes(collect_quotes(removed))
    record = None
    if session is not None:
        try:
            record = session.write_record(record_text(removed))
        except OSError:
            record = None
    return Compacted([boundary(summary, quotes, omitted, record)] + tail, cut, estimate_text(summary), record)
