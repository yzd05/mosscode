"""Compliant official CLI proxy backends (Claude Code, Codex, Gemini CLI).

Invokes allow-listed vendor CLIs via their documented headless JSON-streaming subprocess interfaces
so users can reuse existing official CLI subscriptions without reading or copying private OAuth tokens.

Security and isolation guarantees:
- Executables are resolved exclusively from explicit absolute directories on `PATH` (never CWD), and on
  Windows only directly launchable executable/batch extensions (`.com`, `.exe`, `.bat`, `.cmd`) are accepted.
- Vendor-side tool/MCP/hook/write restrictions are enforced via CLI flags (`--tools ""`, `--strict-mcp-config`,
  and `--setting-sources user` for Claude; `--sandbox read-only` for Codex; `--approval-mode plan` + a
  deny-all `--policy` TOML for Gemini), backed by a bounded capability preflight that fails closed on
  unsupported CLI versions.
- Inherited vendor API-key / cloud-routing environment variables are scrubbed per backend, and loaded Claude
  settings files are checked before launch so API keys or cloud selectors cannot silently override the
  user's CLI subscription billing route.
- Subprocess trees are tracked independently of leader exit and only marked clean once termination is
  confirmed (Windows kill-on-close Job Object with descendant capture; POSIX process groups).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .messages import (
    PREFIX_KINDS,
    Completed,
    ContentBlock,
    Delta,
    Message,
    PromptRequest,
    ProviderError,
    StreamEvent,
    ToolCall,
    ToolDefinition,
    ToolProgress,
    Usage,
    as_prompt,
)
from .redaction import redact

CLI_PROTOCOLS: frozenset[str] = frozenset({"cli-claude", "cli-codex", "cli-gemini"})
CLI_BINARIES: dict[str, str] = {
    "cli-claude": "claude",
    "cli-codex": "codex",
    "cli-gemini": "gemini",
}
CLI_LABELS: dict[str, str] = {
    "cli-claude": "Claude Code CLI",
    "cli-codex": "OpenAI Codex CLI",
    "cli-gemini": "Google Gemini CLI",
}
CLI_PRESET_KEYS: dict[str, str] = {
    "cli-claude": "claude-cli",
    "cli-codex": "codex-cli",
    "cli-gemini": "gemini-cli",
}
CLI_DEFAULT_MODELS: dict[str, str] = {
    "cli-claude": "claude-sonnet-5-5",
    "cli-codex": "gpt-5.6",
    "cli-gemini": "gemini-2.5-flash",
}
CLI_INSTALL_HINTS: dict[str, str] = {
    "cli-claude": "请先安装并登录官方 Claude Code CLI（claude）后再试",
    "cli-codex": "请先安装并登录官方 OpenAI Codex CLI（codex）后再试",
    "cli-gemini": "请先安装并登录官方 Google Gemini CLI（gemini）后再试",
}

# Strict identifier pattern prevents CLI flag injection (leading '-') and Windows .cmd metacharacters.
CLI_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
CLI_MODEL_ERROR = "CLI 代理协议的 model 必须以字母或数字开头，仅允许字母、数字及 . _ : / @ -（最长 128 字符）。"

# Vendor-side deny-all policy loaded via `--policy` for Gemini CLI (highest priority, overrides auto-edit).
GEMINI_DENY_ALL_POLICY = '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n'

# Exact environment variables and prefixes scrubbed per protocol so inherited API keys / cloud selectors
# never silently hijack the user's official CLI subscription billing route.
_CLAUDE_SCRUB_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
    }
)
_CLAUDE_SCRUB_PREFIXES = ("CLAUDE_CODE_USE_",)
_CLAUDE_SETTINGS_BLOCKED_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")
_CODEX_SCRUB_EXACT = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
    }
)
_GEMINI_SCRUB_EXACT = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
    }
)

# Windows launcher extensions that `CreateProcessW` / `subprocess` can run directly without an external interpreter.
_WINDOWS_DIRECT_LAUNCH_EXTS: tuple[str, ...] = (".com", ".exe", ".bat", ".cmd")
_WINDOWS_DIRECT_LAUNCH_SET: frozenset[str] = frozenset(_WINDOWS_DIRECT_LAUNCH_EXTS)

# Required CLI flags verified by capability preflight so older CLIs lacking tool/MCP/hook/write isolation fail closed.
_REQUIRED_HELP_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "cli-claude": (("--help",), ("--tools", "--strict-mcp-config", "--setting-sources")),
    "cli-codex": (("exec", "--help"), ("--json", "--sandbox")),
    "cli-gemini": (("--help",), ("--approval-mode", "--policy")),
}
# Claude Code < 2.1.75 on Windows loaded legacy ProgramData managed settings (GHSA-5cwg-9f6j-9jvx).
_CLAUDE_MIN_WINDOWS_VERSION: tuple[int, int, int, bool] = (2, 1, 75, True)
_SEMVER_RE = re.compile(r"(?<![0-9A-Za-z._-])v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?![0-9A-Za-z.])")

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_TOOL_ENVELOPE_CHARS = 512 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_HELP_BYTES = 256 * 1024
MAX_CLEANUP_ATTEMPTS = 3
READ_CHUNK_BYTES = 64 * 1024
UNSUPPORTED_IMAGE = "当前模型不支持图片，已省略"
_CAPABILITY_CACHE: dict[tuple[str, str, int, int], str | None] = {}
_INFLIGHT_HELP_PROBES: dict[tuple[int, str, str], tuple[tuple[int, int] | None, asyncio.Task[bool]]] = {}


@dataclass(frozen=True)
class DetectedCLIBackend:
    """An installed official vendor CLI discovered on PATH."""

    protocol: str
    preset_key: str
    binary: str
    label: str
    executable: str
    default_model: str


def is_valid_cli_model(model: str) -> bool:
    """True when `model` is a safe model identifier with no flag-injection or shell metacharacters."""
    return isinstance(model, str) and bool(CLI_MODEL_RE.fullmatch(model))


def _windows_candidate_names(binary: str, pathext_env: str | None = None) -> list[str]:
    """Return directly launchable Windows executable candidates, skipping interpreter-only scripts (e.g. `.ps1`)."""
    suffix = Path(binary).suffix.lower()
    if suffix:
        return [binary] if suffix in _WINDOWS_DIRECT_LAUNCH_SET else []
    raw_ext = os.environ.get("PATHEXT", ";".join(_WINDOWS_DIRECT_LAUNCH_EXTS)) if pathext_env is None else pathext_env
    exts: list[str] = []
    for item in raw_ext.split(";"):
        ext = item.strip().lower()
        if ext in _WINDOWS_DIRECT_LAUNCH_SET and ext not in exts:
            exts.append(ext)
    if not exts:
        exts = list(_WINDOWS_DIRECT_LAUNCH_EXTS)
    return [f"{binary}{ext}" for ext in exts]


def resolve_cli_executable(
    binary: str,
    *,
    path_env: str | None = None,
    pathext_env: str | None = None,
) -> str | None:
    """Resolve `binary` strictly from explicit absolute directories in `PATH` (never CWD or relative entries)."""
    if not binary or os.sep in binary or (os.altsep and os.altsep in binary) or ":" in binary:
        return None
    raw_path = os.environ.get("PATH", "") if path_env is None else path_env
    if not raw_path:
        return None
    candidates = _windows_candidate_names(binary, pathext_env) if os.name == "nt" else [binary]
    if not candidates:
        return None

    for entry in raw_path.split(os.pathsep):
        cleaned = entry.strip().strip('"')
        if not cleaned:
            continue
        dir_path = Path(cleaned)
        if not dir_path.is_absolute():
            continue
        for name in candidates:
            candidate = dir_path / name
            try:
                if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                    return str(Path(os.path.abspath(candidate)))
            except OSError:
                continue
    return None


def detect_installed_clis(
    which: Callable[[str], str | None] = resolve_cli_executable,
) -> list[DetectedCLIBackend]:
    """Detect allow-listed official CLIs available on absolute PATH entries without spawning any subprocess."""
    found: list[DetectedCLIBackend] = []
    for protocol in ("cli-claude", "cli-codex", "cli-gemini"):
        binary = CLI_BINARIES[protocol]
        executable = which(binary)
        if executable and Path(executable).is_absolute():
            found.append(
                DetectedCLIBackend(
                    protocol=protocol,
                    preset_key=CLI_PRESET_KEYS[protocol],
                    binary=binary,
                    label=CLI_LABELS[protocol],
                    executable=executable,
                    default_model=CLI_DEFAULT_MODELS[protocol],
                )
            )
    return found


def build_cli_argv(
    protocol: str,
    executable: str,
    model: str,
    *,
    policy_path: str | None = None,
) -> list[str]:
    """Construct the strict allow-listed argv list for one headless official CLI invocation."""
    if protocol not in CLI_PROTOCOLS:
        raise ProviderError(f"不支持的 CLI 代理协议：{protocol}。")
    if not is_valid_cli_model(model):
        raise ProviderError(CLI_MODEL_ERROR)
    if protocol == "cli-claude":
        return [
            executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "user",
            "--model",
            model,
        ]
    if protocol == "cli-codex":
        return [
            executable,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--model",
            model,
            "-",
        ]
    if not policy_path or not isinstance(policy_path, str):
        raise ProviderError("Google Gemini CLI 缺少强制只读策略文件，已拒绝启动。")
    return [
        executable,
        "-p",
        "",
        "--output-format",
        "stream-json",
        "--approval-mode",
        "plan",
        "--policy",
        policy_path,
        "--model",
        model,
    ]


def _cli_env(protocol: str) -> dict[str, str]:
    """Preserve user CLI subscription context while scrubbing MossCode flags and vendor API-key overrides."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("MOSSCODE_"):
            continue
        if protocol == "cli-claude" and (
            upper in _CLAUDE_SCRUB_EXACT or any(upper.startswith(p) for p in _CLAUDE_SCRUB_PREFIXES)
        ):
            continue
        if protocol == "cli-codex" and upper in _CODEX_SCRUB_EXACT:
            continue
        if protocol == "cli-gemini" and upper in _GEMINI_SCRUB_EXACT:
            continue
        env[key] = value
    env.setdefault("NO_COLOR", "1")
    return env


def _claude_managed_dir(env: Mapping[str, str]) -> Path:
    """Return the documented OS managed-settings root for Claude Code (excluding legacy unused Windows paths)."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if os.name == "nt":
        root = env.get("ProgramFiles") or os.environ.get("ProgramFiles") or r"C:\Program Files"
        return Path(root) / "ClaudeCode"
    return Path("/etc/claude-code")


def _claude_settings_paths(env: Mapping[str, str]) -> list[Path]:
    """Return user settings plus active managed settings (`managed-settings.json` and `managed-settings.d/*.json`)."""
    raw_dir = env.get("CLAUDE_CONFIG_DIR", "").strip()
    user_root = Path(raw_dir).expanduser() if raw_dir else Path.home() / ".claude"
    paths = [user_root / "settings.json"]
    managed_dir = _claude_managed_dir(env)
    paths.append(managed_dir / "managed-settings.json")
    dropin_dir = managed_dir / "managed-settings.d"
    try:
        entries = sorted(dropin_dir.iterdir(), key=lambda p: p.name)
    except FileNotFoundError:
        entries = []
    except OSError as exc:
        raise ProviderError(
            f"无法读取 Claude Code 受管配置目录 {dropin_dir} 以校验计费路由（{redact(str(exc))}），已拒绝启动 cli-claude。"
        ) from None
    for entry in entries:
        # Claude Code ignores hidden dot-files and matches the `.json` suffix case-sensitively.
        if entry.name.startswith(".") or not entry.name.endswith(".json"):
            continue
        paths.append(entry)
    return paths


def _is_truthy_selector(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _load_claude_settings_json(path: Path) -> dict[str, Any] | None:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ProviderError(
            f"无法读取 Claude Code 配置文件 {path} 以校验计费路由（{redact(str(exc))}），已拒绝启动 cli-claude。"
        ) from None
    try:
        data = json.loads(raw_text)
    except ValueError:
        raise ProviderError(
            f"Claude Code 配置文件 {path} 不是合法的 JSON，无法确认是否覆盖订阅计费路由，已拒绝启动 cli-claude。"
        ) from None
    if not isinstance(data, dict):
        raise ProviderError(
            f"Claude Code 配置文件 {path} 根节点必须是 JSON 对象，无法确认计费路由安全，已拒绝启动 cli-claude。"
        )
    raw_env = data.get("env")
    if raw_env is not None and not isinstance(raw_env, dict):
        raise ProviderError(
            f"Claude Code 配置文件 {path} 的 env 字段格式无效，无法确认计费路由安全，已拒绝启动 cli-claude。"
        )
    return data


def _validate_claude_settings_dict(data: Mapping[str, Any], source_label: str) -> None:
    blocked: list[str] = []
    for key in _CLAUDE_SETTINGS_BLOCKED_KEYS:
        val = data.get(key)
        if val is not None and (not isinstance(val, str) or val.strip() != ""):
            blocked.append(key)
    login_method = data.get("forceLoginMethod")
    if isinstance(login_method, str) and login_method.strip() and login_method.strip().lower() != "claudeai":
        blocked.append("forceLoginMethod")
    elif login_method is not None and not isinstance(login_method, str):
        blocked.append("forceLoginMethod")

    raw_env = data.get("env")
    if isinstance(raw_env, dict):
        for key, val in raw_env.items():
            if not isinstance(key, str):
                continue
            upper = key.upper()
            if upper in _CLAUDE_SCRUB_EXACT:
                if val is not None and str(val).strip() != "":
                    blocked.append(f"env.{key}")
            elif any(upper.startswith(prefix) for prefix in _CLAUDE_SCRUB_PREFIXES):
                if _is_truthy_selector(val):
                    blocked.append(f"env.{key}")

    if blocked:
        keys_text = "、".join(sorted(set(blocked)))
        raise ProviderError(
            f"检测到 Claude Code 配置 {source_label} 设置了会覆盖订阅计费路由的项（{keys_text}），"
            "已拒绝启动 cli-claude；如需使用该 API 配置请改用 follow: claude-code，或先清理这些配置项。"
        )


def _check_claude_settings_billing(
    env: Mapping[str, str],
    *,
    settings_paths: Sequence[Path] | None = None,
) -> None:
    """Fail closed if user settings or effective managed settings (+ `managed-settings.d/*.json`) override billing."""
    paths = _claude_settings_paths(env) if settings_paths is None else list(settings_paths)
    managed_layers: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        data = _load_claude_settings_json(path)
        if data is None:
            continue
        if path.name == "managed-settings.json" or path.parent.name == "managed-settings.d":
            managed_layers.append((path, data))
        else:
            _validate_claude_settings_dict(data, str(path))

    if managed_layers:
        effective: dict[str, Any] = {}
        effective_env: dict[str, Any] = {}
        has_env = False
        for _path, layer in managed_layers:
            for key, val in layer.items():
                if key == "env":
                    if isinstance(val, dict):
                        has_env = True
                        effective_env.update(val)
                else:
                    effective[key] = val
        if has_env:
            effective["env"] = effective_env
        sources = "、".join(str(p) for p, _ in managed_layers)
        _validate_claude_settings_dict(effective, sources)


def _strip_images(messages: Sequence[Message]) -> list[Message]:
    """Replace image blocks and tool-result images with text placeholders for text-only CLI stdin."""
    prepared: list[Message] = []
    for message in messages:
        content = tuple(
            block if block.image is None else ContentBlock("text", block.image.placeholder(UNSUPPORTED_IMAGE))
            for block in message.content
        )
        results = []
        for result in message.tool_results:
            if result.images:
                notes = [ref.placeholder(UNSUPPORTED_IMAGE) for ref in result.images]
                output = "\n".join([result.output, *notes] if result.output else notes)
                result = replace(result, output=output, images=())
            results.append(result)
        prepared.append(replace(message, content=content, tool_results=tuple(results)))
    return prepared


def render_cli_prompt(
    messages: Sequence[Message],
    system: str | PromptRequest,
    tools: Sequence[ToolDefinition] = (),
    *,
    allow_tools: bool = True,
    nonce: str | None = None,
) -> str:
    """Serialize system prompt, conversation history, and nonce-framed tool schema for CLI stdin."""
    prompt = as_prompt(system)
    sections: list[str] = []

    if prompt.stable:
        sections.append(f"[System Instructions]\n{prompt.stable}")

    prefix_supplements = [item.render() for item in prompt.supplements if item.kind in PREFIX_KINDS]
    if prefix_supplements:
        sections.append("[Context]\n" + "\n\n".join(prefix_supplements))

    if tools and allow_tools and nonce:
        open_tag = f"<mosscode_tool_call_{nonce}>"
        close_tag = f"</mosscode_tool_call_{nonce}>"
        catalog_entries = [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]
        tool_guide = (
            "[MossCode Tool Bridge]\n"
            "You are serving as the reasoning backend inside MossCode. "
            "Do NOT invoke your own built-in shell, file, or web tools.\n"
            "To invoke a MossCode tool, emit one or more blocks using this exact XML tag and JSON object:\n"
            f'{open_tag}{{"name": "<tool_name>", "arguments": {{...}}}}{close_tag}\n'
            "Rules:\n"
            "- Only use tool names listed below; arguments MUST be a valid JSON object matching the schema.\n"
            "- Wait for MossCode to return [Tool Result] in the next turn before assuming a tool succeeded.\n"
            "- When no tool is needed, answer directly in plain text without emitting the tag.\n"
            "Available tools (JSON):\n" + json.dumps(catalog_entries, ensure_ascii=False)
        )
        sections.append(tool_guide)
    else:
        sections.append(
            "[Execution Constraints]\n"
            "Do NOT invoke any built-in tools, file edits, or shell commands. Reply with plain text only."
        )

    cleaned = _strip_images(messages)
    transcript: list[str] = []
    for message in cleaned:
        if message.role == "tool":
            for result in message.tool_results:
                status = "error" if result.is_error else "ok"
                transcript.append(
                    f"[Tool Result call_id={result.call_id} name={result.name} status={status}]\n{result.model_text()}"
                )
        elif message.role == "assistant":
            parts: list[str] = []
            if message.text:
                parts.append(message.text)
            for call in message.tool_calls:
                parts.append(f"[Tool Call id={call.id} name={call.name}]\n{call.arguments}")
            transcript.append("[Assistant]\n" + "\n".join(parts))
        else:
            transcript.append(f"[User]\n{message.text}")
    if transcript:
        sections.append("[Conversation]\n" + "\n\n".join(transcript))

    trailing_supplements = [item.render() for item in prompt.supplements if item.kind not in PREFIX_KINDS]
    if trailing_supplements:
        sections.append("[Turn Reminders]\n" + "\n\n".join(trailing_supplements))

    return "\n\n".join(sections) + "\n"


def _parse_tool_envelope(payload: str, allowed_names: frozenset[str]) -> ToolCall | None:
    """Parse and strictly validate one nonce-delimited tool-call JSON payload."""
    try:
        parsed = json.loads(payload.strip())
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name")
    arguments = parsed.get("arguments")
    if not isinstance(name, str) or name not in allowed_names:
        return None
    if not isinstance(arguments, dict):
        return None
    return ToolCall(
        id=f"cli_{secrets.token_hex(6)}",
        name=name,
        arguments=json.dumps(arguments, ensure_ascii=False),
    )


class _ToolTagFilter:
    """Streaming splitter that emits visible text deltas while extracting bounded nonce-delimited tool calls."""

    def __init__(
        self,
        nonce: str | None,
        allowed_names: frozenset[str],
        *,
        max_envelope_chars: int = MAX_TOOL_ENVELOPE_CHARS,
    ) -> None:
        self._open = f"<mosscode_tool_call_{nonce}>" if nonce else ""
        self._close = f"</mosscode_tool_call_{nonce}>" if nonce else ""
        self._allowed = allowed_names
        self._max_envelope_chars = max_envelope_chars
        self._in_tag = False
        self._tag_oversized = False
        self._buf = ""
        self._tag_tail = ""
        self._close_scan_pos = 0
        self.text_parts: list[str] = []
        self.calls: list[ToolCall] = []

    def _emit_text(self, chunk: str, out: list[StreamEvent]) -> None:
        if chunk:
            self.text_parts.append(chunk)
            out.append(Delta("text", chunk))

    def feed(self, chunk: str) -> list[StreamEvent]:
        if not chunk:
            return []
        if not self._open:
            self.text_parts.append(chunk)
            return [Delta("text", chunk)]

        out: list[StreamEvent] = []
        if self._in_tag and self._tag_oversized:
            window = self._tag_tail + chunk
            end_idx = window.find(self._close)
            if end_idx == -1:
                self._tag_tail = window[-(len(self._close) - 1) :] if len(self._close) > 1 else ""
                return out
            # Oversized candidate is discarded without parsing or dispatching; resume after close tag.
            self._in_tag = False
            self._tag_oversized = False
            self._tag_tail = ""
            self._close_scan_pos = 0
            self._buf = window[end_idx + len(self._close) :]
        else:
            prev_len = len(self._buf)
            self._buf += chunk
            if self._in_tag:
                self._close_scan_pos = max(0, prev_len - len(self._close) + 1)

        while self._buf:
            if self._in_tag:
                end_idx = self._buf.find(self._close, self._close_scan_pos)
                if end_idx == -1:
                    if len(self._buf) > self._max_envelope_chars + len(self._close):
                        self._tag_oversized = True
                        self._tag_tail = self._buf[-(len(self._close) - 1) :] if len(self._close) > 1 else ""
                        self._buf = ""
                        self._close_scan_pos = 0
                    else:
                        self._close_scan_pos = max(0, len(self._buf) - len(self._close) + 1)
                    break
                raw_payload = self._buf[:end_idx]
                self._buf = self._buf[end_idx + len(self._close) :]
                self._in_tag = False
                self._close_scan_pos = 0
                if len(raw_payload) <= self._max_envelope_chars:
                    call = _parse_tool_envelope(raw_payload, self._allowed)
                    if call is not None:
                        self.calls.append(call)
                        out.append(ToolProgress(call, "parsing"))
            else:
                start_idx = self._buf.find(self._open)
                if start_idx != -1:
                    self._emit_text(self._buf[:start_idx], out)
                    self._buf = self._buf[start_idx + len(self._open) :]
                    self._in_tag = True
                    self._tag_oversized = False
                    self._tag_tail = ""
                    self._close_scan_pos = 0
                    continue
                # Hold back only a suffix that could still complete `self._open`.
                keep = 0
                max_prefix = min(len(self._buf), len(self._open) - 1)
                for size in range(max_prefix, 0, -1):
                    if self._open.startswith(self._buf[-size:]):
                        keep = size
                        break
                emit_len = len(self._buf) - keep
                if emit_len > 0:
                    self._emit_text(self._buf[:emit_len], out)
                    self._buf = self._buf[emit_len:]
                break
        return out

    def finish(self) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        if not self._in_tag and self._buf:
            self._emit_text(self._buf, out)
        # Drop any truncated/unclosed or oversized tool-call tag without executing it (fail-closed).
        self._buf = ""
        self._tag_tail = ""
        self._in_tag = False
        self._tag_oversized = False
        self._close_scan_pos = 0
        return out


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _extract_error_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("message", "error", "detail", "result"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, dict):
                nested = _extract_error_text(candidate)
                if nested:
                    return nested
    return ""


def _parse_claude_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    uncached = _int_or_none(raw.get("input_tokens"))
    output = _int_or_none(raw.get("output_tokens"))
    read = _int_or_none(raw.get("cache_read_input_tokens"))
    write = _int_or_none(raw.get("cache_creation_input_tokens"))
    if raw.get("cache_read_input_tokens") is None and raw.get("cache_creation_input_tokens") is None:
        total = uncached
        uncached = None
    else:
        total = uncached + read + write if all(v is not None for v in (uncached, read, write)) else None
    return Usage(total, output, read, write, uncached)


def _parse_codex_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    total = _int_or_none(raw.get("input_tokens"))
    output = _int_or_none(raw.get("output_tokens"))
    cached = _int_or_none(raw.get("cached_input_tokens"))
    uncached = max(total - cached, 0) if (total is not None and cached is not None) else None
    return Usage(total, output, cached, None, uncached)


def _parse_gemini_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    total = _int_or_none(raw.get("input_tokens"))
    if total is None:
        total = _int_or_none(raw.get("prompt_tokens"))
    if total is None:
        total = _int_or_none(raw.get("input"))
    output = _int_or_none(raw.get("output_tokens"))
    if output is None:
        output = _int_or_none(raw.get("candidates_tokens"))
    if output is None:
        output = _int_or_none(raw.get("output"))
    cached = _int_or_none(raw.get("cached"))
    if cached is None:
        cached = _int_or_none(raw.get("cached_input_tokens"))
    uncached = max(total - cached, 0) if (total is not None and cached is not None) else None
    return Usage(total, output, cached, None, uncached)


class _CLIStreamParser:
    """Stateful NDJSON/JSONL parser for Claude Code, Codex, and Gemini CLI headless streams."""

    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.usage = Usage()
        self.error_message: str = ""
        self.saw_json_frame = False
        self.saw_terminal_success = False
        self.saw_stream_text = False
        self.saw_stream_thinking = False
        self.saw_any_text = False
        self.saw_any_thinking = False
        self.thinking_parts: list[str] = []
        self._claude_usage_fields: dict[str, Any] = {}
        self.non_json_tail: str = ""

    def _note_non_json(self, stripped: str) -> None:
        if not stripped:
            return
        combined = f"{self.non_json_tail} {stripped}".strip() if self.non_json_tail else stripped
        self.non_json_tail = combined[-512:]

    def feed_line(self, line: str) -> tuple[list[str], list[str]]:
        """Parse one stdout line; return `(text_chunks, thinking_chunks)`."""
        stripped = line.strip()
        if not stripped:
            return [], []
        if not stripped.startswith("{"):
            self._note_non_json(stripped)
            return [], []
        try:
            frame = json.loads(stripped)
        except ValueError:
            self._note_non_json(stripped)
            return [], []
        if not isinstance(frame, dict):
            self._note_non_json(stripped)
            return [], []
        ftype = frame.get("type")
        if ftype is not None and not isinstance(ftype, str):
            raise ProviderError(f"{CLI_LABELS[self.protocol]} 返回了非法流事件（type 字段必须为字符串）。")
        self.saw_json_frame = True
        if ftype is None:
            return [], []
        if self.protocol == "cli-claude":
            return self._feed_claude(ftype, frame)
        if self.protocol == "cli-codex":
            return self._feed_codex(ftype, frame)
        return self._feed_gemini(ftype, frame)

    def _feed_claude(self, ftype: str, frame: dict[str, Any]) -> tuple[list[str], list[str]]:
        texts: list[str] = []
        thinkings: list[str] = []

        if ftype == "stream_event":
            event = frame.get("event")
            if isinstance(event, dict):
                etype = event.get("type")
                if etype == "content_block_delta":
                    delta = event.get("delta")
                    if isinstance(delta, dict):
                        dtype = delta.get("type")
                        if dtype == "text_delta" and isinstance(delta.get("text"), str) and delta["text"]:
                            self.saw_stream_text = True
                            self.saw_any_text = True
                            texts.append(delta["text"])
                        elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str) and delta["thinking"]:
                            self.saw_stream_thinking = True
                            self.saw_any_thinking = True
                            self.thinking_parts.append(delta["thinking"])
                            thinkings.append(delta["thinking"])
                elif etype == "message_start":
                    msg = event.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                        self._claude_usage_fields.update(msg["usage"])
                        self.usage = _parse_claude_usage(self._claude_usage_fields)
                elif etype == "message_delta":
                    if isinstance(event.get("usage"), dict):
                        self._claude_usage_fields.update({k: v for k, v in event["usage"].items() if v is not None})
                        self.usage = _parse_claude_usage(self._claude_usage_fields)
                elif etype == "error":
                    self.error_message = _extract_error_text(event) or "Claude Code CLI 返回错误。"
            return texts, thinkings

        if ftype == "assistant":
            msg = frame.get("message")
            if isinstance(msg, dict):
                if isinstance(msg.get("usage"), dict):
                    self._claude_usage_fields.update({k: v for k, v in msg["usage"].items() if v is not None})
                    self.usage = _parse_claude_usage(self._claude_usage_fields)
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text" and isinstance(block.get("text"), str) and block["text"]:
                            if not self.saw_stream_text:
                                self.saw_any_text = True
                                texts.append(block["text"])
                        elif btype == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"]:
                            if not self.saw_stream_thinking:
                                self.saw_any_thinking = True
                                self.thinking_parts.append(block["thinking"])
                                thinkings.append(block["thinking"])
            return texts, thinkings

        if ftype == "result":
            if isinstance(frame.get("usage"), dict):
                self._claude_usage_fields.update({k: v for k, v in frame["usage"].items() if v is not None})
                self.usage = _parse_claude_usage(self._claude_usage_fields)
            raw_subtype = frame.get("subtype")
            subtype = raw_subtype if isinstance(raw_subtype, str) else ""
            is_error = frame.get("is_error")
            if is_error is True or subtype.startswith("error"):
                self.error_message = _extract_error_text(frame) or "Claude Code CLI 执行失败。"
            elif subtype == "success" and (is_error is None or is_error is False):
                self.saw_terminal_success = True
                if not self.saw_any_text and isinstance(frame.get("result"), str) and frame["result"]:
                    self.saw_any_text = True
                    texts.append(frame["result"])
            return texts, thinkings

        if ftype == "error":
            self.error_message = _extract_error_text(frame) or "Claude Code CLI 返回错误。"
        return texts, thinkings

    def _feed_codex(self, ftype: str, frame: dict[str, Any]) -> tuple[list[str], list[str]]:
        texts: list[str] = []
        thinkings: list[str] = []

        if ftype in {"item.delta", "response.output_text.delta"}:
            delta = frame.get("delta")
            if isinstance(delta, str) and delta:
                self.saw_stream_text = True
                self.saw_any_text = True
                texts.append(delta)
            return texts, thinkings

        if ftype == "item.completed":
            item = frame.get("item")
            if isinstance(item, dict):
                itype = item.get("type")
                if not isinstance(itype, str):
                    return texts, thinkings
                if itype in {"agent_message", "message"}:
                    if not self.saw_stream_text:
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            self.saw_any_text = True
                            texts.append(text)
                        elif isinstance(item.get("content"), list):
                            for block in item["content"]:
                                if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]:
                                    self.saw_any_text = True
                                    texts.append(block["text"])
                elif itype == "reasoning":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        self.saw_any_thinking = True
                        self.thinking_parts.append(text)
                        thinkings.append(text)
                    elif isinstance(item.get("summary"), list):
                        for block in item["summary"]:
                            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]:
                                self.saw_any_thinking = True
                                self.thinking_parts.append(block["text"])
                                thinkings.append(block["text"])
            return texts, thinkings

        if ftype == "turn.completed":
            self.saw_terminal_success = True
            if isinstance(frame.get("usage"), dict):
                self.usage = _parse_codex_usage(frame["usage"])
            return texts, thinkings

        if ftype in {"turn.failed", "error"}:
            self.error_message = _extract_error_text(frame) or "OpenAI Codex CLI 返回错误。"
        return texts, thinkings

    def _feed_gemini(self, ftype: str, frame: dict[str, Any]) -> tuple[list[str], list[str]]:
        texts: list[str] = []
        thinkings: list[str] = []

        if ftype == "message":
            raw_role = frame.get("role")
            role = raw_role.lower() if isinstance(raw_role, str) else ""
            if role not in {"assistant", "model"}:
                return texts, thinkings
            is_delta = frame.get("delta")
            if is_delta is False and self.saw_stream_text:
                return texts, thinkings
            content = frame.get("content")
            extracted: list[str] = []
            if isinstance(content, str) and content:
                extracted.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                        extracted.append(part["text"])
            if extracted:
                if is_delta is True:
                    self.saw_stream_text = True
                self.saw_any_text = True
                texts.extend(extracted)
            return texts, thinkings

        if ftype in {"thought", "thinking"}:
            content = frame.get("content") or frame.get("text")
            if isinstance(content, str) and content:
                self.saw_any_thinking = True
                self.thinking_parts.append(content)
                thinkings.append(content)
            return texts, thinkings

        if ftype == "result":
            stats = frame.get("stats") if isinstance(frame.get("stats"), dict) else frame.get("usage")
            if isinstance(stats, dict):
                parsed = _parse_gemini_usage(stats)
                if (
                    parsed.input_tokens is None
                    and parsed.output_tokens is None
                    and isinstance(stats.get("models"), dict)
                ):
                    # Sum per-model counters only when EVERY model provides a valid token record; never fabricate 0.
                    in_sum, out_sum, cache_sum = 0, 0, 0
                    in_complete, out_complete, cache_complete = True, True, True
                    saw_model = False
                    for entry in stats["models"].values():
                        saw_model = True
                        tokens = entry.get("tokens") if isinstance(entry, dict) else None
                        if not isinstance(tokens, dict):
                            in_complete = False
                            out_complete = False
                            cache_complete = False
                            continue
                        p = _parse_gemini_usage(tokens)
                        if p.input_tokens is None:
                            in_complete = False
                        else:
                            in_sum += p.input_tokens
                        if p.output_tokens is None:
                            out_complete = False
                        else:
                            out_sum += p.output_tokens
                        if p.cached_input_tokens is None:
                            cache_complete = False
                        else:
                            cache_sum += p.cached_input_tokens
                    if saw_model:
                        total_in = in_sum if in_complete else None
                        total_out = out_sum if out_complete else None
                        total_cached = cache_sum if cache_complete else None
                        uncached = (
                            max(total_in - total_cached, 0)
                            if (total_in is not None and total_cached is not None)
                            else None
                        )
                        parsed = Usage(total_in, total_out, total_cached, None, uncached)
                self.usage = parsed
            raw_status = frame.get("status")
            status = raw_status.lower() if isinstance(raw_status, str) else ""
            if status == "error" or frame.get("error"):
                self.error_message = _extract_error_text(frame) or "Google Gemini CLI 执行失败。"
            elif status == "success":
                self.saw_terminal_success = True
                if not self.saw_any_text and isinstance(frame.get("response"), str) and frame["response"]:
                    self.saw_any_text = True
                    texts.append(frame["response"])
            return texts, thinkings

        if ftype == "error":
            self.error_message = _extract_error_text(frame) or "Google Gemini CLI 返回错误。"
        return texts, thinkings


def _windows_descendant_pids(root_pid: int) -> list[int]:
    """Enumerate all currently alive descendant PIDs of `root_pid` via Toolhelp32 snapshot."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    api.Process32FirstW.restype = wintypes.BOOL
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    api.Process32NextW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL

    snap = api.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    invalid_handle = ctypes.c_void_p(-1).value
    if not snap or snap == invalid_handle:
        return []
    children_by_parent: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        if api.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                children_by_parent.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                if not api.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        api.CloseHandle(snap)

    descendants: list[int] = []
    seen = {root_pid}
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children_by_parent.get(current, ()):
            if child not in seen:
                seen.add(child)
                descendants.append(child)
                stack.append(child)
    return descendants


def _windows_pid_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL

    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == 259  # STILL_ACTIVE
    finally:
        api.CloseHandle(handle)


def _taskkill_tree_windows(pid: int) -> None:
    """Fallback Windows process-tree kill using System32 taskkill.exe."""
    if os.name != "nt":
        return
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    exe = os.path.join(system_root, "System32", "taskkill.exe")
    if not os.path.isfile(exe):
        return
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [exe, "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )


class _WinProcessJob:
    """Minimal L0 Windows kill-on-close Job Object capturing leader and early descendants."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64),
                ("JobTime", ctypes.c_int64),
                ("Flags", wintypes.DWORD),
                ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t),
                ("ActiveProcesses", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IOCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperations",
                    "WriteOperations",
                    "OtherOperations",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("Basic", _BasicLimit),
                ("IO", _IOCounters),
                ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t),
                ("PeakJobMemory", ctypes.c_size_t),
            ]

        class _BasicAccounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL

        self._api = api
        self._accounting_cls = _BasicAccounting
        self._handle = api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimit()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._assign_pid(pid, required=True)
            # Capture any descendants spawned between CreateProcess and AssignProcessToJobObject.
            assigned = {pid}
            for _ in range(3):
                newly = [child for child in _windows_descendant_pids(pid) if child not in assigned]
                if not newly:
                    break
                for child in newly:
                    assigned.add(child)
                    self._assign_pid(child, required=False)
        except OSError:
            self.close()
            raise

    def _assign_pid(self, pid: int, *, required: bool) -> None:
        import ctypes

        proc_handle = self._api.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not proc_handle:
            if required:
                raise ctypes.WinError(ctypes.get_last_error())
            return
        try:
            if not self._api.AssignProcessToJobObject(self._handle, proc_handle) and required:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._api.CloseHandle(proc_handle)

    def active_processes(self) -> int | None:
        import ctypes

        if not self._handle:
            return 0
        info = self._accounting_cls()
        if not self._api.QueryInformationJobObject(self._handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return int(info.ActiveProcesses)

    def terminate(self) -> None:
        if self._handle:
            with suppress(OSError):
                self._api.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            handle = self._handle
            self._handle = None
            with suppress(OSError):
                self._api.CloseHandle(handle)


def _attach_windows_job(pid: int) -> tuple[_WinProcessJob | None, OSError | None]:
    if os.name != "nt":
        return None, None
    try:
        return _WinProcessJob(pid), None
    except OSError as exc:
        return None, exc


@dataclass(eq=False)
class _ManagedProcess:
    """Process-tree ownership tracked independently of leader exit."""

    proc: asyncio.subprocess.Process
    pid: int
    job: _WinProcessJob | None = None
    pgid: int | None = None
    cleaned: bool = False
    cleanup_attempts: int = 0
    teardown_task: asyncio.Task[bool] | None = None
    probe_key: tuple[str, str] | None = None


_UNCONFIRMED_HELP_PROBES: set[_ManagedProcess] = set()


def _linux_pgid_has_live_member(pgid: int, *, proc_root: Path = Path("/proc")) -> bool | None:
    """Inspect Linux `/proc/<pid>/stat` to distinguish live process-group members from unreaped zombies.

    Returns `True` if a live member exists, `False` if only zombies/dead entries (or none) exist,
    or `None` when liveness cannot be determined (caller must fail closed and retain ownership).
    """
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    uncertain = False
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            raw = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            try:
                if os.getpgid(pid) == pgid:
                    uncertain = True
            except ProcessLookupError:
                continue
            except (OSError, AttributeError):
                uncertain = True
            continue
        rparen = raw.rfind(")")
        if rparen == -1:
            uncertain = True
            continue
        fields = raw[rparen + 1 :].split()
        if len(fields) < 3:
            uncertain = True
            continue
        state = fields[0]
        try:
            member_pgid = int(fields[2])
        except ValueError:
            uncertain = True
            continue
        if member_pgid != pgid:
            continue
        if state not in {"Z", "X", "x"}:
            return True
        # A zombie main thread can still have running worker threads in the same thread group.
        # Field 20 (`num_threads`, index 17 after `") "`) must equal 1 to confirm no other threads remain.
        if len(fields) <= 17:
            uncertain = True
            continue
        try:
            num_threads = int(fields[17])
        except ValueError:
            uncertain = True
            continue
        if num_threads < 1:
            uncertain = True
        elif num_threads > 1:
            return True
    return None if uncertain else False


def _pgid_alive(pgid: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    if sys.platform.startswith("linux"):
        live = _linux_pgid_has_live_member(pgid)
        # Fail closed when `/proc` inspection is inconclusive.
        return True if live is None else live
    return True


def _managed_alive(managed: _ManagedProcess) -> bool:
    """True when the leader or any tracked descendant is still alive (or status is unknown)."""
    if managed.proc.returncode is None:
        return True
    if os.name == "nt":
        if managed.job is not None:
            active = managed.job.active_processes()
            # Keep job status pending when accounting query fails; a parent-PID tree walk can miss
            # live grandchildren whose intermediate parent already exited.
            return True if active is None else active > 0
        return _windows_pid_alive(managed.pid) or bool(_windows_descendant_pids(managed.pid))
    return managed.pgid is not None and _pgid_alive(managed.pgid)


def _signal_tree_sync(managed: _ManagedProcess) -> None:
    """Synchronously initiate process-tree termination before any await point."""
    if os.name == "nt":
        if managed.job is not None:
            managed.job.terminate()
        if managed.proc.returncode is None:
            with suppress(ProcessLookupError, OSError):
                managed.proc.terminate()
        return
    if managed.pgid is not None and _pgid_alive(managed.pgid):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(managed.pgid, signal.SIGTERM)
    if managed.proc.returncode is None:
        with suppress(ProcessLookupError, OSError):
            managed.proc.terminate()


def _kill_tree_sync(managed: _ManagedProcess) -> None:
    """Synchronously escalate process-tree termination on cancellation before re-raising."""
    if os.name == "nt":
        if managed.job is not None:
            managed.job.terminate()
        if managed.proc.returncode is None:
            with suppress(ProcessLookupError, OSError):
                managed.proc.kill()
        return
    if managed.pgid is not None and _pgid_alive(managed.pgid):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(managed.pgid, signal.SIGKILL)
    if managed.proc.returncode is None:
        with suppress(ProcessLookupError, OSError):
            managed.proc.kill()


async def _terminate_managed(
    managed: _ManagedProcess,
    *,
    grace: float = 0.5,
    kill_wait: float = 2.0,
) -> bool:
    """Tear down leader and descendants cleanly; mark `cleaned=True` only once exit is confirmed."""
    if managed.cleaned:
        return True
    proc = managed.proc
    try:
        if os.name == "nt":
            if managed.job is not None:
                managed.job.terminate()
            elif _managed_alive(managed):
                await asyncio.to_thread(_taskkill_tree_windows, managed.pid)
            if proc.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    proc.terminate()
            loop = asyncio.get_running_loop()
            grace_deadline = loop.time() + grace
            while loop.time() < grace_deadline and _managed_alive(managed):
                if proc.returncode is None:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=0.05)
                else:
                    await asyncio.sleep(0.02)
            if _managed_alive(managed):
                if managed.job is not None:
                    managed.job.terminate()
                else:
                    await asyncio.to_thread(_taskkill_tree_windows, managed.pid)
                if proc.returncode is None:
                    with suppress(ProcessLookupError, OSError):
                        proc.kill()
                kill_deadline = loop.time() + kill_wait
                while loop.time() < kill_deadline and _managed_alive(managed):
                    if proc.returncode is None:
                        with suppress(TimeoutError):
                            await asyncio.wait_for(proc.wait(), timeout=0.1)
                    else:
                        await asyncio.sleep(0.02)
        else:
            pgid = managed.pgid
            if _managed_alive(managed):
                if pgid is not None:
                    with suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(pgid, signal.SIGTERM)
                if proc.returncode is None:
                    with suppress(ProcessLookupError, OSError):
                        proc.terminate()
                loop = asyncio.get_running_loop()
                grace_deadline = loop.time() + grace
                while loop.time() < grace_deadline and _managed_alive(managed):
                    if proc.returncode is None:
                        with suppress(TimeoutError):
                            await asyncio.wait_for(proc.wait(), timeout=0.05)
                    else:
                        await asyncio.sleep(0.02)
            # Escalate to SIGKILL if leader or any descendant in the POSIX process group survived SIGTERM.
            if _managed_alive(managed):
                if pgid is not None:
                    with suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(pgid, signal.SIGKILL)
                if proc.returncode is None:
                    with suppress(ProcessLookupError, OSError):
                        proc.kill()
                loop = asyncio.get_running_loop()
                kill_deadline = loop.time() + kill_wait
                while loop.time() < kill_deadline and _managed_alive(managed):
                    if proc.returncode is None:
                        with suppress(TimeoutError):
                            await asyncio.wait_for(proc.wait(), timeout=0.1)
                    else:
                        await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        _kill_tree_sync(managed)
        raise

    confirmed = not _managed_alive(managed)
    if confirmed:
        if managed.job is not None:
            managed.job.close()
        managed.cleaned = True
    return confirmed


async def _run_teardown(managed: _ManagedProcess) -> bool:
    """Run or join one shared, cancellation-safe teardown task; count attempts once per created task."""
    if managed.cleaned:
        return True
    task = managed.teardown_task
    if task is None or task.done():
        managed.cleanup_attempts += 1
        task = asyncio.create_task(_terminate_managed(managed))
        managed.teardown_task = task

        def _clear(done_task: asyncio.Task[bool]) -> None:
            if managed.teardown_task is done_task:
                managed.teardown_task = None
            with suppress(BaseException):
                done_task.exception()

        task.add_done_callback(_clear)
    return await asyncio.shield(task)


async def _write_stdin(stream: asyncio.StreamWriter | None, payload: bytes) -> None:
    if stream is None:
        return
    try:
        stream.write(payload)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        with suppress(OSError):
            stream.close()
        with suppress(OSError):
            await stream.wait_closed()


async def _drain_stderr(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    buf = bytearray()
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_STDERR_BYTES:
            del buf[: len(buf) - MAX_STDERR_BYTES]
    return bytes(buf)


async def _iter_lines_bounded(
    stream: asyncio.StreamReader,
    *,
    max_line_bytes: int = MAX_LINE_BYTES,
) -> AsyncIterator[str]:
    """Read newline-delimited UTF-8 lines, enforcing `max_line_bytes` per line (excluding delimiter)."""
    pending = bytearray()
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            idx = pending.find(b"\n")
            if idx == -1:
                break
            raw_line = bytes(pending[:idx])
            del pending[: idx + 1]
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if len(raw_line) > max_line_bytes:
                raise ProviderError("官方 CLI 输出的单行 JSON 超过大小上限，已中止读取。")
            yield raw_line.decode("utf-8", errors="replace")
        # Allow a single trailing '\r' when CRLF splits across read chunks after an exact-length line.
        unterminated_len = len(pending) - (1 if pending.endswith(b"\r") else 0)
        if unterminated_len > max_line_bytes:
            raise ProviderError("官方 CLI 输出的单行 JSON 超过大小上限，已中止读取。")
    if pending:
        raw_line = bytes(pending[:-1] if pending.endswith(b"\r") else pending)
        if len(raw_line) > max_line_bytes:
            raise ProviderError("官方 CLI 输出的单行 JSON 超过大小上限，已中止读取。")
        yield raw_line.decode("utf-8", errors="replace")


def _safe_detail(text: str) -> str:
    cleaned = " ".join(redact(text).split())
    if len(cleaned) > 300:
        cleaned = cleaned[:300] + "…"
    return cleaned


async def _read_help_bounded(
    proc: asyncio.subprocess.Process,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, bytes]:
    """Read stdout and stderr concurrently under a single shared byte budget."""
    budget = [max_bytes]
    out_buf = bytearray()
    err_buf = bytearray()

    async def _pump(stream: asyncio.StreamReader | None, target: bytearray) -> None:
        if stream is None:
            return
        while True:
            limit = max(budget[0], 0)
            chunk = await stream.read(min(READ_CHUNK_BYTES, limit + 1))
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise ProviderError(f"{label} 帮助输出超过大小上限，已拒绝启用。")
            target.extend(chunk)

    stdout_task = asyncio.create_task(_pump(proc.stdout, out_buf))
    stderr_task = asyncio.create_task(_pump(proc.stderr, err_buf))
    wait_task = asyncio.create_task(proc.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                for other in pending:
                    other.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise exc
        if pending:
            await asyncio.gather(*pending)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return bytes(out_buf), bytes(err_buf)


def _parse_semver(text: str) -> tuple[int, int, int, bool] | None:
    """Parse the first semantic version (`major.minor.patch[-pre]`); final flag is True for non-prerelease."""
    match = _SEMVER_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4) is None)


async def _run_probe_subprocess(
    protocol: str,
    executable: str,
    args: Sequence[str],
    *,
    timeout: float,
    max_help_bytes: int,
    tracked: set[_ManagedProcess],
    probe_key: tuple[str, str],
    label: str,
) -> tuple[int | None, bytes, bytes]:
    """Run one bounded capability/version probe subprocess and retain its handle if teardown is unconfirmed."""
    spawn_kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": _cli_env(protocol),
    }
    if os.name == "nt":
        spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        spawn_kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(executable, *args, **spawn_kwargs)
    except OSError as exc:
        raise ProviderError(f"无法检测 {label} 版本能力：{redact(str(exc))}") from None

    job, job_err = _attach_windows_job(proc.pid)
    managed = _ManagedProcess(
        proc=proc,
        pid=proc.pid,
        job=job,
        pgid=None if os.name == "nt" else proc.pid,
        probe_key=probe_key,
    )
    tracked.add(managed)
    if os.name == "nt" and job is None and job_err is not None:
        if _windows_pid_alive(proc.pid) or _windows_descendant_pids(proc.pid):
            try:
                await _run_teardown(managed)
            finally:
                if managed.cleaned:
                    tracked.discard(managed)
            raise ProviderError(f"无法为 {label} 能力检测建立 Windows 进程树隔离，已中止执行。") from None

    try:
        async with asyncio.timeout(min(timeout, 10.0)):
            stdout_b, stderr_b = await _read_help_bounded(proc, max_bytes=max_help_bytes, label=label)
    except TimeoutError:
        raise ProviderError(f"检测 {label} 安全参数能力超时，已拒绝启用。") from None
    finally:
        _signal_tree_sync(managed)
        try:
            await _run_teardown(managed)
        finally:
            if managed.cleaned:
                tracked.discard(managed)

    if not managed.cleaned:
        raise ProviderError(f"检测 {label} 安全参数能力后子进程树未能确认完全退出，已保留待关闭重试。")
    return proc.returncode, stdout_b, stderr_b


def _executable_fingerprint(executable: str) -> tuple[int, int] | None:
    try:
        st = os.stat(executable)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


async def _execute_capability_probe(
    protocol: str,
    executable: str,
    *,
    expected_fp: tuple[int, int] | None,
    timeout: float,
    max_help_bytes: int,
    tracked: set[_ManagedProcess],
    probe_key: tuple[str, str],
) -> bool:
    """Run capability/version checks; return False without caching if the executable changed mid-probe."""
    help_args, required_flags = _REQUIRED_HELP_ARGS[protocol]
    label = CLI_LABELS[protocol]
    cache_key = (protocol, executable, expected_fp[0], expected_fp[1]) if expected_fp is not None else None

    returncode, stdout_b, stderr_b = await _run_probe_subprocess(
        protocol,
        executable,
        help_args,
        timeout=timeout,
        max_help_bytes=max_help_bytes,
        tracked=tracked,
        probe_key=probe_key,
        label=label,
    )

    if expected_fp is not None and _executable_fingerprint(executable) != expected_fp:
        return False

    # Do not permanently cache nonzero help exits so transient startup failures recover on retry.
    if returncode != 0:
        raise ProviderError(f"无法验证 {label} 安全参数能力（退出码 {returncode}），已拒绝启用。")

    combined = (stdout_b + b"\n" + stderr_b).decode("utf-8", errors="replace")
    missing = [flag for flag in required_flags if flag not in combined]
    if missing:
        err = (
            f"当前安装的 {label} 不支持必需的安全隔离参数（缺少 {'、'.join(missing)}），"
            f"为防止绕过 MossCode 权限与审批，已拒绝启用 {protocol}；请升级官方 CLI 后重试。"
        )
        if cache_key is not None and _executable_fingerprint(executable) == expected_fp:
            _CAPABILITY_CACHE[cache_key] = err
        raise ProviderError(err)

    if protocol == "cli-claude" and os.name == "nt":
        ver_rc, ver_out, ver_err = await _run_probe_subprocess(
            protocol,
            executable,
            ("--version",),
            timeout=timeout,
            max_help_bytes=max_help_bytes,
            tracked=tracked,
            probe_key=probe_key,
            label=label,
        )
        if expected_fp is not None and _executable_fingerprint(executable) != expected_fp:
            return False
        if ver_rc != 0:
            raise ProviderError(f"无法验证 Windows {label} 版本（退出码 {ver_rc}），已拒绝启用。")
        parsed_ver = _parse_semver(ver_out.decode("utf-8", errors="replace")) or _parse_semver(
            ver_err.decode("utf-8", errors="replace")
        )
        if parsed_ver is None or parsed_ver < _CLAUDE_MIN_WINDOWS_VERSION:
            shown = (
                f"{parsed_ver[0]}.{parsed_ver[1]}.{parsed_ver[2]}" + ("" if parsed_ver[3] else "-prerelease")
                if parsed_ver is not None
                else "未知版本"
            )
            err = (
                f"Windows 平台要求官方 {label} 版本 >= 2.1.75（当前检测为 {shown}），"
                "以避免旧版本读取遗留 ProgramData 受管配置（GHSA-5cwg-9f6j-9jvx）；请升级官方 CLI 后重试。"
            )
            if cache_key is not None and _executable_fingerprint(executable) == expected_fp:
                _CAPABILITY_CACHE[cache_key] = err
            raise ProviderError(err)

    if expected_fp is not None and _executable_fingerprint(executable) != expected_fp:
        return False
    if cache_key is not None:
        _CAPABILITY_CACHE[cache_key] = None
    return True


async def verify_cli_capabilities(
    protocol: str,
    executable: str,
    *,
    timeout: float = 10.0,
    max_help_bytes: int = MAX_HELP_BYTES,
    active_processes: set[_ManagedProcess] | None = None,
    probe_tasks: set[asyncio.Task[Any]] | None = None,
) -> None:
    """Verify the installed official CLI advertises required vendor-side safety flags; fail closed otherwise."""
    if protocol not in _REQUIRED_HELP_ARGS:
        raise ProviderError(f"不支持的 CLI 代理协议：{protocol}。")
    label = CLI_LABELS[protocol]
    probe_key = (protocol, executable)
    tracked = _UNCONFIRMED_HELP_PROBES if active_processes is None else active_processes
    inflight_key = (id(tracked), protocol, executable)

    for _attempt in range(3):
        fp = _executable_fingerprint(executable)
        cache_key = (protocol, executable, fp[0], fp[1]) if fp is not None else None

        # Join a live reservation BEFORE checking `tracked`, so callers arriving after `tracked.add(managed)`
        # share the active probe rather than falsely tripping the unconfirmed-teardown rejection.
        existing_entry = _INFLIGHT_HELP_PROBES.get(inflight_key)
        if existing_entry is not None:
            existing_fp, existing_task = existing_entry
            if not existing_task.done():
                if existing_fp is not None and fp is not None and existing_fp == fp:
                    consistent = await asyncio.shield(existing_task)
                    if consistent and _executable_fingerprint(executable) == fp:
                        return
                    continue
                try:
                    await asyncio.shield(existing_task)
                except ProviderError:
                    pass
                continue

        # No active probe owns this binary: reject leftover unconfirmed processes from an earlier failure.
        for existing in list(tracked):
            if existing.probe_key != probe_key:
                continue
            if existing.cleaned:
                tracked.discard(existing)
                continue
            in_flight = existing.teardown_task is not None and not existing.teardown_task.done()
            if not in_flight and not _managed_alive(existing):
                if existing.job is not None:
                    existing.job.close()
                existing.cleaned = True
                tracked.discard(existing)
                continue
            raise ProviderError(
                f"上一次 {label} 能力检测子进程树（PID {existing.pid}）尚未确认完全退出，已拒绝重复启动检测；请先关闭重试清理。"
            )

        if cache_key is not None and cache_key in _CAPABILITY_CACHE:
            cached_err = _CAPABILITY_CACHE[cache_key]
            if cached_err is not None:
                raise ProviderError(cached_err)
            return

        # Reserve the probe key synchronously before any await so concurrent callers share one probe task.
        probe_task: asyncio.Task[bool] = asyncio.create_task(
            _execute_capability_probe(
                protocol,
                executable,
                expected_fp=fp,
                timeout=timeout,
                max_help_bytes=max_help_bytes,
                tracked=tracked,
                probe_key=probe_key,
            )
        )
        _INFLIGHT_HELP_PROBES[inflight_key] = (fp, probe_task)
        if probe_tasks is not None:
            probe_tasks.add(probe_task)

        def _release_probe(
            done_task: asyncio.Task[bool],
            *,
            key: tuple[int, str, str] = inflight_key,
            owners: set[asyncio.Task[Any]] | None = probe_tasks,
        ) -> None:
            entry = _INFLIGHT_HELP_PROBES.get(key)
            if entry is not None and entry[1] is done_task:
                _INFLIGHT_HELP_PROBES.pop(key, None)
            if owners is not None:
                owners.discard(done_task)
            with suppress(BaseException):
                done_task.exception()

        probe_task.add_done_callback(_release_probe)
        consistent = await asyncio.shield(probe_task)
        if consistent and (fp is None or _executable_fingerprint(executable) == fp):
            return

    raise ProviderError(f"检测 {label} 安全参数能力期间可执行文件持续变化，无法确认一致版本，已拒绝启用。")


def _write_temp_policy() -> Path:
    """Create a private temporary TOML policy file denying all native/MCP Gemini CLI tools."""
    # `tempfile.mkstemp` atomically creates the file with owner-only permissions (0o600 on POSIX).
    fd, raw_path = tempfile.mkstemp(prefix="mosscode-gemini-policy-", suffix=".toml")
    path = Path(raw_path)
    fd_owned = True
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd_owned = False
        with stream:
            stream.write(GEMINI_DENY_ALL_POLICY)
    except BaseException:
        if fd_owned:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise
    return path


class _Deadline:
    """Monotonic deadline tracker used outside caller yields to enforce wall-clock stream timeouts."""

    def __init__(self, loop: asyncio.AbstractEventLoop, timeout: float) -> None:
        self._loop = loop
        self.at = loop.time() + timeout

    def expired(self) -> bool:
        return self._loop.time() >= self.at


class CLIProxyProvider:
    """Official CLI execution adapter conforming to MossCode's L0/L1 Provider protocol."""

    def __init__(
        self,
        config: Any,
        *,
        which: Callable[[str], str | None] = resolve_cli_executable,
        argv_builder: Callable[..., list[str]] = build_cli_argv,
        capability_checker: Callable[[str, str], Any] | None = None,
        claude_settings_paths: Callable[[Mapping[str, str]], Sequence[Path]] | None = None,
    ) -> None:
        if config.protocol not in CLI_PROTOCOLS:
            raise ProviderError(f"不支持的 CLI 代理协议：{config.protocol}。")
        if not is_valid_cli_model(config.model):
            raise ProviderError(CLI_MODEL_ERROR)
        self.config = config
        self.thinking: str | None = None
        self._which = which
        self._argv_builder = argv_builder
        self._capability_checker = capability_checker
        self._claude_settings_paths = claude_settings_paths
        self._active: set[_ManagedProcess] = set()
        self._active_probes: set[asyncio.Task[Any]] = set()

    async def close(self) -> None:
        # Cancel and await provider-owned capability probes before collecting remaining process records,
        # so no probe can spawn or hold a subprocess after close() returns.
        while self._active_probes:
            probes = list(self._active_probes)
            for probe in probes:
                if not probe.done():
                    probe.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
            for probe in probes:
                self._active_probes.discard(probe)

        failed: list[int] = []
        for managed in list(self._active):
            if managed.cleaned:
                self._active.discard(managed)
                continue
            in_flight = managed.teardown_task is not None and not managed.teardown_task.done()
            if not in_flight and managed.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS:
                if managed.job is not None:
                    managed.job.close()
                self._active.discard(managed)
                failed.append(managed.pid)
                continue
            confirmed = await _run_teardown(managed)
            if confirmed and managed.cleaned:
                self._active.discard(managed)
            else:
                failed.append(managed.pid)
                if managed.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS:
                    if managed.job is not None:
                        managed.job.close()
                    self._active.discard(managed)
        if failed:
            pids = ", ".join(str(pid) for pid in failed)
            raise ProviderError(f"{CLI_LABELS[self.config.protocol]} 子进程树（PID {pids}）未能确认完全退出。")

    async def stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        protocol = self.config.protocol
        binary = CLI_BINARIES[protocol]
        label = CLI_LABELS[protocol]
        executable = self._which(binary)
        if not executable or not Path(executable).is_absolute():
            raise ProviderError(f"PATH 绝对目录中未找到官方命令 {binary}；{CLI_INSTALL_HINTS[protocol]}。")

        env = _cli_env(protocol)
        if protocol == "cli-claude":
            settings_paths = self._claude_settings_paths(env) if self._claude_settings_paths is not None else None
            _check_claude_settings_billing(env, settings_paths=settings_paths)

        if self._capability_checker is not None:
            await self._capability_checker(protocol, executable)
        elif self._argv_builder is build_cli_argv:
            await verify_cli_capabilities(
                protocol,
                executable,
                timeout=self.config.timeout,
                active_processes=self._active,
                probe_tasks=self._active_probes,
            )

        policy_file: Path | None = None
        try:
            if protocol == "cli-gemini" and self._argv_builder is build_cli_argv:
                policy_file = _write_temp_policy()
                argv = build_cli_argv(protocol, executable, self.config.model, policy_path=str(policy_file))
            else:
                argv = self._argv_builder(protocol, executable, self.config.model)

            nonce = secrets.token_hex(8) if (tools and allow_tools) else None
            allowed_names = frozenset(tool.name for tool in tools) if (tools and allow_tools) else frozenset()
            prompt_bytes = render_cli_prompt(
                messages,
                system,
                tools,
                allow_tools=allow_tools,
                nonce=nonce,
            ).encode("utf-8")

            spawn_kwargs: dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "env": env,
            }
            if os.name == "nt":
                spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                spawn_kwargs["start_new_session"] = True

            try:
                proc = await asyncio.create_subprocess_exec(*argv, **spawn_kwargs)
            except FileNotFoundError:
                raise ProviderError(f"无法执行官方命令 {binary}；{CLI_INSTALL_HINTS[protocol]}。") from None
            except OSError as exc:
                raise ProviderError(f"启动 {label} 失败：{redact(str(exc))}") from None

            job, job_err = _attach_windows_job(proc.pid)
            managed = _ManagedProcess(
                proc=proc,
                pid=proc.pid,
                job=job,
                pgid=None if os.name == "nt" else proc.pid,
            )
            if os.name == "nt" and job is None and job_err is not None:
                # Fail closed if Windows process-tree containment cannot be established while the process is alive.
                if _windows_pid_alive(proc.pid) or _windows_descendant_pids(proc.pid):
                    self._active.add(managed)
                    try:
                        await _run_teardown(managed)
                    finally:
                        if managed.cleaned:
                            self._active.discard(managed)
                    raise ProviderError(f"无法为 {label} 建立 Windows 进程树隔离，已中止执行。") from None

            self._active.add(managed)
            stdin_task = asyncio.create_task(_write_stdin(proc.stdin, prompt_bytes))
            stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
            parser = _CLIStreamParser(protocol)
            tag_filter = _ToolTagFilter(nonce, allowed_names)
            deadline = _Deadline(asyncio.get_running_loop(), self.config.timeout)
            stream_error: ProviderError | None = None
            stderr_bytes = b""
            returncode: int | None = None

            try:
                assert proc.stdout is not None
                # Never yield while an asyncio.timeout scope is active (PEP 789): scope only subprocess awaits,
                # and explicitly check `deadline.expired()` so buffered/ready I/O cannot bypass expiry.
                async with aclosing(_iter_lines_bounded(proc.stdout)) as line_iter:
                    while True:
                        if deadline.expired():
                            raise TimeoutError
                        try:
                            async with asyncio.timeout_at(deadline.at):
                                line = await anext(line_iter)
                        except StopAsyncIteration:
                            break
                        if deadline.expired():
                            raise TimeoutError
                        texts, thinkings = parser.feed_line(line)
                        for thought in thinkings:
                            yield Delta("thinking", thought)
                        for text in texts:
                            for event in tag_filter.feed(text):
                                yield event
                if deadline.expired():
                    raise TimeoutError
                for event in tag_filter.finish():
                    yield event
                if deadline.expired():
                    raise TimeoutError
                async with asyncio.timeout_at(deadline.at):
                    await stdin_task
                    stderr_bytes = await stderr_task
                    returncode = await proc.wait()
                if deadline.expired():
                    raise TimeoutError
            except TimeoutError:
                stream_error = ProviderError(
                    f"{label} 执行超时（>{self.config.timeout:g} 秒），请检查网络、登录状态或增大 timeout。"
                )
            finally:
                _signal_tree_sync(managed)
                for task in (stdin_task, stderr_task):
                    if not task.done():
                        task.cancel()
                try:
                    await asyncio.gather(stdin_task, stderr_task, return_exceptions=True)
                finally:
                    try:
                        await _run_teardown(managed)
                    finally:
                        if managed.cleaned:
                            self._active.discard(managed)

            if not managed.cleaned:
                if stream_error is not None:
                    raise ProviderError(f"{stream_error}（且子进程树未能确认完全退出，已保留待关闭重试。）")
                raise ProviderError(f"{label} 子进程树未能确认完全退出，已保留待关闭重试。")
            if stream_error is not None:
                raise stream_error
        finally:
            if policy_file is not None:
                with suppress(OSError):
                    policy_file.unlink(missing_ok=True)

        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if returncode != 0:
            detail = _safe_detail(parser.error_message or stderr_text or parser.non_json_tail)
            suffix = f"：{detail}" if detail else "。"
            raise ProviderError(f"{label} 执行失败（退出码 {returncode}）{suffix}")

        if parser.error_message:
            raise ProviderError(f"{label} 返回错误：{_safe_detail(parser.error_message)}")

        if not parser.saw_json_frame:
            detail = _safe_detail(stderr_text or parser.non_json_tail)
            suffix = f"（{detail}）" if detail else ""
            raise ProviderError(f"{label} 未输出有效的 JSON 流，请检查 CLI 版本及登录状态{suffix}。")

        if not parser.saw_terminal_success:
            raise ProviderError(f"{label} 流在完成信号前中断；未执行不完整的工具调用。")

        content: list[ContentBlock] = []
        if parser.thinking_parts:
            content.append(ContentBlock("thinking", "".join(parser.thinking_parts)))
        final_text = "".join(tag_filter.text_parts)
        if final_text or not tag_filter.calls:
            content.append(ContentBlock("text", final_text))

        calls = tuple(tag_filter.calls)
        stop_reason = "tool_calls" if calls else "completed"
        yield Completed(
            Message("assistant", tuple(content), protocol, (), calls),
            stop_reason,
            parser.usage,
        )
