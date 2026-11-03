"""Approximate context accounting: no tokenizer, anchored on the last reported usage."""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .messages import Message, Usage

DEFAULT_CONTEXT_WINDOW = 128_000
SUMMARY_RESERVE = 20_000  # Room for a structured summary; normal replies use max_output_tokens.
AUTO_MARGIN = 13_000  # Absorbs one round of growth plus estimation error between checks.
MANUAL_MARGIN = 3_000
MIN_THRESHOLD = 16_000
MESSAGE_OVERHEAD = 4


def estimate_text(text: str) -> int:
    """ASCII at 4 characters per token, every other character (CJK etc.) as one token: errs high."""
    if not text:
        return 0
    ascii_count = len(text.encode("ascii", "ignore"))
    return math.ceil(ascii_count / 4) + len(text) - ascii_count


def estimate_message(message: Message) -> int:
    total = MESSAGE_OVERHEAD
    total += sum(b.image.tokens if b.image is not None else estimate_text(b.text) for b in message.content)
    total += sum(estimate_text(call.name) + estimate_text(call.arguments) for call in message.tool_calls)
    total += sum(estimate_text(result.model_text()) for result in message.tool_results)
    return total + sum(image.tokens for result in message.tool_results for image in result.images)


def estimate_messages(messages: Sequence[Message]) -> int:
    return sum(estimate_message(m) for m in messages)


@dataclass(frozen=True)
class ContextBudget:
    """Thresholds for one provider. The window comes from configuration, never from a model table."""

    window: int
    max_output_tokens: int
    compact_threshold: int | None = None
    configured: bool = True

    @classmethod
    def for_provider(
        cls, context_window: int | None, max_output_tokens: int, compact_threshold: int | None = None
    ) -> "ContextBudget":
        return cls(
            context_window or DEFAULT_CONTEXT_WINDOW, max_output_tokens, compact_threshold, context_window is not None
        )

    @property
    def effective(self) -> int:
        # Services usually require input + max output to fit the window.
        return self.window - max(self.max_output_tokens, SUMMARY_RESERVE)

    @property
    def window_threshold(self) -> int:
        return self.effective - AUTO_MARGIN

    @property
    def auto_threshold(self) -> int:
        limit = self.window_threshold
        return min(limit, self.compact_threshold) if self.compact_threshold else limit

    @property
    def manual_limit(self) -> int:
        return self.effective - MANUAL_MARGIN

    def problem(self) -> str | None:
        if self.window_threshold < MIN_THRESHOLD:
            return (
                f"context_window（{self.window}）减去输出预留和安全余量后不足 {MIN_THRESHOLD}，"
                "请调大 context_window 或调小 max_output_tokens。"
            )
        if self.compact_threshold is not None and not MIN_THRESHOLD <= self.compact_threshold <= self.window_threshold:
            return f"compact_threshold 需在 {MIN_THRESHOLD} 与 {self.window_threshold} 之间。"
        return None


class ContextMeter:
    """Size of the next request: last reported input + output, plus an estimate of what came after."""

    def __init__(self):
        self.anchor: int | None = None
        self.anchor_length = 0

    def record(self, usage: Usage, history_length: int) -> None:
        if usage.input_tokens is None:
            self.anchor = None
            return
        self.anchor = usage.input_tokens + (usage.output_tokens or 0)
        self.anchor_length = history_length

    def invalidate(self) -> None:
        self.anchor = None

    def estimate(self, history: Sequence[Message], extra: str = "", fixed: Callable[[], str] = lambda: "") -> int:
        if self.anchor is not None and self.anchor_length <= len(history):
            return self.anchor + estimate_messages(history[self.anchor_length :]) + estimate_text(extra)
        # No usable anchor: count the whole request, including system prompt and tool definitions.
        return estimate_text(fixed()) + estimate_messages(history) + estimate_text(extra)
