"""Windows console resize check: run the app in a new conhost window, change its width, read the screen back.

    python scripts/verify_resize.py               expect every check to pass
    python scripts/verify_resize.py --no-redraw   the same run without the width-change repaint (shows the bug)

The child window uses a fake model and no API configuration; it closes by itself.
"""

import asyncio
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests"))

k32 = ctypes.windll.kernel32


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


def handle():
    return k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)


def info(h):
    c = CSBI()
    k32.GetConsoleScreenBufferInfo(h, ctypes.byref(c))
    return c


def set_width(h, cols):
    c = info(h)
    old = c.srWindow.Right - c.srWindow.Left + 1
    if cols < old:
        k32.SetConsoleWindowInfo(h, False, ctypes.byref(SMALL_RECT(0, 0, cols - old, 0)))
        k32.SetConsoleScreenBufferSize(h, COORD(cols, c.dwSize.Y))
    else:
        k32.SetConsoleScreenBufferSize(h, COORD(cols, c.dwSize.Y))
        k32.SetConsoleWindowInfo(h, False, ctypes.byref(SMALL_RECT(0, 0, cols - old, 0)))


def screen_rows(h):
    c = info(h)
    rows = []
    width = c.dwSize.X
    for y in range(0, c.dwCursorPosition.Y + 40):
        buffer = ctypes.create_unicode_buffer(width)
        read = wintypes.DWORD()
        k32.ReadConsoleOutputCharacterW(h, buffer, width, COORD(0, y), ctypes.byref(read))
        rows.append(buffer.value[:read.value].rstrip())
    while rows and not rows[-1]:
        rows.pop()
    return rows


def report(rows, label):
    text = "\n".join(rows)
    # Rows are joined without spaces so a paragraph wrapped across rows still counts as complete.
    flat = "".join(text.split())
    return {"label": label, "question": text.count("RESIZE-QUESTION"),
            "answers": sum(text.count(f"RESIZE-ANSWER-{i:02d}") for i in range(8)),
            "complete_paragraphs": flat.count("是否正确的中文文本。"),
            "status_bars": text.count("resize-model"), "rows": rows}


def child(out_path, redraw):
    from mosscode.conversation import Conversation
    from mosscode.tui import MossCodeApp, PromptArea
    from mosscode.windows_inline import WindowsInlineDriver
    from test_agent import SequenceProvider, response

    if not redraw:
        async def skip(self):
            return None
        MossCodeApp._redraw_after_resize = skip

    h = handle()
    set_width(h, 100)
    print("SHELL-LINE-BEFORE-APP", flush=True)
    # Drop the size event of the width set above: the app should start in a settled window.
    time.sleep(0.3)
    k32.FlushConsoleInputBuffer(k32.GetStdHandle(-10))
    results = []

    async def pilot_script(pilot):
        app = pilot.app
        await pilot.pause()
        app.query_one("#prompt", PromptArea).value = "RESIZE-QUESTION"
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 10)
        await pilot.pause(0.5)
        results.append(report(screen_rows(h), "before"))
        for cols in (80, 64, 56):
            set_width(h, cols)
            await asyncio.sleep(0.05)
        await pilot.pause(1.0)
        results.append(report(screen_rows(h), "narrow"))
        set_width(h, 96)
        await pilot.pause(1.0)
        results.append(report(screen_rows(h), "wide"))
        await pilot.press("ctrl+q")

    answer = "\n\n".join(f"RESIZE-ANSWER-{i:02d} 这是一段用于检查窗口宽度变化后的换行与重绘是否正确的中文文本。" for i in range(8))
    app = MossCodeApp(Conversation(SequenceProvider(response(answer))), "Demo", model="resize-model")
    app.driver_class = WindowsInlineDriver
    app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=pilot_script)
    Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def parent(redraw):
    out = REPO / ".test-artifacts" / ("verify_resize.json" if redraw else "verify_resize_no_redraw.json")
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    args = ["conhost.exe", sys.executable, str(HERE), "--child", str(out)] + ([] if redraw else ["--no-redraw"])
    process = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=REPO)
    deadline = time.monotonic() + 60
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    process.wait(timeout=10)
    if not out.exists():
        raise SystemExit("child produced no report")
    expected = {"question": 1, "answers": 8, "complete_paragraphs": 8, "status_bars": 1}
    failed = False
    for item in json.loads(out.read_text(encoding="utf-8")):
        counts = {key: item[key] for key in expected}
        print(item["label"], counts, "passed" if counts == expected else "FAILED")
        failed |= counts != expected
    print("screen rows:", out)
    return 1 if failed else 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        child(sys.argv[sys.argv.index("--child") + 1], "--no-redraw" not in sys.argv)
    else:
        if sys.platform != "win32":
            raise SystemExit("Run this check on Windows.")
        sys.exit(parent("--no-redraw" not in sys.argv))
