"""The lead's team tools and a member's message, task and plan tools."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import Field

from ..teams.lock import TeamError
from ..teams.mailbox import BODY_LIMIT, SUMMARY_LIMIT
from .base import Params, Tool, ToolFailure

if TYPE_CHECKING:
    from ..teams.member import MemberRunner, TeamAccess
    from ..teams.runtime import TeamRuntime

LEAD_TOOLS = ("team_create", "team_spawn", "team_delete", "team_merge", "team_task_create", "team_task_get",
              "team_task_list", "team_task_update", "send_message")
TEAM_TOOLS = frozenset(LEAD_TOOLS)  # GLOBAL_BAN for sub-agents and forks.

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
    description = ("新建一个团队并设为当前团队；同名团队已存在时打开它。团队由你（lead）和至多 4 个长期存在的成员组成，"
                   "成员各自在自己的 git 工作树中工作，通过消息和共享任务协作。")
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
    require_plan_approval: bool = False
    isolation: Literal["worktree", "none"] = "worktree"


class TeamSpawn(TeamTool):
    name = "team_spawn"
    timeout = 120.0  # Creating a worktree runs git.
    description = ("在当前团队中创建一个成员并开始它的第一轮。agent_type 是 Agent 定义名（默认 general），"
                   "prompt 是它的初始任务，要写清楚背景、范围和交付方式。默认在独立的 git 工作树中工作；"
                   "isolation: none 让它在主目录工作，只适合只读的角色。require_plan_approval 为 true 时，"
                   "成员先以计划模式调查并提交计划，由你批准后才能动手。")
    prompt_summary = "创建团队成员"
    params_model = TeamSpawnParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: TeamSpawnParams):
        try:
            return await self.runtime.spawn(params.name.strip(), params.agent_type.strip(), params.prompt,
                                            require_plan_approval=params.require_plan_approval,
                                            isolation=params.isolation)
        except TeamError as exc:
            raise failure(exc) from None


class TeamDeleteParams(Params):
    force: bool = False


class TeamDelete(TeamTool):
    name = "team_delete"
    plan_allowed = False
    description = ("删除当前团队：记录、邮箱、任务和成员对话。有成员在运行时需要 force: true，运行中的成员会先被停止。"
                   "成员的工作树和分支保留，用 /worktree 管理。")
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
    description = ("把一个成员工作树分支上的提交合并到主目录的当前分支（git merge --no-ff）。合并前会检查主目录和成员的状态，"
                   "并请用户确认。有多个成员时按任务依赖的顺序逐个合并；冲突时合并会被放弃，按返回的下一步让成员处理后再合并。")
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
    description = ("在当前团队中新建共享任务。blocked_by 列出它依赖的任务编号，依赖都完成后它才就绪；"
                   "owner 可以直接指派给已创建的成员；成员还没创建时先留空，之后用 team_task_update 指派。")
    prompt_summary = "新建团队任务"
    params_model = TaskCreateParams

    async def execute(self, params: TaskCreateParams):
        access = self._access()
        task = await self.run(access.task_create, params.subject, params.description, params.blocked_by,
                              params.owner.strip())
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
    description = ("修改当前团队的一个任务。领取任务时把 owner 设为自己、status 设为 in_progress；完成后设为 completed。"
                   "状态只能向前推进。")
    prompt_summary = "修改团队任务"
    params_model = TaskUpdateParams

    async def execute(self, params: TaskUpdateParams):
        from ..teams.board import STATUS_LABELS
        access = self._access()
        changes = {key: value for key, value in (("status", params.status), ("owner", params.owner),
                                                 ("subject", params.subject), ("description", params.description))
                   if value is not None}
        updated = await self.run(access.task_update, params.id.strip(), add_blocked_by=params.add_blocked_by,
                                 remove_blocked_by=params.remove_blocked_by, **changes)
        task = updated.task
        text = f"任务 {task.id}：{STATUS_LABELS.get(task.status, task.status)}，负责人 {task.owner or '未指派'}。"
        if updated.ready:
            text += "现在就绪：" + "、".join(f"{item.id}（{item.owner or '未指派'}）" for item in updated.ready) + "。"
        return text, {"team": access.team, "task": task.id, "status": task.status}


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
    description = ("给团队中的其他人发消息：to 是成员名、lead 或 *（广播给除你以外的全部成员，只限 text）。"
                   "text 消息必须有一句话的 summary，正文写在 body。对方会在它的下一轮开始时收到。"
                   "plan_approval_response（只限 lead）回复成员的计划：写上 request_id、approve 和 feedback；"
                   "shutdown_request（只限 lead）请成员停止；shutdown_response（只限成员）回复停止请求。")
    prompt_summary = "给团队成员或 lead 发消息"
    params_model = SendParams

    def __init__(self, root, runtime: TeamRuntime):
        super().__init__(root)
        self.runtime = runtime

    async def execute(self, params: SendParams):
        extra = extra_fields(params)
        try:
            targets, revived = await self.runtime.message(params.to.strip(), params.type, params.body, params.summary,
                                                          **extra)
        except TeamError as exc:
            raise failure(exc) from None
        text = sent(targets, params.type)
        if revived:
            text += "；".join(f"{name} 原已停止，已从记录恢复（{self.runtime.revived_with(name)}）"
                              for name in revived) + "，它会读到这条消息。"
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
        if params.type == "shutdown_response":
            self.runner.shutdown = bool(params.approve)  # True stops it after this turn.
            if params.approve:
                return sent(targets, params.type) + "这一轮结束后你将停止；现在收尾并结束这一轮。", {
                    "to": targets, "type": params.type}
        return sent(targets, params.type), {"to": targets, "type": params.type}


class MemberPlanParams(Params):
    plan: str = Field(min_length=1, max_length=20000)


class MemberExitPlan(Tool):
    """Same parameters as the main agent's; the plan goes to the lead, not the user."""
    name = "exit_plan_mode"
    plan_only = True
    description = ("计划模式下调查完成后调用，提交 Markdown 格式的实施计划给 lead 审批。提交后结束这一轮；"
                   "lead 的答复会作为下一条消息到达，批准后你才能写入、编辑和执行命令。")
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
        self.runner.access.wake(["lead"])
        return (f"计划已提交给 lead（request_id={request_id}），等待审批；收到回复前不要继续，现在结束这一轮。",
                {"request_id": request_id})


# Registries -----------------------------------------------------------------------------------------


def lead_tools(root, runtime: TeamRuntime) -> tuple[Tool, ...]:
    access = runtime.access
    return (TeamCreate(root, runtime), TeamSpawn(root, runtime), TeamDelete(root, runtime), TeamMerge(root, runtime),
            TeamTaskCreate(root, access), TeamTaskGet(root, access), TeamTaskList(root, access),
            TeamTaskUpdate(root, access), SendMessage(root, runtime))


def member_tools(root, access: TeamAccess, runner: MemberRunner) -> tuple[Tool, ...]:
    own = lambda: access  # noqa: E731
    tools: list[Tool] = [MemberSendMessage(root, access, runner), TeamTaskCreate(root, own), TeamTaskGet(root, own),
                         TeamTaskList(root, own), TeamTaskUpdate(root, own)]
    if runner.require_plan_approval:
        tools.append(MemberExitPlan(root, runner))
    return tuple(tools)
