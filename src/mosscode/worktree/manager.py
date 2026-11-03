"""Create, resume, check, delete and clean the worktrees MossCode manages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import shutil
import stat
import time

from . import git
from .changes import Changes, count_changes
from .git import WorktreeError
from .meta import WorktreeMeta, load_meta, process_alive, save_meta
from .names import EPHEMERAL, branch_of, ephemeral_name, flatten, validate_name
from .setup import copy_included, copy_local, create_links, is_link, unlink

WORKTREES = Path(".mosscode") / "worktrees"
META = ".meta"
CLEAN_INTERVAL = 24 * 3600
DEFAULT_STALE_HOURS = 24.0


class WorktreeChanged(WorktreeError):
    def __init__(self, name: str, changes: Changes):
        files = "、".join(changes.files)
        super().__init__(f"工作树 {name} 有改动（{changes.describe()}{'：' + files if files else ''}），未删除；"
                         "确认丢弃时加 --discard。", "worktree_changed")
        self.changes = changes


@dataclass(frozen=True)
class Created:
    meta: WorktreeMeta
    warnings: tuple[str, ...] = ()
    resumed: bool = False
    dirty: bool = False  # The main directory had uncommitted changes.


@dataclass(frozen=True)
class Finished:
    kept: bool
    changes: Changes
    error: str = ""  # No changes, but deleting failed.


@dataclass(frozen=True)
class Entry:
    meta: WorktreeMeta | None
    name: str
    status: str


@dataclass
class CleanReport:
    removed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _writable(function, path, _exc):
    """Git objects are read-only on Windows."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


class WorktreeManager:
    def __init__(self, top: Path, common: Path, project: Path | None = None, *, links=(),
                 stale_hours: float = DEFAULT_STALE_HOURS):
        self.top, self.common = top.resolve(), common.resolve()
        self.project = (project or top).resolve()
        self.links = tuple(links)
        self.stale_hours = stale_hours
        self._lock = asyncio.Lock()  # Creating and deleting run one at a time.

    @classmethod
    def discover(cls, project: Path, **options) -> WorktreeManager:
        top, common = git.find_repo(project)
        return cls(top, common, project, **options)

    # Paths ---------------------------------------------------------------------------------

    @property
    def base(self) -> Path:
        return self.top / WORKTREES

    @property
    def meta_dir(self) -> Path:
        return self.base / META

    def meta_path(self, flat: str) -> Path:
        return self.meta_dir / f"{flat}.json"

    @property
    def project_part(self) -> str:
        """Where the project sits inside the repository ("" at the top level)."""
        try:
            relative = self.project.relative_to(self.top).as_posix()
        except ValueError:
            return ""
        return "" if relative == "." else relative

    def root_in(self, meta: WorktreeMeta) -> Path:
        part = self.project_part
        return Path(meta.path) / part if part else Path(meta.path)

    def display(self, path: Path | str) -> str:
        path = Path(path)
        try:
            return path.relative_to(self.project).as_posix()
        except ValueError:
            return str(path)

    def metas(self) -> list[tuple[Path, WorktreeMeta]]:
        try:
            files = sorted(self.meta_dir.glob("*.json"))
        except OSError:
            return []
        found = [(path, load_meta(path)) for path in files]
        return [(path, meta) for path, meta in found if meta is not None]

    # Creating ------------------------------------------------------------------------------

    async def create(self, name: str, *, manual: bool = True, task: str = "") -> Created:
        async with self._lock:
            return await asyncio.to_thread(self._create, name, manual, task)

    async def ephemeral(self, task: str) -> Created:
        """A name nobody chose, marked in use by this process."""
        async with self._lock:
            for _ in range(5):
                name = ephemeral_name()
                if not (self.base / name).exists() and not self.meta_path(name).exists():
                    return await asyncio.to_thread(self._create, name, False, task)
            raise WorktreeError("无法生成不重复的工作树名称。")

    def _create(self, name: str, manual: bool, task: str) -> Created:
        problem = validate_name(name)
        if problem:
            raise WorktreeError(f"名称 {name} 不可用：{problem}。", "invalid_name")
        flat = flatten(name)
        path, meta_path = self.base / flat, self.meta_path(flat)
        self._check_case(flat)
        if path.exists():
            return self._resume(flat, path, meta_path)
        leftover = load_meta(meta_path)
        if leftover is not None:  # The directory is gone: finish what the record says.
            self._forget(leftover, meta_path)
        git.ensure_excluded(self.common)
        base = git.git_text(["rev-parse", "HEAD"], self.top)
        branch = branch_of(flat)
        if git.run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], self.top).returncode == 0:
            raise WorktreeError(f"分支 {branch} 已存在；换一个名称，或先删除这个分支。", "branch_exists")
        dirty = bool(git.git_text(["status", "--porcelain"], self.top))
        self.base.mkdir(parents=True, exist_ok=True)
        done = git.run_git(["worktree", "add", "-b", branch, "--", str(path), base], self.top)
        if done.returncode != 0:
            self._undo(path, branch)
            raise WorktreeError(f"git worktree add 失败：{(done.stderr or done.stdout).strip()[:500]}")
        in_use = {"pid": os.getpid(), "task": task} if task else None
        meta = WorktreeMeta(name, flat, str(path), branch, base, _now(), manual, in_use=in_use)
        save_meta(meta, meta_path)
        warnings = self._setup(meta, meta_path)
        return Created(meta, tuple(warnings), False, dirty)

    def _check_case(self, flat: str) -> None:
        """Windows file names ignore case, so names must too."""
        folded = flat.casefold()
        names = {meta.flat for _, meta in self.metas()}
        try:
            names.update(child.name for child in self.base.iterdir() if child.name != META)
        except OSError:
            pass
        clash = next((other for other in names if other != flat and other.casefold() == folded), None)
        if clash is not None:
            raise WorktreeError(f"名称与已有的工作树 {clash} 只差大小写。", "invalid_name")

    def _undo(self, path: Path, branch: str) -> None:
        if path.exists() and not is_link(path):
            shutil.rmtree(path, onexc=_writable)
        git.run_git(["worktree", "prune"], self.top)
        git.run_git(["branch", "-D", branch], self.top)

    def _setup(self, meta: WorktreeMeta, meta_path: Path) -> list[str]:
        """Links, copies and the setup command in order; the record says when it is complete."""
        warnings: list[str] = []
        path = Path(meta.path)
        def copied(items) -> None:
            meta.copied.extend(item for item in items if item not in meta.copied)

        steps = (lambda: copied(copy_local(self.top, path, self.project_part, warnings)),
                 lambda: meta.links.extend(item for item in create_links(self.top, path, self.links, warnings)
                                           if item not in meta.links),
                 lambda: copied(copy_included(self.top, path, meta.links, warnings)))
        for step in steps:
            try:
                step()
            except Exception as exc:  # Best effort: a failed step never fails the worktree.
                warnings.append(f"初始化未完成一步：{exc}")
        if (self.top / ".gitmodules").is_file():
            warnings.append("仓库有子模块，工作树中的子模块未初始化。")
        meta.setup_done = True
        save_meta(meta, meta_path)
        return warnings

    def _resume(self, flat: str, path: Path, meta_path: Path) -> Created:
        """The filesystem only, no git process."""
        head = git.read_head(path)
        meta = load_meta(meta_path)
        if head is None or meta is None or meta.flat != flat:
            raise WorktreeError(f"{self.display(path)} 存在，但不是有效的 MossCode 工作树；"
                                f"用 /worktree remove {flat} --discard 清理，或手动处理。", "worktree_invalid")
        if meta.removing:
            raise WorktreeError(f"工作树 {meta.name} 的删除尚未完成；用 /worktree remove {meta.name} 再试。",
                                "worktree_invalid")
        warnings = []
        if head[1] != f"refs/heads/{meta.branch}":
            warnings.append(f"工作树当前不在分支 {meta.branch} 上。")
        if not meta.setup_done:
            warnings.extend(self._setup(meta, meta_path))
        return Created(meta, tuple(warnings), True, False)

    # Changes and deleting ------------------------------------------------------------------

    def changes(self, meta: WorktreeMeta) -> Changes:
        path = Path(meta.path)
        if path.is_dir():
            return count_changes(path, meta.branch, (*meta.links, *meta.copied))
        # The directory is gone: only the branch can still hold work.
        if git.run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{meta.branch}"], self.top).returncode != 0:
            return Changes()
        done = git.run_git(["rev-list", "--count", f"refs/heads/{meta.branch}", "--not",
                            f"--exclude={meta.branch}", "--branches", "--remotes"], self.top)
        if done.returncode != 0:
            return Changes(1, 0, (), done.stderr.strip()[:200] or "git rev-list 失败")
        return Changes(0, int(done.stdout.strip() or "0"))

    async def remove(self, name: str, *, discard: bool = False) -> Changes:
        async with self._lock:
            return await asyncio.to_thread(self._remove, name, discard)

    def _remove(self, name: str, discard: bool) -> Changes:
        problem = validate_name(name)
        if problem:
            raise WorktreeError(f"名称 {name} 不可用：{problem}。", "invalid_name")
        flat = flatten(name)
        path, meta_path = self.base / flat, self.meta_path(flat)
        meta = load_meta(meta_path)
        if meta is None:
            if path.exists():
                raise WorktreeError(f"{self.display(path)} 没有 MossCode 的记录，不会自动删除；请手动处理。",
                                    "worktree_invalid")
            raise WorktreeError(f"没有名为 {name} 的工作树。", "unknown_worktree")
        if meta.in_use and process_alive(int(meta.in_use.get("pid", 0))):
            task = meta.in_use.get("task") or "?"
            raise WorktreeError(f"工作树 {name} 正被子 Agent 使用（任务 {task}），结束后再删除。", "worktree_in_use")
        changes = Changes() if meta.removing else self.changes(meta)
        if changes.any and not discard:
            raise WorktreeChanged(name, changes)
        self._delete(meta, meta_path)
        return changes

    def _forget(self, meta: WorktreeMeta, meta_path: Path) -> None:
        """A record whose directory is gone: drop it; the branch stays unless it holds nothing."""
        drop = meta.removing or not self.changes(meta).any
        git.run_git(["worktree", "prune"], self.top)
        if drop:
            git.run_git(["branch", "-D", meta.branch], self.top)
        meta_path.unlink(missing_ok=True)

    def _delete(self, meta: WorktreeMeta, meta_path: Path) -> None:
        """After the change check passed or the user discarded. Never `git worktree remove`."""
        path = Path(meta.path).resolve()
        if path.parent != self.base.resolve():
            raise WorktreeError(f"记录中的路径 {path} 不在 {self.display(self.base)} 中，拒绝删除。", "worktree_invalid")
        meta.removing, meta.in_use = True, None
        save_meta(meta, meta_path)  # An interrupted deletion is retried, never re-checked into "changed".
        for relative in meta.links:
            link = path / relative
            if is_link(link):
                try:
                    unlink(link)
                except OSError:
                    pass  # rmtree below does not follow links either.
        if path.exists():
            if is_link(path):
                raise WorktreeError(f"{self.display(path)} 是一个链接，拒绝删除。", "worktree_invalid")
            try:
                shutil.rmtree(path, onexc=_writable)
            except OSError as exc:
                raise WorktreeError(f"删除 {self.display(path)} 未完成：{exc.strerror or exc}；"
                                    f"关闭占用它的程序后用 /worktree remove {meta.name} 重试。",
                                    "remove_failed") from None
        git.run_git(["worktree", "prune"], self.top)
        git.run_git(["branch", "-D", meta.branch], self.top)
        meta_path.unlink(missing_ok=True)

    # Sub-agents ----------------------------------------------------------------------------

    async def finish(self, meta: WorktreeMeta) -> Finished:
        """No changes -> delete; changes -> keep. Always clears in_use."""
        async with self._lock:
            return await asyncio.to_thread(self._finish, meta)

    def _finish(self, meta: WorktreeMeta) -> Finished:
        meta_path = self.meta_path(meta.flat)
        current = load_meta(meta_path) or meta
        current.in_use = None
        save_meta(current, meta_path)
        changes = self.changes(current)
        if changes.any:
            return Finished(True, changes)
        try:
            self._delete(current, meta_path)
        except WorktreeError as exc:
            return Finished(False, changes, str(exc))
        return Finished(False, changes)

    async def mark_in_use(self, flat: str, task: str = "", pid: int | None = None) -> None:
        """A team member's worktree is in use while the member runs; "" clears it. Never deletes.

        pid: the process running the member (a pane member's own), this one by default."""
        async with self._lock:
            meta_path = self.meta_path(flat)
            meta = load_meta(meta_path)
            if meta is not None:
                meta.in_use = {"pid": pid or os.getpid(), "task": task} if task else None
                await asyncio.to_thread(save_meta, meta, meta_path)

    def report(self, meta: WorktreeMeta, finished: Finished) -> str:
        """The lines after a sub-agent's result."""
        if not finished.kept:
            if finished.error:
                return f"[工作树] 无改动，但删除未完成：{finished.error}"
            return "[工作树] 无改动，已删除。"
        where = self.display(meta.path)
        return (f"[工作树] 已保留：{where}\n"
                f"分支 {meta.branch}，基于 {meta.base[:7]}；{finished.changes.describe()}。\n"
                f"合并：git merge {meta.branch}；查看：git -C {where} status；删除：/worktree remove {meta.name}")

    # Listing and cleaning ------------------------------------------------------------------

    async def entries(self) -> list[Entry]:
        return await asyncio.to_thread(self._entries)

    def _entries(self) -> list[Entry]:
        found, known = [], set()
        for _, meta in self.metas():
            known.add(meta.flat)
            found.append(Entry(meta, meta.name, self._status(meta)))
        try:
            others = sorted(child.name for child in self.base.iterdir() if child.is_dir() and child.name != META)
        except OSError:
            others = []
        found.extend(Entry(None, name, "未登记（没有 MossCode 的记录）") for name in others if name not in known)
        return found

    def _status(self, meta: WorktreeMeta) -> str:
        if meta.removing:
            return "删除未完成"
        if meta.in_use and process_alive(int(meta.in_use.get("pid", 0))):
            return f"使用中（任务 {meta.in_use.get('task') or '?'}）"
        if not Path(meta.path).is_dir():
            return "目录不存在"
        changes = self.changes(meta)
        return f"有改动（{changes.describe()}）" if changes.any else "无改动"

    async def clean(self, hours: float | None = None) -> CleanReport:
        async with self._lock:
            return await asyncio.to_thread(self._clean, self.stale_hours if hours is None else hours)

    def _clean(self, hours: float) -> CleanReport:
        """Only records MossCode made for sub-agents, not in use, old, and without changes."""
        report = CleanReport()
        cutoff = time.time() - hours * 3600
        for meta_path, meta in self.metas():
            if meta.manual:
                report.skipped.append((meta.name, "手动创建"))
                continue
            if not EPHEMERAL.fullmatch(meta.name):
                report.skipped.append((meta.name, "名称不是临时格式"))
                continue
            if meta.in_use and process_alive(int(meta.in_use.get("pid", 0))):
                report.skipped.append((meta.name, "使用中"))
                continue
            try:
                if meta_path.stat().st_mtime > cutoff:
                    report.skipped.append((meta.name, "未过期"))
                    continue
            except OSError:
                continue
            if not meta.removing:
                changes = self.changes(meta)
                if changes.any:
                    report.skipped.append((meta.name, f"有改动（{changes.describe()}）"))
                    continue
            try:
                self._delete(meta, meta_path)
            except WorktreeError as exc:
                report.skipped.append((meta.name, str(exc)))
                continue
            report.removed.append(meta.name)
        try:
            git.run_git(["worktree", "prune"], self.top)
        except WorktreeError:
            pass
        return report

    async def keep_clean(self, on_report, *, delay: float = 5.0, interval: float = CLEAN_INTERVAL) -> None:
        """In the background, after start and then every day; nothing when turned off."""
        if self.stale_hours <= 0:
            return
        await asyncio.sleep(delay)
        while True:
            try:
                report = await self.clean()
            except Exception:  # A broken repository must not take the session down.
                report = None
            if report is not None and report.removed:
                on_report(report)
            await asyncio.sleep(interval)
