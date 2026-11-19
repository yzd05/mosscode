"""Read-only `repo_map` tool: on-demand AST & Personalized PageRank structural map."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ..permissions import Operation, PermissionPolicy
from ..repomap import build_repo_map
from ..security import inside
from .base import CURRENT_REGISTRY, Params, Tool, ToolFailure, file_io


class RepoMapParams(Params):
    path: str = "."
    focus_files: list[str] = Field(default_factory=list, max_length=30)
    query: str = Field(default="", max_length=400)
    max_tokens: int = Field(default=2000, ge=256, le=8000)


class RepoMap(Tool):
    read_only = True
    plan_allowed = True
    name = "repo_map"
    description = (
        "基于 Tree-sitter 提取多语言 AST 符号定义与跨文件引用，通过 Personalized PageRank 生成按相关性排序的代码结构地图"
        "（文件路径、[L起-L止] 行号范围与类/函数签名）。可指定子目录 path、关注文件 focus_files、任务关键词 query 与 max_tokens 预算（256–8000）。"
        "遵守 .gitignore 与读取拒绝规则。项目目录之外的路径需要用户确认。"
    )
    prompt_summary = "按相关性生成多语言 AST 代码结构与依赖地图"
    prompt_guidelines = (
        "探索不熟悉的模块或跨文件调用关系时优先调用 repo_map，用 query 或 focus_files 聚焦相关符号，避免盲目读取大量无关全文。",
        "repo_map 只提供符号位置与签名；修改文件前仍须用 read_file（可配合 symbol 或 offset/limit）读取目标原文。",
    )
    params_model = RepoMapParams

    def __init__(self, root: Path, permissions: PermissionPolicy | None = None):
        super().__init__(root)
        self.permissions = permissions

    def _active_policy(self) -> PermissionPolicy | None:
        registry = CURRENT_REGISTRY.get()
        if registry is not None and registry.permissions is not None:
            return registry.permissions
        return self.permissions

    async def execute(self, params: RepoMapParams):
        target = self.read_path(params.path)
        if not target.exists():
            raise FileNotFoundError()
        policy = self._active_policy()
        if policy is not None:
            t_out = not inside(policy.root, target)
            t_rel = target.as_posix() if t_out else target.relative_to(policy.root).as_posix()
            for tool_name in ("read_file", "repo_map"):
                decision = policy.decide(Operation(tool_name, t_rel, outside=t_out), read_only=True)
                if decision.action == "deny":
                    raise ToolFailure(decision.code or "permission_denied", decision.reason, executed=False)

        def allow_file(candidate: Path) -> bool:
            if policy is None:
                return True
            outside = not inside(policy.root, candidate)
            rel_or_abs = candidate.as_posix() if outside else candidate.relative_to(policy.root).as_posix()
            for tool_name in ("read_file", "repo_map"):
                if policy.decide(Operation(tool_name, rel_or_abs, outside=outside), read_only=True).action == "deny":
                    return False
            return True

        def run():
            result = build_repo_map(
                self.root,
                target,
                focus_files=params.focus_files,
                query=params.query,
                max_tokens=params.max_tokens,
                allow_file=allow_file,
            )
            details = {
                "path": str(target),
                "files_indexed": result.files_indexed,
                "files_shown": result.files_shown,
                "symbols_total": result.symbols_total,
                "symbols_shown": result.symbols_shown,
                "tokens": result.tokens,
                "truncated": result.truncated,
            }
            return result.text, details

        return await file_io(run)
