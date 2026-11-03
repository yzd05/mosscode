import asyncio
import io

from PIL import Image
from rich.console import Console
from textual import events
from textual.widgets import Static

from mosscode import tui
from mosscode.clipboard import ClipboardError
from mosscode.config import ProviderConfig
from mosscode.conversation import Conversation
from mosscode.images import ImageRef
from mosscode.messages import Completed, ContentBlock, Message
from mosscode.tui import ChatEntry, MossCodeApp, PromptArea, resumed_rows
from test_conversation import FakeProvider


def png(color: str, size=(4, 3)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


class FakeClipboard:
    """Stands in for read_clipboard_async: hands out the queued contents in order."""

    def __init__(self, *contents):
        self.contents, self.reads = list(contents), 0

    async def __call__(self):
        self.reads += 1
        content = self.contents.pop(0)
        if isinstance(content, Exception):
            raise content
        return content


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def paste_image(app, pilot):
    await pilot.press("alt+v")
    await settle(app, pilot)


async def send(app, pilot, text=None):
    field = app.query_one("#prompt", PromptArea)
    if text is not None:
        field.value = text
    await pilot.press("enter")
    await pilot.pause()
    await asyncio.wait_for(app._reply_task, timeout=5)


def last_user(provider) -> Message:
    return [message for message in provider.requests[-1] if message.role == "user"][-1]


def layout(message: Message) -> list[str]:
    return [
        block.text if block.kind == "text" else f"<{block.image.width}x{block.image.height}>"
        for block in message.content
    ]


def status(app) -> str:
    return str(app.query_one("#status", Static).render())


async def test_alt_v_inserts_numbered_placeholders_and_sends_each_image_after_its_own(monkeypatch):
    monkeypatch.setattr(tui, "read_clipboard_async", FakeClipboard(png("red", (4, 3)), png("blue", (5, 2))))
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "比较 "
        await paste_image(app, pilot)
        field.insert("和 ")
        await paste_image(app, pilot)
        assert field.text == "比较 [图片 #1] 和 [图片 #2] "
        panel = str(app.query_one("#attachments", Static).render())
        assert "[图片 #1] 剪贴板 · 4×3" in panel and "[图片 #2] 剪贴板 · 5×2" in panel

        await send(app, pilot)
        assert layout(last_user(provider)) == ["比较 [图片 #1]", "<4x3>", " 和 [图片 #2]", "<5x2>"]
        assert app.query(ChatEntry)[-2].notes == (
            f"[图片 #1] 剪贴板 · 4×3 · {len(png('red', (4, 3)))} B",
            f"[图片 #2] 剪贴板 · 5×2 · {len(png('blue', (5, 2)))} B",
        )
        assert not app.query_one("#attachments", Static).display


async def test_numbers_continue_after_sending_and_a_deleted_placeholder_drops_its_image(monkeypatch):
    clipboard = FakeClipboard(png("red"), png("green"), png("blue"))
    monkeypatch.setattr(tui, "read_clipboard_async", clipboard)
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        await paste_image(app, pilot)
        await send(app, pilot)
        await paste_image(app, pilot)
        await paste_image(app, pilot)
        assert field.text == "[图片 #2] [图片 #3] "
        field.value = "只要 [图片 #3]"
        await send(app, pilot)
        assert layout(last_user(provider)) == ["只要 [图片 #3]", "<4x3>"]
        sent = last_user(provider).content[1].image
        assert sent.data == png("blue")


class GatedProvider(FakeProvider):
    """Answers at once, or once release is set again after a test clears it."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.release.set()

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append(tuple(messages))
        self.started.set()
        await self.release.wait()
        yield Completed(Message("assistant", (ContentBlock("text", "ok"),)), "stop")


async def test_recalled_history_and_queued_messages_keep_their_images(monkeypatch):
    monkeypatch.setattr(tui, "read_clipboard_async", FakeClipboard(png("red"), png("blue")))
    provider = GatedProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        await paste_image(app, pilot)
        await send(app, pilot)
        await pilot.press("up")
        assert app.query_one("#prompt", PromptArea).text == "[图片 #1] "
        await send(app, pilot)
        assert layout(last_user(provider))[1] == "<4x3>"

        provider.release.clear()
        await send_without_waiting(app, pilot, "开始")
        await provider.started.wait()
        await paste_image(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert app.conversation.queued == ("[图片 #2] ",)
        provider.release.set()
        await asyncio.wait_for(app._reply_task, timeout=5)
        # The model finished before the queue could join, so it went out as the next request.
        await pilot.pause()
        await asyncio.wait_for(app._reply_task, timeout=5)
        steered = [m for m in app.conversation.history if m.role == "user"][-1]
        assert layout(steered) == ["[图片 #2]", "<4x3>"]
        assert app.query(ChatEntry)[-2].notes[0].startswith("[图片 #2] 剪贴板")


async def send_without_waiting(app, pilot, text):
    field = app.query_one("#prompt", PromptArea)
    field.value = text
    await pilot.press("enter")
    await pilot.pause()


async def test_pasted_image_paths_become_placeholders_but_other_pastes_stay_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot one.png").write_bytes(png("red", (6, 4)))
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        app.post_message(events.Paste(f'"{tmp_path / "shot one.png"}"'))
        await settle(app, pilot)
        assert field.text == "[图片 #1] "
        assert "[图片 #1] shot one.png · 6×4" in str(app.query_one("#attachments", Static).render())
        field.value = ""
        app.post_message(events.Paste("notes.txt"))
        await settle(app, pilot)
        assert field.text == "notes.txt"
        # A path typed by hand is just text.
        await send(app, pilot, "shot.png")
        assert layout(last_user(provider)) == ["shot.png"]


async def test_empty_or_failing_clipboard_says_why_and_ctrl_v_falls_back_to_text(monkeypatch):
    clipboard = FakeClipboard(None, ClipboardError("读取剪贴板超时（5 秒）。"), None)
    monkeypatch.setattr(tui, "read_clipboard_async", clipboard)
    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        await paste_image(app, pilot)
        assert "剪贴板中没有图片" in status(app)
        await paste_image(app, pilot)
        assert "无法粘贴图片：读取剪贴板超时" in status(app)
        app.copy_to_clipboard("copied text")
        await pilot.press("ctrl+v")
        await settle(app, pilot)
        assert field.text == "copied text" and clipboard.reads == 3


async def test_a_model_without_images_gets_a_hint_and_no_clipboard_read(tmp_path, monkeypatch):
    clipboard = FakeClipboard(png("red"))
    monkeypatch.setattr(tui, "read_clipboard_async", clipboard)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.png").write_bytes(png("red"))
    provider = FakeProvider()
    provider.config = ProviderConfig("text-only", "openai-chat-completions", "m")
    app = MossCodeApp(Conversation(provider), "text-only")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        await paste_image(app, pilot)
        assert "当前模型不支持图片（providers.text-only.images）" in status(app)
        assert field.text == "" and clipboard.reads == 0
        app.post_message(events.Paste("a.png"))
        await settle(app, pilot)
        assert field.text == "a.png" and not field.attachments


async def test_status_reports_whether_the_model_takes_images():
    provider = FakeProvider()
    provider.config = ProviderConfig("text-only", "openai-chat-completions", "m")
    app = MossCodeApp(Conversation(provider), "text-only")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        assert "当前模型不支持（providers.text-only.images: false）" in str(app.query(".local-note")[-1].render())


def test_resumed_user_messages_list_their_images():
    ref = ImageRef("a" * 32 + ".png", "image/png", 40, 30, 2048, "[图片 #1]", data=b"x")
    message = Message("user", (ContentBlock("text", "看 [图片 #1]"), ContentBlock("image", "[图片 #1]", ref)))
    console = Console(width=60, record=True)
    console.print(resumed_rows([message])[0])
    assert "└ [图片 #1] · 40×30 · 2 KB" in console.export_text()
