import asyncio
from contextlib import aclosing, suppress
from time import monotonic
import sys
import json
import os
from typing import Callable, TypeVar

from rich.text import Text
from rich.table import Table
from rich.console import Group
from rich.padding import Padding
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message as UIMessage
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.markdown import MarkdownFence

from .appearance import (COMMAND_HIGHLIGHT_COLOR, TERMINAL_CSS, USER_MESSAGE_BACKGROUND, USER_MESSAGE_COLOR,
                         TOOL_SUCCESS_COLOR, PARAMETER_COLOR, OPERATION_COLOR, ERROR_COLOR,
                         compact_notice, moss_themes, startup_banner)
from .builtin_commands import BUILTIN, dispatch, open_sessions
from .commands import CommandContext, CommandRegistry, Hint, Parsed, parse_input, unknown_note
from .config import ConfigError, ProviderConfig, Settings
from .conversation import Conversation
from .tools import shell_label
from .conversation import MemoryUpdated, Notice, Resumed
from .memory import SCOPE_LABELS
from .messages import (CompactFailed, CompactFinished, CompactStarted, Completed, Delta, Message, ProviderError,
                       ResponseBoundary, ToolCall, ToolProgress, ToolResult, IterationProgress, Retrying, UsageUpdate,
                       UsageTotals, RunStopped, UserInput)
from .tool_budget import persisted_note
from .scrollback import TranscriptMarkdown, commit_transcript, message_row, redraw_transcript
from .usage import cache_summary
from .permissions import EDIT_TOOLS, ApprovalRequest, CHOICES, MODE_BADGES, safe_display


Result = TypeVar("Result")


class TerminalApp(App[Result]):
    ENABLE_COMMAND_PALETTE = False
    ESCAPE_TO_MINIMIZE = False

    def __init__(self) -> None:
        # Preserve terminal-default colors instead of converting them to RGB.
        super().__init__(ansi_color=True)

    def get_driver_class(self):
        if sys.platform == "win32":
            from .windows_inline import WindowsInlineDriver

            return WindowsInlineDriver
        return super().get_driver_class()

    def on_mount(self) -> None:
        for theme in moss_themes():
            self.register_theme(theme)
        self.theme = "moss-dark"


CHOICE_LABELS = {
    "once": "本次允许 · 仅执行当前调用", "session": "本会话允许 · 同工具与目标，退出后失效",
    "permanent": "永久允许 · 保存到本项目本地配置", "deny": "拒绝 · 让模型调整策略",
    "execute": "批准计划并执行 · 恢复为进入计划前的权限", "accept_edits": "批准并自动接受编辑 · 切换为接受编辑档位",
}


def choice_label(request: ApprovalRequest, choice: str) -> str:
    if request.operation.tool == "exit_plan_mode" and choice == "deny":
        return "继续规划 · 拒绝并让模型修改计划"
    if request.operation.risk and choice == "once":
        return "本次允许 · 高风险命令每次都需确认"
    if choice == "prefix":
        return f"永久允许以 {request.operation.prefix} 开头的命令 · 保存到本项目本地配置"
    if choice == "accept_edits" and request.operation.tool in EDIT_TOOLS:
        return "本会话自动接受编辑 · 切换为接受编辑档位，命令仍需确认"
    if (request.operation.server or request.operation.skill) and choice == "session":
        return "本会话允许该工具 · 任意参数，退出后失效"
    if (request.operation.server or request.operation.skill) and choice == "permanent":
        return "永久允许该工具 · 任意参数，保存到本项目本地配置"
    return CHOICE_LABELS[choice]


def preview_text(preview: str) -> Text:
    """Approval preview; diffs are colored line by line."""
    text = Text()
    diff = any(line.startswith(("@@", "--- a/", "+++ b/")) for line in preview.splitlines()[:3]) \
        or preview.startswith("新建文件 ")
    for index, line in enumerate(preview.splitlines()):
        style = None
        if diff and line.startswith("+") and not line.startswith("+++"):
            style = TOOL_SUCCESS_COLOR
        elif diff and line.startswith("-") and not line.startswith("---"):
            style = ERROR_COLOR
        elif diff and line.startswith(("@@", "---", "+++", "…")):
            style = "dim"
        text.append(("\n" if index else "") + line, style=style)
    return text


# Shift+Tab order; bypassPermissions is entered only explicitly and leaves to default.
MODE_CYCLE = {"default": "acceptEdits", "acceptEdits": "plan", "plan": "default", "bypassPermissions": "default"}
DIFF_ROWS = 20  # Diff lines shown under an edit/write result.
NEW_FILE_ROWS = 8


def diff_rows(diff: str, limit: int = DIFF_ROWS) -> Text:
    """Unified diff as numbered, colored rows; hunks are separated by ⋯."""
    rows: list[tuple[str, str | None]] = []
    old = new = 0
    notes = []
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            continue
        if line.startswith("@@"):
            try:
                ranges = line.split("@@")[1].split()
                old = int(ranges[0][1:].split(",")[0])
                new = int(ranges[1][1:].split(",")[0])
            except (IndexError, ValueError):
                continue
            if rows:
                rows.append(("     ⋯", None))
        elif line.startswith("-"):
            rows.append((f"{old:>5} - {line[1:]}", ERROR_COLOR))
            old += 1
        elif line.startswith("+"):
            rows.append((f"{new:>5} + {line[1:]}", TOOL_SUCCESS_COLOR))
            new += 1
        elif line.startswith(" "):
            rows.append((f"{new:>5}   {line[1:]}", None))
            old, new = old + 1, new + 1
        elif line.strip():
            notes.append(line)  # e.g. the generator's own truncation note
    text = Text()
    for row, style in rows[:limit]:
        text.append("\n    " + row[:160], style=style)
    if len(rows) > limit:
        text.append(f"\n    … 还有 {len(rows) - limit} 行 diff 未显示")
    for note in notes:
        text.append("\n    " + note)
    return text


class PromptArea(TextArea):
    """Multi-line prompt. Enter sends; Shift+Enter, Ctrl+J, Alt+Enter or a trailing \\ insert a newline.

    ↑ on the top row and ↓ on the bottom row browse this session's input history;
    while messages are queued, ↑ on the top row first takes them back into the prompt.
    """

    NEWLINE_KEYS = {"shift+enter", "ctrl+j", "alt+enter"}

    class Submitted(UIMessage):
        def __init__(self, text_area: "PromptArea", value: str) -> None:
            super().__init__()
            self.text_area, self.value = text_area, value

        @property
        def control(self) -> "PromptArea":
            return self.text_area

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, tab_behavior="focus", compact=True, highlight_cursor_line=False, **kwargs)
        self.input_history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        # Set by the app: removes and returns the running task's queued messages.
        self.take_queued: Callable[[], tuple[str, ...]] | None = None

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            row, column = self.cursor_location
            if column and not self.selected_text and self.document.get_line(row)[column - 1] == "\\":
                self.replace("\n", (row, column - 1), (row, column))
                return
            self.post_message(self.Submitted(self, self.text))
            return
        await super()._on_key(event)

    @property
    def recalled(self) -> bool:
        """The text is an unedited history entry, so ↑ / ↓ keep browsing instead of opening command hints."""
        return self._history_index is not None and self.text == self.input_history[self._history_index]

    def remember(self, text: str) -> None:
        if text.strip() and (not self.input_history or self.input_history[-1] != text):
            self.input_history.append(text)
        self._history_index = None
        self._draft = ""

    def restore_queued(self, queued: tuple[str, ...]) -> None:
        """Put queued messages back in front of any draft."""
        self._history_index = None
        self.value = "\n\n".join((*queued, self.text) if self.text.strip() else queued)

    def action_cursor_up(self, select: bool = False) -> None:
        location = self.cursor_location
        on_top = self.navigator.is_first_document_line(location) and self.navigator.is_first_wrapped_line(location)
        if not select and on_top and self.take_queued is not None:
            queued = self.take_queued()
            if queued:
                # Queued messages come back for editing first; the running task continues.
                self.restore_queued(queued)
                return
        if not select and self.input_history and on_top:
            self._browse(-1)
            return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        location = self.cursor_location
        if (not select and self._history_index is not None and self.navigator.is_last_document_line(location)
                and self.navigator.is_last_wrapped_line(location)):
            self._browse(1)
            return
        super().action_cursor_down(select)

    def _browse(self, step: int) -> None:
        if self._history_index is None:
            if step > 0:
                return
            self._draft = self.text
            index = len(self.input_history) - 1
        else:
            index = self._history_index + step
        if index < 0:
            return
        if index >= len(self.input_history):
            # Past the newest entry: back to the unsent draft.
            self._history_index = None
            self.value = self._draft
            return
        self._history_index = index
        self.value = self.input_history[index]


class PlanEntry(Static):
    """The submitted plan, committed to scrollback before approval so it can be read in full."""

    def __init__(self, plan: str):
        super().__init__(classes="entry plan-entry")
        self.transcript = Group(Text("计划", style=f"bold {OPERATION_COLOR}"), message_row("│", TranscriptMarkdown(plan)))
        self.update(self.transcript)


CANCELLED_COMPACT = "└ 已取消压缩，对话历史未改变。"


class CompactRow(Static):
    """A running compaction, shown in the conversation and turned into its result line in place."""

    def __init__(self, manual: bool):
        super().__init__(classes="local-note")
        self.label = "正在压缩上下文" if manual else "上下文接近上限，正在压缩"
        self.started = monotonic()
        self.transcript: Text | None = None  # Set once finished; scrollback waits until then.
        self.tick()

    def tick(self) -> None:
        if self.transcript is None:
            self.update(Text(f"✻ {self.label} · {monotonic() - self.started:.1f}s", style=OPERATION_COLOR))

    def finish(self, text: str, style: str | None = None) -> None:
        self.transcript = Text(text, style=style or "")
        self.update(self.transcript)


class KeyboardOptions(OptionList):
    async def _on_click(self, event: events.Click) -> None:
        # Choices require Enter. Mouse clicks may focus the list but cannot execute an action.
        event.prevent_default()
        event.stop()


class SessionInfo(Static):
    class Resized(UIMessage):
        pass

    def on_resize(self) -> None:
        self.post_message(self.Resized())


class ModelPanel(Vertical):
    """The choice list under the prompt, shared by /model and /resume."""

    def compose(self) -> ComposeResult:
        yield Static("切换模型 · 对话历史保留", id="model-title", markup=False)
        yield KeyboardOptions(id="models")
        yield Static("↑ / ↓ 选择 · Enter 确认 · Esc 收起", id="model-help")


class PlainCodeFence(MarkdownFence):
    @classmethod
    def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
        return Content(code)


class DialogueMarkdown(Markdown):
    BLOCKS = {**Markdown.BLOCKS, "fence": PlainCodeFence, "code_block": PlainCodeFence}


class ProviderSelectApp(TerminalApp[ProviderConfig]):
    TITLE = "MossCode"
    SUB_TITLE = "选择本次会话的提供商与模型"
    CSS = TERMINAL_CSS + """
    Screen { background: $background; padding: 1 2; }
    Screen:inline { height: auto; border: none; }
    #selection { height: auto; max-height: 50vh; border: none; background: $background; }
    #selection-help { margin: 1 0; height: auto; color: $text-muted; }
    #banner { height: auto; margin-bottom: 1; }
    """
    BINDINGS = [
        Binding("escape", "quit", "退出", priority=True),
        Binding("ctrl+c", "quit", "退出", priority=True),
        Binding("ctrl+q", "quit", "退出", priority=True),
    ]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        yield Static(startup_banner("选择提供商"), id="banner")
        yield Static("↑ / ↓ 选择，Enter 确认；本次会话使用选定的配置。", id="selection-help")
        yield KeyboardOptions(
            *(Text(item.name) for item in self.settings.providers),
            id="selection",
        )

    def on_mount(self) -> None:
        super().on_mount()
        options = self.query_one("#selection", OptionList)
        options.highlighted = next(
            (index for index, item in enumerate(self.settings.providers)
             if item.name == self.settings.default_provider),
            0,
        )
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.exit(self.settings.providers[event.option_index])


class ChatEntry(Vertical):
    def __init__(self, speaker: str, text: str = "") -> None:
        super().__init__(classes="entry user-entry" if speaker == "You" else "entry assistant-entry")
        self.speaker = speaker
        self.body = text
        self.thinking = ""
        self.started = monotonic()
        self.phase = "waiting"
        self.thinking_started: float | None = None
        self.thinking_elapsed = 0.0
        self.completed_elapsed: float | None = None
        self.interruption_note = ""
        self.tool_request = False

    def compose(self) -> ComposeResult:
        if self.speaker == "You":
            with Horizontal(classes="message-row"):
                yield Static("❯", classes="message-marker")
                yield Static(self.body, classes="user-message", markup=False)
            return
        for classes in ("thinking-label", "thinking"):
            widget = Static("", classes=classes, markup=False)
            widget.display = False
            yield widget
        with Horizontal(classes="message-row"):
            yield Static("●", classes="message-marker")
            with Vertical(classes="response-content"):
                yield Static(self.body or "等待回复…", classes="raw", markup=False)
                answer = DialogueMarkdown("", classes="answer")
                answer.display = False
                yield answer
        summary = Static("", classes="turn-summary", markup=False)
        summary.display = False
        yield summary

    def refresh_timer(self) -> None:
        if self.thinking_started is not None:
            elapsed = self.thinking_elapsed + monotonic() - self.thinking_started
            self.query_one(".thinking-label", Static).update(f"思考中 · {elapsed:.1f}s")

    def _finish_thinking(self) -> None:
        if self.thinking_started is not None:
            self.thinking_elapsed += monotonic() - self.thinking_started
            self.thinking_started = None
            self.query_one(".thinking-label", Static).update(f"思考用时 {self.thinking_elapsed:.1f}s")
        self.query_one(".thinking", Static).display = False

    def append(self, event: Delta) -> None:
        if not event.text:
            return
        if event.kind == "thinking":
            self.phase = "thinking"
            if self.thinking_started is None:
                self.thinking_started = monotonic()
            self.thinking += event.text
            self.query_one(".thinking-label", Static).display = True
            self.refresh_timer()
            widget = self.query_one(".thinking", Static)
            widget.display = True
            widget.update(self.thinking)
            self.query_one(".message-row").display = bool(self.body)
        else:
            self._finish_thinking()
            self.phase = "streaming"
            self.body += event.text
            self.query_one(".message-row").display = True
            self.query_one(".raw", Static).update(self.body)

    async def finish(self, message: Message) -> None:
        self._finish_thinking()
        self.phase = "complete"
        self.body = message.text
        markdown = self.query_one(".answer", Markdown)
        await markdown.update(self.body or "（服务返回了空正文）")
        markdown.display = True
        self.query_one(".message-row").display = True
        self.query_one(".raw", Static).display = False
        summary = self.query_one(".turn-summary", Static)
        self.completed_elapsed = monotonic() - self.started
        summary.update(f"✻ 完成 · {self.completed_elapsed:.1f}s")
        summary.display = True

    async def seal_tool_request(self, message: Message) -> None:
        self._finish_thinking()
        self.phase = "tools"
        self.tool_request = True
        self.body = message.text
        self.query_one(".message-row").display = bool(self.body)
        if not self.body and not self.thinking:
            self.display = False
        if self.body:
            markdown = self.query_one(".answer", Markdown)
            await markdown.update(self.body)
            markdown.display = True
            self.query_one(".raw", Static).display = False

    def interrupted(self, note: str) -> None:
        self.interruption_note = note
        self.display = True
        self._finish_thinking()
        self.phase = "interrupted"
        self.query_one(".message-row").display = True
        raw = self.query_one(".raw", Static)
        raw.update((self.body + "\n\n" if self.body else "") + note)
        raw.display = True
        self.query_one(".answer").display = False

    def transcript(self):
        if self.speaker == "You":
            return Padding(message_row("❯", Text(self.body)), 0, expand=True,
                           style=f"{USER_MESSAGE_COLOR} on {USER_MESSAGE_BACKGROUND}")
        if not self.display:
            return None
        parts = []
        if self.thinking_elapsed:
            parts.append(Text(f"思考用时 {self.thinking_elapsed:.1f}s"))
        if self.interruption_note:
            parts.append(message_row("●", Text((self.body + "\n\n" if self.body else "") + self.interruption_note)))
        elif self.body or not self.tool_request:
            # A response that only calls tools has no text to show, as on screen before it was committed.
            parts.append(message_row("●", TranscriptMarkdown(self.body or "（服务返回了空正文）")))
        if self.completed_elapsed is not None:
            parts.append(Text(f"\n✻ 完成 · {self.completed_elapsed:.1f}s"))
        return Group(*parts)


def plan_text(arguments: str) -> str:
    try:
        plan = json.loads(arguments).get("plan")
    except (ValueError, AttributeError):
        return ""
    return plan if isinstance(plan, str) else ""


def shell_name(label: str | None) -> str:
    if label is None:
        return "PowerShell" if os.name == "nt" else "Shell"
    return "PowerShell" if label == "PowerShell" else "Bash"


def describe_call(call: ToolCall, shell: str | None = None) -> tuple[str, str, dict]:
    """(display name, main argument, parsed arguments) for one tool line."""
    names = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "memory": "Memory",
             "execute_command": shell_name(shell), "glob": "Glob", "grep": "Grep", "exit_plan_mode": "Plan"}
    arguments: dict = {}
    try:
        args = json.loads(call.arguments)
        if isinstance(args, dict):
            arguments = args
        if call.name.startswith(("mcp__", "skill__")):
            detail = json.dumps(args, ensure_ascii=False, sort_keys=True)
        elif call.name == "memory":
            detail = f"{args.get('action', '')} {args.get('name', '')}".strip()
        else:
            detail = args.get("pattern", "") if call.name in {"glob", "grep"} else args.get("command") or args.get("path") or ""
            if call.name == "exit_plan_mode":
                detail = "提交计划"
    except (ValueError, AttributeError):
        detail = "参数无效"
    detail = " ".join(str(detail).splitlines())
    return names.get(call.name, call.name), detail[:100], arguments


RESUMED_MESSAGES = 20


def resumed_rows(history, shell: str | None = None, limit: int = RESUMED_MESSAGES) -> list:
    """R14: the latest messages in brief, for the scrollback after a resume."""
    shown = [m for m in history if m.role != "tool"][-limit:]
    rows = []
    for message in shown:
        if message.synthetic == "compact":
            rows.append(Text("（较早的对话已压缩）"))
        elif message.role == "user":
            rows.append(Padding(message_row("❯", Text(message.shown)), 0, expand=True,
                                style=f"{USER_MESSAGE_COLOR} on {USER_MESSAGE_BACKGROUND}"))
        else:
            parts = [message_row("●", TranscriptMarkdown(message.text))] if message.text.strip() else []
            for call in message.tool_calls:
                name, detail, _ = describe_call(call, shell)
                line = Text("  ● ")
                line.append(name, style="bold")
                line.append("(")
                line.append(detail, style=PARAMETER_COLOR)
                line.append(")")
                parts.append(line)
            if parts:
                rows.append(Group(*parts))
    return rows


def memory_line(details: dict) -> Text:
    """W5: what the memory tool did, in place of the usual result summary."""
    text = Text("✎ ", style=TOOL_SUCCESS_COLOR)
    name = details.get("name", "")
    if details.get("action") == "delete":
        text.append(f"已删除笔记：{name}")
    else:
        text.append(f"已记住：{name}（{SCOPE_LABELS.get(details.get('scope'), '')}）")
    return text


class ToolEntry(Static):
    """Semantic foreground accents on one compact tool line; no extra panel."""

    def __init__(self, event: ToolProgress, shell: str | None = None):
        super().__init__(classes="tool-entry", markup=False)
        self.started = monotonic()
        self.elapsed: float | None = None
        call = event.call
        self.operation = call.name
        self.operation_name, self.detail, self.arguments = describe_call(call, shell)
        self.label = f"● {self.operation_name}({self.detail})"
        self.transcript = self._heading("running") + Text("\n  └ 执行中…", style=OPERATION_COLOR)
        self.update(self.transcript)

    def _heading(self, state: str) -> Text:
        marker, color = {
            "running": ("●", OPERATION_COLOR), "success": ("✓", TOOL_SUCCESS_COLOR),
            "error": ("×", ERROR_COLOR), "stopped": ("!", OPERATION_COLOR),
        }[state]
        if state in {"error", "stopped"}:
            action_color = color
        else:
            action_color = OPERATION_COLOR if self.operation in {"write_file", "edit_file", "execute_command"} or self.operation.startswith(("mcp__", "skill__")) else TOOL_SUCCESS_COLOR
        text = Text()
        text.append(marker + " ", style=color)
        text.append(self.operation_name, style=f"bold {action_color}")
        text.append("(")
        text.append(self.detail, style=PARAMETER_COLOR)
        text.append(")")
        return text

    def _changes(self, result: ToolResult) -> Text | None:
        """Numbered diff for a successful edit or overwrite; the first lines of a new file."""
        if result.is_error or self.operation not in EDIT_TOOLS:
            return None
        if result.details.get("diff"):
            return diff_rows(result.details["diff"])
        content = self.arguments.get("content")
        if self.operation == "write_file" and result.details.get("created") and isinstance(content, str):
            lines = content.splitlines()
            text = Text()
            for number, line in enumerate(lines[:NEW_FILE_ROWS], 1):
                text.append(f"\n    {number:>5} + {line[:160]}", style=TOOL_SUCCESS_COLOR)
            if len(lines) > NEW_FILE_ROWS:
                text.append(f"\n    … 还有 {len(lines) - NEW_FILE_ROWS} 行未显示")
            return text
        return None

    def complete(self, result: ToolResult, elapsed: float | None = None):
        lines = result.output.splitlines() or ["完成，无输出。"]
        shown = lines[:3]
        if elapsed is not None or self.elapsed is None:
            self.elapsed = elapsed if elapsed is not None else monotonic() - self.started
        stopped = result.code in {"cancelled", "not_executed", "plan_blocked"}
        state = "stopped" if stopped else "error" if result.is_error else "success"
        if self.operation == "memory" and not result.is_error:
            text = memory_line(result.details)
            text.append(f" · {self.elapsed:.1f}s", style=PARAMETER_COLOR)
            self.transcript = text
            self.update(text)
            return
        text = self._heading(state)
        text.append(f" · {self.elapsed:.1f}s\n  └ ")
        if result.is_error:
            label = "已取消" if result.code == "cancelled" else "未执行" if stopped else "失败"
            text.append(label + " · ", style=f"bold {OPERATION_COLOR if stopped else ERROR_COLOR}")
        text.append(shown[0][:160])
        changes = self._changes(result)
        if changes is not None:
            text.append_text(changes)
        else:
            for line in shown[1:]:
                text.append("\n    " + line[:160])
            if len(lines) > 3 or any(len(line) > 160 for line in lines[:3]) or result.details.get("truncated"):
                text.append("\n    （仅显示结果摘要）")
        stored = persisted_note(result)
        if stored:
            text.append("\n    " + stored, style=PARAMETER_COLOR)
        self.transcript = text
        self.update(text)


class MossCodeApp(TerminalApp[None]):
    TITLE = "MossCode"
    SUB_TITLE = "第四章 · Agent Loop"
    CSS = TERMINAL_CSS + """
    Screen { background: $background; color: $foreground; padding: 1 2 0 2; overflow: hidden hidden; }
    Screen:inline { height: auto; border: none; }
    #banner { height: auto; margin-bottom: 1; }
    #chat { height: auto; max-height: 100vh; scrollbar-size: 1 0; }
    .entry { height: auto; padding: 0; margin-bottom: 1; background: $background; }
    .message-row { height: auto; }
    .user-entry .message-row, .user-entry .message-row > Static {
        background: USER_MESSAGE_BACKGROUND !important; color: USER_MESSAGE_COLOR !important;
    }
    .message-marker { width: 2; height: 1; text-style: bold; }
    .user-message, .response-content { width: 1fr; height: auto; }
    .raw, .thinking, .answer { height: auto; padding: 0; margin: 0; background: $background; }
    .thinking-label, .thinking { height: auto; padding: 0 0 0 2; margin-bottom: 1; }
    .turn-summary { height: auto; margin-top: 1; }
    .answer MarkdownHeader { content-align: left middle; margin: 1 0; }
    .answer > MarkdownBlock:last-of-type { margin-bottom: 0; }
    .answer MarkdownBlock > .code_inline { color: PARAMETER_COLOR !important; text-style: bold; }
    #status { height: auto; margin-top: 0; color: $text-muted; }
    #queue { height: auto; margin-top: 1; padding: 0 1; }
    #input-row { height: auto; margin-top: 1; padding: 0 1; border-top: solid $foreground 35%; border-bottom: solid $foreground 35%; }
    #input-prefix { width: 2; height: 1; color: $primary; }
    #prompt { width: 1fr; height: auto; max-height: 8; border: none; padding: 0; background: $background; }
    #prompt:focus { border: none; background-tint: transparent; }
    #command-hints { height: auto; max-height: 7; border: none; padding: 0 1; }
    #command-hints > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #command-help { height: 1; color: $text-muted; padding: 0 1; }
    #model-panel { width: 1fr; height: auto; padding: 0 1; border-top: solid $foreground; border-bottom: solid $foreground; }
    #model-title, #model-help { height: 1; }
    #models { height: auto; max-height: 8; border: none; padding: 0; }
    #models > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #approval-panel { height: auto; max-height: 15; padding: 0 1; border-top: solid $foreground; }
    #approval-detail { height: auto; max-height: 20; }
    #approval-options { height: 4; border: none; padding: 0; }
    #approval-options > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #approval-help { height: auto; }
    .local-note { height: auto; margin: 0 0 1 2; color: $text-muted; }
    .resumed-entry { height: auto; margin-bottom: 1; }
    .tool-entry { height: auto; margin-bottom: 1; }
    OptionList { background: $background; }
    """.replace("COMMAND_HIGHLIGHT_COLOR", COMMAND_HIGHLIGHT_COLOR).replace("PARAMETER_COLOR", PARAMETER_COLOR) \
       .replace("USER_MESSAGE_BACKGROUND", USER_MESSAGE_BACKGROUND).replace("USER_MESSAGE_COLOR", USER_MESSAGE_COLOR)
    BINDINGS = [
        Binding("escape", "cancel", "取消生成", priority=True, show=False),
        Binding("ctrl+c", "cancel_task", "取消任务", priority=True, show=False),
        Binding("ctrl+q", "quit", "退出", priority=True, show=False),
        Binding("up", "move_hint(-1)", priority=True, show=False),
        Binding("down", "move_hint(1)", priority=True, show=False),
        Binding("tab", "complete_hint", priority=True, show=False),
        Binding("shift+tab", "cycle_mode", priority=True, show=False),
        Binding("enter", "complete_hint(False)", priority=True, show=False),
    ]

    def __init__(self, conversation: Conversation, provider_name: str, *, settings: Settings | None = None,
                 model: str = "", provider_factory=None, resume: str | None = None,
                 commands: CommandRegistry | None = None) -> None:
        super().__init__()
        self.conversation = conversation
        # --resume ID / --continue restore at startup; "" opens the session list once started.
        self._resume = resume
        self._panel_rows = 0
        self._pick = None  # The open picker's on_pick (CommandUI.choose).
        # A table of this run's own: skill commands are added here and replaced by /skills reload.
        self.commands = commands if commands is not None else BUILTIN.with_skills(conversation.skills)[0]
        self.provider_name = provider_name
        self.settings, self.model = settings, model
        if provider_factory is None:
            from .providers import create_provider as provider_factory
        self.provider_factory = provider_factory
        self._reply_task: asyncio.Task[None] | None = None
        self._generating = False
        self._active_reply: ChatEntry | None = None
        self._closing = False
        self.started = 0.0
        self._iteration = 0
        self._iteration_limit = 25
        self._usage = conversation.total_usage
        self._status_note = ""
        self._mcp_notices: list[str] = []  # MCP servers that settled while a task was running.
        self._retry_notice = ""  # Held until the next event so the ticker does not hide it.
        self._hint_until = 0.0  # Short notes shown over the running status until this time.
        self._hint_choices: list[Hint] = []
        self._dismissed_prompt: str | None = None
        self._scrollback_lock = asyncio.Lock()
        self._history: list = []  # Everything committed to scrollback, re-rendered after a width change.
        self._width: int | None = None
        self._redraw_timer = None
        self._approval_request: ApprovalRequest | None = None
        self._startup_task = None
        self._compacting: float | None = None  # Start time while a compaction runs.
        self._compact_row: CompactRow | None = None
        self._context_share: tuple[int, bool] | None = None  # (percent of the effective window, near threshold)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat"):
            yield SessionInfo(startup_banner(self.provider_name), id="banner")
        queue = Static("", id="queue", markup=False)
        queue.display = False
        yield queue
        with Horizontal(id="input-row"):
            yield Static("❯", id="input-prefix")
            prompt = PromptArea(placeholder="输入消息，或 / 查看命令；Shift+Enter 换行", id="prompt")
            prompt.take_queued = self._take_back_queued
            yield prompt
        panel = ModelPanel(id="model-panel")
        panel.display = False
        yield panel
        with Vertical(id="approval-panel") as approval:
            approval.display = False
            yield Static("", id="approval-detail", markup=False)
            yield KeyboardOptions(*(CHOICE_LABELS[choice] for choice in CHOICES), id="approval-options")
            yield Static("↑ / ↓ 选择 · Enter 确认 · Esc 拒绝本次 · Ctrl+C 取消任务", id="approval-help", markup=False)
        hints = KeyboardOptions(id="command-hints")
        hints.can_focus = False
        hints.display = False
        yield hints
        help_line = Static("", id="command-help", markup=False)
        help_line.display = False
        yield help_line
        yield SessionInfo(Text(""), id="status")

    def on_mount(self) -> None:
        super().on_mount()
        if self.conversation.registry:
            self.conversation.registry.permissions.interactive = True
        self.conversation.on_memory = self._on_memory
        self.query_one("#prompt", PromptArea).focus()
        self._width = self.size.width
        self._status("")
        self.set_interval(0.2, self._tick)
        self.call_later(self._refresh_layout)
        self.call_after_refresh(self._commit_ready_transcript)
        # The prompt stays usable: a first task waits only for project context, and MCP
        # servers join in the background as each one becomes ready.
        if self._resume:
            # Restoring loads the project context itself; MCP and the other notices follow.
            self._start_resume(self._resume, startup=True)
        elif self.conversation.mcp_manager is not None or self.conversation.context_root is not None:
            self._startup_task = asyncio.create_task(self._initialize())
        elif self._resume == "":
            self.call_later(open_sessions, self._command_context())

    async def _initialize(self):
        try:
            for notice in await self.conversation.start(wait_mcp=False, on_mcp=self._on_mcp_update):
                await self._local_note("└ " + notice)
            await self._commit_ready_transcript()
        except Exception:
            await self._local_note("└ 启动初始化失败；请检查配置后重新启动。")
        finally:
            if not self._closing and not self._generating:
                self._status("")
        if self._resume == "" and not self._closing:
            self._resume = None
            await open_sessions(self._command_context())

    def _on_mcp_update(self, status) -> None:
        if self._closing:
            return
        if status.state == "ready":
            pass  # Like Claude Code: only problems are announced; /mcp lists connected servers.
        elif self._task_running:
            # Keep the running reply contiguous; report once the task ends.
            self._mcp_notices.append(status.notice())
        else:
            self.call_later(self._local_note, "└ " + status.notice())
        if not self._generating:
            self._status(self._status_note)

    def _on_memory(self, event) -> None:
        """From the background extractor; a running task's output stays contiguous, so wait for it."""
        if self._closing:
            return
        if isinstance(event, MemoryUpdated):
            text, style = "✎ 已更新记忆：" + "、".join(event.names), PARAMETER_COLOR
        else:
            text, style = "! " + event.text, OPERATION_COLOR
        if self._task_running:
            self._reply_task.add_done_callback(lambda _: self._closing or self.call_later(self._local_note, text, style))
        else:
            self.call_later(self._local_note, text, style)
        self._usage = self.conversation.total_usage  # A10: extraction requests are a real cost.
        if not self._generating:
            self._status(self._status_note)

    def on_resize(self, event: events.Resize) -> None:
        self.call_later(self._refresh_layout)
        width, self._width = self._width, event.size.width
        if self.is_inline and width is not None and width != event.size.width:
            # A drag sends many resizes; repaint once the width settles.
            if self._redraw_timer is not None:
                self._redraw_timer.stop()
            self._redraw_timer = self.set_timer(0.2, self._redraw_after_resize)

    async def _redraw_after_resize(self) -> None:
        self._redraw_timer = None
        if self._closing:
            return
        async with self._scrollback_lock:
            with self.batch_update():
                redraw_transcript(self, self._history)

    def on_session_info_resized(self) -> None:
        self._fit_chat()

    def _refresh_layout(self) -> None:
        if self.query("#banner"):
            self.query_one("#banner", Static).update(startup_banner(self.provider_name))
        self._status(self._status_note)
        self._fit_chat()
        if self._hint_choices:
            self._refresh_hints(self.query_one("#prompt", PromptArea).value)

    def _fit_chat(self) -> None:
        if not self.query("#chat") or not self.query("#status"):
            return
        # Reserve the actual controls, rather than a percentage or spare rows.
        input_row = self.query_one("#input-row")
        # The prompt grows with its text; its region may still hold the previous height.
        prompt_rows = min(8, max(1, self.query_one("#prompt", PromptArea).wrapped_document.height))
        reserved = (self.screen.styles.gutter.height
                    + max(2 + prompt_rows, input_row.region.height) + input_row.styles.margin.height
                    + max(1, self.query_one("#status").outer_size.height))
        queue = self.query_one("#queue")
        if queue.display:
            reserved += 1 + min(4, len(self.conversation.queued))
        if self.query_one("#model-panel").display:
            reserved += 4 + min(8, self._panel_rows)
        if self.query_one("#approval-panel").display:
            # Keep the choices and status reachable in a short terminal; only
            # the preview shrinks. Completed history already lives in scrollback.
            # Border, one row per choice and up to two rows of help.
            fixed = 3 + (len(self._approval_request.choices) if self._approval_request else 4)
            limit = max(fixed, self.size.height - reserved - 1)
            self.query_one("#approval-panel").styles.max_height = limit
            self.query_one("#approval-detail").styles.max_height = min(20, max(1, limit - fixed))
            # Its current size may predate the new preview; reserve what it may grow to.
            reserved += limit
        hints = self.query_one("#command-hints")
        hint_limit = min(8, max(1, self.size.height - reserved - 2))
        if hints.styles.max_height is None or hints.styles.max_height.value != hint_limit:
            hints.styles.max_height = hint_limit
        # Reserve hints before layout: their old size may still be zero on show.
        hint_rows = min(len(self._hint_choices), hint_limit)
        hints.styles.height = max(1, hint_rows)
        if hints.display:
            reserved += hint_rows + 1
        chat = self.query_one("#chat")
        available = max(1, self.size.height - reserved)
        if chat.styles.max_height is None or chat.styles.max_height.value != available:
            chat.styles.max_height = available

    async def _commit_ready_transcript(self) -> None:
        if not self.is_inline or self._closing:
            return
        async with self._scrollback_lock:
            chat = self.query_one("#chat", VerticalScroll)
            ready, renderables = [], []
            for widget in list(chat.children):
                if isinstance(widget, ChatEntry):
                    if widget.speaker != "You" and widget.phase not in {"complete", "tools", "interrupted"}:
                        break
                    rendered = widget.transcript()
                elif isinstance(widget, ToolEntry):
                    if widget.elapsed is None:
                        break
                    rendered = widget.transcript
                elif isinstance(widget, CompactRow):
                    if widget.transcript is None:
                        break
                    rendered = widget.transcript
                elif widget.id == "banner":
                    rendered = startup_banner(self.provider_name)
                elif hasattr(widget, "transcript"):
                    rendered = widget.transcript
                else:
                    break
                ready.append(widget)
                if rendered is not None:
                    renderables.append(rendered)
            if ready:
                with self.batch_update():
                    if renderables:
                        commit_transcript(self, renderables)
                        self._history.extend(renderables)
                    await chat.remove_children(ready)
                    self._fit_chat()

    def _status(self, note: str) -> None:
        self._status_note = note
        text = Text()
        registry = self.conversation.registry
        if registry is None:
            text.append("未启用工具", style="bold")
        else:
            mode = registry.permissions.mode
            color = {"acceptEdits": TOOL_SUCCESS_COLOR, "plan": OPERATION_COLOR, "bypassPermissions": ERROR_COLOR}.get(mode)
            text.append(MODE_BADGES[mode], style=f"bold {color}" if color else "bold")
            if not self._generating and self.size.width >= 80:
                text.append(" (Shift+Tab 切换)")
        if self.model:
            text.append(" · " + self.model)
        if note:
            stage, separator, rest = note.partition(" · ")
            stage_color = ERROR_COLOR if stage.startswith("失败") else OPERATION_COLOR if self._generating or stage.startswith(("已取消", "已停止")) else None
            text.append(" · ")
            text.append(stage, style=stage_color)
            if separator:
                text.append(separator + rest)
        manager = self.conversation.mcp_manager
        if manager is not None and manager.pending:
            total = len(manager.connections)
            text.append(f" · MCP 连接中 {total - manager.pending}/{total}", style=OPERATION_COLOR)
        queued = len(self.conversation.queued)
        if queued:
            text.append(f" · 已排队 {queued} 条", style=OPERATION_COLOR)
        if self._usage.requests:
            def tokens(value, missing):
                if missing == self._usage.requests:
                    return "—"
                return ("≥" if missing else "") + str(value)
            if self._context_share is not None:
                percent, near = self._context_share
                text.append(f" · 上下文 约 {percent}%", style=OPERATION_COLOR if near else None)
            text.append(f" · Token 入 {tokens(self._usage.input_tokens, self._usage.missing_input)} / 出 {tokens(self._usage.output_tokens, self._usage.missing_output)}")
            text.append(" · " + cache_summary(self._usage))
        status = self.query("#status")
        if not status:  # Timers and MCP callbacks can fire while the screen is torn down.
            return
        status.first(Static).update(text)
        self.call_later(self._fit_chat)

    def _refresh_context(self) -> None:
        """Cached: the ticker repaints every 0.2s, the estimate changes only with the history."""
        estimate = self.conversation.context_estimate() if self._usage.requests else None
        if estimate is None:
            self._context_share = None
            return
        budget = self.conversation.budget
        self._context_share = (round(100 * estimate / budget.effective), estimate >= 0.8 * budget.auto_threshold)

    def _tick(self) -> None:
        if self._generating and self._compacting is not None:
            if self._compact_row is not None:
                self._compact_row.tick()
            self._status(f"压缩上下文中 · {monotonic() - self._compacting:.1f}s · Esc 取消")
            return
        if self._generating and self._active_reply is not None:
            if self._approval_request is not None:
                self._status("等待授权 · Esc 拒绝本次 · Ctrl+C 取消任务")
                return
            if self._retry_notice:
                self._status(self._retry_notice)
                return
            self._active_reply.refresh_timer()
            if monotonic() < self._hint_until:
                return
            label = {"waiting": "连接中", "thinking": "思考中", "streaming": "输出中", "tools": "执行工具中"}.get(self._active_reply.phase, "处理中")
            self._status(f"{label} · {self._iteration}/{self._iteration_limit} 轮 · {monotonic() - self.started:.1f}s · Esc 取消")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "prompt":
            value = event.text_area.text
            # Esc suppresses this edit only, not a matching command typed later.
            if value != self._dismissed_prompt:
                self._dismissed_prompt = None
            self._refresh_hints(value)

    def _take_back_queued(self) -> tuple[str, ...]:
        """↑ in the prompt: pull queued messages back for editing without interrupting the task."""
        if not self._task_running:
            return ()
        queued = self.conversation.take_queued()
        if queued:
            self._refresh_queue()
        return queued

    def _refresh_queue(self) -> None:
        queue = self.query_one("#queue", Static)
        queued = self.conversation.queued
        queue.display = bool(queued)
        if queued:
            text = Text()
            for index, item in enumerate(queued[:4]):
                first = item.strip().splitlines()[0] if item.strip() else ""
                more = "…" if len(first) > 80 or "\n" in item.strip() else ""
                text.append(("\n" if index else "") + "↳ 排队：" + first[:80] + more, style=OPERATION_COLOR)
            if len(queued) > 4:
                text.append(f"\n↳ 另有 {len(queued) - 4} 条", style=OPERATION_COLOR)
            text.append("\n  将在本轮工具执行后发送给模型 · ↑ 取回编辑 · Esc 取消任务时放回输入框")
            queue.update(text)
        self._status(self._status_note)

    def _refresh_hints(self, value: str) -> None:
        hints = self.query_one("#command-hints", OptionList)
        help_line = self.query_one("#command-help", Static)
        previous = (self._hint_choices[hints.highlighted][0]
                    if hints.highlighted is not None and hints.highlighted < len(self._hint_choices) else None)
        recalled = self.query_one("#prompt", PromptArea).recalled
        self._hint_choices = ([] if recalled or value == self._dismissed_prompt
                              else self.commands.matches(self._command_context(), value))
        hints.clear_options()
        width = min(24, max(10, (self.size.width - 8) // 2))
        for hint in self._hint_choices:
            row = Table.grid(padding=(0, 2), expand=True)
            row.add_column(width=width, no_wrap=True, overflow="ellipsis")
            row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            row.add_row(Text(hint.label or hint.value), Text(hint.description))
            hints.add_option(row)
        hints.display = bool(self._hint_choices)
        help_line.display = hints.display
        if self._hint_choices:
            hints.highlighted = next((i for i, choice in enumerate(self._hint_choices) if choice[0] == previous), 0)
            help_line.update("↑ / ↓ 选择 · Tab 补全 · Enter 确认 · Esc 收起")
        self.call_later(self._fit_chat)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"move_hint", "complete_hint"}:
            field = self.focused
            if not isinstance(field, PromptArea) or field.id != "prompt" or field.disabled:
                return False
            if not self._hint_choices:
                # C3: after Esc closed the menu, Tab still completes a unique command prefix.
                return action == "complete_hint" and parameters != (False,) and self._unique_hint() is not None
            if action == "complete_hint" and parameters == (False,):
                hints = self.query_one("#command-hints", OptionList)
                return hints.highlighted is not None and self._hint_choices[hints.highlighted][0] != field.value.strip()
        return super().check_action(action, parameters)

    def action_move_hint(self, direction: int) -> None:
        hints = self.query_one("#command-hints", OptionList)
        if direction < 0:
            hints.action_cursor_up()
        else:
            hints.action_cursor_down()

    def _unique_hint(self) -> Hint | None:
        value = self.query_one("#prompt", PromptArea).value
        if value != self._dismissed_prompt or " " in value.strip():
            return None
        matches = self.commands.matches(self._command_context(), value)
        return matches[0] if len(matches) == 1 and matches[0].value != value.strip() else None

    def action_complete_hint(self, tab: bool = True) -> None:
        hints = self.query_one("#command-hints", OptionList)
        if not self._hint_choices:
            unique = self._unique_hint() if tab else None
            if unique is None:
                return
            completion = unique.value
        elif hints.highlighted is None:
            return
        else:
            completion = self._hint_choices[hints.highlighted].value
        # C4: Tab continues into the argument candidates of commands that have them.
        if tab and self.commands.takes_completion(self._command_context(), completion):
            completion += " "
        self._dismissed_prompt = None if completion.endswith(" ") else completion
        field = self.query_one("#prompt", PromptArea)
        field.value = completion
        self._refresh_hints(completion)

    @property
    def _task_running(self) -> bool:
        return self._reply_task is not None and not self._reply_task.done()

    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        prompt = event.value
        field = event.text_area
        parsed = parse_input(prompt, self.commands)
        if parsed.kind == "empty":
            return
        if self._task_running:
            if parsed.kind != "message":
                # Commands change state the running task depends on; keep the text for later.
                self._hint_until = monotonic() + 2
                self._status("运行中只能排队消息，命令请在完成后发送 · Esc 取消")
                return
            field.remember(prompt)
            field.clear()
            self.conversation.queue(parsed.text)
            self._refresh_queue()
            return
        field.remember(prompt)
        field.clear()
        self._dismissed_prompt = None
        self._refresh_hints("")
        if parsed.kind != "message":
            await self._command(prompt.strip(), parsed)
            return
        await self._start_request(parsed.text)

    async def _start_request(self, prompt: str, *, show_user: bool = True, shown: tuple[str, ...] = (),
                             command: str | None = None, fork=None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        reply = ChatEntry("MossCode")
        self._active_reply = reply
        if show_user:
            for text in shown or (prompt,):
                await chat.mount(ChatEntry("You", text))
        await chat.mount(reply)
        chat.anchor()
        self.started = monotonic()
        self._iteration = 0
        self._generating = True
        self._status("连接中…")
        await self._commit_ready_transcript()
        self._reply_task = asyncio.create_task(self._respond(prompt, reply, command, fork))

    @staticmethod
    def _note(text: str, style: str | None = None) -> Static:
        note = Static(Text(text, style=style or ""), classes="local-note")
        note.transcript = Text(text, style=style or "")
        return note

    async def _local_note(self, text: str, style: str | None = None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(self._note(text, style))
        chat.anchor()
        await self._commit_ready_transcript()

    @staticmethod
    def _compact_style(event) -> str | None:
        return OPERATION_COLOR if isinstance(event, CompactFailed) and event.code != "nothing" else None

    async def _show_compact(self, manual: bool, before=None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        self._compact_row = CompactRow(manual)
        self._compacting = self._compact_row.started
        await chat.mount(self._compact_row, before=before)
        chat.anchor()
        self._tick()

    async def _end_compact(self, text: str, style: str | None = None, before=None) -> None:
        """The running row becomes the result; without one (nothing to compact) a note is added."""
        self._compacting = None
        row, self._compact_row = self._compact_row, None
        if row is not None:
            row.finish(text, style)
            return
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(self._note(text, style), before=before)
        chat.anchor()

    async def _start_compact(self, instructions: str) -> None:
        self.started = self._compacting = monotonic()
        self._generating = True
        self._tick()
        self._reply_task = asyncio.create_task(self._run_compact(instructions))

    async def _run_compact(self, instructions: str) -> None:
        try:
            async with aclosing(self.conversation.compact(instructions)) as stream:
                async for event in stream:
                    if isinstance(event, CompactStarted):
                        await self._show_compact(event.manual)
                    elif isinstance(event, (CompactFinished, CompactFailed)):
                        await self._end_compact(compact_notice(event), self._compact_style(event))
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    await self._commit_ready_transcript()
        except asyncio.CancelledError:
            if not self._closing:
                await self._end_compact(CANCELLED_COMPACT)
        except ProviderError as exc:
            await self._end_compact(f"! {exc}", OPERATION_COLOR)
        finally:
            self._generating = False
            self._compacting = None
            if not self._closing:
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                queued = self.conversation.take_queued()
                if queued:
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                self._refresh_queue()
                self._refresh_context()
                self._status("")

    async def _clear_screen(self) -> None:
        """Like Claude Code's /clear: wipe the screen and scrollback, then start again from the banner."""
        if not self.is_inline:
            return
        async with self._scrollback_lock:
            self._history = [startup_banner(self.provider_name)]
            with self.batch_update():
                redraw_transcript(self, self._history)

    def _start_resume(self, session_id: str, *, startup: bool = False) -> None:
        self.started = monotonic()
        self._generating = True
        self._status("恢复会话中…")
        self._reply_task = asyncio.create_task(self._run_resume(session_id, startup))

    async def _show_resumed(self, event: Resumed) -> None:
        restored = event.restored
        self._usage = self.conversation.total_usage
        await self._clear_screen()
        when = restored.last_time.strftime("%Y-%m-%d %H:%M") if restored.last_time else "未知"
        await self._local_note(f"└ 已恢复会话 · {restored.title} · {event.messages} 条消息 · 上次活动 {when}")
        chat = self.query_one("#chat", VerticalScroll)
        for renderable in resumed_rows(restored.history, shell_label(self.conversation.registry)):
            row = Static(renderable, classes="resumed-entry")
            row.transcript = renderable
            await chat.mount(row)
        chat.anchor()
        for notice in event.notices:
            await self._local_note("└ " + notice)

    async def _run_resume(self, session_id: str, startup: bool = False) -> None:
        unanswered: tuple[str, ...] = ()
        try:
            async with aclosing(self.conversation.resume(session_id)) as stream:
                async for event in stream:
                    if isinstance(event, Resumed):
                        unanswered = event.restored.unanswered
                        await self._show_resumed(event)
                    elif isinstance(event, CompactStarted):
                        await self._show_compact(event.manual)
                    elif isinstance(event, (CompactFinished, CompactFailed)):
                        await self._end_compact(compact_notice(event), self._compact_style(event))
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    await self._commit_ready_transcript()
        except asyncio.CancelledError:
            if not self._closing and self._compact_row is not None:
                await self._end_compact(CANCELLED_COMPACT)
        except ProviderError as exc:
            await self._local_note(f"! {exc}", OPERATION_COLOR)
        finally:
            self._generating = False
            self._compacting = None
            if not self._closing:
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                # R8: unanswered messages come back for the user to edit or resend.
                texts = [*unanswered, *self.conversation.take_queued()]
                if texts:
                    field.value = "\n\n".join((*texts, field.value) if field.value.strip() else texts)
                self._refresh_queue()
                self._refresh_context()
                self._status("")
                if startup:
                    self._resume = None
                    self._startup_task = asyncio.create_task(self._initialize())

    def _show_panel(self, title: str, rows: int) -> None:
        self._panel_rows = rows
        self.query_one("#model-title", Static).update(title)
        options = self.query_one("#models", OptionList)
        # One row per choice, so the panel height reserved in _fit_chat holds.
        options.styles.height = min(8, rows)
        self.query_one("#model-panel").display = True
        options.focus()
        self.call_later(self._fit_chat)

    async def _command(self, typed: str, parsed: Parsed) -> None:
        """P9: echo what was typed, then run it; prompt commands echo the command, not the template."""
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(ChatEntry("You", typed))
        chat.anchor()
        await self._commit_ready_transcript()
        if parsed.kind == "unknown":
            await self._local_note(unknown_note(parsed, self.commands))
            return
        try:
            await dispatch(self._command_context(), parsed)
        except ProviderError as exc:
            await self._local_note(f"! {exc}", OPERATION_COLOR)

    def _command_context(self) -> CommandContext:
        return CommandContext(self.conversation, self, self.commands, self.settings)

    # CommandUI (chapter 10 U1/U2) -------------------------------------------------------

    async def note(self, text: str, style: str | None = None) -> None:
        await self._local_note(text, style)

    async def submit(self, prompt: str, *, command: str | None = None, fork=None) -> None:
        await self._start_request(prompt, show_user=False, command=command, fork=fork)

    def refresh_status(self) -> None:
        self._usage = self.conversation.total_usage
        self._refresh_context()
        self._status("")

    async def clear_screen(self) -> None:
        await self._clear_screen()

    def choose(self, title: str, options, current: int, on_pick) -> None:
        picker = self.query_one("#models", OptionList)
        picker.clear_options()
        picker.add_options(list(options))
        self._pick = on_pick
        self._show_panel(title, len(options))
        picker.highlighted = current

    async def compact(self, instructions: str) -> None:
        await self._start_compact(instructions)

    def resume(self, session_id: str) -> None:
        self._start_resume(session_id)

    async def switch_model(self, name: str) -> None:
        config = next(item for item in self.settings.providers if item.name == name)
        await self._switch_model(config)

    async def _switch_model(self, config: ProviderConfig) -> None:
        if config.name == self.provider_name:
            await self._local_note(f"└ 已在使用 {config.name}（{config.model}）。")
            return
        try:
            provider = self.provider_factory(config)
            await self.conversation.set_provider(provider, config.budget(), (config.name, config.protocol, config.model))
        except (ProviderError, ConfigError) as exc:
            await self._local_note(f"切换失败：{exc}")
            return
        self.provider_name, self.model = config.name, config.model
        self._refresh_context()
        self._status("")
        await self._local_note(f"└ 已切换到 {config.name}（{config.protocol} / {config.model}）；对话历史保留。")
        if config.context_window is None:
            await self._local_note(f"└ {config.name} 未配置 context_window，按 128K 估算上下文。")

    def _close_model_panel(self) -> None:
        self._pick = None
        self.query_one("#model-panel").display = False
        self.query_one("#prompt", PromptArea).focus()
        self.call_later(self._fit_chat)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "models":
            pick = self._pick
            self._close_model_panel()
            if pick is not None:
                await pick(event.option_index)
        elif event.option_list.id == "approval-options" and self._approval_request:
            self._answer_approval(self._approval_request.choices[event.option_index])

    def _show_approval(self, request: ApprovalRequest) -> None:
        self._approval_request = request
        operation = request.operation
        labels = {"read_file": "读取文件", "write_file": "写入文件", "edit_file": "修改文件",
                  "glob": "查找文件", "grep": "搜索内容", "exit_plan_mode": "审阅计划",
                  "execute_command": shell_name(shell_label(self.conversation.registry))}
        text = Text()
        text.append("需要授权 · " + labels.get(operation.tool, operation.tool) + "\n", style=f"bold {OPERATION_COLOR}")
        text.append(safe_display(operation.target), style=PARAMETER_COLOR)
        if operation.cwd is not None:
            text.append("\n工作目录：" + operation.cwd)
        text.append("\n" + request.reason)
        if operation.tool == "exit_plan_mode":
            text.append("\n完整计划显示在上方对话中。")
        elif operation.preview:
            text.append("\n")
            text.append_text(preview_text(operation.preview))
        self.query_one("#approval-detail", Static).update(text)
        self.query_one("#approval-panel").display = True
        options = self.query_one("#approval-options", OptionList)
        options.clear_options()
        options.add_options([choice_label(request, choice) for choice in request.choices])
        options.styles.height = len(request.choices)
        options.highlighted = 0
        options.focus()
        self._tick()
        self.call_later(self._fit_chat)

    def _hide_approval(self) -> None:
        self._approval_request = None
        self.query_one("#approval-panel").display = False
        if not self._closing:
            # The prompt stays usable while the task runs, e.g. to queue a correction.
            self.query_one("#prompt", PromptArea).focus()
        self.call_later(self._fit_chat)

    def _answer_approval(self, choice: str) -> None:
        if self._approval_request and self.conversation.registry:
            self.conversation.registry.permissions.respond(self._approval_request.id, choice)
            self._hide_approval()
            self._tick()

    async def _respond(self, prompt: str, reply: ChatEntry, command: str | None = None, fork=None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        tool_rows: dict[str, ToolEntry] = {}
        continuation = False
        completed = False

        try:
            async with aclosing(self.conversation.reply(prompt, command=command, fork=fork)) as stream:
                async for event in stream:
                    if not isinstance(event, Retrying):
                        self._retry_notice = ""
                    if isinstance(event, IterationProgress):
                        self._iteration, self._iteration_limit = event.iteration, event.max_iterations
                        reply.phase = "waiting"
                        self._tick()
                    elif isinstance(event, UsageUpdate):
                        self._usage = event.total
                        self._refresh_context()
                        self._tick()
                    elif isinstance(event, CompactStarted):
                        # Before a reply that has not started yet, so the row reads in time order.
                        await self._show_compact(event.manual, None if continuation else reply)
                    elif isinstance(event, (CompactFinished, CompactFailed)):
                        await self._end_compact(compact_notice(event), self._compact_style(event),
                                                None if continuation else reply)
                        self._refresh_context()
                    elif isinstance(event, RunStopped):
                        completed = event.reason == "completed"
                        for result in event.results:
                            if result.call_id in tool_rows:
                                tool_rows[result.call_id].complete(result)
                        if event.reason in {"max_iterations", "unknown_tools", "max_tokens"}:
                            await self._local_note("└ " + event.notice)
                            self._status("已停止 · 可以继续提问")
                    elif isinstance(event, Retrying):
                        self._retry_notice = f"{event.reason} · {event.delay:.0f} 秒后重试（{event.attempt}/{event.max_attempts}）· Esc 取消"
                        self._status(self._retry_notice)
                    elif isinstance(event, Delta):
                        if continuation:
                            reply = ChatEntry("MossCode")
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                            continuation = False
                        reply.append(event)
                        self._tick()
                    elif isinstance(event, ResponseBoundary):
                        if continuation:
                            reply = ChatEntry("MossCode")
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                        await reply.seal_tool_request(event.message)
                        tool_rows = {}
                        continuation = True
                    elif isinstance(event, ToolProgress):
                        if event.phase == "parsing":
                            self._status("解析工具参数… · Esc 取消")
                        else:
                            if event.phase == "running":
                                reply.phase = "tools"
                            row = tool_rows.get(event.call.id)
                            if row is None:
                                if event.call.name == "exit_plan_mode":
                                    plan = plan_text(event.call.arguments)
                                    if plan:
                                        await chat.mount(PlanEntry(plan))
                                row = ToolEntry(event, shell_label(self.conversation.registry))
                                tool_rows[event.call.id] = row
                                await chat.mount(row)
                            if event.result is not None:
                                row.complete(event.result, event.elapsed)
                                reply.phase = "tools" if any(r.elapsed is None for r in tool_rows.values()) else "waiting"
                            self._tick()
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    elif isinstance(event, UserInput):
                        # Queued messages joined the context at this iteration boundary.
                        for text in event.texts:
                            await chat.mount(ChatEntry("You", text))
                        chat.anchor()
                        self._refresh_queue()
                    elif isinstance(event, ApprovalRequest):
                        self._show_approval(event)
                        if event.call_id in tool_rows:
                            row = tool_rows[event.call_id]
                            row.update(row._heading("running") + Text("\n  └ 等待用户确认…", style=OPERATION_COLOR))
                    elif isinstance(event, Completed):
                        if continuation:
                            reply = ChatEntry("MossCode")
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                        self._generating = False
                        await reply.finish(event.message)
                        self._status("")
                    await self._commit_ready_transcript()
        except asyncio.CancelledError:
            if not self._closing:
                reply.interrupted("已取消，部分回复未加入对话历史。已执行的工具结果保留。")
                self._status("已取消 · 可以重新发送")
        except ProviderError as exc:
            reply.interrupted(str(exc))
            self._status("失败 · 未完成正文未保存，已执行工具结果保留")
        except Exception:
            reply.interrupted("出现内部错误，请检查安装环境。")
            self._status("失败 · 未完成正文未保存，已执行工具结果保留")
        finally:
            if self._compact_row is not None:
                await self._end_compact(CANCELLED_COMPACT)
            self._generating = False
            self._compacting = None
            self._retry_notice = ""
            self._active_reply = None
            self._refresh_context()
            if not self._closing:
                self._hide_approval()
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                field.disabled = False
                field.focus()
                queued = self.conversation.take_queued()
                if queued and completed:
                    # The model answered before the queue could join; send it as the next task.
                    self.call_later(self._start_request, "\n\n".join(queued), shown=queued)
                elif queued:
                    # Stopped, cancelled or failed: never resend automatically.
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                self._refresh_queue()
                notices, self._mcp_notices = self._mcp_notices, []
                for notice in notices:
                    await self._local_note("└ " + notice)

    def action_cycle_mode(self) -> None:
        registry = self.conversation.registry
        if registry is None or self._approval_request or self.query_one("#model-panel").display:
            return
        if self._generating:
            self._hint_until = monotonic() + 2
            self._status("运行中不能切换权限档位 · Esc 取消")
            return
        self.conversation.set_permission_mode(MODE_CYCLE[registry.permissions.mode])
        self._status("")

    def action_cancel(self) -> None:
        if self._approval_request:
            self._answer_approval("deny")
            return
        if self.query_one("#model-panel").display:
            self._close_model_panel()
            return
        if self._hint_choices:
            self._dismissed_prompt = self.query_one("#prompt", PromptArea).value
            self._refresh_hints(self._dismissed_prompt)
            return
        queued = self._take_back_queued()
        if queued:
            # The first Esc only withdraws queued messages; the next one cancels the task.
            self.query_one("#prompt", PromptArea).restore_queued(queued)
            self._hint_until = monotonic() + 2
            self._status("已取回排队消息，任务继续 · 再按 Esc 取消任务")
            return
        self.action_cancel_task()

    def action_cancel_task(self) -> None:
        if self._generating and self._reply_task and not self._reply_task.done():
            if not self._reply_task.cancelling():
                self._reply_task.cancel()

    async def on_unmount(self) -> None:
        self._closing = True
        if self.conversation.registry:
            self.conversation.registry.permissions.interactive = False
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        if self._reply_task and not self._reply_task.done():
            if not self._reply_task.cancelling():
                self._reply_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reply_task
        await self.conversation.close()
