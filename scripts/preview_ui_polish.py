"""The mock-up docs/specs/ui-polish was reviewed with: logo candidates and samples of the proposed look.

Printed with Rich from fixed data; it does not import the TUI. The logo that was chosen is drawn by the
product (mosscode.appearance.logo) and shown first; the four candidates are kept as the record of the proposal.
Run: .venv/Scripts/python.exe scripts/preview_ui_polish.py [logo|chat|panels|all] [--json DIR]
"""

import json
import math
import sys
from pathlib import Path

from rich.console import Console, Group
from rich.padding import Padding
from rich.style import Style
from rich.table import Table
from rich.text import Text

from mosscode.appearance import (
    ERROR_COLOR,
    OPERATION_COLOR,
    PARAMETER_COLOR,
    TOOL_SUCCESS_COLOR,
    USER_MESSAGE_BACKGROUND,
    USER_MESSAGE_COLOR,
    logo,
)

BRAND = "#b48cff"  # The violet end of the logo gradient; no semantic color is near it.
MUTED = "#8b929c"  # Secondary text: an explicit grey, readable on dark (6:1), not SGR dim.
RULE = "#5c636e"
SELECT = "#3673ff"

AURORA = ("#5eead4", "#7aa2ff", "#b48cff")
LOWER = " ▁▂▃▄▅▆▇█"  # Lower blocks in eighths of a cell: edges move by 1/8 of a line instead of a half.


def blend(stops: tuple[str, ...], position: float) -> str:
    span = min(1.0, max(0.0, position)) * (len(stops) - 1)
    index = min(len(stops) - 2, int(span))
    low, high = (tuple(int(stop[i : i + 2], 16) for i in (1, 3, 5)) for stop in stops[index : index + 2])
    return "#{:02x}{:02x}{:02x}".format(*(round(a + (b - a) * (span - index)) for a, b in zip(low, high, strict=True)))


def columns(fills: list, lines: int) -> Text:
    """Draw per-column fills, each (low, high, color) in eighths of a line counted from the bottom.

    A cell holds one edge: a fill rising from the cell's floor is a lower block, a fill hanging from its ceiling
    is the same block in reverse video, and two fills meeting in a cell are foreground over background.
    """
    text = Text(no_wrap=True)
    for line in range(lines):
        if line:
            text.append("\n")
        floor = 8 * (lines - 1 - line)
        for column in fills:
            inside = sorted(
                (max(0, lo - floor), min(8, hi - floor), c) for lo, hi, c in column if hi > floor and lo < floor + 8
            )
            if not inside:
                text.append(" ")
            elif len(inside) == 2:
                text.append(LOWER[inside[0][1]], Style(color=inside[0][2], bgcolor=inside[1][2]))
            elif inside[0][0] <= 0:
                text.append(LOWER[inside[0][1]], Style(color=inside[0][2]))
            else:
                text.append(LOWER[inside[0][0]], Style(color=inside[0][2], reverse=True))
    return text


def wave(width: int = 14) -> Text:
    """Candidate A: one period of a sine as a ribbon one line thick, two lines tall."""
    fills = []
    for x in range(width):
        center = 8 + 4 * math.sin(2 * math.pi * (x + 0.5) / width)
        fills.append([(round(center) - 4, round(center) + 4, blend(AURORA, x / (width - 1)))])
    return columns(fills, 2)


STRATA = (("#a78bfa", "#c4b5fd", 2.2), ("#5b8def", "#7aa2ff", 0.7), ("#2dd4bf", "#67e8f9", -0.9))


def strata(width: int = 8) -> Text:
    """Candidate B: three stacked layers with wavy crests, each crest kept inside its own line."""
    fills = []
    for x in range(width):
        column, low = [], 0
        for index, (start, end, phase) in enumerate(STRATA):
            crest = 8 * index + 4 + 3 * math.sin(2 * math.pi * (x + 0.5) / width + phase)
            crest = min(8 * index + 8, max(8 * index + 1, round(crest)))
            column.append((low, crest, blend((start, end), x / (width - 1))))
            low = crest
        fills.append(column)
    return columns(fills, 3)


# Candidate C: a gem of four facets, each a flat shade. 4 lines of half blocks.
PRISM = (
    "....aa....",
    "...aabb...",
    "..aaabbb..",
    ".aaaabbbb.",
    ".ccccdddd.",
    "..cccddd..",
    "...ccdd...",
    "....cd....",
)
PRISM_COLORS = {"a": "#c4b5fd", "b": "#8b5cf6", "c": "#7aa2ff", "d": "#4f46e5"}


def half_blocks(rows: tuple[str, ...], colors: dict[str, str]) -> Text:
    """Two rows of pixels per line: the upper pixel is the foreground of ▀ (or ▄ alone), the lower its background."""
    text = Text(no_wrap=True)
    for line in range(0, len(rows), 2):
        if line:
            text.append("\n")
        for upper, lower in zip(rows[line], rows[line + 1], strict=True):
            top, bottom = colors.get(upper), colors.get(lower)
            if top:
                text.append("▀", Style(color=top, bgcolor=bottom))
            else:
                text.append("▄" if bottom else " ", Style(color=bottom) if bottom else None)
    return text


def peaks() -> Text:
    """Candidate D: a far peak three lines tall and a nearer, lower one in front of it."""
    far = (3, 6, 9, 12, 15, 18, 21, 24, 21, 18, 15, 12, 9, 6, 3, 0, 0, 0, 0)
    near = (0, 0, 0, 0, 0, 0, 0, 0, 2, 4, 6, 8, 10, 12, 14, 12, 10, 8, 6)
    fills = []
    for x, (back, front) in enumerate(zip(far, near, strict=True)):
        column = [(0, front, BRAND)] if front else []
        if back > front:
            column.append((front, back, blend(AURORA[:2], x / 14)))
        fills.append(column)
    return columns(fills, 3)


INFO = (
    (("MossCode", Style(bold=True)), (" v0.2.0", MUTED)),
    (("DeepSeek V4.1 Flash", None),),
    (("~\\code-storage\\PyCharm\\MossCode", MUTED), ("  context-management", MUTED)),
    (("/help 命令 · @ 引用文件 · ! 执行命令", MUTED),),
)


def info(lines: tuple = INFO) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    for index, parts in enumerate(lines):
        if index:
            text.append("\n")
        for part, style in parts:
            text.append(part, style)
    return text


def beside(logo: Text, right: Text) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=max(map(len, logo.plain.split("\n"))), no_wrap=True)
    grid.add_column(no_wrap=True, vertical="middle")
    grid.add_row(logo, right)
    return grid


def banner(logo: Text) -> Group:
    """The mark with as many info lines beside it as it is tall; the remaining ones below, after a blank line."""
    height = logo.plain.count("\n") + 1
    parts = [beside(logo, info(INFO[:height]))]
    if height < len(INFO):
        parts += [Text(""), info(INFO[height:])]
    return Group(*parts)


def prompt_box(text: str = "", color: str = RULE, placeholder: bool = True) -> Group:
    line = Text()
    line.append("❯ ", Style(color=BRAND, bold=True))
    if text:
        line.append(text)
    elif placeholder:
        line.append("输入消息，或 / 查看命令；Shift+Enter 换行", MUTED)
    return Group(Text("─" * 96, color), Padding(line, (0, 1)), Text("─" * 96, color))


def status(mode: str = "默认权限", color: str | None = None, right: str = "") -> Table:
    left = Text()
    left.append(mode, Style(color=color, bold=True))
    left.append(" (Shift+Tab 切换)", MUTED)
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    grid.add_row(left, Text(right, MUTED))
    return grid


def heading(title: str) -> Text:
    return Text(f"\n━━ {title} ━━\n", Style(bold=True, color=OPERATION_COLOR))


def logos() -> list:
    shown = []
    for title, mark in (
        ("选定：彗星（2 行）", logo()),
        ("候选 A：波带（2 行）", wave()),
        ("候选 B：层叠（3 行）", strata()),
        ("候选 C：棱镜（4 行）", half_blocks(PRISM, PRISM_COLORS)),
        ("候选 D：双峰（3 行）", peaks()),
    ):
        shown += [heading(title), banner(mark), prompt_box()]
    return shown


def tool(marker, color, name, name_color, detail, elapsed, lines, result_style=MUTED) -> Text:
    text = Text()
    text.append(marker + " ", color)
    text.append(name, Style(color=name_color, bold=True))
    text.append("(")
    text.append(detail, PARAMETER_COLOR)
    text.append(")")
    if elapsed:
        text.append(" " + elapsed, MUTED)
    for index, (line, style) in enumerate(lines):
        text.append("\n  └ " if not index else "\n    ", MUTED)
        text.append(line, style if style is not None else result_style)
    return text


def user(text: str) -> Padding:
    return Padding(Text("❯ " + text), 0, expand=True, style=f"{USER_MESSAGE_COLOR} on {USER_MESSAGE_BACKGROUND}")


def chat() -> list:
    diff = Text()
    for number, sign, line, color in (
        ("10", " ", " def greet(name):", None),
        ("11", "-", '     return "Hello " + name', ERROR_COLOR),
        ("11", "+", '     message = f"Hello, {name}!"', TOOL_SUCCESS_COLOR),
        ("12", "+", "     return message", TOOL_SUCCESS_COLOR),
    ):
        diff.append(f"\n    {number:>5} ", MUTED)
        diff.append(f"{sign} {line}", color)
    edit = tool(
        "✓", TOOL_SUCCESS_COLOR, "Edit", OPERATION_COLOR, "src/app.py", "0.1s", [("已修改 src/app.py · 1 处", None)]
    )
    edit.append_text(diff)
    answer = Text("● ")
    answer.append("已把 ")
    answer.append("greet", Style(color=PARAMETER_COLOR, bold=True))
    answer.append(" 改成 ")
    answer.append("f-string", Style(bold=True))
    answer.append("，但测试失败了：\n  • ")
    answer.append("tests/test_app.py::test_greet", Style(color=PARAMETER_COLOR, bold=True))
    answer.append(" 期望的是旧格式\n  • 需要同步更新断言\n\n")
    answer.append("    assert", "#c792ea")
    answer.append(" greet(")
    answer.append('"Moss"', "#c3e88d")
    answer.append(") == ")
    answer.append('"Hello, Moss!"', "#c3e88d")
    activity = Text()
    activity.append("⠹ ", BRAND)
    activity.append("执行工具中", OPERATION_COLOR)
    activity.append(" · 2/25 轮 · 1.3s · Esc 取消", MUTED)
    return [
        heading("改后的对话区（现在的样子见 spec 中的对照）"),
        user("把 greet 改成 f-string，然后跑一下测试。"),
        Text(""),
        Text("● 先看一下现在的实现。"),
        Text(""),
        tool("✓", TOOL_SUCCESS_COLOR, "Read", TOOL_SUCCESS_COLOR, "src/app.py", "0.0s", [("已读取 42 行", None)]),
        Text(""),
        tool(
            "✓",
            TOOL_SUCCESS_COLOR,
            "Grep",
            TOOL_SUCCESS_COLOR,
            "greet",
            "0.1s",
            [("src/app.py:10: def greet(name):", None), ("tests/test_app.py:4: greet", None)],
        ),
        Text(""),
        edit,
        Text(""),
        tool(
            "×",
            ERROR_COLOR,
            "PowerShell",
            ERROR_COLOR,
            "python -m pytest -q tests/test_app.py",
            "2.4s",
            [("失败 · 退出码 1", ERROR_COLOR), ("FAILED tests/test_app.py::test_greet", "default")],
        ),
        Text(""),
        answer,
        Text(""),
        Text("✻ 完成 · 12.3s", MUTED),
        Text(""),
        user("那就更新断言。"),
        Text(""),
        tool(
            "●",
            OPERATION_COLOR,
            "PowerShell",
            OPERATION_COLOR,
            "python -m pytest -q",
            "",
            [("执行中…", OPERATION_COLOR)],
        ),
        Text(""),
        activity,
        prompt_box(),
        status(right="上下文 约 9K / 1M（1%） · 缓存命中 82%"),
    ]


def option(selected: bool, label: str, note: str) -> Text:
    text = Text()
    text.append("❯ " if selected else "  ", Style(color=SELECT, bold=True))
    text.append(label, Style(color=SELECT, bold=True) if selected else None)
    text.append("  " + note, MUTED)
    return text


def panels() -> list:
    hints = Table.grid(padding=(0, 2))
    hints.add_column(width=24, no_wrap=True)
    hints.add_column(no_wrap=True)
    for index, (name, note) in enumerate(
        (
            ("/help [命令]", "查看命令和快捷键"),
            ("/clear", "开始新会话：清空模型上下文和 Token 统计"),
            ("/compact [说明]", "压缩上下文：较早的对话换成摘要，近期消息保留"),
            ("/resume [ID]", "恢复本项目之前的会话"),
        )
    ):
        chosen = Style(color=SELECT, bold=True)
        hints.add_row(
            Text(("❯ " if not index else "  ") + name, chosen if not index else None),
            Text(note, chosen if not index else MUTED),
        )
    approval = Group(
        Text("─" * 96, OPERATION_COLOR),
        Padding(
            Group(
                Text("需要授权 · 写入文件", Style(color=OPERATION_COLOR, bold=True)),
                Text("hello.txt", PARAMETER_COLOR),
                Text("此操作没有明确的放行规则。", MUTED),
                Text("新建文件 hello.txt（1 行）"),
                Text("+Hello World", TOOL_SUCCESS_COLOR),
                Text(""),
                option(True, "本次允许", "仅执行当前调用"),
                option(False, "本会话允许", "同工具与目标，退出后失效"),
                option(False, "本会话自动接受编辑", "切换为接受编辑档位，命令仍需确认"),
                option(False, "永久允许", "保存到本项目本地配置"),
                option(False, "拒绝", "让模型调整策略"),
                Text("↑ / ↓ 选择 · Enter 确认 · Esc 拒绝本次 · Ctrl+C 取消任务", MUTED),
            ),
            (0, 1),
        ),
    )
    return [
        heading("输入框：空闲 / 计划模式 / 接受编辑"),
        prompt_box(),
        status(),
        Text(""),
        prompt_box(color=OPERATION_COLOR),
        status("‖ 计划模式", OPERATION_COLOR),
        Text(""),
        prompt_box(color=TOOL_SUCCESS_COLOR),
        status("⏵⏵ 接受编辑", TOOL_SUCCESS_COLOR),
        heading("命令提示"),
        prompt_box("/"),
        Padding(hints, (0, 1)),
        Padding(Text("↑ / ↓ 选择 · Tab 补全 · Enter 确认 · Esc 收起", MUTED), (0, 1)),
        heading("审批面板"),
        approval,
    ]


SECTIONS = {"logo": logos, "chat": chat, "panels": panels}


def cells(console: Console, renderables) -> list:
    rows = []
    for renderable in renderables:
        for line in console.render_lines(Padding(renderable, (0, 2)), pad=False):
            rows.append(
                [
                    {
                        "text": segment.text,
                        "fg": segment.style.color.get_truecolor().hex
                        if segment.style and segment.style.color and not segment.style.color.is_default
                        else None,
                        "bg": segment.style.bgcolor.get_truecolor().hex
                        if segment.style and segment.style.bgcolor and not segment.style.bgcolor.is_default
                        else None,
                        "bold": bool(segment.style and segment.style.bold),
                    }
                    for segment in line
                    if not segment.control
                ]
            )
    return rows


def main() -> None:
    arguments = sys.argv[1:]
    target = None
    if "--json" in arguments:
        at = arguments.index("--json")
        target = Path(arguments[at + 1])
        del arguments[at : at + 2]
    names = [name for name in arguments if name in SECTIONS] or list(SECTIONS)
    console = Console(width=100, highlight=False, color_system="truecolor", force_terminal=True, legacy_windows=False)
    for name in names:
        renderables = SECTIONS[name]()
        if target is None:
            for renderable in renderables:
                console.print(Padding(renderable, (0, 2)))
        else:
            target.mkdir(parents=True, exist_ok=True)
            rows = cells(console, renderables)
            (target / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if target is None:
        console.print()


if __name__ == "__main__":
    main()
