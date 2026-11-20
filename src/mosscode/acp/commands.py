"""ACP slash command and local command dispatch."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from acp import RequestError
from acp.schema import AllowedOutcome, PermissionOption, PromptResponse, ToolCallUpdate

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
    swarm_report,
    tasks_report,
    team_report,
    thinking_detail,
    typed,
)
from ..commands import argument_problem, parse_input, unknown_note
from ..coordinator import SWARM_OFF_NOTE, SWARM_ON_NOTE
from ..mcp_surface import (
    PromptArgumentError,
    expand_mcp_prompt,
    is_mcp_prompt_command,
    register_mcp_prompts_from_manager,
)
from ..messages import ProviderError
from ..permissions import MODES as PERMISSION_MODES
from ..session import _clean_title, write_title
from ..skills import ForkRequest
from .events import EXCLUDED_ACP_COMMANDS, todos_to_plan_update
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

if TYPE_CHECKING:
    from .session import AcpSession


async def handle_slash_command(
    session: AcpSession,
    text: str,
    context_parts: Sequence[str] = (),
) -> tuple[PromptResponse | None, str, str | None, ForkRequest | None, dict[str, Any]]:
    """Handle slash commands isolated from attached context blocks (`!` inputs stay plain messages)."""

    def _with_context(base: str) -> str:
        extra = "\n".join(p.strip() for p in context_parts if p and p.strip())
        return f"{base}\n\n{extra}" if extra else base

    parsed = parse_input(text, session.commands)
    if parsed.kind == "empty":
        extra = "\n".join(p.strip() for p in context_parts if p and p.strip())
        if extra:
            return None, extra, None, None, {}
        return PromptResponse(stop_reason="end_turn"), "", None, None, {}
    if parsed.kind in {"message", "shell"}:
        msg_text = parsed.text if parsed.kind == "message" else text
        return None, _with_context(msg_text), None, None, {}
    if parsed.kind == "unknown":
        await session._send_text(escape_markdown_inline(unknown_note(parsed, session.commands)))
        return PromptResponse(stop_reason="end_turn"), "", None, None, {}

    cmd = parsed.command
    assert cmd is not None
    args = parsed.argument.strip()

    if cmd.name in (EXCLUDED_ACP_COMMANDS - {"team"}):
        if cmd.name == "sandbox":
            if not args:
                raw_sb = "\n".join(session.sandbox.report(session.cwd))
                await session._send_text(format_terminal_report_markdown(raw_sb))
            else:
                await session._send_text(
                    "ACP 会话中请在启动 `moss acp` 时使用 `--sandbox` 或 `--no-sandbox` 控制命令沙箱。"
                )
            return PromptResponse(stop_reason="end_turn"), "", None, None, {}
        await session._send_text(format_help_markdown(session.commands, cmd.name, session.conversation.skills))
        return PromptResponse(stop_reason="end_turn"), "", None, None, {}

    if problem := argument_problem(cmd, parsed.argument):
        await session._send_text(escape_markdown_inline(problem))
        return PromptResponse(stop_reason="end_turn"), "", None, None, {}

    if cmd.kind == "prompt":
        if is_mcp_prompt_command(cmd):
            try:
                expanded, cmd_label, mcp_images = await expand_mcp_prompt(session.conversation, cmd, parsed.argument)
            except (PromptArgumentError, ProviderError) as exc:
                await session._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None, {}
            return None, _with_context(expanded), cmd_label, None, dict(mcp_images)
        return None, _with_context(cmd.expand(parsed.argument)), typed(cmd, parsed.argument), None, {}

    if cmd.kind == "skill":
        try:
            skill, rendered, notices = session.conversation.prepare_skill(cmd.skill, parsed.argument)
        except ProviderError as exc:
            await session._send_text(escape_markdown_inline(str(exc)))
            return PromptResponse(stop_reason="end_turn"), "", None, None, {}
        if notices:
            await session._send_text(format_notices_markdown(notices))
        cmd_label = typed(cmd, parsed.argument)
        combined_rendered = _with_context(rendered)
        if skill.mode == "fork":
            return None, combined_rendered, cmd_label, ForkRequest(skill, combined_rendered), {}
        return None, combined_rendered, cmd_label, None, {}

    immediate, local_prompt, local_label, local_fork = await session._handle_local_command(cmd.name, args)
    if immediate is None and local_prompt:
        local_prompt = _with_context(local_prompt)
        if local_fork is not None:
            local_fork = ForkRequest(local_fork.skill, local_prompt)
    return immediate, local_prompt, local_label, local_fork, {}


async def handle_local_command(
    session: AcpSession,
    name: str,
    args: str,
) -> tuple[PromptResponse | None, str, str | None, ForkRequest | None]:
    if name == "help":
        target = args.strip().removeprefix("/").lower()
        if target in {"team", "teams"}:
            await session._send_text(
                "### `/team [子命令]`\n\n"
                "- **说明**：查看、停止、删除本项目的进程内 Agent 团队及黑板记录\n"
                "- **类型**：本地命令（直接返回结果，不调用模型）\n"
                "- **别名**：`/teams`\n"
                "- **用法**：`/team`、`/team <名称>`、`/team stop <成员>`、`/team delete <名称>`"
            )
        else:
            help_md = format_help_markdown(session.commands, args, session.conversation.skills)
            if not target and "`/team" not in help_md:
                help_md += "\n- `/team [子命令]` — 查看、停止、删除 Agent 团队（别名 `/teams`）"
            await session._send_text(help_md)
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "clear":
        session._team_notes.clear()
        session._due_loops.clear()
        notices = await session.conversation.clear(keep_session=True)
        await session.client.session_update(session_id=session.session_id, update=todos_to_plan_update(()))
        if notices:
            await session._send_text(format_notices_markdown(notices))
        await session._send_text(CLEARED)
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "compact":
        async with aclosing(session.conversation.compact(args)) as events:
            async for event in events:
                for u in session.mapper.map_event(event):
                    await session.client.session_update(session_id=session.session_id, update=u)
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "session":
        sessions = await asyncio.to_thread(session.conversation.sessions)
        await session._send_text(format_session_markdown(session.conversation, sessions))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "rename":
        cur_session = session.conversation.session
        if cur_session is None or not session.conversation.persist or cur_session.transcript_path is None:
            await session._send_text("会话存档已关闭（`persist_sessions: false`），不能重命名会话。")
            return PromptResponse(stop_reason="end_turn"), "", None, None
        clean_title = _clean_title(args)
        if not clean_title:
            await session._send_text("`/rename` 需要参数。用法：`/rename <标题>`")
            return PromptResponse(stop_reason="end_turn"), "", None, None
        prev_rename = getattr(session, "_rename_task", None)

        async def _write_title_serialized() -> None:
            if prev_rename is not None and not prev_rename.done():
                try:
                    await asyncio.shield(prev_rename)
                except Exception:
                    pass
            await asyncio.to_thread(
                write_title,
                cur_session,
                clean_title,
                transcript=getattr(session.conversation, "_transcript", None),
                cwd=session.cwd,
            )

        rename_task = asyncio.create_task(_write_title_serialized())
        rename_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        session._rename_task = rename_task
        try:
            await asyncio.shield(rename_task)
        except (OSError, ValueError) as exc:
            await session._send_text(f"无法重命名会话：{escape_markdown_inline(str(exc))}")
            return PromptResponse(stop_reason="end_turn"), "", None, None
        await session._send_text(f"已将当前会话重命名为：**{escape_markdown_inline(clean_title)}**。")
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "memory":
        await session._send_text(format_terminal_report_markdown(memory_report(session.conversation, args)))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "mcp":
        await session._send_text(format_terminal_report_markdown(mcp_report(session.conversation, args)))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "skills":
        if args == "reload" and session.conversation.registry is not None:
            old = session.conversation.skills
            catalog = await asyncio.to_thread(old.reload)
            problems = session.conversation.registry.set_skills(catalog)
            notices = session.conversation.use_skills(catalog)
            session.commands, skipped = BUILTIN.with_skills(catalog)
            register_mcp_prompts_from_manager(session.commands, session.conversation.mcp_manager)
            await session.send_initial_updates()
            lines = [f"已重新扫描 Skill：共 **`{len(catalog.skills)}`** 个。"]
            all_issues = (*catalog.problems, *problems, *notices, *skipped)
            if all_issues:
                lines.append("")
                lines.extend(f"- ⚠️ {escape_markdown_inline(p)}" for p in all_issues)
            await session._send_text("\n".join(lines))
        else:
            await session._send_text(
                format_skills_markdown(session.conversation.skills, args, session.conversation.active_skills)
            )
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "agents":
        if (
            args == "reload"
            and session.conversation.registry is not None
            and getattr(session.conversation, "agents_enabled", False)
        ):
            catalog = await asyncio.to_thread(session.conversation.agents.reload)
            session.conversation.use_agents(catalog)
            lines = [f"已重新加载子 Agent 定义：共 **`{len(catalog.agents)}`** 个，下一次请求起生效。"]
            if catalog.problems:
                lines.append("")
                lines.extend(f"- ⚠️ {escape_markdown_inline(p)}" for p in catalog.problems)
            await session._send_text("\n".join(lines))
        else:
            await session._send_text(format_agents_markdown(session.conversation, args))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "hooks":
        engine = session.conversation.hooks
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
            await session._send_text("\n".join(lines))
        else:
            await session._send_text(format_terminal_report_markdown(hooks_report(engine, args)))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "plugins":
        from ..plugin_command import report as plugins_report

        text = await asyncio.to_thread(plugins_report, session.options.home, args, session.cwd)
        await session._send_text(format_terminal_report_markdown(text))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "tasks":
        action, _, rest = args.partition(" ")
        if action == "stop":
            task_id = rest.strip()
            if not task_id:
                await session._send_text("用法：`/tasks stop <id>`")
            elif await session.conversation.tasks.stop(task_id):
                session._due_loops = [item for item in session._due_loops if item[0].id != task_id]
                await session._send_text(f"已停止任务 `{escape_markdown_inline(task_id)}`。")
            else:
                await session._send_text(f"任务 `{escape_markdown_inline(task_id)}` 不存在或已结束。")
        else:
            await session._send_text(format_terminal_report_markdown(tasks_report(session.conversation, args)))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "todo":
        await session._send_text(format_todo_markdown(session.conversation))
        if session.conversation.todos:
            await session.client.session_update(
                session_id=session.session_id,
                update=todos_to_plan_update(session.conversation.todos),
            )
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "model":
        if not args:
            await session._send_text(format_model_markdown(session.provider_config, session.settings))
        else:
            try:
                await session.set_config_option("model", args)
                await session._send_text(
                    f"已切换到 **`{session.provider_config.name}`**（`{session.provider_config.model}`）。"
                )
            except RequestError as exc:
                await session._send_text(escape_markdown_inline(str((exc.data or {}).get("reason", exc))))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "thinking":
        value = args.lower()
        if not value or value == "status":
            await session._send_text(format_thinking_markdown(session.provider_config, session.current_thinking))
        else:
            try:
                await session.set_config_option("thought_level", value)
                detail = escape_markdown_inline(thinking_detail(session.provider_config, session.current_thinking))
                await session._send_text(f"思考强度已设为 **`{value}`**（{detail}）。")
            except RequestError as exc:
                await session._send_text(escape_markdown_inline(str((exc.data or {}).get("reason", exc))))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "permissions":
        if args in PERMISSION_MODES:
            await session.set_mode(args)
            await session._send_text(
                f"权限档位已切换为：**{MODE_LABELS[session.current_mode]}**（`{session.current_mode}`）。"
            )
        else:
            await session._send_text(format_permissions_markdown(session.current_mode, invalid_arg=args))
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "plan":
        await session.set_mode("plan")
        if args:
            return None, args, None, None
        await session._send_text(PLAN_ENTERED)
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "do":
        policy = session.policy
        if policy is None or policy.mode != "plan":
            await session._send_text("当前不在计划模式。" + ("任务未发送。" if args else ""))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        await session.set_mode(policy.resume_mode)
        if args:
            return None, args, None, None
        await session._send_text(
            f"已退出计划模式，回到 **{MODE_LABELS[session.current_mode]}**（`{session.current_mode}`）。"
        )
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "status":
        await session._send_text(
            format_status_markdown(
                provider_config=session.provider_config,
                current_mode=session.current_mode,
                current_thinking=session.current_thinking,
                sandbox_badge=session.sandbox.badge(),
                cwd=session.cwd,
                session_id=session.session_id,
                context_estimate=session.conversation.context_estimate(),
                effective_budget=session.conversation.budget.effective,
                totals=session.conversation.total_usage,
            )
        )
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "goal":
        if not args or args == "status":
            await session._send_text(format_goal_markdown(session.conversation))
            goal = session.conversation.goal
            if goal is not None and session.conversation.resume_goal():
                return None, f"继续完成目标：{goal.condition}", "/goal", None
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if args.lower() in {"clear", "stop", "off", "cancel", "清除"}:
            cleared = session.conversation.clear_goal()
            await session._send_text("已清除目标。" if cleared is not None else "当前没有目标。")
            return PromptResponse(stop_reason="end_turn"), "", None, None
        try:
            goal = session.conversation.set_goal(args)
            await session._send_text(
                f"已设置自主目标：{escape_markdown_inline(goal.condition)}"
                f"（每轮结束后自动检查是否达成，最多 `{goal.budget}` 轮）\n\n"
            )
            return None, args, f"/goal {args}", None
        except (ValueError, ProviderError) as exc:
            await session._send_text(escape_markdown_inline(str(exc)))
            return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "loop":
        loops = session._ensure_loops()
        if loops is None:
            await session._send_text(escape_markdown_inline(LOOP_HEADLESS_NOTICE))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        try:
            from ..loop_schedule import parse_loop_args
        except ImportError:
            await session._send_text(escape_markdown_inline(LOOP_HEADLESS_NOTICE))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        try:
            parsed = parse_loop_args(args)
        except ValueError as exc:
            await session._send_text(escape_markdown_inline(str(exc)))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if parsed.action == "list":
            await session._send_text(format_terminal_report_markdown(loop_report(session.conversation)))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if parsed.action == "stop":
            res = loops.stop(parsed.target)
            stopped_tasks = await res if inspect.isawaitable(res) else res
            stopped_ids = {t.id for t in stopped_tasks}
            if stopped_ids:
                session._due_loops = [item for item in session._due_loops if item[0].id not in stopped_ids]
            if parsed.target.lower() == "all":
                if stopped_tasks:
                    ids = "、".join(f"`{escape_markdown_inline(t.id)}`" for t in stopped_tasks)
                    await session._send_text(f"已停止 `{len(stopped_tasks)}` 个定时巡检（{ids}）。")
                else:
                    await session._send_text("当前没有运行中的定时巡检。")
            elif stopped_tasks:
                target_task = stopped_tasks[0]
                await session._send_text(f"已停止定时巡检 `{escape_markdown_inline(target_task.id)}`。")
            else:
                existing = session.conversation.tasks.get(parsed.target)
                if existing is None or getattr(existing, "loop", None) is None:
                    await session._send_text(
                        f"没有定时巡检任务 `{escape_markdown_inline(parsed.target)}`。输入 `/loop list` 查看。"
                    )
                else:
                    state_text = STATUS_LABELS.get(existing.status, existing.status)
                    await session._send_text(
                        f"定时巡检 `{escape_markdown_inline(parsed.target)}` 已结束（{state_text}），无需停止。"
                    )
            return PromptResponse(stop_reason="end_turn"), "", None, None
        try:
            res = loops.schedule(parsed.interval, parsed.interval_text, parsed.prompt, max_runs=parsed.max_runs)
            task = await res if inspect.isawaitable(res) else res
        except (ValueError, ProviderError) as exc:
            await session._send_text(escape_markdown_inline(str(exc)))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        first_line = next((ln.strip() for ln in parsed.prompt.splitlines() if ln.strip()), parsed.prompt.strip())
        await session._send_text(
            f"已启动定时巡检 `{escape_markdown_inline(task.id)}`（每 {escape_markdown_inline(parsed.interval_text)}，"
            f"最多 `{parsed.max_runs}` 次）：{escape_markdown_inline(first_line[:80])}；"
            f"可用 `/tasks` 或 `/loop stop {escape_markdown_inline(task.id)}` 停止。"
        )
        return PromptResponse(stop_reason="end_turn"), "", None, None

    if name == "swarm":
        lower = args.lower()
        if not args or lower == "status":
            report = await asyncio.to_thread(swarm_report, session.conversation)
            await session._send_text(format_terminal_report_markdown(report))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if lower == "on":
            try:
                changed = session.conversation.set_coordinator_mode(True)
            except ProviderError as exc:
                await session._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            msg = f"{SWARM_ON_NOTE}。" if changed else "Swarm 协调模式已经处于开启状态（可用 `/swarm off` 关闭）。"
            await session._send_text(escape_markdown_inline(msg))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if lower == "off":
            try:
                changed = session.conversation.set_coordinator_mode(False)
            except ProviderError as exc:
                await session._send_text(escape_markdown_inline(str(exc)))
                return PromptResponse(stop_reason="end_turn"), "", None, None
            msg = SWARM_OFF_NOTE if changed else "Swarm 协调模式当前未开启。"
            await session._send_text(escape_markdown_inline(msg))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        try:
            changed = session.conversation.set_coordinator_mode(True)
        except ProviderError as exc:
            await session._send_text(escape_markdown_inline(str(exc)))
            return PromptResponse(stop_reason="end_turn"), "", None, None
        if changed:
            await session._send_text(f"{escape_markdown_inline(SWARM_ON_NOTE)}。\n\n")
        return None, args, f"/swarm {args}", None

    if name == "team":
        from ..teams import TeamError

        runtime = getattr(session.conversation, "teams", None)
        if runtime is None:
            await session._send_text("当前会话不支持团队。")
            return PromptResponse(stop_reason="end_turn"), "", None, None
        words = args.split()
        try:
            if not words:
                report = await asyncio.to_thread(team_report, session.conversation)
                await session._send_text(format_terminal_report_markdown(report))
            elif words[0] == "stop" and len(words) == 2:
                result_text = await runtime.stop_member(words[1])
                await session._send_text(escape_markdown_inline(result_text))
            elif words[0] == "delete" and len(words) == 2:
                team_name = words[1]
                await asyncio.to_thread(runtime.detail, team_name)
                resp = await session.client.request_permission(
                    session_id=session.session_id,
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
                    await session._send_text(escape_markdown_inline(deleted_text))
                else:
                    await session._send_text("已取消，团队保留。")
            elif len(words) == 1 and words[0] not in {"stop", "delete"}:
                report = await asyncio.to_thread(team_report, session.conversation, words[0])
                await session._send_text(format_terminal_report_markdown(report))
            else:
                await session._send_text(escape_markdown_inline(TEAM_USAGE))
        except TeamError as exc:
            await session._send_text(f"⚠️ {escape_markdown_inline(exc.message)}")
        return PromptResponse(stop_reason="end_turn"), "", None, None

    await session._send_text(f"暂不支持在 ACP 中运行命令 `/{name}`。")
    return PromptResponse(stop_reason="end_turn"), "", None, None
