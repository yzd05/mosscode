"""Tests for cold-start lazy imports, presets, keyless loopback, model discovery, searchable /model, and /connect."""

from __future__ import annotations

import json
import subprocess
import sys

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
    assert store.toggle_favorite("ds", "deepseek-chat") is True
    assert store.load().favorites == ("ds/deepseek-chat",)
    assert store.toggle_favorite("ds", "deepseek-chat") is False
    assert store.load().favorites == ()

    for idx in range(MAX_RECENT_MODELS + 5):
        store.record_recent("p", f"m-{idx}")
    recent = store.load().recent
    assert len(recent) == MAX_RECENT_MODELS
    assert recent[0] == f"p/m-{MAX_RECENT_MODELS + 4}"


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

    class FakeVault:
        def store(self, name: str, secret: str) -> None:
            stored[name] = secret

    # Invalid config (non-loopback auth_type=none) must fail before touching keyring or creating file.
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
    async with app.run_test(size=(100, 32)) as pilot:
        # Ctrl+D/F/N outside picker must not open or trigger picker actions.
        await pilot.press("ctrl+f", "ctrl+d", "ctrl+n")
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

        # Ctrl+F pins beta as favorite.
        await pilot.press("ctrl+f")
        await pilot.pause()
        prefs = ModelPrefsStore(isolated_home).load()
        assert prefs.favorites == ("beta/model-beta",)

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
