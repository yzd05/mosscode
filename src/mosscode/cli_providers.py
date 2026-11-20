"""Compliant official CLI proxy backends (Claude Code, Codex, Gemini CLI).

Invokes allow-listed vendor CLIs via their documented headless JSON-streaming subprocess interfaces
so users can reuse existing official CLI subscriptions without reading or copying private OAuth tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
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

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
READ_CHUNK_BYTES = 64 * 1024
UNSUPPORTED_IMAGE = "当前模型不支持图片，已省略"


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


def detect_installed_clis(
    which: Callable[[str], str | None] = shutil.which,
) -> list[DetectedCLIBackend]:
    """Detect allow-listed official CLIs available on PATH without spawning any subprocess."""
    found: list[DetectedCLIBackend] = []
    for protocol in ("cli-claude", "cli-codex", "cli-gemini"):
        binary = CLI_BINARIES[protocol]
        executable = which(binary)
        if executable:
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


def build_cli_argv(protocol: str, executable: str, model: str) -> list[str]:
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
    return [
        executable,
        "-p",
        "",
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]


def _cli_env() -> dict[str, str]:
    """Preserve user CLI login context while scrubbing MossCode-internal flags."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MOSSCODE_")}
    env.setdefault("NO_COLOR", "1")
    return env


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
    """Streaming splitter that emits visible text deltas while extracting nonce-delimited tool calls."""

    def __init__(self, nonce: str | None, allowed_names: frozenset[str]) -> None:
        self._open = f"<mosscode_tool_call_{nonce}>" if nonce else ""
        self._close = f"</mosscode_tool_call_{nonce}>" if nonce else ""
        self._allowed = allowed_names
        self._in_tag = False
        self._buf = ""
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
        self._buf += chunk
        while self._buf:
            if self._in_tag:
                end_idx = self._buf.find(self._close)
                if end_idx == -1:
                    break
                raw_payload = self._buf[:end_idx]
                self._buf = self._buf[end_idx + len(self._close) :]
                self._in_tag = False
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
        # Drop any truncated/unclosed tool-call tag without executing it (fail-closed).
        self._buf = ""
        self._in_tag = False
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
        self.saw_json_frame = True
        if self.protocol == "cli-claude":
            return self._feed_claude(frame)
        if self.protocol == "cli-codex":
            return self._feed_codex(frame)
        return self._feed_gemini(frame)

    def _feed_claude(self, frame: dict[str, Any]) -> tuple[list[str], list[str]]:
        texts: list[str] = []
        thinkings: list[str] = []
        ftype = frame.get("type")

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
            subtype = str(frame.get("subtype") or "")
            if frame.get("is_error") is True or subtype.startswith("error"):
                self.error_message = _extract_error_text(frame) or "Claude Code CLI 执行失败。"
            elif not self.saw_any_text and isinstance(frame.get("result"), str) and frame["result"]:
                self.saw_any_text = True
                texts.append(frame["result"])
            return texts, thinkings

        if ftype == "error":
            self.error_message = _extract_error_text(frame) or "Claude Code CLI 返回错误。"
        return texts, thinkings

    def _feed_codex(self, frame: dict[str, Any]) -> tuple[list[str], list[str]]:
        texts: list[str] = []
        thinkings: list[str] = []
        ftype = frame.get("type")

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
            if isinstance(frame.get("usage"), dict):
                self.usage = _parse_codex_usage(frame["usage"])
            return texts, thinkings

        if ftype in {"turn.failed", "error"}:
            self.error_message = _extract_error_text(frame) or "OpenAI Codex CLI 返回错误。"
        return texts, thinkings

    def _feed_gemini(self, frame: dict[str, Any]) -> tuple[list[str], list[str]]:
        texts: list[str] = []
        thinkings: list[str] = []
        ftype = frame.get("type")

        if ftype == "message":
            role = str(frame.get("role") or "").lower()
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
                    # Sum per-model token counters only across complete fields; never fabricate missing counters as 0.
                    in_sum, out_sum, cache_sum = 0, 0, 0
                    in_complete, out_complete, cache_complete = True, True, True
                    saw_model = False
                    for entry in stats["models"].values():
                        tokens = entry.get("tokens") if isinstance(entry, dict) else None
                        if isinstance(tokens, dict):
                            p = _parse_gemini_usage(tokens)
                            saw_model = True
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
            status = str(frame.get("status") or "").lower()
            if status == "error" or frame.get("error"):
                self.error_message = _extract_error_text(frame) or "Google Gemini CLI 执行失败。"
            elif not self.saw_any_text and isinstance(frame.get("response"), str) and frame["response"]:
                self.saw_any_text = True
                texts.append(frame["response"])
            return texts, thinkings

        if ftype == "error":
            self.error_message = _extract_error_text(frame) or "Google Gemini CLI 返回错误。"
        return texts, thinkings


class _WinProcessJob:
    """Minimal L0 Windows kill-on-close Job Object to tear down CLI subprocess trees cleanly."""

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

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL

        self._api = api
        self._handle = api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimit()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        proc_handle = None
        try:
            if not api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            proc_handle = api.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
            if not proc_handle or not api.AssignProcessToJobObject(self._handle, proc_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            raise
        finally:
            if proc_handle:
                api.CloseHandle(proc_handle)

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


def _attach_windows_job(pid: int) -> _WinProcessJob | None:
    if os.name != "nt":
        return None
    try:
        return _WinProcessJob(pid)
    except OSError:
        return None


async def _terminate_tree(proc: asyncio.subprocess.Process, job: _WinProcessJob | None) -> None:
    """Terminate the subprocess and any children cleanly without raising on already-exited processes."""
    try:
        if proc.returncode is None:
            if os.name == "nt":
                if job is not None:
                    job.terminate()
                with suppress(ProcessLookupError, OSError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except TimeoutError:
                    with suppress(ProcessLookupError, OSError):
                        proc.kill()
                    with suppress(TimeoutError, OSError):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
            else:
                with suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(proc.pid, signal.SIGTERM)
                with suppress(ProcessLookupError, OSError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except TimeoutError:
                    with suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    with suppress(ProcessLookupError, OSError):
                        proc.kill()
                    with suppress(TimeoutError, OSError):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
    finally:
        if job is not None:
            job.close()


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


async def _iter_lines_bounded(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Read newline-delimited UTF-8 lines without triggering asyncio StreamReader's 64 KiB LimitOverrunError."""
    pending = bytearray()
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        pending.extend(chunk)
        if len(pending) > MAX_LINE_BYTES:
            raise ProviderError("官方 CLI 输出的单行 JSON 超过大小上限，已中止读取。")
        while True:
            idx = pending.find(b"\n")
            if idx == -1:
                break
            raw_line = bytes(pending[:idx])
            del pending[: idx + 1]
            yield raw_line.decode("utf-8", errors="replace")
    if pending:
        yield bytes(pending).decode("utf-8", errors="replace")


def _safe_detail(text: str) -> str:
    cleaned = " ".join(redact(text).split())
    if len(cleaned) > 300:
        cleaned = cleaned[:300] + "…"
    return cleaned


class CLIProxyProvider:
    """Official CLI execution adapter conforming to MossCode's L1 Provider protocol."""

    def __init__(
        self,
        config: Any,
        *,
        which: Callable[[str], str | None] = shutil.which,
        argv_builder: Callable[[str, str, str], list[str]] = build_cli_argv,
    ) -> None:
        if config.protocol not in CLI_PROTOCOLS:
            raise ProviderError(f"不支持的 CLI 代理协议：{config.protocol}。")
        if not is_valid_cli_model(config.model):
            raise ProviderError(CLI_MODEL_ERROR)
        self.config = config
        self.thinking: str | None = None
        self._which = which
        self._argv_builder = argv_builder
        self._active: dict[asyncio.subprocess.Process, _WinProcessJob | None] = {}

    async def close(self) -> None:
        active = list(self._active.items())
        self._active.clear()
        for proc, job in active:
            await _terminate_tree(proc, job)

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
        if not executable:
            raise ProviderError(f"PATH 中未找到官方命令 {binary}；{CLI_INSTALL_HINTS[protocol]}。")

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
            "env": _cli_env(),
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

        job = _attach_windows_job(proc.pid)
        self._active[proc] = job

        stdin_task = asyncio.create_task(_write_stdin(proc.stdin, prompt_bytes))
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
        parser = _CLIStreamParser(protocol)
        tag_filter = _ToolTagFilter(nonce, allowed_names)

        try:
            async with asyncio.timeout(self.config.timeout):
                assert proc.stdout is not None
                async for line in _iter_lines_bounded(proc.stdout):
                    texts, thinkings = parser.feed_line(line)
                    for thought in thinkings:
                        yield Delta("thinking", thought)
                    for text in texts:
                        for event in tag_filter.feed(text):
                            yield event
                for event in tag_filter.finish():
                    yield event
                await stdin_task
                stderr_bytes = await stderr_task
                returncode = await proc.wait()
        except TimeoutError:
            raise ProviderError(
                f"{label} 执行超时（>{self.config.timeout:g} 秒），请检查网络、登录状态或增大 timeout。"
            ) from None
        finally:
            self._active.pop(proc, None)
            if proc.returncode is None:
                # Signal teardown synchronously before any await so even immediate loop cancellation stops the tree.
                if os.name == "nt":
                    if job is not None:
                        job.terminate()
                    with suppress(ProcessLookupError, OSError):
                        proc.terminate()
                else:
                    with suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(proc.pid, signal.SIGTERM)
                    with suppress(ProcessLookupError, OSError):
                        proc.terminate()
            for task in (stdin_task, stderr_task):
                if not task.done():
                    task.cancel()
            with suppress(BaseException):
                await asyncio.shield(asyncio.gather(stdin_task, stderr_task, return_exceptions=True))
            with suppress(BaseException):
                await asyncio.shield(_terminate_tree(proc, job))

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
