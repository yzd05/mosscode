"""/goal: the condition, the evaluator after each turn, pauses, transcript records, resume and the interface."""

import asyncio
import json

import pytest
import yaml

from mosscode.config import ConfigError, load_settings
from mosscode.goal import (
    GOAL,
    MAX_CONDITION,
    Goal,
    check_condition,
    conversation_tail,
    evaluation_prompt,
    goal_reminder,
    parse_verdict,
)
from mosscode.messages import ContentBlock, Message, ToolResult
from mosscode.session import load_session
from test_agent import SequenceProvider, call, response
from test_commands import context, run
from test_sessions import drain, make, records
from waiting import until


def verdict(kind, reason=""):
    return response(json.dumps({"verdict": kind, "reason": reason}, ensure_ascii=False))


def conversation(tmp_path, work, judge, **options):
    return make(tmp_path, work, goal_provider=judge, **options)


def test_the_condition_is_checked():
    assert check_condition("  测试全部通过 \n") == "测试全部通过"
    with pytest.raises(ValueError, match="不能为空"):
        check_condition("   ")
    assert check_condition("长" * MAX_CONDITION)
    with pytest.raises(ValueError, match=str(MAX_CONDITION)):
        check_condition("长" * (MAX_CONDITION + 1))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"verdict": "met", "reason": "测试通过"}', ("met", "测试通过")),
        ('```json\n{"verdict": "not_met", "reason": "还差  文档\n"}\n```', ("not_met", "还差 文档")),
        ('判断如下：{"verdict": "impossible"}', ("impossible", "")),
    ],
)
def test_the_verdict_is_read_from_the_reply(text, expected):
    result = parse_verdict(text)
    assert (result.verdict, result.reason) == expected


@pytest.mark.parametrize(
    "text", ["达成了", '{"verdict": "done", "reason": ""}', '{"verdict": "met", "reason": 1}', "{坏的 json}"]
)
def test_an_unreadable_verdict_is_an_error(text):
    with pytest.raises(ValueError):
        parse_verdict(text)


def test_the_evaluator_reads_the_condition_and_the_newest_tool_results():
    history = [Message("user", (ContentBlock("text", "让测试通过"),))]
    for n in range(25):
        history.append(Message("assistant", (), tool_calls=(call("execute_command", f"c{n}", command=f"run {n}"),)))
        history.append(Message("tool", (), tool_results=(ToolResult(f"c{n}", "execute_command", f"输出 {n}"),)))
    history.append(Message("assistant", (ContentBlock("text", "都跑完了"),)))
    tail = conversation_tail(history)
    assert "输出 24" in tail and "输出 5" in tail and "输出 4" not in tail  # Only the newest 20 keep their text.
    assert "[工具调用 · execute_command]" in tail and "run 0" in tail and tail.endswith("都跑完了")
    prompt = evaluation_prompt("tests 全部通过", history)
    assert "<condition>\ntests 全部通过\n</condition>" in prompt and '"verdict"' in prompt
    long = [Message("user", (ContentBlock("text", "旧" * 40_000),)), Message("user", (ContentBlock("text", "新"),))]
    assert conversation_tail(long, limit=100).startswith("（更早的 1 条消息已省略）")


def test_the_reminder_carries_the_condition_turns_and_latest_reason():
    assert goal_reminder(None) is None
    goal = Goal("测试通过", 30, turns=2, reason="还有 1 个失败")
    note = goal_reminder(goal)
    assert note.kind == "goal" and "测试通过" in note.text and "已运行 2 轮，上限 30 轮" in note.text
    assert "还有 1 个失败" in note.text


async def test_not_met_continues_with_the_reason_and_met_ends_the_goal(tmp_path):
    work = SequenceProvider(
        response("先改", [call("write_file", "w1", path="a.txt", content="x")]),
        response("改好了"),
        response("再看", [call("read_file", "r1", path="a.txt")]),
        response("确认完成"),
    )
    judge = SequenceProvider(verdict("not_met", "还没有验证 a.txt"), verdict("met", "a.txt 已写入并读回"))
    conv = conversation(tmp_path, work, judge)
    conv.set_goal("写好 a.txt 并验证")
    await drain(conv.reply("写好 a.txt 并验证"))
    step = await conv.goal_after_turn()
    assert step.action == "continue" and "还没有验证 a.txt" in step.note and "还没有验证 a.txt" in step.prompt
    assert conv.goal.turns == 1 and conv.goal.reason == "还没有验证 a.txt"
    # The evaluator sees the condition and the turn, without tools.
    request = judge.requests[0]
    assert request[2] == () and request[3] is False and "写好 a.txt 并验证" in request[0][0].text
    assert "[工具调用 · write_file]" in request[0][0].text

    await drain(conv.reply(step.prompt, synthetic=GOAL))
    assert conv.history[4].synthetic == GOAL
    # Every request of the goal carries the reminder after the cached prefix, the latest reason in it.
    supplements = {s.kind: s for s in work.requests[-1][1].supplements}
    assert "还没有验证 a.txt" in supplements["goal"].text
    assert len({request[1].stable for request in work.requests}) == 1
    step = await conv.goal_after_turn()
    assert step.action == "met" and "a.txt 已写入并读回" in step.note
    assert conv.goal is None and conv.last_goal == ("写好 a.txt 并验证", "achieved", "a.txt 已写入并读回")
    assert conv._agent.goal_note is None  # No reminder once the goal has ended.
    saved = [(r["event"], r["condition"]) for r in records(conv.session.transcript_path) if r["type"] == "goal"]
    assert saved == [("set", "写好 a.txt 并验证"), ("achieved", "写好 a.txt 并验证")]
    await conv.close()


async def test_impossible_ends_the_goal_with_the_reason(tmp_path):
    work = SequenceProvider(response("查了", [call("read_file", "r", path="missing.txt")]), response("文件不存在"))
    judge = SequenceProvider(verdict("impossible", "missing.txt 不存在，需要用户提供"))
    conv = conversation(tmp_path, work, judge)
    conv.set_goal("让 missing.txt 通过校验")
    await drain(conv.reply("开始"))
    step = await conv.goal_after_turn()
    assert step.action == "impossible" and "需要用户提供" in step.note and conv.goal is None
    assert conv.last_goal[1] == "impossible"
    await conv.close()


@pytest.mark.parametrize(
    ("reason", "said"),
    [
        ("cancelled", "本轮已取消"),
        ("error", "本轮出错"),
        ("max_iterations", "最大迭代轮数"),
        ("max_tokens", "输出上限"),
    ],
)
async def test_a_turn_that_did_not_finish_pauses_without_an_evaluation(tmp_path, reason, said):
    judge = SequenceProvider()
    conv = conversation(tmp_path, SequenceProvider(), judge)
    conv.set_goal("完成")
    conv._goal_turn = (reason, True)
    step = await conv.goal_after_turn()
    assert step.action == "paused" and said in step.note and "/goal" in step.note
    assert conv.goal.paused and not judge.requests
    assert await conv.goal_after_turn() == type(step)("none")  # Paused: nothing more until the user resumes.


async def test_the_iteration_limit_really_pauses(tmp_path):
    work = SequenceProvider(response(calls=[call("write_file", "w", path="a", content="x")]))
    conv = conversation(tmp_path, work, SequenceProvider(), max_iterations=1)
    conv.set_goal("完成")
    await drain(conv.reply("开始"))
    assert (await conv.goal_after_turn()).action == "paused" and "最大迭代轮数" in conv.goal.paused


async def test_three_turns_without_tools_pause_and_a_user_message_resumes(tmp_path):
    work = SequenceProvider(response("想一想"), response("再想想"), response("还在想"), response("好"))
    judge = SequenceProvider(*[verdict("not_met", "没有进展")] * 4)
    conv = conversation(tmp_path, work, judge)
    conv.set_goal("完成")
    await drain(conv.reply("开始"))
    assert (await conv.goal_after_turn()).action == "continue"
    for _ in range(2):
        await drain(conv.reply("继续", synthetic=GOAL))
        step = await conv.goal_after_turn()
    assert step.action == "paused" and "连续 3 轮没有调用工具" in step.note
    await conv.ask("换个思路")  # The user's own message resumes the goal.
    assert not conv.goal.paused and conv.goal.idle == 0
    await conv.close()


async def test_the_turn_limit_pauses_and_resuming_allows_as_many_again(tmp_path):
    turns = [response("改", [call("write_file", f"w{n}", path="a", content=str(n))]) for n in range(3)]
    work = SequenceProvider(*[item for t in turns for item in (t, response("好"))])
    judge = SequenceProvider(*[verdict("not_met", "还差")] * 3)
    conv = conversation(tmp_path, work, judge, goal_max_turns=2)
    conv.set_goal("完成")
    await drain(conv.reply("开始"))
    assert (await conv.goal_after_turn()).action == "continue"
    await drain(conv.reply("继续", synthetic=GOAL))
    step = await conv.goal_after_turn()
    assert step.action == "paused" and "已自动运行 2 轮" in step.note
    assert conv.resume_goal() and conv.goal.budget == 4
    await drain(conv.reply("继续", synthetic=GOAL))
    assert (await conv.goal_after_turn()).action == "continue"
    await conv.close()


async def test_a_failed_evaluation_counts_as_not_met_and_three_in_a_row_pause(tmp_path):
    work = SequenceProvider(*[response("改", [call("read_file", f"r{n}", path="x")]) for n in range(3)])
    for n in range(3):
        work.responses.insert(2 * n + 1, response("好"))
    judge = SequenceProvider(response("看不懂"), RuntimeError("断线"), response("还是不知道"))
    conv = conversation(tmp_path, work, judge)
    conv.set_goal("完成")
    await drain(conv.reply("开始"))
    step = await conv.goal_after_turn()
    assert step.action == "continue" and conv.goal.failures == 1
    for _ in range(2):
        await drain(conv.reply(step.prompt, synthetic=GOAL))
        step = await conv.goal_after_turn()
    assert step.action == "paused" and "连续 3 次无法判断" in step.note
    await conv.close()


async def test_running_background_work_defers_the_evaluation(tmp_path, monkeypatch):
    judge = SequenceProvider(verdict("met", "完成"))
    conv = conversation(
        tmp_path, SequenceProvider(response("后台跑着", [call("read_file", "r", path="x")]), response("等通知")), judge
    )
    conv.set_goal("完成")
    await drain(conv.reply("开始"))
    monkeypatch.setattr(conv.tasks, "running", lambda: [object()])
    assert (await conv.goal_after_turn()).action == "deferred" and not judge.requests and conv.goal is not None
    monkeypatch.undo()
    await conv.close()


async def test_clearing_records_and_clear_ends_the_goal(tmp_path):
    conv = conversation(tmp_path, SequenceProvider(response("好")), SequenceProvider())
    conv.set_goal("甲")
    conv.set_goal("乙")  # Replaces the first one.
    await drain(conv.reply("开始"))
    path = conv.session.transcript_path
    await conv.clear()
    assert conv.goal is None and conv.last_goal is None
    saved = [(r["event"], r["condition"]) for r in records(path) if r["type"] == "goal"]
    assert saved == [("set", "甲"), ("cleared", "甲"), ("set", "乙"), ("cleared", "乙")]
    await conv.close()


async def test_an_open_goal_is_restored_paused_and_an_ended_one_is_not(tmp_path):
    conv = conversation(tmp_path, SequenceProvider(response("好")), SequenceProvider())
    conv.set_goal("测试全部通过")
    await drain(conv.reply("开始"))
    session_id, path = conv.session_id, conv.session.transcript_path
    await conv.close()
    assert load_session(path).goal == "测试全部通过"

    other = conversation(tmp_path, SequenceProvider(), SequenceProvider())
    events = await drain(other.resume(session_id))
    assert other.goal.condition == "测试全部通过" and other.goal.paused and other.goal.turns == 0
    assert any("未完成的目标" in notice for notice in events[0].notices)
    other.clear_goal()
    await other.close()
    assert load_session(path).goal is None


def test_a_malformed_goal_record_is_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [
        {"type": "goal", "event": "set", "condition": "甲"},
        {"type": "goal", "event": "paused", "condition": "乙"},
        {"type": "goal", "event": "set", "condition": ""},
    ]
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
    restored = load_session(path)
    assert restored.goal == "甲" and restored.skipped == 2


def test_the_settings(tmp_path):
    path = tmp_path / "config.yaml"
    provider = {"name": "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "MOSS_TEST_KEY"}
    path.write_text(yaml.safe_dump({"providers": [provider]}), encoding="utf-8")
    settings = load_settings(path)
    assert settings.goal_max_turns == 30 and settings.goal_provider is None
    path.write_text(
        yaml.safe_dump({"providers": [provider], "goal_max_turns": 5, "goal_provider": "p"}), encoding="utf-8"
    )
    settings = load_settings(path)
    assert settings.goal_max_turns == 5 and settings.goal_provider == "p"
    for bad in ({"goal_max_turns": 0}, {"goal_max_turns": "10"}, {"goal_provider": "missing"}):
        path.write_text(yaml.safe_dump({"providers": [provider], **bad}), encoding="utf-8")
        with pytest.raises(ConfigError, match=next(iter(bad))):
            load_settings(path)


async def test_the_command_sets_shows_resumes_and_clears(tmp_path):
    conv = conversation(tmp_path, SequenceProvider(), SequenceProvider())
    ui = await run(context(conv), "/goal")
    assert "当前没有目标" in ui.notes[-1] and "/goal <条件>" in ui.notes[-1]

    ui = await run(context(conv), "/goal 测试全部通过")
    assert conv.goal.condition == "测试全部通过"
    assert ("submit", "测试全部通过", "/goal 测试全部通过") in ui.calls and "自动继续" in ui.notes[-1]

    conv.goal.reason, conv.goal.turns = "还有 2 个失败", 3
    conv.pause_goal("本轮已取消")
    ui = await run(context(conv), "/goal")
    report = ui.notes[0]
    assert "目标已暂停（本轮已取消）" in report and "测试全部通过" in report and "3/30 轮" in report
    assert "还有 2 个失败" in report and "Token" in report
    assert ui.notes[-1] == "└ 已继续目标。" and not conv.goal.paused
    assert ui.calls[-1][0] == "submit" and ui.calls[-1][3] == GOAL and "还有 2 个失败" in ui.calls[-1][1]

    ui = await run(context(conv), "/goal")  # Running already: shown, nothing submitted.
    assert not any(c[0] == "submit" for c in ui.calls)

    ui = await run(context(conv), "/goal stop")
    assert ui.notes[-1] == "└ 已清除目标。" and conv.goal is None
    ui = await run(context(conv), "/goal")
    assert "最近一次目标已清除：测试全部通过" in ui.notes[-1]

    ui = await run(context(conv), "/goal " + "长" * (MAX_CONDITION + 1))
    assert "最多" in ui.notes[-1] and conv.goal is None and not ui.calls
    await conv.close()


async def test_the_interface_continues_until_met_and_shows_the_goal(tmp_path):
    from textual.widgets import Static

    from mosscode.tui import ChatEntry, MossCodeApp, PromptArea

    work = SequenceProvider(
        response("第一步", [call("write_file", "w", path="a.txt", content="1")]),
        response("写好了"),
        response("验证", [call("read_file", "r", path="a.txt")]),
        response("验证通过"),
    )
    released = asyncio.Event()

    async def slow_met():
        await released.wait()
        yield verdict("met", "a.txt 已验证")

    judge = SequenceProvider(verdict("not_met", "还没验证"), slow_met)
    conv = conversation(tmp_path, work, judge)
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", PromptArea).value = "/goal 写好并验证 a.txt"
        await pilot.press("enter")
        await until(lambda: len(judge.requests) == 2)
        await pilot.pause()
        status = str(app.query_one("#status", Static).render())
        assert "◎ 目标" in status and "判断目标是否达成" in status
        released.set()
        await until(lambda: conv.goal is None and not app._task_running)
        await pilot.pause()
        notes = [str(n.render()) for n in app.query(".local-note")]
        assert any("目标尚未达成：还没验证" in n for n in notes) and any("目标已达成：a.txt 已验证" in n for n in notes)
        # The continuation is not shown as the user's message.
        assert [e.body for e in app.query(ChatEntry) if e.speaker == "You"] == ["/goal 写好并验证 a.txt"]
        assert conv.history[4].synthetic == GOAL and len(work.requests) == 4
        assert "◎ 目标" not in str(app.query_one("#status", Static).render())


async def test_escape_while_judging_pauses_the_goal(tmp_path):
    from textual.widgets import Static

    from mosscode.tui import MossCodeApp, PromptArea

    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()
        yield verdict("met")

    work = SequenceProvider(response("做", [call("read_file", "r", path="x")]), response("好"), response("继续做"))
    judge = SequenceProvider(hang)
    conv = conversation(tmp_path, work, judge)
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", PromptArea).value = "/goal 完成任务"
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), 10)
        await pilot.press("escape")
        await until(lambda: not app._task_running)
        await pilot.pause()
        assert conv.goal is not None and "Esc" in conv.goal.paused
        assert any("目标已暂停" in str(n.render()) for n in app.query(".local-note"))
        assert "◎ 目标已暂停" in str(app.query_one("#status", Static).render())
        assert len(work.requests) == 2  # Nothing started on its own.
