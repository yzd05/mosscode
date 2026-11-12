"""The light or dark palette, chosen from the terminal's background before the interface is imported."""

import os
import subprocess
import sys

import pytest

from mosscode import cli, launcher, terminal_theme


def contrast(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[at : at + 2], 16) / 255 for at in (1, 3, 5)]
        red, green, blue = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize(
    "reply,theme",
    [
        ("\x1b]11;rgb:0c0c/0c0c/0c0c\x1b\\\x1b[?61;4c", "dark"),
        ("\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?1;2c", "light"),
        ("\x1b]11;rgb:fd/f6/e3\x1b\\", "light"),  # Two hex digits per channel, as some terminals answer.
        ("\x1b]11;rgb:2/3/4\x1b\\", "dark"),
        ("\x1b[?1;2c", None),  # Only the device attributes: the terminal ignored the background query.
        ("", None),
    ],
)
def test_the_background_reply_decides_light_or_dark(reply, theme):
    assert terminal_theme.from_reply(reply) == theme


@pytest.mark.parametrize(
    "value,theme",
    [("15;0", "dark"), ("0;15", "light"), ("0;7", "light"), ("15;8", "dark"), ("0;default;15", "light"), ("", None)],
)
def test_colorfgbg_names_the_background_last(value, theme):
    assert terminal_theme.from_colorfgbg(value) == theme


def test_the_environment_wins_then_the_terminal_then_colorfgbg(monkeypatch):
    asked = []

    def ask():
        asked.append(True)
        return "light"

    monkeypatch.setattr(terminal_theme, "ask_terminal", ask)
    monkeypatch.setenv("COLORFGBG", "15;0")
    monkeypatch.setenv(terminal_theme.ENVIRONMENT, "Dark")
    assert terminal_theme.settle() == "dark" and not asked  # Set by hand: the terminal is not asked.

    monkeypatch.delenv(terminal_theme.ENVIRONMENT)
    assert terminal_theme.settle() == "light" and asked
    # Decided once for this process and inherited by the ones it starts, such as team members' windows.
    assert os.environ[terminal_theme.ENVIRONMENT] == "light" and terminal_theme.is_light()

    monkeypatch.delenv(terminal_theme.ENVIRONMENT)
    monkeypatch.setattr(terminal_theme, "ask_terminal", lambda: None)
    assert terminal_theme.settle() == "dark"  # COLORFGBG says white on black.
    monkeypatch.delenv(terminal_theme.ENVIRONMENT)
    monkeypatch.delenv("COLORFGBG")
    assert terminal_theme.settle() == "dark"


def test_a_terminal_that_is_not_interactive_is_not_asked(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert terminal_theme.ask_terminal() is None


def test_the_interactive_interface_settles_the_theme_before_it_is_imported(monkeypatch):
    seen = []
    monkeypatch.delenv(terminal_theme.ENVIRONMENT, raising=False)
    monkeypatch.setattr(launcher, "is_terminal", lambda stream: True)
    monkeypatch.setattr(terminal_theme, "ask_terminal", lambda: "light")
    monkeypatch.setattr(cli, "run", lambda args: seen.append(os.environ.get(terminal_theme.ENVIRONMENT)))
    launcher.main([])
    assert seen == ["light"]

    # Without an interface nothing is drawn, so the terminal is not asked.
    monkeypatch.delenv(terminal_theme.ENVIRONMENT)
    monkeypatch.setattr(terminal_theme, "ask_terminal", lambda: pytest.fail("asked without an interface"))
    launcher.main(["-p", "任务"])
    assert seen == ["light", None]


PALETTE = (
    "TOOL_SUCCESS_COLOR",
    "PARAMETER_COLOR",
    "OPERATION_COLOR",
    "ERROR_COLOR",
    "COMMAND_HIGHLIGHT_COLOR",
    "BRAND_COLOR",
    "MUTED_COLOR",
)


def palette(theme: str) -> dict[str, str]:
    script = (
        "from mosscode import appearance\n"
        f"print(appearance.LIGHT_BACKGROUND, appearance.BACKGROUND, *(getattr(appearance, n) for n in {PALETTE!r}),"
        " *appearance.LOGO_COLORS, appearance.USER_MESSAGE_BACKGROUND)\n"
    )
    environment = {**os.environ, terminal_theme.ENVIRONMENT: theme}
    words = subprocess.run(
        [sys.executable, "-c", script], env=environment, capture_output=True, text=True, check=True
    ).stdout.split()
    names = ("LIGHT", "BACKGROUND", *PALETTE, "LOGO_TAIL", "LOGO_MIDDLE", "LOGO_HEAD", "USER_MESSAGE_BACKGROUND")
    return dict(zip(names, words, strict=True))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_accent_and_the_grey_stay_readable_on_their_background(theme):
    colors = palette(theme)
    assert colors["LIGHT"] == str(theme == "light")
    background = "#ffffff" if theme == "light" else "#0c0c0c"
    assert colors["BACKGROUND"] == background
    for name in PALETTE:
        assert contrast(colors[name], background) >= 4.5, name
    # The comet's head is the strongest color and its tail fades into the background.
    assert contrast(colors["LOGO_HEAD"], background) > contrast(colors["LOGO_TAIL"], background)
    assert colors["USER_MESSAGE_BACKGROUND"] == "#4a4a4a"  # The grey bar under user messages is the same on both.


def test_a_slow_link_gets_longer_to_answer(monkeypatch):
    # The wait ends with the second reply; its limit only matters when a reply is slow, and a reply arriving after
    # the interface has started would be read as typed keys.
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    assert terminal_theme.wait_limit() == 1.0
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 52000 10.0.0.1 22")
    assert terminal_theme.wait_limit() == 2.0
