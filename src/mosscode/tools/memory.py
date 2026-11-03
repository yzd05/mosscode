"""The memory tool: save or delete one note; never takes a path."""

from typing import Literal

from pydantic import Field

from ..memory import SCOPE_LABELS, MemoryFailure, MemoryStore
from .base import Params, Tool, ToolFailure, file_io


class MemoryParams(Params):
    action: Literal["save", "delete"]
    scope: Literal["user", "project"]
    name: str = Field(min_length=1, max_length=200)
    type: Literal["user", "feedback", "project", "reference"] | None = None
    description: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=40000)


class Memory(Tool):
    name = "memory"
    permission_kind = "memory"  # No approval: it writes only the two note directories.
    plan_allowed = True  # Offered in plan mode too; not read-only, so it never runs concurrently.
    description = ("保存或删除跨会话的记忆笔记。save 写入或覆盖同名笔记（更新日期），delete 删除笔记；"
                   "成功后返回文件路径，索引由程序重新生成，下次会话开始时出现在系统提示中。"
                   "只写笔记目录，不需要审批，计划模式中也可以直接调用。"
                   "scope：user 为用户级（所有项目通用），project 为项目级（只用于当前项目）。"
                   "type：user（用户本人的偏好、习惯）、feedback（用户纠正或确认过的做法）、"
                   "project（代码和 git 历史看不出的项目知识，只能写到 project）、reference（外部资源的位置）。")
    prompt_summary = "保存或删除跨会话的记忆笔记"
    prompt_guidelines = (
        "用户明确要求“记住……”“以后……”“不要再……”时立即调用 memory 保存；发现已有笔记有误时更新或删除。"
        "计划模式中同样直接调用，不写进计划等待批准。",
        "先看系统提示中的笔记索引，有同名或同义的笔记就用相同的 scope 和 name 覆盖更新，而不是新建。",
        "不记录代码结构、git 历史中已有的内容和只与本次对话有关的细节；不记录密钥、令牌、密码。",
        "name 只用小写字母、数字和连字符；description 一行，最多 150 字符，写清什么时候有用；body 最多 4 KB。"
        "feedback 和 project 类型的正文写出事实后，另起两行“原因：……”和“适用方式：……”。",
    )
    params_model = MemoryParams

    def __init__(self, root, store: MemoryStore):
        super().__init__(root)
        self.store = store

    async def execute(self, params: MemoryParams):
        label = SCOPE_LABELS[params.scope]
        try:
            if params.action == "delete":
                path = await file_io(self.store.delete, params.scope, params.name)
                return (f"已删除{label}笔记 {params.name}：{path}",
                        {"action": "delete", "scope": params.scope, "name": params.name, "path": str(path)})
            note, created = await file_io(self.store.save, params.scope, params.name, params.type,
                                          params.description, params.body)
        except MemoryFailure as exc:
            raise ToolFailure(exc.code, str(exc)) from None
        verb = "已保存" if created else "已更新"
        return (f"{verb}{label}笔记 {note.name}：{note.path}\n索引已重新生成；本会话的系统提示不会变化，下次会话开始时生效。",
                {"action": "save", "scope": params.scope, "name": note.name, "path": str(note.path), "created": created})
