"""Project-owned messages and stream events; SDK types stop at the adapters."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ContentBlock:
    kind: Literal["text", "thinking"]
    text: str


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    content: tuple[ContentBlock, ...]
    # Retain protocol-native signed/opaque data for the same adapter only.
    protocol: str | None = None
    native: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    tool_calls: tuple["ToolCall", ...] = ()
    tool_results: tuple["ToolResult", ...] = ()

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if b.kind == "text")


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
class Retrying:
    """A transient failure before any event of this request; nothing was executed."""
    attempt: int
    max_attempts: int
    delay: float
    reason: str


from .permissions import ApprovalRequest

StreamEvent = (Delta | Completed | ToolProgress | ResponseBoundary | IterationProgress | UsageUpdate | RunStopped
               | ApprovalRequest | Retrying | UserInput)


class ProviderError(RuntimeError):
    """A safe user-facing error; never includes raw SDK responses or credentials."""
