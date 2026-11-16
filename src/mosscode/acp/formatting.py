"""Markdown formatting helpers for ACP slash-command outputs and notices.

ACP clients (JetBrains IDEs, Zed, VS Code) render AgentMessageChunk payloads as
CommonMark / GitHub-Flavored Markdown, where single newlines collapse into spaces
and unescaped `<placeholder>` tokens are parsed as raw HTML tags. This module
renders ACP command responses as structured Markdown suited for narrow IDE chat
sidebars while keeping terminal TUI outputs in `builtin_commands.py` unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..agents import SOURCE_LABELS as AGENT_SOURCE_LABELS
from ..builtin_commands import (
    MODE_LABELS,
    agents_report,
    goal_report,
    thinking_detail,
)
from ..commands import Command, CommandRegistry
from ..config import ProviderConfig, Settings
from ..reasoning import DEFAULT as DEFAULT_THINKING
from ..reasoning import LEVEL_LABELS, LEVELS
from ..skills import SOURCE_LABELS as SKILL_SOURCE_LABELS
from ..skills import SkillCatalog, SkillDef
from ..todo import completed as completed_todos
from .events import EXCLUDED_ACP_COMMANDS, SESSION_MODES, supported_thinking_levels

_CODE_SPAN_SPLIT = re.compile(r"(`+[^`]*`+)")
_SENTENCE_END = re.compile(r"(?:[。！？；]|\.(?=\s+[A-Z\u4e00-\u9fff]))")
_KEY_VALUE_ROW = re.compile(r"^  (\S(?:.*?\S)?)\s{2,}(.+)$")

_HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("会话与上下文", ("help", "status", "session", "clear", "compact", "memory")),
    ("模式、模型与规划", ("plan", "do", "permissions", "model", "thinking", "goal", "todo")),
    ("扩展与诊断", ("mcp", "skills", "agents", "hooks", "plugins", "tasks")),
)

_EXCLUDED_HINTS: dict[str, str] = {
    "exit": "请直接关闭编辑器的会话标签页或聊天面板；ACP 服务端会随连接断开自动清理。",
    "resume": "请点击编辑器聊天面板右上角的历史会话图标切换或恢复过往会话。",
    "rewind": "对话与文件回退面板仅用于终端交互界面；在编辑器中可使用 Git 或本地历史记录恢复文件。",
    "worktree": "Git 工作树交互管理命令仅用于终端界面；你仍可在不同工作树目录中直接打开新的 ACP 会话。",
    "team": "Agent 团队多面板视图仅用于终端交互界面。",
}

ACP_DESCRIPTION_OVERRIDES: dict[str, str] = {
    "help": "查看可用命令列表或指定命令详情",
}


def escape_markdown_inline(text: str) -> str:
    """Escape `<` and `>` outside inline backtick code spans so Markdown keeps placeholders visible."""
    if "<" not in text and ">" not in text:
        return text
    parts = _CODE_SPAN_SPLIT.split(text)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            out.append(part)
        else:
            out.append(part.replace("<", r"\<").replace(">", r"\>"))
    return "".join(out)


def summarize_command_description(text: str, *, limit: int = 90) -> str:
    """Produce a concise single-line summary for `/help`, `/skills`, and `/` autocomplete menus."""
    cleaned = " ".join(text.split()).strip()
    if cleaned.endswith(" [skill]"):
        cleaned = cleaned[: -len(" [skill]")].rstrip()

    match = _SENTENCE_END.search(cleaned)
    if match is not None and 10 <= match.end() <= limit:
        return cleaned[: match.end()].strip()

    if len(cleaned) <= limit:
        return cleaned

    cut = cleaned[: limit - 1].rstrip()
    if " " in cut and cut[-1].isascii() and cleaned[limit - 1].isascii() and cleaned[limit - 1] != " ":
        last_space = cut.rfind(" ")
        if last_space >= limit // 2:
            cut = cut[:last_space].rstrip(" ,;:-")
    return f"{cut}…"


def _first_nonempty_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "（空）")


def _format_command_bullet(cmd: Command) -> str:
    raw_desc = ACP_DESCRIPTION_OVERRIDES.get(cmd.name, cmd.description)
    desc = escape_markdown_inline(summarize_command_description(raw_desc))
    aliases = "、".join(f"`/{alias}`" for alias in cmd.aliases)
    alias_suffix = f"（别名 {aliases}）" if aliases else ""
    tag = " `[skill]`" if cmd.kind == "skill" else ""
    return f"- `{cmd.usage}` — {desc}{alias_suffix}{tag}"


def format_help_markdown(
    registry: CommandRegistry,
    name: str = "",
    skills: SkillCatalog | None = None,
) -> str:
    """Render `/help` and `/help <command>` as clean Markdown tailored for ACP editors."""
    raw = name.strip()
    if raw:
        target = raw.removeprefix("/")
        command = registry.get(target)
        if command is None or command.hidden:
            return f"未知命令 `/{escape_markdown_inline(target)}`。输入 `/help` 查看可用命令。"

        if command.name == "sandbox":
            return "\n".join(
                [
                    "### `/sandbox`",
                    "",
                    "- **说明**：查看当前会话命令沙箱的后端状态、可写目录与网络边界",
                    "- **类型**：本地诊断命令（不经过模型）",
                    "- **开关方式**：在 ACP 编辑器配置启动参数中传入 `--sandbox` 或 `--no-sandbox`，或在 `config.yaml` 中设置 `sandbox.mode: on`",
                ]
            )

        if command.name in EXCLUDED_ACP_COMMANDS:
            hint = _EXCLUDED_HINTS.get(command.name, "该命令仅用于终端交互界面。")
            return f"### `/{command.name}`\n\n- **状态**：仅用于终端交互界面\n- **编辑器替代方式**：{hint}"

        skill: SkillDef | None = (
            skills.get(command.skill) if skills is not None and command.kind == "skill" and command.skill else None
        )
        if skill is not None:
            source = SKILL_SOURCE_LABELS.get(skill.source, skill.source)
            kind_text = f"Skill（`{skill.mode}` · 来源：{source}，输入 `/skills {skill.name}` 查看详情）"
            raw_desc = skill.description
        elif command.kind == "prompt":
            kind_text = "提示词命令（展开预设模板发送给模型）"
            raw_desc = command.description
        else:
            kind_text = "本地命令（直接返回结果，不调用模型）"
            raw_desc = ACP_DESCRIPTION_OVERRIDES.get(command.name, command.description)

        aliases = "、".join(f"`/{alias}`" for alias in command.aliases) or "无"
        lines = [
            f"### `{command.usage}`",
            "",
            f"- **说明**：{escape_markdown_inline(raw_desc)}",
            f"- **类型**：{kind_text}",
            f"- **别名**：{aliases}",
        ]
        if command.details:
            lines.append(f"- **补充说明**：{escape_markdown_inline(' '.join(command.details.split()))}")
        if command.kind == "prompt" and command.template.strip():
            lines.append(f"- **模板首行**：{escape_markdown_inline(command.template.strip().splitlines()[0])}")
        if skill is not None:
            lines.append(f"- **规程首行**：{escape_markdown_inline(_first_nonempty_line(skill.body))}")
        return "\n".join(lines)

    visible_by_name = {cmd.name: cmd for cmd in registry.visible() if cmd.name not in EXCLUDED_ACP_COMMANDS}
    placed: set[str] = set()
    sections: list[str] = ["### 可用命令"]

    for title, group_names in _HELP_GROUPS:
        items: list[str] = []
        for cmd_name in group_names:
            cmd = visible_by_name.get(cmd_name)
            if cmd is not None:
                placed.add(cmd.name)
                items.append(_format_command_bullet(cmd))
        if title == "扩展与诊断":
            items.append("- `/sandbox` — 查看当前命令沙箱状态与边界")
        if items:
            sections.append(f"\n#### {title}\n\n" + "\n".join(items))

    extra_cmds = [cmd for name_key, cmd in visible_by_name.items() if name_key not in placed]
    if extra_cmds:
        items = [_format_command_bullet(cmd) for cmd in extra_cmds]
        sections.append("\n#### Skill 与提示词命令\n\n" + "\n".join(items))

    sections.append(
        "\n> 提示：输入 `/help <命令>` 查看完整用法；也可直接在输入框下方切换 **Mode / Model / Thinking**，"
        "或用 `@` 引用文件。以 `//` 开头可将斜杠文字原样发给模型。"
    )
    return "\n".join(sections)


def format_status_markdown(
    *,
    provider_config: ProviderConfig,
    current_mode: str,
    current_thinking: str | None,
    sandbox_badge: str,
    cwd: Any,
    session_id: str,
    context_estimate: int | None,
    effective_budget: int,
    totals: Any,
) -> str:
    """Render `/status` as structured Markdown."""
    est_text = (
        f"`{context_estimate:,} / {effective_budget:,}` tokens" if context_estimate is not None else "尚未发送请求"
    )
    think = current_thinking or DEFAULT_THINKING
    think_label = LEVEL_LABELS.get(think, think)
    sb_text = sandbox_badge or "关闭"
    lines = [
        "### 当前会话状态",
        "",
        f"- **模型**：`{provider_config.name}`（`{provider_config.protocol}` / `{provider_config.model}`）",
        f"- **权限档位**：{MODE_LABELS.get(current_mode, current_mode)}（`{current_mode}`）",
        f"- **思考强度**：`{think}`（{think_label}）",
        f"- **命令沙箱**：{escape_markdown_inline(sb_text)}",
        f"- **工作目录**：`{cwd}`",
        f"- **会话 ID**：`{session_id}`",
        f"- **上下文估算**：{est_text}",
        f"- **累计用量**：输入 `{totals.input_tokens:,}` · 输出 `{totals.output_tokens:,}` · 请求 `{totals.requests}` 次",
    ]
    return "\n".join(lines)


def format_session_markdown(conversation: Any, sessions: Sequence[Any]) -> str:
    """Render `/session` as structured Markdown without terminal-only `/resume` hints."""
    session = conversation.session
    if session is None:
        return "当前会话未启用存档（`persist_sessions: false`）。"
    current = next((item for item in sessions if item.id == session.id), None)
    try:
        started = datetime.strptime(session.id[:15], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        started = "未知"
    archive = (
        f"`{session.transcript_path}`"
        if conversation.persist and session.transcript_path is not None
        else "未存档（`persist_sessions: false`）"
    )
    title = escape_markdown_inline(current.title) if current and current.title else "（尚无消息）"
    count = current.messages if current else len(conversation.history)
    lines = [
        "### 当前会话信息",
        "",
        f"- **会话 ID**：`{session.id}`",
        f"- **存档位置**：{archive}",
        f"- **会话标题**：{title}",
        f"- **消息数量**：`{count}` 条",
        f"- **创建时间**：`{started}`",
        "",
        "> 可通过编辑器聊天面板右上角的历史记录切换或加载本项目过往会话。",
    ]
    return "\n".join(lines)


def format_permissions_markdown(current_mode: str, invalid_arg: str = "") -> str:
    """Render `/permissions` overview (and optional invalid-mode warning) as Markdown."""
    lines: list[str] = []
    if invalid_arg:
        lines.extend([f"> ⚠️ 未知权限档位 `{escape_markdown_inline(invalid_arg)}`。", ""])
    lines.extend(
        [
            "### 权限档位",
            "",
            f"当前档位：**{MODE_LABELS.get(current_mode, current_mode)}**（`{current_mode}`）",
            "",
        ]
    )
    for mode in SESSION_MODES:
        mark = "（当前 ✓）" if mode.id == current_mode else ""
        lines.append(f"- `{mode.id}` — {escape_markdown_inline(mode.description or '')}{mark}")
    lines.extend(
        [
            "",
            "> 输入 `/permissions <档位>` 切换，或直接在输入框下方 **Mode** 下拉菜单中选择。",
        ]
    )
    return "\n".join(lines)


def format_model_markdown(current_provider: ProviderConfig, settings: Settings) -> str:
    """Render `/model` provider list as Markdown."""
    lines = [
        "### 模型提供商",
        "",
        f"当前使用：**`{current_provider.name}`**（`{current_provider.protocol}` / `{current_provider.model}`）",
        "",
    ]
    for item in settings.providers:
        mark = " **（当前 ✓）**" if item.name == current_provider.name else ""
        lines.append(f"- `{item.name}` — `{item.model}`（`{item.protocol}`）{mark}")
    lines.extend(
        [
            "",
            "> 输入 `/model <名称>` 切换，或直接在输入框下方 **Model** 下拉菜单中选择（对话历史保留）。",
        ]
    )
    return "\n".join(lines)


def format_thinking_markdown(provider_config: ProviderConfig, current_thinking: str | None) -> str:
    """Render `/thinking` status and available levels as Markdown."""
    cur = current_thinking or DEFAULT_THINKING
    label = LEVEL_LABELS.get(cur, cur)
    detail = escape_markdown_inline(thinking_detail(provider_config, current_thinking))
    supported = set(supported_thinking_levels(provider_config))
    lines = [
        "### 思考强度",
        "",
        f"当前设置：**`{cur}`**（{label}；{detail}）",
        "",
    ]
    for level in (DEFAULT_THINKING, *LEVELS):
        if level != DEFAULT_THINKING and supported and level not in supported:
            continue
        lvl_label = LEVEL_LABELS.get(level, level)
        mark = " **（当前 ✓）**" if level == cur else ""
        lines.append(f"- `{level}` — {lvl_label}{mark}")
    if not supported:
        lines.append("- 当前模型未启用思考强度覆盖参数（`reasoning: false`）。")
    lines.extend(
        [
            "",
            "> 输入 `/thinking <档位>` 设置，或直接在输入框下方 **Thinking** 下拉菜单中选择。",
        ]
    )
    return "\n".join(lines)


def format_skills_markdown(
    catalog: SkillCatalog,
    name: str = "",
    active: Sequence[str] = (),
) -> str:
    """Render `/skills` list or `/skills <name>` detail as clean Markdown."""
    target = name.strip().removeprefix("/")
    if target:
        skill = catalog.get(target)
        if skill is None:
            known = "、".join(f"`{k}`" for k in catalog.skills) or "无"
            return f"未找到 Skill `{escape_markdown_inline(target)}`。已加载：{known}"
        source = SKILL_SOURCE_LABELS.get(skill.source, skill.source)
        overrides = "、".join(f"`{p}`" for p in skill.overrides) or "无"
        mode_text = f"`{skill.mode}`" + (f"（`fork_context: {skill.fork_context}`）" if skill.mode == "fork" else "")
        allowed = "、".join(f"`{t}`" for t in skill.allowed_tools) or "未限制"
        visible = "是" if skill.model_invocable else "否（`disable-model-invocation`）"
        is_active = "是" if skill.name in active else "否"
        cmd_text = f"`/{skill.name}`" if skill.user_invocable else "不列出（`user-invocable: false`），仍可输入调用"
        lines = [
            f"### Skill `{skill.name}`",
            "",
            f"- **说明**：{escape_markdown_inline(skill.description)}",
            f"- **路径**：`{skill.path}`",
            f"- **来源**：{source}",
            f"- **覆盖**：{overrides}",
            f"- **模式**：{mode_text}",
            f"- **模型**：`{skill.model}`" if skill.model else "- **模型**：继承当前模型",
            f"- **工具白名单**：{allowed}",
            f"- **模型可见**：{visible}",
            f"- **已激活**：{is_active}",
            f"- **命令**：{cmd_text}",
            f"- **规程首行**：{escape_markdown_inline(_first_nonempty_line(skill.body))}",
        ]
        if skill.license:
            lines.append(f"- **许可证**：{escape_markdown_inline(skill.license)}")
        if skill.compatibility:
            lines.append(f"- **兼容性**：{escape_markdown_inline(skill.compatibility)}")
        if skill.tools:
            lines.extend(["", "#### 专属工具", ""])
            for tool in skill.tools:
                cmd_str = " ".join(tool.command)
                lines.append(f"- `{tool.full_name}` — `{cmd_str}`（超时 {tool.timeout:g} 秒）")
        if skill.warnings:
            lines.extend(["", "#### 写法提示（不影响使用）", ""])
            for warning in skill.warnings:
                lines.append(f"- ⚠️ {escape_markdown_inline(warning)}")
        return "\n".join(lines)

    if not catalog.skills and not catalog.problems:
        return "当前没有加载任何 Skill。可在 `.mosscode/skills/<名称>/SKILL.md` 中添加。"

    lines = [f"### 已加载 Skill（{len(catalog.skills)}）", ""]
    for skill in catalog.skills.values():
        source = SKILL_SOURCE_LABELS.get(skill.source, skill.source)
        desc = escape_markdown_inline(summarize_command_description(skill.description))
        lines.append(f"- `/{skill.name}`（{source} · `{skill.mode}`）— {desc}")
    if catalog.problems:
        lines.append("")
        for problem in catalog.problems:
            lines.append(f"- ⚠️ {escape_markdown_inline(problem)}")
    noted = [f"`{n}`" for n, s in catalog.skills.items() if s.warnings]
    if noted:
        lines.extend(["", f"> 提示：{'、'.join(noted)} 含写法提示（不影响使用），输入 `/skills <名称>` 查看。"])
    lines.extend(["", "> 输入 `/<名称>` 直接调用，`/skills <名称>` 查看详情，`/skills reload` 重新扫描。"])
    return "\n".join(lines)


def format_agents_markdown(conversation: Any, name: str = "") -> str:
    """Render `/agents` list or `/agents <name>` detail as Markdown."""
    target = name.strip()
    if target:
        return format_terminal_report_markdown(agents_report(conversation, target))
    catalog = conversation.agents
    if not catalog.agents and not catalog.problems:
        return "没有子 Agent 定义。可在 `~/.mosscode/agents/` 或 `.mosscode/agents/` 中添加 `.md` 文件。"
    lines = [f"### 子 Agent 定义（{len(catalog.agents)}）", ""]
    for agent in catalog.agents.values():
        source = AGENT_SOURCE_LABELS.get(agent.source, agent.source)
        desc = escape_markdown_inline(summarize_command_description(agent.description, limit=100))
        lines.append(f"- **`{agent.name}`**（{source}）— {desc}")
    for problem in catalog.problems:
        lines.append(f"- ⚠️ {escape_markdown_inline(problem)}")
    lines.extend(["", "> 模型通过 `agent` 工具按需调用；输入 `/agents <名称>` 查看详情，`/agents reload` 重新加载。"])
    return "\n".join(lines)


def format_todo_markdown(conversation: Any) -> str:
    """Render `/todo` checklist as GFM task items."""
    items = getattr(conversation, "todos", ())
    if not items:
        return "当前没有任务清单。模型处理多步任务时会通过 `todo_write` 建立清单并同步到编辑器计划面板。"
    done = completed_todos(items)
    lines = [f"### 任务清单（已完成 {done}/{len(items)}）", ""]
    for item in items:
        text = escape_markdown_inline(item.content)
        if item.status == "completed":
            lines.append(f"- [x] {item.id}. {text}")
        elif item.status == "in_progress":
            lines.append(f"- [ ] **{item.id}. {text}**（进行中）")
        else:
            lines.append(f"- [ ] {item.id}. {text}")
    return "\n".join(lines)


def format_goal_markdown(conversation: Any) -> str:
    """Render `/goal` status report as Markdown."""
    raw = goal_report(conversation)
    if "\n" not in raw:
        return escape_markdown_inline(raw)
    return format_terminal_report_markdown(raw)


def format_terminal_report_markdown(raw: str) -> str:
    """Convert structured multi-line MossCode terminal reports into readable Markdown.

    Handles single-line notices, section headers, two-space aligned `Key  Value` rows,
    bullet/status rows (`●`, `○`, `×`, `!`), and four-space indented YAML/command blocks.
    """
    raw = raw.replace("；/hooks reload 后可以确认。", "；请在终端确认或启动时传入 `--trust-project-hooks`。")
    stripped = raw.strip()
    if not stripped:
        return ""
    raw_lines = raw.rstrip().splitlines()
    if len(raw_lines) == 1:
        return escape_markdown_inline(raw_lines[0].strip())

    out: list[str] = []
    code_buf: list[str] = []

    def _flush_code() -> None:
        if not code_buf:
            return
        if out and out[-1] != "":
            out.append("")
        out.append("```text")
        out.extend(code_buf)
        out.append("```")
        out.append("")
        code_buf.clear()

    for idx, line in enumerate(raw_lines):
        if not line.strip():
            _flush_code()
            if out and out[-1] != "":
                out.append("")
            continue

        if line.startswith("    "):
            code_buf.append(line[4:])
            continue

        _flush_code()

        if idx == 0:
            out.append(f"### {escape_markdown_inline(line.strip())}")
            out.append("")
            continue

        kv_match = _KEY_VALUE_ROW.match(line)
        if kv_match is not None:
            key, val = kv_match.group(1).strip(), kv_match.group(2).strip()
            out.append(f"- **{escape_markdown_inline(key)}**：{escape_markdown_inline(val)}")
            continue

        trimmed = line.strip()
        if trimmed.startswith("! "):
            out.append(f"- ⚠️ {escape_markdown_inline(trimmed[2:].strip())}")
        elif line.startswith("  "):
            out.append(f"- {escape_markdown_inline(trimmed)}")
        elif trimmed.startswith(("● ", "○ ", "× ")):
            normalized_row = re.sub(r"\s{2,}", " · ", trimmed)
            out.append(f"- {escape_markdown_inline(normalized_row)}")
        elif idx == len(raw_lines) - 1 and (
            "查看" in trimmed or "用法" in trimmed or "重新" in trimmed or "添加" in trimmed
        ):
            if out and out[-1] != "":
                out.append("")
            out.append(f"> {escape_markdown_inline(trimmed)}")
        else:
            if out and out[-1] != "":
                out.append("")
            out.append(f"#### {escape_markdown_inline(trimmed)}")
            out.append("")

    _flush_code()
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def format_notices_markdown(notices: Sequence[str]) -> str:
    """Format one or more system notices as a clean Markdown blockquote with trailing spacing."""
    cleaned = [escape_markdown_inline(n.strip()) for n in notices if n and n.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f"\n> {cleaned[0]}\n\n"
    return "\n" + "\n".join(f"> - {item}" for item in cleaned) + "\n\n"
