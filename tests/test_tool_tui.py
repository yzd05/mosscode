import asyncio

from textual.widgets import Static

from mosscode import cli
from mosscode.conversation import Conversation
from mosscode.messages import ToolCall
from mosscode.tools import create_default_registry
from mosscode.tui import ChatEntry, MossCodeApp, PromptArea, ToolEntry
from test_tool_conversation import ToolProvider, write_call
from test_tui import answer_rows, terminal_background


async def test_tool_rows_and_final_markdown_preserve_prompt_and_history(tmp_path, monkeypatch):
    from mosscode.appearance import MUTED_COLOR, OPERATION_COLOR, PARAMETER_COLOR, TOOL_SUCCESS_COLOR

    monkeypatch.delenv("NO_COLOR", raising=False)
    provider = ToolProvider((write_call(), ToolCall("read", "read_file", '{"path":"result.txt"}')))
    app = MossCodeApp(
        Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        ),
        "Demo",
    )
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        field = app.query_one("#prompt", PromptArea)
        field.value = "写入并读取"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        rows = list(app.query(ToolEntry))
        assert len(rows) == 2
        assert "Write(result.txt)" in str(rows[0].render())
        assert "已创建 result.txt" in str(rows[0].render())
        assert "Read(result.txt)" in str(rows[1].render())
        assert "1\tone" in str(rows[1].render())
        assert "执行中" not in str(rows[0].render())
        assert str(rows[0].render()).startswith("✓ Write")
        entries = list(app.query(ChatEntry))
        assert entries[-1].body == "结果完成"
        assert answer_rows(app, entries[-1]) == ["结果完成"]
        assert not entries[1].query_one(".message-row").display  # No empty assistant heading before tools.
        assert app.focused is field and not field.disabled
        assert "⏵⏵ 跳过审批" in str(app.query_one("#status", Static).render())
        text = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "Write(" in text and "Read(" in text and "结果完成" in text
        for strip in app.screen._compositor.render_strips():
            for segment in strip:
                assert terminal_background(segment)
        segments = [s for strip in app.screen._compositor.render_strips() for s in strip]
        for label, color in [("Write", OPERATION_COLOR), ("Read", TOOL_SUCCESS_COLOR), ("result.txt", PARAMETER_COLOR)]:
            spans = [s for s in segments if s.text == label]
            assert spans and all(s.style.color.name == color for s in spans)
        outputs = [s for s in segments if "已创建 result.txt" in s.text or "1\tone" in s.text]
        # A result summary is secondary text: the fixed grey, never the dim attribute.
        assert outputs and all(s.style.color.name == MUTED_COLOR and not s.style.dim for s in outputs)


async def test_cancel_continuation_keeps_finished_tool_row(tmp_path):
    provider = ToolProvider((write_call(),), hold=True)
    app = MossCodeApp(
        Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        ),
        "Demo",
    )
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "写文件"
        await pilot.press("enter")
        await asyncio.wait_for(provider.continuing.wait(), 3)
        await pilot.press("escape")
        await app._reply_task
        await pilot.pause()
        assert "已创建 result.txt" in str(app.query_one(ToolEntry).render())
        assert not app.query_one("#prompt", PromptArea).disabled
        assert not app.conversation.history[2].tool_results[0].is_error
        interrupted = list(app.query(ChatEntry))[1]
        assert interrupted.display
        assert "已取消" in "\n".join(answer_rows(app, interrupted))


async def test_activity_line_keeps_the_outcome_of_the_latest_tool_calls(tmp_path, monkeypatch):
    from mosscode.appearance import ERROR_COLOR, MUTED_COLOR, TOOL_SUCCESS_COLOR

    monkeypatch.delenv("NO_COLOR", raising=False)
    calls = (write_call(), ToolCall("missing", "read_file", '{"path":"missing.txt"}'))
    provider = ToolProvider(calls, hold=True)
    app = MossCodeApp(
        Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        ),
        "Demo",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "写入再读一个不存在的文件"
        await pilot.press("enter")
        await asyncio.wait_for(provider.continuing.wait(), 3)
        app._tick()
        await pilot.pause()
        assert [str(row.render())[0] for row in app.query(ToolEntry)] == ["✓", "×"]
        activity = app.query_one("#activity", Static)
        shown = app.screen._compositor.render_strips()[activity.region.y]
        assert shown.text.rstrip().endswith("最近 ✓ ×")
        colors = {segment.text.strip(): segment.style.color.name for segment in shown if segment.text.strip()}
        assert colors["最近"] == MUTED_COLOR and colors["✓"] == TOOL_SUCCESS_COLOR and colors["×"] == ERROR_COLOR

        await pilot.press("escape")
        await app._reply_task
        await pilot.pause()
        assert activity.region.height == 0  # Idle, the line and its marks take no row.
        assert [row.state for row in app._recent_tools] == ["success", "error"]
        # They follow the conversation on screen: gone with it on /clear.
        await app.clear_screen()
        assert not app._recent_tools


async def test_final_failure_still_shows_error_after_hidden_tool_request(tmp_path):
    provider = ToolProvider((write_call(),), fail=True)
    app = MossCodeApp(
        Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        ),
        "Demo",
    )
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "写文件"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        reply = list(app.query(ChatEntry))[-1]
        assert reply.display and "test final failure" in "\n".join(answer_rows(app, reply))
        assert not app.conversation.history[2].tool_results[0].is_error


async def test_headless_uses_same_tool_flow_and_prints_results(tmp_path, capsys):
    provider = ToolProvider((write_call(),))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    await cli._prompt(conv, "写文件")
    output = capsys.readouterr().out
    assert "● write_file" in output and "└ 已创建 result.txt" in output and "结果" in output
    assert len(provider.requests) == 2
