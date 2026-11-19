"""Per-project storage under ~/.mosscode, shared by session transcripts and memory notes."""

import hashlib
import os
import re
import secrets
import shutil
import stat
from contextlib import suppress
from datetime import datetime
from pathlib import Path

SESSION_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_GIT_POINTER_LIMIT = 4096
_MIGRATED_MARKER = ".migrated"


def mosscode_home(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".mosscode"


def project_key(root: Path) -> str:
    """Readable tail of the resolved path plus a hash, so a-b and a/b never share a key."""
    path = str(Path(root).resolve())
    if os.name == "nt":
        path = path.casefold()
    readable = re.sub(r"[^A-Za-z0-9]", "-", path)[-48:]
    return f"{readable}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:8]}"


def project_dir(root: Path, home: Path | None = None) -> Path:
    return mosscode_home(home) / "projects" / project_key(root)


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _read_git_pointer(path: Path) -> str | None:
    """Read the first line of a small regular git metadata file, rejecting symlinks and binary/oversized files."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _GIT_POINTER_LIMIT:
            return None
        with path.open("rb") as handle:
            raw = handle.read(_GIT_POINTER_LIMIT + 1)
    except OSError:
        return None
    if len(raw) > _GIT_POINTER_LIMIT or b"\0" in raw:
        return None
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    return lines[0].strip() if lines and lines[0].strip() else None


def git_common_root(root: Path) -> Path:
    """Resolve the canonical repository root across git worktrees via pure filesystem inspection.

    Validates standard gitrepository-layout(5) worktree backlinks (`<common>/HEAD` and
    `<common>/worktrees/<name>/gitdir` pointing back to this worktree's `.git` file) so a crafted
    `.git` file inside an untrusted archive cannot redirect memory to another repository.
    """
    try:
        resolved = Path(root).resolve()
    except OSError:
        return Path(root)
    for candidate in (resolved, *resolved.parents):
        dot_git = candidate / ".git"
        try:
            st = os.lstat(dot_git)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return resolved
        if stat.S_ISDIR(st.st_mode):
            return candidate
        if not stat.S_ISREG(st.st_mode):
            return resolved
        try:
            header = _read_git_pointer(dot_git)
            if not header or not header.startswith("gitdir:"):
                return resolved
            gitdir_raw = header[len("gitdir:") :].strip()
            if not gitdir_raw:
                return resolved
            gitdir_ref = candidate / gitdir_raw
            if gitdir_ref.is_symlink():
                return resolved
            gitdir = gitdir_ref.resolve()
            if not gitdir.is_dir():
                return resolved
            common_raw = _read_git_pointer(gitdir / "commondir")
            if not common_raw:
                return resolved
            common_ref = gitdir / common_raw
            if common_ref.is_symlink():
                return resolved
            common = common_ref.resolve()
            if common.name != ".git" or not common.is_dir() or not common.parent.is_dir():
                return resolved
            head_path = common / "HEAD"
            if head_path.is_symlink() or not head_path.is_file():
                return resolved
            worktrees_dir = (common / "worktrees").resolve()
            if not _same_path(gitdir.parent, worktrees_dir):
                return resolved
            backlink_raw = _read_git_pointer(gitdir / "gitdir")
            if not backlink_raw:
                return resolved
            backlink = (gitdir / backlink_raw).resolve()
            if not _same_path(backlink, dot_git.resolve()):
                return resolved
            return common.parent.resolve()
        except (OSError, ValueError):
            return resolved
    return resolved


def _note_files(directory: Path) -> list[Path]:
    try:
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.glob("*.md") if p.name != "MEMORY.md" and not p.is_symlink() and p.is_file())
    except OSError:
        return []


def memory_project_dir(root: Path, home: Path | None = None) -> Path:
    """Shared project memory directory across git worktrees, migrating legacy per-worktree notes once."""
    resolved = Path(root).resolve()
    common = git_common_root(resolved)
    primary = project_dir(common, home) / "memory"
    if _same_path(common, resolved):
        return primary
    legacy = project_dir(resolved, home) / "memory"
    try:
        if not legacy.is_dir() or (legacy / _MIGRATED_MARKER).exists():
            return primary
    except OSError:
        return primary
    legacy_notes = _note_files(legacy)
    if not legacy_notes:
        return primary
    try:
        primary.mkdir(parents=True, exist_ok=True)
        for src in legacy_notes:
            dst = primary / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
    except OSError:
        return primary if _note_files(primary) else legacy
    with suppress(OSError):
        (legacy / _MIGRATED_MARKER).write_text("1\n", encoding="utf-8")
    return primary


def new_session_id(now: datetime | None = None) -> str:
    return f"{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
