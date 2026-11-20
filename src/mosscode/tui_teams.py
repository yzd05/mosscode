"""Team member panel, member log tailing, stop confirmation, and member view methods."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.widgets import Static

from .appearance import ERROR_COLOR, MUTED_COLOR, OPERATION_COLOR
from .member_ui import PANEL_HELP, STOP_CONFIRM, VIEW_MESSAGES, MemberList, MemberPanel, MemberView
from .teams import TeamError
from .teams.view import TranscriptTail
from .tui_views import PromptArea, todo_panel


class _TeamViewMixin:
    """Team member list, stop confirmation, and member transcript view methods mixed into MossCodeApp."""

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
        asking, now = self._asking(), self._now()
        panel = self.query_one("#member-panel", MemberPanel)
        view = self.query_one("#member-view", MemberView)
        if view.display and (current is None or current.name != view.team):
            self._close_view(to_panel=False)
            self.call_later(self._local_note, f"└ 团队 {view.team} 已不是当前团队，成员视图已关闭")
        if panel.expanded and not rows:
            panel.collapse(now)
            self._focus_home()
        panel.show(rows, asking, now)
        shown = (
            not view.display
            and not self.query_one("#model-panel").display
            and not self.query_one("#connect-panel").display
            and (panel.expanded or panel.live)
        )
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
        card = (
            self.query_one("#model-panel").display
            or self.query_one("#connect-panel").display
            or self.query_one("#approval-panel").display
            or (self.query_one("#question-panel").display and not self._question_typing)
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
        if (
            self._approval_request
            or self._question
            or self.query_one("#model-panel").display
            or self.query_one("#connect-panel").display
        ):
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
            self._hint_until = self._now() + 2
            self._status("没有当前团队 · 让模型用 team_create 建一个")
            return
        if not self._member_rows:
            self._hint_until = self._now() + 2
            self._status(f"团队 {teams.current.name} 还没有成员")
            self._schedule_members()
            return
        panel.display = True
        panel.expand(self._now())
        self._show_members()

    def _collapse_members(self) -> None:
        self._stop_armed = None
        self.query_one("#member-panel", MemberPanel).collapse(self._now())
        self.query_one("#prompt", PromptArea).focus()
        self._show_members()

    def action_stop_member(self) -> None:
        """X twice within STOP_CONFIRM seconds, as /team stop."""
        teams = getattr(self.conversation, "teams", None)
        team_name = teams.current.name if teams is not None and teams.current is not None else None
        if team_name is None:
            return
        panel = self.query_one("#member-panel", MemberPanel)
        name = panel.selected
        row = next((row for row in panel.rows if row.name == name), None)
        if row is None:
            return
        if row.status == "stopped":
            panel.help(f"成员 {name} 已经停止 · " + PANEL_HELP)
            return
        armed = self._stop_armed
        if armed is not None and armed[0] == team_name and armed[1] == name and self._now() <= armed[2]:
            self._stop_armed = None
            panel.help(PANEL_HELP)
            self.call_later(self._stop_member, name, team_name)
            return
        self._stop_armed = (team_name, name, self._now() + STOP_CONFIRM)
        panel.help(f"再按 x 停止成员 {name}")
        self.set_timer(STOP_CONFIRM + 0.05, self._disarm_stop)

    def _disarm_stop(self) -> None:
        if not self._members_mounted() or self._stop_armed is None or self._now() <= self._stop_armed[2]:
            return
        self._stop_armed = None
        self.query_one("#member-panel", MemberPanel).help(PANEL_HELP)

    async def _stop_member(self, name: str, expected_team: str | None = None) -> None:
        try:
            text = await self.conversation.teams.stop_member(name, expected_team=expected_team)
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
            panel.expand(self._now(), select=name)
        else:
            if panel.expanded:
                panel.collapse(self._now())
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
