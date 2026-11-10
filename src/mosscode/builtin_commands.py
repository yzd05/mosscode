"""The built-in slash commands. Handlers reach the screen only through ctx.ui."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

from .agents import claude_model
from .agents.runtime import GLOBAL_BAN, WORKSPACE_TOOLS, effective_mode, stats
from .agents.tasks import ORIGIN_LABELS, STATUS_LABELS
from .appearance import KEY_HELP
from .commands import Command, CommandContext, CommandRegistry, Parsed, argument_problem
from .goal import GOAL, MAX_CONDITION, continue_prompt, elapsed_text
from .hooks.config import SOURCE_LABELS as HOOK_SOURCES
from .memory import SCOPE_LABELS
from .messages import ProviderError
from .permissions import MODE_BADGES, MODE_LABELS, MODES
from .reasoning import DEFAULT, LEVEL_LABELS, LEVELS, Unsupported, thinking_options
from .skills import ForkRequest, source_label
from .todo import render as render_todos
from .usage import approx_tokens, cost_text

USAGE_WIDTH = 22

PLAN_ENTERED = (
    "└ 已进入计划模式：只读调查和规划（含只读命令），模型提交计划后由你批准执行。"
    "/do、Shift+Tab 或 /permissions 可手动退出。"
)
CLEARED = "└ 已开始新会话：屏幕、模型上下文和 Token 统计已清空，权限档位与会话授权保留。"
PERMISSION_HELP = (
    "/permissions default：读取自动通过，写入和命令默认确认\n"
    "/permissions acceptEdits：文件写入和编辑自动通过，命令默认确认\n"
    "/permissions plan：只读调查，批准计划后执行\n"
    "/permissions bypassPermissions：跳过未匹配规则的确认；硬限制、高风险确认和拒绝仍生效\n"
    "Shift+Tab 依次切换 default → acceptEdits → plan"
)
MODE_CHOICES = (
    ("default", "默认：读取自动通过，写入和命令确认"),
    ("acceptEdits", "接受编辑：文件写入/编辑自动通过，命令确认"),
    ("plan", "计划：只读调查，批准计划后执行"),
    ("bypassPermissions", "跳过审批：仍遵守硬限制和拒绝规则"),
)


async def dispatch(ctx: CommandContext, parsed: Parsed) -> None:
    """Check the argument, then run the handler or send the expanded template."""
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
    """Read the skill again, check its tools, then send the rendered SOP as the user's message."""
    try:
        skill, prompt, notices = ctx.conversation.prepare_skill(command.skill, argument)
    except ProviderError as exc:
        await ctx.ui.note(str(exc))
        return
    for notice in notices:
        await ctx.ui.note("└ " + notice)
    if skill.mode == "fork":
        await ctx.ui.note(f"└ 在独立上下文中运行 Skill {skill.name}")
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
            kind = f"Skill：{skill.mode}，来源{source_label(skill)}（/skills {skill.name} 查看详情）"
        elif command.kind == "prompt":
            kind = "提示词命令：把预设的请求发给模型"
        else:
            kind = "本地命令：不经过模型"
        lines = [
            command.usage,
            command.description,
            kind,
            "别名：" + ("、".join(f"/{alias}" for alias in command.aliases) or "无"),
        ]
        if command.details:
            lines.append(command.details)
        if command.kind == "prompt":
            lines.append("模板首行：" + command.template.strip().splitlines()[0])
        if skill is not None:
            lines.append("规程首行：" + _first_line(skill.body))
        return "\n".join(lines)
    commands = registry.visible()
    width = max([USAGE_WIDTH, *(cell_len(command.usage) + 2 for command in commands)])
    rows = [
        (
            command.usage,
            command.description
            + ("（别名 " + "、".join(f"/{alias}" for alias in command.aliases) + "）" if command.aliases else ""),
        )
        for command in commands
    ]
    rows.append(("/help 命令", "查看该命令的用法和别名；// 开头的文字直接发给模型"))
    lines = ["命令", *(usage + " " * (width - cell_len(usage)) + description for usage, description in rows)]
    return "\n".join(lines) + "\n\n" + KEY_HELP


async def _help(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(help_text(ctx.registry, argument, ctx.conversation.skills))


# Session ----------------------------------------------------------------------------------

TASK_OPTIONS = ("取消这些任务并继续", "返回")


async def confirm_tasks(ui, conversation, proceed) -> None:
    """Running background tasks are cancelled only once the user says so; Esc means 返回."""
    tasks = getattr(conversation, "tasks", None)
    running = tasks.background() if tasks is not None else []
    if not running:
        await proceed()
        return

    async def pick(index: int) -> None:
        if index == 0:
            count = await conversation.cancel_tasks()
            await ui.note(f"└ 已取消 {count} 个后台任务。")
            await proceed()

    labels = "、".join(f"{task.id}（{task.label}）" for task in running[:3]) + ("等" if len(running) > 3 else "")
    ui.choose(f"有 {len(running)} 个后台任务在运行：{labels}", TASK_OPTIONS, 1, pick)


async def _clear(ctx: CommandContext, argument: str) -> None:
    await confirm_tasks(ctx.ui, ctx.conversation, lambda: _do_clear(ctx))


async def _do_clear(ctx: CommandContext) -> None:
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
            await confirm_tasks(ctx.ui, ctx.conversation, lambda: _resume_now(ctx, argument))
        return
    await open_sessions(ctx)


async def _resume_now(ctx: CommandContext, session_id: str) -> None:
    ctx.ui.resume(session_id)


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
        rows.append(
            Text(
                f"{item.modified:%m-%d %H:%M}  {item.title}  · {item.messages} 条 · {item.id[-4:]}{mark}",
                no_wrap=True,
                overflow="ellipsis",
            )
        )

    async def pick(index: int) -> None:
        await confirm_tasks(ctx.ui, conversation, lambda: _resume_now(ctx, sessions[index].id))

    ctx.ui.choose(
        "恢复会话 · 本项目最近的会话", rows, next((i for i, item in enumerate(sessions) if item.id != current), 0), pick
    )


# (label, restore code, rewind the conversation); a message with no file changes offers the conversation only.
REWIND_ACTIONS = (("恢复代码和对话", True, True), ("只恢复对话", False, True), ("只恢复代码", True, False))
REWIND_TALK = (("只恢复对话", False, True),)
REWIND_LISTED = 10


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "（空消息）")


def _listed(paths) -> str:
    names = "、".join(paths[:REWIND_LISTED])
    return names + (f" 等 {len(paths)} 个" if len(paths) > REWIND_LISTED else "")


def rewind_report(rewound) -> str:
    lines = []
    restoration = rewound.restoration
    if restoration is not None:
        if restoration.restored:
            lines.append(f"└ 已恢复 {len(restoration.restored)} 个文件：{_listed(restoration.restored)}")
        if restoration.deleted:
            lines.append(f"└ 已删除 {len(restoration.deleted)} 个之后新建的文件：{_listed(restoration.deleted)}")
        lines += [f"└ 跳过 {path}：{why}" for path, why in restoration.skipped]
        lines += [f"! 未能恢复 {path}：{why}" for path, why in restoration.failed]
        if not (restoration.paths or restoration.skipped or restoration.failed):
            lines.append("└ 文件已经是那条消息之前的状态。")
    if rewound.kept is not None:
        lines.append("└ 对话已退回到这条消息之前，原消息已放回输入框。")
    elif restoration is not None and restoration.paths:
        lines.append("└ 对话保留；下一次请求会提醒模型这些文件已恢复，需要重新读取。")
    return "\n".join(lines)


async def _rewind(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    points = conversation.rewind_points()
    if not points:
        await ctx.ui.note("当前上下文中没有可以回退的消息。")
        return
    rows = [
        Text(
            _first_line(point.message.shown) + (f"  · 之后改动 {point.files} 个文件" if point.files else ""),
            no_wrap=True,
            overflow="ellipsis",
        )
        for point in points
    ]

    async def pick(index: int) -> None:
        point = points[index]
        actions = REWIND_ACTIONS if point.files else REWIND_TALK

        async def act(choice: int) -> None:
            if choice >= len(actions):
                return  # 取消
            _, code, talk = actions[choice]
            try:
                rewound = await conversation.rewind(point.message.id, code=code, conversation=talk)
            except ProviderError as exc:
                await ctx.ui.note(f"! {exc}")
                return
            if rewound.message is not None:
                await ctx.ui.rewound(rewound.message)
            ctx.ui.refresh_status()
            await ctx.ui.note(rewind_report(rewound))

        title = _first_line(point.message.shown)
        ctx.ui.choose(f"回退到“{title[:40]}”之前", [label for label, _, _ in actions] + ["取消"], 0, act)

    ctx.ui.choose("回退 · 选择回到哪条消息之前（最新的在最上面）", rows, 0, pick)


def session_report(conversation, sessions=()) -> str:
    session = conversation.session
    if session is None:
        return "本次运行没有会话。"
    current = next((item for item in sessions if item.id == session.id), None)
    try:
        started = datetime.strptime(session.id[:15], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        started = "未知"
    archive = (
        str(session.transcript_path)
        if conversation.persist and session.transcript_path is not None
        else "未存档（persist_sessions: false）"
    )
    return "\n".join(
        [
            f"会话 {session.id}",
            f"  存档    {archive}",
            f"  标题    {current.title if current else '（尚无消息）'}",
            f"  消息    {current.messages if current else 0} 条",
            f"  开始    {started}",
            "/resume 恢复其他会话。",
        ]
    )


async def _session(ctx: CommandContext, argument: str) -> None:
    sessions = await asyncio.to_thread(ctx.conversation.sessions)
    await ctx.ui.note(session_report(ctx.conversation, sessions))


# Memory -----------------------------------------------------------------------------------


def memory_report(conversation, name: str = "") -> str:
    store = conversation.memory
    files = conversation.instruction_files
    instructions = [
        "项目指令（启动、/clear 和恢复会话时加载）",
        *(f"  {item}" for item in files or ("（未找到 AGENTS.md 或 CLAUDE.md）",)),
    ]
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
    lines.append(
        f"自动笔记：{extraction_state(conversation)}。/memory 名称 查看全文；修改或删除可以直接编辑文件，"
        "或让模型用 memory 工具完成。"
    )
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
        await ctx.ui.note(
            f"当前模型：{current}"
            + (f"（{ctx.ui.model}）" if ctx.ui.model else "")
            + "。在配置文件的 providers 中添加更多提供商后可以切换。"
        )
        return
    rows = [
        Text(
            f"{item.name}{'  ✓' if item.name == current else ''}  {item.protocol} / {item.model}",
            no_wrap=True,
            overflow="ellipsis",
        )
        for item in providers
    ]

    async def pick(index: int) -> None:
        await ctx.ui.switch_model(providers[index].name)

    ctx.ui.choose(
        "切换模型 · 对话历史保留", rows, next((i for i, item in enumerate(providers) if item.name == current), 0), pick
    )


def _provider_names(ctx: CommandContext):
    providers = _providers(ctx)
    return [(item.name, "切换到该提供商") for item in providers] if len(providers) > 1 else []


THINKING_CHOICES = (DEFAULT, *LEVELS)
THINKING_HELP = "/thinking off|low|medium|high 设置本会话的思考强度；/thinking default 回到配置文件的设置。"


def thinking_detail(config, level: str | None) -> str:
    """What the current model is sent for level, or why it cannot take it."""
    if level is None:
        return "不发送覆盖参数，按配置文件"
    if config is None:
        return "当前模型不支持：无法确定当前模型"
    try:
        result = thinking_options(config, level)
    except Unsupported as exc:
        return f"当前模型不支持：{exc}"
    return f"发送 {result.summary}" + (f"（{result.note}）" if result.note else "")


async def _set_thinking(ctx: CommandContext, value: str) -> None:
    level = None if value == DEFAULT else value
    try:
        ctx.conversation.set_thinking(level)
    except ProviderError as exc:
        await ctx.ui.note(str(exc))
        return
    ctx.ui.refresh_status()
    if level is None:
        await ctx.ui.note("└ 思考强度：跟随配置，不发送覆盖参数。")
        return
    config = ctx.conversation.provider_config
    detail = thinking_detail(config, level)
    if ctx.conversation.thinking_problem() is not None:
        await ctx.ui.note(
            f"└ 思考强度：{level}；{ctx.ui.provider_name} {detail}，这个模型的请求按配置发送，切换到支持的模型后生效。"
        )
    else:
        await ctx.ui.note(f"└ 思考强度：{level}；{ctx.ui.provider_name} {detail}。")


async def _thinking(ctx: CommandContext, argument: str) -> None:
    value = argument.strip().lower()
    if value:
        if value not in THINKING_CHOICES:
            await ctx.ui.note(f"未知的思考强度 {argument}。" + THINKING_HELP)
        else:
            await _set_thinking(ctx, value)
        return
    current = ctx.conversation.thinking or DEFAULT
    config = ctx.conversation.provider_config
    rows = [
        Text(
            f"{value}{'  ✓' if value == current else ''}  {LEVEL_LABELS[value]} · "
            + thinking_detail(config, None if value == DEFAULT else value),
            no_wrap=True,
            overflow="ellipsis",
        )
        for value in THINKING_CHOICES
    ]

    async def pick(index: int) -> None:
        await _set_thinking(ctx, THINKING_CHOICES[index])

    ctx.ui.choose(f"思考强度 · {ctx.ui.provider_name}", rows, THINKING_CHOICES.index(current), pick)


def _thinking_line(ctx: CommandContext) -> str:
    conversation = ctx.conversation
    level = conversation.thinking
    if level is None:
        return LEVEL_LABELS[DEFAULT]
    return f"{level}（{thinking_detail(conversation.provider_config, level)}）"


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
    """Leave plan mode for the mode it was entered from; A task outside plan mode is not sent."""
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


# Sandbox ----------------------------------------------------------------------------------

SANDBOX_OFF_OPTIONS = ("关闭沙箱", "返回")


async def _sandbox(ctx: CommandContext, argument: str) -> None:
    from .sandbox import current

    sandbox = current()
    root = ctx.conversation.registry.permissions.root if ctx.conversation.registry else Path.cwd()
    if argument == "on":
        if not sandbox.started:
            await ctx.ui.note(
                "└ 本会话启动时沙箱是关闭的，命令工具没有按沙箱构建（Windows 上沙箱中的命令改用 PowerShell）。"
                "请用 --sandbox 重新启动，或在 config.yaml 中设 sandbox.mode: on。"
            )
            return
        sandbox.set_enabled(True)
        ctx.ui.refresh_status()
        await ctx.ui.note("\n".join("└ " + line if i == 0 else line for i, line in enumerate(sandbox.report(root))))
        return
    if argument == "off" and sandbox.enabled:

        async def pick(index: int) -> None:
            if index == 0:
                sandbox.set_enabled(False)
                ctx.ui.refresh_status()
                await ctx.ui.note(
                    "└ 已在本会话关闭沙箱：命令以你的完整权限运行，按权限档位审批。/sandbox on 重新打开。"
                )

        ctx.ui.choose(
            "关闭沙箱后，模型的命令以你的完整权限运行，按权限档位审批。确定关闭？", SANDBOX_OFF_OPTIONS, 1, pick
        )
        return
    await ctx.ui.note("\n".join(sandbox.report(root)))


# Status -----------------------------------------------------------------------------------


CATALOG_FIELDS = {"context_window": "窗口", "max_output_tokens": "输出上限", "images": "图片能力"}


def _model_line(ctx: CommandContext) -> str:
    name, model = ctx.ui.provider_name, ctx.ui.model
    config = next((item for item in _providers(ctx) if item.name == name), None)
    protocol = (
        config.protocol if config else ctx.conversation.provider_info[1] if ctx.conversation.provider_info else ""
    )
    detail = " / ".join(part for part in (protocol, model) if part)
    line = name + (f"（{detail}）" if detail else "")
    if config is not None and config.catalog is not None and config.from_catalog:
        filled = "、".join(label for field, label in CATALOG_FIELDS.items() if field in config.from_catalog)
        line += f"；{filled}来自模型目录（{config.catalog.name}）"
    return line


def _context_line(conversation) -> str:
    usage = conversation.total_usage
    estimate = conversation.context_estimate() if usage.requests else None
    if estimate is None:
        return "尚未发送请求"
    budget = conversation.budget
    line = (
        f"约 {estimate:,} / {budget.effective:,} Token（{round(100 * estimate / budget.effective)}%），"
        f"自动压缩阈值 {budget.auto_threshold:,}；已压缩 {conversation.compactions} 次"
    )
    return line + ("；窗口未知，按 128K 估算" if not budget.configured else "")


def _usage_line(conversation) -> str:
    usage = conversation.total_usage
    if not usage.requests:
        return "尚未发送请求"
    return (
        f"输入 {usage.input_tokens:,}（缓存命中 {usage.cached_input_tokens:,}）· 输出 {usage.output_tokens:,}"
        f" · 请求 {usage.requests} 次 · 费用 {cost_text(usage)}"
    )


def _mcp_line(conversation) -> str:
    manager = conversation.mcp_manager
    if manager is None or not (manager.connections or manager.notices):
        return "未配置"
    states = [status.state for status in manager.statuses()]
    ready = states.count("ready")
    waiting = sum(state in {"new", "connecting"} for state in states)
    failed = len(states) - ready - waiting
    parts = (
        [f"{ready} 个已连接"]
        + ([f"{waiting} 个连接中"] if waiting else [])
        + ([f"{failed} 个失败"] if failed else [])
        + ([f"{len(manager.notices)} 条配置问题"] if manager.notices else [])
    )
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
    memory = (
        "关闭（memory: false）" if conversation.memory is None else f"开启，自动笔记{extraction_state(conversation)}"
    )
    rows = [
        ("模型", _model_line(ctx)),
        ("权限档位", mode),
        ("思考", _thinking_line(ctx)),
        ("上下文", _context_line(conversation)),
        ("本会话用量", _usage_line(conversation)),
        ("会话", conversation.session_id or "无"),
        ("工作目录", str(conversation.context_root or Path.cwd())),
        ("项目指令", f"{len(files)} 个文件（/memory 查看）" if files else "未找到"),
        ("MCP", _mcp_line(conversation)),
        ("网络", _web_line(registry)),
        ("图片", _image_line(ctx)),
        ("记忆", memory),
    ]
    if conversation.hooks is not None:
        rows.append(("Hook", hooks_line(conversation.hooks)))
    if conversation.active_skills:
        rows.append(("已激活 Skill", "、".join(conversation.active_skills)))
    if registry is not None and getattr(conversation, "agents_enabled", False):
        rows.append(("子 Agent", agents_line(conversation)))
    if getattr(conversation, "coordinator", False):
        from .coordinator import STATUS

        rows.append(("角色", STATUS))
    width = max(12, *(cell_len(label) + 2 for label, _ in rows))
    return "状态\n" + "\n".join(f"  {label}{' ' * (width - cell_len(label))}{value}" for label, value in rows)


def _image_line(ctx: CommandContext) -> str:
    accepts = getattr(getattr(ctx.conversation.provider, "config", None), "accepts_images", True)
    if accepts:
        return "可发送（Alt+V 或 Ctrl+V 粘贴，拖入图片文件，或让模型用 read_file 读取）"
    return f"当前模型不支持（providers.{ctx.ui.provider_name}.images: false）"


def _web_line(registry) -> str:
    if registry is None or registry.get("web_fetch") is None:
        return "未启用"
    search = registry.get("web_search")
    if search is None:
        return "读取网页可用；搜索未配置（配置文件中的 web_search）"
    return f"读取网页、搜索（{search.backend.name}）可用"


async def _status(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(status_report(ctx))


# MCP --------------------------------------------------------------------------------------


def mcp_report(conversation, name: str = "") -> str:
    manager = conversation.mcp_manager
    if manager is None or not (manager.connections or manager.notices):
        return (
            "未配置 MCP Server。在 ~/.mosscode/mcp.yaml 或项目 .mosscode/mcp.yaml 的 mcp_servers 中添加；"
            "设置 import_mcp_json: true 可读取项目的 .mcp.json。"
        )
    statuses = manager.statuses()
    if name:
        status = next((item for item in statuses if item.name == name), None)
        if status is None:
            return f"未找到 MCP Server {name}。已配置：" + ("、".join(item.name for item in statuses) or "无")
        lines = [f"MCP {status.name}（{status.type}）· {status.describe()}"]
        for tool in status.tools:
            summary = (
                tool.description.removeprefix(f"MCP Server {tool.server_name} 的工具 {tool.remote_name}。")
                .strip()
                .splitlines()
                or [""]
            )[0]
            lines.append(
                f"  {tool.name}" + ("  [只读]" if tool.read_only else "") + (f"  {summary[:80]}" if summary else "")
            )
        if status.state in {"ready", "closed"} and not status.tools:
            lines.append("  （Server 没有提供工具）")
        return "\n".join(lines)
    marks = {"ready": "●", "connecting": "○", "new": "○"}
    lines = [f"MCP Server（{len(statuses)}）"]
    width = max((len(item.name) for item in statuses), default=0)
    lines.extend(
        f"{marks.get(item.state, '×')} {item.name.ljust(width)}  {item.type:<5}  {item.describe()}" for item in statuses
    )
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
    if name:
        skill = catalog.get(name)
        if skill is None:
            names = "、".join(catalog.skills) or "无"
            return f"未找到 Skill {name}。已加载：{names}"
        rows = [
            ("路径", str(skill.path)),
            ("来源", source_label(skill)),
            ("覆盖", "、".join(str(path) for path in skill.overrides) or "无"),
            ("模式", skill.mode + (f"（fork_context: {skill.fork_context}）" if skill.mode == "fork" else "")),
            ("模型", skill.model or "当前模型"),
            ("工具白名单", "、".join(skill.allowed_tools) or "未限制"),
            ("模型可见", "是" if skill.model_invocable else "否（disable-model-invocation）"),
            ("已激活", "是" if skill.name in active else "否"),
            ("命令", f"/{skill.name}" if skill.user_invocable else "不列出（user-invocable: false），仍可输入调用"),
            ("规程首行", _first_line(skill.body)),
        ]
        rows += [
            (label, value) for label, value in (("许可证", skill.license), ("兼容性", skill.compatibility)) if value
        ]
        lines = [f"Skill {skill.name}", *(f"  {label}{' ' * (12 - cell_len(label))}{value}" for label, value in rows)]
        if skill.tools:
            lines.append("  专属工具")
            for tool in skill.tools:
                lines.append(f"    {tool.full_name}  {' '.join(tool.command)}  超时 {tool.timeout:g} 秒")
        if skill.warnings:
            lines.append("  写法提示（不影响使用）")
            lines.extend(f"    {warning}" for warning in skill.warnings)
        return "\n".join(lines)
    if not catalog.skills and not catalog.problems:
        return "没有 Skill。可以在 .mosscode/skills/<名称>/SKILL.md 中添加。"
    lines = [f"Skill（{len(catalog.skills)}）"]
    width = max((cell_len(f"/{item}") for item in catalog.skills), default=0)
    for skill in catalog.skills.values():
        label = f"/{skill.name}"
        lines.append(
            f"{label}{' ' * (width - cell_len(label))}  {source_label(skill)}  {skill.mode:<6}  {skill.description}"
        )
    lines.extend("! " + problem for problem in catalog.problems)
    noted = [name for name, skill in catalog.skills.items() if skill.warnings]
    if noted:
        lines.append(f"{'、'.join(noted)} 有不符合 Agent Skills 规范的写法，不影响使用；/skills 名称 查看。")
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
    lines = [
        f"└ 已重新扫描 Skill：共 {len(catalog.skills)} 个"
        + (f"，新增 {'、'.join(added)}" if added else "")
        + (f"，移除 {'、'.join(removed)}" if removed else "")
        + "。"
    ]
    lines.extend("  ! " + problem for problem in (*catalog.problems, *problems, *notices))
    await ctx.ui.note("\n".join(lines))


def _skill_names(ctx: CommandContext):
    return [
        ("reload", "重新扫描 Skill 目录"),
        *((skill.name, skill.description) for skill in ctx.conversation.skills.skills.values()),
    ]


# Hooks ------------------------------------------------------------------------------

TRUST_OPTIONS = ("信任并启用", "本次不启用")
OUTCOME_LABELS = {
    "ok": "成功",
    "failed": "失败",
    "rejected": "拒绝",
    "timeout": "超时",
    "cancelled": "已取消",
    "skipped": "跳过",
}


def hooks_line(engine) -> str:
    if not engine.enabled:
        return "已关闭（--no-hooks）"
    waiting = sum(len(file.hooks) for file in engine.untrusted)
    return f"已启用 {len(engine.hooks)} 条" + (f"（项目级未确认 {waiting} 条）" if waiting else "") + "（/hooks 查看）"


ACTION_LABELS = {"command": "命令", "http": "请求", "prompt": "注入", "agent": "子 Agent"}


def _shown(path: Path, root: Path) -> str:
    """Hook files as the user knows them: relative inside the project, ~ under home."""
    for base, prefix in ((root, ""), (Path.home(), "~")):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        return str(Path(prefix, relative)) if prefix else str(relative)
    return str(path)


def _hook_lines(hook, head: str = "") -> list[str]:
    """One line for id and event, then the condition and the action indented below, so long ones wrap legibly."""
    lines = [f"  {hook.id} · {hook.event}{head}"]
    if hook.condition is not None:
        lines.append(f"      条件  {hook.condition.text}")
    action = hook.action
    if action is None:
        lines.append(f"      拒绝  {hook.reject.replace(chr(10), ' ')}")
    elif action.type in {"prompt", "agent"}:
        text = action.message if action.type == "prompt" else action.prompt
        lines.append(f"      {ACTION_LABELS[action.type]}  {text.replace(chr(10), ' ')[:60]}")
    else:
        lines.append(f"      {ACTION_LABELS[action.type]}  {action.summary()}")
    return lines


def _hook_row(engine, hook) -> list[str]:
    flags = "".join(f" [{flag}]" for flag in hook.flags())
    return _hook_lines(hook, f"{flags} · {engine.status(hook)}")


def _run_row(run) -> str:
    detail = f"  {run.detail.splitlines()[0][:80]}" if run.detail.strip() else ""
    return (
        f"  {run.time[11:19]}  {run.hook}  {run.event}  {OUTCOME_LABELS.get(run.outcome, run.outcome)}"
        f"  {run.duration_ms} ms{detail}"
    )


def _masked(raw: dict) -> dict:
    """Header values are secrets more often than not."""
    action = raw.get("action")
    if isinstance(action, dict) and isinstance(action.get("headers"), dict):
        raw = {**raw, "action": {**action, "headers": {key: "***" for key in action["headers"]}}}
    return raw


def hooks_report(engine, name: str = "") -> str:
    import yaml

    if engine is None or not engine.enabled:
        return "Hook 已关闭（本次运行使用了 --no-hooks）。"
    if name:
        hook = engine.find(name)
        if hook is None:
            known = "、".join(h.id for f in engine.config.files for h in f.hooks) or "无"
            return f"未找到 Hook {name}。已加载：{known}"
        lines = [
            f"Hook {hook.id} · {HOOK_SOURCES[hook.source]} · {engine.status(hook)}",
            f"  文件  {_shown(hook.path, engine.root)}",
            "  配置",
        ]
        dumped = yaml.safe_dump(_masked(hook.raw), allow_unicode=True, sort_keys=False).rstrip()
        lines.extend("    " + line for line in dumped.splitlines())
        runs = [run for run in engine.recent if run.hook == hook.id]
        lines.append("  本次运行的执行记录" + ("" if runs else "：无"))
        lines.extend("  " + _run_row(run) for run in runs[-10:])
        lines.append(f"  日志  {engine.log_path or '（本次运行没有会话目录）'}")
        return "\n".join(lines)
    files = [file for file in engine.config.files if file.hooks]
    if not files and not engine.problems:
        return (
            "没有 Hook。可以在 ~/.mosscode/hooks.yaml 或项目 .mosscode/hooks.yaml、.mosscode/hooks.local.yaml 中添加，"
            "格式见 docs/reference/hooks.md。"
        )
    lines = [f"Hook（已启用 {len(engine.hooks)} 条）"]
    for file in files:
        mark = "" if file.trusted else "（未确认，未启用）"
        lines.append(f"{HOOK_SOURCES[file.source]}  {_shown(file.path, engine.root)}{mark}")
        for hook in file.hooks:
            lines.extend(_hook_row(engine, hook))
    lines.extend("! " + problem for problem in engine.problems)
    for file in engine.untrusted:
        lines.append(f"! 项目 Hook 文件 {_shown(file.path, engine.root)} 未确认；/hooks reload 后可以确认。")
    if engine.recent:
        lines.append("最近执行")
        lines.extend(_run_row(run) for run in list(engine.recent)[-10:])
    lines.append("/hooks <id> 查看详情；/hooks reload 重新加载。")
    return "\n".join(lines)


def trust_text(file, root: Path) -> str:
    lines = [
        f"项目 Hook 文件 {_shown(file.path, root)} 尚未确认。",
        "Hook 以 MossCode 的权限自动运行，不经过工具审批；请先确认内容：",
    ]
    for hook in file.hooks:
        lines.extend(_hook_lines(hook))
    return "\n".join(lines)


async def offer_trust(ui, engine, on_done=None) -> None:
    """One picker per untrusted project file; Esc means 本次不启用. Returns at once (callbacks chain)."""
    pending = list(engine.pending_trust()) if engine is not None and engine.enabled else []

    async def next_file() -> None:
        if not pending:
            if on_done is not None:
                await on_done()
            return
        file = pending.pop(0)
        shown = _shown(file.path, engine.root)
        await ui.note(trust_text(file, engine.root))

        async def pick(index: int) -> None:
            if index == 0:
                try:
                    engine.trust(file)
                    await ui.note(f"└ 已信任并启用 {shown}。")
                except OSError as exc:
                    await ui.note(f"└ 无法写入信任记录（{exc.strerror or exc}），未启用 {shown}。")
            else:
                engine.decline(file)
                await ui.note(f"└ 本次不启用 {shown}；/hooks reload 时会再次询问。")
            ui.refresh_status()
            await next_file()

        async def cancel() -> None:
            await pick(1)

        ui.choose(f"信任项目 Hook · {file.path.name}", TRUST_OPTIONS, 0, pick, on_cancel=cancel)

    await next_file()


async def _hooks(ctx: CommandContext, argument: str) -> None:
    engine = ctx.conversation.hooks
    if argument != "reload" or engine is None or not engine.enabled:
        await ctx.ui.note(hooks_report(engine, argument))
        return
    added, removed = await asyncio.to_thread(engine.reload)
    lines = [
        f"└ 已重新加载 Hook：已启用 {len(engine.hooks)} 条"
        + (f"，新增 {'、'.join(added)}" if added else "")
        + (f"，移除 {'、'.join(removed)}" if removed else "")
        + "。"
    ]
    lines.extend("  ! " + problem for problem in engine.problems)
    await ctx.ui.note("\n".join(lines))
    ctx.ui.refresh_status()
    await offer_trust(ctx.ui, engine)


def _hook_names(ctx: CommandContext):
    engine = ctx.conversation.hooks
    hooks = [hook for file in engine.config.files for hook in file.hooks] if engine is not None else []
    return [("reload", "重新加载 Hook 文件"), *((hook.id, f"{hook.event} · {hook.summary()}") for hook in hooks)]


# Sub-agents ---------------------------------------------------------------------------


def agents_line(conversation) -> str:
    catalog, tasks = conversation.agents, conversation.tasks
    running = tasks.background()
    line = f"{len(catalog.agents)} 个定义" + (f"（{len(catalog.problems)} 个有问题）" if catalog.problems else "")
    line += f"，后台任务 {len(running)} 个运行中" if running else "，没有运行中的后台任务"
    return line + "（/agents、/tasks 查看）"


def _no_provider(conversation, agent) -> bool:
    return (
        bool(agent.model)
        and not conversation._is_current(agent.model)
        and conversation._fork_provider_config(agent.model) is None
    )


def _model_missing(conversation, agent) -> bool:
    return _no_provider(conversation, agent) and not claude_model(agent.model)


def _agent_model(conversation, agent) -> str:
    """A missing provider is shown here, and the call fails; only Claude Code's model names use the current one."""
    if not agent.model:
        return "继承当前模型"
    if _no_provider(conversation, agent) and claude_model(agent.model):
        return f"model: {agent.model} 不是这里的 Provider，使用当前模型"
    return f"{agent.model}（没有该提供商，调用时报错）" if _model_missing(conversation, agent) else agent.model


def agents_report(conversation, name: str = "") -> str:
    if conversation.registry is None or not getattr(conversation, "agents_enabled", False):
        return "当前会话未启用工具，子 Agent 不可用。"
    catalog = conversation.agents
    if name:
        agent = catalog.get(name)
        if agent is None:
            return f"未找到 Agent 定义 {name}。已加载：" + ("、".join(catalog.agents) or "无")
        names = conversation.registry.names()
        available = [tool for tool in names if tool not in GLOBAL_BAN and agent.allows(tool)]
        isolated = agent.isolation == "worktree"
        lost = [tool for tool in available if tool not in WORKSPACE_TOOLS] if isolated else []
        available = [tool for tool in available if tool not in lost]
        rows = [
            ("路径", str(agent.path)),
            ("来源", source_label(agent)),
            ("覆盖", "、".join(str(path) for path in agent.overrides) or "无"),
            ("描述", agent.description),
            ("模型", _agent_model(conversation, agent)),
            (
                "权限模式",
                f"{agent.permission_mode or '继承会话'}；本会话实际为 "
                f"{effective_mode(agent, conversation.registry.permissions.mode)}",
            ),
            ("最大轮数", str(agent.max_turns) if agent.max_turns else "与主对话相同"),
            ("默认后台", "是" if agent.background else "否"),
            ("隔离", "worktree（在独立的 git 工作树中运行）" if isolated else "无"),
            ("工具白名单", "、".join(agent.tools) or "未限制"),
            ("禁用工具", "、".join(agent.disallowed_tools) or "无"),
            ("可用工具", "、".join(available) or "无"),
            *([("隔离时不可用", "、".join(lost))] if lost else []),
            ("未支持字段", "、".join(agent.unsupported) or "无"),
            ("不存在的工具", "、".join(agent.unknown_tools(names)) or "无"),
            ("提示词首行", _first_line(agent.prompt)),
        ]
        return "\n".join(
            [f"Agent {agent.name}", *(f"  {label}{' ' * (14 - cell_len(label))}{value}" for label, value in rows)]
        )
    lines = [f"Agent 定义（{len(catalog.agents)}）"]
    width = max((cell_len(item) for item in catalog.agents), default=0)
    for agent in catalog.agents.values():
        marks = (f"  [覆盖 {len(agent.overrides)} 个]" if agent.overrides else "") + (
            f"  [模型 {agent.model} 不存在]" if _model_missing(conversation, agent) else ""
        )
        lines.append(
            f"{agent.name}{' ' * (width - cell_len(agent.name))}  {source_label(agent)}  {agent.description}{marks}"
        )
    lines.extend("! " + problem for problem in catalog.problems)
    lines.append("模型用 agent 工具调用；/agents 名称 查看详情；/agents reload 重新加载。")
    return "\n".join(lines)


async def _agents(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    if argument != "reload" or conversation.registry is None or not getattr(conversation, "agents_enabled", False):
        await ctx.ui.note(agents_report(conversation, argument))
        return
    old = conversation.agents
    catalog = await asyncio.to_thread(old.reload)
    conversation.use_agents(catalog)
    added = [name for name in catalog.agents if name not in old.agents]
    removed = [name for name in old.agents if name not in catalog.agents]
    changed = [name for name, agent in catalog.agents.items() if name in old.agents and agent != old.agents[name]]
    lines = [
        f"└ 已重新加载 Agent 定义：共 {len(catalog.agents)} 个"
        + (f"，新增 {'、'.join(added)}" if added else "")
        + (f"，移除 {'、'.join(removed)}" if removed else "")
        + (f"，修改 {'、'.join(changed)}" if changed else "")
        + "；下一次请求起生效。"
    ]
    lines.extend("  ! " + problem for problem in catalog.problems)
    await ctx.ui.note("\n".join(lines))
    ctx.ui.refresh_status()


async def _plugins(ctx: CommandContext, argument: str) -> None:
    from .plugin_command import report

    text = await asyncio.to_thread(report, None, argument.strip())
    await ctx.ui.note(
        text + "\n安装、更新、删除、停用在命令行中进行（moss plugin install|update|remove|disable …），"
        "变化在下一次启动 moss 时生效。"
    )


def _plugin_names(ctx: CommandContext):
    from .plugins import load_records

    return [(record.name, record.source) for record in load_records()[0]]


def _agent_names(ctx: CommandContext):
    if not getattr(ctx.conversation, "agents_enabled", False):
        return []
    return [
        ("reload", "重新加载 Agent 定义"),
        *((agent.name, agent.description) for agent in ctx.conversation.agents.agents.values()),
    ]


def _task_usage(task) -> str:
    usage = task.usage
    return (
        f"输入 {usage.input_tokens:,}（缓存命中 {usage.cached_input_tokens:,}）· 输出 {usage.output_tokens:,}"
        f" · 请求 {usage.requests} 次 · 费用 {cost_text(usage)}"
    )


def _preview(text: str, lines: int = 12, size: int = 1500) -> list[str]:
    shown = text[:size].splitlines()[:lines]
    more = len(text) > size or len(text.splitlines()) > lines
    return [*shown, *(["…（完整内容见对话记录）"] if more else [])]


def tasks_report(conversation, task_id: str = "") -> str:
    tasks = conversation.tasks
    if task_id:
        task = tasks.get(task_id)
        if task is None:
            return f"没有任务 {task_id}。输入 /tasks 查看本会话的任务。"
        if task.command is not None:
            return _command_report(task)
        rows = [
            ("来源", ORIGIN_LABELS[task.origin]),
            ("类型", task.agent_type),
            ("标签", task.description),
            ("状态", STATUS_LABELS[task.status] + ("（后台）" if task.background else "（前台）")),
            ("统计", stats(task)),
            ("用量", _task_usage(task)),
            ("最近工具", task.last_tool or "无"),
            ("对话记录", str(task.transcript) if task.transcript is not None else "未保存"),
        ]
        worktree = task.worktree
        if worktree:
            rows.append(("工作树", f"{worktree['relative']}（分支 {worktree['branch']}）· {worktree['outcome']}"))
        lines = [f"任务 {task.id}", *(f"  {label}{' ' * (10 - cell_len(label))}{value}" for label, value in rows)]
        lines.append("  任务内容")
        lines.extend("    " + line for line in _preview(task.prompt, 6, 600))
        if task.result:
            lines.append("  结果")
            lines.extend("    " + line for line in _preview(task.result))
        if task.status == "running":
            lines.append(f"/tasks stop {task.id} 停止。")
        return "\n".join(lines)
    if not tasks.tasks:
        return "本会话还没有任务。"
    lines = [f"任务（{len(tasks.tasks)}）"]
    for task in tasks.tasks.values():
        place = "后台" if task.background else "前台"
        if task.command is not None:  # A command has no tokens; its output size instead.
            from .tools.background import size_text

            amount = f"输出 {size_text(task.command.log.size)}"
        else:
            amount = f"Token {task.tokens:,}"
        lines.append(
            f"  {task.id:<4} {STATUS_LABELS[task.status]}  {place}  {task.label}  {task.elapsed:.0f} 秒  {amount}"
        )
    lines.append("/tasks <id> 查看详情；/tasks stop <id> 停止。")
    return "\n".join(lines)


def _command_report(task) -> str:
    from .tools.background import OUTPUT_LINES, size_text, status_text

    info = task.command
    owner = info.owner
    rows = [
        ("来源", ORIGIN_LABELS[task.origin]),
        ("类型", "命令"),
        ("标签", task.description),
        ("状态", status_text(task)),
        ("用时", f"{task.elapsed:.0f} 秒" + (f"（时间限制 {info.time_limit:.0f} 秒）" if info.time_limit else "")),
        ("工作目录", str(info.cwd)),
        ("沙箱", info.sandbox or "不在沙箱中"),
        ("输出文件", f"{info.log.path}（{size_text(info.log.size)}）"),
    ]
    if owner:
        rows.append(
            ("随之结束", f"成员 {owner.removeprefix('member:')}" if owner.startswith("member:") else f"任务 {owner}")
        )
    lines = [f"任务 {task.id}", *(f"  {label}{' ' * (10 - cell_len(label))}{value}" for label, value in rows)]
    lines.append("  命令")
    shown = info.command[:600].splitlines()[:6]
    more = len(info.command) > 600 or len(info.command.splitlines()) > 6
    lines.extend("    " + line for line in [*shown, *(["…"] if more else [])])
    tail = info.log.last_lines(10, 2000)
    if tail:
        lines.append(f"  最后几行输出（完整输出见输出文件，task_output 给模型最后 {OUTPUT_LINES} 行）")
        lines.extend("    " + line for line in tail.splitlines())
    if task.status == "running":
        lines.append(f"/tasks stop {task.id} 停止（连同它启动的进程）。")
    return "\n".join(lines)


async def _tasks(ctx: CommandContext, argument: str) -> None:
    action, _, rest = argument.partition(" ")
    if action != "stop":
        await ctx.ui.note(tasks_report(ctx.conversation, argument))
        return
    task_id = rest.strip()
    if not task_id:
        await ctx.ui.note("用法：/tasks stop <id>")
        return
    tasks = ctx.conversation.tasks
    task = tasks.get(task_id)
    if task is None:
        await ctx.ui.note(f"没有任务 {task_id}。")
    elif await tasks.stop(task_id):
        await ctx.ui.note(f"└ 已停止任务 {task_id}（{task.label}）；不会再收到它的通知。")
    else:
        await ctx.ui.note(f"任务 {task_id} 已结束（{STATUS_LABELS[task.status]}），无需停止。")
    ctx.ui.refresh_status()


def _task_names(ctx: CommandContext):
    tasks = getattr(ctx.conversation, "tasks", None)
    if tasks is None:
        return []
    return [
        *((task.id, f"{STATUS_LABELS[task.status]} · {task.label}") for task in tasks.tasks.values()),
        *((f"stop {task.id}", f"停止 · {task.label}") for task in tasks.running()),
    ]


def todo_report(conversation) -> str:
    items = getattr(conversation, "todos", ())
    if not items:
        return "当前没有任务清单。模型处理多步任务时会用 todo_write 建立清单，显示在输入框上方。"
    return "任务清单\n" + render_todos(items)


async def _todo(ctx: CommandContext, argument: str) -> None:
    await ctx.ui.note(todo_report(ctx.conversation))


# Goal --------------------------------------------------------------------------------

GOAL_CLEAR_WORDS = frozenset({"clear", "stop", "off", "cancel", "清除"})
GOAL_ENDED = {"achieved": "已达成", "impossible": "无法达成", "cleared": "已清除"}
GOAL_USAGE = f"用法：/goal <条件>（最多 {MAX_CONDITION} 字）设置目标并开始 · /goal 查看或继续 · /goal clear 清除"


def goal_report(conversation) -> str:
    goal = getattr(conversation, "goal", None)
    if goal is None:
        last = getattr(conversation, "last_goal", None)
        if last is None:
            return "当前没有目标。" + GOAL_USAGE
        condition, event, reason = last
        lines = [f"当前没有目标。最近一次目标{GOAL_ENDED.get(event, event)}：{condition}"]
        if reason:
            lines.append(f"  理由：{reason}")
        return "\n".join(lines)
    from time import monotonic

    state = f"已暂停（{goal.paused}）" if goal.paused else "进行中"
    lines = [
        f"◎ 目标{state}",
        f"  条件：{goal.condition}",
        f"  已运行 {elapsed_text(monotonic() - goal.started)} · {goal.turns}/{goal.budget} 轮"
        f" · {approx_tokens(conversation.goal_tokens())} Token",
    ]
    if goal.reason:
        lines.append(f"  最近一次判断：尚未达成。{goal.reason}")
    return "\n".join(lines)


def _goal_reads(argument: str) -> bool:
    """Showing and clearing are safe during a run; setting a goal waits."""
    return not argument.strip() or argument.strip().lower() in GOAL_CLEAR_WORDS


async def _goal(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    argument = argument.strip()
    if argument.lower() in GOAL_CLEAR_WORDS:
        goal = conversation.clear_goal()
        ctx.ui.refresh_status()
        await ctx.ui.note("└ 已清除目标。" if goal is not None else "└ 当前没有目标。")
        return
    if not argument:
        goal = conversation.goal
        await ctx.ui.note(goal_report(conversation))
        if goal is not None and conversation.resume_goal():
            ctx.ui.refresh_status()
            await ctx.ui.note("└ 已继续目标。")
            await ctx.ui.submit(continue_prompt(goal.reason), synthetic=GOAL)
        return
    try:
        conversation.set_goal(argument)
    except (ValueError, ProviderError) as exc:
        await ctx.ui.note(f"└ {exc}")
        return
    ctx.ui.refresh_status()
    await ctx.ui.note(
        f"└ 已设置目标：每轮结束后判断是否达成，未达成时自动继续（最多 {conversation.goal.budget} 轮）。"
        "Esc 暂停，/goal clear 清除。"
    )
    await ctx.ui.submit(argument, command=f"/goal {argument}")


# Worktrees ---------------------------------------------------------------------------

WORKTREE_USAGE = (
    "用法：/worktree [list] · /worktree create <名称> · /worktree remove <名称> [--discard] · /worktree clean"
)


def worktree_manager(conversation):
    """(manager, problem)."""
    from .worktree import WorktreeError

    getter = getattr(conversation, "worktree_manager", None)
    if getter is None:
        return None, "当前会话不支持工作树。"
    try:
        return getter(), ""
    except WorktreeError as exc:
        return None, f"工作树不可用：{exc}"


def _when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return stamp or "未知"


async def worktree_report(manager) -> str:
    entries = await manager.entries()
    if not entries:
        return "没有 MossCode 管理的工作树。/worktree create <名称> 创建一个；子 Agent 隔离运行时会自动创建。"
    lines = [f"工作树（{len(entries)}）"]
    for entry in entries:
        meta = entry.meta
        if meta is None:
            lines.append(f"  {entry.name}  {entry.status}")
            continue
        lines.append(f"  {meta.name}  {'手动' if meta.manual else '临时'}  {entry.status}")
        lines.append(
            f"    分支 {meta.branch}，基于 {meta.base[:7]}，创建于 {_when(meta.created)}；{manager.display(meta.path)}"
        )
    lines.append("/worktree remove <名称> 删除；/worktree clean 清理过期的临时工作树。")
    return "\n".join(lines)


def clean_text(report, *, automatic: bool = False) -> str:
    if automatic:
        return f"└ 已自动清理 {len(report.removed)} 个过期的临时工作树：{'、'.join(report.removed)}"
    lines = [
        f"└ 已删除 {len(report.removed)} 个，跳过 {len(report.skipped)} 个"
        + (f"：{'、'.join(report.removed)}" if report.removed else "。")
    ]
    lines.extend(f"  {name}：{reason}" for name, reason in report.skipped)
    return "\n".join(lines)


async def _worktree(ctx: CommandContext, argument: str) -> None:
    from .worktree import WorktreeError

    action, _, rest = argument.strip().partition(" ")
    rest = rest.strip()
    manager, problem = worktree_manager(ctx.conversation)
    if manager is None:
        await ctx.ui.note(problem)
        return
    try:
        if action in ("", "list") and not rest:
            await ctx.ui.note(await worktree_report(manager))
        elif action == "create" and rest and " " not in rest:
            created = await manager.create(rest)
            meta = created.meta
            root = manager.root_in(meta)
            head = f"工作树 {meta.name} 已存在" if created.resumed else f"已创建工作树 {meta.name}"
            lines = [
                f"└ {head}：{manager.display(meta.path)}",
                f"  分支 {meta.branch}，基于 {meta.base[:7]}",
                *(f"  ! {warning}" for warning in created.warnings),
                *(["  ! 主目录有未提交的改动，它们不在这个工作树中"] if created.dirty else []),
                f'  在另一个终端中进入：cd "{root}"',
            ]
            await ctx.ui.note("\n".join(lines))
        elif action == "remove" and rest:
            words = rest.split()
            discard = "--discard" in words
            names = [word for word in words if word != "--discard"]
            if len(names) != 1:
                await ctx.ui.note(WORKTREE_USAGE)
                return
            changes = await manager.remove(names[0], discard=discard)
            dropped = f"（已丢弃：{changes.describe()}）" if changes.any else ""
            await ctx.ui.note(f"└ 已删除工作树 {names[0]} 和它的分支{dropped}。")
        elif action == "clean" and not rest:
            await ctx.ui.note(clean_text(await manager.clean()))
        else:
            await ctx.ui.note(WORKTREE_USAGE)
    except WorktreeError as exc:
        await ctx.ui.note(f"! {exc}")


def _worktree_names(ctx: CommandContext):
    choices = [
        ("list", "列出工作树"),
        ("create", "创建或恢复工作树"),
        ("remove", "删除工作树"),
        ("clean", "清理过期的临时工作树"),
    ]
    manager, _ = worktree_manager(ctx.conversation) if getattr(ctx.conversation, "_worktrees", None) else (None, "")
    if manager is not None:
        choices.extend(
            (f"remove {meta.name}", "删除 · " + ("手动" if meta.manual else "临时")) for _, meta in manager.metas()
        )
    return choices


def _worktree_reads(argument: str) -> bool:
    """Listing is safe during a run."""
    return argument.strip() in ("", "list")


# Teams -------------------------------------------------------------------------------

TEAM_USAGE = "用法：/team · /team <名称> · /team stop <成员> · /team delete <名称>"
TEAM_DETAILS = (
    "子命令：\n"
    "  /team                 列出本项目的团队：成员数、各状态人数、当前团队和持有者\n"
    "  /team <名称>          团队详情：成员状态、后端、工作树、最近工具调用、用量、未读消息和任务\n"
    "  /team stop <成员>     请当前团队的成员在这一轮结束后停止，超时强制停止\n"
    "  /team delete <名称>   确认后停止成员、删除已合并且干净的工作树和团队目录"
)
DELETE_OPTIONS = ("删除团队", "取消")


async def _team(ctx: CommandContext, argument: str) -> None:
    from .teams import TeamError

    runtime = getattr(ctx.conversation, "teams", None)
    if runtime is None:
        await ctx.ui.note("当前会话不支持团队。")
        return
    words = argument.split()
    try:
        if not words:
            await ctx.ui.note(await asyncio.to_thread(runtime.overview))
        elif words[0] == "stop" and len(words) == 2:
            await ctx.ui.note("└ " + await runtime.stop_member(words[1]))
            ctx.ui.refresh_status()
        elif words[0] == "delete" and len(words) == 2:
            name = words[1]
            await asyncio.to_thread(runtime.detail, name)  # unknown_team before asking.

            async def picked(index: int) -> None:
                if index != 0:
                    await ctx.ui.note("已取消，团队保留。")
                    return
                try:
                    text, _ = await runtime.delete(name, force=True)
                except TeamError as exc:
                    await ctx.ui.note(f"! {exc.message}")
                    return
                await ctx.ui.note("└ " + text)
                ctx.ui.refresh_status()

            async def cancelled() -> None:
                await ctx.ui.note("已取消，团队保留。")

            ctx.ui.choose(
                f"删除团队 {name}？会停止它的成员，删除已合并且干净的工作树和团队目录（任务、邮箱、对话记录）。",
                DELETE_OPTIONS,
                1,
                picked,
                cancelled,
            )
        elif len(words) == 1 and words[0] not in ("stop", "delete"):
            await ctx.ui.note(await asyncio.to_thread(runtime.detail, words[0]))
        else:
            await ctx.ui.note(TEAM_USAGE)
    except TeamError as exc:
        await ctx.ui.note(f"! {exc.message}")


def _team_names(ctx: CommandContext):
    runtime = getattr(ctx.conversation, "teams", None)
    if runtime is None:
        return []
    choices = []
    for store in runtime.teams():
        choices += [(store.name, "团队详情"), (f"delete {store.name}", "删除团队")]
    if runtime.current is not None:
        choices += [(f"stop {name}", f"停止 · {runner.status}") for name, runner in runtime.runners.items()]
    return choices


def _team_reads(argument: str) -> bool:
    """Listing, details and stopping are safe during a run; deleting waits."""
    return argument.split()[:1] != ["delete"]


async def _exit(ctx: CommandContext, argument: str) -> None:
    async def leave() -> None:
        ctx.ui.exit()

    await confirm_tasks(ctx.ui, ctx.conversation, leave)


BUILTIN = CommandRegistry(
    (
        Command(
            "help",
            "查看命令和快捷键",
            "/help [命令]",
            "local",
            argument="optional",
            argument_hint="[命令]",
            handler=_help,
        ),
        Command("clear", "开始新会话：清空模型上下文和 Token 统计", "/clear", "local", handler=_clear),
        Command(
            "compact",
            "压缩上下文：较早的对话换成摘要，近期消息保留",
            "/compact [说明]",
            "local",
            argument="optional",
            argument_hint="[说明]",
            handler=_compact,
        ),
        Command(
            "resume",
            "恢复本项目之前的会话",
            "/resume [ID]",
            "local",
            argument="optional",
            argument_hint="[ID]",
            handler=_resume,
        ),
        Command(
            "rewind",
            "把文件或对话退回到之前某条消息发出前的状态",
            "/rewind",
            "local",
            aliases=("checkpoint",),
            handler=_rewind,
        ),
        Command("session", "查看当前会话的 ID、存档位置和消息数", "/session", "local", handler=_session),
        Command(
            "memory",
            "查看已加载的项目指令和记忆笔记",
            "/memory [名称]",
            "local",
            argument="optional",
            argument_hint="[名称]",
            handler=_memory,
            complete=_note_names,
        ),
        Command(
            "model",
            "切换提供商与模型，对话历史保留",
            "/model [名称]",
            "local",
            argument="optional",
            argument_hint="[名称]",
            handler=_model,
            complete=_provider_names,
        ),
        Command(
            "thinking",
            "查看或设置本会话的思考强度",
            "/thinking [档位]",
            "local",
            argument="optional",
            argument_hint="[档位]",
            handler=_thinking,
            complete=lambda ctx: [(value, LEVEL_LABELS[value]) for value in THINKING_CHOICES],
        ),
        Command(
            "plan",
            "只读调查和规划，提交计划经批准后执行",
            "/plan [任务]",
            "local",
            argument="optional",
            argument_hint="[任务]",
            handler=_plan,
        ),
        Command(
            "do",
            "退出计划模式，回到进入前的权限档位",
            "/do [任务]",
            "local",
            argument="optional",
            argument_hint="[任务]",
            handler=_do,
        ),
        Command(
            "permissions",
            "查看或切换工具权限档位",
            "/permissions [档位]",
            "local",
            aliases=("permission",),
            argument="optional",
            argument_hint="[档位]",
            handler=_permissions,
            complete=lambda ctx: MODE_CHOICES,
        ),
        Command(
            "status", "查看模型、权限、上下文、用量和连接状态", "/status", "local", handler=_status, while_running=True
        ),
        Command(
            "sandbox",
            "查看命令沙箱的边界和最近的拦截，或在本会话中打开、关闭",
            "/sandbox [on|off]",
            "local",
            argument="optional",
            argument_hint="[on|off]",
            handler=_sandbox,
            complete=lambda ctx: [("on", "打开沙箱"), ("off", "关闭沙箱（需确认）")],
        ),
        Command(
            "mcp",
            "查看 MCP Server 的连接状态与工具",
            "/mcp [名称]",
            "local",
            argument="optional",
            argument_hint="[名称]",
            handler=_mcp,
            complete=_server_names,
        ),
        Command(
            "skills",
            "查看、重新扫描 Skill",
            "/skills [名称|reload]",
            "local",
            aliases=("skill",),
            argument="optional",
            argument_hint="[名称|reload]",
            handler=_skills,
            complete=_skill_names,
        ),
        Command(
            "hooks",
            "查看、重新加载 Hook",
            "/hooks [id|reload]",
            "local",
            aliases=("hook",),
            argument="optional",
            argument_hint="[id|reload]",
            handler=_hooks,
            complete=_hook_names,
        ),
        Command(
            "agents",
            "查看、重新加载子 Agent 定义",
            "/agents [名称|reload]",
            "local",
            aliases=("agent",),
            argument="optional",
            argument_hint="[名称|reload]",
            handler=_agents,
            complete=_agent_names,
        ),
        Command(
            "plugins",
            "查看已安装的插件",
            "/plugins [名称]",
            "local",
            aliases=("plugin",),
            argument="optional",
            argument_hint="[名称]",
            handler=_plugins,
            complete=_plugin_names,
        ),
        Command(
            "tasks",
            "查看、停止本会话的子 Agent 任务",
            "/tasks [id|stop id]",
            "local",
            aliases=("task",),
            argument="optional",
            argument_hint="[id|stop id]",
            handler=_tasks,
            complete=_task_names,
            while_running=True,
        ),
        Command("todo", "查看模型当前的完整任务清单", "/todo", "local", handler=_todo, while_running=True),
        Command(
            "goal",
            "设置目标，自动续跑到达成；不带参数查看或继续",
            "/goal [条件|clear]",
            "local",
            argument="optional",
            argument_hint="[条件|clear]",
            handler=_goal,
            while_running=_goal_reads,
            details="/goal <条件>：设置目标并立即开始，每轮结束后另一次模型调用判断是否达成，未达成时自动继续\n"
            "/goal：查看目标；目标已暂停时继续\n/goal clear（stop、off）：清除目标；/clear 也会清除",
        ),
        Command(
            "worktree",
            "查看、创建、删除 git 工作树",
            "/worktree [子命令]",
            "local",
            aliases=("worktrees",),
            argument="optional",
            argument_hint="[子命令]",
            handler=_worktree,
            complete=_worktree_names,
            while_running=_worktree_reads,
        ),
        Command(
            "team",
            "查看、停止、删除 Agent 团队",
            "/team [子命令]",
            "local",
            aliases=("teams",),
            argument="optional",
            argument_hint="[子命令]",
            handler=_team,
            complete=_team_names,
            while_running=_team_reads,
            details=TEAM_DETAILS,
        ),
        Command("exit", "退出 MossCode", "/exit", "local", aliases=("quit",), handler=_exit),
    )
)
