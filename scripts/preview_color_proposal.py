"""Accepted color design using actual product widgets and fixed demonstration data."""

import asyncio
from pathlib import Path

from preview_tui import PreviewProvider, capture

from mosscode import tui
from mosscode.conversation import Conversation
from mosscode.messages import ContentBlock, Message, ToolCall, ToolProgress, ToolResult


async def main():
    output = Path("docs/ui/previews")
    app = tui.MossCodeApp(Conversation(PreviewProvider()), "DeepSeek V4.1 Flash")
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        chat = app.query_one("#chat")
        await chat.mount(tui.ChatEntry("You", "创建 hello.txt，写入 Hello World，然后读取确认内容。"))
        write = tui.ToolEntry(ToolProgress(ToolCall("write", "write_file", '{"path":"hello.txt"}'), "running"))
        read = tui.ToolEntry(ToolProgress(ToolCall("read", "read_file", '{"path":"hello.txt"}'), "running"))
        await chat.mount(write, read)
        write.complete(ToolResult("write", "write_file", "文件已写入 · 1 行"), 0.1)
        read.complete(ToolResult("read", "read_file", "1: Hello World"), 0.0)
        answer = "已创建并确认 `hello.txt` 的内容为 **Hello World**，读取结果正确。"
        reply = tui.ChatEntry("MossCode")
        await chat.mount(reply)
        await reply.finish(Message("assistant", (ContentBlock("text", answer),)))
        await chat.mount(tui.ChatEntry("You", "检查 app.py，并运行测试。"))
        missing = tui.ToolEntry(ToolProgress(ToolCall("missing", "read_file", '{"path":"app.py"}'), "running"))
        shell = tui.ToolEntry(
            ToolProgress(ToolCall("shell", "execute_command", '{"command":"python -m pytest"}'), "running")
        )
        await chat.mount(missing, shell)
        missing.complete(ToolResult("missing", "read_file", "文件不存在，请检查路径。", True, "not_found"), 0.0)
        app._generating = True
        app._status("执行工具中 · 2/25 轮 · 1.3s · Esc 取消")
        chat.anchor()
        await pilot.pause()
        await pilot.pause()
        capture(app, output / "color-proposal")
        await pilot.resize_terminal(52, 24)
        await pilot.pause()
        # Resize refreshes the ordinary app status; restore the mock operation.
        app._status("执行工具中 · 1.3s · Esc 取消")
        await pilot.pause()
        await pilot.pause()
        capture(app, output / "color-proposal-narrow")


if __name__ == "__main__":
    asyncio.run(main())
