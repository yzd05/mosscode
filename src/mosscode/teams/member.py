"""A member's long-lived conversation, and what any participant may do in a team.

The same runner serves an in-process member and a member in its own terminal pane.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import asdict, fields
from typing import TYPE_CHECKING

from ..messages import Completed, ProviderError, RunStopped, ToolProgress
from ..permissions import ALL_CHOICES, ApprovalRequest, Decision, Operation, PolicyView
from .board import STATUS_LABELS, TeamTask, blocks, is_ready
from .lock import TeamError
from .mailbox import BROADCAST, LEAD, RUNTIME_ONLY, Message

if TYPE_CHECKING:
    from ..agent import AgentLoop
    from . import TeamStore

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


def addendum(team: str, name: str, worktree: dict | None) -> str:
    """Fixed text, only the names differ."""
    lines = [
        f"<team-member>\n你是团队 {team} 的成员 {name}，Lead 的名称是 {LEAD}。",
        "- 你的文字回复不会被任何人看到：汇报、提问、交付结果都必须用 send_message（通常发给 lead）。",
        "- 工作流程：team_task_list 查看任务 → 领取就绪的任务（team_task_update 把 owner 设为自己、status 设为 in_progress）"
        "→ 完成 → git commit 提交改动 → 把任务设为 completed → send_message 向 lead 汇报结果。",
        "- 每次收到的消息都在 <incoming-messages> 中；处理完就结束这一轮，等待下一条消息，不要空等或轮询。",
        "- 收到 shutdown_request 时，结束手头的工作，用 send_message（type: shutdown_response，request_id 为该请求的 id，"
        "approve: true）回复；确有未完成的工作时可以 approve: false 并说明理由。",
    ]
    if worktree:
        main = worktree.get("main") or "<主分支>"
        lines.append(
            f"- 你在自己的 git 工作树中工作：{worktree['path']}，分支 {worktree['branch']}，主分支是 {main}。"
            "完成的工作必须提交到这个分支，Lead 才能合并；Lead 要求你解决合并冲突时，在工作树中执行 "
            f"git merge {main}，解决冲突后提交。"
        )
    else:
        lines.append("- 你在主项目目录中工作，与其他人共用文件；只改分配给你的文件。")
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
    """What one participant (the lead or a member) does in a team: messages and shared tasks.

    Methods are blocking file work; callers run them in a thread and then call wake() on the event loop.
    """

    def __init__(self, store: TeamStore, actor: str, wake: Callable[[str], None] | None = None):
        self.store = store
        self.actor = actor
        self._wake = wake

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

    def send(
        self, to: str, kind: str, body: str = "", summary: str = "", *, runtime: bool = False, **extra
    ) -> list[str]:
        """The model never sends idle notifications or plan requests itself (runtime=False)."""
        if kind in RUNTIME_ONLY and not runtime:
            raise TeamError("invalid_message", f"{kind} 由系统自动发送，不能手动发送。")
        targets = self.recipients(to, kind)
        for target in targets:
            self.store.mailbox.send(target, Message(self.actor, kind, body, summary, extra=dict(extra)))
        return targets

    async def asend(self, to: str, kind: str, body: str = "", summary: str = "", **extra) -> list[str]:
        targets = await asyncio.to_thread(self.send, to, kind, body, summary, **extra)
        self.wake(targets)
        return targets

    # Shared tasks -----------------------------------------------------------------------

    def task_create(self, subject: str, description: str = "", blocked_by=(), owner: str = "") -> TeamTask:
        return self.store.board.create(
            subject, description, blocked_by, owner=owner, actor=self.actor, participants=self.participants()
        )

    def task_update(self, task_id: str, **changes):
        return self.store.board.update(task_id, actor=self.actor, participants=self.participants(), **changes)


def render_incoming(messages: list[Message]) -> str:
    """One user message for every unread message of a turn."""
    parts = []
    for message in messages:
        head = f"[来自 {message.sender} · {TYPE_LABELS.get(message.type, message.type)} · id={message.id}]"
        extra = message.extra
        if message.type == "text":
            parts.append(f"{head} 摘要：{message.summary}\n{message.body}")
        elif message.type == "plan_approval_response":
            verdict = "已批准" if extra.get("approve") else "被拒绝"
            parts.append(
                f"{head} 你的计划（request_id={extra.get('request_id')}）{verdict}。"
                + (f"\n反馈：{extra.get('feedback')}" if extra.get("feedback") else "")
            )
        elif message.type == "shutdown_request":
            parts.append(f"{head} Lead 请求你停止。理由：{extra.get('reason') or '无'}")
        else:
            parts.append(f"{head}\n{message.body}")
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
    """Read the mailbox, run a turn, tell the lead, wait; until stopped."""

    def __init__(
        self,
        access: TeamAccess,
        name: str,
        *,
        agent_mode: Callable[[str], str],
        session_mode: Callable[[], str],
        require_plan_approval: bool = False,
        on_approval: Callable[[ApprovalRequest], None] | None = None,
        on_status: Callable[[str, dict], None] | None = None,
        poll: float = POLL,
        on_event: Callable[[object], None] | None = None,
    ):
        self.access, self.name = access, name
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

    @property
    def policy(self):
        return self.loop.registry.permissions

    def wake(self) -> None:
        self.wakeup.set()

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

    async def serve(self) -> None:
        try:
            while self.shutdown is not True:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), self.poll)
                except TimeoutError:
                    pass
                self.wakeup.clear()
                messages = await asyncio.to_thread(self.access.store.mailbox.read_unread, self.name)
                # An answer to an approval already settled in the member's own window; not for the model.
                messages = [message for message in messages if message.type != "permission_response"]
                if messages:
                    await self.turn(messages)
        finally:
            self._status("stopped")

    def submit_plan(self, plan: str) -> str:
        """From the member's exit_plan_mode; blocking (run in a thread by the tool)."""
        request_id = f"r-{secrets.token_hex(4)}"
        self.access.send(LEAD, "plan_approval_request", plan, runtime=True, request_id=request_id, plan=plan)
        self.awaiting = request_id
        return request_id

    def _absorb(self, messages: list[Message]) -> None:
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
                    final = event.message.text
                elif isinstance(event, RunStopped):
                    stopped = event
        return final, stopped

    async def turn(self, messages: list[Message]) -> None:
        self._absorb(messages)
        request = next((m for m in messages if m.type == "shutdown_request"), None)
        if request is not None:
            self.shutdown = None  # Its shutdown_response this turn decides.
        mode = member_mode(
            self.agent_mode, self.session_mode(), require=self.require_plan_approval, approved=self.approved
        )
        prompt = render_incoming(messages)
        if self.on_event is not None:
            self.on_event(messages)
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
        # The plan request itself tells the lead; a member that stops says so in its shutdown_response.
        if (self.awaiting is None or failure) and self.shutdown is not True:
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
