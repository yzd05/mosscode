"""Make a fresh worktree usable. Every step is best effort; failures become warnings."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil

from . import git
from .git import WorktreeError

# MossCode's own local files; nothing with secrets unless .worktreeinclude lists it.
LOCAL_FILES = (".mosscode/permissions.local.yaml", ".mosscode/hooks.local.yaml")
INCLUDE_FILE = ".worktreeinclude"
WORKTREES = ".mosscode/worktrees/"


def copy_local(top: Path, worktree: Path, project: str, warnings: list[str]) -> list[str]:
    """Relative to the project directory (MossCode may run in a subdirectory of the repository)."""
    copied = []
    for name in LOCAL_FILES:
        relative = str(PurePosixPath(project) / name) if project else name
        if _copy(top / relative, worktree / relative, relative, warnings):
            copied.append(relative)
    return copied


def _copy(source: Path, target: Path, label: str, warnings: list[str]) -> bool:
    if not source.is_file() or target.exists() or target.is_symlink():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True
    except OSError as exc:
        warnings.append(f"未能复制 {label}：{exc.strerror or exc}")
        return False


def _junction(source: Path, target: Path) -> None:
    import _winapi
    _winapi.CreateJunction(str(source), str(target))


def create_links(top: Path, worktree: Path, links, warnings: list[str]) -> list[str]:
    """Symbolic link first; a directory falls back to a junction where Windows refuses the privilege."""
    made = []
    for relative in links:
        source, target = top / relative, worktree / relative
        if not source.exists() or target.exists() or target.is_symlink():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(source, target, target_is_directory=source.is_dir())
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314 and source.is_dir():
                try:
                    _junction(source, target)
                except OSError as inner:
                    warnings.append(f"未能链接 {relative}：{inner.strerror or inner}")
                    continue
            else:
                reason = "Windows 上创建文件的符号链接需要特权" if getattr(exc, "winerror", None) == 1314 \
                    else (exc.strerror or str(exc))
                warnings.append(f"未能链接 {relative}：{reason}")
                continue
        made.append(relative)
    return made


def unlink(path: Path) -> None:
    """Remove a link itself, never what it points to."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)  # A junction or a directory symlink on Windows.


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def copy_included(top: Path, worktree: Path, skip, warnings: list[str]) -> list[str]:
    """Git itself matches the gitignore-style patterns of .worktreeinclude against untracked files."""
    include = top / INCLUDE_FILE
    if not include.is_file():
        return []
    try:
        listed = git.run_git(["ls-files", "--others", "--ignored", f"--exclude-from={include}", "--",
                              ".", f":(exclude){WORKTREES.rstrip('/')}"], top)
    except WorktreeError as exc:
        warnings.append(f"未能读取 {INCLUDE_FILE} 匹配的文件：{exc}")
        return []
    if listed.returncode != 0:
        warnings.append(f"未能读取 {INCLUDE_FILE} 匹配的文件：{listed.stderr.strip()[:200]}")
        return []
    skip = tuple(item.rstrip("/") + "/" for item in skip)
    copied = []
    for relative in listed.stdout.splitlines():
        relative = relative.strip()
        if not relative or relative.startswith(WORKTREES) or (relative + "/").startswith(skip):
            continue
        if _copy(top / relative, worktree / relative, relative, warnings):
            copied.append(relative)
    return copied
