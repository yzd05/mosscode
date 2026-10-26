"""Terminal colors and a character rendering of the user-provided robot icon."""

from importlib.metadata import version
from pathlib import Path
from typing import Sequence

from rich.table import Table
from rich.style import Style
from rich.text import Text
from textual.theme import Theme


TEXT_COLOR = "ansi_default"
LOGO_COLOR = "#8bd5a6"
TOOL_SUCCESS_COLOR = LOGO_COLOR
PARAMETER_COLOR = "#6fa8ff"
OPERATION_COLOR = "#e3b95f"
ERROR_COLOR = "#e87983"
COMMAND_HIGHLIGHT_COLOR = "#3673ff"
# User messages sit on a grey bar, as in Claude Code: a mid grey with near-white
# text stands out on both dark and light terminal backgrounds.
USER_MESSAGE_BACKGROUND = "#4a4a4a"
USER_MESSAGE_COLOR = "#f2f2f2"

COMMAND_HINTS = (
    ("/help", "查看命令和快捷键"),
    ("/clear", "开始新会话：清空模型上下文和 Token 统计"),
    ("/compact", "压缩上下文：较早的对话换成摘要，近期消息保留"),
    ("/resume", "恢复本项目之前的会话"),
    ("/memory", "查看已加载的项目指令和记忆笔记"),
    ("/model", "切换提供商与模型，对话历史保留"),
    ("/plan", "只读调查和规划，提交计划经批准后执行"),
    ("/permissions", "查看或切换工具权限档位"),
    ("/mcp", "查看 MCP Server 的连接状态与工具"),
    ("/exit", "退出 MossCode"),
)


def command_matches(value: str, providers: Sequence[str] = (), servers: Sequence[str] = ()) -> list[tuple[str, str]]:
    """Complete the local commands and their fixed arguments (modes, providers, MCP servers)."""
    if "\n" in value:
        return []
    value = value.lstrip()
    command, separator, argument = value.partition(" ")
    if command == "/model" and separator:
        return [(f"/model {name}", "切换到该提供商") for name in providers if name.startswith(argument.lstrip())]
    if command == "/mcp" and separator:
        return [(f"/mcp {name}", "查看该 Server 的工具") for name in servers if name.startswith(argument.lstrip())]
    if command == "/permissions" and separator:
        return [(f"/permissions {slug}", description) for slug, description in (
            ("default", "默认：读取自动通过，写入和命令确认"),
            ("acceptEdits", "接受编辑：文件写入/编辑自动通过，命令确认"),
            ("plan", "计划：只读调查，批准计划后执行"),
            ("bypassPermissions", "跳过审批：仍遵守硬限制和拒绝规则"),
        ) if slug.startswith(argument.lstrip())]
    if separator or not value.startswith("/"):
        return []
    choices = COMMAND_HINTS if not value.startswith("/q") else (("/quit", "退出 MossCode（/exit 的别名）"),)
    return [(command, description) for command, description in choices if command.startswith(value)]

ROBOT_ROWS = (
    "     ▀▀     ",
    "▄█▀▀▀▀▀▀▀▀█▄",
    "██  ●  ●  ██",
    "▀█▄▄▄▄▄▄▄▄█▀",
)


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
    variables = {name: TEXT_COLOR for name in (
        "text", "text-muted", "text-disabled", "foreground-muted",
        "input-cursor-foreground", "link-color", "link-color-hover",
    )}
    variables["input-cursor-background"] = "ansi_default"
    return (Theme(
        name="moss-dark", primary=TEXT_COLOR, secondary=TEXT_COLOR, accent=TEXT_COLOR,
        foreground=TEXT_COLOR, background="#0c0c0c", surface="#0c0c0c", panel="#0c0c0c",
        success=TEXT_COLOR, warning=TEXT_COLOR, error=TEXT_COLOR, dark=True, variables=variables,
    ),)


def startup_banner(
    provider: str,
) -> Table:
    # Half-block cells preserve the SVG's antenna, hollow rectangular head, ears and two eyes.
    logo = Text("\n".join(ROBOT_ROWS), style=LOGO_COLOR)
    info = Text(style="default", no_wrap=True, overflow="ellipsis")
    info.append("MossCode", style=Style(bold=True))
    info.append(f" v{version('mosscode')}\n")
    info.append(provider)
    info.append("\n" + str(Path.cwd()))
    banner = Table.grid(padding=(0, 2), expand=True)
    banner.add_column(width=12, no_wrap=True)
    banner.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    banner.add_row(logo, info)
    return banner


def compact_notice(event) -> str:
    """One line for a finished or failed compaction, shared by the TUI and headless text output."""
    if hasattr(event, "removed"):
        return (f"✓ 已压缩上下文 · {event.removed} 条消息 → 摘要 · 约 {event.summary_tokens} Token"
                f" · 用时 {event.elapsed:.1f}s")
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


HELP_TEXT = """命令
/help                 显示命令与按键
/clear                开始新会话：清空模型上下文和 Token 统计，权限与会话授权保留
/compact [说明]       压缩上下文：较早的对话换成摘要，近期消息保留；说明用于指定摘要侧重点
/resume [ID]          恢复本项目之前的会话；不带 ID 时打开会话列表
/memory [名称]        列出已加载的项目指令、记忆笔记和目录；带名称时显示该笔记全文
/model [名称]         切换提供商与模型，对话历史保留；不带名称时打开选择面板
/plan [任务]          只读调查和规划；模型提交计划，批准后执行
/permissions [档位]   default/acceptEdits/plan/bypassPermissions 切换权限
/mcp [名称]           查看 MCP Server 状态；带名称时列出该 Server 的工具
/exit                 退出 MossCode

按键
Enter                 发送消息或执行命令
Shift+Enter / Ctrl+J  换行（也可在行尾输入 \\ 再按 Enter）
↑ / ↓                 在命令提示中选择；首行/末行时浏览本次输入历史
Tab                   补全命令或参数
Shift+Tab             依次切换 default → acceptEdits → plan
运行中 Enter          排队消息，本轮工具执行后发送给模型
Esc                   关闭命令提示（保留输入）
Esc / Ctrl+C          取消正在生成的回复；排队消息放回输入框
授权面板              ↑ / ↓ 选择，Enter 确认，Esc 拒绝本次，Ctrl+C 取消任务
Ctrl+Q                退出
鼠标滚轮              滚动对话记录
鼠标拖选              使用终端原生文字选择
Ctrl+V                粘贴（多行内容保持原样）"""
