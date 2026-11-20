"""Vendor presets: `preset: deepseek` in a provider entry stands for the protocol, address, model and key variable.

Each field the entry writes itself wins, so a preset is only a shorter way to write the same entry. Default models
are names the model catalog knows (tests/test_presets.py checks), so the window and prices come with them.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    protocol: str
    base_url: str | None  # None: the protocol's official address.
    model: str
    api_key_env: str
    catalog_model: str | None = None  # The catalog's name for the default model, when the vendor's differs.
    auth_type: str | None = None  # Set to "none" for keyless local loopback servers.


PRESETS = {
    item.key: item
    for item in (
        Preset(
            "deepseek",
            "DeepSeek",
            "openai-chat-completions",
            "https://api.deepseek.com",
            "deepseek-flash",
            "DEEPSEEK_API_KEY",
        ),
        Preset(
            "kimi",
            "Kimi（月之暗面）",
            "openai-chat-completions",
            "https://api.moonshot.cn/v1",
            "kimi-k3.1-pro",
            "MOONSHOT_API_KEY",
        ),
        Preset(
            "glm",
            "智谱 GLM",
            "openai-chat-completions",
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-5.3",
            "ZHIPUAI_API_KEY",
        ),
        Preset(
            "qwen",
            "通义千问（阿里云百炼）",
            "openai-chat-completions",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-plus",
            "DASHSCOPE_API_KEY",
        ),
        Preset(
            "gemini",
            "Google Gemini",
            "openai-chat-completions",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-2.5-flash",
            "GEMINI_API_KEY",
        ),
        Preset(
            "openrouter",
            "OpenRouter",
            "openai-chat-completions",
            "https://openrouter.ai/api/v1",
            "anthropic/claude-sonnet-5.5",
            "OPENROUTER_API_KEY",
            "claude-sonnet-5-5",
        ),
        Preset(
            "anthropic", "Anthropic（Claude）", "anthropic-messages", None, "claude-sonnet-5-5", "ANTHROPIC_API_KEY"
        ),
        Preset("openai", "OpenAI", "openai-responses", None, "gpt-5.6", "OPENAI_API_KEY"),
        Preset(
            "ollama",
            "Ollama（本地免密钥）",
            "openai-chat-completions",
            "http://127.0.0.1:11434/v1",
            "llama3.1",
            "",
            auth_type="none",
        ),
        Preset(
            "lmstudio",
            "LM Studio（本地免密钥）",
            "openai-chat-completions",
            "http://127.0.0.1:1234/v1",
            "llama3.1",
            "",
            "ollama/llama3.1",
            auth_type="none",
        ),
        Preset(
            "copilot",
            "GitHub Copilot（设备码登录）",
            "openai-chat-completions",
            "https://api.githubcopilot.com",
            "gpt-4.1",
            "GITHUB_COPILOT_TOKEN",
            auth_type="github-copilot",
        ),
        Preset(
            "claude-cli",
            "Claude Code CLI（复用官方订阅）",
            "cli-claude",
            None,
            "claude-sonnet-5-5",
            "",
            auth_type="cli",
        ),
        Preset(
            "codex-cli",
            "OpenAI Codex CLI（复用官方订阅）",
            "cli-codex",
            None,
            "gpt-5.6",
            "",
            auth_type="cli",
        ),
        Preset(
            "gemini-cli",
            "Google Gemini CLI（复用官方订阅）",
            "cli-gemini",
            None,
            "gemini-2.5-flash",
            "",
            auth_type="cli",
        ),
    )
}
CREDENTIALS = ("api_key", "api_key_env", "api_key_keyring")


def expand(raw: dict) -> dict:
    """The entry with what its preset stands for filled in where the entry is silent."""
    from .config import ConfigError

    key = raw.get("preset")
    preset = PRESETS.get(key) if isinstance(key, str) else None
    if preset is None:
        raise ConfigError("preset 需为 " + "、".join(PRESETS) + " 之一。")
    data = dict(raw)
    data.setdefault("protocol", preset.protocol)
    data.setdefault("model", preset.model)
    if preset.catalog_model and data["model"] == preset.model:
        data.setdefault("catalog_model", preset.catalog_model)
    if preset.base_url is not None:
        data.setdefault("base_url", preset.base_url)
    if preset.auth_type is not None:
        data.setdefault("auth_type", preset.auth_type)
    if (
        preset.api_key_env
        and data.get("auth_type") not in {"none", "cli"}
        and not any(data.get(field) for field in CREDENTIALS)
    ):
        data["api_key_env"] = preset.api_key_env
    return data
