"""Sub-agent definitions: Markdown roles with YAML frontmatter, in three places."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Literal

from ..paths import mosscode_home
from ..permissions import ALIASES, MODES
from ..skills import NAME, NAME_LIMIT, SkillError, _field, _read, _split, _text, split_tool_list

Source = Literal["builtin", "user", "project"]
SOURCE_LABELS = {"builtin": "内置", "user": "用户", "project": "项目"}
DESCRIPTION_LIMIT = 1024
CATALOG_DESCRIPTION = 250
CATALOG_LIMIT = 8000
FORK = "fork"
# snake_case first, then the Claude Code and hyphen spellings.
FIELDS = {
    "name": ("name",),
    "description": ("description",),
    "tools": ("tools",),
    "disallowed_tools": ("disallowed_tools", "disallowedTools", "disallowed-tools"),
    "model": ("model",),
    "max_turns": ("max_turns", "maxTurns", "max-turns"),
    "permission_mode": ("permission_mode", "permissionMode", "permission-mode"),
    "background": ("background",),
    "isolation": ("isolation",),
}
# Claude Code fields MossCode does not support; ignored and reported.
ISOLATIONS = ("none", "worktree")
UNSUPPORTED = ("color", "effort", "skills", "mcpServers", "hooks", "memory", "initialPrompt")


class AgentError(ValueError):
    """One definition file is unusable; the message names the file and the field."""


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    prompt: str  # The body: the sub-agent's system prompt.
    path: Path
    source: Source
    tools: tuple[str, ...] = ()  # Aliases expanded; empty means no whitelist.
    disallowed_tools: tuple[str, ...] = ()
    model: str | None = None  # None: inherit.
    max_turns: int | None = None
    permission_mode: str | None = None
    background: bool = False
    isolation: str = "none"  # "worktree" runs it in its own git worktree.
    unsupported: tuple[str, ...] = ()
    overrides: tuple[Path, ...] = ()  # Same-named files this one replaced.

    def allows(self, tool: str) -> bool:
        """The blacklist first, then the whitelist."""
        if any(_matches(tool, pattern) for pattern in self.disallowed_tools):
            return False
        return not self.tools or any(_matches(tool, pattern) for pattern in self.tools)

    def unknown_tools(self, available: Iterable[str]) -> list[str]:
        """Names that match no registered tool now (MCP servers may not be connected yet)."""
        available = tuple(available)
        return [
            pattern
            for pattern in (*self.tools, *self.disallowed_tools)
            if not any(_matches(name, pattern) for name in available)
        ]


def _matches(tool: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(tool, pattern) if "*" in pattern else tool == pattern


def _tools(meta: dict, path: Path, *names: str) -> tuple[str, ...]:
    key, value = _field(meta, path, *names)
    if value is None:
        return ()
    if isinstance(value, str):
        raw = split_tool_list(value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = [item.strip() for item in value if item.strip()]
    else:
        raise AgentError(f"{path}：{key} 必须是工具名列表或用逗号分隔的字符串。")
    items = []
    for item in raw:
        name = item.split("(", 1)[0].strip()  # Bash(git:*) -> Bash: the argument part is ignored.
        items.append(ALIASES.get(name, name))
    return tuple(dict.fromkeys(items))


def parse_agent(path: Path, source: Source) -> AgentDef:
    try:
        meta, body = _split(_read(path), path)
        known = {name for names in FIELDS.values() for name in names}
        unknown = [key for key in meta if key not in known and key not in UNSUPPORTED]
        if unknown:
            raise AgentError(f"{path}：未知字段 {', '.join(map(str, unknown))}。")
        name = _text(meta, path, "name", required=True)
        description = " ".join(_text(meta, path, "description", required=True).split())
        model = _text(meta, path, "model") or None
    except SkillError as exc:
        raise AgentError(str(exc)) from None
    if not NAME.fullmatch(name) or len(name) > NAME_LIMIT:
        raise AgentError(f"{path}：name 须以小写字母开头，只含小写字母、数字和 -，最长 {NAME_LIMIT} 个字符。")
    if name == FORK:
        raise AgentError(f"{path}：name 不能是 fork（省略类型即为 Fork 式）。")
    if len(description) > DESCRIPTION_LIMIT:
        raise AgentError(f"{path}：description 超过 {DESCRIPTION_LIMIT} 个字符。")
    if not body:
        raise AgentError(f"{path}：正文为空。")
    try:
        tools = _tools(meta, path, *FIELDS["tools"])
        disallowed = _tools(meta, path, *FIELDS["disallowed_tools"])
        key, turns = _field(meta, path, *FIELDS["max_turns"])
        mode_key, mode = _field(meta, path, *FIELDS["permission_mode"])
    except SkillError as exc:
        raise AgentError(str(exc)) from None
    if turns is not None and (type(turns) is not int or turns <= 0):
        raise AgentError(f"{path}：{key} 必须是正整数。")
    if mode is not None and mode not in MODES:
        raise AgentError(f"{path}：{mode_key} 只能是 {' / '.join(MODES)}，当前为 {mode!r}。")
    background = meta.get("background", False)
    if not isinstance(background, bool):
        raise AgentError(f"{path}：background 必须是 true 或 false。")
    isolation = meta.get("isolation", "none")
    if isolation not in ISOLATIONS:
        raise AgentError(f"{path}：isolation 只能是 {' / '.join(ISOLATIONS)}，当前为 {isolation!r}。")
    if model == "inherit":
        model = None
    return AgentDef(
        name,
        description,
        body,
        path,
        source,
        tools,
        disallowed,
        model,
        turns,
        mode,
        background,
        isolation,
        tuple(key for key in UNSUPPORTED if key in meta),
    )


# Catalog ----------------------------------------------------------------------------------


def builtin_dir() -> Path:
    return Path(str(resources.files(__name__).joinpath("builtin")))


def agent_dirs(root: Path, home: Path | None = None, builtin: Path | None = None) -> list[tuple[Source, Path]]:
    """Lowest priority first."""
    return [
        ("builtin", builtin if builtin is not None else builtin_dir()),
        ("user", mosscode_home(home) / "agents"),
        ("project", root / ".mosscode" / "agents"),
    ]


@dataclass
class AgentCatalog:
    agents: dict[str, AgentDef] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    _origin: tuple = field(default=(), repr=False)

    @classmethod
    def load(cls, root: Path, home: Path | None = None, *, builtin: Path | None = None) -> AgentCatalog:
        """A later place replaces a same-named definition; within a place the first file name wins."""
        agents: dict[str, AgentDef] = {}
        problems: list[str] = []
        for source, base in agent_dirs(root, home, builtin):
            try:
                files = sorted(child for child in base.iterdir() if child.is_file() and child.suffix == ".md")
            except OSError:
                continue
            seen: dict[str, Path] = {}
            for path in files:
                try:
                    agent = parse_agent(path, source)
                except AgentError as exc:
                    problems.append(f"Agent 定义已跳过：{exc}")
                    continue
                if agent.name in seen:
                    problems.append(f"Agent 定义已跳过：{path}：name {agent.name} 与 {seen[agent.name]} 重复。")
                    continue
                seen[agent.name] = path
                previous = agents.get(agent.name)
                if previous is not None:
                    agent = replace(agent, overrides=previous.overrides + (previous.path,))
                agents[agent.name] = agent
        return cls(dict(sorted(agents.items())), tuple(problems), _origin=(root, home, builtin))

    def reload(self) -> AgentCatalog:
        if not self._origin:
            return self
        root, home, builtin = self._origin
        return AgentCatalog.load(root, home, builtin=builtin)

    def get(self, name: str) -> AgentDef | None:
        return self.agents.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.agents)

    def catalog_section(self) -> str:
        """What the model sees; the agent tool's own description never lists types."""
        lines, size = [], 0
        for agent in self.agents.values():
            description = agent.description
            if len(description) > CATALOG_DESCRIPTION:
                description = description[: CATALOG_DESCRIPTION - 1] + "…"
            line = f"- {agent.name}：{description}（工具：{tool_summary(agent)}）"
            if size + len(line) + 1 > CATALOG_LIMIT:
                continue
            lines.append(line)
            size += len(line) + 1
        text = (
            "## 子 Agent\n用 agent 工具把可以独立完成的工作交给子 Agent，主对话只收到结论。"
            "agent_type 写下列类型之一时，子 Agent 从空白对话开始，按该类型的角色工作，prompt 要写清楚完整的任务和背景；"
            "省略 agent_type 时为 Fork：分身继承当前对话的全部内容，在后台运行，完成后通知你。"
            "多个互不依赖的子任务可以在一次回复中同时发起。"
        )
        if lines:
            text += "\n\n" + "\n".join(lines)
        return text


def tool_summary(agent: AgentDef) -> str:
    parts = []
    if agent.permission_mode == "plan":
        parts.append("只读")
    if agent.tools:
        parts.append("、".join(agent.tools))
    elif not parts:
        parts.append("全部")
    if agent.disallowed_tools:
        parts.append("不含 " + "、".join(agent.disallowed_tools))
    if agent.isolation == "worktree":
        parts.append("在独立的工作树中运行")
    return "，".join(parts)
