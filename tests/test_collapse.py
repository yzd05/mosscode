"""Collapse: when compaction is due, the oldest turns become a summary in requests only."""

import json
from pathlib import Path

import pytest
import yaml

from mosscode import cli
from mosscode.compaction import COLLAPSE, COLLAPSE_TARGET, clear_results, collapse_end, project, released_reads
from mosscode.config import ConfigError, load_settings
from mosscode.messages import (
    Collapse,
    CompactFinished,
    CompactStarted,
    ContentBlock,
    ContextCollapsed,
    Message,
    ToolResult,
)
from mosscode.session import Session, collapse_record, compact_record, load_session, message_record, rewind_record
from mosscode.tui import ChatEntry, MossCodeApp, PromptArea
from test_agent import SequenceProvider, response
from test_config import write_config
from test_context import BLOCK, NO_SUMMARY, SMALL, seeded, steps, summary_reply, text


def turns(tag, pairs):
    """pairs exchanges of a short typed message (even positions) and a reply of about 4,000 tokens."""
    return [
        message
        for i in range(pairs)
        for message in (text("user", f"{tag}user{i}"), text("assistant", f"{tag}assistant{i} " + BLOCK * 2))
    ]


def summary(end, body="摘要"):
    return Collapse(end, Message("user", (ContentBlock("text", body),), synthetic=COLLAPSE))


def compaction_events(events):
    return [type(e) for e in events if isinstance(e, (CompactStarted, ContextCollapsed, CompactFinished))]


# Choosing the range


def test_a_collapse_takes_the_fewest_whole_turns_and_never_reaches_the_kept_tail():
    history = turns("", 8)
    # 32,000 - freed + 2,000 (the assumed summary) is under 16,200 only after five exchanges of 4,013 tokens.
    assert collapse_end(history, [], 11, 32_000, 16_200) == 10
    assert collapse_end(history, [], 9, 32_000, 16_200) is None  # Enough only past the kept tail.
    assert collapse_end(history, [], 11, 20_000, 16_200) == 4
    # A later collapse starts where the last one ended; cut counts the summary in its place.
    assert collapse_end(history, [summary(4)], 8, 20_000, 16_200) == 8
    assert collapse_end(history, [summary(2), summary(4)], 13, 40_000, 1) is None  # Two summaries at most.
    one_turn = [history[0]] + [m for m in history if m.role == "assistant"]
    assert collapse_end(one_turn, [], 8, 40_000, 1) is None  # No turn boundary to end on.
    # The user's words go into the summary verbatim, so long ones make a range count for less.
    # Two exchanges would do without them (30,000 - 16,026 + 2,000); with about 4,070 tokens of quotes it takes three.
    wordy = [text(m.role, m.text + " " + BLOCK * 2) if m.role == "user" else m for m in history]
    assert collapse_end(wordy, [], 11, 30_000, 16_200) == 6


def test_requests_carry_the_summaries_then_the_history_after_them():
    history = turns("", 4)
    collapses = [summary(2, "一"), summary(4, "二")]
    assert project(history, collapses) == [collapses[0].summary, collapses[1].summary, *history[4:]]
    assert project(history, []) == history


def test_clearing_leaves_collapsed_messages_as_they_were(tmp_path):
    done = clear_results(steps(3), 12, Session.create(tmp_path), start=4)
    assert set(done.results) == {"c1", "c2"}


def test_files_read_only_inside_the_range_must_be_read_again():
    def read(call_id, path):
        return Message("tool", (), tool_results=(ToolResult(call_id, "read_file", "x", details={"path": path}),))

    history = [read("1", "a.py"), read("2", "b.py"), text("user", "下一步"), read("3", "b.py")]
    assert released_reads(history, 0, 2) == ("a.py",)


# In the agent loop


async def test_due_compaction_collapses_the_oldest_turns_and_keeps_the_history(tmp_path):
    history = turns("", 8)  # About 32K tokens against an auto threshold of 27,000.
    provider = SequenceProvider(summary_reply("1. 主要请求和意图：较早的部分"), response("好"))
    conv = seeded(provider, history, session=Session.create(tmp_path))
    events = [e async for e in conv.reply("继续")]
    assert compaction_events(events) == [CompactStarted, ContextCollapsed]
    collapsed = next(e for e in events if isinstance(e, ContextCollapsed))
    agent = conv._agent
    end = agent.collapses[0].end
    assert collapsed.messages == end and end % 2 == 0 and 0 < end < 16 and collapsed.before > collapsed.after
    assert collapsed.after <= SMALL.auto_threshold * COLLAPSE_TARGET
    # Every message is still in the history; only the requests change.
    assert agent.history[:16] == history and len(agent.history) == 18 and conv.compactions == 0
    asked = provider.requests[0][0][0].text
    assert provider.requests[0][3] is False and "assistant0 " in asked and f"assistant{end // 2} " not in asked
    sent = provider.requests[1][0]
    assert sent[0].synthetic == COLLAPSE and "较早的部分" in sent[0].text and collapsed.record in sent[0].text
    assert list(sent[1:-1]) == history[end:] and sent[-1].text == "继续"
    assert Path(collapsed.record).name == "collapse-1.md"
    assert "assistant0 " in Path(collapsed.record).read_text("utf-8")


async def test_a_second_collapse_keeps_the_first_summary_and_a_third_time_summarizes_everything():
    provider = SequenceProvider(summary_reply("第一段"), response("一"))
    conv = seeded(provider, turns("a", 8))
    agent = conv._agent
    [e async for e in conv.reply("甲")]
    first = agent.collapses[0]
    agent.history += turns("b", 4)
    provider.responses += [summary_reply("第二段"), response("二")]
    events = [e async for e in conv.reply("乙")]
    assert compaction_events(events) == [CompactStarted, ContextCollapsed] and agent.collapses[0] is first
    sent = provider.requests[-1][0]
    # The first summary and what came before it are sent as they were: the cached prefix still matches.
    assert sent[0] == first.summary and "第二段" in sent[1].text and sent[2] == agent.history[agent.collapses[1].end]
    agent.history += turns("c", 4)
    provider.responses += [summary_reply("全部"), response("三")]
    events = [e async for e in conv.reply("丙")]
    assert compaction_events(events) == [CompactStarted, CompactFinished] and agent.collapses == []
    asked = provider.requests[-2][0][0].text
    # The full summary reads the collapse summaries in place of what they stand for.
    assert "第一段" in asked and "第二段" in asked and "aassistant0 " not in asked
    assert "全部" in agent.history[0].text and conv.compactions == 1


async def test_a_failed_collapse_falls_back_to_a_full_summary_and_three_turn_collapsing_off():
    conv = seeded(SequenceProvider(NO_SUMMARY, summary_reply(), response("好")), turns("", 8))
    agent = conv._agent
    events = [e async for e in conv.reply("继续")]
    assert compaction_events(events) == [CompactStarted, CompactFinished]
    assert agent.collapse_failures == 1 and agent.compact_failures == 0 and agent.collapses == []
    agent.collapse_failures = 3
    agent.history = turns("", 8)
    agent.provider = provider = SequenceProvider(summary_reply(), response("好"))
    events = [e async for e in conv.reply("继续")]
    assert compaction_events(events) == [CompactStarted, CompactFinished] and len(provider.requests) == 2
    await conv.clear()
    assert agent.collapse_failures == 0


async def test_collapsing_can_be_turned_off():
    provider = SequenceProvider(summary_reply(), response("好"))
    conv = seeded(provider, turns("", 8), context_collapse=False)
    events = [e async for e in conv.reply("继续")]
    assert compaction_events(events) == [CompactStarted, CompactFinished]


def test_rewinding_into_a_collapsed_range_undoes_that_collapse():
    agent = seeded(SequenceProvider(), turns("", 8))._agent
    agent.collapses = [summary(4), summary(8)]
    agent.truncate(10, "m")
    assert [c.end for c in agent.collapses] == [4, 8]
    agent.truncate(6, "m")
    assert [c.end for c in agent.collapses] == [4]
    agent.truncate(2, "m")
    assert agent.collapses == []


# Sessions


def test_collapses_are_saved_and_rebuilt_on_resume(tmp_path):
    history = turns("", 6)
    path = tmp_path / "session.jsonl"
    base = [message_record(m) for m in history] + [collapse_record(summary(4, "一"))]

    def load(*extra):
        lines = (json.dumps(record, ensure_ascii=False) for record in base + list(extra))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return load_session(path)

    restored = load()
    assert restored.history == history and [c.end for c in restored.collapses] == [4]
    assert restored.collapses[0].summary.text == "一" and restored.collapses[0].summary.synthetic == COLLAPSE
    agent = seeded(SequenceProvider(), [])._agent
    agent.restore(restored.history, None, restored.collapses)
    assert agent.projected() == [restored.collapses[0].summary, *history[4:]]
    assert [c.end for c in load(rewind_record(4, "m")).collapses] == [4]
    assert load(rewind_record(3, "m")).collapses == ()
    assert load(compact_record(text("user", "摘要"), 2, None)).collapses == ()
    later = load(collapse_record(summary(8, "二")))
    assert [c.end for c in later.collapses] == [4, 8]
    for broken in (summary(4), summary(99)):  # Not after the last one, or past the history.
        restored = load(collapse_record(broken))
        assert [c.end for c in restored.collapses] == [4] and restored.skipped == 1


# What the user sees


async def test_headless_output_reports_a_collapse(capsys):
    conv = seeded(SequenceProvider(summary_reply(), response("好")), turns("", 8))
    result = await cli._prompt(conv, "继续", output_format="stream-json")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    compact = [e for e in lines if e["type"] == "compact"]
    assert [e["subtype"] for e in compact] == ["started", "collapsed"]
    assert compact[1]["messages"] > 0 and compact[1]["before"] > compact[1]["after"] and result["compactions"] == 0
    conv = seeded(SequenceProvider(summary_reply(), response("好")), turns("", 8))
    await cli._prompt(conv, "继续")
    captured = capsys.readouterr()
    assert "✓ 已折叠较早的对话 · " in captured.err and " 条消息 · 约 " in captured.err and "好" in captured.out


async def test_tui_shows_the_collapse_before_the_reply():
    app = MossCodeApp(seeded(SequenceProvider(summary_reply(), response("好")), turns("", 8)), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "继续"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        children = list(app.query_one("#chat").children)
        note = next(w for w in children if "已折叠较早的对话" in str(w.render()))
        reply = [w for w in children if isinstance(w, ChatEntry)][-1]
        assert children.index(note) < children.index(reply) and reply.body == "好"


def test_context_collapse_is_a_switch_in_the_configuration(tmp_path):
    path = write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert load_settings(path).context_collapse is True
    path.write_text(yaml.safe_dump({**raw, "context_collapse": False}), encoding="utf-8")
    assert load_settings(path).context_collapse is False
    path.write_text(yaml.safe_dump({**raw, "context_collapse": "no"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="context_collapse"):
        load_settings(path)


def test_forks_see_collapsed_turns_as_their_summaries():
    history = turns("", 4)
    conv = seeded(SequenceProvider(), history)
    conv._agent.collapses = [summary(4)]
    assert conv._fork_history("full") == [summary(4).summary, *history[4:]]
