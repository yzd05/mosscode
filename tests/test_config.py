import pytest
import yaml

from mosscode.catalog import encode, save, slim
from mosscode.config import ConfigError, ProviderConfig, load_settings
from mosscode.providers import create_provider


def write_config(tmp_path, **overrides):
    provider = {
        "name": "custom",
        "protocol": "anthropic-messages",
        "model": "arbitrary-model",
        "api_key_env": "MOSS_TEST_KEY",
    }
    provider.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"providers": [provider]}), encoding="utf-8")
    return path


@pytest.mark.parametrize("protocol", ["anthropic-messages", "openai-responses", "openai-chat-completions"])
async def test_custom_endpoint_model_and_credential_reach_sdk(tmp_path, monkeypatch, protocol):
    monkeypatch.setenv("MOSS_TEST_KEY", "private-test-key")
    config = load_settings(
        write_config(tmp_path, protocol=protocol, base_url="https://gateway.example/custom/v1")
    ).select()
    provider = create_provider(config)
    assert str(provider.client.base_url) == "https://gateway.example/custom/v1/"
    assert provider.client.api_key == "private-test-key"
    assert config.model == "arbitrary-model"
    assert provider.client.max_retries == 0
    await provider.close()


async def test_bearer_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_TEST_KEY", "private-bearer")
    provider = create_provider(load_settings(write_config(tmp_path, auth_type="bearer")).select())
    assert provider.client.auth_token == "private-bearer"
    assert provider.client.api_key is None
    await provider.close()


def test_missing_env_is_safe_and_listing_needs_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MOSS_TEST_KEY", raising=False)
    config = load_settings(write_config(tmp_path)).select()
    with pytest.raises(ConfigError, match="MOSS_TEST_KEY"):
        config.credential()


def test_literal_key_not_in_repr(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n- name: test\n  protocol: openai\n  model: x\n  api_key: private-test-key\n", encoding="utf-8"
    )
    config = load_settings(path).select()
    assert config.credential() == "private-test-key"
    assert "private-test-key" not in repr(config)
    assert config.protocol == "openai-responses"


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "unknown"},
        {"base_url": "file:///local"},
        {"timeout": float("inf")},
        {"max_output_tokens": True},
        {"stream_usage": "false"},
        {"prompt_cache": "true"},
        {"images": "yes"},
        {"thinking": {"type": "enabled", "budget_tokens": 0}},
        {"thinking": {"type": "adaptive", "budget_tokens": 2000}},
        {"protocol": "openai-responses", "thinking": True},
        {"api_key": "secret", "api_key_env": "MOSS_TEST_KEY"},
    ],
)
def test_invalid_configuration_fails_before_request(tmp_path, change):
    with pytest.raises(ConfigError):
        load_settings(write_config(tmp_path, **change))


@pytest.mark.parametrize(
    "protocol,images,accepts",
    [
        ("anthropic-messages", None, True),
        ("openai-responses", None, True),
        ("openai-chat-completions", None, False),
        ("openai-chat-completions", True, True),
        ("anthropic-messages", False, False),
    ],
)
def test_image_support_defaults_by_protocol_and_the_setting_wins(tmp_path, protocol, images, accepts):
    options = {"protocol": protocol} | ({} if images is None else {"images": images})
    config = load_settings(write_config(tmp_path, **options)).select()
    assert config.images is images and config.accepts_images is accepts


def test_thinking_true_maps_to_adaptive(tmp_path):
    config = load_settings(write_config(tmp_path, thinking=True)).select()
    assert config.thinking == {"type": "adaptive"}


@pytest.mark.parametrize("direct", [False, True])
def test_key_in_environment_name_gets_clear_error_without_disclosing_key(tmp_path, direct):
    secret = "sk-private-test-key"
    with pytest.raises(ConfigError) as exc:
        if direct:
            config = ProviderConfig("custom", "openai-chat-completions", "model", api_key_env=secret)
            assert secret not in repr(config)
            config.credential()
        else:
            load_settings(write_config(tmp_path, api_key_env=secret))
    assert "环境变量名称" in str(exc.value)
    assert "直接填写密钥请使用 api_key" in str(exc.value)
    assert secret not in str(exc.value)


@pytest.mark.parametrize(
    "default_config,expected",
    [
        ({}, None),
        ({"default_provider": None}, None),
        ({"default_provider": "custom"}, "custom"),
    ],
)
def test_loading_preserves_explicit_default_and_falls_back_to_first(tmp_path, default_config, expected):
    path = write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.update(default_config)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_settings(path)
    assert settings.default_provider == expected
    assert settings.select().name == "custom"


@pytest.mark.parametrize("old", [None, "", "You are MossCode, a helpful coding assistant."])
def test_default_and_legacy_config_use_rewritten_modules(tmp_path, old):
    from mosscode.prompts import DEFAULT_SYSTEM_PROMPT

    path = write_config(tmp_path, prompt_cache=True)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if old is not None:
        raw["system_prompt"] = old
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_settings(path)
    assert settings.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert settings.select().prompt_cache is True


def use_catalog(models):
    """Install a small catalog as the user's copy, inside the isolated test home."""
    raw = {
        name: {
            "mode": "chat",
            "litellm_provider": provider,
            "max_input_tokens": window,
            "max_output_tokens": output,
            **({} if vision is None else {"supports_vision": vision}),
        }
        for name, (provider, window, output, vision) in models.items()
    }
    save(encode(slim(raw, "2026-01-02")))


DEEPSEEK = {"protocol": "anthropic-messages", "base_url": "https://api.deepseek.com/anthropic"}


def test_catalog_fills_window_output_and_images_the_config_leaves_out(tmp_path):
    use_catalog({"deepseek/deepseek-v4-flash": ("deepseek", 1_000_000, 393_216, False)})
    config = load_settings(write_config(tmp_path, model="deepseek-v4-flash", **DEEPSEEK)).select()
    assert (config.context_window, config.max_output_tokens, config.images) == (1_000_000, 32_000, False)
    assert config.from_catalog == ("max_output_tokens", "context_window", "images")
    assert config.catalog.name == "deepseek/deepseek-v4-flash" and config.window_note() is None
    assert config.budget().configured and config.budget().window == 1_000_000


def test_configured_values_win_over_the_catalog(tmp_path):
    use_catalog({"deepseek/deepseek-v4-flash": ("deepseek", 1_000_000, 8_192, True)})
    config = load_settings(
        write_config(
            tmp_path,
            model="deepseek-v4-flash",
            context_window=64_000,
            max_output_tokens=4_000,
            images=False,
            **DEEPSEEK,
        )
    ).select()
    assert (config.context_window, config.max_output_tokens, config.images) == (64_000, 4_000, False)
    assert config.from_catalog == () and config.catalog is not None


def test_output_limit_is_the_smaller_of_the_catalog_and_the_cap(tmp_path):
    use_catalog({"deepseek/deepseek-v4-flash": ("deepseek", 1_000_000, 8_192, None)})
    config = load_settings(write_config(tmp_path, model="deepseek-v4-flash", **DEEPSEEK)).select()
    assert config.max_output_tokens == 8_192 and config.images is None and config.accepts_images


def test_unknown_model_keeps_the_old_defaults_and_says_how_to_fix_it(tmp_path):
    use_catalog({"deepseek/deepseek-v4-flash": ("deepseek", 1_000_000, 393_216, True)})
    config = load_settings(write_config(tmp_path, model="deepseek-flash-alias", **DEEPSEEK)).select()
    assert (config.context_window, config.max_output_tokens, config.catalog) == (None, 16_384, None)
    note = config.window_note()
    assert "模型目录中没有 deepseek-flash-alias，按 128K 估算" in note and "catalog_model" in note
    assert not config.budget().configured


def test_catalog_model_names_the_entry_for_an_alias(tmp_path):
    use_catalog({"deepseek/deepseek-v4-flash": ("deepseek", 1_000_000, 393_216, True)})
    config = load_settings(
        write_config(tmp_path, model="flash", catalog_model="deepseek/deepseek-v4-flash", **DEEPSEEK)
    ).select()
    assert config.context_window == 1_000_000 and config.catalog_model == "deepseek/deepseek-v4-flash"
    with pytest.raises(ConfigError, match="catalog_model deepseek/missing"):
        load_settings(write_config(tmp_path, model="flash", catalog_model="deepseek/missing", **DEEPSEEK))
    with pytest.raises(ConfigError, match="catalog_model"):
        load_settings(write_config(tmp_path, model="flash", catalog_model=""))


def test_thinking_budget_is_checked_against_the_resolved_output_limit(tmp_path):
    use_catalog(
        {"claude-small": ("anthropic", 200_000, 8_192, True), "claude-big": ("anthropic", 200_000, 64_000, True)}
    )
    thinking = {"type": "enabled", "budget_tokens": 10_000}
    with pytest.raises(ConfigError, match="max_output_tokens"):
        load_settings(write_config(tmp_path, model="claude-small", thinking=thinking))
    assert (
        load_settings(write_config(tmp_path, model="claude-big", thinking=thinking)).select().max_output_tokens
        == 32_000
    )


def test_a_catalog_window_too_small_for_the_agent_is_not_used(tmp_path):
    use_catalog({"llama3": ("ollama", 8_192, 8_192, None)})
    config = load_settings(
        write_config(tmp_path, protocol="openai-chat-completions", model="llama3", base_url="http://localhost:11434/v1")
    ).select()
    assert config.context_window is None and config.max_output_tokens == 8_192
    assert "上下文窗口为 8K，不足以运行 MossCode" in config.window_note()


@pytest.mark.parametrize("value", [{"requests": -1}, {"tokens": True}, {"seconds": 10}])
def test_run_budget_accepts_only_nonnegative_request_and_token_limits(tmp_path, value):
    path = write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["run_budget"] = value
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="run_budget"):
        load_settings(path)
