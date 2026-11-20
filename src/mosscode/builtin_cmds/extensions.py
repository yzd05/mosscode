"""Extension, tool, sub-agent, and task inspection slash commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.cells import cell_len

from ..agents import claude_model
from ..agents.runtime import GLOBAL_BAN, WORKSPACE_TOOLS, effective_mode, stats
from ..agents.tasks import ORIGIN_LABELS, STATUS_LABELS
from ..commands import CommandContext
from ..goal import elapsed_text
from ..hooks.config import SOURCE_LABELS as HOOK_SOURCES
from ..mcp_surface import describe_mcp_status, format_mcp_capabilities, overview_capability_lines
from ..messages import ProviderError
from ..skills import source_label
from ..todo import render as render_todos
from ..usage import cost_text
from .common import _first_line

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
        lines = [f"MCP {status.name}（{status.type}）· {describe_mcp_status(status)}"]
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
        extra_lines = format_mcp_capabilities(status)
        lines.extend(extra_lines)
        if status.state in {"ready", "closed"} and not status.tools and not extra_lines:
            lines.append("  （Server 没有提供工具）")
        return "\n".join(lines)
    marks = {"ready": "●", "connecting": "○", "new": "○"}
    lines = [f"MCP Server（{len(statuses)}）"]
    width = max((len(item.name) for item in statuses), default=0)
    for item in statuses:
        lines.append(
            f"{marks.get(item.state, '×')} {item.name.ljust(width)}  {item.type:<5}  {describe_mcp_status(item)}"
        )
        lines.extend(overview_capability_lines(item))
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
    lines.append("输入 /<名称> 调用；/skills 名称 查看详情；/skills unload 名称 卸载；/skills reload 重新扫描。")
    return "\n".join(lines)


async def _skills(ctx: CommandContext, argument: str) -> None:
    cleaned = argument.strip()
    action, _, rest = cleaned.partition(" ")
    if action == "unload":
        target = rest.strip()
        if not target or " " in target:
            await ctx.ui.note("用法：/skills unload <名称>")
            return
        try:
            removed = ctx.conversation.unload_skill(target)
        except ProviderError as exc:
            await ctx.ui.note(str(exc))
            return
        if removed:
            ctx.ui.refresh_status()
            await ctx.ui.note(f"└ 已卸载 Skill {target}。")
        elif ctx.conversation.skills.get(target) is not None:
            await ctx.ui.note(f"Skill {target} 当前未激活。")
        else:
            await ctx.ui.note(f"未找到已激活的 Skill {target}。")
        return
    if cleaned != "reload":
        await ctx.ui.note(skills_report(ctx.conversation.skills, cleaned, ctx.conversation.active_skills))
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
    active = getattr(ctx.conversation, "active_skills", ())
    return [
        ("reload", "重新扫描 Skill 目录"),
        *((skill.name, skill.description) for skill in ctx.conversation.skills.skills.values()),
        *((f"unload {name}", "卸载已激活的 Skill") for name in active),
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
    running = [t for t in tasks.background() if getattr(t, "loop", None) is None]
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
    from ..plugins import report

    text = await asyncio.to_thread(report, None, argument.strip())
    await ctx.ui.note(
        text + "\n安装、更新、删除、停用在命令行中进行（moss plugin install|update|remove|disable …），"
        "变化在下一次启动 moss 时生效。"
    )


def _plugin_names(ctx: CommandContext):
    from ..plugins import load_records

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
        if getattr(task, "loop", None) is not None:
            return _loop_task_report(task)
        if task.command is not None:
            return _command_report(task)
        rows = [
            ("来源", ORIGIN_LABELS.get(task.origin, task.origin)),
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
        loop_info = getattr(task, "loop", None)
        if loop_info is not None:
            wait_note = (
                " · 等待空闲触发" if task.status == "running" and getattr(loop_info, "pending_fire", False) else ""
            )
            amount = f"每 {loop_info.interval_text} · 已运行 {loop_info.runs}/{loop_info.max_runs} 次{wait_note}"
        elif task.command is not None:  # A command has no tokens; its output size instead.
            from ..tools.background import size_text

            amount = f"输出 {size_text(task.command.log.size)}"
        else:
            amount = f"Token {task.tokens:,}"
        lines.append(
            f"  {task.id:<4} {STATUS_LABELS[task.status]}  {place}  {task.label}  {task.elapsed:.0f} 秒  {amount}"
        )
    lines.append("/tasks <id> 查看详情；/tasks stop <id> 停止。")
    return "\n".join(lines)


def _loop_task_report(task) -> str:
    from time import monotonic

    info = task.loop
    status_label = STATUS_LABELS.get(task.status, task.status)
    if task.status == "running":
        if getattr(info, "pending_fire", False):
            next_text = "等待空闲触发"
        elif getattr(info, "next_fire", None) is not None:
            next_text = f"{elapsed_text(max(0.0, info.next_fire - monotonic()))} 后"
        else:
            next_text = "排队中"
    else:
        next_text = getattr(info, "end_reason", "") or (
            "已手动停止" if getattr(task, "stopped", False) else status_label
        )
    rows = [
        ("来源", ORIGIN_LABELS.get(task.origin, "/loop 定时")),
        ("类型", "定时巡检"),
        ("间隔", f"每 {info.interval_text}"),
        ("状态", status_label),
        ("已运行", f"{info.runs}/{info.max_runs} 次"),
        ("下次触发" if task.status == "running" else "结束原因", next_text),
        ("已持续", f"{task.elapsed:.0f} 秒"),
    ]
    lines = [f"任务 {task.id}", *(f"  {label}{' ' * (10 - cell_len(label))}{value}" for label, value in rows)]
    lines.append("  巡检内容")
    lines.extend("    " + line for line in _preview(info.prompt, 6, 600))
    if task.result:
        lines.append("  结果")
        lines.extend("    " + line for line in _preview(task.result, 6, 600))
    if task.status == "running":
        lines.append(f"/loop stop {task.id} 或 /tasks stop {task.id} 停止。")
    return "\n".join(lines)


def _command_report(task) -> str:
    from ..tools.background import OUTPUT_LINES, size_text, status_text

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
