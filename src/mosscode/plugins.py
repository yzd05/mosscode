"""Installed plugins: the records in ~/.mosscode/plugins.yaml and what each plugin directory holds.

A plugin is a directory laid out like a Claude Code plugin (skills/, commands/, agents/, .mcp.json and an optional
.claude-plugin/plugin.json), plus MossCode's own hooks.yaml and mcp.yaml. Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .paths import mosscode_home

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MANIFEST = Path(".claude-plugin") / "plugin.json"
MARKETPLACE = Path(".claude-plugin") / "marketplace.json"
RECORD_KEYS = {"name", "source", "ref", "path", "enabled"}
PROJECT_RECORD_KEYS = {"name", "source", "ref", "path"}  # Shared with the repository: where, never whether.
PLUGINS_TRUST = "plugins-trust.json"
# Manifest fields read; the metadata ones are shown or ignored; anything else is a component MossCode lacks.
MANIFEST_READ = {"name", "version", "description", "skills", "commands", "agents", "mcpServers"}
MANIFEST_METADATA = {"$schema", "author", "homepage", "repository", "license", "keywords", "displayName"}
# Conventional places of Claude Code components MossCode does not load.
UNSUPPORTED_PATHS = (
    "hooks/hooks.json",
    "bin",
    ".lsp.json",
    "output-styles",
    "monitors",
    "themes",
    "workflows",
    "settings.json",
)
COMPONENT_PATHS = ("skills", "SKILL.md", "commands", "agents", ".mcp.json", "mcp.yaml", "hooks.yaml", "hooks")
VARIABLE = re.compile(
    r"\$\{(MOSSCODE_PLUGIN_ROOT|CLAUDE_PLUGIN_ROOT|MOSSCODE_PLUGIN_DATA|CLAUDE_PLUGIN_DATA|CLAUDE_PROJECT_DIR)\}"
)
USER_CONFIG = "${user_config."
USER_CONFIG_PROBLEM = "插件需要填写配置（userConfig），MossCode 不支持"


class PluginError(ValueError):
    """A record, a manifest or a source that cannot be used; the message is shown as it is."""


def plugins_home(home: Path | None = None) -> Path:
    return mosscode_home(home) / "plugins"


def records_path(home: Path | None = None) -> Path:
    return mosscode_home(home) / "plugins.yaml"


def repo_dir(name: str, home: Path | None = None) -> Path:
    # Only the name: long nested paths run into the Windows path length limit.
    return plugins_home(home) / "repos" / name


def data_dir(name: str, home: Path | None = None) -> Path:
    return plugins_home(home) / "data" / name


def project_records_path(root: Path) -> Path:
    return Path(root) / ".mosscode" / "plugins.yaml"


def project_clone_dir(name: str, source: str, ref: str | None, home: Path | None = None) -> Path:
    # Keyed by source and ref too: two projects may give the same name to different repositories, and a
    # same-named user-level plugin keeps its own clone.
    key = hashlib.sha256(f"{source}\n{ref or ''}".encode()).hexdigest()[:8]
    return plugins_home(home) / "project" / f"{name}-{key}"


def source_kind(source: str) -> Literal["git", "local"]:
    if re.match(r"(?:https?|ssh|git|file)://", source) or re.match(r"[\w.-]+@[\w.-]+:", source):
        return "git"
    return "local"


# Variables --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginRef:
    """What a component needs to know about the plugin it came from."""

    name: str
    root: Path
    data: Path
    create: bool = field(default=True, compare=False)  # False while previewing: nothing is written.

    def _data(self) -> Path:
        if self.create:
            self.data.mkdir(parents=True, exist_ok=True)
        return self.data

    def substitute(self, text: str, project: Path | None = None) -> str:
        """${CLAUDE_PLUGIN_ROOT} and friends; forward slashes so that Git Bash keeps them."""
        if USER_CONFIG in text:
            raise PluginError(USER_CONFIG_PROBLEM)

        def value(match: re.Match) -> str:
            key = match[1]
            if key.endswith("_ROOT"):
                return self.root.as_posix()
            if key.endswith("_DATA"):
                return self._data().as_posix()
            return (project or Path.cwd()).as_posix()

        return VARIABLE.sub(value, text)

    def substitute_all(self, value: Any, project: Path | None = None) -> Any:
        """Every string inside a parsed YAML or JSON value."""
        if isinstance(value, str):
            return self.substitute(value, project)
        if isinstance(value, list):
            return [self.substitute_all(item, project) for item in value]
        if isinstance(value, dict):
            return {key: self.substitute_all(item, project) for key, item in value.items()}
        return value

    def env(self, project: Path | None = None) -> dict[str, str]:
        """For hook commands, stdio MCP servers and skill tools."""
        data = self._data()
        env = {
            "MOSSCODE_PLUGIN_ROOT": str(self.root),
            "CLAUDE_PLUGIN_ROOT": str(self.root),
            "MOSSCODE_PLUGIN_DATA": str(data),
            "CLAUDE_PLUGIN_DATA": str(data),
        }
        if project is not None:
            env["CLAUDE_PROJECT_DIR"] = str(project)
        return env


# Records ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginRecord:
    name: str
    source: str  # A git address or an absolute local directory.
    ref: str | None = None  # Branch, tag or commit; None follows the default branch.
    path: str | None = None  # Subdirectory inside the repository (a marketplace entry).
    enabled: bool = True
    scope: Literal["user", "project"] = "user"  # Project records come from the project's .mosscode/plugins.yaml.

    @property
    def kind(self) -> Literal["git", "local"]:
        return source_kind(self.source)

    def base(self, home: Path | None = None) -> Path:
        """The clone or the local directory, before the subdirectory."""
        if self.kind == "local":
            return Path(self.source)
        if self.scope == "project":
            return project_clone_dir(self.name, self.source, self.ref, home)
        return repo_dir(self.name, home)

    def root(self, home: Path | None = None) -> Path:
        base = self.base(home)
        return base / self.path if self.path else base

    def to_yaml(self) -> dict:
        data: dict[str, Any] = {"name": self.name, "source": self.source}
        if self.ref:
            data["ref"] = self.ref
        if self.path:
            data["path"] = self.path
        if not self.enabled:
            data["enabled"] = False
        return data


def check_name(name: str) -> str | None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return "插件名只能含小写字母、数字和 -，以字母或数字开头，最长 64 个字符"
    return None


def _record(raw, index: int, scope: Literal["user", "project"] = "user") -> PluginRecord:
    if not isinstance(raw, dict):
        raise PluginError(f"第 {index} 项必须是对象")
    unknown = sorted(map(str, set(raw) - (RECORD_KEYS if scope == "user" else PROJECT_RECORD_KEYS)))
    if unknown:
        raise PluginError(f"第 {index} 项有未知字段 {', '.join(unknown)}")
    name = raw.get("name")
    problem = check_name(name)
    if problem:
        raise PluginError(f"第 {index} 项：{problem}")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise PluginError(f"插件 {name}：source 必须是 git 地址或本机目录")
    for key in ("ref", "path"):
        if raw.get(key) is not None and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise PluginError(f"插件 {name}：{key} 必须是非空字符串")
    # git reads a leading - as one of its options (--upload-pack runs a program), and a project's file is
    # written by someone else.
    for key in ("source", "ref"):
        if isinstance(raw.get(key), str) and raw[key].strip().startswith("-"):
            raise PluginError(f"插件 {name}：{key} 不能以 - 开头")
    path = raw.get("path")
    if path is not None and (Path(path).is_absolute() or ".." in Path(path).parts):
        raise PluginError(f"插件 {name}：path 必须是仓库内的相对路径")
    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise PluginError(f"插件 {name}：enabled 必须是 true 或 false")
    record = PluginRecord(name, source.strip(), raw.get("ref"), path, enabled, scope)
    if scope == "project" and record.kind != "git":
        raise PluginError(f"插件 {name}：项目级插件的 source 必须是 git 地址，别人才能安装")
    return record


def load_records(home: Path | None = None) -> tuple[list[PluginRecord], list[str]]:
    return _load(records_path(home), "user")


def load_project_records(root: Path) -> tuple[list[PluginRecord], list[str]]:
    return _load(project_records_path(root), "project")


def _load(path: Path, scope: Literal["user", "project"]) -> tuple[list[PluginRecord], list[str]]:
    """Records and problems; a broken file is one problem and no plugins, never a failed start."""
    try:
        if not path.is_file():
            return [], []
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return [], [f"插件记录 {path}：不是有效的 UTF-8 YAML，插件都未加载。"]
    if raw is None:
        return [], []
    if not isinstance(raw, dict) or set(raw) - {"plugins"} or not isinstance(raw.get("plugins") or [], list):
        return [], [f"插件记录 {path}：顶层只支持 plugins 列表，插件都未加载。"]
    records, problems, seen = [], [], set()
    for index, item in enumerate(raw.get("plugins") or [], 1):
        try:
            record = _record(item, index, scope)
        except PluginError as exc:
            problems.append(f"插件记录 {path}：{exc}，已跳过。")
            continue
        if record.name in seen:
            problems.append(f"插件记录 {path}：插件 {record.name} 重复，只用第一项。")
            continue
        seen.add(record.name)
        records.append(record)
    return records, problems


def save_records(records: list[PluginRecord], home: Path | None = None) -> None:
    _save(records_path(home), records)


def save_project_records(records: list[PluginRecord], root: Path) -> None:
    _save(project_records_path(root), records)


def _save(path: Path, records: list[PluginRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"plugins": [record.to_yaml() for record in records]}, allow_unicode=True, sort_keys=False)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# Reading a plugin directory ---------------------------------------------------------------


@dataclass(frozen=True)
class Plugin:
    name: str
    root: Path
    data: Path
    version: str = ""
    description: str = ""
    skill_dirs: tuple[Path, ...] = ()  # Each child directory is a skill.
    single_skill: bool = False  # SKILL.md at the root and no skills/: the whole plugin is one skill.
    commands: tuple[Path, ...] = ()  # Directories of *.md or single .md files.
    agents: tuple[Path, ...] = ()
    mcp_json: tuple[Path, ...] = ()
    mcp_inline: tuple[tuple[str, Any], ...] = ()  # mcpServers written in plugin.json.
    mcp_yaml: Path | None = None
    hooks_yaml: Path | None = None
    unsupported: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    record: PluginRecord | None = field(default=None, compare=False)
    # A preview reads a temporary clone but shows the paths it will have once installed, and writes nothing.
    ref_root: Path | None = field(default=None, compare=False)
    create_data: bool = field(default=True, compare=False)

    @property
    def ref(self) -> PluginRef:
        return PluginRef(self.name, self.ref_root or self.root, self.data, self.create_data)


@dataclass(frozen=True)
class Components:
    skills: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()  # name：command and arguments, or the address
    hooks: tuple[str, ...] = ()  # id（event）：what it runs
    problems: tuple[str, ...] = ()

    def count(self) -> str:
        parts = [
            f"{label} {len(items)}"
            for label, items in (
                ("Skill", self.skills),
                ("命令", self.commands),
                ("子 Agent", self.agents),
                ("MCP", self.mcp),
                ("Hook", self.hooks),
            )
            if items
        ]
        return " · ".join(parts) or "没有可用的组件"


def _address(url: str) -> str:
    """Without the query, which may carry a token."""
    from urllib.parse import urlsplit

    return urlsplit(url)._replace(query="", fragment="").geturl()


def components(plugin: Plugin, project: Path) -> Components:
    """Every component as it would load, with problems; reads files only."""
    from .agents import plugin_agents
    from .hooks.config import read_file
    from .mcp_config import plugin_servers
    from .skills import plugin_skills

    skills, problems = plugin_skills(plugin, project)
    agents, agent_problems = plugin_agents(plugin, project)
    servers, notices = plugin_servers(plugin, project)
    hooks, hook_problems = [], []
    if plugin.hooks_yaml is not None:
        file, hook_problems = read_file("plugin", plugin.hooks_yaml, plugin.ref, project)
        hooks = [f"{hook.id}（{hook.event}）：{hook.summary()}" for hook in file.hooks]
    mcp = [
        f"{server.name}：{' '.join((server.command, *server.args)) if server.type == 'stdio' else _address(server.url)}"
        for server in servers
    ]
    return Components(
        tuple(skill.name for skill in skills if skill.path.name == "SKILL.md"),
        tuple("/" + skill.name for skill in skills if skill.path.name != "SKILL.md"),
        tuple(agent.name for agent in agents),
        tuple(mcp),
        tuple(hooks),
        tuple([*plugin.problems, *problems, *agent_problems, *notices, *hook_problems]),
    )


def is_plugin(directory: Path) -> bool:
    return (directory / MANIFEST).is_file() or any((directory / name).exists() for name in COMPONENT_PATHS)


def _manifest(root: Path) -> dict:
    path = root / MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise PluginError(f"{path}：不是有效的 UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise PluginError(f"{path}：顶层必须是对象")
    return data


def _paths(root: Path, manifest: dict, key: str, problems: list[str]) -> list[Path] | None:
    """A manifest path field; None when absent. Paths start with ./ and stay inside the plugin."""
    value = manifest.get(key)
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        problems.append(f"plugin.json 的 {key} 必须是路径或路径列表，已忽略")
        return None
    paths = []
    resolved_root = root.resolve()
    for item in items:
        target = (root / item).resolve()
        if not item.startswith("./") or not target.is_relative_to(resolved_root):
            problems.append(f"plugin.json 的 {key} 中的 {item} 必须以 ./ 开头且在插件目录内，已忽略")
            continue
        paths.append(root / item[2:])
    return paths


def read_plugin(root: Path, name: str, home: Path | None = None, record: PluginRecord | None = None) -> Plugin:
    """The components of one plugin directory; problems are collected, only an unreadable manifest raises."""
    root = Path(root)
    manifest = _manifest(root)
    problems: list[str] = []
    version = manifest.get("version") if isinstance(manifest.get("version"), str) else ""
    description = manifest.get("description") if isinstance(manifest.get("description"), str) else ""

    skill_dirs = [root / "skills"] if (root / "skills").is_dir() else []
    skill_dirs += _paths(root, manifest, "skills", problems) or []
    single = not skill_dirs and (root / "SKILL.md").is_file()
    commands = _paths(root, manifest, "commands", problems)
    if commands is None:
        commands = [root / "commands"] if (root / "commands").is_dir() else []
    agents = _paths(root, manifest, "agents", problems)
    if agents is None:
        agents = [root / "agents"] if (root / "agents").is_dir() else []

    mcp_json = [root / ".mcp.json"] if (root / ".mcp.json").is_file() else []
    inline: tuple = ()
    servers = manifest.get("mcpServers")
    if isinstance(servers, dict):
        inline = tuple(servers.items())
    elif servers is not None:
        mcp_json += [path for path in _paths(root, manifest, "mcpServers", problems) or [] if path not in mcp_json]

    unsupported = [item for item in UNSUPPORTED_PATHS if (root / item).exists()]
    for key in manifest:
        if key not in MANIFEST_READ and key not in MANIFEST_METADATA:
            unsupported.append(f"plugin.json 的 {key}")
    return Plugin(
        name,
        root,
        data_dir(name, home),
        version,
        " ".join(description.split()),
        tuple(skill_dirs),
        single,
        tuple(commands),
        tuple(agents),
        tuple(mcp_json),
        inline,
        root / "mcp.yaml" if (root / "mcp.yaml").is_file() else None,
        root / "hooks.yaml" if (root / "hooks.yaml").is_file() else None,
        tuple(unsupported),
        tuple(problems),
        record,
    )


def manifest_name(root: Path) -> str | None:
    try:
        name = _manifest(root).get("name")
    except PluginError:
        return None
    return name if isinstance(name, str) else None


@dataclass(frozen=True)
class Installed:
    plugins: tuple[Plugin, ...] = ()
    notices: tuple[str, ...] = ()


def installed(home: Path | None = None, root: Path | None = None) -> Installed:
    """Enabled plugins in plugins.yaml order (a later one overrides a same-named component of an earlier one).
    With root, the project's plugins follow once the user has trusted them; nobody is asked here."""
    records, notices = load_records(home)
    plugins = []
    for record in records:
        if not record.enabled:
            continue
        directory = record.root(home)
        if not directory.is_dir():
            notices.append(f"插件 {record.name} 不在磁盘上（{directory}），运行 moss plugin update {record.name}。")
            continue
        try:
            plugin = read_plugin(directory, record.name, home, record)
        except PluginError as exc:
            notices.append(f"插件 {record.name} 未加载：{exc}。")
            continue
        notices.extend(f"插件 {record.name}：{problem}。" for problem in plugin.problems)
        plugins.append(plugin)
    user = Installed(tuple(plugins), tuple(notices))
    if root is None:
        return user
    project = project_plugins(root, home)
    return with_project(user, project) if project.trusted(home) else user


def read_head(base: Path) -> str:
    """The checked-out commit, read from .git so that starting a session runs no git."""
    git_dir = base / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:].strip()
        if (git_dir / ref).is_file():
            return (git_dir / ref).read_text(encoding="utf-8").strip()
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except (OSError, UnicodeError):
        pass
    return ""


@dataclass(frozen=True)
class ProjectPlugins:
    """The plugins a project declares in .mosscode/plugins.yaml, as far as they are installed on this machine."""

    root: Path
    records: tuple[PluginRecord, ...] = ()
    plugins: tuple[Plugin, ...] = ()  # Installed and readable; loaded only once trusted.
    missing: tuple[PluginRecord, ...] = ()  # Declared but not cloned here yet.
    problems: tuple[str, ...] = ()  # The declarations file and plugins that cannot be read.
    digest: str = ""  # The declarations and each installed plugin's commit: any change asks again.

    @property
    def notices(self) -> tuple[str, ...]:
        """For the start of a session, trusted or not."""
        if not self.missing:
            return self.problems
        names = "、".join(record.name for record in self.missing)
        return (
            *self.problems,
            f"项目声明了 {len(self.missing)} 个插件尚未安装（{names}），运行 moss plugin install 安装。",
        )

    def _store(self, home: Path | None):
        from .hooks.config import TrustStore

        return TrustStore(self.root, home, PLUGINS_TRUST)

    def trusted(self, home: Path | None = None) -> bool:
        return bool(self.plugins) and self._store(home).trusted("plugins", self.digest)

    def trust(self, home: Path | None = None) -> None:
        self._store(home).trust("plugins", self.digest)


def project_plugins(root: Path, home: Path | None = None) -> ProjectPlugins:
    """Reads files only: a session never clones, and never runs git."""
    root = Path(root).resolve()
    records, problems = load_project_records(root)
    plugins, missing, commits = [], [], {}
    for record in records:
        directory = record.root(home)
        if not directory.is_dir():
            missing.append(record)
            continue
        try:
            plugins.append(read_plugin(directory, record.name, home, record))
        except PluginError as exc:
            problems.append(f"项目插件 {record.name} 未加载：{exc}。")
            continue
        commits[record.name] = read_head(record.base(home))
    try:
        declared = project_records_path(root).read_bytes() if records else b""
    except OSError:
        declared = b""
    state = {"declared": hashlib.sha256(declared).hexdigest(), "commits": commits}
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return ProjectPlugins(root, tuple(records), tuple(plugins), tuple(missing), tuple(problems), digest)


def with_project(user: Installed, project: ProjectPlugins) -> Installed:
    """Project plugins after the user's; a same-named user plugin gives way entirely."""
    names = {plugin.name for plugin in project.plugins}
    problems = (f"项目插件 {plugin.name}：{problem}。" for plugin in project.plugins for problem in plugin.problems)
    return Installed(
        (*(plugin for plugin in user.plugins if plugin.name not in names), *project.plugins),
        (*user.notices, *problems),
    )


def resolve(plugins, home: Path | None = None, root: Path | None = None) -> tuple[Plugin, ...]:
    """A loader's plugins argument: None reads the installed ones, with the project's trusted ones."""
    return installed(home, root).plugins if plugins is None else tuple(plugins)


def checked(ref: PluginRef | None, value: Any, project: Path | None, error: Callable[[str], Exception]) -> Any:
    """Substitute a component's variables; a ${user_config.…} becomes the loader's own error type."""
    if ref is None:
        return value
    try:
        return ref.substitute_all(value, project)
    except PluginError as exc:
        raise error(str(exc)) from None
