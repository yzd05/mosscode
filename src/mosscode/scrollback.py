"""Render settled transcript above the live inline UI into terminal scrollback."""

from io import StringIO
from time import perf_counter

from markdown_it import MarkdownIt
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Generic, Keyword, Number, String
from pygments.util import ClassNotFound
from rich.console import Console
from rich.control import Control
from rich.markdown import CodeBlock, Heading, HorizontalRule, ListItem, Markdown, TableElement
from rich.padding import Padding
from rich.rule import Rule
from rich.segment import Segment
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


class TranscriptListItem(ListItem):
    def render_number(self, console, options, number, last_number):
        # Rich writes "1 item"; the dot the text was written with reads as a list, not as a quantity.
        number_width = len(str(last_number)) + 3
        lines = console.render_lines(
            self.elements, options.update(width=options.max_width - number_width), style=self.style
        )
        style = console.get_style("markdown.item.number", default="none")
        numeral = Segment(f"{number}.".rjust(number_width - 1) + " ", style)
        padding = Segment(" " * number_width, style)
        for index, line in enumerate(lines):
            yield padding if index else numeral
            yield from line
            yield Segment.line()


class TranscriptMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "fence": TranscriptCode,
        "code_block": TranscriptCode,
        "heading_open": LeftHeading,
        "hr": TranscriptRule,
        "table_open": TranscriptTable,
        "list_item_open": TranscriptListItem,
    }


# Rich's defaults have backgrounds and dim styles; replies use the same foreground-only look as the rest of the
# interface, live and in the scrollback alike.
MARKDOWN_STYLES = {
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
MARKDOWN_STYLES.update({f"markdown.h{level}": "bold" for level in range(1, 5)})
MARKDOWN_STYLES["markdown.h1"] = "bold underline"
MARKDOWN_STYLES["markdown.code"] = f"bold {PARAMETER_COLOR}"
MARKDOWN_THEME = Theme(MARKDOWN_STYLES)

_PARSER = MarkdownIt().enable("strikethrough").enable("table")  # How rich.markdown.Markdown parses.
# Rich separates blocks by the element that closed last. Inside a list, a quote or a table that is one of their own
# children, so these blocks begin with a blank row of their own, even at the top of a reply.
_OWN_GAP = frozenset({"bullet_list_open", "ordered_list_open", "blockquote_open", "table_open"})


def _block_starts(source: str) -> list[tuple[int, bool]]:
    """The line each top-level block begins on, and whether its rendering begins with its own blank row."""
    starts: dict[int, bool] = {}
    for token in _PARSER.parse(source):
        if token.level == 0 and token.map:
            starts.setdefault(token.map[0], token.type in _OWN_GAP)
    return sorted(starts.items())


class StreamingMarkdown:
    """A reply drawn as TranscriptMarkdown draws it, row for row, while its text is still arriving.

    Rendering the whole reply again for every piece of text slows down as the reply grows. Blocks that the text
    has moved past cannot change any more, so their rows are kept for each width and only the last block, the
    one still being written, is rendered again.
    """

    def __init__(self, source: str = "") -> None:
        self.source = ""
        self.complete = False
        self.tail_seconds = 0.0  # What rendering the open block cost last time.
        self._settled_lines = 0  # Source lines covered by the settled blocks.
        self._settled: list[tuple[bool, str]] = []  # Each settled block: (begins with its own blank row, source).
        self._tail_own_gap = False
        self._rows: dict[int, list[list[list[Segment]]]] = {}  # The settled blocks' rows, by width.
        self._last: tuple[int, str, list[list[Segment]]] | None = None  # The latest rendering of the rest.
        self.feed(source)

    def feed(self, text: str) -> None:
        self.source += text
        lines = self.source.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            # A line is only settled once it is complete: "2" is a paragraph until "2." makes it the next item of
            # the list above it, so an unfinished line never settles the block before it.
            lines.pop()
        open_lines = lines[self._settled_lines :]
        starts = _block_starts("".join(open_lines))
        for (begin, own_gap), (end, _) in zip(starts, starts[1:]):
            self._settled.append((own_gap, "".join(open_lines[begin:end])))
        if len(starts) > 1:
            self._settled_lines += starts[-1][0]
        # The unfinished line counts here: it can turn the paragraph above it into a table.
        tail = _block_starts(self.tail)
        self._tail_own_gap = bool(tail) and tail[0][1]

    def finish(self, source: str) -> None:
        """The full text: drawn in one piece, since a link reference defined at the end can change a line above it."""
        self.source = source
        self.complete = True
        self._last = None

    @property
    def tail(self) -> str:
        return "".join(self.source.splitlines(keepends=True)[self._settled_lines :])

    def _render(self, console, options, source: str) -> list[list[Segment]]:
        if self._last is not None and self._last[:2] == (options.max_width, source):
            return self._last[2]  # Textual renders once to measure the height and again to paint.
        started = perf_counter()
        rows = console.render_lines(TranscriptMarkdown(source), options, pad=False)
        self.tail_seconds = perf_counter() - started
        self._last = (options.max_width, source, rows)
        return rows

    def __rich_console__(self, console, options):
        options = options.update(height=None)
        with console.use_theme(MARKDOWN_THEME):
            if self.complete:
                blocks = [(False, self._render(console, options, self.source))]
            else:
                rows = self._rows.setdefault(options.max_width, [])
                for _, source in self._settled[len(rows) :]:
                    rows.append(console.render_lines(TranscriptMarkdown(source), options, pad=False))
                blocks = [(own_gap, block) for (own_gap, _), block in zip(self._settled, rows)]
                if self.tail.strip():
                    blocks.append((self._tail_own_gap, self._render(console, options, self.tail)))
        new_line = Segment.line()
        for index, (own_gap, block) in enumerate(blocks):
            if index and not own_gap:
                yield new_line  # One blank row between two blocks, as Rich puts it.
            for row in block:
                yield from row
                yield new_line


def message_row(marker, body):
    from rich.table import Table

    row = Table.grid(expand=True)
    row.add_column(width=2)
    # Rich breaks lines only at spaces; a table column would cut a long CJK run with "…".
    row.add_column(ratio=1, overflow="fold")
    row.add_row(Text(marker, style="bold"), body)
    return row


def render_transcript(renderables, width: int) -> str:
    stream = StringIO()
    console = Console(
        file=stream,
        width=max(8, width),
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        legacy_windows=False,
        highlight=False,
        theme=MARKDOWN_THEME,
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
