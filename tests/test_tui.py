import asyncio

import pytest
from textual.widgets import Footer, Header, OptionList, Static

from mosscode.builtin_commands import BUILTIN
from mosscode.config import ProviderConfig, Settings
from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message
from mosscode.tui import ChatEntry, ConfigTrustApp, MossCodeApp, PluginTrustApp, PromptArea, ProviderSelectApp
from test_conversation import FakeProvider


@pytest.mark.parametrize(
    "default,keys,selected_index",
    [
        ("second", ("enter",), 1),
        ("second", ("up", "enter"), 0),
        (None, ("down", "enter"), 1),
    ],
)
async def test_startup_picker_selects_model_and_chat_shows_choice(default, keys, selected_index):
    settings = Settings(
        (
            ProviderConfig("first", "anthropic-messages", "model-a"),
            ProviderConfig("second", "openai-chat-completions", "model-b"),
        ),
        default,
    )
    picker = ProviderSelectApp(settings)
    async with picker.run_test() as pilot:
        assert picker.query_one(OptionList).highlighted == (1 if default == "second" else 0)
        await pilot.press(*keys)
    selected = picker.return_value
    assert selected == settings.providers[selected_index]

    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), selected.name)
    async with app.run_test() as pilot:
        assert str(app.query_one("#status", Static).render()) == "未启用工具"
        rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert rendered.count(selected.name) == 1
        assert selected.model not in rendered
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert len(provider.requests) == 1
    assert provider.closed


async def test_escape_exits_startup_picker_without_selection():
    settings = Settings((ProviderConfig("first", "anthropic-messages", "model-a"),), "first")
    picker = ProviderSelectApp(settings)
    async with picker.run_test() as pilot:
        await pilot.press("escape")
    assert picker.return_value is None


@pytest.mark.parametrize("keys,expected", [(("enter",), True), (("down", "enter"), False), (("escape",), None)])
async def test_project_plugin_panel_trusts_only_on_the_first_choice(keys, expected):
    panel = PluginTrustApp(["kit", "later"])
    async with panel.run_test() as pilot:
        assert "kit、later" in str(panel.query_one("#selection-help", Static).render())
        await pilot.press(*keys)
    assert panel.return_value is expected


@pytest.mark.parametrize("keys,expected", [(("enter",), True), (("down", "enter"), False), (("escape",), None)])
async def test_project_config_panel_trusts_only_on_the_first_choice(keys, expected):
    panel = ConfigTrustApp("leave")
    async with panel.run_test() as pilot:
        assert "leave" in str(panel.query_one("#selection-help", Static).render())
        await pilot.press(*keys)
    assert panel.return_value is expected


async def send(app, pilot, text):
    field = app.query_one("#prompt", PromptArea)
    field.value = text
    await pilot.press("enter")
    await pilot.pause()


async def test_tui_sends_multiturn_and_renders_markdown():
    provider = FakeProvider()
    conv = Conversation(provider)
    app = MossCodeApp(conv, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "one")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert len(conv.history) == 2
        assert not app.query_one("#prompt", PromptArea).disabled
        assert answer_rows(app, app.query(ChatEntry)[1]) == ["partial"]
        await send(app, pilot, "two")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert len(conv.history) == 4
    assert provider.closed


async def test_escape_cancels_without_poisoning_next_message():
    provider = FakeProvider()
    provider.hold = True
    conv = Conversation(provider)
    app = MossCodeApp(conv, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "cancel")
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        # The prompt stays usable while generating, to queue corrections.
        assert not app.query_one("#prompt", PromptArea).disabled
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert provider.stream_closed
        assert conv.history == ()
        assert not app.query_one("#prompt", PromptArea).disabled
        provider.hold = False
        await send(app, pilot, "next")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert len(conv.history) == 2
        assert provider.requests[-1][0].text == "next"


async def test_quit_closes_active_stream_and_provider():
    provider = FakeProvider()
    provider.hold = True
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "cancel on quit")
        await asyncio.wait_for(provider.started.wait(), timeout=2)
    assert provider.stream_closed
    assert provider.closed


async def test_cancel_after_response_completion_does_not_mark_committed_turn_cancelled(monkeypatch):
    rendered = asyncio.Event()
    resume = asyncio.Event()
    original_finish = ChatEntry.finish

    async def slow_finish(self, message, **options):
        rendered.set()
        await resume.wait()
        await original_finish(self, message, **options)

    monkeypatch.setattr(ChatEntry, "finish", slow_finish)
    conv = Conversation(FakeProvider())
    app = MossCodeApp(conv, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "one")
        await asyncio.wait_for(rendered.wait(), timeout=2)
        assert len(conv.history) == 2
        await pilot.press("escape")
        assert not app._reply_task.done()
        resume.set()
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert answer_rows(app, app.query(ChatEntry)[1]) == ["partial"]


async def test_chrome_has_no_clickable_toolbar_or_default_command_palette():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        assert not app.query(Header)
        assert not app.query(Footer)
        original_screen = app.screen
        await pilot.press("ctrl+p")
        await pilot.click("#status")
        assert app.screen is original_screen
        assert app.is_running
        assert not provider.closed
        assert app.query_one("#prompt").region.y < app.size.height // 2


async def test_help_and_unknown_commands_stay_local_and_do_not_change_history():
    provider = FakeProvider()
    conv = Conversation(provider)
    app = MossCodeApp(conv, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "/help")
        assert "/clear" in str(app.query(".local-note")[0].render())
        await send(app, pilot, "/unknown")
        assert "未知命令" in str(app.query(".local-note")[1].render())
        assert provider.requests == []
        assert conv.history == ()
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert len(conv.history) == 2
        assert provider.requests[0][0].text == "hello"


TWO_PROVIDERS = Settings(
    (
        ProviderConfig("test", "anthropic-messages", "model-a"),
        ProviderConfig("other", "openai-chat-completions", "model-b"),
    ),
    "test",
)


async def test_model_picker_is_keyboard_only_and_escape_returns_to_input():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test", settings=TWO_PROVIDERS, model="model-a")
    async with app.run_test() as pilot:
        original_screen = app.screen
        banner = app.query_one("#banner")
        await send(app, pilot, "/model")
        assert app.query_one("#model-panel").display
        assert app.screen is original_screen and app.query_one("#banner") is banner
        assert app.query_one("#model-panel").region.y >= app.query_one("#input-row").region.bottom
        models = app.query_one("#models", OptionList)
        assert models.option_count == 2
        rows = shown_rows(app, models)
        assert [row.startswith("❯ ") for row in rows[:2]] == [index == models.highlighted for index in range(2)]
        from mosscode.appearance import COMMAND_HIGHLIGHT_COLOR, MUTED_COLOR

        strips = app.screen._compositor.render_strips()[models.region.y : models.region.y + 2]
        colors = [{s.style.color.name for s in strip if s.text.strip() and "/" in s.text} for strip in strips]
        # The details next to a name are supporting grey, and take the highlight with the name on the chosen row.
        assert colors[models.highlighted] == {COMMAND_HIGHLIGHT_COLOR} and colors[1 - models.highlighted] == {
            MUTED_COLOR
        }
        await pilot.click("#models", offset=(3, 1))
        assert app.query_one("#model-panel").display and app.provider_name == "test"
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#model-panel").display
        assert app.query_one("#prompt", PromptArea).has_focus
        assert provider.requests == [] and not provider.closed


async def test_removed_theme_is_unknown_and_do_without_tools_preserves_conversation():
    provider = FakeProvider()
    conv = Conversation(provider)
    app = MossCodeApp(conv, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, timeout=5)
        history = conv.history
        for command in ("/theme", "/theme dark"):
            await send(app, pilot, command)
            assert "未知命令 /theme" in str(app.query(".local-note")[-1].render())
        for command in ("/do", "/do 继续"):
            await send(app, pilot, command)
            assert str(app.query(".local-note")[-1].render()) == "当前会话未启用工具。"
        assert app.theme == "moss-dark"
        assert conv.history == history
        assert len(provider.requests) == 1


@pytest.mark.parametrize("gone", ["#member-panel", "#member-view"])
async def test_member_timers_skip_while_the_widgets_are_being_removed(gone):
    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test() as pilot:
        await app.query_one(gone).remove()  # Shutting down removes them one at a time.
        app._member_ticks = 1  # The next tick goes on to the panel.
        app._member_tick()
        app._show_members()
        await pilot.pause()


@pytest.mark.parametrize("size", [(44, 20), (100, 30)])
async def test_input_remains_visible_after_long_reply_and_resize(size):
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "long provider name")
    async with app.run_test(size=size) as pilot:
        chat = app.query_one("#chat")
        await chat.mount(ChatEntry("MossCode", "\n".join("A long answer line" for _ in range(80))))
        await pilot.pause()
        field = app.query_one("#prompt", PromptArea)
        assert field.region.bottom <= app.size.height
        assert field.region.width > 0
        await pilot.resize_terminal(60, 22)
        await pilot.pause()
        assert field.region.bottom <= app.size.height


async def test_exit_command_closes_provider_without_request():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "/exit")
    assert provider.closed
    assert provider.requests == []


def terminal_background(segment) -> bool:
    """Default background, or the one deliberate exception: the user-message bar."""
    from mosscode.appearance import USER_MESSAGE_BACKGROUND

    bgcolor = segment.style.bgcolor if segment.style else None
    return bgcolor is None or bgcolor.is_default or bgcolor.name.lower() == USER_MESSAGE_BACKGROUND


def assert_terminal_background(app):
    # Check rendered cells, not just CSS: nested Markdown, focus and disabled
    # states can otherwise reintroduce RGB backgrounds during composition.
    segments = [segment for strip in app.screen._compositor.render_strips() for segment in strip]
    assert segments
    assert all(terminal_background(segment) for segment in segments)


async def test_terminal_background_survives_markdown_model_menu_and_generation(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test", settings=TWO_PROVIDERS)
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        assert_terminal_background(app)
        await send(app, pilot, "/help")
        entry = ChatEntry("MossCode")
        await app.query_one("#chat").mount(entry)
        await entry.finish(
            Message(
                "assistant",
                (
                    ContentBlock(
                        "text",
                        "# Heading\n\nA `code` example.\n\n> Quote\n\n```python\ndef hello():\n    return 42\n```\n\n"
                        "| A | B |\n|---|---|\n| 1 | 2 |",
                    ),
                ),
            )
        )
        await pilot.pause()
        assert_terminal_background(app)
        await send(app, pilot, "/model")
        assert app.query_one("#model-panel").display
        assert_terminal_background(app)
        await pilot.press("escape")
        provider.hold = True
        await send(app, pilot, "hello")
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        assert not app.query_one("#prompt", PromptArea).disabled
        assert_terminal_background(app)
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, timeout=5)


async def test_provider_picker_uses_terminal_background(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    app = ProviderSelectApp(Settings((ProviderConfig("test", "openai-chat-completions", "model"),), None))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert_terminal_background(app)


async def test_slash_hints_filter_show_chinese_descriptions_and_complete_mode_arguments():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        await pilot.press("/")
        await pilot.pause()
        hints = app.query_one("#command-hints", OptionList)
        assert hints.display and hints.option_count == len(BUILTIN.visible())
        rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "查看命令和快捷键" in rendered
        assert "压缩上下文" in rendered  # The list scrolls past eight rows.
        assert_terminal_background(app)
        field.value = "/pe"
        await pilot.pause()
        assert hints.option_count == 1
        await pilot.press("tab")
        await pilot.pause()
        assert field.value == "/permissions " and field.has_focus
        assert hints.option_count == 4
        field.value = "/permissions a"
        await pilot.pause()
        assert hints.option_count == 1
        await pilot.press("down", "tab")
        assert field.value == "/permissions acceptEdits"
        await pilot.press("enter")
        await pilot.pause()
        assert "未启用工具" in str(app.query(".local-note")[-1].render())
        assert not hints.display
        assert provider.requests == [] and app.conversation.history == ()


async def test_enter_completes_partial_exit_before_executing():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "/e"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert field.value == "/exit"
        assert app.is_running and not provider.closed
        assert provider.requests == []
        await pilot.press("enter")
    assert provider.closed


async def test_hint_navigation_enter_and_escape_keep_input_focus_and_text():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        await pilot.press("/", "down", "enter")
        await pilot.pause()
        assert field.value == "/clear" and field.has_focus
        assert not app.query(".local-note")
        await pilot.press("enter")
        await pilot.pause()
        assert "已开始新会话" in str(app.query(".local-note")[-1].render())
        field.value = "/"
        await pilot.pause()
        await pilot.press("escape")
        assert field.value == "/" and field.has_focus
        assert not app.query_one("#command-hints").display
        await pilot.press("h")
        await pilot.pause()
        assert app.query_one("#command-hints").display
        field.value = "请解释 /clear 命令"
        await pilot.pause()
        assert not app.query_one("#command-hints").display
        assert provider.requests == []


async def test_hints_and_input_remain_visible_with_long_chat_in_narrow_terminal():
    app = MossCodeApp(Conversation(FakeProvider()), "test", settings=TWO_PROVIDERS)
    async with app.run_test(size=(44, 20)) as pilot:
        await app.query_one("#chat").mount(ChatEntry("MossCode", "line\n" * 80))
        field = app.query_one("#prompt", PromptArea)
        field.value = "/permissions "
        await pilot.pause()
        assert app.query_one("#command-hints").display
        assert field.region.bottom <= app.size.height
        assert app.query_one("#command-help").region.bottom <= app.size.height
        assert app.query_one("#status").region.bottom <= app.size.height
        await pilot.press("down", "tab")
        assert field.value == "/permissions acceptEdits"
        field.value = "/model"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#model-panel").display
        assert app.query_one("#model-panel").region.bottom <= app.size.height
        assert app.query_one("#status").region.bottom <= app.size.height


async def test_text_is_never_dim_and_only_uses_the_palette(monkeypatch):
    from mosscode.appearance import (
        BRAND_COLOR,
        COMMAND_HIGHLIGHT_COLOR,
        EIGHTHS,
        MUTED_COLOR,
        OPERATION_COLOR,
        PARAMETER_COLOR,
        TOOL_SUCCESS_COLOR,
    )

    def colored(segment, color):
        return bool(segment.style and segment.style.color and segment.style.color.name == color)

    def plain(segment):
        return not segment.style or not segment.style.color or segment.style.color.is_default

    monkeypatch.delenv("NO_COLOR", raising=False)
    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test(size=(100, 60)) as pilot:
        entry = ChatEntry("MossCode")
        await app.query_one("#chat").mount(entry)
        reply = "# Title\n\nA `code` sample.\n\n```python\n# comment\nreturn 42\n```\n\n```\nunlabelled 7\n```"
        await entry.finish(Message("assistant", (ContentBlock("text", reply),)))
        app.query_one("#prompt", PromptArea).value = "/"
        await pilot.pause()
        segments = [segment for strip in app.screen._compositor.render_strips() for segment in strip]
        logo = [segment for segment in segments if segment.text and set(segment.text) <= set(EIGHTHS.strip())]
        assert logo and not any(plain(segment) for segment in logo)
        text_segments = [segment for segment in segments if any(char.isalnum() for char in segment.text)]
        assert text_segments
        palette = {
            COMMAND_HIGHLIGHT_COLOR,
            TOOL_SUCCESS_COLOR,
            PARAMETER_COLOR,
            OPERATION_COLOR,
            BRAND_COLOR,
            MUTED_COLOR,
        }
        assert all(
            segment.style and (plain(segment) or segment.style.color.name in palette) and not segment.style.dim
            for segment in text_segments
        )
        code = [segment for segment in segments if segment.text == "code"]
        assert code and all(colored(segment, PARAMETER_COLOR) for segment in code)
        # A block that names its language is colored by token; the heading and an unlabelled block
        # keep the terminal's own color.
        for text, color in (("# comment", MUTED_COLOR), ("return", BRAND_COLOR), ("42", OPERATION_COLOR)):
            found = [segment for segment in segments if segment.text.strip() == text]
            assert found and all(colored(segment, color) for segment in found), text
        ordinary = [segment for segment in segments if "Title" in segment.text or "unlabelled 7" in segment.text]
        assert len(ordinary) == 2 and all(plain(segment) for segment in ordinary)
        assert_terminal_background(app)


async def test_escape_dismissal_does_not_hide_retyped_slash_and_selected_row_is_blue(monkeypatch):
    from mosscode.appearance import COMMAND_HIGHLIGHT_COLOR, MUTED_COLOR

    monkeypatch.delenv("NO_COLOR", raising=False)
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        hints = app.query_one("#command-hints", OptionList)
        await pilot.press("/")
        for _ in range(3):
            await pilot.press("escape")
            assert field.value == "/" and not hints.display
            await pilot.resize_terminal(98, 30)
            await pilot.pause()
            assert not hints.display
            await pilot.press("backspace", "/")
            await pilot.pause()
            assert field.value == "/" and hints.display and hints.option_count == len(BUILTIN.visible())

        def assert_row_color(command, selected):
            # Only the list's own rows: the banner's tip names /help too.
            strips = app.screen._compositor.render_strips()[hints.region.y : hints.region.bottom]
            row = next(strip for strip in strips if command in strip.text)
            text_segments = [segment for segment in row if any(char.isalnum() for char in segment.text)]
            assert len(text_segments) >= 2  # The command and its description.
            assert all(segment.style and segment.style.color for segment in text_segments)
            colors = [segment.style.color for segment in text_segments]
            if selected:
                # The whole row, description included, is blue behind the pointer.
                assert all(color.name == COMMAND_HIGHLIGHT_COLOR for color in colors)
                assert row.text.lstrip().startswith("❯ " + command)
            else:
                assert colors[0].is_default and all(color.name == MUTED_COLOR for color in colors[1:])
                assert row.text.lstrip().startswith(command)

        assert_row_color("/help", True)
        assert_row_color("/clear", False)
        await pilot.press("down")
        await pilot.pause()
        assert_row_color("/clear", True)
        assert_row_color("/help", False)
        assert field.has_focus
        assert_terminal_background(app)
        assert provider.requests == []


class ThinkingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.thinking_ready = asyncio.Event()
        self.text_ready = asyncio.Event()
        self.advance = asyncio.Event()

    async def stream(self, messages, system):
        self.requests.append(tuple(messages))
        try:
            yield Delta("thinking", "服务返回的思考片段")
            self.thinking_ready.set()
            await self.advance.wait()
            self.advance.clear()
            yield Delta("text", "**分块")
            self.text_ready.set()
            await self.advance.wait()
            yield Delta("text", "回复**")
            yield Completed(
                Message(
                    "assistant",
                    (
                        ContentBlock("thinking", "服务返回的思考片段"),
                        ContentBlock("text", "**分块回复**"),
                    ),
                ),
                "stop",
            )
        finally:
            self.stream_closed = True


def answer_rows(app, entry) -> list[str]:
    region = entry.query_one(".answer").region
    strips = app.screen._compositor.render_strips()[region.y : region.bottom]
    return [strip.crop(region.x, region.right).text.rstrip() for strip in strips]


async def test_thinking_timer_collapses_before_streaming_and_reply_renders_while_it_streams(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("mosscode.tui.monotonic", lambda: clock[0])
    provider = ThinkingProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(100, 40)) as pilot:
        await send(app, pilot, "你好")
        await asyncio.wait_for(provider.thinking_ready.wait(), timeout=2)
        entry = app.query(ChatEntry)[1]
        clock[0] = 102.5
        app._tick()
        assert entry.query_one(".thinking").display
        assert "2.5s" in str(entry.query_one(".thinking-label", Static).render())
        assert not entry.query_one(".message-row").display
        assert app.conversation.history == ()

        provider.advance.set()
        await asyncio.wait_for(provider.text_ready.wait(), timeout=2)
        await pilot.pause()
        assert not entry.query_one(".thinking").display
        assert "✻ 思考 2.5s" == str(entry.query_one(".thinking-label", Static).render())
        assert answer_rows(app, entry) == ["**分块"]  # Bold that has not been closed yet reads as typed.
        visible = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "服务返回的思考片段" not in visible
        assert "输出中" in visible and "❯ 你好" in visible

        clock[0] = 104.0
        provider.advance.set()
        await asyncio.wait_for(app._reply_task, timeout=5)
        await pilot.pause()
        assert answer_rows(app, entry) == ["分块回复"]
        answer = entry.query_one(".answer")
        row = app.screen._compositor.render_strips()[answer.region.y]
        assert any(segment.text.strip() and segment.style.bold for segment in row)
        assert "✻ 完成 · 4.0s" == str(entry.query_one(".turn-summary", Static).render())
        assert app.conversation.history[1].content[0].kind == "thinking"
        assert provider.stream_closed


async def test_cancelling_thinking_collapses_text_without_committing_history():
    provider = ThinkingProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "cancel")
        await asyncio.wait_for(provider.thinking_ready.wait(), timeout=2)
        entry = app.query(ChatEntry)[1]
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, timeout=5)
        assert not entry.query_one(".thinking").display
        assert entry.query_one(".message-row").display
        assert "已取消" in "\n".join(answer_rows(app, entry))
        assert app.conversation.history == ()
        assert not app.query_one("#prompt", PromptArea).disabled
        assert provider.stream_closed


async def test_completed_turns_and_local_commands_remain_in_scrollable_transcript():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(44, 20)) as pilot:
        for index in range(6):
            await send(app, pilot, f"问题 {index}")
            await asyncio.wait_for(app._reply_task, timeout=5)
        await send(app, pilot, "/model")
        await pilot.pause()
        assert len(app.query(ChatEntry)) == 13
        assert len(app.conversation.history) == 12
        assert app.query(ChatEntry)[0].body == "问题 0"
        assert not app.query(".speaker")
        assert all(not entry.query_one(".thinking-label").display for entry in app.query(".assistant-entry"))
        chat = app.query_one("#chat")
        assert app.query_one("#banner").parent is chat
        assert chat.scroll_y > 0
        previous_scroll = chat.scroll_y
        chat.anchor(False)
        chat.scroll_relative(y=-3, animate=False, immediate=True)
        await pilot.pause()
        assert not chat.is_anchored
        assert chat.scroll_y < previous_scroll
        assert "当前模型：test" in str(app.query(".local-note")[-1].render())
        assert app.query_one("#prompt").region.bottom <= app.size.height


async def test_history_uses_remaining_terminal_rows_and_can_jump_to_first_and_latest_turn():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(100, 40)) as pilot:
        for index in range(12):
            await send(app, pilot, f"问题 {index:02d}")
            await asyncio.wait_for(app._reply_task, timeout=5)
        await pilot.pause()
        chat = app.query_one("#chat")
        assert chat.region.height >= 34
        assert len(app.conversation.history) == 24
        assert len(provider.requests[-1]) == 23
        assert provider.requests[-1][0].text == "问题 00"
        visible = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "问题 10" in visible and "问题 11" in visible

        chat.anchor(False)
        chat.scroll_home(animate=False)
        await pilot.pause()
        assert chat.scroll_y == 0 and not chat.is_anchored
        visible = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "问题 00" in visible and "问题 01" in visible
        assert app.query_one("#prompt", PromptArea).has_focus
        await pilot.resize_terminal(100, 50)
        await pilot.pause()
        assert chat.region.height >= 44
        chat.anchor()
        chat.scroll_end(animate=False)
        await pilot.pause()
        assert chat.is_anchored and chat.scroll_y == chat.max_scroll_y
        visible = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "问题 11" in visible
        assert app.query_one("#status").region.bottom <= app.size.height


def shown_rows(app, widget) -> list[str]:
    strips = app.screen._compositor.render_strips()[widget.region.y : widget.region.bottom]
    return [strip.text.strip() for strip in strips]


def test_activity_line_turns_with_time_and_colors_the_stage():
    from mosscode.appearance import BRAND_COLOR, MUTED_COLOR, OPERATION_COLOR
    from mosscode.tui import SPINNER, activity_line

    note = "输出中 · 1/200 轮 · 0.5s · Esc 取消"
    line = activity_line(note, 10.0)
    assert line.plain == SPINNER[0] + " " + note
    assert [(line.plain[span.start : span.end], str(span.style)) for span in line.spans] == [
        (SPINNER[0] + " ", BRAND_COLOR),
        ("输出中", OPERATION_COLOR),
        (" · 1/200 轮 · 0.5s · Esc 取消", MUTED_COLOR),
    ]
    # Five frames a second, and the cycle starts over.
    assert [activity_line(note, 10.0 + at).plain[0] for at in (0.25, 0.5, 2.0)] == [SPINNER[1], SPINNER[2], SPINNER[0]]
    assert activity_line("连接中…", 0.0).plain == SPINNER[0] + " 连接中…"


def test_choice_prompt_sets_the_explanation_apart_in_grey():
    from mosscode.appearance import MUTED_COLOR
    from mosscode.tui import choice_prompt

    text = choice_prompt("本会话允许 · 这类操作不再询问 · 退出后失效")
    assert text.plain == "本会话允许  这类操作不再询问 · 退出后失效"
    assert [(text.plain[span.start : span.end], str(span.style)) for span in text.spans] == [
        ("  这类操作不再询问 · 退出后失效", MUTED_COLOR)
    ]
    assert choice_prompt("拒绝").plain == "拒绝" and not choice_prompt("拒绝").spans


async def test_activity_row_shows_the_running_stage_and_takes_no_row_when_idle(monkeypatch):
    from mosscode.appearance import BRAND_COLOR, MUTED_COLOR, OPERATION_COLOR
    from mosscode.tui import SPINNER

    clock = [100.0]
    monkeypatch.setattr("mosscode.tui.monotonic", lambda: clock[0])
    monkeypatch.delenv("NO_COLOR", raising=False)
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        activity, row = app.query_one("#activity", Static), app.query_one("#input-row")

        def directly_above_the_prompt():
            return activity.region.height == 1 and activity.region.bottom == row.region.y

        def gone():
            # Idle, the blank row that ends the scrollback is the gap above the prompt; a second one would double it.
            return str(activity.render()) == "" and activity.region.height == 0

        assert gone()
        provider.hold = True
        await send(app, pilot, "hello")
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        app._tick()
        await pilot.pause()
        first = str(activity.render())
        assert first[0] in SPINNER and first[1:].startswith(" 输出中 · ") and first.endswith("Esc 取消")
        assert directly_above_the_prompt()
        # The stage is no longer one of the status bar's items.
        assert str(app.query_one("#status", Static).render()) == "未启用工具"
        colors = {
            segment.text.strip(): segment.style.color.name
            for segment in app.screen._compositor.render_strips()[activity.region.y]
            if segment.text.strip()
        }
        assert colors[first[0]] == BRAND_COLOR and colors["输出中"] == OPERATION_COLOR
        assert colors[first[5:].strip()] == MUTED_COLOR

        clock[0] += 0.2
        app._tick()
        later = str(activity.render())
        assert later[0] in SPINNER and later[0] != first[0]

        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, timeout=5)
        await pilot.pause()
        assert gone()


async def test_choice_list_points_at_the_highlighted_row_and_ignores_clicks():
    from textual.app import App
    from textual.widgets.option_list import Option

    from mosscode.choice_list import KeyboardOptions
    from mosscode.member_ui import MemberList

    class Lists(App):
        def compose(self):
            yield KeyboardOptions(
                Option("first\nits second line"), Option("unavailable", disabled=True), Option("last")
            )

    app = Lists()
    async with app.run_test(size=(40, 12)) as pilot:
        options = app.query_one(KeyboardOptions)
        options.focus()
        await pilot.pause()

        def pointed():
            # Without the border Textual draws around a bare list.
            return [row.strip("▊▎ ") for row in shown_rows(app, options) if "❯" in row]

        # The pointer marks the first line of the highlighted option; every other line keeps the same indent.
        assert pointed() == ["❯ first"]
        columns = {
            strip.text.index(word)
            for strip in app.screen._compositor.render_strips()
            for word in ("first", "its second line", "unavailable", "last")
            if word in strip.text
        }
        assert len(columns) == 1
        await pilot.click(KeyboardOptions, offset=(4, 3))
        assert options.highlighted == 0
        await pilot.press("down")
        await pilot.pause()
        assert options.highlighted == 2
        assert pointed() == ["❯ last"]
        options.highlighted = 1
        await pilot.pause()
        assert pointed() == []
    assert issubclass(MemberList, KeyboardOptions)


async def test_a_long_choice_list_scrolls_with_a_thin_grey_bar():
    from mosscode.appearance import RULE_COLOR

    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("/")
        await pilot.pause()
        hints = app.query_one("#command-hints", OptionList)
        assert hints.show_vertical_scrollbar and hints.scrollbar_size_vertical == 1
        bar = hints.region.right - 2  # The column left of the list's right padding.
        strips = app.screen._compositor.render_strips()[hints.region.y : hints.region.bottom]
        thumb = [segment for strip in strips for segment in strip.crop(bar, bar + 1) if segment.style.reverse]
        # The thumb is drawn reversed in the rules' grey, not as the theme's near-black block.
        assert thumb and all(segment.style.color.name == RULE_COLOR for segment in thumb)
        assert all(segment.style.bgcolor.is_default for segment in thumb)
