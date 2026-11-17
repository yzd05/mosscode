"""Single-session ACP state and lifecycle management."""

from __future__ import annotations

import asyncio
import base64
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
    AudioContentBlock,
    BlobResourceContents,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    LoadSessionResponse,
    McpServerStdio,
    NewSessionResponse,
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
)

from ..builtin_commands import (
    BUILTIN,
    CLEARED,
    MODE_LABELS,
    PLAN_ENTERED,
    hooks_report,
    mcp_report,
    memory_report,
    tasks_report,
    thinking_detail,
    typed,
)
from ..command_env import use_command_env
from ..commands import CommandRegistry, argument_problem, parse_input, unknown_note
from ..config import ConfigError, ProviderConfig, Settings, refreshed
from ..conversation import Conversation, Resumed
from ..coordinator import decide as decide_coordinator
from ..credentials import redact
from ..goal import GOAL
from ..images import ImageError, ProcessedImage, load_image
from ..mcp_config import SERVER_NAME, ServerConfig
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
from ..session import Session, SessionLocked, find_session
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
        self._update_tasks: set[asyncio.Task[Any]] = set()
        self._last_reported_mode: str = self.current_mode

        if self.policy is not None:
            self.policy.interactive = True
        if self.conversation.registry is not None:
            prev_todos_hook = self.conversation.registry.todos.on_change

            def _combined_todos_changed(items: tuple[TodoItem, ...], mark: int) -> None:
                if prev_todos_hook is not None:
                    prev_todos_hook(items, mark)
                self._on_todos_changed(items, mark)

            self.conversation.registry.todos.on_change = _combined_todos_changed

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
            coordinator, coord_notice = decide_coordinator(settings.coordinator_mode, {}, interactive=False)
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

            conversation = build_conversation(
                settings,
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
                teams=False,
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

    def _on_todos_changed(self, items: tuple[TodoItem, ...], _version: int) -> None:
        self._spawn_update(todos_to_plan_update(items))

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            with self._runtime_context():
                notes = await self.conversation.start()
            self._started = True
            for note in notes:
                self._startup_notices.append(note)
                print(f"MossCode ACP: {redact(note)}", file=sys.stderr)

    async def send_initial_updates(self) -> None:
        """Send available slash commands after session creation."""
        await self.client.session_update(
            session_id=self.session_id,
            update=build_available_commands(self.commands),
        )

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
        """Switch the active provider, optionally cloning it with an overridden model."""
        selected = refreshed(self.settings.select(name), home=self.options.home)
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
        if model is not None or selected.follow:
            providers = tuple(selected if item.name == selected.name else item for item in self.settings.providers)
            self.settings = replace(self.settings, providers=providers)
            self.conversation.provider_configs = providers
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
                try:
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

        if cmd.name in EXCLUDED_ACP_COMMANDS:
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
            await self._send_text(format_help_markdown(self.commands, args, self.conversation.skills))
            return PromptResponse(stop_reason="end_turn"), "", None, None

        if name == "clear":
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
                if not images and user_text.lstrip().startswith("/"):
                    immediate, text, command_label, fork_req = await self._handle_slash_command(
                        user_text, context_parts
                    )
                    if self._cancelled:
                        return await self._finish_prompt(PromptResponse(stop_reason="cancelled"))
                    if immediate is not None:
                        return await self._finish_prompt(immediate)

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
                next_images: dict[str, ProcessedImage] | None = images or None
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

                    if self.conversation.notifications:
                        pending_notes = self.conversation.take_notifications()
                        if pending_notes:
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

                for update in self.mapper.flush_notices():
                    await self.client.session_update(session_id=self.session_id, update=update)

                running = self.conversation.tasks.running()
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
                if self._active_task is current_task:
                    self._active_task = None

    async def cancel(self) -> None:
        """Cancel the active prompt turn and release any pending permission futures."""
        self._cancelled = True
        policy = self.policy
        if policy is not None:
            for req_id in list(self._pending_approvals):
                if req_id in policy.pending:
                    policy.respond(req_id, "deny")
            self._pending_approvals.clear()
            for req_id, count in list(self._pending_questions.items()):
                if req_id in policy.pending:
                    policy.answer(req_id, (None,) * count)
            self._pending_questions.clear()

        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.policy is not None:
            self.policy.interactive = False
        await self.cancel()
        await self._drain_update_tasks()
        with self._runtime_context():
            await self.conversation.close()
