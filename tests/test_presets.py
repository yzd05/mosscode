"""Vendor presets stand for the protocol, address, model and key variable of a provider entry."""

import pytest

from mosscode.catalog import lookup
from mosscode.config import ConfigError, load_settings
from mosscode.presets import PRESETS, expand


@pytest.mark.parametrize("preset", PRESETS.values(), ids=list(PRESETS))
def test_every_preset_default_model_is_in_the_catalog(preset):
    # So the window and prices come with a preset without writing them.
    info = lookup(preset.model, base_url=preset.base_url, protocol=preset.protocol, alias=preset.catalog_model)
    assert info is not None and info.context_window


def test_a_preset_fills_only_what_the_entry_leaves_out():
    assert expand({"name": "ds", "preset": "deepseek"}) == {
        "name": "ds",
        "preset": "deepseek",
        "protocol": "openai-chat-completions",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
    }
    written = {"name": "ds", "preset": "deepseek", "model": "deepseek-pro", "api_key_keyring": "moss/ds"}
    assert expand(written) == {**written, "protocol": "openai-chat-completions", "base_url": "https://api.deepseek.com"}
    # OpenRouter names the model its own way; the catalog entry goes with the default model only.
    assert expand({"name": "o", "preset": "openrouter"})["catalog_model"] == "claude-sonnet-5-5"
    assert "catalog_model" not in expand({"name": "o", "preset": "openrouter", "model": "openai/gpt-5.6"})
    official = expand({"name": "a", "preset": "anthropic", "api_key_env": "MY_KEY"})
    assert "base_url" not in official and official["api_key_env"] == "MY_KEY"


def test_unknown_preset_lists_the_known_ones():
    with pytest.raises(ConfigError, match="preset 需为 deepseek、kimi"):
        expand({"name": "x", "preset": "nope"})


def test_a_preset_entry_loads_with_the_catalog_window(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("providers:\n  - name: kimi\n    preset: kimi\n", encoding="utf-8")
    config = load_settings(path, tmp_path).providers[0]
    assert (config.protocol, config.base_url, config.model, config.api_key_env, config.preset) == (
        "openai-chat-completions",
        "https://api.moonshot.cn/v1",
        "kimi-k3.1-pro",
        "MOONSHOT_API_KEY",
        "kimi",
    )
    assert config.context_window > 128_000  # From the model catalog.
