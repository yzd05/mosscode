"""Merging a member's branch into the main directory's current branch.

Blocking git work; the runtime runs it in a thread and asks the user in between.
Nothing here ever resets, checks out or cleans files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..worktree.git import WorktreeError, run_git
from ..worktree.meta import load_meta
from .lock import TeamError

if TYPE_CHECKING:
    from ..worktree import WorktreeManager
    from . import TeamStore

COMMIT_LIMIT = 20
STAT_LIMIT = 4000
FILE_LIMIT = 30
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


@dataclass
class MergePlan:
    """What the user confirms, and what the merge needs."""

    member: str
    branch: str
    target: str
    top: Path
    commits: list[str]
    count: int
    stat: str
    untracked: list[str]


def _git(args, top: Path):
    try:
        return run_git(args, top)
    except WorktreeError as exc:
        raise TeamError("main_not_ready", str(exc)) from None


def _output(done) -> str:
    return (done.stderr or done.stdout or "").strip()[:2000]


def git_dir(top: Path) -> Path:
    done = _git(["rev-parse", "--git-dir"], top)
    path = Path(done.stdout.strip())
    return path if path.is_absolute() else top / path


def current_branch(top: Path) -> str:
    """ "" when HEAD is detached or top is no repository."""
    try:
        done = run_git(["symbolic-ref", "-q", "--short", "HEAD"], top)
    except WorktreeError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def unmerged(top: Path, branch: str) -> int:
    """Commits of branch that HEAD does not have; 0 when the branch is gone."""
    done = _git(["rev-list", "--count", f"HEAD..refs/heads/{branch}"], top)
    try:
        return int(done.stdout.strip() or "0") if done.returncode == 0 else 0
    except ValueError:
        return 0


def _main_state(top: Path) -> tuple[str, list[str]]:
    """The target branch and the untracked files; TeamError otherwise."""
    target = current_branch(top)
    if not target:
        raise TeamError("main_not_ready", "主目录的 HEAD 不在分支上（分离 HEAD）；先切换到要合并进去的分支。")
    directory = git_dir(top)
    busy = [name for name in IN_PROGRESS if (directory / name).exists()]
    if busy:
        raise TeamError("main_not_ready", f"主目录有进行中的 git 操作（{'、'.join(busy)}）；先完成或放弃它，再合并。")
    done = _git(["status", "--porcelain", "--untracked-files=all"], top)
    if done.returncode != 0:
        raise TeamError("main_not_ready", f"无法读取主目录的 git 状态：{_output(done)}")
    tracked, untracked = [], []
    for line in done.stdout.splitlines():
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        (untracked if line.startswith("??") else tracked).append(name)
    if tracked:
        shown = "、".join(tracked[:FILE_LIMIT]) + ("…" if len(tracked) > FILE_LIMIT else "")
        raise TeamError(
            "main_dirty",
            f"主目录有未提交的改动（{len(tracked)} 个文件：{shown}）。停下来问用户如何处理"
            "（提交、暂存或放弃由用户决定），不要自己清理。",
        )
    return target, untracked


def check(manager: WorktreeManager, store: TeamStore, name: str, busy: bool) -> MergePlan:
    """In order; the first failing check is the error."""
    top = manager.top
    target, untracked = _main_state(top)
    team = store.load()
    member = team.member(name)
    if member is None:
        raise TeamError(
            "unknown_member", f"团队 {team.name} 中没有成员 {name}；成员：{'、'.join(team.names()) or '无'}。"
        )
    meta = load_meta(manager.meta_path(member.worktree["flat"])) if member.worktree else None
    if meta is None or not Path(meta.path).is_dir():
        raise TeamError("no_worktree", f"成员 {name} 没有工作树，没有可合并的分支。")
    if busy:
        raise TeamError("member_busy", f"成员 {name} 正在运行一轮；等它空闲后再合并。")
    changes = manager.changes(meta)
    if changes.uncommitted:
        files = "、".join(changes.files) or changes.error
        raise TeamError("member_uncommitted", f"成员 {name} 的工作树有未提交的改动（{files}）；让它提交后再合并。")
    count = unmerged(top, meta.branch)
    if count == 0:
        raise TeamError("nothing_to_merge", f"成员 {name} 的分支 {meta.branch} 没有 {target} 中没有的提交。")
    _check_order(top, store, team, name)
    log = _git(["log", "--oneline", "--no-decorate", f"-{COMMIT_LIMIT}", f"HEAD..refs/heads/{meta.branch}"], top)
    stat = _git(["diff", "--stat", f"HEAD...refs/heads/{meta.branch}"], top)
    return MergePlan(
        name,
        meta.branch,
        target,
        top,
        log.stdout.strip().splitlines(),
        count,
        stat.stdout.rstrip()[:STAT_LIMIT],
        untracked,
    )


def _check_order(top: Path, store: TeamStore, team, name: str) -> None:
    """What this member finished waits on another member's work still outside HEAD."""
    tasks = {task.id: task for task in store.board.list()}
    for task in tasks.values():
        if task.owner != name or task.status != "completed":
            continue
        for dep in task.blocked_by:
            other = tasks.get(dep)
            if other is None or not other.owner or other.owner == name:
                continue
            record = team.member(other.owner)
            if record is None or not record.worktree:
                continue
            if unmerged(top, record.worktree["branch"]):
                raise TeamError(
                    "merge_order",
                    f"先合并 {other.owner}：任务 {task.id} 依赖它的任务 {dep}，而它的分支还有未合并的提交。",
                )


def preview(plan: MergePlan) -> str:
    """The confirmation card."""
    more = f"（共 {plan.count} 个，只列前 {COMMIT_LIMIT} 个）" if plan.count > len(plan.commits) else ""
    lines = [f"成员：{plan.member}", f"分支：{plan.branch} → {plan.target}", f"提交{more}："]
    lines += [f"  {line}" for line in plan.commits]
    if plan.stat:
        lines += ["改动：", plan.stat]
    if plan.untracked:
        shown = "、".join(plan.untracked[:FILE_LIMIT]) + ("…" if len(plan.untracked) > FILE_LIMIT else "")
        lines.append(f"主目录中未跟踪的文件（不阻止合并）：{shown}")
    return "\n".join(lines)


def _abort(top: Path) -> None:
    done = _git(["merge", "--abort"], top)
    if done.returncode != 0:
        raise TeamError(
            "merge_abort_failed",
            "合并失败，执行 git merge --abort 也失败了。立即告诉用户，不要再执行任何 git "
            f"写操作。git 输出：{_output(done)}",
        )


def merge(plan: MergePlan) -> tuple[str, dict]:
    top = plan.top
    done = _git(["merge", "--no-ff", "--no-edit", plan.branch], top)
    if done.returncode == 0:
        commit = _git(["rev-parse", "--short", "HEAD"], top).stdout.strip()
        summary = _git(["diff", "--shortstat", "HEAD^1", "HEAD"], top).stdout.strip()
        text = (
            f"已把成员 {plan.member} 的分支 {plan.branch} 合并到 {plan.target}：合并提交 {commit}"
            + (f"，{summary}" if summary else "")
            + "。成员的工作树和分支保留，它可以继续工作；"
            "之后再做改动前，让它先合入 " + plan.target + "。"
        )
        return text, {"member": plan.member, "branch": plan.branch, "commit": commit, "merged": True}
    conflicts = _git(["diff", "--name-only", "--diff-filter=U"], top).stdout.split()
    if conflicts:
        _abort(top)
        shown = "、".join(conflicts[:FILE_LIMIT])
        raise TeamError(
            "merge_conflict",
            f"合并 {plan.branch} 有冲突（{shown}），已放弃这次合并，主目录恢复原状。"
            f"下一步：让成员 {plan.member} 在它的工作树中执行 git merge {plan.target}，"
            "解决冲突并提交后，再调用 team_merge。",
        )
    if (git_dir(top) / "MERGE_HEAD").exists():
        _abort(top)
    raise TeamError("merge_failed", f"git merge 失败，主目录没有改动：{_output(done)}")
