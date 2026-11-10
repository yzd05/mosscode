import asyncio

import pytest
from textual.widgets import Footer, Header, Markdown, OptionList, Static

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
        assert app.query(ChatEntry)[1].query_one(".answer", Markdown).display
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
        assert app.query(ChatEntry)[1].query_one(".answer", Markdown).display


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
        assert app.query_one("#models", OptionList).option_count == 2
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
    """Default background, or the deliberate exceptions: the user-message bar, and the mascot's pixels
    (the lower half of a ▀ cell is its background)."""
    from mosscode.appearance import MASCOT_COLORS, USER_MESSAGE_BACKGROUND

    bgcolor = segment.style.bgcolor if segment.style else None
    if bgcolor is None or bgcolor.is_default or bgcolor.name.lower() == USER_MESSAGE_BACKGROUND:
        return True
    return set(segment.text) == {"▀"} and bgcolor.name.lower() in MASCOT_COLORS.values()


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


async def test_normal_brightness_and_native_background_with_semantic_accents(monkeypatch):
    from mosscode.appearance import COMMAND_HIGHLIGHT_COLOR, MASCOT_COLORS, PARAMETER_COLOR, TOOL_SUCCESS_COLOR

    monkeypatch.delenv("NO_COLOR", raising=False)
    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test(size=(100, 60)) as pilot:
        entry = ChatEntry("MossCode")
        await app.query_one("#chat").mount(entry)
        await entry.finish(
            Message(
                "assistant",
                (ContentBlock("text", "# Title\n\nA `code` sample.\n\n```python\n# comment\nreturn 42\n```"),),
            )
        )
        app.query_one("#prompt", PromptArea).value = "/"
        await pilot.pause()
        segments = [segment for strip in app.screen._compositor.render_strips() for segment in strip]
        assert any(
            segment.style and segment.style.color and segment.style.color.name == MASCOT_COLORS["w"]
            for segment in segments
        )
        text_segments = [segment for segment in segments if any(char.isalnum() for char in segment.text)]
        assert text_segments
        assert all(
            segment.style
            and (
                not segment.style.color
                or segment.style.color.is_default
                or segment.style.color.name in {COMMAND_HIGHLIGHT_COLOR, TOOL_SUCCESS_COLOR, PARAMETER_COLOR}
            )
            and not segment.style.dim
            for segment in text_segments
        )
        code = [segment for segment in segments if segment.text == "code"]
        assert code and all(segment.style.color.name == PARAMETER_COLOR for segment in code)
        ordinary = [
            segment
            for segment in segments
            if "Title" in segment.text or "comment" in segment.text or "return 42" in segment.text
        ]
        assert ordinary and all(not s.style.color or s.style.color.is_default for s in ordinary)
        assert_terminal_background(app)


async def test_escape_dismissal_does_not_hide_retyped_slash_and_selected_row_is_blue(monkeypatch):
    from mosscode.appearance import COMMAND_HIGHLIGHT_COLOR

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
            row = next(strip for strip in app.screen._compositor.render_strips() if command in strip.text)
            text_segments = [segment for segment in row if any(char.isalnum() for char in segment.text)]
            assert text_segments
            assert all(
                segment.style
                and segment.style.color
                and (
                    segment.style.color.name == COMMAND_HIGHLIGHT_COLOR if selected else segment.style.color.is_default
                )
                for segment in text_segments
            )

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


async def test_thinking_timer_collapses_before_streaming_and_markdown_waits_for_completion(monkeypatch):
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
        assert not entry.query_one(".answer").display
        assert app.conversation.history == ()

        provider.advance.set()
        await asyncio.wait_for(provider.text_ready.wait(), timeout=2)
        await pilot.pause()
        assert not entry.query_one(".thinking").display
        assert "思考用时 2.5s" in str(entry.query_one(".thinking-label", Static).render())
        assert entry.query_one(".raw").display
        assert "**分块" == str(entry.query_one(".raw", Static).render())
        assert not entry.query_one(".answer").display
        visible = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "服务返回的思考片段" not in visible
        assert "输出中" in visible and "❯ 你好" in visible

        clock[0] = 104.0
        provider.advance.set()
        await asyncio.wait_for(app._reply_task, timeout=5)
        await pilot.pause()
        assert entry.query_one(".answer", Markdown).display
        assert not entry.query_one(".raw").display
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
        assert "已取消" in str(entry.query_one(".raw", Static).render())
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
