"""Git plumbing for worktrees: run git without prompts, find the repository, read HEAD without git."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT = 120
EXCLUDE_LINE = "/.mosscode/worktrees/"


class WorktreeError(Exception):
    def __init__(self, message: str, code: str = "worktree_error"):
        super().__init__(message)
        self.code = code


def run_git(args, cwd: Path, *, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """No terminal prompt, no stdin; a missing git is a WorktreeError."""
    executable = shutil.which("git")
    if executable is None:
        raise WorktreeError("找不到 git，无法使用工作树。", "worktree_unavailable")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "LC_ALL": "C"}
    try:
        return subprocess.run(
            [executable, "-c", "core.quotepath=false", *args],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise WorktreeError(f"git {args[0]} 超过 {timeout:g} 秒未完成。") from None
    except OSError as exc:
        raise WorktreeError(f"无法运行 git：{exc}", "worktree_unavailable") from None


def git_text(args, cwd: Path) -> str:
    """stdout of a git command that must succeed."""
    done = run_git(args, cwd)
    if done.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} 失败：{(done.stderr or done.stdout).strip()[:500]}")
    return done.stdout.strip()


def find_repo(start: Path) -> tuple[Path, Path]:
    """(top level of the work tree, common git directory)."""
    done = run_git(["rev-parse", "--is-bare-repository", "--show-toplevel", "--git-common-dir"], start)
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or len(lines) < 3:
        if lines[:1] == ["true"]:
            raise WorktreeError("当前仓库是裸仓库，无法使用工作树。", "worktree_unavailable")
        raise WorktreeError("项目目录不在 git 仓库中，无法使用工作树。", "worktree_unavailable")
    if lines[0] == "true":
        raise WorktreeError("当前仓库是裸仓库，无法使用工作树。", "worktree_unavailable")
    top = Path(lines[1]).resolve()
    common = Path(lines[2])
    common = (common if common.is_absolute() else start / common).resolve()
    if run_git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], top).returncode != 0:
        raise WorktreeError("仓库还没有任何提交，无法创建工作树。", "worktree_unavailable")
    return top, common


def ensure_excluded(common: Path) -> bool:
    """True when the line was added."""
    path = common / "info" / "exclude"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    if any(line.strip() == EXCLUDE_LINE for line in text.splitlines()):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(("" if not text or text.endswith("\n") else "\n") + EXCLUDE_LINE + "\n")
    return True


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _resolve_ref(ref: str, gitdir: Path, common: Path) -> str | None:
    for base in (gitdir, common):
        value = _read(base / ref)
        if value:
            return value
    packed = _read(common / "packed-refs") or ""
    for line in packed.splitlines():
        if line and line[0] not in "#^":
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha
    return None


def read_head(worktree: Path) -> tuple[str, str] | None:
    """(commit SHA, ref or "") from .git -> gitdir -> HEAD -> loose ref or packed-refs; no git process."""
    pointer = _read(worktree / ".git")
    if not pointer or not pointer.startswith("gitdir:"):
        return None
    gitdir = Path(pointer.split(":", 1)[1].strip())
    gitdir = gitdir if gitdir.is_absolute() else (worktree / gitdir).resolve()
    head = _read(gitdir / "HEAD")
    if not head:
        return None
    relative = _read(gitdir / "commondir")
    common = (gitdir / relative).resolve() if relative else gitdir
    if head.startswith("ref:"):
        ref = head[4:].strip()
        sha = _resolve_ref(ref, gitdir, common)
        return (sha, ref) if sha and _is_sha(sha) else None
    return (head, "") if _is_sha(head) else None


def _is_sha(value: str) -> bool:
    return len(value) in {40, 64} and all(c in "0123456789abcdef" for c in value)
