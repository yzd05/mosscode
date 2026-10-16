"""Capture the actual Textual compositor with a fake model; never reads API config."""

import asyncio
import json
import os
from pathlib import Path

# CI / tool shells may suppress colors; this development-only child process renders color previews.
os.environ.pop("NO_COLOR", None)

from mosscode.config import ProviderConfig, Settings
from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message
from mosscode.tui import MossCodeApp
from mosscode.tui import PromptArea


class PreviewProvider:
    async def stream(self, messages, system):
        answer = "你好，我是 **MossCode**。\n\n可以在这里进行流式对话，使用 `/help` 查看按键，使用 `/model` 切换模型。"
        yield Delta("text", answer)
        yield Completed(Message("assistant", (ContentBlock("text", answer),)), "stop")

    async def close(self):
        pass


class ThinkingPreviewProvider(PreviewProvider):
    def __init__(self):
        self.thinking_ready = asyncio.Event()
        self.text_ready = asyncio.Event()
        self.advance = asyncio.Event()

    async def stream(self, messages, system):
        if len(messages) > 1:
            async for event in super().stream(messages, system):
                yield event
            return
        thought = "这是模拟服务返回的思考片段，用于检查思考区的显示与收起。"
        answer = "你好，我是 **MossCode**。\n\n可以在这里进行流式对话。\n\n- 输入 `/help` 查看命令\n- 使用鼠标滚轮查看对话记录"
        yield Delta("thinking", thought)
        self.thinking_ready.set()
        await self.advance.wait()
        self.advance.clear()
        yield Delta("text", answer[:27])
        self.text_ready.set()
        await self.advance.wait()
        yield Delta("text", answer[27:])
        yield Completed(Message("assistant", (ContentBlock("thinking", thought), ContentBlock("text", answer))), "stop")


def capture(app, path):
    def color(value, fallback):
        return "#%02x%02x%02x" % value.get_truecolor() if value and not value.is_default else fallback

    lines = []
    for strip in app.screen._compositor.render_strips():
        lines.append([
            {"text": segment.text,
             "fg": color(segment.style.color if segment.style else None, None),
             "bg": color(segment.style.bgcolor if segment.style else None, None),
             "bold": bool(segment.style and segment.style.bold)}
            for segment in strip if not segment.control
        ])
    path.with_suffix(".json").write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    path.with_suffix(".svg").write_text(app.export_screenshot(title="MossCode · 模拟对话预览"), encoding="utf-8")


async def main():
    output = Path("docs/ui/previews")
    output.mkdir(parents=True, exist_ok=True)
    provider = ThinkingPreviewProvider()
    settings = Settings((ProviderConfig("Demo Provider", "anthropic-messages", "demo-model"),
                         ProviderConfig("Backup", "openai-chat-completions", "backup-model")), "Demo Provider")
    app = MossCodeApp(Conversation(provider), "Demo Provider", settings=settings, model="demo-model")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        capture(app, output / "startup-dark")
        app.query_one("#prompt", PromptArea).value = "/"
        await pilot.pause()
        capture(app, output / "command-hints")
        app.query_one("#prompt", PromptArea).value = "/permissions "
        await pilot.pause()
        capture(app, output / "permissions-completion")
        app.query_one("#prompt", PromptArea).value = ""
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "/model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        capture(app, output / "model-menu")
        await pilot.press("escape")
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "你好，介绍一下自己"
        await pilot.press("enter")
        await provider.thinking_ready.wait()
        await pilot.pause()
        capture(app, output / "thinking")
        provider.advance.set()
        await provider.text_ready.wait()
        await pilot.pause()
        capture(app, output / "streaming")
        provider.advance.set()
        await app._reply_task
        await pilot.pause()
        capture(app, output / "conversation-dark")
        app.theme = "moss-dark"
        await pilot.pause()
        capture(app, output / "conversation-light")
        await pilot.resize_terminal(100, 42)
        app.query_one("#prompt", PromptArea).value = "hello"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        capture(app, output / "conversation-history")
        await pilot.resize_terminal(44, 20)
        await pilot.pause()
        capture(app, output / "narrow-light")


if __name__ == "__main__":
    asyncio.run(main())
