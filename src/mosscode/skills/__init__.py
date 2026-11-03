"""Skills: reusable procedures in SKILL.md directories, optionally with tools in tool.json."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml

from ..paths import mosscode_home

# Agent Skills specification: no leading, trailing or doubled "-" (a "--" would also turn into the "__"
# that separates the parts of a skill tool name).
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")
NAME_LIMIT = 64
TOOL_SHORT_LIMIT = 32
TOOL_NAME_LIMIT = 64  # Provider tool-name limit.
FILE_LIMIT = 64 * 1024
DEFAULT_TIMEOUT, MAX_TIMEOUT = 120.0, 600.0
DESCRIPTION_LIMIT = 250  # Shown to the model in the catalog; the rest is cut.
SPEC_DESCRIPTION_LIMIT = 1024
SPEC_COMPATIBILITY_LIMIT = 500
CATALOG_LIMIT = 8000
LOAD_SKILL = "load_skill"
# Names known before MCP connects. load_skill is a system tool and always allowed.
BUILTIN_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "execute_command",
        "glob",
        "grep",
        "memory",
        "exit_plan_mode",
        "web_fetch",
        "web_search",
    }
)
CLAUDE_TOOLS = {
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Bash": "execute_command",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
}
# Fields read below, plus those of the Agent Skills specification; anything else is ignored with a warning.
KNOWN_FIELDS = frozenset(
    {
        "name",
        "description",
        "allowed_tools",
        "allowed-tools",
        "mode",
        "context",
        "fork_context",
        "model",
        "argument-hint",
        "argument_hint",
        "disable-model-invocation",
        "disable_model_invocation",
        "user-invocable",
        "user_invocable",
        "license",
        "compatibility",
        "metadata",
    }
)
Source = Literal["builtin", "user", "project"]
SOURCE_LABELS = {"builtin": "内置", "user": "用户", "project": "项目"}


class SkillError(ValueError):
    """One skill directory is unusable; the message names the file and the field."""


@dataclass(frozen=True)
class SkillToolDef:
    name: str  # Short name, unique within the skill.
    full_name: str  # skill__<skill>__<name>
    description: str
    input_schema: dict
    command: tuple[str, ...]  # argv before ${SKILL_DIR} / ${PYTHON} substitution
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class SkillDef:
    name: str
    description: str
    body: str
    path: Path  # SKILL.md
    source: Source
    mode: Literal["inline", "fork"] = "inline"
    fork_context: Literal["none", "recent", "full"] = "none"
    allowed_tools: tuple[str, ...] = ()  # Claude Code names mapped; own tools expanded to full names.
    model: str | None = None
    argument_hint: str = ""
    model_invocable: bool = True
    user_invocable: bool = True
    tools: tuple[SkillToolDef, ...] = ()
    overrides: tuple[Path, ...] = ()  # Same-named SKILL.md files this one replaced.
    license: str = ""
    compatibility: str = ""
    warnings: tuple[str, ...] = ()  # Departures from the Agent Skills specification that do not stop loading.

    @property
    def directory(self) -> Path:
        return self.path.parent

    def render(self, arguments: str = "", *, fork: bool = False) -> str:
        """Where it comes from, the suggested tools (not for a fork child) and the body."""
        lines = [f"Skill {self.name}（目录：{self.directory}）"]
        if self.tools:
            lines.append("专属工具：" + "、".join(tool.full_name for tool in self.tools))
        if self.allowed_tools and not fork:
            lines.append("本 Skill 建议使用的工具：" + "、".join(self.allowed_tools))
        return "\n".join(lines) + "\n\n" + expand_arguments(self.body, arguments)


def expand_arguments(body: str, arguments: str) -> str:
    arguments = arguments.strip()
    if "$ARGUMENTS" in body:
        return body.replace("$ARGUMENTS", arguments)
    return body + (f"\n\n补充要求：{arguments}" if arguments else "")


def tool_full_name(skill: str, tool: str) -> str:
    return f"skill__{skill.replace('-', '_')}__{tool}"


# Parsing ----------------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SkillError(f"{path}：无法读取（{exc.strerror or exc}）。") from None
    if len(data) > FILE_LIMIT:
        raise SkillError(f"{path}：文件超过 64 KB。")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SkillError(f"{path}：不是 UTF-8 编码。") from None


def _split(text: str, path: Path) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        raise SkillError(f"{path}：缺少 YAML frontmatter（第一行应为 ---）。")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        raise SkillError(f"{path}：frontmatter 没有结束的 ---。")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}：frontmatter 不是合法的 YAML（{getattr(exc, 'problem', None) or exc}）。") from None
    if not isinstance(meta, dict):
        raise SkillError(f"{path}：frontmatter 必须是键值对。")
    return meta, "\n".join(lines[end + 1 :]).strip()


def _field(meta: dict, path: Path, *names: str):
    present = [name for name in names if name in meta]
    if len(present) > 1:
        raise SkillError(f"{path}：{' 和 '.join(present)} 只能写一个。")
    return (present[0], meta[present[0]]) if present else (names[0], None)


def _text(meta, path, *names, required=False) -> str:
    key, value = _field(meta, path, *names)
    if value is None:
        if required:
            raise SkillError(f"{path}：缺少 {key}。")
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise SkillError(f"{path}：{key} 必须是非空字符串。")
    return value.strip()


def _flag(meta, path, *names, default: bool) -> bool:
    key, value = _field(meta, path, *names)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SkillError(f"{path}：{key} 必须是 true 或 false。")
    return value


def _choice(meta, path, key, choices, default):
    value = meta.get(key)
    if value is None:
        return default
    if value not in choices:
        raise SkillError(f"{path}：{key} 只能是 {' / '.join(choices)}，当前为 {value!r}。")
    return value


def split_tool_list(value: str) -> list[str]:
    """Commas or whitespace separate names; both may appear inside Bash(git commit:*)."""
    items, current, depth = [], "", 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and (char == "," or char.isspace()):
            if current:
                items.append(current)
            current = ""
        else:
            current += char
    return items + ([current] if current else [])


def _allowed(meta, path) -> list[str]:
    key, value = _field(meta, path, "allowed_tools", "allowed-tools")
    if value is None:
        return []
    if isinstance(value, str):
        raw = split_tool_list(value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = [item.strip() for item in value if item.strip()]
    else:
        raise SkillError(f"{path}：{key} 必须是工具名列表或用逗号分隔的字符串。")
    names = []
    for item in raw:
        name = item.split("(", 1)[0].strip()  # Bash(git:*) -> Bash: the argument part is ignored.
        names.append(CLAUDE_TOOLS.get(name, name))
    return list(dict.fromkeys(names))


def _tool(item, index: int, skill: str, path: Path) -> SkillToolDef:
    where = f"{path} 第 {index + 1} 项"
    if not isinstance(item, dict):
        raise SkillError(f"{where}：必须是对象。")
    name = item.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name) or len(name) > TOOL_SHORT_LIMIT:
        raise SkillError(f"{where}：name 须以小写字母开头，只含小写字母、数字和 _，最长 {TOOL_SHORT_LIMIT} 个字符。")
    full = tool_full_name(skill, name)
    if len(full) > TOOL_NAME_LIMIT:
        raise SkillError(f"{where}：工具全名 {full} 超过 {TOOL_NAME_LIMIT} 个字符。")
    description = item.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(f"{where}（{name}）：description 必须是非空字符串。")
    schema = item.get("input_schema")
    try:
        from ..mcp_client import check_schema

        check_schema(schema)
    except Exception as exc:  # ValueError or jsonschema.SchemaError
        raise SkillError(
            f"{where}（{name}）：input_schema 不合法（{getattr(exc, 'message', None) or exc}）。"
        ) from None
    command = item.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise SkillError(f"{where}（{name}）：command 必须是非空字符串组成的非空数组。")
    timeout = item.get("timeout", DEFAULT_TIMEOUT)
    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT:
        raise SkillError(f"{where}（{name}）：timeout 必须是 0 到 {MAX_TIMEOUT:g} 之间的秒数。")
    return SkillToolDef(name, full, description.strip(), schema, tuple(command), float(timeout))


def _tools(directory: Path, skill: str) -> tuple[SkillToolDef, ...]:
    path = directory / "tool.json"
    if not path.is_file():
        return ()
    try:
        items = json.loads(_read(path))
    except ValueError as exc:
        raise SkillError(f"{path}：不是合法的 JSON（{exc}）。") from None
    if not isinstance(items, list):
        raise SkillError(f"{path}：必须是工具对象组成的数组。")
    tools = tuple(_tool(item, index, skill, path) for index, item in enumerate(items))
    names = [tool.name for tool in tools]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate:
        raise SkillError(f"{path}：工具名 {duplicate} 重复。")
    return tools


def _spec_warnings(meta: dict, path: Path, description: str) -> tuple[str, ...]:
    notes = []
    unknown = [str(key) for key in meta if key not in KNOWN_FIELDS]
    if unknown:
        notes.append(f"{path}：不认识的字段 {'、'.join(unknown)}，已忽略。")
    if len(description) > SPEC_DESCRIPTION_LIMIT:
        notes.append(f"{path}：description 超过 {SPEC_DESCRIPTION_LIMIT} 个字符（Agent Skills 规范的上限）。")
    license = meta.get("license")
    if license is not None and not (isinstance(license, str) and license.strip()):
        notes.append(f"{path}：license 应为非空字符串，已忽略。")
    compatibility = meta.get("compatibility")
    if compatibility is not None and not (
        isinstance(compatibility, str) and 1 <= len(compatibility.strip()) <= SPEC_COMPATIBILITY_LIMIT
    ):
        notes.append(f"{path}：compatibility 应为 1–{SPEC_COMPATIBILITY_LIMIT} 个字符的字符串，已忽略。")
    metadata = meta.get("metadata")
    if metadata is not None and not (
        isinstance(metadata, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())
    ):
        notes.append(f"{path}：metadata 应为字符串到字符串的映射。")
    return tuple(notes)


def _optional_text(meta: dict, key: str, limit: int | None = None) -> str:
    """A specification field MossCode only shows: an unusable value is dropped (and warned about)."""
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip() or (limit is not None and len(value.strip()) > limit):
        return ""
    return " ".join(value.split())


def parse_skill(directory: Path, source: Source) -> SkillDef:
    """Read and check one skill directory, its tool whitelist included."""
    path = directory / "SKILL.md"
    if not path.is_file():
        raise SkillError(f"{directory}：没有 SKILL.md。")
    meta, body = _split(_read(path), path)
    name = _text(meta, path, "name", required=True)
    if not NAME.fullmatch(name) or len(name) > NAME_LIMIT:
        raise SkillError(
            f"{path}：name 只能含小写字母、数字和 -，不能以 - 开头或结尾，不能有连续的 --，最长 {NAME_LIMIT} 个字符。"
        )
    if name != directory.name:
        raise SkillError(f"{path}：name（{name}）必须与目录名（{directory.name}）相同。")
    description = " ".join(_text(meta, path, "description", required=True).split())
    if not body:
        raise SkillError(f"{path}：正文为空。")
    mode = _choice(meta, path, "mode", ("inline", "fork"), None)
    context = _choice(meta, path, "context", ("fork",), None)
    if context and mode == "inline":
        raise SkillError(f"{path}：mode: inline 与 context: fork 矛盾。")
    mode = "fork" if context else mode or "inline"
    model = _text(meta, path, "model") or None
    tools = _tools(directory, name)
    own = {tool.name: tool.full_name for tool in tools}
    allowed = []
    for item in _allowed(meta, path):
        item = own.get(item, item)
        if item == LOAD_SKILL:
            continue  # A system tool: never narrowed, so listing it changes nothing.
        if item not in BUILTIN_TOOLS and item not in own.values() and not item.startswith("mcp__"):
            raise SkillError(f"Skill {name}：allowed_tools 中的 {item} 不是已知工具（{path}）。")
        allowed.append(item)
    return SkillDef(
        name,
        description,
        body,
        path,
        source,
        mode,
        _choice(meta, path, "fork_context", ("none", "recent", "full"), "none"),
        tuple(allowed),
        model,
        _text(meta, path, "argument-hint", "argument_hint"),
        not _flag(meta, path, "disable-model-invocation", "disable_model_invocation", default=False),
        _flag(meta, path, "user-invocable", "user_invocable", default=True),
        tools,
        license=_optional_text(meta, "license"),
        compatibility=_optional_text(meta, "compatibility", SPEC_COMPATIBILITY_LIMIT),
        warnings=_spec_warnings(meta, path, description),
    )


# Catalog ----------------------------------------------------------------------------------


def builtin_dir() -> Path:
    return Path(str(resources.files(__name__).joinpath("builtin")))


def skill_dirs(root: Path, home: Path | None = None, builtin: Path | None = None) -> list[tuple[Source, Path]]:
    """Lowest priority first. .agents/skills is shared with other agents (Agent Skills); at the same level
    MossCode's own directory wins."""
    return [
        ("builtin", builtin if builtin is not None else builtin_dir()),
        ("user", (Path.home() if home is None else home) / ".agents" / "skills"),
        ("user", mosscode_home(home) / "skills"),
        ("project", root / ".agents" / "skills"),
        ("project", root / ".mosscode" / "skills"),
    ]


@dataclass
class SkillCatalog:
    skills: dict[str, SkillDef] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    _latest: dict[str, SkillDef] = field(default_factory=dict, repr=False)
    _origin: tuple = field(default=(), repr=False)  # load() arguments, for reload().

    @classmethod
    def load(
        cls, root: Path, home: Path | None = None, *, reserved: Iterable[str] = (), builtin: Path | None = None
    ) -> SkillCatalog:
        """Scan every place; a later place replaces a same-named skill of an earlier one."""
        reserved = set(reserved)
        skills: dict[str, SkillDef] = {}
        problems: list[str] = []
        for source, base in skill_dirs(root, home, builtin):
            try:
                children = sorted(
                    child for child in base.iterdir() if child.is_dir() and not child.name.startswith(".")
                )
            except OSError:
                continue  # A missing place is not a problem.
            for directory in children:
                if directory.name == "__pycache__":
                    continue
                try:
                    skill = parse_skill(directory, source)
                except SkillError as exc:
                    problems.append(f"Skill 已跳过：{exc}")
                    continue
                if skill.name in reserved:
                    problems.append(f"Skill {skill.name} 已跳过：与内置命令 /{skill.name} 冲突（{skill.path}）。")
                    continue
                # skill.warnings stay out of problems: the skill works, and skills shared with other agents
                # often carry their fields; /skills lists them instead of every start announcing them.
                previous = skills.get(skill.name)
                if previous is not None:
                    skill = replace(skill, overrides=previous.overrides + (previous.path,))
                skills[skill.name] = skill
        return cls(dict(sorted(skills.items())), tuple(problems), _origin=(root, home, tuple(reserved), builtin))

    def reload(self) -> SkillCatalog:
        """Scan the same places again; a catalog that was not loaded from disk stays as it is."""
        if not self._origin:
            return self
        root, home, reserved, builtin = self._origin
        return SkillCatalog.load(root, home, reserved=reserved, builtin=builtin)

    def get(self, name: str) -> SkillDef | None:
        return self.skills.get(name)

    def fresh(self, name: str) -> tuple[SkillDef, str | None]:
        """Read the files again; on failure keep the last version that loaded."""
        cached = self._latest.get(name) or self.skills[name]
        try:
            skill = parse_skill(cached.directory, cached.source)
        except SkillError as exc:
            return cached, f"Skill {name} 重新读取失败，已使用缓存版本：{exc}"
        skill = replace(skill, overrides=cached.overrides)
        self._latest[name] = skill
        return skill, None

    def catalog_section(self) -> tuple[str, str | None]:
        """What the model sees before loading; returns the section and a notice when cut."""
        lines, size, left = [], 0, 0
        for skill in self.skills.values():
            if not skill.model_invocable:
                continue
            description = skill.description
            if len(description) > DESCRIPTION_LIMIT:
                description = description[: DESCRIPTION_LIMIT - 1] + "…"
            line = f"- {skill.name}：{description}"
            if size + len(line) + 1 > CATALOG_LIMIT:
                left += 1
                continue
            lines.append(line)
            size += len(line) + 1
        if not lines:
            return "", None
        text = (
            "## Skill\nSkill 是可复用的操作规程。用户的请求与下列某个 Skill 的描述相符时，先调用 load_skill 加载它，"
            "再按加载后的规程执行；不要凭描述猜测规程的内容。用户用 /<名称> 直接调用的 Skill 已附带规程，不需要再加载。\n\n"
            + "\n".join(lines)
        )
        notice = f"有 {left} 个 Skill 未列入模型可见的目录，仍可用命令调用。" if left else None
        return text, notice


def missing_tools(skill: SkillDef, available: Iterable[str]) -> list[str]:
    """Names that are not registered now; the skill's own tools count as available."""
    available = set(available) | {tool.full_name for tool in skill.tools}
    return [name for name in skill.allowed_tools if name not in available]


@dataclass(frozen=True)
class ForkRequest:
    """An explicit /<name> of a fork skill; prompt is its rendered SOP."""

    skill: SkillDef
    prompt: str


ACTIVE_LIMIT = 40000  # Characters of pinned SOPs in all.


@dataclass(frozen=True)
class ActiveSkill:
    skill: SkillDef
    arguments: str
    text: str  # Rendered SOP


class ActiveSkills:
    """Skills the model loaded, in load order; loading one again refreshes it in place."""

    def __init__(self) -> None:
        self._items: dict[str, ActiveSkill] = {}

    def __bool__(self) -> bool:
        return bool(self._items)

    def __contains__(self, name: str) -> bool:
        return name in self._items

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def items(self) -> tuple[ActiveSkill, ...]:
        return tuple(self._items.values())

    def texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self._items.values())

    def size(self, without: str = "") -> int:
        return sum(len(item.text) for name, item in self._items.items() if name != without)

    def activate(self, skill: SkillDef, arguments: str, text: str) -> None:
        self._items[skill.name] = ActiveSkill(skill, arguments, text)

    def remove(self, name: str) -> None:
        self._items.pop(name, None)

    def clear(self) -> None:
        self._items.clear()
