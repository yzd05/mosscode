"""Session lifecycle, checkpoint rewind, and memory slash commands."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from rich.text import Text

from ..commands import CommandContext
from ..memory import SCOPE_LABELS
from ..messages import ProviderError
from ..session import _clean_title, count_messages, fork_session, read_title, write_title
from .common import CLEARED, DEFAULT_MUTED_COLOR, _first_line, _muted, choice_row, confirm_tasks


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


def _session_rows(sessions, current: str | None, *, muted_color: str = DEFAULT_MUTED_COLOR) -> list[Text]:
    rows = []
    for item in sessions:
        mark = "  （当前）" if item.id == current else "  （使用中）" if item.locked else ""
        rows.append(
            choice_row(
                f"{item.modified:%m-%d %H:%M}  {item.title}",
                f"· {item.messages} 条 · {item.id[-4:]}{mark}",
                muted_color=muted_color,
            )
        )
    return rows


async def _resume(ctx: CommandContext, argument: str) -> None:
    if not argument:
        await open_sessions(ctx)
        return
    conversation = ctx.conversation
    reason = conversation.unavailable()
    if reason:
        await ctx.ui.note(reason)
        return
    try:
        exact = conversation.resolve_session(argument)
    except ProviderError as exc:
        not_found = str(exc)
    else:
        await confirm_tasks(ctx.ui, conversation, lambda: _resume_now(ctx, exact))
        return
    sessions = await asyncio.to_thread(conversation.sessions, 100)
    needle = argument.casefold()
    matched = [item for item in sessions if needle in item.title.casefold() or needle in item.id.casefold()]
    if not matched:
        await ctx.ui.note(not_found)
        return
    current = conversation.session_id
    rows = _session_rows(matched, current, muted_color=_muted(ctx.ui))

    async def pick(index: int) -> None:
        await confirm_tasks(ctx.ui, conversation, lambda: _resume_now(ctx, matched[index].id))

    default = next((i for i, item in enumerate(matched) if item.id != current), 0)
    ctx.ui.choose(f"恢复会话 · 匹配“{argument}”", rows, default, pick)


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
    rows = _session_rows(sessions, current, muted_color=_muted(ctx.ui))

    async def pick(index: int) -> None:
        await confirm_tasks(ctx.ui, conversation, lambda: _resume_now(ctx, sessions[index].id))

    ctx.ui.choose(
        "恢复会话 · 本项目最近的会话", rows, next((i for i, item in enumerate(sessions) if item.id != current), 0), pick
    )


async def _rename(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    session = conversation.session
    if session is None:
        await ctx.ui.note("本次运行没有会话。")
        return
    if not conversation.persist or session.transcript_path is None:
        await ctx.ui.note("会话存档已关闭（persist_sessions: false），不能重命名会话。")
        return
    clean_title = _clean_title(argument)
    if not clean_title:
        await ctx.ui.note("/rename 需要参数。用法：/rename <标题>")
        return
    try:
        await asyncio.to_thread(
            write_title,
            session,
            clean_title,
            transcript=getattr(conversation, "_transcript", None),
            cwd=conversation.context_root,
        )
    except (OSError, ValueError) as exc:
        await ctx.ui.note(f"! 无法重命名会话：{exc}")
        return
    await ctx.ui.note(f"└ 已将当前会话重命名为：{clean_title}")


async def _fork_session(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    session = conversation.session
    if session is None:
        await ctx.ui.note("本次运行没有会话。")
        return
    if not conversation.persist or session.transcript_path is None:
        await ctx.ui.note("会话存档已关闭（persist_sessions: false），不能创建会话分支。")
        return
    if not session.transcript_path.is_file() or count_messages(session.transcript_path) == 0:
        await ctx.ui.note("当前会话尚无已保存的消息，请先发送消息后再使用 /fork。")
        return
    await confirm_tasks(ctx.ui, conversation, lambda: _do_fork(ctx, argument))


async def _do_fork(ctx: CommandContext, argument: str) -> None:
    conversation = ctx.conversation
    session = conversation.session
    assert session is not None
    original_id = session.id
    root = conversation.context_root or Path.cwd()
    try:
        forked = await asyncio.to_thread(
            fork_session,
            session,
            root,
            home=conversation.sessions_home,
            title=argument,
        )
    except (OSError, ValueError) as exc:
        await ctx.ui.note(f"! 无法创建会话分支：{exc}")
        return
    branch_title = await asyncio.to_thread(read_title, forked.transcript_path)
    await ctx.ui.note(
        f"└ 已从当前会话创建分支 {forked.id}（{branch_title}）；原会话 {original_id} 保持不变，可用 /resume {original_id} 切回。"
    )
    ctx.ui.resume(forked.id)


# (label, restore code, rewind the conversation); a message with no file changes offers the conversation only.
REWIND_ACTIONS = (("恢复代码和对话", True, True), ("只恢复对话", False, True), ("只恢复代码", True, False))
REWIND_TALK = (("只恢复对话", False, True),)
REWIND_LISTED = 10


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
        choice_row(
            _first_line(point.message.shown, "（空消息）"),
            f"· 之后改动 {point.files} 个文件" if point.files else "",
            muted_color=_muted(ctx.ui),
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

        title = _first_line(point.message.shown, "（空消息）")
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
