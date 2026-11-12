"""Providers that follow another tool's settings: Claude Code's or Codex's, as CC Switch and Magpie write them.

`follow: claude-code` reads the address, key and model from ~/.claude/settings.json (its env section);
`follow: codex` reads ~/.codex/config.toml and auth.json. They are read when the configuration loads and again
when the provider is picked with /model, so a switch made in CC Switch or Magpie carries over without a second
copy of the configuration. The key is only read: it is never written, shown or logged.
"""

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

SOURCES = {"claude-code": "Claude Code", "codex": "Codex"}
# Where requests go and with which key come from the followed tool; a following entry may tune the rest. So a
# project's file cannot send the user's key anywhere the user did not point that tool at.
FROM_SOURCE = ("protocol", "base_url", "api_key", "api_key_env", "api_key_keyring", "auth_type", "headers", "preset")
ALIASES = ("opus", "sonnet", "haiku")
ONE_MILLION = re.compile(r"\[1m\]$", re.IGNORECASE)  # Claude Code's mark for a model run with a 1M window.
WIRE_APIS = {"responses": "openai-responses", "chat": "openai-chat-completions"}


def _error(message: str):
    from .config import ConfigError

    return ConfigError(message)


def claude_home(home: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get("CLAUDE_CONFIG_DIR"):
        return Path(environ["CLAUDE_CONFIG_DIR"])
    return (Path.home() if home is None else home) / ".claude"


def codex_home(home: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get("CODEX_HOME"):
        return Path(environ["CODEX_HOME"])
    return (Path.home() if home is None else home) / ".codex"


def settings_path(source: str, home: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    if source == "claude-code":
        return claude_home(home, environ) / "settings.json"
    return codex_home(home, environ) / "config.toml"


def detected(home: Path | None = None, environ: Mapping[str, str] | None = None) -> list[str]:
    """The sources whose settings resolve to a usable provider now, for `moss setup` to offer."""
    found = []
    for source in SOURCES:
        try:
            resolve({"name": source, "follow": source}, home, environ)
        except ValueError:
            continue
        found.append(source)
    return found


def resolve(raw: dict, home: Path | None = None, environ: Mapping[str, str] | None = None) -> dict:
    """raw, an entry with follow, as a whole entry: the followed tool's protocol, address, key and model."""
    environ = os.environ if environ is None else environ
    source = raw.get("follow")
    if source not in SOURCES:
        raise _error("follow 需为 " + "、".join(SOURCES) + " 之一。")
    if fixed := [key for key in FROM_SOURCE if key in raw]:
        raise _error(
            f"Provider {raw.get('name')}：跟随 {SOURCES[source]} 时不能填写 {'、'.join(fixed)}，"
            f"这些取自 {SOURCES[source]} 的设置。"
        )
    found = _claude(home, environ) if source == "claude-code" else _codex(home, environ)
    model = raw.get("model") or found.get("model")
    if not model:
        raise _error(
            f"Provider {raw.get('name')}：{SOURCES[source]} 的设置中没有可用的模型名；在这个 Provider 中填写 model。"
        )
    data = {**found, **raw, "model": model}
    if ONE_MILLION.search(model):
        data["model"] = ONE_MILLION.sub("", model)
        data.setdefault("context_window", 1_000_000)
    return data


def _read(path: Path, label: str, parse):
    try:
        return parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _error(f"跟随 {label}：找不到 {path}。先在 {label}、CC Switch 或 Magpie 中配置一次。") from None
    except (OSError, UnicodeError, ValueError):
        raise _error(
            f"跟随 {label}：无法读取 {path}，需为有效的 {'JSON' if path.suffix == '.json' else 'TOML'}。"
        ) from None


def _claude(home, environ) -> dict:
    path = settings_path("claude-code", home, environ)
    data = _read(path, "Claude Code", json.loads)
    data = data if isinstance(data, dict) else {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}

    def value(name: str) -> str | None:
        # Claude Code lays its settings' env over the process environment.
        found = env.get(name) if isinstance(env.get(name), str) else None
        return (found or environ.get(name) or "").strip() or None

    token, key = value("ANTHROPIC_AUTH_TOKEN"), value("ANTHROPIC_API_KEY")
    if not (token or key):
        helper = "；apiKeyHelper 生成的密钥不能共用" if data.get("apiKeyHelper") else ""
        raise _error(
            f"跟随 Claude Code：{path} 和环境变量中都没有 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY"
            f"（用账号登录的 Claude Code 不能共用{helper}）。"
        )
    found = {"protocol": "anthropic-messages", "api_key": token or key, "auth_type": "bearer" if token else "api_key"}
    if url := value("ANTHROPIC_BASE_URL"):
        found["base_url"] = url
    if headers := _header_lines(value("ANTHROPIC_CUSTOM_HEADERS")):
        found["headers"] = headers
    chosen = value("ANTHROPIC_MODEL") or (data["model"] if isinstance(data.get("model"), str) else None)
    wide = bool(chosen and ONE_MILLION.search(chosen))
    alias = ONE_MILLION.sub("", chosen or "sonnet").strip().lower()  # Claude Code starts with sonnet.
    if alias in ALIASES:  # opus / sonnet / haiku stand for whatever the settings map them to.
        mapped = value(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL")
        chosen = mapped + "[1m]" if mapped and wide and not ONE_MILLION.search(mapped) else mapped
    if chosen:
        found["model"] = chosen
    return found


def _header_lines(text: str | None) -> dict[str, str]:
    """ANTHROPIC_CUSTOM_HEADERS: one "Name: value" per line."""
    headers = {}
    for line in (text or "").splitlines():
        name, sep, content = line.partition(":")
        if sep and name.strip():
            headers[name.strip()] = content.strip()
    return headers


def _codex(home, environ) -> dict:
    root = codex_home(home, environ)
    data = _read(root / "config.toml", "Codex", tomllib.loads)
    profile = data.get("profile")
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    if isinstance(profile, str) and isinstance(profiles.get(profile), dict):
        data = {**data, **profiles[profile]}
    provider = data.get("model_provider") or "openai"
    tables = data.get("model_providers") if isinstance(data.get("model_providers"), dict) else {}
    table = tables.get(provider)
    if table is None:
        if provider != "openai":
            raise _error(
                f"跟随 Codex：不支持 Codex 内置的 {provider}；在 config.toml 的 model_providers 中写明它的地址。"
            )
        table = {}
    if not isinstance(table, dict):
        raise _error(f"跟随 Codex：config.toml 中的 model_providers.{provider} 需为表。")
    wire = table.get("wire_api", "responses")
    if wire not in WIRE_APIS:
        raise _error(f"跟随 Codex：不支持 wire_api = {wire!r}，只支持 responses 和 chat。")
    found = {"protocol": WIRE_APIS[wire]}
    url = table.get("base_url") or (environ.get("OPENAI_BASE_URL") if provider == "openai" else None)
    if url:
        found["base_url"] = url
    elif provider != "openai":
        raise _error(f"跟随 Codex：config.toml 中的 model_providers.{provider} 没有 base_url。")
    env_key = table.get("env_key")
    if isinstance(env_key, str) and env_key:
        if not (key := (environ.get(env_key) or "").strip()):
            raise _error(f"跟随 Codex：Codex 的密钥取自环境变量 {env_key}，它没有设置。")
    else:
        auth = _read(root / "auth.json", "Codex", json.loads)
        key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise _error("跟随 Codex：auth.json 中没有 OPENAI_API_KEY（用 ChatGPT 账号登录的 Codex 不能共用）。")
        key = key.strip()
    found["api_key"] = key
    headers = {k: v for k, v in (table.get("http_headers") or {}).items() if isinstance(v, str)}
    for name, variable in (table.get("env_http_headers") or {}).items():
        if isinstance(variable, str) and environ.get(variable):
            headers[name] = environ[variable]
    if headers:
        found["headers"] = headers
    if isinstance(data.get("model"), str) and data["model"].strip():
        found["model"] = data["model"].strip()
    return found
