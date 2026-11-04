"""Terminal colors and the mascot of the startup banner."""

from importlib.metadata import version
from pathlib import Path

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.theme import Theme

TEXT_COLOR = "ansi_default"
TOOL_SUCCESS_COLOR = "#8bd5a6"
PARAMETER_COLOR = "#6fa8ff"
OPERATION_COLOR = "#e3b95f"
ERROR_COLOR = "#e87983"
COMMAND_HIGHLIGHT_COLOR = "#3673ff"
# User messages sit on a grey bar, as in Claude Code: a mid grey with near-white
# text stands out on both dark and light terminal backgrounds.
USER_MESSAGE_BACKGROUND = "#4a4a4a"
USER_MESSAGE_COLOR = "#f2f2f2"

# The mascot, a little ghost, as pixels: two pixel rows make one line of half blocks, so each pixel is
# about square. "." is the terminal's own background; the other letters are MASCOT_COLORS.
MASCOT_PIXELS = (
    "....wwwwww....",
    "..wwwwwwwwww..",
    ".wwwwwwwwwwww.",
    ".wwkhwwwwkhww.",
    ".wwkkwwwwkkww.",
    ".wrrwwkkwwrrw.",
    ".wwwwwwwwwwww.",
    ".wwwwwwwwwwww.",
    ".ww.www.www.w.",
    ".w...w...w....",
)
MASCOT_COLORS = {"w": "#eef1ff", "k": "#26212e", "h": "#ffffff", "r": "#ff9fb2"}


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


def moss_themes() -> tuple[Theme, ...]:
    # Use the terminal's full-strength foreground; never dim supporting text.
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
            background="#0c0c0c",
            surface="#0c0c0c",
            panel="#0c0c0c",
            success=TEXT_COLOR,
            warning=TEXT_COLOR,
            error=TEXT_COLOR,
            dark=True,
            variables=variables,
        ),
    )


def pixel_art(pixels: tuple[str, ...], colors: dict[str, str]) -> Text:
    """Each pair of pixel rows as one line: ▀ in the upper color over the lower one as background."""
    art = Text(no_wrap=True)
    for index, (upper, lower) in enumerate(zip(pixels[::2], pixels[1::2])):
        if index:
            art.append("\n")
        for top, bottom in zip(upper, lower):
            top, bottom = colors.get(top), colors.get(bottom)
            if top and top == bottom:
                art.append("█", Style(color=top))
            elif top:
                art.append("▀", Style(color=top, bgcolor=bottom))
            elif bottom:
                art.append("▄", Style(color=bottom))
            else:
                art.append(" ")
    return art


def startup_banner(
    provider: str,
) -> Table:
    logo = pixel_art(MASCOT_PIXELS, MASCOT_COLORS)
    info = Text(style="default", no_wrap=True, overflow="ellipsis")
    info.append("MossCode", style=Style(bold=True))
    info.append(f" v{version('mosscode')}\n")
    info.append(provider)
    info.append("\n" + str(Path.cwd()))
    banner = Table.grid(padding=(0, 2), expand=True)
    banner.add_column(width=len(MASCOT_PIXELS[0]), no_wrap=True)
    banner.add_column(ratio=1, no_wrap=True, overflow="ellipsis", vertical="middle")
    banner.add_row(logo, info)
    return banner


def compact_notice(event) -> str:
    """One line for a finished or failed compaction, shared by the TUI and headless text output."""
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
