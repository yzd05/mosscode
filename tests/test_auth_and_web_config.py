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
import threading
import time
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
from mosscode.config import ConfigError, ProviderConfig, load_settings
from mosscode.config_web import ConfigWebServer
from mosscode.conversation import Conversation
from mosscode.messages import ProviderError
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


async def test_loopback_callback_server_security_single_use_and_close_before_start(monkeypatch):
    # Closing without starting must not hang and must release the bound socket.
    unstarted = LoopbackCallbackServer()
    closer = threading.Thread(target=unstarted.close, daemon=True)
    closer.start()
    closer.join(timeout=5.0)
    assert not closer.is_alive(), "close() must not block before start()"
    assert unstarted._server.socket.fileno() == -1

    # If thread startup raises, _started stays False, the socket closes, and a later close() never deadlocks.
    failed = LoopbackCallbackServer()
    monkeypatch.setattr(failed._thread, "start", lambda: (_ for _ in ()).throw(RuntimeError("thread boom")))
    with pytest.raises(RuntimeError, match="thread boom"):
        failed.start()
    assert not failed._started and failed._closed
    assert failed._server.socket.fileno() == -1
    closer2 = threading.Thread(target=failed.close, daemon=True)
    closer2.start()
    closer2.join(timeout=5.0)
    assert not closer2.is_alive(), "close() must not block after failed start()"

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


async def test_openrouter_pkce_full_login_uses_documented_keys_exchange_endpoint():
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Only the documented PKCE key-exchange route `/api/v1/auth/keys` is valid (not `/api/v1/auth/keys/code`).
        if str(request.url) == "https://openrouter.ai/api/v1/auth/keys":
            payload = json.loads(request.content.decode("utf-8"))
            seen_payloads.append(payload)
            if payload.get("code") == "redirect_code":
                return httpx.Response(302, headers={"Location": "https://other.example.com/steal"})
            if payload.get("code") == "bad_code":
                return httpx.Response(401, json={"error": "invalid"})
            return httpx.Response(200, json={"key": "or-live-secret-key-987654", "user_id": "u_1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    # Redirects and non-200 responses must fail closed.
    with pytest.raises(AuthError, match="重定向"):
        await exchange_openrouter_code("redirect_code", "v" * 44, transport=transport)
    with pytest.raises(AuthError, match="401"):
        await exchange_openrouter_code("bad_code", "v" * 44, transport=transport)

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
    captured_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(opened_urls[0]).query)
    recomputed_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(seen_payloads[-1]["code_verifier"].encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert recomputed_challenge == captured_qs["code_challenge"][0]
    assert seen_payloads[-1]["code"] == "valid_pkce_code"
    assert seen_payloads[-1]["code_challenge_method"] == "S256"
    assert set(seen_payloads[-1]) == {"code", "code_verifier", "code_challenge_method"}


# ---------------------------------------------------------------------------
# GitHub Copilot Device Flow, Session Exchange, Caching & Refresh
# ---------------------------------------------------------------------------


async def test_copilot_device_flow_handles_slow_down_timeout_and_deadline():
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
            if poll_calls == 3:
                raise httpx.ReadTimeout("transient network hiccup")
            return httpx.Response(200, json={"access_token": "ghu_copilot_test_token_123456"})
        if url == "https://api.github.com/copilot_internal/v2/token":
            assert request.headers.get("Authorization") == "token ghu_copilot_test_token_123456"
            return httpx.Response(
                200,
                json={
                    "token": "tid=copilot_session_xyz_123456;exp=9999999999",
                    "expires_at": int(time.time()) + 1800,
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
    # RFC 8628: slow_down increases interval by 5s, and transient timeout also backs off by 5s instead of aborting.
    assert slept == [1.0, 1.0, 6.0, 11.0]

    # When remaining deadline is shorter than the required polling interval, do not start another poll.
    never_polled: list[int] = []
    with pytest.raises(AuthError, match="超时"):
        await poll_copilot_device_token(
            DeviceCodeChallenge("d", "1234", "https://github.com/login/device", 60, 5),
            timeout=1.0,
            transport=httpx.MockTransport(lambda _r: (never_polled.append(1), httpx.Response(200))[1]),
            sleep=fake_sleep,
        )
    assert never_polled == []


async def test_copilot_security_rejects_untrusted_uri_endpoint_expired_token_and_cleartext_loopback(tmp_path):
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
                "expires_at": int(time.time()) + 600,
                "endpoints": {"api": "https://evil.example.com"},
            },
        )
    )
    with pytest.raises(AuthError, match="不受信任"):
        exchange_copilot_session_token("ghu_test_12345678", transport=bad_endpoint_transport)

    # Already-expired Copilot session token (expires_at <= now) must be rejected.
    expired_transport = httpx.MockTransport(
        lambda _req: httpx.Response(
            200,
            json={"token": "tid=stale_session", "expires_at": int(time.time()) - 10},
        )
    )
    with pytest.raises(AuthError, match="已过期"):
        exchange_copilot_session_token("ghu_test_12345678", transport=expired_transport)

    # Config load & client construction reject omitted base_url, non-Copilot host, and plaintext loopback HTTP.
    for bad_url in (None, "https://evil.example.com/v1", "http://127.0.0.1:8080/v1"):
        entry = {
            "name": "bad-cp",
            "protocol": "openai-chat-completions",
            "model": "gpt-4.1",
            "auth_type": "github-copilot",
            "api_key": "ghu_12345678",
        }
        if bad_url is not None:
            entry["base_url"] = bad_url
        cfg_file = tmp_path / f"bad-{abs(hash(bad_url))}.yaml"
        cfg_file.write_text(yaml.safe_dump({"providers": [entry]}), encoding="utf-8")
        with pytest.raises(ConfigError, match="githubcopilot.com"):
            load_settings(cfg_file, tmp_path)

        direct = ProviderConfig(
            name="direct-cp",
            protocol="openai-chat-completions",
            model="gpt-4.1",
            base_url=bad_url,
            auth_type="github-copilot",
            api_key="ghu_12345678",
        )
        with pytest.raises(ProviderError, match="githubcopilot.com"):
            ChatCompletionsProvider(direct)


def test_copilot_token_manager_caches_and_refreshes_near_expiry():
    mgr = CopilotTokenManager(refresh_margin=120.0)
    calls = 0
    clock = [1000.0]

    def fake_exchange(gh_token: str) -> tuple[str, float, str]:
        nonlocal calls
        calls += 1
        return f"session-{calls}", clock[0] + 600.0, COPILOT_API_BASE

    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-1"
    clock[0] = 1400.0
    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-1"
    assert calls == 1

    # Transient failure during proactive early refresh reuses the still-unexpired session for the same token.
    clock[0] = 1500.0  # 100s left (< 120s margin, still unexpired before 1600.0)

    def transient_fail(_gh: str) -> tuple[str, float, str]:
        raise AuthError("timeout", transient=True)

    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=transient_fail) == "session-1"

    # Non-transient auth rejection (e.g. 401/403) MUST NOT reuse the cached session and invalidates it.
    def auth_fail(_gh: str) -> tuple[str, float, str]:
        raise AuthError("unauthorized", transient=False)

    with pytest.raises(AuthError, match="unauthorized"):
        mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=auth_fail)

    # Next healthy exchange repopulates the cache cleanly.
    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-2"
    assert calls == 2

    # Once actually expired, a transient failure cannot reuse the expired token.
    clock[0] = 2200.0
    with pytest.raises(AuthError, match="timeout"):
        mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=transient_fail)

    # If invalidate() runs while a refresh is in flight and exchange times out, do NOT resurrect the removed token.
    mgr.prime("ghu_abc12345", "session-live", 3000.0, COPILOT_API_BASE)
    clock[0] = 2950.0  # inside 120s refresh window, still unexpired

    def invalidate_then_timeout(_gh: str) -> tuple[str, float, str]:
        mgr.invalidate("ghu_abc12345")
        raise AuthError("transient timeout after invalidate", transient=True)

    with pytest.raises(AuthError, match="transient timeout after invalidate"):
        mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=invalidate_then_timeout)

    # If prime() installs a fresh session while an older refresh is in flight and that refresh fails auth, keep the primed one.
    mgr.prime("ghu_abc12345", "session-old", 3000.0, COPILOT_API_BASE)

    def prime_then_reject(_gh: str) -> tuple[str, float, str]:
        mgr.prime("ghu_abc12345", "session-primed-fresh", 4000.0, COPILOT_API_BASE)
        raise AuthError("stale unauthorized", transient=False)

    with pytest.raises(AuthError, match="stale unauthorized"):
        mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=prime_then_reject)
    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-primed-fresh"

    # Successful exchange must NOT resurrect a session cleared by invalidate() while exchange was in flight.
    mgr.prime("ghu_abc12345", "session-before-logout", 3000.0, COPILOT_API_BASE)
    clock[0] = 2950.0

    def invalidate_then_succeed(_gh: str) -> tuple[str, float, str]:
        mgr.invalidate("ghu_abc12345")
        return "session-stale-after-logout", 5000.0, COPILOT_API_BASE

    with pytest.raises(AuthError, match="已失效"):
        mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=invalidate_then_succeed)
    assert mgr._cache.get(mgr._cache_key("ghu_abc12345")) is None

    # Successful older exchange must NOT overwrite a newer session installed by prime() during exchange.
    mgr.prime("ghu_abc12345", "session-expiring", 3000.0, COPILOT_API_BASE)

    def prime_then_succeed(_gh: str) -> tuple[str, float, str]:
        mgr.prime("ghu_abc12345", "session-newly-primed", 6000.0, COPILOT_API_BASE)
        return "session-stale-old-exchange", 5000.0, COPILOT_API_BASE

    assert (
        mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=prime_then_succeed)
        == "session-newly-primed"
    )
    assert mgr.get_session_token("ghu_abc12345", now=lambda: clock[0], exchange=fake_exchange) == "session-newly-primed"


async def test_copilot_probe_enforces_destination_and_stream_refreshes_inside_retry_loop(tmp_path, monkeypatch):
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

    # Probe rejects untrusted non-Copilot base_url before calling token exchange.
    untrusted_probe = await probe_provider(
        "openai-chat-completions",
        "https://evil.example.com/v1",
        "ghu_live_token_123456",
        auth_type="github-copilot",
        transport=httpx.MockTransport(lambda _r: pytest.fail("must not send network request")),
    )
    assert not untrusted_probe.ok and "githubcopilot.com" in untrusted_probe.error

    # Header-only Copilot probe with base_url=None and api_key=None MUST NOT fall through to api.openai.com.
    header_only_probe = await probe_provider(
        "openai-chat-completions",
        None,
        None,
        auth_type="github-copilot",
        headers={"Authorization": "Bearer caller-secret-header"},
        transport=httpx.MockTransport(lambda _r: pytest.fail("must not send header-only Copilot probe to default URL")),
    )
    assert not header_only_probe.ok and "githubcopilot.com" in header_only_probe.error

    # Credential-bearing loopback probes must disable env proxy routing (trust_env=False).
    seen_trust_env: list[bool] = []
    real_async_client = httpx.AsyncClient

    class RecordingAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            seen_trust_env.append(bool(kwargs.get("trust_env", True)))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)
    loopback_res = await probe_provider(
        "openai-chat-completions",
        "http://127.0.0.1:11434/v1",
        "local-key-123456",
        auth_type="api_key",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"data": [{"id": "llama3.1"}]})),
    )
    assert loopback_res.ok and seen_trust_env == [False]
    monkeypatch.setattr(httpx, "AsyncClient", real_async_client)

    # Probe with base_url=None uses the exchange-returned Copilot endpoint, never api.openai.com.
    def probe_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.github.com/copilot_internal/v2/token":
            return httpx.Response(
                200,
                json={
                    "token": "tid=probe_session_999",
                    "expires_at": int(time.time()) + 1800,
                    "endpoints": {"api": "https://api.individual.githubcopilot.com"},
                },
            )
        if str(request.url) == "https://api.individual.githubcopilot.com/models":
            assert request.headers.get("Authorization") == "Bearer tid=probe_session_999"
            assert request.headers.get("Copilot-Integration-Id") == "vscode-chat"
            return httpx.Response(200, json={"data": [{"id": "gpt-4.1"}, {"id": "claude-sonnet-4"}]})
        return httpx.Response(404)

    probe_res = await probe_provider(
        "openai-chat-completions",
        None,
        "ghu_live_token_123456",
        auth_type="github-copilot",
        transport=httpx.MockTransport(probe_handler),
    )
    assert probe_res.ok and probe_res.models == ("gpt-4.1", "claude-sonnet-4")
    assert probe_res.resolved_base_url == "https://api.individual.githubcopilot.com"

    # SDKProvider.stream() refreshes client.api_key inside the retry loop on each attempt.
    tokens = iter(["session-attempt-1", "session-attempt-2"])
    monkeypatch.setattr(copilot_token_manager, "get_session_token", lambda _gh: next(tokens))

    class FakeClient:
        def __init__(self) -> None:
            self.api_key = "initial"

        async def close(self) -> None:
            return

    fake_client = FakeClient()
    sdk = ChatCompletionsProvider(prov_cfg, client=fake_client)
    sdk.retry_base_delay = 0.0
    seen_keys: list[str] = []

    class TransientErr(Exception):
        pass

    monkeypatch.setattr(
        "mosscode.providers._retry_reason", lambda exc: "限流" if isinstance(exc, TransientErr) else None
    )

    async def flaky_stream(*args, **kwargs):
        seen_keys.append(fake_client.api_key)
        if len(seen_keys) == 1:
            raise TransientErr("retry once")
        yield f"used:{fake_client.api_key}"

    sdk._stream = flaky_stream  # type: ignore[method-assign]
    events = [ev async for ev in sdk.stream([], "sys")]
    assert seen_keys == ["session-attempt-1", "session-attempt-2"]
    assert events[-1] == "used:session-attempt-2"

    # Real Copilot SDK clients (both OpenAI and Anthropic protocols) disable redirects without TypeError.
    monkeypatch.setattr(copilot_token_manager, "get_session_token", lambda _gh: "tid=live_session_no_redirect")
    real_sdk = ChatCompletionsProvider(prov_cfg)
    try:
        assert real_sdk._owned_http_client is not None
        assert real_sdk._owned_http_client.follow_redirects is False
    finally:
        await real_sdk.close()
    assert real_sdk._owned_http_client.is_closed

    from mosscode.providers import AnthropicProvider

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    anthropic_copilot_cfg = ProviderConfig(
        name="copilot-anthropic",
        protocol="anthropic-messages",
        model="claude-sonnet-4",
        base_url="https://api.githubcopilot.com",
        api_key="ghu_live_token_123456",
        auth_type="github-copilot",
    )
    real_anthropic_sdk = AnthropicProvider(anthropic_copilot_cfg)
    try:
        assert real_anthropic_sdk._owned_http_client is not None
        assert real_anthropic_sdk._owned_http_client.follow_redirects is False
        # Remote Copilot URL retains environment proxy routing.
        assert bool(real_anthropic_sdk._owned_http_client._mounts)
    finally:
        await real_anthropic_sdk.close()
    assert real_anthropic_sdk._owned_http_client.is_closed

    # Approved HTTPS loopback Copilot relay must bypass Anthropic's environment proxy mounts.
    anthropic_loopback_cfg = ProviderConfig(
        name="copilot-anthropic-loopback",
        protocol="anthropic-messages",
        model="claude-sonnet-4",
        base_url="https://127.0.0.1:8443",
        api_key="ghu_live_token_123456",
        auth_type="github-copilot",
    )
    real_anthropic_loopback = AnthropicProvider(anthropic_loopback_cfg)
    try:
        assert real_anthropic_loopback._owned_http_client is not None
        assert real_anthropic_loopback._owned_http_client.follow_redirects is False
        assert real_anthropic_loopback._owned_http_client._mounts == {}
    finally:
        await real_anthropic_loopback.close()
    assert real_anthropic_loopback._owned_http_client.is_closed

    # Transient refresh failure when no unexpired cache exists retries cleanly inside stream().
    refresh_attempts = 0

    def flaky_refresh(_gh: str) -> str:
        nonlocal refresh_attempts
        refresh_attempts += 1
        if refresh_attempts == 1:
            raise AuthError("temporary exchange timeout", transient=True)
        return "session-after-transient-refresh"

    monkeypatch.setattr(copilot_token_manager, "get_session_token", flaky_refresh)
    sdk2 = ChatCompletionsProvider(prov_cfg, client=fake_client)
    sdk2.retry_base_delay = 0.0

    async def ok_stream(*args, **kwargs):
        yield f"ok:{fake_client.api_key}"

    sdk2._stream = ok_stream  # type: ignore[method-assign]
    events2 = [ev async for ev in sdk2.stream([], "sys")]
    assert refresh_attempts == 2
    assert events2[-1] == "ok:session-after-transient-refresh"


# ---------------------------------------------------------------------------
# Persistence fail-closed, `moss login` CLI (login + status + logout), and `moss config`
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

    saved_path, saved_name = persist_oauth_result(result, home=tmp_path, vault=BrokenVault(), allow_plaintext=True)
    assert saved_name == "openrouter"
    assert load_settings(saved_path, tmp_path).providers[0].api_key == "or-secret-token-123456"


def test_login_command_cli_openrouter_status_and_logout(tmp_path):
    from mosscode.login_command import main as login_main

    stored: dict[str, str] = {}

    class MemoryVault:
        def problem(self):
            return None

        def label(self):
            return "测试凭据库"

        def exists(self, name: str) -> bool:
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

        def delete(self, name: str) -> None:
            stored.pop(name, None)

    vault = MemoryVault()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/auth/keys"
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
        vault=vault,
    )
    assert rc == 0, err
    assert stored["openrouter"] == "or-cli-secret-abcdef1234"
    assert "or-cli-secret-abcdef1234" not in "\n".join(out + err)

    # Status shows provider without leaking secret.
    status_out: list[str] = []
    assert (
        login_main(
            ["status", "--config", str(tmp_path / ".mosscode" / "config.yaml")],
            home=tmp_path,
            write=status_out.append,
        )
        == 0
    )
    assert any("openrouter" in line and "默认" in line for line in status_out)
    assert "or-cli-secret-abcdef1234" not in "\n".join(status_out)

    # Logout removes the credential entry cleanly.
    logout_out: list[str] = []
    assert login_main(["logout", "openrouter"], write=logout_out.append, vault=vault) == 0
    assert "openrouter" not in stored
    assert any("已从 测试凭据库 删除" in line for line in logout_out)


def test_config_command_web_shuts_down_even_when_browser_opener_blocks(tmp_path):
    from mosscode.config_command import main as config_main

    release_browser = threading.Event()
    browser_started = threading.Event()
    browser_exited = threading.Event()

    def blocking_browser(url: str) -> None:
        browser_started.set()
        release_browser.wait(timeout=5.0)
        browser_exited.set()

    out: list[str] = []
    try:
        rc = config_main(
            ["--web", "--idle-timeout", "0.2"],
            home=tmp_path,
            write=out.append,
            open_browser=blocking_browser,
        )
        assert rc == 0
        assert browser_started.is_set()
        assert not browser_exited.is_set(), "config_main must return while browser opener is still blocked"
    finally:
        release_browser.set()


# ---------------------------------------------------------------------------
# Local Tokenized Web Configuration UI (`moss config --web`)
# ---------------------------------------------------------------------------


def test_config_web_server_security_boundaries_and_full_api(tmp_path):
    # Closing before start() must not hang and must release the bound socket.
    unstarted = ConfigWebServer(home=tmp_path)
    closer = threading.Thread(target=unstarted.close, daemon=True)
    closer.start()
    closer.join(timeout=5.0)
    assert not closer.is_alive(), "close() must not block before start()"
    assert unstarted._server.socket.fileno() == -1

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

    poll_state = {"calls": 0}

    def mock_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.deepseek.com/models":
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
        if url == "https://api.github.com/copilot_internal/v2/token":
            return httpx.Response(
                200,
                json={
                    "token": "tid=web_session_999",
                    "expires_at": int(time.time()) + 1800,
                    "endpoints": {"api": "https://api.enterprise.githubcopilot.com"},
                },
            )
        if url == "https://api.enterprise.githubcopilot.com/models":
            assert request.headers.get("Authorization") == "Bearer tid=web_session_999"
            return httpx.Response(200, json={"data": [{"id": "gpt-4.1"}]})
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
            poll_state["calls"] += 1
            if poll_state["calls"] == 1:
                raise httpx.ReadTimeout("transient timeout")
            return httpx.Response(200, json={"access_token": "ghu_web_copilot_secret_999"})
        return httpx.Response(404)

    with ConfigWebServer(
        home=tmp_path,
        vault=MemoryVault(),
        transport=httpx.MockTransport(mock_http),
    ) as srv:
        # 1. Unauthorized GET / and /api/state are rejected (403) and do NOT reset idle activity.
        baseline = time.monotonic() - 100.0
        srv._last_activity = baseline
        st_unauth, hdrs_unauth, _ = _http_req(srv.port, "GET", "/")
        assert st_unauth == 403
        assert hdrs_unauth.get("x-frame-options") == "DENY"
        assert hdrs_unauth.get("x-content-type-options") == "nosniff"
        assert hdrs_unauth.get("cache-control") == "no-store"
        assert srv._last_activity == baseline

        st_api_unauth, _, _ = _http_req(srv.port, "GET", "/api/state")
        assert st_api_unauth == 403
        assert srv._last_activity == baseline

        # 2. Anti-DNS-rebinding: non-loopback Host header is rejected even with valid boot token.
        st_rebinding, _, _ = _http_req(
            srv.port,
            "GET",
            "/api/state",
            headers={"Authorization": f"Bearer {srv.boot_token}"},
            host_header="evil.example.com",
        )
        assert st_rebinding == 403
        assert srv._last_activity == baseline

        # 3. Single-use boot token returns origin-scoped HTML with NO host-wide Set-Cookie!
        st_boot, hdrs_boot, raw_html = _http_req(srv.port, "GET", f"/?token={srv.boot_token}")
        assert st_boot == 200
        assert "set-cookie" not in hdrs_boot
        assert srv._last_activity > baseline
        csp = hdrs_boot.get("content-security-policy", "")
        assert "script-src 'nonce-" in csp and "frame-ancestors 'none'" in csp
        html_text = raw_html.decode("utf-8")
        m_tok = re.search(r'<meta name="session-bootstrap" content="([^"]+)">', html_text)
        assert m_tok is not None and m_tok.group(1) == srv.session_token
        session_tok = m_tok.group(1)

        # Reloading GET / returns shell with NO embedded session token (browser uses origin-scoped sessionStorage).
        st_reload, hdrs_reload, reload_html = _http_req(srv.port, "GET", "/")
        assert st_reload == 200 and "set-cookie" not in hdrs_reload
        assert '<meta name="session-bootstrap" content="">' in reload_html.decode("utf-8")

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

        # 4. Missing token header, cross-origin POST, or cross-site Sec-Fetch-Site is rejected (403).
        st_no_tok, _, _ = _http_req(srv.port, "POST", "/api/default", body=b"{}")
        assert st_no_tok == 403
        st_bad_origin, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/default",
            headers={"X-MossCode-Token": session_tok, "Origin": "https://evil.example.com"},
            body=b"{}",
        )
        assert st_bad_origin == 403
        st_bad_fetch, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/default",
            headers={"X-MossCode-Token": session_tok, "Sec-Fetch-Site": "cross-site"},
            body=b"{}",
        )
        assert st_bad_fetch == 403

        # 5. Oversized body (> 64 KiB) returns 413.
        st_big, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/providers",
            headers={"X-MossCode-Token": session_tok},
            body=b"{" + b"a" * (65 * 1024) + b"}",
        )
        assert st_big == 413

        auth_hdrs = {
            "X-MossCode-Token": session_tok,
            "Origin": f"http://127.0.0.1:{srv.port}",
        }

        # 6. Corrupt UTF-8 config.yaml reports a repair error in /api/state and structured 400 on save.
        srv.config_path.parent.mkdir(parents=True, exist_ok=True)
        srv.config_path.write_bytes(b"\xff\xfeinvalid-utf8")
        st_corrupt, _, body_corrupt = _http_req(srv.port, "GET", "/api/state", headers=auth_hdrs)
        assert st_corrupt == 200 and json.loads(body_corrupt)["config_error"]
        st_save_corrupt, _, body_save_corrupt = _http_req(
            srv.port,
            "POST",
            "/api/providers",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "preset": "deepseek",
                    "name": "deepseek",
                    "model": "deepseek-chat",
                    "cred_method": "keyring",
                    "secret": "ds-key",
                }
            ).encode("utf-8"),
        )
        assert st_save_corrupt == 400 and "无法解析现有配置" in json.loads(body_save_corrupt)["error"]
        srv.config_path.unlink()

        # 7. Probe DeepSeek AND Copilot presets via /api/probe (Copilot preset triggers github-copilot exchange).
        st_probe, _, body_probe = _http_req(
            srv.port,
            "POST",
            "/api/probe",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "preset": "deepseek",
                    "protocol": "openai-chat-completions",
                    "base_url": "https://api.deepseek.com",
                    "cred_method": "keyring",
                    "secret": "ds-probe-key-123456",
                }
            ).encode("utf-8"),
        )
        assert st_probe == 200
        assert json.loads(body_probe)["models"] == ["deepseek-chat", "deepseek-reasoner"]

        st_cp_probe, _, body_cp_probe = _http_req(
            srv.port,
            "POST",
            "/api/probe",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "preset": "copilot",
                    "cred_method": "keyring",
                    "secret": "ghu_probe_token_123456",
                }
            ).encode("utf-8"),
        )
        assert st_cp_probe == 200
        cp_probe_json = json.loads(body_cp_probe)
        assert cp_probe_json["models"] == ["gpt-4.1"]
        assert cp_probe_json["resolved_base_url"] == "https://api.enterprise.githubcopilot.com"

        # 8. Save preset Ollama with blank URL falls back to preset loopback URL (while remote none URL is rejected).
        st_ollama_bad, _, _ = _http_req(
            srv.port,
            "POST",
            "/api/providers",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "preset": "ollama",
                    "name": "ollama-bad",
                    "base_url": "https://remote.example.com/v1",
                    "model": "llama3.1",
                    "cred_method": "none",
                }
            ).encode("utf-8"),
        )
        assert st_ollama_bad == 400
        st_ollama_ok, _, body_ollama_ok = _http_req(
            srv.port,
            "POST",
            "/api/providers",
            headers=auth_hdrs,
            body=json.dumps(
                {
                    "preset": "ollama",
                    "name": "ollama",
                    "base_url": "",
                    "model": "llama3.1",
                    "cred_method": "none",
                }
            ).encode("utf-8"),
        )
        assert st_ollama_ok == 200 and json.loads(body_ollama_ok)["name"] == "ollama"

        # 8b. Save provider via /api/providers -> stored in keyring, never leaked in /api/state.
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
        assert st_state == 200 and b"ds-saved-secret-999999" not in body_state
        assert json.loads(body_state)["default_provider"] == "deepseek"

        # 9. GitHub Copilot Device Flow start + transient timeout keeps job (returns slow_down) + retry succeeds.
        st_cp1, _, body_cp1 = _http_req(srv.port, "POST", "/api/auth/copilot/start", headers=auth_hdrs, body=b"{}")
        assert st_cp1 == 200
        cp1 = json.loads(body_cp1)
        assert cp1["user_code"] == "WEB1-2345"

        st_cp_to, _, body_cp_to = _http_req(
            srv.port,
            "POST",
            "/api/auth/copilot/poll",
            headers=auth_hdrs,
            body=json.dumps({"job_id": cp1["job_id"], "set_default": True}).encode("utf-8"),
        )
        assert st_cp_to == 200 and json.loads(body_cp_to) == {"status": "slow_down"}

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

        # 10. Shutdown endpoint fences commits and sets stop event cleanly.
        st_down, _, _ = _http_req(srv.port, "POST", "/api/shutdown", headers=auth_hdrs, body=b"{}")
        assert st_down == 200
        assert srv._stop_event.is_set() and srv._shutting_down


def test_config_web_superseded_openrouter_job_cannot_commit_after_replacement(tmp_path):
    stored: dict[str, str] = {}
    exchange_a_entered = threading.Event()
    release_a = threading.Event()

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
        payload = json.loads(request.content.decode("utf-8"))
        if payload["code"] == "code_a":
            exchange_a_entered.set()
            assert release_a.wait(timeout=5.0)
            return httpx.Response(200, json={"key": "or-stale-key-from-job-a"})
        return httpx.Response(200, json={"key": "or-fresh-key-from-job-b"})

    with ConfigWebServer(home=tmp_path, vault=MemoryVault(), transport=httpx.MockTransport(handler)) as srv:
        auth_hdrs = {"Authorization": f"Bearer {srv.boot_token}", "Origin": f"http://127.0.0.1:{srv.port}"}
        st_a, _, body_a = _http_req(
            srv.port, "POST", "/api/auth/openrouter/start", headers=auth_hdrs, body=b'{"set_default":true}'
        )
        assert st_a == 200
        job_a = json.loads(body_a)
        cb_a = urllib.parse.urlsplit(
            urllib.parse.parse_qs(urllib.parse.urlsplit(job_a["auth_url"]).query)["callback_url"][0]
        )
        assert _http_req(cb_a.port or 80, "GET", f"{cb_a.path}?code=code_a")[0] == 200
        assert exchange_a_entered.wait(timeout=5.0)

        # While job A is exchanging the code, status reports "exchanging" (distinguished from "pending").
        _, _, st_a_ex_raw = _http_req(
            srv.port, "GET", f"/api/auth/openrouter/status?job={job_a['job_id']}", headers=auth_hdrs
        )
        assert json.loads(st_a_ex_raw)["status"] == "exchanging"

        # Start replacement job B while job A is mid-exchange.
        st_b, _, body_b = _http_req(
            srv.port, "POST", "/api/auth/openrouter/start", headers=auth_hdrs, body=b'{"set_default":true}'
        )
        assert st_b == 200
        job_b = json.loads(body_b)
        cb_b = urllib.parse.urlsplit(
            urllib.parse.parse_qs(urllib.parse.urlsplit(job_b["auth_url"]).query)["callback_url"][0]
        )
        assert _http_req(cb_b.port or 80, "GET", f"{cb_b.path}?code=code_b")[0] == 200

        # Wait for job B to finish committing, then release superseded job A and wait deterministically for its worker.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _, _, st_raw = _http_req(
                srv.port, "GET", f"/api/auth/openrouter/status?job={job_b['job_id']}", headers=auth_hdrs
            )
            if json.loads(st_raw)["status"] == "ok":
                break
            time.sleep(0.05)
        assert stored == {"openrouter": "or-fresh-key-from-job-b"}

        worker_a = srv._or_jobs[job_a["job_id"]]["thread"]
        release_a.set()
        worker_a.join(timeout=5.0)
        assert not worker_a.is_alive(), "superseded job A worker must finish promptly"
        # Superseded job A must NOT have persisted its key or overwritten its replaced status.
        assert stored == {"openrouter": "or-fresh-key-from-job-b"}
        _, _, st_a_raw = _http_req(
            srv.port, "GET", f"/api/auth/openrouter/status?job={job_a['job_id']}", headers=auth_hdrs
        )
        st_a_json = json.loads(st_a_raw)
        assert st_a_json["status"] == "error" and "取代" in st_a_json["error"]


def test_config_web_copilot_poll_rejects_duplicate_concurrent_poll_without_dropping_job(tmp_path):
    stored: dict[str, str] = {}
    exchange_entered = threading.Event()
    release_exchange = threading.Event()
    token_poll_calls = 0

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
        nonlocal token_poll_calls
        url = str(request.url)
        if url == "https://github.com/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "dup_dev_code",
                    "user_code": "DUP1-2345",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 300,
                    "interval": 5,
                },
            )
        if url == "https://github.com/login/oauth/access_token":
            token_poll_calls += 1
            if token_poll_calls > 1:
                return httpx.Response(200, json={"error": "expired_token"})
            return httpx.Response(200, json={"access_token": "ghu_single_flight_secret"})
        if url == "https://api.github.com/copilot_internal/v2/token":
            exchange_entered.set()
            assert release_exchange.wait(timeout=5.0)
            return httpx.Response(200, json={"token": "tid=single_flight_sess", "expires_at": int(time.time()) + 1800})
        return httpx.Response(404)

    with ConfigWebServer(home=tmp_path, vault=MemoryVault(), transport=httpx.MockTransport(handler)) as srv:
        auth_hdrs = {"Authorization": f"Bearer {srv.boot_token}", "Origin": f"http://127.0.0.1:{srv.port}"}
        st_start, _, body_start = _http_req(srv.port, "POST", "/api/auth/copilot/start", headers=auth_hdrs, body=b"{}")
        assert st_start == 200
        job_id = json.loads(body_start)["job_id"]

        poll_a_res: list[tuple[int, dict]] = []

        def run_poll_a() -> None:
            st, _, body = _http_req(
                srv.port,
                "POST",
                "/api/auth/copilot/poll",
                headers=auth_hdrs,
                body=json.dumps({"job_id": job_id, "set_default": True}).encode("utf-8"),
            )
            poll_a_res.append((st, json.loads(body)))

        t_a = threading.Thread(target=run_poll_a, daemon=True)
        t_a.start()
        try:
            assert exchange_entered.wait(timeout=5.0)
            # Duplicate poll while poll A is exchanging must return pending without calling GitHub or dropping the job.
            st_dup, _, body_dup = _http_req(
                srv.port,
                "POST",
                "/api/auth/copilot/poll",
                headers=auth_hdrs,
                body=json.dumps({"job_id": job_id, "set_default": True}).encode("utf-8"),
            )
            assert st_dup == 200 and json.loads(body_dup) == {"status": "pending"}
            assert token_poll_calls == 1
        finally:
            release_exchange.set()
            t_a.join(timeout=5.0)
        assert not t_a.is_alive()
        assert poll_a_res == [(200, {"status": "ok", "name": "copilot"})]
        assert stored == {"copilot": "ghu_single_flight_secret"}


def test_config_web_oauth_cross_provider_fencing_generation_ordering_and_exchange_retry(tmp_path):
    stored: dict[str, str] = {}
    cp_exchange_entered = threading.Event()
    release_cp_exchange = threading.Event()
    access_token_calls = 0
    session_exchange_calls = 0

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
        nonlocal access_token_calls, session_exchange_calls
        url = str(request.url)
        if url == "https://github.com/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "retry_dev_code",
                    "user_code": "RET1-2345",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 300,
                    "interval": 5,
                },
            )
        if url == "https://github.com/login/oauth/access_token":
            access_token_calls += 1
            return httpx.Response(200, json={"access_token": f"ghu_tok_{access_token_calls}"})
        if url == "https://api.github.com/copilot_internal/v2/token":
            session_exchange_calls += 1
            if session_exchange_calls == 1:
                # Transient 503 during first session exchange must retain the OAuth token for retry.
                return httpx.Response(503, json={"error": "upstream unavailable"})
            if session_exchange_calls == 2:
                return httpx.Response(200, json={"token": "tid=retried_ok", "expires_at": int(time.time()) + 1800})
            # Third exchange pauses so we can switch providers to OpenRouter mid-exchange.
            cp_exchange_entered.set()
            assert release_cp_exchange.wait(timeout=5.0)
            return httpx.Response(200, json={"token": "tid=stale_cp", "expires_at": int(time.time()) + 1800})
        if url == "https://openrouter.ai/api/v1/auth/keys":
            return httpx.Response(200, json={"key": "or-newer-provider-key"})
        return httpx.Response(404)

    with ConfigWebServer(home=tmp_path, vault=MemoryVault(), transport=httpx.MockTransport(handler)) as srv:
        auth_hdrs = {"Authorization": f"Bearer {srv.boot_token}", "Origin": f"http://127.0.0.1:{srv.port}"}

        # 1. Transient 503 during Copilot session exchange retains github_token and retries ONLY session exchange.
        st_s1, _, body_s1 = _http_req(
            srv.port, "POST", "/api/auth/copilot/start", headers=auth_hdrs, body=b'{"generation":2}'
        )
        assert st_s1 == 200
        cp_job_1 = json.loads(body_s1)["job_id"]

        # Older out-of-order generation=1 start is rejected with 409 Conflict before replacing job 1.
        st_stale_gen, _, _ = _http_req(
            srv.port, "POST", "/api/auth/openrouter/start", headers=auth_hdrs, body=b'{"generation":1}'
        )
        assert st_stale_gen == 409

        st_p1, _, body_p1 = _http_req(
            srv.port,
            "POST",
            "/api/auth/copilot/poll",
            headers=auth_hdrs,
            body=json.dumps({"job_id": cp_job_1, "set_default": True}).encode("utf-8"),
        )
        assert st_p1 == 200 and json.loads(body_p1) == {"status": "slow_down"}
        assert access_token_calls == 1 and session_exchange_calls == 1

        st_p2, _, body_p2 = _http_req(
            srv.port,
            "POST",
            "/api/auth/copilot/poll",
            headers=auth_hdrs,
            body=json.dumps({"job_id": cp_job_1, "set_default": True}).encode("utf-8"),
        )
        assert st_p2 == 200 and json.loads(body_p2) == {"status": "ok", "name": "copilot"}
        # Device-code endpoint was NOT polled a second time; only session exchange retried.
        assert access_token_calls == 1 and session_exchange_calls == 2
        assert stored["copilot"] == "ghu_tok_1"

        # 2. Cross-provider fencing: Copilot poll in-flight -> user switches to OpenRouter -> stale Copilot is fenced!
        st_s2, _, body_s2 = _http_req(
            srv.port, "POST", "/api/auth/copilot/start", headers=auth_hdrs, body=b'{"generation":3}'
        )
        assert st_s2 == 200
        cp_job_2 = json.loads(body_s2)["job_id"]
        poll2_out: list[tuple[int, dict]] = []

        def run_cp2() -> None:
            st, _, raw = _http_req(
                srv.port,
                "POST",
                "/api/auth/copilot/poll",
                headers=auth_hdrs,
                body=json.dumps({"job_id": cp_job_2, "set_default": True}).encode("utf-8"),
            )
            poll2_out.append((st, json.loads(raw)))

        t_cp2 = threading.Thread(target=run_cp2, daemon=True)
        t_cp2.start()
        try:
            assert cp_exchange_entered.wait(timeout=5.0)
            st_or, _, body_or = _http_req(
                srv.port,
                "POST",
                "/api/auth/openrouter/start",
                headers=auth_hdrs,
                body=b'{"set_default":true,"generation":4}',
            )
            assert st_or == 200
            or_job = json.loads(body_or)
            cb = urllib.parse.urlsplit(
                urllib.parse.parse_qs(urllib.parse.urlsplit(or_job["auth_url"]).query)["callback_url"][0]
            )
            assert _http_req(cb.port or 80, "GET", f"{cb.path}?code=or_wins")[0] == 200
            srv._or_jobs[or_job["job_id"]]["thread"].join(timeout=5.0)
            assert load_settings(srv.config_path, tmp_path).default_provider == "openrouter"
        finally:
            release_cp_exchange.set()
            t_cp2.join(timeout=5.0)
        assert not t_cp2.is_alive()
        assert poll2_out[0][0] == 200 and poll2_out[0][1]["status"] == "error" and "取代" in poll2_out[0][1]["error"]
        # OpenRouter stayed default and stale Copilot poll did not overwrite the saved Copilot token.
        assert load_settings(srv.config_path, tmp_path).default_provider == "openrouter"
        assert stored["copilot"] == "ghu_tok_1"
        assert stored["openrouter"] == "or-newer-provider-key"


def test_config_web_reload_epoch_and_pre_github_reservation_fences_openrouter(tmp_path):
    stored: dict[str, str] = {}
    or_exchange_entered = threading.Event()
    release_or_exchange = threading.Event()
    gh_device_entered = threading.Event()
    release_gh_device = threading.Event()

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
        url = str(request.url)
        if url == "https://openrouter.ai/api/v1/auth/keys":
            or_exchange_entered.set()
            assert release_or_exchange.wait(timeout=5.0)
            return httpx.Response(200, json={"key": "or-must-not-commit"})
        if url == "https://github.com/login/device/code":
            gh_device_entered.set()
            assert release_gh_device.wait(timeout=5.0)
            return httpx.Response(503, json={"error": "github temporarily down"})
        return httpx.Response(404)

    with ConfigWebServer(home=tmp_path, vault=MemoryVault(), transport=httpx.MockTransport(handler)) as srv:
        # Bootstrap gets epoch=1; reloading GET / gets epoch=2 so generation=1 works immediately after reload.
        st1, _, html1 = _http_req(srv.port, "GET", f"/?token={srv.boot_token}")
        assert st1 == 200 and '<meta name="oauth-epoch" content="1">' in html1.decode("utf-8")
        st2, _, html2 = _http_req(srv.port, "GET", "/")
        assert st2 == 200 and '<meta name="oauth-epoch" content="2">' in html2.decode("utf-8")

        auth_hdrs = {"X-MossCode-Token": srv.session_token, "Origin": f"http://127.0.0.1:{srv.port}"}

        # Start OpenRouter on epoch=1, gen=2, deliver callback so it blocks mid-exchange.
        st_or, _, body_or = _http_req(
            srv.port,
            "POST",
            "/api/auth/openrouter/start",
            headers=auth_hdrs,
            body=b'{"set_default":true,"epoch":1,"generation":2}',
        )
        assert st_or == 200
        or_job = json.loads(body_or)
        cb = urllib.parse.urlsplit(
            urllib.parse.parse_qs(urllib.parse.urlsplit(or_job["auth_url"]).query)["callback_url"][0]
        )
        assert _http_req(cb.port or 80, "GET", f"{cb.path}?code=stale_or_code")[0] == 200
        assert or_exchange_entered.wait(timeout=5.0)

        # User reloads page (epoch=2) and clicks Copilot login (gen=1). Copilot reserves ownership BEFORE calling GitHub.
        cp_res: list[int] = []

        def start_cp() -> None:
            st, _, _ = _http_req(
                srv.port,
                "POST",
                "/api/auth/copilot/start",
                headers=auth_hdrs,
                body=b'{"epoch":2,"generation":1}',
            )
            cp_res.append(st)

        t_cp = threading.Thread(target=start_cp, daemon=True)
        t_cp.start()
        try:
            assert gh_device_entered.wait(timeout=5.0)
            # Delayed start from previous page epoch=1 (even with higher gen=9) is rejected with 409 Conflict.
            st_old_epoch, _, _ = _http_req(
                srv.port,
                "POST",
                "/api/auth/openrouter/start",
                headers=auth_hdrs,
                body=b'{"epoch":1,"generation":9}',
            )
            assert st_old_epoch == 409

            # Release the older OpenRouter exchange WHILE Copilot is still blocked waiting on GitHub!
            worker_or = srv._or_jobs[or_job["job_id"]]["thread"]
            release_or_exchange.set()
            worker_or.join(timeout=5.0)
            assert not worker_or.is_alive()
        finally:
            release_or_exchange.set()
            release_gh_device.set()
            t_cp.join(timeout=5.0)
        assert not t_cp.is_alive()
        assert cp_res == [502]
        # Even though GitHub returned 503, the superseded OpenRouter exchange MUST NOT have written credentials or config!
        assert stored == {}
        assert not srv.config_path.exists()


# ---------------------------------------------------------------------------
# TUI `/connect` OAuth integration (including blocking browser & OSError) & Cold-start
# ---------------------------------------------------------------------------


async def test_tui_connect_oauth_openrouter_blocking_browser_cancel_and_oserror(isolated_home):
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

    class BlockingCancelOAuthProvider:
        async def login(self, *, on_notice=None, open_browser=None, timeout=180.0):
            if open_browser:
                open_browser("https://openrouter.ai/auth?demo=1")
            await asyncio.sleep(60.0)
            raise AssertionError("should have been cancelled")

    class BindFailOAuthProvider:
        async def login(self, *, on_notice=None, open_browser=None, timeout=180.0):
            raise OSError("cannot bind loopback port")

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

    release_browser = threading.Event()
    browser_called = threading.Event()
    browser_exited = threading.Event()

    def blocking_browser(_url: str) -> None:
        browser_called.set()
        release_browser.wait(timeout=5.0)
        browser_exited.set()

    try:
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

            # 2. Blocking browser opener runs off-loop so Esc cancellation returns while the opener is still blocked.
            app._connect_vault = WorkingVault()
            app._oauth_provider_factory = lambda _key: BlockingCancelOAuthProvider()
            app._oauth_browser_opener = blocking_browser
            await app._handle_connect_option(or_idx)
            await pilot.pause()
            assert await asyncio.to_thread(browser_called.wait, 2.0)
            assert app._connect_step == "oauth_waiting"
            await pilot.press("escape")
            await pilot.pause()
            assert app._connect_step == "source"
            assert not browser_exited.is_set(), "Esc must cancel OAuth before the blocking browser opener exits"

            # 3. Loopback listener OSError shows login-failure screen rather than crashing Textual.
            app._oauth_provider_factory = lambda _key: BindFailOAuthProvider()
            await app._handle_connect_option(or_idx)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._connect_step == "oauth_failed"
            assert "cannot bind loopback port" in str(app._connect_status_text)
            await pilot.press("escape")
            await pilot.pause()
            assert app._connect_step == "source"

            # 4. Working keyring completes OpenRouter OAuth -> probe -> model -> save -> hot-reload.
            app._oauth_provider_factory = lambda _key: FakeOAuthProvider()
            app._oauth_browser_opener = lambda _url: True
            await app._handle_connect_option(or_idx)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._connect_step == "model"
            await app._handle_connect_option(0)
            await pilot.pause()
            assert app._connect_step == "confirm"
            await app._handle_connect_option(0)
            await pilot.pause()
            assert app.provider_name == "openrouter"
            assert stored["openrouter"] == "or-tui-secret-12345678"
            assert load_settings(cfg, isolated_home).default_provider == "openrouter"
    finally:
        release_browser.set()


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


def test_cli_arg_guards_and_doctor_copilot_hint(tmp_path):
    from mosscode import doctor
    from mosscode.config_command import main as config_main
    from mosscode.login_command import main as login_main

    errs: list[str] = []
    assert config_main(["--idle-timeout", "-1"], err_write=errs.append) == 2
    assert login_main(["openrouter", "--timeout", "0"], err_write=errs.append) == 2

    cfg = tmp_path / "copilot.yaml"
    cfg.write_text(
        yaml.safe_dump({"providers": [{"name": "copilot", "preset": "copilot", "api_key_keyring": "copilot"}]}),
        encoding="utf-8",
    )
    items = doctor.check_config(str(cfg), cwd=tmp_path, home=tmp_path)
    text = doctor.report(items)
    assert "moss key set copilot" in text
    assert "moss login copilot --default" in text
