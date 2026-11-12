import asyncio
import re

from rich.text import Text
from textual import events
from textual.drivers.headless_driver import HeadlessDriver
from textual.geometry import Offset, Size

from mosscode.appearance import (
    BRAND_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    OPERATION_COLOR,
    RULE_COLOR,
    TOOL_SUCCESS_COLOR,
)
from mosscode.conversation import Conversation
from mosscode.messages import Delta, ToolProgress, ToolResult
from mosscode.scrollback import TranscriptMarkdown, highlighted_code, render_transcript
from mosscode.tui import ChatEntry, MossCodeApp, PromptArea, ToolEntry
from test_agent import SequenceProvider, call, response


class InlineRecordingDriver(HeadlessDriver):
    @property
    def is_inline(self):
        return True

    def write(self, data):
        self._app.output.append(data)


class InlineRecordingApp(MossCodeApp):
    def __init__(self, conversation, output):
        self.output = output
        super().__init__(conversation, "test")

    def _build_driver(self, headless, inline, mouse, size):
        return InlineRecordingDriver(self, mouse=False, size=size)


async def send(app, pilot, value):
    app.query_one("#prompt", PromptArea).value = value
    await pilot.press("enter")
    if app._reply_task is not None:
        await asyncio.wait_for(app._reply_task, 5)
    await pilot.pause()


CLEAR = "\x1b[H\x1b[2J\x1b[3J"  # Starts the repaint after a width change.


async def settled_repaint(pilot, output, start):
    """Until the debounced repaint has started, then long enough for a second one to show."""
    for _ in range(100):
        if CLEAR in "".join(output[start:]):
            break
        await pilot.pause(0.05)
    await pilot.pause(0.3)


def plain(output):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(output))


async def test_two_inline_runs_keep_all_completed_content_above_the_live_input():
    output = []
    long_answer = "\n".join(f"OLD-LINE-{i:03d}" for i in range(160))
    first = InlineRecordingApp(Conversation(SequenceProvider(response(long_answer))), output)
    async with first.run_test(size=(70, 18)) as pilot:
        await send(first, pilot, "FIRST-SESSION-QUESTION")
        assert len(first.conversation.history) == 2
        assert len(first.query(ChatEntry)) == 0
        assert not first.query("#banner")
        assert first.query_one("#prompt", PromptArea).has_focus
        await send(first, pilot, "/help")
    before_second = list(output)
    second = InlineRecordingApp(Conversation(SequenceProvider(response("SHORT-SECOND-ANSWER"))), output)
    async with second.run_test(size=(70, 18)) as pilot:
        await send(second, pilot, "SECOND-SESSION-QUESTION")
        assert len(second.query(ChatEntry)) == 0
        assert second.query_one("#prompt").region.bottom <= second.size.height
    assert output[: len(before_second)] == before_second
    text = plain(output)
    for token in (
        "FIRST-SESSION-QUESTION",
        "OLD-LINE-000",
        "OLD-LINE-159",
        "SECOND-SESSION-QUESTION",
        "SHORT-SECOND-ANSWER",
    ):
        assert text.count(token) == 1
    assert text.index("OLD-LINE-159") < text.index("SECOND-SESSION-QUESTION")
    assert all(len(write) <= 8192 for write in output)
    assert "鼠标拖选" in text and "PageUp" not in text
    assert "\x1b[2J" not in "".join(output) and "\x1b[?1049" not in "".join(output)


async def test_live_thinking_is_temporary_and_completed_markdown_is_committed_once():
    output, thinking_ready, advance = [], asyncio.Event(), asyncio.Event()

    async def stream():
        yield Delta("thinking", "TEMPORARY-THINKING")
        thinking_ready.set()
        await advance.wait()
        yield Delta("text", "**FINAL-ANSWER**\n\n```python\nprint('code')\n```")
        yield response("**FINAL-ANSWER**\n\n```python\nprint('code')\n```")

    app = InlineRecordingApp(Conversation(SequenceProvider(stream)), output)
    async with app.run_test(size=(70, 18)) as pilot:
        app.query_one("#prompt", PromptArea).value = "QUESTION"
        await pilot.press("enter")
        await asyncio.wait_for(thinking_ready.wait(), 5)
        await pilot.pause()
        assert app.query_one(".thinking").display
        assert "TEMPORARY-THINKING" not in plain(output)
        advance.set()
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        text = plain(output)
        assert "TEMPORARY-THINKING" not in text
        assert text.count("FINAL-ANSWER") == 1 and "**FINAL-ANSWER**" not in text
        assert "print('code')" in text and "```" not in text
        assert "完成" in text and len(app.query(ChatEntry)) == 0


async def test_parallel_tool_results_commit_in_display_order_and_reset_inline_origin():
    output = []
    app = InlineRecordingApp(Conversation(SequenceProvider()), output)
    async with app.run_test(size=(70, 18)) as pilot:
        chat = app.query_one("#chat")
        first = ToolEntry(ToolProgress(call("read_file", "a", path="first.txt"), "running"))
        second = ToolEntry(ToolProgress(call("read_file", "b", path="second.txt"), "running"))
        await chat.mount(first, second)
        second.complete(ToolResult("b", "read_file", "SECOND-RESULT"), 0.2)
        await app._commit_ready_transcript()
        assert "SECOND-RESULT" not in plain(output)
        first.complete(ToolResult("a", "read_file", "FIRST-RESULT"), 0.3)
        app._previous_cursor_position = Offset(5, 10)
        await app._commit_ready_transcript()
        await pilot.pause()
        text = plain(output)
        assert text.index("FIRST-RESULT") < text.index("SECOND-RESULT")
        assert app._previous_cursor_position == Offset(0, 0)
        assert not chat.children
        assert output[-1].startswith("\x1b[5D\x1b[10A\r\x1b[J")


def test_committed_markdown_keeps_terminal_background_and_normal_brightness():
    data = render_transcript(
        [
            TranscriptMarkdown(
                "# Heading\n\n`file.py`\n\n```python\nprint(1)\n```\n\n> Quote\n\n---\n\n|A|B|\n|-|-|\n|1|2|"
            )
        ],
        70,
    )
    assert "print(1)" in plain([data])
    assert "38;2;111;168;255" in data
    for sgr in re.findall(r"\x1b\[([\d;]*)m", data):
        codes, index = [int(code or 0) for code in sgr.split(";")], 0
        while index < len(codes):
            assert codes[index] != 2 and codes[index] != 48
            assert not 40 <= codes[index] <= 47 and not 100 <= codes[index] <= 107
            index += (5 if codes[index + 1] == 2 else 3) if codes[index] == 38 else 1


def foreground(color: str) -> str:
    return "\x1b[38;2;" + ";".join(str(int(color[at : at + 2], 16)) for at in (1, 3, 5)) + "m"


def test_committed_markdown_is_laid_out_like_the_live_reply():
    text = "# Heading\n\nbody\n\n---\n\n|A|B|\n|-|-|\n|1|2|\n\nafter\n\n---\n\n- item\n\n## Second\n\nend"
    data = render_transcript([TranscriptMarkdown(text)], 40)
    # Headings start at the left, a rule is a thin grey line, and one blank line separates blocks:
    # around the rule and the table too, where Rich's own elements leave two.
    assert [line.rstrip() for line in plain([data]).split("\r\n")][:17] == [
        "  Heading",
        "",
        "  body",
        "",
        "  " + "─" * 36,
        "",
        "  A  B",
        "  ────",
        "  1  2",
        "",
        "  after",
        "",
        "  " + "─" * 36,
        "",
        "   • item",
        "",
        "  Second",
    ]
    assert "\x1b[1;4mHeading\x1b[0m" in data and "\x1b[1mSecond\x1b[0m" in data
    assert foreground(RULE_COLOR) + "─" * 36 in data


def test_code_is_colored_only_when_the_fence_names_a_known_language():
    code = "# note\nreturn 'a', 42"
    colored = highlighted_code(code, "python")
    styles = {colored.plain[span.start : span.end]: str(span.style) for span in colored.spans}
    assert colored.plain == code
    assert styles == {
        "# note": MUTED_COLOR,
        "return": BRAND_COLOR,
        "'": TOOL_SUCCESS_COLOR,
        "a": TOOL_SUCCESS_COLOR,
        "42": OPERATION_COLOR,
    }
    # The info string may carry more than the language.
    assert highlighted_code(code, "python title=demo.py").spans == colored.spans
    for language in ("", "  ", "no-such-language"):
        assert highlighted_code(code, language).plain == code and not highlighted_code(code, language).spans
    changed = highlighted_code("+new\n-old\n same", "diff")
    assert {changed.plain[span.start : span.end]: str(span.style) for span in changed.spans} == {
        "+new": TOOL_SUCCESS_COLOR,
        "-old": ERROR_COLOR,
    }
    # Text the lexer would not give back unchanged is shown exactly as an unlabelled block shows it.
    windows = "a = 1\r\nb = 2"
    assert highlighted_code(windows, "python") == highlighted_code(windows, "")


def test_committed_code_blocks_use_the_same_colors_as_the_live_ones():
    text = "```python\n# note\nreturn 42\n```\n\n```\nreturn 42\n```"
    data = render_transcript([TranscriptMarkdown(text)], 40)
    assert foreground(MUTED_COLOR) + "# note" in data
    assert data.count(foreground(BRAND_COLOR) + "return") == 1 and data.count(foreground(OPERATION_COLOR) + "42") == 1
    assert "    return 42" in data  # The block without a language, indented like the other and left plain.


async def test_cancelled_partial_reply_is_kept_once_in_terminal_but_not_model_history():
    output, ready, pending = [], asyncio.Event(), asyncio.Event()

    async def stream():
        yield Delta("text", "PARTIAL-CANCELLED")
        ready.set()
        await pending.wait()

    app = InlineRecordingApp(Conversation(SequenceProvider(stream)), output)
    async with app.run_test(size=(70, 18)) as pilot:
        app.query_one("#prompt", PromptArea).value = "CANCEL-QUESTION"
        await pilot.press("enter")
        await asyncio.wait_for(ready.wait(), 5)
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        assert plain(output).count("PARTIAL-CANCELLED") == 1
        assert "已取消" in plain(output)
        assert app.conversation.history == () and not app.query(ChatEntry)
        assert app.query_one("#prompt", PromptArea).has_focus


async def test_width_change_repaints_the_whole_history_once_the_resize_settles():
    output = []
    app = InlineRecordingApp(Conversation(SequenceProvider(response("WIDE-ANSWER " * 12))), output)
    async with app.run_test(size=(70, 18)) as pilot:
        await send(app, pilot, "FIRST-QUESTION")
        committed = plain(output)
        assert committed.count("WIDE-ANSWER") == 12 and "\x1b[3J" not in "".join(output)
        # Height alone does not re-wrap rows: no repaint.
        await pilot.resize_terminal(70, 16)
        await pilot.pause(0.3)
        assert "\x1b[3J" not in "".join(output)
        # A drag: several widths back to back (resize_terminal waits for a refresh after each one,
        # which on a slow machine outlasts the debounce), one repaint at the final width.
        start = len(output)
        for width in (60, 50, 44):
            app._driver._size = Size(width, 16)
            app.post_message(events.Resize(Size(width, 16), Size(width, 16)))
        await settled_repaint(pilot, output, start)
        repaint = "".join(output[start:])
        assert repaint.count(CLEAR) == 1
        text = plain(output[start:])
        assert text.count("FIRST-QUESTION") == 1 and text.count("WIDE-ANSWER") == 12
        rows = plain([repaint.split("\x1b[3J", 1)[1]]).replace("\r", "").split("\n")
        assert max(len(row) for row in rows) <= 44
        assert app.query_one("#prompt", PromptArea).has_focus


def test_long_cjk_paragraphs_wrap_instead_of_ending_in_ellipsis():
    sentence = "我不知道你所指的“验收暗号”是什么，我的上下文里没有定义或约定过任何暗号，也没有相关的项目指令。"
    entry = ChatEntry("assistant", sentence)
    entry.phase = "complete"
    rendered = plain([render_transcript([entry.transcript(), ChatEntry("You", sentence).transcript()], 40)])
    assert "…" not in rendered
    flat = re.sub(r"\s+", "", rendered)
    assert flat.count(re.sub(r"\s+", "", sentence)) == 2
    assert max(len(row) for row in rendered.split("\r\n")) <= 40


async def test_clear_wipes_screen_and_scrollback_and_keeps_only_the_banner():
    output = []
    app = InlineRecordingApp(Conversation(SequenceProvider(response("OLD-ANSWER"))), output)
    async with app.run_test(size=(70, 18)) as pilot:
        await send(app, pilot, "OLD-QUESTION")
        assert "OLD-ANSWER" in plain(output)
        start = len(output)
        await send(app, pilot, "/clear")
        after = "".join(output[start:])
        assert after.count("[H[2J[3J") == 1
        repaint = plain([after.split("[3J", 1)[1]])
        assert "OLD-" not in repaint and "MossCode" in repaint and "已开始新会话" in repaint
        # A later width change repaints only what came after the clear.
        start = len(output)
        await pilot.resize_terminal(50, 18)
        await settled_repaint(pilot, output, start)
        text = plain(output[start:])
        assert "OLD-" not in text and text.count("已开始新会话") == 1


async def test_a_response_that_only_calls_tools_leaves_no_empty_text_row(tmp_path):
    from mosscode.messages import Delta
    from mosscode.tools import create_default_registry

    (tmp_path / "a.txt").write_text("NEEDLE\n", encoding="utf-8")

    async def think_then_search():
        yield Delta("thinking", "先搜索")
        await asyncio.sleep(0.05)  # Nonzero thinking time, so its row is shown.
        yield response(calls=[call("grep", pattern="NEEDLE")])

    output = []
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    app = InlineRecordingApp(
        Conversation(SequenceProvider(think_then_search, response("FOUND-IT")), registry=registry), output
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await send(app, pilot, "搜索 NEEDLE")
        text = plain(output)
        assert "✻ 思考 " in text and "FOUND-IT" in text
        assert "服务返回了空正文" not in text


def test_thinking_label_is_followed_by_one_blank_line_with_or_without_a_reply():
    def rows(entry, *after):
        return [row.rstrip() for row in plain([render_transcript([entry.transcript(), *after], 40)]).split("\r\n")]

    alone = ChatEntry("assistant")
    alone.thinking_elapsed, alone.tool_request = 1.25, True
    assert rows(alone, Text("NEXT"))[:3] == ["  ✻ 思考 1.2s", "", "  NEXT"]

    answered = ChatEntry("assistant", "ANSWER")
    answered.thinking_elapsed = 1.25
    assert rows(answered, Text("NEXT"))[:5] == ["  ✻ 思考 1.2s", "", "  ● ANSWER", "", "  NEXT"]


async def test_user_messages_sit_on_a_grey_bar_live_and_in_scrollback():
    from mosscode.appearance import USER_MESSAGE_BACKGROUND

    output = []
    app = InlineRecordingApp(Conversation(SequenceProvider(response("ANSWER"))), output)
    async with app.run_test(size=(40, 18)) as pilot:
        app.query_one("#prompt", PromptArea).value = "第一行\n第二行"
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
    bar = "48;2;74;74;74m"
    rows = "".join(output).split("\r\n")
    user_rows = [row for row in rows if "第一行" in row or "第二行" in row]
    assert len(user_rows) == 2 and all(bar in row for row in user_rows)
    assert not any(bar in row for row in rows if "ANSWER" in row)
    assert USER_MESSAGE_BACKGROUND == "#4a4a4a"


async def test_live_user_entry_uses_the_bar_colors():
    app = MossCodeApp(Conversation(SequenceProvider()), "test")
    async with app.run_test(size=(40, 18)):
        entry = ChatEntry("You", "排队中的消息")
        await app.query_one("#chat").mount(entry)
        assert entry.query_one(".message-row").styles.background.hex == "#4A4A4A"
        assert entry.query_one(".user-message").styles.color.hex == "#F2F2F2"
