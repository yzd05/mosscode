"""Agent team, swarm coordination, and exit slash commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..commands import CommandContext
from ..messages import ProviderError
from .common import _first_line, confirm_tasks

# Teams & Swarm -----------------------------------------------------------------------

TEAM_USAGE = "用法：/team · /team <名称> · /team stop <成员> · /team delete <名称>"
TEAM_DETAILS = (
    "子命令：\n"
    "  /team                 列出本项目的团队：成员数、各状态人数、当前团队、持有者和最近黑板记录\n"
    "  /team <名称>          团队详情：成员状态、后端、工作树、最近工具调用、用量、任务和黑板记录\n"
    "  /team stop <成员>     请当前团队的成员在这一轮结束后停止，超时强制停止\n"
    "  /team delete <名称>   确认后停止成员、删除已合并且干净的工作树和团队目录"
)
DELETE_OPTIONS = ("删除团队", "取消")

SWARM_USAGE = "用法：/swarm [status|on|off|<任务>]"
SWARM_DETAILS = (
    "子命令：\n"
    "  /swarm [status]       查看当前 Swarm 协调模式状态、当前团队与黑板摘要\n"
    "  /swarm on             在本会话开启协调者模式（Lead 只做规划、派发、验收与合并）\n"
    "  /swarm off            在本会话关闭协调者模式，恢复 Lead 的完整工具\n"
    "  /swarm <任务>         开启协调者模式并立即提交多 Agent 团队任务"
)


def _recent_board_posts(store, limit: int = 5) -> tuple[list[tuple[int, str, str]], int]:
    """Read recent `(seq, author, summary)` entries and `head_seq` from `store.blackboard` or `board.jsonl`."""
    if store is None:
        return [], 0
    board = getattr(store, "blackboard", None)
    if board is not None and callable(getattr(board, "read_since", None)):
        try:
            posts, head = board.read_since(0, limit=max(limit, 50))
            if head > len(posts) and head > limit:
                posts, head = board.read_since(max(0, head - limit), limit=limit)
            rows = []
            for item in list(posts)[-limit:]:
                seq = int(getattr(item, "seq", 0))
                author = str(getattr(item, "author", "") or "?")
                summary = str(getattr(item, "summary", "") or "").strip()
                if not summary:
                    summary = _first_line(str(getattr(item, "body", "") or ""), "")
                rows.append((seq, author, summary[:100]))
            return rows, int(head)
        except Exception:
            return [], 0
    directory = getattr(store, "directory", None)
    path = Path(directory) / "board.jsonl" if directory is not None else None
    if path is None or not path.is_file():
        return [], 0
    import json

    parsed: list[tuple[int, str, str]] = []
    head_seq = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            seq = int(raw.get("seq") or 0)
            if seq <= 0:
                continue
            head_seq = max(head_seq, seq)
            author = str(raw.get("author") or "?")
            summary = str(raw.get("summary") or "").strip() or _first_line(str(raw.get("body") or ""), "")
            parsed.append((seq, author, summary[:100]))
    except OSError:
        return [], 0
    return parsed[-limit:], head_seq


def _board_block(store, *, heading: str = "黑板", limit: int = 5) -> str:
    posts, head = _recent_board_posts(store, limit=limit)
    if not posts or head <= 0:
        return ""
    lines = [f"{heading}（共 {head} 条，最新 #{head}）"]
    for seq, author, summary in posts:
        lines.append(f"  #{seq} · {author}: {summary or '（无摘要）'}")
    return "\n".join(lines)


def team_report(conversation, name: str = "") -> str:
    """Overview or one team's detail, enriched with recent `board.jsonl` entries when present."""
    runtime = getattr(conversation, "teams", None)
    if runtime is None:
        return "当前会话不支持团队。"
    cleaned = name.strip()
    if cleaned:
        base = runtime.detail(cleaned)
        store = (
            runtime.current
            if runtime.current is not None and runtime.current.name == cleaned
            else next((item for item in runtime.teams() if item.name == cleaned), None)
        )
        block = _board_block(store, limit=8)
        if block and f"#{_recent_board_posts(store, limit=1)[1]}" not in base:
            return base + "\n" + block
        return base
    base = runtime.overview()
    store = runtime.current
    if store is None:
        stores = runtime.teams()
        if len(stores) == 1:
            store = stores[0]
    if store is not None:
        block = _board_block(store, heading=f"黑板（{store.name}）", limit=5)
        if block and f"#{_recent_board_posts(store, limit=1)[1]}" not in base:
            return base + "\n" + block
    return base


def swarm_report(conversation) -> str:
    """Current Swarm / coordinator mode status and active team summary."""
    from ..coordinator import SWARM_UNAVAILABLE

    runtime = getattr(conversation, "teams", None)
    if runtime is None:
        return f"Swarm 协调模式：不可用（{SWARM_UNAVAILABLE}）"
    active = getattr(conversation, "coordinator_mode", getattr(conversation, "coordinator", False))
    state = (
        "已开启（Lead 只负责规划、派发、验收与合并；可用 /swarm off 关闭）"
        if active
        else "未开启（可用 /swarm on 开启，或 /swarm <任务> 开启并立即派发）"
    )
    lines = [
        f"Swarm 协调模式：{state}",
        f"  活跃成员上限  {runtime.max_members} 个（后端模式：{runtime.teammate_mode}）",
    ]
    store = runtime.current
    if store is None:
        existing = runtime.teams()
        hint = f"（本项目已有 {len(existing)} 个历史团队，输入 /team 查看）" if existing else ""
        lines.append(f"  当前团队      无{hint}")
    else:
        running, idle = runtime.counts()
        try:
            team = store.load()
            total_members = len(team.members)
        except Exception:
            total_members = running + idle
        try:
            tasks = store.board.list()
            done_count = sum(1 for item in tasks if item.status == "completed")
            task_summary = f"{done_count}/{len(tasks)} 已完成"
        except Exception:
            task_summary = "无"
        posts, head = _recent_board_posts(store, limit=5)
        lines.append(
            f"  当前团队      {store.name}（成员 {total_members}：运行 {running} / 空闲 {idle} · 任务 {task_summary}）"
        )
        lines.append(f"  黑板消息      共 {head} 条" + (f"（最新 #{head}）" if head else ""))
        for seq, author, summary in posts:
            lines.append(f"    #{seq} · {author}: {summary or '（无摘要）'}")
    lines.append(SWARM_USAGE)
    return "\n".join(lines)


async def _swarm(ctx: CommandContext, argument: str) -> None:
    from ..coordinator import SWARM_OFF_NOTE, SWARM_ON_NOTE

    cleaned = argument.strip()
    lower = cleaned.lower()
    if not cleaned or lower == "status":
        await ctx.ui.note(await asyncio.to_thread(swarm_report, ctx.conversation))
        return
    if lower == "on":
        try:
            changed = ctx.conversation.set_coordinator_mode(True)
        except ProviderError as exc:
            await ctx.ui.note(f"└ {exc}")
            return
        ctx.ui.refresh_status()
        if changed:
            await ctx.ui.note(f"└ {SWARM_ON_NOTE}。")
        else:
            await ctx.ui.note("└ Swarm 协调模式已经处于开启状态（可用 /swarm off 关闭）。")
        return
    if lower == "off":
        try:
            changed = ctx.conversation.set_coordinator_mode(False)
        except ProviderError as exc:
            await ctx.ui.note(f"└ {exc}")
            return
        ctx.ui.refresh_status()
        await ctx.ui.note(f"└ {SWARM_OFF_NOTE}" if changed else "└ Swarm 协调模式当前未开启。")
        return
    try:
        changed = ctx.conversation.set_coordinator_mode(True)
    except ProviderError as exc:
        await ctx.ui.note(f"└ {exc}")
        return
    ctx.ui.refresh_status()
    if changed:
        await ctx.ui.note(f"└ {SWARM_ON_NOTE}。")
    await ctx.ui.submit(cleaned, command=f"/swarm {cleaned}")


def _swarm_names(ctx: CommandContext):
    return [
        ("status", "查看 Swarm 协调状态与黑板摘要"),
        ("on", "开启本会话 Swarm 协调模式"),
        ("off", "关闭本会话 Swarm 协调模式"),
    ]


def _swarm_reads(argument: str) -> bool:
    """Viewing status is safe while a turn runs; toggling or submitting a task waits."""
    return argument.strip().lower() in ("", "status")


async def _team(ctx: CommandContext, argument: str) -> None:
    from ..teams import TeamError

    runtime = getattr(ctx.conversation, "teams", None)
    if runtime is None:
        await ctx.ui.note("当前会话不支持团队。")
        return
    words = argument.split()
    try:
        if not words:
            await ctx.ui.note(await asyncio.to_thread(team_report, ctx.conversation))
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
                f"删除团队 {name}？会停止它的成员，删除已合并且干净的工作树和团队目录（任务、黑板、邮箱、对话记录）。",
                DELETE_OPTIONS,
                1,
                picked,
                cancelled,
            )
        elif len(words) == 1 and words[0] not in ("stop", "delete"):
            await ctx.ui.note(await asyncio.to_thread(team_report, ctx.conversation, words[0]))
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
