"""/thinking: one level per session, mapped to each protocol, ignored where the model cannot take it."""

import gzip
import json
from dataclasses import replace

import pytest

from mosscode.builtin_commands import status_report
from mosscode.catalog import FIELDS, OLDEST_FIELDS, ModelInfo, decode, slim
from mosscode.config import ConfigError, ProviderConfig, Settings, _provider
from mosscode.conversation import Conversation
from mosscode.messages import ProviderError
from mosscode.reasoning import Unsupported, thinking_options
from test_commands import FakeUI, context, run
from test_conversation import FakeProvider
from test_providers import CASES, SSEBody, adapter


def info(name, provider, **flags):
    return ModelInfo(name, provider, None, None, None, None, None, None, None, **flags)


OPUS = info("claude-opus-5-5", "anthropic", reasoning=True, adaptive=True, always_on=True)
SONNET_46 = info("claude-sonnet-4-6", "anthropic", reasoning=True, adaptive=True)
HAIKU = info("claude-haiku-4-5", "anthropic", reasoning=True)
FLASH = info("deepseek/deepseek-flash", "deepseek", reasoning=True)
GPT5 = info("gpt-5", "openai", reasoning=True, none_effort=False, minimal_effort=True)
GPT51 = info("gpt-5.1", "openai", reasoning=True, none_effort=True)
O3 = info("o3", "openai", reasoning=True)
GPT41 = info("gpt-4.1", "openai", reasoning=False)


def config(protocol, catalog=None, **fields):
    return ProviderConfig("p", protocol, "m", catalog=catalog, **fields)


def options(protocol, level, catalog=None, **fields):
    return thinking_options(config(protocol, catalog, **fields), level)


def test_anthropic_models_take_adaptive_thinking_with_an_effort_or_a_budget():
    assert options("anthropic-messages", "high", SONNET_46).options == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert options("anthropic-messages", "off", SONNET_46).options == {"thinking": {"type": "disabled"}}
    with pytest.raises(Unsupported, match="不能关闭"):
        options("anthropic-messages", "off", OPUS)
    assert options("anthropic-messages", "low", OPUS).summary == "adaptive，effort low"
    # Older Claude models take a budget, at most half of the output limit.
    assert options("anthropic-messages", "medium", HAIKU).options == {
        "thinking": {"type": "enabled", "budget_tokens": 8192}
    }
    assert options("anthropic-messages", "high", HAIKU, max_output_tokens=10_000).options["thinking"] == {
        "type": "enabled",
        "budget_tokens": 5000,
    }
    with pytest.raises(Unsupported, match="至少 1024"):
        options("anthropic-messages", "low", HAIKU, max_output_tokens=2000)


def test_a_model_the_catalog_does_not_list_is_sent_the_level_in_the_configured_style():
    assert options("anthropic-messages", "off").options == {"thinking": {"type": "disabled"}}
    budget = {"type": "enabled", "budget_tokens": 2048}
    assert options("anthropic-messages", "low", thinking=budget).options == {"thinking": budget}
    assert options("openai-responses", "off").options == {"reasoning": {"effort": "none"}}
    assert options("openai-chat-completions", "medium").options == {"reasoning_effort": "medium"}


def test_deepseek_turns_thinking_on_and_off_and_treats_medium_as_high():
    anthropic = options("anthropic-messages", "medium", FLASH)
    assert anthropic.options["output_config"] == {"effort": "medium"} and "按 high" in anthropic.note
    assert options("openai-chat-completions", "off", FLASH).options == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    chat = options("openai-chat-completions", "high", base_url="https://api.deepseek.com")
    assert chat.options == {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}


def test_openai_off_is_none_or_the_lowest_effort_the_model_has():
    assert options("openai-responses", "high", GPT5).options == {"reasoning": {"effort": "high"}}
    assert options("openai-responses", "off", GPT51).options == {"reasoning": {"effort": "none"}}
    minimal = options("openai-chat-completions", "off", GPT5)
    assert minimal.options == {"reasoning_effort": "minimal"} and "minimal" in minimal.note
    with pytest.raises(Unsupported, match="不能关闭"):
        options("openai-responses", "off", O3)


def test_models_without_reasoning_and_reasoning_false_are_unsupported_and_reasoning_true_overrides():
    with pytest.raises(Unsupported, match="gpt-4.1"):
        options("openai-responses", "high", GPT41)
    with pytest.raises(Unsupported, match="reasoning: false"):
        options("openai-responses", "high", GPT51, reasoning=False)
    assert options("openai-responses", "low", GPT41, reasoning=True).options == {"reasoning": {"effort": "low"}}


def test_reasoning_is_a_boolean_in_the_configuration():
    assert _provider({"name": "a", "protocol": "openai-responses", "model": "m", "reasoning": False}).reasoning is False
    with pytest.raises(ConfigError, match="reasoning"):
        _provider({"name": "a", "protocol": "openai-responses", "model": "m", "reasoning": "yes"})


def test_the_catalog_keeps_the_thinking_fields_and_reads_files_written_before_them():
    raw = {
        "claude-opus-5-5": {
            "litellm_provider": "anthropic",
            "mode": "chat",
            "supports_reasoning": True,
            "supports_adaptive_thinking": True,
            "thinking_always_on": True,
        },
        "gpt-4.1": {"litellm_provider": "openai", "mode": "chat"},
    }
    catalog = decode(gzip.compress(json.dumps(slim(raw)).encode()))
    opus, plain = catalog.get("claude-opus-5-5"), catalog.get("gpt-4.1")
    assert (opus.reasoning, opus.adaptive, opus.always_on, opus.none_effort) == (True, True, True, None)
    assert plain.reasoning is False  # Left out by LiteLLM: no reasoning.
    old = slim(raw)
    old["fields"] = FIELDS[:OLDEST_FIELDS]
    old["models"] = {name: row[:OLDEST_FIELDS] for name, row in old["models"].items()}
    earlier = decode(gzip.compress(json.dumps(old).encode())).get("gpt-4.1")
    assert earlier.reasoning is None  # Not known, so /thinking is still sent.
    assert thinking_options(config("openai-responses", earlier), "low").options == {"reasoning": {"effort": "low"}}


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_the_level_replaces_the_configured_thinking_on_the_wire(protocol, cls, frames, path):
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()))
    if protocol == "openai-chat-completions":
        provider.config = replace(provider.config, catalog=FLASH)
    try:
        conv = Conversation(provider)
        await conv.ask("one")
        conv.set_thinking("high")
        await conv.ask("two")
        conv.set_thinking(None)
        await conv.ask("three")
        default, high, back = (payload for _, payload in requests)
        assert back == {**default, **{key: back[key] for key in ("messages", "input") if key in back}}
        if protocol == "anthropic-messages":
            assert default["thinking"] == {"type": "adaptive"} and "output_config" not in default
            assert high["thinking"] == {"type": "adaptive"} and high["output_config"] == {"effort": "high"}
        elif protocol == "openai-responses":
            assert "reasoning" not in default and high["reasoning"] == {"effort": "high"}
        else:
            assert "reasoning_effort" not in default and "thinking" not in default
            assert high["reasoning_effort"] == "high" and high["thinking"] == {"type": "enabled"}
    finally:
        await provider.close()


async def test_an_unsupported_level_is_ignored_and_the_request_follows_the_configuration():
    provider, requests, _ = adapter(*CASES[1][:2], lambda: SSEBody(CASES[1][2]()))
    provider.config = replace(provider.config, catalog=GPT41)
    try:
        conv = Conversation(provider)
        conv.set_thinking("high")
        assert "gpt-4.1" in conv.thinking_problem()
        await conv.ask("one")
        assert "reasoning" not in requests[0][1]
    finally:
        await provider.close()


class ConfiguredProvider(FakeProvider):
    def __init__(self, config):
        super().__init__()
        self.config, self.thinking = config, None


async def test_thinking_command_sets_reports_and_follows_a_model_switch():
    conv = Conversation(ConfiguredProvider(config("anthropic-messages", SONNET_46)))
    ctx = context(conv, Settings((config("anthropic-messages", SONNET_46),), "p"), FakeUI("p"))
    await run(ctx, "/thinking high")
    assert conv.thinking == "high" and conv.provider.thinking == "high"
    assert ctx.ui.notes[-1] == "└ 思考强度：high；p 发送 adaptive，effort high。"
    assert "high（发送 adaptive，effort high）" in status_report(ctx)
    await run(ctx, "/thinking max")
    assert ctx.ui.notes[-1].startswith("未知的思考强度 max。") and conv.thinking == "high"
    await run(ctx, "/thinking")
    assert ctx.ui.calls[-1] == ("choose", "思考强度 · p", 5, 4)
    await ctx.ui.pick(1)
    assert conv.thinking == "off" and "thinking disabled" in ctx.ui.notes[-1]
    # A model that cannot stop thinking keeps the level but is sent nothing for it.
    switched = ConfiguredProvider(config("anthropic-messages", OPUS))
    await conv.set_provider(switched)
    assert switched.thinking == "off" and "不能关闭" in conv.thinking_problem()
    assert "当前模型不支持" in status_report(ctx)
    await run(ctx, "/thinking default")
    assert conv.thinking is None and switched.thinking is None and "跟随配置" in ctx.ui.notes[-1]
    assert "  思考 " in status_report(ctx) and "跟随配置" in status_report(ctx)


async def test_unsupported_level_note_and_busy_refusal():
    conv = Conversation(ConfiguredProvider(config("openai-responses", GPT41)))
    ctx = context(conv, ui=FakeUI("p"))
    await run(ctx, "/thinking low")
    assert "当前模型不支持" in ctx.ui.notes[-1] and "切换到支持的模型后生效" in ctx.ui.notes[-1]
    conv._agent.busy = True
    with pytest.raises(ProviderError, match="不能切换思考强度"):
        conv.set_thinking("high")


async def test_the_status_bar_and_model_switch_name_the_level():
    from textual.widgets import Static

    from mosscode.tui import MossCodeApp
    from test_interaction import send

    configs = (config("anthropic-messages", SONNET_46), replace(config("anthropic-messages", OPUS), name="opus"))
    conv = Conversation(ConfiguredProvider(configs[0]))
    app = MossCodeApp(conv, "test", settings=Settings(configs, "p"), model="m", provider_factory=ConfiguredProvider)
    app.provider_name = "p"
    async with app.run_test(size=(120, 30)) as pilot:
        await send(app, pilot, "/thinking off")
        assert " · 思考 off" in str(app.query_one("#status", Static).render())
        await send(app, pilot, "/model opus")
        notes = [str(note.render()) for note in app.query(".local-note")]
        assert any("思考强度 off：opus 不支持" in note for note in notes)
        assert " · 思考 off（不支持）" in str(app.query_one("#status", Static).render())
    await conv.close()
