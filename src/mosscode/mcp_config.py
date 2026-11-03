"""Two-layer MCP configuration plus opt-in .mcp.json; credentials stay out of repr and diagnostics."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class MCPConfigError(ValueError):
    pass


DEFAULT_TIMEOUT = 30.0
DEFAULT_STARTUP_TIMEOUT = 10.0
SERVER_NAME = r"[A-Za-z0-9_-]{1,64}"
OPTIONS = {"timeout", "startup_timeout", "trust_read_only_hint"}


@dataclass(frozen=True)
class ServerConfig:
    name: str
    type: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    url: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    timeout: float = DEFAULT_TIMEOUT  # One tool call.
    startup_timeout: float | None = None  # Connect + discovery; None uses the manager default.
    trust_read_only_hint: bool = False  # Treat readOnlyHint tools as read-only tools.


@dataclass(frozen=True)
class MCPSettings:
    servers: tuple[ServerConfig, ...] = ()
    notices: tuple[str, ...] = ()


def config_paths(root: Path, user_path: Path | None = None):
    return user_path or Path.home() / ".mosscode/mcp.yaml", root / ".mosscode/mcp.yaml"


def mcp_json_path(root: Path) -> Path:
    return root / ".mcp.json"


def _read(path: Path) -> tuple[dict, bool]:
    try:
        if not path.exists():
            if path.is_symlink():
                raise MCPConfigError("配置文件是失效链接。")
            return {}, False
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            return {}, False
        if not isinstance(raw, dict) or set(raw) - {"mcp_servers", "import_mcp_json"}:
            raise MCPConfigError("顶层仅支持 mcp_servers 和 import_mcp_json。")
        if type(raw.get("import_mcp_json", False)) is not bool:
            raise MCPConfigError("import_mcp_json 必须是 true 或 false。")
        servers = raw.get("mcp_servers", {}) or {}
        if not isinstance(servers, dict):
            raise MCPConfigError("mcp_servers 必须是以 Server 名称为键的对象。")
        if any(not isinstance(k, str) or not re.fullmatch(SERVER_NAME, k) for k in servers):
            raise MCPConfigError("Server 名称需为 1–64 位英文字母、数字、下划线或连字符。")
        return servers, raw.get("import_mcp_json", False)
    except (OSError, UnicodeError, yaml.YAMLError, MCPConfigError) as exc:
        detail = str(exc) if isinstance(exc, MCPConfigError) else "无法读取有效 UTF-8 YAML。"
        raise MCPConfigError(f"MCP 配置 {path}：{detail}") from None


PLACEHOLDER = r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"


def substitute_env(value: str) -> str:
    """${VAR} must be set; ${VAR:-default} falls back when VAR is unset or empty."""
    if "${" in re.sub(PLACEHOLDER, "", value):
        raise MCPConfigError("环境变量占位符需为 ${VAR} 或 ${VAR:-默认值} 格式。")

    def replace(match):
        value = os.environ.get(match[1])
        if match[2] is not None and not value:
            return match[2]
        if value is None:
            raise MCPConfigError("配置引用的环境变量未设置。")
        return value

    return re.sub(PLACEHOLDER, replace, value)


def _expand(values, label):
    if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
        raise MCPConfigError(f"{label} 必须是字符串键值对象。")
    return {k: substitute_env(v) for k, v in values.items()}


def _options(raw) -> dict:
    options = {}
    for key, default, limit in (("timeout", DEFAULT_TIMEOUT, 600), ("startup_timeout", None, 120)):
        value = raw.get(key, default)
        if value is not None and (type(value) not in {int, float} or not 0 < value <= limit):
            raise MCPConfigError(f"{key} 必须是 (0, {limit}] 秒之间的数字。")
        options[key] = None if value is None else float(value)
    trust = raw.get("trust_read_only_hint", False)
    if type(trust) is not bool:
        raise MCPConfigError("trust_read_only_hint 必须是 true 或 false。")
    options["trust_read_only_hint"] = trust
    return options


def _server(name, raw):
    if not isinstance(raw, dict):
        raise MCPConfigError("Server 配置必须是对象。")
    kind = raw.get("type")
    options = _options(raw)
    if kind == "stdio":
        if set(raw) - {"type", "command", "args", "env"} - OPTIONS:
            raise MCPConfigError("stdio 仅支持 type、command、args、env 及超时和只读提示选项。")
        command, args = raw.get("command"), raw.get("args", [])
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise MCPConfigError("command 必须是非空可执行文件名或路径。")
        if not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args):
            raise MCPConfigError("args 必须是字符串列表。")
        env = _expand(raw.get("env", {}), "env")
        if any(not k or "=" in k or "\0" in k or "\0" in v for k, v in env.items()):
            raise MCPConfigError("env 包含非法名称或空字符。")
        return ServerConfig(name, kind, command, tuple(args), env, **options)
    if kind == "http":
        if set(raw) - {"type", "url", "headers"} - OPTIONS:
            raise MCPConfigError("http 仅支持 type、url、headers 及超时和只读提示选项。")
        url = raw.get("url")
        try:
            if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (
                not parsed
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError
            _ = parsed.port  # Raises ValueError for a port out of range.
        except ValueError:
            raise MCPConfigError("url 必须是无嵌入凭据和 fragment 的 HTTP(S) 地址。") from None
        headers = _expand(raw.get("headers", {}), "headers")
        if any(
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k) or any(c in v for c in "\r\n\0")
            for k, v in headers.items()
        ):
            raise MCPConfigError("headers 含非法字段或换行。")
        return ServerConfig(name, kind, url=url, headers=headers, **options)
    raise MCPConfigError("type 必须是 stdio 或 http（Streamable HTTP）。")


def _read_mcp_json(path: Path) -> tuple[dict, list[str]]:
    """Claude Code's project file, converted to the mcp.yaml shape. Problems are notices, never fatal."""
    try:
        if not path.is_file():
            return {}, []
        raw = json.loads(path.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers", {}) if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        return {}, [f"MCP 配置 {path.name}：无法读取有效 UTF-8 JSON 或缺少 mcpServers 对象，已忽略。"]
    converted, notices = {}, []
    for name, entry in servers.items():
        if not isinstance(name, str) or not re.fullmatch(SERVER_NAME, name):
            notices.append(f"MCP 配置 {path.name}：Server 名称需为 1–64 位英文字母、数字、下划线或连字符，已跳过一项。")
            continue
        if not isinstance(entry, dict):
            notices.append(f"MCP {name} 不可用：Server 配置必须是对象。")
            continue
        kind = entry.get("type", "stdio" if "command" in entry else None)
        if kind not in {"stdio", "http"}:
            notices.append(f"MCP {name} 不可用：.mcp.json 中的传输方式 {kind!s} 不受支持（支持 stdio、http）。")
            continue
        keys = {"command", "args", "env"} if kind == "stdio" else {"url", "headers"}
        item = {"type": kind, **{k: entry[k] for k in keys | OPTIONS if k in entry}}
        try:
            # Claude Code expands variables in these fields too, not only env/headers.
            if isinstance(item.get("command"), str):
                item["command"] = substitute_env(item["command"])
            if isinstance(item.get("url"), str):
                item["url"] = substitute_env(item["url"])
            if isinstance(item.get("args"), list):
                item["args"] = [substitute_env(a) if isinstance(a, str) else a for a in item["args"]]
        except MCPConfigError as exc:
            notices.append(f"MCP {name} 不可用：{exc}")
            continue
        converted[name] = item
    return converted, notices


def load_mcp_settings(root: Path, *, user_path: Path | None = None) -> MCPSettings:
    user_file, project_file = config_paths(root, user_path)
    user, user_import = _read(user_file)
    project, project_import = _read(project_file)
    merged, notices = dict(user), []
    if user_import or project_import:
        imported, notices = _read_mcp_json(mcp_json_path(root))
        merged.update(imported)
    merged.update(project)  # Replace the complete server, never merge transport fields.
    servers = []
    for name, raw in merged.items():
        try:
            servers.append(_server(name, raw))
        except MCPConfigError as exc:
            notices.append(f"MCP {name} 不可用：{exc}")
    return MCPSettings(tuple(servers), tuple(notices))
