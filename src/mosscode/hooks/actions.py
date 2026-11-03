"""Run one hook action and report what happened; the engine decides what it means."""

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import TEMPLATE, Action

STREAM_HALF = 16 * 1024  # Head and tail of stdout and stderr: 32 KB each.
BODY_LIMIT = 64 * 1024
DETAIL_LIMIT = 500
FEEDBACK_LIMIT = 8000
REJECT_LIMIT = 2000


@dataclass(frozen=True)
class ActionResult:
    outcome: str  # ok, failed, rejected, timeout, skipped
    detail: str = ""  # Failure reason or the end of the output, for the log and the notice.
    output: str = ""  # Text for the model: a prompt message or command feedback.
    exit_code: int | None = None
    status: int | None = None
    reason: str = ""  # Dynamic rejection (exit code 2).


def short(text: str, limit: int = DETAIL_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def render(text: str, fields) -> str:
    """{{field}} becomes the field's value; only ever in text for the model or a JSON string."""
    return TEMPLATE.sub(lambda match: str(fields.get(match[1], "")), text)


def render_json(value, fields):
    if isinstance(value, str):
        return render(value, fields)
    if isinstance(value, dict):
        return {key: render_json(item, fields) for key, item in value.items()}
    if isinstance(value, list):
        return [render_json(item, fields) for item in value]
    return value


def public_url(url: str) -> str:
    """Never log query strings (they often carry tokens)."""
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _executable(first: str) -> str | None:
    if os.sep in first or "/" in first:
        return first if Path(first).is_file() else None
    return shutil.which(first)


def argv(command: tuple[str, ...], values: dict[str, str]) -> list[str]:
    """Each ${NAME} becomes one whole argument's text; nothing is re-split or interpreted."""
    parts = []
    for part in command:
        for name, value in values.items():
            part = part.replace("${" + name + "}", value)
        parts.append(part)
    return parts


async def run_command(
    action: Action, *, root: Path, shell, env: dict[str, str], values: dict[str, str], stdin: bytes, rejecting: bool
) -> ActionResult:
    from ..tools.base import ToolFailure
    from ..tools.process import run_process

    if isinstance(action.command, str):
        if shell is None:
            return ActionResult("failed", "没有可用的 shell，无法执行字符串命令；可改用 argv 数组。")
        args = shell.args(action.command)
        environment = {**(shell.env(action.command) or os.environ), **env}
    else:
        args = argv(action.command, values)
        found = _executable(args[0])
        if found is None:
            return ActionResult("failed", f"找不到命令：{args[0]}")
        args[0] = found
        environment = {**os.environ, **env}
    try:
        details = await asyncio.wait_for(
            run_process(args, root, check=False, limit=STREAM_HALF, tail=STREAM_HALF, env=environment, input=stdin),
            timeout=action.timeout,
        )
    except TimeoutError:
        return ActionResult("timeout", f"超过 {action.timeout:g} 秒未结束，已终止。")
    except ToolFailure as exc:
        return ActionResult("failed", str(exc))
    except OSError as exc:
        return ActionResult("failed", f"无法启动命令：{exc.strerror or type(exc).__name__}")
    code, stdout, stderr = details["exit_code"], details["stdout"], details["stderr"]
    if code == 0:
        output = stdout.strip()[:FEEDBACK_LIMIT] if action.feedback else ""
        return ActionResult("ok", short(stdout), output, exit_code=0)
    if code == 2 and rejecting:
        reason = (stderr.strip() or stdout.strip() or "未说明原因")[:REJECT_LIMIT]
        return ActionResult("rejected", short(reason), exit_code=2, reason=reason)
    tail = short(stderr) or short(stdout)
    return ActionResult("failed", f"退出码为 {code}" + (f"：{tail}" if tail else ""), exit_code=code)


async def run_http(action: Action, *, fields, context: dict) -> ActionResult:
    import httpx

    body = None
    if action.has_json:
        body = render_json(action.json_body, fields)
    elif action.method in {"POST", "PUT", "PATCH"}:
        body = context
    options = {"json": body} if body is not None else {}

    async def send():
        async with httpx.AsyncClient(follow_redirects=False, timeout=action.timeout) as client:
            async with client.stream(action.method, action.url, headers=dict(action.headers), **options) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) >= BODY_LIMIT:
                        break
                return response.status_code, bytes(data[:BODY_LIMIT]).decode("utf-8", errors="replace")

    try:
        status, text = await asyncio.wait_for(send(), timeout=action.timeout)
    except (TimeoutError, httpx.TimeoutException):
        return ActionResult("timeout", f"{public_url(action.url)} 超过 {action.timeout:g} 秒未响应。")
    except httpx.HTTPError as exc:
        return ActionResult("failed", f"{public_url(action.url)} 请求失败（{type(exc).__name__}）。")
    if 200 <= status < 300:
        return ActionResult("ok", text[:DETAIL_LIMIT], status=status)
    return ActionResult(
        "failed",
        f"{public_url(action.url)} 返回 HTTP {status}" + (f"：{text[:200]}" if text.strip() else ""),
        status=status,
    )


def stdin_json(context: dict) -> bytes:
    return json.dumps(context, ensure_ascii=False, default=str).encode("utf-8")


PYTHON = sys.executable
