"""A lock file next to each data file, and writes a reader never sees half done.

Every team file (config, tasks, mailboxes) is read, changed and written under its lock; the lock is
held only for the file work, never across a model call.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

TIMEOUT = 5.0  # Seconds of waiting for a lock before the operation fails.
REPLACE_RETRIES = 3


class TeamError(Exception):
    """A team operation that did not happen; code is the tool error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def file_lock(path: Path, *, busy: str = "team_busy", timeout: float | None = None):
    """An OS lock on <file>.lock, or TeamError(busy) after `timeout` seconds (TIMEOUT by default).

    The system releases the lock when its holder exits or crashes, so a dead process never leaves a team
    file locked; the .lock file itself may stay behind and means nothing on its own.
    """
    wait = TIMEOUT if timeout is None else timeout
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path, timeout=wait)
    try:
        lock.acquire()
    except Timeout:
        raise TeamError(busy, f"{path.name} 正被其他进程使用，等待 {wait:g} 秒后仍拿不到锁；稍后再试。") from None
    try:
        yield
    finally:
        lock.release()


def atomic_write(path: Path, data: Any) -> None:
    """A temporary file in the same directory, then os.replace (retried briefly on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for attempt in range(REPLACE_RETRIES + 1):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:  # Windows: another process has it open for a moment.
                if attempt == REPLACE_RETRIES:
                    raise
                time.sleep(0.02)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def read_text(path: Path) -> str:
    """Readers without the lock (/team, peek) can meet os.replace mid-way on Windows: retry briefly."""
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == REPLACE_RETRIES:
                raise
            time.sleep(0.02)


def load_json(
    path: Path, default: Callable[[], Any], valid: Callable[[Any], bool], on_notice: Callable[[str], None] | None = None
) -> Any:
    """Missing -> default; unreadable or the wrong shape -> kept as <file>.corrupt-<time>, then default."""
    try:
        text = read_text(path)
    except FileNotFoundError:
        return default()
    try:
        data = json.loads(text)
        if valid(data):
            return data
    except ValueError:
        pass
    kept = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(path, kept)
        note = f"{path} 已损坏，原文件保留为 {kept.name}，按空文件继续。"
    except OSError as exc:
        note = f"{path} 已损坏，且无法改名保留（{exc}），按空文件继续。"
    if on_notice is not None:
        on_notice(note)
    return default()
