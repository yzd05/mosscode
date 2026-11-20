"""Runner construction, pane watcher, update rendering, and transcript helpers for teams."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..messages import UsageTotals
from ..permissions import ApprovalRequest
from ..worktree.meta import process_alive
from . import BoardPost, Member, TeamError, TeamStore
from .backends import IN_PROCESS, IN_PROCESS_REASON, LABELS, PaneBackend
from .lock import now
from .mailbox import Message
from .member import POLL, MemberPolicy, MemberRunner, TeamAccess, addendum

if TYPE_CHECKING:
    from ..conversation import Conversation

MAX_MEMBERS = 6
VERIFIER_BAN = frozenset({"write_file", "edit_file"})
BODY_PREVIEW = 4000  # Characters of a message body in a team update.
REASON = IN_PROCESS_REASON
BACKEND_LABELS = LABELS
STATUS_NAMES = {"running": "运行中", "idle": "空闲", "stopped": "已停止"}
STOP_WAIT = 10.0  # Seconds a member has to stop after a shutdown_request.
IDLE_STATES = {"failed": "失败", "interrupted": "被用户中断"}  # idle_notification reasons.
LEAD_GUIDE = """## Agent 团队

你可以作为 lead 组建团队：成员是长期存在的 Agent，各自在独立的 git 工作树中工作，通过黑板（post_board / read_board）、消息和共享任务协作。
- 何时使用：用户的任务能拆成几块可以并行、彼此独立的多文件改动时。一处小改动、一个问题或只需要调查时不要建团队，直接做或用子 Agent。
- 做法：team_create 建团队；先用 team_task_create 建好任务并写明依赖（blocked_by），再用 team_spawn 创建成员（至多 {max_members} 个活跃成员，可指定 role 为 worker、integrator 或只读审查员 verifier），在 prompt 中写清它负责的任务、背景、范围和完成后要提交改动。
- 黑板定序接力：成员可通过 post_board 点名同级成员（如 @integrator、@verifier）并引用 #seq 直接交接，全部通过后再点名 @lead；创建后不要轮询，点名 @lead 的黑板条目、空闲通知和巡检提示会作为 <team-update> 自动送达。
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


def render_update(team: str, messages: list[Message], ready: list[str], board_posts: Sequence[BoardPost] = ()) -> str:
    """One notification for everything that arrived in the lead's mailbox and blackboard."""
    lines = [f"<team-update>\n团队 {team}："]
    for post in board_posts:
        body = post.body
        if len(body) > BODY_PREVIEW:
            body = body[:BODY_PREVIEW] + f"\n（正文共 {len(post.body)} 字，已截断）"
        if post.kind == "watchdog":
            lines.append(f"[黑板 #{post.seq} · 系统巡检] 摘要：{post.summary}\n{body}")
        else:
            lines.append(f"[黑板 #{post.seq} · {post.author}] 摘要：{post.summary}\n{body}")
    for message in messages:
        extra, sender = message.extra, message.sender
        if message.type == "text":
            body = message.body
            if len(body) > BODY_PREVIEW:
                body = body[:BODY_PREVIEW] + f"\n（正文共 {len(message.body)} 字，已截断）"
            lines.append(f"[{sender} 的消息] 摘要：{message.summary}\n{body}")
        elif message.type == "plan_approval_request":
            lines.append(
                f"[{sender} 提交了计划，request_id={extra.get('request_id')}]\n{extra.get('plan', '')}\n"
                f"用 send_message（to: {sender}，type: plan_approval_response，request_id，approve，feedback）答复。"
            )
        elif message.type == "idle_notification":
            tasks = "、".join(extra.get("completed_tasks") or [])
            state = IDLE_STATES.get(extra.get("reason"), "空闲")
            lines.append(
                f"{sender} {state}"
                + (f"，完成任务 {tasks}" if tasks else "")
                + (f"。最后回复：{message.summary}" if message.summary else "。")
            )
        elif message.type == "shutdown_response":
            verdict = "同意停止" if extra.get("approve") else f"拒绝停止：{extra.get('reason') or '未说明'}"
            lines.append(f"{sender} {verdict}。")
        else:
            lines.append(f"[{sender} · {message.type}] {message.body}")
    if ready:
        lines.append("现在就绪的任务：" + "、".join(ready) + "。")
    return (
        "\n".join(lines)
        + "\n</team-update>\n以上是团队的最新消息；需要时用 post_board 或 send_message 回复成员，不要轮询。"
    )


def note_for(message: Message) -> str:
    """One line in the chat for each update."""
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


@dataclass(frozen=True)
class MemberRow:
    """One line of the member panel. Waiting approvals are counted by the interface."""

    name: str
    status: str
    pane: bool
    since: float | None = None  # time.monotonic() when it entered its status; None when stopped.
    last_tool: str = ""
    task: str = ""  # The subject of its first in_progress task.
    window: str = ""


class PaneRunner:
    """The lead's view of a member in a pane of its own. It polls its mailbox itself; its
    record in config.json says how it is doing, and its process ending is the member stopping."""

    def __init__(
        self,
        store: TeamStore,
        name: str,
        backend: PaneBackend,
        pid: int,
        pane: str | None,
        *,
        status: str = "idle",
        poll: float = POLL,
    ):
        self.store, self.name, self.backend = store, name, backend
        self.backend_name = backend.name
        self.pid, self.pane, self.poll = pid, pane, poll
        self.status, self.last_tool = status, ""
        self.since = time.monotonic()  # When the lead saw it enter its status.
        self.on_status: Callable[[str, dict], None] | None = None
        # A request it published in its record, and one that left the record (or the member
        # stopped with it still there).
        self.on_request: Callable[[dict], None] | None = None
        self.on_settled: Callable[[str, bool], None] | None = None  # (request id, the member ended)
        self.asked: set[str] = set()
        self.crashed: bool = False
        self.job: asyncio.Task | None = None

    def wake(self) -> None:
        """Nothing to do; it reads its mailbox every POLL seconds."""

    def start(self) -> asyncio.Task:
        self.job = asyncio.get_running_loop().create_task(self.watch())
        return self.job

    def _report(self, status: str) -> None:
        if status != self.status:
            self.since = time.monotonic()
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
                    if member is not None and member.error:
                        self.crashed = True
                    return
                self.last_tool = member.last_tool if member.status == "running" else ""
                self._approvals(member.approvals)
                if member.status != self.status:
                    self._report(member.status)
            try:
                member = (await asyncio.to_thread(self.store.load)).member(self.name)
            except TeamError:
                member = None
            if member is not None and member.status != "stopped":
                self.crashed = True
        finally:
            self._approvals([], ended=True)
            self._report("stopped")

    def _approvals(self, items: list, *, ended: bool = False) -> None:
        """New requests in the record go to the lead; the ones gone from it were answered or dropped."""
        listed = {item.get("id") for item in items if isinstance(item, dict) and item.get("id")}
        for item in items:
            if isinstance(item, dict) and item.get("id") and item["id"] not in self.asked:
                self.asked.add(item["id"])
                if self.on_request is not None:
                    self.on_request(item)
        for request_id in self.asked - listed:
            self.asked.discard(request_id)
            if self.on_settled is not None:
                self.on_settled(request_id, ended)

    async def stop(self) -> None:
        """End its process tree (and its tmux pane)."""
        if self.job is not None and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await asyncio.to_thread(self.backend.kill, self.pid if process_alive(self.pid) else None, self.pane)

    async def close(self) -> None:
        pass


async def build_runner(
    conversation: Conversation,
    store: TeamStore,
    member: Member,
    agent,
    root: Path | None,
    relative: str,
    *,
    wake: Callable[[str], None] | None = None,
    on_approval: Callable[[ApprovalRequest], None] | None = None,
    poll: float = POLL,
    session_mode: Callable[[], str] | None = None,
    has_active_peers: Callable[[], bool] | None = None,
) -> MemberRunner:
    """The definition's prompt plus the team addendum, tools rooted in its worktree. The lead builds
    in-process members with it; a pane member's process builds itself with it (session_mode from its record)."""
    from ..agent import AgentLoop, add_usage
    from ..agents.runtime import GLOBAL_BAN, SubagentLog, effective_mode
    from ..permissions import stricter
    from ..prompts import git_snapshot
    from ..tools.team import TEAM_TOOLS, member_tools
    from ..usage import price_of

    main = conversation._agent
    parent = main.registry.permissions
    access = TeamAccess(store, member.name, wake)
    session_mode = session_mode or (lambda: parent.mode)
    is_verifier = member.role == "verifier"
    compute_mode = (
        (lambda m: stricter(effective_mode(agent, m), m)) if is_verifier else (lambda m: effective_mode(agent, m))
    )
    runner = MemberRunner(
        access,
        member.name,
        role=member.role,
        agent_mode=compute_mode,
        session_mode=session_mode,
        require_plan_approval=member.require_plan_approval,
        on_approval=on_approval,
        poll=poll,
        has_active_peers=has_active_peers,
    )
    runner._cursor = member.board_cursor
    provider, budget, owned = conversation.subagents._provider(agent.model, f"成员 {member.name} ")
    mode = "plan" if member.require_plan_approval else compute_mode(session_mode())
    policy = MemberPolicy(parent, mode, interactive=parent.interactive, label=f"成员 {member.name}", root=root)
    base = root or parent.root
    extra = member_tools(base, access, runner)
    banned = GLOBAL_BAN | TEAM_TOOLS | (VERIFIER_BAN if is_verifier else frozenset())
    keep = lambda name: name not in banned and agent.allows(name)  # noqa: E731
    if root is None:
        # Members run alongside the user's turns, so /rewind leaves their files alone.
        registry = main.registry.for_agent(
            agent="teammate", agent_type=agent.name, permissions=policy, keep=keep, extra=extra, checkpoints=False
        )
        git = main.git
    else:
        from ..tools import create_default_registry, web_tools
        from ..tools.background import share

        shell = getattr(main.registry.get("execute_command"), "shell", None)
        todo = main.registry.get("todo_write") is not None  # Only when the main conversation has it.
        fresh = create_default_registry(
            root, permissions=policy, shell=shell if shell is not None else "auto", todo=todo
        )
        web = tuple(tool for tool in web_tools(main.registry, root) if keep(tool.name))
        registry = fresh.for_agent(
            agent="teammate", agent_type=agent.name, permissions=policy, keep=keep, extra=(*web, *extra)
        )
        registry.session, registry.hooks, registry.worktree = main.registry.session, main.registry.hooks, relative
        share(main.registry, registry)
        git = await asyncio.to_thread(git_snapshot, root)
    # Its background commands end with it (MemberRunner.close) and notify nobody.
    registry.task_id = runner.owner = f"member:{member.name}"
    runner.commands = registry.commands
    system = agent.prompt + "\n\n" + addendum(store.name, member.name, member.worktree, member.role)
    turns = min(agent.max_turns or main.max_iterations, main.max_iterations)
    loop = AgentLoop(
        provider,
        system,
        registry,
        max_iterations=turns,
        unknown_limit=main.unknown_limit,
        policies=main.policies,
    )
    loop.instructions = "\n\n".join(
        part
        for part in (
            conversation.project_instructions,
            getattr(conversation, "memory_instructions", ""),
        )
        if part
    )
    loop.git = git
    loop.budget, loop.auto_compact, loop.session = budget, main.auto_compact, main.session
    loop.context_collapse = main.context_collapse

    def counted(usage) -> None:  # The session total includes in-process members.
        main.total_usage = add_usage(main.total_usage, usage, price_of(loop.provider))

    loop.on_usage = counted
    loop.run_budget = main.run_budget
    log = SubagentLog(
        store.directory / member.transcript,
        {
            "type": "teammate",
            "v": 1,
            "team": store.name,
            "name": member.name,
            "agent_type": agent.name,
            "created": now(),
        },
    )
    loop.on_commit = log.append
    registry.image_dir = (store.directory / member.transcript).with_suffix("") / "images"
    runner.loop, runner.owned = loop, owned
    return runner


def load_history(store: TeamStore, member: Member) -> list:
    """The member's conversation from its transcript; revive_failed when it cannot be trusted. Blocking."""
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
    """The old conversation, its token total and its plan approval."""
    runner.loop.restore(history)
    usage = member.usage or {}  # The record keeps the member's total across revivals.
    runner.loop.total_usage = UsageTotals(
        **{key: int(usage.get(key, 0) or 0) for key in ("input_tokens", "output_tokens", "cached_input_tokens")}
    )
    runner.approved = member.approved
