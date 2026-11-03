"""Project-owned messages and stream events; SDK types stop at the adapters."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .images import ImageRef
from .permissions import ApprovalRequest


@dataclass(frozen=True)
class ContentBlock:
    kind: Literal["text", "thinking", "image"]
    text: str  # For an image, its alt text.
    image: ImageRef | None = None


IMAGE_MARK = re.compile(r"\[图片 #(\d+)\]")


def image_mark(number: int) -> str:
    return f"[图片 #{number}]"


def user_content(text: str, images: Mapping[str, ImageRef] | None = None) -> tuple[ContentBlock, ...]:
    """The text with each attached image right after the first occurrence of its placeholder."""
    blocks: list[ContentBlock] = []
    start, used = 0, set()
    for match in IMAGE_MARK.finditer(text):
        image = (images or {}).get(match.group())
        if image is None or match.group() in used:
            continue
        used.add(match.group())
        blocks += [ContentBlock("text", text[start : match.end()]), ContentBlock("image", image.alt, image)]
        start = match.end()
    # Anthropic rejects whitespace-only text blocks, e.g. the space typed after the last placeholder.
    if text[start:].strip() or not blocks:
        blocks.append(ContentBlock("text", text[start:]))
    return tuple(blocks)


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    content: tuple[ContentBlock, ...]
    # Retain protocol-native signed/opaque data for the same adapter only.
    protocol: str | None = None
    native: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    tool_calls: tuple["ToolCall", ...] = ()
    tool_results: tuple["ToolResult", ...] = ()
    # Written by the program, not typed by the user: "compact" marks a compaction boundary.
    synthetic: str | None = None
    # A prompt command as typed ("/review 安全"); content holds the expanded template.
    command: str | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if b.kind == "text")

    @property
    def shown(self) -> str:
        """What the user typed: the command for prompt commands, otherwise the text."""
        return self.command if self.command is not None else self.text


@dataclass(frozen=True)
class Delta:
    kind: Literal["text", "thinking"]
    text: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    uncached_input_tokens: int | None = None


@dataclass(frozen=True)
class Completed:
    message: Message
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    # Local prompt contributions; adapters serialize only API-supported fields.
    prompt_summary: str = ""
    prompt_guidelines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    output: str
    is_error: bool = False
    code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    images: tuple[ImageRef, ...] = ()

    def model_text(self) -> str:
        # Details stay local (UI/tests); the model reads plain text only.
        if not self.is_error:
            return self.output
        return f"错误 [{self.code or 'error'}]：{self.output}"


@dataclass(frozen=True)
class ToolProgress:
    call: ToolCall
    phase: Literal["parsing", "running", "finished"]
    result: ToolResult | None = None
    elapsed: float = 0.0


@dataclass(frozen=True)
class ResponseBoundary:
    """A complete assistant tool request, followed by execution and another iteration."""

    message: Message


@dataclass(frozen=True)
class IterationProgress:
    iteration: int
    max_iterations: int
    mode: Literal["normal", "plan"]


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    missing_input: int = 0
    missing_output: int = 0
    requests: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    missing_cached: int = 0
    missing_cache_write: int = 0
    cost_usd: float = 0.0  # Estimated from the model catalog's base prices.
    missing_cost: int = 0  # Requests without a price: model not in the catalog, or counts not reported.


@dataclass(frozen=True)
class UsageUpdate:
    iteration: int
    usage: Usage
    total: UsageTotals


@dataclass(frozen=True)
class RunStopped:
    reason: Literal["completed", "max_iterations", "max_tokens", "unknown_tools", "cancelled", "error"]
    notice: str
    iterations: int
    results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class UserInput:
    """Messages queued during a run, merged into history at an iteration boundary."""

    texts: tuple[str, ...]


@dataclass(frozen=True)
class TasksNotified:
    """Background task notifications merged into history at an iteration boundary."""

    texts: tuple[str, ...]


NOTIFICATION = "task-notification"  # Message.synthetic of a background task notification.


@dataclass(frozen=True)
class Retrying:
    """A transient failure before any event of this request; nothing was executed."""

    attempt: int
    max_attempts: int
    delay: float
    reason: str


@dataclass(frozen=True)
class CompactStarted:
    manual: bool


@dataclass(frozen=True)
class CompactFinished:
    removed: int
    summary_tokens: int
    elapsed: float
    record: str | None = None


@dataclass(frozen=True)
class CompactFailed:
    reason: str
    consecutive: int  # Consecutive automatic failures; 3 pauses automatic compaction.
    paused: bool = False
    code: Literal["failed", "nothing", "restore"] = "failed"  # restore: compacting a resumed session
    manual: bool = False


StreamEvent = (
    Delta
    | Completed
    | ToolProgress
    | ResponseBoundary
    | IterationProgress
    | UsageUpdate
    | RunStopped
    | ApprovalRequest
    | Retrying
    | UserInput
    | CompactStarted
    | CompactFinished
    | CompactFailed
)


class ProviderError(RuntimeError):
    """A safe user-facing error; never includes raw SDK responses or credentials."""

    def __init__(self, *args, code: str | None = None):
        super().__init__(*args)
        # Machine-readable kind, e.g. "context_overflow"; the message stays the user-facing text.
        self.code = code
