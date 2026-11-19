"""Single-session ACP state and lifecycle management."""

from __future__ import annotations

import asyncio
import base64
import inspect
import os
import re
import sys
from collections.abc import Callable, Sequence
from contextlib import aclosing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from acp import RequestError
from acp.schema import (
    AcpMcpServer,
    AllowedOutcome,
    AudioContentBlock,
    AvailableCommand,
    AvailableCommandInput,
    AvailableCommandsUpdate,
    BlobResourceContents,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    LoadSessionResponse,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    ResourceContentBlock,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    TextContentBlock,
    TextResourceContents,
    ToolCallProgress,
    ToolCallUpdate,
    UnstructuredCommandInput,
)

from ..agents.tasks import STATUS_LABELS
from ..builtin_commands import (
    BUILTIN,
    CLEARED,
    LOOP_HEADLESS_NOTICE,
    MODE_LABELS,
    PLAN_ENTERED,
    TEAM_USAGE,
    hooks_report,
    loop_report,
    mcp_report,
    memory_report,
    resolve_model_target,
    swarm_report,
    tasks_report,
    team_report,
    thinking_detail,
    typed,
)
from ..command_env import use_command_env
from ..commands import CommandRegistry, argument_problem, parse_input, unknown_note
from ..config import ConfigError, ProviderConfig, Settings, refreshed
from ..conversation import Conversation, Resumed
from ..coordinator import SWARM_OFF_NOTE, SWARM_ON_NOTE
from ..coordinator import decide as decide_coordinator
from ..credentials import redact
from ..goal import GOAL
from ..images import ImageError, ProcessedImage, load_image
from ..mcp_config import SERVER_NAME, ServerConfig
from ..mcp_surface import (
    PromptArgumentError,
    expand_mcp_prompt,
    is_mcp_prompt_command,
    register_mcp_prompts,
    register_mcp_prompts_from_manager,
)
from ..mentions import quoted
from ..messages import NOTIFICATION, ProviderError, RunStopped, TasksNotified, ToolProgress, image_mark
from ..permissions import MODES as PERMISSION_MODES
from ..permissions import (
    ApprovalRequest,
    Operation,
    PermissionConfigError,
    PermissionPolicy,
    QuestionRequest,
    normalize_mode,
)
from ..plugins import Installed
from ..providers import create_provider
from ..reasoning import DEFAULT as DEFAULT_THINKING
from ..reasoning import LEVELS
from ..runtime_factory import (
    assemble_workspace_extensions,
    build_conversation,
    claude_notice,
    resolve_plugins,
    with_model,
)
from ..sandbox import HOME_SECRETS, Sandbox, use_sandbox
from ..security import inside
from ..session import Session, SessionLocked, _clean_title, find_session, write_title
from ..skills import ForkRequest
from ..todo import TodoItem
from .auth import load_session_settings, remember_settings_credentials
from .events import (
    EXCLUDED_ACP_COMMANDS,
    AcpEventMapper,
    SessionUpdateItem,
    build_available_commands,
    build_config_options,
    build_mode_state,
    display_path,
    map_stop_reason,
    replay_history_updates,
    supported_thinking_levels,
    text_chunk,
    todos_to_plan_update,
)
from .formatting import (
    escape_markdown_inline,
    format_agents_markdown,
    format_goal_markdown,
    format_help_markdown,
    format_model_markdown,
    format_notices_markdown,
    format_permissions_markdown,
    format_session_markdown,
    format_skills_markdown,
    format_status_markdown,
    format_terminal_report_markdown,
    format_thinking_markdown,
    format_todo_markdown,
    summarize_command_description,
)
from .permissions import handle_approval_request, handle_question_request

if TYPE_CHECKING:
    from acp import Client

McpServerParam = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio


@dataclass(frozen=True)
class AcpLaunchOptions:
    """Command-line options passed to `moss acp`."""

    config: str | None = None
    provider: str | None = None
    permission_mode: str | None = None
    no_hooks: bool = False
    sandbox: bool | None = None
    trust_project_config: bool = False
    trust_project_mcp: bool = False
    trust_project_hooks: bool = False
    trust_project_plugins: bool = False
    home: Path | None = None
    settings: Settings | None = None
    provider_factory: Callable[[ProviderConfig], Any] | None = None


def _is_file_uri(uri: str | None) -> bool:
    return bool(uri) and uri.lstrip()[:5].lower() == "file:"


def _uri_paths(uri: str) -> tuple[Path, Path] | None:
    """Parse a file: URI into (lexical_abspath, resolved_path), or None when non-file or invalid."""
    if not _is_file_uri(uri):
        return None
    stripped = uri.lstrip()
    if "\0" in stripped:
        return None
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return None
    if parsed.scheme.lower() != "file":
        return None
    netloc = parsed.netloc
    if netloc and netloc.lower() != "localhost":
        if os.name == "nt":
            if re.fullmatch(r"[A-Za-z]:", netloc) and parsed.path.startswith("/"):
                url_path = f"/{netloc}{parsed.path}"
            elif parsed.path.startswith("/"):
                url_path = f"//{netloc}{parsed.path}"
            else:
                return None
        else:
            return None
    else:
        if not parsed.path.startswith("/"):
            return None
        url_path = parsed.path
    try:
        raw_path = url2pathname(url_path)
    except (ValueError, OSError):
        return None
    if not raw_path or "\0" in raw_path:
        return None
    try:
        lexical = Path(os.path.abspath(raw_path))
        resolved = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return lexical, resolved


def _uri_to_path(uri: str) -> Path | None:
    pair = _uri_paths(uri)
    return pair[1] if pair is not None else None


def convert_client_mcp_servers(
    mcp_servers: Sequence[McpServerParam] | None,
) -> tuple[dict[str, ServerConfig], list[str]]:
    """Convert ACP client mcpServers definitions into MossCode ServerConfig objects."""
    servers: dict[str, ServerConfig] = {}
    notices: list[str] = []
    for item in mcp_servers or ():
        if not re.fullmatch(SERVER_NAME, item.name):
            notices.append(f"客户端 MCP 服务器名称 {item.name!r} 不合法，已跳过。")
            continue
        if isinstance(item, McpServerStdio):
            if not item.command or "\0" in item.command:
                notices.append(f"MCP {item.name} 的 command 不合法，已跳过。")
                continue
            env = {var.name: var.value for var in item.env if var.name and "\0" not in var.value}
            servers[item.name] = ServerConfig(
                name=item.name,
                type="stdio",
                command=item.command,
                args=tuple(item.args),
                env=env,
            )
        elif isinstance(item, HttpMcpServer):
            parsed = urlsplit(item.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                notices.append(f"MCP {item.name} 的 URL 不合法，已跳过。")
                continue
            headers = {h.name: h.value for h in item.headers}
            servers[item.name] = ServerConfig(
                name=item.name,
                type="http",
                url=item.url,
                headers=headers,
            )
        elif isinstance(item, (SseMcpServer, AcpMcpServer)):
            kind = getattr(item, "type", "unknown")
            notices.append(f"MCP 服务器 {item.name} 使用了暂不支持的 {kind} 传输协议，已跳过。")
    return servers, notices


_ACP_UNTRUSTED_PLUGINS_HINT = (
    "项目插件 {names} 未经确认，未加载；请在终端启动一次确认，或传入 --trust-project-plugins。"
)
_ACP_UNTRUSTED_MCP_HINT = "项目 MCP 服务器 {names} 未经确认，未启动；请在终端确认或传入 --trust-project-mcp。"
_ACP_UNTRUSTED_HOOK_HINT = "项目 Hook 文件 {path} 未经确认，未启用；可在终端确认或传入 --trust-project-hooks。"


def _load_plugins_headless(
    cwd: Path,
    *,
    trust_project: bool = False,
    home: Path | None = None,
) -> Installed:
    return resolve_plugins(
        cwd,
        trust_project=trust_project,
        home=home,
        untrusted_hint=_ACP_UNTRUSTED_PLUGINS_HINT,
    )


def _decode_image_block(data_b64: str, mime_type: str) -> ProcessedImage | None:
    try:
        raw = base64.b64decode(data_b64, validate=True)
        return load_image(raw)
    except (ImageError, ValueError, Exception):
        return None


class AcpSession:
    """Wraps one isolated MossCode Conversation bound to an explicit working directory."""

    def __init__(
        self,
        *,
        session_id: str,
        cwd: Path,
        client: Client,
        settings: Settings,
        provider_config: ProviderConfig,
        conversation: Conversation,
        commands: CommandRegistry,
        options: AcpLaunchOptions,
        sandbox: Sandbox | None = None,
        command_env: tuple[tuple[str, ...], tuple[str, ...]] | None = None,
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.client = client
        self.settings = settings
        self._configured_providers = settings.providers
        self.provider_config = provider_config
        self.conversation = conversation
        self.commands = commands
        self.options = options
        self.sandbox = sandbox or Sandbox(settings.sandbox, enabled=options.sandbox, source=settings.source)
        self.command_env = (
            command_env if command_env is not None else (settings.command_env_keep, settings.command_env_drop)
        )
        self.mapper = AcpEventMapper(cwd)

        self._started = False
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._cancelled = False
        self._active_task: asyncio.Task[Any] | None = None
        self._startup_notices: list[str] = []
        self._pending_approvals: set[str] = set()
        self._pending_questions: dict[str, int] = {}
        self._bg_approval_tasks: dict[str, asyncio.Task[Any]] = {}
        self._deferred_approvals: list[ApprovalRequest] = []
        self._team_notes: list[str] = []
        self._due_loops: list[tuple[Any, str, int]] = []
        self._loop_window_open = False
        self._bg_wakeup = asyncio.Event()
        self._update_tasks: set[asyncio.Task[Any]] = set()
        self._last_reported_mode: str = self.current_mode
        self._initial_commands_sent = False
        self._slash_images: dict[str, Any] = {}
        if self.conversation.mcp_manager is not None:
            register_mcp_prompts_from_manager(self.commands, self.conversation.mcp_manager)

        if self.policy is not None:
            self.policy.interactive = True
        self.conversation.on_tasks = self._on_background_event
        prev_task_change = getattr(self.conversation.tasks, "on_change", None)

        def _combined_task_change(task: Any) -> None:
            if prev_task_change is not None:
                prev_task_change(task)
            self._on_background_event()

        self.conversation.tasks.on_change = _combined_task_change
        subagents = getattr(self.conversation, "subagents", None)
        if subagents is not None:
            subagents.on_approval = self._on_background_approval
            subagents.on_approval_settled = self._withdraw_approval
        teams = getattr(self.conversation, "teams", None)
        if teams is not None:
            teams.teammate_mode = "in-process"
            teams.on_approval = self._on_background_approval
            teams.on_withdraw = self._withdraw_approval
            teams.on_note = self._on_team_note
            teams.on_change = self._on_background_event
            teams.on_update = self._on_background_event
        if self.conversation.registry is not None:
            prev_todos_hook = self.conversation.registry.todos.on_change

            def _combined_todos_changed(items: tuple[TodoItem, ...], mark: int) -> None:
                if prev_todos_hook is not None:
                    prev_todos_hook(items, mark)
                self._on_todos_changed(items, mark)

            self.conversation.registry.todos.on_change = _combined_todos_changed
        self._ensure_loops()

    @contextmanager
    def _runtime_context(self):
        remember_settings_credentials(self.settings)
        with use_sandbox(self.sandbox), use_command_env(*self.command_env):
            yield

    @classmethod
    def create(
        cls,
        *,
        cwd: Path,
        client: Client,
        options: AcpLaunchOptions,
        mcp_servers: Sequence[McpServerParam] | None = None,
        resume_session_id: str | None = None,
    ) -> AcpSession:
        if not cwd.is_absolute():
            raise RequestError.invalid_params({"cwd": str(cwd), "reason": "cwd 必须是绝对路径。"})
        if not cwd.is_dir():
            raise RequestError.invalid_params({"cwd": str(cwd), "reason": f"工作目录不存在：{cwd}"})

        resolved_cwd = cwd.resolve()
        settings, provider_config, config_notices = load_session_settings(
            resolved_cwd,
            explicit_config=options.config,
            provider_name=options.provider,
            trust_project_config=options.trust_project_config,
            home=options.home,
            settings=options.settings,
        )

        if resume_session_id is not None:
            if not settings.persist_sessions:
                raise RequestError.invalid_params(
                    {
                        "sessionId": resume_session_id,
                        "reason": "会话存档已关闭（persist_sessions: false），不能恢复会话。",
                    }
                )
            found_path = find_session(resolved_cwd, resume_session_id, home=options.home)
            if found_path is None:
                raise RequestError.invalid_params(
                    {"sessionId": resume_session_id, "reason": f"未找到会话：{resume_session_id}"}
                )
            target_id: str | None = found_path.stem
        else:
            target_id = None

        plugins = _load_plugins_headless(
            resolved_cwd,
            trust_project=options.trust_project_plugins,
            home=options.home,
        )
        claude = settings.claude_directories
        sandbox = Sandbox(settings.sandbox, enabled=options.sandbox, source=settings.source)
        command_env = (settings.command_env_keep, settings.command_env_drop)

        with use_sandbox(sandbox), use_command_env(*command_env):
            user_perm_path = options.home / ".mosscode" / "permissions.yaml" if options.home is not None else None
            try:
                permissions = PermissionPolicy(
                    resolved_cwd,
                    mode=options.permission_mode,
                    user_path=user_perm_path,
                )
            except PermissionConfigError as exc:
                raise RequestError.invalid_params({"reason": str(exc)}) from None

            client_servers, client_mcp_notices = convert_client_mcp_servers(mcp_servers)
            components = assemble_workspace_extensions(
                resolved_cwd,
                settings,
                home=options.home,
                permissions=permissions,
                ask=True,
                no_hooks=options.no_hooks,
                interactive_hooks=False,
                trust_project_plugins=options.trust_project_plugins,
                trust_project_mcp=options.trust_project_mcp,
                trust_project_hooks=options.trust_project_hooks,
                plugins=plugins,
                extra_mcp_servers=client_servers,
                extra_mcp_notices=client_mcp_notices,
                catch_mcp_config_error=True,
                merge_mcp_notices=True,
                untrusted_plugins_hint=_ACP_UNTRUSTED_PLUGINS_HINT,
                untrusted_mcp_hint=_ACP_UNTRUSTED_MCP_HINT,
                untrusted_hook_hint=_ACP_UNTRUSTED_HOOK_HINT,
            )
            commands = components.commands

            extra_notices = [n for n in (None if claude else claude_notice(resolved_cwd),) if n]
            notices = (
                tuple(config_notices) + components.notices + tuple(extra_notices) + tuple(sandbox.notices(resolved_cwd))
            )
            coordinator, coord_notice = decide_coordinator(settings.coordinator_mode, os.environ, interactive=True)
            if coord_notice:
                notices += (coord_notice,)

            factory = options.provider_factory or create_provider
            try:
                provider = factory(provider_config)
                goal_provider = (
                    factory(refreshed(settings.select(settings.goal_provider), home=options.home))
                    if settings.goal_provider
                    else None
                )
            except ConfigError as exc:
                raise RequestError.auth_required({"reason": str(exc)}) from None

            initial_session = Session.new(resolved_cwd, home=options.home)
            final_id = target_id if target_id is not None else initial_session.id

            acp_settings = (
                settings if settings.teammate_mode == "in-process" else replace(settings, teammate_mode="in-process")
            )
            conversation = build_conversation(
                acp_settings,
                provider_config,
                root=resolved_cwd,
                registry=components.registry,
                skills=components.skills,
                agents=components.agents,
                mcp_manager=components.mcp_manager,
                hooks=components.hooks,
                session=initial_session,
                home=options.home,
                notices=notices,
                auto_memory=False,
                enable_goal_provider=True,
                teams=True,
                coordinator=coordinator,
                provider=provider,
                goal_provider=goal_provider,
                provider_factory=factory,
            )

        return cls(
            session_id=final_id,
            cwd=resolved_cwd,
            client=client,
            settings=settings,
            provider_config=provider_config,
            conversation=conversation,
            commands=commands,
            options=options,
            sandbox=sandbox,
            command_env=command_env,
        )

    @property
    def policy(self) -> PermissionPolicy | None:
        return self.conversation.registry.permissions if self.conversation.registry is not None else None

    @property
    def current_mode(self) -> str:
        policy = self.policy
        return policy.mode if policy is not None and policy.mode in PERMISSION_MODES else "default"

    @property
    def current_thinking(self) -> str | None:
        return self.conversation.thinking

    def mode_state(self):
        return build_mode_state(self.current_mode)

    def config_options(self) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
        return build_config_options(
            self.current_mode,
            self.settings,
            self.provider_config,
            self.current_thinking,
        )

    def _spawn_update(self, update: SessionUpdateItem) -> None:
        if self._closed:
            return

        async def _send():
            try:
                await self.client.session_update(session_id=self.session_id, update=update)
            except Exception as exc:
                print(f"MossCode ACP：发送会话更新通知失败：{redact(str(exc))}", file=sys.stderr)

        task = asyncio.create_task(_send())
        self._update_tasks.add(task)
        task.add_done_callback(self._update_tasks.discard)

    async def _drain_update_tasks(self) -> None:
        while self._update_tasks:
            pending = list(self._update_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _finish_prompt(self, response: PromptResponse) -> PromptResponse:
        await self._drain_update_tasks()
        return response

    def _ensure_not_busy_external(self) -> None:
        if (
            self._active_task is not None
            and not self._active_task.done()
            and asyncio.current_task() is not self._active_task
        ):
            raise RequestError(-32001, "当前会话已有正在运行的请求。", {"reason": "当前会话已有正在运行的请求。"})

    def _available_commands_update(self) -> AvailableCommandsUpdate:
        base = build_available_commands(self.commands)
        present = {item.name for item in base.available_commands}
        commands = list(base.available_commands)
        team_cmd = self.commands.get("team")
        if team_cmd is not None and not team_cmd.hidden and "team" not in present:
            cmd_input = (
                AvailableCommandInput(root=UnstructuredCommandInput(hint=team_cmd.argument_hint))
                if team_cmd.argument_hint
                else None
            )
            desc = summarize_command_description(team_cmd.description)
            team_item = AvailableCommand(name=team_cmd.name, description=desc, input=cmd_input)
            swarm_idx = next((i for i, item in enumerate(commands) if item.name == "swarm"), None)
            if swarm_idx is not None:
                commands.insert(swarm_idx, team_item)
            else:
                commands.append(team_item)
        return AvailableCommandsUpdate(available_commands=commands)

    @property
    def _in_prompt(self) -> bool:
        return self._active_task is not None and not self._active_task.done() and not self._closed

    def _on_background_event(self, *_args: Any) -> None:
        if not self._closed:
            self._bg_wakeup.set()

    def _on_team_note(self, text: str) -> None:
        if self._closed or not text:
            return
        self._team_notes.append(text)
        self._bg_wakeup.set()

    async def _flush_team_notes(self) -> None:
        if not self._team_notes or self._cancelled or self._closed:
            return
        notes, self._team_notes = self._team_notes[:], []
        block = "\n".join(f"> {escape_markdown_inline(note)}" for note in notes)
        await self._send_text(f"\n{block}\n")

    def _on_background_approval(self, request: ApprovalRequest) -> None:
        policy = self.policy
        if self._closed or policy is None:
            if policy is not None and request.id in policy.pending:
                policy.respond(request.id, "deny")
            return
        if self._active_task is not None and not self._active_task.done():
            if self._cancelled:
                if request.id in policy.pending:
                    policy.respond(request.id, "deny")
                return
            self._start_background_approval(request)
            return
        # Idle between prompt RPCs: queue without sending unsolicited JSON-RPC turn frames.
        if all(item.id != request.id for item in self._deferred_approvals):
            self._deferred_approvals.append(request)

    def _flush_deferred_approvals(self) -> None:
        if not self._deferred_approvals or self._cancelled or self._closed:
            return
        policy = self.policy
        queued, self._deferred_approvals = self._deferred_approvals[:], []
        if policy is None:
            return
        for request in queued:
            if request.id in policy.pending:
                self._start_background_approval(request)

    def _start_background_approval(self, request: ApprovalRequest) -> None:
        policy = self.policy
        if policy is None or request.id not in policy.pending or request.id in self._bg_approval_tasks:
            return
        self._pending_approvals.add(request.id)

        async def _run() -> None:
            try:
                with self._runtime_context():
                    await handle_approval_request(
                        self.client,
                        self.session_id,
                        policy,
                        self.mapper,
                        request,
                    )
                    if not self._cancelled and not self._closed and self._in_prompt:
                        await self._sync_mode_if_changed()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if request.id in policy.pending:
                    policy.respond(request.id, "deny")
                print(f"MossCode ACP：后台审批处理失败：{redact(str(exc))}", file=sys.stderr)
            finally:
                self._pending_approvals.discard(request.id)
                self._bg_approval_tasks.pop(request.id, None)
                self._bg_wakeup.set()

        task = asyncio.create_task(_run())
        self._bg_approval_tasks[request.id] = task

    def _withdraw_approval(self, request_id: str) -> None:
        self._deferred_approvals = [item for item in self._deferred_approvals if item.id != request_id]
        self._pending_approvals.discard(request_id)
        task = self._bg_approval_tasks.pop(request_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._bg_wakeup.set()

    def _ensure_loops(self):
        loops = getattr(self.conversation, "loops", None)
        if loops is not None:
            return loops
        attach = getattr(self.conversation, "attach_loops", None)
        if callable(attach):
            return attach(is_busy=self._is_loop_busy, trigger=self._on_loop_trigger)
        return None

    def _is_loop_busy(self) -> bool:
        return bool(
            self._closed
            or self.conversation.busy
            or self._pending_approvals
            or self._pending_questions
            or (self.conversation.goal is not None and self.conversation.goal.active)
            or (self._in_prompt and not self._loop_window_open)
            or (bool(self._due_loops) and not self._loop_window_open)
        )

    async def _on_loop_trigger(self, task: Any, prompt: str) -> None:
        if self._closed:
            return
        runs = int(getattr(getattr(task, "loop", None), "runs", 1))
        # Queue safely when idle between session/prompt RPCs; never emit unsolicited JSON-RPC turn frames.
        self._due_loops = [item for item in self._due_loops if item[0].id != task.id]
        self._due_loops.append((task, prompt, runs))
        self._bg_wakeup.set()

    async def _drain_due_loops(self) -> tuple[RunStopped | None, bool]:
        last_stopped: RunStopped | None = None
        while self._due_loops and not self._cancelled and not self._closed:
            task, loop_prompt, runs = self._due_loops.pop(0)
            if getattr(task, "stopped", False) or task.status == "cancelled":
                continue
            first_line = next((ln.strip() for ln in loop_prompt.splitlines() if ln.strip()), loop_prompt.strip())
            await self._send_text(
                f"\n> ⏰ 定时巡检 `{escape_markdown_inline(task.id)}`（第 {runs} 次）："
                f"{escape_markdown_inline(first_line[:100])}\n"
            )
            if loop_prompt.lstrip().startswith("/"):
                immediate, cmd_text, cmd_label, cmd_fork = await self._handle_slash_command(loop_prompt)
                if self._cancelled:
                    return last_stopped, True
                if immediate is not None or not cmd_text:
                    continue
                last_stopped, was_cancelled = await self._consume_reply_stream(
                    self.conversation.reply(cmd_text, command=cmd_label, fork=cmd_fork)
                )
                if was_cancelled:
                    return last_stopped, True
                continue
            try:
                from ..loop_schedule import LOOP_SYNTHETIC, format_loop_prompt

                rendered = format_loop_prompt(task)
                synthetic = LOOP_SYNTHETIC
            except ImportError:
                rendered = f"[定时任务 {task.id} · 第 {runs} 次]\n{loop_prompt}"
                synthetic = "loop"
            last_stopped, was_cancelled = await self._consume_reply_stream(
                self.conversation.reply(rendered, synthetic=synthetic)
            )
            if was_cancelled:
                return last_stopped, True
        return last_stopped, False

    def _has_active_background_turn_work(self) -> bool:
        if self._bg_approval_tasks:
            return True
        if any(
            task.command is None and getattr(task, "loop", None) is None for task in self.conversation.tasks.running()
        ):
            return True
        teams = getattr(self.conversation, "teams", None)
        if teams is not None and teams.current is not None:
            if any(
                runner.status == "running" or getattr(getattr(runner, "wakeup", None), "is_set", lambda: False)()
                for runner in teams.runners.values()
            ):
                return True
            if getattr(teams, "_revives", None):
                return True
            if getattr(teams, "_lead_wake", None) is not None and teams._lead_wake.is_set():
                return True
        return False

    def _on_todos_changed(self, items: tuple[TodoItem, ...], _version: int) -> None:
        self._spawn_update(todos_to_plan_update(items))

    def _on_mcp_update(self, status: Any) -> None:
        if self._closed:
            return
        if status.state == "ready":
            skipped: list[str] = []
            added = register_mcp_prompts(self.commands, status, self.conversation.mcp_manager, skipped=skipped)
            for note in skipped:
                if note not in self._startup_notices:
                    self._startup_notices.append(note)
                    print(f"MossCode ACP: {redact(note)}", file=sys.stderr)
            if added and self._initial_commands_sent:
                self._spawn_update(self._available_commands_update())
        else:
            note = status.notice()
            if note not in self._startup_notices:
                self._startup_notices.append(note)
                print(f"MossCode ACP: {redact(note)}", file=sys.stderr)

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            with self._runtime_context():
                notes = await self.conversation.start(wait_mcp=False, on_mcp=self._on_mcp_update)
                if self.conversation.mcp_manager is not None and hasattr(self.conversation.mcp_manager, "wait"):
                    await self.conversation.mcp_manager.wait()
            skipped: list[str] = []
            added = register_mcp_prompts_from_manager(self.commands, self.conversation.mcp_manager, skipped=skipped)
            self._started = True
            for note in (*notes, *skipped):
                if note not in self._startup_notices:
                    self._startup_notices.append(note)
                    print(f"MossCode ACP: {redact(note)}", file=sys.stderr)
            if added and self._initial_commands_sent:
                self._spawn_update(self._available_commands_update())

    async def send_initial_updates(self) -> None:
        """Send available slash commands after session creation and start background MCP discovery."""
        if self.conversation.mcp_manager is not None:
            register_mcp_prompts_from_manager(
                self.commands, self.conversation.mcp_manager, skipped=self._startup_notices
            )
        self._initial_commands_sent = True
        await self.client.session_update(
            session_id=self.session_id,
            update=self._available_commands_update(),
        )
        manager = self.conversation.mcp_manager
        if (
            manager is not None
            and not self._closed
            and not self.conversation._mcp_background
            and not getattr(manager, "_tasks", None)
            and callable(getattr(manager, "begin", None))
        ):
            self.conversation._mcp_background = True
            for note in getattr(manager, "notices", ()) or ():
                if note not in self._startup_notices:
                    self._startup_notices.append(note)
            with self._runtime_context():
                manager.begin(self.conversation.registry, self._on_mcp_update)

    async def load_and_replay(self, *, replay: bool = True) -> LoadSessionResponse:
        """Restore session transcript and optionally replay history notifications to the client."""
        resumed: Resumed | None = None
        extra_updates: list[SessionUpdateItem] = []
        with self._runtime_context():
            try:
                async with aclosing(self.conversation.resume(self.session_id)) as events:
                    async for event in events:
                        if isinstance(event, Resumed):
                            resumed = event
                        else:
                            extra_updates.extend(self.mapper.map_event(event))
            except (SessionLocked, ProviderError, OSError) as exc:
                raise RequestError.invalid_params({"sessionId": self.session_id, "reason": str(exc)}) from None

            await self.ensure_started()
            self._startup_notices.clear()
            self._last_reported_mode = self.current_mode

            if replay:
                for update in replay_history_updates(self.conversation.history, self.cwd):
                    await self.client.session_update(session_id=self.session_id, update=update)

                for update in extra_updates:
                    await self.client.session_update(session_id=self.session_id, update=update)

                if resumed is not None:
                    if resumed.notices:
                        notice_block = "\n".join(f"> {n}" for n in resumed.notices)
                        await self.client.session_update(
                            session_id=self.session_id,
                            update=text_chunk(notice_block + "\n"),
                        )
                    if resumed.restored.unanswered:
                        unanswered = "\n".join(f"- {item}" for item in resumed.restored.unanswered)
                        await self.client.session_update(
                            session_id=self.session_id,
                            update=text_chunk(f"> 上次未获得回复的输入：\n{unanswered}\n"),
                        )

                if self.conversation.todos:
                    await self.client.session_update(
                        session_id=self.session_id,
                        update=todos_to_plan_update(self.conversation.todos),
                    )

                await self.send_initial_updates()

        return LoadSessionResponse(
            modes=self.mode_state(),
            config_options=self.config_options(),
        )

    def new_session_response(self) -> NewSessionResponse:
        return NewSessionResponse(
            session_id=self.session_id,
            modes=self.mode_state(),
            config_options=self.config_options(),
        )

    async def set_mode(self, mode_id: str) -> SetSessionModeResponse:
        self._ensure_not_busy_external()
        with self._runtime_context():
            try:
                normalized = normalize_mode(mode_id)
                self.conversation.set_permission_mode(normalized)
            except PermissionConfigError as exc:
                raise RequestError.invalid_params({"modeId": mode_id, "reason": str(exc)}) from None
            except ProviderError as exc:
                raise RequestError(-32001, str(exc), {"modeId": mode_id, "reason": str(exc)}) from None

            self._last_reported_mode = self.current_mode
            await self.client.session_update(
                session_id=self.session_id,
                update=CurrentModeUpdate(current_mode_id=self.current_mode),
            )
            await self.client.session_update(
                session_id=self.session_id,
                update=ConfigOptionUpdate(config_options=self.config_options()),
            )
            return SetSessionModeResponse()

    async def switch_model(self, name: str, model: str | None = None) -> None:
        """Switch the active provider, optionally cloning its configured base with an overridden model."""
        current = self.settings.select(name)
        configured = next((item for item in self._configured_providers if item.name == current.name), current)
        selected = refreshed(configured, home=self.options.home)
        if model is not None:
            selected = with_model(selected, model, home=self.options.home)
        selected.credential()
        if (
            selected.name != self.provider_config.name
            or selected.model != self.provider_config.model
            or selected.follow
        ):
            factory = self.options.provider_factory or create_provider
            await self.conversation.set_provider(
                factory(selected),
                selected.budget(),
                info=(selected.name, selected.protocol, selected.model),
            )
        if model is not None or selected.follow or current is not configured:
            providers = tuple(selected if item.name == selected.name else item for item in self.settings.providers)
            self.settings = replace(self.settings, providers=providers)
            self.conversation.provider_configs = providers
            if model is None and selected.follow:
                self._configured_providers = tuple(
                    selected if item.name == selected.name else item for item in self._configured_providers
                )
        self.provider_config = selected

    async def set_config_option(self, config_id: str, value: str | bool) -> SetSessionConfigOptionResponse:
        if not isinstance(value, str):
            raise RequestError.invalid_params({"configId": config_id, "reason": "仅支持字符串选择项。"})
        self._ensure_not_busy_external()

        with self._runtime_context():
            if config_id == "mode":
                try:
                    normalized = normalize_mode(value)
                    self.conversation.set_permission_mode(normalized)
                except PermissionConfigError as exc:
                    raise RequestError.invalid_params(
                        {"configId": config_id, "value": value, "reason": str(exc)}
                    ) from None
                except ProviderError as exc:
                    raise RequestError(
                        -32001, str(exc), {"configId": config_id, "value": value, "reason": str(exc)}
                    ) from None
                self._last_reported_mode = self.current_mode
                await self.client.session_update(
                    session_id=self.session_id,
                    update=CurrentModeUpdate(current_mode_id=self.current_mode),
                )
            elif config_id == "model":
                target = resolve_model_target(
                    value, self.settings.providers, self.provider_config, home=self.options.home
                )
                try:
                    if target is not None:
                        await self.switch_model(target[0], target[1])
                    else:
                        await self.switch_model(value)
                except ConfigError as exc:
                    raise RequestError.invalid_params(
                        {"configId": config_id, "value": value, "reason": str(exc)}
                    ) from None
                except ProviderError as exc:
                    raise RequestError(
                        -32001, str(exc), {"configId": config_id, "value": value, "reason": str(exc)}
                    ) from None
            elif config_id == "thought_level":
                level: str | None = None if value == DEFAULT_THINKING else value
                if level is not None:
                    if level not in LEVELS:
                        raise RequestError.invalid_params(
                            {"configId": config_id, "value": value, "reason": f"不支持的思考档位：{value}"}
                        )
                    if level not in supported_thinking_levels(self.provider_config):
                        raise RequestError.invalid_params(
                            {"configId": config_id, "value": value, "reason": f"当前模型不支持思考档位：{value}"}
                        )
                try:
                    self.conversation.set_thinking(level)
                except ProviderError as exc:
                    raise RequestError(
                        -32001, str(exc), {"configId": config_id, "value": value, "reason": str(exc)}
                    ) from None
            else:
                raise RequestError.invalid_params({"configId": config_id, "reason": f"未知的配置项：{config_id}"})

            options = self.config_options()
            await self.client.session_update(
                session_id=self.session_id,
                update=ConfigOptionUpdate(config_options=options),
            )
            return SetSessionConfigOptionResponse(config_options=options)

    def _refused_resource_reason(self, lexical: Path, resolved: Path | None = None) -> str | None:
        if resolved is None:
            try:
                resolved = lexical.resolve()
            except OSError:
                return "路径解析失败"
        candidates = (lexical, resolved) if lexical != resolved else (lexical,)
        raw_home = (self.options.home or Path.home()).expanduser()
        home_lex = Path(os.path.abspath(raw_home))
        try:
            home_res = home_lex.resolve()
        except OSError:
            home_res = home_lex

        for secret in HOME_SECRETS:
            secret_lex = Path(os.path.abspath(home_lex / secret))
            try:
                secret_res = (home_res / secret).resolve()
            except OSError:
                secret_res = secret_lex
            for cand in candidates:
                if inside(secret_lex, cand) or inside(secret_res, cand):
                    return f"受保护的凭据路径 {secret_lex.name} 未附加"

        if self.settings.source is not None:
            src_lex = Path(os.path.abspath(self.settings.source))
            try:
                src_res = src_lex.resolve()
            except OSError:
                src_res = src_lex
            for cand in candidates:
                if inside(src_lex, cand) or inside(src_res, cand):
                    return "受保护的配置文件未附加"

        registry = self.conversation.registry
        policy = self.policy
        if registry is not None:
            read_tool = registry.get("read_file")
            if read_tool is not None:
                for cand in candidates:
                    try:
                        params = read_tool.validate_arguments({"path": str(cand)})
                        refusal = registry.refusal(read_tool, params)
                    except Exception:
                        return "路径校验未通过"
                    if refusal is not None:
                        return refusal.reason
                    if policy is not None:
                        outside = not inside(self.cwd, cand)
                        target = cand.as_posix() if outside else cand.relative_to(self.cwd).as_posix()
                        decision = policy.decide(Operation("read_file", target, outside=outside), read_only=True)
                        if decision.action == "deny":
                            return decision.reason
        return None

    def _unpack_prompt_blocks_parts(
        self,
        blocks: Sequence[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
    ) -> tuple[str, list[str], str, dict[str, ProcessedImage]]:
        user_parts: list[str] = []
        context_parts: list[str] = []
        ordered_parts: list[str] = []
        images: dict[str, ProcessedImage] = {}
        img_counter = 0

        def _add_context(item: str) -> None:
            context_parts.append(item)
            ordered_parts.append(item)

        for block in blocks:
            if isinstance(block, TextContentBlock):
                if block.text:
                    user_parts.append(block.text)
                    ordered_parts.append(block.text)
            elif isinstance(block, ResourceContentBlock):
                if _is_file_uri(block.uri):
                    paths = _uri_paths(block.uri)
                    if paths is None:
                        _add_context(f"[已忽略受限引用 {block.name or 'file'}：不合法的 file URI]")
                        continue
                    lexical, resolved = paths
                    if reason := self._refused_resource_reason(lexical, resolved):
                        _add_context(f"[已忽略受限引用 {block.name or resolved.name}：{reason}]")
                        continue
                    if inside(self.cwd, lexical) and inside(self.cwd, resolved):
                        rel = resolved.relative_to(self.cwd).as_posix()
                        _add_context(f"@{quoted(rel)}")
                    else:
                        _add_context(f"[外部文件引用：{resolved}，如需查看请调用 read_file]")
                else:
                    _add_context(f"[{block.name or block.uri}]({block.uri})")
            elif isinstance(block, EmbeddedResourceContentBlock):
                res = block.resource
                if isinstance(res, TextResourceContents):
                    if _is_file_uri(res.uri):
                        paths = _uri_paths(res.uri)
                        if paths is None:
                            _add_context("[已忽略受保护或被拒绝的上下文：不合法的 file URI]")
                            continue
                        lexical, resolved = paths
                        label = display_path(self.cwd, resolved)
                        if reason := self._refused_resource_reason(lexical, resolved):
                            _add_context(f"[已忽略受保护或被拒绝的上下文 {label}：{reason}]")
                            continue
                    else:
                        label = res.uri
                    _add_context(f'\n<context path="{label}">\n{res.text}\n</context>\n')
                elif isinstance(res, BlobResourceContents):
                    if _is_file_uri(res.uri):
                        paths = _uri_paths(res.uri)
                        if paths is None:
                            _add_context("[已忽略受保护或被拒绝的资源：不合法的 file URI]")
                            continue
                        lexical, resolved = paths
                        label = display_path(self.cwd, resolved)
                        if reason := self._refused_resource_reason(lexical, resolved):
                            _add_context(f"[已忽略受保护或被拒绝的资源 {label}：{reason}]")
                            continue
                    mime = res.mime_type or ""
                    if mime.startswith("image/"):
                        decoded = _decode_image_block(res.blob, mime)
                        if decoded is not None:
                            img_counter += 1
                            mark = image_mark(img_counter)
                            images[mark] = decoded
                            _add_context(mark)
                        else:
                            _add_context(f"[无法解析的图片资源: {res.uri}]")
                    else:
                        _add_context(f"[二进制资源: {res.uri}]")
            elif isinstance(block, ImageContentBlock):
                if block.uri and _is_file_uri(block.uri):
                    paths = _uri_paths(block.uri)
                    if paths is None:
                        _add_context("[已忽略受保护或被拒绝的图片：不合法的 file URI]")
                        continue
                    lexical, resolved = paths
                    label = display_path(self.cwd, resolved)
                    if reason := self._refused_resource_reason(lexical, resolved):
                        _add_context(f"[已忽略受保护或被拒绝的图片 {label}：{reason}]")
                        continue
                decoded = _decode_image_block(block.data, block.mime_type)
                if decoded is not None:
                    img_counter += 1
                    mark = image_mark(img_counter)
                    images[mark] = decoded
                    _add_context(mark)
                else:
                    _add_context("[无法解析的图片内容块]")
            elif isinstance(block, AudioContentBlock):
                _add_context("[当前模型会话暂不支持音频内容块]")

        user_text = "\n".join(part for part in user_parts if part)
        combined = "\n".join(part for part in ordered_parts if part).strip()
        if not combined and images:
            combined = " ".join(images.keys())
        return user_text, context_parts, combined, images

    def _unpack_prompt_blocks(
        self,
        blocks: Sequence[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
    ) -> tuple[str, dict[str, ProcessedImage]]:
        _, _, combined, images = self._unpack_prompt_blocks_parts(blocks)
        return combined, images

    async def _send_text(self, text: str) -> None:
        await self.client.session_update(session_id=self.session_id, update=text_chunk(text))

    async def _handle_slash_command(
        self,
        text: str,
        context_parts: Sequence[str] = (),
    ) -> tuple[PromptResponse | None, str, str | None, ForkRequest | None]:
        """Handle slash commands isolated from attached context blocks (`!` inputs stay plain messages)."""

        def _with_context(base: str) -> str:
            extra = "\n".join(p.strip() for p in context_parts if p and p.strip())
            return f"{base}\n\n{extra}" if extra else base

        parsed = parse_input(text, self.commands)
        if parsed.kind == "empty":
            extra = "\n".join(p.strip() for p in context_parts if p and p.strip())
            if extra:
                return None, extra, None, None
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if parsed.kind in {"message", "shell"}:
            msg_text = parsed.text if parsed.kind == "message" else text
            return None, _with_context(msg_text), None, None
        if parsed.kind == "unknown":
            await self._send_text(escape_markdown_inline(unknown_note(parsed, self.commands)))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        cmd = parsed.command
        assert cmd is not None
        args = parsed.argument.strip()

        if cmd.name in (EXCLUDED_ACP_COMMANDS - {"team"}):
            if cmd.name == "sandbox":
                if not args:
                    raw_sb = "\n".join(self.sandbox.report(self.cwd))
                    await self._send_text(format_terminal_report_markdown(raw_sb))
                else:
                    await self._send_text(
                        "ACP 会话中请在启动 `moss acp` 时使用 `--sandbox` 或 `--no-sandbox` 控制命令沙箱。"
                    )
                return PromptResponse(stop_reason="end_turn"), "", None, None
            await self._send_text(format_help_markdown(self.commands, cmd.name, self.conversation.skills))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if problem := argument_problem(cmd, parsed.argument):
            await self._send_text(escape_markdown_inline(problem))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if cmd.kind == "prompt":
            if is_mcp_prompt_command(cmd):
                try:
                    expanded, cmd_label, mcp_images = await expand_mcp_prompt(self.conversation, cmd, parsed.argument)
                except (PromptArgumentError, ProviderError) as exc:
                    await self._send_text(escape_markdown_inline(str(exc)))
                    return PromptResponse(stop_reason="end_turn"), "", None, None
                self._slash_images = mcp_images
                return None, _with_context(expanded), cmd_label, None
            return None, _with_context(cmd.expand(parsed.argument)), typed(cmd, parsed.argument), None

        if cmd.kind == "skill":
            try:
                skill, rendered, notices = self.conversation.prepare_skill(cmd.skill, parsed.argument)
            except ProviderError as exc:
                await self._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if notices:
                await self._send_text(format_notices_markdown(notices))
            cmd_label = typed(cmd, parsed.argument)
            combined_rendered = _with_context(rendered)
            if skill.mode == "fork":
                return None, combined_rendered, cmd_label, ForkRequest(skill, combined_rendered)
            return None, combined_rendered, cmd_label, None

        immediate, local_prompt, local_label, local_fork = await self._handle_local_command(cmd.name, args)
        if immediate is None and local_prompt:
            local_prompt = _with_context(local_prompt)
            if local_fork is not None:
                local_fork = ForkRequest(local_fork.skill, local_prompt)
        return immediate, local_prompt, local_label, local_fork

    async def _handle_local_command(
        self, name: str, args: str
    ) -> tuple[PromptResponse | None, str, str | None, ForkRequest | None]:
        if name == "help":
            target = args.strip().removeprefix("/").lower()
            if target in {"team", "teams"}:
                await self._send_text(
                    "### `/team [子命令]`\n\n"
                    "- **说明**：查看、停止、删除本项目的进程内 Agent 团队及黑板记录\n"
                    "- **类型**：本地命令（直接返回结果，不调用模型）\n"
                    "- **别名**：`/teams`\n"
                    "- **用法**：`/team`、`/team <名称>`、`/team stop <成员>`、`/team delete <名称>`"
                )
            else:
                help_md = format_help_markdown(self.commands, args, self.conversation.skills)
                if not target and "`/team" not in help_md:
                    help_md += "\n- `/team [子命令]` — 查看、停止、删除 Agent 团队（别名 `/teams`）"
                await self._send_text(help_md)
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "clear":
            self._team_notes.clear()
            self._due_loops.clear()
            notices = await self.conversation.clear(keep_session=True)
            await self.client.session_update(session_id=self.session_id, update=todos_to_plan_update(()))
            if notices:
                await self._send_text(format_notices_markdown(notices))
            await self._send_text(CLEARED)
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "compact":
            async with aclosing(self.conversation.compact(args)) as events:
                async for event in events:
                    for u in self.mapper.map_event(event):
                        await self.client.session_update(session_id=self.session_id, update=u)
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "session":
            sessions = await asyncio.to_thread(self.conversation.sessions)
            await self._send_text(format_session_markdown(self.conversation, sessions))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "rename":
            session = self.conversation.session
            if session is None or not self.conversation.persist or session.transcript_path is None:
                await self._send_text("会话存档已关闭（`persist_sessions: false`），不能重命名会话。")
                return PromptResponse(stop_reason="end_turn"), "", None, None
            clean_title = _clean_title(args)
            if not clean_title:
                await self._send_text("`/rename` 需要参数。用法：`/rename <标题>`")
                return PromptResponse(stop_reason="end_turn"), "", None, None
            try:
                await asyncio.to_thread(
                    write_title,
                    session,
                    clean_title,
                    transcript=getattr(self.conversation, "_transcript", None),
                    cwd=self.cwd,
                )
            except (OSError, ValueError) as exc:
                await self._send_text(f"无法重命名会话：{escape_markdown_inline(str(exc))}")
                return PromptResponse(stop_reason="end_turn"), "", None, None
            await self._send_text(f"已将当前会话重命名为：**{escape_markdown_inline(clean_title)}**。")
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "memory":
            await self._send_text(format_terminal_report_markdown(memory_report(self.conversation, args)))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "mcp":
            await self._send_text(format_terminal_report_markdown(mcp_report(self.conversation, args)))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "skills":
            if args == "reload" and self.conversation.registry is not None:
                old = self.conversation.skills
                catalog = await asyncio.to_thread(old.reload)
                problems = self.conversation.registry.set_skills(catalog)
                notices = self.conversation.use_skills(catalog)
                self.commands, skipped = BUILTIN.with_skills(catalog)
                register_mcp_prompts_from_manager(self.commands, self.conversation.mcp_manager)
                await self.send_initial_updates()
                lines = [f"已重新扫描 Skill：共 **`{len(catalog.skills)}`** 个。"]
                all_issues = (*catalog.problems, *problems, *notices, *skipped)
                if all_issues:
                    lines.append("")
                    lines.extend(f"- ⚠️ {escape_markdown_inline(p)}" for p in all_issues)
                await self._send_text("\n".join(lines))
            else:
                await self._send_text(
                    format_skills_markdown(self.conversation.skills, args, self.conversation.active_skills)
                )
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "agents":
            if (
                args == "reload"
                and self.conversation.registry is not None
                and getattr(self.conversation, "agents_enabled", False)
            ):
                catalog = await asyncio.to_thread(self.conversation.agents.reload)
                self.conversation.use_agents(catalog)
                lines = [f"已重新加载子 Agent 定义：共 **`{len(catalog.agents)}`** 个，下一次请求起生效。"]
                if catalog.problems:
                    lines.append("")
                    lines.extend(f"- ⚠️ {escape_markdown_inline(p)}" for p in catalog.problems)
                await self._send_text("\n".join(lines))
            else:
                await self._send_text(format_agents_markdown(self.conversation, args))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "hooks":
            engine = self.conversation.hooks
            if args == "reload" and engine is not None and engine.enabled:
                added, removed = await asyncio.to_thread(engine.reload)
                summary = (
                    f"已重新加载 Hook：当前已启用 **`{len(engine.hooks)}`** 条"
                    + (f"，新增 {'、'.join(f'`{a}`' for a in added)}" if added else "")
                    + (f"，移除 {'、'.join(f'`{r}`' for r in removed)}" if removed else "")
                    + "。"
                )
                lines = [summary]
                if engine.problems:
                    lines.append("")
                    lines.extend(f"- ⚠️ {escape_markdown_inline(p)}" for p in engine.problems)
                await self._send_text("\n".join(lines))
            else:
                await self._send_text(format_terminal_report_markdown(hooks_report(engine, args)))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "plugins":
            from ..plugin_command import report as plugins_report

            text = await asyncio.to_thread(plugins_report, self.options.home, args, self.cwd)
            await self._send_text(format_terminal_report_markdown(text))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "tasks":
            action, _, rest = args.partition(" ")
            if action == "stop":
                task_id = rest.strip()
                if not task_id:
                    await self._send_text("用法：`/tasks stop <id>`")
                elif await self.conversation.tasks.stop(task_id):
                    self._due_loops = [item for item in self._due_loops if item[0].id != task_id]
                    await self._send_text(f"已停止任务 `{escape_markdown_inline(task_id)}`。")
                else:
                    await self._send_text(f"任务 `{escape_markdown_inline(task_id)}` 不存在或已结束。")
            else:
                await self._send_text(format_terminal_report_markdown(tasks_report(self.conversation, args)))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "todo":
            await self._send_text(format_todo_markdown(self.conversation))
            if self.conversation.todos:
                await self.client.session_update(
                    session_id=self.session_id,
                    update=todos_to_plan_update(self.conversation.todos),
                )
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "model":
            if not args:
                await self._send_text(format_model_markdown(self.provider_config, self.settings))
            else:
                try:
                    await self.set_config_option("model", args)
                    await self._send_text(
                        f"已切换到 **`{self.provider_config.name}`**（`{self.provider_config.model}`）。"
                    )
                except RequestError as exc:
                    await self._send_text(escape_markdown_inline(str((exc.data or {}).get("reason", exc))))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "thinking":
            value = args.lower()
            if not value or value == "status":
                await self._send_text(format_thinking_markdown(self.provider_config, self.current_thinking))
            else:
                try:
                    await self.set_config_option("thought_level", value)
                    detail = escape_markdown_inline(thinking_detail(self.provider_config, self.current_thinking))
                    await self._send_text(f"思考强度已设为 **`{value}`**（{detail}）。")
                except RequestError as exc:
                    await self._send_text(escape_markdown_inline(str((exc.data or {}).get("reason", exc))))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "permissions":
            if args in PERMISSION_MODES:
                await self.set_mode(args)
                await self._send_text(
                    f"权限档位已切换为：**{MODE_LABELS[self.current_mode]}**（`{self.current_mode}`）。"
                )
            else:
                await self._send_text(format_permissions_markdown(self.current_mode, invalid_arg=args))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "plan":
            await self.set_mode("plan")
            if args:
                return None, args, None, None
            await self._send_text(PLAN_ENTERED)
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "do":
            policy = self.policy
            if policy is None or policy.mode != "plan":
                await self._send_text("当前不在计划模式。" + ("任务未发送。" if args else ""))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            await self.set_mode(policy.resume_mode)
            if args:
                return None, args, None, None
            await self._send_text(
                f"已退出计划模式，回到 **{MODE_LABELS[self.current_mode]}**（`{self.current_mode}`）。"
            )
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "status":
            await self._send_text(
                format_status_markdown(
                    provider_config=self.provider_config,
                    current_mode=self.current_mode,
                    current_thinking=self.current_thinking,
                    sandbox_badge=self.sandbox.badge(),
                    cwd=self.cwd,
                    session_id=self.session_id,
                    context_estimate=self.conversation.context_estimate(),
                    effective_budget=self.conversation.budget.effective,
                    totals=self.conversation.total_usage,
                )
            )
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "goal":
            if not args or args == "status":
                await self._send_text(format_goal_markdown(self.conversation))
                goal = self.conversation.goal
                if goal is not None and self.conversation.resume_goal():
                    return None, f"继续完成目标：{goal.condition}", "/goal", None
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if args.lower() in {"clear", "stop", "off", "cancel", "清除"}:
                cleared = self.conversation.clear_goal()
                await self._send_text("已清除目标。" if cleared is not None else "当前没有目标。")
                return PromptResponse(stop_reason="end_turn"), "", None, None
            try:
                goal = self.conversation.set_goal(args)
                await self._send_text(
                    f"已设置自主目标：{escape_markdown_inline(goal.condition)}"
                    f"（每轮结束后自动检查是否达成，最多 `{goal.budget}` 轮）\n\n"
                )
                return None, args, f"/goal {args}", None
            except (ValueError, ProviderError) as exc:
                await self._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "loop":
            loops = self._ensure_loops()
            if loops is None:
                await self._send_text(escape_markdown_inline(LOOP_HEADLESS_NOTICE))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            try:
                from ..loop_schedule import parse_loop_args
            except ImportError:
                await self._send_text(escape_markdown_inline(LOOP_HEADLESS_NOTICE))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            try:
                parsed = parse_loop_args(args)
            except ValueError as exc:
                await self._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if parsed.action == "list":
                await self._send_text(format_terminal_report_markdown(loop_report(self.conversation)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if parsed.action == "stop":
                res = loops.stop(parsed.target)
                stopped_tasks = await res if inspect.isawaitable(res) else res
                stopped_ids = {t.id for t in stopped_tasks}
                if stopped_ids:
                    self._due_loops = [item for item in self._due_loops if item[0].id not in stopped_ids]
                if parsed.target.lower() == "all":
                    if stopped_tasks:
                        ids = "、".join(f"`{escape_markdown_inline(t.id)}`" for t in stopped_tasks)
                        await self._send_text(f"已停止 `{len(stopped_tasks)}` 个定时巡检（{ids}）。")
                    else:
                        await self._send_text("当前没有运行中的定时巡检。")
                elif stopped_tasks:
                    target_task = stopped_tasks[0]
                    await self._send_text(f"已停止定时巡检 `{escape_markdown_inline(target_task.id)}`。")
                else:
                    existing = self.conversation.tasks.get(parsed.target)
                    if existing is None or getattr(existing, "loop", None) is None:
                        await self._send_text(
                            f"没有定时巡检任务 `{escape_markdown_inline(parsed.target)}`。输入 `/loop list` 查看。"
                        )
                    else:
                        state_text = STATUS_LABELS.get(existing.status, existing.status)
                        await self._send_text(
                            f"定时巡检 `{escape_markdown_inline(parsed.target)}` 已结束（{state_text}），无需停止。"
                        )
                return PromptResponse(stop_reason="end_turn"), "", None, None
            try:
                res = loops.schedule(parsed.interval, parsed.interval_text, parsed.prompt, max_runs=parsed.max_runs)
                task = await res if inspect.isawaitable(res) else res
            except (ValueError, ProviderError) as exc:
                await self._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            first_line = next((ln.strip() for ln in parsed.prompt.splitlines() if ln.strip()), parsed.prompt.strip())
            await self._send_text(
                f"已启动定时巡检 `{escape_markdown_inline(task.id)}`（每 {escape_markdown_inline(parsed.interval_text)}，"
                f"最多 `{parsed.max_runs}` 次）：{escape_markdown_inline(first_line[:80])}；"
                f"可用 `/tasks` 或 `/loop stop {escape_markdown_inline(task.id)}` 停止。"
            )
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "swarm":
            lower = args.lower()
            if not args or lower == "status":
                report = await asyncio.to_thread(swarm_report, self.conversation)
                await self._send_text(format_terminal_report_markdown(report))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if lower == "on":
                try:
                    changed = self.conversation.set_coordinator_mode(True)
                except ProviderError as exc:
                    await self._send_text(escape_markdown_inline(str(exc)))
                    return PromptResponse(stop_reason="end_turn"), "", None, None
                msg = f"{SWARM_ON_NOTE}。" if changed else "Swarm 协调模式已经处于开启状态（可用 `/swarm off` 关闭）。"
                await self._send_text(escape_markdown_inline(msg))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if lower == "off":
                try:
                    changed = self.conversation.set_coordinator_mode(False)
                except ProviderError as exc:
                    await self._send_text(escape_markdown_inline(str(exc)))
                    return PromptResponse(stop_reason="end_turn"), "", None, None
                msg = SWARM_OFF_NOTE if changed else "Swarm 协调模式当前未开启。"
                await self._send_text(escape_markdown_inline(msg))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            try:
                changed = self.conversation.set_coordinator_mode(True)
            except ProviderError as exc:
                await self._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            if changed:
                await self._send_text(f"{escape_markdown_inline(SWARM_ON_NOTE)}。\n\n")
            return None, args, f"/swarm {args}", None

        if name == "team":
            from ..teams import TeamError

            runtime = getattr(self.conversation, "teams", None)
            if runtime is None:
                await self._send_text("当前会话不支持团队。")
                return PromptResponse(stop_reason="end_turn"), "", None, None
            words = args.split()
            try:
                if not words:
                    report = await asyncio.to_thread(team_report, self.conversation)
                    await self._send_text(format_terminal_report_markdown(report))
                elif words[0] == "stop" and len(words) == 2:
                    result_text = await runtime.stop_member(words[1])
                    await self._send_text(escape_markdown_inline(result_text))
                elif words[0] == "delete" and len(words) == 2:
                    team_name = words[1]
                    await asyncio.to_thread(runtime.detail, team_name)
                    resp = await self.client.request_permission(
                        session_id=self.session_id,
                        tool_call=ToolCallUpdate(
                            tool_call_id=f"team-delete-{team_name}",
                            name="team_delete",
                            title=f"删除团队 {team_name}（停止成员并清理已合并的干净工作树与团队目录）",
                            kind="other",
                            status="pending",
                            raw_input={"team": team_name},
                        ),
                        options=[
                            PermissionOption(option_id="delete", name="确认删除团队", kind="allow_once"),
                            PermissionOption(option_id="cancel", name="取消", kind="reject_once"),
                        ],
                    )
                    if isinstance(resp.outcome, AllowedOutcome) and resp.outcome.option_id == "delete":
                        deleted_text, _ = await runtime.delete(team_name, force=True)
                        await self._send_text(escape_markdown_inline(deleted_text))
                    else:
                        await self._send_text("已取消，团队保留。")
                elif len(words) == 1 and words[0] not in {"stop", "delete"}:
                    report = await asyncio.to_thread(team_report, self.conversation, words[0])
                    await self._send_text(format_terminal_report_markdown(report))
                else:
                    await self._send_text(escape_markdown_inline(TEAM_USAGE))
            except TeamError as exc:
                await self._send_text(f"⚠️ {escape_markdown_inline(exc.message)}")
            return PromptResponse(stop_reason="end_turn"), "", None, None

        await self._send_text(f"暂不支持在 ACP 中运行命令 `/{name}`。")
        return PromptResponse(stop_reason="end_turn"), "", None, None

    async def _sync_mode_if_changed(self) -> None:
        if self.current_mode != self._last_reported_mode:
            self._last_reported_mode = self.current_mode
            await self.client.session_update(
                session_id=self.session_id,
                update=CurrentModeUpdate(current_mode_id=self.current_mode),
            )
            await self.client.session_update(
                session_id=self.session_id,
                update=ConfigOptionUpdate(config_options=self.config_options()),
            )

    async def _consume_reply_stream(self, reply_stream: Any) -> tuple[RunStopped | None, bool]:
        stopped: RunStopped | None = None
        policy = self.policy
        async with aclosing(reply_stream) as stream:
            async for event in stream:
                if self._cancelled:
                    return stopped, True

                if isinstance(event, ApprovalRequest):
                    if policy is None:
                        continue
                    self._pending_approvals.add(event.id)
                    try:
                        decision = await handle_approval_request(
                            self.client,
                            self.session_id,
                            policy,
                            self.mapper,
                            event,
                        )
                    finally:
                        self._pending_approvals.discard(event.id)
                    if decision != "deny" and not self._cancelled and not event.operation.network and event.call_id:
                        await self.client.session_update(
                            session_id=self.session_id,
                            update=ToolCallProgress(tool_call_id=event.call_id, status="in_progress"),
                        )
                    await self._sync_mode_if_changed()
                    continue

                if isinstance(event, QuestionRequest):
                    if policy is None:
                        continue
                    self._pending_questions[event.id] = len(event.questions)
                    try:
                        await handle_question_request(
                            self.client,
                            self.session_id,
                            policy,
                            event,
                        )
                    finally:
                        self._pending_questions.pop(event.id, None)
                    continue

                if isinstance(event, RunStopped):
                    stopped = event

                for update in self.mapper.map_event(event):
                    await self.client.session_update(session_id=self.session_id, update=update)

                if isinstance(event, ToolProgress) and event.phase == "finished":
                    await self._sync_mode_if_changed()

        cancelled = self._cancelled or (stopped is not None and stopped.reason == "cancelled")
        return stopped, cancelled

    async def prompt(
        self,
        blocks: Sequence[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
    ) -> PromptResponse:
        """Process a user prompt turn and stream session/update notifications."""
        if self._active_task is not None and not self._active_task.done():
            raise RequestError(-32001, "当前会话已有正在运行的请求。", {"reason": "当前会话已有正在运行的请求。"})

        current_task = asyncio.current_task()
        self._active_task = current_task
        self._cancelled = False
        stopped: RunStopped | None = None

        with self._runtime_context():
            try:
                await self.ensure_started()
                if self._cancelled:
                    return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))

                if self._startup_notices:
                    pending_startup = self._startup_notices[:]
                    self._startup_notices.clear()
                    await self._send_text(format_notices_markdown(pending_startup))

                user_text, context_parts, combined_text, images = self._unpack_prompt_blocks_parts(blocks)
                if not combined_text and not images:
                    return await self._finish_prompt(PromptResponse(stop_reason="end_turn"))

                command_label: str | None = None
                fork_req: ForkRequest | None = None
                text = combined_text
                self._slash_images = {}
                if not images and user_text.lstrip().startswith("/"):
                    immediate, text, command_label, fork_req = await self._handle_slash_command(
                        user_text, context_parts
                    )
                    if self._cancelled:
                        return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))
                    if immediate is not None:
                        return await self._finish_prompt(immediate)
                merged_images: dict[str, Any] = {**images, **self._slash_images}
                self._slash_images = {}
                self._flush_deferred_approvals()
                await self._flush_team_notes()
                if self._due_loops:
                    _, was_cancelled = await self._drain_due_loops()
                    if was_cancelled:
                        return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))

                while self.conversation.notifications:
                    pending_notes = self.conversation.take_notifications()
                    if not pending_notes:
                        break
                    for update in self.mapper.map_event(TasksNotified(pending_notes)):
                        await self.client.session_update(session_id=self.session_id, update=update)
                    stopped, was_cancelled = await self._consume_reply_stream(
                        self.conversation.reply("\n\n".join(pending_notes), synthetic=NOTIFICATION)
                    )
                    if was_cancelled:
                        return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))

                next_prompt = text
                next_fork: ForkRequest | None = fork_req
                next_images: dict[str, Any] | None = merged_images or None
                next_command = command_label
                next_synthetic: str | None = None

                while True:
                    stopped, was_cancelled = await self._consume_reply_stream(
                        self.conversation.reply(
                            next_prompt,
                            fork=next_fork,
                            images=next_images,
                            command=next_command,
                            synthetic=next_synthetic,
                        )
                    )
                    if was_cancelled:
                        return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))

                    await self._flush_team_notes()
                    self._flush_deferred_approvals()

                    while (
                        not self._cancelled
                        and not self.conversation.notifications
                        and stopped is not None
                        and stopped.reason == "completed"
                    ):
                        while (
                            not self._cancelled
                            and not self.conversation.notifications
                            and self._has_active_background_turn_work()
                        ):
                            self._bg_wakeup.clear()
                            await self._flush_team_notes()
                            self._flush_deferred_approvals()
                            if self.conversation.notifications or not self._has_active_background_turn_work():
                                break
                            try:
                                await asyncio.wait_for(self._bg_wakeup.wait(), timeout=0.05)
                            except TimeoutError:
                                pass
                        if self._cancelled or self.conversation.notifications:
                            break
                        teams = getattr(self.conversation, "teams", None)
                        if teams is None or teams.current is None:
                            break
                        await teams.settle()
                        if not self.conversation.notifications:
                            await asyncio.sleep(min(getattr(teams, "poll", 0.05), 0.05))
                        await self._flush_team_notes()
                        self._flush_deferred_approvals()
                        if not self._has_active_background_turn_work():
                            break
                    if self._cancelled:
                        return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))

                    if self.conversation.notifications:
                        pending_notes = self.conversation.take_notifications()
                        if pending_notes:
                            await self._flush_team_notes()
                            for update in self.mapper.map_event(TasksNotified(pending_notes)):
                                await self.client.session_update(session_id=self.session_id, update=update)
                            next_prompt = "\n\n".join(pending_notes)
                            next_fork = None
                            next_images = None
                            next_command = None
                            next_synthetic = NOTIFICATION
                            continue

                    if self.conversation.goal is not None and stopped is not None and stopped.reason == "completed":
                        step = await self.conversation.goal_after_turn()
                        if step.note:
                            await self._send_text(f"\n> {step.note}\n")
                        if step.action == "continue" and step.prompt:
                            next_prompt = step.prompt
                            next_fork = None
                            next_images = None
                            next_command = None
                            next_synthetic = GOAL
                            continue
                    break

                loops = getattr(self.conversation, "loops", None)
                if (
                    loops is not None
                    and not self._cancelled
                    and (self.conversation.goal is None or not self.conversation.goal.active)
                ):
                    self._loop_window_open = True
                    try:
                        loops.notify_idle()
                        await asyncio.sleep(0)
                    finally:
                        self._loop_window_open = False
                    if self._due_loops:
                        loop_stopped, was_cancelled = await self._drain_due_loops()
                        if was_cancelled:
                            return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))
                        if loop_stopped is not None:
                            stopped = loop_stopped

                await self._flush_team_notes()
                for update in self.mapper.flush_notices():
                    await self.client.session_update(session_id=self.session_id, update=update)

                running = [t for t in self.conversation.tasks.running() if getattr(t, "loop", None) is None]
                if running:
                    await self._send_text(
                        f"\n> 当前仍有 {len(running)} 个后台任务在运行，输入框已恢复可用；"
                        "可输入 `/tasks` 查看状态，或在下一条消息继续跟进。\n"
                    )

                return await self._finish_prompt(PromptResponse(stop_reason=map_stop_reason(stopped)))

            except asyncio.CancelledError:
                if self._cancelled:
                    return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))
                raise
            except ProviderError as exc:
                message = redact(str(exc))
                for update in self.mapper.flush_notices():
                    await self.client.session_update(session_id=self.session_id, update=update)
                if exc.code == "empty_reply" and message.startswith("服务拒绝了这次请求"):
                    already_sent = bool(stopped is not None and stopped.notice and redact(stopped.notice) == message)
                    if not already_sent:
                        await self._send_text(f"\n> {message}\n")
                    return await self._finish_prompt(PromptResponse(stop_reason="refusal"))
                await self._drain_update_tasks()
                if "认证或权限失败" in message:
                    raise RequestError.auth_required({"reason": message}) from None
                raise RequestError.internal_error({"reason": message}) from None
            finally:
                if self._bg_approval_tasks:
                    policy = self.policy
                    if policy is not None:
                        for req_id in list(self._bg_approval_tasks):
                            if req_id in policy.pending:
                                policy.respond(req_id, "deny")
                    bg_tasks = list(self._bg_approval_tasks.values())
                    for bg_task in bg_tasks:
                        if not bg_task.done():
                            bg_task.cancel()
                    await asyncio.gather(*bg_tasks, return_exceptions=True)
                    self._bg_approval_tasks.clear()
                if self._active_task is current_task:
                    self._active_task = None
                loops = getattr(self.conversation, "loops", None)
                if loops is not None and not self._closed and not self._is_loop_busy():
                    loops.notify_idle()

    async def cancel(self) -> None:
        """Cancel the active prompt turn and release any pending permission futures."""
        self._cancelled = True
        policy = self.policy
        if policy is not None:
            for req in list(self._deferred_approvals):
                if req.id in policy.pending:
                    policy.respond(req.id, "deny")
            self._deferred_approvals.clear()
            for req_id in list(self._pending_approvals):
                if req_id in policy.pending:
                    policy.respond(req_id, "deny")
            self._pending_approvals.clear()
            for req_id, count in list(self._pending_questions.items()):
                if req_id in policy.pending:
                    policy.answer(req_id, (None,) * count)
            self._pending_questions.clear()

        bg_tasks = list(self._bg_approval_tasks.values())
        for bg_task in bg_tasks:
            if not bg_task.done():
                bg_task.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        self._bg_approval_tasks.clear()
        self._bg_wakeup.set()

        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._due_loops.clear()
        if self.policy is not None:
            self.policy.interactive = False
        await self.cancel()
        await self._drain_update_tasks()
        with self._runtime_context():
            await self.conversation.close()
