"""The agent tool and the two task tools of the main conversation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from .base import CURRENT_CALL, Params, Tool, ToolFailure

if TYPE_CHECKING:
    from ..agents.runtime import SubAgents

WAIT_LIMIT = 300
PREVIEW = 4000  # Characters of a running task's last reply shown by task_output.


class AgentParams(Params):
    description: str = Field(min_length=1, max_length=60)
    prompt: str = Field(min_length=1, max_length=40000)
    agent_type: str = Field(default="", max_length=64)
    run_in_background: bool = False
    isolation: Literal["none", "worktree"] = "none"


class AgentTool(Tool):
    name = "agent"
    permission_kind = "agent"  # No approval: every call the sub-agent makes is checked on its own.
    plan_allowed = True  # In plan mode its sub-agents are in plan mode too.
    timeout = None  # Foreground sub-agents end on their own, or move to the background.
    description = (
        "把一段可以独立完成的工作交给子 Agent，你只收到它的结论。系统提示的“子 Agent”一节列出了可用的类型。"
        "agent_type 写类型名时，子 Agent 从空白对话开始，prompt 必须写清楚完整的任务、背景和期望的结果；"
        "省略 agent_type 时为 Fork：它继承当前对话的全部内容，总在后台运行，完成后你会收到 <task-notification>。"
        "description 是 3–10 个字的任务标签。isolation 为 worktree 时，子 Agent 在独立的 git 工作树中运行。"
    )
    prompt_summary = "把独立的子任务交给子 Agent"
    prompt_guidelines = (
        "适合委派：需要大量搜索或阅读、但只需要结论的调查；互不依赖的多个子任务（在一次回复中同时发起，它们会并行运行）。",
        "不适合委派：一两次工具调用就能完成的事；需要与用户来回确认的事。",
        "子 Agent 不能向用户提问；它的结论不会直接显示给用户，需要时由你转述。",
        "run_in_background 为 true 时立即返回任务 id；不要在等待通知期间重复做同一件事，需要结果时用 task_output。",
        "会改动文件、且可能与你或其他子 Agent 的改动冲突的任务（例如并行的实验性修改），用 isolation: worktree 让它在独立的"
        "工作树中进行；有改动时工作树会保留，结果中给出分支名，由你决定是否合并。Fork 不能隔离。",
    )
    params_model = AgentParams

    def __init__(self, root, runner: SubAgents):
        super().__init__(root)
        self.runner = runner

    async def execute(self, params: AgentParams):
        call = CURRENT_CALL.get()
        return await self.runner.call(
            params.description.strip(),
            params.prompt,
            params.agent_type.strip(),
            params.run_in_background,
            call.id if call is not None else "",
            isolation=params.isolation,
        )


class TaskOutputParams(Params):
    task_id: str = Field(min_length=1, max_length=20)
    wait: int = Field(default=0, ge=0, le=WAIT_LIMIT)


class TaskOutput(Tool):
    name = "task_output"
    permission_kind = "agent"
    read_only = True
    timeout = WAIT_LIMIT + 10
    description = (
        "查看后台任务（子 Agent 或命令）的状态与结果；命令任务给出退出码、输出文件路径和最后 50 行输出。"
        "wait 为等待完成的秒数（最多 300），0 表示立即返回；任务完成时你也会自动收到 <task-notification>，一般不需要轮询。"
    )
    prompt_summary = "查看后台任务的状态与结果"
    params_model = TaskOutputParams

    def __init__(self, root, runner: SubAgents):
        super().__init__(root)
        self.runner = runner

    async def execute(self, params: TaskOutputParams):
        task = self.runner.tasks.get(params.task_id.strip())
        if task is None:
            raise ToolFailure("unknown_task", f"没有任务 {params.task_id}。")
        if task.status == "running" and params.wait and task.job is not None:
            await asyncio.wait({task.job}, timeout=params.wait)
        if task.command is not None:
            from .background import report

            return report(task), {"task": task.id, "status": task.status, "exit_code": task.command.exit_code}
        from ..agents.runtime import stats
        from ..agents.tasks import STATUS_LABELS

        head = f"任务 {task.id}（{task.label}）：{STATUS_LABELS[task.status]}，{stats(task)}。"
        if task.status == "running":
            last = f"最近一次工具调用：{task.last_tool}。" if task.last_tool else "尚未调用工具。"
            return f"{head}\n{last}", {"task": task.id, "status": task.status}
        return f"{head}\n\n{task.result or ''}", {"task": task.id, "status": task.status}


class TaskStopParams(Params):
    task_id: str = Field(min_length=1, max_length=20)


class TaskStop(Tool):
    name = "task_stop"
    permission_kind = "agent"
    plan_allowed = True
    description = (
        "停止一个运行中的后台任务（子 Agent 或命令；命令连同它启动的进程一起结束）。"
        "停止后任务记为已取消，不再收到它的通知。"
    )
    prompt_summary = "停止后台任务"
    params_model = TaskStopParams

    def __init__(self, root, runner: SubAgents):
        super().__init__(root)
        self.runner = runner

    async def execute(self, params: TaskStopParams):
        task_id = params.task_id.strip()
        task = self.runner.tasks.get(task_id)
        if task is None:
            raise ToolFailure("unknown_task", f"没有任务 {task_id}。")
        if not await self.runner.tasks.stop(task_id):
            from ..agents.tasks import STATUS_LABELS

            return f"任务 {task_id} 已结束（{STATUS_LABELS[task.status]}），无需停止。", {"task": task_id}
        return f"已停止任务 {task_id}。", {"task": task_id, "stopped": True}
