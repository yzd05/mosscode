"""An image, or copied image files, from the system clipboard; image paths in pasted text."""

import asyncio
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .images import sniff

TIMEOUT = 5.0
# Asked for in this order: lossless first, BMP last because it is converted anyway.
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp")
MISSING_TOOL = "读取剪贴板中的图片需要 wl-paste（Wayland）或 xclip（X11），请先安装。"
# A quoted path, or a bare one where "\ " is an escaped space (macOS Terminal drags files in that way).
TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|((?:\\ |\S)+)')


class ClipboardError(Exception):
    pass


Clipboard = bytes | list[Path] | None


def read_clipboard() -> Clipboard:
    """Image bytes, copied files (Explorer, Finder), or None when the clipboard holds neither. Blocking."""
    if sys.platform in ("win32", "darwin"):
        return _grab()
    return _linux()


async def read_clipboard_async(timeout: float = TIMEOUT) -> Clipboard:
    """In a worker thread so the interface keeps running; a stuck clipboard owner costs at most timeout."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(read_clipboard), timeout)
    except TimeoutError:
        raise ClipboardError(f"读取剪贴板超时（{timeout:g} 秒）。") from None


def _grab() -> Clipboard:
    from PIL import ImageGrab

    try:
        content = ImageGrab.grabclipboard()
    except (OSError, ValueError, NotImplementedError) as exc:
        raise ClipboardError(f"无法读取剪贴板：{exc}") from None
    if content is None:
        return None
    if isinstance(content, list):
        return [Path(name) for name in content] or None
    buffer = io.BytesIO()
    content.save(buffer, "PNG")
    return buffer.getvalue()


def _linux() -> Clipboard:
    # Pillow runs these same programs on Linux; calling them here gives a clearer error when neither exists.
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        types = _run(["wl-paste", "--list-types"])

        def fetch(kind: str) -> bytes:
            return _run(["wl-paste", "--no-newline", "--type", kind])

    elif shutil.which("xclip"):
        types = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])

        def fetch(kind: str) -> bytes:
            return _run(["xclip", "-selection", "clipboard", "-t", kind, "-o"])

    else:
        raise ClipboardError(MISSING_TOOL)
    offered = types.decode("utf-8", "replace").split()
    for kind in IMAGE_TYPES:
        if kind in offered:
            return fetch(kind) or None
    if "text/uri-list" in offered:
        return _uri_files(fetch("text/uri-list").decode("utf-8", "replace")) or None
    return None


def _run(command: list[str]) -> bytes:
    """stdout; empty when the program fails, which is how both tools report an empty clipboard."""
    try:
        done = subprocess.run(command, capture_output=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise ClipboardError(f"读取剪贴板超时（{TIMEOUT:g} 秒）。") from None
    except OSError as exc:
        raise ClipboardError(f"无法运行 {command[0]}：{exc}") from None
    return done.stdout if done.returncode == 0 else b""


def _uri_files(text: str) -> list[Path]:
    files = []
    for line in text.splitlines():
        parts = urlsplit(line.strip())
        if parts.scheme == "file":
            path = unquote(parts.path)
            if re.match(r"/[A-Za-z]:", path):  # file:///C:/x on Windows
                path = path[1:]
            files.append(Path(path))
    return files


def is_image_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return path.is_file() and sniff(stream.read(32)) is not None
    except OSError:
        return False


def pasted_image_paths(text: str, root: Path) -> list[Path] | None:
    """The image files when the pasted text is nothing but their paths (a drag into the terminal), else None."""
    tokens = [next(group for group in match.groups() if group is not None) for match in TOKEN.finditer(text)]
    if not tokens:
        return None
    paths = []
    for token in tokens:
        if token.startswith("file://"):
            files = _uri_files(token)
            if not files:
                return None
            path = files[0]
        else:
            if sys.platform != "win32":
                token = token.replace("\\ ", " ")
            path = Path(token).expanduser()
        path = path if path.is_absolute() else root / path
        if not is_image_file(path):
            return None
        paths.append(path)
    return paths
