"""The built-in slash commands (chapter 10). Handlers reach the screen only through ctx.ui."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

from .appearance import KEY_HELP
from .commands import Command, CommandContext, CommandRegistry, Parsed, argument_problem
from .memory import SCOPE_LABELS
from .messages import ProviderError
from .permissions import MODE_BADGES, MODE_LABELS, MODES
from .skills import SOURCE_LABELS, ForkRequest

USAGE_WIDTH = 22

PLAN_ENTERED = ("└ 已进入计划模式：只读调查和规划（含只读命令），模型提交计划后由你批准执行。"
                "/do、Shift+Tab 或 /permissions 可手动退出。")
CLEARED = "└ 已开始新会话：屏幕、模型上下文和 Token 统计已清空，权限档位与会话授权保留。"
PERMISSION_HELP = (
    "/permissions default：读取自动通过，写入和命令默认确认\n"
    "/permissions acceptEdits：文件写入和编辑自动通过，命令默认确认\n"
    "/permissions plan：只读调查，批准计划后执行\n"
    "/permissions bypassPermissions：跳过未匹配规则的确认；硬限制、高风险确认和拒绝仍生效\n"
    "Shift+Tab 依次切换 default → acceptEdits → plan")
MODE_CHOICES = (
    ("default", "默认：读取自动通过，写入和命令确认"),
    ("acceptEdits", "接受编辑：文件写入/编辑自动通过，命令确认"),
    ("plan", "计划：只读调查，批准计划后执行"),
    ("bypassPermissions", "跳过审批：仍遵守硬限制和拒绝规则"),
)

async def dispatch(ctx: CommandContext, parsed: Parsed) -> None:
    """P7 and K1/K2: check the argument, then run the handler or send the expanded template."""
    command, argument = parsed.command, parsed.argument
    problem = argument_problem(command, argument)
    if problem:
        await ctx.ui.note(problem)
    elif command.kind == "prompt":
        await ctx.ui.submit(command.expand(argument), command=typed(command, argument))
    elif command.kind == "skill":
        await run_skill(ctx, command, argument)
    else:
        await command.handler(ctx, argument)


def typed(command: Command, argument: str) -> str:
    """The canonical form stored as Message.command: "/review 安全"."""
    return f"/{command.name}" + (f" {argument}" if argument else "")


async def run_skill(ctx: CommandContext, command: Command, argument: str) -> None:
    """E2: read the skill again, check its tools, then send the rendered SOP as the user's message."""
    try:
        skill, prompt, notices = ctx.conversation.prepare_skill(command.skill, argument)
    except ProviderError as exc:
        await ctx.ui.note(str(exc))
        return
    for notice in notices:
        await ctx.ui.note("└ " + notice)
    if skill.mode == "fork":
        await ctx.ui.note(f"└ 在独立上下文中运行 Skill {skill.name}")  # K7
        await ctx.ui.submit(prompt, command=typed(command, argument), fork=ForkRequest(skill, prompt))
        return
    await ctx.ui.submit(prompt, command=typed(command, argument))


# Help -------------------------------------------------------------------------------------

def help_text(registry: CommandRegistry, name: str = "", skills=None) -> str:
    if name:
        command = registry.get(name)
        if command is None or command.hidden:
            return f"未知命令 /{name.removeprefix('/')}。输入 /help 查看命令。"
        skill = skills.get(command.skill) if skills is not None and command.kind == "skill" else None
        if skill is not None:
            kind = f"Skill：{skill.mode}，来源{SOURCE_LABELS[skill.source]}（/skills {skill.name} 查看详情）"
        elif command.kind == "prompt":
            kind = "提示词命令：把预设的请求发给模型"
        else:
            kind = "本地命令：不经过模型"
        lines = [command.usage, command.description, kind,
                 "别名：" + ("、".join(f"/{alias}" for alias in command.aliases) or "无")]
        if command.kind == "prompt":
            lines.append("模板首行：" + command.template.strip().splitlines()[0])
        if skill is not None:
            lines.append("规程首行：" + _first_line(skill.body))
        return "\n".join(lines)
    commands = registry.visible()
    width = max([USAGE_WIDTH, *(cell_len(command.usage) + 2 for command in commands)])
    rows = [(command.usage, command.description + ("（别名 " + "、".join(f"/{alias}" for alias in command.aliases)
                                                   + "）" if command.aliases else "")) for command in commands]
    rows.append(("/help 命令", "查看该命令的用法和别名；// 开头的文字直接发给模型"))
    lines = ["命令", *(usage + " " * (width - cell_len(usage)) + description for usage, description in rows)]
    return "\n".join(lines) + "\n\n" + KEY_HELP


async def _help(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(help_text(ctx.registry, argument, ctx.conversation.skills))


# Session ----------------------------------------------------------------------------------

async def _clear(ctx: CommandContext, argument: str) -> None:
    notices = await ctx.conversation.clear()
    ctx.ui.refresh_status()
    await ctx.ui.clear_screen()
    await ctx.ui.note(CLEARED)
    for notice in notices:
        await ctx.ui.note("└ " + notice)


async def _compact(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.compact(argument)


async def _resume(ctx: CommandContext, argument: str) -> None:
    if argument:
        try:
            ctx.conversation.resolve_session(argument)
        except ProviderError as exc:
            await ctx.ui.note(str(exc))
        else:
            ctx.ui.resume(argument)
        return
    await open_sessions(ctx)


async def open_sessions(ctx: CommandContext) -> None:
    conversation = ctx.conversation
    reason = conversation.unavailable()
    if reason:
        await ctx.ui.note(reason)
        return
    current = conversation.session_id
    sessions = await asyncio.to_thread(conversation.sessions)
    if not any(item.id != current for item in sessions):
        await ctx.ui.note("本项目没有可恢复的会话。")
        return
    rows = []
    for item in sessions:
        mark = "  （当前）" if item.id == current else "  （使用中）" if item.locked else ""
        rows.append(Text(f"{item.modified:%m-%d %H:%M}  {item.title}  · {item.messages} 条 · {item.id[-4:]}{mark}",
                         no_wrap=True, overflow="ellipsis"))

    async def pick(index: int) -> None:
        ctx.ui.resume(sessions[index].id)

    ctx.ui.choose("恢复会话 · 本项目最近的会话", rows,
                  next((i for i, item in enumerate(sessions) if item.id != current), 0), pick)


def session_report(conversation, sessions=()) -> str:
    session = conversation.session
    if session is None:
        return "本次运行没有会话。"
    current = next((item for item in sessions if item.id == session.id), None)
    try:
        started = datetime.strptime(session.id[:15], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        started = "未知"
    archive = (str(session.transcript_path) if conversation.persist and session.transcript_path is not None
               else "未存档（persist_sessions: false）")
    return "\n".join([
        f"会话 {session.id}",
        f"  存档    {archive}",
        f"  标题    {current.title if current else '（尚无消息）'}",
        f"  消息    {current.messages if current else 0} 条",
        f"  开始    {started}",
        "/resume 恢复其他会话。",
    ])


async def _session(ctx: CommandContext, argument: str) -> None:
    sessions = await asyncio.to_thread(ctx.conversation.sessions)
    await ctx.ui.note(session_report(ctx.conversation, sessions))


# Memory -----------------------------------------------------------------------------------

def memory_report(conversation, name: str = "") -> str:
    store = conversation.memory
    files = conversation.instruction_files
    instructions = ["项目指令（启动、/clear 和恢复会话时加载）",
                    *(f"  {item}" for item in files or ("（未找到 AGENTS.md 或 CLAUDE.md）",))]
    if store is None:
        return "\n".join([*instructions, "记忆已关闭（memory: false）。"])
    notes = store.load()
    if name:
        matches = [note for note in notes if note.name == name]
        if not matches:
            return f"没有名为 {name} 的笔记。输入 /memory 查看全部笔记。"
        parts = []
        for note in matches:
            try:
                content = note.path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                content = "（无法读取）"
            parts.append(f"{SCOPE_LABELS[note.scope]} · {note.path}\n\n{content}")
        return "\n\n".join(parts)
    lines = list(instructions)
    for scope in ("user", "project"):
        lines.append(f"{SCOPE_LABELS[scope]}笔记 · {store.directory(scope)}")
        scoped = [note for note in notes if note.scope == scope]
        for note in scoped:
            if note.error:
                lines.append(f"  {note.path.name}  格式错误：{note.error}")
            else:
                lines.append(f"  {note.updated}  {note.type:<9} {note.name} — {note.description}")
        if not scoped:
            lines.append("  （暂无）")
    lines.append(f"自动笔记：{extraction_state(conversation)}。/memory 名称 查看全文；修改或删除可以直接编辑文件，"
                 "或让模型用 memory 工具完成。")
    return "\n".join(lines)


def extraction_state(conversation) -> str:
    extractor = conversation.extractor
    return "关闭" if extractor is None else "已暂停" if extractor.paused else "开启"


async def _memory(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(memory_report(ctx.conversation, argument))


def _note_names(ctx: CommandContext):
    store = ctx.conversation.memory
    if store is None:
        return []
    seen, names = set(), []
    for note in store.load():
        if not note.error and note.name not in seen:
            seen.add(note.name)
            names.append((note.name, f"{SCOPE_LABELS[note.scope]} · {note.description}"))
    return names


# Model and modes --------------------------------------------------------------------------

def _providers(ctx: CommandContext):
    return ctx.settings.providers if ctx.settings else ()


async def _model(ctx: CommandContext, argument: str) -> None:
    providers = _providers(ctx)
    if argument:
        if not any(item.name == argument for item in providers):
            names = "、".join(item.name for item in providers) or "无"
            await ctx.ui.note(f"未找到提供商 {argument}。可用：{names}")
        else:
            await ctx.ui.switch_model(argument)
        return
    current = ctx.ui.provider_name
    if len(providers) < 2:
        await ctx.ui.note(f"当前模型：{current}" + (f"（{ctx.ui.model}）" if ctx.ui.model else "")
                          + "。在配置文件的 providers 中添加更多提供商后可以切换。")
        return
    rows = [Text(f"{item.name}{'  ✓' if item.name == current else ''}  {item.protocol} / {item.model}",
                 no_wrap=True, overflow="ellipsis") for item in providers]

    async def pick(index: int) -> None:
        await ctx.ui.switch_model(providers[index].name)

    ctx.ui.choose("切换模型 · 对话历史保留", rows,
                  next((i for i, item in enumerate(providers) if item.name == current), 0), pick)


def _provider_names(ctx: CommandContext):
    providers = _providers(ctx)
    return [(item.name, "切换到该提供商") for item in providers] if len(providers) > 1 else []


async def _plan(ctx: CommandContext, argument: str) -> None:
    try:
        ctx.conversation.set_mode("plan")
    except ProviderError as exc:
        await ctx.ui.note(str(exc))
        return
    ctx.ui.refresh_status()
    if argument:
        await ctx.ui.submit(argument)
    else:
        await ctx.ui.note(PLAN_ENTERED)


async def _do(ctx: CommandContext, argument: str) -> None:
    """D4: leave plan mode for the mode it was entered from; Q2: a task outside plan mode is not sent."""
    registry = ctx.conversation.registry
    if registry is None:
        await ctx.ui.note("当前会话未启用工具。")
        return
    if registry.permissions.mode != "plan":
        await ctx.ui.note("当前不在计划模式。" + ("任务未发送。" if argument else ""))
        return
    ctx.conversation.set_mode("normal")
    mode = registry.permissions.mode
    ctx.ui.refresh_status()
    await ctx.ui.note(f"└ 已退出计划模式，回到{MODE_LABELS[mode]}（{mode}）。")
    if argument:
        await ctx.ui.submit(argument)


async def _permissions(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    if not conversation.registry:
        await ctx.ui.note("当前会话未启用工具。")
    elif argument in MODES:
        conversation.set_permission_mode(argument)
        ctx.ui.refresh_status()
        mode = conversation.registry.permissions.mode
        await ctx.ui.note("└ 权限档位：" + MODE_LABELS[mode] + f"（{mode}）；硬限制和显式拒绝仍生效。")
    else:
        await ctx.ui.note(PERMISSION_HELP)


# Status -----------------------------------------------------------------------------------

def _model_line(ctx: CommandContext) -> str:
    name, model = ctx.ui.provider_name, ctx.ui.model
    config = next((item for item in _providers(ctx) if item.name == name), None)
    protocol = config.protocol if config else ctx.conversation.provider_info[1] if ctx.conversation.provider_info else ""
    detail = " / ".join(part for part in (protocol, model) if part)
    return name + (f"（{detail}）" if detail else "")


def _context_line(conversation) -> str:
    usage = conversation.total_usage
    estimate = conversation.context_estimate() if usage.requests else None
    if estimate is None:
        return "尚未发送请求"
    budget = conversation.budget
    line = (f"约 {estimate:,} / {budget.effective:,} Token（{round(100 * estimate / budget.effective)}%），"
            f"自动压缩阈值 {budget.auto_threshold:,}；已压缩 {conversation.compactions} 次")
    return line + ("；未配置 context_window，按 128K 估算" if not budget.configured else "")


def _usage_line(conversation) -> str:
    usage = conversation.total_usage
    if not usage.requests:
        return "尚未发送请求"
    return (f"输入 {usage.input_tokens:,}（缓存命中 {usage.cached_input_tokens:,}）· 输出 {usage.output_tokens:,}"
            f" · 请求 {usage.requests} 次")


def _mcp_line(conversation) -> str:
    manager = conversation.mcp_manager
    if manager is None or not (manager.connections or manager.notices):
        return "未配置"
    states = [status.state for status in manager.statuses()]
    ready = states.count("ready")
    waiting = sum(state in {"new", "connecting"} for state in states)
    failed = len(states) - ready - waiting
    parts = [f"{ready} 个已连接"] + ([f"{waiting} 个连接中"] if waiting else []) \
        + ([f"{failed} 个失败"] if failed else []) + ([f"{len(manager.notices)} 条配置问题"] if manager.notices else [])
    return "，".join(parts) + "（/mcp 查看）"


def status_report(ctx: CommandContext) -> str:
    conversation = ctx.conversation
    registry = conversation.registry
    if registry is None:
        mode = "未启用工具"
    else:
        current = registry.permissions.mode
        mode = f"{MODE_BADGES[current]}（{current}）"
    files = conversation.instruction_files
    memory = ("关闭（memory: false）" if conversation.memory is None
              else f"开启，自动笔记{extraction_state(conversation)}")
    rows = [
        ("模型", _model_line(ctx)),
        ("权限档位", mode),
        ("上下文", _context_line(conversation)),
        ("本会话用量", _usage_line(conversation)),
        ("会话", conversation.session_id or "无"),
        ("工作目录", str(conversation.context_root or Path.cwd())),
        ("项目指令", f"{len(files)} 个文件（/memory 查看）" if files else "未找到"),
        ("MCP", _mcp_line(conversation)),
        ("记忆", memory),
    ]
    if conversation.active_skills:
        rows.append(("已激活 Skill", "、".join(conversation.active_skills)))
    width = max(12, *(cell_len(label) + 2 for label, _ in rows))
    return "状态\n" + "\n".join(f"  {label}{' ' * (width - cell_len(label))}{value}" for label, value in rows)


async def _status(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(status_report(ctx))


# MCP --------------------------------------------------------------------------------------

def mcp_report(conversation, name: str = "") -> str:
    manager = conversation.mcp_manager
    if manager is None or not (manager.connections or manager.notices):
        return ("未配置 MCP Server。在 ~/.mosscode/mcp.yaml 或项目 .mosscode/mcp.yaml 的 mcp_servers 中添加；"
                "设置 import_mcp_json: true 可读取项目的 .mcp.json。")
    statuses = manager.statuses()
    if name:
        status = next((item for item in statuses if item.name == name), None)
        if status is None:
            return f"未找到 MCP Server {name}。已配置：" + ("、".join(item.name for item in statuses) or "无")
        lines = [f"MCP {status.name}（{status.type}）· {status.describe()}"]
        for tool in status.tools:
            summary = (tool.description.removeprefix(f"MCP Server {tool.server_name} 的工具 {tool.remote_name}。")
                       .strip().splitlines() or [""])[0]
            lines.append(f"  {tool.name}" + ("  [只读]" if tool.read_only else "") + (f"  {summary[:80]}" if summary else ""))
        if status.state in {"ready", "closed"} and not status.tools:
            lines.append("  （Server 没有提供工具）")
        return "\n".join(lines)
    marks = {"ready": "●", "connecting": "○", "new": "○"}
    lines = [f"MCP Server（{len(statuses)}）"]
    width = max((len(item.name) for item in statuses), default=0)
    lines.extend(f"{marks.get(item.state, '×')} {item.name.ljust(width)}  {item.type:<5}  {item.describe()}" for item in statuses)
    lines.extend("! " + notice for notice in manager.notices)
    lines.append("/mcp 名称 查看工具；修改配置后重新启动生效，不自动重连。")
    return "\n".join(lines)


async def _mcp(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(mcp_report(ctx.conversation, argument))


def _server_names(ctx: CommandContext):
    manager = ctx.conversation.mcp_manager
    if manager is None:
        return []
    return [(connection.config.name, "查看该 Server 的工具") for connection in manager.connections]


# Skills -----------------------------------------------------------------------------------

def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "（空）")


def skills_report(catalog, name: str = "", active=()) -> str:
    """C1/C2."""
    if name:
        skill = catalog.get(name)
        if skill is None:
            names = "、".join(catalog.skills) or "无"
            return f"未找到 Skill {name}。已加载：{names}"
        rows = [
            ("路径", str(skill.path)),
            ("来源", SOURCE_LABELS[skill.source]),
            ("覆盖", "、".join(str(path) for path in skill.overrides) or "无"),
            ("模式", skill.mode + (f"（fork_context: {skill.fork_context}）" if skill.mode == "fork" else "")),
            ("模型", skill.model or "当前模型"),
            ("工具白名单", "、".join(skill.allowed_tools) or "未限制"),
            ("模型可见", "是" if skill.model_invocable else "否（disable-model-invocation）"),
            ("已激活", "是" if skill.name in active else "否"),
            ("命令", f"/{skill.name}" if skill.user_invocable else "不列出（user-invocable: false），仍可输入调用"),
            ("规程首行", _first_line(skill.body)),
        ]
        lines = [f"Skill {skill.name}", *(f"  {label}{' ' * (12 - cell_len(label))}{value}" for label, value in rows)]
        if skill.tools:
            lines.append("  专属工具")
            for tool in skill.tools:
                lines.append(f"    {tool.full_name}  {' '.join(tool.command)}  超时 {tool.timeout:g} 秒")
        return "\n".join(lines)
    if not catalog.skills and not catalog.problems:
        return "没有 Skill。可以在 .mosscode/skills/<名称>/SKILL.md 中添加。"
    lines = [f"Skill（{len(catalog.skills)}）"]
    width = max((cell_len(f"/{item}") for item in catalog.skills), default=0)
    for skill in catalog.skills.values():
        label = f"/{skill.name}"
        lines.append(f"{label}{' ' * (width - cell_len(label))}  {SOURCE_LABELS[skill.source]}  {skill.mode:<6}  "
                     f"{skill.description}")
    lines.extend("! " + problem for problem in catalog.problems)
    lines.append("输入 /<名称> 调用；/skills 名称 查看详情；/skills reload 重新扫描。")
    return "\n".join(lines)


async def _skills(ctx: CommandContext, argument: str) -> None:
    if argument != "reload":
        await ctx.ui.note(skills_report(ctx.conversation.skills, argument, ctx.conversation.active_skills))
        return
    old = ctx.conversation.skills
    catalog = await asyncio.to_thread(old.reload)
    problems = ctx.registry.set_skills(catalog)
    notices = ctx.conversation.use_skills(catalog)
    added = [name for name in catalog.skills if name not in old.skills]
    removed = [name for name in old.skills if name not in catalog.skills]
    lines = [f"└ 已重新扫描 Skill：共 {len(catalog.skills)} 个"
             + (f"，新增 {'、'.join(added)}" if added else "") + (f"，移除 {'、'.join(removed)}" if removed else "")
             + "。"]
    lines.extend("  ! " + problem for problem in (*catalog.problems, *problems, *notices))
    await ctx.ui.note("\n".join(lines))


def _skill_names(ctx: CommandContext):
    return [("reload", "重新扫描 Skill 目录"),
            *((skill.name, skill.description) for skill in ctx.conversation.skills.skills.values())]


async def _exit(ctx: CommandContext, argument: str) -> None:
    ctx.ui.exit()


BUILTIN = CommandRegistry((
    Command("help", "查看命令和快捷键", "/help [命令]", "local", argument="optional", argument_hint="[命令]",
            handler=_help),
    Command("clear", "开始新会话：清空模型上下文和 Token 统计", "/clear", "local", handler=_clear),
    Command("compact", "压缩上下文：较早的对话换成摘要，近期消息保留", "/compact [说明]", "local",
            argument="optional", argument_hint="[说明]", handler=_compact),
    Command("resume", "恢复本项目之前的会话", "/resume [ID]", "local", argument="optional", argument_hint="[ID]",
            handler=_resume),
    Command("session", "查看当前会话的 ID、存档位置和消息数", "/session", "local", handler=_session),
    Command("memory", "查看已加载的项目指令和记忆笔记", "/memory [名称]", "local", argument="optional",
            argument_hint="[名称]", handler=_memory, complete=_note_names),
    Command("model", "切换提供商与模型，对话历史保留", "/model [名称]", "local", argument="optional",
            argument_hint="[名称]", handler=_model, complete=_provider_names),
    Command("plan", "只读调查和规划，提交计划经批准后执行", "/plan [任务]", "local",
            argument="optional", argument_hint="[任务]", handler=_plan),
    Command("do", "退出计划模式，回到进入前的权限档位", "/do [任务]", "local", argument="optional",
            argument_hint="[任务]", handler=_do),
    Command("permissions", "查看或切换工具权限档位", "/permissions [档位]", "local", aliases=("permission",),
            argument="optional", argument_hint="[档位]", handler=_permissions, complete=lambda ctx: MODE_CHOICES),
    Command("status", "查看模型、权限、上下文、用量和连接状态", "/status", "local", handler=_status),
    Command("mcp", "查看 MCP Server 的连接状态与工具", "/mcp [名称]", "local", argument="optional",
            argument_hint="[名称]", handler=_mcp, complete=_server_names),
    Command("skills", "查看、重新扫描 Skill", "/skills [名称|reload]", "local", aliases=("skill",),
            argument="optional", argument_hint="[名称|reload]", handler=_skills, complete=_skill_names),
    Command("exit", "退出 MossCode", "/exit", "local", aliases=("quit",), handler=_exit),
))
