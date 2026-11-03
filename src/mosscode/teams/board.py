"""The team's shared task list, tasks.json under its lock.

Not agents/tasks.py: those are the one-shot sub-agents of a session (/tasks, task_output).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .lock import TeamError, atomic_write, file_lock, load_json, now
from .mailbox import LEAD

STATUSES = ("pending", "in_progress", "completed")
STATUS_LABELS = {"pending": "待办", "in_progress": "进行中", "completed": "已完成"}
TASK_LIMIT = 200
SUBJECT_LIMIT = 120
DESCRIPTION_LIMIT = 20000


@dataclass
class TeamTask:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str = ""
    blocked_by: list[str] = field(default_factory=list)
    created: str = field(default_factory=now)
    updated: str = field(default_factory=now)

    @classmethod
    def from_dict(cls, data: dict) -> TeamTask:
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True)
class Updated:
    task: TeamTask
    ready: tuple[TeamTask, ...] = ()  # Pending tasks this update made ready.


def _valid(data) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("tasks"), list)
        and all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in data["tasks"])
    )


def is_ready(task: TeamTask, tasks: dict[str, TeamTask]) -> bool:
    """Every task it waits for is completed."""
    return all(tasks[dep].status == "completed" for dep in task.blocked_by if dep in tasks)


def blocks(task_id: str, tasks: Iterable[TeamTask]) -> list[str]:
    """Derived, never stored."""
    return [task.id for task in tasks if task_id in task.blocked_by]


def _reaches(start: str, target: str, tasks: dict[str, TeamTask]) -> bool:
    """Whether start waits, directly or not, for target."""
    seen, stack = set(), [start]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in seen or current not in tasks:
            continue
        seen.add(current)
        stack.extend(tasks[current].blocked_by)
    return False


def _text(value: str, name: str, limit: int, *, required: bool = False) -> str:
    value = value.strip()
    if required and not value:
        raise TeamError("invalid_task", f"{name} 不能为空。")
    if len(value) > limit:
        raise TeamError("invalid_task", f"{name} 最多 {limit} 字。")
    return value


class TaskBoard:
    def __init__(self, path: Path, on_notice: Callable[[str], None] | None = None):
        self.path = path
        self.on_notice = on_notice

    def _load(self) -> dict[str, TeamTask]:
        data = load_json(self.path, lambda: {"v": 1, "tasks": []}, _valid, self.on_notice)
        tasks = {}
        for item in data["tasks"]:
            try:
                task = TeamTask.from_dict(item)
            except TypeError:
                continue
            tasks[task.id] = task
        return tasks

    def _save(self, tasks: dict[str, TeamTask]) -> None:
        atomic_write(self.path, {"v": 1, "tasks": [asdict(task) for task in tasks.values()]})

    def list(self) -> list[TeamTask]:
        return list(self._load().values())

    def get(self, task_id: str) -> TeamTask:
        task = self._load().get(task_id)
        if task is None:
            raise TeamError("unknown_task", f"没有编号为 {task_id} 的任务。")
        return task

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: Iterable[str] = (),
        *,
        owner: str = "",
        actor: str = LEAD,
        participants: Iterable[str] = (),
    ) -> TeamTask:
        subject = _text(subject, "subject", SUBJECT_LIMIT, required=True)
        description = _text(description, "description", DESCRIPTION_LIMIT)
        with file_lock(self.path):
            tasks = self._load()
            if len(tasks) >= TASK_LIMIT:
                raise TeamError("too_many_tasks", f"一个团队最多 {TASK_LIMIT} 个任务。")
            deps = self._deps(blocked_by, tasks)
            owner = self._owner(owner, actor, participants)
            number = max((int(key) for key in tasks if key.isdigit()), default=0) + 1
            task = TeamTask(str(number), subject, description, owner=owner, blocked_by=deps)
            tasks[task.id] = task
            self._save(tasks)
        return task

    def update(
        self,
        task_id: str,
        *,
        actor: str,
        participants: Iterable[str] = (),
        status: str | None = None,
        owner: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        add_blocked_by: Iterable[str] = (),
        remove_blocked_by: Iterable[str] = (),
    ) -> Updated:
        """The result lists the tasks that became ready."""
        add_blocked_by, remove_blocked_by = list(add_blocked_by), list(remove_blocked_by)
        with file_lock(self.path):
            tasks = self._load()
            task = tasks.get(task_id)
            if task is None:
                raise TeamError("unknown_task", f"没有编号为 {task_id} 的任务。")
            lead = actor == LEAD
            if not lead and task.owner not in {"", actor}:
                raise TeamError("not_owner", f"任务 {task_id} 归 {task.owner} 负责，只有负责人或 Lead 能修改。")
            if remove_blocked_by and not lead:
                raise TeamError("lead_only", "只有 Lead 能移除依赖。")
            before = {key: is_ready(item, tasks) for key, item in tasks.items() if item.status == "pending"}
            if subject is not None:
                task.subject = _text(subject, "subject", SUBJECT_LIMIT, required=True)
            if description is not None:
                task.description = _text(description, "description", DESCRIPTION_LIMIT)
            if owner is not None:
                task.owner = self._owner(owner, actor, participants)
            if remove_blocked_by:
                task.blocked_by = [dep for dep in task.blocked_by if dep not in remove_blocked_by]
            for dep in self._deps(add_blocked_by, tasks):
                if dep == task.id or _reaches(dep, task.id, tasks):
                    raise TeamError("dependency_cycle", f"任务 {task.id} 依赖 {dep} 会形成循环依赖。")
                if dep not in task.blocked_by:
                    task.blocked_by.append(dep)
            if status is not None:
                self._move(task, status, actor, tasks)
            task.updated = now()
            self._save(tasks)
        ready = tuple(
            tasks[key]
            for key, was in before.items()
            if not was and tasks[key].status == "pending" and is_ready(tasks[key], tasks)
        )
        return Updated(task, ready)

    @staticmethod
    def _deps(blocked_by: Iterable[str], tasks: dict[str, TeamTask]) -> list[str]:
        deps = []
        for dep in blocked_by:
            dep = str(dep).strip()
            if dep not in tasks:
                raise TeamError("unknown_task", f"依赖的任务 {dep} 不存在。")
            if dep not in deps:
                deps.append(dep)
        return deps

    @staticmethod
    def _owner(owner: str, actor: str, participants: Iterable[str]) -> str:
        """A member takes tasks for itself only; the lead assigns anyone on the roster."""
        owner = owner.strip()
        if not owner:
            if actor != LEAD:
                raise TeamError("lead_only", "只有 Lead 能清除负责人。")
            return ""
        if actor != LEAD and owner != actor:
            raise TeamError("not_owner", "成员只能把任务领给自己。")
        if owner != LEAD and owner not in set(participants):
            raise TeamError("unknown_member", f"团队中没有成员 {owner}；先用 team_spawn 创建它，或先不填负责人。")
        return owner

    @staticmethod
    def _move(task: TeamTask, status: str, actor: str, tasks: dict[str, TeamTask]) -> None:
        """Forward only, and only once ready; the lead may send a task back to pending."""
        if status not in STATUSES:
            raise TeamError("invalid_task", f"状态只能是 {'、'.join(STATUSES)}。")
        old, new = STATUSES.index(task.status), STATUSES.index(status)
        if new < old:
            if actor != LEAD or status != "pending":
                raise TeamError("invalid_transition", "状态只能前进；只有 Lead 能把任务改回 pending。")
        elif new > old:
            if not is_ready(task, tasks):
                waiting = [dep for dep in task.blocked_by if dep in tasks and tasks[dep].status != "completed"]
                raise TeamError("task_blocked", f"任务 {task.id} 还在等待任务 {'、'.join(waiting)} 完成。")
            if not task.owner:
                if actor == LEAD:
                    raise TeamError("no_owner", f"任务 {task.id} 没有负责人；先指定 owner。")
                task.owner = actor
        task.status = status
