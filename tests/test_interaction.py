"""Interaction: input, queueing and the status line."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from textual import events
from textual.widgets import Static

from mosscode import cli
from mosscode.catalog import ModelInfo
from mosscode.config import ProviderConfig, Settings
from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError, ToolCall, ToolResult, Usage
from mosscode.permissions import MODES
from mosscode.providers import AnthropicProvider, ChatCompletionsProvider, ResponsesProvider
from mosscode.tools import create_default_registry
from mosscode.tools.diffing import unified_diff
from mosscode.tui import (
    DIFF_ROWS,
    ERROR_COLOR,
    MODE_BADGES,
    MUTED_COLOR,
    OPERATION_COLOR,
    TOOL_SUCCESS_COLOR,
    ChatEntry,
    MossCodeApp,
    PromptArea,
    ToolEntry,
    diff_rows,
)
from test_agent import SequenceProvider, call, response
from test_cli import setup_cli
from test_conversation import FakeProvider
from test_tui import send

TWO = Settings(
    (
        ProviderConfig("test", "anthropic-messages", "model-a"),
        ProviderConfig("other", "openai-chat-completions", "model-b"),
    ),
    "test",
)


def bypass(tmp_path, mode="bypassPermissions"):
    return create_default_registry(tmp_path, permission_mode=mode, load_permissions=False)


def held(message: Completed, gate: asyncio.Event, started: asyncio.Event):
    """A response that streams its first text, then waits for the test."""

    async def run():
        yield Delta("text", message.message.text or "…")
        started.set()
        await gate.wait()
        yield message

    return run


def status(app) -> str:
    return str(app.query_one("#status", Static).render())


async def wait_until(pilot, predicate, rounds=100):
    for _ in range(rounds):
        if predicate():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not reached")


async def test_multiline_keys_backslash_paste_and_history():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        await pilot.press("a", "shift+enter", "b", "ctrl+j", "c", "backslash", "enter", "d")
        assert field.value == "a\nb\nc\nd" and not provider.requests
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 5)
        assert provider.requests[0][-1].text == "a\nb\nc\nd"
        assert [e.body for e in app.query(ChatEntry) if e.speaker == "You"] == ["a\nb\nc\nd"]
        # Pasted lines stay as they are and do not submit (the driver posts paste to the app).
        app.post_message(events.Paste("第一行\n第二行"))
        await pilot.pause()
        assert field.value == "第一行\n第二行" and len(provider.requests) == 1
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 5)
        assert provider.requests[1][-1].text == "第一行\n第二行"
        # ↑ on the first row browses history; ↓ past the newest restores the draft.
        field.value = "草稿"
        await pilot.press("up")
        assert field.value == "第一行\n第二行"
        field.move_cursor((0, 0))
        await pilot.press("up")
        assert field.value == "a\nb\nc\nd" and field.cursor_location == (3, 1)
        field.move_cursor((0, 0))
        await pilot.press("down")
        assert field.value == "a\nb\nc\nd" and field.cursor_location[0] == 1  # Moves within the text.
        field.move_cursor(field.document.end)
        await pilot.press("down")
        assert field.value == "第一行\n第二行"
        await pilot.press("down")
        assert field.value == "草稿"
        # With command hints open, ↑/↓ select hints instead of history.
        field.value = "/"
        await pilot.pause()
        assert app._hint_choices
        await pilot.press("down")
        assert field.value == "/"


async def test_history_steps_through_recalled_commands_without_opening_hints():
    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        await send(app, pilot, "/help")
        await send(app, pilot, "/permissions")
        await pilot.press("a", "b", "c")
        steps = []
        for key in ("up", "up", "down", "down"):
            await pilot.press(key)
            steps.append((field.value, bool(app._hint_choices)))
        assert steps == [("/permissions", False), ("/help", False), ("/permissions", False), ("abc", False)]
        # Editing a recalled command brings its hints back.
        await pilot.press("up", "backspace")
        assert field.value == "/permission" and app._hint_choices
        # Enter on an unedited recalled command runs it instead of completing a hint.
        await pilot.press("escape")
        field.value = ""
        await pilot.press("up", "up", "enter")
        await pilot.pause()
        assert field.value == "" and "/help" in [e.body for e in app.query(ChatEntry) if e.speaker == "You"][-1]


async def test_queued_messages_join_at_the_next_iteration_boundary(tmp_path):
    gate, started = asyncio.Event(), asyncio.Event()
    provider = SequenceProvider(
        held(response("先写入", [call("write_file", path="a", content="one")]), gate, started),
        response("已按新要求完成"),
    )
    app = MossCodeApp(Conversation(provider, registry=bypass(tmp_path)), "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "执行任务")
        await asyncio.wait_for(started.wait(), 3)
        field = app.query_one("#prompt", PromptArea)
        assert not field.disabled
        for text in ("改用 b", "再补充\n一行"):
            field.value = text
            await pilot.press("enter")
            await pilot.pause()
        assert field.value == "" and app.conversation.queued == ("改用 b", "再补充\n一行")
        assert "已排队 2 条" in status(app)
        queue = app.query_one("#queue", Static)
        assert queue.display and "↳ 排队：改用 b" in str(queue.render())
        gate.set()
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        last = provider.requests[1][0][-1]
        assert last.role == "user" and last.text == "改用 b\n\n再补充\n一行"
        assert [e.body for e in app.query(ChatEntry) if e.speaker == "You"] == ["执行任务", "改用 b", "再补充\n一行"]
        assert not app.conversation.queued and not queue.display and "已排队" not in status(app)
        assert len(provider.requests) == 2


async def test_up_takes_queued_messages_back_without_interrupting(tmp_path):
    gate, started = asyncio.Event(), asyncio.Event()
    provider = SequenceProvider(
        held(response("先写入", [call("write_file", path="a", content="one")]), gate, started), response("完成")
    )
    app = MossCodeApp(Conversation(provider, registry=bypass(tmp_path)), "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "执行任务")
        await asyncio.wait_for(started.wait(), 3)
        field = app.query_one("#prompt", PromptArea)
        for text in ("改用 b", "再补充\n一行"):
            field.value = text
            await pilot.press("enter")
            await pilot.pause()
        assert "已排队 2 条" in status(app)
        await pilot.press("up")
        await pilot.pause()
        queue = app.query_one("#queue", Static)
        assert field.value == "改用 b\n\n再补充\n一行"
        assert not app.conversation.queued and not queue.display and "已排队" not in status(app)
        assert app._task_running and not app._reply_task.cancelling()
        # With the queue empty, ↑ on the top row browses input history again.
        field.move_cursor((0, 0))
        await pilot.press("up")
        await pilot.pause()
        assert field.value == "再补充\n一行"
        field.clear()
        gate.set()
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        assert [m.text for m in provider.requests[1][0] if m.role == "user"] == ["执行任务"]
        assert len(provider.requests) == 2 and field.value == ""


async def test_queue_during_final_reply_becomes_the_next_task(tmp_path):
    gate, started = asyncio.Event(), asyncio.Event()
    provider = SequenceProvider(held(response("回答"), gate, started), response("追问已回答"))
    app = MossCodeApp(Conversation(provider, registry=bypass(tmp_path)), "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "问题")
        await asyncio.wait_for(started.wait(), 3)
        app.query_one("#prompt", PromptArea).value = "追问"
        await pilot.press("enter")
        gate.set()
        await wait_until(pilot, lambda: len(provider.requests) == 2 and app._reply_task.done())
        assert provider.requests[1][0][-1].text == "追问"
        assert [m.role for m in provider.requests[1][0]] == ["user", "assistant", "user"]
        assert [e.body for e in app.query(ChatEntry) if e.speaker == "You"] == ["问题", "追问"]


async def test_cancel_returns_queue_to_input_and_commands_are_not_queued(tmp_path):
    gate, started = asyncio.Event(), asyncio.Event()
    provider = SequenceProvider(held(response("长回答"), gate, started))
    conv = Conversation(provider, registry=bypass(tmp_path))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "开始")
        await asyncio.wait_for(started.wait(), 3)
        field = app.query_one("#prompt", PromptArea)
        for command in ("/help", "/clear"):
            field.post_message(PromptArea.Submitted(field, command))
            await pilot.pause()
            assert not conv.queued and "运行中只能排队消息" in status(app)
        assert len(conv.history) == 0 and not app.query(".local-note")
        field.value = "稍后再做"
        await pilot.press("enter")
        await pilot.pause()
        field.value = "草稿"
        # The first Esc only withdraws the queue; the running task keeps going.
        await pilot.press("escape")
        await pilot.pause()
        assert field.value == "稍后再做\n\n草稿" and not conv.queued
        assert app._task_running and not app._reply_task.cancelling()
        assert "已取回排队消息" in status(app)
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        assert field.value == "稍后再做\n\n草稿" and not conv.queued
        assert len(provider.requests) == 1


async def test_ctrl_c_cancels_at_once_and_returns_the_queue(tmp_path):
    gate, started = asyncio.Event(), asyncio.Event()
    provider = SequenceProvider(held(response("长回答"), gate, started))
    conv = Conversation(provider, registry=bypass(tmp_path))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "开始")
        await asyncio.wait_for(started.wait(), 3)
        field = app.query_one("#prompt", PromptArea)
        field.value = "稍后再做"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+c")
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        assert field.value == "稍后再做" and not conv.queued
        assert len(provider.requests) == 1


def test_diff_rows_number_color_separate_hunks_and_truncate():
    before = "".join(f"line {i}\n" for i in range(1, 41))
    after = before.replace("line 3\n", "line three\n").replace("line 30\n", "line thirty\n")
    text = diff_rows(unified_diff(before, after, "f.txt"))
    plain = text.plain
    assert "    3 - line 3" in plain and "    3 + line three" in plain
    assert "   30 - line 30" in plain and "   30 + line thirty" in plain
    assert "⋯" in plain and "---" not in plain and "@@" not in plain
    # The line number is secondary; the color marks only the changed line.
    styles = {text.plain[s.start : s.end].strip(): str(s.style) for s in text.spans}
    assert styles["- line 3"] == ERROR_COLOR and styles["+ line three"] == TOOL_SUCCESS_COLOR
    assert styles["3"] == styles["30"] == MUTED_COLOR and "line 2" not in styles
    big = diff_rows(unified_diff("".join(f"{i}\n" for i in range(30)), "".join(f"x{i}\n" for i in range(30)), "g"))
    rows = [line for line in big.plain.splitlines() if line.strip()]
    assert len(rows) == DIFF_ROWS + 1 and rows[-1].strip() == f"… 还有 {60 - DIFF_ROWS} 行 diff 未显示"


async def test_tool_rows_show_diff_for_edit_and_content_for_new_file(tmp_path):
    (tmp_path / "old.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    new_file = "".join(f"row {i}\n" for i in range(1, 11))
    provider = SequenceProvider(
        response(calls=[call("read_file", "r", path="old.py")]),
        response(
            calls=[
                call("edit_file", "e", path="old.py", old_text="b = 2", new_text="b = 3"),
                call("write_file", "w", path="new.txt", content=new_file),
            ]
        ),
        response("完成"),
    )
    app = MossCodeApp(Conversation(provider, registry=bypass(tmp_path)), "Demo")
    async with app.run_test(size=(100, 40)) as pilot:
        await send(app, pilot, "修改")
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        _, edit, write = (str(row.render()) for row in app.query(ToolEntry))
        assert "    2 - b = 2" in edit and "    2 + b = 3" in edit and "    1   a = 1" in edit
        assert "    1 + row 1" in write and "    8 + row 8" in write and "row 9" not in write
        assert "… 还有 2 行未显示" in write


async def test_status_shows_mode_badge_model_hint_and_tokens(tmp_path):
    conv = Conversation(FakeProvider(), registry=bypass(tmp_path, "default"))
    app = MossCodeApp(conv, "test", settings=TWO, model="model-a")
    async with app.run_test(size=(100, 30)) as pilot:
        for mode in MODES:
            conv.set_permission_mode(mode)
            app._status("")
            assert status(app) == MODE_BADGES[mode] + " (Shift+Tab 切换)"
            badge = app.query_one("#status", Static).render().spans[0]
            expected = {
                "acceptEdits": TOOL_SUCCESS_COLOR,
                "plan": OPERATION_COLOR,
                "bypassPermissions": ERROR_COLOR,
            }.get(mode)
            assert badge.end == len(MODE_BADGES[mode])
            assert (badge.style.foreground.hex.lower() if badge.style.foreground else None) == (
                expected and expected.lower()
            )
        await pilot.resize_terminal(60, 30)
        await pilot.pause()
        app._status("")
        assert "Shift+Tab" not in status(app) and "model-a" not in status(app)
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, 5)
        assert "上下文 约" in status(app) and "Token" not in status(app)


async def test_status_shows_context_against_the_window_and_the_cache_hit_rate(tmp_path):
    from mosscode.context import ContextBudget

    provider = SequenceProvider(response("好", usage=Usage(8930, 11, 2944, 0)))
    conv = Conversation(
        provider, registry=bypass(tmp_path, "default"), budget=ContextBudget.for_provider(1_000_000, 4096)
    )
    app = MossCodeApp(conv, "test", settings=TWO, model="model-a")
    async with app.run_test(size=(120, 30)) as pilot:
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        usage = "上下文 约 9K / 1M（1%） · 缓存命中 33%"
        # Usage sits at the right edge of the row, apart from the items on the left.
        shown = app.query_one("#status", Static).render()
        assert status(app).startswith("默认权限 (Shift+Tab 切换)  ") and status(app).endswith("  " + usage)
        assert shown.cell_length == app.query_one("#status").content_size.width
        # Too narrow for a gap: it follows the other items as one more of them.
        await pilot.resize_terminal(44, 30)
        await pilot.pause()
        assert status(app) == "默认权限 · " + usage


async def test_status_names_the_model_once_the_banner_is_out_of_date():
    def factory(config):
        return FakeProvider()

    app = MossCodeApp(Conversation(FakeProvider()), "test", settings=TWO, model="model-a", provider_factory=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.query_one("#banner").remove()  # Committed to the scrollback, as in the inline mode.
        await send(app, pilot, "/model other")
        assert app.provider_name == "other" and " · other" in status(app)


async def test_clear_starts_fresh_context_and_keeps_mode(tmp_path):
    reg = bypass(tmp_path, "acceptEdits")
    provider = SequenceProvider(response("一"), response("二"), response("三"))
    conv = Conversation(provider, registry=reg)
    app = MossCodeApp(conv, "Demo")

    def reminders(index):
        return [s.kind for s in provider.requests[index][1].supplements if s.kind != "environment"]

    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "/plan")
        await send(app, pilot, "问题一")
        await asyncio.wait_for(app._reply_task, 5)
        await send(app, pilot, "问题二")
        await asyncio.wait_for(app._reply_task, 5)
        assert reminders(0) == ["system-reminder"] and reminders(1) == []
        assert "上下文 约" in status(app)
        await send(app, pilot, "/clear")
        assert "已开始新会话" in str(app.query(".local-note")[-1].render())
        assert not conv.history and conv.total_usage.requests == 0 and "上下文" not in status(app)
        assert reg.permissions.mode == "plan" and conv.mode == "plan"
        await send(app, pilot, "问题三")
        await asyncio.wait_for(app._reply_task, 5)
        assert [m.text for m in provider.requests[2][0]] == ["问题三"]
        assert reminders(2) == ["system-reminder"]  # The fresh context hears the plan rules again.


async def test_clear_is_refused_while_the_loop_is_busy():
    conv = Conversation(FakeProvider())
    conv._agent.busy = True
    with pytest.raises(ProviderError):
        await conv.clear()
    with pytest.raises(ProviderError):
        await conv.set_provider(FakeProvider())


async def test_model_switch_by_argument_and_panel_keeps_history():
    first, created = FakeProvider(), []

    def factory(config):
        created.append((config.name, FakeProvider()))
        return created[-1][1]

    conv = Conversation(first)
    app = MossCodeApp(conv, "test", settings=TWO, model="model-a", provider_factory=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, 5)
        await send(app, pilot, "/model missing")
        assert "可用：test、other" in str(app.query(".local-note")[-1].render()) and not created
        await send(app, pilot, "/model other")
        assert first.closed and [name for name, _ in created] == ["other"]
        # The banner is still on screen and shows the new model, so the status bar need not.
        assert app.provider_name == app._banner_provider == "other"
        assert "other" not in status(app)
        await send(app, pilot, "again")
        await asyncio.wait_for(app._reply_task, 5)
        second = created[0][1]
        assert [m.text for m in second.requests[0]] == ["hello", "partial", "again"] and len(first.requests) == 1
        await send(app, pilot, "/model")
        assert app.query_one("#model-panel").display and app.query_one("#models").has_focus
        await pilot.press("up", "enter")
        await pilot.pause()
        assert not app.query_one("#model-panel").display and app.query_one("#prompt", PromptArea).has_focus
        assert app.provider_name == "test" and second.closed and conv.provider is created[1][1]
        assert len(conv.history) == 4


class Captured(Exception):
    pass


def capturing_client(protocol):
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        raise Captured()

    endpoint = SimpleNamespace(create=create)
    client = SimpleNamespace(
        messages=endpoint, responses=endpoint, chat=SimpleNamespace(completions=endpoint), base_url=""
    )
    return client, seen


# History as set_provider leaves it: protocol-native blocks stripped.
PORTABLE_HISTORY = [
    Message("user", (ContentBlock("text", "读 a"),)),
    Message("assistant", (ContentBlock("text", "先读"),), tool_calls=(ToolCall("c1", "read_file", '{"path": "a"}'),)),
    Message("tool", (), tool_results=(ToolResult("c1", "read_file", "内容"),)),
    Message("user", (ContentBlock("text", "排队的补充"),)),
    Message("assistant", (), tool_calls=(ToolCall("c2", "glob", '{"pattern": "*"}'),)),
    Message("tool", (), tool_results=(ToolResult("c2", "glob", "a"),)),
]


async def request_for(cls, protocol):
    client, seen = capturing_client(protocol)
    provider = cls(ProviderConfig("p", protocol, "m", max_retries=0), client=client)
    with pytest.raises(Captured):
        async for _ in provider.stream(PORTABLE_HISTORY, "系统"):
            pass
    return seen


async def test_set_provider_strips_native_blocks_but_keeps_calls_and_results():
    conv = Conversation(FakeProvider())
    native = Message(
        "assistant",
        (ContentBlock("text", "先读"),),
        "openai-responses",
        ({"type": "reasoning", "id": "r"},),
        tool_calls=(ToolCall("c1", "read_file", '{"path": "a"}'),),
    )
    conv._agent.history = [PORTABLE_HISTORY[0], native, *PORTABLE_HISTORY[2:]]
    await conv.set_provider(FakeProvider())
    assert list(conv.history) == PORTABLE_HISTORY


async def test_anthropic_rebuilds_tool_use_and_merges_same_role_turns():
    turns = (await request_for(AnthropicProvider, "anthropic-messages"))["messages"]
    roles = [t["role"] for t in turns]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert [b["type"] for b in turns[1]["content"]] == ["text", "tool_use"]
    assert turns[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a"}}
    assert [b["type"] for b in turns[2]["content"]] == ["tool_result", "text"]
    assert [b["type"] for b in turns[3]["content"]] == ["tool_use"]  # No empty text block.
    assert turns[4]["content"][0]["tool_use_id"] == "c2"


async def test_responses_and_chat_pair_every_call_with_its_result():
    items = (await request_for(ResponsesProvider, "openai-responses"))["input"]
    calls = [i["call_id"] for i in items if i.get("type") == "function_call"]
    outputs = [i["call_id"] for i in items if i.get("type") == "function_call_output"]
    assert calls == outputs == ["c1", "c2"]
    assert {"role": "assistant", "content": "先读"} in items and {"role": "user", "content": "排队的补充"} in items
    chat = (await request_for(ChatCompletionsProvider, "openai-chat-completions"))["messages"]
    called = [c["id"] for m in chat if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in chat if m["role"] == "tool"]
    assert called == answered == ["c1", "c2"]


def run_cli(monkeypatch, capsys, args, provider=None):
    fake, _ = setup_cli(monkeypatch, args)
    if provider is not None:
        monkeypatch.setattr(cli, "create_provider", lambda config: provider)
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err, provider or fake


def test_short_flag_text_output_is_unchanged(monkeypatch, capsys):
    code, out, _, provider = run_cli(monkeypatch, capsys, ["-p", "hello"])
    assert code == 0 and out == "partial\n" and provider.closed


def test_json_outputs_one_result_object(monkeypatch, capsys):
    code, out, _, provider = run_cli(monkeypatch, capsys, ["-p", "hello", "--output-format", "json"])
    result = json.loads(out)
    assert code == 0 and provider.closed
    assert result["type"] == "result" and result["subtype"] == "completed" and result["is_error"] is False
    assert result["result"] == "partial" and result["num_turns"] == 1 and result["model"] == "model-a"
    assert result["permission_mode"] == "default" and result["usage"]["requests"] == 1
    assert isinstance(result["duration_ms"], int) and result["total_cost_usd"] is None  # No usage, no price.


def test_stream_json_lines_include_tools_in_order(monkeypatch, capsys):
    provider = SequenceProvider(
        response("先看文件", [call("read_file", "r", path="pyproject.toml")], Usage(5, 2)),
        response("读完了", usage=Usage(7, 3)),
    )
    provider.config = ProviderConfig(
        "a",
        "openai-responses",
        "model-a",
        catalog=ModelInfo("model-a", "openai", None, None, 1e-6, 2e-6, None, None, None),
    )
    code, out, _, _ = run_cli(monkeypatch, capsys, ["-p", "看看", "--output-format", "stream-json"], provider)
    events = [json.loads(line) for line in out.splitlines()]
    kinds = [e["type"] for e in events if e["type"] != "notice"]
    assert code == 0 and kinds == ["session", "system", "assistant", "tool_result", "assistant", "result"]
    session, init, first, tool, final, result = (e for e in events if e["type"] != "notice")
    assert session["resumed"] is False and session["id"] == result["session_id"]
    assert init["subtype"] == "init" and "read_file" in init["tools"] and init["model"] == "model-a"
    assert first["tool_calls"] == [{"id": "r", "name": "read_file", "arguments": {"path": "pyproject.toml"}}]
    assert first["stop_reason"] == "tool_use" and first["usage"]["input_tokens"] == 5
    assert tool["id"] == "r" and tool["is_error"] is False and "[project]" in tool["output"]
    assert final["text"] == "读完了" and final["stop_reason"] == "stop"
    assert result["num_turns"] == 2 and result["usage"]["input_tokens"] == 12
    assert result["total_cost_usd"] == pytest.approx(12 * 1e-6 + 5 * 2e-6)


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_errors_report_is_error_and_exit_one(monkeypatch, capsys, fmt):
    provider = FakeProvider()
    provider.fail = True
    code, out, _, _ = run_cli(monkeypatch, capsys, ["-p", "hello", "--output-format", fmt], provider)
    result = json.loads(out.splitlines()[-1])
    assert code == 1 and result["is_error"] is True and result["subtype"] == "error"
    assert result["result"] == "test failure" and provider.closed


def test_output_format_requires_prompt(monkeypatch, capsys):
    code, _, err, _ = run_cli(monkeypatch, capsys, ["--output-format", "json"])
    assert code == 2 and "--output-format" in err
