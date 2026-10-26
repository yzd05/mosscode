"""Layered policy and queued approval; no UI or provider dependencies."""

import asyncio
from dataclasses import dataclass, replace
import fnmatch
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal
from uuid import uuid4

import yaml

from .security import (SecurityError, check_command, command_prefix, command_risk, command_segments, inside,
                       project_path, read_only_command, resolve_path, simple_command, starts_with)


MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
REMOVED_MODES = {"strict": "default", "permissive": "bypassPermissions"}
MODE_LABELS = {"default": "默认", "acceptEdits": "接受编辑", "plan": "计划", "bypassPermissions": "跳过审批"}
ALIASES = {"Read": "read_file", "Write": "write_file", "Edit": "edit_file",
           "Bash": "execute_command", "Shell": "execute_command", "Glob": "glob", "Grep": "grep"}
TOOL_NAMES = set(ALIASES.values())
READ_TOOLS = {"read_file", "glob", "grep"}  # May read outside the project after approval.
EDIT_TOOLS = {"write_file", "edit_file"}


def is_mcp_tool(name: str) -> bool:
    return bool(re.fullmatch(r"mcp__[A-Za-z0-9_]+__[A-Za-z0-9_]+", name)) and len(name) <= 64
CHOICES = ("once", "session", "permanent", "deny")
COMMAND_CHOICES = ("once", "session", "prefix", "permanent", "deny")  # prefix: saved as Bash(<prefix>:*).
EDIT_CHOICES = ("once", "session", "accept_edits", "permanent", "deny")  # accept_edits: switch mode.
RISKY_CHOICES = ("once", "deny")  # High-risk commands: never remembered.
PLAN_CHOICES = ("execute", "accept_edits", "deny")  # deny = keep planning.
ALL_CHOICES = frozenset((*COMMAND_CHOICES, *EDIT_CHOICES, *PLAN_CHOICES))
EXIT_PLAN = "exit_plan_mode"
PREFIX_SUFFIX = ":*"


class PermissionConfigError(ValueError):
    pass


def normalize_mode(mode: str) -> str:
    if mode in REMOVED_MODES:
        # Fail loudly rather than silently change how much an old config trusts the model.
        raise PermissionConfigError(f"权限模式 {mode} 已移除，请改用 {REMOVED_MODES[mode]}。")
    if mode not in MODES:
        raise PermissionConfigError("权限模式需为 " + "/".join(MODES) + "。")
    return mode


@dataclass(frozen=True)
class Operation:
    tool: str
    target: str
    cwd: str | None = None
    preview: str = ""
    outside: bool = False  # Read target outside the project; absolute posix path.
    risk: str = ""  # Why a command must be confirmed every time.
    read_only: bool = False  # Whitelisted inspection command; runs without approval.
    prefix: str = ""  # Command prefix offered for a permanent Bash(<prefix>:*) rule.
    server: str = ""  # MCP server name from the configuration (tool names may be hashed).
    notes: bool = False  # The memory tool: writes only the note directories, no approval.


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    call_id: str
    operation: Operation
    reason: str
    choices: tuple[str, ...] = CHOICES


@dataclass(frozen=True)
class Decision:
    action: Literal["allow", "deny", "ask"]
    code: str = ""
    reason: str = ""


def path_matches(target: str, pattern: str) -> bool:
    # '*' is one segment; '**' spans zero or more. Windows paths are case-insensitive.
    if os.name == "nt":
        target, pattern = target.casefold(), pattern.casefold()
    parts, patterns = target.split("/"), pattern.split("/")
    memo = {}
    def match(i, j):
        if (i, j) not in memo:
            if j == len(patterns):
                answer = i == len(parts)
            elif patterns[j] == "**":
                answer = match(i, j + 1) or (i < len(parts) and match(i + 1, j))
            else:
                answer = i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)
            memo[i, j] = answer
        return memo[i, j]
    return match(0, 0)


def normalized(value: str) -> str:
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value or "."


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str
    action: Literal["allow", "deny"]
    exact: bool = False
    cwd: str | None = None
    server: str | None = None  # mcp__server / mcp__server__*: every tool of that server.

    def matches(self, operation: Operation) -> bool:
        if self.server is not None:
            return bool(operation.server) and operation.server == self.server
        if self.tool != operation.tool or (self.cwd is not None and self.cwd != operation.cwd):
            return False
        if self.exact:
            if operation.tool != "execute_command" and not is_mcp_tool(operation.tool) and os.name == "nt":
                return self.pattern.casefold() == operation.target.casefold()
            return self.pattern == operation.target
        if is_mcp_tool(operation.tool):
            return fnmatch.fnmatchcase(operation.target, self.pattern)
        if operation.tool == "execute_command":
            if self.pattern.endswith(PREFIX_SUFFIX):
                prefix = self.pattern[:-len(PREFIX_SUFFIX)]
                if self.action == "deny":
                    # Any segment of a compound command may be the denied one.
                    return any(starts_with(segment, prefix) for segment in command_segments(operation.target))
                return simple_command(operation.target) and starts_with(operation.target, prefix)
            if self.action == "allow" and any(c in self.pattern for c in "*?[") and not simple_command(operation.target):
                return False
            return fnmatch.fnmatchcase(operation.target, self.pattern)
        # Relative patterns cover the project only; absolute ones cover outside reads.
        if Path(self.pattern).is_absolute() != operation.outside:
            return False
        return path_matches(operation.target, self.pattern)


def parse_rule(value, action: str) -> Rule:
    if isinstance(value, str):
        text, exact, cwd = value, False, None
    elif isinstance(value, dict) and not set(value) - {"rule", "exact", "cwd"}:
        text, exact, cwd = value.get("rule"), value.get("exact", False), value.get("cwd")
        if type(exact) is not bool or (cwd is not None and not isinstance(cwd, str)):
            raise PermissionConfigError("exact 需为布尔值，cwd 需为项目相对路径。")
    else:
        raise PermissionConfigError("规则需为 Tool(pattern) 字符串或带 rule/exact/cwd 的对象。")
    if isinstance(text, str) and text.startswith("mcp__") and "(" not in text and not exact and cwd is None:
        return mcp_rule(text, action)
    match = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9]*)\((.*)\)", text, re.DOTALL) if isinstance(text, str) else None
    if not match or not match[2] or "\0" in match[2]:
        raise PermissionConfigError("规则格式需为 Tool(pattern)，模式不能为空。")
    tool = ALIASES.get(match[1], match[1])
    if tool not in TOOL_NAMES and not is_mcp_tool(tool):
        raise PermissionConfigError(f"未知权限工具名：{match[1]}。")
    pattern = match[2] if tool == "execute_command" or is_mcp_tool(tool) else normalized(match[2])
    if tool != "execute_command" and not is_mcp_tool(tool) and ".." in pattern.split("/"):
        raise PermissionConfigError("文件规则不能包含 ..。")
    if tool not in READ_TOOLS and tool != "execute_command" and not is_mcp_tool(tool) and Path(pattern).is_absolute():
        raise PermissionConfigError("写入和编辑规则需使用项目相对路径；只有读取类规则可以使用绝对路径。")
    if cwd is not None:
        cwd = normalized(cwd)
        if tool != "execute_command" or Path(cwd).is_absolute() or ".." in cwd.split("/"):
            raise PermissionConfigError("cwd 仅用于命令规则，且必须为项目相对路径。")
    return Rule(tool, pattern, action, exact, cwd)


def mcp_rule(text: str, action: str) -> Rule:
    """Claude Code style: mcp__server, mcp__server__* (whole server) or mcp__server__tool (any arguments)."""
    server, separator, tool = text[len("mcp__"):].partition("__")
    if not separator or tool == "*":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server) or server.endswith("_"):
            raise PermissionConfigError(f"MCP Server 规则格式需为 mcp__Server名称 或 mcp__Server名称__*：{text}")
        return Rule("mcp__" + server, "*", action, server=server)
    if not is_mcp_tool(text):
        raise PermissionConfigError(f"MCP 工具规则需为完整工具名 mcp__server__tool：{text}")
    return Rule(text, "*", action)


def read_policy(path: Path):
    try:
        if not path.exists():
            if path.is_symlink():
                raise PermissionConfigError("权限文件是失效的符号链接。")
            return {}, ()
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict) or set(raw) - {"mode", "allow", "deny"}:
            raise PermissionConfigError("仅支持 mode、allow、deny 字段。")
        if "mode" in raw:
            raw["mode"] = normalize_mode(raw["mode"])
        rules = []
        for action in ("allow", "deny"):
            entries = raw.get(action, [])
            if not isinstance(entries, list):
                raise PermissionConfigError(f"{action} 需为规则列表。")
            rules.extend(parse_rule(entry, action) for entry in entries)
        return raw, tuple(rules)
    except (OSError, UnicodeError, yaml.YAMLError, PermissionConfigError, TypeError) as exc:
        # Do not include YAML source snippets, which may contain sensitive data.
        detail = str(exc) if isinstance(exc, PermissionConfigError) else "无法读取有效 UTF-8 YAML。"
        raise PermissionConfigError(f"权限配置 {path}：{detail}") from None


def safe_display(value: str, limit: int = 1000) -> str:
    return "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))[:limit]


class PermissionPolicy:
    def __init__(self, root: Path, *, mode: str | None = None, load: bool = True, user_path: Path | None = None):
        self.root = root.resolve()
        self.user_path = user_path or Path.home() / ".mosscode" / "permissions.yaml"
        self.project_file = self.root / ".mosscode" / "permissions.yaml"
        self.local_file = self.root / ".mosscode" / "permissions.local.yaml"
        self.layers: list[tuple[Rule, ...]] = []  # User, project, local; evaluated in reverse.
        configured = "default"
        for path in (self.user_path, self.project_file, self.local_file):
            raw, rules = read_policy(path) if load else ({}, ())
            self.layers.append(rules)
            configured = raw.get("mode", configured)
        self.mode = configured
        self.resume_mode = "default" if configured == "plan" else configured
        if mode is not None:
            self.set_mode(mode)
        self.session: list[Rule] = []
        self.interactive = False
        self.session_dir: Path | None = None
        self.memory_dirs: tuple[Path, ...] = ()  # Note directories: read_file and glob need no approval.
        self.requests: asyncio.Queue[ApprovalRequest] = asyncio.Queue()
        self.pending: dict[str, asyncio.Future] = {}
        self._choices: dict[str, tuple[str, ...]] = {}
        self._approval_lock = asyncio.Lock()

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = normalize_mode(value)

    def set_mode(self, mode: str) -> None:
        mode = normalize_mode(mode)
        if mode == "plan" and self.mode != "plan":
            self.resume_mode = self.mode
        elif mode != "plan":
            self.resume_mode = mode
        self.mode = mode

    def operation(self, tool, params) -> Operation:
        args = params.model_dump()
        if self.mode == "plan" and not tool.read_only and tool.name != EXIT_PLAN and not getattr(tool, "plan_allowed", False):
            if not (tool.name == "execute_command" and read_only_command(args["command"])):
                raise SecurityError("plan_blocked", "计划模式只开放只读工具和只读命令（如 git status/diff/log、ls）；此调用未执行。"
                                                    "调查完成后用 exit_plan_mode 提交计划，用户批准后才能写入、编辑或执行其他命令。")
        if getattr(tool, "permission_kind", None) == "memory":
            return Operation(tool.name, f"{args['scope']}:{args['name']}", preview=f"{args['action']} 笔记", notes=True)
        if getattr(tool, "permission_kind", None) == "mcp":
            target = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            return Operation(tool.name, target, preview="外部 MCP 调用；本地路径限制不约束 Server 的实际访问。",
                             server=getattr(tool, "server_name", ""))
        if tool.name == "execute_command":
            target = args["command"]
            cwd = project_path(self.root, args.get("cwd") or ".").relative_to(self.root).as_posix()
            check_command(target)
            risk = command_risk(target) or ""
            preview = f"高风险命令：{risk}。每次都需要确认，规则和档位不会自动放行。" if risk else ""
            return Operation(tool.name, target, cwd, preview=preview, risk=risk, read_only=read_only_command(target),
                             prefix="" if risk else command_prefix(target) or "")
        if tool.name in READ_TOOLS:
            resolved = resolve_path(self.root, args.get("path", "."))
        else:
            resolved = project_path(self.root, args.get("path", "."))
        outside = not inside(self.root, resolved)
        target = resolved.as_posix() if outside else resolved.relative_to(self.root).as_posix()
        if not tool.read_only:
            actual = project_path(self.root, target)
            protected = (self.user_path, self.project_file, self.local_file,
                         Path.home() / ".mosscode/mcp.yaml", self.root / ".mosscode/mcp.yaml", self.root / ".mcp.json")
            if any(actual == p.resolve() for p in protected):
                raise SecurityError("policy_protected", "权限与 MCP 配置文件由用户管理，模型不能自行写入或编辑。")
        preview = ""
        if tool.name == "write_file":
            preview = f"写入 {len(args['content'])} 个字符，内容预览：\n" + safe_display(args["content"])
        elif tool.name == "edit_file":
            preview = "原文：\n" + safe_display(args["old_text"], 400) + "\n替换为：\n" + safe_display(args["new_text"], 400)
        elif tool.name in {"glob", "grep"}:
            preview = "搜索模式：" + safe_display(args["pattern"], 400)
            if args.get("glob"):
                preview += "\n文件筛选：" + safe_display(args["glob"], 200)
        elif tool.name == "read_file":
            preview = f"从第 {args['offset']} 行读取，最多 {args['limit']} 行。"
        if tool.name == "grep" and args.get("output_mode"):
            preview += f"\n输出方式：{args['output_mode']}"
        if outside:
            preview = "项目目录之外\n" + preview
        return Operation(tool.name, target, preview=preview, outside=outside)

    def decide(self, operation: Operation, *, read_only: bool) -> Decision:
        matched = next((hits for layer in (tuple(self.session), *reversed(self.layers))
                        if (hits := [rule for rule in layer if rule.matches(operation)])), [])
        if any(rule.action == "deny" for rule in matched):
            return Decision("deny", "permission_denied", "命中显式拒绝规则；此调用未执行。")
        if operation.risk:
            return Decision("ask", reason=f"高风险命令（{operation.risk}）需要每次确认。")
        if matched or operation.notes:
            return Decision("allow")
        if (operation.outside and operation.tool in {"read_file", "glob"}
                and any(inside(folder, Path(operation.target)) for folder in self.memory_dirs)):
            return Decision("allow")  # Memory notes (9c J4), plan mode included.
        if (operation.outside and operation.tool == "read_file" and self.session_dir is not None
                and inside(self.session_dir, Path(operation.target))):
            return Decision("allow")  # Stored tool results and compaction records of this run.
        if operation.outside:
            if self.mode == "bypassPermissions":
                return Decision("allow")
            return Decision("ask", reason="读取项目目录之外的路径需要确认。")
        if self.mode == "bypassPermissions" or read_only or operation.read_only:
            return Decision("allow")
        if self.mode == "acceptEdits" and operation.tool in EDIT_TOOLS:
            return Decision("allow")
        return Decision("ask", reason="此操作没有明确的放行规则。")

    async def authorize(self, call_id: str, tool, params) -> Decision:
        if tool.name == EXIT_PLAN:
            return await self._approve_plan(call_id, params)
        # Hard checks always run, including for bypassPermissions and exact grants.
        try:
            operation = self.operation(tool, params)
        except SecurityError as exc:
            return Decision("deny", exc.code, str(exc))
        decision = self.decide(operation, read_only=tool.read_only)
        if decision.action != "ask":
            return decision
        if not self.interactive:
            return Decision("deny", "approval_required", "此操作需要用户确认；无界面模式不会自动授权。")
        async with self._approval_lock:
            # A preceding approval may have added a session/local rule.
            try:
                operation = self.operation(tool, params)
            except SecurityError as exc:
                return Decision("deny", exc.code, str(exc))
            decision = self.decide(operation, read_only=tool.read_only)
            if decision.action != "ask":
                return decision
            preview = await self._diff_preview(tool, params) if operation.tool in EDIT_TOOLS else None
            if preview:
                operation = replace(operation, preview=preview)
            request = ApprovalRequest(uuid4().hex, call_id, operation, decision.reason, self._choices_for(operation))
            try:
                choice = await self._ask(request)
                if choice == "deny":
                    return Decision("deny", "permission_denied", "用户拒绝了此操作；此调用未执行。")
                try:
                    current = self.operation(tool, params)
                except SecurityError as exc:
                    return Decision("deny", exc.code, str(exc))
                if (current.target, current.cwd) != (operation.target, operation.cwd):
                    return Decision("deny", "target_changed", "确认期间目标路径发生变化；请重新申请此操作。")
                latest = self.decide(current, read_only=tool.read_only)
                if latest.action == "deny":
                    return latest
                rule = Rule(operation.tool, operation.target, "allow", True, operation.cwd)
                if operation.server:
                    rule = Rule(operation.tool, "*", "allow")  # MCP grants cover the tool with any arguments.
                if choice == "prefix":
                    rule = Rule(operation.tool, operation.prefix + PREFIX_SUFFIX, "allow")
                if choice == "session":
                    self.session.append(rule)
                elif choice == "accept_edits":
                    self.set_mode("acceptEdits")
                elif choice in {"permanent", "prefix"}:
                    try:
                        self._persist(rule)
                    except (PermissionConfigError, OSError, SecurityError):
                        return Decision("deny", "permission_save_failed", "永久授权未保存成功，此调用未执行；可选择本次允许或检查本地权限文件。")
                latest = self.decide(current, read_only=tool.read_only)
                if latest.action == "deny":
                    return latest
                # The target might have changed while the user was deciding.
                try:
                    current = self.operation(tool, params)
                except SecurityError as exc:
                    return Decision("deny", exc.code, str(exc))
                if (current.target, current.cwd) != (operation.target, operation.cwd):
                    return Decision("deny", "target_changed", "确认期间目标路径发生变化；请重新申请此操作。")
                return Decision("allow")
            finally:
                self._settle(request)

    @staticmethod
    def _choices_for(operation: Operation) -> tuple[str, ...]:
        if operation.risk:
            return RISKY_CHOICES
        if operation.tool == "execute_command":
            return COMMAND_CHOICES if operation.prefix else CHOICES
        return EDIT_CHOICES if operation.tool in EDIT_TOOLS else CHOICES

    @staticmethod
    async def _diff_preview(tool, params) -> str | None:
        preview = getattr(tool, "approval_preview", None)
        if preview is None:
            return None
        try:
            return safe_display(await asyncio.to_thread(preview, params), 8000)
        except Exception:
            return None  # e.g. no match yet; the plain old/new preview still applies.

    async def _ask(self, request: ApprovalRequest) -> str:
        future = asyncio.get_running_loop().create_future()
        self.pending[request.id] = future
        self._choices[request.id] = request.choices
        await self.requests.put(request)
        return await future

    def _settle(self, request: ApprovalRequest) -> None:
        future = self.pending.pop(request.id, None)
        self._choices.pop(request.id, None)
        if future is not None and not future.done():
            future.cancel()

    async def _approve_plan(self, call_id: str, params) -> Decision:
        """The user, never a rule or mode, decides whether planning ends."""
        if self.mode != "plan":
            return Decision("deny", "not_in_plan", "当前不在计划模式，无需提交计划；直接按用户请求执行。")
        if not self.interactive:
            return Decision("deny", "approval_required", "计划需要用户批准；无界面模式不会自动批准，仍处于计划模式。")
        async with self._approval_lock:
            operation = Operation(EXIT_PLAN, "提交计划", preview=safe_display(params.plan, 4000))
            request = ApprovalRequest(uuid4().hex, call_id, operation, "请审阅计划，决定是否开始执行。", PLAN_CHOICES)
            try:
                choice = await self._ask(request)
            finally:
                self._settle(request)
            if choice == "deny":
                return Decision("deny", "permission_denied", "用户选择继续规划；仍处于计划模式。请根据用户的反馈调整计划。")
            if self.mode != "plan":
                return Decision("deny", "not_in_plan", "确认期间已离开计划模式；此调用未执行。")
            self.set_mode("acceptEdits" if choice == "accept_edits" else self.resume_mode)
            return Decision("allow")

    def respond(self, request_id: str, choice: str) -> bool:
        choices = self._choices.get(request_id)
        if choices is not None and choice not in choices:
            raise ValueError("该请求不支持此授权选项。")
        if choice not in ALL_CHOICES:
            raise ValueError("未知授权选项。")
        future = self.pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(choice)
        return True

    def _persist(self, rule: Rule) -> None:
        # Validate the current disk file again instead of overwriting external edits.
        path = project_path(self.root, self.local_file)
        raw, existing = read_policy(path)
        if rule.exact:
            entry = {"rule": f"{rule.tool}({rule.pattern})", "exact": True}
            if rule.cwd is not None:
                entry["cwd"] = rule.cwd
        elif is_mcp_tool(rule.tool):
            entry = rule.tool  # Whole-tool MCP grant, Claude Code style.
        else:
            entry = f"Bash({rule.pattern})"  # Prefix grant: readable and editable by hand.
        raw.setdefault("allow", []).append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".permissions-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.layers[2] = (*existing, rule)
