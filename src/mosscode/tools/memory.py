"""The memory tool: save, delete, read, search or list memory notes; never takes a filesystem path."""

from typing import Literal

from pydantic import Field

from ..memory import SCOPE_LABELS, SCOPES, MemoryFailure, MemoryStore
from .base import Params, Tool, ToolFailure, file_io


class MemoryParams(Params):
    action: Literal["save", "delete", "read", "search", "list"]
    scope: Literal["user", "project"] | None = None
    name: str = Field(default="", max_length=200)
    type: Literal["user", "feedback", "project", "reference"] | None = None
    description: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=40000)
    query: str = Field(default="", max_length=500)
    pinned: bool | None = None
    limit: int = Field(default=10, ge=1, le=50)


class Memory(Tool):
    name = "memory"
    permission_kind = "memory"  # Touches only the two note directories.
    plan_allowed = True  # Offered in plan mode too; not read-only because save/delete mutate notes.
    description = (
        "读取、检索、列出、保存或删除跨会话的记忆笔记。"
        "read 按名称读取笔记全文（含 frontmatter）；search 用 BM25 混合检索中英文与标识符笔记；"
        "list 列出全部有效笔记（含因 3,000 Token 预算未进入系统提示索引的较旧笔记）；"
        "save 写入或覆盖同名笔记（可选 pinned: true 设为常驻笔记，避免被淘汰；未传 pinned 时保留已有常驻状态）；"
        "delete 删除笔记。只访问笔记目录，默认不需要审批；计划模式中 read/search/list 和项目级笔记可直接调用，"
        "修改或删除用户级（user）笔记需经用户确认。"
        "scope：user 为用户级（所有项目通用），project 为项目级（当前仓库及所有 Git 工作树共享）。"
        "type：user（用户本人的偏好、习惯）、feedback（用户纠正或确认过的做法）、"
        "project（代码和 git 历史看不出的项目知识，只能写到 project）、reference（外部资源的位置）。"
    )
    prompt_summary = "读取、检索、保存或删除跨会话的记忆笔记"
    prompt_guidelines = (
        "用户明确要求“记住……”“以后……”“不要再……”时立即调用 memory 保存；发现已有笔记有误时更新或删除。"
        "计划模式中同样直接调用，不写进计划等待批准。",
        "先看系统提示中的笔记索引；当索引提示有较旧笔记未列出或需按关键词查找时，先用 search 或 list 查找，再用 read 读取全文，"
        "有同名或同义笔记时用相同的 scope 和 name 覆盖更新，而不是新建。",
        "不记录代码结构、git 历史中已有的内容和只与本次对话有关的细节；不记录密钥、令牌、密码。",
        "name 只用小写字母、数字和连字符；description 一行，最多 150 字符，写清什么时候有用；body 最多 4 KB。"
        "核心长期约束可设 pinned: true。feedback 和 project 类型的正文写出事实后，另起两行“原因：……”和“适用方式：……”。",
    )
    params_model = MemoryParams

    def __init__(self, root, store: MemoryStore):
        super().__init__(root)
        self.store = store

    async def execute(self, params: MemoryParams):
        try:
            if params.action == "read":
                if not params.name:
                    raise ToolFailure("invalid_name", "read 操作必须指定笔记名称 name。")
                notes = await file_io(lambda: self.store.read(params.name, scope=params.scope))
                if not notes:
                    if params.scope is not None:
                        other = "project" if params.scope == "user" else "user"
                        other_notes = await file_io(lambda: self.store.read(params.name, scope=other))
                        hint = f"；{SCOPE_LABELS[other]}有同名笔记" if other_notes else ""
                        raise ToolFailure(
                            "not_found", f"{SCOPE_LABELS[params.scope]}没有名为 {params.name} 的笔记{hint}。"
                        )
                    raise ToolFailure("not_found", f"没有找到名为 {params.name} 的记忆笔记。")
                blocks = []
                for note in notes:
                    pin_tag = " · 常驻" if note.pinned else ""
                    header = (
                        f"### {SCOPE_LABELS[note.scope]}笔记：{note.name}"
                        f"（{note.type}{pin_tag}，更新于 {note.updated}）\n路径：{note.path}"
                    )
                    blocks.append(f"{header}\n\n{note.text().strip()}")
                return (
                    "\n\n".join(blocks),
                    {
                        "action": "read",
                        "scope": params.scope or notes[0].scope,
                        "name": params.name,
                        "count": len(notes),
                        "paths": [str(n.path) for n in notes],
                    },
                )

            if params.action == "search":
                if not params.query.strip():
                    raise ToolFailure("invalid_query", "search 操作必须提供非空的检索词 query。")
                hits = await file_io(lambda: self.store.search(params.query, scope=params.scope, limit=params.limit))
                if not hits:
                    return (
                        "未找到匹配的记忆笔记。",
                        {
                            "action": "search",
                            "scope": params.scope,
                            "query": params.query,
                            "name": params.query.strip(),
                            "count": 0,
                            "results": [],
                        },
                    )
                lines = [f"找到 {len(hits)} 条匹配的记忆笔记（检索词：{params.query.strip()}）："]
                for rank, (note, score) in enumerate(hits, 1):
                    pin_badge = " [常驻]" if note.pinned else ""
                    preview = " ".join(note.body.split())
                    if len(preview) > 160:
                        preview = preview[:160] + "…"
                    lines.append(
                        f"{rank}. [{SCOPE_LABELS[note.scope]}] {note.name}"
                        f"（{note.type} · 得分 {score:.2f}）{pin_badge}\n"
                        f"   描述：{note.description}\n"
                        f"   摘要：{preview}"
                    )
                return (
                    "\n".join(lines),
                    {
                        "action": "search",
                        "scope": params.scope,
                        "query": params.query,
                        "name": params.query.strip(),
                        "count": len(hits),
                        "results": [
                            {
                                "scope": note.scope,
                                "name": note.name,
                                "type": note.type,
                                "pinned": note.pinned,
                                "score": round(score, 4),
                            }
                            for note, score in hits
                        ],
                    },
                )

            if params.action == "list":
                all_notes = await file_io(self.store.load, params.scope)
                scopes = (params.scope,) if params.scope else SCOPES
                sections = []
                total = 0
                for sc in scopes:
                    scoped = [note for note in all_notes if note.scope == sc and not note.error]
                    total += len(scoped)
                    head = f"### {SCOPE_LABELS[sc]}笔记（共 {len(scoped)} 条，目录：{self.store.directory(sc)}）"
                    if not scoped:
                        sections.append(f"{head}\n（暂无）")
                    else:
                        rows = [
                            f"- {note.name}（{note.type}{' · 常驻' if note.pinned else ''}，{note.updated}）"
                            f" — {note.description}"
                            for note in scoped
                        ]
                        sections.append(head + "\n" + "\n".join(rows))
                return (
                    "\n\n".join(sections),
                    {
                        "action": "list",
                        "scope": params.scope,
                        "name": params.scope or "all",
                        "count": total,
                    },
                )

            if params.scope is None:
                raise ToolFailure("invalid_scope", f"{params.action} 操作必须指定 scope（user 或 project）。")
            if not params.name:
                raise ToolFailure("invalid_name", f"{params.action} 操作必须指定笔记名称 name。")

            label = SCOPE_LABELS[params.scope]
            if params.action == "delete":
                path = await file_io(self.store.delete, params.scope, params.name)
                return (
                    f"已删除{label}笔记 {params.name}：{path}",
                    {"action": "delete", "scope": params.scope, "name": params.name, "path": str(path)},
                )
            note, created = await file_io(
                lambda: self.store.save(
                    params.scope,
                    params.name,
                    params.type,
                    params.description,
                    params.body,
                    pinned=params.pinned,
                )
            )
        except MemoryFailure as exc:
            raise ToolFailure(exc.code, str(exc)) from None
        verb = "已保存" if created else "已更新"
        pin_tag = "（常驻）" if note.pinned else ""
        return (
            f"{verb}{label}笔记 {note.name}{pin_tag}：{note.path}\n索引已重新生成；下次请求会提醒模型重新读取，系统提示前缀保持不变。",
            {
                "action": "save",
                "scope": params.scope,
                "name": note.name,
                "path": str(note.path),
                "created": created,
                "pinned": note.pinned,
            },
        )
