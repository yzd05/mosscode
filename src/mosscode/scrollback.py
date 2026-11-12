"""Render settled transcript above the live inline UI into terminal scrollback."""

from io import StringIO

from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Generic, Keyword, Number, String
from pygments.util import ClassNotFound
from rich.console import Console
from rich.control import Control
from rich.markdown import CodeBlock, Heading, HorizontalRule, Markdown, TableElement
from rich.padding import Padding
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme
from textual.geometry import Offset

from .appearance import (
    BRAND_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    OPERATION_COLOR,
    PARAMETER_COLOR,
    RULE_COLOR,
    TOOL_SUCCESS_COLOR,
)

# Code is colored with the accents the interface already has, foreground only; everything else in
# a block keeps the terminal's own color.
CODE_COLORS = (
    (Comment, MUTED_COLOR),
    (Keyword, BRAND_COLOR),
    (String, TOOL_SUCCESS_COLOR),
    (Number, OPERATION_COLOR),
    (Generic.Inserted, TOOL_SUCCESS_COLOR),
    (Generic.Deleted, ERROR_COLOR),
)


def highlighted_code(code: str, language: str) -> Text:
    """A fenced block that names its language, colored by token; any other code stays plain."""
    name = language.split()[0] if language.strip() else ""
    if not name:
        return Text(code)
    try:
        lexer = get_lexer_by_name(name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return Text(code)
    text = Text()
    for kind, value in lexer.get_tokens(code):
        text.append(value, style=next((color for token, color in CODE_COLORS if kind in token), None))
    # A lexer may normalize what it reads; the code shown must be the code written.
    return text if text.plain == code else Text(code)


class TranscriptCode(CodeBlock):
    def __rich_console__(self, console, options):
        language = "" if self.lexer_name == "text" else self.lexer_name
        yield Padding(highlighted_code(str(self.text).rstrip(), language), (0, 2))


class LeftHeading(Heading):
    LEVEL_ALIGN = {}  # Rich centers a first-level heading; the live view starts every heading at the left.


class TranscriptRule(HorizontalRule):
    # Rich follows its rule with a blank line of its own, which doubles before a table or a list.
    new_line = True

    def __rich_console__(self, console, options):
        yield Rule(style=RULE_COLOR, characters="─")


class TranscriptTable(TableElement):
    def __rich_console__(self, console, options):
        for table in super().__rich_console__(console, options):
            table.show_edge = False  # The edge of Rich's box is a blank row above and below the table.
            yield table


class TranscriptMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "fence": TranscriptCode,
        "code_block": TranscriptCode,
        "heading_open": LeftHeading,
        "hr": TranscriptRule,
        "table_open": TranscriptTable,
    }


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
    # appearance as the live UI, including blue filenames.
    styles = {
        name: "none"
        for name in (
            "markdown.code_block",
            "markdown.block_quote",
            "markdown.list",
            "markdown.item.number",
            "markdown.hr",
            "markdown.table.border",
            "markdown.table.header",
            "markdown.link",
            "markdown.link_url",
            "markdown.h1.border",
            "markdown.h5",
            "markdown.h6",
            "markdown.h7",
        )
    }
    styles.update({f"markdown.h{level}": "bold" for level in range(1, 5)})
    styles["markdown.h1"] = "bold underline"
    styles["markdown.code"] = f"bold {PARAMETER_COLOR}"
    stream = StringIO()
    console = Console(
        file=stream,
        width=max(8, width),
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        legacy_windows=False,
        highlight=False,
        theme=Theme(styles),
    )
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
        app._driver.write(sequence[start : start + 8192])
    app._driver.flush()
    app._previous_cursor_position = Offset(0, 0)
    app._previous_inline_height = None
    app.screen.refresh(layout=True)
