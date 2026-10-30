"""W15: does a worktree hold work that deleting it would lose? Any doubt counts as yes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git
from .git import WorktreeError

FILE_LIMIT = 10


@dataclass(frozen=True)
class Changes:
    uncommitted: int = 0
    commits: int = 0
    files: tuple[str, ...] = ()
    error: str = ""  # The check failed; uncommitted is then 1.

    @property
    def any(self) -> bool:
        return self.uncommitted > 0 or self.commits > 0

    def describe(self) -> str:
        if self.error:
            return f"无法确认是否有改动（{self.error}），按有改动处理"
        parts = []
        if self.uncommitted:
            parts.append(f"未提交 {self.uncommitted} 个文件")
        if self.commits:
            parts.append(f"独有提交 {self.commits} 个")
        return "，".join(parts) or "无改动"


def count_changes(path: Path, branch: str, skip=()) -> Changes:
    """skip: links and copied files MossCode put there itself."""
    skip = {item.rstrip("/") for item in skip}
    try:
        status = git.run_git(["status", "--porcelain", "--untracked-files=all"], path)
        if status.returncode != 0:
            return Changes(1, 0, (), (status.stderr or "git status 失败").strip()[:200])
        files = []
        for line in status.stdout.splitlines():
            name = line[3:].split(" -> ")[-1].strip().strip('"').rstrip("/")
            # On Windows git walks into a junction, so the files under a link show up one by one.
            if name and not any(name == item or name.startswith(item + "/") for item in skip):
                files.append(name)
        # With --branches the exclude pattern is relative to refs/heads/.
        commits = git.run_git(["rev-list", "--count", "HEAD", "--not", f"--exclude={branch}",
                               "--branches", "--remotes"], path)
        if commits.returncode != 0:
            return Changes(max(len(files), 1), 0, tuple(files[:FILE_LIMIT]),
                           (commits.stderr or "git rev-list 失败").strip()[:200])
        return Changes(len(files), int(commits.stdout.strip() or "0"), tuple(files[:FILE_LIMIT]))
    except (WorktreeError, ValueError) as exc:
        return Changes(1, 0, (), str(exc)[:200])
