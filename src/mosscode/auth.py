"""OAuth / browser authentication providers (OpenRouter PKCE, GitHub Copilot Device Flow) and token lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import math
import os
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import ConfigError
from .credentials import keyring_problem, remember
from .model_discovery import save_provider_entry
from .redaction import redact

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

# Public VS Code / Copilot CLI OAuth App client ID used by open-source Copilot integrations.
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
COPILOT_DEVICE_CODE_URL = "https://github.com/login/device/code"
COPILOT_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_SESSION_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_DEFAULT_MODEL = "gpt-4.1"
COPILOT_REFRESH_MARGIN_SECONDS = 120.0
COPILOT_HEADERS: tuple[tuple[str, str], ...] = (
    ("Copilot-Integration-Id", "vscode-chat"),
    ("Editor-Version", "vscode/1.99.0"),
    ("Editor-Plugin-Version", "copilot-chat/0.26.7"),
    ("User-Agent", "GitHubCopilotChat/0.26.7"),
    ("openai-intent", "conversation-panel"),
    ("x-github-api-version", "2025-04-01"),
)

AUTH_PROVIDERS: dict[str, str] = {
    "openrouter": "OpenRouter（浏览器 OAuth PKCE 一键登录）",
    "copilot": "GitHub Copilot（浏览器设备码登录）",
}


class AuthError(Exception):
    """User-facing OAuth/authentication failure with secrets stripped."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class OAuthLoginResult:
    """Normalized outcome of an interactive OAuth/device login flow."""

    provider_key: str
    preset: str
    credential: str
    auth_type: str
    default_model: str
    base_url: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    expires_at: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class DeviceCodeChallenge:
    """RFC 8628 OAuth 2.0 Device Authorization challenge."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = 900
    interval: int = 5


@runtime_checkable
class AuthProvider(Protocol):
    """Pluggable interactive browser/device authentication flow."""

    @property
    def key(self) -> str: ...

    @property
    def label(self) -> str: ...

    async def login(
        self,
        *,
        on_notice: Callable[[str], None] | None = None,
        open_browser: Callable[[str], Any] | None = None,
        timeout: float = 180.0,
    ) -> OAuthLoginResult: ...


def generate_pkce_pair() -> tuple[str, str]:
    """Generate an RFC 7636 PKCE (code_verifier, S256 code_challenge) pair."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_openrouter_auth_url(callback_url: str, code_challenge: str) -> str:
    """Build the OpenRouter OAuth PKCE authorization URL."""
    query = urlencode(
        {
            "callback_url": callback_url,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{OPENROUTER_AUTH_URL}?{query}"


def _allowed_https_host(url: str, allowed_hosts: set[str], *, allow_subdomains: bool = False) -> bool:
    """Fail-closed HTTPS host whitelist validation to prevent phishing redirects or SSRF."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in allowed_hosts:
        return True
    if allow_subdomains:
        return any(host.endswith(f".{suffix}") for suffix in allowed_hosts)
    return False


def _safe_error_text(exc: Exception, *secrets_to_hide: str | None) -> str:
    text = redact(f"{type(exc).__name__}: {exc}")
    for value in secrets_to_hide:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def _callback_html(title: str, body: str, *, ok: bool) -> bytes:
    accent = "#16a34a" if ok else "#dc2626"
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    doc = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px;}"
        ".card{max-width:460px;width:100%;background:#1e293b;border:1px solid #334155;"
        "border-radius:14px;padding:28px 30px;box-shadow:0 20px 50px rgba(0,0,0,.35);}"
        f"h1{{margin:0 0 12px;font-size:20px;color:{accent};}}"
        "p{margin:0;line-height:1.6;color:#cbd5e1;font-size:14px;}"
        "</style></head><body>"
        f"<main class='card'><h1>{safe_title}</h1><p>{safe_body}</p></main>"
        "</body></html>"
    )
    return doc.encode("utf-8")


class LoopbackCallbackServer:
    """Single-use loopback HTTP callback receiver bound strictly to 127.0.0.1 with an unguessable path."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host != "127.0.0.1":
            raise AuthError("OAuth 回调监听地址必须为 127.0.0.1。")
        self._nonce = secrets.token_urlsafe(18)
        self.callback_path = f"/callback/{self._nonce}"
        self._lock = threading.Lock()
        self._code: str | None = None
        self._error: str | None = None
        self._done_thread_event = threading.Event()
        self._async_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self._started = False
        self._closed = False
        self._server = self._build_server(host, port)
        self.port = int(self._server.server_address[1])
        self._allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.callback_url = f"http://127.0.0.1:{self.port}{self.callback_path}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="mosscode-oauth-callback",
            daemon=True,
        )

    def _notify_waiters_locked(self) -> None:
        self._done_thread_event.set()
        for loop, event in self._async_waiters:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)
        self._async_waiters.clear()

    def _build_server(self, host: str, port: int) -> ThreadingHTTPServer:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_html(self, status: HTTPStatus, payload: bytes) -> None:
                self.send_response(int(status))
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                host_hdr = (self.headers.get("Host") or "").strip().lower()
                if host_hdr not in parent._allowed_hosts:
                    self._send_html(
                        HTTPStatus.FORBIDDEN,
                        _callback_html("非法请求来源", "回调仅接受本机回环 Host。", ok=False),
                    )
                    return
                parsed = urlsplit(self.path)
                if not hmac.compare_digest(parsed.path, parent.callback_path):
                    self._send_html(
                        HTTPStatus.NOT_FOUND,
                        _callback_html("无效回调路径", "该本地回调地址无效或已过期。", ok=False),
                    )
                    return
                params = parse_qs(parsed.query, keep_blank_values=False)
                code = (params.get("code") or [""])[0].strip()
                err = (params.get("error") or [""])[0].strip()
                with parent._lock:
                    if parent._done_thread_event.is_set():
                        self._send_html(
                            HTTPStatus.GONE,
                            _callback_html("回调已完成", "本次授权回调已被处理，可直接关闭此标签页。", ok=True),
                        )
                        return
                    if err:
                        parent._error = f"授权被拒绝或取消（{err}）。"
                        parent._notify_waiters_locked()
                        self._send_html(
                            HTTPStatus.BAD_REQUEST,
                            _callback_html("授权未完成", "OpenRouter 授权已取消，可关闭此标签页并返回终端。", ok=False),
                        )
                        return
                    if not code:
                        self._send_html(
                            HTTPStatus.BAD_REQUEST,
                            _callback_html("缺少授权码", "回调参数缺少 code，请重新发起登录。", ok=False),
                        )
                        return
                    parent._code = code
                    parent._notify_waiters_locked()
                self._send_html(
                    HTTPStatus.OK,
                    _callback_html("授权成功", "已收到 OpenRouter 授权码，请关闭此标签页并返回 MossCode。", ok=True),
                )

        class _LoopbackHTTPServer(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        return _LoopbackHTTPServer((host, port), _Handler)

    def start(self) -> LoopbackCallbackServer:
        with self._lock:
            if self._started or self._closed:
                return self
            try:
                self._thread.start()
            except BaseException:
                self._closed = True
                self._notify_waiters_locked()
                with suppress(Exception):
                    self._server.server_close()
                raise
            self._started = True
        return self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            was_started = self._started
            self._notify_waiters_locked()
        if was_started:
            with suppress(Exception):
                self._server.shutdown()
        with suppress(Exception):
            self._server.server_close()
        if was_started and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> LoopbackCallbackServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    async def wait_for_code(self, timeout: float = 180.0) -> str:
        """Await the single-use authorization code without polling."""
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        with self._lock:
            if self._done_thread_event.is_set():
                ready.set()
            else:
                self._async_waiters.append((loop, ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
        except TimeoutError:
            raise AuthError("等待浏览器授权回调超时，请重试。") from None
        with self._lock:
            if self._error:
                raise AuthError(self._error)
            if not self._code:
                raise AuthError("浏览器授权已取消。")
            return self._code


async def exchange_openrouter_code(
    code: str,
    code_verifier: str,
    callback_url: str | None = None,
    *,
    timeout: float = 15.0,
    transport: Any = None,
) -> str:
    """Exchange an OpenRouter PKCE authorization code for a user-owned API key via POST /api/v1/auth/keys."""
    import httpx

    if not code.strip() or not code_verifier.strip():
        raise AuthError("授权码或 PKCE 校验码为空。")
    payload = {
        "code": code.strip(),
        "code_verifier": code_verifier,
        "code_challenge_method": "S256",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
            resp = await client.post(
                OPENROUTER_EXCHANGE_URL,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except httpx.TimeoutException:
        raise AuthError("向 OpenRouter 换取 API 密钥超时，请检查网络后重试。") from None
    except Exception as exc:
        raise AuthError(f"向 OpenRouter 换取 API 密钥失败：{_safe_error_text(exc, code, code_verifier)}") from None

    if 300 <= resp.status_code < 400:
        raise AuthError(f"OpenRouter 密钥交换端点返回异常重定向（HTTP {resp.status_code}），出于安全考虑已中止。")
    if resp.status_code != 200:
        raise AuthError(f"OpenRouter 换取 API 密钥失败（HTTP {resp.status_code}），请重新发起登录。")
    try:
        data = resp.json()
    except ValueError:
        raise AuthError("OpenRouter 返回了无法解析的响应格式。") from None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise AuthError("OpenRouter 响应中缺少有效的 API 密钥。")
    key = key.strip()
    remember(key)
    return key


class OpenRouterPKCEAuth:
    """Browser one-click OpenRouter login via OAuth 2.0 PKCE + ephemeral loopback callback."""

    key = "openrouter"
    label = AUTH_PROVIDERS["openrouter"]

    def __init__(self, *, transport: Any = None) -> None:
        self._transport = transport

    async def login(
        self,
        *,
        on_notice: Callable[[str], None] | None = None,
        open_browser: Callable[[str], Any] | None = None,
        timeout: float = 180.0,
    ) -> OAuthLoginResult:
        verifier, challenge = generate_pkce_pair()
        with LoopbackCallbackServer() as callback:
            auth_url = build_openrouter_auth_url(callback.callback_url, challenge)
            if on_notice is not None:
                on_notice(f"请在浏览器中完成 OpenRouter 授权：\n{auth_url}")
            if open_browser is not None:
                with suppress(Exception):
                    open_browser(auth_url)
            code = await callback.wait_for_code(timeout=timeout)
            api_key = await exchange_openrouter_code(
                code,
                verifier,
                callback.callback_url,
                transport=self._transport,
            )
        return OAuthLoginResult(
            provider_key="openrouter",
            preset="openrouter",
            credential=api_key,
            auth_type="api_key",
            default_model=OPENROUTER_DEFAULT_MODEL,
            base_url="https://openrouter.ai/api/v1",
            detail="已通过 OpenRouter OAuth PKCE 获取专属 API 密钥。",
        )


async def start_copilot_device_flow(
    *,
    client_id: str = COPILOT_CLIENT_ID,
    timeout: float = 15.0,
    transport: Any = None,
) -> DeviceCodeChallenge:
    """Request an RFC 8628 GitHub Device Authorization challenge for Copilot."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
            resp = await client.post(
                COPILOT_DEVICE_CODE_URL,
                json={"client_id": client_id, "scope": "read:user"},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except httpx.TimeoutException:
        raise AuthError("请求 GitHub 设备码超时，请检查网络。") from None
    except Exception as exc:
        raise AuthError(f"请求 GitHub 设备码失败：{_safe_error_text(exc)}") from None

    if 300 <= resp.status_code < 400:
        raise AuthError(f"GitHub 设备码端点返回异常重定向（HTTP {resp.status_code}），出于安全考虑已中止。")
    if resp.status_code != 200:
        raise AuthError(f"请求 GitHub 设备码失败（HTTP {resp.status_code}）。")
    try:
        data = resp.json()
    except ValueError:
        raise AuthError("GitHub 设备码响应不是合法 JSON。") from None
    if not isinstance(data, dict):
        raise AuthError("GitHub 设备码响应格式无效。")

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri")
    expires_in = data.get("expires_in", 900)
    interval = data.get("interval", 5)

    if (
        not isinstance(device_code, str)
        or not device_code.strip()
        or not isinstance(user_code, str)
        or not user_code.strip()
        or not isinstance(verification_uri, str)
        or not _allowed_https_host(verification_uri, {"github.com"})
    ):
        raise AuthError("GitHub 返回了无效或不受信任的设备验证信息。")

    safe_expires = (
        int(expires_in)
        if isinstance(expires_in, (int, float))
        and not isinstance(expires_in, bool)
        and math.isfinite(expires_in)
        and expires_in > 0
        else 900
    )
    safe_interval = (
        max(1, int(interval))
        if isinstance(interval, (int, float))
        and not isinstance(interval, bool)
        and math.isfinite(interval)
        and interval > 0
        else 5
    )
    return DeviceCodeChallenge(
        device_code=device_code.strip(),
        user_code=user_code.strip(),
        verification_uri=verification_uri.strip(),
        expires_in=safe_expires,
        interval=safe_interval,
    )


async def poll_copilot_device_token_once(
    device_code: str,
    *,
    client_id: str = COPILOT_CLIENT_ID,
    timeout: float = 15.0,
    transport: Any = None,
) -> tuple[str, str | None]:
    """Perform one RFC 8628 poll attempt.

    Returns `(status, token_or_detail)` where status is one of:
    `"ok"`, `"pending"`, `"slow_down"`, `"timeout"`, `"denied"`, `"expired"`.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
            resp = await client.post(
                COPILOT_ACCESS_TOKEN_URL,
                json={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except httpx.TimeoutException:
        # RFC 8628 §3.5: transient connection timeout slows polling rather than aborting authorization.
        return "timeout", None
    except Exception as exc:
        raise AuthError(f"轮询 GitHub 授权状态失败：{_safe_error_text(exc, device_code)}") from None

    if 300 <= resp.status_code < 400:
        raise AuthError(f"GitHub 令牌端点返回异常重定向（HTTP {resp.status_code}），出于安全考虑已中止。")
    if resp.status_code != 200:
        raise AuthError(f"GitHub 令牌端点返回 HTTP {resp.status_code}。")
    try:
        data = resp.json()
    except ValueError:
        raise AuthError("GitHub 令牌响应不是合法 JSON。") from None
    if not isinstance(data, dict):
        raise AuthError("GitHub 令牌响应格式无效。")

    access_token = data.get("access_token")
    if isinstance(access_token, str) and access_token.strip():
        token = access_token.strip()
        remember(token)
        return "ok", token

    error = str(data.get("error") or "").strip()
    if error == "authorization_pending":
        return "pending", None
    if error == "slow_down":
        return "slow_down", None
    if error == "access_denied":
        return "denied", "你已取消 GitHub Copilot 授权。"
    if error == "expired_token":
        return "expired", "设备码已过期，请重新发起登录。"
    raise AuthError(f"GitHub 授权失败：{error or '未知错误'}。")


async def poll_copilot_device_token(
    challenge: DeviceCodeChallenge,
    *,
    client_id: str = COPILOT_CLIENT_ID,
    timeout: float = 180.0,
    transport: Any = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> str:
    """Poll GitHub until the user authorizes the device code or the shared deadline elapses."""
    deadline = time.monotonic() + min(float(challenge.expires_in), float(timeout))
    interval = float(challenge.interval)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or remaining < interval:
            raise AuthError("等待 GitHub 设备码授权超时，请重试。")
        await sleep(interval)
        remaining_after = deadline - time.monotonic()
        if remaining_after <= 0:
            raise AuthError("等待 GitHub 设备码授权超时，请重试。")
        try:
            status, value = await asyncio.wait_for(
                poll_copilot_device_token_once(
                    challenge.device_code,
                    client_id=client_id,
                    timeout=min(15.0, remaining_after),
                    transport=transport,
                ),
                timeout=remaining_after,
            )
        except TimeoutError:
            raise AuthError("等待 GitHub 设备码授权超时，请重试。") from None
        if status == "ok" and value:
            return value
        if status in {"slow_down", "timeout"}:
            interval += 5.0
            continue
        if status == "pending":
            continue
        raise AuthError(value or "GitHub 授权未完成。")


def _parse_copilot_session_response(
    status_code: int, payload: Any, *, now: float | None = None
) -> tuple[str, float, str]:
    if 300 <= status_code < 400:
        raise AuthError(f"GitHub Copilot 令牌交换返回异常重定向（HTTP {status_code}），出于安全考虑已中止。")
    if status_code in {401, 403, 404}:
        raise AuthError(
            f"GitHub Copilot 令牌交换失败（HTTP {status_code}）：请确认当前 GitHub 账号已开通 GitHub Copilot 订阅，或重新运行 moss login copilot。"
        )
    if status_code == 429 or status_code >= 500:
        raise AuthError(f"GitHub Copilot 令牌交换暂时不可用（HTTP {status_code}）。", transient=True)
    if status_code != 200:
        raise AuthError(f"GitHub Copilot 令牌交换失败（HTTP {status_code}）。")
    if not isinstance(payload, dict):
        raise AuthError("GitHub Copilot 令牌响应格式无效。")
    session_token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(session_token, str) or not session_token.strip():
        raise AuthError("GitHub Copilot 响应中缺少会话令牌。")
    current_time = time.time() if now is None else now
    if (
        not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or not math.isfinite(expires_at)
        or expires_at <= current_time
    ):
        raise AuthError("GitHub Copilot 返回的会话令牌过期时间无效或已过期，请检查系统时间或重新登录。")
    endpoint = COPILOT_API_BASE
    endpoints = payload.get("endpoints")
    if isinstance(endpoints, dict):
        api_url = endpoints.get("api")
        if isinstance(api_url, str) and api_url.strip():
            candidate = api_url.strip().rstrip("/")
            if _allowed_https_host(candidate, {"githubcopilot.com"}, allow_subdomains=True):
                endpoint = candidate
            else:
                raise AuthError("GitHub Copilot 返回了不受信任的 API 地址，出于安全考虑已拒绝。")
    token = session_token.strip()
    remember(token)
    return token, float(expires_at), endpoint


def exchange_copilot_session_token(
    github_token: str,
    *,
    timeout: float = 15.0,
    transport: Any = None,
) -> tuple[str, float, str]:
    """Synchronously exchange a GitHub OAuth token (`ghu_...`) for a short-lived Copilot session token."""
    import httpx

    remember(github_token)
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=False) as client:
            resp = client.get(
                COPILOT_SESSION_TOKEN_URL,
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/json",
                    "User-Agent": "GitHubCopilotChat/0.26.7",
                },
            )
    except httpx.TimeoutException:
        raise AuthError("获取 GitHub Copilot 会话令牌超时，请检查网络。", transient=True) from None
    except httpx.TransportError as exc:
        raise AuthError(
            f"获取 GitHub Copilot 会话令牌连接失败：{_safe_error_text(exc, github_token)}",
            transient=True,
        ) from None
    except Exception as exc:
        raise AuthError(f"获取 GitHub Copilot 会话令牌失败：{_safe_error_text(exc, github_token)}") from None
    try:
        payload = resp.json() if resp.status_code == 200 else None
    except ValueError:
        raise AuthError("GitHub Copilot 会话令牌响应不是合法 JSON。") from None
    return _parse_copilot_session_response(resp.status_code, payload)


async def exchange_copilot_session_token_async(
    github_token: str,
    *,
    timeout: float = 15.0,
    transport: Any = None,
) -> tuple[str, float, str]:
    """Asynchronously exchange a GitHub OAuth token (`ghu_...`) for a short-lived Copilot session token."""
    import httpx

    remember(github_token)
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
            resp = await client.get(
                COPILOT_SESSION_TOKEN_URL,
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/json",
                    "User-Agent": "GitHubCopilotChat/0.26.7",
                },
            )
    except httpx.TimeoutException:
        raise AuthError("获取 GitHub Copilot 会话令牌超时，请检查网络。", transient=True) from None
    except httpx.TransportError as exc:
        raise AuthError(
            f"获取 GitHub Copilot 会话令牌连接失败：{_safe_error_text(exc, github_token)}",
            transient=True,
        ) from None
    except Exception as exc:
        raise AuthError(f"获取 GitHub Copilot 会话令牌失败：{_safe_error_text(exc, github_token)}") from None
    try:
        payload = resp.json() if resp.status_code == 200 else None
    except ValueError:
        raise AuthError("GitHub Copilot 会话令牌响应不是合法 JSON。") from None
    return _parse_copilot_session_response(resp.status_code, payload)


class CopilotTokenManager:
    """Thread-safe in-memory cache and auto-refresher for short-lived GitHub Copilot session tokens."""

    def __init__(self, *, refresh_margin: float = COPILOT_REFRESH_MARGIN_SECONDS) -> None:
        self._lock = threading.Lock()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._cache: dict[str, tuple[str, float, str]] = {}
        self._versions: dict[str, int] = {}
        self._global_version = 0
        self._refresh_margin = refresh_margin

    @staticmethod
    def _cache_key(github_token: str) -> str:
        # Never store the raw GitHub OAuth token as a dict key so tracebacks/reprs cannot leak it.
        return hashlib.sha256(github_token.encode("utf-8")).hexdigest()

    def _key_version_locked(self, key: str) -> tuple[int, int]:
        return (self._global_version, self._versions.get(key, 0))

    def get_session_token(
        self,
        github_token: str,
        *,
        now: Callable[[], float] = time.time,
        exchange: Callable[[str], tuple[str, float, str]] = exchange_copilot_session_token,
    ) -> str:
        """Return a valid cached Copilot session token, refreshing before expiry with per-key serialization."""
        key = self._cache_key(github_token)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now() < cached[1] - self._refresh_margin:
                return cached[0]
            key_lock = self._refresh_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                observed = self._cache.get(key)
                if observed is not None and now() < observed[1] - self._refresh_margin:
                    return observed[0]
                observed_version = self._key_version_locked(key)
            try:
                token, expires_at, endpoint = exchange(github_token)
            except AuthError as exc:
                if exc.transient:
                    with self._lock:
                        current = self._cache.get(key)
                    # Recheck live cache under lock so a token removed by invalidate() is never resurrected.
                    if current is not None and now() < current[1]:
                        return current[0]
                else:
                    with self._lock:
                        # Evict only if neither prime() nor invalidate() mutated this key during exchange.
                        if (
                            self._key_version_locked(key) == observed_version
                            and observed is not None
                            and self._cache.get(key) == observed
                        ):
                            self._cache.pop(key, None)
                raise
            with self._lock:
                if self._key_version_locked(key) != observed_version:
                    # A concurrent prime() or invalidate() mutated the cache while exchange() ran.
                    current = self._cache.get(key)
                    if current is not None and now() < current[1]:
                        return current[0]
                    raise AuthError("GitHub Copilot 会话缓存已失效，请重新获取。", transient=True)
                self._cache[key] = (token, expires_at, endpoint)
            return token

    def prime(self, github_token: str, session_token: str, expires_at: float, endpoint: str) -> None:
        """Store a freshly exchanged session token under the lock."""
        key = self._cache_key(github_token)
        with self._lock:
            self._versions[key] = self._versions.get(key, 0) + 1
            self._cache[key] = (session_token, expires_at, endpoint)

    def invalidate(self, github_token: str | None = None) -> None:
        with self._lock:
            if github_token is None:
                self._global_version += 1
                self._cache.clear()
            else:
                key = self._cache_key(github_token)
                self._versions[key] = self._versions.get(key, 0) + 1
                self._cache.pop(key, None)


copilot_token_manager = CopilotTokenManager()


class GitHubCopilotAuth:
    """Browser GitHub Copilot authentication via OAuth 2.0 Device Flow (RFC 8628)."""

    key = "copilot"
    label = AUTH_PROVIDERS["copilot"]

    def __init__(self, *, transport: Any = None, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self._transport = transport
        self._sleep = sleep

    async def login(
        self,
        *,
        on_notice: Callable[[str], None] | None = None,
        open_browser: Callable[[str], Any] | None = None,
        timeout: float = 180.0,
    ) -> OAuthLoginResult:
        challenge = await start_copilot_device_flow(transport=self._transport)
        if on_notice is not None:
            on_notice(f"请在浏览器中打开 {challenge.verification_uri}\n并输入一次性验证码：{challenge.user_code}")
        if open_browser is not None:
            with suppress(Exception):
                open_browser(challenge.verification_uri)
        github_token = await poll_copilot_device_token(
            challenge,
            timeout=timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        session_token, expires_at, endpoint = await exchange_copilot_session_token_async(
            github_token,
            transport=self._transport,
        )
        # Prime the in-memory session token cache so the immediate next request reuses it.
        copilot_token_manager.prime(github_token, session_token, expires_at, endpoint)
        return OAuthLoginResult(
            provider_key="copilot",
            preset="copilot",
            credential=github_token,
            auth_type="github-copilot",
            default_model=COPILOT_DEFAULT_MODEL,
            base_url=endpoint,
            headers=COPILOT_HEADERS,
            expires_at=expires_at,
            detail="已通过 GitHub Copilot 设备码授权并验证会话令牌。",
        )


def persist_oauth_result(
    result: OAuthLoginResult,
    *,
    home: Path | None = None,
    name: str | None = None,
    model: str | None = None,
    set_default: bool = False,
    allow_plaintext: bool = False,
    vault: Any = None,
    config_path: Path | None = None,
    extra_taken_names: set[str] | frozenset[str] | None = None,
) -> tuple[Path, str]:
    """Persist an OAuth login result into user config and system keyring (fail-closed without keyring)."""
    remember(result.credential)
    provider_name = (name or result.provider_key).strip()
    if not provider_name:
        raise AuthError("Provider 名称不能为空。")
    chosen_model = (model or result.default_model).strip()
    if not chosen_model:
        raise AuthError("模型名不能为空。")

    env_plaintext = os.environ.get("MOSSCODE_ALLOW_PLAINTEXT_OAUTH", "").strip() == "1"
    problem = (
        vault.problem() if (vault is not None and callable(getattr(vault, "problem", None))) else keyring_problem()
    )
    entry: dict[str, Any] = {
        "name": provider_name,
        "preset": result.preset,
        "model": chosen_model,
    }
    if result.base_url and result.preset == "copilot" and result.base_url != COPILOT_API_BASE:
        entry["base_url"] = result.base_url

    pending_key: str | None = None
    if problem is None:
        entry["api_key_keyring"] = provider_name
        pending_key = result.credential
    elif allow_plaintext or env_plaintext:
        entry["api_key"] = result.credential
    else:
        raise AuthError(
            f"系统凭据管理器不可用（{problem}），默认拒绝将 OAuth 凭据明文写入配置文件。"
            "请修复系统凭据管理器，或显式使用 --plaintext / 设置环境变量 MOSSCODE_ALLOW_PLAINTEXT_OAUTH=1。"
        )

    try:
        return save_provider_entry(
            home,
            entry,
            pending_key=pending_key,
            set_default=set_default,
            vault=vault,
            config_path=config_path,
            extra_taken_names=extra_taken_names,
        )
    except (ConfigError, OSError) as exc:
        raise AuthError(f"保存 OAuth 配置失败：{exc}") from None
