"""Replies with nothing usable are sent again; replies cut off after some text are continued automatically."""

import asyncio
import json
from dataclasses import replace

import pytest

import mosscode.cli as cli
from mosscode.conversation import Conversation
from mosscode.messages import (
    CONTINUE,
    REMINDER,
    AutoContinue,
    Completed,
    ContentBlock,
    Delta,
    Message,
    ProviderError,
    ReplyRetry,
    RunStopped,
    Usage,
)
from mosscode.prompts import PREFIX_KINDS, Supplement
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response


def empty(stop="stop", *blocks, usage=Usage(5, 1)):
    return Completed(Message("assistant", tuple(blocks)), stop, usage)


def thinking_cut():
    return empty("max_tokens", ContentBlock("thinking", "想了很久"))


def notes(request, kind="reply-recovery"):
    return [s.text for s in request[1].supplements if s.kind == kind]


def prefix(request):
    system = request[1]
    return request[0], system.stable, [s for s in system.supplements if s.kind in PREFIX_KINDS]


async def run(conv, prompt="任务"):
    events = []
    try:
        async for event in conv.reply(prompt):
            events.append(event)
    except ProviderError as exc:
        return events, exc
    return events, None


async def test_an_empty_reply_is_dropped_and_the_same_request_sent_again():
    provider = SequenceProvider(empty(), response("  \n"), response("好的", usage=Usage(5, 2)))
    conv = Conversation(provider)
    events, error = await run(conv)
    assert error is None and [m.role for m in conv.history] == ["user", "assistant"]
    assert conv.history[-1].text == "好的"
    retries = [e for e in events if isinstance(e, ReplyRetry)]
    assert [(e.reason, e.attempt, e.max_attempts) for e in retries] == [
        ("模型返回了空回复", 1, 2),
        ("模型返回了空回复", 2, 2),
    ]
    assert events[-1] == RunStopped("completed", "", 1)
    first, second, third = provider.requests
    assert notes(first) == [] and "请继续" in notes(second)[0] and notes(third) == notes(second)
    # The note follows the history: the cached prefix is the same byte for byte.
    assert prefix(first) == prefix(second) == prefix(third)
    assert conv.total_usage.requests == 3 and conv.total_usage.output_tokens == 3  # The dropped reply counts too.


async def test_after_tool_results_the_note_points_at_them(tmp_path):
    (tmp_path / "a.txt").write_text("内容")
    provider = SequenceProvider(response(calls=[call("read_file", "r", path="a.txt")]), empty(), response("读完了"))
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv = Conversation(provider, registry=registry)
    _, error = await run(conv)
    assert error is None and [m.role for m in conv.history] == ["user", "assistant", "tool", "assistant"]
    assert "根据上方的工具结果继续" in notes(provider.requests[2])[0]
    assert provider.requests[1][0] == provider.requests[2][0]


async def test_a_reply_cut_while_thinking_asks_for_shorter_thinking():
    provider = SequenceProvider(thinking_cut(), response("结论"))
    conv = Conversation(provider)
    events, error = await run(conv)
    assert error is None and conv.history[-1].text == "结论"
    assert [e.reason for e in events if isinstance(e, ReplyRetry)] == ["回复在思考阶段就达到了输出上限"]
    assert "缩短思考" in notes(provider.requests[1])[0]


def cut(text):
    return replace(response(text), stop_reason="max_tokens")


async def test_a_reply_cut_after_some_text_is_continued_where_it_stopped():
    provider = SequenceProvider(cut("第一段，"), cut("第二段，"), response("结尾。"))
    conv = Conversation(provider)
    events, error = await run(conv)
    assert error is None and not any(isinstance(e, ReplyRetry) for e in events)
    assert [m.role for m in conv.history] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert [m.synthetic for m in conv.history if m.role == "user"] == [None, CONTINUE, CONTINUE]
    # Each piece stays in the history as written; the request to go on is the last message sent.
    assert [m.text for m in conv.history if m.role == "assistant"] == ["第一段，", "第二段，", "结尾。"]
    second = provider.requests[1][0]
    assert second[-2].text == "第一段，" and "从中断处直接接着写" in second[-1].text
    assert [(e.attempt, e.max_attempts) for e in events if isinstance(e, AutoContinue)] == [(1, 3), (2, 3)]
    answers = [e.answer for e in events if isinstance(e, Completed)]
    assert answers == ["第一段，", "第一段，第二段，", "第一段，第二段，结尾。"]
    assert events[-1] == RunStopped("completed", "", 3)


async def test_after_three_continuations_the_turn_stops_at_the_output_limit():
    provider = SequenceProvider(*(cut(f"段{n}") for n in range(4)), response("不会用到"))
    conv = Conversation(provider)
    events, error = await run(conv)
    assert error is None and len(provider.requests) == 4
    assert events[-1].reason == "max_tokens" and "已自动续写 3 次" in events[-1].notice
    assert conv.history[-1].text == "段3"  # The last cut piece is kept, as before.
    # The next turn starts afresh: its own cut reply is continued again.
    provider.responses = [cut("再一段"), response("完")]
    assert await conv.ask("继续") == "再一段完"


async def test_a_cut_reply_with_tool_calls_is_not_continued(tmp_path):
    truncated = replace(
        response("写文件", [call("write_file", "w", path="a.txt", content="x")]), stop_reason="max_tokens"
    )
    provider = SequenceProvider(truncated, response("改为分段写入"))
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv = Conversation(provider, registry=registry)
    events, _ = await run(conv)
    assert not any(isinstance(e, AutoContinue) for e in events) and not (tmp_path / "a.txt").exists()
    assert conv.history[2].tool_results[0].code == "output_truncated"


async def test_the_whole_answer_reaches_ask_and_headless_results(capsys):
    assert await Conversation(SequenceProvider(cut("前半"), response("后半"))).ask("任务") == "前半后半"
    conv = Conversation(SequenceProvider(cut("前半"), response("后半")))
    result = await cli._prompt(conv, "任务", output_format="json")
    assert result["subtype"] == "completed" and result["result"] == "前半后半"
    capsys.readouterr()
    conv = Conversation(SequenceProvider(cut("前半"), response("后半")))
    await cli._prompt(conv, "任务", output_format="stream-json")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"type": "auto_continue", "attempt": 1, "max_attempts": 3} in lines
    assert lines[-1]["result"] == "前半后半"
    conv = Conversation(SequenceProvider(cut("前半"), response("后半")))
    await cli._prompt(conv, "任务")
    captured = capsys.readouterr()
    assert "前半后半" in captured.out and "回复达到输出上限，自动续写（1/3）" in captured.err


async def test_a_sub_agent_returns_the_whole_answer(tmp_path):
    from test_subagents import ScriptProvider, agents_dir, conversation, main_requests, tool_results_of, write_agent

    write_agent(agents_dir(tmp_path), "worker", "tools: Read\n", "SUB-WORKER")

    def script(messages, system, tools):
        if "SUB-WORKER" in system.stable:
            return cut("前半") if len(messages) == 1 else response("后半")
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a1", description="写", prompt="写长文", agent_type="worker")])
        return response("主对话完成")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await run(conv, "开始")
    assert tool_results_of(main_requests(provider)[1])[0].output.startswith("前半后半")
    await conv.close()


async def test_the_interface_shows_each_piece_with_a_note_between():
    from rich.console import Console

    from mosscode.tui import ChatEntry, MossCodeApp, resumed_rows
    from test_agent_tui import send

    conv = Conversation(SequenceProvider(cut("前半"), response("后半")))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "任务")
        await app._reply_task
        await pilot.pause()
        replies = [e for e in app.query(ChatEntry) if e.speaker != "You"]
        assert [e.body for e in replies] == ["前半", "后半"]
        # The cut piece has no done summary; the note says what happened instead.
        assert replies[0].completed_elapsed is None and replies[1].completed_elapsed is not None
        notes = [str(note.render()) for note in app.query(".local-note")]
        assert "↻ 回复达到输出上限，自动续写（1/3）" in notes
        assert not app.query_one("#prompt").disabled
    console = Console(record=True, width=80)
    for row in resumed_rows(conv.history):
        console.print(row)
    shown = console.export_text()
    assert "↻ 回复达到输出上限，自动续写" in shown and "从中断处" not in shown


@pytest.mark.parametrize(
    "replies, expected",
    [
        ((empty(), empty(), empty()), ["连续 3 次", "停止原因：stop", "内容块：无", "重新发送"]),
        ((thinking_cut(),) * 3, ["停止原因：max_tokens", "内容块：thinking", "max_output_tokens"]),
        ((empty("stop", ContentBlock("text", " ")),) * 3, ["内容块：空 text"]),
    ],
)
async def test_after_two_retries_the_turn_ends_with_a_diagnosis(replies, expected):
    provider = SequenceProvider(*replies, response("不会用到"))
    conv = Conversation(provider)
    events, error = await run(conv)
    assert error is not None and error.code == "empty_reply" and len(provider.requests) == 3
    assert events[-1].reason == "error" and all(part in events[-1].notice for part in expected)
    assert list(conv.history) == []  # Neither the empty replies nor the unanswered prompt.
    assert await conv.ask("再试") == "不会用到"  # The session still works.


@pytest.mark.parametrize("stop", ["refusal", "content_filter"])
async def test_a_refusal_is_reported_without_sending_again(stop):
    provider = SequenceProvider(empty(stop))
    events, error = await run(Conversation(provider))
    assert error is not None and len(provider.requests) == 1
    assert "服务拒绝了这次请求" in str(error) and stop in str(error)


async def test_one_time_reminders_go_with_the_repeated_request():
    provider = SequenceProvider(empty(), response("好"))
    conv = Conversation(provider)
    conv._agent.remind(Supplement("system-reminder", "距离上次对话已经很久"))
    await run(conv)
    told = [[m.text for m in r[0] if m.synthetic == REMINDER] for r in provider.requests]
    assert told[0] == told[1] and len(told[0]) == 1 and "距离上次对话已经很久" in told[0][0]
    # Kept once in the history, with the message it came with.
    assert [m.text for m in conv.history if m.synthetic == REMINDER] == told[0]
    assert all(s.kind != "system-reminder" for r in provider.requests for s in r[1].supplements)


async def test_headless_output_reports_the_retry(capsys):
    conv = Conversation(SequenceProvider(empty(), response("好")))
    await cli._prompt(conv, "任务", output_format="stream-json")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"type": "reply_retry", "attempt": 1, "max_attempts": 2, "reason": "模型返回了空回复"} in lines
    conv = Conversation(SequenceProvider(empty(), response("好")))
    await cli._prompt(conv, "任务")
    captured = capsys.readouterr()
    assert "模型返回了空回复，重新请求（1/2）" in captured.err and "好" in captured.out


async def test_the_interface_drops_the_discarded_thinking_and_shows_the_retry():
    from mosscode.tui import ChatEntry, MossCodeApp
    from test_agent_tui import send
    from test_tui import answer_rows

    arrived, release = asyncio.Event(), asyncio.Event()

    async def cut():
        yield Delta("thinking", "想了很久")
        yield thinking_cut()

    async def answer():
        arrived.set()
        await release.wait()
        yield Delta("text", "结论")
        yield response("结论")

    app = MossCodeApp(Conversation(SequenceProvider(cut, answer)), "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "任务")
        await asyncio.wait_for(arrived.wait(), 3)
        await pilot.pause(0.3)
        assert "回复在思考阶段就达到了输出上限，重新请求（1/2）" in str(app.query_one("#status").render())
        replies = [e for e in app.query(ChatEntry) if e.speaker != "You"]
        assert len(replies) == 1 and replies[0].thinking == ""
        assert answer_rows(app, replies[0]) == ["等待回复…"]
        release.set()
        await app._reply_task
        await pilot.pause()
        replies = [e for e in app.query(ChatEntry) if e.speaker != "You"]
        assert [(e.body, e.thinking) for e in replies] == [("结论", "")]
