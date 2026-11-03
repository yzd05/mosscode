import gzip
import json
from pathlib import Path

from mosscode.catalog import FIELDS, decode, encode, load_catalog, lookup, providers_for, save, slim, tokens_label


def entry(provider, window=None, output=None, price=None, vision=None, mode="chat", **extra):
    data = {"litellm_provider": provider, "mode": mode, **extra}
    if window is not None:
        data["max_input_tokens"] = window
    if output is not None:
        data["max_output_tokens"] = output
    if price is not None:
        data["input_cost_per_token"], data["output_cost_per_token"] = price
    if vision is not None:
        data["supports_vision"] = vision
    return data


RAW = {
    "sample_spec": {"mode": "chat", "litellm_provider": "openai or cohere"},
    "deepseek/deepseek-v4-flash": entry("deepseek", 1_000_000, 393_216, (3e-7, 1.2e-6), True),
    "deepseek-chat": entry("deepseek", 128_000, 8192),
    "claude-sonnet-4-5": entry("anthropic", 200_000, 64_000, (3e-6, 1.5e-5), True),
    "bedrock/claude-sonnet-4-5": entry("bedrock", 100, 100),
    "glm-4.6": entry("zai", 200_000, 128_000),
    "bigmodel/glm-4.6": entry("bigmodel", 128_000, 96_000),
    "text-embedding-3-small": entry("openai", 8191, mode="embedding"),
    "broken": entry("openai", "large", -5, ("free", 1e-6), "yes"),
}


def catalog():
    return decode(encode(slim(RAW, "2026-01-02")))


def test_slim_keeps_chat_models_and_valid_numbers_with_source_and_license():
    data = slim(RAW, "2026-01-02")
    assert data["fetched"] == "2026-01-02" and "MIT License" in data["license"] and "litellm" in data["source"]
    assert list(data["fields"]) == list(FIELDS)
    assert "text-embedding-3-small" not in data["models"] and "sample_spec" in data["models"]
    assert data["models"]["broken"] == ["openai", None, None, None, 1e-6, None, None, None]
    info = catalog().get("deepseek/deepseek-v4-flash")
    assert (info.provider, info.context_window, info.max_output_tokens, info.vision) == (
        "deepseek",
        1_000_000,
        393_216,
        True,
    )
    assert info.priced and not catalog().get("deepseek-chat").priced


def test_encoding_is_reproducible():
    assert encode(slim(RAW, "2026-01-02")) == encode(slim(RAW, "2026-01-02"))


def test_lookup_prefers_the_service_behind_the_base_url():
    c = catalog()
    assert lookup("deepseek-v4-flash", base_url="https://api.deepseek.com/anthropic", catalog=c).name == (
        "deepseek/deepseek-v4-flash"
    )
    # A bare entry counts for the host's provider only when it belongs to that provider.
    assert lookup("glm-4.6", base_url="https://open.bigmodel.cn/api/paas/v4", catalog=c).name == "bigmodel/glm-4.6"
    assert lookup("glm-4.6", base_url="https://api.z.ai/api/paas/v4", catalog=c).name == "glm-4.6"
    # Without a base URL the protocol names the official service.
    assert lookup("claude-sonnet-4-5", protocol="anthropic-messages", catalog=c).name == "claude-sonnet-4-5"


def test_lookup_falls_back_to_the_bare_name_then_a_provider_prefix_and_never_guesses():
    c = catalog()
    assert lookup("claude-sonnet-4-5", base_url="https://gateway.example.com/v1", catalog=c).name == (
        "claude-sonnet-4-5"
    )
    assert lookup("anthropic/claude-sonnet-4-5", base_url="https://openrouter.example/v1", catalog=c).name == (
        "claude-sonnet-4-5"
    )
    assert lookup("zai/claude-sonnet-4-5", catalog=c) is None
    assert lookup("deepseek-v4", base_url="https://api.deepseek.com", catalog=c) is None
    assert lookup("DeepSeek-Chat", catalog=c) is None


def test_catalog_model_names_the_entry_or_a_name_resolved_like_a_model_name():
    c = catalog()
    assert lookup("my-alias", alias="deepseek/deepseek-v4-flash", catalog=c).context_window == 1_000_000
    deepseek = "https://api.deepseek.com/anthropic"
    assert lookup("my-alias", alias="deepseek-v4-flash", base_url=deepseek, catalog=c).name == (
        "deepseek/deepseek-v4-flash"
    )
    # The alias replaces the model name: the model's own entry is not used when the alias is missing.
    assert lookup("deepseek-chat", alias="missing", base_url=deepseek, catalog=c) is None


def test_hosts_with_ports_and_unknown_hosts():
    assert providers_for("http://localhost:11434/v1", None) == ("ollama",)
    assert providers_for("http://localhost:8000/v1", "openai-chat-completions") == ()
    assert providers_for("api.deepseek.com", None) == ("deepseek",)
    assert providers_for(None, "openai-responses") == ("openai",)


def test_packaged_snapshot_loads_and_lists_well_known_models():
    packaged = load_catalog()
    assert packaged.path is None and packaged.problem is None and len(packaged.models) > 1000
    assert lookup("gpt-5", protocol="openai-responses", catalog=packaged).context_window
    assert (
        "Berri AI"
        in json.loads(
            gzip.decompress(
                Path(__file__).parents[1].joinpath("src", "mosscode", "data", "models.json.gz").read_bytes()
            )
        )["license"]
    )


def test_user_copy_wins_and_a_corrupt_one_falls_back_with_a_notice(tmp_path):
    path = save(encode(slim(RAW, "2026-01-02")), tmp_path)
    assert path == tmp_path / ".mosscode" / "models.json.gz"
    user = load_catalog(tmp_path)
    assert user.path == path and user.fetched == "2026-01-02" and len(user.models) == len(catalog().models)
    path.write_bytes(b"not gzip")
    fallback = load_catalog(tmp_path)
    assert fallback.path is None and len(fallback.models) > 1000
    assert "无法读取" in fallback.problem and "moss models update" in fallback.problem
    assert list(path.parent.glob("*.tmp")) == []


def test_token_labels():
    assert [tokens_label(n) for n in (1_000_000, 1_048_576, 393_216, 128_000, 32_000, 200_000, 258_048, None)] == [
        "1M",
        "1M",
        "384K",
        "128K",
        "32K",
        "200K",
        "252K",
        "?",
    ]
