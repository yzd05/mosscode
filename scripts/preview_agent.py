"""Chapter 4 visual/console fixture: real local tools, fixed model, no API config."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from preview_tui import capture

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ToolCall, Usage
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp, PromptArea, ToolEntry


class AgentPreviewProvider:
    def __init__(self):
        self.iteration = 0

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.iteration += 1
        calls, text = (), ""
        if self.iteration == 1:
            # Plan mode offers the readers and the way out, never the writers; the rest of the set grows over time.
            names = {t.name for t in tools}
            assert {"read_file", "glob", "grep", "execute_command", "exit_plan_mode"} <= names
            assert not names & {"write_file", "edit_file"}
            text = "先查看入口文件。"
            calls = (ToolCall("plan-read", "read_file", '{"path":"demo.py"}'),)
        elif self.iteration == 2:
            text = "实施计划：\n\n1. 将问候语改为 `Hello, Agent!`。\n2. 读取文件并搜索新字符串，核对修改结果。"
        elif self.iteration == 3:
            assert {"write_file", "edit_file"} <= {t.name for t in tools}
            text = "按计划修改，并重新读取。"
            calls = (
                ToolCall(
                    "edit", "edit_file", '{"path":"demo.py","old_text":"Hello, MossCode!","new_text":"Hello, Agent!"}'
                ),
                ToolCall("read", "read_file", '{"path":"demo.py"}'),
            )
        elif self.iteration == 4:
            text = "文件读取结果正确，再搜索确认。"
            calls = (ToolCall("grep", "grep", '{"pattern":"Hello, Agent!","glob":"*.py"}'),)
        else:
            text = '**修改完成。** `demo.py` 的问候语已更新，读取与搜索结果一致。\n\n```python\ndef greet():\n    return "Hello, Agent!"\n```'
        yield Delta("text", text[:10])
        yield Delta("text", text[10:])
        yield Completed(
            Message("assistant", (ContentBlock("text", text),), tool_calls=calls),
            "tool_calls" if calls else "stop",
            Usage(40 * self.iteration, 15),
        )

    async def close(self):
        pass


async def exercise_plan(pilot):
    app = pilot.app
    await pilot.pause()
    app.query_one("#prompt", PromptArea).value = "/plan 修改问候语，先给出计划"
    await pilot.press("enter")
    await app._reply_task
    await pilot.pause()
    assert app.conversation.mode == "plan" and len(app.conversation.history) == 4


async def exercise_do(pilot):
    app = pilot.app
    app.query_one("#prompt", PromptArea).value = "/permissions bypassPermissions"
    await pilot.press("enter")
    await pilot.pause()
    assert app.conversation.mode == "normal" and not app._task_running
    app.query_one("#prompt", PromptArea).value = "按计划执行"
    await pilot.press("enter")
    await app._reply_task
    await pilot.pause()
    assert app.conversation.mode == "normal" and app._iteration == 3
    assert len(app.conversation.history) == 10 and len(app.query(ToolEntry)) == (0 if app.is_inline else 4)
    assert app.conversation.total_usage.input_tokens == 600
    assert not app.query_one("#prompt", PromptArea).disabled


async def main():
    output = Path("docs/ui/previews")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mosscode-agent-preview-") as temporary:
        root = Path(temporary)
        (root / "demo.py").write_text('def greet():\n    return "Hello, MossCode!"\n', encoding="utf-8")
        app = MossCodeApp(
            Conversation(
                AgentPreviewProvider(),
                registry=create_default_registry(root, permission_mode="bypassPermissions", load_permissions=False),
            ),
            "Demo Provider",
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await exercise_plan(pilot)
            capture(app, output / "agent-plan")
            await exercise_do(pilot)
            assert "Hello, Agent!" in (root / "demo.py").read_text()
            capture(app, output / "agent-loop")
            await pilot.resize_terminal(44, 20)
            await pilot.pause()
            capture(app, output / "agent-loop-narrow")


if __name__ == "__main__":
    asyncio.run(main())
