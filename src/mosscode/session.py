"""Session storage: the directory of stored tool results and compaction records,
and the JSONL transcript that lets a session be resumed."""

import json
import os
import re
import secrets
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .images import ImageRef, ProcessedImage, store_image
from .messages import Message, ToolResult
from .paths import SESSION_ID, mosscode_home, new_session_id, project_dir
from .serialization import message_from_json, message_to_json

MARKER = ".mosscode-session"
KEEP_DAYS = 30
TITLE_BYTES = 64 * 1024
TITLE_CHARS = 40
MESSAGE_PREFIX = b'{"type":"message"'
INTERRUPTED = "会话中断，这个调用可能未执行，也可能执行了但结果丢失；需要时请检查当前状态后重新执行。"


def sessions_root() -> Path:
    """The old location of tool results; no longer written, only pruned until it empties."""
    return Path.home() / ".mosscode" / "sessions"


def project_sessions(root: Path, home: Path | None = None) -> Path:
    return project_dir(root, home) / "sessions"


class Session:
    def __init__(self, directory: Path, *, id: str | None = None, transcript: Path | None = None):
        self.directory = directory.resolve()
        self.id = id or self.directory.name
        # <id>.jsonl beside the directory; None for sessions that keep no transcript.
        self.transcript_path = transcript

    @classmethod
    def create(cls, base: Path | None = None, now: datetime | None = None, *, lazy: bool = False) -> "Session":
        """lazy=True defers the directory to the first write, so short runs leave nothing behind."""
        base = sessions_root() if base is None else base
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        session = cls(base / f"{stamp}-{secrets.token_hex(2)}")
        if not lazy:
            session.ensure()
        return session

    @classmethod
    def new(cls, root: Path, *, home: Path | None = None, now: datetime | None = None) -> "Session":
        """A fresh ID for this project; nothing is created until the first write."""
        base = project_sessions(root, home)
        while True:
            id = new_session_id(now)
            if not any(p.exists() for p in (base / id, base / f"{id}.jsonl", base / f"{id}.lock")):
                return cls(base / id, id=id, transcript=base / f"{id}.jsonl")

    @classmethod
    def from_transcript(cls, path: Path) -> "Session":
        return cls(path.with_suffix(""), id=path.stem, transcript=path)

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        marker = self.directory / MARKER
        if not marker.exists():
            marker.write_text("Created by MossCode; safe to delete.\n", encoding="utf-8")

    def persist_result(self, call_id: str, text: str) -> Path:
        """Write once per call ID; a later attempt keeps the first file."""
        self.ensure()
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", call_id)[:100].strip(".") or secrets.token_hex(4)
        path = self.directory / "tool-results" / f"{name}.txt"
        path.parent.mkdir(exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
        except FileExistsError:
            pass
        return path

    def store_image(self, image: ProcessedImage, alt: str) -> ImageRef:
        """images/<sha256 prefix>.<ext>: the same image is stored once."""
        self.ensure()
        return store_image(self.directory / "images", image, alt)

    def write_record(self, text: str) -> Path:
        """compact-<n>.md with the next free n; earlier records are never overwritten."""
        self.ensure()
        number = 1
        while True:
            path = self.directory / f"compact-{number}.md"
            try:
                with path.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(text)
                return path
            except FileExistsError:
                number += 1


# Transcript records -------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def message_record(message: Message) -> dict:
    return {"type": "message", "time": _now(), "message": message_to_json(message)}


def compact_record(boundary: Message, keep: int, record: str | None) -> dict:
    """The history became the boundary plus the last `keep` messages."""
    return {"type": "compact", "time": _now(), "boundary": message_to_json(boundary), "keep": keep, "record": record}


def skill_record(name: str, arguments: str, pinned: bool) -> dict:
    """A skill was used; pinned when its SOP went into <active-skills>."""
    return {"type": "skill", "time": _now(), "name": name, "arguments": arguments, "pinned": pinned}


def provider_record(name: str, protocol: str, model: str) -> dict:
    """Earlier messages lose their native data, as in a /model switch."""
    return {"type": "provider", "time": _now(), "name": name, "protocol": protocol, "model": model}


# Locks --------------------------------------------------------------------------------------


class SessionLocked(Exception):
    pass


def _lock(stream) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class Lock:
    """An OS lock on <id>.lock: released by the system when the process exits or crashes."""

    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    @property
    def held(self) -> bool:
        return self._stream is not None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            _lock(stream)
        except OSError:
            stream.close()
            raise SessionLocked("该会话正在另一个 MossCode 中使用。") from None
        self._stream = stream

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        if os.name == "nt":
            import msvcrt

            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        stream.close()  # Also releases the lock; a crash releases it the same way.


def is_locked(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    probe = Lock(lock_path)
    try:
        probe.acquire()
    except (SessionLocked, OSError):
        return True
    probe.release()
    return False


# Transcript ---------------------------------------------------------------------------------


def _repair_tail(path: Path) -> None:
    """Cut a half-written last line so the next record starts on its own line."""
    with path.open("r+b") as stream:
        size = stream.seek(0, os.SEEK_END)
        if size == 0:
            return
        stream.seek(size - 1)
        if stream.read(1) == b"\n":
            return
        position = size
        while position > 0:
            start = max(0, position - 65536)
            stream.seek(start)
            chunk = stream.read(position - start)
            index = chunk.rfind(b"\n")
            if index >= 0:
                stream.truncate(start + index + 1)
                return
            position = start
        stream.truncate(0)


class Transcript:
    """Append-only JSONL: one complete line per write; the first failure stops writing for this session."""

    def __init__(self, session: Session, cwd: Path):
        self.session, self.cwd = session, cwd
        self.path = session.transcript_path
        self.lock = Lock(self.path.with_suffix(".lock"))
        self.failed: str | None = None
        self._stream = None

    def open_existing(self) -> None:
        """Resume: lock first, then repair and continue the same file."""
        self.lock.acquire()
        try:
            _repair_tail(self.path)
            self._stream = self.path.open("ab")
        except OSError:
            self.lock.release()
            raise

    def _create(self) -> None:
        self.lock.acquire()
        self._stream = self.path.open("xb")
        self._write(
            {
                "type": "session",
                "v": 1,
                "id": self.session.id,
                "cwd": str(self.cwd),
                "created": _now(),
                "mosscode": version("mosscode"),
            }
        )

    def _write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._stream.write(line.encode("utf-8"))
        self._stream.flush()

    def append(self, record: dict) -> str | None:
        """None on success (or after an earlier failure); the one-time notice on the first failure."""
        if self.failed is not None:
            return None
        try:
            if self._stream is None:
                self._create()
            self._write(record)
            return None
        except (OSError, SessionLocked, ValueError, TypeError) as exc:
            self.failed = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            self.close()
            return f"会话存档写入失败：{self.failed}，本会话之后的消息不会被保存。"

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        self.lock.release()


# Listing ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    id: str
    path: Path
    modified: datetime
    title: str
    messages: int
    locked: bool


def _first_user_line(data: Any) -> str | None:
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict) or message.get("role") != "user" or message.get("synthetic") is not None:
        return None
    command = message.get("command")
    text = (
        command.strip()
        if isinstance(command, str)
        else "".join(
            b.get("text", "")
            for b in message.get("content", [])
            if isinstance(b, dict) and b.get("kind") == "text" and isinstance(b.get("text"), str)
        ).strip()
    )
    return text.splitlines()[0][:TITLE_CHARS] if text else None


def read_title(path: Path) -> str:
    """First real user message, looked for only in the first 64 KiB."""
    try:
        with path.open("rb") as stream:
            head = stream.read(TITLE_BYTES)
    except OSError:
        return "（无标题）"
    for line in head.split(b"\n"):
        if line.startswith(MESSAGE_PREFIX):
            try:
                title = _first_user_line(json.loads(line))
            except ValueError:
                continue
            if title:
                return title
    return "（无标题）"


def count_messages(path: Path) -> int:
    """Lines starting with {"type":"message", counted on bytes without parsing JSON."""
    pattern = b"\n" + MESSAGE_PREFIX
    count, tail, first = 0, b"", True
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1 << 20):
                if first:
                    count += chunk.startswith(MESSAGE_PREFIX)
                    first = False
                data = tail + chunk
                count += data.count(pattern)
                tail = data[-(len(pattern) - 1) :]
    except OSError:
        return 0
    return count


def list_sessions(root: Path, *, home: Path | None = None, limit: int = 20) -> list[SessionSummary]:
    base = project_sessions(root, home)
    try:
        files = [p for p in base.iterdir() if p.suffix == ".jsonl" and SESSION_ID.fullmatch(p.stem)]
    except OSError:
        return []
    stamped = []
    for path in files:
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [
        SessionSummary(
            path.stem,
            path,
            datetime.fromtimestamp(mtime),
            read_title(path),
            count_messages(path),
            is_locked(path.with_suffix(".lock")),
        )
        for mtime, path in stamped[:limit]
    ]


def find_session(root: Path, id: str, *, home: Path | None = None) -> Path | None:
    path = project_sessions(root, home) / f"{id}.jsonl"
    return path if SESSION_ID.fullmatch(id) and path.is_file() else None


# Restoring ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Restored:
    id: str
    history: list[Message]
    skipped: int = 0
    interrupted: int = 0
    unanswered: tuple[str, ...] = ()
    last_time: datetime | None = None
    title: str = "（无标题）"
    stripped: bool = False
    skills: tuple[tuple[str, str, bool], ...] = ()  # (name, arguments, pinned) in record order.
    lost_images: int = 0  # Image files missing from the session directory.


def _strip(history: Sequence[Message]) -> list[Message]:
    return [replace(m, protocol=None, native=()) for m in history]


def _time(record: dict) -> datetime | None:
    value = record.get("time", record.get("created"))
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def repair(history: Sequence[Message]) -> tuple[list[Message], int]:
    """Pair every tool call with a result: orphan results go, missing ones become `interrupted`."""
    repaired, filled = [], 0
    index = 0
    while index < len(history):
        message = history[index]
        index += 1
        if message.role == "tool":
            continue  # Not directly after a tool request: an orphan.
        repaired.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        found = {}
        if index < len(history) and history[index].role == "tool":
            found = {r.call_id: r for r in history[index].tool_results}
            index += 1
        results = []
        for call in message.tool_calls:
            result = found.get(call.id)
            if result is None:
                result = ToolResult(call.id, call.name, INTERRUPTED, True, "interrupted", {"executed": None})
                filled += 1
            results.append(result)
        repaired.append(Message("tool", (), tool_results=tuple(results)))
    return repaired, filled


def load_session(path: Path, provider: tuple[str, str] | None = None) -> Restored:
    """Replay the records. provider is the current (name, protocol)."""
    history: list[Message] = []
    skipped, last_time, last_provider = 0, None, None
    skills: list[tuple[str, str, bool]] = []
    images, lost = path.with_suffix("") / "images", []
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                    raise ValueError("记录类型不合法")
                kind = record["type"]
                if kind == "message":
                    history.append(message_from_json(record.get("message"), images, lost))
                elif kind == "compact":
                    keep = record.get("keep")
                    if type(keep) is not int or keep < 0:
                        raise ValueError("keep 不合法")
                    boundary = message_from_json(record.get("boundary"), images, lost)
                    history = [boundary] + (history[len(history) - min(keep, len(history)) :] if keep else [])
                elif kind == "provider":
                    if not isinstance(record.get("name"), str) or not isinstance(record.get("protocol"), str):
                        raise ValueError("provider 记录不合法")
                    history = _strip(history)
                    last_provider = (record["name"], record["protocol"])
                elif kind == "skill":
                    name, arguments, pinned = record.get("name"), record.get("arguments", ""), record.get("pinned")
                    if (
                        not isinstance(name, str)
                        or not name
                        or not isinstance(arguments, str)
                        or not isinstance(pinned, bool)
                    ):
                        raise ValueError("skill 记录不合法")
                    skills.append((name, arguments, pinned))
                elif kind != "session":
                    pass  # A later version's record type.
            except (ValueError, TypeError, KeyError, UnicodeError):
                skipped += 1
                continue
            last_time = _time(record) or last_time
    stripped = False
    if provider is not None:
        if last_provider is not None:
            differs = last_provider != tuple(provider)
        else:
            protocol = next((m.protocol for m in reversed(history) if m.protocol), None)
            differs = protocol is not None and protocol != provider[1]
        if differs and any(m.protocol or m.native for m in history):
            history, stripped = _strip(history), True
    history, filled = repair(history)
    unanswered = []
    while history and history[-1].role == "user" and history[-1].synthetic is None:
        unanswered.insert(0, history.pop().shown)
    return Restored(
        path.stem,
        history,
        skipped,
        filled,
        tuple(unanswered),
        last_time,
        read_title(path),
        stripped,
        tuple(skills),
        len(lost),
    )


# Pruning ------------------------------------------------------------------------------------


def prune_sessions(base: Path | None = None, now: datetime | None = None, days: int = KEEP_DAYS) -> int:
    """Delete old session directories this program created (marked); anything else is left alone."""
    base = sessions_root() if base is None else base
    cutoff = ((now or datetime.now()) - timedelta(days=days)).timestamp()
    removed = 0
    try:
        children = list(base.iterdir())
    except OSError:
        return 0
    for child in children:
        marker = child / MARKER
        try:
            if child.is_dir() and not child.is_symlink() and marker.is_file() and marker.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                removed += 1
        except OSError:
            continue
    return removed


def _prune_project(base: Path, cutoff: float) -> int:
    try:
        children = list(base.iterdir())
    except OSError:
        return 0
    ids = {child.stem if child.suffix in {".jsonl", ".lock"} else child.name for child in children}
    removed = 0
    for id in sorted(i for i in ids if SESSION_ID.fullmatch(i)):
        transcript, directory, lock = base / f"{id}.jsonl", base / id, base / f"{id}.lock"
        marked = directory.is_dir() and not directory.is_symlink() and (directory / MARKER).is_file()
        try:
            if transcript.is_file():
                modified = transcript.stat().st_mtime
            elif marked:
                modified = (directory / MARKER).stat().st_mtime
            else:
                continue
            if modified >= cutoff or is_locked(lock):
                continue
            if transcript.is_file():
                transcript.unlink()
            if marked:
                shutil.rmtree(directory)
            lock.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue  # In use or not ours to delete: try again next start.
    return removed


def prune_all(home: Path | None = None, now: datetime | None = None, days: int = KEEP_DAYS) -> int:
    """Startup cleanup: every project's sessions, then the old location of tool results."""
    root = mosscode_home(home)
    cutoff = ((now or datetime.now()) - timedelta(days=days)).timestamp()
    removed = 0
    try:
        projects = list((root / "projects").iterdir())
    except OSError:
        projects = []
    for project in projects:
        removed += _prune_project(project / "sessions", cutoff)
    return removed + prune_sessions(root / "sessions", now, days)
