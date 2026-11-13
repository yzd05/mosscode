"""Rounds that repeat the same tool calls and get the same results: a reminder at 3, a stop at 5."""

import json
from dataclasses import replace

import pytest

from mosscode import cli
from mosscode.agent import REPEAT_NUDGE, REPEAT_STOP
from mosscode.conversation import Conversation
from mosscode.messages import REMINDER, STOPPED_EARLY, ToolCall
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response


def conversation(tmp_path, *responses):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    provider = SequenceProvider(*responses)
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    return Conversation(provider, registry=registry), provider


def glob(id="g"):
    return response(calls=[call("glob", id, pattern="*.txt")])


def reminders(request) -> list[str]:
    messages = request[0]
    return [m.text for m in messages if m.synthetic == REMINDER and "连续" in m.text and "相同的参数" in m.text]


async def test_identical_rounds_get_one_reminder_then_stop(tmp_path):
    conv, provider = conversation(tmp_path, *(glob(f"g{n}") for n in range(10)))
    events = [e async for e in conv.reply("找文件")]
    stopped = events[-1]
    assert stopped.reason == "repeated_calls" and stopped.iterations == REPEAT_STOP
    assert "连续 5 轮" in stopped.notice and len(provider.requests) == REPEAT_STOP
    assert reminders(provider.requests[REPEAT_NUDGE - 1]) == []
    told = reminders(provider.requests[REPEAT_NUDGE])
    assert len(told) == 1 and "glob" in told[0]
    # Said once: the later requests carry the same message from the history, not a second one.
    assert len(reminders(provider.requests[-1])) == 1
    assert not conv._busy and "repeated_calls" in STOPPED_EARLY


async def test_spacing_and_key_order_do_not_make_a_repeat_look_new(tmp_path):
    variants = [
        '{"pattern": "*.txt", "path": "."}',
        '{"path":".","pattern":"*.txt"}',
        '{ "pattern" : "*.txt", "path" : "." }',
        '{"path": ".", "pattern":  "*.txt"}',
    ]
    rounds = [response(calls=[ToolCall(f"v{n}", "glob", variants[n % 4])]) for n in range(6)]
    conv, provider = conversation(tmp_path, *rounds)
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "repeated_calls" and len(provider.requests) == REPEAT_STOP


async def test_a_different_round_starts_the_count_again(tmp_path):
    other = response(calls=[call("glob", "o", pattern="*.md")])
    rounds = [glob("a"), glob("b"), glob("c"), glob("d"), other, glob("e"), glob("f"), glob("g"), glob("h")]
    conv, provider = conversation(tmp_path, *rounds, response("完成"))
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "completed" and len(provider.requests) == 10
    # Four in a row before the other call, four after: one reminder each time the count reached three.
    assert len(reminders(provider.requests[-1])) == 2


async def test_the_same_call_with_new_results_is_not_a_repeat(tmp_path):
    def growing(n):
        async def round():
            (tmp_path / f"new{n}.txt").write_text("x", encoding="utf-8")  # Each glob finds one more file.
            yield glob(f"n{n}")

        return round

    conv, provider = conversation(tmp_path, *(growing(n) for n in range(7)), response("完成"))
    events = [e async for e in conv.reply("等文件出现")]
    assert events[-1].reason == "completed" and len(provider.requests) == 8
    assert reminders(provider.requests[-1]) == []


async def test_ask_returns_the_notice_when_rounds_repeat(tmp_path):
    conv, _ = conversation(tmp_path, *(glob(f"g{n}") for n in range(REPEAT_STOP)))
    answer = await conv.ask("找文件")
    assert "连续 5 轮" in answer and "已停止" in answer


async def test_a_goal_pauses_when_rounds_repeat(tmp_path):
    conv, _ = conversation(tmp_path, *(glob(f"g{n}") for n in range(REPEAT_STOP)))
    conv.set_goal("找到所有文件")
    [e async for e in conv.reply("找文件")]
    step = await conv.goal_after_turn()
    assert step.action == "paused" and "重复相同的工具调用" in conv.goal.paused


@pytest.mark.parametrize("kind", ["user", "notification"])
async def test_new_information_during_the_last_batch_restarts_the_count(tmp_path, kind):
    async def steered():
        if kind == "user":
            conv.queue("现在重新查找")
        else:
            conv._agent.notifications.append("后台任务刚刚完成，请重新查找")
        yield glob("steered")

    rounds = [glob(f"g{n}") for n in range(REPEAT_STOP - 1)]
    conv, provider = conversation(tmp_path, *rounds, steered, *rounds, response("完成"))
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "completed" and len(provider.requests) == REPEAT_STOP * 2
    assert any("重新查找" in message.text for message in provider.requests[REPEAT_STOP][0])


async def test_incomplete_calls_break_the_streak_and_are_not_executed(tmp_path):
    rounds = [glob(f"g{n}") for n in range(REPEAT_STOP - 1)]
    truncated = replace(glob("cut"), stop_reason="max_tokens")
    conv, provider = conversation(tmp_path, *rounds, truncated, *rounds, response("完成"))
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "completed" and len(provider.requests) == REPEAT_STOP * 2
    cut_result = next(r for m in conv.history for r in m.tool_results if r.call_id == "cut")
    assert cut_result.code == "output_truncated" and not cut_result.details["executed"]


async def test_an_unsent_reminder_does_not_leak_into_the_next_turn(tmp_path):
    conv, provider = conversation(tmp_path, *(glob(f"g{n}") for n in range(REPEAT_NUDGE)), response("完成"))
    conv._agent.max_iterations = REPEAT_NUDGE
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "max_iterations"
    assert await conv.ask("换个任务") == "完成"
    assert reminders(provider.requests[-1]) == []


async def test_a_new_turn_can_run_after_repeated_calls_stop(tmp_path):
    rounds = [glob(f"g{n}") for n in range(REPEAT_STOP)]
    conv, provider = conversation(tmp_path, *rounds, glob("retry"), response("完成"))
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "repeated_calls"
    assert await conv.ask("再试一次") == "完成"
    assert len(provider.requests) == REPEAT_STOP + 2
    assert conv.history[-1].text == "完成"


async def test_identical_failures_stop_with_their_results_preserved(tmp_path):
    rounds = [response(calls=[call("read_file", f"r{n}", path="missing.txt")]) for n in range(REPEAT_STOP)]
    conv, _ = conversation(tmp_path, *rounds)
    events = [e async for e in conv.reply("读取文件")]
    assert events[-1].reason == "repeated_calls"
    results = [r for m in conv.history for r in m.tool_results]
    assert len(results) == REPEAT_STOP and all(r.is_error for r in results)
    assert events[-1].results == conv.history[-1].tool_results


async def test_multi_tool_rounds_compare_every_call_and_result(tmp_path):
    rounds = [
        response(calls=[call("glob", f"g{n}", pattern="*.txt"), call("glob", f"m{n}", pattern="*.md")])
        for n in range(REPEAT_STOP)
    ]
    conv, _ = conversation(tmp_path, *rounds)
    events = [e async for e in conv.reply("找文件")]
    assert events[-1].reason == "repeated_calls"
    assert len(events[-1].results) == 2
    assert len([r for m in conv.history for r in m.tool_results]) == REPEAT_STOP * 2


@pytest.mark.parametrize("output_format", ["text", "json", "stream-json"])
async def test_headless_formats_report_the_stop(tmp_path, monkeypatch, capsys, output_format):
    monkeypatch.chdir(tmp_path)
    conv, _ = conversation(tmp_path, *(glob(f"g{n}") for n in range(REPEAT_STOP)))
    result = await cli._prompt(conv, "找文件", output_format=output_format)
    output = capsys.readouterr().out
    if output_format == "text":
        assert result is None and "连续 5 轮" in output and "已停止" in output
    else:
        records = [json.loads(line) for line in output.splitlines()]
        assert records[-1] == result
        assert result["subtype"] == "repeated_calls" and result["is_error"]
        assert result["num_turns"] == REPEAT_STOP and "连续 5 轮" in result["result"]
        if output_format == "stream-json":
            assert len([r for r in records if r["type"] == "tool_result"]) == REPEAT_STOP


async def test_changing_image_results_keep_the_loop_running(tmp_path):
    from PIL import Image

    async def changed():
        Image.new("RGB", (8, 8), "blue").save(tmp_path / "image.png")
        yield response(calls=[call("read_file", "changed", path="image.png")])

    Image.new("RGB", (8, 8), "red").save(tmp_path / "image.png")
    reads = [response(calls=[call("read_file", f"r{n}", path="image.png")]) for n in range(REPEAT_STOP - 1)]
    conv, provider = conversation(tmp_path, *reads, changed, *reads[:-1], response("完成"))
    events = [e async for e in conv.reply("查看图片变化")]
    assert events[-1].reason == "completed" and len(provider.requests) == REPEAT_STOP * 2 - 1
    results = [r for m in conv.history for r in m.tool_results]
    assert all(r.images for r in results)
    assert results[0].images[0].name != results[-1].images[0].name


async def test_the_interface_reports_the_stop_and_accepts_another_task(tmp_path):
    from textual.widgets import Static

    from mosscode.tui import MossCodeApp, PromptArea, ToolEntry
    from test_tui import send

    rounds = [glob(f"g{n}") for n in range(REPEAT_STOP)]
    conv, _ = conversation(tmp_path, *rounds, response("完成"))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "找文件")
        await app._reply_task
        await pilot.pause()
        assert "连续 5 轮" in str(app.query_one(".local-note", Static).render())
        assert "已停止" in str(app.query_one("#status", Static).render())
        assert not app.query_one("#prompt", PromptArea).disabled
        assert len(app.query(ToolEntry)) == REPEAT_STOP
        await send(app, pilot, "换个任务")
        await app._reply_task
        assert conv.history[-1].text == "完成"


async def test_a_subagent_reports_repeated_calls_as_unfinished(tmp_path):
    from test_subagents import (
        ScriptProvider,
        agents_dir,
        main_requests,
        tool_results_of,
        write_agent,
    )
    from test_subagents import conversation as subagent_conversation

    write_agent(agents_dir(tmp_path), "loop", body="SUB-LOOP")

    def script(messages, system, tools):
        if "SUB-LOOP" in system.stable:
            return glob(f"g{len(messages)}")
        if messages[-1].role == "user":
            return response(calls=[call("agent", "child", description="找文件", prompt="找文件", agent_type="loop")])
        return response("子任务已停止")

    provider = ScriptProvider(script)
    conv = subagent_conversation(tmp_path, provider)
    try:
        events = [e async for e in conv.reply("开始")]
        assert events[-1].reason == "completed"
        task = conv.tasks.get("t1")
        assert task.status == "failed" and task.tool_calls == REPEAT_STOP
        result = tool_results_of(main_requests(provider)[1])[0]
        assert result.details["task"] == task.id
        assert "子 Agent 未完成" in result.output and "连续 5 轮" in result.output
    finally:
        await conv.close()
