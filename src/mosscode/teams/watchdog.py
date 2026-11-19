"""Zero-token deterministic stall watchdog and peer-handoff circuit breaker."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .blackboard import BoardPost, mentions_actor
from .board import STATUS_LABELS
from .lock import TeamError
from .mailbox import LEAD

if TYPE_CHECKING:
    from . import Team, TeamStore

MAX_PEER_HOPS = 12
DEFAULT_STALL_AFTER = 15.0

TaskSnapshot = tuple[tuple[str, str, str], ...]
StallKey = tuple[int, TaskSnapshot]


@dataclass(frozen=True)
class WatchdogAction:
    wake_members: tuple[str, ...] = ()
    stall_post: BoardPost | None = None


def reset_peer_hops(store: TeamStore) -> None:
    """Reset consecutive peer-only hop count when Lead intervenes or a task completes."""
    try:
        team = store.load()
    except TeamError:
        return
    if team.peer_hops == 0:
        return

    def clear(current: Team) -> None:
        current.peer_hops = 0

    try:
        store.modify(clear)
    except TeamError:
        pass


def try_peer_handoff(store: TeamStore, *, max_hops: int = MAX_PEER_HOPS) -> tuple[int, bool]:
    """Atomically increment peer_hops in config.json before appending the handoff post."""
    hops = 0
    tripped = False

    def bump(team: Team) -> None:
        nonlocal hops, tripped
        if team.peer_hops >= max_hops:
            hops = team.peer_hops
            tripped = True
            return
        team.peer_hops += 1
        hops = team.peer_hops
        tripped = hops >= max_hops

    store.modify(bump)
    return hops, tripped


def escalate_peer_handoff(
    store: TeamStore,
    sender: str,
    peer_targets: Sequence[str],
    post_seq: int,
    *,
    hops: int,
    max_hops: int = MAX_PEER_HOPS,
) -> BoardPost:
    """Advance target cursors past the tripped post and append a watchdog escalation naming @lead."""

    def advance_targets(team: Team) -> None:
        for target in peer_targets:
            member = team.member(target)
            if member is not None and member.board_cursor < post_seq:
                member.board_cursor = post_seq

    store.modify(advance_targets)
    targets_label = "、".join(peer_targets)
    summary = f"点对点交接达 {max_hops} 跳上限，已熔断"
    body = (
        f"[circuit-breaker] 成员点对点交接已连续达到 {hops} 跳上限"
        f"（最近：{sender} → {targets_label}，#{post_seq}），已抑制自动唤醒，请 @lead 介入裁决。"
    )
    return store.blackboard.append(
        "watchdog",
        body,
        summary,
        kind="watchdog",
        explicit_mentions=(LEAD,),
    )


class SwarmWatchdog:
    """Deterministic 0-token stall detector and missed-mention waker."""

    def __init__(self, *, stall_after: float = DEFAULT_STALL_AFTER):
        self.stall_after = stall_after
        self.last_active = time.monotonic()
        self._observed_state: StallKey | None = None
        self._last_stall_state: StallKey | None = None

    def reset(self) -> None:
        self.last_active = time.monotonic()
        self._observed_state = None
        self._last_stall_state = None

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def check(
        self,
        store: TeamStore,
        *,
        runner_states: Mapping[str, str],
        lead_busy: bool,
        has_pending_notifications: bool = False,
    ) -> WatchdogAction:
        now_mono = time.monotonic()
        try:
            team = store.load()
            posts, head_seq = store.blackboard.read_since(0, limit=1_000_000)
            tasks = store.board.list()
        except TeamError:
            return WatchdogAction()

        done_seq = team.done_seq
        if done_seq == 0:
            found_done = next((post.seq for post in reversed(posts) if post.kind == "done"), 0)
            if found_done:
                done_seq = found_done

                def mark_done(current: Team, seq: int = found_done) -> None:
                    if current.done_seq == 0:
                        current.done_seq = seq

                try:
                    store.modify(mark_done)
                except TeamError:
                    pass

        if team.peer_hops < MAX_PEER_HOPS:
            to_wake: list[str] = []
            for member in team.members:
                if runner_states.get(member.name) != "idle":
                    continue
                latest_mention = max((post.seq for post in posts if mentions_actor(post, member.name)), default=0)
                if member.board_cursor < latest_mention:
                    to_wake.append(member.name)
            if to_wake:
                self.last_active = now_mono
                return WatchdogAction(wake_members=tuple(to_wake))

        ordered = sorted(tasks, key=lambda item: int(item.id) if item.id.isdigit() else 0)
        incomplete: TaskSnapshot = tuple(
            (task.id, task.status, task.owner) for task in ordered if task.status in {"pending", "in_progress"}
        )
        state: StallKey = (head_seq, incomplete)
        if self._observed_state != state:
            self._observed_state = state
            self.last_active = now_mono

        lead_latest = max((post.seq for post in posts if mentions_actor(post, LEAD)), default=0)
        if (
            any(status == "running" for status in runner_states.values())
            or lead_busy
            or team.lead_board_cursor < lead_latest
        ):
            self.last_active = now_mono
            return WatchdogAction()

        try:
            if store.mailbox.unread_count(LEAD) > 0 or any(
                store.mailbox.unread_count(name) > 0
                for name, status in runner_states.items()
                if status in {"running", "idle"}
            ):
                self.last_active = now_mono
                return WatchdogAction()
        except TeamError:
            pass

        if done_seq != 0 or not incomplete:
            return WatchdogAction()
        if now_mono - self.last_active < self.stall_after:
            return WatchdogAction()
        if state == self._last_stall_state:
            return WatchdogAction()

        listed = "、".join(
            f"{task_id}[{STATUS_LABELS.get(status, status)}" + (f":{owner}]" if owner else "]")
            for task_id, status, owner in incomplete[:10]
        )
        summary = f"团队停滞告警：仍有 {len(incomplete)} 个未完成任务"
        body = (
            f"[stall] 所有成员与 Lead 均已空闲超过 {self.stall_after:g} 秒，"
            f"但任务面板仍有未完成任务（{listed}）。请 @lead 检查进度、指派返工或发布 [done] 收尾。"
        )
        try:
            post = store.blackboard.append(
                "watchdog",
                body,
                summary,
                kind="watchdog",
                explicit_mentions=(LEAD,),
            )
        except TeamError:
            return WatchdogAction()

        settled: StallKey = (post.seq, incomplete)
        self._last_stall_state = settled
        self._observed_state = settled
        self.last_active = now_mono
        return WatchdogAction(stall_post=post)
