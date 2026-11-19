"""Shared workspace extension and Conversation assembly for CLI, TUI and ACP."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .agents import AgentCatalog
from .builtin_commands import BUILTIN
from .catalog import load_catalog, lookup
from .commands import CommandRegistry
from .config import OUTPUT_CAP, ConfigError, ProviderConfig, Settings, catalog_window, refreshed
from .conversation import Conversation
from .hooks import HookEngine
from .mcp_client import MCPManager
from .mcp_config import MCPConfigError, MCPSettings, ProjectMCP, ServerConfig, load_mcp_settings, project_mcp
from .mcp_surface import register_mcp_resource_tools
from .memory import MemoryStore
from .permissions import PermissionPolicy
from .plugins import Installed, project_plugins, with_project
from .plugins import installed as installed_plugins
from .providers import create_provider
from .session import Session
from .skills import SkillCatalog
from .tools import create_default_registry
from .tools.base import ToolRegistry

UNTRUSTED_PLUGINS_HINT = (
    "项目插件 {names} 未经确认，未加载；请在交互界面中启动一次并确认，或使用 --trust-project-plugins。"
)
UNTRUSTED_MCP_HINT = (
    "项目 MCP 服务器 {names} 未经确认，未启动；请在交互界面中启动一次并确认，或使用 --trust-project-mcp。"
)
UNTRUSTED_HOOK_HINT = (
    "项目 Hook 文件 {path} 未经确认，未启用；请在交互界面中启动一次并确认，或使用 --trust-project-hooks。"
)


@dataclass(frozen=True)
class WorkspaceComponents:
    """Assembled workspace extensions and tool runtime for one session."""

    plugins: Installed
    skills: SkillCatalog
    commands: CommandRegistry
    skipped_commands: tuple[str, ...]
    agents: AgentCatalog
    registry: ToolRegistry
    mcp_manager: MCPManager | None
    hooks: HookEngine
    notices: tuple[str, ...]


def with_model(config: ProviderConfig, model: str, *, home: Path | None = None) -> ProviderConfig:
    """Clone a ProviderConfig with an overridden model name, re-enriching catalog metadata."""
    cleaned = model.strip()
    if not cleaned:
        raise ConfigError("Provider 的 model 必须是非空字符串。")
    alias = config.catalog_model if cleaned == config.model else None
    catalog = load_catalog(home)
    info = lookup(cleaned, base_url=config.base_url, protocol=config.protocol, alias=alias, catalog=catalog)
    from_cat = set(config.from_catalog)
    filled: list[str] = []

    if "max_output_tokens" in from_cat or (
        config.catalog is None and config.max_output_tokens == ProviderConfig.max_output_tokens
    ):
        if info is not None and info.max_output_tokens:
            max_output_tokens = min(info.max_output_tokens, OUTPUT_CAP)
            filled.append("max_output_tokens")
        else:
            max_output_tokens = ProviderConfig.max_output_tokens
    else:
        max_output_tokens = config.max_output_tokens

    if isinstance(config.thinking, dict) and config.thinking.get("type") == "enabled":
        budget_tokens = config.thinking.get("budget_tokens", 0)
        if isinstance(budget_tokens, int) and budget_tokens >= max_output_tokens:
            max_output_tokens = max(config.max_output_tokens, budget_tokens + 1024)

    if config.context_window is None or "context_window" in from_cat:
        window = catalog_window(info, max_output_tokens) if info is not None else None
        context_window = window
        if window is not None:
            filled.append("context_window")
    else:
        context_window = config.context_window

    if config.images is None or "images" in from_cat:
        images = info.vision if (info is not None and info.vision is not None) else None
        if images is not None:
            filled.append("images")
    else:
        images = config.images

    updated = replace(
        config,
        model=cleaned,
        max_output_tokens=max_output_tokens,
        context_window=context_window,
        images=images,
        catalog_model=alias,
        catalog=info,
        from_catalog=tuple(filled),
    )
    problem = updated.budget().problem()
    if problem:
        raise ConfigError(f"Provider {updated.name}：{problem}")
    return updated


def claude_notice(root: Path) -> str | None:
    """Definitions written for Claude Code in the project that stay unread while claude_directories is off."""
    agents = sum(1 for path in (root / ".claude" / "agents").glob("*.md") if path.is_file())
    skills = sum(1 for path in (root / ".claude" / "skills").glob("*/SKILL.md") if path.is_file())
    found = "、".join(
        part for part in (f"{agents} 个子 Agent" if agents else "", f"{skills} 个 Skill" if skills else "") if part
    )
    if not found:
        return None
    return f"发现 .claude 中的 {found}，在 config.yaml 中设置 claude_directories: true 即可使用。"


def resolve_plugins(
    root: Path,
    *,
    trust_project: bool = False,
    home: Path | None = None,
    confirm: Callable[[Any], tuple[bool, Sequence[str]]] | None = None,
    untrusted_hint: str = UNTRUSTED_PLUGINS_HINT,
) -> Installed:
    """Load user plugins and, when trusted or interactively confirmed, project plugins."""
    user = installed_plugins(home=home)
    project = project_plugins(root, home=home)
    notices = list(project.notices)
    use = bool(project.plugins) and (trust_project or project.trusted(home))
    if project.plugins and not use:
        if confirm is not None:
            use, extra = confirm(project)
            notices.extend(extra)
        else:
            names = "、".join(plugin.name for plugin in project.plugins)
            notices.append(untrusted_hint.format(names=names))
    combined = with_project(user, project) if use else user
    return Installed(combined.plugins, (*combined.notices, *notices))


def load_skills_and_commands(
    root: Path,
    *,
    plugins: Sequence[Any] = (),
    claude: bool = False,
    home: Path | None = None,
    builtin_commands: CommandRegistry = BUILTIN,
) -> tuple[SkillCatalog, CommandRegistry, tuple[str, ...]]:
    """Load skill definitions and bind them to a fresh command registry."""
    skills = SkillCatalog.load(
        root,
        home,
        reserved=builtin_commands.names(),
        plugins=plugins,
        claude=claude,
    )
    commands, skipped = builtin_commands.with_skills(skills)
    return skills, commands, skipped


def build_hook_engine(
    root: Path,
    registry: ToolRegistry,
    *,
    home: Path | None = None,
    no_hooks: bool = False,
    trust_project_hooks: bool = False,
    interactive: bool = False,
    plugins: Sequence[Any] | None = None,
    untrusted_hint: str = UNTRUSTED_HOOK_HINT,
) -> tuple[HookEngine, tuple[str, ...]]:
    """Construct the HookEngine and gather configuration/untrusted hook notices."""
    exec_tool = registry.get("execute_command")
    engine = HookEngine(
        root,
        home=home,
        shell=getattr(exec_tool, "shell", None),
        enabled=not no_hooks,
        trust_project=trust_project_hooks,
        plugins=plugins,
    )
    notices = list(engine.problems)
    if not interactive:
        notices.extend(untrusted_hint.format(path=file.path) for file in engine.untrusted)
    return engine, tuple(notices)


def build_mcp_manager(
    root: Path,
    *,
    home: Path | None = None,
    plugins: Sequence[Any] = (),
    trust_project_mcp: bool = False,
    project: ProjectMCP | None = None,
    confirm: Callable[[ProjectMCP], tuple[bool, Sequence[str]]] | None = None,
    extra_servers: Mapping[str, ServerConfig] | None = None,
    extra_notices: Sequence[str] = (),
    mcp_loader: Callable[..., MCPSettings] = load_mcp_settings,
    catch_config_error: bool = False,
    merge_mcp_notices: bool = False,
    untrusted_hint: str = UNTRUSTED_MCP_HINT,
) -> tuple[MCPManager | None, tuple[str, ...]]:
    """Load MCP settings (with optional client overrides) and build MCPManager when servers or notices exist."""
    user_mcp_path = home / ".mosscode" / "mcp.yaml" if home is not None else None
    mcp_notices: list[str] = []
    try:
        proj_mcp = project if project is not None else project_mcp(root, user_path=user_mcp_path)
        if confirm is not None:
            use_proj, confirmed_notices = confirm(proj_mcp)
            mcp_notices.extend(confirmed_notices)
        else:
            use_proj = not proj_mcp.servers or trust_project_mcp or proj_mcp.trusted(home)
            if proj_mcp.servers and not use_proj:
                mcp_notices.append(untrusted_hint.format(names="、".join(proj_mcp.servers)))
        load_kwargs: dict[str, Any] = {
            "plugins": plugins,
            "project": proj_mcp if use_proj else False,
        }
        if user_mcp_path is not None:
            load_kwargs["user_path"] = user_mcp_path
        base_mcp = mcp_loader(root, **load_kwargs)
    except MCPConfigError as exc:
        if not catch_config_error:
            raise
        mcp_notices = [str(exc)]
        base_mcp = MCPSettings()

    if merge_mcp_notices or extra_servers or extra_notices:
        merged_by_name = {srv.name: srv for srv in base_mcp.servers}
        if extra_servers:
            merged_by_name.update(extra_servers)
        mcp_settings = MCPSettings(
            servers=tuple(merged_by_name.values()),
            notices=(*base_mcp.notices, *mcp_notices, *extra_notices),
        )
        manager = MCPManager(root, mcp_settings) if mcp_settings.servers or mcp_settings.notices else None
        return manager, ()

    manager = MCPManager(root, base_mcp) if base_mcp.servers or base_mcp.notices else None
    return manager, tuple(mcp_notices)


def build_memory_options(
    settings: Settings,
    root: Path,
    *,
    home: Path | None = None,
    auto_memory: bool = False,
    provider_factory: Callable[[ProviderConfig], Any] = create_provider,
) -> dict[str, Any]:
    """Build Conversation keyword arguments for user/project memory and optional background extraction."""
    if not settings.memory:
        return {}
    store = MemoryStore.for_project(root, home=home) if home is not None else MemoryStore.for_project(root)
    options: dict[str, Any] = {
        "memory": store,
        "auto_memory": auto_memory and settings.auto_memory,
    }
    if options["auto_memory"] and settings.memory_provider:
        selected = settings.select(settings.memory_provider)
        options["memory_provider"] = provider_factory(refreshed(selected, home=home) if home is not None else selected)
    return options


def assemble_workspace_extensions(
    root: Path,
    settings: Settings,
    *,
    home: Path | None = None,
    permissions: PermissionPolicy | None = None,
    permission_mode: str | None = None,
    ask: bool = True,
    no_hooks: bool = False,
    interactive_hooks: bool = False,
    trust_project_plugins: bool = False,
    trust_project_mcp: bool = False,
    trust_project_hooks: bool = False,
    plugins: Installed | None = None,
    skills_bundle: tuple[SkillCatalog, CommandRegistry, tuple[str, ...]] | None = None,
    extra_mcp_servers: Mapping[str, ServerConfig] | None = None,
    extra_mcp_notices: Sequence[str] = (),
    catch_mcp_config_error: bool = False,
    merge_mcp_notices: bool = False,
    registry_factory: Callable[..., ToolRegistry] = create_default_registry,
    mcp_loader: Callable[..., MCPSettings] = load_mcp_settings,
    builtin_commands: CommandRegistry = BUILTIN,
    untrusted_plugins_hint: str = UNTRUSTED_PLUGINS_HINT,
    untrusted_mcp_hint: str = UNTRUSTED_MCP_HINT,
    untrusted_hook_hint: str = UNTRUSTED_HOOK_HINT,
) -> WorkspaceComponents:
    """Assemble plugins, skills, commands, agents, tool registry, MCP manager and hooks for a workspace."""
    resolved_plugins = plugins or resolve_plugins(
        root,
        trust_project=trust_project_plugins,
        home=home,
        untrusted_hint=untrusted_plugins_hint,
    )
    claude = settings.claude_directories
    skills, commands, skipped = skills_bundle or load_skills_and_commands(
        root,
        plugins=resolved_plugins.plugins,
        claude=claude,
        home=home,
        builtin_commands=builtin_commands,
    )
    reg_kwargs: dict[str, Any] = {
        "shell": settings.shell,
        "web_search": settings.web_search,
        "todo": settings.todo_tool,
        "ask": ask,
    }
    if permissions is not None:
        reg_kwargs["root"] = root
        reg_kwargs["permissions"] = permissions
    else:
        if root != Path.cwd():
            reg_kwargs["root"] = root
        reg_kwargs["permission_mode"] = permission_mode
    registry = registry_factory(**reg_kwargs)
    mcp_manager, mcp_notices = build_mcp_manager(
        root,
        home=home,
        plugins=resolved_plugins.plugins,
        trust_project_mcp=trust_project_mcp,
        extra_servers=extra_mcp_servers,
        extra_notices=extra_mcp_notices,
        mcp_loader=mcp_loader,
        catch_config_error=catch_mcp_config_error,
        merge_mcp_notices=merge_mcp_notices,
        untrusted_hint=untrusted_mcp_hint,
    )
    register_mcp_resource_tools(registry, mcp_manager)
    hooks, hook_notices = build_hook_engine(
        root,
        registry,
        home=home,
        no_hooks=no_hooks,
        trust_project_hooks=trust_project_hooks,
        interactive=interactive_hooks,
        plugins=resolved_plugins.plugins,
        untrusted_hint=untrusted_hook_hint,
    )
    agents = AgentCatalog.load(root, home, plugins=resolved_plugins.plugins, claude=claude)
    notices = resolved_plugins.notices + skills.problems + skipped + hook_notices + mcp_notices
    return WorkspaceComponents(
        plugins=resolved_plugins,
        skills=skills,
        commands=commands,
        skipped_commands=skipped,
        agents=agents,
        registry=registry,
        mcp_manager=mcp_manager,
        hooks=hooks,
        notices=notices,
    )


def build_conversation(
    settings: Settings,
    provider_config: ProviderConfig,
    *,
    root: Path,
    registry: ToolRegistry,
    skills: SkillCatalog,
    agents: AgentCatalog | None = None,
    mcp_manager: MCPManager | None = None,
    hooks: HookEngine | None = None,
    session: Session | None = None,
    home: Path | None = None,
    notices: Sequence[str] = (),
    auto_memory: bool = False,
    enable_goal_provider: bool = True,
    command_time_limit: float | None = None,
    teams: bool = False,
    team_launch: dict[str, Any] | None = None,
    coordinator: bool = False,
    provider: Any | None = None,
    goal_provider: Any | None = None,
    provider_factory: Callable[[ProviderConfig], Any] = create_provider,
    conversation_cls: Callable[..., Conversation] = Conversation,
) -> Conversation:
    """Build a Conversation from Settings and workspace runtime components."""
    active_provider = provider if provider is not None else provider_factory(provider_config)
    resolved_goal_provider = goal_provider
    if resolved_goal_provider is None and enable_goal_provider and settings.goal_provider:
        selected_goal = settings.select(settings.goal_provider)
        resolved_goal_provider = provider_factory(
            refreshed(selected_goal, home=home) if home is not None else selected_goal
        )
    resolved_agents = (
        agents if agents is not None else AgentCatalog.load(root, home, claude=settings.claude_directories)
    )
    resolved_session = session if session is not None else Session.new(root, home=home)
    memory_opts = build_memory_options(
        settings,
        root,
        home=home,
        auto_memory=auto_memory,
        provider_factory=provider_factory,
    )
    extra_kwargs: dict[str, Any] = {}
    if home is not None:
        extra_kwargs["instructions_home"] = home
        extra_kwargs["sessions_home"] = home
    if command_time_limit is not None:
        extra_kwargs["command_time_limit"] = command_time_limit
    if team_launch is not None:
        extra_kwargs["team_launch"] = team_launch

    register_mcp_resource_tools(registry, mcp_manager)
    return conversation_cls(
        active_provider,
        settings.system_prompt,
        registry,
        mcp_manager=mcp_manager,
        max_iterations=settings.max_iterations,
        run_budget=settings.run_budget,
        context_root=root,
        budget=provider_config.budget(),
        session=resolved_session,
        auto_compact=settings.auto_compact,
        context_collapse=settings.context_collapse,
        notices=tuple(notices),
        persist=settings.persist_sessions,
        skills=skills,
        provider_configs=settings.providers,
        provider_factory=provider_factory,
        provider_info=(provider_config.name, provider_config.protocol, provider_config.model),
        hooks=hooks,
        agents=resolved_agents,
        agent_auto_background=settings.agent_auto_background,
        agent_max_background=settings.agent_max_background,
        command_max_background=settings.command_max_background,
        command_auto_background=settings.command_auto_background,
        worktree_links=settings.worktree_links,
        worktree_stale_hours=settings.worktree_stale_hours,
        teams=teams,
        team_max_members=settings.team_max_members,
        teammate_mode=settings.teammate_mode,
        coordinator=coordinator,
        goal_max_turns=settings.goal_max_turns,
        goal_provider=resolved_goal_provider,
        **extra_kwargs,
        **memory_opts,
    )
