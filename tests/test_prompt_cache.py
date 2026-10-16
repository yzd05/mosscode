import json
from dataclasses import replace

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Message, Usage
from mosscode.prompts import PromptRequest, Supplement
from mosscode.tools import create_default_registry
from mosscode.usage import parse_usage, cache_summary
from test_agent import SequenceProvider, response
from test_providers import CASES, SSEBody, adapter


def with_cache(frames, protocol):
    updated = []
    for raw in frames:
        if b"data: [DONE]" in raw:
            updated.append(raw)
            continue
        prefix, data = raw.decode().split("data: ", 1)
        event = json.loads(data)
        if protocol == "anthropic-messages":
            if event["type"] == "message_start":
                event["message"]["usage"].update(cache_read_input_tokens=80, cache_creation_input_tokens=20)
            if event["type"] == "message_delta":
                # Cumulative update overwrites 80/20, retaining start input if omitted.
                event["usage"].update(cache_read_input_tokens=100, cache_creation_input_tokens=30)
        elif protocol == "openai-responses" and event["type"] == "response.completed":
            event["response"]["usage"].update(input_tokens_details={"cached_tokens": 8, "cache_write_tokens": 2})
        elif protocol == "openai-chat-completions" and event.get("usage"):
            event["usage"].update(prompt_cache_hit_tokens=8, prompt_cache_miss_tokens=4)
        updated.append((prefix + "data: " + json.dumps(event) + "\n\n").encode())
    return updated


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_actual_sdk_cache_stats_and_separate_system_instructions(tmp_path, protocol, cls, frames, path):
    provider, requests, bodies = adapter(protocol, cls, lambda: SSEBody(with_cache(frames(), protocol)))
    provider.config = replace(provider.config, prompt_cache=True)
    try:
        conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False))
        conv.set_mode("plan")
        events = [e async for e in conv.reply("用户任务")]
        usage = next(e.usage for e in events if isinstance(e, Completed))
        assert usage.output_tokens == 7
        if protocol == "anthropic-messages":
            assert usage == Usage(142, 7, 100, 30, 12)
        elif protocol == "openai-responses":
            assert usage == Usage(12, 7, 8, 2)
        else:
            assert usage == Usage(12, 7, 8, None, 4)
        assert conv.total_usage.input_tokens == usage.input_tokens
        assert conv.total_usage.cached_input_tokens == usage.cached_input_tokens
        payload = requests[0][1]
        assert "prompt_summary" not in json.dumps(payload) and "prompt_guidelines" not in json.dumps(payload)
        tools = json.dumps(payload["tools"])
        assert "exit_plan_mode" in tools and "write_file" not in tools
        assert all(m.role != "system" for m in conv.history)
        if protocol == "anthropic-messages":
            blocks = payload["system"]
            stable = blocks[0]["text"]
            assert blocks[0]["cache_control"] == {"type": "ephemeral"}
            assert len(blocks) == 2 and "cache_control" not in blocks[1]
            assert "<environment>" in blocks[1]["text"] and "<system-reminder>" not in json.dumps(blocks, ensure_ascii=False)
            assert len(payload["messages"]) == 1 and payload["messages"][0]["role"] == "user"
            # History breakpoint on the last history block; the reminder follows it.
            user = payload["messages"][0]["content"]
            assert user[-2]["text"] == "用户任务" and user[-2]["cache_control"] == {"type": "ephemeral"}
            assert user[-1]["text"].startswith("<system-reminder>") and "cache_control" not in user[-1]
        else:
            content = payload["input"] if protocol == "openai-responses" else payload["messages"]
            role = "developer" if protocol == "openai-responses" else "system"
            assert content[-1]["role"] == role and "<system-reminder>" in content[-1]["content"]
            assert [m["content"] for m in content if m.get("role") == "user"] == ["用户任务"]
            assert "cache_control" not in json.dumps(payload)
            stable = payload["instructions"] if protocol == "openai-responses" else content[0]["content"]
            assert "<environment>" not in stable and "<system-reminder>" not in stable
        assert "`read_file`" in stable and "`exit_plan_mode`" in stable and "`write_file`" not in stable
        # Tool rules travel once, in the API tool descriptions.
        rule = "修改或覆盖已有文件前必须先用 read_file 读取"
        assert rule not in stable and rule in json.dumps(payload["tools"], ensure_ascii=False)
        assert all(b.closed for b in bodies)
    finally:
        await provider.close()


@pytest.mark.parametrize("explicit,official,marked", [(None, True, True), (None, False, False),
                                                      (True, False, True), (False, True, False)])
async def test_anthropic_explicit_cache_capability_switch(explicit, official, marked):
    protocol, cls, frames, _ = CASES[0]
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()))
    # A missing config URL does not imply official: SDK environment/profile routes may differ.
    provider.config = replace(provider.config, prompt_cache=explicit, base_url=None)
    if official:
        provider.client.base_url = "https://api.anthropic.com"
    try:
        prompt = PromptRequest("stable", (Supplement("environment", "changing"),))
        _ = [e async for e in provider.stream([Message("user", (ContentBlock("text", "问题"),))], prompt)]
        assert ("cache_control" in requests[0][1]["system"][0]) == marked
        assert "cache_control" not in requests[0][1]["system"][1]
        assert ("cache_control" in requests[0][1]["messages"][-1]["content"][-1]) == marked
    finally:
        await provider.close()


@pytest.mark.parametrize("protocol,details,expected", [
    ("openai-chat-completions", {}, None),
    ("openai-chat-completions", {"prompt_tokens_details": {"cached_tokens": 0}}, 0),
    ("openai-chat-completions", {"prompt_tokens_details": {"cached_tokens": -1}}, None),
    ("openai-chat-completions", {"prompt_cache_hit_tokens": True}, None),
    ("openai-chat-completions", {"prompt_cache_hit_tokens": "8"}, None),
    ("openai-responses", {"input_tokens_details": {"cached_tokens": 7}}, 7),
])
def test_missing_zero_and_invalid_cache_counters(protocol, details, expected):
    assert parse_usage(details, protocol).cached_input_tokens == expected


def test_anthropic_partial_measurement_does_not_invent_total_or_creation():
    assert parse_usage({"input_tokens": 12, "cache_read_input_tokens": 8}, "anthropic-messages") == Usage(None, None, 8, None, 12)
    assert parse_usage({"input_tokens": 12}, "anthropic-messages") == Usage(12)


async def test_cache_totals_are_known_lower_bound_after_missing_request():
    provider = SequenceProvider(response("a", usage=Usage(10, 2, 8, 2)), response("b"),
                                response("c", usage=Usage(12, 2, 0, 0)))
    conv = Conversation(provider)
    for text in ("one", "two", "three"):
        await conv.ask(text)
    total = conv.total_usage
    assert (total.input_tokens, total.cached_input_tokens, total.cache_write_tokens) == (22, 8, 2)
    assert (total.missing_cached, total.missing_cache_write, total.requests) == (1, 1, 3)
    assert cache_summary(total) == "缓存读 ≥8 / 写 ≥2"
