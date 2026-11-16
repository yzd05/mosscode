"""Protocol-independent tool contract and a single validation/execution boundary."""

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..credentials import redact, redact_value
from ..messages import ToolCall, ToolDefinition, ToolResult
from ..permissions import Decision, PermissionPolicy
from ..security import SecurityError, project_path, resolve_path
from ..todo import TodoList

# The call a tool is executing, for tools that need its id (the agent tool's row in the UI).
CURRENT_CALL: ContextVar[ToolCall | None] = ContextVar("mosscode_current_call", default=None)
# The registry running it: its session stores images, its images flag says whether the model sees them.
CURRENT_REGISTRY: ContextVar["ToolRegistry | None"] = ContextVar("mosscode_current_registry", default=None)
# A details key for the images a tool returns; the registry moves them to ToolResult.images.
IMAGES = "_images"


def image_context() -> tuple[bool, Any]:
    """(whether the model accepts images, where to store them) for the running tool."""
    registry = CURRENT_REGISTRY.get()
    if registry is None:
        return True, None
    return registry.images, registry


def _split_images(details: dict[str, Any]) -> tuple[dict[str, Any], tuple]:
    if IMAGES not in details:
        return details, ()
    details = dict(details)
    return details, tuple(details.pop(IMAGES))


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
        return (encoded[:65536].decode("utf-8", errors="ignore") + "\n（结果已截断）", {**details, "truncated": True})
    return output, details


HOOK_NOTE_LIMIT = 8000  # Hook text added to one tool result.


def with_hook_notes(result: ToolResult, notes, modified_by: str | None) -> ToolResult:
    """Hook notes and the reformatting notice go after the result, into history and the transcript."""
    if not notes and not modified_by:
        return result
    blocks, room = [], HOOK_NOTE_LIMIT
    for hook_id, note in notes:
        if room <= 0:
            break
        text = note if len(note) <= room else note[:room] + "\n（Hook 内容超出 8000 个字符，已截断）"
        room -= len(note)
        blocks.append(f'<hook-notification hook="{hook_id}">\n{text}\n</hook-notification>')
    details = result.details
    if modified_by:
        blocks.append(f"文件已被 Hook {modified_by} 修改（如格式化）；再次编辑前请重新读取。")
        details = {**details, "hook_modified": modified_by}  # The tool row shows it; the output may be cut there.
    return replace(result, output=result.output + "\n\n" + "\n\n".join(blocks), details=details)


def _stamp(path: Path | None):
    try:
        status = path.stat() if path is not None else None
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size) if status is not None else None


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
        return ToolDefinition(
            self.name, description, self.params_model.model_json_schema(), self.prompt_summary, guidelines
        )

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
        self.hooks = None  # The HookEngine, set by the application.
        self.agent = "main"  # "fork" or "subagent" in a sub-agent's view.
        self.agent_type = ""  # The definition name, "fork" or "skill:<name>".
        self.forbidden: frozenset[str] = frozenset()  # Offered (fork cache) but refused when called.
        self.nested = False  # Started by a hook's agent action; its tool events run no agent action.
        self.worktree = ""  # The worktree a sub-agent works in, relative to the repository.
        self.images = True  # Whether the current model accepts images; the agent loop keeps it current.
        # A sub-agent or member keeps its images beside its own transcript, where reloading it looks for them.
        self.image_dir: Path | None = None
        # withheld(tool) -> True keeps a tool for sub-agents and members but neither offers nor runs it here.
        self.withheld = None
        self.commands = None  # The session's BackgroundCommands; None runs every command in the foreground.
        # Who its background commands end with: "" for the main conversation, else a sub-agent task id or member:<name>.
        self.task_id = ""
        self.todos = TodoList()  # This agent's own checklist (todo_write); a sub-agent's view starts empty.
        if any(tool.root != self.permissions.root for tool in self._tools.values()):
            raise ValueError("工具与权限策略必须使用同一个项目根目录。")

    def use_session(self, session) -> None:
        """Store oversized results in this run's directory and let read_file open them without approval."""
        self.session = session
        self.permissions.session_dir = session.directory if session is not None else None

    def store_image(self, image, alt: str):
        from ..images import image_ref, store_image

        if self.image_dir is not None:
            return store_image(self.image_dir, image, alt)
        return image_ref(image, alt, self.session)

    def forget_reads(self, paths: Iterable[str] | None = None) -> None:
        """The model no longer holds these files' contents (every file after compaction): edits need a fresh read."""
        paths = None if paths is None else tuple(paths)
        for tool in self._tools.values():
            state = getattr(tool, "state", None)
            if state is None or not hasattr(state, "clear"):
                continue
            if paths is None:
                state.clear()
            else:
                for path in paths:
                    state.forget(path)

    def register(self, tool: Tool) -> None:
        if hasattr(self, "permissions") and tool.root != self.permissions.root:
            raise ValueError("工具与权限策略必须使用同一个项目根目录。")
        if tool.name in self._tools:
            raise ValueError(f"工具名称重复：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, tool in self._tools.items() if not self._withheld(tool))

    def _withheld(self, tool: Tool) -> bool:
        return self.withheld is not None and self.withheld(tool)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def for_agent(
        self,
        *,
        agent: str,
        agent_type: str,
        permissions: PermissionPolicy,
        state=None,
        keep=None,
        extra=(),
        forbidden=(),
        checkpoints: bool = True,
    ) -> "ToolRegistry":
        """A sub-agent's registry.

        Tools holding a FileState are copied and share `state` (a new, empty one when None);
        keep(name) filters the rest; extra tools are added; forbidden names stay offered but are refused.
        checkpoints=False stops the copies recording file snapshots for /rewind.
        """
        from copy import copy

        from .files import FileState

        state = state if state is not None else FileState()
        view = ToolRegistry(permissions=permissions)
        view.session, view.hooks, view.agent, view.agent_type = self.session, self.hooks, agent, agent_type
        view.images = self.images
        view.commands, view.task_id = self.commands, self.task_id
        view.forbidden = frozenset(forbidden)
        for name, tool in self._tools.items():
            if keep is not None and not keep(name):
                continue
            if getattr(tool, "state", None) is not None:
                tool = copy(tool)
                tool.state = state
                if not checkpoints and hasattr(tool, "checkpoints"):
                    tool.checkpoints = None
            view._tools[name] = tool
        for tool in extra:  # Replace same-named ones.
            if tool.root != permissions.root:
                raise ValueError("工具与权限策略必须使用同一个项目根目录。")
            view._tools[tool.name] = tool
        return view

    def file_state(self):
        """The FileState the file tools share, if any."""
        return next((tool.state for tool in self._tools.values() if getattr(tool, "state", None) is not None), None)

    def definitions(self, *, read_only: bool = False, plan: bool = False) -> tuple[ToolDefinition, ...]:
        # Plan mode offers read-only tools, read-only commands and the tool that submits the plan.
        def offered(tool):
            if plan:
                return tool.read_only or tool.plan_only or tool.plan_commands or tool.plan_allowed
            return not tool.plan_only and (not read_only or tool.read_only)

        return tuple(tool.definition() for tool in self._tools.values() if offered(tool) and not self._withheld(tool))

    def is_read_only(self, name: str) -> bool:
        tool = self.get(name)
        return tool is not None and tool.read_only

    def _bound(self, call: ToolCall, output: str, details: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from ..tool_budget import EXEMPT, bound_result

        if call.name in EXEMPT:
            return bounded_output(output, details)
        return bound_result(call.name, call.id, output, details, self.session)

    async def _invoke(self, call: ToolCall, tool: Tool, params: Params) -> ToolResult:
        CURRENT_CALL.set(call)  # Runs in its own task: the values never leak to the caller.
        CURRENT_REGISTRY.set(self)
        try:
            output, details = await tool.execute(params)
            details, images = _split_images(details)
            # Hidden before _bound, which writes long output to the session directory.
            output, details = self._bound(call, redact(output), redact_value(details))
            return ToolResult(call.id, call.name, output, details=details, images=images)
        except ToolFailure as exc:
            details, images = _split_images(exc.details)
            output, details = self._bound(call, redact(str(exc)), redact_value(details))
            return ToolResult(call.id, call.name, output, True, exc.code, details, images)
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
        if self._withheld(tool):
            from ..coordinator import WITHHELD

            return ToolResult(
                call.id, call.name, WITHHELD.format(name=call.name), True, "coordinator_withheld", {"executed": False}
            )
        if call.name in self.forbidden:
            return ToolResult(
                call.id,
                call.name,
                f"你是子 Agent，不能使用 {call.name}；请在自己的范围内完成任务并在报告中说明。",
                True,
                "subagent_forbidden",
                {"executed": False},
            )
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
            issues = [
                {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                for e in exc.errors(include_input=False, include_context=False, include_url=False)
            ]
            listed = "\n".join(f"- {issue['field'] or '(参数)'}：{issue['message']}" for issue in issues[:20])
            return ToolResult(
                call.id, call.name, "工具参数校验失败：\n" + listed, True, "invalid_arguments", {"issues": issues}
            )
        hooks, state = self.hooks, {}
        gate = notify = None
        if hooks is not None:

            async def gate(operation):
                state["operation"] = operation
                if not hooks.wants("pre_tool_use"):
                    return None
                outcome = await hooks.fire(self._event("pre_tool_use", call.name, raw, operation, tool.root))
                state["notes"] = list(outcome.notes)
                if outcome.rejection is None:
                    return None
                state["hook"] = outcome.rejection[0]
                return Decision("deny", "hook_rejected", outcome.rejection[1])

            async def notify(request):
                if hooks.wants("permission_request"):
                    operation = request.operation if request.operation.tool == call.name else None
                    await hooks.fire(self._event("permission_request", call.name, raw, operation, tool.root))

        decision = await self.permissions.authorize(call.id, tool, params, gate=gate, notify=notify)
        if decision.action != "allow":
            details = {"executed": False, **({"hook": state["hook"]} if decision.code == "hook_rejected" else {})}
            return ToolResult(call.id, call.name, decision.reason, True, decision.code, details)
        result = await self._run(call, tool, params)
        if hooks is None:
            return result
        return await self._after(call, tool, raw, state, result)

    def refusal(self, tool: Tool, params: Params) -> Decision | None:
        """The hard checks and deny rules alone: for what the user asked for, which needs no approval."""
        try:
            operation = self.permissions.operation(tool, params, typed=True)
        except SecurityError as exc:
            return Decision("deny", exc.code, str(exc))
        decision = self.permissions.decide(operation, read_only=tool.read_only)
        return decision if decision.action == "deny" else None

    async def read_for_user(self, path: str, offset: int = 1, limit: int | None = None) -> ToolResult:
        """read_file for a path the user typed (an @ mention): no approval or hooks, the same result otherwise."""
        arguments: dict[str, Any] = {"path": path, "offset": offset} | ({"limit": limit} if limit else {})
        call = ToolCall("mention", "read_file", json.dumps(arguments, ensure_ascii=False))
        tool = self.get("read_file")
        if tool is None or self._withheld(tool):
            return ToolResult(call.id, call.name, "当前没有启用 read_file。", True, "unknown_tool")
        try:
            params = tool.validate_arguments(arguments)
        except (ToolFailure, ValidationError):
            return ToolResult(call.id, call.name, "路径或行号不合法。", True, "invalid_arguments")
        refused = self.refusal(tool, params)
        if refused is not None:
            return ToolResult(call.id, call.name, refused.reason, True, refused.code, {"executed": False})
        return await self._run(call, tool, params)

    async def _after(self, call: ToolCall, tool: Tool, raw, state, result: ToolResult) -> ToolResult:
        """post_tool_use; a cancelled hook keeps the tool result."""
        hooks, notes, modified = self.hooks, list(state.get("notes", ())), None
        if hooks.wants("post_tool_use"):
            event = self._event("post_tool_use", call.name, raw, state.get("operation"), tool.root, result)
            watched = event.file_path if call.name in {"write_file", "edit_file"} and not result.is_error else None
            before = _stamp(watched)
            try:
                outcome = await hooks.fire(event)
            except asyncio.CancelledError:
                raise ExecutionCancelled(with_hook_notes(result, notes, None)) from None
            notes.extend(outcome.notes)
            if before is not None and outcome.ran and _stamp(watched) != before:
                modified = outcome.ran[-1]  # FileState keeps the tool's own stamp: the next edit must reread.
        return with_hook_notes(result, notes, modified)

    def _event(self, name, tool_name, raw, operation, root, result=None):
        from ..hooks import tool_event

        event = tool_event(name, self.agent, tool_name, raw, operation, root, result, agent_type=self.agent_type)
        if self.worktree:
            event = replace(
                event,
                fields={**event.fields, "worktree": self.worktree},
                payload={**event.payload, "worktree": self.worktree},
            )
        return replace(event, nested=True) if self.nested else event

    async def _run(self, call: ToolCall, tool: Tool, params: Params) -> ToolResult:
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
                outcome = ToolResult(
                    call.id,
                    call.name,
                    "工具执行超时。" if code == "timeout" else "工具已取消，可能已有部分外部效果。",
                    True,
                    code,
                )
            if isinstance(exc, asyncio.CancelledError):
                raise ExecutionCancelled(outcome) from None
            # Local file operations may finish during cancellation cleanup.
            return replace(outcome, details={**outcome.details, "deadline_exceeded": True})
