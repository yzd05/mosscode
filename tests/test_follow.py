"""Providers that follow Claude Code's or Codex's settings, as CC Switch and Magpie write them."""

import json

import pytest

from mosscode.config import ConfigError, ProviderConfig, Settings, load_settings, project_summary, refreshed
from mosscode.conversation import Conversation
from mosscode.credentials import forget_all, plaintext_notice, protect, redact
from mosscode.follow import detected, resolve
from mosscode.setup import setup, user_config
from mosscode.tui import MossCodeApp
from test_cli_product import wizard
from test_conversation import FakeProvider

TOKEN = "sk-follow-token-0123456789"
KEY = "sk-codex-key-0123456789"
VARIABLES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    # The real Claude Code or Codex settings of whoever runs the tests never take part.
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    yield
    forget_all()


def claude_settings(home, env=None, **extra):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env or {}, **extra}), encoding="utf-8")
    return path


def codex_settings(home, toml, auth=None):
    root = home / ".codex"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(toml, encoding="utf-8")
    if auth is not None:
        (root / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def user_file(tmp_path, *entries):
    path = tmp_path / "config.yaml"
    lines = ["providers:"]
    for entry in entries:
        lines.append(f"  - name: {entry['name']}")
        lines += [f"    {key}: {value}" for key, value in entry.items() if key != "name"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# Claude Code ------------------------------------------------------------------


def test_claude_code_settings_written_by_cc_switch_become_the_provider(tmp_path):
    claude_settings(
        tmp_path,
        {
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            "ANTHROPIC_MODEL": "deepseek-flash[1M]",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Team: moss\nbroken line\nX-Trace: 1",
        },
    )
    config = load_settings(user_file(tmp_path, {"name": "cc", "follow": "claude-code"}), tmp_path).providers[0]
    assert (config.protocol, config.base_url, config.model) == (
        "anthropic-messages",
        "https://api.deepseek.com/anthropic",
        "deepseek-flash",
    )
    assert config.auth_type == "bearer" and config.api_key == TOKEN
    assert config.context_window == 1_000_000  # [1M] is Claude Code's mark for the 1M window.
    assert config.headers == {"X-Team": "moss", "X-Trace": "1"}
    assert config.follow == "claude-code" and TOKEN not in repr(config)


def test_claude_code_aliases_map_through_its_default_models(tmp_path):
    env = {"ANTHROPIC_API_KEY": TOKEN, "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3", "ANTHROPIC_MODEL": "opus[1m]"}
    claude_settings(tmp_path, env)
    found = resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})
    assert found["auth_type"] == "api_key" and "base_url" not in found
    assert (found["model"], found["context_window"]) == ("glm-5.3", 1_000_000)
    # The settings' own model field, and sonnet when nothing is chosen.
    claude_settings(tmp_path, {"ANTHROPIC_API_KEY": TOKEN, "ANTHROPIC_DEFAULT_HAIKU_MODEL": "kimi-k3.1"}, model="haiku")
    assert resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})["model"] == "kimi-k3.1"
    claude_settings(tmp_path, {"ANTHROPIC_API_KEY": TOKEN, "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen-plus"})
    assert resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})["model"] == "qwen-plus"


def test_an_alias_claude_code_does_not_map_needs_a_model_in_the_entry(tmp_path):
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN}, model="opus")
    with pytest.raises(ConfigError, match="没有可用的模型名"):
        resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})
    found = resolve({"name": "cc", "follow": "claude-code", "model": "claude-opus-5-5"}, tmp_path, {})
    assert found["model"] == "claude-opus-5-5"


def test_the_process_environment_fills_what_the_settings_leave_out(tmp_path):
    claude_settings(tmp_path, {"ANTHROPIC_MODEL": "deepseek-flash"})
    found = resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN})
    assert found["api_key"] == TOKEN
    # CLAUDE_CONFIG_DIR moves the settings, as it does for Claude Code.
    elsewhere = tmp_path / "elsewhere"
    claude_settings(elsewhere, {"ANTHROPIC_API_KEY": TOKEN, "ANTHROPIC_MODEL": "glm-5.3"})
    environ = {"CLAUDE_CONFIG_DIR": str(elsewhere / ".claude")}
    assert resolve({"name": "cc", "follow": "claude-code"}, tmp_path, environ)["model"] == "glm-5.3"


def test_claude_code_without_a_key_or_settings_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="找不到 .*CC Switch"):
        resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})
    claude_settings(tmp_path, {}, apiKeyHelper="~/bin/key.sh")
    with pytest.raises(ConfigError, match="账号登录.*apiKeyHelper"):
        resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="有效的 JSON"):
        resolve({"name": "cc", "follow": "claude-code"}, tmp_path, {})


@pytest.mark.parametrize("field", ["base_url", "api_key", "api_key_env", "headers", "protocol", "auth_type", "preset"])
def test_a_following_entry_cannot_redirect_the_followed_key(tmp_path, field):
    # A project's file could otherwise send the key Claude Code holds to an address of its choosing.
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash"})
    with pytest.raises(ConfigError, match=f"不能填写 {field}"):
        resolve({"name": "cc", "follow": "claude-code", field: "https://evil.example"}, tmp_path, {})


def test_unknown_follow_source_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="follow 需为 claude-code、codex"):
        load_settings(user_file(tmp_path, {"name": "x", "follow": "cursor"}), tmp_path)


def test_a_following_entry_may_tune_the_rest(tmp_path):
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash[1m]"})
    entry = {"name": "cc", "follow": "claude-code", "context_window": 200000, "max_output_tokens": 8192}
    config = load_settings(user_file(tmp_path, entry), tmp_path).providers[0]
    assert (config.context_window, config.max_output_tokens) == (200000, 8192)


# Codex ------------------------------------------------------------------------


def test_codex_chat_provider_with_a_profile_and_its_key_variable(tmp_path):
    codex_settings(
        tmp_path,
        'model = "gpt-5.6"\nprofile = "ds"\n'
        '[profiles.ds]\nmodel = "deepseek-flash"\nmodel_provider = "deepseek"\n'
        '[model_providers.deepseek]\nname = "DeepSeek"\nbase_url = "https://api.deepseek.com"\n'
        'wire_api = "chat"\nenv_key = "DEEPSEEK_API_KEY"\n'
        'http_headers = { "X-Team" = "moss" }\nenv_http_headers = { "X-Org" = "ORG_ID" }\n',
    )
    found = resolve({"name": "cx", "follow": "codex"}, tmp_path, {"DEEPSEEK_API_KEY": KEY, "ORG_ID": "o-1"})
    assert found["protocol"] == "openai-chat-completions" and found["base_url"] == "https://api.deepseek.com"
    assert (found["model"], found["api_key"]) == ("deepseek-flash", KEY)
    assert found["headers"] == {"X-Team": "moss", "X-Org": "o-1"}
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY，它没有设置"):
        resolve({"name": "cx", "follow": "codex"}, tmp_path, {})


def test_codex_through_cc_switch_reads_auth_json(tmp_path):
    codex_settings(
        tmp_path,
        'model_provider = "ccswitch"\nmodel = "kimi-k3.1-pro"\n'
        '[model_providers.ccswitch]\nbase_url = "http://127.0.0.1:15721/v1"\nwire_api = "responses"\n',
        {"OPENAI_API_KEY": KEY},
    )
    config = load_settings(user_file(tmp_path, {"name": "cx", "follow": "codex"}), tmp_path).providers[0]
    assert (config.protocol, config.base_url, config.model) == (
        "openai-responses",
        "http://127.0.0.1:15721/v1",
        "kimi-k3.1-pro",
    )
    assert config.api_key == KEY and config.auth_type == "api_key"


def test_codex_builtin_openai_and_its_limits(tmp_path):
    codex_settings(tmp_path, 'model = "gpt-5.6"\n', {"OPENAI_API_KEY": KEY})
    found = resolve({"name": "cx", "follow": "codex"}, tmp_path, {})
    assert found["protocol"] == "openai-responses" and "base_url" not in found
    codex_settings(tmp_path, 'model = "gpt-5.6"\n', {"OPENAI_API_KEY": None, "tokens": {"id_token": "x"}})
    with pytest.raises(ConfigError, match="ChatGPT 账号登录"):
        resolve({"name": "cx", "follow": "codex"}, tmp_path, {})
    codex_settings(tmp_path, 'model = "llama3"\nmodel_provider = "ollama"\n', {"OPENAI_API_KEY": KEY})
    with pytest.raises(ConfigError, match="内置的 ollama"):
        resolve({"name": "cx", "follow": "codex"}, tmp_path, {})
    codex_settings(tmp_path, 'model_provider = "x"\n[model_providers.x]\nwire_api = "grpc"\n', {})
    with pytest.raises(ConfigError, match="wire_api"):
        resolve({"name": "cx", "follow": "codex"}, tmp_path, {})
    codex_settings(tmp_path, "model = [", {})
    with pytest.raises(ConfigError, match="有效的 TOML"):
        resolve({"name": "cx", "follow": "codex"}, tmp_path, {})


def test_detected_lists_the_tools_whose_settings_work(tmp_path):
    assert detected(tmp_path, {}) == []
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash"})
    codex_settings(tmp_path, 'model = "gpt-5.6"\n', {"OPENAI_API_KEY": KEY})
    assert detected(tmp_path, {}) == ["claude-code", "codex"]


# Reading again, and keeping the key out of sight -------------------------------


def test_refreshed_reads_a_switch_made_since_loading(tmp_path):
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash"})
    config = load_settings(user_file(tmp_path, {"name": "cc", "follow": "claude-code"}), tmp_path).providers[0]
    claude_settings(
        tmp_path,
        {"ANTHROPIC_AUTH_TOKEN": "sk-other-token-9876543210", "ANTHROPIC_MODEL": "glm-5.3"},
    )
    again = refreshed(config, tmp_path)
    assert (again.model, again.api_key, again.name, again.follow) == (
        "glm-5.3",
        "sk-other-token-9876543210",
        "cc",
        "claude-code",
    )
    plain = ProviderConfig("p", "anthropic-messages", "m")
    assert refreshed(plain, tmp_path) is plain


def test_the_followed_key_is_protected_and_never_reported_as_plaintext(tmp_path):
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash"})
    settings = load_settings(user_file(tmp_path, {"name": "cc", "follow": "claude-code"}), tmp_path)
    protect(settings)
    assert TOKEN not in redact(f"echo {TOKEN}")
    assert plaintext_notice(settings, tmp_path) is None  # The file holds no key.
    summary = project_summary(settings)
    assert "Claude Code 的设置中的密钥" in summary and TOKEN not in summary


# moss setup -------------------------------------------------------------------


def test_setup_offers_the_detected_tool_first_and_writes_no_key(tmp_path):
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash[1M]"})
    w, out = wizard(["1", ""])
    assert setup(tmp_path, w) == "ready"
    text = user_config(tmp_path).read_text(encoding="utf-8")
    assert "follow: claude-code" in text and TOKEN not in text and "api_key" not in text
    assert any("跟随 Claude Code 的设置" in line for line in out)
    assert any("anthropic-messages / deepseek-flash" in line for line in out)
    assert not any(TOKEN in line for line in out)


def test_setup_with_a_preset_asks_only_for_the_model_and_the_key(tmp_path):
    # Nothing detected: DeepSeek is the first choice; the model default is kept; the key is an environment variable.
    w, out = wizard(["1", "", "", "", ""], environ={"DEEPSEEK_API_KEY": "set"})
    assert setup(tmp_path, w) == "ready"
    text = user_config(tmp_path).read_text(encoding="utf-8")
    assert "preset: deepseek" in text and "base_url" not in text and "protocol" not in text
    config = load_settings(user_config(tmp_path), tmp_path).providers[0]
    assert (config.protocol, config.base_url, config.model, config.api_key_env) == (
        "openai-chat-completions",
        "https://api.deepseek.com",
        "deepseek-flash",
        "DEEPSEEK_API_KEY",
    )
    assert any("上下文窗口 按模型目录" in line for line in out)


def test_setup_warns_about_a_model_the_catalog_does_not_know(tmp_path):
    w, out = wizard(["3", "glm-unknown-9", "", "", ""], environ={"ZHIPUAI_API_KEY": "set"})
    assert setup(tmp_path, w) == "ready"
    assert any("模型目录中没有 glm-unknown-9" in line for line in out)


# /model -----------------------------------------------------------------------


async def test_model_reads_the_followed_settings_again(tmp_path, monkeypatch):
    claude_settings(tmp_path, {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "deepseek-flash"})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    settings = load_settings(user_file(tmp_path, {"name": "cc", "follow": "claude-code"}), tmp_path)
    made = []

    def factory(config):
        made.append(config)
        return FakeProvider()

    conversation = Conversation(FakeProvider())
    app = MossCodeApp(conversation, "cc", settings=settings, provider_factory=factory)
    async with app.run_test() as pilot:
        # Switched in CC Switch while MossCode runs.
        claude_settings(
            tmp_path, {"ANTHROPIC_AUTH_TOKEN": "sk-switched-token-0123456789", "ANTHROPIC_MODEL": "glm-5.3"}
        )
        await app.switch_model("cc")
        await pilot.pause()
        assert [config.model for config in made] == ["glm-5.3"]
        assert app.settings.providers[0].model == "glm-5.3"
        assert "sk-switched-token-0123456789" not in redact("sk-switched-token-0123456789")
        notes = [str(note.render()) for note in app.query(".local-note")]
        assert any("已重新读取 Claude Code 的设置" in note and "glm-5.3" in note for note in notes)
        # A broken file leaves the running provider as it was.
        (tmp_path / ".claude" / "settings.json").write_text("{", encoding="utf-8")
        await app.switch_model("cc")
        await pilot.pause()
        assert "切换失败" in str(app.query(".local-note")[-1].render())
        assert len(made) == 1 and app.settings.providers[0].model == "glm-5.3"


def test_a_project_summary_names_where_a_followed_key_comes_from():
    config = ProviderConfig("cc", "anthropic-messages", "glm-5.3", api_key=TOKEN, follow="claude-code")
    summary = project_summary(Settings((config,), "cc"))
    assert "Claude Code 的设置中的密钥" in summary and "写在这份文件里的密钥" not in summary


def test_doctor_reports_a_followed_key_without_showing_it():
    from mosscode.doctor import OK, _credential

    config = ProviderConfig("cc", "anthropic-messages", "glm-5.3", api_key=TOKEN, follow="codex")
    level, shown, fix = _credential(config)
    assert (level, fix) == (OK, "") and "跟随 Codex 的设置" in shown and TOKEN not in shown
