"""Protocol-independent tool contract and a single validation/execution boundary."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..messages import ToolCall, ToolDefinition, ToolResult
from ..permissions import PermissionPolicy
from ..security import SecurityError, project_path, resolve_path


class Params(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


def bounded_output(output, details):
    encoded = output.encode("utf-8")
    if len(encoded) > 65536:
        return (encoded[:65536].decode("utf-8", errors="ignore") + "\n（结果已截断）",
                {**details, "truncated": True})
    return output, details


def reject_json_constant(value):
    raise ValueError("非有限数字不是 JSON 参数。")


class ExecutionCancelled(asyncio.CancelledError):
    def __init__(self, result: ToolResult):
        super().__init__()
        self.result = result


class Tool(ABC):
    name: str
    description: str
    params_model: type[Params]
    prompt_summary = ""
    prompt_guidelines: tuple[str, ...] = ()
    timeout = 30.0
    read_only = False
    plan_only = False  # Offered only in plan mode (exit_plan_mode).
    plan_commands = False  # Offered in plan mode; the policy admits only read-only commands.
    plan_allowed = False  # Offered and allowed in plan mode although not read-only (memory).

    def validate_arguments(self, raw):
        return self.params_model.model_validate(raw)

    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, value: str) -> Path:
        try:
            return project_path(self.root, value)
        except SecurityError as exc:
            raise ToolFailure(exc.code, str(exc)) from None

    def read_path(self, value: str) -> Path:
        """Read tools may leave the project; the permission policy asks first."""
        try:
            return resolve_path(self.root, value)
        except SecurityError as exc:
            raise ToolFailure(exc.code, str(exc)) from None

    def display(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)

    def definition(self) -> ToolDefinition:
        guidelines = tuple(dict.fromkeys(rule.strip() for rule in self.prompt_guidelines if rule.strip()))
        description = "\n".join((self.description, *(f"- {rule}" for rule in guidelines)))
        return ToolDefinition(self.name, description, self.params_model.model_json_schema(),
                              self.prompt_summary, guidelines)

    @abstractmethod
    async def execute(self, params: Params) -> tuple[str, dict[str, Any]]: ...


async def file_io(function, *args):
    # A cancelled Python thread cannot roll back filesystem changes. Settle it
    # before returning so the runtime can retain the actual outcome in history.
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        return await asyncio.shield(worker)


class ToolRegistry:
    def __init__(self, tools=(), *, permissions: PermissionPolicy | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)
        first = next(iter(self._tools.values()), None)
        self.permissions = permissions or PermissionPolicy(first.root if first else Path.cwd())
        # Set by the application; without it oversized results are cut to a preview, not stored.
        self.session = None
        if any(tool.root != self.permissions.root for tool in self._tools.values()):
            raise ValueError("工具与权限策略必须使用同一个项目根目录。")

    def use_session(self, session) -> None:
        """Store oversized results in this run's directory and let read_file open them without approval."""
        self.session = session
        self.permissions.session_dir = session.directory if session is not None else None

    def forget_reads(self) -> None:
        """After compaction the model no longer holds file contents: edits need a fresh read."""
        for tool in self._tools.values():
            state = getattr(tool, "state", None)
            if state is not None and hasattr(state, "clear"):
                state.clear()

    def register(self, tool: Tool) -> None:
        if hasattr(self, "permissions") and tool.root != self.permissions.root:
            raise ValueError("工具与权限策略必须使用同一个项目根目录。")
        if tool.name in self._tools:
            raise ValueError(f"工具名称重复：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self, *, read_only: bool = False, plan: bool = False) -> tuple[ToolDefinition, ...]:
        # Plan mode offers read-only tools, read-only commands and the tool that submits the plan.
        def offered(tool):
            if plan:
                return tool.read_only or tool.plan_only or tool.plan_commands or tool.plan_allowed
            return not tool.plan_only and (not read_only or tool.read_only)
        return tuple(tool.definition() for tool in self._tools.values() if offered(tool))

    def is_read_only(self, name: str) -> bool:
        tool = self.get(name)
        return tool is not None and tool.read_only

    def _bound(self, call: ToolCall, output: str, details: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from ..tool_budget import EXEMPT, bound_result
        if call.name in EXEMPT:
            return bounded_output(output, details)
        return bound_result(call.name, call.id, output, details, self.session)

    async def _invoke(self, call: ToolCall, tool: Tool, params: Params) -> ToolResult:
        try:
            output, details = await tool.execute(params)
            output, details = self._bound(call, output, details)
            return ToolResult(call.id, call.name, output, details=details)
        except ToolFailure as exc:
            output, details = self._bound(call, str(exc), exc.details)
            return ToolResult(call.id, call.name, output, True, exc.code, details)
        except FileNotFoundError:
            return ToolResult(call.id, call.name, "文件或目录不存在。", True, "not_found")
        except IsADirectoryError:
            return ToolResult(call.id, call.name, "目标是目录，不能作为文本文件操作。", True, "is_directory")
        except UnicodeError:
            return ToolResult(call.id, call.name, "文件不是可读取的 UTF-8 文本。", True, "encoding_error")
        except PermissionError:
            return ToolResult(call.id, call.name, "系统拒绝访问目标。", True, "permission_denied")
        except OSError:
            return ToolResult(call.id, call.name, "文件系统或进程操作失败。", True, "os_error")
        except Exception:
            return ToolResult(call.id, call.name, "工具内部错误。", True, "internal_error")

    async def execute(self, call: ToolCall, *, read_only: bool = False) -> ToolResult:
        tool = self.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, "未知工具。", True, "unknown_tool")
        if read_only and not tool.read_only:
            # Explicit caller restriction, separate from the approval-based plan mode.
            return ToolResult(call.id, call.name, "本次调用限定只读工具；此调用未执行。", True, "plan_blocked")
        try:
            raw = json.loads(call.arguments, parse_constant=reject_json_constant)
        except (ValueError, TypeError):
            return ToolResult(call.id, call.name, "工具参数不是完整、合法的 JSON。", True, "invalid_json")
        try:
            params = tool.validate_arguments(raw)
        except ToolFailure as exc:
            return ToolResult(call.id, call.name, str(exc), True, exc.code, exc.details)
        except ValidationError as exc:
            issues = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                      for e in exc.errors(include_input=False, include_context=False, include_url=False)]
            listed = "\n".join(f"- {issue['field'] or '(参数)'}：{issue['message']}" for issue in issues[:20])
            return ToolResult(call.id, call.name, "工具参数校验失败：\n" + listed, True, "invalid_arguments", {"issues": issues})
        decision = await self.permissions.authorize(call.id, tool, params)
        if decision.action != "allow":
            return ToolResult(call.id, call.name, decision.reason, True, decision.code,
                              {"executed": False})
        task = asyncio.create_task(self._invoke(call, tool, params))
        limit = getattr(params, "timeout", tool.timeout)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=limit)
        except (TimeoutError, asyncio.CancelledError) as exc:
            task.cancel()
            try:
                outcome = await task
            except asyncio.CancelledError:
                code = "timeout" if isinstance(exc, TimeoutError) else "cancelled"
                outcome = ToolResult(call.id, call.name, "工具执行超时。" if code == "timeout" else "工具已取消，可能已有部分外部效果。", True, code)
            if isinstance(exc, asyncio.CancelledError):
                raise ExecutionCancelled(outcome) from None
            # Local file operations may finish during cancellation cleanup.
            return replace(outcome, details={**outcome.details, "deadline_exceeded": True})
