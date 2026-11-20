"""Model, reasoning, permission mode, sandbox, and status slash commands."""

from __future__ import annotations

import re
from pathlib import Path

from rich.cells import cell_len

from ..catalog import ModelInfo, load_catalog, lookup, providers_for, tokens_label
from ..commands import CommandContext
from ..config import catalog_window
from ..follow import SOURCES as FOLLOWED
from ..messages import ProviderError
from ..permissions import MODE_BADGES, MODE_LABELS, MODES
from ..reasoning import DEFAULT, LEVEL_LABELS, LEVELS, Unsupported, thinking_options
from ..usage import cache_hit_rate, cost_text
from .common import PERMISSION_HELP, PLAN_ENTERED, _muted, choice_row
from .extensions import agents_line, hooks_line
from .session import extraction_state

# Model and modes --------------------------------------------------------------------------

_DATED_MODEL = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")
_NON_CHAT_HINTS = ("-audio-", "gpt-audio", "-realtime-", "computer-use", "-transcribe", "-tts")
MODEL_DETAILS = (
    "用法：\n"
    "  /model                  打开提供商或同服务模型选择列表\n"
    "  /model <提供商>         切换到已配置的提供商\n"
    "  /model <提供商>/<模型>  复用该提供商的凭据与服务地址，切换到指定模型\n"
    "  /model <模型>           切换到当前提供商支持的模型目录条目（任意模型名可用 /model :<模型>）"
)


def _providers(ctx: CommandContext):
    return ctx.settings.providers if ctx.settings else ()


def _current_provider_config(ctx: CommandContext, providers):
    current = ctx.ui.provider_name
    return (
        next((item for item in providers if item.name == current), None)
        or ctx.conversation.provider_config
        or (providers[0] if providers else None)
    )


def _sibling_models(config, active_model: str = "", limit: int = 25) -> list[tuple[str, ModelInfo]]:
    """Catalog models compatible with `config` when it comes from a preset or matched the catalog."""
    if config is None or config.follow or (config.preset is None and config.catalog is None):
        return []
    catalog = load_catalog()
    vendors = providers_for(config.base_url, config.protocol)
    seen: dict[str, ModelInfo] = {}
    if vendors:
        for info in catalog.models.values():
            if info.provider not in vendors or catalog_window(info) is None:
                continue
            model_id = info.name.removeprefix(f"{info.provider}/")
            if model_id != active_model and (
                model_id.startswith("ft:") or "/" in model_id or any(tag in model_id for tag in _NON_CHAT_HINTS)
            ):
                continue
            seen.setdefault(model_id, info)
    elif config.catalog is not None:
        vendor = config.catalog.provider
        for info in catalog.models.values():
            if info.provider != vendor or catalog_window(info) is None:
                continue
            bare = info.name.removeprefix(f"{vendor}/")
            if "/" in bare:
                continue
            model_id = f"{vendor}/{bare}"
            seen.setdefault(model_id, info)
    filtered = [
        (mid, info)
        for mid, info in seen.items()
        if mid == active_model or not (_DATED_MODEL.search(mid) and _DATED_MODEL.sub("", mid) in seen)
    ]
    if active_model and config.catalog is not None and all(mid != active_model for mid, _ in filtered):
        filtered.insert(0, (active_model, config.catalog))
    filtered.sort(key=lambda item: (item[0] != active_model, item[0]))
    return filtered[:limit]


def _resolve_catalog_override(argument: str, config, *, home: Path | None = None) -> str | None:
    if config is None or not any(ch in argument for ch in ("/", "-", ".")):
        return None
    catalog = load_catalog(home)
    info = lookup(argument, base_url=config.base_url, protocol=config.protocol, catalog=catalog)
    if info is None:
        return None
    vendors = providers_for(config.base_url, config.protocol)
    if vendors and info.provider not in vendors:
        return None
    return argument.removeprefix(f"{info.provider}/") if vendors else argument


def resolve_model_target(
    argument: str,
    providers,
    active,
    *,
    home: Path | None = None,
) -> tuple[str, str | None] | None:
    """Resolve `/model <arg>` to `(provider_name, model_override_or_none)`, or None when unknown."""
    cleaned = argument.strip()
    if not cleaned:
        return None
    if any(item.name == cleaned for item in providers):
        return cleaned, None
    if cleaned.startswith(":") and cleaned[1:].strip():
        if active is not None:
            return active.name, cleaned[1:].strip()
        return None
    if "/" in cleaned:
        prefix, _, rest = cleaned.partition("/")
        rest = rest.strip()
        if rest and any(item.name == prefix for item in providers):
            return prefix, rest
    override = _resolve_catalog_override(cleaned, active, home=home)
    if active is not None and override is not None:
        return active.name, override
    return None


async def _model(ctx: CommandContext, argument: str) -> None:
    providers = _providers(ctx)
    argument = argument.strip()
    if argument:
        active = _current_provider_config(ctx, providers)
        target = resolve_model_target(argument, providers, active)
        if target is not None:
            name, override = target
            if override is None:
                await ctx.ui.switch_model(name)
            else:
                await ctx.ui.switch_model(name, override)
            return
        names = "、".join(item.name for item in providers) or "无"
        await ctx.ui.note(f"未找到提供商 {argument}。可用：{names}")
        return
    current = ctx.ui.provider_name
    following = next((item for item in providers if item.name == current and item.follow), None)
    if len(providers) < 2 and following is not None:
        await ctx.ui.switch_model(current)  # Nothing to pick: read the followed tool's settings again.
        return
    if not providers:
        await ctx.ui.note(
            f"当前模型：{current}"
            + (f"（{ctx.ui.model}）" if ctx.ui.model else "")
            + "。在配置文件的 providers 中添加更多提供商后可以切换。"
        )
        return
    if len(providers) == 1:
        single = providers[0]
        active_model = ctx.ui.model or single.model
        siblings = _sibling_models(single, active_model)
        if len(siblings) >= 2:
            rows = [
                choice_row(
                    f"{single.name}/{mid}{'  ✓' if mid == active_model else ''}",
                    f"{tokens_label(info.context_window)} 窗口"
                    + (f" · 输出 {tokens_label(info.max_output_tokens)}" if info.max_output_tokens else ""),
                    muted_color=_muted(ctx.ui),
                )
                for mid, info in siblings
            ]

            async def pick_model(index: int) -> None:
                await ctx.ui.switch_model(single.name, siblings[index][0])

            selected = next((i for i, (mid, _) in enumerate(siblings) if mid == active_model), 0)
            ctx.ui.choose(f"切换模型 · {single.name}（对话历史保留）", rows, selected, pick_model)
            return
        await ctx.ui.note(
            f"当前模型：{current}"
            + (f"（{ctx.ui.model}）" if ctx.ui.model else "")
            + f"。可用 /model {single.name}/<模型名> 切换同服务的模型，或在配置文件的 providers 中添加更多提供商。"
        )
        return
    rows = [
        choice_row(
            f"{item.name}{'  ✓' if item.name == current else ''}",
            f"{item.protocol} / {ctx.ui.model if item.name == current and ctx.ui.model else item.model}"
            + (f" · 跟随 {FOLLOWED[item.follow]}" if item.follow else ""),
            muted_color=_muted(ctx.ui),
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
    choices = [(item.name, "切换到该提供商") for item in providers] if len(providers) > 1 else []
    active = _current_provider_config(ctx, providers)
    if active is not None and any(item.name == active.name for item in providers):
        active_model = ctx.ui.model or active.model
        choices.extend(
            (f"{active.name}/{mid}", f"{active.name} · {tokens_label(info.context_window)} 窗口")
            for mid, info in _sibling_models(active, active_model)
        )
    return choices


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
        choice_row(
            f"{value}{'  ✓' if value == current else ''}  {LEVEL_LABELS[value]}",
            thinking_detail(config, None if value == DEFAULT else value),
            muted_color=_muted(ctx.ui),
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
    from ..sandbox import current

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
    snapshot_fn = getattr(conversation, "context_snapshot", None)
    if not usage.requests and not getattr(conversation, "history", ()):
        return "尚未发送请求"
    snapshot = snapshot_fn() if snapshot_fn is not None else None
    if snapshot is None:
        estimate = conversation.context_estimate() if usage.requests else None
        if estimate is None:
            return "尚未发送请求"
        tokens, source, anchored, delta = estimate, "estimated", None, 0
    else:
        tokens = snapshot.tokens
        source = snapshot.source
        anchored = getattr(snapshot, "anchored_tokens", None)
        delta = getattr(snapshot, "delta_tokens", 0)
    budget = conversation.budget
    pct = round(100 * tokens / budget.effective)
    if source == "reported":
        head = f"{tokens:,} / {budget.effective:,} Token（{pct}%，服务端返回）"
    elif source == "mixed" and anchored is not None:
        head = f"约 {tokens:,} / {budget.effective:,} Token（{pct}%，服务端 {anchored:,} + 估算 {delta:+,}）"
    else:
        head = f"约 {tokens:,} / {budget.effective:,} Token（{pct}%，估算）"
    line = f"{head}，自动压缩阈值 {budget.auto_threshold:,}；已压缩 {conversation.compactions} 次"
    return line + ("；窗口未知，按 128K 估算" if not budget.configured else "")


def _usage_line(conversation) -> str:
    usage = conversation.total_usage
    if not usage.requests:
        return "尚未发送请求"
    line = (
        f"输入 {usage.input_tokens:,}（缓存命中 {usage.cached_input_tokens:,}）· 输出 {usage.output_tokens:,}"
        f" · 请求 {usage.requests} 次 · 费用 {cost_text(usage)}"
    )
    if (rate := cache_hit_rate(usage)) is not None:
        line += f" · 对话缓存命中率 {rate}%"
    if usage.side_requests:
        line += (
            f"；其中摘要、目标判断和记忆整理 {usage.side_requests} 次，输入 {usage.side_input_tokens:,}"
            f"（缓存命中 {usage.side_cached_input_tokens:,}），不计入命中率"
        )
    return line


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
    limits = conversation.run_budget
    if limits.enabled:
        requests = f"{limits.used_requests}/{limits.requests or '不限'} 次请求"
        tokens = f"{limits.used_tokens:,}/{limits.tokens or '不限'} Token"
        rows.append(("本次运行预算", requests + " · " + tokens + ("（部分用量未知）" if limits.unknown else "")))
    if conversation.hooks is not None:
        rows.append(("Hook", hooks_line(conversation.hooks)))
    if conversation.active_skills:
        rows.append(("已激活 Skill", "、".join(conversation.active_skills)))
    if registry is not None and getattr(conversation, "agents_enabled", False):
        rows.append(("子 Agent", agents_line(conversation)))
    active_loops = [t for t in conversation.tasks.running() if getattr(t, "loop", None) is not None]
    if active_loops:
        rows.append(("定时巡检", f"{len(active_loops)} 个运行中（/loop、/tasks 查看）"))
    if getattr(conversation, "coordinator_mode", getattr(conversation, "coordinator", False)):
        from ..coordinator import DYNAMIC_STATUS, STATUS

        role_status = STATUS if getattr(conversation, "_coordinator_configured", False) else DYNAMIC_STATUS
        rows.append(("角色", role_status))
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
