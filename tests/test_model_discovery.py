"""Tests for cold-start lazy imports, presets, keyless loopback, model discovery, searchable /model, and /connect."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from textual.widgets import Input, OptionList, Static

from mosscode.config import ConfigError, ProviderConfig, is_loopback_url, load_settings
from mosscode.conversation import Conversation
from mosscode.model_discovery import (
    MAX_RECENT_MODELS,
    DetectedLocalServer,
    ModelPrefsStore,
    ProbeResult,
    detect_local_servers,
    fuzzy_score,
    model_key,
    probe_provider,
    save_provider_entry,
    set_default_model,
)
from mosscode.presets import PRESETS, expand
from mosscode.tui import MossCodeApp
from mosscode.tui_views import PromptArea
from test_conversation import FakeProvider


def test_cli_import_does_not_eagerly_load_textual_mcp_or_httpx():
    """Importing mosscode.cli must stay lightweight for headless -p and utility subcommands."""
    code = (
        "import json, sys, mosscode.cli; "
        "print(json.dumps({m: (m in sys.modules) for m in ('textual', 'mcp', 'httpx')}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    loaded = json.loads(proc.stdout.strip())
    assert loaded == {"textual": False, "mcp": False, "httpx": False}


@pytest.mark.parametrize("key", ["gemini", "ollama", "lmstudio"])
def test_new_presets_expand_and_load_with_catalog_window(key, tmp_path):
    preset = PRESETS[key]
    raw = {"name": key, "preset": key}
    if preset.auth_type != "none":
        raw["api_key"] = "test-placeholder-key"
    expanded = expand(raw)
    assert expanded["protocol"] == preset.protocol
    assert expanded["model"] == preset.model
    if preset.auth_type == "none":
        assert expanded["auth_type"] == "none"
        assert "api_key_env" not in expanded
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"default_provider": key, "providers": [raw]}), encoding="utf-8")
    settings = load_settings(cfg, tmp_path)
    prov = settings.providers[0]
    assert prov.name == key
    assert prov.catalog is not None
    if preset.auth_type == "none":
        assert prov.credential() == "mosscode-local"
        from mosscode import doctor

        items = doctor.check_config(str(cfg), cwd=tmp_path, home=tmp_path)
        prov_item = next(i for i in items if "Provider" in i.text)
        assert prov_item.level == doctor.OK and "免密钥（本机回环地址）" in prov_item.text
    else:
        assert prov.context_window and prov.context_window >= 100_000


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434/v1", True),
        ("http://localhost:1234/v1", True),
        ("http://[::1]:8000/v1", True),
        ("https://127.0.0.1:8443/v1", True),
        ("https://api.example.com/v1", False),
        ("http://localhost.evil.example.com/v1", False),
        ("http://127.0.0.1.nip.io/v1", False),
        ("http://localhost@evil.example.com/v1", False),
        ("ftp://127.0.0.1/v1", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback_url_boundaries(url, expected):
    assert is_loopback_url(url) is expected


@pytest.mark.parametrize(
    "bad_url",
    [
        None,
        "https://api.openai.com/v1",
        "http://localhost.evil.example.com/v1",
        "http://localhost@evil.example.com/v1",
        "http://192.168.1.10:11434/v1",
    ],
)
def test_auth_type_none_rejected_for_non_loopback_urls(bad_url, tmp_path):
    entry = {"name": "remote", "protocol": "openai-chat-completions", "model": "m", "auth_type": "none"}
    if bad_url is not None:
        entry["base_url"] = bad_url
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"providers": [entry]}), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"回环|凭据"):
        load_settings(cfg, tmp_path)


def test_fuzzy_score_ranking_and_multitoken():
    assert fuzzy_score("", "anything") == 0
    exact = fuzzy_score("deepseek", "deepseek")
    boundary = fuzzy_score("sonnet", "anthropic/claude-sonnet-4")
    subseq = fuzzy_score("dsn", "deepseek-chat-new")
    assert exact is not None and boundary is not None and subseq is not None
    assert exact > boundary > subseq
    assert fuzzy_score("ds chat", "deepseek deepseek-chat openai-chat-completions") is not None
    assert fuzzy_score("ds zzz", "deepseek deepseek-chat") is None


async def test_probe_provider_openai_anthropic_ollama_and_errors():
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.clear()
        seen_headers.update({k.lower(): v for k, v in request.headers.items()})
        url = str(request.url)
        if url == "https://api.example.com/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m-1"}, {"id": "m-2"}, {"id": "m-1"}]})
        if url == "https://api.anthropic.com/v1/models":
            return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4"}]})
        if url == "http://127.0.0.1:11434/v1/models":
            return httpx.Response(404)
        if url == "http://127.0.0.1:11434/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.1:latest"}, {"name": "qwen2.5:7b"}]})
        if url == "https://relay.example.com/v1/models":
            return httpx.Response(404)
        if url == "https://denied.example.com/v1/models":
            return httpx.Response(401)
        if url == "https://slow.example.com/v1/models":
            raise httpx.ReadTimeout("timed out")
        if url == "https://boom.example.com/v1/models":
            raise httpx.ConnectError("failed with secret-token-99999")
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)

    res = await probe_provider(
        "openai-chat-completions", "https://api.example.com/v1", "key-12345678", transport=transport
    )
    assert res.ok and res.models == ("m-1", "m-2")
    assert seen_headers["authorization"] == "Bearer key-12345678"

    res_anth = await probe_provider("anthropic-messages", None, "anth-key-12345678", transport=transport)
    assert res_anth.ok and res_anth.models == ("claude-sonnet-4",)
    assert seen_headers["x-api-key"] == "anth-key-12345678" and "anthropic-version" in seen_headers

    res_bearer = await probe_provider(
        "anthropic-messages", None, "anth-bearer-123456", auth_type="bearer", transport=transport
    )
    assert res_bearer.ok and seen_headers["authorization"] == "Bearer anth-bearer-123456"

    res_ollama = await probe_provider(
        "openai-chat-completions", "http://127.0.0.1:11434/v1", None, auth_type="none", transport=transport
    )
    assert res_ollama.ok and res_ollama.models == ("llama3.1:latest", "qwen2.5:7b")

    res_relay = await probe_provider(
        "openai-chat-completions", "https://relay.example.com/v1", "key-12345678", transport=transport
    )
    assert res_relay.ok and res_relay.models == () and "未提供 /models" in res_relay.detail

    res_401 = await probe_provider(
        "openai-chat-completions", "https://denied.example.com/v1", "bad-key", transport=transport
    )
    assert not res_401.ok and res_401.status_code == 401

    res_timeout = await probe_provider(
        "openai-chat-completions", "https://slow.example.com/v1", "key", transport=transport
    )
    assert not res_timeout.ok and "超时" in res_timeout.error

    res_boom = await probe_provider(
        "openai-chat-completions", "https://boom.example.com/v1", "secret-token-99999", transport=transport
    )
    assert not res_boom.ok and "secret-token-99999" not in res_boom.error

    res_non_loopback = await probe_provider(
        "openai-chat-completions", "https://api.example.com/v1", None, auth_type="none", transport=transport
    )
    assert not res_non_loopback.ok and "回环" in res_non_loopback.error


async def test_detect_local_servers_discovers_running_endpoints():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://127.0.0.1:11434/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.1"}]})
        raise httpx.ConnectError("connection refused")

    servers = await detect_local_servers(transport=httpx.MockTransport(handler))
    assert len(servers) == 1
    assert servers[0].preset_key == "ollama"
    assert servers[0].models == ("llama3.1",)


def test_model_prefs_store_favorites_and_mru_cap(tmp_path):
    store = ModelPrefsStore(tmp_path)
    assert store.load().favorites == () and store.load().recent == ()
    assert model_key("team", "eu/chat") != model_key("team/eu", "chat")
    assert store.toggle_favorite("ds", "deepseek-chat") is True
    assert store.load().favorites == (model_key("ds", "deepseek-chat"),)
    assert store.toggle_favorite("ds", "deepseek-chat") is False
    assert store.load().favorites == ()

    for idx in range(MAX_RECENT_MODELS + 5):
        store.record_recent("p", f"m-{idx}")
    recent = store.load().recent
    assert len(recent) == MAX_RECENT_MODELS
    assert recent[0] == model_key("p", f"m-{MAX_RECENT_MODELS + 4}")

    # Malformed or null fields degrade gracefully while preserving valid sibling fields.
    store.path.write_text(json.dumps({"favorites": None, "recent": [model_key("a", "b")]}), encoding="utf-8")
    loaded = store.load()
    assert loaded.favorites == () and loaded.recent == (model_key("a", "b"),)
    store.path.write_text(json.dumps({"favorites": "not-a-list", "recent": 123}), encoding="utf-8")
    loaded2 = store.load()
    assert loaded2.favorites == () and loaded2.recent == ()


def test_set_default_model_preserves_comments_and_validates(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# Top comment\n"
        "default_provider: a  # keep inline comment\n"
        "providers:\n"
        "  # provider a comment\n"
        "  - name: a\n"
        "    protocol: openai-chat-completions\n"
        "    model: m-a\n"
        "    api_key: test-a\n"
        "  # provider b comment\n"
        "  - name: b\n"
        "    preset: deepseek\n"
        "    api_key: test-b\n",
        encoding="utf-8",
    )
    ok, msg = set_default_model(cfg, "b", "deepseek-reasoner", home=tmp_path)
    assert ok, msg
    text = cfg.read_text(encoding="utf-8")
    assert "# Top comment" in text
    assert "# keep inline comment" in text
    assert "# provider b comment" in text
    settings = load_settings(cfg, tmp_path)
    assert settings.default_provider == "b"
    assert next(p for p in settings.providers if p.name == "b").model == "deepseek-reasoner"

    ok_missing, _ = set_default_model(cfg, "nonexistent", home=tmp_path)
    assert not ok_missing


def test_save_provider_entry_validates_before_keyring_and_deduplicates(tmp_path):
    stored: dict[str, str] = {}
    exists_calls: list[str] = []

    class FakeVault:
        def exists(self, name: str) -> bool:
            exists_calls.append(name)
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

    # Invalid config (non-loopback auth_type=none) must fail before consulting or writing keyring.
    with pytest.raises(ConfigError):
        save_provider_entry(
            tmp_path,
            {
                "name": "bad",
                "protocol": "openai-chat-completions",
                "base_url": "https://remote.example/v1",
                "model": "m",
                "auth_type": "none",
                "api_key_keyring": "bad",
            },
            pending_key="should-not-be-stored",
            vault=FakeVault(),
        )
    assert exists_calls == []
    assert stored == {}
    assert not (tmp_path / ".mosscode" / "config.yaml").exists()

    # Valid creation + comment-preserving append with name deduplication.
    path1, name1 = save_provider_entry(
        tmp_path,
        {"name": "ollama", "preset": "ollama", "model": "llama3.1"},
    )
    assert name1 == "ollama"
    path1.write_text("# user header comment\n" + path1.read_text(encoding="utf-8"), encoding="utf-8")

    path2, name2 = save_provider_entry(
        tmp_path,
        {"name": "ollama", "preset": "ollama", "model": "qwen2.5"},
        set_default=True,
    )
    assert path2 == path1 and name2 == "ollama-2"
    raw = path2.read_text(encoding="utf-8")
    assert "# user header comment" in raw
    reloaded = load_settings(path2, tmp_path)
    assert reloaded.default_provider == "ollama-2"
    assert [p.name for p in reloaded.providers] == ["ollama", "ollama-2"]


async def _send(app: MossCodeApp, pilot, text: str) -> None:
    field = app.query_one("#prompt", PromptArea)
    field.value = text
    await pilot.pause()
    await app.on_prompt_area_submitted(PromptArea.Submitted(field, text))
    await pilot.pause()


async def test_searchable_model_picker_filter_favorite_default_and_alt_m(isolated_home):
    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# Keep my comment\n"
        "default_provider: alpha\n"
        "providers:\n"
        "  - name: alpha\n"
        "    protocol: anthropic-messages\n"
        "    model: model-alpha\n"
        "    api_key: test-alpha\n"
        "  - name: beta\n"
        "    protocol: openai-chat-completions\n"
        "    model: model-beta\n"
        "    api_key: test-beta\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)
    created: list[ProviderConfig] = []

    def factory(config: ProviderConfig):
        created.append(config)
        return FakeProvider()

    app = MossCodeApp(
        Conversation(FakeProvider()),
        "alpha",
        settings=settings,
        model="model-alpha",
        provider_factory=factory,
    )

    async def no_local_servers(timeout: float = 0.5):
        return []

    app._local_server_detector = no_local_servers
    async with app.run_test(size=(100, 32)) as pilot:
        # Ctrl+D/F/N outside picker must not open picker, and Ctrl+D keeps normal forward-delete in #prompt.
        prompt = app.query_one("#prompt", PromptArea)
        prompt.value = "abcd"
        prompt.move_cursor((0, 1))
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert prompt.value == "acd"
        prompt.value = ""
        await pilot.press("ctrl+f", "ctrl+n")
        await pilot.pause()
        assert not app.query_one("#model-panel").display
        assert not app.query_one("#connect-panel").display

        # Alt+M opens unified searchable picker.
        await pilot.press("alt+m")
        await pilot.pause()
        assert app.query_one("#model-panel").display
        filter_input = app.query_one("#model-filter", Input)
        assert filter_input.display and filter_input.has_focus

        # Typing filters the option list in real time.
        filter_input.value = "beta"
        await pilot.pause()
        models = app.query_one("#models", OptionList)
        assert models.option_count == 1

        # Ctrl+F pins beta as favorite (runs off the event loop in a worker).
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        prefs = ModelPrefsStore(isolated_home).load()
        assert prefs.favorites == (model_key("beta", "model-beta"),)

        # Clearing filter puts favorited beta at index 0; down/up moves highlight while filter stays focused.
        filter_input.value = ""
        await pilot.pause()
        assert models.option_count == 2
        assert app._model_picker_filtered[0].provider_name == "beta"
        assert filter_input.has_focus

        # Highlight beta (index 0) and press Ctrl+D to switch and persist global default.
        models.highlighted = 0
        await pilot.press("ctrl+d")
        await pilot.pause()
        await pilot.pause()
        assert not app.query_one("#model-panel").display
        assert app.provider_name == "beta"
        assert load_settings(cfg, isolated_home).default_provider == "beta"
        assert "# Keep my comment" in cfg.read_text(encoding="utf-8")

        # Ctrl+N from picker jumps straight into /connect.
        await pilot.press("alt+m")
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert not app.query_one("#model-panel").display
        assert app.query_one("#connect-panel").display


async def test_connect_wizard_local_ollama_and_preset_hot_reload(isolated_home):
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
    created: list[ProviderConfig] = []

    def factory(config: ProviderConfig):
        created.append(config)
        return FakeProvider()

    app = MossCodeApp(
        Conversation(FakeProvider()),
        "start",
        settings=settings,
        model="start-model",
        provider_factory=factory,
    )

    async def fake_detect(timeout: float = 0.5):
        return [
            DetectedLocalServer(
                preset_key="ollama",
                label="Ollama",
                base_url="http://127.0.0.1:11434/v1",
                models=("llama3.1:latest", "qwen2.5:14b"),
            )
        ]

    async def fake_probe(protocol, base_url, api_key, *, auth_type="api_key", timeout=8.0):
        return ProbeResult(True, models=("deepseek-chat", "deepseek-reasoner"), latency_ms=42, detail="已连通（42ms）")

    app._local_server_detector = fake_detect
    app._provider_prober = fake_probe

    async with app.run_test(size=(100, 32)) as pilot:
        # 1. Local Ollama keyless flow via /connect.
        await _send(app, pilot, "/connect")
        await pilot.pause()
        assert app.query_one("#connect-panel").display
        assert not app.query_one("#input-row").display
        # Detected local Ollama is placed at index 0.
        assert app._connect_choices[0][0] == "local"
        await app._handle_connect_option(0)  # Select detected Ollama -> model list.
        await pilot.pause()
        assert app._connect_step == "model"
        assert any(payload == "llama3.1:latest" for action, payload in app._connect_choices if action == "pick_model")
        await app._handle_connect_option(0)  # Pick llama3.1:latest -> confirm.
        await pilot.pause()
        assert app._connect_step == "confirm"
        await app._handle_connect_option(0)  # Save & switch + set default.
        await pilot.pause()
        assert not app.query_one("#connect-panel").display
        assert app.provider_name == "ollama"
        assert app.model == "llama3.1:latest"
        assert load_settings(cfg, isolated_home).default_provider == "ollama"

        # 2. Preset flow with masked plaintext key + live probe + Esc back navigation.
        await _send(app, pilot, "/connect")
        await pilot.pause()
        ds_idx = next(i for i, (act, val) in enumerate(app._connect_choices) if act == "preset" and val == "deepseek")
        await app._handle_connect_option(ds_idx)
        await pilot.pause()
        assert app._connect_step == "credential"
        plain_idx = next(i for i, (act, val) in enumerate(app._connect_choices) if act == "cred" and val == "plain")
        await app._handle_connect_option(plain_idx)
        await pilot.pause()
        assert app._connect_step == "secret"
        assert app.query_one("#connect-input", Input).password is True

        # Pressing Esc goes back to credential step rather than closing immediately.
        await pilot.press("escape")
        await pilot.pause()
        assert app._connect_step == "credential"
        await app._handle_connect_option(plain_idx)
        await pilot.pause()
        await app._handle_connect_input("test-deepseek-secret-xyz")
        await pilot.pause()
        await pilot.pause()
        assert app._connect_step == "model"
        assert "已连通" in str(app.query_one("#connect-status", Static).render())
        await app._handle_connect_option(0)  # Pick deepseek-chat.
        await pilot.pause()
        await app._handle_connect_option(1)  # Save & switch session only (keep default ollama).
        await pilot.pause()
        assert app.provider_name == "deepseek"
        assert app.model == "deepseek-chat"
        reloaded = load_settings(cfg, isolated_home)
        assert reloaded.default_provider == "ollama"
        assert any(p.name == "deepseek" for p in reloaded.providers)


def test_fuzzy_score_literal_always_outranks_long_subsequence():
    literal = fuzzy_score("abcdefghijklmnopqrst", "abcdefghijklmnopqrst-v2")
    subseq = fuzzy_score("abcdefghijklmnopqrst", "abcdefghijklmnopqrs-t")
    assert literal is not None and subseq is not None and literal > subseq


async def test_probe_provider_enforces_https_for_credentials_and_blocks_redirects():
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        if str(request.url).startswith("https://redirect.example.com"):
            return httpx.Response(302, headers={"Location": "https://other.example.com/steal"})
        if str(request.url).startswith("http://127.0.0.1:11434"):
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)

    # Remote cleartext HTTP with API key or secret header must be rejected before making any network request.
    res_http_key = await probe_provider(
        "openai-chat-completions", "http://api.example.com/v1", "secret-12345678", transport=transport
    )
    assert not res_http_key.ok and "HTTPS" in res_http_key.error
    res_http_hdr = await probe_provider(
        "openai-chat-completions",
        "http://api.example.com/v1",
        None,
        auth_type="api_key",
        headers={"X-API-Key": "secret-hdr-123"},
        transport=transport,
    )
    assert not res_http_hdr.ok and "HTTPS" in res_http_hdr.error
    assert called == []

    # Loopback HTTP with credentials is explicitly exempted (e.g. local proxies / CC Switch).
    res_loopback_key = await probe_provider(
        "openai-chat-completions", "http://127.0.0.1:15721/v1", "local-relay-key", transport=transport
    )
    assert res_loopback_key.ok
    assert called == ["http://127.0.0.1:15721/v1/models"]

    # Redirects are never followed (prevents cross-origin x-api-key leak and loopback SSRF).
    res_redir_anth = await probe_provider(
        "anthropic-messages", "https://redirect.example.com", "anth-key-12345678", transport=transport
    )
    assert not res_redir_anth.ok and res_redir_anth.status_code == 302
    res_redir_local = await probe_provider(
        "openai-chat-completions", "http://127.0.0.1:11434/v1", None, auth_type="none", transport=transport
    )
    assert not res_redir_local.ok and res_redir_local.status_code == 302
    assert called == [
        "http://127.0.0.1:15721/v1/models",
        "https://redirect.example.com/v1/models",
        "http://127.0.0.1:11434/v1/models",
    ]


def test_keyring_collision_allocates_fresh_name_and_rolls_back_on_commit_failure(tmp_path, monkeypatch):
    stored: dict[str, str] = {"deepseek": "existing-other-config-key"}

    class TrackedVault:
        def exists(self, name: str) -> bool:
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

        def delete(self, name: str) -> None:
            stored.pop(name, None)

    vault = TrackedVault()
    # Existing keyring entry "deepseek" from another config must not be overwritten.
    path, _ = save_provider_entry(
        tmp_path,
        {"name": "deepseek", "preset": "deepseek", "api_key_keyring": "deepseek"},
        pending_key="new-key-for-this-config",
        vault=vault,
    )
    assert stored["deepseek"] == "existing-other-config-key"
    assert stored["deepseek-2"] == "new-key-for-this-config"
    assert load_settings(path, tmp_path).providers[0].api_key_keyring == "deepseek-2"

    # If os.replace fails after storing a new keyring entry, the newly written keyring entry is rolled back.
    monkeypatch.setattr("mosscode.model_discovery.os.replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        save_provider_entry(
            tmp_path,
            {"name": "kimi", "preset": "kimi", "api_key_keyring": "kimi"},
            pending_key="rolled-back-key",
            vault=vault,
        )
    assert "kimi" not in stored


def test_setup_add_preserves_concurrent_default_update(tmp_path):
    from mosscode.setup import setup, user_config
    from test_cli_product import wizard

    cfg = user_config(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: first\n"
        "providers:\n"
        "  - name: first\n"
        "    protocol: openai-chat-completions\n"
        "    model: m1\n"
        "    api_key: k1\n"
        "  - name: second\n"
        "    protocol: openai-chat-completions\n"
        "    model: m2\n"
        "    api_key: k2\n",
        encoding="utf-8",
    )

    answers = iter(["1", "", "2", ""])
    concurrent_done: list[bool] = []

    def read_with_concurrent_default(prompt: str) -> str:
        val = next(answers)
        if not concurrent_done and val == "1":
            # Simulate TUI Ctrl+D updating default_provider while `moss setup --add` waits for input.
            ok, _ = set_default_model(cfg, "second", home=tmp_path)
            assert ok
            concurrent_done.append(True)
        return val

    w, _ = wizard([], secret=lambda _: "concurrent-key")
    w.read = read_with_concurrent_default
    assert setup(tmp_path, w, add=True) == "ready"
    assert concurrent_done == [True]
    reloaded = load_settings(cfg, tmp_path)
    assert reloaded.default_provider == "second"
    assert any(p.name == "deepseek" for p in reloaded.providers)


async def test_connect_wizard_project_config_isolation_back_reset_and_malformed_url(isolated_home, tmp_path):
    project_cfg = tmp_path / ".mosscode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_yaml = yaml.safe_dump(
        {
            "default_provider": "proj",
            "providers": [
                {
                    "name": "proj",
                    "protocol": "openai-chat-completions",
                    "model": "proj-model",
                    "api_key": "proj-key",
                }
            ],
        }
    )
    project_cfg.write_text(project_yaml, encoding="utf-8")
    settings = load_settings(project_cfg, isolated_home)

    app = MossCodeApp(
        Conversation(FakeProvider()),
        "proj",
        settings=settings,
        model="proj-model",
        provider_factory=lambda _cfg: FakeProvider(),
    )

    async def fake_detect(timeout: float = 0.5):
        return [DetectedLocalServer("ollama", "Ollama", "http://127.0.0.1:11434/v1", ("llama3.1",))]

    app._local_server_detector = fake_detect
    async with app.run_test(size=(100, 32)) as pilot:
        await _send(app, pilot, "/connect")
        await pilot.pause()

        # 1. Malformed URL in custom flow shows validation error instead of raising ValueError.
        custom_idx = next(i for i, (act, _) in enumerate(app._connect_choices) if act == "custom")
        await app._handle_connect_option(custom_idx)
        await pilot.pause()
        await app._handle_connect_option(2)  # Chat Completions -> base_url.
        await pilot.pause()
        await app._handle_connect_input("http://[")
        await pilot.pause()
        assert app._connect_step == "base_url"
        assert "有效地址" in str(app.query_one("#connect-status", Static).render())

        # 2. Enter keyring credential for DeepSeek, go Back to source, pick local Ollama -> draft resets cleanly.
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app._connect_step == "source"
        ds_idx = next(i for i, (act, val) in enumerate(app._connect_choices) if act == "preset" and val == "deepseek")
        await app._handle_connect_option(ds_idx)
        await pilot.pause()
        # Enter secret on keyring/plain path then back out to source.
        plain_idx = next(i for i, (act, val) in enumerate(app._connect_choices) if act == "cred" and val == "plain")
        await app._handle_connect_option(plain_idx)
        await pilot.pause()
        app._connect_draft.pending_key = "abandoned-secret"
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app._connect_step == "source"
        await app._handle_connect_option(0)  # Detected local Ollama.
        await pilot.pause()
        assert app._connect_draft.pending_key is None

        # 3. Manual model -> confirm -> Back returns to custom_model preserving input.
        manual_idx = next(i for i, (act, _) in enumerate(app._connect_choices) if act == "manual_model")
        await app._handle_connect_option(manual_idx)
        await pilot.pause()
        await app._handle_connect_input("my-local-model")
        await pilot.pause()
        assert app._connect_step == "confirm"
        await pilot.press("escape")
        await pilot.pause()
        assert app._connect_step == "custom_model"
        assert app.query_one("#connect-input", Input).value == "my-local-model"

        # 4. Saving from a project-config session writes to user ~/.mosscode/config.yaml, not project config,
        # and keeps the project providers and project config source intact in the running session!
        await app._handle_connect_input("my-local-model")
        await pilot.pause()
        await app._handle_connect_option(0)
        await pilot.pause()
        assert project_cfg.read_text(encoding="utf-8") == project_yaml
        user_cfg = isolated_home / ".mosscode" / "config.yaml"
        assert user_cfg.is_file()
        assert load_settings(user_cfg, isolated_home).providers[0].model == "my-local-model"
        assert app.settings.source == project_cfg
        assert {p.name for p in app.settings.providers} == {"proj", "ollama"}
        assert app.provider_name == "ollama"
        assert await app.switch_model("proj")
        assert app.provider_name == "proj"


async def test_picker_session_override_target_and_failed_switch_does_not_persist_default(isolated_home):
    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: alpha\n"
        "providers:\n"
        "  - name: alpha\n"
        "    protocol: anthropic-messages\n"
        "    model: model-a\n"
        "    api_key: k-a\n"
        "  - name: broken\n"
        "    protocol: openai-chat-completions\n"
        "    model: model-broken\n"
        "    api_key: k-b\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)

    def factory(config: ProviderConfig):
        if config.name == "broken":
            raise ConfigError("broken provider rejected")
        return FakeProvider()

    app = MossCodeApp(
        Conversation(FakeProvider()),
        "alpha",
        settings=settings,
        model="model-a",
        provider_factory=factory,
    )
    async with app.run_test(size=(100, 32)) as pilot:
        # Session override to uncatalogued model-b must appear as an explicit override row.
        await _send(app, pilot, "/model :model-b")
        await pilot.pause()
        assert app.model == "model-b"

        await pilot.press("alt+m")
        await pilot.pause()
        override_row = next(c for c in app._model_picker_filtered if c.model == "model-b")
        assert override_row.override == "model-b" and override_row.is_current
        models = app.query_one("#models", OptionList)
        models.highlighted = app._model_picker_filtered.index(override_row)
        await pilot.press("ctrl+d")
        await pilot.pause()
        await pilot.pause()
        assert app.model == "model-b"
        assert load_settings(cfg, isolated_home).providers[0].model == "model-b"

        # Pressing Ctrl+D on a provider whose factory raises ConfigError must NOT overwrite default_provider.
        await pilot.press("alt+m")
        await pilot.pause()
        broken_idx = next(i for i, c in enumerate(app._model_picker_filtered) if c.provider_name == "broken")
        models.highlighted = broken_idx
        await pilot.press("ctrl+d")
        await pilot.pause()
        await pilot.pause()
        assert app.provider_name == "alpha"
        assert load_settings(cfg, isolated_home).default_provider == "alpha"


def test_keyring_lookup_error_fails_closed_and_lock_timeout_is_bounded(tmp_path):
    from filelock import FileLock

    from mosscode.credentials import KeyringError
    from mosscode.model_discovery import config_lock_path

    stored: dict[str, str] = {}

    class BrokenLookupVault:
        def exists(self, name: str) -> bool:
            raise KeyringError("backend locked")

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

    with pytest.raises(ConfigError, match="无法读取系统凭据管理器"):
        save_provider_entry(
            tmp_path,
            {"name": "deepseek", "preset": "deepseek", "api_key_keyring": "deepseek"},
            pending_key="never-written",
            vault=BrokenLookupVault(),
        )
    assert stored == {}

    cfg, _ = save_provider_entry(tmp_path, {"name": "ollama", "preset": "ollama", "model": "llama3.1"})
    held = FileLock(str(config_lock_path(cfg)))
    held.acquire()
    try:
        ok, msg = set_default_model(cfg, "ollama", home=tmp_path, lock_timeout=0.05)
        assert not ok and "其他进程" in msg
        with pytest.raises(ConfigError, match="其他进程"):
            save_provider_entry(
                tmp_path,
                {"name": "ollama", "preset": "ollama", "model": "qwen2.5"},
                lock_timeout=0.05,
            )
    finally:
        held.release()


def test_setup_handles_mkdir_failure_and_rolls_back_keyring_even_when_unlink_fails(tmp_path, monkeypatch):
    from mosscode.setup import setup
    from test_cli_product import wizard

    # 1. mkdir failure after confirmation returns "failed" instead of raising uncaught OSError.
    w_dir, out_dir = wizard(["1", "", "2", ""], secret=lambda _: "dir-fail-key")
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("denied")))
    assert setup(tmp_path / "blocked", w_dir) == "failed"
    assert any("无法创建配置目录" in str(line) for line in out_dir)
    monkeypatch.undo()

    # 2. os.replace failure + unlink failure still rolls back the newly written keyring entry.
    stored: dict[str, str] = {}

    class RollbackVault:
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

    w_rb, out_rb = wizard(["1", "", "1", ""], secret=lambda _: "rollback-secret", vault=RollbackVault())
    monkeypatch.setattr("mosscode.setup.os.replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("replace boom")))
    orig_unlink = Path.unlink

    def failing_unlink(self, *a, **kw):
        if ".yaml.tmp" in self.name:
            raise OSError("unlink boom")
        return orig_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert setup(tmp_path / "rb", w_rb) == "failed"
    assert stored == {}
    assert any("清理失败" in str(line) for line in out_rb)


async def test_concurrent_model_switches_are_serialized(isolated_home):
    import asyncio

    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: p1\n"
        "providers:\n"
        "  - name: p1\n"
        "    protocol: anthropic-messages\n"
        "    model: m1\n"
        "    api_key: k1\n"
        "  - name: p2\n"
        "    protocol: openai-chat-completions\n"
        "    model: m2\n"
        "    api_key: k2\n"
        "  - name: p3\n"
        "    protocol: openai-chat-completions\n"
        "    model: m3\n"
        "    api_key: k3\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)

    close_entered = asyncio.Event()
    allow_close = asyncio.Event()

    class GatedCloseProvider(FakeProvider):
        def __init__(self, label: str, *, gate: bool = False):
            super().__init__()
            self.label = label
            self._gate = gate

        async def close(self):
            if self._gate:
                close_entered.set()
                await allow_close.wait()
            await super().close()

    first = GatedCloseProvider("p1", gate=True)
    created: dict[str, GatedCloseProvider] = {}

    def factory(config: ProviderConfig):
        prov = GatedCloseProvider(config.name)
        created[config.name] = prov
        return prov

    conv = Conversation(first)
    app = MossCodeApp(conv, "p1", settings=settings, model="m1", provider_factory=factory)
    async with app.run_test(size=(100, 32)):
        t1 = asyncio.create_task(app.switch_model("p2"))
        await asyncio.wait_for(close_entered.wait(), timeout=2.0)
        t2 = asyncio.create_task(app.switch_model("p3"))
        await asyncio.sleep(0)
        assert "p3" not in created
        allow_close.set()
        results = await asyncio.gather(t1, t2)
        assert results == [True, True]
        assert app.provider_name == "p3"
        assert app.model == "m3"
        assert conv.provider is created["p3"]


def test_credentialed_remote_http_rejected_at_config_load_and_client_construction(tmp_path):
    from mosscode.messages import ProviderError
    from mosscode.providers import AnthropicProvider, ChatCompletionsProvider

    # 1. Config load rejects saved remote http:// providers that send credentials.
    remote_http = tmp_path / "remote-http.yaml"
    remote_http.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "insecure",
                        "protocol": "openai-chat-completions",
                        "base_url": "http://api.example.com/v1",
                        "model": "gpt-4o",
                        "api_key": "plain-secret-1234",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="https://"):
        load_settings(remote_http, tmp_path)

    # 2. Direct SDK client construction enforces the same defense-in-depth check.
    direct_chat = ProviderConfig(
        name="direct-chat",
        protocol="openai-chat-completions",
        model="gpt-4o",
        base_url="http://api.example.com/v1",
        api_key="plain-secret-1234",
    )
    with pytest.raises(ProviderError, match="https://"):
        ChatCompletionsProvider(direct_chat)

    direct_anth = ProviderConfig(
        name="direct-anth",
        protocol="anthropic-messages",
        model="claude-sonnet-4-5",
        base_url="http://api.example.com",
        api_key="plain-secret-1234",
    )
    with pytest.raises(ProviderError, match="https://"):
        AnthropicProvider(direct_anth)

    # 3. Documented loopback exception allows local proxies over loopback HTTP with credentials.
    loopback_cfg = tmp_path / "loopback-http.yaml"
    loopback_cfg.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "local-relay",
                        "protocol": "openai-chat-completions",
                        "base_url": "http://127.0.0.1:15721/v1",
                        "model": "gpt-4o",
                        "api_key": "local-relay-secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings(loopback_cfg, tmp_path)
    assert loaded.providers[0].base_url == "http://127.0.0.1:15721/v1"


def test_lock_open_permission_error_handled_and_cleans_staged_temp(tmp_path, monkeypatch):
    from filelock import FileLock

    cfg, _ = save_provider_entry(tmp_path, {"name": "ollama", "preset": "ollama", "model": "llama3.1"})

    # 1. set_default_model catches PermissionError on sibling lock open and returns (False, message).
    orig_acquire = FileLock.acquire

    def deny_config_lock(self, *args, **kwargs):
        if str(self.lock_file).endswith(".config.yaml.lock"):
            raise PermissionError("read-only lock dir")
        return orig_acquire(self, *args, **kwargs)

    monkeypatch.setattr(FileLock, "acquire", deny_config_lock)
    ok, msg = set_default_model(cfg, "ollama", home=tmp_path)
    assert not ok and "无法打开配置锁文件" in msg
    monkeypatch.undo()

    # 2. save_provider_entry cleans staged temp YAML when keyring lock raises PermissionError (new & append).
    def deny_keyring_lock(self, *args, **kwargs):
        if str(self.lock_file).endswith(".keyring.lock"):
            raise PermissionError("keyring lock denied")
        return orig_acquire(self, *args, **kwargs)

    monkeypatch.setattr(FileLock, "acquire", deny_keyring_lock)
    fresh_home = tmp_path / "fresh"
    with pytest.raises(PermissionError, match="keyring lock denied"):
        save_provider_entry(
            fresh_home,
            {"name": "deepseek", "preset": "deepseek", "api_key_keyring": "deepseek"},
            pending_key="secret-new",
        )
    assert list((fresh_home / ".mosscode").glob(".config-*.yaml.tmp")) == []

    with pytest.raises(PermissionError, match="keyring lock denied"):
        save_provider_entry(
            tmp_path,
            {"name": "deepseek", "preset": "deepseek", "api_key_keyring": "deepseek"},
            pending_key="secret-append",
        )
    assert list((tmp_path / ".mosscode").glob(".config-*.yaml.tmp")) == []


async def test_connect_post_commit_reload_failure_retries_without_duplicate_append(isolated_home, monkeypatch):
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
    stored: dict[str, str] = {}

    class TrackedVault:
        def exists(self, name: str) -> bool:
            return name in stored

        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

        def delete(self, name: str) -> None:
            stored.pop(name, None)

    app = MossCodeApp(
        Conversation(FakeProvider()),
        "start",
        settings=settings,
        model="start-model",
        provider_factory=lambda _cfg: FakeProvider(),
    )
    app._connect_vault = TrackedVault()

    async def no_local(timeout: float = 0.5):
        return []

    async def fake_probe(protocol, base_url, api_key, **kwargs):
        return ProbeResult(True, models=("deepseek-chat",), latency_ms=10, detail="ok")

    app._local_server_detector = no_local
    app._provider_prober = fake_probe

    import mosscode.tui_connect as tui_connect_mod

    orig_load = tui_connect_mod.load_settings
    fail_once = {"done": False}

    def flaky_load(path, home=None):
        if not fail_once["done"]:
            fail_once["done"] = True
            raise OSError("transient read error")
        return orig_load(path, home)

    async with app.run_test(size=(100, 32)) as pilot:
        await _send(app, pilot, "/connect")
        await pilot.pause()
        ds_idx = next(i for i, (act, val) in enumerate(app._connect_choices) if act == "preset" and val == "deepseek")
        await app._handle_connect_option(ds_idx)
        await pilot.pause()
        # Use keyring storage so duplicate allocation would be detectable.
        app._connect_draft.cred_method = "keyring"
        app._connect_draft.pending_key = "ds-secret-123456"
        app._connect_draft.model = "deepseek-chat"
        app._connect_step = "confirm"
        app._render_connect_confirm()

        monkeypatch.setattr(tui_connect_mod, "load_settings", flaky_load)
        await app._handle_connect_option(0)  # Save & switch -> commits, then load_settings raises once.
        await pilot.pause()
        assert app._connect_step == "reload_failed"
        assert app._connect_committed == (cfg, "deepseek")
        assert list(stored) == ["deepseek"]

        # Retrying reloads and switches without appending deepseek-2 or allocating a second keyring entry.
        await app._handle_connect_option(0)  # retry_reload
        await pilot.pause()
        assert not app.query_one("#connect-panel").display
        assert app.provider_name == "deepseek"
        reloaded = orig_load(cfg, isolated_home)
        assert [p.name for p in reloaded.providers] == ["start", "deepseek"]
        assert list(stored) == ["deepseek"]


async def test_picker_follow_override_not_reported_saved_and_external_append_selectable(isolated_home):
    # Prepare a fake Claude Code settings file for follow: claude-code.
    claude_dir = isolated_home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "cc-secret-12345678",
                    "ANTHROPIC_MODEL": "claude-sonnet-4-5",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: alpha\n"
        "providers:\n"
        "  - name: alpha\n"
        "    protocol: anthropic-messages\n"
        "    model: model-alpha\n"
        "    api_key: k-alpha\n"
        "  - name: cc\n"
        "    follow: claude-code\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)
    app = MossCodeApp(
        Conversation(FakeProvider()),
        "alpha",
        settings=settings,
        model="model-alpha",
        provider_factory=lambda _cfg: FakeProvider(),
    )
    async with app.run_test(size=(100, 32)) as pilot:
        # 1. Switch to follow provider with a session model override, then press Ctrl+D on that override row.
        assert await app.switch_model("cc", "override-m")
        await pilot.press("alt+m")
        await pilot.pause()
        override_row = next(
            c for c in app._model_picker_filtered if c.provider_name == "cc" and c.override == "override-m"
        )
        models = app.query_one("#models", OptionList)
        models.highlighted = app._model_picker_filtered.index(override_row)
        await pilot.press("ctrl+d")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Default provider must NOT have been changed when rejecting follow override persistence.
        assert load_settings(cfg, isolated_home).default_provider == "alpha"

        # 2. Another process appends provider `ext` before the user sets `alpha` as default via Ctrl+D.
        save_provider_entry(
            isolated_home,
            {
                "name": "ext",
                "protocol": "openai-chat-completions",
                "model": "ext-model",
                "api_key": "k-ext",
            },
            config_path=cfg,
        )
        await pilot.press("alt+m")
        await pilot.pause()
        alpha_idx = next(
            i for i, c in enumerate(app._model_picker_filtered) if c.provider_name == "alpha" and c.override is None
        )
        models.highlighted = alpha_idx
        await pilot.press("ctrl+d")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Newly appended `ext` must be present in settings.providers and immediately selectable.
        assert any(p.name == "ext" for p in app.settings.providers)
        assert await app.switch_model("ext")
        assert app.provider_name == "ext"


async def test_switch_model_completes_when_old_provider_close_raises(isolated_home):
    from mosscode.messages import ProviderError

    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: p1\n"
        "providers:\n"
        "  - name: p1\n"
        "    protocol: anthropic-messages\n"
        "    model: m1\n"
        "    api_key: k1\n"
        "  - name: p2\n"
        "    protocol: openai-chat-completions\n"
        "    model: m2\n"
        "    api_key: k2\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)

    class FailingCloseProvider(FakeProvider):
        async def close(self):
            raise ProviderError("close transport error")

    conv = Conversation(FailingCloseProvider())
    app = MossCodeApp(
        conv,
        "p1",
        settings=settings,
        model="m1",
        provider_factory=lambda _cfg: FakeProvider(),
    )
    async with app.run_test(size=(100, 32)):
        assert await app.switch_model("p2")
        assert app.provider_name == "p2"
        assert app.model == "m2"


def test_acp_connect_hint_mentions_reconfirming_project_config():
    from mosscode.acp.formatting import _EXCLUDED_HINTS

    hint = _EXCLUDED_HINTS["connect"]
    assert "moss" in hint
    assert "重新确认" in hint


def test_sdk_env_base_url_enforces_https_and_allows_loopback(monkeypatch):
    from mosscode.messages import ProviderError
    from mosscode.providers import AnthropicProvider, ChatCompletionsProvider, ResponsesProvider

    anth_cfg = ProviderConfig(
        name="anth",
        protocol="anthropic-messages",
        model="claude-sonnet-4-5",
        base_url=None,
        api_key="anth-secret-1234",
    )
    chat_cfg = ProviderConfig(
        name="chat",
        protocol="openai-chat-completions",
        model="gpt-4o",
        base_url=None,
        api_key="openai-secret-1234",
    )
    resp_cfg = ProviderConfig(
        name="resp",
        protocol="openai-responses",
        model="gpt-4o",
        base_url=None,
        api_key="openai-secret-1234",
    )

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://api.example.com")
    with pytest.raises(ProviderError, match="https://"):
        AnthropicProvider(anth_cfg)

    monkeypatch.setenv("OPENAI_BASE_URL", "http://api.example.com/v1")
    with pytest.raises(ProviderError, match="https://"):
        ChatCompletionsProvider(chat_cfg)
    with pytest.raises(ProviderError, match="https://"):
        ResponsesProvider(resp_cfg)

    # Loopback env base URLs remain allowed by the documented loopback exemption.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:15721")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:15721/v1")
    assert AnthropicProvider(anth_cfg).client is not None
    assert ChatCompletionsProvider(chat_cfg).client is not None


async def test_recent_model_recording_preserves_switch_order(isolated_home):
    import threading

    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: p1\n"
        "providers:\n"
        "  - name: p1\n"
        "    protocol: anthropic-messages\n"
        "    model: m1\n"
        "    api_key: k1\n"
        "  - name: p2\n"
        "    protocol: openai-chat-completions\n"
        "    model: m2\n"
        "    api_key: k2\n"
        "  - name: p3\n"
        "    protocol: openai-chat-completions\n"
        "    model: m3\n"
        "    api_key: k3\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)
    first_entered = threading.Event()
    allow_first = threading.Event()
    store = ModelPrefsStore(isolated_home)
    orig_record = store.record_recent
    order: list[str] = []

    def gated_record(provider_name: str, model: str) -> None:
        if provider_name == "p2":
            first_entered.set()
            assert allow_first.wait(timeout=2.0)
        order.append(provider_name)
        orig_record(provider_name, model)

    store.record_recent = gated_record  # type: ignore[method-assign]
    app = MossCodeApp(
        Conversation(FakeProvider()),
        "p1",
        settings=settings,
        model="m1",
        provider_factory=lambda _cfg: FakeProvider(),
    )
    app._prefs_store = lambda: store  # type: ignore[method-assign]
    async with app.run_test(size=(100, 32)):
        assert await app.switch_model("p2")
        assert first_entered.wait(timeout=2.0)
        assert await app.switch_model("p3")
        allow_first.set()
        await app.workers.wait_for_complete()
        assert order == ["p2", "p3"]
        assert store.load().recent[:2] == (model_key("p3", "m3"), model_key("p2", "m2"))


async def test_reloaded_endpoint_change_rebuilds_active_client_and_hides_new_env_var(isolated_home, monkeypatch):
    from mosscode.credentials import child_environment

    cfg = isolated_home / ".mosscode" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "default_provider: alpha\n"
        "providers:\n"
        "  - name: alpha\n"
        "    protocol: openai-chat-completions\n"
        "    base_url: https://old.example.com/v1\n"
        "    model: m-alpha\n"
        "    api_key: k-old\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, isolated_home)
    built: list[ProviderConfig] = []

    def factory(config: ProviderConfig):
        built.append(config)
        return FakeProvider()

    app = MossCodeApp(
        Conversation(FakeProvider()),
        "alpha",
        settings=settings,
        model="m-alpha",
        provider_factory=factory,
    )
    async with app.run_test(size=(100, 32)) as pilot:
        # Edit alpha's endpoint and env credential on disk while alpha is already active.
        monkeypatch.setenv("SERVICE_ACCESS", "opaque-non-heuristic-val")
        cfg.write_text(
            "default_provider: alpha\n"
            "providers:\n"
            "  - name: alpha\n"
            "    protocol: openai-chat-completions\n"
            "    base_url: https://new.example.com/v1\n"
            "    model: m-alpha\n"
            "    api_key_env: SERVICE_ACCESS\n",
            encoding="utf-8",
        )
        await pilot.press("alt+m")
        await pilot.pause()
        await pilot.press("ctrl+d")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(built) == 1
        assert built[0].base_url == "https://new.example.com/v1"
        assert built[0].api_key_env == "SERVICE_ACCESS"
        # Newly loaded api_key_env must be stripped from subprocess environments even with a non-heuristic name.
        env_out = child_environment({"SERVICE_ACCESS": "opaque-non-heuristic-val", "PATH": "/bin"})
        assert env_out is not None and "SERVICE_ACCESS" not in env_out
