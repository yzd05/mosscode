"""What a command wrote, as text: UTF-8 where it is UTF-8, otherwise the code page Windows programs write in.

Windows programs such as ping, ipconfig and cmd's built-ins write to a pipe in the console code page (936/GBK on
Chinese Windows), while Python, Git Bash tools and MossCode's own lines are UTF-8, so one output can hold both. The
choice is made line by line: a line that is valid UTF-8 is UTF-8, any other line is the code page. Code-page text is
almost never valid UTF-8 (a GBK character is valid UTF-8 only by accident, and a line of them practically never), so
UTF-8 output decodes exactly as it always did. Systems other than Windows have no fallback and decode as UTF-8.
"""

import codecs
import functools
import sys

# Code pages in which a character may take two bytes, so a cut can split one (Python's names: cp936 is gbk).
DOUBLE_BYTE = frozenset({"cp932", "gbk", "cp949", "cp950"})
LINE_HOLD = 4096
_CONSOLE = object()


@functools.cache
def console_encoding() -> str | None:
    """The code page a command's non-UTF-8 output is in, or None where there is none (not Windows, or UTF-8 already).

    Commands run in a console of their own (CREATE_NO_WINDOW), which starts in the OEM code page whatever code page
    MossCode's own console was switched to; hence GetOEMCP and not GetConsoleOutputCP. On Chinese, Japanese and Korean
    Windows the ANSI code page some programs use instead is the same one.
    """
    if sys.platform != "win32":
        return None
    import ctypes

    code_page = ctypes.windll.kernel32.GetOEMCP()
    if code_page == 65001:
        return None
    try:
        return codecs.lookup(f"cp{code_page}").name
    except LookupError:
        return None


def _aligned(line: bytes, encoding: str) -> bytes:
    """line, which follows a cut, without the end of a character the cut split."""
    if encoding == "utf-8":
        skip = 0
        while skip < min(3, len(line)) and 0x80 <= line[skip] < 0xC0:
            skip += 1
        return line[skip:]
    if codecs.lookup(encoding).name in DOUBLE_BYTE:
        # A trail byte cannot be told from a lead byte by itself. Windows' own messages are GB2312-style text, whose
        # trail bytes are high too: an odd run of high bytes at the start then begins with half a character.
        run = next((index for index, byte in enumerate(line) if byte < 0x80), len(line))
        return line[run % 2 :]
    return line


def _decode(data: bytes, encoding: str, *, cut: bool, final: bool, errors: str = "strict") -> tuple[str, bytes]:
    """(text, bytes held back); unless final, an unfinished character at the end is held back rather than decoded."""
    decoder = codecs.getincrementaldecoder(encoding)(errors)
    text = decoder.decode(_aligned(data, encoding) if cut else data, final=final)
    return text, decoder.getstate()[0]


def _text(data: bytes, encoding: str | None, *, cut: bool, final: bool) -> tuple[str, bytes]:
    """(text, bytes held back) with the encoding chosen line by line."""
    try:
        return _decode(data, "utf-8", cut=cut, final=final)
    except UnicodeDecodeError:
        pass
    if encoding is None:
        return _decode(data, "utf-8", cut=cut, final=final, errors="replace")
    parts, held = [], b""
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        first, last = index == 0, index == len(lines) - 1
        for candidate in ("utf-8", encoding):
            try:
                text, held = _decode(line, candidate, cut=cut and first, final=final or not last)
                break
            except UnicodeDecodeError:
                continue
        else:
            # Neither fits (binary output, a broken stream): UTF-8 with replacement, as on every other system.
            text, held = _decode(line, "utf-8", cut=cut and first, final=final or not last, errors="replace")
        parts.append(text)
    return "".join(parts), held


class OutputDecoder:
    """One stream's chunks as text, for output that arrives a piece at a time.

    With a code page to fall back to, a line is decoded once it is complete: a few bytes of GBK can pass for UTF-8,
    a whole line practically never. An unfinished line waits for the rest of it, but not past LINE_HOLD bytes, so
    long output without line breaks still appears as it comes. Without a fallback, only a split character waits.
    """

    def __init__(self, encoding: str | None | object = _CONSOLE):
        self.encoding = console_encoding() if encoding is _CONSOLE else encoding
        self.pending = b""
        self._cut = False

    def decode(self, data: bytes, *, final: bool = False, cut: bool = False) -> str:
        """cut: bytes before data were dropped, maybe in the middle of a character, which is then left out."""
        data, self.pending = self.pending + data, b""
        cut, self._cut = cut or self._cut, False
        if not final and self.encoding is not None:
            end = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
            if len(data) - end < LINE_HOLD:
                data, self.pending, final = data[:end], data[end:], True
                if not data:
                    self._cut = cut
                    return ""
        text, held = _text(data, self.encoding, cut=cut, final=final)
        self.pending = held + self.pending
        return text


def decode_output(
    data: bytes, *, cut_start: bool = False, cut_end: bool = False, encoding: str | None | object = _CONSOLE
) -> str:
    """data as text. cut_start / cut_end: bytes before / after it were dropped, and a character split at that edge
    is left out instead of becoming garbage."""
    return _text(data, console_encoding() if encoding is _CONSOLE else encoding, cut=cut_start, final=not cut_end)[0]
