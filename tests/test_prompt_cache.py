import json
from dataclasses import replace

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Message, Usage
from mosscode.prompts import PromptRequest, Supplement
from mosscode.tools import create_default_registry
from mosscode.usage import cache_hit_rate, parse_usage
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
        conv = Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        )
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
            assert "<environment>" in blocks[1]["text"] and "<system-reminder>" not in json.dumps(
                blocks, ensure_ascii=False
            )
            assert len(payload["messages"]) == 1 and payload["messages"][0]["role"] == "user"
            # The reminder is part of the history, so the history breakpoint sits on it.
            user = payload["messages"][0]["content"]
            assert len(user) == 2 and user[0]["text"] == "用户任务" and "cache_control" not in user[0]
            assert user[1]["text"].startswith("<system-reminder>") and user[1]["cache_control"] == {"type": "ephemeral"}
        else:
            content = payload["input"] if protocol == "openai-responses" else payload["messages"]
            role = "developer" if protocol == "openai-responses" else "system"
            # Nothing follows the conversation: the reminder is its last message, not an instruction after it.
            assert [m["role"] for m in content[-2:]] == ["user", "user"]
            assert content[-2]["content"] == "用户任务" and content[-1]["content"].startswith("<system-reminder>")
            assert not any(m["role"] == role and "<system-reminder>" in m["content"] for m in content)
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


def _unmarked(value):
    """A payload without Anthropic's cache breakpoints, which move to the newest block on purpose."""
    if isinstance(value, dict):
        return {key: _unmarked(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_unmarked(item) for item in value]
    return value


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_each_request_starts_with_the_whole_previous_request_and_its_reply(tmp_path, protocol, cls, frames, path):
    # Services that cache on their own (DeepSeek) reuse only a prefix an earlier request ended with.
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()))
    try:
        registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
        conv = Conversation(provider, registry=registry)
        conv.set_mode("plan")
        registry.todos.replace([(None, "甲", "in_progress")])
        conv._agent.remind(Supplement("system-reminder", "距上次对话已过去约 7 小时"))
        await conv.ask("一")
        registry.todos.replace([("1", "甲", "completed"), (None, "乙", "in_progress")])
        await conv.ask("二")
        await conv.ask("三")
        key = "input" if protocol == "openai-responses" else "messages"
        rest = [_unmarked({name: value for name, value in body.items() if name != key}) for _, body in requests]
        assert rest == [rest[0]] * 3  # Instructions, tools and environment do not change.
        sent = [_unmarked(body[key]) for _, body in requests]
        for before, after in zip(sent, sent[1:]):
            assert after[: len(before)] == before and after[len(before)]["role"] == "assistant"
        told = [json.dumps(after[len(before) :], ensure_ascii=False) for before, after in zip([[]] + sent, sent)]
        assert "<todo-list>" in told[0] and "计划模式" in told[0] and "距上次对话" in told[0]
        assert "<todo-list>" in told[1] and "[>] 2. 乙" in told[1] and "计划模式" not in told[1]
        assert "<" not in told[2]  # The list did not change: nothing to tell.
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "explicit,official,marked", [(None, True, True), (None, False, False), (True, False, True), (False, True, False)]
)
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


@pytest.mark.parametrize(
    "protocol,details,expected",
    [
        ("openai-chat-completions", {}, None),
        ("openai-chat-completions", {"prompt_tokens_details": {"cached_tokens": 0}}, 0),
        ("openai-chat-completions", {"prompt_tokens_details": {"cached_tokens": -1}}, None),
        ("openai-chat-completions", {"prompt_cache_hit_tokens": True}, None),
        ("openai-chat-completions", {"prompt_cache_hit_tokens": "8"}, None),
        ("openai-responses", {"input_tokens_details": {"cached_tokens": 7}}, 7),
    ],
)
def test_missing_zero_and_invalid_cache_counters(protocol, details, expected):
    assert parse_usage(details, protocol).cached_input_tokens == expected


def test_anthropic_partial_measurement_does_not_invent_total_or_creation():
    assert parse_usage({"input_tokens": 12, "cache_read_input_tokens": 8}, "anthropic-messages") == Usage(
        None, None, 8, None, 12
    )
    assert parse_usage({"input_tokens": 12}, "anthropic-messages") == Usage(12)


async def test_cache_totals_are_known_lower_bound_after_missing_request():
    provider = SequenceProvider(
        response("a", usage=Usage(10, 2, 8, 2)), response("b"), response("c", usage=Usage(12, 2, 0, 0))
    )
    conv = Conversation(provider)
    for text in ("one", "two", "three"):
        await conv.ask(text)
    total = conv.total_usage
    assert (total.input_tokens, total.cached_input_tokens, total.cache_write_tokens) == (22, 8, 2)
    assert (total.missing_cached, total.missing_cache_write, total.requests) == (1, 1, 3)
    assert cache_hit_rate(total) is None
