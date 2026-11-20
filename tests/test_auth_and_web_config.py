"""Tests for OAuth PKCE (OpenRouter), GitHub Copilot Device Flow, token refresh, `moss login`, and `moss config --web`."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import re
import subprocess
import sys
import urllib.parse

import httpx
import pytest
import yaml

from mosscode.auth import (
    COPILOT_API_BASE,
    COPILOT_HEADERS,
    AuthError,
    CopilotTokenManager,
    DeviceCodeChallenge,
    GitHubCopilotAuth,
    LoopbackCallbackServer,
    OAuthLoginResult,
    OpenRouterPKCEAuth,
    build_openrouter_auth_url,
    copilot_token_manager,
    exchange_copilot_session_token,
    exchange_openrouter_code,
    generate_pkce_pair,
    persist_oauth_result,
    poll_copilot_device_token,
    start_copilot_device_flow,
)
from mosscode.config import load_settings
from mosscode.config_web import ConfigWebServer
from mosscode.conversation import Conversation
from mosscode.model_discovery import probe_provider
from mosscode.providers import ChatCompletionsProvider
from mosscode.tui import MossCodeApp
from test_conversation import FakeProvider


def _http_req(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    host_header: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        hdrs = dict(headers or {})
        if host_header is not None:
            hdrs["Host"] = host_header
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, data
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OpenRouter OAuth 2.0 PKCE
# ---------------------------------------------------------------------------


def test_pkce_pair_and_auth_url_follow_rfc7636():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in challenge

    url = build_openrouter_auth_url("http://127.0.0.1:43210/callback/abc", challenge)
    parsed = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc == "openrouter.ai" and parsed.path == "/auth"
    assert qs["code_challenge"] == [challenge]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["callback_url"] == ["http://127.0.0.1:43210/callback/abc"]


async def test_loopback_callback_server_security_and_single_use():
    with LoopbackCallbackServer() as cb:
        # DNS-rebinding Host header is rejected with 403.
        st_host, _, _ = _http_req(cb.port, "GET", f"{cb.callback_path}?code=bad", host_header="evil.example.com")
        assert st_host == 403

        # Wrong path returns 404 with security headers.
        st_404, hdrs_404, _ = _http_req(cb.port, "GET", "/callback/wrong-nonce?code=bad")
        assert st_404 == 404
        assert hdrs_404.get("x-content-type-options") == "nosniff"
        assert hdrs_404.get("x-frame-options") == "DENY"
        assert hdrs_404.get("cache-control") == "no-store"

        # Missing code returns 400.
        st_400, _, _ = _http_req(cb.port, "GET", cb.callback_path)
        assert st_400 == 400

        # Valid single-use callback resolves wait_for_code.
        waiter = asyncio.create_task(cb.wait_for_code(timeout=5.0))
        st_ok, _, body_ok = await asyncio.to_thread(_http_req, cb.port, "GET", f"{cb.callback_path}?code=or_auth_123")
        assert st_ok == 200 and "授权成功" in body_ok.decode("utf-8")
        assert await waiter == "or_auth_123"

        # Replay of callback path returns 410 Gone.
        st_gone, _, _ = _http_req(cb.port, "GET", f"{cb.callback_path}?code=second_attempt")
        assert st_gone == 410


async def test_openrouter_pkce_full_login_and_exchange_guards():
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://openrouter.ai/api/v1/auth/keys/code":
            payload = json.loads(request.content.decode("utf-8"))
            seen_payloads.append(payload)
            if payload.get("code") == "redirect_code":
                return httpx.Response(302, headers={"Location": "https://other.example.com/steal"})
            if payload.get("code") == "bad_code":
                return httpx.Response(401, json={"error": "invalid"})
            return httpx.Response(200, json={"key": "or-live-secret-key-987654"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    # Redirects and non-200 responses must fail closed.
    with pytest.raises(AuthError, match="重定向"):
        await exchange_openrouter_code("redirect_code", "v" * 44, "http://127.0.0.1:1/callback/x", transport=transport)
    with pytest.raises(AuthError, match="401"):
        await exchange_openrouter_code("bad_code", "v" * 44, "http://127.0.0.1:1/callback/x", transport=transport)

    # Full end-to-end OpenRouterPKCEAuth.login() with local callback delivery.
    opened_urls: list[str] = []

    def simulate_browser(auth_url: str) -> None:
        opened_urls.append(auth_url)
        cb_url = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)["callback_url"][0]
        parsed_cb = urllib.parse.urlsplit(cb_url)
        status, _, _ = _http_req(parsed_cb.port or 80, "GET", f"{parsed_cb.path}?code=valid_pkce_code")
        assert status == 200

    auth = OpenRouterPKCEAuth(transport=transport)
    result = await auth.login(open_browser=simulate_browser, timeout=5.0)
    assert len(opened_urls) == 1
    assert result.provider_key == "openrouter"
    assert result.preset == "openrouter"
    assert result.credential == "or-live-secret-key-987654"
    assert seen_payloads[-1]["code"] == "valid_pkce_code"
    assert seen_payloads[-1]["code_challenge_method"] == "S256"


# ---------------------------------------------------------------------------
# GitHub Copilot Device Flow, Session Exchange, Caching & Refresh
# ---------------------------------------------------------------------------


async def test_copilot_device_flow_validates_host_handles_slow_down_and_exchanges_token():
    poll_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        url = str(request.url)
        if url == "https://github.com/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev_code_abc",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        if url == "https://github.com/login/oauth/access_token":
            poll_calls += 1
            if poll_calls == 1:
                return httpx.Response(200, json={"error": "authorization_pending"})
            if poll_calls == 2:
                return httpx.Response(200, json={"error": "slow_down"})
            return httpx.Response(200, json={"access_token": "ghu_copilot_test_token_123456"})
        if url == "https://api.github.com/copilot_internal/v2/token":
            assert request.headers.get("Authorization") == "token ghu_copilot_test_token_123456"
            return httpx.Response(
                200,
                json={
                    "token": "tid=copilot_session_xyz_123456;exp=9999999999",
                    "expires_at": 9999999999,
                    "endpoints": {"api": "https://api.individual.githubcopilot.com"},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    copilot_token_manager.invalidate()
    notices: list[str] = []
    auth = GitHubCopilotAuth(transport=transport, sleep=fake_sleep)
    result = await auth.login(on_notice=notices.append, timeout=60.0)
    assert result.provider_key == "copilot"
    assert result.auth_type == "github-copilot"
    assert result.credential == "ghu_copilot_test_token_123456"
    assert result.base_url == "https://api.individual.githubcopilot.com"
    assert any("ABCD-1234" in msg for msg in notices)
    # RFC 8628: after slow_down, polling interval increased by 5 seconds.
    assert slept == [1.0, 1.0, 6.0]


async def test_copilot_security_rejects_untrusted_verification_uri_and_api_endpoint():
    # Spoofed verification_uri outside github.com must be rejected.
    bad_uri_transport = httpx.MockTransport(
        lambda _req: httpx.Response(
            200,
            json={
                "device_code": "d",
                "user_code": "1234-5678",
                "verification_uri": "https://phish.example.com/login/device",
            },
        )
    )
    with pytest.raises(AuthError, match="不受信任"):
        await start_copilot_device_flow(transport=bad_uri_transport)

    # Denied authorization stops polling cleanly.
    denied_transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"error": "access_denied"}))

    async def no_sleep(_s: float) -> None:
        return

    with pytest.raises(AuthError, match="取消"):
        await poll_copilot_device_token(
            DeviceCodeChallenge("d", "1234", "https://github.com/login/device", 60, 1),
            transport=denied_transport,
            sleep=no_sleep,
        )

    # Spoofed Copilot session API endpoint outside githubcopilot.com must be rejected (anti-SSRF).
    bad_endpoint_transport = httpx.MockTransport(
        lambda _req: httpx.Response(
            200,
            json={
                "token": "tid=session123",
                "expires_at": 9999999999,
                "endpoints": {"api": "https://evil.example.com"},
            },
        )
    )
    with pytest.raises(AuthError, match="不受信任"):
        exchange_copilot_session_token("ghu_test_12345678", transport=bad_endpoint_transport)


def test_copilot_token_manager_caches_and_refreshes_near_expiry():
    mgr = CopilotTokenManager(refresh_margin=120.0)
    calls = 0
    clock = [1000.0]

    def fake_exchange(gh_token: str) -> tuple[str, float, str]:
        nonlocal calls
        calls += 1
        return f"session-{calls}", clock[0] + 600.0, COPILOT_API_BASE

    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-1"
    # Within valid window -> cache hit, no exchange.
    clock[0] = 1400.0
    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-1"
    assert calls == 1
    # Within 120s refresh margin -> refreshes automatically.
    clock[0] = 1500.0
    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-2"
    assert calls == 2


async def test_copilot_provider_merges_headers_and_refreshes_token_on_stream(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "copilot",
                        "preset": "copilot",
                        "api_key": "ghu_live_token_123456",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(cfg_path, tmp_path)
    prov_cfg = settings.providers[0]
    assert prov_cfg.auth_type == "github-copilot"
    for k, v in COPILOT_HEADERS:
        assert prov_cfg.headers[k] == v

    # Probe with auth_type="github-copilot" exchanges session token and sends Copilot headers.
    def probe_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.github.com/copilot_internal/v2/token":
            return httpx.Response(200, json={"token": "tid=probe_session_999", "expires_at": 9999999999})
        if str(request.url) == "https://api.githubcopilot.com/models":
            assert request.headers.get("Authorization") == "Bearer tid=probe_session_999"
            assert request.headers.get("Copilot-Integration-Id") == "vscode-chat"
            return httpx.Response(200, json={"data": [{"id": "gpt-4.1"}, {"id": "claude-sonnet-4"}]})
        return httpx.Response(404)

    probe_res = await probe_provider(
        "openai-chat-completions",
        COPILOT_API_BASE,
        "ghu_live_token_123456",
        auth_type="github-copilot",
        transport=httpx.MockTransport(probe_handler),
    )
    assert probe_res.ok and probe_res.models == ("gpt-4.1", "claude-sonnet-4")

    # Verify SDKProvider.stream() refreshes client.api_key before sending.
    tokens = iter(["session-tok-1", "session-tok-2"])
    monkeypatch.setattr(copilot_token_manager, "get_session_token", lambda _gh: next(tokens))

    class FakeClient:
        def __init__(self) -> None:
            self.api_key = "initial"

        async def close(self) -> None:
            return

    fake_client = FakeClient()
    sdk = ChatCompletionsProvider(prov_cfg, client=fake_client)

    async def fake_stream(*args, **kwargs):
        yield f"used:{fake_client.api_key}"

    sdk._stream = fake_stream  # type: ignore[method-assign]
    out1 = [ev async for ev in sdk.stream([], "sys")]
    out2 = [ev async for ev in sdk.stream([], "sys")]
    assert out1 == ["used:session-tok-1"]
    assert out2 == ["used:session-tok-2"]


# ---------------------------------------------------------------------------
# Persistence fail-closed & `moss login` CLI
# ---------------------------------------------------------------------------


def test_persist_oauth_result_fails_closed_without_keyring_unless_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("MOSSCODE_ALLOW_PLAINTEXT_OAUTH", raising=False)
    result = OAuthLoginResult(
        provider_key="openrouter",
        preset="openrouter",
        credential="or-secret-token-123456",
        auth_type="api_key",
        default_model="anthropic/claude-sonnet-5.5",
        base_url="https://openrouter.ai/api/v1",
    )

    class BrokenVault:
        def problem(self) -> str:
            return "无桌面密钥环"

    with pytest.raises(AuthError, match="拒绝"):
        persist_oauth_result(result, home=tmp_path, vault=BrokenVault())
    assert not (tmp_path / ".mosscode" / "config.yaml").exists()

    # Explicit opt-in writes with private permissions.
    saved_path, saved_name = persist_oauth_result(result, home=tmp_path, vault=BrokenVault(), allow_plaintext=True)
    assert saved_name == "openrouter"
    assert load_settings(saved_path, tmp_path).providers[0].api_key == "or-secret-token-123456"


def test_login_command_cli_openrouter_status_and_logout(tmp_path):
    from mosscode.login_command import main as login_main

    stored: dict[str, str] = {}

    class MemoryVault:
        def problem(self):
            return None

        def exists(self, name: str) -> bool:
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

        def delete(self, name: str) -> None:
            stored.pop(name, None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"key": "or-cli-secret-abcdef1234"})

    def fake_browser(auth_url: str) -> None:
        cb_url = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)["callback_url"][0]
        parsed_cb = urllib.parse.urlsplit(cb_url)
        st, _, _ = _http_req(parsed_cb.port or 80, "GET", f"{parsed_cb.path}?code=cli_pkce_ok")
        assert st == 200

    out: list[str] = []
    err: list[str] = []
    rc = login_main(
        ["openrouter", "--default"],
        home=tmp_path,
        write=out.append,
        err_write=err.append,
        open_browser=fake_browser,
        transport=httpx.MockTransport(handler),
        vault=MemoryVault(),
    )
    assert rc == 0, err
    assert stored["openrouter"] == "or-cli-secret-abcdef1234"
    # Secret must never be printed to stdout or stderr.
    combined = "\n".join(out + err)
    assert "or-cli-secret-abcdef1234" not in combined

    # Status shows provider without leaking secret.
    status_out: list[str] = []
    assert (
        login_main(
            ["status", "--config", str(tmp_path / ".mosscode" / "config.yaml")], home=tmp_path, write=status_out.append
        )
        == 0
    )
    assert any("openrouter" in line and "默认" in line for line in status_out)
    assert "or-cli-secret-abcdef1234" not in "\n".join(status_out)


# ---------------------------------------------------------------------------
# Local Tokenized Web Configuration UI (`moss config --web`)
# ---------------------------------------------------------------------------


def test_config_web_server_security_boundaries_and_full_api(tmp_path):
    stored: dict[str, str] = {}

    class MemoryVault:
        def problem(self):
            return None

        def exists(self, name: str) -> bool:
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

        def delete(self, name: str) -> None:
            stored.pop(name, None)

    def mock_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.deepseek.com/models":
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
        if url == "https://github.com/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "web_dev_code",
                    "user_code": "WEB1-2345",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 300,
                    "interval": 5,
                },
            )
        if url == "https://github.com/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "ghu_web_copilot_secret_999"})
        if url == "https://api.github.com/copilot_internal/v2/token":
            return httpx.Response(200, json={"token": "tid=web_session_999", "expires_at": 9999999999})
        return httpx.Response(404)

    with ConfigWebServer(
        home=tmp_path,
        vault=MemoryVault(),
        transport=httpx.MockTransport(mock_http),
    ) as srv:
        # 1. Unauthorized GET / and /api/state are rejected (403).
        st_unauth, hdrs_unauth, _ = _http_req(srv.port, "GET", "/")
        assert st_unauth == 403
        assert hdrs_unauth.get("x-frame-options") == "DENY"
        assert hdrs_unauth.get("x-content-type-options") == "nosniff"
        assert hdrs_unauth.get("cache-control") == "no-store"

        st_api_unauth, _, _ = _http_req(srv.port, "GET", "/api/state")
        assert st_api_unauth == 403

        # 2. Anti-DNS-rebinding: non-loopback Host header is rejected even with valid token.
        st_rebinding, _, _ = _http_req(
            srv.port,
            "GET",
            "/api/state",
            headers={"Authorization": f"Bearer {srv.boot_token}"},
            host_header="evil.example.com",
        )
        assert st_rebinding == 403

        # 3. Boot token exchange: GET /?token=<boot_token> sets HttpOnly; SameSite=Strict cookie + 303 redirect.
        st_boot, hdrs_boot, _ = _http_req(srv.port, "GET", f"/?token={srv.boot_token}")
        assert st_boot == 303 and hdrs_boot.get("location") == "/"
        set_cookie = hdrs_boot.get("set-cookie", "")
        assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
        cookie_pair = set_cookie.split(";", 1)[0]

        # Replaying the single-use boot token on GET /?token=... or Authorization Bearer is rejected.
        st_replay, _, _ = _http_req(srv.port, "GET", f"/?token={srv.boot_token}")
        assert st_replay == 403
        st_bearer_after, _, _ = _http_req(
            srv.port,
            "GET",
            "/api/state",
            headers={"Authorization": f"Bearer {srv.boot_token}"},
        )
        assert st_bearer_after == 403

        # 4. Authenticated GET / renders HTML with CSP script nonce and CSRF meta token.
        st_html, hdrs_html, raw_html = _http_req(srv.port, "GET", "/", headers={"Cookie": cookie_pair})
        assert st_html == 200
        csp = hdrs_html.get("content-security-policy", "")
        assert "script-src 'nonce-" in csp and "frame-ancestors 'none'" in csp
        html_text = raw_html.decode("utf-8")
        m_csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', html_text)
        assert m_csrf is not None
        csrf = m_csrf.group(1)

        # 5. Missing CSRF header, cross-origin POST, or cross-site Sec-Fetch-Site is rejected (403).
        st_no_csrf, _, _ = _http_req(srv.port, "POST", "/api/default", headers={"Cookie": cookie_pair}, body=b"{}")
        assert st_no_csrf == 403
        st_bad_origin, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/default",
            headers={
                "Cookie": cookie_pair,
                "X-MossCode-Token": csrf,
                "Origin": "https://evil.example.com",
            },
            body=b"{}",
        )
        assert st_bad_origin == 403
        st_bad_fetch, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/default",
            headers={
                "Cookie": cookie_pair,
                "X-MossCode-Token": csrf,
                "Sec-Fetch-Site": "cross-site",
            },
            body=b"{}",
        )
        assert st_bad_fetch == 403

        # 6. Oversized body (> 64 KiB) returns 413.
        st_big, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/providers",
            headers={"Cookie": cookie_pair, "X-MossCode-Token": csrf},
            body=b"{" + b"a" * (65 * 1024) + b"}",
        )
        assert st_big == 413

        auth_hdrs = {
            "Cookie": cookie_pair,
            "X-MossCode-Token": csrf,
            "Origin": f"http://127.0.0.1:{srv.port}",
        }

        # 7. Probe DeepSeek via /api/probe.
        st_probe, _, body_probe = _http_req(
            srv.port,
            "POST",
            "/api/probe",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "protocol": "openai-chat-completions",
                    "base_url": "https://api.deepseek.com",
                    "cred_method": "keyring",
                    "secret": "ds-probe-key-123456",
                }
            ).encode("utf-8"),
        )
        assert st_probe == 200
        probe_json = json.loads(body_probe)
        assert probe_json["ok"] and probe_json["models"] == ["deepseek-chat", "deepseek-reasoner"]

        # 8. Save provider via /api/providers -> stored in keyring, never leaked in /api/state.
        st_save, _, body_save = _http_req(
            srv.port,
            "POST",
            "/api/providers",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "preset": "deepseek",
                    "name": "deepseek",
                    "protocol": "openai-chat-completions",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                    "cred_method": "keyring",
                    "secret": "ds-saved-secret-999999",
                    "set_default": True,
                }
            ).encode("utf-8"),
        )
        assert st_save == 200 and json.loads(body_save)["name"] == "deepseek"
        assert stored["deepseek"] == "ds-saved-secret-999999"

        st_state, _, body_state = _http_req(srv.port, "GET", "/api/state", headers=auth_hdrs)
        assert st_state == 200
        assert b"ds-saved-secret-999999" not in body_state
        state_json = json.loads(body_state)
        assert state_json["default_provider"] == "deepseek"
        assert state_json["providers"][0]["name"] == "deepseek"

        # 9. GitHub Copilot Device Flow start + poll via Web UI API.
        st_cp1, _, body_cp1 = _http_req(srv.port, "POST", "/api/auth/copilot/start", headers=auth_hdrs, body=b"{}")
        assert st_cp1 == 200
        cp1 = json.loads(body_cp1)
        assert cp1["user_code"] == "WEB1-2345"

        st_cp2, _, body_cp2 = _http_req(
            srv.port,
            "POST",
            "/api/auth/copilot/poll",
            headers=auth_hdrs,
            body=json.dumps({"job_id": cp1["job_id"], "set_default": True}).encode("utf-8"),
        )
        assert st_cp2 == 200 and json.loads(body_cp2) == {"status": "ok", "name": "copilot"}
        assert stored["copilot"] == "ghu_web_copilot_secret_999"
        assert load_settings(srv.config_path, tmp_path).default_provider == "copilot"

        # 10. Shutdown endpoint sets stop event cleanly.
        st_down, _, _ = _http_req(srv.port, "POST", "/api/shutdown", headers=auth_hdrs, body=b"{}")
        assert st_down == 200
        assert srv._stop_event.is_set()


# ---------------------------------------------------------------------------
# TUI `/connect` OAuth integration & Cold-start isolation
# ---------------------------------------------------------------------------


async def test_tui_connect_oauth_openrouter_flow_and_keyring_fail_closed(isolated_home):
    stored: dict[str, str] = {}

    class BrokenVault:
        def problem(self):
            return "无系统凭据库"

    class WorkingVault:
        def problem(self):
            return None

        def exists(self, name: str) -> bool:
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

        def delete(self, name: str) -> None:
            stored.pop(name, None)

    class FakeOAuthProvider:
        async def login(self, *, on_notice=None, open_browser=None, timeout=180.0):
            if on_notice:
                on_notice("正在等待浏览器授权…")
            return OAuthLoginResult(
                provider_key="openrouter",
                preset="openrouter",
                credential="or-tui-secret-12345678",
                auth_type="api_key",
                default_model="anthropic/claude-sonnet-5.5",
                base_url="https://openrouter.ai/api/v1",
            )

    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        yaml.safe_dump(
            {
                "default_provider": "start",
                "providers": [
                    {
                        "name": "start",
                        "protocol": "anthropic-messages",
                        "model": "start-model",
                        "api_key": "start-key",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)
    app = MossCodeApp(
        Conversation(FakeProvider()),
        "start",
        settings=settings,
        model="start-model",
        provider_factory=lambda _cfg: FakeProvider(),
    )

    async def no_local(timeout: float = 0.5):
        return []

    async def fake_probe(protocol, base_url, api_key, **kwargs):
        from mosscode.model_discovery import ProbeResult

        return ProbeResult(True, models=("anthropic/claude-sonnet-5.5", "openai/gpt-5.6"), detail="已连通")

    app._local_server_detector = no_local
    app._provider_prober = fake_probe
    app._oauth_provider_factory = lambda _key: FakeOAuthProvider()
    app._oauth_browser_opener = lambda _url: True

    async with app.run_test(size=(100, 32)) as pilot:
        # 1. Fail-closed when keyring is unavailable before starting browser OAuth.
        app._connect_vault = BrokenVault()
        app.open_connect()
        await pilot.pause()
        or_idx = next(i for i, (act, _) in enumerate(app._connect_choices) if act == "oauth_openrouter")
        await app._handle_connect_option(or_idx)
        await pilot.pause()
        assert app._connect_step == "source"
        assert "拒绝明文保存" in str(app._connect_status_text)

        # 2. Working keyring completes OpenRouter OAuth -> probe -> model -> save -> hot-reload.
        app._connect_vault = WorkingVault()
        await app._handle_connect_option(or_idx)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._connect_step == "model"
        await app._handle_connect_option(0)  # Select first discovered model -> confirm.
        await pilot.pause()
        assert app._connect_step == "confirm"
        await app._handle_connect_option(0)  # Save & switch as default.
        await pilot.pause()
        assert app.provider_name == "openrouter"
        assert stored["openrouter"] == "or-tui-secret-12345678"
        assert load_settings(cfg, isolated_home).default_provider == "openrouter"


def test_launcher_still_lazy_imports_auth_and_config_web():
    code = (
        "import sys\n"
        "from mosscode import launcher\n"
        "try:\n"
        "    launcher.main(['--version'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "bad = [m for m in ('mosscode.auth', 'mosscode.config_web', 'mosscode.login_command', 'mosscode.config_command', 'httpx', 'textual') if m in sys.modules]\n"
        "print(bad)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "[]"


def test_copilot_config_rejects_untrusted_remote_base_url_and_cli_arg_guards(tmp_path):
    from mosscode.config import ConfigError
    from mosscode.config_command import main as config_main
    from mosscode.login_command import main as login_main

    bad_cfg = tmp_path / "bad-copilot.yaml"
    bad_cfg.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "evil-copilot",
                        "protocol": "openai-chat-completions",
                        "base_url": "https://evil.example.com/v1",
                        "model": "gpt-4.1",
                        "auth_type": "github-copilot",
                        "api_key": "ghu_12345678",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="githubcopilot.com"):
        load_settings(bad_cfg, tmp_path)

    errs: list[str] = []
    assert config_main(["--idle-timeout", "-1"], err_write=errs.append) == 2
    assert login_main(["openrouter", "--timeout", "0"], err_write=errs.append) == 2
