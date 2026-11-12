"""The lead's member panel and member view.

The widgets only show what MossCodeApp hands them; reading the team record and transcripts, and the
keys that route between them, stay in tui.py.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from .appearance import MUTED_COLOR, OPERATION_COLOR, TOOL_SUCCESS_COLOR
from .choice_list import KeyboardOptions
from .teams.runtime import STATUS_NAMES, MemberRow
from .teams.view import Feed

COMPACT_ROWS = 4
EXPANDED_ROWS = 8
VIEW_MESSAGES = 200
VIEW_SHARE = 1.0  # Of the rows left by the status line (and a card).
STOP_CONFIRM = 2.0  # Seconds between the two presses of x.
ASKING = "待审批"
SYMBOLS = {
    "running": ("●", TOOL_SUCCESS_COLOR),
    "idle": ("○", MUTED_COLOR),
    "stopped": ("·", MUTED_COLOR),
    "asking": ("◆", OPERATION_COLOR),
}
PANEL_HELP = "↑ / ↓ 选择 · Enter 查看对话 · x 停止成员 · Esc 收起"
VIEW_HELP = "PgUp / PgDn 滚动 · ← / → 切换成员 · Esc 返回 Lead"


def elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def state_of(row: MemberRow, asking: dict[str, int]) -> str:
    """A waiting approval shows over running."""
    return "asking" if asking.get(row.name) and row.status != "stopped" else row.status


def state_name(row: MemberRow, asking: dict[str, int]) -> str:
    state = state_of(row, asking)
    return ASKING if state == "asking" else STATUS_NAMES.get(state, state)


def _grid() -> Table:
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(no_wrap=True)  # Symbol and name.
    grid.add_column(no_wrap=True)  # State.
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")  # Task and tool: cut, never wrapped.
    grid.add_column(no_wrap=True, justify="right")  # Elapsed, pane.
    return grid


def _cells(row: MemberRow, asking: dict[str, int], now: float) -> tuple[Text, Text, Text, Text]:
    state = state_of(row, asking)
    symbol, style = SYMBOLS.get(state, ("·", MUTED_COLOR))
    name = Text(symbol + " ", style=style)
    name.append(row.name, style="bold" if state != "stopped" else MUTED_COLOR)
    detail = " · ".join(part for part in (row.task, row.last_tool if row.status == "running" else "") if part)
    tail = []
    if row.status == "running" and row.since is not None:
        tail.append(elapsed(now - row.since))
    if row.pane:
        tail.append("窗格")
    return (
        name,
        Text(state_name(row, asking), style=style if state != "idle" else MUTED_COLOR),
        Text(detail, style=MUTED_COLOR),
        Text(" · ".join(tail), style=MUTED_COLOR),
    )


def row_line(row: MemberRow, asking: dict[str, int], now: float) -> Table:
    grid = _grid()
    grid.add_row(*_cells(row, asking, now))
    return grid


def compact_rows(rows: list[MemberRow], asking: dict[str, int], now: float, limit: int = COMPACT_ROWS):
    """At most `limit` lines; stopped members, then idle ones, then the rest are summed up."""
    live = [row for row in rows if row.status != "stopped"]
    stopped = len(rows) - len(live)
    entries: list[tuple[MemberRow | Text, int]] = [(row, 1) for row in live]
    tail: list[tuple[Text, int]] = [(Text(f"已停止 {stopped}", style=MUTED_COLOR), stopped)] if stopped else []
    if len(entries) + len(tail) > limit:
        idle = [row for row in live if row.status == "idle" and not asking.get(row.name)]
        if len(idle) > 1:
            entries = [(row, 1) for row in live if row not in idle]
            names = "、".join(row.name for row in idle)
            entries.append(
                (Text(f"○ 空闲 {len(idle)}：{names}", style=MUTED_COLOR, no_wrap=True, overflow="ellipsis"), len(idle))
            )
    entries += tail
    if len(entries) > limit:
        hidden = sum(count for _, count in entries[limit - 1 :])
        entries = entries[: limit - 1] + [(Text(f"另有 {hidden} 个成员 · Ctrl+T 展开", style=MUTED_COLOR), hidden)]
    grid = _grid()
    for entry, _ in entries:
        if isinstance(entry, MemberRow):
            grid.add_row(*_cells(entry, asking, now))
        else:
            grid.add_row(entry, Text(""), Text(""), Text(""))
    return grid, len(entries)


class MemberList(KeyboardOptions):
    """The members to pick from; x stops the highlighted one."""

    BINDINGS = [Binding("x", "app.stop_member", "停止成员", show=False)]


class MemberPanel(Vertical):
    """Compact lines under the prompt; Ctrl+T turns it into a list to pick from."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: list[MemberRow] = []
        self.asking: dict[str, int] = {}
        self.expanded = False
        self.lines = 0  # Rows it takes on screen, for MossCodeApp._fit_chat.

    def compose(self) -> ComposeResult:
        yield Static("", id="member-compact")
        members = MemberList(id="member-list")
        members.display = False
        yield members
        help_line = Static(PANEL_HELP, id="member-help", markup=False)
        help_line.display = False
        yield help_line

    @property
    def live(self) -> bool:
        return any(row.status != "stopped" for row in self.rows)

    @property
    def selected(self) -> str:
        members = self.query_one("#member-list", MemberList)
        index = members.highlighted
        return self.rows[index].name if index is not None and index < len(self.rows) else ""

    def show(self, rows: list[MemberRow], asking: dict[str, int], now: float) -> None:
        self.rows, self.asking = rows, asking
        if self.expanded:
            members = self.query_one("#member-list", MemberList)
            chosen = self.selected
            members.clear_options()
            members.add_options([row_line(row, asking, now) for row in rows])
            members.styles.height = max(1, min(EXPANDED_ROWS, len(rows)))
            names = [row.name for row in rows]
            if rows:
                members.highlighted = names.index(chosen) if chosen in names else 0
            self.lines = min(EXPANDED_ROWS, max(1, len(rows))) + 1
        else:
            grid, self.lines = compact_rows(rows, asking, now)
            self.query_one("#member-compact", Static).update(grid)

    def expand(self, now: float, select: str = "") -> None:
        self.expanded = True
        self.query_one("#member-compact").display = False
        members = self.query_one("#member-list", MemberList)
        members.display = True
        self.query_one("#member-help").display = True
        self.help(PANEL_HELP)
        self.show(self.rows, self.asking, now)
        names = [row.name for row in self.rows]
        if select in names:
            members.highlighted = names.index(select)
        members.focus()

    def collapse(self, now: float) -> None:
        self.expanded = False
        self.query_one("#member-list").display = False
        self.query_one("#member-help").display = False
        self.query_one("#member-compact").display = True
        self.show(self.rows, self.asking, now)

    def help(self, text: str) -> None:
        self.query_one("#member-help", Static).update(text)


class MemberLog(RichLog):
    """PgUp/PgDn, Home/End and ↑/↓ scroll (RichLog's own keys); ←/→ switch members."""

    BINDINGS = [Binding("left", "app.member_step(-1)", show=False), Binding("right", "app.member_step(1)", show=False)]


class MemberView(Vertical):
    """Header, the member's conversation, its calls still running, help."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.name_shown = ""
        self.team = ""
        self.tail = None  # teams.view.TranscriptTail of the member shown.
        self.feed = Feed()
        self.unseen = False

    def compose(self) -> ComposeResult:
        yield Static("", id="member-head", markup=False)
        yield MemberLog(id="member-log", wrap=True, markup=False, max_lines=5000)
        running = Static("", id="member-running", markup=False)
        running.display = False
        yield running
        yield Static(VIEW_HELP, id="member-view-help", markup=False)

    @property
    def member_log(self) -> MemberLog:
        return self.query_one("#member-log", MemberLog)

    def head(self, row: MemberRow | None, asking: dict[str, int]) -> None:
        text = Text(f"成员 {self.name_shown} · 团队 {self.team}", style="bold")
        if row is None:
            text.append(" · 已不在团队中", style=MUTED_COLOR)
        else:
            text.append(
                " · " + state_name(row, asking), style=OPERATION_COLOR if row.status != "stopped" else MUTED_COLOR
            )
            if row.status == "running" and row.last_tool:
                text.append(" · " + row.last_tool, style=MUTED_COLOR)
        self.query_one("#member-head", Static).update(text)

    def reset(self, name: str, team: str, tail) -> None:
        self.name_shown, self.team, self.tail, self.unseen = name, team, tail, False
        self.feed = Feed()
        self.member_log.clear()
        self.show_running()
        self.help()

    def write(self, lines: list, *, follow: bool | None = None) -> None:
        """Follow only while at the bottom; otherwise say there is more."""
        if not lines:
            return
        log = self.member_log
        follow = log.is_vertical_scroll_end if follow is None else follow
        for line in lines:
            log.write(line, scroll_end=follow)
        if not follow and not self.unseen:
            self.unseen = True
            self.help()

    def show_running(self) -> None:
        running = self.query_one("#member-running", Static)
        running.update(self.feed.running_text())
        running.display = bool(self.feed.waiting)

    def help(self) -> None:
        text = Text(VIEW_HELP, style=MUTED_COLOR)
        if self.unseen:
            text = Text("有新内容 · End 回到底部", style=OPERATION_COLOR)
            text.append(" · " + VIEW_HELP, style=MUTED_COLOR)
        self.query_one("#member-view-help", Static).update(text)

    def seen(self) -> None:
        if self.unseen and self.member_log.is_vertical_scroll_end:
            self.unseen = False
            self.help()


__all__ = [
    "ASKING",
    "COMPACT_ROWS",
    "EXPANDED_ROWS",
    "MemberList",
    "MemberLog",
    "MemberPanel",
    "MemberView",
    "PANEL_HELP",
    "STOP_CONFIRM",
    "VIEW_HELP",
    "VIEW_MESSAGES",
    "VIEW_SHARE",
    "compact_rows",
    "elapsed",
    "row_line",
    "state_name",
]
