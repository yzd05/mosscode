"""Inspect the real approval panel with a fake provider and temporary project."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from textual.widgets import OptionList
from mosscode.tui import PromptArea

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ToolCall
from mosscode.permissions import PermissionPolicy
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp
from preview_tui import capture


class PermissionPreviewProvider:
    async def stream(self, messages, system, tools=(), **kwargs):
        if messages[-1].role == "user":
            call = ToolCall("permission-demo", "write_file", json.dumps({"path": "hello.txt", "content": "Hello World\n"}))
            yield Completed(Message("assistant", (), tool_calls=(call,)), "tool_calls")
        else:
            assert not messages[-1].tool_results[0].is_error
            text = "已创建 **`hello.txt`**，内容为 `Hello World`。"
            yield Delta("text", text)
            yield Completed(Message("assistant", (ContentBlock("text", text),)), "stop")

    async def close(self):
        pass


def approval_app(root):
    policy = PermissionPolicy(root, load=False)
    registry = create_default_registry(root, permissions=policy)
    return MossCodeApp(Conversation(PermissionPreviewProvider(), registry=registry), "Demo Provider")


async def exercise_approval(pilot, snapshot=None):
    app = pilot.app
    await pilot.pause()
    app.query_one("#prompt", PromptArea).value = "创建 hello.txt，写入 Hello World"
    await pilot.press("enter")
    for _ in range(100):
        await pilot.pause(.02)
        if app._approval_request:
            break
    assert app._approval_request
    policy = app.conversation.registry.permissions
    assert not (policy.root / "hello.txt").exists()
    assert app.focused is app.query_one("#approval-options", OptionList)
    assert app.query_one("#approval-panel").region.y >= app.query_one("#input-row").region.bottom
    if snapshot:
        capture(app, snapshot)
    await pilot.press("enter")
    await asyncio.wait_for(app._reply_task, 10)
    await pilot.pause()
    assert (policy.root / "hello.txt").read_text() == "Hello World\n"
    assert not policy.pending and not policy.local_file.exists()
    assert not app.query_one("#approval-panel").display


async def main():
    output = Path("docs/ui/previews")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mosscode-approval-") as temporary:
        app = approval_app(Path(temporary))
        async with app.run_test(size=(100, 32)) as pilot:
            await exercise_approval(pilot, output / "permission-approval")


if __name__ == "__main__":
    asyncio.run(main())
