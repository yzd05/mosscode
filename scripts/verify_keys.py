"""Windows console key check: type into the app through the console input queue, as a keyboard does.

    python scripts/verify_keys.py                  expect every check to pass
    python scripts/verify_keys.py --textual-input  the same keys through Textual's own input thread (shows the bug)

Pilot tests hand keys straight to the app; this script writes console input records
(WriteConsoleInputW) so they pass through the console's VT translation and the driver.
The child window uses a fake model and no API configuration; it closes by itself.
"""

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
sys.path.insert(0, str(HERE.parent))

k32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
VK_RETURN, VK_UP, VK_DOWN, VK_J = 0x0D, 0x26, 0x28, 0x4A
SHIFT, LEFT_CTRL = 0x0010, 0x0008


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR),
                ("dwControlKeyState", wintypes.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _Event(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]
    _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]


def key(vk, char="\0", state=0):
    """Key-down and key-up records, as the console receives them from the keyboard."""
    records = []
    for down in (True, False):
        record = INPUT_RECORD()
        record.EventType = 1
        record.Event.KeyEvent = KEY_EVENT_RECORD(down, 1, vk, user32.MapVirtualKeyW(vk, 0), char, state)
        records.append(record)
    return records


def text(value):
    records = []
    for char in value:
        scan = user32.VkKeyScanW(char)
        records += key(scan & 0xFF, char, SHIFT if scan & 0x100 else 0)
    return records


def inject(records):
    array = (INPUT_RECORD * len(records))(*records)
    written = wintypes.DWORD()
    k32.WriteConsoleInputW(k32.GetStdHandle(-10), array, len(records), ctypes.byref(written))


def child(out_path, textual_input):
    from mosscode import windows_inline
    from mosscode.conversation import Conversation
    from mosscode.tui import MossCodeApp, PromptArea
    from test_agent import SequenceProvider
    from verify_resize import handle, screen_rows

    if textual_input:
        from textual.drivers import win32
        windows_inline.EventMonitor = win32.EventMonitor

    h = handle()
    print("SHELL-LINE-BEFORE-APP", flush=True)
    time.sleep(0.3)
    k32.FlushConsoleInputBuffer(k32.GetStdHandle(-10))
    results = {}

    async def pilot_script(pilot):
        app = pilot.app
        field = app.query_one("#prompt", PromptArea)

        async def typed(records):
            field.value = ""
            inject(records)
            await pilot.pause(0.6)
            return field.value

        async def submit(value):
            # Type, then press Enter a moment later as a person would: the command hints follow the text.
            await typed(text(value))
            inject(key(VK_RETURN, "\r"))
            await pilot.pause(0.6)

        await pilot.pause(0.5)
        results["shift_enter"] = await typed(text("a") + key(VK_RETURN, "\r", SHIFT) + text("b"))
        results["ctrl_j"] = await typed(text("c") + key(VK_J, "\n", LEFT_CTRL) + text("d"))
        results["backslash_enter"] = await typed(text("e\\") + key(VK_RETURN, "\r") + text("f"))
        results["nothing_sent"] = field.input_history == []
        await submit("/help")
        await submit("/permissions")
        results["enter_sends"] = list(field.input_history)
        steps = [await typed(text("abc"))]
        for vk in (VK_UP, VK_UP, VK_DOWN, VK_DOWN):
            inject(key(vk))
            await pilot.pause(0.4)
            steps.append(field.value)
        results["history"] = steps
        await submit("/clear")
        screen = "\n".join(screen_rows(h))
        results["clear"] = {"shell_line": screen.count("SHELL-LINE-BEFORE-APP"), "permissions": screen.count("/permissions"),
                            "banner": screen.count("MossCode v"), "note": screen.count("已开始新会话")}
        results["screen"] = screen.splitlines()
        await pilot.press("ctrl+q")

    app = MossCodeApp(Conversation(SequenceProvider()), "Demo", model="keys-model")
    app.driver_class = windows_inline.WindowsInlineDriver
    app.run(inline=True, inline_no_clear=True, mouse=False, auto_pilot=pilot_script)
    Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


EXPECTED = {
    "shift_enter": "a\nb",
    "ctrl_j": "c\nd",
    "backslash_enter": "e\nf",
    "nothing_sent": True,
    "enter_sends": ["/help", "/permissions"],
    "history": ["abc", "/permissions", "/help", "/permissions", "abc"],
    "clear": {"shell_line": 0, "permissions": 0, "banner": 1, "note": 1},
}


def parent(textual_input):
    out = REPO / ".test-artifacts" / ("verify_keys_textual.json" if textual_input else "verify_keys.json")
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    args = ["conhost.exe", sys.executable, str(HERE), "--child", str(out)] + (["--textual-input"] if textual_input else [])
    process = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=REPO)
    deadline = time.monotonic() + 60
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    process.wait(timeout=10)
    if not out.exists():
        raise SystemExit("child produced no report")
    results = json.loads(out.read_text(encoding="utf-8"))
    failed = False
    for name, expected in EXPECTED.items():
        ok = results[name] == expected
        failed |= not ok
        print(name, json.dumps(results[name], ensure_ascii=False), "passed" if ok else "FAILED")
    print("screen rows:", out)
    return 1 if failed else 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        child(sys.argv[sys.argv.index("--child") + 1], "--textual-input" in sys.argv)
    else:
        if sys.platform != "win32":
            raise SystemExit("Run this check on Windows.")
        sys.exit(parent("--textual-input" in sys.argv))
