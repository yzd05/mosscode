"""Configured commands can gate a main conversation's completion."""

import asyncio
import json

import pytest

from mosscode import cli
from mosscode.conversation import Conversation
from mosscode.hooks import HookEvent
from mosscode.hooks.conditions import HookConfigError
from mosscode.hooks.config import parse_hook
from mosscode.messages import Completed, ProviderError, RunStopped
from test_agent import SequenceProvider, call, response
from test_hooks import engine, py, registry
from test_tools import python_command
from waiting import until


def check(command, **options):
    return {"id": "verify", "event": "pre_finish", "action": {"type": "command", "command": command}, **options}


async def collect(conversation, text="修改文件"):
    return [event async for event in conversation.reply(text)]


async def test_failed_check_feeds_back_and_only_repaired_answer_completes(tmp_path):
    hooks = engine(
        tmp_path, check(py("from pathlib import Path; assert Path('a').read_text() == 'good', 'expected good'"))
    )
    provider = SequenceProvider(
        response(calls=[call("write_file", path="a", content="bad")]),
        response("草稿完成"),
        response(calls=[call("write_file", path="a", content="good")]),
        response("已修复并验收"),
    )
    conv = Conversation(provider, registry=registry(hooks), hooks=hooks)
    events = await collect(conv)
    assert [e.message.text for e in events if isinstance(e, Completed)] == ["已修复并验收"]
    assert [e.reason for e in events if isinstance(e, RunStopped)] == ["completed"]
    assert "expected good" in provider.requests[2][0][-1].text
    assert "不是新的用户指令" in provider.requests[2][0][-1].text
    assert (hooks.root / "a").read_text() == "good"
    assert [(r.event, r.exit_code) for r in hooks.recent] == [("pre_finish", 1), ("pre_finish", 0)]
    await conv.close()


async def test_repeated_failures_stop_without_success_and_can_be_retried(tmp_path):
    hooks = engine(tmp_path, check(py("raise SystemExit(1)")))
    provider = SequenceProvider(
        response(calls=[call("write_file", path="a", content="bad")]), *[response("完成") for _ in range(4)]
    )
    conv = Conversation(provider, registry=registry(hooks), hooks=hooks)
    events = await collect(conv)
    assert not any(isinstance(e, Completed) for e in events)
    assert [e.reason for e in events if isinstance(e, RunStopped)] == ["finish_check_failed"]
    assert len(provider.requests) == 4
    assert conv._agent._finish_pending
    (hooks.root / ".mosscode/hooks.yaml").unlink()
    hooks.reload()
    events = await collect(conv, "检查配置已移除，继续")
    assert any(isinstance(e, Completed) for e in events)
    assert not conv._agent._finish_pending
    await conv.close()


@pytest.mark.parametrize("kind", ["question", "read", "web", "command", "disabled", "plan"])
async def test_read_only_or_unconfigured_work_does_not_run_checks(tmp_path, kind, monkeypatch):
    hooks = engine(tmp_path, check(py("raise SystemExit(1)")), trust=kind != "disabled")
    (hooks.root / "a").write_text("hello")
    responses = []
    if kind == "read":
        responses.append(response(calls=[call("read_file", path="a")]))
    elif kind == "web":
        responses.append(response(calls=[call("web_fetch", url="https://example.com")]))
    elif kind == "command":
        responses.append(response(calls=[call("execute_command", command="git status")]))
    elif kind == "disabled":
        responses.append(response(calls=[call("write_file", path="a", content="new")]))
    responses.append(response("解释完成"))
    tools = registry(hooks)
    if kind == "web":

        async def fetch(params):
            return "page text", {}

        monkeypatch.setattr(tools.get("web_fetch"), "execute", fetch)
    conv = Conversation(SequenceProvider(*responses), registry=tools, hooks=hooks)
    if kind == "plan":
        conv._agent.set_mode("plan")
        conv._agent._finish_pending = True
    events = await collect(conv)
    assert any(isinstance(e, Completed) for e in events)
    assert not hooks.recent
    await conv.close()


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
async def test_command_failures_block_completion(tmp_path, failure):
    command = {
        "exit": py("raise SystemExit(2)"),
        "timeout": py("import time; time.sleep(10)"),
        "missing": [str(tmp_path / "absent-command")],
    }[failure]
    entry = check(command)
    entry["action"]["timeout"] = 1
    hooks = engine(tmp_path, entry)
    outcome = await hooks.fire(HookEvent("pre_finish", fields={"iteration": "1", "attempt": "1"}))
    assert outcome.rejection and outcome.rejection[0] == "verify"
    assert hooks.recent[-1].outcome == ("timeout" if failure == "timeout" else "failed")
    assert not hooks.take_notices()  # A blocked check must not be reported as ignored.


async def test_cancelled_check_does_not_send_a_repair_request(tmp_path):
    marker = tmp_path / "started"
    hooks = engine(
        tmp_path, check(py("import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)", marker))
    )
    provider = SequenceProvider(response(calls=[call("write_file", path="a", content="x")]), response("完成"))
    conv = Conversation(provider, registry=registry(hooks), hooks=hooks)
    events = []

    async def consume():
        async for event in conv.reply("修改文件"):
            events.append(event)

    task = asyncio.create_task(consume())
    await until(marker.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.requests) == 2
    assert not any(isinstance(e, Completed) for e in events)
    assert hooks.recent[-1].outcome == "cancelled"
    assert conv._agent._finish_pending
    await conv.close()


@pytest.mark.parametrize(
    "options", [{"once": True}, {"async": True}, {"action": {"type": "prompt", "message": "pass"}}]
)
def test_checks_cannot_be_skipped_or_replaced_by_a_model(options, tmp_path):
    with pytest.raises(HookConfigError):
        parse_hook(check(py("pass"), **options), "project", tmp_path / "hooks.yaml")


async def test_background_work_is_checked_after_its_notification(tmp_path):
    hooks = engine(tmp_path, check(py("from pathlib import Path; assert Path('a').read_text() == 'done'")))
    script = "from pathlib import Path; import time; exec(\"while not Path('release').exists(): time.sleep(.02)\"); Path('a').write_text('done')"
    provider = SequenceProvider(
        response(calls=[call("execute_command", command=python_command(script), run_in_background=True)]),
        response("等待后台任务"),
        response("文件已写入"),
    )
    conv = Conversation(provider, registry=registry(hooks), hooks=hooks)
    try:
        await collect(conv)
        assert conv.tasks.running() and conv._agent._finish_pending
        assert not hooks.recent
        (hooks.root / "release").touch()
        await until(lambda: not conv.tasks.running() and conv.notifications)
        events = [event async for event in conv.notify()]
        assert any(isinstance(e, Completed) for e in events)
        assert [r.exit_code for r in hooks.recent] == [0]
        assert not conv._agent._finish_pending
    finally:
        await conv.close()


async def test_repair_requests_obey_the_shared_budget(tmp_path):
    hooks = engine(tmp_path, check(py("raise SystemExit(1)")))
    provider = SequenceProvider(response(calls=[call("write_file", path="a", content="bad")]), response("完成"))
    conv = Conversation(provider, registry=registry(hooks), hooks=hooks, run_budget={"requests": 2})
    with pytest.raises(ProviderError) as error:
        await collect(conv)
    assert error.value.code == "budget_exhausted"
    assert len(provider.requests) == 2 and conv._agent._finish_pending
    await conv.close()


@pytest.mark.parametrize("output_format", ["text", "stream-json"])
async def test_headless_failure_is_not_reported_as_success(tmp_path, capsys, output_format):
    hooks = engine(tmp_path, check(py("raise SystemExit(1)")))
    provider = SequenceProvider(
        response(calls=[call("write_file", path="a", content="bad")]), *[response("完成") for _ in range(3)]
    )
    conv = Conversation(provider, registry=registry(hooks), hooks=hooks)
    result = await cli._prompt(conv, "修改文件", output_format=output_format)
    assert result["is_error"]
    if output_format == "stream-json":
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert records[-1]["subtype"] == "finish_check_failed"
        drafts = [r for r in records if r["type"] == "assistant" and not r["tool_calls"]]
        assert len(drafts) == 3
        assert all(r["stop_reason"] == "finish_check" for r in drafts)
