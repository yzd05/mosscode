"""Print the console input records this terminal delivers, under the same console modes as the app.

    python scripts/dump_keys.py

Press the keys to inspect (for example Enter, then Shift+Enter); press q to stop, or wait 20 seconds.
Each record shows: down/up, virtual key code, character, modifier state, and whether Shift is held
on the keyboard at read time. The last column is the text the app's driver passes to the parser.
"""

import sys
import time
from ctypes import byref, wintypes

if sys.platform != "win32":
    raise SystemExit("Run this check on Windows.")

from textual.drivers import win32  # noqa: E402

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from mosscode.windows_inline import key_text, physical_shift_down  # noqa: E402


def main() -> None:
    out = sys.stdout
    restore = win32.enable_application_mode()
    # Same requests as the app: kitty keyboard protocol and bracketed paste.
    out.write("\x1b[>1u\x1b[?2004h")
    out.write("按要检查的键（例如先按 Enter，再按 Shift+Enter），按 q 结束。\r\n")
    out.flush()
    handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
    records = (win32.INPUT_RECORD * 64)()
    count = wintypes.DWORD()
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if win32.wait_for_handles([handle], 100) is None:
                continue
            win32.KERNEL32.ReadConsoleInputW(handle, byref(records), 64, byref(count))
            batch = records[: count.value]
            shift = physical_shift_down()
            for record in batch:
                if record.EventType != 1:
                    continue
                event = record.Event.KeyEvent
                out.write(
                    f"{'down' if event.bKeyDown else 'up  '}  vk=0x{event.wVirtualKeyCode:02X}"
                    f"  char={event.uChar.UnicodeChar!r:6}  state=0x{event.dwControlKeyState:04X}"
                    f"  shift_held={shift}\r\n"
                )
            text = key_text(batch)
            if text:
                out.write(f"  -> {text!r}\r\n")
            out.flush()
            if text == "q":
                break
    finally:
        out.write("\x1b[<u\x1b[?2004l")
        out.flush()
        restore()


if __name__ == "__main__":
    main()
