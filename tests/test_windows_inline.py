import sys

import pytest

from mosscode.conversation import Conversation
from mosscode.tui import ChatEntry, MossCodeApp
from test_conversation import FakeProvider


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows inline input adapter")


async def test_stale_mouse_reports_cannot_scroll_or_execute_commands():
    from textual._xterm_parser import XTermParser
    from mosscode.tui import PromptArea
    from mosscode.windows_inline import WindowsInlineDriver

    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test(size=(100, 30)) as pilot:
        chat = app.query_one("#chat")
        await chat.mount(ChatEntry("You", "最早的问题"), ChatEntry("MossCode", "旧回复\n" * 60))
        await pilot.pause()
        chat.anchor()
        await pilot.pause()
        driver = WindowsInlineDriver(app, mouse=False)
        driver.cursor_origin = (0, 100)
        parser = XTermParser()
        latest = chat.scroll_y
        await pilot.press("pageup", "pagedown")
        await pilot.pause()
        assert chat.scroll_y == latest and chat.is_anchored
        for event in parser.feed("\x1b[<64;5;24M"):
            driver.process_message(event)
        await pilot.pause()
        assert chat.scroll_y == latest and chat.is_anchored
        selected_position = chat.scroll_y
        await pilot.press("pageup", "pagedown")
        await pilot.pause()
        assert chat.scroll_y == selected_position
        for event in parser.feed("\x1b[<65;5;24M"):
            driver.process_message(event)
        await pilot.pause()
        assert chat.scroll_y == selected_position

        field = app.query_one("#prompt", PromptArea)
        field.value = "/exit"
        await pilot.pause()
        for event in parser.feed("\x1b[<0;5;24M\x1b[<0;5;24m"):
            driver.process_message(event)
        await pilot.pause()
        assert field.has_focus and field.value == "/exit"
        assert app.is_running and provider.requests == []
