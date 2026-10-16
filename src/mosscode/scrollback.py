"""Render settled transcript above the live inline UI into terminal scrollback."""

from io import StringIO

from rich.console import Console
from rich.control import Control
from rich.markdown import Markdown, CodeBlock
from rich.padding import Padding
from rich.text import Text
from rich.theme import Theme
from textual.geometry import Offset

from .appearance import PARAMETER_COLOR


class PlainCodeBlock(CodeBlock):
    def __rich_console__(self, console, options):
        yield Padding(Text(str(self.text).rstrip()), (0, 2))


class TranscriptMarkdown(Markdown):
    elements = {**Markdown.elements, "fence": PlainCodeBlock, "code_block": PlainCodeBlock}


def message_row(marker, body):
    from rich.table import Table

    row = Table.grid(expand=True)
    row.add_column(width=2)
    # Rich breaks lines only at spaces; a table column would cut a long CJK run with "…".
    row.add_column(ratio=1, overflow="fold")
    row.add_row(Text(marker, style="bold"), body)
    return row


def render_transcript(renderables, width: int) -> str:
    # Rich defaults have backgrounds/dim styles; use the same foreground-only
    # appearance as the live UI, including plain code fences and blue filenames.
    styles = {name: "none" for name in (
        "markdown.code_block", "markdown.block_quote", "markdown.list",
        "markdown.item.number", "markdown.hr", "markdown.table.border",
        "markdown.table.header", "markdown.link", "markdown.link_url",
        "markdown.h1.border", "markdown.h5", "markdown.h6", "markdown.h7",
    )}
    styles.update({f"markdown.h{level}": "bold" for level in range(1, 5)})
    styles["markdown.code"] = f"bold {PARAMETER_COLOR}"
    stream = StringIO()
    console = Console(file=stream, width=max(8, width), force_terminal=True,
                      color_system="truecolor", no_color=False, legacy_windows=False,
                      highlight=False, theme=Theme(styles))
    for renderable in renderables:
        console.print(Padding(renderable, (0, 2)))
        console.print()
    return stream.getvalue().replace("\n", "\r\n")


def commit_transcript(app, renderables) -> None:
    """Move the inline origin forward; subsequent relative paints stay below it.

    Textual 8 has no public output-above-inline API. This small adapter owns the
    cursor bookkeeping already used by its inline compositor.
    """
    output = render_transcript(renderables, app.size.width)
    cursor = app._previous_cursor_position
    _write_above(app, Control.move(-cursor.x, -cursor.y).segment.text + "\r\x1b[J" + output)


def redraw_transcript(app, renderables) -> None:
    """Repaint the whole session at the current width after the terminal changed width.

    Terminals re-wrap existing rows on a width change, so the row count behind
    the relative cursor moves no longer holds and old rows keep the old wrapping.
    Clear screen and scrollback, as Pi does, then print the history again.
    """
    output = render_transcript(renderables, app.size.width) if renderables else ""
    _write_above(app, "\x1b[H\x1b[2J\x1b[3J" + output)


def _write_above(app, sequence: str) -> None:
    # Match Textual's bounded writes; Windows console writes can reject large
    # strings. Chunking is transport-only, never truncates the transcript.
    for start in range(0, len(sequence), 8192):
        app._driver.write(sequence[start:start + 8192])
    app._driver.flush()
    app._previous_cursor_position = Offset(0, 0)
    app._previous_inline_height = None
    app.screen.refresh(layout=True)
