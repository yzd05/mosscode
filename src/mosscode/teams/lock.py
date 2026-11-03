"""A lock file next to each data file, and writes a reader never sees half done.

Every team file (config, tasks, mailboxes) is read, changed and written under its lock; the lock is
held only for the file work, never across a model call.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

LOCK_RETRIES = 10
BACKOFF = (0.005, 0.1)  # Seconds, random within.
STALE_AFTER = 10.0  # Seconds; an older lock file belongs to a process that died holding it.
DEADLINE = 5.0  # Seconds of waiting in all, however often the lock changed hands.
REPLACE_RETRIES = 3


class TeamError(Exception):
    """A team operation that did not happen; code is the tool error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stale(lock: Path, after: float) -> bool:
    try:
        return time.time() - lock.stat().st_mtime > after
    except OSError:
        return False  # Gone already: the next attempt will tell.


def _holder(lock: Path) -> str:
    try:
        return lock.read_text(encoding="utf-8")
    except OSError:
        return ""


@contextmanager
def file_lock(
    path: Path,
    *,
    busy: str = "team_busy",
    retries: int = LOCK_RETRIES,
    stale: float = STALE_AFTER,
    deadline: float = DEADLINE,
):
    """O_CREAT|O_EXCL on <file>.lock; retries with random backoff, then TeamError(busy).

    The count starts again whenever the lock changed hands (others are making progress, it is not stuck),
    within an overall deadline; a lock held by one holder throughout fails after `retries`.
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()} {now()} {secrets.token_hex(4)}"
    attempt, seen, start = 0, None, time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):  # Windows: PermissionError while it is being deleted.
            if _stale(lock, stale):
                try:
                    os.remove(lock)
                except OSError:
                    pass
            holder = _holder(lock)
            if holder and seen is not None and holder != seen:
                attempt = 0
            seen = holder or seen
            if attempt >= retries or time.monotonic() - start > deadline:
                raise TeamError(
                    busy, f"{path.name} 正被其他进程使用，重试 {retries} 次后仍拿不到锁；稍后再试。"
                ) from None
            attempt += 1
            time.sleep(random.uniform(*BACKOFF))
            continue
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        _release(lock)


def _release(lock: Path) -> None:
    """Windows refuses to delete a file another waiter has open for reading its holder: retry briefly,
    or the lock would stay until it is stale."""
    for _ in range(40):
        try:
            os.remove(lock)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.005)
        except OSError:
            return


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
