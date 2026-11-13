"""Context management."""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import httpx
import openai
import pytest
import yaml

from mosscode.config import ConfigError, load_settings
from mosscode.context import ContextBudget, ContextMeter, estimate_message, estimate_text
from mosscode.messages import REMINDER, ContentBlock, Message, ToolCall, ToolResult, Usage
from mosscode.providers import _safe_error
from mosscode.session import MARKER, Session, prune_sessions
from mosscode.tool_budget import MESSAGE_TOKENS, READ_FLOOR, RESULT_TOKENS, bound_message, share
from mosscode.tools import create_default_registry
from mosscode.tools.base import Params, Tool
from mosscode.tools.execution import execute_batch
from test_config import write_config
from waiting import until


def text(role, value):
    return Message(role, (ContentBlock("text", value),))


# Estimation and budget


def test_estimate_counts_ascii_by_four_and_every_cjk_character_as_a_token():
    assert estimate_text("") == 0
    assert estimate_text("abcd" * 10) == 10
    assert estimate_text("上下文管理") == 5
    assert estimate_text("中文" * 1000) >= 2000  # Never below the character count.
    message = Message("assistant", (ContentBlock("text", "abcd"),), tool_calls=(ToolCall("1", "read_file", '{"p":1}'),))
    assert estimate_message(message) == 4 + 1 + 3 + 2
    tool = Message("tool", (), tool_results=(ToolResult("1", "bash", "x", True, "failed"),))
    assert estimate_message(tool) == 4 + estimate_text("错误 [failed]：x")


def image_messages():
    from mosscode.images import ImageRef

    pasted = ImageRef("0" * 32 + ".png", "image/png", 1000, 1000, 10, "[图片 #1]", data=b"pixels")
    shot = ImageRef("1" * 32 + ".png", "image/png", 2000, 1125, 10, "a.png", data=b"pixels")
    user = Message("user", (ContentBlock("text", "abcd"), ContentBlock("image", "[图片 #1]", pasted)))
    tool = Message("tool", (), tool_results=(ToolResult("1", "read_file", "a.png", images=(shot,)),))
    return user, tool


def test_images_count_by_their_area_capped_at_1600():
    user, tool = image_messages()
    assert estimate_message(user) == 4 + 1 + 1334
    assert estimate_message(tool) == 4 + estimate_text("a.png") + 1600


def test_compaction_sees_images_as_alt_text_and_size():
    from mosscode.compaction import transcript

    text = transcript(image_messages())
    assert "[用户]\nabcd[图片 #1]（图片 1000×1000）" in text
    assert "[工具结果 · read_file]\na.png\na.png（图片 2000×1125）" in text and "pixels" not in text


def test_meter_anchors_on_reported_usage_and_estimates_only_the_increment():
    meter = ContextMeter()
    history = [text("user", "hi"), text("assistant", "hello")]
    meter.record(Usage(input_tokens=5000, output_tokens=200), len(history))
    history.append(text("user", "abcd" * 100))
    assert meter.estimate(history, extra="abcd") == 5200 + 4 + 100 + 1
    meter.record(Usage(input_tokens=None, output_tokens=10), len(history))
    assert meter.anchor is None
    full = meter.estimate(history, fixed=lambda: "abcd" * 50)
    assert full == 50 + sum(estimate_message(m) for m in history)
    meter.record(Usage(input_tokens=100, output_tokens=1), 99)  # Anchor past the history: fall back.
    assert meter.estimate(history, fixed=lambda: "") == sum(estimate_message(m) for m in history)


def test_budget_thresholds_follow_the_configured_window():
    budget = ContextBudget.for_provider(1_000_000, 16_384)
    assert budget.configured and budget.effective == 980_000
    assert budget.auto_threshold == 967_000 and budget.manual_limit == 977_000
    assert ContextBudget.for_provider(1_000_000, 384_000).auto_threshold == 603_000
    early = ContextBudget.for_provider(1_000_000, 16_384, 200_000)
    assert early.auto_threshold == 200_000 and early.manual_limit == 977_000
    default = ContextBudget.for_provider(None, 16_384)
    assert not default.configured and default.window == 128_000 and default.auto_threshold == 95_000


def test_context_fields_are_validated_when_loading(tmp_path):
    settings = load_settings(write_config(tmp_path, context_window=1_000_000, compact_threshold=200_000))
    assert settings.providers[0].budget().auto_threshold == 200_000 and settings.auto_compact
    for change, message in [
        ({"context_window": 40_000}, "不足"),
        ({"context_window": 0}, "正整数"),
        ({"context_window": 200_000, "compact_threshold": 190_000}, "compact_threshold"),
        ({"compact_threshold": 1_000}, "compact_threshold"),
    ]:
        with pytest.raises(ConfigError, match=message):
            load_settings(write_config(tmp_path, **change))
    path = write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**raw, "auto_compact": False}), encoding="utf-8")
    assert load_settings(path).auto_compact is False
    path.write_text(yaml.safe_dump({**raw, "auto_compact": "no"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="auto_compact"):
        load_settings(path)


# Layer 1


class DumpParams(Params):
    size: int
    fill: str = "x"


class Dump(Tool):
    name = "dump"
    description = "Return size characters."
    params_model = DumpParams
    read_only = True

    async def execute(self, params):
        body = params.fill * params.size
        return "HEAD" + body[8:] + "TAIL", {}


def dump_registry(tmp_path, mode="bypassPermissions", session=True):
    reg = create_default_registry(tmp_path / "project", permission_mode=mode, load_permissions=False)
    reg.register(Dump(tmp_path / "project"))
    if session:
        reg.use_session(Session.create(tmp_path / "sessions"))
    return reg


def dump(call_id, size, fill="x"):
    return ToolCall(call_id, "dump", json.dumps({"size": size, "fill": fill}))


async def test_oversized_result_is_stored_once_with_a_head_and_tail_preview(tmp_path):
    (tmp_path / "project").mkdir()
    reg = dump_registry(tmp_path)
    result = await reg.execute(dump("call/1", 60_000))
    stored = result.details["persisted"]
    assert result.details["budgeted"] and stored.endswith("call_1.txt")
    original = "HEAD" + "x" * 59_992 + "TAIL"
    assert open(stored, encoding="utf-8").read() == original
    assert stored in result.output and "read_file" in result.output
    assert "\nHEAD" in result.output and result.output.endswith("TAIL") and "中间省略" in result.output
    assert estimate_text(result.output) < 2_000
    # Same call ID again: the first file is kept.
    again = await reg.execute(dump("call/1", 60_000, "y"))
    assert open(again.details["persisted"], encoding="utf-8").read() == original
    small = await reg.execute(dump("call-2", RESULT_TOKENS * 4))
    assert "budgeted" not in small.details and small.output.endswith("TAIL")
    # Chinese counts one token per character: 20,000 characters is over the limit.
    chinese = await reg.execute(dump("call-3", 20_000, "中"))
    assert chinese.details["budgeted"]


async def test_batch_over_the_combined_budget_stores_the_largest_first(tmp_path):
    (tmp_path / "project").mkdir()
    reg = dump_registry(tmp_path)
    sizes = [44_000, 42_000, 48_000, 46_000, 40_000]  # 11K–12K tokens each, 55K together.
    calls = tuple(dump(f"c{i}", size) for i, size in enumerate(sizes))
    history = []
    async for _ in execute_batch(Message("assistant", (), tool_calls=calls), reg, history):
        pass
    results = history[-1].tool_results
    assert [bool(r.details.get("persisted")) for r in results] == [False, False, True, False, False]
    assert sum(estimate_text(r.model_text()) for r in results) <= MESSAGE_TOKENS
    # Already decided: applying the budget again changes nothing.
    assert bound_message(history[-1], reg.session) == history[-1]


def long_file(project, name, lines=3000):
    """More than one read_file page: about 1,200 lines (14K tokens) fit in the first."""
    (project / name).write_text("".join(f"line {n:05d} {'x' * 30}\n" for n in range(1, lines + 1)), encoding="utf-8")
    return ToolCall(f"read-{name}", "read_file", json.dumps({"path": name}))


async def batch(reg, calls):
    history = []
    async for _ in execute_batch(Message("assistant", (), tool_calls=tuple(calls)), reg, history):
        pass
    return history[-1]


def tokens(results):
    return sum(estimate_text(r.model_text()) for r in results)


def test_a_fair_share_keeps_small_items_whole_and_cuts_the_large_ones_alike():
    assert share([10, 20, 30], 100) == 30  # Everything fits.
    assert share([10, 100, 100], 90) == 40  # 10 whole, the other two share the remaining 80.
    assert share([50, 50], 60) == 30 and share([50], -5) == 0 and share([], 10) == 0


async def test_reads_of_one_batch_over_the_budget_become_shorter_pages(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    reg = dump_registry(tmp_path)
    (project / "small.txt").write_text("短文件\n", encoding="utf-8")
    calls = [long_file(project, f"f{i}.txt") for i in range(10)]
    calls.insert(3, ToolCall("read-small", "read_file", json.dumps({"path": "small.txt"})))
    message = await batch(reg, calls)
    results = message.tool_results
    assert tokens(results) <= MESSAGE_TOKENS and not any(r.details.get("persisted") for r in results)
    assert results[3].output.endswith("短文件") and not results[3].details["truncated"]  # Under its share: whole.
    cut = [r for i, r in enumerate(results) if i != 3]
    assert all(4_000 < estimate_text(r.output) <= 5_000 for r in cut)  # About a tenth of the budget each.
    for result in cut:
        details = result.details
        last = details["next_offset"] - 1
        body = result.output.split("\n")
        assert details["truncated"] and details["lines"] == last == len(body) - 1
        assert body[-2] == f"{last:>6}\tline {last:05d} {'x' * 30}"  # Cut at a line boundary.
        assert "合计超出上下文预算" in body[-1] and f"请从 offset={last + 1} 继续读取" in body[-1]
        assert "共 3000 行" in body[-1] and body[-1].count("继续读取") == 1  # The page's own note is replaced.
    # The offset in the note continues exactly where the shortened page stopped.
    offset = cut[0].details["next_offset"]
    following = await reg.execute(ToolCall("next", "read_file", json.dumps({"path": "f0.txt", "offset": offset})))
    assert following.output.startswith(f"{offset:>6}\tline {offset:05d} ")
    # Already decided: applying the budget again changes nothing.
    assert bound_message(message, reg.session) == message


async def test_reads_are_shortened_before_other_results_are_stored(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    reg = dump_registry(tmp_path)
    # Four results of 11K tokens and two reads of about 14K: the reads share what is left, nothing is stored.
    calls = [dump(f"c{i}", 44_000) for i in range(4)] + [long_file(project, f"f{i}.txt") for i in range(2)]
    results = (await batch(reg, calls)).tool_results
    assert tokens(results) <= MESSAGE_TOKENS and not any(r.details.get("persisted") for r in results)
    assert all(r.details["truncated"] and 2_000 < estimate_text(r.output) <= 3_000 for r in results[4:])
    # The other results alone are over the budget: the read keeps its floor and the largest of them is stored.
    calls = [dump(f"d{i}", size) for i, size in enumerate([44_000, 42_000, 48_000, 46_000, 40_000])]
    results = (await batch(reg, [*calls, long_file(project, "g.txt")])).tool_results
    assert [bool(r.details.get("persisted")) for r in results] == [False, False, True, False, False, False]
    assert 1_500 < estimate_text(results[5].output) <= READ_FLOOR


async def test_every_read_keeps_a_floor_and_small_batches_stay_whole(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    reg = dump_registry(tmp_path)
    results = (await batch(reg, [long_file(project, f"f{i}.txt") for i in range(30)])).tool_results
    assert all(1_500 < estimate_text(r.output) <= READ_FLOOR for r in results)  # 30 × 2,000: over, by design.
    whole = (await batch(reg, [long_file(project, f"w{i}.txt") for i in range(3)])).tool_results
    assert all("合计超出" not in r.output and "budgeted" not in r.details for r in whole)
    # Failed reads and files that fit their share are left as they are.
    missing = ToolCall("missing", "read_file", json.dumps({"path": "nowhere.txt"}))
    results = (await batch(reg, [missing, *[long_file(project, f"m{i}.txt") for i in range(5)]])).tool_results
    assert results[0].is_error and "budgeted" not in results[0].details and tokens(results) <= MESSAGE_TOKENS


async def test_read_file_is_exempt_and_session_files_are_readable_without_approval(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "big.txt").write_text("abcd" * 20_000, encoding="utf-8")
    for mode in ("default", "plan"):
        reg = dump_registry(tmp_path, mode)
        page = await reg.execute(ToolCall("r", "read_file", '{"path": "big.txt"}'))
        assert not page.is_error and "budgeted" not in page.details
        stored = reg.session.persist_result("saved", "第一行\n第二行\n")
        read = await reg.execute(ToolCall("s", "read_file", json.dumps({"path": str(stored)})))
        assert not read.is_error and "第二行" in read.output
        outside = tmp_path / "other.txt"
        outside.write_text("x", encoding="utf-8")
        denied = await reg.execute(ToolCall("o", "read_file", json.dumps({"path": str(outside)})))
        assert denied.code == "approval_required"


async def test_without_a_session_directory_large_results_are_cut_to_the_preview(tmp_path):
    (tmp_path / "project").mkdir()
    reg = dump_registry(tmp_path, session=False)
    result = await reg.execute(dump("c", 60_000))
    assert result.details["truncated"] and "未能保存" in result.output and result.output.endswith("TAIL")


def test_old_marked_session_directories_are_pruned(tmp_path):
    now = datetime(2026, 10, 16, 12)
    old = Session.create(tmp_path, now - timedelta(days=40))
    recent = Session.create(tmp_path, now)
    stale = (now - timedelta(days=40)).timestamp()
    os.utime(old.directory / MARKER, (stale, stale))
    foreign = tmp_path / "unmarked"
    foreign.mkdir()
    os.utime(foreign, (stale, stale))
    assert prune_sessions(tmp_path, now) == 1
    assert not old.directory.exists() and recent.directory.exists() and foreign.exists()


# Context overflow is recognized, other 400s are not


def status_error(module, status, message, body):
    request = httpx.Request("POST", "https://gateway.example/v1")
    return (
        module.BadRequestError(message, response=httpx.Response(status, request=request), body=body)
        if status == 400
        else module.APIStatusError(message, response=httpx.Response(status, request=request), body=body)
    )


@pytest.mark.parametrize(
    "module, status, message, body",
    [
        (
            anthropic,
            400,
            "prompt is too long: 210000 tokens > 200000 maximum",
            {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long"}},
        ),
        (openai, 400, "Error code: 400", {"error": {"code": "context_length_exceeded", "message": "too long"}}),
        (openai, 400, "This model's maximum context length is 131072 tokens.", None),
        (anthropic, 413, "Request too large", {"error": {"message": "Input is too long for requested model."}}),
    ],
)
def test_overflow_errors_get_a_code_and_a_safe_message(module, status, message, body):
    error = _safe_error(status_error(module, status, message, body))
    assert error.code == "context_overflow" and "上下文窗口" in str(error)
    assert "131072" not in str(error) and "tokens" not in str(error)


def test_other_bad_requests_are_not_overflow():
    error = _safe_error(status_error(openai, 400, "Invalid model", {"error": {"message": "model not found"}}))
    assert error.code is None and "请求不被服务接受" in str(error)


# Layer 2 building blocks

from mosscode.compaction import (
    BOUNDARY,
    CLIPPED,
    CompactionError,
    collect_quotes,
    compact_history,
    cut_point,
    fit_quotes,
    split_point,
)
from mosscode.messages import Completed, ProviderError
from test_agent import SequenceProvider, response

BLOCK = "abcd" * 2000  # 2,000 tokens.


def chat(pairs):
    return [text(role, f"{role}{i} " + BLOCK) for i in range(pairs) for role in ("user", "assistant")]


NO_SUMMARY_REPLY = Completed(Message("assistant", (ContentBlock("text", "只有分析"),)), "stop")


def summary_reply(body="1. 主要请求和意图：测试", stop="stop"):
    return Completed(
        Message("assistant", (ContentBlock("text", f"<analysis>草稿</analysis>\n<summary>\n{body}\n</summary>"),)), stop
    )


def test_split_keeps_ten_thousand_tokens_and_five_messages_within_the_share():
    history = chat(10)
    assert split_point(history, 100_000) == 15
    assert split_point(history, 20_000) == 17  # 40% cap = 8,000: only three messages fit.
    assert split_point(chat(2), 100_000) == 0
    # Never start the tail on tool results; a single oversized group is still kept whole.
    calls = (ToolCall("t", "dump", "{}"),)
    tooled = chat(3) + [
        Message("assistant", (), tool_calls=calls),
        Message("tool", (), tool_results=(ToolResult("t", "dump", BLOCK * 25),)),
    ]
    assert split_point(tooled, 100_000) == 6  # 50K tokens exceed the 40K share: only the last group stays.
    mixed = tooled[:-1] + [Message("tool", (), tool_results=(ToolResult("t", "dump", "ok"),))] + chat(4)
    for threshold in range(20_000, 120_000, 5_000):
        assert mixed[split_point(mixed, threshold)].role != "tool"
    lone = [Message("user", (ContentBlock("text", "摘要"),), synthetic=BOUNDARY)] + chat(1)[1:]
    assert split_point(lone, 4_000) == 0


def test_manual_cut_keeps_only_the_last_group_when_the_tail_covers_everything():
    assert cut_point(chat(2), 100_000) == 0
    assert cut_point(chat(2), 100_000, manual=True) == 3
    calls = (ToolCall("t", "dump", "{}"),)
    tooled = chat(1) + [
        Message("assistant", (), tool_calls=calls),
        Message("tool", (), tool_results=(ToolResult("t", "dump", "ok"),)),
    ]
    assert cut_point(tooled, 100_000, manual=True) == 2  # The call stays with its result.
    lone = [Message("user", (ContentBlock("text", "摘要"),), synthetic=BOUNDARY)] + chat(1)[1:]
    assert cut_point(lone, 100_000, manual=True) == 0 and cut_point([], 100_000, manual=True) == 0


async def test_summary_request_has_no_tools_and_the_boundary_keeps_user_words(tmp_path):
    session = Session.create(tmp_path)
    history = chat(8)
    history[0] = text("user", "第一条要求：保持中文回复")
    history[3] = Message(
        "assistant", (ContentBlock("text", "看一下"),), tool_calls=(ToolCall("r", "read_file", '{"path": "a.py"}'),)
    )
    history.insert(4, Message("tool", (), tool_results=(ToolResult("r", "read_file", "print('原文')"),)))
    original = list(history)
    provider = SequenceProvider(summary_reply())
    budget = ContextBudget.for_provider(200_000, 8_000)
    done = await compact_history(provider, history, budget, session=session, instructions="侧重接口变化")
    assert history == original  # The input list is never touched.
    messages, system, tools, allow_tools = provider.requests[0]
    prompt = messages[0].text
    assert tools == () and allow_tools is False and "禁止调用任何工具" in system
    assert prompt.startswith(system) and prompt.rstrip().endswith("只输出 <analysis> 和 <summary>。")
    assert "[工具调用] read_file" in prompt and "print('原文')" in prompt and "侧重接口变化" in prompt
    boundary = done.history[0]
    assert (
        boundary.synthetic == BOUNDARY
        and boundary.role == "user"
        and done.removed == len(history) - len(done.history) + 1
    )
    assert "主要请求和意图" in boundary.text and "草稿" not in boundary.text
    assert "第一条要求：保持中文回复" in boundary.text and "修改前必须重新读取" in boundary.text
    assert done.history[1:] == history[done.removed :]
    record = done.record.read_text(encoding="utf-8")
    assert str(done.record) in boundary.text and "print('原文')" in record and done.record.name == "compact-1.md"


@pytest.mark.parametrize(
    "reply, reason",
    [
        (Completed(Message("assistant", (ContentBlock("text", "只有分析"),)), "stop"), "没有有效"),
        (Completed(Message("assistant", (ContentBlock("text", "<summary> </summary>"),)), "stop"), "没有有效"),
        (summary_reply(stop="max_tokens"), "截断"),
        (Completed(Message("assistant", (), tool_calls=(ToolCall("x", "read_file", "{}"),)), "tool_calls"), "工具调用"),
        (ProviderError("服务暂时不可用。"), "服务暂时不可用"),
    ],
)
async def test_failed_summaries_leave_the_history_unchanged(reply, reason):
    with pytest.raises(CompactionError, match=reason) as caught:
        await compact_history(SequenceProvider(reply), chat(8), ContextBudget.for_provider(200_000, 8_000))
    assert caught.value.code == "failed"
    with pytest.raises(CompactionError) as nothing:
        await compact_history(SequenceProvider(), chat(2), ContextBudget.for_provider(200_000, 8_000))
    assert nothing.value.code == "nothing"
    with pytest.raises(CompactionError, match="没有有效"):
        await compact_history(
            SequenceProvider(NO_SUMMARY_REPLY), chat(2), ContextBudget.for_provider(200_000, 8_000), manual=True
        )


async def test_oversized_summary_requests_shrink_results_then_drop_the_oldest():
    calls = (ToolCall("t", "dump", "{}"),)
    history = []
    for i in range(10):
        history += [
            text("user", f"第 {i} 步"),
            Message("assistant", (), tool_calls=calls),
            Message("tool", (), tool_results=(ToolResult("t", "dump", "x" * 40_000),)),
        ]
    history += chat(3)
    # manual limit 37,000: ten 10K-token results only fit once each is cut to 2,000 characters.
    provider = SequenceProvider(summary_reply())
    await compact_history(provider, history, ContextBudget.for_provider(60_000, 4_096))
    prompt = provider.requests[0][0][0].text
    assert "已缩短，原文 40000 字符" in prompt and "第 0 步" in prompt
    # The service still says too long: drop the earliest 20% next.
    overflow = ProviderError("请求超出模型的上下文窗口（HTTP 400）。", code="context_overflow")
    provider = SequenceProvider(overflow, summary_reply())
    await compact_history(provider, history, ContextBudget.for_provider(60_000, 4_096))
    assert len(provider.requests) == 2
    assert "因篇幅省略" in provider.requests[1][0][0].text and "第 0 步" not in provider.requests[1][0][0].text
    provider = SequenceProvider(overflow, overflow, overflow)
    with pytest.raises(CompactionError, match="缩减 3 次"):
        await compact_history(provider, history, ContextBudget.for_provider(60_000, 4_096))


async def test_user_words_are_carried_across_compactions_within_the_budget():
    first = await compact_history(
        SequenceProvider(summary_reply()),
        [text("user", "最初的需求")] + chat(8),
        ContextBudget.for_provider(200_000, 8_000),
    )
    later = first.history + chat(8)
    quotes = collect_quotes(later[:1])
    assert quotes[0] == "最初的需求" and quotes[1].endswith(CLIPPED)  # Older words over 8K are clipped.
    second = await compact_history(SequenceProvider(summary_reply()), later, ContextBudget.for_provider(200_000, 8_000))
    assert "最初的需求" in second.history[0].text
    kept, omitted = fit_quotes(["早" * 9_000, "中" * 3_000, "新" * 4_000], budget=8_000)
    assert kept == ["早" * 200 + CLIPPED, "中" * 3_000, "新" * 4_000] and omitted == 0
    assert fit_quotes(["x"] * 3, budget=0) == ([], 3)


# The agent compacts before requests, rescues overflows and trips a breaker

from mosscode.conversation import Conversation
from mosscode.messages import CompactFailed, CompactFinished, CompactStarted

SMALL = ContextBudget.for_provider(60_000, 4_096)  # auto threshold 27,000; summary input up to 37,000.
NO_SUMMARY = Completed(Message("assistant", (ContentBlock("text", "忘了写摘要"),)), "stop")
OVERFLOW = ProviderError("请求超出模型的上下文窗口（HTTP 400）。", code="context_overflow")


async def test_summary_requests_count_in_usage_but_not_in_the_hit_rate():
    from dataclasses import replace

    from mosscode.builtin_commands import _usage_line
    from mosscode.usage import cache_hit_rate

    provider = SequenceProvider(
        response("一", usage=Usage(1_000, 10, 900, 0)),
        replace(summary_reply(), usage=Usage(3_000, 200, 0, 0)),
        response("二", usage=Usage(1_200, 10, 1_000, 0)),
    )
    conv = seeded(provider, chat(8), auto_compact=False)
    await conv.ask("一")
    [e async for e in conv.compact()]
    await conv.ask("二")
    usage = conv.total_usage
    assert usage.requests == 3 and usage.input_tokens == 5_200 and usage.output_tokens == 220
    assert (usage.side_requests, usage.side_input_tokens) == (1, 3_000)
    assert cache_hit_rate(usage) == 86  # 1,900 of the conversation's 2,200, the summary left out.
    line = _usage_line(conv)
    assert "对话缓存命中率 86%" in line and "其中摘要、目标判断和记忆整理 1 次，输入 3,000" in line


def seeded(provider, history, **options):
    conv = Conversation(provider, budget=SMALL, **options)
    conv._agent.history = list(history)
    return conv


async def test_threshold_reached_compacts_before_the_request():
    provider = SequenceProvider(summary_reply(), response("好"))
    conv = seeded(provider, chat(8), context_collapse=False)  # About 32K tokens, estimated in full: over 27K.
    events = [e async for e in conv.reply("继续")]
    started = [e for e in events if isinstance(e, CompactStarted)]
    finished = [e for e in events if isinstance(e, CompactFinished)]
    assert len(started) == 1 and not started[0].manual and finished[0].removed > 0
    assert provider.requests[0][3] is False and provider.requests[1][3] is True
    sent = provider.requests[1][0]
    assert sent[0].synthetic == "compact" and sent[-1].text == "继续"
    assert conv.compactions == 1 and conv.history[0].synthetic == "compact"
    assert conv.context_estimate() < SMALL.auto_threshold


async def test_estimate_is_anchored_on_the_reported_usage():
    provider = SequenceProvider(response("一", usage=Usage(27_000, 0)), summary_reply(), response("二"))
    conv = seeded(provider, chat(4))  # About 16K: no compaction on the first request.
    await conv.ask("第一问")
    assert conv.compactions == 0 and conv.context_estimate() >= 27_000
    await conv.ask("第二问")
    assert conv.compactions == 1 and len(provider.requests) == 3


async def test_overflow_compacts_and_retries_once():
    provider = SequenceProvider(OVERFLOW, summary_reply(), response("好"))
    conv = seeded(provider, chat(4))
    assert await conv.ask("继续") == "好"
    assert conv.compactions == 1 and provider.requests[2][0][0].synthetic == "compact"
    provider = SequenceProvider(OVERFLOW, summary_reply(), OVERFLOW)
    conv = seeded(provider, chat(4))
    events = []
    with pytest.raises(ProviderError, match="上下文窗口"):
        async for event in conv.reply("继续"):
            events.append(event)
    assert events[-1].reason == "error" and len(provider.requests) == 3


async def test_three_failures_pause_automatic_compaction_until_a_manual_success():
    provider = SequenceProvider(
        NO_SUMMARY,
        response("1"),
        NO_SUMMARY,
        response("2"),
        NO_SUMMARY,
        response("3"),
        response("4"),
        summary_reply(),
        response("5"),
    )
    conv = seeded(provider, chat(8), context_collapse=False)
    seen = []
    for prompt in ("a", "b", "c"):
        before = len(conv.history)
        events = [e async for e in conv.reply(prompt)]
        failed = [e for e in events if isinstance(e, CompactFailed)]
        seen.append((failed[0].consecutive, failed[0].paused))
        assert len(conv.history) == before + 2  # History unchanged by the failure; the task went on.
    assert seen == [(1, False), (2, False), (3, True)] and conv.auto_compact_paused
    events = [e async for e in conv.reply("d")]
    assert not any(isinstance(e, CompactStarted) for e in events)
    manual = [e async for e in conv.compact("侧重待办")]
    assert isinstance(manual[0], CompactStarted) and manual[0].manual and isinstance(manual[-1], CompactFinished)
    assert not conv.auto_compact_paused and "侧重待办" in provider.requests[-1][0][0].text
    await conv.ask("e")
    assert provider.requests[-1][0][0].synthetic == "compact"


async def test_threshold_reached_with_nothing_older_skips_silently():
    provider = SequenceProvider(response("一", usage=Usage(27_000, 0)), response("二"))
    conv = seeded(provider, chat(2))
    await conv.ask("第一问")
    events = [e async for e in conv.reply("第二问")]  # Over the threshold, but the tail is the whole history.
    assert not any(isinstance(e, (CompactStarted, CompactFailed)) for e in events)
    assert conv._agent.compact_failures == 0 and len(provider.requests) == 2


async def test_manual_compaction_of_a_short_history_keeps_the_last_reply():
    conv = seeded(SequenceProvider(summary_reply()), chat(2))
    events = [e async for e in conv.compact()]
    assert isinstance(events[0], CompactStarted) and isinstance(events[-1], CompactFinished)
    assert [m.synthetic for m in conv.history] == ["compact", None] and conv.history[1].role == "assistant"
    events = [e async for e in conv.compact()]
    assert len(events) == 1 and events[0].code == "nothing" and events[0].consecutive == 0
    assert "没有可压缩" in events[0].reason and events[0].manual
    conv._agent.busy = True
    with pytest.raises(ProviderError, match="正在运行"):
        async for _ in conv.compact():
            pass


async def test_compaction_forgets_reads_and_repeats_the_mode_reminder(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("old", encoding="utf-8")
    reg = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    provider = SequenceProvider(response("一"), summary_reply(), response("二"), response("三"))
    conv = seeded(provider, chat(8), registry=reg, auto_compact=False)
    conv.set_mode("plan")
    assert (await reg.execute(ToolCall("r", "read_file", '{"path": "a.txt"}'))).is_error is False
    await conv.ask("一")
    [e async for e in conv.compact()]
    await conv.ask("二")
    await conv.ask("三")
    # Told at the end of the first request and again once the summary replaced it, not after that.
    told = [r[0][-1].synthetic == REMINDER and "计划模式" in r[0][-1].text for r in provider.requests if r[3]]
    assert told == [True, True, False]
    assert all(s.kind != "system-reminder" for r in provider.requests if r[3] for s in r[1].supplements)
    edit = ToolCall("e", "edit_file", json.dumps({"path": "a.txt", "old_text": "old", "new_text": "new"}))
    conv.set_mode("normal")
    assert (await reg.execute(edit)).code == "not_read"


def test_clear_and_model_switch_reset_the_breaker():
    conv = seeded(SequenceProvider(), chat(2))
    conv._agent.compact_failures = 3
    conv._agent.clear()
    assert not conv.auto_compact_paused
    conv._agent.compact_failures = 3
    conv._agent.set_provider(SequenceProvider(), ContextBudget.for_provider(1_000_000, 8_000))
    assert not conv.auto_compact_paused and conv.budget.window == 1_000_000


# Application wiring — TUI, headless output, startup notices, lazy session directory

from textual.widgets import Static

from mosscode import cli
from mosscode.appearance import compact_notice
from mosscode.messages import ToolProgress
from mosscode.tui import ChatEntry, CompactRow, MossCodeApp, PromptArea, ToolEntry


def notes(app):
    return [str(w.render()) for w in app.query(".local-note")]


async def test_tui_shows_automatic_compaction_before_the_reply_and_the_context_share():
    provider = SequenceProvider(summary_reply(), response("好", usage=Usage(20_000, 100)))
    app = MossCodeApp(seeded(provider, chat(8), context_collapse=False), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "继续"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        children = list(app.query_one("#chat").children)
        note = next(w for w in children if "已压缩上下文" in str(w.render()))
        reply = [w for w in children if isinstance(w, ChatEntry)][-1]
        assert children.index(note) < children.index(reply) and reply.body == "好"
        assert "（50%）" in str(app.query_one("#status", Static).render())


async def test_tui_compact_command_runs_idle_with_instructions():
    provider = SequenceProvider(summary_reply(), summary_reply())
    app = MossCodeApp(seeded(provider, chat(8)), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "/compact 侧重测试"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        assert any("已压缩上下文" in n for n in notes(app)) and "侧重测试" in provider.requests[0][0][0].text
        assert app.conversation.history[0].synthetic == "compact" and not app._generating
        for _ in range(2):  # Right after a compaction, /compact still folds all but the last reply.
            app.query_one("#prompt", PromptArea).value = "/compact"
            await pilot.press("enter")
            await app._reply_task
            await pilot.pause()
        assert len(provider.requests) == 2 and "没有可压缩" in notes(app)[-1]
        assert all(row.transcript is not None for row in app.query(CompactRow))


async def test_tui_shows_a_live_row_while_compacting_and_turns_it_into_the_result():
    gate = asyncio.Event()

    async def hold():
        await gate.wait()
        yield summary_reply()

    provider = SequenceProvider(hold, response("好"))
    app = MossCodeApp(seeded(provider, chat(8), context_collapse=False), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "继续"
        await pilot.press("enter")
        await until(lambda: app.query(CompactRow))
        await pilot.pause()
        row = app.query_one(CompactRow)
        assert "上下文接近上限，正在压缩" in str(row.render()) and row.transcript is None
        await until(lambda: "压缩上下文中" in str(app.query_one("#activity", Static).render()))  # The next tick.
        gate.set()
        await app._reply_task
        await pilot.pause()
        assert row.transcript is not None and "已压缩上下文" in row.transcript.plain
        assert len(app.query(CompactRow)) == 1


async def test_tui_cancelling_a_compaction_finishes_its_row():
    gate = asyncio.Event()

    async def hold():
        await gate.wait()
        yield summary_reply()

    app = MossCodeApp(seeded(SequenceProvider(hold), chat(8)), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "/compact"
        await pilot.press("enter")
        await until(lambda: app.query(CompactRow))
        await pilot.press("escape")
        row = app.query_one(CompactRow)
        await until(lambda: row.transcript is not None)
        assert "已取消压缩" in row.transcript.plain
        assert app.conversation.history[0].synthetic is None and not app._generating


async def test_tool_row_names_the_stored_file(tmp_path):
    app = MossCodeApp(Conversation(SequenceProvider()), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        call = ToolCall("c", "execute_command", '{"command": "x"}')
        row = ToolEntry(ToolProgress(call, "running"), None)
        await app.query_one("#chat").mount(row)
        row.complete(ToolResult("c", "execute_command", "预览", details={"persisted": "D:/s/tool-results/c.txt"}), 0.1)
        await pilot.pause()
        assert "完整结果已存盘：D:/s/tool-results/c.txt" in row.transcript.plain


def test_compact_notices_cover_success_failure_and_pause():
    assert (
        compact_notice(CompactFinished(12, 900, 3.25)) == "✓ 已压缩上下文 · 12 条消息 → 摘要 · 约 900 Token · 用时 3.2s"
    )
    assert compact_notice(CompactFailed("超时", 1)).startswith("! 自动压缩上下文失败（连续 1 次）")
    assert "/compact 手动重试或 /clear" in compact_notice(CompactFailed("超时", 3, True))
    assert compact_notice(CompactFailed("超时", 0, manual=True)) == "! 压缩上下文失败：超时"


async def test_headless_output_counts_compactions(capsys):
    conv = seeded(SequenceProvider(summary_reply(), response("好")), chat(8), context_collapse=False)
    result = await cli._prompt(conv, "继续", output_format="stream-json")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["subtype"] for e in lines if e["type"] == "compact"] == ["started", "finished"]
    assert result["compactions"] == 1 and lines[-1]["compactions"] == 1
    conv = seeded(SequenceProvider(summary_reply(), response("好")), chat(8), context_collapse=False)
    await cli._prompt(conv, "继续")
    captured = capsys.readouterr()
    assert "正在压缩上下文" in captured.err and "已压缩上下文" in captured.err and "好" in captured.out


async def test_startup_notices_are_reported_once_and_the_session_directory_is_lazy(tmp_path):
    conv = Conversation(SequenceProvider(), notices=("未配置 context_window，按 128K 估算上下文。",))
    assert await conv.start() == ("未配置 context_window，按 128K 估算上下文。",)
    assert await conv.start() == ()
    session = Session.create(tmp_path, lazy=True)
    assert not session.directory.exists()
    session.persist_result("a", "x")
    assert (session.directory / MARKER).is_file()
    assert session.write_record("一").name == "compact-1.md" and session.write_record("二").name == "compact-2.md"


# A compacted history is a valid request in all three protocols; compaction also happens mid-task

from test_providers import CASES, SSEBody, adapter


@pytest.mark.parametrize("protocol,cls,plain_frames,path", CASES)
async def test_compacted_history_is_a_valid_request_in_every_protocol(protocol, cls, plain_frames, path):
    batches = iter([plain_frames()])
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(next(batches)))
    try:
        conv = Conversation(provider)
        conv._agent.history = [
            Message(
                "user",
                (ContentBlock("text", "（上下文已压缩）"), ContentBlock("text", "\n\n摘要：读过 a.py")),
                synthetic="compact",
            ),
            Message("assistant", (), tool_calls=(ToolCall("t1", "read_file", '{"path": "a.py"}'),)),
            Message("tool", (), tool_results=(ToolResult("t1", "read_file", "print(1)"),)),
            text("assistant", "读完了"),
        ]
        assert await conv.ask("继续") == "你好"
        body = requests[0][1]
        if protocol == "anthropic-messages":
            roles = [m["role"] for m in body["messages"]]
            assert roles == ["user", "assistant", "user", "assistant", "user"]
            assert body["messages"][0]["content"][0]["text"].startswith("（上下文已压缩）")
        elif protocol == "openai-responses":
            kinds = [item.get("type", item.get("role")) for item in body["input"]]
            assert kinds.index("function_call") < kinds.index("function_call_output")
            texts = [json.dumps(item, ensure_ascii=False) for item in body["input"]]
            summary = next(i for i, t in enumerate(texts) if "摘要：读过 a.py" in t)
            assert summary < kinds.index("function_call")
        else:
            roles = [m["role"] for m in body["messages"] if m["role"] != "system"]
            assert roles == ["user", "assistant", "tool", "assistant", "user"]
    finally:
        await provider.close()


async def test_compaction_also_happens_between_iterations_of_one_task(tmp_path):
    (tmp_path / "project").mkdir()
    reg = dump_registry(tmp_path)
    provider = SequenceProvider(
        response("先看", [dump("d1", 40_000)], Usage(26_000, 100)), summary_reply(), response("完成")
    )
    conv = seeded(provider, chat(4), registry=reg)  # About 16K at the start: no compaction yet.
    events = [e async for e in conv.reply("开始")]
    kinds = [type(e).__name__ for e in events]
    assert kinds.index("CompactStarted") > kinds.index("ToolProgress")  # After the tool round, before request two.
    assert provider.requests[2][0][0].synthetic == "compact" and provider.requests[2][0][-1].role == "tool"


# Stored results keep later requests byte-identical prefixes; A failed rescue reports the overflow;
# Cancelling a compaction leaves the history unchanged


async def test_requests_keep_the_previous_request_as_a_prefix(tmp_path):
    (tmp_path / "project").mkdir()
    reg = dump_registry(tmp_path)
    provider = SequenceProvider(
        response("一", [dump("a", 60_000)]), response("二", [dump("b", 60_000)]), response("完")
    )
    conv = Conversation(provider, registry=reg)
    await conv.ask("开始")
    first, second = provider.requests[1][0], provider.requests[2][0]
    assert second[: len(first)] == first and first[-1].tool_results[0].details["persisted"]


async def test_overflow_with_a_failed_compaction_stops_with_the_overflow_error():
    conv = seeded(SequenceProvider(OVERFLOW, NO_SUMMARY), chat(4))
    events = []
    with pytest.raises(ProviderError, match="上下文窗口") as caught:
        async for event in conv.reply("继续"):
            events.append(event)
    assert caught.value.code == "context_overflow" and any(isinstance(e, CompactFailed) for e in events)
    assert len(conv.history) == 8


async def test_cancelled_compaction_leaves_the_history_unchanged():
    gate = asyncio.Event()

    async def hold():
        await gate.wait()
        yield summary_reply()

    conv = seeded(SequenceProvider(hold), chat(8))
    before = conv.history

    async def run():
        async for _ in conv.compact():
            pass

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conv.history == before and not conv._busy


# Due compaction clears older tool results first, when that alone frees enough

from mosscode.compaction import CLEAR_MINIMUM, clear_results, clearable_tokens
from mosscode.messages import ResultsCleared


def step(index, name="execute_command", size=16_000, details=None):
    """One exchange whose tool result is about size / 4 tokens."""
    return [
        text("user", f"第 {index} 步"),
        Message("assistant", (ContentBlock("text", f"执行 {index}"),), tool_calls=(ToolCall(f"c{index}", name, "{}"),)),
        Message(
            "tool",
            (),
            tool_results=(ToolResult(f"c{index}", name, f"out{index} " + "x" * size, details=details or {}),),
        ),
        text("assistant", f"第 {index} 步完成"),
    ]


def steps(count, **options):
    return [message for index in range(count) for message in step(index, **options)]


def result_of(history, call_id):
    return next(r for m in history for r in m.tool_results if r.call_id == call_id)


def test_clearing_stores_what_cannot_be_read_again_and_leaves_the_rest(tmp_path):
    session = Session.create(tmp_path)
    stored = session.persist_result("old", "完整的旧输出")
    history = [
        *step(0),
        *step(1, "read_file", details={"path": "/p/a.py", "lines": 10}),
        *step(2, "ask_user"),
        *step(3, "grep", size=400),  # 100 tokens: under the minimum.
        *step(4, "mcp__db__query"),
        *step(5, "execute_command", details={"persisted": str(stored), "budgeted": True}),
        *step(6, "read_file", details={"path": "/p/b.py", "lines": 10}),
        *step(7, "read_file", size=40, details={"path": "/p/b.py", "lines": 1}),
        *step(8),
    ]
    original = list(history)
    cut = 32  # Everything but the last step.
    assert clearable_tokens(history, cut) > 5 * 3_800
    done = clear_results(history, cut, session)
    assert history == original and len(done.history) == len(history)
    assert sorted(done.results) == ["c0", "c1", "c4", "c5", "c6"]
    assert done.history[:2] == history[:2] and done.history[cut:] == history[cut:]
    command = result_of(done.history, "c0")
    saved = Path(command.details["persisted"])
    assert saved.read_text(encoding="utf-8") == result_of(history, "c0").output
    assert str(saved) in command.output and "不要凭记忆补写" in command.output and command.details["cleared"]
    read = result_of(done.history, "c1")
    assert "重新读取这个文件" in read.output and "persisted" not in read.details and read.details["path"] == "/p/a.py"
    assert Path(result_of(done.history, "c4").details["persisted"]).name == "c4.txt"
    assert str(stored) in result_of(done.history, "c5").output  # A stored preview keeps its file.
    for kept in ("c2", "c3", "c7", "c8"):
        assert result_of(done.history, kept) == result_of(history, kept)
    # b.py still has a read the model can see; a.py has none.
    assert done.paths == ("/p/a.py",)
    assert 5 * 3_800 < done.tokens <= clearable_tokens(history, cut) + 5 * 100
    for result in done.results.values():
        assert estimate_text(result.model_text()) < CLEAR_MINIMUM
    again = clear_results(done.history, cut, session)
    assert not again.results and again.tokens == 0 and again.history == done.history


def test_without_a_place_to_store_them_only_reads_are_cleared(tmp_path):
    history = [*step(0), *step(1, "read_file", details={"path": "/p/a.py", "lines": 10}), *step(2)]
    assert sorted(clear_results(history, 8, None).results) == ["c1"]
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    unwritable = Session(blocked / "session")
    assert sorted(clear_results(history, 8, unwritable).results) == ["c1"]


async def test_due_compaction_clears_results_and_sends_no_summary_request(tmp_path):
    provider = SequenceProvider(response("好"), response("再好"))
    history = steps(8)  # About 32K tokens, nearly all tool results: over 27K.
    conv = seeded(provider, history, session=Session.create(tmp_path))
    events = [e async for e in conv.reply("继续")]
    cleared = [e for e in events if isinstance(e, ResultsCleared)]
    assert len(cleared) == 1 and cleared[0].results == 6 and cleared[0].tokens > 20_000
    assert not any(isinstance(e, (CompactStarted, CompactFinished, CompactFailed)) for e in events)
    assert len(provider.requests) == 1 and provider.requests[0][3] is True
    sent = provider.requests[0][0]
    assert [m.role for m in sent[: len(history)]] == [m.role for m in history] and sent[-1].text == "继续"
    assert [m.text for m in sent[: len(history)] if m.role != "tool"] == [m.text for m in history if m.role != "tool"]
    assert "已清理" in result_of(sent, "c0").output and result_of(sent, "c7") == result_of(history, "c7")
    assert conv.compactions == 0 and conv.context_estimate() < SMALL.auto_threshold // 2
    # Nothing is due on the next turn, and a cleared result is never touched again.
    events = [e async for e in conv.reply("再继续")]
    assert not any(isinstance(e, (ResultsCleared, CompactStarted)) for e in events)
    assert provider.requests[1][0][: len(sent)] == sent


async def test_a_history_that_clearing_cannot_bring_down_is_summarized_untouched(tmp_path):
    session = Session.create(tmp_path)
    provider = SequenceProvider(summary_reply(), response("好"))
    history = steps(2) + chat(6)  # 8K of results in 32K: clearing leaves about 24K, over half the threshold.
    conv = seeded(provider, history, session=session, context_collapse=False)
    events = [e async for e in conv.reply("继续")]
    assert not any(isinstance(e, ResultsCleared) for e in events)
    assert any(isinstance(e, CompactFinished) for e in events) and conv.compactions == 1
    assert "out0 xxxx" in provider.requests[0][0][0].text  # The summary read the results as they were.
    assert not (session.directory / "tool-results").exists()


async def test_overflow_rescue_and_manual_compaction_always_summarize(tmp_path):
    provider = SequenceProvider(OVERFLOW, summary_reply(), response("好"))
    conv = seeded(provider, steps(5), session=Session.create(tmp_path / "a"))  # About 20K: under the threshold.
    events = [e async for e in conv.reply("继续")]
    assert not any(isinstance(e, ResultsCleared) for e in events) and conv.compactions == 1
    conv = seeded(SequenceProvider(summary_reply()), steps(8), session=Session.create(tmp_path / "b"))
    events = [e async for e in conv.compact()]
    assert isinstance(events[0], CompactStarted) and isinstance(events[-1], CompactFinished)
    conv = seeded(
        SequenceProvider(response("好")), steps(8), session=Session.create(tmp_path / "c"), auto_compact=False
    )
    events = [e async for e in conv.reply("继续")]
    assert not any(isinstance(e, ResultsCleared) for e in events) and list(conv.history[: len(steps(8))]) == steps(8)


async def test_a_file_whose_read_was_cleared_needs_a_fresh_read_before_an_edit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("MARK\n" + "line of text\n" * 3_000, encoding="utf-8")
    (project / "b.txt").write_text("old-b", encoding="utf-8")
    reg = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    big = await reg.execute(ToolCall("ra", "read_file", '{"path": "a.txt"}'))
    small = await reg.execute(ToolCall("rb", "read_file", '{"path": "b.txt"}'))
    assert not big.is_error and estimate_text(big.output) > 9_000

    def read(result, path):
        return [
            Message("assistant", (), tool_calls=(ToolCall(result.call_id, "read_file", json.dumps({"path": path})),)),
            Message("tool", (), tool_results=(result,)),
        ]

    history = [text("user", "看两个文件"), *read(big, "a.txt"), *read(small, "b.txt"), *steps(6), *chat(2)]
    # The real tool definitions count too: a threshold of 47,000 leaves room for them under the clearing target.
    conv = Conversation(
        SequenceProvider(response("好")),
        budget=ContextBudget.for_provider(80_000, 4_096),
        registry=reg,
        session=Session.create(tmp_path / "s"),
    )
    conv._agent.history = history
    events = [e async for e in conv.reply("继续")]
    assert [e.results for e in events if isinstance(e, ResultsCleared)] == [6]

    def edit(path, old):
        return ToolCall("e" + path, "edit_file", json.dumps({"path": path, "old_text": old, "new_text": "new"}))

    assert (await reg.execute(edit("a.txt", "MARK"))).code == "not_read"
    assert (await reg.execute(edit("b.txt", "old-b"))).is_error is False
    assert (await reg.execute(ToolCall("r2", "read_file", '{"path": "a.txt", "limit": 5}'))).is_error is False
    assert (await reg.execute(edit("a.txt", "MARK"))).is_error is False


async def test_tui_and_headless_output_report_cleared_results(tmp_path, capsys):
    assert compact_notice(ResultsCleared(6, 23_500)) == "✓ 已清理较早的工具结果 · 6 个 · 约 23500 Token"
    provider = SequenceProvider(response("好", usage=Usage(9_000, 100)))
    app = MossCodeApp(seeded(provider, steps(8), session=Session.create(tmp_path / "a")), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "继续"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        children = list(app.query_one("#chat").children)
        note = next(w for w in children if "已清理较早的工具结果 · 6 个" in str(w.render()))
        reply = [w for w in children if isinstance(w, ChatEntry)][-1]
        assert children.index(note) < children.index(reply) and reply.body == "好"
        assert not list(app.query(CompactRow))
    capsys.readouterr()
    conv = seeded(SequenceProvider(response("好")), steps(8), session=Session.create(tmp_path / "b"))
    result = await cli._prompt(conv, "继续", output_format="stream-json")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    compact = [e for e in lines if e["type"] == "compact"]
    assert (
        [e["subtype"] for e in compact] == ["cleared"] and compact[0]["results"] == 6 and compact[0]["tokens"] > 20_000
    )
    assert result["compactions"] == 0
    conv = seeded(SequenceProvider(response("好")), steps(8), session=Session.create(tmp_path / "c"))
    await cli._prompt(conv, "继续")
    captured = capsys.readouterr()
    assert "已清理较早的工具结果" in captured.err and "正在压缩上下文" not in captured.err and "好" in captured.out


async def test_repeated_compaction_keeps_the_latest_correction_goal_and_checklist(tmp_path):
    from mosscode.messages import REMINDER
    from mosscode.tools import create_default_registry

    provider = SequenceProvider(summary_reply("旧约束摘要"), summary_reply("再次摘要"), response("按最新要求继续"))
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv = seeded(
        provider,
        [text("user", "使用旧接口"), *chat(8), text("user", "纠正：必须使用新接口")],
        registry=registry,
        auto_compact=False,
    )
    conv.set_goal("新接口测试通过")
    registry.todos.replace([(None, "补充新接口的测试", "in_progress")])
    [event async for event in conv.compact()]
    conv._agent.history += chat(8)
    [event async for event in conv.compact()]
    assert await conv.ask("继续") == "按最新要求继续"
    request = provider.requests[-1][0]
    assert any("纠正：必须使用新接口" in m.text for m in request)
    reminders = "\n".join(m.text for m in request if m.synthetic == REMINDER)
    assert "新接口测试通过" in reminders and "补充新接口的测试" in reminders
    await conv.close()
