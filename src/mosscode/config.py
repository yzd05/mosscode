"""Endpoint configuration is independent of the service's vendor name."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .catalog import ModelInfo, load_catalog, lookup, tokens_label
from .context import ContextBudget
from .prompts import DEFAULT_SYSTEM_PROMPT, stable_prompt


class ConfigError(ValueError):
    pass


ALIASES = {
    "anthropic": "anthropic-messages",
    "openai": "openai-responses",
    "openai-compat": "openai-chat-completions",
}
PROTOCOLS = set(ALIASES.values())
TEAMMATE_MODES = ("auto", "in-process", "windows-terminal", "tmux")
SEARCH_PROVIDERS = ("tavily",)
CREDENTIAL_FIELDS = ("api_key", "api_key_env", "api_key_keyring")
# Default output limit taken from the catalog at most: some models allow as much output as their whole window,
# and the reply reserve comes out of the window.
OUTPUT_CAP = 32_000


def _validate_env_name(value: str | None) -> None:
    if value is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise ConfigError(
            "api_key_env 请填写环境变量名称（例如 DEEPSEEK_API_KEY），不能填写密钥；直接填写密钥请使用 api_key。"
        )


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    protocol: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    api_key_keyring: str | None = None  # The entry name in the system credential manager.
    auth_type: str = "api_key"
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    thinking: dict[str, Any] | None = None
    max_output_tokens: int = 16384
    timeout: float = 60.0
    max_retries: int = 4
    stream_usage: bool = False
    max_tokens_field: str = "max_tokens"
    include_encrypted_reasoning: bool = True
    prompt_cache: bool | None = None
    context_window: int | None = None
    compact_threshold: int | None = None
    images: bool | None = None  # Whether the model accepts images; None follows the protocol.
    reasoning: bool | None = None  # Whether /thinking applies; None follows the model catalog.
    catalog_model: str | None = None  # The model catalog entry, when the model name is an alias it does not list.
    # Filled in when the configuration is read: the catalog entry and which fields came from it.
    catalog: ModelInfo | None = field(default=None, compare=False, repr=False)
    from_catalog: tuple[str, ...] = field(default=(), compare=False, repr=False)

    @property
    def accepts_images(self) -> bool:
        # Many Chat Completions services (DeepSeek and the like) have text-only models.
        return self.images if self.images is not None else self.protocol != "openai-chat-completions"

    def window_note(self) -> str | None:
        """Why the context window is a guess, or None when it is known."""
        if self.context_window is not None:
            return None
        if self.catalog is not None and self.catalog.context_window:
            return (
                f"{self.name}：模型目录记录 {self.catalog.name} 的上下文窗口为 {tokens_label(self.catalog.context_window)}，"
                "不足以运行 MossCode，按 128K 估算；如果服务实际支持更大的窗口，请在配置中填写 context_window。"
            )
        return (
            f"{self.name}：模型目录中没有 {self.model}，按 128K 估算上下文；可以在配置中填写 context_window，"
            "或用 catalog_model 指定目录中的条目（moss models <关键词> 查找）。"
        )

    def budget(self) -> ContextBudget:
        return ContextBudget.for_provider(self.context_window, self.max_output_tokens, self.compact_threshold)

    def credential(self) -> str:
        _validate_env_name(self.api_key_env)
        value = _stored(self.api_key_env, self.api_key_keyring, self.api_key, self.name)
        if not value:
            if self.api_key_env:
                hint = f"请设置环境变量 {self.api_key_env}（api_key_env 只填写变量名；直接填写密钥请使用 api_key）"
            elif self.api_key_keyring:
                hint = (
                    f"系统凭据管理器中没有 {self.api_key_keyring}，请运行 moss key set {self.api_key_keyring} 保存密钥"
                )
            else:
                hint = "请配置 api_key_env（环境变量名）、api_key_keyring（凭据管理器中的名称）或 api_key（密钥）"
            raise ConfigError(f"{self.name}：{hint}。")
        return value


def _stored(env: str | None, keyring: str | None, plain: str | None, owner: str) -> str | None:
    """The key from whichever of the three places is configured; registered so it is hidden from the model."""
    from . import credentials

    if env:
        value = os.environ.get(env) or None
    elif keyring:
        try:
            value = credentials.keyring_get(keyring)
        except credentials.KeyringError as exc:
            raise ConfigError(f"{owner}：{exc}。") from None
    else:
        value = plain or None
    credentials.remember(value)
    return value


@dataclass(frozen=True)
class WebSearchConfig:
    provider: str
    api_key_env: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    api_key_keyring: str | None = None

    def credential(self) -> str | None:
        """The key, or None when the variable or credential manager entry it names is missing."""
        return _stored(self.api_key_env, self.api_key_keyring, self.api_key, "web_search")


@dataclass(frozen=True)
class Settings:
    providers: tuple[ProviderConfig, ...]
    default_provider: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_iterations: int = 200
    source: Path | None = None
    shell: str = "auto"
    auto_compact: bool = True
    persist_sessions: bool = True
    memory: bool = True
    auto_memory: bool = True
    memory_provider: str | None = None  # Background extraction; None uses the current provider.
    agent_auto_background: float = 120.0  # Seconds before a foreground sub-agent moves on; 0 never.
    agent_max_background: int = 4  # Running background tasks at most.
    worktree_links: tuple[str, ...] = ()  # Paths relative to the repository linked into each worktree.
    worktree_stale_hours: float = 24.0  # Unused sub-agent worktrees older than this go; 0 never.
    team_max_members: int = 4  # Members of one team at most (1–8).
    teammate_mode: str = "auto"  # auto / in-process / windows-terminal / tmux.
    coordinator_mode: bool = False  # Also needs MOSSCODE_COORDINATOR_MODE.
    web_search: WebSearchConfig | None = None  # None: web_search is not offered.
    claude_directories: bool = False  # Also read skills and sub-agents from .claude directories.

    def select(self, name: str | None = None) -> ProviderConfig:
        selected = name or self.default_provider or self.providers[0].name
        for provider in self.providers:
            if provider.name == selected:
                return provider
        raise ConfigError(f"未知 Provider：{selected}。")


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{label} 必须是正整数。")
    return value


def _links(raw: Any) -> tuple[str, ...]:
    """Relative paths inside the repository."""
    problem = "worktree_links 需为相对仓库根的路径列表（不能是绝对路径，不能含 ..）。"
    if not isinstance(raw, list):
        raise ConfigError(problem)
    links = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(problem)
        text = item.strip().replace("\\", "/").strip("/")
        parts = text.split("/")
        if (
            not text
            or Path(item.strip()).is_absolute()
            or item.strip().startswith(("/", "\\"))
            or ":" in text
            or ".." in parts
            or "." in parts
            or parts[0] == ".git"
            or parts[:2] == [".mosscode", "worktrees"]
        ):
            raise ConfigError(problem)
        if text not in links:
            links.append(text)
    return tuple(links)


def _provider(raw: Any) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError("providers 的每一项必须是配置对象。")
    allowed = set(ProviderConfig.__dataclass_fields__) - {"catalog", "from_catalog"}
    if set(raw) - allowed:
        raise ConfigError("Provider 包含未知字段，请对照 config.example.yaml。")
    for key in ("name", "protocol", "model"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ConfigError(f"Provider 的 {key} 必须是非空字符串。")
    data = dict(raw)
    data["protocol"] = ALIASES.get(data["protocol"], data["protocol"])
    if data["protocol"] not in PROTOCOLS:
        raise ConfigError("protocol 必须是 anthropic-messages、openai-responses 或 openai-chat-completions。")
    url = data.get("base_url")
    if url is not None:
        if not isinstance(url, str):
            raise ConfigError("base_url 必须是 HTTP(S) URL。")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ConfigError("base_url 必须是没有嵌入凭据的 HTTP(S) URL。")
    for key in CREDENTIAL_FIELDS:
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise ConfigError(f"{key} 必须是非空字符串。")
    if sum(bool(data.get(key)) for key in CREDENTIAL_FIELDS) > 1:
        raise ConfigError("api_key、api_key_env 和 api_key_keyring 只选择一种。")
    _validate_env_name(data.get("api_key_env"))
    headers = data.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
    ):
        raise ConfigError("headers 必须是字符串键值对象。")
    if "max_output_tokens" in data:
        data["max_output_tokens"] = _positive_int(data["max_output_tokens"], "max_output_tokens")
    retries = data.get("max_retries", ProviderConfig.max_retries)
    if type(retries) is not int or retries < 0:
        raise ConfigError("max_retries 必须是非负整数。")
    timeout = data.get("timeout", 60.0)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("timeout 必须是有限的正数秒数。")
    if data.get("auth_type", "api_key") not in {"api_key", "bearer"}:
        raise ConfigError("auth_type 必须是 api_key 或 bearer。")
    if type(data.get("stream_usage", False)) is not bool:
        raise ConfigError("stream_usage 必须是布尔值。")
    if type(data.get("include_encrypted_reasoning", True)) is not bool:
        raise ConfigError("include_encrypted_reasoning 必须是布尔值。")
    if data.get("prompt_cache") is not None and type(data["prompt_cache"]) is not bool:
        raise ConfigError("prompt_cache 必须是布尔值或 null。")
    if data.get("images") is not None and type(data["images"]) is not bool:
        raise ConfigError("images 必须是布尔值或 null。")
    if data.get("reasoning") is not None and type(data["reasoning"]) is not bool:
        raise ConfigError("reasoning 必须是布尔值或 null。")
    if data.get("max_tokens_field", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
        raise ConfigError("max_tokens_field 必须是 max_tokens 或 max_completion_tokens。")
    thinking = data.get("thinking")
    if thinking is True:
        thinking = {"type": "adaptive"}
    elif thinking is False:
        thinking = {"type": "disabled"}
    if thinking is not None:
        if data["protocol"] != "anthropic-messages":
            raise ConfigError("本章 thinking 配置用于 Anthropic Messages。")
        if not isinstance(thinking, dict) or thinking.get("type") not in {"adaptive", "enabled", "disabled"}:
            raise ConfigError("thinking.type 必须是 adaptive、enabled 或 disabled。")
        if set(thinking) - {"type", "budget_tokens", "effort"}:
            raise ConfigError("thinking 包含未知字段。")
        if thinking["type"] == "enabled":
            if _positive_int(thinking.get("budget_tokens"), "thinking.budget_tokens") < 1024:
                raise ConfigError("手动思考预算需至少 1024 且小于 max_output_tokens。")
        elif "budget_tokens" in thinking:
            raise ConfigError("budget_tokens 仅用于手动 enabled 模式。")
        if "effort" in thinking and (thinking["type"] != "adaptive" or not isinstance(thinking["effort"], str)):
            raise ConfigError("effort 需用于 adaptive 模式并为字符串。")
    for key in ("context_window", "compact_threshold"):
        if data.get(key) is not None:
            data[key] = _positive_int(data[key], key)
    _catalog_defaults(data)
    if (
        thinking is not None
        and thinking["type"] == "enabled"
        and not thinking["budget_tokens"] < data["max_output_tokens"]
    ):
        raise ConfigError("手动思考预算需至少 1024 且小于 max_output_tokens。")
    data["thinking"] = thinking
    config = ProviderConfig(**data)
    problem = config.budget().problem()
    if problem:
        raise ConfigError(f"Provider {config.name}：{problem}")
    return config


def catalog_window(info: ModelInfo, output: int | None = None) -> int | None:
    """The catalog's window, when the agent fits in it with this output limit (default: the catalog's)."""
    if output is None:
        output = min(info.max_output_tokens, OUTPUT_CAP) if info.max_output_tokens else ProviderConfig.max_output_tokens
    # Old models list windows too small for the agent; a local service may run them with a larger one,
    # so the estimate is kept rather than refusing to start.
    if info.context_window and ContextBudget.for_provider(info.context_window, output).problem() is None:
        return info.context_window
    return None


def _catalog_defaults(data: dict) -> None:
    """Fill the window, output limit and image support the configuration leaves out from the model catalog."""
    alias = data.get("catalog_model")
    if alias is not None and (not isinstance(alias, str) or not alias.strip()):
        raise ConfigError("catalog_model 必须是非空字符串。")
    catalog = load_catalog()
    info = lookup(data["model"], base_url=data.get("base_url"), protocol=data["protocol"], alias=alias, catalog=catalog)
    if alias and info is None:
        raise ConfigError(
            f"Provider {data['name']}：模型目录中没有 catalog_model {alias}（moss models <关键词> 查找）。"
        )
    filled = []
    if info is not None:
        if "max_output_tokens" not in data and info.max_output_tokens:
            data["max_output_tokens"] = min(info.max_output_tokens, OUTPUT_CAP)
            filled.append("max_output_tokens")
        output = data.get("max_output_tokens", ProviderConfig.max_output_tokens)
        if data.get("context_window") is None and (window := catalog_window(info, output)):
            data["context_window"] = window
            filled.append("context_window")
        if data.get("images") is None and info.vision is not None:
            data["images"] = info.vision
            filled.append("images")
    data.setdefault("max_output_tokens", ProviderConfig.max_output_tokens)
    data["catalog"], data["from_catalog"] = info, tuple(filled)


def _web_search(raw: Any) -> WebSearchConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"provider", *CREDENTIAL_FIELDS}:
        raise ConfigError("web_search 只支持 provider、api_key_env、api_key_keyring、api_key 字段。")
    if raw.get("provider") not in SEARCH_PROVIDERS:
        raise ConfigError("web_search.provider 需为 " + "、".join(SEARCH_PROVIDERS) + " 之一。")
    for key in CREDENTIAL_FIELDS:
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ConfigError(f"web_search.{key} 必须是非空字符串。")
    if sum(bool(raw.get(key)) for key in CREDENTIAL_FIELDS) != 1:
        raise ConfigError(
            "web_search 需要 api_key_env（环境变量名）、api_key_keyring（凭据管理器中的名称）或 api_key（密钥）中的一个。"
        )
    _validate_env_name(raw.get("api_key_env"))
    return WebSearchConfig(raw["provider"], raw.get("api_key_env"), raw.get("api_key"), raw.get("api_key_keyring"))


def config_candidates(cwd: Path | None = None, home: Path | None = None) -> tuple[Path, ...]:
    """A config.yaml in the working directory is a project's own file, not ours."""
    cwd = Path.cwd() if cwd is None else cwd
    home = Path.home() if home is None else home
    project, user = cwd / ".mosscode" / "config.yaml", home / ".mosscode" / "config.yaml"
    same = os.path.normcase(os.path.abspath(project)) == os.path.normcase(os.path.abspath(user))
    return (user,) if same else (project, user)  # Started in the home folder, both are one file.


def find_config(cwd: Path | None = None, home: Path | None = None) -> Path:
    """Project file, then the user file; the first one wins."""
    candidates = config_candidates(cwd, home)
    for path in candidates:
        if path.is_file():
            return path
    raise ConfigError(
        "找不到配置文件，已查找："
        + "、".join(str(p) for p in candidates)
        + "。运行 moss setup 创建配置，或用 --config 指定。"
    )


def render_config(provider: dict[str, Any]) -> str:
    """The file `moss setup` writes, with one provider made the default."""
    body = yaml.safe_dump(
        {"default_provider": provider["name"], "providers": [provider]}, sort_keys=False, allow_unicode=True
    )
    return (
        "# MossCode 配置，由 moss setup 生成。\n"
        "# 字段说明见 config.example.yaml；追加 Provider：在 providers 下再写一项。\n" + body
    )


def load_settings(path: str | Path) -> Settings:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise ConfigError("无法读取配置文件，请检查 --config 路径。") from None
    except (yaml.YAMLError, UnicodeError):
        raise ConfigError("配置文件需为有效的 UTF-8 YAML。") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list) or not raw["providers"]:
        raise ConfigError("配置需包含非空 providers 列表。")
    if set(raw) - {
        "providers",
        "default_provider",
        "system_prompt",
        "max_iterations",
        "shell",
        "auto_compact",
        "persist_sessions",
        "memory",
        "auto_memory",
        "memory_provider",
        "agent_auto_background",
        "agent_max_background",
        "worktree_links",
        "worktree_stale_hours",
        "team_max_members",
        "teammate_mode",
        "coordinator_mode",
        "web_search",
        "claude_directories",
    }:
        raise ConfigError("配置顶层包含未知字段。")
    providers = tuple(_provider(item) for item in raw["providers"])
    if len({p.name for p in providers}) != len(providers):
        raise ConfigError("Provider name 不能重复。")
    default = raw.get("default_provider")
    system = raw.get("system_prompt", Settings.__dataclass_fields__["system_prompt"].default)
    if default is not None and (not isinstance(default, str) or not default.strip()):
        raise ConfigError("default_provider 需为非空配置名称；不设置时多配置启动会显示选择列表。")
    if not isinstance(system, str):
        raise ConfigError("system_prompt 必须是字符串。")
    iterations = _positive_int(raw.get("max_iterations", Settings.max_iterations), "max_iterations")
    shell = raw.get("shell", Settings.shell)
    if not isinstance(shell, str) or not shell.strip():
        raise ConfigError("shell 需为 auto、bash、powershell 或 shell 可执行文件路径。")
    switches = {}
    for key in ("auto_compact", "persist_sessions", "memory", "auto_memory"):
        switches[key] = raw.get(key, True)
        if type(switches[key]) is not bool:
            raise ConfigError(f"{key} 必须是布尔值。")
    memory_provider = raw.get("memory_provider")
    if memory_provider is not None and memory_provider not in {p.name for p in providers}:
        raise ConfigError("memory_provider 必须是已配置的 Provider 名称。")
    auto_background = raw.get("agent_auto_background", Settings.agent_auto_background)
    if type(auto_background) not in {int, float} or not 0 <= auto_background <= 3600:
        raise ConfigError("agent_auto_background 需为 0–3600 之间的秒数（0 表示不自动转入后台）。")
    max_background = _positive_int(
        raw.get("agent_max_background", Settings.agent_max_background), "agent_max_background"
    )
    links = _links(raw.get("worktree_links", []))
    stale = raw.get("worktree_stale_hours", Settings.worktree_stale_hours)
    if type(stale) not in {int, float} or not 0 <= stale <= 8760:
        raise ConfigError("worktree_stale_hours 需为 0–8760 之间的小时数（0 表示不自动清理）。")
    members = raw.get("team_max_members", Settings.team_max_members)
    if type(members) is not int or not 1 <= members <= 8:
        raise ConfigError("team_max_members 需为 1–8 之间的整数。")
    teammate_mode = raw.get("teammate_mode", Settings.teammate_mode)
    if teammate_mode not in TEAMMATE_MODES:
        raise ConfigError("teammate_mode 需为 " + "、".join(TEAMMATE_MODES) + " 之一。")
    coordinator = raw.get("coordinator_mode", Settings.coordinator_mode)
    if type(coordinator) is not bool:
        raise ConfigError("coordinator_mode 必须是布尔值。")
    claude_directories = raw.get("claude_directories", Settings.claude_directories)
    if type(claude_directories) is not bool:
        raise ConfigError("claude_directories 必须是布尔值。")
    settings = Settings(
        providers,
        default,
        stable_prompt(system),
        iterations,
        Path(path),
        shell.strip(),
        memory_provider=memory_provider,
        agent_auto_background=float(auto_background),
        agent_max_background=max_background,
        worktree_links=links,
        worktree_stale_hours=float(stale),
        team_max_members=members,
        teammate_mode=teammate_mode,
        coordinator_mode=coordinator,
        web_search=_web_search(raw.get("web_search")),
        claude_directories=claude_directories,
        **switches,
    )
    settings.select()
    return settings
