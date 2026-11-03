import asyncio
from dataclasses import replace

from textual.widgets import Markdown, Static

from mosscode.conversation import Conversation
from mosscode.messages import Delta, Retrying, Usage
from mosscode.tools import create_default_registry
from mosscode.tui import ChatEntry, MossCodeApp, PromptArea, ToolEntry
from test_agent import SequenceProvider, call, response
from test_tui import send
from waiting import until


async def test_plan_then_permissions_command_help_and_chinese_completion(tmp_path):
    provider = SequenceProvider(
        response("计划：创建 a"), response(calls=[call("write_file", path="a", content="done")]), response("执行完成")
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "/p"
        await pilot.pause()
        assert [hint[:2] for hint in app._hint_choices] == [
            ("/plan", "只读调查和规划，提交计划经批准后执行"),
            ("/permissions", "查看或切换工具权限档位"),
        ]
        await pilot.press("tab")
        assert field.value == "/plan"
        await pilot.press("enter")
        await pilot.pause()
        assert conv.mode == "plan" and not provider.requests
        assert "计划模式" in str(app.query_one("#status", Static).render())
        assert "跳过审批" not in str(app.query_one("#status", Static).render())
        await send(app, pilot, "/plan 创建 a")
        await app._reply_task
        assert provider.requests[0][0][0].text == "创建 a"
        assert len(provider.requests[0][2]) == 5
        assert not (tmp_path / "a").exists()
        await send(app, pilot, "/permissions bypassPermissions")
        assert conv.mode == "normal" and len(provider.requests) == 1
        await send(app, pilot, "按计划执行")
        await app._reply_task
        assert (tmp_path / "a").read_text() == "done"
        assert "跳过审批" in str(app.query_one("#status", Static).render())
        assert "计划模式" not in str(app.query_one("#status", Static).render())
        assert "按计划执行" in provider.requests[1][0][-1].text
        assert len(provider.requests[1][2]) == 6
        user_rows = [e.body for e in app.query(ChatEntry) if e.speaker == "You"]
        assert user_rows == ["/plan", "/plan 创建 a", "/permissions bypassPermissions", "按计划执行"]
        assert app.focused is field and not field.disabled
        await send(app, pilot, "/help")
        assert "/plan" in str(list(app.query(".local-note"))[-1].render())
        assert len(provider.requests) == 3


async def test_leaving_plan_mode_by_command_sends_nothing(tmp_path):
    provider = SequenceProvider(response("完成"))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    conv.set_mode("plan")
    app = MossCodeApp(conv, "Demo")
    async with app.run_test() as pilot:
        await send(app, pilot, "/permissions default")
        assert conv.mode == "normal" and not provider.requests
        await send(app, pilot, "明确任务")
        await app._reply_task
        assert provider.requests[0][0][-1].text == "明确任务"


async def test_multiple_iterations_streaming_final_markdown_and_usage(tmp_path):
    arrived, release = asyncio.Event(), asyncio.Event()

    async def final():
        yield Delta("text", "**已完")
        arrived.set()
        await release.wait()
        yield Delta("text", "成**")
        yield response("**已完成**", usage=Usage(12, 4))

    provider = SequenceProvider(
        response("先创建", [call("write_file", path="a", content="one")], Usage(10, 2, 8, 2)),
        response("再修改", [call("write_file", path="a", content="two")]),
        final,
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "执行两步")
        await asyncio.wait_for(arrived.wait(), 3)
        await pilot.pause()
        reply = list(app.query(ChatEntry))[-1]
        assert reply.query_one(".raw").display and not reply.query_one(".answer").display
        assert "**已完" in str(reply.query_one(".raw", Static).render())
        status = str(app.query_one("#status", Static).render())
        assert "跳过审批" in status and "3/200 轮" in status and "≥10" in status
        release.set()
        await app._reply_task
        await pilot.pause()
        assert reply.query_one(".answer", Markdown).display and not reply.query_one(".raw").display
        assert reply.body == "**已完成**"
        assert [e.body for e in app.query(ChatEntry) if e.speaker != "You"] == ["先创建", "再修改", "**已完成**"]
        assert len(app.query(ToolEntry)) == 2  # Same call ID in another round creates another row.
        assert "已创建 a" in str(list(app.query(ToolEntry))[0].render())
        status = str(app.query_one("#status", Static).render())
        assert "跳过审批" in status and "就绪" not in status and "≥22" in status and "≥6" in status
        assert "缓存读 ≥8 / 写 ≥2" in status
        assert all("<system-reminder>" not in e.body and "<environment>" not in e.body for e in app.query(ChatEntry))
        assert not app.query_one("#prompt", PromptArea).disabled


async def test_loop_limit_notice_is_local_and_input_recovers(tmp_path):
    provider = SequenceProvider(response(calls=[call("write_file", path="a", content="x")]))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        max_iterations=1,
    )
    app = MossCodeApp(conv, "Demo")
    async with app.run_test() as pilot:
        await send(app, pilot, "任务")
        await app._reply_task
        await pilot.pause()
        assert "最大迭代轮数 1" in str(app.query_one(".local-note", Static).render())
        assert "已停止" in str(app.query_one("#status", Static).render())
        assert "入 — / 出 —" in str(app.query_one("#status", Static).render())
        assert "缓存读 未提供" in str(app.query_one("#status", Static).render())
        assert not app.query_one("#prompt", PromptArea).disabled
        assert len(conv.history) == 3


async def test_tool_only_next_iteration_does_not_erase_preceding_assistant_text(tmp_path):
    provider = SequenceProvider(
        response("先创建文件", [call("write_file", path="a", content="x")]),
        response(calls=[call("read_file", path="a")]),
        response("检查完成"),
    )
    app = MossCodeApp(
        Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        ),
        "Demo",
    )
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "创建并检查")
        await app._reply_task
        await pilot.pause()
        replies = [e for e in app.query(ChatEntry) if e.speaker != "You"]
        assert [e.body for e in replies] == ["先创建文件", "", "检查完成"]
        assert replies[0].display and replies[0].query_one(".answer").display
        assert not replies[1].display and len(app.query(ToolEntry)) == 2
        await pilot.resize_terminal(44, 20)
        await pilot.pause()
        assert "鼠标滚轮查看历史" not in str(app.query_one("#status", Static).render())


async def test_repeated_escape_waits_for_file_io_outcome_and_pairs_later_calls(tmp_path, monkeypatch):
    import threading

    from mosscode.tools import files

    entered, release = threading.Event(), threading.Event()
    original = files.atomic_write

    def slow_write(path, content):
        entered.set()
        release.wait(3)
        original(path, content)

    monkeypatch.setattr(files, "atomic_write", slow_write)
    provider = SequenceProvider(
        response(
            calls=[
                call("write_file", "first", path="a", content="x"),
                call("write_file", "later", path="b", content="y"),
            ]
        )
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    app = MossCodeApp(conv, "Demo")
    async with app.run_test() as pilot:
        try:
            await send(app, pilot, "任务")
            await until(entered.is_set)
            await pilot.press("escape", "escape")
            assert app._reply_task.cancelling() == 1
        finally:
            release.set()
        await app._reply_task
        await pilot.pause()
        assert (tmp_path / "a").read_text() == "x" and not (tmp_path / "b").exists()
        assert not conv.history[2].tool_results[0].is_error
        assert conv.history[2].tool_results[1].code == "not_executed"
        assert "已创建 a" in str(list(app.query(ToolEntry))[0].render())
        assert not app.query_one("#prompt", PromptArea).disabled


async def test_retry_status_and_max_tokens_notice(tmp_path):
    arrived, release = asyncio.Event(), asyncio.Event()

    async def flaky():
        yield Retrying(1, 4, 2.0, "服务过载")
        arrived.set()
        await release.wait()
        yield Delta("text", "很长的回答")
        yield replace(response("很长的回答"), stop_reason="max_tokens")

    conv = Conversation(SequenceProvider(flaky))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "写长文")
        await asyncio.wait_for(arrived.wait(), 3)
        await pilot.pause(0.5)  # Several 0.2 s status ticks must not hide the retry notice.
        status = str(app.query_one("#status", Static).render())
        assert "服务过载" in status and "2 秒后重试（1/4）" in status
        release.set()
        await app._reply_task
        await pilot.pause()
        assert "max_output_tokens" in str(list(app.query(".local-note"))[-1].render())
        assert [e.body for e in app.query(ChatEntry) if e.speaker != "You"] == ["很长的回答"]
        assert not app.query_one("#prompt", PromptArea).disabled
