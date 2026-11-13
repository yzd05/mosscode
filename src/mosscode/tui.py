import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic, perf_counter
from typing import TypeVar

from rich.cells import cell_len
from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message as UIMessage
from textual.widgets import OptionList, Static, TextArea

from .agents.tasks import Announcer
from .appearance import (
    BRAND_COLOR,
    COMMAND_HIGHLIGHT_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    OPERATION_COLOR,
    PARAMETER_COLOR,
    RULE_COLOR,
    TERMINAL_CSS,
    TOOL_SUCCESS_COLOR,
    USER_MESSAGE_BACKGROUND,
    USER_MESSAGE_COLOR,
    compact_notice,
    moss_themes,
    startup_banner,
)
from .builtin_commands import (
    BUILTIN,
    TRUST_OPTIONS,
    clean_text,
    confirm_tasks,
    dispatch,
    offer_trust,
    open_sessions,
)
from .catalog import tokens_label
from .choice_list import KeyboardOptions
from .clipboard import ClipboardError, is_image_file, pasted_image_paths, read_clipboard_async
from .commands import CommandContext, CommandRegistry, Hint, Parsed, parse_input, unknown_note
from .config import ConfigError, ProviderConfig, Settings, refreshed
from .conversation import Conversation, MemoryUpdated, Notice, Resumed
from .coordinator import BADGE as COORDINATOR_BADGE
from .follow import SOURCES as FOLLOWED
from .goal import GOAL, GoalStep, elapsed_text
from .images import ImageError, ImageRef, ProcessedImage, load_image, size_label
from .member_ui import PANEL_HELP, STOP_CONFIRM, VIEW_MESSAGES, VIEW_SHARE, MemberList, MemberPanel, MemberView
from .memory import SCOPE_LABELS
from .mentions import FileIndex, mentioned, partial_mention, quoted
from .messages import (
    CONTINUE,
    IMAGE_MARK,
    MENTIONS,
    NOTIFICATION,
    REMINDER,
    SHELL,
    STOPPED_EARLY,
    AutoContinue,
    CompactFailed,
    CompactFinished,
    CompactStarted,
    Completed,
    ContextCollapsed,
    Delta,
    FilesAttached,
    IterationProgress,
    Message,
    ProviderError,
    ReplyRetry,
    ResponseBoundary,
    ResultsCleared,
    Retrying,
    RunStopped,
    ToolCall,
    ToolProgress,
    ToolResult,
    UsageUpdate,
    UserInput,
    image_mark,
)
from .permissions import CHOICES, EDIT_TOOLS, MODE_BADGES, Answer, ApprovalRequest, QuestionRequest, safe_display
from .sandbox import badge as sandbox_badge
from .scrollback import StreamingMarkdown, TranscriptMarkdown, commit_transcript, message_row, redraw_transcript
from .teams import TeamError
from .teams.runtime import MemberRow
from .teams.view import TranscriptTail
from .todo import completed as todos_completed
from .tool_budget import persisted_note
from .tools import shell_label
from .tools.ask import OTHER as OTHER_ANSWER
from .usage import approx_tokens, cache_hit_rate
from .user_command import CommandRun, ran

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
    "once": "本次允许 · 仅执行当前调用",
    "session": "本会话允许 · 同工具与目标，退出后失效",
    "permanent": "永久允许 · 保存到本项目本地配置",
    "deny": "拒绝 · 让模型调整策略",
    "execute": "批准计划并执行 · 恢复为进入计划前的权限",
    "accept_edits": "批准并自动接受编辑 · 切换为接受编辑档位",
    "merge": "合并 · 把成员的提交合并到当前分支",
}


DECLINED_HINT = "└ 没有合并。想说明原因或改动要求，直接在输入框告诉 Lead。"


def choice_label(request: ApprovalRequest, choice: str) -> str:
    if request.operation.tool == "exit_plan_mode" and choice == "deny":
        return "继续规划 · 拒绝并让模型修改计划"
    if request.operation.tool == "team_merge" and choice == "deny":
        return "不合并 · Lead 会停下来问你"
    if request.operation.unsandboxed and choice == "once":
        return "本次在沙箱外运行 · 以你的完整权限执行这一条"
    if request.operation.unsandboxed and choice == "session":
        prefix = request.operation.prefix
        what = f"以 {prefix} 开头的命令" if prefix else "这条命令"
        return f"本会话允许{what}在沙箱外运行 · 退出后失效"
    if request.operation.unsandboxed and choice == "deny":
        return "拒绝 · 命令留在沙箱中，让模型调整策略"
    if request.operation.risk and choice == "once":
        return "本次允许 · 高风险命令每次都需确认"
    if choice == "prefix":
        return f"永久允许以 {request.operation.prefix} 开头的命令 · 保存到本项目本地配置"
    if choice == "accept_edits" and request.operation.tool in EDIT_TOOLS:
        return "本会话自动接受编辑 · 切换为接受编辑档位，命令仍需确认"
    if request.operation.network:
        host = request.operation.target
        if choice == "once":
            return f"本次允许 · 只在这条命令中连接 {host}"
        if choice == "session":
            return f"本会话允许这个域名 · 命令和 web_fetch 都可以访问 {host}，退出后失效"
        if choice == "permanent":
            return f"永久允许这个域名 · 保存为 WebFetch(domain:{host})"
        if choice == "deny":
            return "拒绝 · 这条命令连接这个域名会失败"
    if request.operation.tool == "web_fetch" and choice in {"session", "permanent"}:
        host = request.operation.target
        if choice == "session":
            return f"本会话允许这个域名 · {host} 的任意页面，退出后失效"
        return f"永久允许这个域名 · 保存为 WebFetch(domain:{host})"
    if request.operation.tool == "web_search" and choice == "session":
        return "本会话允许搜索 · 退出后失效"
    if request.operation.tool == "web_search" and choice == "permanent":
        return "永久允许搜索 · 保存为 WebSearch"
    if (request.operation.server or request.operation.skill) and choice == "session":
        return "本会话允许该工具 · 任意参数，退出后失效"
    if (request.operation.server or request.operation.skill) and choice == "permanent":
        return "永久允许该工具 · 任意参数，保存到本项目本地配置"
    return CHOICE_LABELS[choice]


def choice_prompt(label: str) -> Text:
    """An approval option as shown: its name at full strength, what it does in the supporting grey."""
    name, _, explanation = label.partition(" · ")
    text = Text(name)
    if explanation:
        text.append("  " + explanation, style=MUTED_COLOR)
    return text


def preview_text(preview: str) -> Text:
    """Approval preview; diffs are colored line by line."""
    text = Text()
    diff = any(line.startswith(("@@", "--- a/", "+++ b/")) for line in preview.splitlines()[:3]) or preview.startswith(
        "新建文件 "
    )
    for index, line in enumerate(preview.splitlines()):
        style = None
        if diff and line.startswith("+") and not line.startswith("+++"):
            style = TOOL_SUCCESS_COLOR
        elif diff and line.startswith("-") and not line.startswith("---"):
            style = ERROR_COLOR
        elif diff and line.startswith(("@@", "---", "+++", "…")):
            style = MUTED_COLOR
        text.append(("\n" if index else "") + line, style=style)
    return text


# The status badge and the prompt's rules share a color per mode; the default mode has none.
MODE_COLORS = {"acceptEdits": TOOL_SUCCESS_COLOR, "plan": OPERATION_COLOR, "bypassPermissions": ERROR_COLOR}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def activity_line(note: str, now: float) -> Text:
    """The running stage above the prompt: a spinner that turns five times a second, the stage, its details."""
    stage, separator, rest = note.partition(" · ")
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(SPINNER[int(now * 5) % len(SPINNER)] + " ", style=BRAND_COLOR)
    text.append(stage, style=OPERATION_COLOR)
    text.append(separator + rest, style=MUTED_COLOR)
    return text


# A tool row's mark: the same mark stands for the call in the activity line's recent results.
TOOL_MARKS = {
    "running": ("●", OPERATION_COLOR),
    "success": ("✓", TOOL_SUCCESS_COLOR),
    "error": ("×", ERROR_COLOR),
    "stopped": ("!", OPERATION_COLOR),
}
RECENT_TOOLS = 5  # Tool calls whose outcome stays on the activity line.
STAGE_ROOM = 16  # Narrower than this beside the recent results, the stage keeps the line to itself.


def tool_state(result: ToolResult) -> str:
    """How a call ended: stopped before or while running, failed, or succeeded."""
    if result.code in {"cancelled", "not_executed", "plan_blocked"}:
        return "stopped"
    return "error" if result.is_error else "success"


def recent_tools_line(states: Iterable[str]) -> Text:
    """The outcome marks of the latest tool calls, oldest first, colored like their rows."""
    text = Text(no_wrap=True)
    for index, state in enumerate(states):
        if not index:
            text.append("最近 ", style=MUTED_COLOR)
        marker, color = TOOL_MARKS[state]
        text.append(("" if not index else " ") + marker, style=color)
    return text


def activity_row(note: str, now: float, states: Iterable[str], width: int) -> Text:
    """The stage on the left and, at the right end, the recent tool outcomes; the stage gives way when narrow."""
    line = activity_line(note, now)
    recent = recent_tools_line(states)
    if not recent or width < recent.cell_len + 2 + STAGE_ROOM:
        return line
    line.truncate(width - recent.cell_len - 2, overflow="ellipsis")
    line.append(" " * (width - line.cell_len - recent.cell_len))
    line.append_text(recent)
    return line


# Shift+Tab order; bypassPermissions is entered only explicitly and leaves to default.
MODE_CYCLE = {"default": "acceptEdits", "acceptEdits": "plan", "plan": "default", "bypassPermissions": "default"}
DIFF_ROWS = 20  # Diff lines shown under an edit/write result.
NEW_FILE_ROWS = 8


def diff_rows(diff: str, limit: int = DIFF_ROWS) -> Text:
    """Unified diff as numbered, colored rows; hunks are separated by ⋯."""
    rows: list[tuple[str, str, str | None]] = []  # Line number, the marked line, its color.
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
                rows.append(("     ⋯", "", None))
        elif line.startswith("-"):
            rows.append((f"{old:>5}", f" - {line[1:]}", ERROR_COLOR))
            old += 1
        elif line.startswith("+"):
            rows.append((f"{new:>5}", f" + {line[1:]}", TOOL_SUCCESS_COLOR))
            new += 1
        elif line.startswith(" "):
            rows.append((f"{new:>5}", f"   {line[1:]}", None))
            old, new = old + 1, new + 1
        elif line.strip():
            notes.append(line)  # e.g. the generator's own truncation note
    text = Text()
    for number, row, style in rows[:limit]:
        text.append("\n    " + number, style=MUTED_COLOR)
        text.append(row[: 160 - len(number)], style=style)
    if len(rows) > limit:
        text.append(f"\n    … 还有 {len(rows) - limit} 行 diff 未显示", style=MUTED_COLOR)
    for note in notes:
        text.append("\n    " + note)
    return text


@dataclass(frozen=True)
class Attachment:
    image: ProcessedImage
    source: str  # "剪贴板" or the file name

    @property
    def label(self) -> str:
        return f"{self.source} · {self.image.describe()}"


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

    class ImagePaste(UIMessage):
        """Attach images: files (dragged in or pasted as paths), or whatever image the clipboard holds.

        otherwise runs when the clipboard holds no image (Ctrl+V's ordinary paste); without it the user is told.
        """

        def __init__(
            self,
            text_area: "PromptArea",
            files: list[Path] | None = None,
            otherwise: Callable[[], None] | None = None,
        ) -> None:
            super().__init__()
            self.text_area, self.files, self.otherwise = text_area, files, otherwise

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
        # Every image attached in this process, by placeholder. Kept after sending so numbers never repeat
        # and a recalled history entry or queued message still has its images.
        self.attachments: dict[str, Attachment] = {}
        # Set by the app: relative pasted paths resolve against the workspace.
        self.root: Callable[[], Path] = Path.cwd

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
        if event.key == "alt+v" and not self.read_only:
            # Windows Terminal keeps Ctrl+V for its own paste, which carries text only.
            event.stop()
            event.prevent_default()
            self.post_message(self.ImagePaste(self))
            return
        await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        # Overrides TextArea._on_paste only when handled; otherwise Textual still runs the base handler.
        if self.read_only:
            return
        files = await asyncio.to_thread(pasted_image_paths, event.text, self.root())
        if files:
            event.prevent_default()
            self.post_message(self.ImagePaste(self, files, otherwise=lambda: self.insert_pasted(event.text)))

    def action_paste(self) -> None:
        """Ctrl+V, where the terminal passes it through: the clipboard image if any, else the usual paste."""
        if not self.read_only:
            self.post_message(self.ImagePaste(self, otherwise=super().action_paste))

    def insert_pasted(self, text: str) -> None:
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)

    def attach(self, items: list["Attachment"]) -> None:
        """Insert a placeholder per image at the cursor."""
        marks = []
        for item in items:
            mark = image_mark(len(self.attachments) + 1)
            self.attachments[mark] = item
            marks.append(mark)
        result = self.replace(" ".join(marks) + " ", *self.selection, maintain_selection_offset=False)
        self.move_cursor(result.end_location)
        self.focus()

    def images_in(self, text: str) -> dict[str, ProcessedImage]:
        """The attached images whose placeholders are still in text."""
        return {
            match.group(): self.attachments[match.group()].image
            for match in IMAGE_MARK.finditer(text)
            if match.group() in self.attachments
        }

    def image_notes(self, text: str) -> list[str]:
        return [f"{mark} {self.attachments[mark].label}" for mark in self.images_in(text)]

    @property
    def recalled(self) -> bool:
        """The text is an unedited history entry, so ↑ / ↓ keep browsing instead of opening command hints."""
        return self._history_index is not None and self.text == self.input_history[self._history_index]

    def remember(self, text: str) -> None:
        if text.strip() and (not self.input_history or self.input_history[-1] != text):
            self.input_history.append(text)
        self._history_index = None
        self._draft = ""

    def restore_message(self, text: str, images: Mapping[str, ProcessedImage]) -> None:
        """A message /rewind took back: images this process no longer holds are attached again.

        A placeholder already naming a different image here gets a fresh number, so it never sends the wrong one.
        """
        renamed = {}
        for mark, image in images.items():
            known = self.attachments.get(mark)
            if known is not None and known.image.data == image.data:
                continue
            if known is not None:
                renamed[mark] = mark = image_mark(len(self.attachments) + 1)
            self.attachments[mark] = Attachment(image, "回退")
        self.restore_queued((IMAGE_MARK.sub(lambda match: renamed.get(match.group(), match.group()), text),))

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
        if (
            not select
            and self._history_index is not None
            and self.navigator.is_last_document_line(location)
            and self.navigator.is_last_wrapped_line(location)
        ):
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
        self.transcript = Group(
            Text("计划", style=f"bold {OPERATION_COLOR}"), message_row("│", TranscriptMarkdown(plan))
        )
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


SHELL_HEAD, SHELL_TAIL = 10, 20  # Output lines shown under a ! command; the context keeps more.


def shell_heading(command: str, keep: bool = True) -> Text:
    text = Text()
    text.append("! " if keep else "!! ", style=f"bold {OPERATION_COLOR}")
    text.append(" ".join(command.splitlines())[:200], style=PARAMETER_COLOR)
    return text


def shell_output(output: str) -> Text:
    lines = output.splitlines()
    if len(lines) > SHELL_HEAD + SHELL_TAIL:
        hidden = len(lines) - SHELL_HEAD - SHELL_TAIL
        lines = [*lines[:SHELL_HEAD], f"…（省略 {hidden} 行）…", *lines[-SHELL_TAIL:]]
    return Text("\n".join(f"  {line[:300]}" for line in lines))


class ShellRow(Static):
    """A ! command while it runs, then its output and exit code in place."""

    def __init__(self, command: str, keep: bool):
        super().__init__(classes="tool-entry")
        self.heading = shell_heading(command, keep)
        self.keep = keep
        self.started = monotonic()
        self.transcript: Text | None = None  # Set once finished; scrollback waits until then.
        self.tick()

    def tick(self) -> None:
        if self.transcript is None:
            elapsed = monotonic() - self.started
            self.update(self.heading + Text(f"\n  └ 执行中 · {elapsed:.1f}s · Esc 取消", style=OPERATION_COLOR))

    def finish(self, run: CommandRun) -> None:
        text = self.heading.copy()
        if run.exit_code is None:
            text.append(f"\n  └ 未执行 · {run.problem}", style=OPERATION_COLOR)
        else:
            if run.output:
                text.append("\n")
                text.append_text(shell_output(run.output))
            kept = "已加入对话，下一条消息发送时模型能看到" if self.keep else "未加入对话"
            text.append(
                f"\n  └ 退出码 {run.exit_code} · {run.elapsed:.1f}s · {kept}",
                style=TOOL_SUCCESS_COLOR if run.exit_code == 0 else ERROR_COLOR,
            )
        self.transcript = text
        self.update(text)

    def stop(self, reason: str) -> None:
        self.transcript = self.heading + Text(f"\n  └ {reason}", style=OPERATION_COLOR)
        self.update(self.transcript)


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


class ProviderSelectApp(TerminalApp[ProviderConfig]):
    TITLE = "MossCode"
    SUB_TITLE = "选择本次会话的提供商与模型"
    CSS = (
        TERMINAL_CSS
        + """
    Screen { background: $background; padding: 1 2; }
    Screen:inline { height: auto; border: none; }
    #selection { height: auto; max-height: 50vh; border: none; background: $background; }
    #selection-help { margin: 1 0; height: auto; color: $text-muted; }
    #banner { height: auto; margin-bottom: 1; }
    """
    )
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
            (
                index
                for index, item in enumerate(self.settings.providers)
                if item.name == self.settings.default_provider
            ),
            0,
        )
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.exit(self.settings.providers[event.option_index])


class PluginTrustApp(TerminalApp[bool]):
    """Asked before a session loads project plugins; their contents are printed above it, where they can be
    scrolled. True trusts them; Esc and 本次不启用 return False or None."""

    TITLE = "MossCode"
    SUB_TITLE = "确认项目插件"
    CSS = ProviderSelectApp.CSS
    BINDINGS = ProviderSelectApp.BINDINGS

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def compose(self) -> ComposeResult:
        yield Static(
            f"信任并启用项目插件 {'、'.join(self.names)}？↑ / ↓ 选择，Enter 确认；Esc 本次不启用。",
            id="selection-help",
            markup=False,
        )
        yield KeyboardOptions(*(Text(option) for option in TRUST_OPTIONS), id="selection")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#selection", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.exit(event.option_index == 0)


class ConfigTrustApp(PluginTrustApp):
    """Asked before a session uses a project's configuration; what it names is printed above. True trusts it;
    Esc and 本次不使用 return False or None."""

    SUB_TITLE = "确认项目配置"
    OPTIONS = ("信任并使用", "本次不使用")

    def __init__(self, otherwise: str) -> None:
        super().__init__([])
        self.otherwise = otherwise  # What declining leads to.

    def compose(self) -> ComposeResult:
        yield Static(
            f"信任并使用这份项目配置？↑ / ↓ 选择，Enter 确认；Esc 本次不使用（{self.otherwise}）。",
            id="selection-help",
            markup=False,
        )
        yield KeyboardOptions(*(Text(option) for option in self.OPTIONS), id="selection")


class MCPTrustApp(PluginTrustApp):
    """Asked before a session starts the MCP servers a project's files declare; what each runs is printed above.
    True trusts them; Esc and 本次不启动 return False or None."""

    SUB_TITLE = "确认项目 MCP 服务器"
    OPTIONS = ("信任并启动", "本次不启动")

    def compose(self) -> ComposeResult:
        yield Static(
            f"信任并启动项目 MCP 服务器 {'、'.join(self.names)}？↑ / ↓ 选择，Enter 确认；Esc 本次不启动。",
            id="selection-help",
            markup=False,
        )
        yield KeyboardOptions(*(Text(option) for option in self.OPTIONS), id="selection")


class ChatEntry(Vertical):
    def __init__(self, speaker: str, text: str = "", notes: list[str] | tuple[str, ...] = ()) -> None:
        super().__init__(classes="entry user-entry" if speaker == "You" else "entry assistant-entry")
        self.speaker = speaker
        self.body = text
        self.notes = tuple(notes)  # Under a user message: its attached images.
        self.thinking = ""
        self.started = monotonic()
        self.phase = "waiting"
        self.thinking_started: float | None = None
        self.thinking_elapsed = 0.0
        self.completed_elapsed: float | None = None
        self.interruption_note = ""
        self.tool_request = False
        self._markdown: StreamingMarkdown | None = None
        self._paint_at = 0.0  # perf_counter time before which the reply is not drawn again.
        self._paint_timer = None

    def compose(self) -> ComposeResult:
        if self.speaker == "You":
            with Horizontal(classes="message-row"):
                yield Static("❯", classes="message-marker")
                yield Static(self.body, classes="user-message", markup=False)
            if self.notes:
                yield Static(image_lines(self.notes), classes="image-notes")
            return
        for classes in ("thinking-label", "thinking"):
            widget = Static("", classes=classes, markup=False)
            widget.display = False
            yield widget
        with Horizontal(classes="message-row"):
            yield Static("●", classes="message-marker")
            yield Static(self.body or "等待回复…", classes="response-content answer", markup=False)
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
            self.query_one(".thinking-label", Static).update(f"✻ 思考 {self.thinking_elapsed:.1f}s")
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
            if self._markdown is None:
                self._markdown = StreamingMarkdown()
            self._markdown.feed(event.text)
            self.query_one(".message-row").display = True
            self._paint_reply()

    def _paint_reply(self) -> None:
        """Draw the reply as it stands, but not faster than drawing it costs: a long block still being written,
        such as a code block of hundreds of lines, is rendered again for each redraw and would hold up the keys."""
        if self._paint_timer is not None:
            return  # A redraw is already due; it will show the latest text.
        wait = self._paint_at - perf_counter()
        if wait > 0:
            self._paint_timer = self.set_timer(wait, self._paint_now)
            return
        self._paint_now()

    def _paint_now(self) -> None:
        self._paint_timer = None
        if self._markdown is None or self.phase != "streaming":
            return  # Finished, dropped or cut off meanwhile: that has already drawn the reply.
        self.query_one(".answer", Static).update(self._markdown)
        self._paint_at = perf_counter() + min(0.25, 3 * self._markdown.tail_seconds)

    def _show_reply(self, renderable) -> None:
        if self._paint_timer is not None:
            self._paint_timer.stop()
            self._paint_timer = None
        self.query_one(".answer", Static).update(renderable)

    def _reply_markdown(self) -> StreamingMarkdown:
        """The reply text in full, drawn in one piece, as the scrollback will draw it."""
        markdown = self._markdown or StreamingMarkdown()
        markdown.finish(self.body or "（服务返回了空正文）")
        self._markdown = markdown
        return markdown

    def discard(self) -> None:
        """The streamed reply was dropped and the request is sent again: start over."""
        self.thinking, self.body, self.thinking_started, self.thinking_elapsed = "", "", None, 0.0
        self.phase = "waiting"
        self._markdown = None
        for selector in (".thinking-label", ".thinking"):
            self.query_one(selector, Static).display = False
        self._show_reply("等待回复…")
        self.query_one(".message-row").display = True

    async def finish(self, message: Message, *, cut: bool = False) -> None:
        """cut: stopped at the output limit; the note after it says what happens next, not a done summary."""
        self._finish_thinking()
        self.phase = "complete"
        self.body = message.text
        self._show_reply(self._reply_markdown())
        self.query_one(".message-row").display = True
        if cut:
            return
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
            self._show_reply(self._reply_markdown())

    def interrupted(self, note: str) -> None:
        self.interruption_note = note
        self.display = True
        self._finish_thinking()
        self.phase = "interrupted"
        self.query_one(".message-row").display = True
        self._show_reply(self._interrupted_reply(self._reply_markdown() if self.body else None))

    def _interrupted_reply(self, markdown):
        """What arrived, drawn as it was while it arrived, then a blank row and what stopped it."""
        note = Text(self.interruption_note)
        return Group(markdown, Text(""), note) if markdown is not None else note

    def transcript(self):
        if self.speaker == "You":
            return user_row(self.body, self.notes)
        if not self.display:
            return None
        parts = []
        if self.thinking_elapsed:
            parts.append(Text(f"✻ 思考 {self.thinking_elapsed:.1f}s", style=MUTED_COLOR))
            if self.interruption_note or self.body or not self.tool_request:
                parts.append(Text(""))  # As on screen: a blank line before the reply, none after a label left alone.
        if self.interruption_note:
            body = TranscriptMarkdown(self.body) if self.body else None
            parts.append(message_row("●", self._interrupted_reply(body)))
        elif self.body or not self.tool_request:
            # A response that only calls tools has no text to show, as on screen before it was committed.
            parts.append(message_row("●", TranscriptMarkdown(self.body or "（服务返回了空正文）")))
        if self.completed_elapsed is not None:
            parts.append(Text(f"\n✻ 完成 · {self.completed_elapsed:.1f}s", style=MUTED_COLOR))
        return Group(*parts)


def plan_text(arguments: str) -> str:
    try:
        plan = json.loads(arguments).get("plan")
    except (ValueError, AttributeError):
        return ""
    return plan if isinstance(plan, str) else ""


def _writes_only(operation) -> bool:
    """A command the partial sandbox runs: its approval still decides what the command may read and reach."""
    from .sandbox import current

    sandbox = current()
    command = operation.tool == "execute_command" or operation.tool.startswith("skill__")
    return command and not operation.unsandboxed and sandbox.enabled and sandbox.status().state == "partial"


def shell_name(label: str | None) -> str:
    if label is None:
        return "PowerShell" if os.name == "nt" else "Shell"
    return "PowerShell" if label == "PowerShell" else "Bash"


def agent_detail(args: dict, isolated: bool) -> str:
    """An agent row names the worktree only when the sub-agent runs in one."""
    parts = [args.get("agent_type") or "fork", *(["工作树"] if isolated else []), args.get("description", "")]
    return " ".join(" · ".join(map(str, parts)).splitlines())[:100]


def describe_call(call: ToolCall, shell: str | None = None) -> tuple[str, str, dict]:
    """(display name, main argument, parsed arguments) for one tool line."""
    names = {
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Edit",
        "memory": "Memory",
        "execute_command": shell_name(shell),
        "glob": "Glob",
        "grep": "Grep",
        "exit_plan_mode": "Plan",
        "todo_write": "Todo",
        "ask_user": "Ask",
        "agent": "Agent",
        "task_output": "TaskOutput",
        "task_stop": "TaskStop",
    }
    arguments: dict = {}
    try:
        args = json.loads(call.arguments)
        if isinstance(args, dict):
            arguments = args
        if call.name.startswith(("mcp__", "skill__")):
            detail = json.dumps(args, ensure_ascii=False, sort_keys=True)
        elif call.name == "memory":
            detail = f"{args.get('action', '')} {args.get('name', '')}".strip()
        elif call.name == "agent":
            detail = agent_detail(args, args.get("isolation") == "worktree")
        elif call.name in {"task_output", "task_stop"}:
            detail = args.get("task_id", "")
        elif call.name == "ask_user":
            questions = args.get("questions")
            questions = questions if isinstance(questions, list) else []
            detail = "、".join(str(q.get("header", "")) for q in questions if isinstance(q, dict))
        elif call.name == "todo_write":
            todos = args.get("todos")
            todos = todos if isinstance(todos, list) else []
            done = sum(isinstance(item, dict) and item.get("status") == "completed" for item in todos)
            detail = f"已完成 {done}/{len(todos)}" if todos else "清空清单"
        else:
            detail = (
                args.get("pattern", "")
                if call.name in {"glob", "grep"}
                else args.get("command") or args.get("path") or ""
            )
            if call.name == "exit_plan_mode":
                detail = "提交计划"
    except (ValueError, AttributeError):
        detail = "参数无效"
    detail = " ".join(str(detail).splitlines())
    return names.get(call.name, call.name), detail[:100], arguments


TODO_PANEL_ROWS = 4  # Unfinished items shown under the heading; /todo lists all.


def todo_panel(items) -> Text:
    """The checklist above the prompt: unfinished items in order, or one line once all are done."""
    done = todos_completed(items)
    if done == len(items):
        return Text(f"✓ 清单已完成 {done} 项", style=MUTED_COLOR)
    text = Text()
    text.append(f"清单 · 已完成 {done}/{len(items)}", style=MUTED_COLOR)
    unfinished = [item for item in items if item.status != "completed"]
    for item in unfinished[:TODO_PANEL_ROWS]:
        if item.status == "in_progress":
            text.append("\n▶ " + safe_display(item.content, 200), style=f"bold {PARAMETER_COLOR}")
        else:
            text.append("\n□ " + safe_display(item.content, 200))
    if len(unfinished) > TODO_PANEL_ROWS:
        text.append(f"\n… 另有 {len(unfinished) - TODO_PANEL_ROWS} 项未完成 · /todo 查看全部", style=MUTED_COLOR)
    return text


def answer_lines(details: dict) -> list[str]:
    """ask_user's tool row: each question's label and what the user answered."""
    lines = []
    for question, answer in zip(details.get("questions") or [], details.get("answers") or [], strict=False):
        if not isinstance(question, dict):
            continue
        if not isinstance(answer, dict):
            said = "未回答"
        else:
            parts = ["、".join(str(c) for c in answer.get("choices") or [])] if answer.get("choices") else []
            if answer.get("other"):
                parts.append("“" + str(answer["other"]) + "”")
            said = "；".join(parts) or "未回答"
        lines.append(f"{question.get('header', '')}：{safe_display(said, 160)}")
    return lines


class QuestionOptions(KeyboardOptions):
    BINDINGS = [Binding("space", "toggle", show=False)]

    def action_toggle(self) -> None:
        self.app._toggle_question()


RESUMED_MESSAGES = 20
DOUBLE_ESCAPE = 0.5  # Seconds between two Esc presses on an empty prompt that open /rewind.


def image_lines(notes) -> Text:
    return Text("\n".join(f"  └ {note}" for note in notes), style=MUTED_COLOR)


def user_row(text: str, notes=()):
    """A user message on its gray band, with its images listed underneath."""
    row = Padding(
        message_row("❯", Text(text)), 0, expand=True, style=f"{USER_MESSAGE_COLOR} on {USER_MESSAGE_BACKGROUND}"
    )
    return Group(row, image_lines(notes)) if notes else row


def attached_line(entries) -> str:
    return "└ 已附上 " + "、".join(entries)


def resumed_rows(history, shell: str | None = None, limit: int = RESUMED_MESSAGES) -> list:
    """The latest messages in brief, for the scrollback after a resume."""
    shown = [m for m in history if m.role != "tool" and m.synthetic != REMINDER][-limit:]
    # ask_user's answers are shown with its call, so a resumed session still tells what the user chose.
    answers = {
        r.call_id: r.details
        for m in history
        if m.role == "tool"
        for r in m.tool_results
        if not r.is_error and r.details.get("answers") is not None
    }
    rows = []
    for message in shown:
        if message.synthetic == "compact":
            rows.append(Text("（较早的对话已压缩）"))
        elif message.synthetic == NOTIFICATION:
            ids = "、".join(re.findall(r"^任务：(\S+)", message.text, re.MULTILINE))
            rows.append(Text(f"└ 已收到后台任务通知：{ids}", style=PARAMETER_COLOR))
        elif message.synthetic == MENTIONS:
            rows.append(Text(attached_line(mentioned(message)), style=PARAMETER_COLOR))
        elif message.synthetic == SHELL:
            rows.append(shell_heading(ran(message)))
        elif message.synthetic == GOAL:
            rows.append(Text("◎ 继续目标", style=PARAMETER_COLOR))
        elif message.synthetic == CONTINUE:
            rows.append(Text("↻ 回复达到输出上限，自动续写", style=PARAMETER_COLOR))
        elif message.role == "user":
            notes = [
                f"{block.text} · {block.image.width}×{block.image.height} · {size_label(block.image.size)}"
                for block in message.content
                if block.image is not None
            ]
            rows.append(user_row(message.shown, notes))
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
                if call.name == "ask_user" and call.id in answers:
                    parts.extend(Text(f"    └ {said}") for said in answer_lines(answers[call.id]))
            if parts:
                rows.append(Group(*parts))
    return rows


def rewound_image(ref: ImageRef) -> ProcessedImage:
    """A sent image back as an attachment: it was already processed, so its sizes are what was sent."""
    return ProcessedImage(ref.read(), ref.media_type, ref.width, ref.height, ref.width, ref.height, ref.size)


def memory_line(details: dict) -> Text:
    """What the memory tool did, in place of the usual result summary."""
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
        self.state = "running"
        self.elapsed: float | None = None
        self.subagent = None  # The task of an agent row, once it starts.
        call = event.call
        self.operation = call.name
        self.operation_name, self.detail, self.arguments = describe_call(call, shell)
        if self.operation == "agent":  # The task says whether it really got a worktree (plan mode skips it).
            self.detail = agent_detail(self.arguments, False)
        self.label = f"● {self.operation_name}({self.detail})"
        self.transcript = self._heading("running") + Text("\n  └ 执行中…", style=OPERATION_COLOR)
        self.update(self.transcript)

    def _heading(self, state: str) -> Text:
        marker, color = TOOL_MARKS[state]
        if state in {"error", "stopped"}:
            action_color = color
        else:
            action_color = (
                OPERATION_COLOR
                if self.operation in {"write_file", "edit_file", "execute_command"}
                or self.operation.startswith(("mcp__", "skill__"))
                else TOOL_SUCCESS_COLOR
            )
        text = Text()
        text.append(marker + " ", style=color)
        text.append(self.operation_name, style=f"bold {action_color}")
        text.append("(")
        text.append(self.detail, style=PARAMETER_COLOR)
        text.append(")")
        return text

    def progress(self, task) -> None:
        """A sub-agent's tool calls are not rows of their own; its agent row counts them."""
        if self.elapsed is not None:
            return
        if self.operation == "agent":
            self.detail = agent_detail(self.arguments, task.isolated)
            self.label = f"● {self.operation_name}({self.detail})"
        place = f"工作树 {task.worktree['name']} · " if task.worktree else ""
        if task.background:
            line = f"后台 · {place}任务 {task.id}"
        else:
            line = (
                place
                + f"工具调用 {task.tool_calls} 次"
                + (f" · 最近 {task.last_tool}" if task.last_tool else "")
                + f" · Token {task.tokens:,} · {task.elapsed:.0f}s · Ctrl+B 转入后台"
            )
        self.transcript = self._heading("running") + Text("\n  └ " + line, style=OPERATION_COLOR)
        self.update(self.transcript)

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
                text.append(f"\n    {number:>5}", style=MUTED_COLOR)
                text.append(f" + {line[:160]}", style=TOOL_SUCCESS_COLOR)
            if len(lines) > NEW_FILE_ROWS:
                text.append(f"\n    … 还有 {len(lines) - NEW_FILE_ROWS} 行未显示", style=MUTED_COLOR)
            return text
        return None

    def complete(self, result: ToolResult, elapsed: float | None = None):
        lines = result.output.splitlines() or ["完成，无输出。"]
        shown = lines[:3]
        if elapsed is not None or self.elapsed is None:
            self.elapsed = elapsed if elapsed is not None else monotonic() - self.started
        state = self.state = tool_state(result)
        if self.operation == "memory" and not result.is_error:
            text = memory_line(result.details)
            text.append(f" {self.elapsed:.1f}s", style=MUTED_COLOR)
            self.transcript = text
            self.update(text)
            return
        text = self._heading(state)
        text.append(f" {self.elapsed:.1f}s\n  └ ", style=MUTED_COLOR)
        # What a successful call returned is a note on the process; a failure's detail is what to read.
        summary = MUTED_COLOR if state == "success" else None
        if self.operation in {"agent", "execute_command"} and result.details.get("background"):
            text.append(f"后台 · 任务 {result.details.get('task', '')}", style=PARAMETER_COLOR)
            if result.details.get("log"):
                text.append(f" · {result.details['log']}", style=PARAMETER_COLOR)
            self.transcript = text
            self.update(text)
            return
        if self.operation == "ask_user" and not result.is_error and result.details.get("answers") is not None:
            shown = answer_lines(result.details) or shown
            summary = None  # The user's own answers.
        if self.operation == "agent" and len(lines) > 3 and lines[-1].startswith("（子 Agent"):
            shown = [*lines[:2], lines[-1]]
        if result.is_error:
            label = "已取消" if result.code == "cancelled" else "未执行" if state == "stopped" else "失败"
            text.append(label + " · ", style=f"bold {OPERATION_COLOR if state == 'stopped' else ERROR_COLOR}")
        text.append(shown[0][:160], style=summary)
        changes = self._changes(result)
        if changes is not None:
            text.append_text(changes)
        else:
            for line in shown[1:]:
                text.append("\n    " + line[:160], style=summary)
            if len(lines) > 3 or any(len(line) > 160 for line in lines[:3]) or result.details.get("truncated"):
                text.append("\n    （仅显示结果摘要）", style=MUTED_COLOR)
        stored = persisted_note(result)
        if stored:
            text.append("\n    " + stored, style=PARAMETER_COLOR)
        if result.details.get("hook_modified"):
            text.append(
                f"\n    文件已被 Hook {result.details['hook_modified']} 修改（如格式化），模型再次编辑前会重新读取。",
                style=PARAMETER_COLOR,
            )
        self.transcript = text
        self.update(text)


class MossCodeApp(TerminalApp[None]):
    TITLE = "MossCode"
    SUB_TITLE = "第四章 · Agent Loop"
    CSS = (
        TERMINAL_CSS
        + """
    Screen { background: $background; color: $foreground; padding: 0 2; overflow: hidden hidden; }
    Screen:inline { height: auto; border: none; }
    #banner { height: auto; margin: 1 0; }
    /* Each block in the scrollback ends with a blank row: that row is the gap above whatever is live, so nothing
       live adds one of its own at the top. */
    #chat { height: auto; max-height: 100vh; scrollbar-size: 1 0; }
    .entry { height: auto; padding: 0; margin-bottom: 1; background: $background; }
    .message-row { height: auto; }
    .user-entry .message-row, .user-entry .message-row > Static {
        background: USER_MESSAGE_BACKGROUND !important; color: USER_MESSAGE_COLOR !important;
    }
    .message-marker { width: 2; height: 1; text-style: bold; }
    .user-message, .response-content { width: 1fr; height: auto; }
    .thinking, .answer { height: auto; padding: 0; margin: 0; background: $background; }
    .thinking { height: auto; padding: 0 0 0 2; margin-bottom: 1; }
    .thinking-label { height: auto; margin-bottom: 1; color: MUTED_COLOR !important; }
    .turn-summary { height: auto; margin-top: 1; color: MUTED_COLOR !important; }
    #status { height: auto; margin-top: 0; color: $text-muted; }
    #queue { height: auto; margin-bottom: 1; padding: 0 1; }
    #todo-panel { height: auto; max-height: 6; margin-bottom: 1; padding: 0 1; }
    #attachments { height: auto; margin-bottom: 1; padding: 0 1; color: MUTED_COLOR !important; }
    .image-notes { height: auto; }
    #activity { height: 1; padding: 0 1; }
    #activity.idle { height: 0; }
    #input-row { height: auto; padding: 0 1; border-top: solid RULE_COLOR; border-bottom: solid RULE_COLOR; }
    #input-row.mode-plan { border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR; }
    #input-row.mode-acceptEdits { border-top: solid TOOL_SUCCESS_COLOR; border-bottom: solid TOOL_SUCCESS_COLOR; }
    #input-row.mode-bypassPermissions { border-top: solid ERROR_COLOR; border-bottom: solid ERROR_COLOR; }
    #input-prefix { width: 2; height: 1; color: BRAND_COLOR !important; text-style: bold; }
    #input-prefix.shell { color: OPERATION_COLOR !important; }
    #prompt { width: 1fr; height: auto; max-height: 8; border: none; padding: 0; background: $background; }
    #prompt > .text-area--placeholder { color: MUTED_COLOR !important; }
    #prompt:focus { border: none; background-tint: transparent; }
    #command-hints { height: auto; max-height: 7; border: none; padding: 0 1; }
    #command-hints > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    /* Grey like the status line below it; a blank row keeps the two apart. */
    #command-help { height: 1; margin-bottom: 1; color: MUTED_COLOR !important; padding: 0 1; }
    #model-panel { width: 1fr; height: auto; padding: 0 1; border-top: solid RULE_COLOR; border-bottom: solid RULE_COLOR; }
    #model-title, #model-help { height: 1; }
    #model-help, #approval-help, #question-help, #member-help { color: MUTED_COLOR !important; }
    #models { height: auto; max-height: 8; border: none; padding: 0; }
    #models > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    /* A card waiting for an answer takes the prompt's place, between two rules as the prompt is. */
    #approval-panel {
        height: auto; max-height: 16; padding: 0 1;
        border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR;
    }
    #approval-detail { height: auto; max-height: 20; }
    #approval-options { height: 4; margin-top: 1; border: none; padding: 0; }
    #approval-options > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #approval-help { height: auto; }
    #question-panel { height: auto; padding: 0 1; border-top: solid OPERATION_COLOR; border-bottom: solid OPERATION_COLOR; }
    #question-detail, #question-help { height: auto; }
    #question-options { height: auto; border: none; padding: 0; }
    #question-options > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    .local-note { height: auto; margin: 0 0 1 2; color: $text-muted; }
    .resumed-entry { height: auto; margin-bottom: 1; }
    .tool-entry { height: auto; margin-bottom: 1; }
    #member-panel { height: auto; padding: 0 1; }
    #member-compact { height: auto; }
    #member-list { height: auto; max-height: 8; border: none; padding: 0; }
    #member-list > .option-list--option-highlighted { color: COMMAND_HIGHLIGHT_COLOR !important; text-style: bold; }
    #member-help { height: 1; }
    #member-view { height: auto; padding: 0 1; }
    #member-head { height: 1; }
    #member-log { height: 1fr; scrollbar-size: 1 0; background: $background; }
    #member-running { height: auto; max-height: 6; }
    #member-view-help { height: 1; }
    /* A long list scrolls with a thin bar in the rules' grey; the theme's would be a block of near-black that
       stands out against the terminal's own background. */
    OptionList {
        background: $background; scrollbar-size-vertical: 1;
        scrollbar-color: RULE_COLOR; scrollbar-color-hover: MUTED_COLOR; scrollbar-color-active: MUTED_COLOR;
    }
    """.replace("COMMAND_HIGHLIGHT_COLOR", COMMAND_HIGHLIGHT_COLOR)
        .replace("PARAMETER_COLOR", PARAMETER_COLOR)
        .replace("USER_MESSAGE_BACKGROUND", USER_MESSAGE_BACKGROUND)
        .replace("USER_MESSAGE_COLOR", USER_MESSAGE_COLOR)
        .replace("OPERATION_COLOR", OPERATION_COLOR)
        .replace("TOOL_SUCCESS_COLOR", TOOL_SUCCESS_COLOR)
        .replace("ERROR_COLOR", ERROR_COLOR)
        .replace("BRAND_COLOR", BRAND_COLOR)
        .replace("MUTED_COLOR", MUTED_COLOR)
        .replace("RULE_COLOR", RULE_COLOR)
    )
    BINDINGS = [
        Binding("escape", "cancel", "取消生成", priority=True, show=False),
        Binding("ctrl+c", "cancel_task", "取消任务", priority=True, show=False),
        Binding("ctrl+q", "quit", "退出", priority=True, show=False),
        Binding("up", "move_hint(-1)", priority=True, show=False),
        Binding("down", "move_hint(1)", priority=True, show=False),
        Binding("tab", "complete_hint", priority=True, show=False),
        Binding("shift+tab", "cycle_mode", priority=True, show=False),
        Binding("ctrl+b", "background", priority=True, show=False),
        Binding("ctrl+t", "members", priority=True, show=False),
        Binding("enter", "complete_hint(False)", priority=True, show=False),
    ]

    def __init__(
        self,
        conversation: Conversation,
        provider_name: str,
        *,
        settings: Settings | None = None,
        model: str = "",
        provider_factory=None,
        resume: str | None = None,
        commands: CommandRegistry | None = None,
        initial_prompt: str | None = None,
    ) -> None:
        super().__init__()
        self.conversation = conversation
        # --resume ID / --continue restore at startup; "" opens the session list once started.
        self._resume = resume
        # `moss "..."` sends this once startup is done, as if typed.
        self._initial_prompt = initial_prompt
        self._panel_rows = 0
        self._pick = None  # The open picker's on_pick (CommandUI.choose).
        self._on_cancel = None  # Its on_cancel, run by Esc.
        self._hooks_confirmed = False  # Asked once at startup, before session_start.
        # A table of this run's own: skill commands are added here and replaced by /skills reload.
        self.commands = commands if commands is not None else BUILTIN.with_skills(conversation.skills)[0]
        self.provider_name = self._banner_provider = provider_name
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
        self._stage_note = ""  # What the running task is doing, shown on the line above the prompt.
        self._activity_shown: tuple | None = None
        # The rows of the latest main-conversation tool calls, oldest first; each knows how its call ended.
        self._recent_tools: dict[ToolEntry, None] = {}
        self._mcp_notices: list[str] = []  # MCP servers that settled while a task was running.
        self._retry_notice = ""  # Held until the next event so the ticker does not hide it.
        self._hint_until = 0.0  # Short notes shown over the running status until this time.
        self._escape_at = 0.0  # The last Esc on an empty, idle prompt: a second one soon after opens /rewind.
        self._hint_choices: list[Hint] = []
        # Per @ mention hint: where the cursor goes after it, as an index into the prompt's text.
        self._mention_cursors: list[int] = []
        self._file_index: FileIndex | None = None
        self._indexing = False
        self._dismissed_prompt: str | None = None
        self._scrollback_lock = asyncio.Lock()
        self._history: list = []  # Everything committed to scrollback, re-rendered after a width change.
        self._width: int | None = None
        self._redraw_timer = None
        self._approval_request: ApprovalRequest | None = None
        self._approval_text = Text()  # What the card on screen says above its choices.
        self._approval_queue: list[ApprovalRequest] = []  # Shown one at a time, in arrival order.
        self._question: QuestionRequest | None = None  # ask_user's questions on screen.
        self._question_waiting: QuestionRequest | None = None  # Arrived while a member's approval card was up.
        self._question_step = 0
        self._question_answers: list[Answer | None] = []
        self._question_checked: set[int] = set()
        self._question_typing = False  # The answer is being typed in the prompt ("其他").
        self._question_draft = ""  # The prompt's text before typing an answer; put back afterwards.
        self._todo_shown: tuple | None = None  # The checklist the panel shows; None when hidden.
        self._todo_dismissed: tuple = ()  # A finished list the user has moved on from (sent a message since).
        self._startup_task = None
        self._compacting: float | None = None  # Start time while a compaction runs.
        self._compact_row: CompactRow | None = None
        self._shell_row: ShellRow | None = None  # While a ! command runs.
        # (estimate, window, percent of the effective window, near the compaction threshold)
        self._context_share: tuple[str, str, int, bool] | None = None
        self._agent_rows: dict[str, ToolEntry] = {}  # Agent tool rows by call id.
        self._notify_waiting = False  # A notification turn waits for the running task.
        self._goal_checking = False  # The /goal evaluation after a turn is running as _reply_task.
        self._announce = Announcer(self._task_note)
        # The member panel's rows, read off the loop, and the member view.
        self._member_rows: list[MemberRow] = []
        self._members_task: asyncio.Task | None = None
        self._members_again = False
        self._member_ticks = 0
        self._stop_armed: tuple[str, float] | None = None  # The member the first x picked, until when.
        self._view_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat"):
            yield SessionInfo(startup_banner(self.provider_name), id="banner")
        view = MemberView(id="member-view")
        view.display = False
        yield view
        todo = Static("", id="todo-panel", markup=False)
        todo.display = False
        yield todo
        queue = Static("", id="queue", markup=False)
        queue.display = False
        yield queue
        attachments = Static("", id="attachments", markup=False)
        attachments.display = False
        yield attachments
        # The running stage, on the row above the prompt; it takes no row while idle, where the blank row that
        # ends the scrollback already sits above the prompt.
        yield Static("", id="activity", classes="idle", markup=False)
        with Horizontal(id="input-row"):
            yield Static("❯", id="input-prefix")
            prompt = PromptArea(placeholder="输入消息，或 / 查看命令；Shift+Enter 换行", id="prompt")
            prompt.take_queued = self._take_back_queued
            prompt.root = self._workspace_root
            yield prompt
        panel = ModelPanel(id="model-panel")
        panel.display = False
        yield panel
        with Vertical(id="approval-panel") as approval:
            approval.display = False
            yield Static("", id="approval-detail", markup=False)
            yield KeyboardOptions(*(choice_prompt(CHOICE_LABELS[choice]) for choice in CHOICES), id="approval-options")
            yield Static("↑ / ↓ 选择 · Enter 确认 · Esc 拒绝本次 · Ctrl+C 取消任务", id="approval-help", markup=False)
        with Vertical(id="question-panel") as question:
            question.display = False
            yield Static("", id="question-detail", markup=False)
            yield QuestionOptions(id="question-options")
            yield Static("", id="question-help", markup=False)
        hints = KeyboardOptions(id="command-hints")
        hints.can_focus = False
        hints.display = False
        yield hints
        help_line = Static("", id="command-help", markup=False)
        help_line.display = False
        yield help_line
        members = MemberPanel(id="member-panel")
        members.display = False
        yield members
        yield SessionInfo(Text(""), id="status")

    def on_mount(self) -> None:
        super().on_mount()
        if self.conversation.registry:
            self.conversation.registry.permissions.interactive = True
        self.conversation.on_memory = self._on_memory
        self.conversation.on_tasks = self._schedule_notify
        self.conversation.tasks.on_change = self._on_task_change
        teams = getattr(self.conversation, "teams", None)
        if teams is not None:  # Members ask and report while the lead runs or waits.
            teams.on_approval = self._member_approval
            teams.on_note = lambda text: self._closing or self.call_later(self._local_note, "└ " + text)
            teams.on_change = lambda: self._closing or self.call_later(self._members_changed)
            teams.on_withdraw = lambda request_id: self._closing or self.call_later(self._withdraw_approval, request_id)
            teams.on_commit = self._member_committed
        if self.conversation.context_root is not None:
            self.conversation.start_worktree_cleanup(
                lambda report: self.conversation._hook_notice(clean_text(report, automatic=True))
            )
        self.query_one("#prompt", PromptArea).focus()
        self._width = self.size.width
        self._status("")
        self.set_interval(0.2, self._tick)
        self.set_interval(0.5, self._member_tick)
        self.set_interval(0.5, self._refresh_todos)
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
        elif self._initial_prompt:
            self.call_later(self._send_initial)

    async def _send_initial(self) -> None:
        text, self._initial_prompt = self._initial_prompt, None
        if text and text.strip() and not self._closing:
            field = self.query_one("#prompt", PromptArea)
            await self.on_prompt_area_submitted(PromptArea.Submitted(field, text))

    async def _confirm_hooks(self) -> None:
        """Untrusted project hook files are confirmed before the first session_start."""
        if self._hooks_confirmed:
            return
        self._hooks_confirmed = True
        done = asyncio.get_running_loop().create_future()

        async def finished() -> None:
            if not done.done():
                done.set_result(None)

        await offer_trust(self, self.conversation.hooks, finished)
        await done

    async def _initialize(self):
        try:
            await self._confirm_hooks()
            for notice in await self.conversation.start(wait_mcp=False, on_mcp=self._on_mcp_update):
                await self._local_note("└ " + notice)
            await self._commit_ready_transcript()
        except Exception:
            await self._local_note("└ 启动初始化失败；请检查配置后重新启动。")
        finally:
            if not self._closing and not self._generating:
                self._status("")
        if self._initial_prompt and not self._closing:
            await self._send_initial()
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
            self._reply_task.add_done_callback(
                lambda _: self._closing or self.call_later(self._local_note, text, style)
            )
        else:
            self.call_later(self._local_note, text, style)
        self._usage = self.conversation.total_usage  # Extraction requests are a real cost.
        if not self._generating:
            self._status(self._status_note)

    # Sub-agents -------------------------------------------------------------------

    def _on_task_change(self, task) -> None:
        if self._closing:
            return
        self._announce(task)
        row = self._agent_rows.get(task.call_id) if task.call_id else None
        if row is not None:
            row.subagent = task
            row.progress(task)
        if not self._generating:
            self._status(self._status_note)

    def _task_note(self, text: str) -> None:
        """Like memory notes, a running task's output stays contiguous."""
        if self._task_running:
            self._reply_task.add_done_callback(
                lambda _: self._closing or self.call_later(self._local_note, "└ " + text)
            )
        else:
            self.call_later(self._local_note, "└ " + text)

    def _schedule_notify(self) -> None:
        """Background results wait while the main agent is busy, then start a turn of their own."""
        if self._closing:
            return
        if not self._task_running:
            self.call_later(self._start_notify)
        elif not self._notify_waiting:
            self._notify_waiting = True
            self._reply_task.add_done_callback(self._notify_after)

    def _notify_after(self, _) -> None:
        self._notify_waiting = False
        if not self._closing:
            self.call_later(self._start_notify)

    async def _start_notify(self) -> None:
        if self._closing or not self.conversation.notifications:
            return
        if self._task_running:
            self._schedule_notify()
            return
        await self._start_request("", show_user=False, notify=True)

    def action_background(self) -> None:
        """Ctrl+B moves every foreground command and sub-agent to the background; Esc still cancels."""
        commands = getattr(self.conversation, "commands", None)
        moved = commands.detach_foreground() if commands is not None else 0
        count = self.conversation.tasks.detach_foreground()
        parts = [f"{moved} 个命令" if moved else "", f"{count} 个子 Agent" if count else ""]
        self._hint_until = monotonic() + 2
        if moved or count:
            moved_text = "和".join(part for part in parts if part)
            # A space only after a Latin word: "1 个子 Agent 转入后台" but "1 个命令转入后台".
            self._status(f"已把 {moved_text}{' ' if moved_text[-1].isascii() else ''}转入后台")
        else:
            self._status("没有在前台运行的命令或子 Agent")

    async def action_quit(self) -> None:
        async def leave() -> None:
            self.exit()

        await confirm_tasks(self, self.conversation, leave)

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
        reserved = self.screen.styles.gutter.height + max(1, self.query_one("#status").outer_size.height)
        if input_row.display:  # Hidden while a member view is open or a card stands in its place.
            # Its two rules and the prompt.
            reserved += max(2 + prompt_rows, input_row.region.height) + input_row.styles.margin.height
        activity = self.query_one("#activity")
        if activity.display and not activity.has_class("idle"):  # The running stage, above prompt or card.
            reserved += 1
        members = self.query_one("#member-panel", MemberPanel)
        if members.display:
            reserved += members.lines
        queue = self.query_one("#queue")
        if queue.display:
            reserved += 1 + min(4, len(self.conversation.queued))
        todo = self.query_one("#todo-panel")
        if todo.display:
            reserved += 1 + (
                1 if self._todo_shown is None else min(6, len(str(todo_panel(self._todo_shown)).splitlines()))
            )
        if self.query_one("#model-panel").display:
            reserved += 4 + min(8, self._panel_rows)
        if self.query_one("#approval-panel").display:
            # Keep the choices and status reachable in a short terminal; only
            # the preview shrinks. Completed history already lives in scrollback.
            # Two rules, the blank row above the choices, one row per choice and up to two rows of help.
            fixed = 5 + (len(self._approval_request.choices) if self._approval_request else 4)
            limit = max(fixed, self.size.height - reserved - 1)
            self.query_one("#approval-panel").styles.max_height = limit
            self.query_one("#approval-detail").styles.max_height = min(20, max(1, limit - fixed))
            # Its current size may predate the new preview, so its rows are counted from the text. Only what it
            # needs is reserved: the tool row it asks about stays in sight above it.
            width = max(10, self.size.width - self.screen.styles.gutter.width - 2)
            preview = len(self._approval_text.wrap(self.console, width))
            reserved += min(limit, fixed + min(20, preview))
        if self.query_one("#question-panel").display and self._question is not None:
            question = self._question.questions[self._question_step]
            width = max(10, self.size.width - 2)
            # Two rules, heading, the wrapped question, one row per option and "其他", help.
            reserved += 4 + -(-cell_len(question.question) // width) + len(question.options) + 1
        hints = self.query_one("#command-hints")
        hint_limit = min(8, max(1, self.size.height - reserved - 3))
        if hints.styles.max_height is None or hints.styles.max_height.value != hint_limit:
            hints.styles.max_height = hint_limit
        # Reserve hints before layout: their old size may still be zero on show.
        hint_rows = min(len(self._hint_choices), hint_limit)
        hints.styles.height = max(1, hint_rows)
        if hints.display:  # The list, its key help and the blank row below that.
            reserved += hint_rows + 2
        chat = self.query_one("#chat")
        available = max(1, self.size.height - reserved)
        view = self.query_one("#member-view", MemberView)
        if view.display:  # The whole height but the status line (and a card).
            height = max(3, int(available * VIEW_SHARE))
            if view.styles.height is None or view.styles.height.value != height:
                view.styles.height = height
            return
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

    def _stage(self, note: str) -> None:
        """What the running task is doing, on the line above the prompt. A short hint keeps its place in
        the status bar until it expires; with no task running the note is an ordinary status note."""
        if not self._generating:
            self._status(note)
            return
        self._stage_note = note
        self._status(self._status_note if monotonic() < self._hint_until else "")

    def _paint_activity(self) -> None:
        activity = self.query("#activity")
        if not activity:
            return
        if not self._generating:
            self._stage_note = ""
        if activity.first().has_class("idle") == self._generating:
            activity.set_class(not self._generating, "idle")
            self.call_later(self._fit_chat)  # One row more or less above the prompt.
        now = monotonic()
        line = activity.first(Static)
        width = line.content_size.width
        states = tuple(row.state for row in self._recent_tools)
        shown = (self._stage_note, int(now * 5) if self._stage_note else 0, states, width)
        if shown != self._activity_shown:
            self._activity_shown = shown
            line.update(activity_row(self._stage_note, now, states, width) if self._stage_note else Text(""))

    def _record_tool(self, row: ToolEntry) -> None:
        """Keep a finished call's row for the activity line; a call reported twice keeps its place."""
        self._recent_tools[row] = None
        while len(self._recent_tools) > RECENT_TOOLS:
            del self._recent_tools[next(iter(self._recent_tools))]

    def _status(self, note: str) -> None:
        self._status_note = note
        self._paint_activity()
        muted = MUTED_COLOR
        text = Text()
        registry = self.conversation.registry
        mode = registry.permissions.mode if registry is not None else None
        for row in self.query("#input-row"):
            for name in MODE_COLORS:
                row.set_class(name == mode, f"mode-{name}")
        if registry is None:
            text.append("未启用工具", style="bold")
        else:
            color = MODE_COLORS.get(mode)
            text.append(MODE_BADGES[mode], style=f"bold {color}" if color else "bold")
            if getattr(self.conversation, "coordinator", False):
                text.append(" " + COORDINATOR_BADGE, style=f"bold {OPERATION_COLOR}")
            if badge := sandbox_badge():
                text.append(" · ", style=muted)
                # Partial confines writes only: amber, like an operation still awaiting approval.
                colors = {"沙箱": TOOL_SUCCESS_COLOR, "沙箱·部分": OPERATION_COLOR}
                text.append(badge, style=colors.get(badge, ERROR_COLOR))
            if not self._generating and self.size.width >= 80:
                text.append(" (Shift+Tab 切换)", style=muted)
        if self.provider_name != self._banner_provider:
            # The banner names the model; after /model it is stale, so the status bar names the new one.
            text.append(" · " + self.provider_name, style=muted)
        if (thinking := getattr(self.conversation, "thinking", None)) is not None:
            unsupported = self.conversation.thinking_problem() is not None
            text.append(f" · 思考 {thinking}" + ("（不支持）" if unsupported else ""), style=muted)
        if (goal := getattr(self.conversation, "goal", None)) is not None:
            if goal.paused:
                text.append(" · ◎ 目标已暂停", style=OPERATION_COLOR)
            else:
                text.append(f" · ◎ 目标 {elapsed_text(monotonic() - goal.started)}", style=TOOL_SUCCESS_COLOR)
        if note:
            stage, separator, rest = note.partition(" · ")
            stage_color = (
                ERROR_COLOR
                if stage.startswith("失败")
                else OPERATION_COLOR
                if self._generating or stage.startswith(("已取消", "已停止"))
                else None
            )
            text.append(" · ", style=muted)
            text.append(stage, style=stage_color)
            if separator:
                text.append(separator + rest, style=muted)
        manager = self.conversation.mcp_manager
        if manager is not None and manager.pending:
            total = len(manager.connections)
            text.append(f" · MCP 连接中 {total - manager.pending}/{total}", style=OPERATION_COLOR)
        queued = len(self.conversation.queued)
        if queued:
            text.append(f" · 已排队 {queued} 条", style=OPERATION_COLOR)
        tasks = self.conversation.tasks
        background = len(tasks.background())
        if background:
            text.append(f" · 后台任务 {background}", style=OPERATION_COLOR)
        commands = getattr(self.conversation, "commands", None)
        if any(not task.background and task.origin == "agent" for task in tasks.running()) or (
            commands is not None and commands.foreground_count()
        ):
            text.append(" · Ctrl+B 转入后台", style=OPERATION_COLOR)
        teams = getattr(self.conversation, "teams", None)
        if teams is not None and teams.current is not None:
            running, idle = teams.counts()
            text.append(f" · 团队 {teams.current.name} 运行 {running} / 空闲 {idle}", style=muted)
            asking = sum(self._asking().values())  # Pane members' too.
            if asking:
                text.append(f" · 成员审批 {asking}", style=OPERATION_COLOR)
            if (
                self.query("#member-panel")
                and self.query_one("#member-panel").display
                and not self.query_one("#member-panel", MemberPanel).expanded
            ):
                text.append(" · Ctrl+T 成员", style=muted)
        usage = Text()
        if self._usage.requests:
            if self._context_share is not None:
                used, window, percent, near = self._context_share
                usage.append(f"上下文 约 {used} / {window}（{percent}%）", style=OPERATION_COLOR if near else muted)
            if (rate := cache_hit_rate(self._usage)) is not None:
                usage.append((" · " if usage else "") + f"缓存命中 {rate}%", style=muted)
        if usage:
            # The numbers sit at the right edge when the line has room, and follow the rest when it does not.
            gap = self.size.width - self.screen.styles.gutter.width - text.cell_len - usage.cell_len
            text.append(" " * gap if gap >= 2 else " · ", style=muted)
            text.append_text(usage)
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
        self._context_share = (
            approx_tokens(estimate),
            tokens_label(budget.window),
            round(100 * estimate / budget.effective),
            estimate >= 0.8 * budget.auto_threshold,
        )

    def _tick(self) -> None:
        if self._closing or not self.query("#status"):
            return  # A queued timer can run after the screen's widgets have unmounted.
        self._paint_activity()  # Keeps the spinner turning in stages that have nothing else to refresh.
        if self._approval_request is not None and not self._waiting(self._approval_request):
            # Withdrawn without an answer, e.g. a sandboxed command ended while asking to connect.
            self._hide_approval()
        if self._question is not None and not self._waiting(self._question):
            self._hide_question()  # The turn was cancelled while it waited.
        if self._generating and self._shell_row is not None:
            self._shell_row.tick()
            if self._approval_request is not None:  # A sandboxed command asking to connect.
                self._stage("等待授权 · Esc 拒绝本次 · Ctrl+C 取消任务")
                return
            self._stage(f"执行命令中 · {monotonic() - self._shell_row.started:.1f}s · Esc 取消")
            return
        if self._generating and self._goal_checking:
            self._stage(f"判断目标是否达成 · {monotonic() - self.started:.1f}s · Esc 暂停目标")
            return
        if self._generating and self._compacting is not None:
            if self._compact_row is not None:
                self._compact_row.tick()
            self._stage(f"压缩上下文中 · {monotonic() - self._compacting:.1f}s · Esc 取消")
            return
        if self._generating and self._active_reply is not None:
            if self._approval_request is not None:
                self._stage("等待授权 · Esc 拒绝本次 · Ctrl+C 取消任务")
                return
            if self._question is not None:
                self._stage(
                    "等待回答 · 输入回答后 Enter 提交 · Esc 返回选项"
                    if self._question_typing
                    else "等待回答 · Esc 不回答 · Ctrl+C 取消任务"
                )
                return
            if self._retry_notice:
                self._stage(self._retry_notice)
                return
            self._active_reply.refresh_timer()
            for row in self._agent_rows.values():
                if row.subagent is not None:
                    row.progress(row.subagent)
            label = {"waiting": "连接中", "thinking": "思考中", "streaming": "输出中", "tools": "执行工具中"}.get(
                self._active_reply.phase, "处理中"
            )
            self._stage(
                f"{label} · {self._iteration}/{self._iteration_limit} 轮 · {monotonic() - self.started:.1f}s · Esc 取消"
            )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "prompt":
            value = event.text_area.text
            # Esc suppresses this edit only, not a matching command typed later.
            if value != self._dismissed_prompt:
                self._dismissed_prompt = None
            self._refresh_hints(value)
            self._refresh_attachments()
            shell = value.lstrip().startswith("!")
            prefix = self.query_one("#input-prefix", Static)
            if prefix.has_class("shell") != shell:
                prefix.set_class(shell, "shell")
                prefix.update("!" if shell else "❯")

    def _workspace_root(self) -> Path:
        registry = getattr(self.conversation, "registry", None)
        return registry.permissions.root if registry is not None else Path.cwd()

    def _image_notes(self, text: str) -> list[str]:
        return self.query_one("#prompt", PromptArea).image_notes(text)

    def _refresh_attachments(self) -> None:
        field = self.query_one("#prompt", PromptArea)
        notes = field.image_notes(field.text)
        panel = self.query_one("#attachments", Static)
        panel.display = bool(notes)
        if notes:
            panel.update(Text("\n".join(notes) + "\n删除占位符即取消该图片", style=MUTED_COLOR))

    def _hint(self, text: str) -> None:
        self._hint_until = monotonic() + 2
        self._status(text)

    def on_prompt_area_image_paste(self, event: PromptArea.ImagePaste) -> None:
        if not getattr(getattr(self.conversation.provider, "config", None), "accepts_images", True):
            # Ctrl+V still pastes text and dragged-in paths stay as text; only an explicit Alt+V or a drag says why.
            if event.otherwise is not None:
                event.otherwise()
            if event.otherwise is None or event.files is not None:
                self._hint(f"当前模型不支持图片（providers.{self.provider_name}.images）")
            return
        # The clipboard owner can stall; reading it must not hold up the interface.
        self.run_worker(self._attach_images(event), group="image-paste")

    async def _attach_images(self, event: PromptArea.ImagePaste) -> None:
        field = event.text_area
        try:
            if event.files is None:
                self._hint("正在读取剪贴板…")
                content = await read_clipboard_async()
            else:
                content = event.files
            if isinstance(content, list):
                content = [path for path in content if await asyncio.to_thread(is_image_file, path)] or None
            if content is None:
                if event.otherwise is not None:
                    event.otherwise()
                else:
                    self._hint("剪贴板中没有图片")
                return
            if isinstance(content, bytes):
                items = [Attachment(await asyncio.to_thread(load_image, content), "剪贴板")]
            else:
                items = [Attachment(await asyncio.to_thread(load_image, path), path.name) for path in content]
        except (ClipboardError, ImageError, OSError) as exc:
            self._hint(f"无法粘贴图片：{exc}")
            return
        field.attach(items)
        self._hint(f"已添加 {len(items)} 张图片" if len(items) > 1 else "已添加图片")

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
            text.append("\n  将在本轮工具执行后发送给模型 · ↑ 取回编辑 · Esc 取消任务时放回输入框", style=MUTED_COLOR)
            queue.update(text)
        self._status(self._status_note)

    def _refresh_hints(self, value: str) -> None:
        hints = self.query_one("#command-hints", OptionList)
        help_line = self.query_one("#command-help", Static)
        previous = (
            self._hint_choices[hints.highlighted][0]
            if hints.highlighted is not None and hints.highlighted < len(self._hint_choices)
            else None
        )
        recalled = self.query_one("#prompt", PromptArea).recalled
        self._hint_choices = (
            [] if recalled or value == self._dismissed_prompt else self.commands.matches(self._command_context(), value)
        )
        self._mention_cursors = []
        if not self._hint_choices and not recalled and value != self._dismissed_prompt:
            self._hint_choices, self._mention_cursors = self._mention_hints(value)
        hints.clear_options()
        width = min(24, max(10, (self.size.width - 8) // 2))
        if self._mention_cursors:
            width = min(72, max(10, self.size.width - 16))  # Paths need the room more than the description.
        for hint in self._hint_choices:
            row = Table.grid(padding=(0, 2), expand=True)
            row.add_column(width=width, no_wrap=True, overflow="ellipsis")
            row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            row.add_row(Text(hint.label or hint.value), Text(hint.description, style=MUTED_COLOR))
            hints.add_option(row)
        hints.display = bool(self._hint_choices)
        help_line.display = hints.display
        if self._hint_choices:
            hints.highlighted = next((i for i, choice in enumerate(self._hint_choices) if choice[0] == previous), 0)
            help_line.update("↑ / ↓ 选择 · Tab 补全 · Enter 确认 · Esc 收起")
        self.call_later(self._fit_chat)

    def _mention_hints(self, value: str) -> tuple[list[Hint], list[int]]:
        """Files and directories for the @ mention being typed before the cursor."""
        field = self.query_one("#prompt", PromptArea)
        if "@" not in value or value != field.text or value.lstrip().startswith("!"):
            return [], []  # A ! command's @ is the shell's.
        cursor = field.document.get_index_from_location(field.cursor_location)
        before, after = value[:cursor], value[cursor:]
        typed = partial_mention(before)
        if typed is None:
            return [], []
        index = self._mention_index()
        if index is None:
            return [], []
        start, query = typed
        if query in index.paths and not query.endswith("/"):
            return [], []  # Already a whole file's path: nothing left to choose.
        if before[start + 1 : start + 2] == '"' and after[:1] == '"':
            after = after[1:]  # The closing quote of the mention being replaced.
        hints, cursors = [], []
        for path in index.candidates(query):
            directory = path.endswith("/")
            inserted = "@" + quoted(path) + ("" if directory or after[:1].isspace() else " ")
            hints.append(Hint(before[:start] + inserted + after, "目录" if directory else "", path))
            # Inside a quoted directory the cursor stays before the quote, so its entries come next.
            cursors.append(start + len(inserted) - (1 if directory and inserted.endswith('"') else 0))
        return hints, cursors

    def _mention_index(self) -> FileIndex | None:
        root = self._workspace_root()
        if self._file_index is None or self._file_index.root != root:
            self._file_index = FileIndex(root)
        index = self._file_index
        if index.stale and not self._indexing:
            self._indexing = True
            self.run_worker(lambda: self._load_index(index), thread=True, group="file-index")
        return index if index.loaded_at is not None else None

    def _load_index(self, index: FileIndex) -> None:
        try:
            index.load()
        finally:
            self._indexing = False
        try:
            self.call_from_thread(self._index_loaded)
        except RuntimeError:  # The app closed meanwhile.
            pass

    def _index_loaded(self) -> None:
        try:
            field = self.query_one("#prompt", PromptArea)
        except NoMatches:
            return
        if "@" in field.text:
            self._refresh_hints(field.text)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        # Moving the cursor onto or off a mention changes what the menu offers, without any edit.
        if event.text_area.id == "prompt" and "@" in event.text_area.text:
            self._refresh_hints(event.text_area.text)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"move_hint", "complete_hint"}:
            field = self.focused
            if not isinstance(field, PromptArea) or field.id != "prompt" or field.disabled:
                return False
            if not self._hint_choices:
                # After Esc closed the menu, Tab still completes a unique command prefix.
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
        elif self._mention_cursors:
            completion, cursor = self._hint_choices[hints.highlighted].value, self._mention_cursors[hints.highlighted]
            field = self.query_one("#prompt", PromptArea)
            self._dismissed_prompt = None  # A directory goes on to its own entries.
            field.value = completion
            field.move_cursor(field.document.get_location_from_index(cursor))
            self._refresh_hints(completion)
            return
        else:
            completion = self._hint_choices[hints.highlighted].value
        # Tab continues into the argument candidates of commands that have them.
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
        if self._question is not None and self._question_typing:
            if prompt.strip():
                question = self._question.questions[self._question_step]
                checked = tuple(question.options[i][0] for i in sorted(self._question_checked))
                self._next_question(Answer(checked, prompt.strip()))
            return
        parsed = parse_input(prompt, self.commands)
        if parsed.kind == "empty":
            return
        running_ok = (
            parsed.kind == "command"
            and parsed.command is not None
            and parsed.command.runs_while_running(parsed.argument)
        )
        if parsed.kind == "shell" and not parsed.text:
            self._hint_until = monotonic() + 2
            self._status("在 ! 后面输入要执行的命令；!! 开头只执行，不加入对话")
            return
        if self._task_running and not running_ok:
            if parsed.kind == "shell":
                self._hint_until = monotonic() + 2
                self._status("运行中不能执行 ! 命令，请在完成后发送 · Esc 取消")
                return
            if parsed.kind != "message":
                # Commands change state the running task depends on; keep the text for later.
                self._hint_until = monotonic() + 2
                self._status(
                    "运行中只能排队消息和使用 /tasks、/todo、/goal、/status、/worktree list，其他命令请在完成后发送 · Esc 取消"
                )
                return
            field.remember(prompt)
            field.clear()
            self.conversation.queue(parsed.text, field.images_in(parsed.text))
            self._refresh_queue()
            return
        field.remember(prompt)
        field.clear()
        self._dismissed_prompt = None
        self._refresh_hints("")
        if parsed.kind == "shell":
            await self._start_shell(parsed.text, keep=parsed.word == "!")
            return
        if parsed.kind != "message":
            await self._command(prompt.strip(), parsed)
            return
        await self._start_request(parsed.text, images=field.images_in(parsed.text))

    async def _start_request(
        self,
        prompt: str,
        *,
        show_user: bool = True,
        shown: tuple[str, ...] = (),
        command: str | None = None,
        fork=None,
        notify: bool = False,
        images: dict[str, ProcessedImage] | None = None,
        synthetic: str | None = None,
    ) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        reply = ChatEntry("MossCode")
        self._active_reply = reply
        if show_user:
            for text in shown or (prompt,):
                await chat.mount(ChatEntry("You", text, self._image_notes(text)))
        await chat.mount(reply)
        chat.anchor()
        self.started = monotonic()
        self._iteration = 0
        self._generating = True
        self._stage("连接中…")
        await self._commit_ready_transcript()
        self._reply_task = asyncio.create_task(self._respond(prompt, reply, command, fork, notify, images, synthetic))

    async def _start_shell(self, command: str, *, keep: bool) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        self._shell_row = ShellRow(command, keep)
        await chat.mount(self._shell_row)
        chat.anchor()
        self._generating = True
        self._tick()
        self._reply_task = asyncio.create_task(self._run_shell(command, keep))

    async def _run_shell(self, command: str, keep: bool) -> None:
        row = self._shell_row
        try:
            row.finish(await self.conversation.run_command(command, keep=keep))
        except asyncio.CancelledError:
            if not self._closing:
                row.stop("已取消 · 未加入对话")
        except ProviderError as exc:
            row.stop(f"未执行 · {exc}")
        finally:
            self._generating = False
            self._shell_row = None
            if not self._closing:
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                queued = self.conversation.take_queued()
                if queued:
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                self._refresh_queue()
                self._refresh_context()
                self._status("")

    @staticmethod
    def _note(text: str, style: str | None = None) -> Static:
        note = Static(Text(text, style=style or ""), classes="local-note")
        note.transcript = Text(text, style=style or "")
        return note

    async def _local_note(self, text: str, style: str | None = None) -> None:
        if self._closing or self._exit:  # A note queued just before exit (e.g. a task cancelled on exit) is dropped.
            return
        try:
            chat = self.query_one("#chat", VerticalScroll)
        except NoMatches:
            return
        if not chat.is_attached:
            return
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
        self._recent_tools.clear()  # They belonged to the history that is gone from the screen.
        if not self.is_inline:
            return
        async with self._scrollback_lock:
            self._history = [startup_banner(self.provider_name)]
            self._banner_provider = self.provider_name
            with self.batch_update():
                redraw_transcript(self, self._history)

    def _start_resume(self, session_id: str, *, startup: bool = False) -> None:
        self.started = monotonic()
        self._generating = True
        self._stage("恢复会话中…")
        self._reply_task = asyncio.create_task(self._run_resume(session_id, startup))

    async def _show_resumed(self, event: Resumed) -> None:
        restored = event.restored
        self._usage = self.conversation.total_usage
        await self._clear_screen()
        when = restored.last_time.strftime("%Y-%m-%d %H:%M") if restored.last_time else "未知"
        await self._local_note(f"└ 已恢复会话 · {restored.title} · {event.messages} 条消息 · 上次活动 {when}")
        await self._show_history(restored.history)
        for notice in event.notices:
            await self._local_note("└ " + notice)

    async def _show_history(self, history) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        for renderable in resumed_rows(history, shell_label(self.conversation.registry)):
            row = Static(renderable, classes="resumed-entry")
            row.transcript = renderable
            await chat.mount(row)
        chat.anchor()

    async def _run_resume(self, session_id: str, startup: bool = False) -> None:
        unanswered: tuple[str, ...] = ()
        try:
            if startup:
                await self._confirm_hooks()
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
                # Unanswered messages come back for the user to edit or resend.
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
        self._show_members()  # Hidden while the model panel is open.
        self.call_later(self._fit_chat)

    async def _command(self, typed: str, parsed: Parsed) -> None:
        """Echo what was typed, then run it; prompt commands echo the command, not the template."""
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

    # CommandUI -------------------------------------------------------

    async def note(self, text: str, style: str | None = None) -> None:
        await self._local_note(text, style)

    async def submit(self, prompt: str, *, command: str | None = None, fork=None, synthetic: str | None = None) -> None:
        if synthetic is not None and self._task_running:
            return  # A resumed goal continues when the running turn ends.
        await self._start_request(prompt, show_user=False, command=command, fork=fork, synthetic=synthetic)

    def refresh_status(self) -> None:
        self._usage = self.conversation.total_usage
        self._refresh_context()
        self._status("")

    async def clear_screen(self) -> None:
        await self._clear_screen()

    def choose(self, title: str, options, current: int, on_pick, on_cancel=None) -> None:
        picker = self.query_one("#models", OptionList)
        picker.clear_options()
        picker.add_options(list(options))
        self._pick, self._on_cancel = on_pick, on_cancel
        self._show_panel(title, len(options))
        picker.highlighted = current

    async def compact(self, instructions: str) -> None:
        await self._start_compact(instructions)

    def resume(self, session_id: str) -> None:
        self._start_resume(session_id)

    async def rewound(self, message: Message) -> None:
        await self._clear_screen()
        await self._show_history(self.conversation.history)
        images = {}
        for block in message.content:
            if block.kind == "image" and block.image is not None and IMAGE_MARK.fullmatch(block.image.alt):
                try:
                    images[block.image.alt] = rewound_image(block.image)
                except OSError:
                    continue  # Its file is gone: the placeholder stays as text.
        if message.command is not None:
            images = {}
        self.query_one("#prompt", PromptArea).restore_message(message.shown, images)
        self._refresh_context()

    def _open_rewind(self) -> None:
        self.run_worker(dispatch(self._command_context(), parse_input("/rewind", self.commands)), group="rewind")

    async def switch_model(self, name: str) -> None:
        config = next(item for item in self.settings.providers if item.name == name)
        if not config.follow:
            await self._switch_model(config)
            return
        # Read again: the tool it follows may have been switched (in CC Switch, say) since MossCode started.
        try:
            config = refreshed(config)
        except ConfigError as exc:
            await self._local_note(f"切换失败：{exc}")
            return
        from .credentials import remember

        remember(config.api_key)  # A key switched to in that tool since startup is kept out of output too.
        providers = tuple(config if item.name == name else item for item in self.settings.providers)
        self.settings = replace(self.settings, providers=providers)
        self.conversation.provider_configs = providers  # Skills and sub-agents that name it get this one.
        await self._switch_model(config, again=config.name == self.provider_name)

    async def _switch_model(self, config: ProviderConfig, *, again: bool = False) -> None:
        """again: the current provider, read again from the tool it follows."""
        if config.name == self.provider_name and not again:
            await self._local_note(f"└ 已在使用 {config.name}（{config.model}）。")
            return
        try:
            provider = self.provider_factory(config)
            await self.conversation.set_provider(
                provider, config.budget(), (config.name, config.protocol, config.model)
            )
        except (ProviderError, ConfigError) as exc:
            await self._local_note(f"切换失败：{exc}")
            return
        self.provider_name, self.model = config.name, config.model
        if self.query("#banner"):  # Not yet committed to the scrollback: it can still change.
            self.query_one("#banner", Static).update(startup_banner(self.provider_name))
            self._banner_provider = self.provider_name
        self._refresh_context()
        self._status("")
        if again:
            await self._local_note(
                f"└ 已重新读取 {FOLLOWED[config.follow]} 的设置：{config.name}"
                f"（{config.protocol} / {config.model}）；对话历史保留。"
            )
        else:
            await self._local_note(f"└ 已切换到 {config.name}（{config.protocol} / {config.model}）；对话历史保留。")
        if note := config.window_note():
            await self._local_note(f"└ {note}")
        if (problem := self.conversation.thinking_problem()) is not None:
            await self._local_note(
                f"└ 思考强度 {self.conversation.thinking}：{config.name} 不支持（{problem}），这个模型的请求按配置发送。"
            )

    def _close_model_panel(self) -> None:
        self._pick = self._on_cancel = None
        self.query_one("#model-panel").display = False
        self.query_one("#prompt", PromptArea).focus()
        self._show_members()
        self.call_later(self._fit_chat)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "models":
            pick = self._pick
            self._close_model_panel()
            if pick is not None:
                await pick(event.option_index)
        elif event.option_list.id == "approval-options" and self._approval_request:
            self._answer_approval(self._approval_request.choices[event.option_index])
        elif event.option_list.id == "question-options" and self._question is not None:
            self._pick_question(event.option_index)
        elif event.option_list.id == "member-list":  # Enter opens the member view.
            rows = self.query_one("#member-panel", MemberPanel).rows
            if event.option_index < len(rows):
                await self._open_view(rows[event.option_index].name)

    def _waiting(self, request: ApprovalRequest) -> bool:
        registry = self.conversation.registry
        future = registry.permissions.pending.get(request.id) if registry is not None else None
        return future is not None and not future.done()

    def _member_approval(self, request: ApprovalRequest) -> None:
        """A member's request; once the window is closing there is no card to show, so it is refused."""
        if self._closing or not self.query("#approval-panel"):
            teams = getattr(self.conversation, "teams", None)
            if teams is not None and request.id in teams.forwarded:
                return  # A pane member still asks in its own window.
            if self.conversation.registry is not None:
                self.conversation.registry.permissions.respond(request.id, "deny")
            return
        self._show_approval(request)

    def _show_approval(self, request: ApprovalRequest) -> None:
        if self._question is not None:
            if all(item.id != request.id for item in self._approval_queue):
                self._approval_queue.append(request)  # After the questions.
            return
        if self._approval_request is not None and self._approval_request.id != request.id:
            if all(item.id != request.id for item in self._approval_queue):
                self._approval_queue.append(request)  # After the one on screen.
                self._status(self._status_note)
            return
        self._approval_request = request
        operation = request.operation
        labels = {
            "read_file": "读取文件",
            "write_file": "写入文件",
            "edit_file": "修改文件",
            "glob": "查找文件",
            "grep": "搜索内容",
            "exit_plan_mode": "审阅计划",
            "execute_command": shell_name(shell_label(self.conversation.registry)),
        }
        text = Text()
        if operation.tool == "team_merge":
            text.append(f"需要确认 · 合并成员 {safe_display(operation.target)} 的分支", style=f"bold {OPERATION_COLOR}")
        else:
            text.append(
                "需要授权 · "
                + ("沙箱中的命令联网" if operation.network else labels.get(operation.tool, operation.tool))
                + (" · 在沙箱外运行" if operation.unsandboxed else "")
                + (" · 沙箱只限制写入" if _writes_only(operation) else "")
                + (f" · {request.source}" if request.source else "")
                + "\n",
                style=f"bold {OPERATION_COLOR}",
            )
            text.append(safe_display(operation.target), style=PARAMETER_COLOR)
        if operation.cwd is not None:
            text.append("\n工作目录：" + operation.cwd)
        text.append("\n" + request.reason, style=MUTED_COLOR)
        if operation.tool == "exit_plan_mode":
            text.append("\n完整计划显示在上方对话中。")
        elif operation.preview:
            text.append("\n")
            text.append_text(preview_text(operation.preview))
        self._approval_text = text
        self.query_one("#approval-detail", Static).update(text)
        self.query_one("#approval-panel").display = True
        self._place_prompt()
        options = self.query_one("#approval-options", OptionList)
        options.clear_options()
        options.add_options([choice_prompt(choice_label(request, choice)) for choice in request.choices])
        options.styles.height = len(request.choices)
        options.highlighted = 0
        members = self.query_one("#member-panel", MemberPanel)
        if members.expanded:  # Compact while a card waits.
            self._stop_armed = None
            members.collapse(monotonic())
        options.focus()
        self._show_members()
        if not self._generating:  # A member's request while the lead waits for input.
            self._status("等待授权 · Esc 拒绝本次")
        else:
            self._status(self._status_note)  # The member approval count.
        self._tick()
        self.call_later(self._fit_chat)

    def _show_question(self, request: QuestionRequest) -> None:
        if self._question is not None or self._closing or not self.query("#question-panel"):
            return
        if self._approval_request is not None:  # A member's card; the questions come next.
            self._question_waiting = request
            return
        self._question, self._question_step, self._question_answers = request, 0, []
        self._question_checked, self._question_typing = set(), False
        members = self.query_one("#member-panel", MemberPanel)
        if members.expanded:
            self._stop_armed = None
            members.collapse(monotonic())
        self._render_question()

    def _question_labels(self) -> list[Text]:
        question = self._question.questions[self._question_step]
        labels = []
        for index, (label, description) in enumerate(question.options):
            if question.multi_select:
                mark = "[x] " if index in self._question_checked else "[ ] "
            else:
                mark = f"{index + 1}. "
            text = Text(mark + safe_display(label, 30))
            if description:
                text.append(" — " + safe_display(description, 200), style=MUTED_COLOR)
            labels.append(text)
        labels.append(Text(("    " if question.multi_select else f"{len(question.options) + 1}. ") + OTHER_ANSWER))
        return labels

    def _render_question(self, highlighted: int = 0) -> None:
        request = self._question
        question = request.questions[self._question_step]
        text = Text(question.header, style=f"bold {OPERATION_COLOR}")
        if len(request.questions) > 1:
            text.append(f" · 第 {self._question_step + 1}/{len(request.questions)} 题", style=f"bold {OPERATION_COLOR}")
        if question.multi_select:
            text.append(" · 可多选", style=f"bold {OPERATION_COLOR}")
        if request.source:
            text.append(f" · {request.source}")
        text.append("\n" + safe_display(question.question, 1000))
        self.query_one("#question-detail", Static).update(text)
        options = self.query_one("#question-options", OptionList)
        options.clear_options()
        options.add_options(self._question_labels())
        options.highlighted = highlighted
        self.query_one("#question-help", Static).update(
            "↑ / ↓ 选择 · 空格勾选 · Enter 确认 · Esc 不回答"
            if question.multi_select
            else "↑ / ↓ 选择 · Enter 确认 · Esc 不回答 · Ctrl+C 取消任务"
        )
        self.query_one("#question-panel").display = True
        self._place_prompt()
        options.focus()
        self._show_members()
        self._stage("等待回答 · Esc 不回答")
        self.call_later(self._fit_chat)

    def _toggle_question(self) -> None:
        if self._question is None or self._question_typing:
            return
        question = self._question.questions[self._question_step]
        index = self.query_one("#question-options", OptionList).highlighted
        if not question.multi_select or index is None or index >= len(question.options):
            return
        self._question_checked ^= {index}
        self._render_question(index)

    def _pick_question(self, index: int) -> None:
        question = self._question.questions[self._question_step]
        if index >= len(question.options):  # "其他": the prompt takes the answer.
            self._question_typing = True
            self._place_prompt()
            field = self.query_one("#prompt", PromptArea)
            self._question_draft, field.value = field.value, ""
            self.query_one("#question-help", Static).update("在输入框中输入你的回答 · Enter 提交 · Esc 返回选项")
            field.focus()
            self._stage("等待回答 · 输入回答后 Enter 提交 · Esc 返回选项")
            return
        picked = sorted(self._question_checked) if question.multi_select and self._question_checked else [index]
        self._next_question(Answer(tuple(question.options[i][0] for i in picked)))

    def _next_question(self, answer: Answer) -> None:
        if self._question_typing:
            self._question_typing = False
            self.query_one("#prompt", PromptArea).value = self._question_draft
        self._question_answers.append(answer)
        self._question_checked = set()
        if len(self._question_answers) < len(self._question.questions):
            self._question_step += 1
            self._render_question()
            return
        self._finish_question()

    def _finish_question(self) -> None:
        """Send the answers given so far; the remaining questions go unanswered."""
        request = self._question
        if request is None:
            return
        answers = tuple(self._question_answers)
        answers += (None,) * (len(request.questions) - len(answers))
        if self.conversation.registry is not None:
            self.conversation.registry.permissions.answer(request.id, answers)
        self._hide_question()

    def _hide_question(self) -> None:
        if self._question is None:
            return
        if self._question_typing and not self._closing:
            self.query_one("#prompt", PromptArea).value = self._question_draft
        self._question, self._question_answers, self._question_checked = None, [], set()
        self._question_step, self._question_typing, self._question_draft = 0, False, ""
        if self._closing:
            return
        self.query_one("#question-panel").display = False
        self._place_prompt()
        if self._approval_queue:
            self._hide_approval()  # A member's card that waited comes up.
        else:
            self._focus_home()
            self._show_members()
        self._status(self._status_note if self._generating else "")
        self._tick()
        self.call_later(self._fit_chat)

    def _hide_approval(self) -> None:
        """The next request still waiting stays or comes up (a member's outlives the lead's turn)."""
        waiting = [
            item
            for item in ([self._approval_request] if self._approval_request else []) + self._approval_queue
            if self._waiting(item)
        ]
        self._approval_request = None
        self._approval_queue = waiting[1:]
        if waiting and not self._closing:
            self._show_approval(waiting[0])
            return
        self.query_one("#approval-panel").display = False
        self._place_prompt()
        question, self._question_waiting = self._question_waiting, None
        if question is not None and self._waiting(question) and not self._closing:
            self._show_question(question)
            return
        if not self._closing:
            # The prompt stays usable while the task runs, e.g. to queue a correction.
            self._focus_home()
            self._show_members()
        self.call_later(self._fit_chat)

    def _withdraw_approval(self, request_id: str) -> None:
        """A pane member's request answered in its window, or no longer asked."""
        if self._closing:
            return
        if self._approval_request is not None and self._approval_request.id == request_id:
            self._hide_approval()  # Its future is cancelled: the next one still waiting comes up.
            if self._approval_request is None and not self._generating:
                self._status("")
        else:
            self._approval_queue = [item for item in self._approval_queue if item.id != request_id]
            self._status(self._status_note)
            self._show_members()

    def _answer_approval(self, choice: str) -> None:
        if self._approval_request and self.conversation.registry:
            request, self._approval_request = self._approval_request, None
            self.conversation.registry.permissions.respond(request.id, choice)
            self._hide_approval()
            if request.operation.tool == "team_merge" and choice == "deny":
                self.call_later(self._local_note, DECLINED_HINT)  # The reason is the user's next message.
            if self._approval_request is None and not self._generating:
                self._status("")
            self._tick()

    async def _respond(
        self,
        prompt: str,
        reply: ChatEntry,
        command: str | None = None,
        fork=None,
        notify: bool = False,
        images: dict[str, ProcessedImage] | None = None,
        synthetic: str | None = None,
    ) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        tool_rows: dict[str, ToolEntry] = {}
        continuation = False
        completed = False
        if not notify:
            # A finished checklist stays until the user moves on to the next request.
            items = self.conversation.todos
            if items and todos_completed(items) == len(items):
                self._todo_dismissed = items
            self._refresh_todos()

        try:
            turn = (
                self.conversation.notify()
                if notify
                else self.conversation.reply(prompt, command=command, fork=fork, images=images, synthetic=synthetic)
            )
            async with aclosing(turn) as stream:
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
                    elif isinstance(event, (CompactFinished, CompactFailed, ResultsCleared, ContextCollapsed)):
                        await self._end_compact(
                            compact_notice(event), self._compact_style(event), None if continuation else reply
                        )
                        self._refresh_context()
                    elif isinstance(event, RunStopped):
                        completed = event.reason == "completed"
                        for result in event.results:
                            if result.call_id in tool_rows:
                                tool_rows[result.call_id].complete(result)
                                self._record_tool(tool_rows[result.call_id])
                        if event.reason in STOPPED_EARLY:
                            await self._local_note("└ " + event.notice)
                            self._status("已停止 · 可以继续提问")
                    elif isinstance(event, Retrying):
                        self._retry_notice = f"{event.reason} · {event.delay:.0f} 秒后重试（{event.attempt}/{event.max_attempts}）· Esc 取消"
                        self._stage(self._retry_notice)
                    elif isinstance(event, ReplyRetry):
                        if not continuation:  # Deltas of the dropped reply already went into this entry.
                            reply.discard()
                        self._hint(f"{event.reason}，重新请求（{event.attempt}/{event.max_attempts}）")
                    elif isinstance(event, AutoContinue):
                        # The cut reply is finished; the next piece gets its own entry, after this note.
                        await self._local_note("↻ " + event.text, PARAMETER_COLOR)
                        continuation = True
                        self._generating = True
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
                            self._stage("解析工具参数… · Esc 取消")
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
                                if event.call.name == "agent":
                                    self._agent_rows[event.call.id] = row
                                await chat.mount(row)
                            if event.result is not None:
                                row.complete(event.result, event.elapsed)
                                self._record_tool(row)
                                if event.call.name == "todo_write":
                                    self._refresh_todos()
                                reply.phase = (
                                    "tools" if any(r.elapsed is None for r in tool_rows.values()) else "waiting"
                                )
                            self._tick()
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    elif isinstance(event, FilesAttached):
                        await chat.mount(self._note(attached_line(event.entries), PARAMETER_COLOR), before=reply)
                    elif isinstance(event, UserInput):
                        # Queued messages joined the context at this iteration boundary.
                        for text in event.texts:
                            await chat.mount(ChatEntry("You", text, self._image_notes(text)))
                        chat.anchor()
                        self._refresh_queue()
                    elif isinstance(event, ApprovalRequest):
                        self._show_approval(event)
                        if event.call_id in tool_rows:
                            row = tool_rows[event.call_id]
                            row.update(row._heading("running") + Text("\n  └ 等待用户确认…", style=OPERATION_COLOR))
                    elif isinstance(event, QuestionRequest):
                        self._show_question(event)
                        if event.call_id in tool_rows:
                            row = tool_rows[event.call_id]
                            row.update(row._heading("running") + Text("\n  └ 等待用户回答…", style=OPERATION_COLOR))
                    elif isinstance(event, Completed):
                        if continuation:
                            reply = ChatEntry("MossCode")
                            reply.started = self.started
                            await chat.mount(reply)
                            self._active_reply = reply
                        self._generating = False
                        await reply.finish(event.message, cut=event.stop_reason == "max_tokens")
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
            self._agent_rows = {}
            self._refresh_context()
            if not self._closing:
                self._hide_question()
                self._hide_approval()
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                field.disabled = False
                if self._approval_request is None:  # A member's card still waiting keeps the keys.
                    self._focus_home()  # A member view or the open panel keeps them too.
                queued = self.conversation.take_queued()
                if queued and completed:
                    # The model answered before the queue could join; send it as the next task.
                    text = "\n\n".join(queued)
                    self.call_later(self._start_request, text, shown=queued, images=field.images_in(text))
                elif queued:
                    # Stopped, cancelled or failed: never resend automatically.
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                if getattr(self.conversation, "goal", None) is not None and not (queued and completed):
                    self.call_later(self._start_goal_check)
                self._refresh_queue()
                notices, self._mcp_notices = self._mcp_notices, []
                for notice in notices:
                    await self._local_note("└ " + notice)

    async def _start_goal_check(self) -> None:
        """After a turn while a goal is set: judge it, then continue, end or pause."""
        if self._closing or self._task_running or getattr(self.conversation, "goal", None) is None:
            return
        self.started = monotonic()
        self._generating = self._goal_checking = True
        self._tick()
        self._reply_task = asyncio.create_task(self._check_goal())

    async def _check_goal(self) -> None:
        step = GoalStep("none")
        try:
            step = await self.conversation.goal_after_turn()
        except asyncio.CancelledError:
            if self._closing:
                raise
            step = self.conversation.pause_goal("判断时按了 Esc")
        finally:
            self._generating = self._goal_checking = False
        if self._closing:
            return
        if step.note:
            style = (
                TOOL_SUCCESS_COLOR if step.action == "met" else OPERATION_COLOR if step.action != "continue" else None
            )
            await self._local_note(step.note, style)
        self._status("")
        field = self.query_one("#prompt", PromptArea)
        queued = self.conversation.take_queued()
        if queued:
            # Typed while the goal was judged: the user's message goes first and resumes a paused goal.
            text = "\n\n".join(queued)
            self.call_later(self._start_request, text, shown=queued, images=field.images_in(text))
            self._refresh_queue()
        elif step.action == "continue" and not self.conversation.notifications:
            self.call_later(self._start_request, step.prompt, show_user=False, synthetic=GOAL)

    # Team members -------------------------------------------------------

    def _asking(self) -> dict[str, int]:
        """Waiting approvals by the member asking."""
        teams = getattr(self.conversation, "teams", None)
        counts: dict[str, int] = {}
        if teams is None:
            return counts
        for item in [self._approval_request, *self._approval_queue]:
            if item is not None and self._waiting(item):
                name = teams.asker(item.id)
                if name:
                    counts[name] = counts.get(name, 0) + 1
        return counts

    def _members_changed(self) -> None:
        self._status(self._status_note)
        self._schedule_members()

    def _schedule_members(self) -> None:
        """The rows are read off the loop; a change during a read reads again."""
        if self._closing:
            return
        if self._members_task is not None and not self._members_task.done():
            self._members_again = True
            return
        self._members_task = asyncio.create_task(self._refresh_members())

    async def _refresh_members(self) -> None:
        teams = getattr(self.conversation, "teams", None)
        while not self._closing:
            self._members_again = False
            try:
                rows = await teams.rows() if teams is not None else []
            except Exception:
                rows = []
            if self._closing:
                return
            self._member_rows = rows
            self._show_members()
            if not self._members_again:
                return

    def _members_mounted(self) -> bool:
        # Timers keep firing while the app shuts down, when its widgets go one at a time.
        return not self._closing and bool(self.query("#member-panel")) and bool(self.query("#member-view"))

    def _show_members(self) -> None:
        """The member panel from the rows already read, and the member view."""
        if not self._members_mounted():
            return
        teams = getattr(self.conversation, "teams", None)
        current = teams.current if teams is not None else None
        rows = self._member_rows if current is not None else []
        asking, now = self._asking(), monotonic()
        panel = self.query_one("#member-panel", MemberPanel)
        view = self.query_one("#member-view", MemberView)
        if view.display and (current is None or current.name != view.team):
            self._close_view(to_panel=False)
            self.call_later(self._local_note, f"└ 团队 {view.team} 已不是当前团队，成员视图已关闭")
        if panel.expanded and not rows:
            panel.collapse(now)
            self._focus_home()
        panel.show(rows, asking, now)
        shown = not view.display and not self.query_one("#model-panel").display and (panel.expanded or panel.live)
        if panel.display != shown:
            panel.display = shown
            if not shown and self.focused is self.query_one("#member-list"):
                self._focus_home()
        if view.display:
            view.head(next((row for row in rows if row.name == view.name_shown), None), asking)
        self._status(self._status_note)
        self.call_later(self._fit_chat)

    def _refresh_todos(self) -> None:
        """The main conversation's checklist above the prompt; updated only when it changes."""
        if self._closing or not self.query("#todo-panel"):
            return
        items = self.conversation.todos
        shown = items if items and items != self._todo_dismissed else None
        if shown == self._todo_shown:
            return
        self._todo_shown = shown
        panel = self.query_one("#todo-panel", Static)
        panel.display = shown is not None
        if shown is not None:
            panel.update(todo_panel(shown))
        self.call_later(self._fit_chat)

    def _member_tick(self) -> None:
        """A pane member's transcript every 0.5s; The rows every second while something moves."""
        if not self._members_mounted():
            return
        view = self.query_one("#member-view", MemberView)
        if view.display:
            view.seen()
            self.call_later(self._pull_view)
        self._member_ticks += 1
        if self._member_ticks % 2:
            return
        panel = self.query_one("#member-panel", MemberPanel)
        if view.display or panel.expanded or any(row.status == "running" for row in self._member_rows):
            self._schedule_members()

    def _place_prompt(self) -> None:
        """A card waiting for an answer stands where the prompt was, so the eye stays on one spot; the prompt
        comes back when an answer is to be typed in it ("其他"). It keeps its draft while hidden."""
        if not self.query("#input-row"):
            return
        card = self.query_one("#approval-panel").display or (
            self.query_one("#question-panel").display and not self._question_typing
        )
        shown = not card and not self.query_one("#member-view").display
        self.query_one("#input-row").display = shown
        if not shown:  # What was being typed is out of sight, and so are its suggestions.
            self.query_one("#command-hints").display = self.query_one("#command-help").display = False

    def _focus_home(self) -> None:
        """Where the keys go back to: the member view, the expanded panel, or the prompt."""
        if self.query_one("#member-view").display:
            self.query_one("#member-view", MemberView).member_log.focus()
        elif self.query_one("#member-panel").display and self.query_one("#member-panel", MemberPanel).expanded:
            self.query_one("#member-list", MemberList).focus()
        else:
            self.query_one("#prompt", PromptArea).focus()

    def action_members(self) -> None:
        """Ctrl+T opens the panel to pick from, and closes it (or the member view) again."""
        if self._approval_request or self._question or self.query_one("#model-panel").display:
            return
        if self.query_one("#member-view").display:
            self._close_view(to_panel=False)
            return
        panel = self.query_one("#member-panel", MemberPanel)
        if panel.expanded:
            self._collapse_members()
            return
        teams = getattr(self.conversation, "teams", None)
        if teams is None or teams.current is None:
            self._hint_until = monotonic() + 2
            self._status("没有当前团队 · 让模型用 team_create 建一个")
            return
        if not self._member_rows:
            self._hint_until = monotonic() + 2
            self._status(f"团队 {teams.current.name} 还没有成员")
            self._schedule_members()
            return
        panel.display = True
        panel.expand(monotonic())
        self._show_members()

    def _collapse_members(self) -> None:
        self._stop_armed = None
        self.query_one("#member-panel", MemberPanel).collapse(monotonic())
        self.query_one("#prompt", PromptArea).focus()
        self._show_members()

    def action_stop_member(self) -> None:
        """X twice within STOP_CONFIRM seconds, as /team stop."""
        panel = self.query_one("#member-panel", MemberPanel)
        name = panel.selected
        row = next((row for row in panel.rows if row.name == name), None)
        if row is None:
            return
        if row.status == "stopped":
            panel.help(f"成员 {name} 已经停止 · " + PANEL_HELP)
            return
        armed = self._stop_armed
        if armed is not None and armed[0] == name and monotonic() <= armed[1]:
            self._stop_armed = None
            panel.help(PANEL_HELP)
            self.call_later(self._stop_member, name)
            return
        self._stop_armed = (name, monotonic() + STOP_CONFIRM)
        panel.help(f"再按 x 停止成员 {name}")
        self.set_timer(STOP_CONFIRM + 0.05, self._disarm_stop)

    def _disarm_stop(self) -> None:
        if not self._members_mounted() or self._stop_armed is None or monotonic() <= self._stop_armed[1]:
            return
        self._stop_armed = None
        self.query_one("#member-panel", MemberPanel).help(PANEL_HELP)

    async def _stop_member(self, name: str) -> None:
        try:
            text = await self.conversation.teams.stop_member(name)
        except TeamError as exc:
            await self._local_note("! " + exc.message, OPERATION_COLOR)
            return
        await self._local_note("└ " + text)

    async def _open_view(self, name: str) -> None:
        """In place of the lead's running items, scrolled to the bottom."""
        teams = getattr(self.conversation, "teams", None)
        path = teams.transcript(name) if teams is not None else None
        if path is None:
            return
        self._stop_armed = None
        view = self.query_one("#member-view", MemberView)
        view.reset(name, teams.current.name, TranscriptTail(path))
        self.query_one("#chat").display = False
        self.query_one("#activity").display = False
        self.query_one("#command-hints").display = self.query_one("#command-help").display = False
        view.display = True
        self._place_prompt()  # Read-only.
        view.member_log.focus()
        self._show_members()
        self._fit_chat()
        await self._pull_view(initial=True)

    def _close_view(self, *, to_panel: bool = True) -> None:
        """Back to the panel (or the prompt); the lead's items come back."""
        view = self.query_one("#member-view", MemberView)
        if not view.display:
            return
        name = view.name_shown
        view.display, view.tail = False, None
        self.query_one("#chat").display = self.query_one("#activity").display = True
        self._place_prompt()
        panel = self.query_one("#member-panel", MemberPanel)
        self._show_members()
        if to_panel and self._member_rows:
            panel.display = True
            panel.expand(monotonic(), select=name)
        else:
            if panel.expanded:
                panel.collapse(monotonic())
            self.query_one("#prompt", PromptArea).focus()
        self._refresh_hints(self.query_one("#prompt", PromptArea).value)
        self._show_members()
        self.call_later(self._fit_chat)

    def action_member_step(self, step: int) -> None:
        """←/→ in the panel's order."""
        view = self.query_one("#member-view", MemberView)
        names = [row.name for row in self._member_rows]
        if not view.display or len(names) < 2:
            return
        index = names.index(view.name_shown) if view.name_shown in names else 0
        self.call_later(self._open_view, names[(index + step) % len(names)])

    def _member_committed(self, name: str) -> None:
        """An in-process member's transcript grew; read it now rather than at the next tick."""
        if self._closing or not self.query("#member-view"):
            return
        view = self.query_one("#member-view", MemberView)
        if view.display and view.name_shown == name:
            self.call_later(self._pull_view)

    async def _pull_view(self, initial: bool = False) -> None:
        """The records added since the last read, as lines; reads never overlap."""
        view = self.query_one("#member-view", MemberView)
        async with self._view_lock:
            tail = view.tail
            if tail is None or not view.display:
                return
            name = view.name_shown
            try:
                records = await asyncio.to_thread(tail.read)
            except OSError as exc:
                if initial:
                    view.write([Text(f"无法读取成员 {name} 的对话记录：{exc}", style=ERROR_COLOR)], follow=True)
                return
            if view.tail is not tail or self._closing:
                return  # Switched to another member meanwhile.
            lines = []
            if initial:
                messages = [index for index, record in enumerate(records) if record.get("type") == "message"]
                if len(messages) > VIEW_MESSAGES:
                    skipped = len(messages) - VIEW_MESSAGES
                    records = records[messages[skipped] :]
                    lines.append(Text(f"（更早的 {skipped} 条消息未显示）", style=MUTED_COLOR))
                if not records and not await asyncio.to_thread(tail.path.exists):
                    lines.append(Text(f"成员 {name} 还没有对话记录", style=MUTED_COLOR))
            for record in records:
                lines += view.feed.record(record)
            view.write(lines, follow=True if initial else None)
            view.show_running()

    def action_cycle_mode(self) -> None:
        registry = self.conversation.registry
        if registry is None or self._approval_request or self._question or self.query_one("#model-panel").display:
            return
        if self._generating:
            self._hint_until = monotonic() + 2
            self._status("运行中不能切换权限档位 · Esc 取消")
            return
        self.conversation.set_permission_mode(MODE_CYCLE[registry.permissions.mode])
        self._status("")

    def action_cancel(self) -> None:
        if self._question is not None:
            if self._question_typing:  # Back to the options.
                self._question_typing = False
                self.query_one("#prompt", PromptArea).value = self._question_draft
                other = len(self._question.questions[self._question_step].options)
                self._render_question(other)  # Still on "其他".
            else:
                self._finish_question()  # The rest stay unanswered.
            return
        if self._approval_request:
            self._answer_approval("deny")
            return
        if self.query_one("#member-view").display:  # Back to the panel.
            self._close_view()
            return
        if self.query_one("#member-panel", MemberPanel).expanded:
            self._collapse_members()
            return
        if self.query_one("#model-panel").display:
            cancel = self._on_cancel
            self._close_model_panel()
            if cancel is not None:
                self.call_later(cancel)
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
        if not self._task_running and not self._generating and not self.query_one("#prompt", PromptArea).value:
            now = monotonic()
            if now - self._escape_at <= DOUBLE_ESCAPE:
                self._escape_at = 0.0
                self._open_rewind()
            else:
                self._escape_at = now
            return
        self.action_cancel_task()

    def action_cancel_task(self) -> None:
        if self._generating and self._reply_task and not self._reply_task.done():
            if not self._reply_task.cancelling():
                self._reply_task.cancel()

    async def on_unmount(self) -> None:
        self._closing = True
        if self._members_task is not None and not self._members_task.done():
            self._members_task.cancel()
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
