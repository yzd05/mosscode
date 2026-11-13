"""/goal: a completion condition the session keeps working towards, judged after each turn by a separate call."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from .context import estimate_text
from .limits import stream
from .messages import CONTINUE, NOTIFICATION, REMINDER, Completed, ContentBlock, Message, ProviderError
from .prompts import Supplement

GOAL = "goal"  # Message.synthetic of the message that starts a continuation turn.
MAX_CONDITION = 4000
DEFAULT_MAX_TURNS = 30
IDLE_LIMIT = 3  # Consecutive turns without a tool call.
FAILURE_LIMIT = 3  # Consecutive evaluations that failed or could not be read.
INPUT_TOKENS = 16_000  # The conversation tail the evaluator reads.
RESULT_CHARS = 1000  # Per tool result.
RESULTS_SHOWN = 20  # The newest tool results keep their text; older ones only their name.
ARGUMENT_CHARS = 300
REASON_CHARS = 500

SYSTEM = "你是目标判断器。禁止调用任何工具，只输出一行 JSON；任何工具调用都会被拒绝，本次判断会失败。"
INSTRUCTIONS = """用户给一个编码助手设定了目标条件，助手会一轮一轮地工作，直到条件满足。请根据下面的对话判断条件现在是否已经满足。

- 只依据对话中已经出现的证据：工具结果、命令输出、文件内容。助手自己声称完成但没有对应证据的，不算满足。
- 条件中提到的每一项都要有证据；部分满足算未满足。
- 只有确实无法达成时（例如所需的东西不存在、只能由用户提供、与条件本身矛盾）才判断为不可能；还能继续尝试的算未满足。

只输出一行 JSON，不要其他文字：
{"verdict": "met" | "not_met" | "impossible", "reason": "…"}
reason 用中文，不超过 200 字：met 时写依据；not_met 时写还差什么、下一步做什么；impossible 时写原因。"""

Verdicts = Literal["met", "not_met", "impossible"]


@dataclass(frozen=True)
class Verdict:
    verdict: Verdicts
    reason: str


@dataclass
class Goal:
    condition: str
    budget: int  # Turns allowed before pausing; resuming from that pause allows as many again.
    tokens: int = 0  # Session tokens when the goal was set (or restored).
    started: float = field(default_factory=monotonic)
    turns: int = 0
    idle: int = 0
    failures: int = 0
    reason: str = ""  # The latest evaluator reason.
    paused: str = ""  # Why the automatic turns stopped; empty while they run.

    @property
    def active(self) -> bool:
        return not self.paused


@dataclass(frozen=True)
class GoalStep:
    """What follows the turn that just ended."""

    action: Literal["continue", "met", "impossible", "paused", "deferred", "none"]
    note: str = ""
    prompt: str = ""  # The next turn's message, for continue.


def check_condition(text: str) -> str:
    condition = text.strip()
    if not condition:
        raise ValueError("目标条件不能为空。")
    if len(condition) > MAX_CONDITION:
        raise ValueError(f"目标条件最多 {MAX_CONDITION} 字，当前 {len(condition)} 字。")
    return condition


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字，已截断）"


def _render(message: Message, shown_results: set[int]) -> str:
    if message.role == "user":
        if message.synthetic == GOAL:
            return "[目标续跑]\n" + message.text
        if message.synthetic == CONTINUE:
            return "[回复达到输出上限，自动请模型接着写]"
        if message.synthetic == "compact":
            return "[较早对话的摘要]\n" + message.text
        if message.synthetic == NOTIFICATION:
            return "[后台任务通知]\n" + _clip(message.text, RESULT_CHARS)
        if message.synthetic is not None:
            return f"[附加内容 · {message.synthetic}]\n" + _clip(message.text, RESULT_CHARS)
        return "[用户]\n" + message.shown
    if message.role == "assistant":
        parts = ["[助手]\n" + message.text] if message.text.strip() else []
        for call in message.tool_calls:
            parts.append(f"[工具调用 · {call.name}] {_clip(call.arguments, ARGUMENT_CHARS)}")
        return "\n".join(parts) or "[助手]（无文本）"
    parts = []
    for result in message.tool_results:
        status = "失败" if result.is_error else "成功"
        if id(result) in shown_results:
            parts.append(f"[工具结果 · {result.name} · {status}]\n{_clip(result.model_text(), RESULT_CHARS)}")
        else:
            parts.append(f"[工具结果 · {result.name} · {status}，内容已省略]")
    return "\n".join(parts)


def conversation_tail(messages: Sequence[Message], limit: int = INPUT_TOKENS) -> str:
    """The newest messages within limit tokens; only the newest tool results keep their text."""
    results = [r for m in messages if m.role == "tool" for r in m.tool_results]
    shown = {id(r) for r in results[-RESULTS_SHOWN:]}
    kept, used = [], 0
    for message in reversed(messages):
        if message.synthetic == REMINDER:
            continue  # The agent's own notes (the checklist, the goal itself) are not evidence.
        block = _render(message, shown)
        cost = estimate_text(block) + 1
        if kept and used + cost > limit:
            break
        kept.append(block)
        used += cost
    kept.reverse()
    omitted = len(messages) - len(kept)
    lead = [f"（更早的 {omitted} 条消息已省略）"] if omitted else []
    return "\n\n".join(lead + kept)


def evaluation_prompt(condition: str, messages: Sequence[Message]) -> str:
    return (
        f"{INSTRUCTIONS}\n\n## 目标条件\n<condition>\n{condition}\n</condition>\n\n"
        f"## 对话（最近部分）\n<conversation>\n{conversation_tail(messages)}\n</conversation>"
    )


def parse_verdict(text: str) -> Verdict:
    """The JSON object in the reply; ValueError when there is none or it is malformed."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError("判断结果不是 JSON。")
    raw = json.loads(match.group(), strict=False)  # Models sometimes put a raw newline in the reason.
    if not isinstance(raw, dict) or raw.get("verdict") not in {"met", "not_met", "impossible"}:
        raise ValueError("判断结果不合法。")
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("判断结果不合法。")
    return Verdict(raw["verdict"], " ".join(reason.split())[:REASON_CHARS])


async def evaluate(
    provider, condition: str, messages: Sequence[Message], on_usage: Callable | None = None, limits=None
) -> Verdict:
    """One request without tools; ProviderError or ValueError when it fails."""
    prompt = evaluation_prompt(condition, messages)
    async with aclosing(
        stream(provider, [Message("user", (ContentBlock("text", prompt),))], SYSTEM, allow_tools=False, limits=limits)
    ) as events:
        async for event in events:
            if isinstance(event, Completed):
                if on_usage is not None:
                    on_usage(event.usage)
                return parse_verdict(event.message.text)
    raise ProviderError("判断请求在完成信号前中断。")


def goal_reminder(goal: Goal | None) -> Supplement | None:
    """With every request while a goal is set; after the cache breakpoint."""
    if goal is None:
        return None
    lines = [
        "用户设定了一个目标，会话会自动一轮一轮地继续，直到它达成：",
        f"<condition>\n{goal.condition}\n</condition>",
        f"已运行 {goal.turns} 轮，上限 {goal.budget} 轮。",
    ]
    if goal.reason:
        lines.append(f"上一次判断：尚未达成。{goal.reason}")
    lines.append(
        "每轮结束后由另一次模型调用只根据对话中的证据判断是否达成：完成后运行能证明条件满足的检查（如测试命令），"
        "在回复中给出结果。条件确实无法达成时说明原因。不要为了显得达成而降低标准或修改检查。"
    )
    return Supplement("goal", "\n".join(lines))


def continue_prompt(reason: str) -> str:
    detail = reason or "没有给出理由。"
    return f"继续推进目标。上一轮结束后的判断：尚未达成。{detail}"


def elapsed_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟"
