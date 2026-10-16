"""Use Textual's inline compositor with its existing Windows input driver.

Textual 8 does not ship a Windows inline driver. Keep its console input and mode
restoration, but stay in the main buffer and consume cursor-position reports.
"""

import asyncio
import ctypes
from ctypes import byref, wintypes

from textual import constants, events
from textual._xterm_parser import XTermParser
from textual.drivers import win32
from textual.drivers.windows_driver import WindowsDriver
from textual.drivers._writer_thread import WriterThread

KEY_EVENT, WINDOW_BUFFER_SIZE_EVENT = 0x0001, 0x0004
VK_RETURN, VK_SHIFT = 0x0D, 0x10
SHIFT_PRESSED = 0x0010
CTRL_OR_ALT = 0x000F  # Right/left Alt and Ctrl.
# Kitty keyboard protocol encoding, which Textual's parser reads as "shift+enter".
SHIFT_ENTER = "\x1b[13;2u"


def physical_shift_down() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)


def _shift_enter(records, index: int, shift_down) -> bool:
    event = records[index].Event.KeyEvent
    state = event.dwControlKeyState
    if event.wVirtualKeyCode != VK_RETURN:
        # With VT input enabled the console replaces Enter's key-down record by a
        # bare "\r" without key code or modifiers, so Shift+Enter would submit.
        # conhost keeps the modifiers on the key-up record when it follows in the
        # same batch; Windows Terminal leaves them out there too.
        following = records[index + 1] if index + 1 < len(records) else None
        if (following is not None and following.EventType == KEY_EVENT
                and not following.Event.KeyEvent.bKeyDown and following.Event.KeyEvent.wVirtualKeyCode == VK_RETURN):
            state = following.Event.KeyEvent.dwControlKeyState
    # A record may omit Shift; the keyboard state read right after the key press
    # covers that, so either source is enough.
    return (bool(state & SHIFT_PRESSED) or shift_down()) and not state & CTRL_OR_ALT


def key_text(records, shift_down=physical_shift_down) -> str:
    """Text for the parser from one batch of console input records."""
    keys = []
    for index, record in enumerate(records):
        if record.EventType != KEY_EVENT:
            continue
        event = record.Event.KeyEvent
        if not event.bKeyDown or (event.dwControlKeyState and event.wVirtualKeyCode == 0):
            continue
        key = event.uChar.UnicodeChar
        keys.append(SHIFT_ENTER if key == "\r" and _shift_enter(records, index, shift_down) else key)
    # Textual issue 3178: join surrogate pairs typed as two records.
    return "".join(keys).encode("utf-16", "surrogatepass").decode("utf-16")


class EventMonitor(win32.EventMonitor):
    """Textual's console input thread with Shift+Enter kept apart from Enter."""

    def run(self) -> None:
        exit_requested = self.exit_event.is_set
        parser = XTermParser(debug=constants.DEBUG)
        try:
            read_count = wintypes.DWORD(0)
            handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
            input_records = (win32.INPUT_RECORD * 1024)()
            while not exit_requested():
                for event in parser.tick():
                    self.process_event(event)
                if win32.wait_for_handles([handle], 100) is None:
                    continue
                win32.KERNEL32.ReadConsoleInputW(handle, byref(input_records), 1024, byref(read_count))
                records = input_records[: read_count.value]
                text = key_text(records)
                new_size = None
                for record in records:
                    if record.EventType == WINDOW_BUFFER_SIZE_EVENT:
                        size = record.Event.WindowBufferSizeEvent.dwSize
                        new_size = (size.X, size.Y)
                if text:
                    for event in parser.feed(text):
                        self.process_event(event)
                if new_size is not None:
                    self.on_size_change(*new_size)
        except Exception as error:
            self.app.log.error("EVENT MONITOR ERROR", error)


class WindowsInlineDriver(WindowsDriver):
    @property
    def is_inline(self) -> bool:
        return True

    def start_application_mode(self) -> None:
        # Same sequence as WindowsDriver, with the input thread replaced.
        loop = asyncio.get_running_loop()
        self._restore_console = win32.enable_application_mode()
        self._writer_thread = WriterThread(self._file)
        self._writer_thread.start()
        self.write("\x1b[?1049h")
        self._enable_mouse_support()
        self.write("\x1b[?25l")
        self.write("\033[?1004h")
        self.write("\x1b[>1u")
        self.flush()
        self._enable_bracketed_paste()
        self._event_thread = EventMonitor(loop, self._app, self.exit_event, self.process_message)
        self._event_thread.start()

    def write(self, data: str) -> None:
        # WindowsDriver emits these only at startup / shutdown. Do not enter or
        # leave the alternate screen: inline rendering uses relative cursor moves.
        data = data.replace("\x1b[?1049h", "\r\n" * self._app.INLINE_PADDING)
        # On exit Textual has already placed the cursor after retained output,
        # or at the top of a temporary picker. Clear only below that cursor.
        data = data.replace("\x1b[?1049l", "\x1b[J")
        if data:
            # Textual prints the retained screen directly on inline exit. Keep
            # cursor updates synchronous so queued writes cannot race that print.
            self._file.write(data)

    def flush(self) -> None:
        self._file.flush()

    def _enable_mouse_support(self) -> None:
        # Inline transcript uses the terminal's scrollback, even if a caller
        # supplied mouse=True or an interrupted older app left tracking enabled.
        self.write("\x1b[?1000l\x1b[?1003l\x1b[?1015l\x1b[?1006l")
        self.flush()

    def process_message(self, message) -> None:
        if isinstance(message, events.MouseEvent):
            # This interface is keyboard-operated: mouse clicks cannot execute
            # commands or shift focus away from the prompt. Native wheel input
            # belongs to the terminal; ignore any stale mouse reports.
            return
        elif isinstance(message, events.CursorPosition):
            self.cursor_origin = (message.x, message.y)
        else:
            super().process_message(message)
