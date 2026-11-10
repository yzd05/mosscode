import argparse
import asyncio
import json
import os
import sys
from contextlib import aclosing
from pathlib import Path
from time import monotonic

from .agents import AgentCatalog
from .agents.tasks import Announcer
from .appearance import compact_notice
from .builtin_commands import BUILTIN, typed
from .catalog import load_catalog
from .commands import CommandRegistry, argument_problem, parse_input, unknown_note
from .config import ConfigError, find_config, load_settings, project_notice
from .conversation import Conversation, Notice, Resumed
from .coordinator import decide as decide_coordinator
from .credentials import plaintext_notice, protect
from .hooks import HookEngine
from .launcher import is_terminal, parse_arguments
from .mcp_client import MCPManager
from .mcp_config import MCPConfigError, load_mcp_settings
from .memory import MemoryStore
from .messages import (
    CompactFailed,
    CompactFinished,
    CompactStarted,
    Completed,
    Delta,
    FilesAttached,
    Message,
    ProviderError,
    ResponseBoundary,
    Retrying,
    RunStopped,
    ToolProgress,
    UsageUpdate,
)
from .permissions import PermissionConfigError
from .plugins import Installed, project_plugins, with_project
from .plugins import installed as installed_plugins
from .providers import create_provider
from .sandbox import configure as configure_sandbox
from .session import Session, prune_all
from .skills import ForkRequest, SkillCatalog
from .tools import create_default_registry
from .tools.shell import ShellNotFound
from .tui import MossCodeApp, PluginTrustApp, ProviderSelectApp, plan_text
from .usage import request_usage_text


def _headless_input(
    text: str, commands: CommandRegistry = BUILTIN, notices: tuple[str, ...] = ()
) -> tuple[str, str | None, str | None]:
    """-p sends messages, prompt commands and skills; returns (prompt, command, skill) or exits with 2.

    For a skill the prompt is its argument: the request is rendered once the tools are known.
    """
    parsed = parse_input(text, commands)
    if parsed.kind in {"empty", "message", "shell"}:  # -p speaks to the model; ! runs nothing here.
        return (parsed.text if parsed.kind == "message" else text), None, None
    if parsed.kind == "unknown":
        problem = unknown_note(parsed, commands)
    elif parsed.command.kind == "local":
        problem = f"/{parsed.command.name} 只能在交互界面中使用。"
    else:
        problem = argument_problem(parsed.command, parsed.argument)
    if problem:
        for notice in notices:  # Say why a skill is missing before the run ends here.
            print(notice, file=sys.stderr)
        print(problem, file=sys.stderr)
        raise SystemExit(2)
    if parsed.command.kind == "skill":
        return parsed.argument, typed(parsed.command, parsed.argument), parsed.command.skill
    return parsed.command.expand(parsed.argument), typed(parsed.command, parsed.argument), None


def _skill_prompt(conversation: Conversation, skill: str | None, prompt: str, report):
    """Check the skill's tools once MCP is up, then send it like the TUI does; returns (prompt, fork)."""
    if skill is None:
        return prompt, None
    found, rendered, notices = conversation.prepare_skill(skill, prompt)
    for notice in notices:
        report("notice", notice)
    if found.mode != "fork":
        return rendered, None
    report("notice", f"在独立上下文中运行 Skill {found.name}")
    return rendered, ForkRequest(found, rendered)


def _arguments(arguments: str):
    try:
        return json.loads(arguments)
    except ValueError:
        return arguments


def _assistant(message: Message, usage, stop_reason: str | None) -> dict:
    return {
        "type": "assistant",
        "text": message.text,
        "tool_calls": [{"id": c.id, "name": c.name, "arguments": _arguments(c.arguments)} for c in message.tool_calls],
        "stop_reason": stop_reason,
        "usage": _usage_dict(usage),
    }


def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def _compact_event(event) -> dict:
    if isinstance(event, CompactStarted):
        return {"type": "compact", "subtype": "started", "manual": event.manual}
    if isinstance(event, CompactFinished):
        return {
            "type": "compact",
            "subtype": "finished",
            "removed": event.removed,
            "summary_tokens": event.summary_tokens,
            "duration_ms": round(event.elapsed * 1000),
            "record": event.record,
        }
    return {
        "type": "compact",
        "subtype": "failed",
        "reason": event.reason,
        "consecutive": event.consecutive,
        "paused": event.paused,
    }


async def _resume(conversation: Conversation, session_id: str | None, report) -> None:
    """Restore before the task; report(kind, value) prints notices and compaction progress."""
    if session_id is None:
        return
    async with aclosing(conversation.resume(session_id)) as stream:
        async for event in stream:
            if isinstance(event, Resumed):
                restored = event.restored
                report("notice", f"已恢复会话 {restored.id} · {restored.title} · {event.messages} 条消息")
                for notice in event.notices:
                    report("notice", notice)
                for text in restored.unanswered:
                    # Never resent automatically; shown so the user can decide.
                    report("unanswered", text)
            elif isinstance(event, Notice):
                report("notice", event.text)
            else:
                report("compact", event)


def _background_notices(conversation: Conversation, report) -> None:
    """Hook failures, background ones included, go to stderr (stream-json: a notice event)."""
    conversation.on_memory = lambda event: report("notice", event.text) if isinstance(event, Notice) else None


def _task_notices(conversation: Conversation, report) -> None:
    """A line on stderr (stream-json: a notice event) when a background task starts or ends."""
    conversation.tasks.on_change = Announcer(lambda text: report("notice", text))


async def _turns(conversation: Conversation, prompt: str, *, command: str | None, fork):
    """The task's turn, then one turn per batch of notifications until no task is left running."""
    async with aclosing(conversation.reply(prompt, command=command, fork=fork)) as stream:
        async for event in stream:
            yield event
    tasks = conversation.tasks
    while True:
        if conversation.notifications:
            async with aclosing(conversation.notify()) as stream:
                async for event in stream:
                    yield event
            continue
        if not tasks.running():
            return
        tasks.finished.clear()
        await tasks.finished.wait()


def _hook_engine(args, registry, *, interactive: bool, plugins=None):
    """--no-hooks loads nothing; -p skips untrusted project files unless --trust-project-hooks."""
    tool = registry.get("execute_command")
    engine = HookEngine(
        Path.cwd(),
        shell=getattr(tool, "shell", None),
        enabled=not args.no_hooks,
        trust_project=args.trust_project_hooks,
        plugins=plugins,
    )
    notices = list(engine.problems)
    if not interactive:
        notices += [
            f"项目 Hook 文件 {file.path} 未经确认，未启用；请在交互界面中启动一次并确认，或使用 --trust-project-hooks。"
            for file in engine.untrusted
        ]
    return engine, tuple(notices)


async def _prompt(
    conversation: Conversation,
    prompt: str,
    *,
    show_usage: bool = False,
    output_format: str = "text",
    model: str = "",
    resume: str | None = None,
    command: str | None = None,
    skill: str | None = None,
) -> dict | None:
    """Run one headless task; JSON formats return the result object instead of raising."""
    if output_format == "text":
        await _text_prompt(conversation, prompt, show_usage=show_usage, resume=resume, command=command, skill=skill)
        return None
    stream_json = output_format == "stream-json"
    started = monotonic()
    stopped: RunStopped | None = None
    final = ""
    error: str | None = None

    def report(kind, value):
        if kind == "compact":
            if stream_json:
                _emit(_compact_event(value))
        elif stream_json:
            _emit({"type": "notice", "text": value if kind == "notice" else "未得到回复的消息：" + value})
        else:
            print(value if kind == "notice" else "未得到回复的消息：" + value, file=sys.stderr, flush=True)

    _background_notices(conversation, report)
    _task_notices(conversation, report)
    try:
        try:
            if stream_json:
                _emit(
                    {
                        "type": "session",
                        "id": conversation.session_id if resume is None else resume,
                        "resumed": resume is not None,
                    }
                )
            await _resume(conversation, resume, report)
            notices = await conversation.start()
            for notice in notices:
                if stream_json:
                    _emit({"type": "notice", "text": notice})
                else:
                    print(notice, file=sys.stderr, flush=True)
            prompt, fork = _skill_prompt(conversation, skill, prompt, report)
            registry = conversation.registry
            if stream_json:
                _emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "cwd": str(Path.cwd()),
                        "model": model,
                        "permission_mode": registry.permissions.mode if registry else None,
                        "tools": [d.name for d in registry.definitions()] if registry else [],
                    }
                )
            usage = None
            async with aclosing(_turns(conversation, prompt, command=command, fork=fork)) as stream:
                async for event in stream:
                    if isinstance(event, UsageUpdate):
                        usage = event.usage
                    elif isinstance(event, RunStopped):
                        stopped = event
                    elif isinstance(event, Completed):
                        final = event.message.text
                    if not stream_json:
                        continue
                    if isinstance(event, ResponseBoundary):
                        _emit(_assistant(event.message, usage, "tool_use"))
                    elif isinstance(event, Completed):
                        _emit(_assistant(event.message, usage, event.stop_reason))
                    elif isinstance(event, ToolProgress) and event.phase == "finished":
                        result = event.result
                        item = {
                            "type": "tool_result",
                            "id": event.call.id,
                            "name": event.call.name,
                            "is_error": result.is_error,
                            "code": result.code,
                            "output": result.output,
                        }
                        if result.images:
                            # Metadata only: the bytes stay in the session directory.
                            item["images"] = [
                                {"name": i.name, "media_type": i.media_type, "width": i.width, "height": i.height}
                                for i in result.images
                            ]
                        _emit(item)
                    elif isinstance(event, Retrying):
                        _emit(
                            {
                                "type": "retry",
                                "attempt": event.attempt,
                                "max_attempts": event.max_attempts,
                                "delay": event.delay,
                                "reason": event.reason,
                            }
                        )
                    elif isinstance(event, (CompactStarted, CompactFinished, CompactFailed)):
                        _emit(_compact_event(event))
                    elif isinstance(event, Notice):
                        _emit({"type": "notice", "text": event.text})
                    elif isinstance(event, FilesAttached):
                        _emit({"type": "files_attached", "entries": list(event.entries)})
        except ProviderError as exc:
            error = str(exc)
        registry = conversation.registry
        total = conversation.total_usage
        subtype = "error" if error is not None else stopped.reason if stopped else "error"
        result = {
            "type": "result",
            "subtype": subtype,
            "is_error": subtype != "completed",
            "result": final if subtype == "completed" else error or (stopped.notice if stopped else "没有完整回复。"),
            "num_turns": stopped.iterations if stopped else 0,
            "duration_ms": round((monotonic() - started) * 1000),
            "usage": {
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "cached_input_tokens": total.cached_input_tokens,
                "cache_write_tokens": total.cache_write_tokens,
                "requests": total.requests,
            },
            # Base prices from the model catalog; null when no request had a price.
            "total_cost_usd": round(total.cost_usd, 6) if total.missing_cost < total.requests else None,
            "compactions": conversation.compactions,
            "session_id": conversation.session_id,
            "model": model,
            "permission_mode": registry.permissions.mode if registry else None,
        }
        _emit(result)
        return result
    finally:
        await conversation.close()


async def _text_prompt(
    conversation: Conversation,
    prompt: str,
    *,
    show_usage: bool = False,
    resume: str | None = None,
    command: str | None = None,
    skill: str | None = None,
) -> None:
    def report(kind, value):
        if kind == "compact":
            print(
                "正在压缩上下文…" if isinstance(value, CompactStarted) else compact_notice(value),
                file=sys.stderr,
                flush=True,
            )
        else:
            print(value if kind == "notice" else "未得到回复的消息：" + value, file=sys.stderr, flush=True)

    _background_notices(conversation, report)
    _task_notices(conversation, report)
    try:
        await _resume(conversation, resume, report)
        for notice in await conversation.start():
            print(notice, file=sys.stderr, flush=True)
        prompt, fork = _skill_prompt(conversation, skill, prompt, report)
        async with aclosing(_turns(conversation, prompt, command=command, fork=fork)) as stream:
            async for event in stream:
                if isinstance(event, Delta) and event.kind == "text":
                    print(event.text, end="", flush=True)
                elif isinstance(event, ResponseBoundary):
                    print()
                elif (
                    isinstance(event, ToolProgress) and event.phase == "running" and event.call.name == "exit_plan_mode"
                ):
                    print(f"\n计划：\n{plan_text(event.call.arguments)}", flush=True)
                elif isinstance(event, ToolProgress) and event.phase == "finished":
                    print(f"\n● {event.call.name} · {event.elapsed:.1f}s\n└ {event.result.output}", flush=True)
                elif isinstance(event, RunStopped) and event.reason in {
                    "max_iterations",
                    "unknown_tools",
                    "max_tokens",
                }:
                    print("\n" + event.notice, flush=True)
                elif isinstance(event, Retrying):
                    print(
                        f"{event.reason}，{event.delay:.1f} 秒后重试（{event.attempt}/{event.max_attempts}）",
                        file=sys.stderr,
                        flush=True,
                    )
                elif isinstance(event, UsageUpdate) and show_usage:
                    print(request_usage_text(event.usage, event.iteration), file=sys.stderr, flush=True)
                elif isinstance(event, CompactStarted):
                    print("正在压缩上下文…", file=sys.stderr, flush=True)
                elif isinstance(event, (CompactFinished, CompactFailed)):
                    print(compact_notice(event), file=sys.stderr, flush=True)
                elif isinstance(event, Notice):
                    print(event.text, file=sys.stderr, flush=True)
                elif isinstance(event, FilesAttached):
                    print("已附上 " + "、".join(event.entries), file=sys.stderr, flush=True)
        print()
    finally:
        await conversation.close()


def _memory_options(settings, *, interactive: bool) -> dict:
    """Notes and the memory tool everywhere; background extraction only in the TUI."""
    if not settings.memory:
        return {}
    options = {"memory": MemoryStore.for_project(Path.cwd()), "auto_memory": interactive and settings.auto_memory}
    if options["auto_memory"] and settings.memory_provider:
        options["memory_provider"] = create_provider(settings.select(settings.memory_provider))
    return options


def load_plugins(args, root: Path) -> Installed:
    """The user's plugins and, once trusted, the project's: the interface asks before anything loads, -p loads
    them only with --trust-project-plugins or an earlier confirmation."""
    user = installed_plugins()
    project = project_plugins(root)
    notices = list(project.notices)
    use = bool(project.plugins) and (args.trust_project_plugins or project.trusted())
    if project.plugins and not use:
        names = [plugin.name for plugin in project.plugins]
        if args.prompt is None:
            from .plugin_command import trust_text

            print(trust_text(project))
            use = bool(PluginTrustApp(names).run(inline=True, mouse=False))
            if not use:
                notices.append("本次未启用项目插件；下次启动时会再次询问。")
            else:
                try:
                    project.trust()
                except OSError as exc:
                    notices.append(f"无法写入项目插件的确认记录（{exc.strerror or exc}），只在本次启用。")
        else:
            notices.append(
                f"项目插件 {'、'.join(names)} 未经确认，未加载；请在交互界面中启动一次并确认，"
                "或使用 --trust-project-plugins。"
            )
    combined = with_project(user, project) if use else user
    return Installed(combined.plugins, (*combined.notices, *notices))


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


def _config_path(args) -> Path | None:
    """No configuration in an interactive terminal starts the setup; None when it was left."""
    if args.config is not None:
        return Path(args.config)
    try:
        return find_config()
    except ConfigError:
        if args.prompt is not None or args.list_providers or not (is_terminal(sys.stdin) and is_terminal(sys.stdout)):
            raise
    from .setup import run_setup

    return run_setup()


def main(argv: list[str] | None = None) -> None:
    """The arguments as launcher.main parses them, without its terminal checks (tests call this)."""
    run(parse_arguments(argv))


def run(args: argparse.Namespace) -> None:
    if args.team_dir is not None:
        from .teams.process import run_member

        raise SystemExit(run_member(args.team_dir, args.team_member, args.config, args.provider, sandbox=args.sandbox))
    try:
        path = _config_path(args)
        if path is None:
            return
        settings = load_settings(path)
        if args.list_providers:
            for item in settings.providers:
                print(f"{item.name}: {item.protocol} / {item.model}")
            return
        # Plugins are read once; skills come before the provider so that -p "/<name>" parses.
        plugins = load_plugins(args, Path.cwd())
        claude = settings.claude_directories
        skills = SkillCatalog.load(Path.cwd(), reserved=BUILTIN.names(), plugins=plugins.plugins, claude=claude)
        commands, skipped = BUILTIN.with_skills(skills)
        prompt, command, skill = (
            _headless_input(args.prompt, commands, plugins.notices + skills.problems + skipped)
            if args.prompt is not None
            else (None, None, None)
        )
        protect(settings)
        # Before the tools are built: the command tool's shell and parameters depend on it.
        sandbox = configure_sandbox(settings.sandbox, override=args.sandbox, source=settings.source)
        config = settings.select(args.provider)
        if (
            args.prompt is None
            and args.provider is None
            and settings.default_provider is None
            and len(settings.providers) > 1
        ):
            config = ProviderSelectApp(settings).run(inline=True, mouse=False)
            if config is None:
                return
        registry = create_default_registry(
            permission_mode=args.permission_mode, shell=settings.shell, web_search=settings.web_search
        )
        mcp_settings = load_mcp_settings(Path.cwd(), plugins=plugins.plugins)
        manager = MCPManager(Path.cwd(), mcp_settings) if mcp_settings.servers or mcp_settings.notices else None
        prune_all()
        budget = config.budget()
        hooks, hook_notices = _hook_engine(args, registry, interactive=args.prompt is None, plugins=plugins.plugins)
        notices = plugins.notices + skills.problems + skipped + hook_notices
        if args.prompt is None and not claude and (found := claude_notice(Path.cwd())):
            notices += (found,)
        coordinator, coordinator_notice = decide_coordinator(
            settings.coordinator_mode, os.environ, interactive=args.prompt is None
        )
        if coordinator_notice:
            notices += (coordinator_notice,)
        notices += tuple(filter(None, (project_notice(settings),))) + tuple(sandbox.notices())
        notices += tuple(
            filter(None, (load_catalog().problem, config.window_note(), plaintext_notice(settings, Path.cwd())))
        )
        conversation = Conversation(
            create_provider(config),
            settings.system_prompt,
            registry,
            mcp_manager=manager,
            max_iterations=settings.max_iterations,
            context_root=Path.cwd(),
            budget=budget,
            session=Session.new(Path.cwd()),
            auto_compact=settings.auto_compact,
            notices=notices,
            persist=settings.persist_sessions,
            skills=skills,
            provider_configs=settings.providers,
            provider_factory=create_provider,
            provider_info=(config.name, config.protocol, config.model),
            hooks=hooks,
            agents=AgentCatalog.load(Path.cwd(), plugins=plugins.plugins, claude=claude),
            agent_auto_background=settings.agent_auto_background,
            agent_max_background=settings.agent_max_background,
            worktree_links=settings.worktree_links,
            worktree_stale_hours=settings.worktree_stale_hours,
            teams=args.prompt is None,
            team_max_members=settings.team_max_members,
            teammate_mode=settings.teammate_mode,
            team_launch={"config": settings.source, "provider": config.name},
            coordinator=coordinator,
            **_memory_options(settings, interactive=args.prompt is None),
        )
        resume = args.resume
        if args.continue_session or resume:
            resume = conversation.resolve_session(resume or None)
        if args.prompt is not None:
            if args.output_format != "text" and hasattr(sys.stdout, "reconfigure"):
                # Machine-readable output is always UTF-8, whatever the console code page.
                sys.stdout.reconfigure(encoding="utf-8")
            result = asyncio.run(
                _prompt(
                    conversation,
                    prompt,
                    show_usage=args.show_usage,
                    output_format=args.output_format,
                    model=config.model,
                    resume=resume,
                    command=command,
                    skill=skill,
                )
            )
            if result is not None and result["is_error"]:
                raise SystemExit(1)
        else:
            if resume == "" and conversation.unavailable():
                raise ProviderError(conversation.unavailable())
            MossCodeApp(
                conversation,
                config.name,
                settings=settings,
                model=config.model,
                resume=resume,
                commands=commands,
                initial_prompt=args.initial,
            ).run(
                inline=True,
                inline_no_clear=True,
                mouse=False,
            )
    except (ConfigError, ProviderError, PermissionConfigError, MCPConfigError, ShellNotFound) as exc:
        print(f"MossCode: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130) from None
