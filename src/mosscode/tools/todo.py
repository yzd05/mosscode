"""todo_write: the model's own task checklist, replaced whole on every call."""

from typing import Literal

from pydantic import Field

from ..todo import MAX_CONTENT, MAX_ITEMS, TodoError, completed, render
from .base import CURRENT_REGISTRY, Params, Tool, ToolFailure


class TodoEntry(Params):
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    status: Literal["pending", "in_progress", "completed"]
    id: str | int | None = None


class TodoParams(Params):
    todos: list[TodoEntry] = Field(max_length=MAX_ITEMS)


class TodoWrite(Tool):
    name = "todo_write"
    # It changes only this agent's in-memory checklist: no approval, plan mode included.
    read_only = True
    description = (
        "更新本次任务的清单（整表替换）：传入完整的新清单，省略的项即被删除，传入空列表清空清单。"
        "已有的项带上它的 id，新增的项不带 id，由程序分配。status 为 pending、in_progress 或 completed，"
        "同一时间最多一项 in_progress。返回更新后的清单。清单只属于当前对话，不写入任何文件。"
    )
    prompt_summary = "维护本次任务的清单"
    prompt_guidelines = (
        "任务需要三步或更多步骤，或用户一次给出多项要求时，开始动手前用 todo_write 列出清单；"
        "简单问答和一两步就能完成的修改不建清单。",
        "开始一项前把它标为 in_progress，做完立即标为 completed，不要攒到最后一起更新；"
        "发现新的必要步骤时加入清单，不再需要的项删除。",
        "只有真正完成的项才标为 completed：测试失败、实现不完整或遇到阻塞时保持 in_progress，并加一项说明要解决的问题。",
    )
    params_model = TodoParams

    async def execute(self, params: TodoParams):
        registry = CURRENT_REGISTRY.get()
        todos = getattr(registry, "todos", None)
        if todos is None:
            raise ToolFailure("todo_unavailable", "当前对话没有任务清单。")
        entries = [(None if entry.id is None else str(entry.id), entry.content, entry.status) for entry in params.todos]
        try:
            items = todos.replace(entries)
        except TodoError as exc:
            raise ToolFailure("todo_invalid", f"{exc}清单未改变。") from None
        return (
            "清单已更新：\n" + render(items),
            {"todos": [item.to_json() for item in items], "completed": completed(items), "total": len(items)},
        )
