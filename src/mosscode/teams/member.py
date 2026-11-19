"""A member's long-lived conversation, and what any participant may do in a team.

The same runner serves an in-process member and a member in its own terminal pane.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import aclosing
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING

from ..messages import Completed, ProviderError, RunStopped, ToolProgress
from ..permissions import ALL_CHOICES, ApprovalRequest, Decision, Operation, PolicyView
from .blackboard import BoardPost, partition_mentions
from .board import STATUS_LABELS, TeamTask, blocks, is_ready
from .lock import TeamError
from .mailbox import BROADCAST, LEAD, RUNTIME_ONLY, Message, check
from .watchdog import MAX_PEER_HOPS, escalate_peer_handoff, reset_peer_hops, try_peer_handoff

if TYPE_CHECKING:
    from ..agent import AgentLoop
    from . import Team, TeamStore

POLL = 0.5  # Seconds between mailbox reads when nothing woke the reader.
SUMMARY = 300  # Characters of the last reply in an idle notification.
TYPE_LABELS = {
    "text": "消息",
    "plan_approval_request": "计划审批请求",
    "plan_approval_response": "计划审批答复",
    "shutdown_request": "停止请求",
    "shutdown_response": "停止答复",
    "idle_notification": "空闲通知",
    "permission_response": "审批答复",
}


@dataclass(frozen=True)
class PostResult:
    post: BoardPost
    wake_targets: tuple[str, ...]
    peer_targets: tuple[str, ...]
    mentioned_lead: bool
    unknown_mentions: tuple[str, ...] = ()
    breaker_post: BoardPost | None = None


def addendum(team: str, name: str, worktree: dict | None, role: str = "worker") -> str:
    """Fixed text, only the names and role differ."""
    lines = [
        f"<team-member>\n你是团队 {team} 的成员 {name}，Lead 的名称是 {LEAD}。",
        "- 你的文字回复不会被任何人看到：团队协作使用 post_board（在黑板追加 #seq 条目并用 @成员名 或 @lead 点名唤醒）"
        "或 send_message；点对点交接给同级成员（如 @integrator、@verifier）时不要同时点名 @lead。",
        "- 黑板定序与按需查阅：收到的黑板条目带有 [黑板 #seq · 作者] 标头；交接时引用 #seq 编号（如 #1），"
        "需要查看原始需求或上下文时调用 read_board（传 seqs 或 since）。代码块与反引号内的 @提及 不会唤醒成员。",
    ]
    if role == "verifier":
        lines.append(
            "- 你是只读审查员（verifier），没有文件写入/编辑工具；运行测试与审查 diff，"
            "发现问题用 post_board 点名 @integrator 或对应作者返工并引用 #seq，全部通过后再点名 @lead。"
        )
    else:
        if role == "integrator":
            lines.append(
                "- 你是合流员（integrator）：汇总并合流各作者提交、解决冲突并运行验证后，"
                "用 post_board 点名 @verifier 审查并附上 #seq；收到审查返工意见时修复后再点名 @verifier。"
            )
        lines.append(
            "- 工作流程：team_task_list 查看任务 → 领取就绪的任务（team_task_update 把 owner 设为自己、status 设为 in_progress）"
            "→ 完成 → git commit 提交改动 → 把任务设为 completed → 用 post_board（点名下游成员或 @lead）或 send_message 汇报结果。"
        )
    lines.extend(
        [
            "- 每次收到的消息都在 <incoming-messages> 中；处理完就结束这一轮，等待下一条消息，不要空等或轮询。",
            "- 收到 shutdown_request 时，结束手头的工作，用 send_message（type: shutdown_response，request_id 为该请求的 id，"
            "approve: true）回复；确有未完成的工作时可以 approve: false 并说明理由。",
        ]
    )
    if worktree:
        main = worktree.get("main") or "<主分支>"
        lines.append(
            f"- 你在自己的 git 工作树中工作：{worktree['path']}，分支 {worktree['branch']}，主分支是 {main}。"
            "完成的工作必须提交到这个分支，Lead 才能合并；Lead 要求你解决合并冲突时，在工作树中执行 "
            f"git merge {main}，解决冲突后提交。"
        )
    else:
        lines.append(
            "- 你在主项目目录中工作，与其他人共用文件；只改分配给你的文件。"
            if role != "verifier"
            else "- 你在主项目目录中执行只读审查与验证。"
        )
    return "\n".join(lines) + "\n</team-member>"


def member_mode(agent_mode: Callable[[str], str], session_mode: str, *, require: bool, approved: bool) -> str:
    """Plan until the lead approves; then the sub-agent rules against the current session mode."""
    if require and not approved:
        return "plan"
    return agent_mode(session_mode)


class MemberPolicy(PolicyView):
    """A member's exit_plan_mode goes to the lead, not to the user; the tool sends it."""

    async def _approve_plan(self, call_id, params, gate=None, notify=None) -> Decision:
        if self.mode != "plan":
            return Decision("deny", "not_in_plan", "当前不在计划模式，无需提交计划。")
        if gate is not None:
            rejected = await gate(None)
            if rejected is not None:
                return rejected
        return Decision("allow")


def _task_line(task: TeamTask, tasks: dict[str, TeamTask]) -> str:
    owner = task.owner or "未指派"
    waiting = [dep for dep in task.blocked_by if dep in tasks and tasks[dep].status != "completed"]
    tail = f"；等待 {'、'.join(waiting)}" if waiting else ("；就绪" if task.status == "pending" else "")
    return f"{task.id} [{STATUS_LABELS.get(task.status, task.status)}] {owner} · {task.subject}{tail}"


def task_listing(tasks: list[TeamTask]) -> str:
    if not tasks:
        return "还没有任务。"
    index = {task.id: task for task in tasks}
    return "\n".join(_task_line(task, index) for task in tasks)


def task_detail(task: TeamTask, tasks: list[TeamTask]) -> str:
    index = {item.id: item for item in tasks}
    lines = [
        f"任务 {task.id}：{task.subject}",
        f"状态：{STATUS_LABELS.get(task.status, task.status)}",
        f"负责人：{task.owner or '未指派'}",
        f"依赖：{'、'.join(task.blocked_by) or '无'}（{'就绪' if is_ready(task, index) else '未就绪'}）",
        f"被依赖：{'、'.join(blocks(task.id, tasks)) or '无'}",
        f"更新：{task.updated}",
    ]
    if task.description:
        lines += ["", task.description]
    return "\n".join(lines)


class TeamAccess:
    """What one participant (the lead or a member) does in a team: blackboard, control RPC and shared tasks.

    Methods are blocking file work; callers run them in a thread and then call wake() on the event loop.
    """

    def __init__(self, store: TeamStore, actor: str, wake: Callable[[str], None] | None = None):
        self.store = store
        self.actor = actor
        self._wake = wake
        self._last_breaker: BoardPost | None = None

    @property
    def team(self) -> str:
        return self.store.name

    def wake(self, names) -> None:
        if self._wake is not None:
            for name in names:
                self._wake(name)

    def participants(self) -> tuple[str, ...]:
        return self.store.load().names()

    def recipients(self, to: str, kind: str) -> list[str]:
        """Who a message goes to; TeamError when nobody or the wrong one."""
        names = self.participants()
        if to == BROADCAST:
            if kind != "text":
                raise TeamError("invalid_message", "只有 text 消息可以广播。")
            found = [name for name in names if name != self.actor]
            if not found:
                raise TeamError("unknown_member", "团队中没有其他成员可以接收广播。")
            return found
        if to == self.actor:
            raise TeamError("invalid_message", "不能给自己发消息。")
        if to != LEAD and to not in names:
            raise TeamError("unknown_member", f"团队 {self.team} 中没有成员 {to}；成员：{'、'.join(names) or '无'}。")
        return [to]

    def post_board(self, body: str, summary: str = "") -> PostResult:
        """Append a collaboration post to board.jsonl (sole source of truth for team text)."""
        participants = self.participants()
        resolved, unknown = partition_mentions(body, participants, author=self.actor)
        mentioned_lead = LEAD in resolved and self.actor != LEAD
        if BROADCAST in resolved:
            peer_targets = tuple(name for name in participants if name != self.actor)
        else:
            peer_targets = tuple(name for name in resolved if name not in {LEAD, self.actor})

        is_done_body = body.lstrip().lower().startswith("[done]")
        hops, tripped = (0, False)
        if self.actor == LEAD:
            reset_peer_hops(self.store)
        elif peer_targets and not mentioned_lead and not is_done_body:
            hops, tripped = try_peer_handoff(self.store)

        post = self.store.blackboard.append(self.actor, body, summary, valid_members=participants)
        if post.kind == "done":

            def mark_done(current: Team) -> None:
                if current.done_seq == 0:
                    current.done_seq = post.seq

            self.store.modify(mark_done)

        breaker_post: BoardPost | None = None
        if self.actor == LEAD:
            wake_targets = peer_targets
        elif peer_targets and not mentioned_lead and not is_done_body:
            if tripped:
                breaker_post = escalate_peer_handoff(self.store, self.actor, peer_targets, post.seq, hops=hops)
                wake_targets = (LEAD,)
            else:
                wake_targets = peer_targets
        else:
            wake_targets = (*peer_targets, *((LEAD,) if mentioned_lead else ()))

        return PostResult(
            post=post,
            wake_targets=wake_targets,
            peer_targets=peer_targets,
            mentioned_lead=mentioned_lead,
            unknown_mentions=unknown,
            breaker_post=breaker_post,
        )

    async def apost_board(self, body: str, summary: str = "") -> PostResult:
        outcome = await asyncio.to_thread(self.post_board, body, summary)
        self.wake(outcome.wake_targets)
        return outcome

    def send(
        self, to: str, kind: str, body: str = "", summary: str = "", *, runtime: bool = False, **extra
    ) -> list[str]:
        """Route text messages to board.jsonl (kind='direct'); control frames stay on mailbox."""
        if kind in RUNTIME_ONLY and not runtime:
            raise TeamError("invalid_message", f"{kind} 由系统自动发送，不能手动发送。")
        targets = self.recipients(to, kind)
        self._last_breaker = None
        if kind == "text":
            check(Message(self.actor, kind, body, summary, extra=dict(extra)))
            participants = self.participants()
            explicit = (BROADCAST,) if to == BROADCAST else tuple(targets)
            peer_targets = tuple(target for target in targets if target != LEAD)
            hops, tripped = (0, False)
            if self.actor == LEAD:
                reset_peer_hops(self.store)
            elif peer_targets and LEAD not in targets:
                hops, tripped = try_peer_handoff(self.store)
            post = self.store.blackboard.append(
                self.actor,
                body,
                summary,
                kind="direct",
                explicit_mentions=explicit,
                valid_members=participants,
            )
            if tripped:
                self._last_breaker = escalate_peer_handoff(self.store, self.actor, peer_targets, post.seq, hops=hops)
            return targets

        if self.actor == LEAD and kind == "plan_approval_response":
            reset_peer_hops(self.store)
        for target in targets:
            self.store.mailbox.send(target, Message(self.actor, kind, body, summary, extra=dict(extra)))
        return targets

    async def asend(self, to: str, kind: str, body: str = "", summary: str = "", **extra) -> list[str]:
        targets = await asyncio.to_thread(self.send, to, kind, body, summary, **extra)
        if self._last_breaker is not None:
            self.wake([LEAD])
        else:
            self.wake(targets)
        return targets

    # Shared tasks -----------------------------------------------------------------------

    def task_create(self, subject: str, description: str = "", blocked_by=(), owner: str = "") -> TeamTask:
        return self.store.board.create(
            subject, description, blocked_by, owner=owner, actor=self.actor, participants=self.participants()
        )

    def task_update(self, task_id: str, **changes):
        updated = self.store.board.update(task_id, actor=self.actor, participants=self.participants(), **changes)
        if updated.task.status == "completed":
            reset_peer_hops(self.store)
        return updated


def render_incoming(messages: Sequence[Message | BoardPost]) -> str:
    """One user message for every unread control message and blackboard mention of a turn."""
    parts = []
    for item in messages:
        if isinstance(item, BoardPost):
            head = f"[黑板 #{item.seq} · {item.author}]"
            parts.append(f"{head} 摘要：{item.summary}\n{item.body}")
            continue
        head = f"[来自 {item.sender} · {TYPE_LABELS.get(item.type, item.type)} · id={item.id}]"
        extra = item.extra
        if item.type == "text":
            parts.append(f"{head} 摘要：{item.summary}\n{item.body}")
        elif item.type == "plan_approval_response":
            verdict = "已批准" if extra.get("approve") else "被拒绝"
            parts.append(
                f"{head} 你的计划（request_id={extra.get('request_id')}）{verdict}。"
                + (f"\n反馈：{extra.get('feedback')}" if extra.get("feedback") else "")
            )
        elif item.type == "shutdown_request":
            parts.append(f"{head} Lead 请求你停止。理由：{extra.get('reason') or '无'}")
        else:
            parts.append(f"{head}\n{item.body}")
    return "<incoming-messages>\n" + "\n\n".join(parts) + "\n</incoming-messages>"


def approval_record(request: ApprovalRequest) -> dict:
    """A pane member's waiting request, as its record keeps it for the lead."""
    return {
        "id": request.id,
        "call_id": request.call_id,
        "operation": asdict(request.operation),
        "reason": request.reason,
        "choices": list(request.choices),
    }


def approval_from_record(item, source: str) -> ApprovalRequest | None:
    """The lead's side: None when the record cannot be shown or answered."""
    try:
        known = {field.name for field in fields(Operation)}
        operation = Operation(**{key: value for key, value in item["operation"].items() if key in known})
        choices = tuple(item["choices"])
        if not item["id"] or not choices or not all(choice in ALL_CHOICES for choice in choices):
            return None
        return ApprovalRequest(
            str(item["id"]), str(item.get("call_id", "")), operation, str(item.get("reason", "")), choices, source
        )
    except (KeyError, TypeError, AttributeError):
        return None


class MemberRunner:
    """Read control mailbox & blackboard mentions, run a turn, hand off or report, wait; until stopped."""

    def __init__(
        self,
        access: TeamAccess,
        name: str,
        *,
        role: str = "worker",
        agent_mode: Callable[[str], str],
        session_mode: Callable[[], str],
        require_plan_approval: bool = False,
        on_approval: Callable[[ApprovalRequest], None] | None = None,
        on_status: Callable[[str, dict], None] | None = None,
        poll: float = POLL,
        on_event: Callable[[object], None] | None = None,
    ):
        self.access, self.name, self.role = access, name, role
        self.loop: AgentLoop | None = None  # Set once the tools that need this runner exist.
        self.owned = False  # The loop's provider is closed when the member stops.
        self.commands, self.owner = None, ""  # Its background commands, stopped when it closes.
        self.agent_mode, self.session_mode = agent_mode, session_mode
        self.require_plan_approval = require_plan_approval
        self.approved = False
        self.awaiting: str | None = None  # request_id of the plan waiting for the lead.
        self.shutdown: bool | None = None  # Set by its shutdown_response: True stops after this turn.
        self.on_approval, self.on_status = on_approval, on_status
        self.on_event = on_event  # A pane member's own window shows the turn.
        self.poll = poll
        self.status = "idle"
        self.since = time.monotonic()  # When it entered its status.
        self.last_tool = ""
        self.note = ""  # Why the mode is what it is after an approval.
        self.wakeup = asyncio.Event()
        self.job: asyncio.Task | None = None
        self._run: asyncio.Task | None = None  # The model run of the current turn (Esc stops it).
        self._interrupted = False
        self.initial_post: BoardPost | None = None  # Turn-1 brief post when board_cursor already equals its seq.
        self._cursor = 0
        self._turn_woke_peers = False
        self._turn_mentioned_lead = False
        self._turn_breaker_tripped = False

    @property
    def policy(self):
        return self.loop.registry.permissions

    def wake(self) -> None:
        self.wakeup.set()

    def record_handoff(self, *, peers: Sequence[str], mentioned_lead: bool, breaker_tripped: bool = False) -> None:
        """Track whether this turn handed work to peer(s) without waking Lead."""
        if breaker_tripped:
            self._turn_breaker_tripped = True
            return
        if peers:
            self._turn_woke_peers = True
        if mentioned_lead:
            self._turn_mentioned_lead = True

    def interrupt(self) -> bool:
        """End the current turn early; the member still reports and goes idle."""
        if self._run is None or self._run.done():
            return False
        self._interrupted = True
        self._run.cancel()
        return True

    def start(self) -> asyncio.Task:
        self.job = asyncio.get_running_loop().create_task(self.serve())
        return self.job

    async def stop(self) -> None:
        if self.job is not None and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.close()

    async def close(self) -> None:
        if self.commands is not None:
            await self.commands.stop_owned(self.owner)
        if self.owned and self.loop is not None:
            self.owned = False
            await self.loop.provider.close()

    def _status(self, status: str, **fields) -> None:
        if status != self.status:
            self.since = time.monotonic()
        self.status = status
        if self.on_status is not None:
            self.on_status(status, fields)

    def _read_inputs(self) -> list[Message | BoardPost]:
        store = self.access.store
        messages = store.mailbox.read_unread(self.name)
        # An answer to an approval already settled in the member's own window; not for the model.
        messages = [message for message in messages if message.type != "permission_response"]
        board_posts: list[BoardPost] = []
        if self.initial_post is not None:
            board_posts.append(self.initial_post)
            self._cursor = max(self._cursor, self.initial_post.seq)
            self.initial_post = None
        try:
            team = store.load()
        except TeamError:
            return [*messages, *board_posts]
        member = team.member(self.name)
        if member is None:
            return [*messages, *board_posts]
        if team.peer_hops < MAX_PEER_HOPS:
            cursor = max(self._cursor, member.board_cursor)
            unread, head_seq = store.blackboard.unread_for(self.name, cursor)
            self._cursor = max(cursor, head_seq)
            if unread:
                new_cursor = unread[-1].seq

                def advance(current: Team) -> None:
                    record = current.member(self.name)
                    if record is not None and record.board_cursor < new_cursor:
                        record.board_cursor = new_cursor

                store.modify(advance)
                board_posts.extend(unread)
        return [*messages, *board_posts]

    async def serve(self) -> None:
        try:
            while self.shutdown is not True:
                if self.initial_post is None:
                    try:
                        await asyncio.wait_for(self.wakeup.wait(), self.poll)
                    except TimeoutError:
                        pass
                self.wakeup.clear()
                items = await asyncio.to_thread(self._read_inputs)
                if items:
                    await self.turn(items)
        finally:
            self._status("stopped")

    def submit_plan(self, plan: str) -> str:
        """From the member's exit_plan_mode; blocking (run in a thread by the tool)."""
        request_id = f"r-{secrets.token_hex(4)}"
        self.access.send(LEAD, "plan_approval_request", plan, runtime=True, request_id=request_id, plan=plan)
        self.awaiting = request_id
        return request_id

    def _absorb(self, messages: Sequence[Message]) -> None:
        """Answers to this member's plan change its state before the turn starts."""
        for message in messages:
            if message.type != "plan_approval_response" or message.extra.get("request_id") != self.awaiting:
                continue
            self.awaiting = None
            if message.extra.get("approve"):
                self.approved = True

    def _completed(self) -> set[str]:
        try:
            tasks = self.access.store.board.list()
        except TeamError:
            return set()
        return {task.id for task in tasks if task.owner == self.name and task.status == "completed"}

    def _has_incomplete_upstream(self) -> bool:
        try:
            tasks = self.access.store.board.list()
        except TeamError:
            return False
        return any(
            task.status != "completed"
            and task.owner != self.name
            and not (task.status == "pending" and task.blocked_by)
            for task in tasks
        )

    async def _consume(self, prompt: str):
        final, stopped = "", None
        async with aclosing(self.loop.run(prompt)) as events:
            async for event in events:
                if self.on_event is not None:
                    self.on_event(event)
                if isinstance(event, ApprovalRequest):
                    if self.on_approval is not None:
                        self.on_approval(event)
                elif isinstance(event, ToolProgress) and event.phase == "running":
                    from ..agents.runtime import describe_call

                    self.last_tool = describe_call(event.call, self.loop.registry.permissions.root)
                    self._status("running", last_tool=self.last_tool)
                elif isinstance(event, Completed):
                    final = event.answer
                elif isinstance(event, RunStopped):
                    stopped = event
        return final, stopped

    async def turn(self, messages: Sequence[Message | BoardPost]) -> None:
        self._turn_woke_peers = False
        self._turn_mentioned_lead = False
        self._turn_breaker_tripped = False
        control = [item for item in messages if isinstance(item, Message)]
        self._absorb(control)
        request = next((m for m in control if m.type == "shutdown_request"), None)
        if request is not None:
            self.shutdown = None  # Its shutdown_response this turn decides.
        mode = member_mode(
            self.agent_mode, self.session_mode(), require=self.require_plan_approval, approved=self.approved
        )
        prompt = render_incoming(messages)
        if self.on_event is not None:
            self.on_event(list(messages))
        if self.approved and mode == "plan":
            prompt += "\n（会话当前处于计划模式，你仍只能做只读调查；等用户退出计划模式后再动手。）"
        self.policy.set_mode(mode)
        before = await asyncio.to_thread(self._completed)
        self._status("running")
        final, stopped, failure, interrupted = "", None, "", False
        self._interrupted = False
        self._run = asyncio.ensure_future(self._consume(prompt))
        try:
            final, stopped = await self._run
        except ProviderError as exc:
            failure = str(exc)
        except asyncio.CancelledError:
            if not self._interrupted or asyncio.current_task().cancelling():
                raise
            failure, interrupted = "用户在成员窗格中中断了这一轮", True
        finally:
            self._run = None
        if not failure and (stopped is None or stopped.reason not in {"completed", "max_tokens"}):
            failure = stopped.notice if stopped is not None else "没有完整回复"
        done = sorted(
            await asyncio.to_thread(self._completed) - before, key=lambda key: int(key) if key.isdigit() else 0
        )
        self.last_tool = ""
        if request is not None and self.shutdown is None:
            self.shutdown = True  # It never answered: the request stands.
            try:
                await asyncio.to_thread(
                    self.access.send, LEAD, "shutdown_response", request_id=request.id, approve=True, reason=""
                )
                self.access.wake([LEAD])
            except TeamError:
                pass
        woke_peers_only = self._turn_woke_peers and not self._turn_mentioned_lead
        is_brief_turn = bool(messages) and all(isinstance(m, BoardPost) and m.kind == "brief" for m in messages)
        waiting_upstream = (
            self.role == "integrator"
            and not is_brief_turn
            and not self._turn_mentioned_lead
            and not self._turn_woke_peers
            and await asyncio.to_thread(self._has_incomplete_upstream)
        )
        role_quiet = (
            self.role in {"integrator", "verifier"}
            and not done
            and (is_brief_turn or self._turn_mentioned_lead or waiting_upstream)
        )
        suppress_idle = (not failure and not interrupted) and (
            woke_peers_only or role_quiet or self._turn_breaker_tripped
        )
        # Peer-only handoffs pass the baton directly without waking Lead; plans and shutdowns have their own replies.
        if (self.awaiting is None or failure) and self.shutdown is not True and not suppress_idle:
            reason = "interrupted" if interrupted else "failed" if failure else "completed"
            summary = (failure or final.strip())[:SUMMARY]
            try:
                await asyncio.to_thread(
                    self.access.send,
                    LEAD,
                    "idle_notification",
                    "",
                    summary,
                    runtime=True,
                    reason=reason,
                    completed_tasks=done,
                )
                self.access.wake([LEAD])
            except TeamError:
                pass  # The lead's mailbox is busy or gone; the status below still shows the member idle.
        usage = self.loop.total_usage
        self._status(
            "idle",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
            },
            approved=self.approved,
        )
