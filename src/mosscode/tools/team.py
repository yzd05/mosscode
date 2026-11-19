"""The lead's team tools and a member's blackboard, message, task and plan tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from ..teams.blackboard import BODY_LIMIT as BOARD_BODY_LIMIT
from ..teams.blackboard import SUMMARY_LIMIT as BOARD_SUMMARY_LIMIT
from ..teams.blackboard import BoardPost
from ..teams.lock import TeamError
from ..teams.mailbox import BODY_LIMIT, LEAD, SUMMARY_LIMIT
from .base import Params, Tool, ToolFailure

if TYPE_CHECKING:
    from ..teams.member import MemberRunner, PostResult, TeamAccess
    from ..teams.runtime import TeamRuntime

LEAD_TOOLS = (
    "team_create",
    "team_spawn",
    "team_delete",
    "team_merge",
    "team_task_create",
    "team_task_get",
    "team_task_list",
    "team_task_update",
    "send_message",
    "post_board",
    "read_board",
)
TEAM_TOOLS = frozenset(LEAD_TOOLS)  # GLOBAL_BAN for sub-agents and forks.

try:
    from ..agents import runtime as _agents_runtime

    _agents_runtime.GLOBAL_BAN = _agents_runtime.GLOBAL_BAN | TEAM_TOOLS
except ImportError:  # pragma: no cover
    pass

MESSAGE_TYPES = Literal["text", "plan_approval_response", "shutdown_request", "shutdown_response"]


class TeamTool(Tool):
    permission_kind = "team"  # Team bookkeeping needs no approval; members' own calls are checked one by one.
    plan_allowed = True

    async def run(self, function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except TeamError as exc:
            raise ToolFailure(exc.code, exc.message) from None


def failure(exc: TeamError) -> ToolFailure:
    return ToolFailure(exc.code, exc.message)


# Lead -----------------------------------------------------------------------------------------------


class TeamCreateParams(Params):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=500)


class TeamCreate(TeamTool):
    name = "team_create"
    description = (
        "新建一个团队并设为当前团队；同名团队已存在时打开它。团队由你（lead）和至多 6 个长期存在的活跃成员组成，"
        "成员各自在自己的 git 工作树（或只读主目录）中工作，通过共享黑板（post_board / read_board）和共享任务协作。"
    )
    prompt_summary = "新建或打开团队"
    params_model = TeamCreateParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: TeamCreateParams):
        try:
            return await self.runtime.create(params.name.strip(), params.description)
        except TeamError as exc:
            raise failure(exc) from None


class TeamSpawnParams(Params):
    name: str = Field(min_length=1, max_length=32)
    agent_type: str = Field(default="general", max_length=64)
    prompt: str = Field(min_length=1, max_length=BODY_LIMIT)
    role: Literal["worker", "integrator", "verifier"] = "worker"
    require_plan_approval: bool = False
    isolation: Literal["auto", "worktree", "none"] = "worktree"


class TeamSpawn(TeamTool):
    name = "team_spawn"
    timeout = 120.0  # Creating a worktree runs git.
    description = (
        "在当前团队中创建一个成员并开始它的第一轮。agent_type 是 Agent 定义名（默认 general），"
        "role 可选 worker（默认）、integrator（合流员）或 verifier（只读审查员，硬移除文件写入/编辑工具）。"
        "prompt 是它的初始任务（记录到黑板 #seq），要写清楚背景、范围和交付方式。"
        "worker 与 integrator 默认在独立的 git 工作树中工作，verifier 默认在主目录只读工作（isolation: none）。"
        "require_plan_approval 为 true 时，成员先以计划模式调查并提交计划，由你审批后才能动手。"
    )
    prompt_summary = "创建团队成员"
    params_model = TeamSpawnParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: TeamSpawnParams):
        isolation = params.isolation if "isolation" in params.model_fields_set else "auto"
        try:
            return await self.runtime.spawn(
                params.name.strip(),
                params.agent_type.strip(),
                params.prompt,
                role=params.role,
                require_plan_approval=params.require_plan_approval,
                isolation=isolation,
            )
        except TeamError as exc:
            raise failure(exc) from None


class TeamDeleteParams(Params):
    force: bool = False


class TeamDelete(TeamTool):
    name = "team_delete"
    plan_allowed = False
    description = (
        "删除当前团队：记录、黑板、邮箱、任务和成员对话。有成员在运行时需要 force: true，运行中的成员会先被停止。"
        "成员的工作树和分支保留，用 /worktree 管理。"
    )
    prompt_summary = "删除当前团队"
    params_model = TeamDeleteParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: TeamDeleteParams):
        try:
            return await self.runtime.delete(force=params.force)
        except TeamError as exc:
            raise failure(exc) from None


class TeamMergeParams(Params):
    member: str = Field(min_length=1, max_length=32)


class TeamMerge(TeamTool):
    name = "team_merge"
    plan_allowed = False
    timeout = None  # It waits for the user's answer.
    description = (
        "把一个成员工作树分支上的提交合并到主目录的当前分支（git merge --no-ff）。合并前会检查主目录和成员的状态，"
        "并请用户确认。有多个成员时按任务依赖的顺序逐个合并；冲突时合并会被放弃，按返回的下一步让成员处理后再合并。"
    )
    prompt_summary = "合并成员的分支"
    params_model = TeamMergeParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: TeamMergeParams):
        try:
            return await self.runtime.merge(params.member.strip())
        except TeamError as exc:
            raise failure(exc) from None


# Shared tasks (lead and members) ---------------------------------------------------------------------


class TaskCreateParams(Params):
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    blocked_by: list[str] = Field(default_factory=list, max_length=50)
    owner: str = Field(default="", max_length=32)


class TaskGetParams(Params):
    id: str = Field(min_length=1, max_length=10)


class TaskListParams(Params):
    pass


class TaskUpdateParams(Params):
    id: str = Field(min_length=1, max_length=10)
    status: Literal["pending", "in_progress", "completed"] | None = None
    owner: str | None = Field(default=None, max_length=32)
    subject: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    add_blocked_by: list[str] = Field(default_factory=list, max_length=50)
    remove_blocked_by: list[str] = Field(default_factory=list, max_length=50)


class SharedTaskTool(TeamTool):
    """access() is the lead's current team (no_team without one) or the member's own team."""

    def __init__(self, root, access: Callable[[], TeamAccess]):
        super().__init__(root)
        self.access = access

    def _access(self) -> TeamAccess:
        try:
            return self.access()
        except TeamError as exc:
            raise failure(exc) from None


class TeamTaskCreate(SharedTaskTool):
    name = "team_task_create"
    description = (
        "在当前团队中新建共享任务。blocked_by 列出它依赖的任务编号，依赖都完成后它才就绪；"
        "owner 可以直接指派给已创建的成员；成员还没创建时先留空，之后用 team_task_update 指派。"
    )
    prompt_summary = "新建团队任务"
    params_model = TaskCreateParams

    async def execute(self, params: TaskCreateParams):
        access = self._access()
        task = await self.run(
            access.task_create, params.subject, params.description, params.blocked_by, params.owner.strip()
        )
        waits = f"，依赖 {'、'.join(task.blocked_by)}" if task.blocked_by else ""
        owner = f"，负责人 {task.owner}" if task.owner else ""
        return f"已新建任务 {task.id}：{task.subject}{waits}{owner}。", {"team": access.team, "task": task.id}


class TeamTaskGet(SharedTaskTool):
    name = "team_task_get"
    read_only = True
    description = "查看当前团队的一个任务：状态、负责人、依赖、被依赖和说明。"
    prompt_summary = "查看团队任务"
    params_model = TaskGetParams

    async def execute(self, params: TaskGetParams):
        from ..teams.member import task_detail

        access = self._access()
        board = access.store.board

        def read():
            return board.get(params.id.strip()), board.list()

        task, tasks = await self.run(read)
        return task_detail(task, tasks), {"team": access.team, "task": task.id}


class TeamTaskList(SharedTaskTool):
    name = "team_task_list"
    read_only = True
    description = "列出当前团队的全部任务：编号、状态、负责人、标题，以及是否就绪或在等待哪些任务。"
    prompt_summary = "列出团队任务"
    params_model = TaskListParams

    async def execute(self, params: TaskListParams):
        from ..teams.member import task_listing

        access = self._access()
        tasks = await self.run(access.store.board.list)
        return task_listing(tasks), {"team": access.team, "tasks": len(tasks)}


class TeamTaskUpdate(SharedTaskTool):
    name = "team_task_update"
    description = (
        "修改当前团队的一个任务。领取任务时把 owner 设为自己、status 设为 in_progress；完成后设为 completed。"
        "状态只能向前推进。"
    )
    prompt_summary = "修改团队任务"
    params_model = TaskUpdateParams

    async def execute(self, params: TaskUpdateParams):
        from ..teams.board import STATUS_LABELS

        access = self._access()
        changes = {
            key: value
            for key, value in (
                ("status", params.status),
                ("owner", params.owner),
                ("subject", params.subject),
                ("description", params.description),
            )
            if value is not None
        }
        updated = await self.run(
            access.task_update,
            params.id.strip(),
            add_blocked_by=params.add_blocked_by,
            remove_blocked_by=params.remove_blocked_by,
            **changes,
        )
        task = updated.task
        text = f"任务 {task.id}：{STATUS_LABELS.get(task.status, task.status)}，负责人 {task.owner or '未指派'}。"
        if updated.ready:
            text += "现在就绪：" + "、".join(f"{item.id}（{item.owner or '未指派'}）" for item in updated.ready) + "。"
        return text, {"team": access.team, "task": task.id, "status": task.status}


# Blackboard (lead and members) -----------------------------------------------------------------------


class PostBoardParams(Params):
    body: str = Field(min_length=1, max_length=BOARD_BODY_LIMIT)
    summary: str = Field(default="", max_length=BOARD_SUMMARY_LIMIT)


class ReadBoardParams(Params):
    since: int | None = Field(default=None, ge=0)
    seqs: list[int] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


def _format_post_result(
    outcome: PostResult, revived: list[str] | None = None, runtime: TeamRuntime | None = None
) -> str:
    post = outcome.post
    parts = [f"已发布黑板 #{post.seq}"]
    if post.mentions:
        shown = "、".join("@all" if name == "*" else f"@{name}" for name in post.mentions)
        parts.append(f"（点名：{shown}）")
    text = "".join(parts) + "。"
    if outcome.unknown_mentions:
        ignored = "、".join(f"@{name}" for name in outcome.unknown_mentions)
        text += f"未识别的提及已忽略：{ignored}。"
    if outcome.breaker_post is not None:
        text += (
            f"点对点交接已达上限，自动唤醒已熔断并转交 @lead（#{outcome.breaker_post.seq}）；请结束本轮等待 Lead 指示。"
        )
    if revived and runtime is not None:
        text += (
            "；".join(f"{name} 原已停止，已从记录恢复（{runtime.revived_with(name)}）" for name in revived)
            + "，它会读到这条黑板消息。"
        )
    return text


def _format_board_posts(posts: list[BoardPost], head_seq: int) -> str:
    if not posts:
        return f"黑板暂无匹配的条目（当前最新 #{head_seq}）。"
    blocks = [f"[黑板 #{post.seq} · {post.author} · {post.kind}] 摘要：{post.summary}\n{post.body}" for post in posts]
    return "\n\n".join(blocks) + f"\n\n（共 {len(posts)} 条，当前黑板最新 #{head_seq}）"


class PostBoard(TeamTool):
    name = "post_board"
    description = (
        "向当前团队的共享追加式黑板（board.jsonl）发布一条记录并分配单调递增的 #seq 编号。"
        "在正文（代码块和反引号之外）写 @成员名、@lead 或 @all 即可唤醒对应角色；"
        "用 #seq 引用已有条目；全部收尾时以 [done] 开头标记团队完成。"
    )
    prompt_summary = "在团队黑板发布并 @点名交接"
    params_model = PostBoardParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: PostBoardParams):
        try:
            outcome, revived = await self.runtime.post_board(params.body, params.summary)
        except TeamError as exc:
            raise failure(exc) from None
        post = outcome.post
        return _format_post_result(outcome, revived, self.runtime), {
            "team": self.runtime.current.name if self.runtime.current else "",
            "seq": post.seq,
            "kind": post.kind,
            "mentions": list(post.mentions),
            "refs": list(post.refs),
            "revived": revived,
        }


class MemberPostBoard(PostBoard):
    def __init__(self, root, access: TeamAccess, runner: MemberRunner):
        Tool.__init__(self, root)
        self.access, self.runner = access, runner

    async def execute(self, params: PostBoardParams):
        try:
            outcome = await self.access.apost_board(params.body, params.summary)
        except TeamError as exc:
            raise failure(exc) from None
        self.runner.record_handoff(
            peers=outcome.peer_targets,
            mentioned_lead=outcome.mentioned_lead,
            breaker_tripped=outcome.breaker_post is not None,
        )
        post = outcome.post
        return _format_post_result(outcome), {
            "team": self.access.team,
            "seq": post.seq,
            "kind": post.kind,
            "mentions": list(post.mentions),
            "refs": list(post.refs),
        }


class ReadBoard(SharedTaskTool):
    name = "read_board"
    read_only = True
    description = (
        "按需读取团队黑板（board.jsonl）：传 seqs 精确读取指定 #seq 条目（如初始任务 #1 或上游交接记录），"
        "或传 since 读取该序号之后的新条目；都不传时返回最近的 limit 条记录。"
    )
    prompt_summary = "读取团队黑板记录"
    params_model = ReadBoardParams

    async def execute(self, params: ReadBoardParams):
        access = self._access()
        blackboard = access.store.blackboard
        if params.seqs:
            posts, head_seq = await self.run(blackboard.read_seqs, params.seqs)
        elif params.since is not None:
            posts, head_seq = await self.run(blackboard.read_since, params.since, limit=params.limit)
        else:
            posts, head_seq = await self.run(blackboard.read_recent, params.limit)
        return _format_board_posts(posts, head_seq), {
            "team": access.team,
            "count": len(posts),
            "head_seq": head_seq,
            "seqs": [post.seq for post in posts],
        }


class MemberReadBoard(ReadBoard):
    """Member read_board advances board_cursor monotonically on contiguous scans to avoid stale re-wakeups."""

    def __init__(self, root, access: TeamAccess, runner: MemberRunner):
        super().__init__(root, lambda: access)
        self.runner = runner

    async def execute(self, params: ReadBoardParams):
        access = self._access()
        blackboard = access.store.blackboard
        cursor = self.runner.cursor
        covered = 0
        if params.seqs:
            posts, head_seq = await self.run(blackboard.read_seqs, params.seqs)
        elif params.since is not None:
            posts, head_seq = await self.run(blackboard.read_since, params.since, limit=params.limit)
            if params.since <= cursor:
                covered = head_seq if len(posts) < params.limit else (posts[-1].seq if posts else cursor)
        else:
            posts, head_seq = await self.run(blackboard.read_recent, params.limit)
            if not posts or posts[0].seq <= cursor + 1:
                covered = head_seq
        if covered > cursor:
            try:
                # Transient lock contention must not abort a read-only board query; unread mentions will simply retry.
                await asyncio.to_thread(self.runner.advance_cursor, covered)
            except TeamError:
                pass
        return _format_board_posts(posts, head_seq), {
            "team": access.team,
            "count": len(posts),
            "head_seq": head_seq,
            "seqs": [post.seq for post in posts],
        }


# Messages -------------------------------------------------------------------------------------------


class SendParams(Params):
    to: str = Field(min_length=1, max_length=32)
    type: MESSAGE_TYPES = "text"
    body: str = Field(default="", max_length=BODY_LIMIT)
    summary: str = Field(default="", max_length=SUMMARY_LIMIT)
    request_id: str = Field(default="", max_length=40)
    approve: bool | None = None
    feedback: str = Field(default="", max_length=4000)


def extra_fields(params: SendParams) -> dict:
    """Each type's own fields; a missing one is invalid_message."""
    if params.type == "plan_approval_response":
        if params.approve is None:
            raise ToolFailure("invalid_message", "plan_approval_response 需要 approve（true 或 false）。")
        return {"request_id": params.request_id, "approve": params.approve, "feedback": params.feedback}
    if params.type == "shutdown_request":
        return {"reason": params.body or params.feedback}
    if params.type == "shutdown_response":
        if params.approve is None:
            raise ToolFailure("invalid_message", "shutdown_response 需要 approve（true 或 false）。")
        if not params.request_id:
            raise ToolFailure("invalid_message", "shutdown_response 需要对应 shutdown_request 的 request_id。")
        return {"request_id": params.request_id, "approve": params.approve, "reason": params.feedback or params.body}
    return {}


def sent(targets: list[str], kind: str) -> str:
    return f"已发送 {kind} 给 {'、'.join(targets)}。"


class SendMessage(TeamTool):
    name = "send_message"
    description = (
        "给团队中的其他人发消息：to 是成员名、lead 或 *（广播给除你以外的全部成员，只限 text）。"
        "text 消息会记录到共享黑板并唤醒目标；控制消息用于审批或停止。"
        "plan_approval_response（只限 lead）回复成员的计划：写上 request_id、approve 和 feedback；"
        "shutdown_request（只限 lead）请成员停止；shutdown_response（只限成员）回复停止请求。"
    )
    prompt_summary = "给团队成员或 lead 发消息"
    params_model = SendParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: SendParams):
        extra = extra_fields(params)
        try:
            targets, revived = await self.runtime.message(
                params.to.strip(), params.type, params.body, params.summary, **extra
            )
        except TeamError as exc:
            raise failure(exc) from None
        text = sent(targets, params.type)
        if revived:
            text += (
                "；".join(f"{name} 原已停止，已从记录恢复（{self.runtime.revived_with(name)}）" for name in revived)
                + "，它会读到这条消息。"
            )
        return text, {"to": targets, "type": params.type, "revived": revived}


class MemberSendMessage(SendMessage):
    def __init__(self, root, access: TeamAccess, runner: MemberRunner):
        Tool.__init__(self, root)
        self.access, self.runner = access, runner

    async def execute(self, params: SendParams):
        extra = extra_fields(params)
        try:
            targets = await self.access.asend(params.to.strip(), params.type, params.body, params.summary, **extra)
        except TeamError as exc:
            raise failure(exc) from None
        if params.type == "text":
            peers = [target for target in targets if target != LEAD]
            self.runner.record_handoff(
                peers=peers,
                mentioned_lead=LEAD in targets,
                breaker_tripped=self.access._last_breaker is not None,
            )
        if params.type == "shutdown_response":
            self.runner.shutdown = bool(params.approve)  # True stops it after this turn.
            if params.approve:
                return sent(targets, params.type) + "这一轮结束后你将停止；现在收尾并结束这一轮。", {
                    "to": targets,
                    "type": params.type,
                }
        return sent(targets, params.type), {"to": targets, "type": params.type}


class MemberPlanParams(Params):
    plan: str = Field(min_length=1, max_length=20000)


class MemberExitPlan(Tool):
    """Same parameters as the main agent's; the plan goes to the lead, not the user."""

    name = "exit_plan_mode"
    plan_only = True
    description = (
        "计划模式下调查完成后调用，提交 Markdown 格式的实施计划给 lead 审批。提交后结束这一轮；"
        "lead 的答复会作为下一条消息到达，批准后你才能写入、编辑和执行命令。"
    )
    prompt_summary = "提交计划给 lead 审批"
    params_model = MemberPlanParams

    def __init__(self, root, runner: MemberRunner):
        super().__init__(root)
        self.runner = runner

    async def execute(self, params: MemberPlanParams):
        try:
            request_id = await asyncio.to_thread(self.runner.submit_plan, params.plan)
        except TeamError as exc:
            raise failure(exc) from None
        self.runner.access.wake([LEAD])
        return (
            f"计划已提交给 lead（request_id={request_id}），等待审批；收到回复前不要继续，现在结束这一轮。",
            {"request_id": request_id},
        )


# Registries -----------------------------------------------------------------------------------------


def lead_tools(root, runtime: TeamRuntime) -> tuple[Tool, ...]:
    access = runtime.access
    return (
        TeamCreate(root, runtime),
        TeamSpawn(root, runtime),
        TeamDelete(root, runtime),
        TeamMerge(root, runtime),
        TeamTaskCreate(root, access),
        TeamTaskGet(root, access),
        TeamTaskList(root, access),
        TeamTaskUpdate(root, access),
        SendMessage(root, runtime),
        PostBoard(root, runtime),
        ReadBoard(root, access),
    )


def member_tools(root, access: TeamAccess, runner: MemberRunner) -> tuple[Tool, ...]:
    own = lambda: access  # noqa: E731
    tools: list[Tool] = [
        MemberPostBoard(root, access, runner),
        MemberReadBoard(root, access, runner),
        MemberSendMessage(root, access, runner),
        TeamTaskCreate(root, own),
        TeamTaskGet(root, own),
        TeamTaskList(root, own),
        TeamTaskUpdate(root, own),
    ]
    if runner.require_plan_approval:
        tools.append(MemberExitPlan(root, runner))
    return tuple(tools)
