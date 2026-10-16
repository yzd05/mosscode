"""Visual check of real tools + Textual; fixed model, temporary files, no API config."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from mosscode.tui import PromptArea

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ToolCall
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp, ToolEntry
from preview_tui import capture


class ToolPreviewProvider:
    def __init__(self):
        self.continuing = asyncio.Event()
        self.advance = asyncio.Event()

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        if messages[-1].role == "user":
            yield Completed(Message("assistant", (), tool_calls=(
                ToolCall("read-demo", "read_file", '{"path":"demo.py"}'),
                ToolCall("grep-demo", "grep", '{"pattern":"greet","glob":"*.py"}'),
            )), "tool_calls")
        else:
            self.continuing.set()
            await self.advance.wait()
            answer = "入口文件定义了 **`greet()`**，返回问候语。\n\n```python\ndef greet():\n    return \"Hello, MossCode!\"\n```\n\n- 已读取 `demo.py`\n- 已找到 `greet` 的定义"
            yield Delta("text", answer[:23])
            yield Delta("text", answer[23:])
            yield Completed(Message("assistant", (ContentBlock("text", answer),)), "stop")

    async def close(self):
        pass


async def exercise_tools(pilot):
    app = pilot.app
    await pilot.pause()
    field = app.query_one("#prompt", PromptArea)
    field.value = "看看入口文件里有什么，并搜索 greet 的定义"
    await pilot.press("enter")
    provider = app.conversation.provider
    await asyncio.wait_for(provider.continuing.wait(), 10)
    assert len(app.query(ToolEntry)) == (0 if app.is_inline else 2)
    provider.advance.set()
    await app._reply_task
    await pilot.pause()
    assert len(app.conversation.history) == 4
    assert app.focused is field and not field.disabled


async def main():
    output = Path("docs/ui/previews")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mosscode-preview-") as temporary:
        root = Path(temporary)
        (root / "demo.py").write_text('def greet():\n    return "Hello, MossCode!"\n', encoding="utf-8")
        provider = ToolPreviewProvider()
        app = MossCodeApp(Conversation(provider, registry=create_default_registry(root, permission_mode="bypassPermissions", load_permissions=False)), "Demo Provider")
        async with app.run_test(size=(100, 36)) as pilot:
            await exercise_tools(pilot)
            capture(app, output / "tool-dialogue")
            await pilot.resize_terminal(44, 20)
            await pilot.pause()
            capture(app, output / "tool-dialogue-narrow")


if __name__ == "__main__":
    asyncio.run(main())
