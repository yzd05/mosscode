"""Windows console smoke test: real input/output driver, fake model, automatic exit."""

import asyncio
import json
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from mosscode.tui import PromptArea
from textual.drivers.win32 import get_console_mode

from mosscode.conversation import Conversation
from mosscode.config import ProviderConfig, Settings
from mosscode.tui import ChatEntry, MossCodeApp, ProviderSelectApp
from mosscode.messages import Completed, ContentBlock, Delta, Message, ToolCall
from mosscode.mcp_client import MCPManager
from mosscode.mcp_config import MCPSettings, ServerConfig
from mosscode.windows_inline import WindowsInlineDriver
from preview_tui import ThinkingPreviewProvider
from preview_tools import ToolPreviewProvider, exercise_tools
from preview_agent import AgentPreviewProvider, exercise_plan, exercise_do
from preview_permissions import approval_app, exercise_approval
from mosscode.tools import create_default_registry
from mosscode.builtin_commands import mcp_report


class RecordingStream:
    def __init__(self, stream, writes):
        self.stream = stream
        self.writes = writes

    def write(self, data):
        self.writes.append(data)
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()


class RecordingDriver(WindowsInlineDriver):
    writes = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._file = RecordingStream(self._file, self.writes)


async def choose_provider(pilot):
    assert pilot.app.is_inline
    await pilot.pause()
    assert pilot.app._get_inline_height() < pilot.app.size.height
    await pilot.press("down", "enter")


async def exercise(pilot):
    app = pilot.app
    assert app.is_inline
    await pilot.pause()
    assert app._get_inline_height() < app.size.height
    field = app.query_one("#prompt", PromptArea)
    await pilot.press("/", "m", "o", "tab", "s", "tab")
    assert field.value == "/model second"
    field.value = "hello"
    await pilot.press("enter")
    provider = app.conversation.provider
    await asyncio.wait_for(provider.thinking_ready.wait(), 10)
    await pilot.pause()
    reply = app.query_one(ChatEntry)
    assert reply.query_one(".thinking").display
    provider.advance.set()
    await asyncio.wait_for(provider.text_ready.wait(), 10)
    await pilot.pause()
    assert not reply.query_one(".thinking").display
    assert reply.query_one(".raw").display and not reply.query_one(".answer").display
    provider.advance.set()
    await app._reply_task
    assert not app.query(ChatEntry)
    assert len(app.conversation.history) == 2
    for index in range(5):
        field.value = f"history-{index}"
        await pilot.press("enter")
        await app._reply_task
    await pilot.pause()
    assert len(app.conversation.history) == 12
    assert not app.query_one("#chat").children
    assert "history-0" in "".join(RecordingDriver.writes)
    assert "history-4" in "".join(RecordingDriver.writes)
    field.value = "/model"
    original_screen = app.screen
    await pilot.press("enter")
    await pilot.pause()
    assert app.screen is original_screen
    assert app.query_one("#model-panel").region.y >= app.query_one("#input-row").region.bottom
    await pilot.press("escape")
    await pilot.pause()
    assert not app.query_one("#model-panel").display and app.provider_name == "second"
    field.value = "/e"
    await pilot.pause()
    await pilot.press("enter")
    assert app.is_running and field.value == "/exit"
    await pilot.press("enter")


class LongTranscriptProvider:
    def __init__(self, label, count):
        self.label, self.count = label, count

    async def stream(self, messages, system, tools=(), **kwargs):
        text = "\n".join(f"{self.label}-{index:03d}" for index in range(self.count))
        yield Delta("text", text)
        yield Completed(Message("assistant", (ContentBlock("text", text),)), "stop")

    async def close(self):
        pass


class MCPPreviewProvider:
    def __init__(self):
        self.requests = 0

    async def stream(self, messages, system, tools=(), **kwargs):
        self.requests += 1
        if self.requests == 1:
            yield Completed(Message("assistant", (), tool_calls=(ToolCall(
                "mcp-test", "mcp__console__echo", '{"text":"console MCP"}'),)), "tool_calls")
        else:
            assert messages[-1].tool_results[0].output == "console MCP"
            yield Completed(Message("assistant", (ContentBlock("text", "**MCP 已完成**"),)), "stop")

    async def close(self):
        pass


async def exercise_mcp(pilot):
    app = pilot.app
    await asyncio.wait_for(app._startup_task, 10)
    assert not app.query_one("#prompt", PromptArea).disabled  # MCP connects in the background.
    await asyncio.wait_for(app.conversation.mcp_manager.wait(), 10)
    await pilot.pause()
    app.query_one("#prompt", PromptArea).value = "/mcp"
    await pilot.press("enter")
    await pilot.pause()
    assert "已连接 · 4 个工具" in mcp_report(app.conversation)  # Notes move to native scrollback inline.
    app.query_one("#prompt", PromptArea).value = "/permissions acceptEdits"
    await pilot.press("enter")
    assert app.conversation.registry.permissions.mode == "acceptEdits"
    app.query_one("#prompt", PromptArea).value = "调用外部工具"
    await pilot.press("enter")
    for _ in range(80):
        await pilot.pause(.02)
        if app._approval_request:
            break
    assert app._approval_request and not app._reply_task.done()
    await pilot.press("enter")
    await asyncio.wait_for(app._reply_task, 10)
    assert app.conversation.history[2].tool_results[0].output == "console MCP"
    await pilot.press("ctrl+q")


async def exercise_scrollback(pilot):
    app = pilot.app
    await pilot.pause()
    app.query_one("#prompt", PromptArea).value = "native-scrollback-question"
    await pilot.press("enter")
    await asyncio.wait_for(app._reply_task, 10)
    await pilot.pause()
    assert len(app.conversation.history) == 2 and not app.query_one("#chat").children
    await pilot.press("ctrl+q")


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Run this smoke test in a Windows terminal.")
    print("MOSSCODE_SHELL_SENTINEL: content before the app stays in the main buffer.", flush=True)
    modes_before = tuple(get_console_mode(stream) for stream in (sys.__stdin__, sys.__stdout__))
    settings = Settings((ProviderConfig("first", "openai-chat-completions", "first-model"),
                         ProviderConfig("second", "openai-chat-completions", "second-model")), None)
    picker = ProviderSelectApp(settings)
    picker.driver_class = RecordingDriver
    assert picker.run(inline=True, mouse=False, auto_pilot=choose_provider) == settings.providers[1]
    app = MossCodeApp(Conversation(ThinkingPreviewProvider()), "second", settings=settings, model="second-model")
    app.driver_class = RecordingDriver
    app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=exercise)
    # Consecutive runs in this same real console: long old output, then a short app.
    for label, count in (("OLD-SCROLLBACK", 80), ("NEW-SCROLLBACK", 1)):
        history_app = MossCodeApp(Conversation(LongTranscriptProvider(label, count)), "Demo Provider")
        history_app.driver_class = RecordingDriver
        history_app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=exercise_scrollback)
    with TemporaryDirectory(prefix="mosscode-console-") as temporary:
        root = Path(temporary)
        (root / "demo.py").write_text('def greet():\n    return "Hello, MossCode!"\n', encoding="utf-8")
        tool_app = MossCodeApp(Conversation(ToolPreviewProvider(), registry=create_default_registry(root, permission_mode="bypassPermissions", load_permissions=False)), "Demo Provider")
        tool_app.driver_class = RecordingDriver

        async def exercise_and_exit(pilot):
            await exercise_tools(pilot)
            await pilot.press("ctrl+q")

        tool_app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=exercise_and_exit)
        loop_app = MossCodeApp(Conversation(AgentPreviewProvider(), registry=create_default_registry(root, permission_mode="bypassPermissions", load_permissions=False)), "Demo Provider")
        loop_app.driver_class = RecordingDriver

        async def exercise_loop_and_exit(pilot):
            await exercise_plan(pilot)
            await exercise_do(pilot)
            assert 'Hello, Agent!' in (root / "demo.py").read_text()
            await pilot.press("ctrl+q")

        loop_app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=exercise_loop_and_exit)
        permission_app = approval_app(root)
        permission_app.driver_class = RecordingDriver
        async def exercise_approval_and_exit(pilot):
            await exercise_approval(pilot)
            await pilot.press("ctrl+q")
        permission_app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=exercise_approval_and_exit)
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/mcp_stdio_server.py"
        mcp_manager = MCPManager(root, MCPSettings((ServerConfig(
            "console", "stdio", sys.executable, (str(fixture), str(root / "mcp.log"))),)))
        mcp_app = MossCodeApp(Conversation(MCPPreviewProvider(), registry=create_default_registry(
            root, load_permissions=False), mcp_manager=mcp_manager), "Demo Provider")
        mcp_app.driver_class = RecordingDriver
        mcp_app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=exercise_mcp)
        assert mcp_manager.connections[0].task.done() and (root / "mcp.closed").exists()
    output = "".join(RecordingDriver.writes)
    assert "\x1b[?1049" not in output
    assert "\x1b[2J" not in output
    for code in ("\x1b[?1000h", "\x1b[?1003h", "\x1b[?1006h"):
        assert code not in output
    assert "\x1b[?1000l" in output and "\x1b[?1006l" in output
    permanent = "".join(write.split("\r\x1b[J", 1)[1] for write in RecordingDriver.writes if "\r\x1b[J" in write)
    assert permanent.count("OLD-SCROLLBACK-000") == permanent.count("OLD-SCROLLBACK-079") == 1
    assert permanent.count("NEW-SCROLLBACK-000") == 1
    assert permanent.index("OLD-SCROLLBACK-079") < permanent.index("NEW-SCROLLBACK-000")
    for sgr in re.findall(r"\x1b\[([\d;]*)m", output):
        codes = [int(code or 0) for code in sgr.split(";")]
        index = 0
        while index < len(codes):
            code = codes[index]
            assert code != 48 and not 40 <= code <= 47 and not 100 <= code <= 107
            assert code != 2  # No dim text, including directory and status.
            # RGB / indexed foreground payload values are not SGR commands.
            index += (5 if codes[index + 1] == 2 else 3) if code == 38 else 1
    assert tuple(get_console_mode(stream) for stream in (sys.__stdin__, sys.__stdout__)) == modes_before
    print(json.dumps({"inline": True, "alternate_screen": False, "clear_screen": False,
                      "terminal_background": True, "console_modes_restored": True,
                      "command_completion": "passed",
                      "inline_model_panel": True, "normal_text_brightness": True,
                      "thinking_streaming_markdown": "passed",
                      "tools_and_final_markdown": "passed",
                      "plan_then_execute_multi_iteration_and_usage": "passed",
                      "native_scrollback_across_restarts": "passed",
                      "mouse_reporting_disabled": True,
                      "inline_keyboard_permission_approval": "passed",
                      "mcp_discovery_approval_call_cleanup": "passed",
                      "picker_dialogue_model_exit": "passed"}))
