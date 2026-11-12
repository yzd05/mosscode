"""Terminal colors, the logo and the startup banner."""

from importlib.metadata import version
from pathlib import Path

from rich.console import Group
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.theme import Theme

from .terminal_theme import is_light

TEXT_COLOR = "ansi_default"
LIGHT_BACKGROUND = is_light()
# Each accent has a light and a deep shade: the light ones fade into a white background and the deep ones into a
# black one. Both sets keep at least 4.5:1 contrast on the background they are for.
TOOL_SUCCESS_COLOR = "#1a7f37" if LIGHT_BACKGROUND else "#8bd5a6"
PARAMETER_COLOR = "#0969da" if LIGHT_BACKGROUND else "#6fa8ff"
OPERATION_COLOR = "#9a6700" if LIGHT_BACKGROUND else "#e3b95f"
ERROR_COLOR = "#cf222e" if LIGHT_BACKGROUND else "#e87983"
COMMAND_HIGHLIGHT_COLOR = "#1f4fd8" if LIGHT_BACKGROUND else "#3673ff"
# Green, blue, amber and red already mean success, parameter, in progress and failure, so the
# color that only says "MossCode" (the prompt marker, the spinner) is the one hue left over.
BRAND_COLOR = "#8250df" if LIGHT_BACKGROUND else "#b48cff"
# Supporting text (timings, summaries, key help) is a fixed grey rather than the dim attribute:
# terminals render dim very differently, and this grey keeps 4.5:1 or more on its background.
MUTED_COLOR = "#6e7781" if LIGHT_BACKGROUND else "#8b929c"
RULE_COLOR = "#9ea5ae" if LIGHT_BACKGROUND else "#5c636e"
# User messages sit on a grey bar, as in Claude Code: a mid grey with near-white
# text stands out on both dark and light terminal backgrounds.
USER_MESSAGE_BACKGROUND = "#4a4a4a"
USER_MESSAGE_COLOR = "#f2f2f2"

# The logo, a comet: per column, how many eighths of a line are filled from the bottom of its two
# lines. Eighth blocks move the edge by a fraction of a line, which is what makes the curve smooth.
LOGO_HEIGHTS = (1, 1, 2, 2, 3, 4, 5, 6, 7, 9, 11, 13, 16)
# The tail fades toward the background and the head is the strongest color, so the head is near white on a dark
# background and deep blue on a light one.
LOGO_COLORS = ("#d5dbf0", "#5b7cfa", "#1d2b7a") if LIGHT_BACKGROUND else ("#3b3f55", "#7aa2ff", "#e0e7ff")
LOGO_LINES = 2
EIGHTHS = " ▁▂▃▄▅▆▇█"
STARTUP_TIP = "/help 命令 · @ 引用文件 · ! 执行命令"


# Transparent TCSS inherits the parent's painted background. ANSI default means
# the terminal's own background, and must be preserved by ansi_color=True.
TERMINAL_CSS = """
App, Screen, * {
    color: ansi_default !important;
    text-opacity: 100% !important;
    opacity: 1 !important;
    background: ansi_default !important;
    background-tint: transparent !important;
    tint: transparent !important;
    scrollbar-background: ansi_default;
    scrollbar-background-hover: ansi_default;
    scrollbar-background-active: ansi_default;
    scrollbar-corner-color: ansi_default;
}
Input > .input--placeholder, Input > .input--suggestion {
    color: ansi_default !important;
    text-style: none !important;
}
Markdown MarkdownBlock > .code_inline {
    background: ansi_default !important;
}
Input > .input--cursor {
    color: $foreground;
    background: ansi_default !important;
    text-style: reverse;
}
Input > .input--selection {
    color: $primary;
    background: ansi_default !important;
    text-style: bold underline;
}
OptionList:focus > .option-list--option-highlighted {
    color: $primary;
    background: ansi_default !important;
    text-style: bold;
}
"""


# What Textual composes against; the terminal's own background is what shows.
BACKGROUND = "#ffffff" if LIGHT_BACKGROUND else "#0c0c0c"


def moss_themes() -> tuple[Theme, ...]:
    # Theme colors stay the terminal's own foreground; supporting text gets MUTED_COLOR explicitly.
    variables = {
        name: TEXT_COLOR
        for name in (
            "text",
            "text-muted",
            "text-disabled",
            "foreground-muted",
            "input-cursor-foreground",
            "link-color",
            "link-color-hover",
        )
    }
    variables["input-cursor-background"] = "ansi_default"
    return (
        Theme(
            name="moss-dark",
            primary=TEXT_COLOR,
            secondary=TEXT_COLOR,
            accent=TEXT_COLOR,
            foreground=TEXT_COLOR,
            background=BACKGROUND,
            surface=BACKGROUND,
            panel=BACKGROUND,
            success=TEXT_COLOR,
            warning=TEXT_COLOR,
            error=TEXT_COLOR,
            dark=not LIGHT_BACKGROUND,
            variables=variables,
        ),
    )


def blend(stops: tuple[str, ...], position: float) -> str:
    """The color at `position` (0 to 1) along evenly spaced `stops`."""
    span = min(1.0, max(0.0, position)) * (len(stops) - 1)
    index = min(len(stops) - 2, int(span))
    low, high = (tuple(int(stop[at : at + 2], 16) for at in (1, 3, 5)) for stop in stops[index : index + 2])
    return "#" + "".join(f"{round(a + (b - a) * (span - index)):02x}" for a, b in zip(low, high))


def logo() -> Text:
    art = Text(no_wrap=True)
    for line in range(LOGO_LINES):
        if line:
            art.append("\n")
        floor = 8 * (LOGO_LINES - 1 - line)
        for column, height in enumerate(LOGO_HEIGHTS):
            fill = min(8, max(0, height - floor))
            color = blend(LOGO_COLORS, column / (len(LOGO_HEIGHTS) - 1))
            art.append(EIGHTHS[fill], Style(color=color) if fill else None)
    return art


def current_branch(directory: Path) -> str:
    """The checked-out branch, read from Git's files so that drawing the banner never waits for a process."""
    for folder in (directory, *directory.parents):
        marker = folder / ".git"
        try:
            if marker.is_file():
                # A worktree or submodule has a file pointing at its real Git directory.
                marker = folder / marker.read_text(encoding="utf-8").partition("gitdir:")[2].strip()
            head = (marker / "HEAD").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        return head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:7]
    return ""


def short_path(path: Path) -> str:
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except (ValueError, RuntimeError):
        return str(path)


def startup_banner(provider: str) -> Group:
    muted = Style(color=MUTED_COLOR)
    info = Text(no_wrap=True, overflow="ellipsis")
    info.append("MossCode", Style(bold=True))
    info.append(f" v{version('mosscode')}", muted)
    info.append("\n" + provider)
    top = Table.grid(padding=(0, 2), expand=True)
    top.add_column(width=len(LOGO_HEIGHTS), no_wrap=True)
    top.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    top.add_row(logo(), info)
    directory = Path.cwd()
    where = Text(short_path(directory), muted, no_wrap=True, overflow="ellipsis")
    if branch := current_branch(directory):
        where.append("  " + branch)
    return Group(top, Text(""), where, Text(STARTUP_TIP, muted, no_wrap=True, overflow="ellipsis"))


def compact_notice(event) -> str:
    """One line for a finished or failed compaction, shared by the TUI and headless text output."""
    if hasattr(event, "results"):
        return f"✓ 已清理较早的工具结果 · {event.results} 个 · 约 {event.tokens} Token"
    if hasattr(event, "removed"):
        return (
            f"✓ 已压缩上下文 · {event.removed} 条消息 → 摘要 · 约 {event.summary_tokens} Token"
            f" · 用时 {event.elapsed:.1f}s"
        )
    if event.code == "nothing":
        return "└ " + event.reason
    if event.code == "restore":
        return "! " + event.reason
    if event.manual:
        return "! 压缩上下文失败：" + event.reason
    notice = f"! 自动压缩上下文失败（连续 {event.consecutive} 次）：{event.reason}"
    if event.paused:
        notice += "\n  自动压缩已暂停，可以 /compact 手动重试或 /clear"
    return notice


# The key half of /help; the command half comes from the registry.
KEY_HELP = """按键
Enter                 发送消息或执行命令
Shift+Enter / Ctrl+J  换行（也可在行尾输入 \\ 再按 Enter）
↑ / ↓                 在命令提示中选择；首行/末行时浏览本次输入历史
Tab                   补全命令或参数
Shift+Tab             依次切换 default → acceptEdits → plan
运行中 Enter          排队消息，本轮工具执行后发送给模型
Esc                   关闭命令提示（保留输入）
Esc / Ctrl+C          取消正在生成的回复；排队消息放回输入框
Esc Esc               空闲且输入框为空时打开 /rewind
授权面板              ↑ / ↓ 选择，Enter 确认，Esc 拒绝本次，Ctrl+C 取消任务
Ctrl+B                把前台运行的子 Agent 转入后台
Ctrl+T                展开团队成员面板：Enter 查看对话，x 停止成员
成员视图              PgUp / PgDn 滚动，← / → 切换成员，Esc 返回
Ctrl+Q                退出（有后台任务时先确认）
鼠标滚轮              滚动对话记录
鼠标拖选              使用终端原生文字选择
Ctrl+V                粘贴（多行内容保持原样）"""
