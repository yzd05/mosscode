"""Whether the terminal has a light or a dark background, decided once before the interface is imported.

The interface colors are fixed values chosen for one kind of background, so they are picked before any module
that uses them is imported. MOSSCODE_THEME set to light or dark decides; otherwise the terminal is asked for its
background color (OSC 11), and COLORFGBG is the fallback for terminals that do not answer.
"""

from __future__ import annotations

import os
import re
import sys
import time

ENVIRONMENT = "MOSSCODE_THEME"
THEMES = ("light", "dark")
# Background color query, then Primary Device Attributes: every terminal answers the second, so its reply ends the
# wait early on a terminal that ignores the first.
QUERY = "\x1b]11;?\x1b\\\x1b[c"
_COLOR = re.compile(r"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")
_ATTRIBUTES = re.compile(r"\x1b\[\?[0-9;]*c")


def requested() -> str | None:
    value = os.environ.get(ENVIRONMENT, "").strip().lower()
    return value if value in THEMES else None


def is_light() -> bool:
    """What the interface draws for; dark unless the background was found to be light."""
    return requested() == "light"


def from_reply(reply: str) -> str | None:
    """The theme for a terminal's answer to the background query, or None when there is no answer in it."""
    match = _COLOR.search(reply)
    if not match:
        return None
    red, green, blue = (int(part, 16) / (16 ** len(part) - 1) for part in match.groups())
    return "light" if 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5 else "dark"


def from_colorfgbg(value: str | None) -> str | None:
    """COLORFGBG is "foreground;background" in the 16 ANSI colors; 7 and 9 to 15 are the light backgrounds."""
    background = (value or "").rsplit(";", 1)[-1]
    if not background.isdigit():
        return None
    number = int(background)
    return "light" if number == 7 or 9 <= number <= 15 else "dark"


def _ask_posix(timeout: float) -> str:
    import select
    import termios

    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(stdin)
    quiet = termios.tcgetattr(stdin)
    quiet[3] &= ~(termios.ICANON | termios.ECHO)  # The reply must not be echoed or wait for Enter.
    reply = ""
    try:
        termios.tcsetattr(stdin, termios.TCSANOW, quiet)
        os.write(stdout, QUERY.encode())
        deadline = time.monotonic() + timeout
        while not _ATTRIBUTES.search(reply):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([stdin], [], [], remaining)[0]:
                break
            reply += os.read(stdin, 1024).decode(errors="replace")
    finally:
        termios.tcsetattr(stdin, termios.TCSANOW, saved)
    return reply


def _ask_windows(timeout: float) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    stdin = msvcrt.get_osfhandle(sys.stdin.fileno())
    stdout = msvcrt.get_osfhandle(sys.stdout.fileno())
    input_mode, output_mode = wintypes.DWORD(), wintypes.DWORD()
    if not kernel32.GetConsoleMode(stdin, ctypes.byref(input_mode)):
        return ""
    if not kernel32.GetConsoleMode(stdout, ctypes.byref(output_mode)):
        return ""
    reply = ""
    try:
        # Virtual terminal input delivers the reply as typed characters; without line input and echo it is
        # neither shown nor held back until Enter.
        kernel32.SetConsoleMode(stdin, 0x0200)
        kernel32.SetConsoleMode(stdout, output_mode.value | 0x0004)
        os.write(sys.stdout.fileno(), QUERY.encode())
        deadline = time.monotonic() + timeout
        while not _ATTRIBUTES.search(reply) and time.monotonic() < deadline:
            if msvcrt.kbhit():
                reply += msvcrt.getwch()
            else:
                time.sleep(0.005)
    finally:
        kernel32.SetConsoleMode(stdin, input_mode.value)
        kernel32.SetConsoleMode(stdout, output_mode.value)
    return reply


def ask_terminal(timeout: float = 0.3) -> str | None:
    """The terminal's own answer, or None when it gives none in time."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        reply = _ask_windows(timeout) if sys.platform == "win32" else _ask_posix(timeout)
    except (OSError, ValueError, AttributeError, ImportError):
        return None
    return from_reply(reply)


def settle() -> str:
    """Decide the theme for this process and the ones it starts, which find it in MOSSCODE_THEME."""
    theme = requested() or ask_terminal() or from_colorfgbg(os.environ.get("COLORFGBG")) or "dark"
    os.environ[ENVIRONMENT] = theme
    return theme
