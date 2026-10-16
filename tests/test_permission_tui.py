import asyncio

from textual.widgets import OptionList, Static
from mosscode.tui import PromptArea

from mosscode.conversation import Conversation
from mosscode.permissions import PermissionPolicy
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp
from test_agent import SequenceProvider, call, response
from test_tui import send


def make_app(root, provider):
    policy = PermissionPolicy(root, load=False)
    registry = create_default_registry(root, permissions=policy)
    return MossCodeApp(Conversation(provider, registry=registry), "Demo"), policy


async def wait_approval(app, pilot):
    for _ in range(40):
        await pilot.pause(.02)
        if app._approval_request:
            return
        if app._reply_task.done():
            raise AssertionError("调用结束但未显示审批")
    raise AssertionError("未显示审批")


async def test_keyboard_approval_below_input_and_once_scope(tmp_path):
    provider = SequenceProvider(response(calls=[call("write_file", path="hello.txt", content="Hello")]), response("已创建"))
    app, policy = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 32)) as pilot:
        await send(app, pilot, "创建文件")
        await wait_approval(app, pilot)
        options = app.query_one("#approval-options", OptionList)
        panel = app.query_one("#approval-panel")
        assert app.focused is options and not (tmp_path / "hello.txt").exists()
        assert panel.region.y >= app.query_one("#input-row").region.bottom
        assert panel.region.height < app.size.height
        assert "hello.txt" in str(app.query_one("#approval-detail", Static).render())
        assert "Hello" in str(app.query_one("#approval-detail", Static).render())
        assert options.highlighted == 0
        await pilot.click("#approval-options", offset=(1, 0))
        assert not app._reply_task.done() and not (tmp_path / "hello.txt").exists()
        await pilot.press("down")
        assert options.highlighted == 1
        await pilot.press("up", "enter")
        await asyncio.wait_for(app._reply_task, 3)
        assert (tmp_path / "hello.txt").read_text() == "Hello"
        assert not policy.session and not policy.local_file.exists()
        assert not panel.display and app.focused is app.query_one("#prompt", PromptArea)
        assert not policy.pending


async def test_escape_denies_one_operation_but_model_continues(tmp_path):
    provider = SequenceProvider(response(calls=[call("write_file", path="no", content="bad")]), response("按要求跳过写入"))
    app, policy = make_app(tmp_path, provider)
    async with app.run_test(size=(90, 28)) as pilot:
        await send(app, pilot, "任务")
        await wait_approval(app, pilot)
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, 3)
        assert len(provider.requests) == 2
        assert provider.requests[1][0][-1].tool_results[0].code == "permission_denied"
        assert not (tmp_path / "no").exists() and not policy.pending
        assert not app.query_one("#approval-panel").display


async def test_ctrl_c_cancels_pending_approval_and_reuse_has_no_stale_dialog(tmp_path):
    provider = SequenceProvider(response(calls=[call("write_file", path="no", content="bad")]), response("第二次正常回复"))
    app, policy = make_app(tmp_path, provider)
    async with app.run_test(size=(90, 28)) as pilot:
        await send(app, pilot, "任务")
        await wait_approval(app, pilot)
        await pilot.press("ctrl+c")
        try:
            await app._reply_task
        except asyncio.CancelledError:
            pass
        assert not policy.pending and not (tmp_path / "no").exists()
        assert app.conversation.history[-1].tool_results[0].code == "cancelled"
        assert not app.query_one("#approval-panel").display
        await send(app, pilot, "第二次")
        await asyncio.wait_for(app._reply_task, 3)
        assert len(provider.requests) == 2 and not app._approval_request


async def test_permanent_choice_and_permission_commands(tmp_path):
    provider = SequenceProvider(response(calls=[call("write_file", path="a", content="saved")]), response("完成"))
    app, policy = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 32)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "/permissions "
        await pilot.pause()
        assert len(app._hint_choices) == 4
        assert "默认" in app._hint_choices[0][1]
        field.clear()
        await send(app, pilot, "创建")
        await wait_approval(app, pilot)
        assert app._approval_request.choices == ("once", "session", "accept_edits", "permanent", "deny")
        await pilot.press("down", "down", "down", "enter")
        await asyncio.wait_for(app._reply_task, 3)
        assert policy.local_file.exists() and (tmp_path / "a").read_text() == "saved"
        assert policy.mode == "default"
        await send(app, pilot, "/permissions strict")
        assert policy.mode == "default"
        await send(app, pilot, "/permissions bypassPermissions")
        assert policy.mode == "bypassPermissions" and len(provider.requests) == 2
        assert "⏵⏵ 跳过审批" in str(app.query_one("#status", Static).render())


async def test_narrow_approval_keeps_choices_and_status_visible(tmp_path):
    provider = SequenceProvider(response(calls=[call("write_file", path="hello.txt", content="many lines\n" * 50)]), response("完成"))
    app, policy = make_app(tmp_path, provider)
    async with app.run_test(size=(44, 20)) as pilot:
        await send(app, pilot, "创建")
        await wait_approval(app, pilot)
        await pilot.pause()
        assert app.query_one("#approval-options").region.bottom <= app.size.height
        assert app.query_one("#status").region.bottom <= app.size.height
        assert app.query_one("#approval-panel").region.y >= app.query_one("#input-row").region.bottom
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 3)
        assert (tmp_path / "hello.txt").exists() and not policy.pending
