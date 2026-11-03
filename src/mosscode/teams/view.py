"""A member's conversation as short lines — the same in the lead's member view
(read from its transcript) and in the member's own window (from the events of its turns)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from rich.text import Text

from ..appearance import ERROR_COLOR, OPERATION_COLOR, PARAMETER_COLOR
from ..messages import Completed, Message, ProviderError, RunStopped, ToolCall, ToolProgress, ToolResult
from ..permissions import safe_display
from ..serialization import message_from_json
from .member import TYPE_LABELS

# render_incoming's head of each message: "[来自 lead · 消息 · id=m-1] 摘要：…".
HEAD = re.compile(r"^\[来自 (\S+) · (.+?) · id=[^\]]*\]\s*(.*)$")
COMPACTED = "（较早的对话已压缩）"


def incoming_line(sender: str, label: str, summary: str) -> Text:
    line = Text(f"← {sender} · {label}", style="bold")
    if summary:
        line.append(f"：{summary[:200]}")
    return line


def call_line(call: ToolCall) -> Text:
    line = Text("● ", style=OPERATION_COLOR)
    line.append(call.name, style="bold")
    line.append(" " + safe_display(call.arguments, 160), style=PARAMETER_COLOR)
    return line


def result_line(result: ToolResult) -> Text:
    first = (result.output or "").strip().splitlines()[:1]
    style = ERROR_COLOR if result.is_error else "dim"
    return Text("  └ " + safe_display(first[0] if first else "完成", 200), style=style)


def _incoming(text: str) -> list[Text]:
    lines, rows = text.splitlines(), []
    for index, line in enumerate(lines):
        match = HEAD.match(line)
        if match is None:
            continue
        sender, label, rest = match.groups()
        rest = rest.removeprefix("摘要：").strip()
        if not rest:  # The body starts on the next line.
            rest = next((item.strip() for item in lines[index + 1 :] if item.strip()), "")
            rest = "" if HEAD.match(rest) or rest.startswith("</incoming") else rest
        rows.append(incoming_line(sender, label, rest))
    return rows


def message_lines(message: Message) -> list[Text]:
    """One committed message of the transcript, without its tool calls (Feed pairs them with their results)."""
    if message.role == "tool":
        return [result_line(result) for result in message.tool_results]
    if message.role == "user":
        if message.synthetic == "compact":
            return [Text(COMPACTED, style="dim")]
        text = message.text.strip()
        if text.startswith("<incoming-messages>"):
            return _incoming(text)
        first = text.splitlines()[0] if text else ""
        return [Text("❯ " + safe_display(first, 200), style="dim")] if first else []
    return [Text(message.text.strip())] if message.text.strip() else []


class Feed:
    """A member's conversation as lines, from its transcript (lead) or its turn's events (own window).

    A tool call is written together with its result, so the results of parallel calls sit under their own
    calls; until then the call is in `running`, shown apart from the log as the lead shows its own."""

    def __init__(self) -> None:
        self.waiting: dict[str, ToolCall] = {}

    @property
    def running(self) -> list[Text]:
        return [call_line(call) for call in self.waiting.values()]

    def running_text(self) -> Text:
        text = Text("\n").join(self.running)
        text.no_wrap, text.overflow = True, "ellipsis"
        return text

    def flush(self) -> list[Text]:
        """Calls whose results never came (an interrupted turn) are written as they were."""
        lines, self.waiting = self.running, {}
        return lines

    def result(self, result: ToolResult) -> list[Text]:
        call = self.waiting.pop(result.call_id, None)
        return ([call_line(call)] if call is not None else []) + [result_line(result)]

    def message(self, message: Message) -> list[Text]:
        if message.role == "tool":
            return [line for result in message.tool_results for line in self.result(result)]
        lines = self.flush() + message_lines(message)
        self.waiting.update((call.id, call) for call in message.tool_calls)
        return lines

    def record(self, record: dict) -> list[Text]:
        kind = record.get("type")
        if kind == "message":
            try:
                message = message_from_json(record.get("message"))
            except (ValueError, TypeError, KeyError):
                return []
            return self.message(message)
        if kind == "compact":
            return self.flush() + [Text(COMPACTED, style="dim")]
        return []

    def event(self, event) -> list[Text]:
        """A member's own window, as its turn runs."""
        if isinstance(event, list):  # The messages of a new turn.
            return self.flush() + [
                incoming_line(
                    message.sender,
                    TYPE_LABELS.get(message.type, message.type),
                    message.summary or (message.body.strip().splitlines() or [""])[0],
                )
                for message in event
            ]
        if isinstance(event, ToolProgress):
            if event.phase == "running":
                self.waiting[event.call.id] = event.call
            elif event.phase == "finished" and event.result is not None:
                return self.result(event.result)
        elif isinstance(event, Completed) and event.message.text:
            return self.flush() + [Text(event.message.text)]
        elif isinstance(event, RunStopped):
            lines = self.flush()
            return lines + (
                [Text(event.notice, style=ERROR_COLOR)] if event.reason not in {"completed", "max_tokens"} else []
            )
        elif isinstance(event, ProviderError):
            return self.flush() + [Text(str(event), style=ERROR_COLOR)]
        return []


class TranscriptTail:
    """The records appended to a transcript since the last read. A line still being written stays
    for the next read; a line that cannot be parsed is skipped."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self._partial = b""

    def read(self) -> list[dict]:
        """Blocking. OSError when the file cannot be read; [] while it does not exist yet."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:  # Replaced by a shorter file: start over.
            self.offset, self._partial = 0, b""
        if size == self.offset:
            return []
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(size - self.offset)
        self.offset += len(data)
        data = self._partial + data
        *complete, self._partial = data.split(b"\n")
        records = []
        for raw in complete:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records
