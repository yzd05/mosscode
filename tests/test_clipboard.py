import io
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from mosscode import clipboard
from mosscode.clipboard import ClipboardError, pasted_image_paths, read_clipboard, read_clipboard_async


def save_png(path, size=(3, 2)):
    Image.new("RGB", size, "red").save(path, "PNG")
    return path


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_grabbed_images_become_png_and_copied_files_become_paths(monkeypatch, platform, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", platform)
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: Image.new("RGB", (4, 3), "blue"))
    data = read_clipboard()
    assert data.startswith(b"\x89PNG") and Image.open(io.BytesIO(data)).size == (4, 3)
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: [str(tmp_path / "a.png")])
    assert read_clipboard() == [tmp_path / "a.png"]
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: None)
    assert read_clipboard() is None

    def broken():
        raise OSError("clipboard busy")

    monkeypatch.setattr(ImageGrab, "grabclipboard", broken)
    with pytest.raises(ClipboardError, match="clipboard busy"):
        read_clipboard()


class FakeRun:
    """subprocess.run for wl-paste/xclip: outputs by the requested type, records the commands."""

    def __init__(self, types, contents, *, fail=False, slow=False):
        self.types, self.contents, self.fail, self.slow, self.commands = types, contents, fail, slow, []

    def __call__(self, command, **options):
        self.commands.append(command)
        if self.slow:
            raise subprocess.TimeoutExpired(command, options.get("timeout"))
        if self.fail:
            return SimpleNamespace(returncode=1, stdout=b"")
        if "--list-types" in command or "TARGETS" in command:
            return SimpleNamespace(returncode=0, stdout="\n".join(self.types).encode())
        kind = command[command.index("--type") + 1] if "--type" in command else command[command.index("-t") + 1]
        return SimpleNamespace(returncode=0, stdout=self.contents[kind])


def linux(monkeypatch, tools, wayland=True):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
    if wayland:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    else:
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def test_linux_prefers_wl_paste_under_wayland_and_asks_for_png_first(monkeypatch):
    linux(monkeypatch, {"wl-paste", "xclip"})
    run = FakeRun(["text/plain", "image/jpeg", "image/png"], {"image/png": b"PNG-BYTES"})
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert read_clipboard() == b"PNG-BYTES"
    assert run.commands[0][0] == "wl-paste" and run.commands[1][-1] == "image/png"


def test_linux_uses_xclip_without_wayland_and_reads_copied_files(monkeypatch):
    linux(monkeypatch, {"wl-paste", "xclip"}, wayland=False)
    run = FakeRun(["text/uri-list"], {"text/uri-list": b"file:///home/me/a%20b.png\r\nfile:///home/me/c.png\r\n"})
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert [path.as_posix() for path in read_clipboard()] == ["/home/me/a b.png", "/home/me/c.png"]
    assert run.commands[0][0] == "xclip"


def test_linux_empty_clipboard_missing_tool_and_timeout(monkeypatch):
    linux(monkeypatch, {"xclip"}, wayland=False)
    monkeypatch.setattr(clipboard.subprocess, "run", FakeRun([], {}, fail=True))
    assert read_clipboard() is None
    monkeypatch.setattr(clipboard.subprocess, "run", FakeRun(["text/plain"], {}))
    assert read_clipboard() is None
    monkeypatch.setattr(clipboard.subprocess, "run", FakeRun([], {}, slow=True))
    with pytest.raises(ClipboardError, match="超时"):
        read_clipboard()
    linux(monkeypatch, set())
    with pytest.raises(ClipboardError, match="wl-paste（Wayland）或 xclip"):
        read_clipboard()


async def test_a_stuck_clipboard_is_given_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(clipboard, "read_clipboard", lambda: time.sleep(0.5))
    with pytest.raises(ClipboardError, match="超时"):
        await read_clipboard_async(timeout=0.05)


def test_pasted_text_of_only_image_paths_becomes_paths(tmp_path):
    spaced = save_png(tmp_path / "my shot.png")
    plain = save_png(tmp_path / "plain.png")
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    assert pasted_image_paths(f'"{spaced}"', tmp_path) == [spaced]
    assert pasted_image_paths(f"'{spaced}'  {plain}\n", tmp_path) == [spaced, plain]
    assert pasted_image_paths("plain.png", tmp_path) == [plain]
    assert pasted_image_paths(plain.as_uri(), tmp_path) == [plain]
    for text in ("", "   ", f"看看 {plain}", f"{plain} notes.txt", "missing.png", f'"{tmp_path}"'):
        assert pasted_image_paths(text, tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="Backslash is a path separator on Windows")
def test_escaped_spaces_from_a_macos_drag(tmp_path):
    spaced = save_png(tmp_path / "my shot.png")
    assert pasted_image_paths(str(spaced).replace(" ", "\\ "), tmp_path) == [spaced]
