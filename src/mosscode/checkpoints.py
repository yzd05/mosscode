"""File snapshots taken just before write_file / edit_file change a file, so /rewind can put it back.

Each typed message starts a turn; the first write to a path in a turn saves what the path held
(its bytes, or that it did not exist) under that message. Restoring to message M writes back, for every
path recorded after M was sent, its first snapshot after M: the state just before M's turn touched it.
"""

import hashlib
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

TOO_LARGE = 10 * 1024 * 1024
DIGITS = 32  # Of the SHA-256, as for images: short enough to keep blob paths under Windows' limit.


@dataclass(frozen=True)
class Snapshot:
    message: str  # The id of the latest typed message when the file was first written.
    path: str  # Relative to the workspace (with /), or absolute outside it.
    blob: str | None  # SHA-256 prefix naming the content's blob; None: the file did not exist.
    too_large: bool = False  # Over TOO_LARGE: nothing was saved and it cannot be restored.


@dataclass
class Restoration:
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, why)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, error)

    @property
    def paths(self) -> list[str]:
        """What now differs from what the model last saw."""
        return [*self.restored, *self.deleted]


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc) or type(exc).__name__


class Checkpoints:
    def __init__(
        self,
        root: Path,
        session,
        *,
        record: Callable[[Snapshot], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ):
        self.root = root.resolve()
        self.session = session  # Blobs go in <session directory>/checkpoints.
        self.blobs = session.directory / "checkpoints"
        self.record = record  # Appends the snapshot to the transcript.
        self.on_notice = on_notice
        self.turn: str | None = None
        self.snapshots: list[Snapshot] = []
        self.marks: dict[str, int] = {}
        self._seen: set[str] = set()  # Paths already saved this turn.
        self._failed = False
        self._lock = threading.Lock()  # Sub-agents write from their own tool threads.

    def load(self, snapshots, marks) -> None:
        """A resumed session's checkpoints; the next typed message starts a new turn."""
        with self._lock:
            self.snapshots, self.marks = list(snapshots), dict(marks)
            self.turn, self._seen = None, set()

    def begin(self, message: str) -> None:
        with self._lock:
            self.turn = message
            self.marks[message] = len(self.snapshots)
            self._seen = set()

    def _name(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _target(self, name: str) -> Path:
        return self.root / name  # An absolute name stays itself.

    def before_write(self, path: Path) -> None:
        """Called in the tool's thread after its checks pass and right before it writes path."""
        key = os.path.normcase(str(path))
        with self._lock:
            if self.turn is None or key in self._seen:
                return
            self._seen.add(key)
            turn = self.turn
        try:
            snapshot = self._save(turn, path)
        except OSError as exc:
            self._fail(f"检查点保存失败（{self._name(path)}：{_reason(exc)}），这次改动之后不能用 /rewind 恢复它。")
            return
        with self._lock:
            self.snapshots.append(snapshot)
            if self.record is not None:
                self.record(snapshot)

    def _save(self, turn: str, path: Path) -> Snapshot:
        name = self._name(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return Snapshot(turn, name, None)
        if size > TOO_LARGE:
            return Snapshot(turn, name, None, too_large=True)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:DIGITS]
        blob = self.blobs / digest
        if not blob.is_file():
            self.session.ensure()  # Marks the directory as ours, so pruning removes it with the transcript.
            self.blobs.mkdir(exist_ok=True)
            partial = self.blobs / f".{secrets.token_hex(4)}.tmp"
            try:
                partial.write_bytes(data)
                os.replace(partial, blob)
            finally:
                partial.unlink(missing_ok=True)
        return Snapshot(turn, name, digest)

    def _fail(self, text: str) -> None:
        """The write still goes ahead; the user hears about the first failure only."""
        if self._failed:
            return
        self._failed = True
        if self.on_notice is not None:
            self.on_notice(text)

    def changed_after(self, message: str) -> list[Snapshot]:
        """Each path recorded since message was sent, with its first snapshot since then."""
        with self._lock:
            start = self.marks.get(message)
            later = [] if start is None else self.snapshots[start:]
        first: dict[str, Snapshot] = {}
        for snapshot in later:
            first.setdefault(os.path.normcase(snapshot.path), snapshot)
        return list(first.values())

    def restore(self, message: str) -> Restoration:
        """Every snapshot is resolved first; then each file on its own, so one failure leaves the rest."""
        from .tools.files import atomic_write

        result = Restoration()
        plan: list[tuple[Snapshot, bytes | None]] = []
        for snapshot in self.changed_after(message):
            if snapshot.too_large:
                result.skipped.append((snapshot.path, f"超过 {TOO_LARGE // 1024 // 1024} MB，没有保存快照"))
                continue
            if snapshot.blob is None:
                plan.append((snapshot, None))
                continue
            try:
                plan.append((snapshot, (self.blobs / snapshot.blob).read_bytes()))
            except OSError:
                result.skipped.append((snapshot.path, "快照文件已丢失"))
        for snapshot, data in plan:
            target = self._target(snapshot.path)
            try:
                if data is None:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                        result.deleted.append(snapshot.path)
                    continue
                if target.is_file() and target.read_bytes() == data:
                    continue
                atomic_write(target, data)
                result.restored.append(snapshot.path)
            except OSError as exc:
                result.failed.append((snapshot.path, _reason(exc)))
        return result
