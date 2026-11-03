import pytest
import yaml

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
        {"thinking": {"type": "enabled", "budget_tokens": 0}},
        {"thinking": {"type": "adaptive", "budget_tokens": 2000}},
        {"protocol": "openai-responses", "thinking": True},
        {"api_key": "secret", "api_key_env": "MOSS_TEST_KEY"},
    ],
)
def test_invalid_configuration_fails_before_request(tmp_path, change):
    with pytest.raises(ConfigError):
        load_settings(write_config(tmp_path, **change))


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
