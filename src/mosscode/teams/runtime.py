"""Ch15: the lead's side of a team — the current team, its members (in-process or in panes) and the lead's mailbox."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Callable

from ..messages import ProviderError, UsageTotals
from ..permissions import ApprovalRequest
from ..worktree.meta import process_alive
from . import LEAD, Member, TeamError, TeamStore, list_teams, teams_root, validate_member_name
from .backends import (IN_PROCESS, IN_PROCESS_REASON, LABELS, START_TIMEOUT, Choice, PaneBackend, detect_backend,
                       member_alive, member_arguments, pane_backend)
from .board import STATUS_LABELS
from .lock import now
from .mailbox import Message
from .member import POLL, MemberPolicy, MemberRunner, TeamAccess, addendum, task_listing

if TYPE_CHECKING:
    from ..conversation import Conversation

MAX_MEMBERS = 4
BODY_PREVIEW = 4000  # LD3: characters of a message body in a team update.
REASON = IN_PROCESS_REASON
BACKEND_LABELS = LABELS
STATUS_NAMES = {"running": "运行中", "idle": "空闲", "stopped": "已停止"}
STOP_WAIT = 10.0  # ID5/LD6: seconds a member has to stop after a shutdown_request.
IDLE_STATES = {"failed": "失败", "interrupted": "被用户中断"}  # MB2: idle_notification reasons.
LEAD_GUIDE = """## Agent 团队

你可以作为 lead 组建团队：成员是长期存在的 Agent，各自在独立的 git 工作树中工作，通过消息和共享任务协作。
- 何时使用：用户的任务能拆成几块可以并行、彼此独立的多文件改动时。一处小改动、一个问题或只需要调查时不要建团队，直接做或用子 Agent。
- 做法：team_create 建团队；先用 team_task_create 建好任务并写明依赖（blocked_by），再用 team_spawn 创建成员（至多 {max_members} 个），在 prompt 中写清它负责的任务、背景、范围和完成后要提交改动。
- 创建后不要轮询、不要反复查看任务列表：成员的消息、空闲通知和新就绪的任务会作为 <team-update> 自动送达。需要时用 send_message 回复或指派。
- 全部成员完成后，用 team_merge 按任务依赖的顺序逐个合并它们的分支；每次合并都要用户确认。
- 合并冲突时：让该成员在它的工作树中合入主分支、解决冲突并提交，再 team_merge。主目录有未提交的改动、或用户拒绝合并时，停下来问用户，不要自己清理或重试。
- 最后向用户汇报：每个成员做了什么、合并了哪些分支、还剩什么问题。"""


def remove_tree(path: Path) -> None:
    """Windows: a reader may hold a file for a moment; retry briefly."""
    for attempt in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def render_update(team: str, messages: list[Message], ready: list[str]) -> str:
    """LD3: one notification for everything that arrived in the lead's mailbox."""
    lines = [f"<team-update>\n团队 {team}："]
    for message in messages:
        extra, sender = message.extra, message.sender
        if message.type == "text":
            body = message.body
            if len(body) > BODY_PREVIEW:
                body = body[:BODY_PREVIEW] + f"\n（正文共 {len(message.body)} 字，已截断）"
            lines.append(f"[{sender} 的消息] 摘要：{message.summary}\n{body}")
        elif message.type == "plan_approval_request":
            lines.append(f"[{sender} 提交了计划，request_id={extra.get('request_id')}]\n{extra.get('plan', '')}\n"
                         f"用 send_message（to: {sender}，type: plan_approval_response，request_id，approve，feedback）答复。")
        elif message.type == "idle_notification":
            tasks = "、".join(extra.get("completed_tasks") or [])
            state = IDLE_STATES.get(extra.get("reason"), "空闲")
            lines.append(f"{sender} {state}" + (f"，完成任务 {tasks}" if tasks else "")
                         + (f"。最后回复：{message.summary}" if message.summary else "。"))
        elif message.type == "shutdown_response":
            verdict = "同意停止" if extra.get("approve") else f"拒绝停止：{extra.get('reason') or '未说明'}"
            lines.append(f"{sender} {verdict}。")
        else:
            lines.append(f"[{sender} · {message.type}] {message.body}")
    if ready:
        lines.append("现在就绪的任务：" + "、".join(ready) + "。")
    return "\n".join(lines) + "\n</team-update>\n以上是团队的最新消息；需要时用 send_message 回复成员，不要轮询。"


def note_for(message: Message) -> str:
    """LD4: one line in the chat for each update."""
    sender, extra = message.sender, message.extra
    if message.type == "text":
        return f"{sender} → lead：{message.summary}"
    if message.type == "plan_approval_request":
        return f"成员 {sender} 提交了计划，等待 Lead 审批"
    if message.type == "idle_notification":
        tasks = "、".join(extra.get("completed_tasks") or [])
        state = IDLE_STATES.get(extra.get("reason"), "空闲")
        return f"{sender} {state}" + (f"：完成任务 {tasks}" if tasks else "")
    if message.type == "shutdown_response":
        return f"{sender} {'已停止' if extra.get('approve') else '拒绝停止'}"
    return f"{sender}：{message.type}"


def label_of(runner) -> str:
    return BACKEND_LABELS.get(getattr(runner, "backend_name", IN_PROCESS), IN_PROCESS)


class PaneRunner:
    """BE4–BE7: the lead's view of a member in a pane of its own. It polls its mailbox itself; its
    record in config.json says how it is doing, and its process ending is the member stopping."""

    def __init__(self, store: TeamStore, name: str, backend: PaneBackend, pid: int, pane: str | None, *,
                 status: str = "idle", poll: float = POLL):
        self.store, self.name, self.backend = store, name, backend
        self.backend_name = backend.name
        self.pid, self.pane, self.poll = pid, pane, poll
        self.status, self.last_tool = status, ""
        self.on_status: Callable[[str, dict], None] | None = None
        self.job: asyncio.Task | None = None

    def wake(self) -> None:
        """BE4: nothing to do; it reads its mailbox every POLL seconds."""

    def start(self) -> asyncio.Task:
        self.job = asyncio.get_running_loop().create_task(self.watch())
        return self.job

    def _report(self, status: str) -> None:
        self.status = status
        if self.on_status is not None:
            self.on_status(status, {})

    async def watch(self) -> None:
        try:
            while process_alive(self.pid):
                await asyncio.sleep(self.poll)
                try:
                    team = await asyncio.to_thread(self.store.load)
                except TeamError:
                    continue
                member = team.member(self.name)
                if member is None or member.status == "stopped" or (member.pid and member.pid != self.pid):
                    return
                self.last_tool = member.last_tool if member.status == "running" else ""
                if member.status != self.status:
                    self._report(member.status)
        finally:
            self._report("stopped")

    async def stop(self) -> None:
        """BE7 step 3: end its process tree (and its tmux pane)."""
        if self.job is not None and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await asyncio.to_thread(self.backend.kill, self.pid if process_alive(self.pid) else None, self.pane)

    async def close(self) -> None:
        pass


async def build_runner(conversation: Conversation, store: TeamStore, member: Member, agent, root: Path | None,
                       relative: str, *, wake: Callable[[str], None] | None = None,
                       on_approval: Callable[[ApprovalRequest], None] | None = None, poll: float = POLL,
                       session_mode: Callable[[], str] | None = None) -> MemberRunner:
    """RT2: the definition's prompt plus the team addendum, tools rooted in its worktree. The lead builds
    in-process members with it; a pane member's process builds itself with it (session_mode from its record)."""
    from ..agent import AgentLoop, add_usage
    from ..agents.runtime import GLOBAL_BAN, SubagentLog, effective_mode
    from ..prompts import git_snapshot
    from ..tools.team import member_tools
    main = conversation._agent
    parent = main.registry.permissions
    access = TeamAccess(store, member.name, wake)
    session_mode = session_mode or (lambda: parent.mode)
    runner = MemberRunner(access, member.name, agent_mode=lambda mode: effective_mode(agent, mode),
                          session_mode=session_mode, require_plan_approval=member.require_plan_approval,
                          on_approval=on_approval, poll=poll)
    provider, budget, owned = conversation.subagents._provider(agent.model, f"成员 {member.name} ")
    mode = "plan" if member.require_plan_approval else effective_mode(agent, session_mode())
    policy = MemberPolicy(parent, mode, interactive=parent.interactive, label=f"成员 {member.name}", root=root)
    base = root or parent.root
    extra = member_tools(base, access, runner)
    keep = lambda name: name not in GLOBAL_BAN and agent.allows(name)  # noqa: E731
    if root is None:
        registry = main.registry.for_agent(agent="teammate", agent_type=agent.name, permissions=policy, keep=keep,
                                           extra=extra)
        git = main.git
    else:
        from ..tools import create_default_registry
        shell = getattr(main.registry.get("execute_command"), "shell", None)
        fresh = create_default_registry(root, permissions=policy, shell=shell if shell is not None else "auto")
        registry = fresh.for_agent(agent="teammate", agent_type=agent.name, permissions=policy, keep=keep,
                                   extra=extra)
        registry.session, registry.hooks, registry.worktree = main.registry.session, main.registry.hooks, relative
        git = await asyncio.to_thread(git_snapshot, root)
    system = agent.prompt + "\n\n" + addendum(store.name, member.name, member.worktree)
    turns = min(agent.max_turns or main.max_iterations, main.max_iterations)
    loop = AgentLoop(provider, system, registry, max_iterations=turns, unknown_limit=main.unknown_limit)
    loop.instructions, loop.git = conversation.project_instructions, git
    loop.budget, loop.auto_compact, loop.session = budget, main.auto_compact, main.session

    def counted(usage) -> None:  # RT4: the session total includes in-process members.
        main.total_usage = add_usage(main.total_usage, usage)

    loop.on_usage = counted
    log = SubagentLog(store.directory / member.transcript, {"type": "teammate", "v": 1, "team": store.name,
                                                            "name": member.name, "agent_type": agent.name,
                                                            "created": now()})
    loop.on_commit = log.append
    runner.loop, runner.owned = loop, owned
    return runner


def load_history(store: TeamStore, member: Member) -> list:
    """ID4: the member's conversation from its transcript; revive_failed when it cannot be trusted. Blocking."""
    from ..session import load_session
    path = store.directory / member.transcript if member.transcript else None
    if path is None or not path.exists():
        return []
    try:
        restored = load_session(path)
    except (OSError, UnicodeError) as exc:
        raise TeamError("revive_failed", f"无法读取对话记录 {path.name}：{exc}") from None
    if restored.skipped:
        raise TeamError("revive_failed", f"对话记录 {path.name} 已损坏（{restored.skipped} 行无法读取）。")
    return restored.history


def restore_member(runner: MemberRunner, member: Member, history: list) -> None:
    """ID4: the old conversation, its token total and its plan approval."""
    runner.loop.restore(history)
    usage = member.usage or {}  # RT4: the record keeps the member's total across revivals.
    runner.loop.total_usage = UsageTotals(**{key: int(usage.get(key, 0) or 0) for key in
                                             ("input_tokens", "output_tokens", "cached_input_tokens")})
    runner.approved = member.approved


class TeamRuntime:
    def __init__(self, conversation: Conversation, *, home: Path | None = None, max_members: int = MAX_MEMBERS,
                 poll: float = POLL, teammate_mode: str = IN_PROCESS, launch: dict | None = None):
        self.conversation = conversation
        self.home = home
        self.max_members = max_members
        self.poll = poll
        # BE1/BE5: where members run, and what a pane member needs to load the same configuration.
        self.teammate_mode = teammate_mode
        self.launch = dict(launch or {})  # {"config": path, "provider": name}
        self.env: dict | None = None  # Tests replace these; None/defaults use the real environment.
        self.which = shutil.which
        self.run = subprocess.run
        self.python: str | None = None
        self.start_timeout = START_TIMEOUT  # BE6
        self._synced_mode = ""  # PM3: the session mode last written to pane members' records.
        self._revived: dict[str, str] = {}
        self.current: TeamStore | None = None
        self.runners: dict[str, MemberRunner | PaneRunner] = {}
        self.plan_requests: dict[str, str] = {}  # TL5: member -> request_id the lead has not answered.
        self.on_approval: Callable[[ApprovalRequest], None] | None = None  # PM2: the TUI shows it.
        self.on_note: Callable[[str], None] | None = None  # LD4, PA6, UI2.
        self.on_change: Callable[[], None] | None = None  # UI1: a member's status changed.
        self.stop_wait = STOP_WAIT  # ID5/LD6.
        self._stops: set[asyncio.Task] = set()
        self.notices: list[str] = []  # Corrupt files kept aside (MB4) and the like.
        self._lead_wake = asyncio.Event()
        self._watcher: asyncio.Task | None = None
        self._writes: set[asyncio.Task] = set()
        self._write_lock = asyncio.Lock()  # Status writes land in the order they happened.
        self._merge_lock = asyncio.Lock()  # One team_merge at a time, from the first check to the merge.

    def guide(self) -> str:
        """LD1: the fixed team section of the lead's system prompt."""
        return LEAD_GUIDE.format(max_members=self.max_members)

    # Places ---------------------------------------------------------------------------------------

    @property
    def project(self) -> Path:
        conversation = self.conversation
        return Path(conversation.context_root or conversation.registry.permissions.root)

    @property
    def root(self) -> Path:
        return teams_root(self.project, self.home)

    def teams(self) -> list[TeamStore]:
        return list_teams(self.root)

    def _note(self, text: str) -> None:
        if self.on_note is not None:
            try:
                self.on_note(text)
            except Exception:
                pass

    def access(self) -> TeamAccess:
        if self.current is None:
            raise TeamError("no_team", "还没有当前团队；先用 team_create 新建或打开一个团队。")
        return TeamAccess(self.current, LEAD, self.wake)

    def wake(self, name: str) -> None:
        if name == LEAD:
            self._lead_wake.set()
        elif name in self.runners:
            self.runners[name].wake()

    # Teams ----------------------------------------------------------------------------------------

    async def create(self, name: str, description: str = "") -> tuple[str, dict]:
        """D13/TM4/ID6: new or reopened, then current."""
        store = TeamStore.at(self.root, name, self.notices.append)
        if self.current is not None and self.current.name == name:
            return f"团队 {name} 已经是当前团队。", {"team": name, "opened": True}
        busy = [runner.name for runner in self.runners.values() if runner.status == "running"]
        if busy:
            raise TeamError("team_busy", f"当前团队 {self.current.name} 还有成员在运行（{'、'.join(busy)}）；"
                                         "等它们空闲后再切换团队。")
        opened = await asyncio.to_thread(store.exists)
        if not opened:
            await asyncio.to_thread(store.create, description)
        session = self.conversation.session
        team = await asyncio.to_thread(store.acquire, os.getpid(), getattr(session, "id", "") or "",
                                       str(self.project))
        await self._leave(keep_panes=True)
        live: list[Member] = []
        if opened:  # ID3: a pane member that still runs and reports keeps going; everything else is stopped.
            def refresh(team):
                for member in team.members:
                    if member.backend != IN_PROCESS and member.status != "stopped" \
                            and member_alive(member.pid, member.heartbeat):
                        live.append(member)
                    else:
                        member.status, member.pid = "stopped", None
            team = await asyncio.to_thread(store.modify, refresh)
        self.current = store
        for member in live:
            self._attach(store, member)
        self._start_watching()
        self._changed()  # UI1
        if opened:
            states = "、".join(f"{m.name}（{m.agent_type}，{STATUS_NAMES.get(m.status, m.status)}）" for m in team.members
                              if m in live)
            stopped = "、".join(f"{m.name}（{m.agent_type}）" for m in team.members if m not in live)
            text = f"已打开已有团队 {name}："
            if states:
                text += f"仍在窗格中运行的成员 {states}"
                text += f"；已停止的成员 {stopped}。" if stopped else "。"
            else:
                text += f"{stopped or '无成员'}，成员都处于停止状态。"
            return text, {"team": name, "opened": True, "live": [m.name for m in live]}
        return (f"已创建团队 {name} 并设为当前团队。接下来用 team_task_create 拆分任务（写好依赖），"
                "再用 team_spawn 创建成员；owner 只能填已创建的成员，可以先留空，创建成员后再用 team_task_update 指派。"), {"team": name, "opened": False}

    async def _leave(self, *, keep_panes: bool = False) -> None:
        """ID6: stop this process's members of the current team and let it go; pane members switched
        away from keep running (keep_panes) and are found again by ID3."""
        if self.current is None:
            return
        await self._stop_members(keep_panes=keep_panes)
        try:
            await asyncio.to_thread(self.current.release, os.getpid())
        except TeamError:
            pass
        self.current = None
        self.plan_requests.clear()

    async def _stop_members(self, *, keep_panes: bool = False) -> None:
        runners, self.runners = list(self.runners.values()), {}
        if keep_panes:
            for runner in [runner for runner in runners if isinstance(runner, PaneRunner)]:
                runners.remove(runner)
                runner.on_status = None
                if runner.job is not None:
                    runner.job.cancel()
                    await asyncio.gather(runner.job, return_exceptions=True)
        for runner in runners:
            await runner.stop()
        await self.settle()
        if runners and self.current is not None:
            names = {runner.name for runner in runners}

            def stopped(team):
                for member in team.members:
                    if member.name in names:
                        member.status, member.pid = "stopped", None
            try:
                await asyncio.to_thread(self.current.modify, stopped)
            except TeamError:
                pass
            await self._release(self.current, names)
            self._changed()

    def _start_watching(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.get_running_loop().create_task(self._watch())

    async def close(self) -> None:
        """LD6: ask every member to stop, wait up to stop_wait in all, cancel the rest; the team stays."""
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        for job in list(self._stops):
            job.cancel()
        await asyncio.gather(*self._stops, return_exceptions=True)
        store, runners = self.current, list(self.runners.values())
        if store is not None and runners:
            access = TeamAccess(store, LEAD)
            for runner in runners:
                try:
                    await asyncio.to_thread(access.send, runner.name, "shutdown_request", reason="Lead 正在退出")
                    runner.wake()
                except TeamError:
                    pass
            jobs = [runner.job for runner in runners if runner.job is not None]
            if jobs:
                await asyncio.wait(jobs, timeout=self.stop_wait)
            await self.settle()
            try:  # Answers nobody will read; a later lead should not see them as news.
                await asyncio.to_thread(store.mailbox.discard_unread, LEAD, {"shutdown_response"})
            except TeamError:
                pass
        await self._leave()
        await self.settle()

    def counts(self) -> tuple[int, int]:
        """UI1: (running, idle) members of this process."""
        states = [runner.status for runner in self.runners.values()]
        return states.count("running"), states.count("idle")

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    async def _ended(self, store: TeamStore, runner: MemberRunner | PaneRunner) -> None:
        """ID5: a member stopped by itself after its shutdown_response (a pane member: its process ended)."""
        if self.runners.get(runner.name) is not runner:
            return  # Stopped by the runtime, which cleans up itself.
        del self.runners[runner.name]
        self.plan_requests.pop(runner.name, None)
        await runner.close()
        if isinstance(runner, PaneRunner):  # It may have died without saying so.
            await self._mark_stopped(store, runner.name, pid=runner.pid)
        await self._release(store, [runner.name])
        self._note(f"成员 {runner.name} 已停止（{label_of(runner)}）")
        self._changed()

    async def _mark_stopped(self, store: TeamStore, name: str, *, pid: int | None = None, error: str = "") -> None:
        def stopped(team):
            member = team.member(name)
            if member is not None and (pid is None or member.pid in {pid, None}):
                member.status, member.pid, member.last_tool = "stopped", None, ""
                if error:
                    member.error = error
        try:
            await asyncio.to_thread(store.modify, stopped)
        except TeamError:
            pass

    async def _release(self, store: TeamStore, names) -> None:
        """TM6: the worktrees of stopped members are no longer in use."""
        try:
            team = await asyncio.to_thread(store.load)
        except TeamError:
            return
        for name in names:
            member = team.member(name)
            if member is None or not member.worktree:
                continue
            try:
                manager = self.conversation.worktree_manager()
                await manager.mark_in_use(member.worktree["flat"], "")
            except Exception:
                pass

    # Stopping one member (ID5, CM3) ---------------------------------------------------------------

    async def stop_member(self, name: str) -> str:
        access = self.access()
        store = self.current
        team = await asyncio.to_thread(store.load)
        if team.member(name) is None:
            raise TeamError("unknown_member", f"团队 {team.name} 中没有成员 {name}。")
        runner = self.runners.get(name)
        if runner is None:
            return f"成员 {name} 已经停止。"
        await access.asend(name, "shutdown_request", reason="用户用 /team stop 停止")
        job = asyncio.get_running_loop().create_task(self._stop_later(store, runner))
        self._stops.add(job)
        job.add_done_callback(self._stops.discard)
        return f"已请求成员 {name} 停止：它在这一轮结束后停止；{self.stop_wait:g} 秒内没有停止时强制停止。"

    async def _stop_later(self, store: TeamStore, runner: MemberRunner | PaneRunner) -> None:
        """BE7: cancel an in-process member, end a pane member's processes, when it has not stopped in time."""
        if runner.job is not None:
            await asyncio.wait([runner.job], timeout=self.stop_wait)
        if self.runners.get(runner.name) is not runner or (runner.job is not None and runner.job.done()):
            return
        del self.runners[runner.name]
        self.plan_requests.pop(runner.name, None)
        await runner.stop()
        await self.settle()

        await self._mark_stopped(store, runner.name)
        await self._release(store, [runner.name])
        self._note(f"成员 {runner.name} 没有在 {self.stop_wait:g} 秒内停止，已强制停止（{label_of(runner)}）")
        self._changed()

    async def delete(self, name: str | None = None, *, force: bool = False) -> tuple[str, dict]:
        """CM4: stop the members, delete clean and merged worktrees, then the team. name: any team of
        this project (/team delete), the current one by default (team_delete)."""
        if name is None or (self.current is not None and self.current.name == name):
            self.access()
            store = self.current
            busy = [runner.name for runner in self.runners.values() if runner.status == "running"]
            if busy and not force:
                raise TeamError("team_busy", f"成员 {'、'.join(busy)} 正在运行；等它们空闲，或用 force: true 停止后删除。")
            for job in list(self._stops):
                job.cancel()
            await self._stop_members()
            if self._watcher is not None:
                self._watcher.cancel()
                await asyncio.gather(self._watcher, return_exceptions=True)
                self._watcher = None
            self.current = None
            self.plan_requests.clear()
        else:
            store = TeamStore.at(self.root, name, self.notices.append)
            if not await asyncio.to_thread(store.exists):
                raise TeamError("unknown_team", f"本项目没有名为 {name} 的团队。")
            await asyncio.to_thread(store.acquire, os.getpid())  # team_held while another lead has it.
        team = await asyncio.to_thread(store.load)
        removed, kept = [], []
        for member in team.members:
            if not member.worktree:
                continue
            worktree = member.worktree.get("name", "")
            try:
                manager = self.conversation.worktree_manager()
                await manager.remove(worktree)
                removed.append(worktree)
            except Exception as exc:  # Changes, unmerged commits or a failed deletion: keep it.
                kept.append(f"{worktree}（{getattr(exc, 'changes', None) and exc.changes.describe() or exc}）")
        await asyncio.to_thread(remove_tree, store.directory)
        text = f"已删除团队 {team.name}。"
        if removed:
            text += "已删除工作树：" + "、".join(removed) + "。"
        if kept:
            text += "保留的工作树（有未合并的提交或改动）：" + "；".join(kept) + "。用 /worktree 查看。"
        self._note(f"团队 {team.name} 已删除")
        self._changed()
        return text, {"team": team.name, "removed": removed, "kept": kept}

    # Reports (CM1, CM2, LD5) ----------------------------------------------------------------------

    def _holder(self, team) -> str:
        pid = int((team.lead or {}).get("pid") or 0)
        if not pid:
            return "无"
        if pid == os.getpid():
            return "本进程"
        return f"PID {pid}（{'运行中' if process_alive(pid) else '已退出'}）"

    def _state(self, store: TeamStore, team, member: Member) -> str:
        """In this process the runner knows best; a team no live lead holds has nothing running (ID3)."""
        if self.current is not None and self.current.name == store.name:
            runner = self.runners.get(member.name)
            return runner.status if runner is not None else "stopped"
        pid = int((team.lead or {}).get("pid") or 0)
        return member.status if pid and process_alive(pid) else "stopped"

    def overview(self) -> str:
        """CM1; blocking."""
        stores = self.teams()
        if not stores:
            return "本项目还没有团队。让模型用 team_create 建一个。"
        lines = [f"团队（{len(stores)}）"]
        for store in stores:
            try:
                team = store.load()
            except TeamError as exc:
                lines.append(f"  {store.name}  ! {exc.message}")
                continue
            states = [self._state(store, team, member) for member in team.members]
            counts = "、".join(f"{STATUS_NAMES[state]} {states.count(state)}" for state in STATUS_NAMES
                              if states.count(state))
            current = "  当前" if self.current is not None and self.current.name == store.name else ""
            lines.append(f"  {store.name}{current}  成员 {len(team.members)}" + (f"（{counts}）" if counts else "")
                         + f"  持有者 {self._holder(team)}")
        lines.append("/team <名称> 查看详情；/team stop <成员> 停止当前团队的成员；/team delete <名称> 删除团队。")
        return "\n".join(lines)

    def detail(self, name: str) -> str:
        """CM2; blocking."""
        store = TeamStore.at(self.root, name)
        if not store.exists():
            raise TeamError("unknown_team", f"本项目没有名为 {name} 的团队。")
        team = store.load()
        lines = [f"团队 {team.name}" + (f"：{team.description}" if team.description else ""),
                 f"创建于 {team.created}；持有者 {self._holder(team)}", f"成员（{len(team.members)}）"]
        for member in team.members:
            state = self._state(store, team, member)
            backend = BACKEND_LABELS.get(member.backend, member.backend)
            lines.append(f"  {member.name}  {STATUS_NAMES.get(state, state)}  {member.agent_type}  "
                         f"后端 {backend}（{member.backend_reason or '—'}）")
            if member.worktree:
                lines.append(f"    工作树 {member.worktree.get('name', '')}，分支 {member.worktree.get('branch', '')}")
            else:
                lines.append("    在主项目目录中工作")
            runner = self.runners.get(member.name) if self.current is not None and self.current.name == name else None
            if runner is not None and runner.last_tool:
                lines.append(f"    最近的工具调用：{runner.last_tool}")
            usage = member.usage or {}
            try:
                unread = store.mailbox.unread_count(member.name)
            except TeamError:
                unread = 0
            lines.append(f"    Token 入 {usage.get('input_tokens', 0)} / 出 {usage.get('output_tokens', 0)}；"
                         f"未读消息 {unread}")
        tasks = store.board.list()
        lines += [f"任务（{len(tasks)}）", *("  " + line for line in task_listing(tasks).splitlines())]
        return "\n".join(lines)

    def context(self) -> str:
        """LD5: what the lead needs to know again after /clear or /resume; blocking."""
        store = self.current
        if store is None:
            return ""
        team = store.load()
        members = "；".join(f"{member.name}（{member.agent_type}，{STATUS_NAMES.get(self._state(store, team, member))}"
                           + (f"，分支 {member.worktree['branch']}" if member.worktree else "") + "）"
                           for member in team.members) or "无"
        tasks = store.board.list()
        counts = "、".join(f"{label} {sum(1 for task in tasks if task.status == key)}"
                          for key, label in STATUS_LABELS.items() if any(task.status == key for task in tasks))
        return (f"<team-context>\n你仍是团队 {team.name} 的 Lead（会话已清空或恢复，之前的对话不在上下文中）。\n"
                f"成员：{members}。\n任务：共 {len(tasks)} 个" + (f"（{counts}）" if counts else "") + "；"
                "用 team_task_list 查看详情。团队更新会自动送达，不要轮询。\n</team-context>")

    # Merging --------------------------------------------------------------------------------------

    async def merge(self, name: str) -> tuple[str, dict]:
        """MG1–MG9: checks, the user's confirmation, the checks again, then git merge."""
        from ..permissions import MERGE_CHOICES, Operation
        from ..worktree import WorktreeError
        from . import merge
        self.access()
        store = self.current
        try:
            manager = self.conversation.worktree_manager()
        except WorktreeError as exc:
            raise TeamError("main_not_ready", str(exc)) from None
        busy = lambda: name in self.runners and self.runners[name].status == "running"  # noqa: E731
        async with self._merge_lock:
            plan = await asyncio.to_thread(merge.check, manager, store, name, busy())
            policy = self.conversation._agent.registry.permissions
            operation = Operation("team_merge", name, preview=merge.preview(plan))
            choice = await policy.confirm("", operation, f"把成员 {name} 的分支 {plan.branch} 合并到 {plan.target}？",
                                          MERGE_CHOICES)
            if choice is None:
                raise TeamError("merge_not_confirmed", "合并需要用户在界面中确认；当前无法请求确认，没有合并。")
            if choice != "merge":
                raise TeamError("merge_declined", "用户拒绝了这次合并，没有合并。停下来问用户想怎么处理，不要重试。")
            plan = await asyncio.to_thread(merge.check, manager, store, name, busy())  # It may have changed meanwhile.
            text, details = await asyncio.to_thread(merge.merge, plan)
        self._note(f"已合并成员 {name} 的分支 {plan.branch}")
        return text, {"team": store.name, **details}

    # Members --------------------------------------------------------------------------------------

    def _choose(self) -> Choice:
        """BE2/BE3, ID4: from the current teammate_mode, every time a member starts."""
        return detect_backend(self.teammate_mode, self.env, self.which)

    async def spawn(self, name: str, agent_type: str, prompt: str, *, require_plan_approval: bool = False,
                    isolation: str = "worktree") -> tuple[str, dict]:
        from ..agents import FORK
        self.access()
        store = self.current
        problem = validate_member_name(name)
        if problem:
            raise TeamError("invalid_name", f"成员名 {name} 不可用：{problem}。")
        team = await asyncio.to_thread(store.load)
        if team.member(name) is not None:
            raise TeamError("member_exists", f"团队 {team.name} 已有成员 {name}。")
        if len(team.members) >= self.max_members:
            raise TeamError("too_many_members", f"一个团队最多 {self.max_members} 个成员（team_max_members）。")
        catalog = self.conversation.subagents.catalog
        kind = agent_type.strip() or "general"
        agent = catalog.get(kind) if kind != FORK else None
        if agent is None:
            names = "、".join(catalog.names()) or "无"
            raise TeamError("unknown_agent_type", f"没有名为 {kind} 的 Agent 定义（成员不能是 Fork）。可用：{names}。")
        subagents = self.conversation.subagents
        try:
            subagents._check_provider(agent.model, f"成员 {name} ")
        except ProviderError as exc:
            raise TeamError("agent_unavailable", str(exc)) from None
        choice = self._choose()  # BE3: before anything is created.
        worktree, root, relative = None, None, ""
        if isolation == "worktree":
            from ..worktree import WorktreeError
            from ..worktree.manager import WORKTREES
            try:
                manager = self.conversation.worktree_manager()
                created = await manager.create(f"team-{team.name}/{name}", manual=True,
                                               task=f"团队 {team.name} 成员 {name}")
            except WorktreeError as exc:
                raise TeamError(getattr(exc, "code", "") or "worktree_unavailable", str(exc)) from None
            meta = created.meta
            await manager.mark_in_use(meta.flat, f"团队 {team.name} 成员 {name}")  # TM6, also when resumed.
            root = manager.root_in(meta)
            relative = (WORKTREES / meta.flat).as_posix()
            from .merge import current_branch
            worktree = {"name": meta.name, "flat": meta.flat, "path": str(meta.path), "branch": meta.branch,
                        "relative": relative, "main": await asyncio.to_thread(current_branch, manager.top)}  # RT3
        pane = choice.backend != IN_PROCESS
        member = Member(name, agent.name, choice.backend, choice.reason, "stopped" if pane else "idle",
                        require_plan_approval, model=agent.model or "", worktree=worktree,
                        transcript=f"transcripts/{name}.jsonl", prompt=prompt)
        if not pane:
            runner = await self._runner(store, member, agent, root, relative)
        await asyncio.to_thread(store.modify, lambda team: team.members.append(member))
        await asyncio.to_thread(store.mailbox.send, name, Message(LEAD, "text", prompt, "初始任务"))
        if pane:
            runner = await self._launch(store, member, choice, root)
        self._own(store, runner)
        self.runners[name] = runner
        runner.start()
        runner.wake()
        place = (f"工作树 {worktree['path']}，分支 {worktree['branch']}" if worktree else "在主项目目录中工作")
        plan = "；以计划模式开始，提交的计划会出现在团队更新中，由你审批" if require_plan_approval else ""
        self._note(f"成员 {name} 已创建（{agent.name}，{BACKEND_LABELS[choice.backend]}）")
        self._changed()
        return (f"已创建成员 {name}（{agent.name}，后端：{choice.backend}（{choice.reason}）），{place}{plan}。"
                "它完成一轮后你会收到 <team-update>；在此之前不要轮询。"), {
                    "team": team.name, "member": name, "backend": choice.backend, "backend_reason": choice.reason,
                    "worktree": worktree["branch"] if worktree else None}

    async def _launch(self, store: TeamStore, member: Member, choice: Choice, root: Path | None) -> PaneRunner:
        """BE5/BE6: open the pane, wait for the member to write its pid. backend_failed leaves the member
        stopped and its worktree kept; nothing falls back to in-process."""
        backend = pane_backend(choice.backend, self.run, self.which, self.python)
        mode = self.conversation._agent.registry.permissions.mode
        name = member.name

        def reset(team):
            record = team.member(name)
            if record is not None:
                record.backend, record.backend_reason = choice.backend, choice.reason
                record.status, record.pid, record.heartbeat, record.pane, record.error = "stopped", None, None, None, ""
                record.mode, record.last_tool = mode, ""  # PM3
        await asyncio.to_thread(store.modify, reset)
        self._synced_mode = mode
        cwd = root or self.project
        arguments = member_arguments(store.directory, name, self.launch.get("config"), self.launch.get("provider"))
        try:
            pane = await asyncio.to_thread(backend.launch, cwd, arguments)
        except TeamError as exc:
            await self._mark_stopped(store, name, error=exc.message)
            await self._release(store, [name])
            raise
        if pane:
            def remember(team):
                record = team.member(name)
                if record is not None:
                    record.pane = pane
            await asyncio.to_thread(store.modify, remember)
        end = asyncio.get_running_loop().time() + self.start_timeout
        while True:
            try:
                record = (await asyncio.to_thread(store.load)).member(name)
            except TeamError:
                record = None
            if record is not None and record.error:
                failure = f"成员 {name} 的{BACKEND_LABELS[choice.backend]}启动失败：{record.error}"
                break
            if record is not None and record.pid:
                status = record.status if record.status != "stopped" else "idle"
                return PaneRunner(store, name, backend, int(record.pid), pane, status=status, poll=self.poll)
            if asyncio.get_running_loop().time() >= end:
                failure = (f"成员 {name} 的{BACKEND_LABELS[choice.backend]}在 {self.start_timeout:g} 秒内没有启动"
                           "（没有写入进程号）。")
                break
            await asyncio.sleep(min(0.1, self.poll))
        await asyncio.to_thread(backend.kill, None, pane)  # tmux: the pane; Windows Terminal closes its own.
        await self._mark_stopped(store, name, error=failure)
        await self._release(store, [name])
        self._note(failure)
        raise TeamError("backend_failed", failure + "成员已标为停止，工作树保留；没有改用其他后端。")

    def _attach(self, store: TeamStore, member: Member) -> PaneRunner:
        """ID3: a pane member that kept running while no lead held the team."""
        backend = pane_backend(member.backend, self.run, self.which, self.python)
        runner = PaneRunner(store, member.name, backend, int(member.pid), member.pane, status=member.status,
                            poll=self.poll)
        self._own(store, runner)
        self.runners[member.name] = runner
        runner.start()
        return runner

    def _own(self, store: TeamStore, runner: MemberRunner | PaneRunner) -> None:
        """Status changes: an in-process member's go into its record; a pane member writes its own."""
        def status(state: str, fields: dict) -> None:
            if not isinstance(runner, PaneRunner) and "last_tool" not in fields:  # CM2 reads the runner.
                self._member_status(store, runner.name, state, fields)
            if state == "stopped" and self.runners.get(runner.name) is runner:
                self._track(self._ended(store, runner))
            self._changed()
        runner.on_status = status

    async def _runner(self, store: TeamStore, member: Member, agent, root: Path | None, relative: str) -> MemberRunner:
        return await build_runner(self.conversation, store, member, agent, root, relative, wake=self.wake,
                                  on_approval=self._approval, poll=self.poll)

    def _approval(self, request: ApprovalRequest) -> None:
        if self.on_approval is not None:
            self.on_approval(request)

    def _member_status(self, store: TeamStore, name: str, state: str, fields: dict) -> None:
        def change(team):
            member = team.member(name)
            if member is not None:
                member.status = state
                if "usage" in fields:
                    member.usage = fields["usage"]
                if "approved" in fields:
                    member.approved = fields["approved"]

        async def write():
            async with self._write_lock:
                try:
                    await asyncio.to_thread(store.modify, change)
                except TeamError:
                    pass
        self._track(write())

    def _track(self, work) -> None:
        """Background bookkeeping that settle() waits for."""
        try:
            job = asyncio.get_running_loop().create_task(work)
        except RuntimeError:
            work.close()
            return
        self._writes.add(job)
        job.add_done_callback(self._writes.discard)

    async def settle(self) -> None:
        """Wait for the status writes already started."""
        while self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)

    # Messages -------------------------------------------------------------------------------------

    async def send(self, to: str, kind: str, body: str = "", summary: str = "", **extra) -> list[str]:
        return (await self.message(to, kind, body, summary, **extra))[0]

    async def message(self, to: str, kind: str, body: str = "", summary: str = "", **extra) -> tuple[list[str], list[str]]:
        """TL5 for the lead: a plan answer must name the member's open request. ID4: a stopped
        recipient is revived after the message is in its mailbox. (targets, revived)."""
        access = self.access()
        if kind == "plan_approval_response":
            open_request = self.plan_requests.get(to)
            if open_request is None or open_request != extra.get("request_id"):
                raise TeamError("unknown_request", f"{to} 没有 request_id 为 {extra.get('request_id')} 的待审批计划"
                                + (f"（待审批的是 {open_request}）。" if open_request else "。"))
        if kind == "shutdown_request" and to not in self.runners:
            await asyncio.to_thread(access.recipients, to, kind)  # unknown_member first.
            raise TeamError("member_stopped", f"成员 {to} 已经停止，不需要停止请求。")
        store = self.current
        targets = await access.asend(to, kind, body, summary, **extra)
        if kind == "plan_approval_response":
            self.plan_requests.pop(to, None)
            verdict = "批准" if extra.get("approve") else "拒绝"
            self._note(f"Lead {verdict}了 {to} 的计划")
        revived, failures = [], []
        for name in targets:
            if name == LEAD or name in self.runners:
                continue
            try:
                await self._revive(store, name)
                revived.append(name)
            except TeamError as exc:
                failures.append(f"{name}：{exc.message}")
        if failures:
            raise TeamError("revive_failed", "消息已写入邮箱，下次恢复时仍会读到；恢复失败——" + "；".join(failures))
        return targets, revived

    async def _revive(self, store: TeamStore, name: str) -> None:
        """ID4: the backend from the current teammate_mode, the conversation from its transcript, the same
        worktree, then a turn for the waiting messages. A pane member restores its conversation itself."""
        from ..agents import FORK
        team = await asyncio.to_thread(store.load)
        member = team.member(name)
        if member is None:
            raise TeamError("revive_failed", "没有这个成员的记录。")
        agent = self.conversation.subagents.catalog.get(member.agent_type) if member.agent_type != FORK else None
        if agent is None:
            raise TeamError("revive_failed", f"Agent 定义 {member.agent_type} 已不存在。")
        try:
            self.conversation.subagents._check_provider(agent.model, f"成员 {name} ")
        except ProviderError as exc:
            raise TeamError("revive_failed", str(exc)) from None
        try:
            choice = self._choose()
        except TeamError as exc:
            raise TeamError("revive_failed", exc.message) from None
        history = await asyncio.to_thread(load_history, store, member)
        root, relative, manager = None, "", None
        if member.worktree:
            from ..worktree.meta import load_meta
            try:
                manager = self.conversation.worktree_manager()
                meta = await asyncio.to_thread(load_meta, manager.meta_path(member.worktree["flat"]))
            except Exception as exc:
                raise TeamError("revive_failed", f"工作树不可用：{exc}") from None
            if meta is None or not Path(meta.path).is_dir():
                raise TeamError("revive_failed", f"工作树 {member.worktree.get('name', '')} 已不存在。")
            await manager.mark_in_use(meta.flat, f"团队 {store.name} 成员 {name}")
            root, relative = manager.root_in(meta), member.worktree.get("relative", "")
        try:  # A request left from an earlier stop would stop it again at once.
            await asyncio.to_thread(store.mailbox.discard_unread, name, {"shutdown_request"})
        except TeamError:
            pass
        if choice.backend == IN_PROCESS:
            try:
                runner = await self._runner(store, member, agent, root, relative)
            except Exception as exc:
                if manager is not None:
                    await manager.mark_in_use(member.worktree["flat"], "")
                raise TeamError("revive_failed", str(exc)) from None
            restore_member(runner, member, history)
            if member.backend != IN_PROCESS:
                def moved(team):
                    record = team.member(name)
                    if record is not None:
                        record.backend, record.backend_reason, record.pane = choice.backend, choice.reason, None
                await asyncio.to_thread(store.modify, moved)
        else:
            try:
                runner = await self._launch(store, member, choice, root)
            except TeamError as exc:
                raise TeamError("revive_failed", exc.message) from None
        changed = "" if choice.backend == member.backend else f"，上次是{BACKEND_LABELS.get(member.backend, member.backend)}"
        self._revived[name] = f"后端：{choice.backend}，{choice.reason}{changed}"  # ID4: said in the result.
        self._own(store, runner)
        self.runners[name] = runner
        runner.start()
        runner.wake()
        self._note(f"成员 {name} 已恢复（{BACKEND_LABELS[choice.backend]}{changed}）")
        self._changed()

    async def _watch(self) -> None:
        """LD2: read the lead's mailbox when woken or every poll; hand it to the conversation."""
        while True:
            try:
                await asyncio.wait_for(self._lead_wake.wait(), self.poll)
            except TimeoutError:
                pass
            self._lead_wake.clear()
            store = self.current
            if store is None:
                continue
            await self._sync_mode(store)
            try:
                messages = await asyncio.to_thread(store.mailbox.read_unread, LEAD)
            except TeamError:
                continue
            if messages:
                await self._deliver(store, messages)

    def revived_with(self, name: str) -> str:
        return self._revived.get(name, "")

    async def _sync_mode(self, store: TeamStore) -> None:
        """PM3: a pane member reads the session mode from its record at the start of each turn."""
        mode = self.conversation._agent.registry.permissions.mode
        if mode == self._synced_mode or not any(isinstance(r, PaneRunner) for r in self.runners.values()):
            return

        def write(team):
            for member in team.members:
                if member.backend != IN_PROCESS:
                    member.mode = mode
        try:
            await asyncio.to_thread(store.modify, write)
            self._synced_mode = mode
        except TeamError:
            pass

    async def _deliver(self, store: TeamStore, messages: list[Message]) -> None:
        ready = []
        if any(m.type == "idle_notification" and m.extra.get("completed_tasks") for m in messages):
            from .board import is_ready
            try:
                tasks = await asyncio.to_thread(store.board.list)
            except TeamError:
                tasks = []
            index = {task.id: task for task in tasks}
            done = {key for m in messages if m.type == "idle_notification" for key in m.extra.get("completed_tasks") or []}
            ready = [f"{task.id}（{task.owner or '未指派'}）" for task in tasks  # TK7: made ready by these.
                     if task.status == "pending" and done & set(task.blocked_by) and is_ready(task, index)]
        for message in messages:
            if message.type == "plan_approval_request":
                self.plan_requests[message.sender] = str(message.extra.get("request_id", ""))
            note = note_for(message)
            if message.type == "idle_notification":  # UI2: with its backend.
                note += f"（{label_of(self.runners.get(message.sender))}）"
            self._note(f"团队 {store.name}：" + note)
        conversation = self.conversation
        conversation._agent.notifications.append(render_update(store.name, messages, ready))
        conversation._tasks_ready()
