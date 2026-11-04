"""/thinking: one session-wide level, mapped to each protocol's own thinking parameters.

The model catalog says which models think at all, which cannot stop thinking and which take effort "none";
a model the catalog does not list is sent the parameters and the service decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .config import ProviderConfig

LEVELS = ("off", "low", "medium", "high")
DEFAULT = "default"
LEVEL_LABELS = {
    DEFAULT: "跟随配置",
    "off": "关闭",
    "low": "低",
    "medium": "中",
    "high": "高",
}
# Older Claude models take a token budget instead of an effort; at most half of the output limit.
BUDGETS = {"low": 2048, "medium": 8192, "high": 16384}
MIN_BUDGET = 1024


class Unsupported(Exception):
    """The current model cannot take this level; the message says why."""


@dataclass(frozen=True)
class ThinkingOptions:
    options: dict[str, Any] = field(default_factory=dict)  # Merged into the request.
    summary: str = ""  # What is sent, e.g. "adaptive，effort high".
    note: str = ""  # A difference from what was asked, e.g. medium treated as high.


def _deepseek(config: ProviderConfig) -> bool:
    info = config.catalog
    if info is not None and info.provider == "deepseek":
        return True
    host = urlsplit(config.base_url).hostname if config.base_url else None
    return host == "api.deepseek.com"


def _check(config: ProviderConfig) -> None:
    if config.reasoning is False:
        raise Unsupported(f"提供商 {config.name} 配置了 reasoning: false")
    info = config.catalog
    if config.reasoning is None and info is not None and info.reasoning is False:
        raise Unsupported(f"模型目录（{info.name}）中这个模型不支持思考")


def _effort_off(config: ProviderConfig) -> tuple[str, str]:
    """The OpenAI effort for off, and a note when the model cannot turn thinking off completely."""
    info = config.catalog
    # The catalog marks the models that take "none"; a file without the thinking fields says nothing.
    if info is None or info.reasoning is None or info.none_effort:
        return "none", ""
    if info.minimal_effort:
        return "minimal", "这个模型不能完全关闭思考，已改用最低的 minimal"
    raise Unsupported("这个模型不能关闭思考；可以用 /thinking low 降到最低")


def thinking_options(config: ProviderConfig, level: str) -> ThinkingOptions:
    """What a request to this provider carries for level; raises Unsupported when it cannot take it."""
    if level not in LEVELS:
        raise ValueError(level)
    _check(config)
    info = config.catalog
    if config.protocol == "anthropic-messages":
        if level == "off":
            if info is not None and info.always_on:
                raise Unsupported("这个模型的思考不能关闭；可以用 /thinking low 降到最低")
            return ThinkingOptions({"thinking": {"type": "disabled"}}, "thinking disabled")
        if info is not None and info.reasoning is not None:
            budget_model = info.provider == "anthropic" and not info.adaptive
        else:  # Not known: a configuration that uses a budget says the model takes one.
            budget_model = (config.thinking or {}).get("type") == "enabled"
        if budget_model:
            budget = min(BUDGETS[level], config.max_output_tokens // 2)
            if budget < MIN_BUDGET:
                raise Unsupported(f"max_output_tokens 太小，思考预算至少 {MIN_BUDGET}")
            return ThinkingOptions(
                {"thinking": {"type": "enabled", "budget_tokens": budget}}, f"budget_tokens {budget}"
            )
        note = "DeepSeek 把 medium 按 high 处理" if level == "medium" and _deepseek(config) else ""
        return ThinkingOptions(
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}},
            f"adaptive，effort {level}",
            note,
        )
    if config.protocol == "openai-chat-completions" and _deepseek(config):
        if level == "off":
            return ThinkingOptions({"extra_body": {"thinking": {"type": "disabled"}}}, "thinking disabled")
        return ThinkingOptions(
            {"reasoning_effort": level, "extra_body": {"thinking": {"type": "enabled"}}},
            f"thinking enabled，reasoning_effort {level}",
            "DeepSeek 把 medium 按 high 处理" if level == "medium" else "",
        )
    effort, note = _effort_off(config) if level == "off" else (level, "")
    if config.protocol == "openai-responses":
        return ThinkingOptions({"reasoning": {"effort": effort}}, f"reasoning.effort {effort}", note)
    return ThinkingOptions({"reasoning_effort": effort}, f"reasoning_effort {effort}", note)
