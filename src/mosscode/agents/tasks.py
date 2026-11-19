"""The tasks of one session: sub-agents, background commands, and /loop schedules."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..messages import UsageTotals

STATUS_LABELS = {"running": "运行中", "completed": "已完成", "failed": "失败", "cancelled": "已取消"}
ORIGIN_LABELS = {
    "agent": "agent 工具",
    "skill": "Skill fork",
    "hook": "Hook",
    "command": "execute_command",
    "loop": "/loop 定时",
}
CANCEL_WAIT = 5.0


@dataclass
class CommandInfo:
    """What a background command task has instead of a conversation."""

    command: str
    cwd: Path
    log: object  # The CommandLog its output goes to.
    owner: str = ""  # "" for the main conversation, else the sub-agent task id or member:<name> it ends with.
    sandbox: str = ""  # How it was confined, for /tasks; empty outside the sandbox.
    time_limit: float | None = None  # Seconds from the start (or the move to the background); None for none.
    exit_code: int | None = None
    end_reason: str = ""  # Why MossCode ended it (time or output limit), empty when it exited on its own.
    notes: list[str] = field(default_factory=list)  # Sandbox notes, the refused domains among them.
    tail: str = ""  # The last lines of output when it ended, for the notification.


@dataclass
class LoopInfo:
    """State of a session-scoped /loop recurring schedule."""

    interval: float
    interval_text: str
    prompt: str
    max_runs: int = 100
    expires_at: float = 0.0
    runs: int = 0
    next_fire: float | None = None
    pending_fire: bool = False
    end_reason: str = ""


@dataclass
class Task:
    id: str
    origin: Literal["agent", "skill", "hook", "command", "loop"]
    agent: Literal["fork", "subagent", "command", "loop"]
    agent_type: str
    description: str
    prompt: str
    call_id: str = ""
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    background: bool = False
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    tool_calls: int = 0
    last_tool: str = ""
    result: str | None = None
    transcript: Path | None = None
    notified: bool = False
    stopped: bool = False  # Stopped by /tasks stop or task_stop: never notified.
    pending_approvals: set[str] = field(default_factory=set)
    job: asyncio.Task | None = field(default=None, repr=False)
    detach: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    policy: object = field(default=None, repr=False)  # The PolicyView of this sub-agent.
    isolated: bool = False  # Runs in a worktree (asked for and not skipped by plan mode).
    root: Path | None = None  # The worktree directory it works in; None for the project.
    # {"name", "path", "branch", "base", "outcome"}; outcome is 使用中 / 已保留 / 已删除 / 删除未完成.
    worktree: dict | None = None
    command: CommandInfo | None = None  # Set for a background command.
    loop: LoopInfo | None = None  # Set for a /loop recurring schedule.

    @property
    def kind(self) -> Literal["agent", "command", "loop"]:
        if self.loop is not None:
            return "loop"
        if self.command is not None:
            return "command"
        return "agent"

    @property
    def elapsed(self) -> float:
        return (self.ended if self.ended is not None else time.monotonic()) - self.started

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    @property
    def label(self) -> str:
        return f"{self.agent_type} · {self.description}"

    def to_background(self, *, interactive: bool = False) -> None:
        self.background = True
        if not interactive and self.policy is not None:
            self.policy.to_background()


def task_line(task: Task) -> str:
    """The line shown when a background task starts or ends."""
    state = "已启动" if task.status == "running" else STATUS_LABELS[task.status]
    if task.command is not None and task.status != "running" and task.command.exit_code is not None:
        state += f"，退出码 {task.command.exit_code}"
    elif task.loop is not None and task.status != "running" and task.loop.end_reason:
        state += f"（{task.loop.end_reason}）"
    return f"后台任务 {task.id}（{task.label}）{state}"


class Announcer:
    """One line when a background task starts and one when it ends, however often on_change fires between.

    Hook tasks are left out: the hook engine reports them. /loop starts and manual stops are reported by /loop.
    """

    def __init__(self, say: Callable[[str], None]):
        self.say = say
        self.started: set[str] = set()
        self.ended: set[str] = set()

    def __call__(self, task: Task) -> None:
        if not task.background or task.origin == "hook":
            return
        if task.id not in self.started:
            self.started.add(task.id)
            if task.status == "running" and task.origin != "loop":
                self.say(task_line(task))
        if task.status != "running" and task.id not in self.ended:
            self.ended.add(task.id)
            if not (task.origin == "loop" and task.stopped):
                self.say(task_line(task))


class TaskManager:
    def __init__(self, *, max_background: int = 4):
        self.tasks: dict[str, Task] = {}
        self.max_background = max_background
        self._next = 0
        self.on_finish: Callable[[Task], None] | None = None
        self.on_cancel: Callable[[Task], None] | None = None
        self.on_change: Callable[[Task], None] | None = None  # The UI refreshes a tool row or the status bar.
        self.finished = asyncio.Event()  # Set whenever a task ends; -p waits on it.

    def new(self, **fields) -> Task:
        self._next += 1
        task = Task(f"t{self._next}", **fields)
        self.tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def running(self) -> list[Task]:
        return [task for task in self.tasks.values() if task.status == "running"]

    def background(self) -> list[Task]:
        return [task for task in self.running() if task.background]

    def full(self) -> bool:
        """Background commands and /loop schedules have their own limits."""
        return len([task for task in self.background() if task.kind == "agent"]) >= self.max_background

    def changed(self, task: Task) -> None:
        if self.on_change is not None:
            try:
                self.on_change(task)
            except Exception:
                pass

    def launch(self, task: Task, run: Callable[[], Awaitable[str]]) -> asyncio.Task:
        task.job = asyncio.get_running_loop().create_task(self._run(task, run))
        self.changed(task)
        return task.job

    @staticmethod
    def _cancelled_text(task: Task) -> str:
        if task.kind == "command":
            return "[命令已停止]"
        if task.kind == "loop":
            return "[定时巡检已停止]"
        return "[子 Agent 未完成：已取消]"

    async def _run(self, task: Task, run) -> str:
        try:
            text = await run()
        except asyncio.CancelledError:
            text = task.result or self._cancelled_text(task)
            task.status = "cancelled"
        except Exception as exc:  # run() turns provider errors into text; anything else is a bug.
            label = "定时巡检" if task.kind == "loop" else "子 Agent "
            text = f"[{label}未完成：内部错误 {type(exc).__name__}]"
            task.status = "failed"
        self.settle(task, text)
        return text

    def settle(self, task: Task, text: str) -> None:
        """The end of a task, also of one run outside launch (a Skill fork)."""
        task.result = text
        task.ended = time.monotonic()
        if task.status == "running":
            task.status = "completed"
        self.finished.set()
        self.changed(task)
        if self.on_finish is not None:
            try:
                self.on_finish(task)
            except Exception:
                pass

    def detach_foreground(self) -> int:
        """ctrl+b moves every running foreground sub-agent of the agent tool to the background."""
        count = 0
        for task in self.running():
            if not task.background and task.origin == "agent" and not task.detach.is_set():
                task.detach.set()
                count += 1
        return count

    def _cancelling(self, task: Task) -> None:
        if self.on_cancel is not None:
            try:
                self.on_cancel(task)
            except AssertionError:
                raise
            except Exception:
                pass

    async def stop(self, task_id: str) -> bool:
        """True when it was running; its notification is never sent."""
        task = self.tasks.get(task_id)
        if task is None or task.status != "running" or task.job is None:
            return False
        task.stopped = task.notified = True
        if task.loop is not None:
            task.loop.next_fire = None
            task.loop.pending_fire = False
        self._cancelling(task)
        task.job.cancel()
        if task.job is not asyncio.current_task():
            await asyncio.gather(task.job, return_exceptions=True)
            if task.status == "running":
                task.status = "cancelled"
                self.settle(task, task.result or self._cancelled_text(task))
        return True

    async def cancel_all(self, wait: float = CANCEL_WAIT) -> int:
        """On exit and when the user confirms: cancel, wait a little, never notify."""
        jobs = []
        current = asyncio.current_task()
        for task in self.running():
            task.stopped = task.notified = True
            if task.loop is not None:
                task.loop.next_fire = None
                task.loop.pending_fire = False
            self._cancelling(task)
            if task.job is not None:
                task.job.cancel()
                if task.job is not current:
                    jobs.append(task.job)
        if jobs:
            await asyncio.wait(jobs, timeout=wait)
            for task in list(self.tasks.values()):
                if task.status == "running" and task.job is not None and task.job.done():
                    task.status = "cancelled"
                    self.settle(task, task.result or self._cancelled_text(task))
        return len(jobs)

    async def reset(self) -> None:
        """/clear and resume start with an empty list."""
        await self.cancel_all()
        self.tasks.clear()
