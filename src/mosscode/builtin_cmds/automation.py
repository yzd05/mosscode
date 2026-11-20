"""Goal, recurring loop, and git worktree slash commands."""

from __future__ import annotations

import inspect
from datetime import datetime

from ..agents.tasks import STATUS_LABELS
from ..commands import CommandContext
from ..goal import GOAL, MAX_CONDITION, continue_prompt, elapsed_text
from ..messages import ProviderError
from ..usage import approx_tokens
from .common import _first_line, dispatch

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


# Loop --------------------------------------------------------------------------------

LOOP_HEADLESS_NOTICE = "定时巡检 /loop 仅在交互会话（TUI 或 ACP）中可用。"
LOOP_DETAILS = (
    "用法：\n"
    "  /loop [间隔] <提示词>   启动定时巡检（默认 5m，支持 s/m/h/d 及中文单位，或末尾 every 5m、--max-runs N）\n"
    "  /loop [list]            查看当前会话活跃与已结束的定时巡检\n"
    "  /loop stop <id|all>     停止指定或全部运行中的定时巡检"
)


def loop_report(conversation) -> str:
    """Render active and finished /loop schedules for `/loop` and `/loop list`."""
    from time import monotonic

    loops = getattr(conversation, "loops", None)
    if loops is not None and callable(getattr(loops, "all_loops", None)):
        items = list(loops.all_loops())
    else:
        tasks = getattr(conversation, "tasks", None)
        items = [t for t in (tasks.tasks.values() if tasks is not None else ()) if getattr(t, "loop", None) is not None]
    if not items:
        return "当前没有定时巡检任务。用法：/loop [间隔] <提示词>（默认 5m，例如 /loop 5m 检查构建 或 /loop 10m /test）"
    lines = [f"定时巡检（{len(items)}）"]
    now = monotonic()
    for task in items:
        info = task.loop
        status_label = STATUS_LABELS.get(task.status, task.status)
        if task.status == "running":
            if getattr(info, "pending_fire", False):
                timing = "等待空闲触发"
            elif getattr(info, "next_fire", None) is not None:
                timing = f"下次触发 {elapsed_text(max(0.0, info.next_fire - now))} 后"
            else:
                timing = "排队中"
        else:
            timing = getattr(info, "end_reason", "") or (
                "已手动停止" if getattr(task, "stopped", False) else status_label
            )
        preview = _first_line(info.prompt)[:80]
        lines.append(
            f"  {task.id:<4} {status_label}  每 {info.interval_text} · 已运行 {info.runs}/{info.max_runs} 次"
            f" · {timing} · {preview}"
        )
    lines.append("/loop [间隔] <提示词> 新建；/loop stop <id|all> 停止；/tasks <id> 查看详情。")
    return "\n".join(lines)


def _ensure_loops(ctx: CommandContext):
    """Return the session's LoopScheduler, lazily attaching one for interactive contexts when available."""
    if (
        ctx.ui is None
        or getattr(ctx.ui, "headless", False)
        or getattr(ctx.conversation, "headless", False)
        or not callable(getattr(ctx.ui, "submit", None))
    ):
        return None
    loops = getattr(ctx.conversation, "loops", None)
    if loops is not None:
        return loops
    attach = getattr(ctx.conversation, "attach_loops", None)
    if not callable(attach):
        return None

    async def _default_trigger(task, prompt: str) -> None:
        from ..commands import parse_input, unknown_note
        from ..loop_schedule import LOOP_SYNTHETIC, format_loop_prompt

        runs = getattr(getattr(task, "loop", None), "runs", 1)
        await ctx.ui.note(f"⏰ 定时巡检 {task.id}（第 {runs} 次）：{_first_line(prompt)[:100]}")
        if prompt.lstrip().startswith("/"):
            parsed = parse_input(prompt, ctx.registry)
            if parsed.kind == "unknown":
                await ctx.ui.note(unknown_note(parsed, ctx.registry))
                return
            if parsed.kind == "command":
                await dispatch(ctx, parsed)
                return
        await ctx.ui.submit(format_loop_prompt(task), synthetic=LOOP_SYNTHETIC)

    return attach(
        is_busy=lambda: bool(
            ctx.conversation.busy or (ctx.conversation.goal is not None and ctx.conversation.goal.active)
        ),
        trigger=_default_trigger,
    )


async def _loop(ctx: CommandContext, argument: str) -> None:
    loops = _ensure_loops(ctx)
    if loops is None:
        if ctx.ui is not None:
            await ctx.ui.note(LOOP_HEADLESS_NOTICE)
        return
    try:
        from ..loop_schedule import parse_loop_args
    except ImportError:
        await ctx.ui.note(LOOP_HEADLESS_NOTICE)
        return
    try:
        parsed = parse_loop_args(argument)
    except ValueError as exc:
        await ctx.ui.note(f"└ {exc}")
        return
    if parsed.action == "list":
        await ctx.ui.note(loop_report(ctx.conversation))
        return
    if parsed.action == "stop":
        res = loops.stop(parsed.target)
        stopped = await res if inspect.isawaitable(res) else res
        if parsed.target.lower() == "all":
            if stopped:
                ids = "、".join(t.id for t in stopped)
                await ctx.ui.note(f"└ 已停止 {len(stopped)} 个定时巡检（{ids}）。")
            else:
                await ctx.ui.note("└ 当前没有运行中的定时巡检。")
        elif stopped:
            target_task = stopped[0]
            preview = (
                _first_line(target_task.loop.prompt)[:60]
                if getattr(target_task, "loop", None) is not None
                else target_task.description
            )
            await ctx.ui.note(f"└ 已停止定时巡检 {target_task.id}（{preview}）。")
        else:
            existing = ctx.conversation.tasks.get(parsed.target)
            if existing is None or getattr(existing, "loop", None) is None:
                await ctx.ui.note(f"└ 没有定时巡检任务 {parsed.target}。输入 /loop list 查看。")
            else:
                await ctx.ui.note(
                    f"└ 定时巡检 {parsed.target} 已结束（{STATUS_LABELS.get(existing.status, existing.status)}），无需停止。"
                )
        ctx.ui.refresh_status()
        return
    try:
        res = loops.schedule(parsed.interval, parsed.interval_text, parsed.prompt, max_runs=parsed.max_runs)
        task = await res if inspect.isawaitable(res) else res
    except (ValueError, ProviderError) as exc:
        await ctx.ui.note(f"└ {exc}")
        return
    ctx.ui.refresh_status()
    shown_prompt = _first_line(parsed.prompt)[:80]
    await ctx.ui.note(
        f"└ 已启动定时巡检 {task.id}（每 {parsed.interval_text}，最多 {parsed.max_runs} 次）："
        f"{shown_prompt}；可用 /tasks 或 /loop stop {task.id} 停止。"
    )


def _loop_names(ctx: CommandContext):
    choices = [
        ("list", "查看当前会话的定时巡检计划"),
        ("stop all", "停止全部运行中的定时巡检"),
        ("5m ", "每 5 分钟巡检一次"),
        ("30s ", "每 30 秒巡检一次"),
        ("30m ", "每 30 分钟巡检一次"),
    ]
    tasks = getattr(ctx.conversation, "tasks", None)
    if tasks is not None:
        for task in tasks.running():
            info = getattr(task, "loop", None)
            if info is not None:
                choices.append((f"stop {task.id}", f"停止 · 每 {info.interval_text} · {_first_line(info.prompt)[:40]}"))
    return choices


# Worktrees ---------------------------------------------------------------------------

WORKTREE_USAGE = (
    "用法：/worktree [list] · /worktree create <名称> · /worktree remove <名称> [--discard] · /worktree clean"
)


def worktree_manager(conversation):
    """(manager, problem)."""
    from ..worktree import WorktreeError

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
    from ..worktree import WorktreeError

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
