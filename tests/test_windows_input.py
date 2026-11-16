"""Console input records → parser text (Windows inline driver)."""

import pytest
from textual._xterm_parser import XTermParser

win32 = pytest.importorskip("textual.drivers.win32")
from mosscode.windows_inline import SHIFT_ENTER, key_text  # noqa: E402

SHIFT, CTRL = 0x0010, 0x0008


def record(char="\0", down=True, vk=0, state=0):
    item = win32.INPUT_RECORD()
    item.EventType = 1
    event = item.Event.KeyEvent
    event.bKeyDown, event.wVirtualKeyCode, event.dwControlKeyState = down, vk, state
    event.uChar.UnicodeChar = char
    return item


def vt_enter(state):
    # What conhost delivers with VT input on: a bare "\r" key-down, then the real key-up.
    return [record("\r"), record("\r", down=False, vk=0x0D, state=state)]


def keys(text):
    return [event.key for event in XTermParser().feed(text) if hasattr(event, "key")]


def test_shift_enter_is_read_from_the_key_up_record_under_vt_input():
    text = key_text([record("a"), *vt_enter(SHIFT), record("b")], shift_down=lambda: False)
    assert text == "a" + SHIFT_ENTER + "b"
    assert keys(text) == ["a", "shift+enter", "b"]


def test_plain_and_ctrl_enter_still_submit():
    assert key_text(vt_enter(0), shift_down=lambda: False) == "\r"
    assert key_text(vt_enter(SHIFT | CTRL), shift_down=lambda: True) == "\r"


def test_without_a_key_up_yet_the_physical_shift_state_decides():
    assert key_text([record("\r")], shift_down=lambda: True) == SHIFT_ENTER
    assert key_text([record("\r")], shift_down=lambda: False) == "\r"
    # The next record is another key, not Enter's release.
    assert key_text([record("\r"), record("x")], shift_down=lambda: True) == SHIFT_ENTER + "x"


def test_windows_terminal_key_up_without_modifiers_falls_back_to_the_keyboard():
    # Recorded in Windows Terminal 1.24: both records of Shift+Enter report no modifiers.
    assert key_text(vt_enter(0), shift_down=lambda: True) == SHIFT_ENTER
    assert key_text(vt_enter(0), shift_down=lambda: False) == "\r"


def test_records_with_a_key_code_carry_their_own_modifiers():
    shift_enter = record("\r", vk=0x0D, state=SHIFT)
    assert key_text([shift_enter], shift_down=lambda: False) == SHIFT_ENTER
    assert key_text([record("\r", vk=0x0D)], shift_down=lambda: False) == "\r"
    assert key_text([record("\r", vk=0x0D, state=SHIFT | CTRL)], shift_down=lambda: True) == "\r"


def test_other_keys_and_escape_sequences_pass_through():
    up = [record("\x1b"), record("["), record("A"), record("\0", down=False, vk=0x26)]
    ctrl_j = [record("\n"), record("\n", down=False, vk=0x4A, state=CTRL)]
    assert keys(key_text(up + ctrl_j, shift_down=lambda: False)) == ["up", "ctrl+j"]
    # Modifier-only records (Shift pressed alone) produce no text.
    assert key_text([record("\0", vk=0, state=SHIFT)], shift_down=lambda: True) == ""
