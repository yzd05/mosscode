"""Per-run session directory: stored tool results and compaction records. Not a transcript store."""

from datetime import datetime, timedelta
from pathlib import Path
import re
import secrets
import shutil

MARKER = ".mosscode-session"
KEEP_DAYS = 30


def sessions_root() -> Path:
    return Path.home() / ".mosscode" / "sessions"


class Session:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    @classmethod
    def create(cls, base: Path | None = None, now: datetime | None = None, *, lazy: bool = False) -> "Session":
        """lazy=True defers the directory to the first write, so short runs leave nothing behind."""
        base = sessions_root() if base is None else base
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        session = cls(base / f"{stamp}-{secrets.token_hex(2)}")
        if not lazy:
            session.ensure()
        return session

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
