"""The sub-agents of one session, foreground or background."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Awaitable, Callable, Literal

from ..messages import UsageTotals

STATUS_LABELS = {"running": "运行中", "completed": "已完成", "failed": "失败", "cancelled": "已取消"}
ORIGIN_LABELS = {"agent": "agent 工具", "skill": "Skill fork", "hook": "Hook"}
CANCEL_WAIT = 5.0


@dataclass
class Task:
    id: str
    origin: Literal["agent", "skill", "hook"]
    agent: Literal["fork", "subagent"]
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
    job: asyncio.Task | None = field(default=None, repr=False)
    detach: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    policy: object = field(default=None, repr=False)  # The PolicyView, switched to non-interactive on detach.
    isolated: bool = False  # Runs in a worktree (asked for and not skipped by plan mode).
    root: Path | None = None  # The worktree directory it works in; None for the project.
    # {"name", "path", "branch", "base", "outcome"}; outcome is 使用中 / 已保留 / 已删除 / 删除未完成.
    worktree: dict | None = None

    @property
    def elapsed(self) -> float:
        return (self.ended if self.ended is not None else time.monotonic()) - self.started

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    @property
    def label(self) -> str:
        return f"{self.agent_type} · {self.description}"

    def to_background(self) -> None:
        self.background = True
        if self.policy is not None:
            self.policy.to_background()


def task_line(task: Task) -> str:
    """The line shown when a background task starts or ends."""
    state = "已启动" if task.status == "running" else STATUS_LABELS[task.status]
    return f"后台任务 {task.id}（{task.label}）{state}"


class Announcer:
    """One line when a background task starts and one when it ends, however often on_change fires between.

    Hook tasks are left out: the hook engine reports them.
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
            if task.status == "running":
                self.say(task_line(task))
        if task.status != "running" and task.id not in self.ended:
            self.ended.add(task.id)
            self.say(task_line(task))


class TaskManager:
    def __init__(self, *, max_background: int = 4):
        self.tasks: dict[str, Task] = {}
        self.max_background = max_background
        self._next = 0
        self.on_finish: Callable[[Task], None] | None = None
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
        return len(self.background()) >= self.max_background

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

    async def _run(self, task: Task, run) -> str:
        try:
            text = await run()
        except asyncio.CancelledError:
            text = task.result or "[子 Agent 未完成：已取消]"
            task.status = "cancelled"
        except Exception as exc:  # run() turns provider errors into text; anything else is a bug.
            text = f"[子 Agent 未完成：内部错误 {type(exc).__name__}]"
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

    async def stop(self, task_id: str) -> bool:
        """True when it was running; its notification is never sent."""
        task = self.tasks.get(task_id)
        if task is None or task.status != "running" or task.job is None:
            return False
        task.stopped = task.notified = True
        task.job.cancel()
        await asyncio.gather(task.job, return_exceptions=True)
        return True

    async def cancel_all(self, wait: float = CANCEL_WAIT) -> int:
        """On exit and when the user confirms: cancel, wait a little, never notify."""
        jobs = []
        for task in self.running():
            task.stopped = task.notified = True
            if task.job is not None:
                task.job.cancel()
                jobs.append(task.job)
        if jobs:
            await asyncio.wait(jobs, timeout=wait)
        return len(jobs)

    async def reset(self) -> None:
        """/clear and resume start with an empty list."""
        await self.cancel_all()
        self.tasks.clear()
