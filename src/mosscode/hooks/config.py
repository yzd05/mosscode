"""Read the three hook files, validate each hook, trust records."""

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

from ..mcp_config import MCPConfigError, substitute_env
from ..paths import mosscode_home, project_dir
from .conditions import ConditionGroup, HookConfigError, check_field, parse_condition
from .events import EVENTS

FILE_LIMIT = 256 * 1024
TEXT_LIMIT = 8000
REJECT_LIMIT = 2000
HOOK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
HOOK_KEYS = {"id", "event", "if", "action", "reject", "on_error", "once", "async"}
ACTION_KEYS = {"command": {"type", "command", "timeout", "feedback"}, "prompt": {"type", "message"},
               "http": {"type", "url", "method", "headers", "json", "timeout"}, "agent": {"type", "prompt", "agent_type", "timeout"}}
METHODS = ("POST", "GET", "PUT", "PATCH", "DELETE")
ARGV_PLACEHOLDERS = ("FILE_PATH", "PROJECT_ROOT", "PYTHON", "TOOL_NAME", "EVENT")
ARGV_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
AGENT_TYPE = re.compile(r"[a-z][a-z0-9-]{0,63}")
AGENT_TIMEOUT = 300  # A sub-agent needs longer than a command.
TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
REJECT_TYPE = "reject 需为拒绝原因；动态拒绝请让命令以退出码 2 结束。"
SOURCES = ("user", "project", "local")
SOURCE_LABELS = {"user": "用户级", "project": "项目级", "local": "本地"}


@dataclass(frozen=True)
class Action:
    type: Literal["command", "prompt", "http", "agent"]
    command: str | tuple[str, ...] | None = None
    message: str = ""
    url: str = ""
    method: str = "POST"
    headers: tuple[tuple[str, str], ...] = ()
    json_body: Any = None  # The json field; None also when absent (has_json tells them apart).
    has_json: bool = False
    prompt: str = ""  # agent
    agent_type: str = "general"  # agent: the definition to run.
    timeout: float = 30.0
    feedback: bool = False

    def summary(self) -> str:
        if self.type == "command":
            return self.command if isinstance(self.command, str) else " ".join(self.command)
        if self.type == "http":
            return f"{self.method} {urlsplit(self.url)._replace(query='', fragment='').geturl()}"
        if self.type == "prompt":
            return "注入：" + self.message.replace("\n", " ")[:60]
        return f"子 Agent {self.agent_type}：" + self.prompt.replace("\n", " ")[:60]


@dataclass(frozen=True)
class Hook:
    id: str
    event: str
    source: Literal["user", "project", "local"]
    path: Path
    condition: ConditionGroup | None
    action: Action | None  # None only for a static rejection.
    reject: str | None = None
    on_error: Literal["ignore", "block"] = "ignore"
    once: bool = False
    background: bool = False  # async, or an event that always runs in the background
    raw: dict = field(default_factory=dict, compare=False)  # For /hooks <id>.

    def summary(self) -> str:
        if self.action is None:
            return "拒绝：" + self.reject.replace("\n", " ")[:60]
        return self.action.summary()

    def flags(self) -> tuple[str, ...]:
        return tuple(name for name, on in (("reject", self.reject is not None), ("once", self.once),
                                            ("async", self.background), ("on_error: block", self.on_error == "block"))
                     if on)


@dataclass(frozen=True)
class HookFile:
    source: str
    path: Path
    digest: str | None  # None when the file does not exist or could not be read.
    hooks: tuple[Hook, ...] = ()
    trusted: bool = True
    error: str | None = None

    @property
    def needs_trust(self) -> bool:
        return not self.trusted and bool(self.hooks)


@dataclass(frozen=True)
class HookConfig:
    files: tuple[HookFile, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def hooks(self) -> tuple[Hook, ...]:
        """Enabled hooks, in order: user, project, local; each file in written order."""
        return tuple(hook for file in self.files if file.trusted for hook in file.hooks)

    @property
    def untrusted(self) -> tuple[HookFile, ...]:
        return tuple(file for file in self.files if file.needs_trust)


def hook_paths(root: Path, home: Path | None = None) -> tuple[tuple[str, Path], ...]:
    root = Path(root).resolve()
    return (("user", mosscode_home(home) / "hooks.yaml"), ("project", root / ".mosscode" / "hooks.yaml"),
            ("local", root / ".mosscode" / "hooks.local.yaml"))


class TrustStore:
    """SHA-256 of each project hook file's content when the user trusted it."""

    def __init__(self, root: Path, home: Path | None = None):
        self.path = project_dir(root, home) / "hooks-trust.json"

    def _read(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            files = raw.get("files", {}) if isinstance(raw, dict) else {}
            return {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError):
            return {}

    def trusted(self, name: str, digest: str) -> bool:
        return self._read().get(name) == digest

    def trust(self, name: str, digest: str) -> None:
        files = {**self._read(), name: digest}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".hooks-trust-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "files": files}, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _templates(text: str, event: str) -> None:
    for match in TEMPLATE.finditer(text):
        check_field(match[1], event)


def _json_templates(value, event: str) -> None:
    if isinstance(value, str):
        _templates(value, event)
    elif isinstance(value, dict):
        for item in value.values():
            _json_templates(item, event)
    elif isinstance(value, list):
        for item in value:
            _json_templates(item, event)


def _timeout(raw: dict, default: float = 30) -> float:
    value = raw.get("timeout", default)
    if type(value) not in {int, float} or not 1 <= value <= 600:
        raise HookConfigError("timeout 需为 1–600 之间的秒数。")
    return float(value)


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HookConfigError(f"{name} 需为非空字符串。")
    if len(value) > limit:
        raise HookConfigError(f"{name} 最长 {limit} 个字符。")
    return value


def _action(raw, event: str) -> Action:
    if not isinstance(raw, dict):
        raise HookConfigError("action 需为对象，包含 type。")
    kind = raw.get("type")
    if kind not in ACTION_KEYS:
        raise HookConfigError("action.type 需为 command、prompt、http、agent 之一。")
    unknown = sorted(map(str, set(raw) - ACTION_KEYS[kind]))
    if unknown:
        raise HookConfigError(f"{kind} 动作不认识的字段：{', '.join(unknown)}")
    if kind not in EVENTS[event].actions:
        raise HookConfigError(f"事件 {event} 不能使用 {kind} 动作。")
    if kind == "prompt":
        message = _text(raw.get("message"), "message", TEXT_LIMIT)
        _templates(message, event)
        return Action("prompt", message=message)
    if kind == "agent":
        agent_type = raw.get("agent_type", "general")
        if not isinstance(agent_type, str) or not AGENT_TYPE.fullmatch(agent_type):
            raise HookConfigError("agent_type 需为子 Agent 定义的名称（小写字母开头，只含小写字母、数字和 -）。")
        return Action("agent", prompt=_text(raw.get("prompt"), "prompt", TEXT_LIMIT), agent_type=agent_type,
                      timeout=_timeout(raw, AGENT_TIMEOUT))
    if kind == "command":
        command = raw.get("command")
        if isinstance(command, list):
            # YAML reads bare `-3` or `8080` as numbers; argv wants their text.
            command = [str(part) if isinstance(part, (int, float)) and not isinstance(part, bool) else part
                       for part in command]
            if not command or any(not isinstance(part, str) or not part for part in command):
                raise HookConfigError("command 数组需由非空字符串组成。")
            for part in command:
                for match in ARGV_PLACEHOLDER.finditer(part):
                    if match[1] not in ARGV_PLACEHOLDERS:
                        raise HookConfigError(f"command 数组只支持 {', '.join('${' + p + '}' for p in ARGV_PLACEHOLDERS)}："
                                              f"{match[0]}")
            command = tuple(command)
        elif not isinstance(command, str) or not command.strip():
            raise HookConfigError("command 需为字符串或非空字符串数组。")
        feedback = raw.get("feedback", False)
        if type(feedback) is not bool:
            raise HookConfigError("feedback 需为 true 或 false。")
        if feedback and not EVENTS[event].feedback:
            raise HookConfigError("feedback 只能用于 session_start、turn_start、pre_tool_use、post_tool_use。")
        return Action("command", command=command, timeout=_timeout(raw), feedback=feedback)
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        raise HookConfigError("http 动作需要 url。")
    try:
        url = substitute_env(url)
        headers = raw.get("headers", {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise HookConfigError("headers 需为字符串键值对象。")
        headers = tuple((k, substitute_env(v)) for k, v in headers.items())
    except MCPConfigError as exc:
        raise HookConfigError(str(exc)) from None
    if any(not (k + v).isascii() for k, v in headers):
        raise HookConfigError("headers 的名称和值只能包含 ASCII 字符。")
    if TEMPLATE.search(url):
        raise HookConfigError("url 中不支持 {{字段}}；请放在 json 请求体中。")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise HookConfigError("url 需为 http 或 https 地址。")
    method = raw.get("method", "POST")
    if not isinstance(method, str) or method.upper() not in METHODS:
        raise HookConfigError("method 需为 " + "、".join(METHODS) + " 之一。")
    has_json = "json" in raw
    if has_json:
        _json_templates(raw["json"], event)
        try:
            json.dumps(raw["json"], allow_nan=False)
        except (TypeError, ValueError):
            raise HookConfigError("json 需为可序列化为 JSON 的值。") from None
    return Action("http", url=url, method=method.upper(), headers=headers, json_body=raw.get("json"),
                  has_json=has_json, timeout=_timeout(raw))


def parse_hook(raw, source: str, path: Path) -> Hook:
    if not isinstance(raw, dict):
        raise HookConfigError("Hook 需为对象。")
    unknown = sorted(map(str, set(raw) - HOOK_KEYS))
    if unknown:
        raise HookConfigError(f"不认识的字段：{', '.join(unknown)}")
    hook_id = raw.get("id")
    if not isinstance(hook_id, str) or not HOOK_ID.fullmatch(hook_id):
        raise HookConfigError("id 必填，由字母、数字、_ . - 组成，以字母或数字开头，最长 64 个字符。")
    event = raw.get("event")
    if event not in EVENTS:
        raise HookConfigError("event 需为 " + "、".join(EVENTS) + " 之一。")
    spec = EVENTS[event]
    condition = parse_condition(raw["if"], event) if "if" in raw else None
    reject = None
    if "reject" in raw:
        if event != "pre_tool_use":
            raise HookConfigError("reject 只能用于 pre_tool_use。")
        if not isinstance(raw["reject"], str) or not raw["reject"].strip():
            raise HookConfigError(REJECT_TYPE)
        reject = _text(raw["reject"], "reject", REJECT_LIMIT).strip()
    on_error = raw.get("on_error", "ignore")
    if "on_error" in raw and event != "pre_tool_use":
        raise HookConfigError("on_error 只能用于 pre_tool_use。")
    if on_error not in {"ignore", "block"}:
        raise HookConfigError("on_error 需为 ignore 或 block。")
    for name in ("once", "async"):
        if type(raw.get(name, False)) is not bool:
            raise HookConfigError(f"{name} 需为 true 或 false。")
    if "action" in raw:
        action = _action(raw["action"], event)
    elif reject is not None:
        action = None  # A static rejection needs no action.
    else:
        raise HookConfigError("缺少 action（只有写了 reject 的 Hook 可以省略）。")
    if reject is not None and action is not None and action.type == "prompt":
        raise HookConfigError("reject 不能与 prompt 动作同用：被拒绝的调用不再注入内容。")
    asynchronous = raw.get("async", False)
    if asynchronous:
        if spec.sync_only:
            raise HookConfigError("pre_tool_use 只能同步执行，不能 async。")
        if action is not None and action.type == "prompt":
            raise HookConfigError("prompt 动作不能 async：后台结果来不及注入。")
        if action is not None and action.feedback:
            raise HookConfigError("feedback 不能与 async 同用。")
    if spec.background and "async" in raw and not asynchronous and action is not None and action.type != "prompt":
        raise HookConfigError(f"{event} 上的 {action.type} 动作总在后台执行，不能写 async: false。")
    agent = action is not None and action.type == "agent"
    if agent and ("async" in raw and not asynchronous or reject is not None or raw.get("on_error") == "block"):
        # The sub-agent always runs in the background; it can neither reject nor block a call.
        raise HookConfigError("agent 动作总在后台执行，不能写 async: false、reject 或 on_error: block。")
    background = asynchronous or agent or (spec.background and action is not None and action.type != "prompt")
    return Hook(hook_id, event, source, path, condition, action, reject, on_error, raw.get("once", False),
                background, raw=raw)


def read_file(source: str, path: Path) -> tuple[HookFile, list[str]]:
    """A file-level error skips the file; a hook-level error skips that hook."""
    try:
        if not path.is_file():
            return HookFile(source, path, None), []
        data = path.read_bytes()
    except OSError:
        return HookFile(source, path, None, error="无法读取"), [f"Hook 文件 {path}：无法读取，已跳过。"]
    digest = hashlib.sha256(data).hexdigest()
    try:
        if len(data) > FILE_LIMIT:
            raise HookConfigError("超过 256 KB")
        raw = yaml.safe_load(data.decode("utf-8-sig"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise HookConfigError("顶层需为对象")
        if set(raw) - {"hooks"}:
            raise HookConfigError("顶层只支持 hooks 字段")
        entries = raw.get("hooks") or []
        if not isinstance(entries, list):
            raise HookConfigError("hooks 需为列表")
    except (UnicodeError, yaml.YAMLError, HookConfigError) as exc:
        # Never echo file contents: they may contain tokens.
        detail = str(exc) if isinstance(exc, HookConfigError) else "不是有效的 UTF-8 YAML"
        return HookFile(source, path, digest, error=detail), [f"Hook 文件 {path}：{detail}，已跳过。"]
    hooks, problems = [], []
    for index, entry in enumerate(entries, 1):
        try:
            hooks.append(parse_hook(entry, source, path))
        except HookConfigError as exc:
            label = entry.get("id") if isinstance(entry, dict) and isinstance(entry.get("id"), str) else f"第 {index} 项"
            problems.append(f"{path} · {label} · {exc}")
    return HookFile(source, path, digest, tuple(hooks)), problems


def load_hooks(root: Path, home: Path | None = None, *, store: TrustStore | None = None,
               trust_project: bool = False) -> HookConfig:
    files, problems, seen = [], [], set()
    for source, path in hook_paths(root, home):
        file, issues = read_file(source, path)
        problems.extend(issues)
        unique = []
        for hook in file.hooks:
            if hook.id in seen:
                problems.append(f"{path} · {hook.id} · id 与之前加载的 Hook 重复，已跳过。")
                continue
            seen.add(hook.id)
            unique.append(hook)
        trusted = (source == "user" or trust_project or file.digest is None
                   or (store is not None and store.trusted(path.name, file.digest)))
        files.append(replace(file, hooks=tuple(unique), trusted=trusted))
    return HookConfig(tuple(files), tuple(problems))
