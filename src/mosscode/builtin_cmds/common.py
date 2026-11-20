"""Shared helpers, constants, dispatch, and /help for built-in slash commands."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from ..agents.tasks import STATUS_LABELS
from ..commands import KEY_HELP, Command, CommandContext, CommandRegistry, Parsed, argument_problem
from ..goal import elapsed_text
from ..messages import ProviderError
from ..skills import ForkRequest, source_label

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
        if command.handler is not None:
            await command.handler(ctx, argument)
        else:
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


TASK_OPTIONS = ("取消这些任务并继续", "返回")
DEFAULT_MUTED_COLOR = "#8b929c"


def _muted(ui) -> str:
    return getattr(ui, "muted_color", DEFAULT_MUTED_COLOR)


def choice_row(name: str, detail: str = "", *, muted_color: str = DEFAULT_MUTED_COLOR) -> Text:
    """A row of a list to choose from: what is chosen at full strength, its details in the supporting grey."""
    text = Text(name, no_wrap=True, overflow="ellipsis")
    if detail:
        text.append("  " + detail, style=muted_color)
    return text


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


def _first_line(text: str, empty: str = "（空）") -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), empty)


def format_loop_timing(task, now: float, *, prefix_next: bool = False) -> str:
    """Format the next-fire countdown or end reason for a /loop task."""
    info = task.loop
    status_label = STATUS_LABELS.get(task.status, task.status)
    if task.status == "running":
        if getattr(info, "pending_fire", False):
            return "等待空闲触发"
        if getattr(info, "next_fire", None) is not None:
            remaining = f"{elapsed_text(max(0.0, info.next_fire - now))} 后"
            return f"下次触发 {remaining}" if prefix_next else remaining
        return "排队中"
    return getattr(info, "end_reason", "") or ("已手动停止" if getattr(task, "stopped", False) else status_label)
