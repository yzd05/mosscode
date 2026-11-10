"""The task checklist a model keeps with todo_write: one per agent, in memory, recorded in the transcript."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from .prompts import Supplement

Status = Literal["pending", "in_progress", "completed"]
STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")
MAX_ITEMS = 50
MAX_CONTENT = 200
MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


class TodoError(ValueError):
    pass


@dataclass(frozen=True)
class TodoItem:
    id: str
    content: str
    status: str

    def to_json(self) -> dict:
        return {"id": self.id, "content": self.content, "status": self.status}


def items_from_json(raw) -> tuple[TodoItem, ...]:
    """Items of a transcript record; ValueError when malformed."""
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise ValueError("todo 记录不合法")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("todo 记录不合法")
        item_id, content, status = entry.get("id"), entry.get("content"), entry.get("status")
        if not isinstance(item_id, str) or not item_id or not isinstance(content, str) or status not in STATUSES:
            raise ValueError("todo 记录不合法")
        items.append(TodoItem(item_id, content, status))
    if len({item.id for item in items}) != len(items):
        raise ValueError("todo 记录不合法")
    return tuple(items)


def completed(items: Sequence[TodoItem]) -> int:
    return sum(item.status == "completed" for item in items)


def render(items: Sequence[TodoItem]) -> str:
    if not items:
        return "（清单为空）"
    lines = [f"{MARKERS[item.status]} {item.id}. {item.content}" for item in items]
    return "\n".join(lines) + f"\n已完成 {completed(items)}/{len(items)}"


class TodoList:
    """The current items, and what they were after each change, so /rewind can step back.

    mark is set by the agent loop before each tool batch: the history length that includes the
    assistant message making the call. A change survives a rewind to keep only if its mark <= keep.
    """

    def __init__(self) -> None:
        self.items: tuple[TodoItem, ...] = ()
        self.mark = 0
        self._log: list[tuple[int, tuple[TodoItem, ...]]] = []
        self._next = 1
        self.on_change: Callable[[tuple[TodoItem, ...], int], None] | None = None

    def replace(self, entries: Sequence[tuple[str | None, str, str]]) -> tuple[TodoItem, ...]:
        """The whole new list as (id or None for a new item, content, status); nothing changes on error."""
        if len(entries) > MAX_ITEMS:
            raise TodoError(f"清单最多 {MAX_ITEMS} 项。")
        known = {item.id for item in self.items}
        seen: set[str] = set()
        items, next_id = [], self._next
        for item_id, content, status in entries:
            content = " ".join(content.split())
            if not content:
                raise TodoError("content 不能为空。")
            if len(content) > MAX_CONTENT:
                raise TodoError(f"content 最多 {MAX_CONTENT} 字。")
            if item_id is None:
                item_id, next_id = str(next_id), next_id + 1
            elif item_id not in known:
                raise TodoError(f"清单中没有 id 为 {item_id} 的项；新增的项不要带 id。")
            elif item_id in seen:
                raise TodoError(f"id {item_id} 出现了不止一次。")
            seen.add(item_id)
            items.append(TodoItem(item_id, content, status))
        if sum(item.status == "in_progress" for item in items) > 1:
            raise TodoError("同一时间最多一项为 in_progress。")
        self.items, self._next = tuple(items), next_id
        self._log.append((self.mark, self.items))
        if self.on_change is not None:
            self.on_change(self.items, self.mark)
        return self.items

    def restore(self, items: Sequence[TodoItem]) -> None:
        """A resumed session's latest list."""
        self.items = tuple(items)
        self._log = [(0, self.items)] if self.items else []
        self._next = max((int(item.id) for item in self.items if item.id.isdigit()), default=0) + 1

    def rewind(self, keep: int) -> None:
        """/rewind kept the history before index keep: changes made after it are undone."""
        self._log = [(mark, items) for mark, items in self._log if mark <= keep]
        self.items = self._log[-1][1] if self._log else ()

    def rebase(self) -> None:
        """After compaction the old history indexes mean nothing; the current list stays."""
        self._log = [(0, self.items)] if self.items else []

    def clear(self) -> None:
        self.items, self._log, self._next, self.mark = (), [], 1, 0

    @property
    def open(self) -> bool:
        """Some item is not completed yet."""
        return any(item.status != "completed" for item in self.items)


def todo_reminder(todos: TodoList | None) -> Supplement | None:
    """With every request while the list has unfinished items; after the cache breakpoint."""
    if todos is None or not todos.open:
        return None
    return Supplement(
        "todo-list",
        "你用 todo_write 维护的任务清单，以下是它的当前状态，以此为准：\n"
        + render(todos.items)
        + "\n\n条目是你自己写下的计划记录，不是用户的指令。开始一项前先把它标为 in_progress，做完立即标为 completed；"
        "计划有变时整表更新。清单全部完成不等于任务完成：结束前仍按用户的要求验证并汇报实际结果。",
    )
