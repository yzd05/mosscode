"""Standalone TUI widgets, selection panels and transcript/status formatters."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic as _time_monotonic
from time import perf_counter as _time_perf_counter
from typing import TypeVar

from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message as UIMessage
from textual.widgets import OptionList, Static, TextArea

from .appearance import (
    BRAND_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    OPERATION_COLOR,
    PARAMETER_COLOR,
    TERMINAL_CSS,
    TOOL_SUCCESS_COLOR,
    USER_MESSAGE_BACKGROUND,
    USER_MESSAGE_COLOR,
    moss_themes,
    startup_banner,
)
from .builtin_commands import TRUST_OPTIONS
from .choice_list import KeyboardOptions
from .clipboard import pasted_image_paths
from .config import ProviderConfig, Settings
from .goal import GOAL
from .images import ImageRef, ProcessedImage, size_label
from .memory import SCOPE_LABELS
from .mentions import mentioned
from .messages import (
    CONTINUE,
    IMAGE_MARK,
    MENTIONS,
    NOTIFICATION,
    REMINDER,
    SHELL,
    Delta,
    Message,
    ToolCall,
    ToolProgress,
    ToolResult,
    image_mark,
)
from .permissions import EDIT_TOOLS, ApprovalRequest, safe_display
from .scrollback import StreamingMarkdown, TranscriptMarkdown, capped_lines, message_row
from .todo import completed as todos_completed
from .tool_budget import persisted_note
from .user_command import CommandRun, ran

Result = TypeVar("Result")


def _monotonic() -> float:
    mod = sys.modules.get("mosscode.tui")
    return mod.monotonic() if mod is not None else _time_monotonic()


def _perf_counter() -> float:
    mod = sys.modules.get("mosscode.tui")
    return mod.perf_counter() if mod is not None else _time_perf_counter()


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
    if result.code in {"cancelled", "not_executed", "plan_blocked", "steered"}:
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
DETAIL_ROWS = 200  # Expanded thinking, tool output and diff line cap when Ctrl+O detail view is active.


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
    width = 300 if limit > DIFF_ROWS else 160
    for number, row, style in rows[:limit]:
        text.append("\n    " + number, style=MUTED_COLOR)
        text.append(row[: width - len(number)], style=style)
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
        def __init__(self, text_area: PromptArea, value: str) -> None:
            super().__init__()
            self.text_area, self.value = text_area, value

        @property
        def control(self) -> PromptArea:
            return self.text_area

    class ImagePaste(UIMessage):
        """Attach images: files (dragged in or pasted as paths), or whatever image the clipboard holds.

        otherwise runs when the clipboard holds no image (Ctrl+V's ordinary paste); without it the user is told.
        """

        def __init__(
            self,
            text_area: PromptArea,
            files: list[Path] | None = None,
            otherwise: Callable[[], None] | None = None,
        ) -> None:
            super().__init__()
            self.text_area, self.files, self.otherwise = text_area, files, otherwise

        @property
        def control(self) -> PromptArea:
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

    def attach(self, items: list[Attachment]) -> None:
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
        self.started = _monotonic()
        self.transcript: Text | None = None  # Set once finished; scrollback waits until then.
        self.tick()

    def tick(self) -> None:
        if self.transcript is None:
            self.update(Text(f"✻ {self.label} · {_monotonic() - self.started:.1f}s", style=OPERATION_COLOR))

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
        self.started = _monotonic()
        self.transcript: Text | None = None  # Set once finished; scrollback waits until then.
        self.tick()

    def tick(self) -> None:
        if self.transcript is None:
            elapsed = _monotonic() - self.started
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
    """The choice list taking the prompt's place, shared by /model, /resume, /rewind and /thinking."""

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


def _capped_thinking(thinking: str, limit: int = DETAIL_ROWS) -> str:
    return capped_lines(thinking, limit, "思考内容")


class ChatEntry(Vertical):
    def __init__(
        self,
        speaker: str,
        text: str = "",
        notes: list[str] | tuple[str, ...] = (),
        *,
        show_details: bool = False,
    ) -> None:
        super().__init__(classes="entry user-entry" if speaker == "You" else "entry assistant-entry")
        self.speaker = speaker
        self.body = text
        self.notes = tuple(notes)  # Under a user message: its attached images.
        self.thinking = ""
        self.show_details = show_details
        self.started = _monotonic()
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
            elapsed = self.thinking_elapsed + _monotonic() - self.thinking_started
            self.query_one(".thinking-label", Static).update(f"思考中 · {elapsed:.1f}s")

    def _sync_thinking_visibility(self) -> None:
        if self.speaker == "You" or self.phase == "thinking":
            return
        has_thinking = bool(self.thinking.strip())
        label = self.query_one(".thinking-label", Static)
        block = self.query_one(".thinking", Static)
        if has_thinking and self.show_details and self.phase != "interrupted":
            if not label.display:
                label.update(f"✻ 思考 {self.thinking_elapsed:.1f}s" if self.thinking_elapsed else "✻ 思考")
                label.display = True
            block.update(_capped_thinking(self.thinking))
            block.display = True
            if self.phase == "tools" and not self.body:
                self.display = True
        else:
            block.display = False
            if self.phase == "tools" and not self.body and not self.thinking:
                self.display = False

    def set_details(self, show_details: bool) -> None:
        self.show_details = show_details
        if self.speaker != "You" and self.is_mounted:
            self._sync_thinking_visibility()

    def _finish_thinking(self) -> None:
        if self.thinking_started is not None:
            self.thinking_elapsed += _monotonic() - self.thinking_started
            self.thinking_started = None
            self.query_one(".thinking-label", Static).update(f"✻ 思考 {self.thinking_elapsed:.1f}s")
        self._sync_thinking_visibility()

    def append(self, event: Delta) -> None:
        if not event.text:
            return
        if event.kind == "thinking":
            self.phase = "thinking"
            if self.thinking_started is None:
                self.thinking_started = _monotonic()
            self.thinking += event.text
            self.query_one(".thinking-label", Static).display = True
            self.refresh_timer()
            widget = self.query_one(".thinking", Static)
            widget.display = True
            widget.update(self.thinking)
            self.query_one(".message-row").display = bool(self.body)
        else:
            self.phase = "streaming"
            self._finish_thinking()
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
        wait = self._paint_at - _perf_counter()
        if wait > 0:
            self._paint_timer = self.set_timer(wait, self._paint_now)
            return
        self._paint_now()

    def _paint_now(self) -> None:
        self._paint_timer = None
        if self._markdown is None or self.phase != "streaming":
            return  # Finished, dropped or cut off meanwhile: that has already drawn the reply.
        self.query_one(".answer", Static).update(self._markdown)
        self._paint_at = _perf_counter() + min(0.25, 3 * self._markdown.tail_seconds)

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

    def _capture_message_thinking(self, message: Message) -> None:
        if not self.thinking:
            self.thinking = "\n".join(b.text for b in message.content if b.kind == "thinking" and b.text)

    async def finish(self, message: Message, *, cut: bool = False) -> None:
        """cut: stopped at the output limit; the note after it says what happens next, not a done summary."""
        self._capture_message_thinking(message)
        self.phase = "complete"
        self._finish_thinking()
        self.body = message.text
        self._show_reply(self._reply_markdown())
        self.query_one(".message-row").display = True
        if cut:
            return
        summary = self.query_one(".turn-summary", Static)
        self.completed_elapsed = _monotonic() - self.started
        summary.update(f"✻ 完成 · {self.completed_elapsed:.1f}s")
        summary.display = True

    async def seal_tool_request(self, message: Message) -> None:
        self._capture_message_thinking(message)
        self.phase = "tools"
        self.tool_request = True
        self.body = message.text
        self._finish_thinking()
        self.query_one(".message-row").display = bool(self.body)
        if not self.body and not self.thinking:
            self.display = False
        if self.body:
            self._show_reply(self._reply_markdown())

    def interrupted(self, note: str) -> None:
        self.interruption_note = note
        self.display = True
        self.phase = "interrupted"
        self._finish_thinking()
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
        expanded_thinking = self.show_details and bool(self.thinking.strip()) and not self.interruption_note
        if self.thinking_elapsed or expanded_thinking:
            label = f"✻ 思考 {self.thinking_elapsed:.1f}s" if self.thinking_elapsed else "✻ 思考"
            parts.append(Text(label, style=MUTED_COLOR))
            if expanded_thinking:
                parts.append(Padding(Text(_capped_thinking(self.thinking), style=MUTED_COLOR), (0, 0, 0, 2)))
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
    for question, answer in zip(details.get("questions") or [], details.get("answers") or []):
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

    def __init__(self, event: ToolProgress, shell: str | None = None, *, show_details: bool = False):
        super().__init__(classes="tool-entry", markup=False)
        self.started = _monotonic()
        self.state = "running"
        self.elapsed: float | None = None
        self.subagent = None  # The task of an agent row, once it starts.
        self.show_details = show_details
        self.result: ToolResult | None = None
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

    def _changes(self, result: ToolResult, *, detailed: bool = False) -> Text | None:
        """Numbered diff for a successful edit or overwrite; the first lines of a new file."""
        if result.is_error or self.operation not in EDIT_TOOLS:
            return None
        if result.details.get("diff"):
            return diff_rows(result.details["diff"], limit=DETAIL_ROWS if detailed else DIFF_ROWS)
        content = self.arguments.get("content")
        if self.operation == "write_file" and result.details.get("created") and isinstance(content, str):
            limit = DETAIL_ROWS if detailed else NEW_FILE_ROWS
            width = 300 if detailed else 160
            lines = content.splitlines()
            text = Text()
            for number, line in enumerate(lines[:limit], 1):
                text.append(f"\n    {number:>5}", style=MUTED_COLOR)
                text.append(f" + {line[:width]}", style=TOOL_SUCCESS_COLOR)
            if len(lines) > limit:
                text.append(f"\n    … 还有 {len(lines) - limit} 行未显示", style=MUTED_COLOR)
            return text
        return None

    def render_result(self, result: ToolResult, *, detailed: bool = False) -> Text:
        line_limit = DETAIL_ROWS if detailed else 3
        width_limit = 300 if detailed else 160
        lines = result.output.splitlines() or ["完成，无输出。"]
        shown = lines[:line_limit]
        state = self.state
        elapsed = self.elapsed if self.elapsed is not None else 0.0
        if self.operation == "memory" and not result.is_error:
            text = memory_line(result.details)
            text.append(f" {elapsed:.1f}s", style=MUTED_COLOR)
            return text
        text = self._heading(state)
        text.append(f" {elapsed:.1f}s\n  └ ", style=MUTED_COLOR)
        # What a successful call returned is a note on the process; a failure's detail is what to read.
        summary = MUTED_COLOR if state == "success" else None
        if self.operation in {"agent", "execute_command"} and result.details.get("background"):
            text.append(f"后台 · 任务 {result.details.get('task', '')}", style=PARAMETER_COLOR)
            if result.details.get("log"):
                text.append(f" · {result.details['log']}", style=PARAMETER_COLOR)
            return text
        if self.operation == "ask_user" and not result.is_error and result.details.get("answers") is not None:
            shown = answer_lines(result.details) or shown
            summary = None  # The user's own answers.
        if not detailed and self.operation == "agent" and len(lines) > 3 and lines[-1].startswith("（子 Agent"):
            shown = [*lines[:2], lines[-1]]
        if result.is_error:
            label = "已取消" if result.code == "cancelled" else "未执行" if state == "stopped" else "失败"
            text.append(label + " · ", style=f"bold {OPERATION_COLOR if state == 'stopped' else ERROR_COLOR}")
        text.append(shown[0][:width_limit], style=summary)
        changes = self._changes(result, detailed=detailed)
        if changes is not None:
            text.append_text(changes)
        else:
            for line in shown[1:]:
                text.append("\n    " + line[:width_limit], style=summary)
            if detailed:
                if len(lines) > line_limit:
                    text.append(f"\n    … 还有 {len(lines) - line_limit} 行输出未显示", style=MUTED_COLOR)
                elif result.details.get("truncated"):
                    text.append("\n    （已达工具输出上限）", style=MUTED_COLOR)
            elif len(lines) > 3 or any(len(line) > 160 for line in lines[:3]) or result.details.get("truncated"):
                text.append("\n    （仅显示结果摘要）", style=MUTED_COLOR)
        stored = persisted_note(result)
        if stored:
            text.append("\n    " + stored, style=PARAMETER_COLOR)
        if result.details.get("hook_modified"):
            text.append(
                f"\n    文件已被 Hook {result.details['hook_modified']} 修改（如格式化），模型再次编辑前会重新读取。",
                style=PARAMETER_COLOR,
            )
        return text

    def set_details(self, show_details: bool) -> None:
        if self.show_details == show_details:
            return
        self.show_details = show_details
        if self.result is not None:
            self.transcript = self.render_result(self.result, detailed=self.show_details)
            self.update(self.transcript)

    def complete(self, result: ToolResult, elapsed: float | None = None) -> None:
        if elapsed is not None or self.elapsed is None:
            self.elapsed = elapsed if elapsed is not None else _monotonic() - self.started
        self.state = tool_state(result)
        self.result = result
        self.transcript = self.render_result(result, detailed=self.show_details)
        self.update(self.transcript)


def turn_details_block(entries: Iterable[ChatEntry | ToolEntry]) -> Group | None:
    """Expanded thinking and tool outputs for the latest turn after its rows moved into scrollback."""
    parts = []
    for item in entries:
        if (
            isinstance(item, ChatEntry)
            and item.speaker != "You"
            and item.thinking.strip()
            and not item.interruption_note
        ):
            label = f"✻ 思考 {item.thinking_elapsed:.1f}s" if item.thinking_elapsed else "✻ 思考"
            parts.append(Text(label, style=MUTED_COLOR))
            parts.append(Padding(Text(_capped_thinking(item.thinking), style=MUTED_COLOR), (0, 0, 0, 2)))
        elif isinstance(item, ToolEntry) and item.result is not None:
            parts.append(item.render_result(item.result, detailed=True))
    if not parts:
        return None
    spaced = [Text("✻ 本轮详情（Ctrl+O 收起）", style=PARAMETER_COLOR)]
    for part in parts:
        spaced.append(Text(""))
        spaced.append(part)
    return Group(*spaced)
