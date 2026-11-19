"""Mapping between MossCode StreamEvents/state and ACP session/update notifications."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AvailableCommand,
    AvailableCommandInput,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    ContentToolCallContent,
    CurrentModeUpdate,
    FileEditToolCallContent,
    ImageContentBlock,
    PlanEntry,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
    StopReason,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    ToolKind,
    UnstructuredCommandInput,
    UserMessageChunk,
)

from ..appearance import compact_notice
from ..commands import CommandRegistry
from ..config import ProviderConfig, Settings
from ..credentials import redact
from ..messages import (
    REMINDER,
    AutoContinue,
    CompactFailed,
    CompactFinished,
    CompactStarted,
    ContextCollapsed,
    Delta,
    FilesAttached,
    Message,
    Notice,
    ReplyRetry,
    ResponseBoundary,
    ResultsCleared,
    Retrying,
    RunStopped,
    StreamEvent,
    TasksNotified,
    ToolCall,
    ToolProgress,
    ToolResult,
    UserInput,
)
from ..permissions import MODES as PERMISSION_MODES
from ..permissions import ApprovalRequest
from ..reasoning import DEFAULT as DEFAULT_THINKING
from ..reasoning import LEVEL_LABELS, LEVELS, Unsupported, thinking_options
from ..todo import TodoItem
from ..tools.diffing import DIFF_INPUT_LIMIT
from ..tools.files import EditFile, EditParams, ToolFailure, read_text

SESSION_MODES: tuple[SessionMode, ...] = (
    SessionMode(
        id="default",
        name="默认 (Default)",
        description="读取与搜索自动放行，修改文件、执行命令和联网请求需审批",
    ),
    SessionMode(
        id="acceptEdits",
        name="自动接受编辑 (Accept Edits)",
        description="工作区内文件写入与编辑自动放行，执行命令仍需审批",
    ),
    SessionMode(
        id="plan",
        name="规划模式 (Plan)",
        description="只读探索代码并编写方案，禁止修改文件或执行非只读命令",
    ),
    SessionMode(
        id="bypassPermissions",
        name="跳过审批 (Bypass Permissions)",
        description="自动放行所有常规操作（受保护路径与高危拦截除外）",
    ),
)

# Terminal-only commands or lifecycle commands replaced by ACP host UI.
EXCLUDED_ACP_COMMANDS = frozenset(
    {
        "exit",
        "fork",
        "resume",
        "rewind",
        "sandbox",
        "worktree",
        "team",
    }
)

TOOL_KIND_MAP: dict[str, ToolKind] = {
    "read_file": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "memory": "read",
    "glob": "search",
    "grep": "search",
    "repo_map": "search",
    "web_search": "search",
    "web_fetch": "fetch",
    "execute_command": "execute",
    "exit_plan_mode": "switch_mode",
    "todo_write": "think",
    "load_skill": "think",
    "agent": "think",
    "ask_user": "other",
    "task_output": "other",
    "task_stop": "other",
    "team_create": "other",
    "team_spawn": "other",
    "team_delete": "other",
    "team_merge": "other",
    "team_task_create": "other",
    "team_task_get": "other",
    "team_task_list": "other",
    "team_task_update": "other",
    "send_message": "other",
    "post_board": "other",
    "read_board": "read",
}

SessionUpdateItem = (
    UserMessageChunk
    | AgentMessageChunk
    | AgentThoughtChunk
    | ToolCallStart
    | ToolCallProgress
    | AgentPlanUpdate
    | AvailableCommandsUpdate
    | CurrentModeUpdate
    | ConfigOptionUpdate
)


def tool_kind(name: str) -> ToolKind:
    return TOOL_KIND_MAP.get(name, "other")


def parse_tool_arguments(arguments: str) -> dict[str, Any]:
    if not arguments or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return {"_raw": arguments}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def resolve_session_path(cwd: Path, raw_path: str | None) -> Path | None:
    if not raw_path or not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def display_path(cwd: Path, path: Path) -> str:
    try:
        return path.relative_to(cwd.resolve()).as_posix()
    except ValueError:
        return str(path)


def tool_title(name: str, raw_input: dict[str, Any], cwd: Path, *, source: str = "") -> str:
    prefix = f"[{source}] " if source else ""
    path_val = raw_input.get("path")
    resolved = resolve_session_path(cwd, path_val if isinstance(path_val, str) else None)
    rel = display_path(cwd, resolved) if resolved is not None else (str(path_val) if path_val else "")

    if name == "read_file":
        symbol = raw_input.get("symbol")
        if raw_input.get("outline") is True:
            base = f"读取大纲 {rel}".strip() if rel else "读取文件大纲"
        elif isinstance(symbol, str) and symbol.strip():
            sym = symbol.strip()
            base = f"读取符号 {rel} · {sym}" if rel else f"读取符号 {sym}"
        else:
            base = f"读取 {rel}" if rel else "读取文件"
    elif name == "write_file":
        base = f"写入 {rel}" if rel else "写入文件"
    elif name == "edit_file":
        base = f"编辑 {rel}" if rel else "编辑文件"
    elif name == "glob":
        pattern = raw_input.get("pattern", "")
        base = f"查找文件 {pattern}".strip()
    elif name == "grep":
        pattern = raw_input.get("pattern", "")
        base = f"搜索内容 {pattern}".strip()
    elif name == "repo_map":
        parts = [f"生成代码地图 {rel or '.'}"]
        query = raw_input.get("query")
        if isinstance(query, str) and query.strip():
            parts.append(query.strip()[:60])
        focus = raw_input.get("focus_files")
        if isinstance(focus, list) and focus:
            parts.append(f"聚焦 {len(focus)} 个文件")
        base = " · ".join(parts)
    elif name == "execute_command":
        desc = raw_input.get("description") or raw_input.get("command") or ""
        short = str(desc).strip().splitlines()[0][:80] if desc else ""
        base = f"运行 `{short}`" if short else "执行命令"
    elif name == "web_fetch":
        url = raw_input.get("url") or raw_input.get("target") or ""
        host = urlsplit(str(url)).hostname or str(url)
        base = f"抓取 {host}".strip()
    elif name == "web_search":
        query = raw_input.get("query", "")
        base = f"搜索 {query}".strip()
    elif name == "exit_plan_mode":
        base = "提交实施计划审批"
    elif name == "todo_write":
        base = "更新任务清单"
    elif name == "ask_user":
        base = "向用户询问选择"
    elif name == "agent":
        desc = raw_input.get("description") or raw_input.get("subagent_type") or ""
        base = f"子 Agent {desc}".strip() if desc else "子 Agent"
    elif name == "memory":
        scope = raw_input.get("scope", "")
        note_name = raw_input.get("name", "")
        target = f"{scope}:{note_name}".strip(":")
        base = f"操作记忆 {target}".strip()
    elif name == "load_skill":
        skill = raw_input.get("name", "")
        base = f"加载 Skill {skill}".strip()
    elif name == "post_board":
        summary = raw_input.get("summary") or raw_input.get("body") or ""
        short = str(summary).strip().splitlines()[0][:60] if summary else ""
        base = f"发布黑板 {short}".strip() if short else "发布团队黑板"
    elif name == "read_board":
        seqs = raw_input.get("seqs")
        if isinstance(seqs, list) and seqs:
            shown = " ".join(f"#{item}" for item in seqs[:8])
            base = f"读取黑板 {shown}"
        elif raw_input.get("since") is not None:
            base = f"读取黑板 since #{raw_input['since']}"
        else:
            base = "读取团队黑板"
    elif name.startswith(("mcp__", "skill__")):
        parts = name.split("__", 2)
        base = f"{parts[1]}/{parts[2]}" if len(parts) == 3 else name
    else:
        base = name
    return f"{prefix}{base}"


def tool_locations(
    name: str,
    raw_input: dict[str, Any],
    cwd: Path,
    *,
    details: dict[str, Any] | None = None,
    line: int | None = None,
) -> list[ToolCallLocation] | None:
    if name not in {"read_file", "write_file", "edit_file", "glob", "grep", "repo_map"}:
        return None
    raw_path = (details or {}).get("path") or raw_input.get("path")
    if name in {"glob", "grep", "repo_map"} and (not isinstance(raw_path, str) or not raw_path.strip()):
        return [ToolCallLocation(path=str(cwd))]
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    resolved = resolve_session_path(cwd, raw_path)
    if resolved is None:
        return None
    loc_line = line
    if loc_line is None and name == "read_file":
        offset = raw_input.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 1:
            loc_line = offset
    return [ToolCallLocation(path=str(resolved), line=loc_line)]


def _safe_read_for_diff(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > DIFF_INPUT_LIMIT:
            return None
        text = read_text(path)
    except (OSError, UnicodeError, ToolFailure):
        return None
    return text if len(text) <= DIFF_INPUT_LIMIT else None


def preview_file_diff(
    name: str,
    raw_input: dict[str, Any],
    cwd: Path,
    *,
    before_override: str | None = None,
    existed_override: bool | None = None,
) -> tuple[FileEditToolCallContent | None, int | None]:
    """Compute a structured ACP FileEditToolCallContent and starting line for write_file/edit_file."""
    if name not in {"write_file", "edit_file"}:
        return None, None
    raw_path = raw_input.get("path")
    resolved = resolve_session_path(cwd, raw_path if isinstance(raw_path, str) else None)
    if resolved is None:
        return None, None

    existed = existed_override if existed_override is not None else resolved.is_file()
    old_text = before_override if before_override is not None else (_safe_read_for_diff(resolved) if existed else None)
    if existed and old_text is None:
        return None, None

    if name == "write_file":
        content = raw_input.get("content")
        if not isinstance(content, str) or len(content) > DIFF_INPUT_LIMIT:
            return None, None
        return (
            FileEditToolCallContent(
                path=str(resolved),
                old_text=redact(old_text) if old_text is not None else None,
                new_text=redact(content),
            ),
            1,
        )

    if old_text is None:
        return None, None
    try:
        # Validate the same shape the tool takes, so batch `edits` get a diff too.
        params = EditParams.model_validate({**raw_input, "path": str(resolved)})
        new_text, _, first_line, _ = EditFile._replace(old_text, params)
    except Exception:
        return None, None
    if len(new_text) > DIFF_INPUT_LIMIT:
        return None, first_line
    return (
        FileEditToolCallContent(
            path=str(resolved),
            old_text=redact(old_text),
            new_text=redact(new_text),
        ),
        first_line,
    )


def text_chunk(text: str) -> AgentMessageChunk:
    return AgentMessageChunk(content=TextContentBlock(text=text))


def thought_chunk(text: str) -> AgentThoughtChunk:
    return AgentThoughtChunk(content=TextContentBlock(text=text))


def user_chunk(text: str) -> UserMessageChunk:
    return UserMessageChunk(content=TextContentBlock(text=text))


def todos_to_plan_update(items: Sequence[TodoItem]) -> AgentPlanUpdate:
    entries = [PlanEntry(content=item.content, priority="medium", status=item.status) for item in items]
    return AgentPlanUpdate(entries=entries)


def build_available_commands(registry: CommandRegistry) -> AvailableCommandsUpdate:
    from .formatting import ACP_DESCRIPTION_OVERRIDES, summarize_command_description

    commands: list[AvailableCommand] = []
    for cmd in registry.visible():
        if cmd.name in EXCLUDED_ACP_COMMANDS:
            continue
        cmd_input = (
            AvailableCommandInput(root=UnstructuredCommandInput(hint=cmd.argument_hint)) if cmd.argument_hint else None
        )
        raw_desc = ACP_DESCRIPTION_OVERRIDES.get(cmd.name, cmd.description)
        desc = summarize_command_description(raw_desc)
        commands.append(AvailableCommand(name=cmd.name, description=desc, input=cmd_input))
    return AvailableCommandsUpdate(available_commands=commands)


def build_mode_state(current_mode: str) -> SessionModeState:
    mode_id = current_mode if current_mode in PERMISSION_MODES else "default"
    return SessionModeState(current_mode_id=mode_id, available_modes=list(SESSION_MODES))


def supported_thinking_levels(config: ProviderConfig) -> list[str]:
    supported: list[str] = []
    for level in LEVELS:
        try:
            thinking_options(config, level)
        except Unsupported:
            continue
        supported.append(level)
    return supported


def build_config_options(
    current_mode: str,
    settings: Settings,
    current_provider: ProviderConfig,
    current_thinking: str | None,
) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
    mode_id = current_mode if current_mode in PERMISSION_MODES else "default"
    options: list[SessionConfigOptionSelect | SessionConfigOptionBoolean] = [
        SessionConfigOptionSelect(
            id="mode",
            name="Mode",
            description="会话权限与规划模式",
            category="mode",
            current_value=mode_id,
            options=[
                SessionConfigSelectOption(value=m.id, name=m.name, description=m.description) for m in SESSION_MODES
            ],
        ),
        SessionConfigOptionSelect(
            id="model",
            name="Model",
            description="当前会话使用的模型 Provider",
            category="model",
            current_value=current_provider.name,
            options=[
                SessionConfigSelectOption(
                    value=p.name,
                    name=f"{p.name} ({p.model})",
                    description=p.protocol,
                )
                for p in settings.providers
            ],
        ),
    ]
    levels = supported_thinking_levels(current_provider)
    if levels:
        select_options = [
            SessionConfigSelectOption(
                value=DEFAULT_THINKING,
                name=LEVEL_LABELS[DEFAULT_THINKING],
                description="使用配置文件中的默认思考设置",
            ),
            *[
                SessionConfigSelectOption(
                    value=lvl,
                    name=LEVEL_LABELS.get(lvl, lvl),
                    description=f"思考强度：{LEVEL_LABELS.get(lvl, lvl)}",
                )
                for lvl in levels
            ],
        ]
        current_val = current_thinking if current_thinking in levels else DEFAULT_THINKING
        options.append(
            SessionConfigOptionSelect(
                id="thought_level",
                name="Thinking",
                description="推理思考强度档位",
                category="thought_level",
                current_value=current_val,
                options=select_options,
            )
        )
    return options


def map_stop_reason(stopped: RunStopped | None) -> StopReason:
    if stopped is None or stopped.reason == "completed":
        return "end_turn"
    if stopped.reason == "max_tokens":
        return "max_tokens"
    if stopped.reason == "max_iterations":
        return "max_turn_requests"
    if stopped.reason == "cancelled":
        return "cancelled"
    if stopped.reason == "refusal":
        return "refusal"
    return "end_turn"


@dataclass
class _TrackedCall:
    call: ToolCall
    raw_input: dict[str, Any]
    started_sent: bool = False
    existed_before: bool | None = None
    before_text: str | None = None
    first_line: int | None = None


class AcpEventMapper:
    """Stateful mapper for one session's streaming events and tool calls."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._calls: dict[str, _TrackedCall] = {}
        self._fence_open = False
        self._backtick_run = 0
        self._buffered_notices: list[str] = []

    def _advance_fence(self, text: str) -> None:
        run = self._backtick_run
        for ch in text:
            if ch == "`":
                run += 1
                if run == 3:
                    self._fence_open = not self._fence_open
                    run = 0
            else:
                run = 0
        self._backtick_run = run

    def _emit_notice(self, text: str) -> list[SessionUpdateItem]:
        if not text:
            return []
        formatted = f"\n> {text}\n"
        if self._fence_open:
            self._buffered_notices.append(formatted)
            return []
        return [text_chunk(formatted)]

    def flush_notices(self) -> list[SessionUpdateItem]:
        self._fence_open = False
        if not self._buffered_notices:
            return []
        joined = "".join(self._buffered_notices)
        self._buffered_notices.clear()
        return [text_chunk(joined)]

    def map_event(self, event: StreamEvent) -> list[SessionUpdateItem]:
        if isinstance(event, Delta):
            if not event.text:
                return []
            if event.kind == "thinking":
                return [thought_chunk(event.text)]
            self._advance_fence(event.text)
            updates: list[SessionUpdateItem] = [text_chunk(event.text)]
            if not self._fence_open and self._buffered_notices:
                updates.extend(self.flush_notices())
            return updates

        if isinstance(event, ResponseBoundary):
            self._backtick_run = 0
            if self._buffered_notices and not self._fence_open:
                return self.flush_notices()
            return []

        if isinstance(event, ToolProgress):
            self._backtick_run = 0
            return self.flush_notices() + self._map_tool_progress(event)

        if isinstance(event, Notice):
            return self._emit_notice(event.text)

        if isinstance(event, Retrying):
            return self._emit_notice(
                f"{event.reason}，{event.delay:.1f} 秒后重试（{event.attempt}/{event.max_attempts}）"
            )

        if isinstance(event, ReplyRetry):
            return self._emit_notice(f"{event.reason}，正在重新请求（{event.attempt}/{event.max_attempts}）")

        if isinstance(event, AutoContinue):
            return self._emit_notice(event.text)

        if isinstance(event, CompactStarted):
            kind = "手动" if event.manual else "自动"
            return self._emit_notice(f"正在执行{kind}上下文压缩…")

        if isinstance(event, (CompactFinished, CompactFailed, ResultsCleared, ContextCollapsed)):
            return self._emit_notice(compact_notice(event))

        if isinstance(event, FilesAttached):
            if not event.entries:
                return []
            return self._emit_notice("已附上 " + "、".join(event.entries))

        if isinstance(event, TasksNotified):
            if not event.texts:
                return []
            return self._emit_notice(f"收到 {len(event.texts)} 条后台任务通知。")

        if isinstance(event, UserInput):
            return [user_chunk(text) for text in event.texts if text]

        if isinstance(event, RunStopped):
            self._backtick_run = 0
            updates = self.flush_notices()
            if (
                event.reason
                in {
                    "max_iterations",
                    "max_tokens",
                    "unknown_tools",
                    "repeated_calls",
                    "finish_check_failed",
                    "error",
                }
                and event.notice
            ):
                updates.append(text_chunk(f"\n> {redact(event.notice)}\n"))
            return updates

        return []

    def _map_tool_progress(self, event: ToolProgress) -> list[SessionUpdateItem]:
        call = event.call
        raw_input = parse_tool_arguments(call.arguments)
        tracked = self._calls.get(call.id)
        if tracked is None:
            tracked = _TrackedCall(call=call, raw_input=raw_input)
            self._calls[call.id] = tracked
        else:
            tracked.call = call
            if raw_input:
                tracked.raw_input = raw_input

        if event.phase == "parsing":
            tracked.started_sent = True
            return [
                ToolCallStart(
                    tool_call_id=call.id,
                    name=call.name,
                    title=tool_title(call.name, tracked.raw_input, self.cwd),
                    kind=tool_kind(call.name),
                    status="pending",
                    raw_input=tracked.raw_input or None,
                )
            ]

        if event.phase == "running":
            if call.name in {"write_file", "edit_file"}:
                resolved = resolve_session_path(
                    self.cwd,
                    tracked.raw_input.get("path") if isinstance(tracked.raw_input.get("path"), str) else None,
                )
                if resolved is not None and tracked.existed_before is None:
                    tracked.existed_before = resolved.is_file()
                    if tracked.existed_before:
                        tracked.before_text = _safe_read_for_diff(resolved)
                _, first_line = preview_file_diff(
                    call.name,
                    tracked.raw_input,
                    self.cwd,
                    before_override=tracked.before_text,
                    existed_override=tracked.existed_before,
                )
                tracked.first_line = (
                    first_line if first_line is not None else (1 if call.name == "write_file" else None)
                )

            locations = tool_locations(
                call.name,
                tracked.raw_input,
                self.cwd,
                line=tracked.first_line,
            )
            title = tool_title(call.name, tracked.raw_input, self.cwd)
            if not tracked.started_sent:
                tracked.started_sent = True
                return [
                    ToolCallStart(
                        tool_call_id=call.id,
                        name=call.name,
                        title=title,
                        kind=tool_kind(call.name),
                        status="in_progress",
                        raw_input=tracked.raw_input or None,
                        locations=locations,
                    )
                ]
            return [
                ToolCallProgress(
                    tool_call_id=call.id,
                    name=call.name,
                    title=title,
                    kind=tool_kind(call.name),
                    status="in_progress",
                    raw_input=tracked.raw_input or None,
                    locations=locations,
                )
            ]

        # phase == "finished"
        result = event.result
        self._calls.pop(call.id, None)
        if result is None:
            return []
        return self._build_finished_updates(tracked, result)

    def _build_finished_updates(self, tracked: _TrackedCall, result: ToolResult) -> list[SessionUpdateItem]:
        call = tracked.call
        status = "failed" if result.is_error else "completed"
        locations = tool_locations(
            call.name,
            tracked.raw_input,
            self.cwd,
            details=result.details,
            line=tracked.first_line,
        )
        content_items: list[ContentToolCallContent | FileEditToolCallContent] = []
        if not result.is_error and call.name in {"write_file", "edit_file"}:
            diff_item = self._finished_diff_content(tracked, result)
            if diff_item is not None:
                content_items.append(diff_item)
            elif isinstance(result.details.get("diff"), str) and result.details["diff"]:
                content_items.append(ContentToolCallContent(content=TextContentBlock(text=result.details["diff"])))

        if result.output:
            content_items.append(ContentToolCallContent(content=TextContentBlock(text=result.output)))

        for image in result.images:
            try:
                encoded = image.base64()
            except OSError:
                continue
            content_items.append(
                ContentToolCallContent(content=ImageContentBlock(data=encoded, mime_type=image.media_type))
            )

        updates: list[SessionUpdateItem] = []
        title = tool_title(call.name, tracked.raw_input, self.cwd)
        if not tracked.started_sent:
            updates.append(
                ToolCallStart(
                    tool_call_id=call.id,
                    name=call.name,
                    title=title,
                    kind=tool_kind(call.name),
                    status="in_progress",
                    raw_input=tracked.raw_input or None,
                    locations=locations,
                )
            )
        updates.append(
            ToolCallProgress(
                tool_call_id=call.id,
                name=call.name,
                title=title,
                kind=tool_kind(call.name),
                status=status,
                locations=locations,
                content=content_items or None,
                raw_input=tracked.raw_input or None,
                raw_output={"output": result.output, "is_error": result.is_error, "code": result.code},
            )
        )
        return updates

    def _finished_diff_content(self, tracked: _TrackedCall, result: ToolResult) -> FileEditToolCallContent | None:
        raw_path = result.details.get("path") or tracked.raw_input.get("path")
        resolved = resolve_session_path(self.cwd, raw_path if isinstance(raw_path, str) else None)
        if resolved is None:
            return None
        created = bool(result.details.get("created", False))
        existed = False if created else (tracked.existed_before if tracked.existed_before is not None else True)
        old_text = None if not existed else tracked.before_text
        if existed and old_text is None:
            return None
        new_text = _safe_read_for_diff(resolved)
        if new_text is None and tracked.call.name == "write_file":
            candidate = tracked.raw_input.get("content")
            if isinstance(candidate, str) and len(candidate) <= DIFF_INPUT_LIMIT:
                new_text = candidate
        if new_text is None:
            return None
        return FileEditToolCallContent(
            path=str(resolved),
            old_text=redact(old_text) if old_text is not None else None,
            new_text=redact(new_text),
        )

    def approval_tool_call_update(self, request: ApprovalRequest) -> ToolCallUpdate:
        """Build the ToolCallUpdate payload attached to session/request_permission."""
        op = request.operation
        matched = self._calls.get(request.call_id)
        if op.network:
            call_id = f"{request.call_id or 'net'}:{request.id}"
            raw_input: dict[str, Any] = {"target": op.target}
        elif matched is not None:
            call_id = matched.call.id
            raw_input = matched.raw_input
        else:
            call_id = f"sub:{request.call_id}:{request.id}" if request.call_id else f"approval:{request.id}"
            raw_input = {"target": op.target}

        title = tool_title(op.tool, raw_input, self.cwd, source=request.source)
        if request.reason and not op.network:
            title = f"{title}（{request.reason}）"
        elif op.network:
            title = f"{title}（{op.target}）"

        content_items: list[ContentToolCallContent | FileEditToolCallContent] = []
        line: int | None = matched.first_line if matched is not None else None
        if not op.network and op.tool in {"write_file", "edit_file"} and matched is not None:
            diff_item, first_line = preview_file_diff(
                op.tool,
                matched.raw_input,
                self.cwd,
                before_override=matched.before_text,
                existed_override=matched.existed_before,
            )
            if first_line is not None:
                line = first_line
            if diff_item is not None:
                content_items.append(diff_item)
        if op.preview and not content_items:
            content_items.append(ContentToolCallContent(content=TextContentBlock(text=redact(op.preview))))

        locations = None if op.network else tool_locations(op.tool, raw_input, self.cwd, line=line)
        if (
            locations is None
            and not op.network
            and op.tool in {"read_file", "write_file", "edit_file", "glob", "grep", "repo_map"}
        ):
            if op.target:
                resolved = resolve_session_path(self.cwd, op.target)
                if resolved is not None:
                    locations = [ToolCallLocation(path=str(resolved), line=line)]

        return ToolCallUpdate(
            tool_call_id=call_id,
            name=op.tool,
            title=title,
            kind=tool_kind(op.tool),
            status="pending",
            locations=locations,
            content=content_items or None,
            raw_input=raw_input or None,
        )


def replay_history_updates(messages: Sequence[Message], cwd: Path) -> list[SessionUpdateItem]:
    """Convert restored Session history messages into ACP session/update notifications for session/load."""
    updates: list[SessionUpdateItem] = []
    call_meta: dict[str, tuple[str, dict[str, Any]]] = {}

    for msg in messages:
        if msg.synthetic == REMINDER:
            continue
        if msg.role == "user":
            if msg.tool_results:
                for res in msg.tool_results:
                    updates.append(_replayed_result(res, call_meta, cwd))
            if not msg.synthetic:
                text = msg.shown
                if text:
                    updates.append(user_chunk(text))
                else:
                    image_labels = "、".join(b.text or "[图片]" for b in msg.content if b.kind == "image")
                    if image_labels:
                        updates.append(user_chunk(image_labels))
        elif msg.role == "assistant":
            for block in msg.content:
                if not block.text:
                    continue
                if block.kind == "thinking":
                    updates.append(thought_chunk(block.text))
                elif block.kind == "text":
                    updates.append(text_chunk(block.text))
            for call in msg.tool_calls:
                raw_in = parse_tool_arguments(call.arguments)
                call_meta[call.id] = (call.name, raw_in)
                updates.append(
                    ToolCallStart(
                        tool_call_id=call.id,
                        name=call.name,
                        title=tool_title(call.name, raw_in, cwd),
                        kind=tool_kind(call.name),
                        status="in_progress",
                        raw_input=raw_in or None,
                        locations=tool_locations(call.name, raw_in, cwd),
                    )
                )
        elif msg.role == "tool":
            for res in msg.tool_results:
                updates.append(_replayed_result(res, call_meta, cwd))
    return updates


def _replayed_result(
    res: ToolResult,
    call_meta: dict[str, tuple[str, dict[str, Any]]],
    cwd: Path,
) -> ToolCallProgress:
    name, raw_in = call_meta.get(res.call_id, (res.name, {}))
    status = "failed" if res.is_error else "completed"
    content_items: list[ContentToolCallContent] = []
    if res.output:
        content_items.append(ContentToolCallContent(content=TextContentBlock(text=res.output)))
    return ToolCallProgress(
        tool_call_id=res.call_id,
        name=name,
        title=tool_title(name, raw_in, cwd),
        kind=tool_kind(name),
        status=status,
        locations=tool_locations(name, raw_in, cwd, details=res.details),
        content=content_items or None,
        raw_output={"output": res.output, "is_error": res.is_error, "code": res.code},
    )
