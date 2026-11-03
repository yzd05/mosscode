"""Build, run and report sub-agents; the agent tool, Skill fork and the hook agent action share it."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent import AgentLoop, add_usage
from ..messages import Completed, Message, ProviderError, RunStopped, ToolProgress, ToolResult
from ..permissions import ApprovalRequest, stricter
from ..prompts import git_snapshot, hook_notification
from ..skills import ActiveSkills
from ..tool_budget import bound_result
from ..usage import price_of
from . import FORK, AgentCatalog, AgentDef
from .tasks import STATUS_LABELS, Task, TaskManager

if TYPE_CHECKING:
    from ..conversation import Conversation
    from ..worktree import Created, WorktreeManager

AGENT_TOOL = "agent"
# Never for a sub-agent. Removed from a definition's tools; a fork keeps them offered (cache) but refused.
# The team tools too (tools/team.py TEAM_TOOLS); a member gets its own versions as extra tools.
GLOBAL_BAN = frozenset(
    {
        AGENT_TOOL,
        "load_skill",
        "memory",
        "exit_plan_mode",
        "task_output",
        "task_stop",
        "team_create",
        "team_spawn",
        "team_merge",
        "team_delete",
        "team_task_create",
        "team_task_get",
        "team_task_list",
        "team_task_update",
        "send_message",
    }
)
# What an isolated sub-agent can have (create_default_registry, rooted in its worktree).
WORKSPACE_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "execute_command",
        "glob",
        "grep",
        "exit_plan_mode",
        "web_fetch",
        "web_search",
    }
)
AUTO_BACKGROUND = 120.0
MAX_BACKGROUND = 4
FORK_PLACEHOLDER = "已在后台分出 Fork 处理。"  # The same for every call, so sibling forks share a prefix.
FORK_DIRECTIVE = """<fork-directive>
你是从上面的对话中分出的 Fork 子 Agent，在后台独立完成下面的任务，你的回复不会直接给用户看。
- 不要再创建子 Agent（agent 工具对你不可用），不要调用 memory、load_skill；
- 不要向用户提问，遇到需要决定的事在报告中说明；
- 只做这项任务，不要超出范围；
- 完成后给出最终报告，以“范围：”开头，依次写：范围、结果、涉及的关键文件、未完成的部分。
</fork-directive>

任务："""


def effective_mode(agent: AgentDef | None, session_mode: str) -> str:
    """Plan always wins; a project definition never loosens the session; user and built-in ones may."""
    if session_mode == "plan" or agent is None or agent.permission_mode is None:
        return session_mode
    if agent.source == "project":
        return stricter(agent.permission_mode, session_mode)
    return agent.permission_mode


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SubagentLog:
    """subagents/<task id>.jsonl, the same message records as the main transcript."""

    def __init__(self, path: Path, header: dict):
        self.path = path
        self.failed = False
        self._write(header)

    def _write(self, record: dict) -> None:
        if self.failed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError):
            self.failed = True

    def append(self, record: dict) -> None:
        self._write(record)


def describe_call(call, root: Path | None = None) -> str:
    """The latest tool call of a sub-agent, for its tool row; paths relative to its root."""
    try:
        args = json.loads(call.arguments)
    except (ValueError, TypeError):
        args = {}
    value, key = "", ""
    if isinstance(args, dict):
        keys = ("pattern", "path") if call.name in {"glob", "grep"} else ("path", "pattern", "command", "name")
        key = next((key for key in keys if args.get(key)), "")
        value = str(args[key]) if key else ""
    if key == "path":
        try:  # Paths inside the project read as relative, so the file name survives the width limit.
            value = str(Path(value).relative_to(root or Path.cwd()))
        except ValueError:
            pass
    value = " ".join(value.split())
    if len(value) > 60:
        value = "…" + value[-59:] if key == "path" else value[:59] + "…"
    return f"{call.name} {value}".strip()


def stats(task: Task) -> str:
    return f"工具调用 {task.tool_calls} 次，Token {task.tokens}，用时 {task.elapsed:.0f} 秒"


PLAN_NO_WORKTREE = "（计划模式下未创建工作树：子 Agent 在主目录只读运行。）"


def worktree_context(manager: WorktreeManager, created: Created, root: Path) -> str:
    """Where the sub-agent is and what that means, before its first message."""
    meta = created.meta
    lines = [
        "你在独立的 git 工作树中工作，与主目录隔离：",
        f"- 你的工作目录：{root}",
        f"- 主项目目录：{manager.project}（不要修改其中的文件，写入会被拒绝）",
        f"- 分支 {meta.branch}，基于提交 {meta.base[:12]}",
        "- 父 Agent 给出的主项目中的绝对路径，换成你的工作目录中的同一路径再读写",
        "- 完成时有改动会保留（提交与否都可以），没有改动会自动删除；你不需要删除工作树",
    ]
    if created.dirty:
        lines.append("- 主目录有未提交的改动，它们不在这个工作树中")
    return "<worktree-context>\n" + "\n".join(lines) + "\n</worktree-context>"


def worktree_fields(task: Task, *, stop: bool) -> dict:
    worktree = task.worktree or {}
    fields = {"worktree": worktree.get("relative", "")}
    if stop:
        fields["worktree_kept"] = "" if not worktree else ("true" if worktree.get("outcome") == "已保留" else "false")
    return fields


async def noted(note: str, run) -> str:
    """The plan-mode note first."""
    text = await run
    return f"{note}\n\n{text}" if note else text


def unfinished(reason: str, loop: AgentLoop | None = None) -> str:
    """The reason, then the last reply the sub-agent wrote, if any."""
    text = f"[子 Agent 未完成：{reason.rstrip('。')}]"
    last = (
        next((m.text for m in reversed(loop.history) if m.role == "assistant" and m.text.strip()), "")
        if loop is not None
        else ""
    )
    return text + (f"\n\n{last}" if last else "")


class SubAgents:
    def __init__(
        self,
        conversation: Conversation,
        catalog: AgentCatalog | None = None,
        *,
        auto_background: float = AUTO_BACKGROUND,
        max_background: int = MAX_BACKGROUND,
    ):
        self.conversation = conversation
        self.catalog = catalog if catalog is not None else AgentCatalog()
        self.auto_background = auto_background
        self.tasks = TaskManager(max_background=max_background)
        self.tasks.on_finish = self._finished
        self._logs: dict[str, SubagentLog] = {}

    @property
    def main(self) -> AgentLoop:
        return self.conversation._agent

    # Building ------------------------------------------------------------------------------

    def _provider(self, model: str | None, what: str):
        """(provider, budget, owned); owned providers are closed when the sub-agent ends."""
        conversation = self.conversation
        if not model or conversation._is_current(model):
            return conversation.provider, self.main.budget, False
        config = conversation._fork_provider_config(model)
        if config is None:
            names = "、".join(c.name for c in conversation.provider_configs) or "无"
            raise ProviderError(f"{what}未运行：没有名为 {model} 的提供商（可用：{names}）。")
        factory = conversation.provider_factory
        if factory is None:
            from ..providers import create_provider as factory
        try:
            return factory(config), config.budget(), True
        except Exception as exc:
            raise ProviderError(f"{what}未运行：无法创建提供商 {model}：{exc}") from None

    def _check_provider(self, model: str | None, what: str) -> None:
        """An isolated sub-agent builds its loop inside the task; a wrong model is still a tool error."""
        conversation = self.conversation
        if model and not conversation._is_current(model) and conversation._fork_provider_config(model) is None:
            names = "、".join(c.name for c in conversation.provider_configs) or "无"
            raise ProviderError(f"{what}未运行：没有名为 {model} 的提供商（可用：{names}）。")

    def policy(self, agent: AgentDef | None, task: Task, root: Path | None = None):
        parent = self.main.registry.permissions
        label = f"子 Agent {task.label}"
        policy = parent.view(
            effective_mode(agent, parent.mode),
            interactive=parent.interactive and not task.background,
            label=label,
            root=root,
        )
        task.policy = policy
        return policy

    def attach(self, task: Task, loop: AgentLoop) -> None:
        """Usage goes to the task and the session; messages to the sub-agent's own transcript."""
        main = self.main

        def counted(usage) -> None:
            price = price_of(loop.provider)
            task.usage = add_usage(task.usage, usage, price)
            main.total_usage = add_usage(main.total_usage, usage, price)
            self.tasks.changed(task)

        loop.on_usage = counted
        conversation = self.conversation
        session = conversation.session
        if conversation.persist and session is not None:
            task.transcript = session.directory / "subagents" / f"{task.id}.jsonl"
            log = SubagentLog(
                task.transcript,
                {
                    "type": "subagent",
                    "v": 1,
                    "id": task.id,
                    "agent": task.agent,
                    "agent_type": task.agent_type,
                    "description": task.description,
                    "created": _now(),
                },
            )
            self._logs[task.id] = log
            loop.on_commit = log.append
            loop.registry.image_dir = task.transcript.with_suffix("") / "images"
        conversation._record(
            {
                "type": "task",
                "time": _now(),
                "id": task.id,
                "event": "start",
                "origin": task.origin,
                "agent": task.agent,
                "agent_type": task.agent_type,
                "description": task.description,
                "background": task.background,
                "transcript": str(task.transcript) if task.transcript else None,
            }
        )

    def definition_loop(
        self,
        agent: AgentDef,
        task: Task,
        *,
        nested: bool = False,
        root: Path | None = None,
        git: dict | None = None,
        worktree: str = "",
    ) -> tuple[AgentLoop, bool]:
        """A blank conversation with the role's prompt; returns the loop and whether it owns its provider.

        With root (a worktree), fresh workspace tools and a policy view rooted there; no MCP or
        Skill script tools, which would act on the main directory.
        """
        main = self.main
        provider, budget, owned = self._provider(agent.model, f"子 Agent {agent.name} ")
        policy = self.policy(agent, task, root)
        keep = lambda name: name not in GLOBAL_BAN and agent.allows(name)  # noqa: E731
        if root is None:
            registry = main.registry.for_agent(agent="subagent", agent_type=agent.name, permissions=policy, keep=keep)
        else:
            from ..tools import create_default_registry, web_tools

            shell = getattr(main.registry.get("execute_command"), "shell", None)
            fresh = create_default_registry(root, permissions=policy, shell=shell if shell is not None else "auto")
            registry = fresh.for_agent(
                agent="subagent",
                agent_type=agent.name,
                permissions=policy,
                keep=keep,
                extra=tuple(tool for tool in web_tools(main.registry, root) if keep(tool.name)),
            )
            registry.session, registry.hooks, registry.worktree = main.registry.session, main.registry.hooks, worktree
        registry.nested = nested
        turns = min(agent.max_turns or main.max_iterations, main.max_iterations)
        loop = AgentLoop(provider, agent.prompt, registry, max_iterations=turns, unknown_limit=main.unknown_limit)
        loop.instructions = self.conversation.project_instructions
        loop.git = git if git is not None else main.git
        loop.budget, loop.auto_compact, loop.session = budget, main.auto_compact, main.session
        self.attach(task, loop)
        return loop, owned

    def fork_loop(self, task: Task) -> AgentLoop:
        """The parent's prompt, tools and history, up to the assistant message making this call."""
        main = self.main
        policy = self.policy(None, task)
        state = main.registry.file_state()
        registry = main.registry.for_agent(
            agent="fork",
            agent_type=FORK,
            permissions=policy,
            state=state.copy() if state is not None else None,
            forbidden=GLOBAL_BAN & set(main.registry.names()),
        )
        loop = AgentLoop(
            main.provider, "", registry, max_iterations=main.max_iterations, unknown_limit=main.unknown_limit
        )
        loop.system, loop.instructions, loop.git = main.system, main.instructions, main.git
        loop.skill_catalog, loop.agent_catalog, loop.hook_context = (
            main.skill_catalog,
            main.agent_catalog,
            main.hook_context,
        )
        loop.active_skills = ActiveSkills()
        for item in main.active_skills.items():
            loop.active_skills.activate(item.skill, item.arguments, item.text)
        loop._announced = main._announced
        loop.budget, loop.auto_compact, loop.session = main.budget, main.auto_compact, main.session
        loop.history = fork_history(main.history)
        self.attach(task, loop)
        return loop

    # Running -------------------------------------------------------------------------------

    async def start_hooks(self, task: Task, prompt: str) -> str:
        """subagent_start; its prompt and feedback go before the sub-agent's first message."""
        hooks = self.conversation.hooks
        if hooks is None or not hooks.wants("subagent_start"):
            return prompt
        from ..hooks import HookEvent

        outcome = await hooks.fire(
            HookEvent(
                "subagent_start",
                fields={
                    "task_id": task.id,
                    "agent_type": task.agent_type,
                    "description": task.description,
                    "background": "true" if task.background else "false",
                    **worktree_fields(task, stop=False),
                },
            )
        )
        note = hook_notification(outcome.notes)
        return f"{note.render()}\n\n{prompt}" if note else prompt

    async def drive(self, task: Task, loop: AgentLoop, prompt: str, *, owned: bool = False) -> str:
        """Run to completion (no user input); approvals of a foreground sub-agent go to the parent's queue."""
        parent = self.main.registry.permissions
        final, stopped = None, None
        try:
            prompt = await self.start_hooks(task, prompt)
            async with aclosing(loop.run(prompt)) as events:
                async for event in events:
                    if isinstance(event, ApprovalRequest):
                        parent.requests.put_nowait(event)  # The main tool batch shows it.
                    elif isinstance(event, ToolProgress) and event.phase == "running":
                        task.tool_calls += 1
                        task.last_tool = describe_call(event.call, task.root)
                        self.tasks.changed(task)
                    elif isinstance(event, Completed):
                        final = event.message.text
                    elif isinstance(event, RunStopped):
                        stopped = event
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.result = unfinished("已取消", loop)
            raise
        except ProviderError as exc:
            task.status = "failed"
            return unfinished(str(exc), loop)
        finally:
            if owned:
                await loop.provider.close()
        if stopped is not None and stopped.reason in {"completed", "max_tokens"} and final is not None:
            return (
                final.strip() or "（子 Agent 没有输出正文。）"
            ) + f"\n\n（子 Agent {task.agent_type}：{stats(task)}）"
        task.status = "failed"
        if stopped is not None and stopped.reason == "max_iterations":
            return unfinished(f"达到最大轮数 {loop.max_iterations}", loop)
        return unfinished(stopped.notice if stopped is not None else "没有完整回复", loop)

    async def run_isolated(
        self, task: Task, agent: AgentDef, prompt: str, manager: WorktreeManager, *, nested: bool = False
    ) -> str:
        """Create the worktree, run in it, then delete or keep it; the report ends the result."""
        from ..worktree import WorktreeError
        from ..worktree.manager import WORKTREES

        task.last_tool = "创建工作树…"
        self.tasks.changed(task)
        try:
            created = await manager.ephemeral(task.id)
        except WorktreeError as exc:
            task.status = "failed"
            return unfinished(f"无法创建工作树：{exc}")
        meta = created.meta
        root = manager.root_in(meta)
        task.root = root
        task.worktree = {
            "name": meta.name,
            "path": str(meta.path),
            "branch": meta.branch,
            "base": meta.base,
            "relative": (WORKTREES / meta.flat).as_posix(),
            "outcome": "使用中",
        }
        task.last_tool = ""
        self.tasks.changed(task)
        text = None
        try:
            try:
                snapshot = await asyncio.to_thread(git_snapshot, root)
                loop, owned = self.definition_loop(
                    agent, task, nested=nested, root=root, git=snapshot, worktree=task.worktree["relative"]
                )
            except ProviderError as exc:
                task.status = "failed"
                text = unfinished(str(exc))
            else:
                text = await self.drive(
                    task, loop, worktree_context(manager, created, root) + "\n\n" + prompt, owned=owned
                )
        finally:
            try:
                finished = await asyncio.shield(manager.finish(meta))
                report = manager.report(meta, finished)
                task.worktree["outcome"] = "已保留" if finished.kept else ("删除未完成" if finished.error else "已删除")
            except asyncio.CancelledError:
                report = f"[工作树] 结束处理被中断；{meta.name} 留在磁盘上，用 /worktree 查看。"
                task.worktree["outcome"] = "已保留"
            except Exception as exc:  # A broken repository: keep everything and say so.
                report = f"[工作树] 结束处理失败（{exc}）；{meta.name} 留在磁盘上，用 /worktree 查看。"
                task.worktree["outcome"] = "已保留"
            if created.warnings:
                report = "[工作树] 初始化提示：" + "；".join(created.warnings) + "\n" + report
            task.worktree["report"] = report
            if text is None:  # Cancelled: TaskManager keeps task.result.
                task.result = (task.result or "[子 Agent 未完成：已取消]") + "\n\n" + report
            self.tasks.changed(task)
        return text + "\n\n" + report

    def _finished(self, task: Task) -> None:
        """subagent_stop, the end record, and the notification of a background task."""
        conversation = self.conversation
        conversation._record({"type": "task", "time": _now(), "id": task.id, "event": "end", "status": task.status})
        hooks = conversation.hooks
        if hooks is not None and hooks.wants("subagent_stop"):
            from ..hooks import HookEvent

            hooks.spawn(
                hooks.fire(
                    HookEvent(
                        "subagent_stop",
                        fields={
                            "task_id": task.id,
                            "agent_type": task.agent_type,
                            "status": task.status,
                            "background": "true" if task.background else "false",
                            **worktree_fields(task, stop=True),
                        },
                    )
                )
            )
        if task.origin != "agent" or not task.background or task.notified:
            return
        task.notified = True
        self.main.notifications.append(self.notification(task))
        conversation._tasks_ready()

    def notification(self, task: Task) -> str:
        result, _ = bound_result(AGENT_TOOL, f"task-{task.id}", task.result or "", {}, self.conversation.session)
        usage = task.usage
        return (
            f"<task-notification>\n任务：{task.id}\n类型：{task.agent_type}\n标签：{task.description}\n"
            f"状态：{STATUS_LABELS[task.status]}\n用量：输入 {usage.input_tokens} Token（缓存命中 "
            f"{usage.cached_input_tokens}），输出 {usage.output_tokens} Token，用时 {task.elapsed:.0f} 秒\n"
            f"结果：\n{result}\n</task-notification>\n以上是后台任务的结果；根据需要继续处理，或向用户报告。"
        )

    # Entry points --------------------------------------------------------------------------

    async def call(
        self,
        description: str,
        prompt: str,
        agent_type: str = "",
        background: bool = False,
        call_id: str = "",
        isolation: str = "none",
    ) -> tuple[str, dict]:
        """The agent tool; Isolation."""
        from ..tools.base import ToolFailure

        kind = agent_type.strip() or FORK
        if kind == FORK and isolation == "worktree":
            raise ToolFailure(
                "isolation_unsupported",
                "Fork 继承当前对话，不能在工作树中运行；需要隔离时请指定 agent_type（例如 general）。",
            )
        if kind == FORK:
            if self.tasks.full():
                raise ToolFailure("too_many_tasks", self._full())
            task = self.tasks.new(
                origin="agent",
                agent="fork",
                agent_type=FORK,
                description=description,
                prompt=prompt,
                call_id=call_id,
                background=True,
            )
            loop = self.fork_loop(task)
            self.tasks.launch(task, lambda: self.drive(task, loop, FORK_DIRECTIVE + prompt))
            return (
                f"已在后台启动 Fork：任务 {task.id}。完成后你会收到 <task-notification>；"
                "在此之前可以继续其他工作，不要重复做同一件事。"
            ), {"task": task.id, "background": True}
        agent = self.catalog.get(kind)
        if agent is None:
            names = "、".join(self.catalog.names()) or "无"
            raise ToolFailure(
                "unknown_agent_type", f"没有名为 {kind} 的子 Agent 类型。可用：{names}；省略 agent_type 即为 Fork。"
            )
        background = background or agent.background
        if background and self.tasks.full():
            raise ToolFailure("too_many_tasks", self._full())
        isolate, note = self._isolation(agent, isolation)
        manager = self._manager(ToolFailure) if isolate else None
        task = self.tasks.new(
            origin="agent",
            agent="subagent",
            agent_type=agent.name,
            description=description,
            prompt=prompt,
            call_id=call_id,
            background=background,
            isolated=isolate,
        )
        loop = None
        try:
            if isolate:
                self._check_provider(agent.model, f"子 Agent {agent.name} ")
            else:
                loop, owned = self.definition_loop(agent, task)
        except ProviderError as exc:
            del self.tasks.tasks[task.id]
            raise ToolFailure("agent_unavailable", str(exc)) from None
        if isolate:
            job = self.tasks.launch(task, lambda: self.run_isolated(task, agent, prompt, manager))
        else:
            job = self.tasks.launch(task, lambda: noted(note, self.drive(task, loop, prompt, owned=owned)))
        if background:
            return (
                f"已在后台启动子 Agent {agent.name}：任务 {task.id}。完成后你会收到 <task-notification>；"
                "在此之前可以继续其他工作，不要重复做同一件事。"
            ), {"task": task.id, "background": True}
        detached = asyncio.ensure_future(task.detach.wait())
        try:
            await asyncio.wait(
                {job, detached}, timeout=self.auto_background or None, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            job.cancel()  # Cancelling the turn cancels its foreground sub-agents.
            await asyncio.gather(job, return_exceptions=True)
            return task.result or unfinished("已取消", loop), {"task": task.id, "cancelled": True}
        finally:
            detached.cancel()
        if job.done():
            details = {"task": task.id, "background": False}
            if task.worktree:
                details["worktree"] = task.worktree["outcome"]
            return job.result(), details
        task.to_background()  # ctrl+b or the timer.
        self.tasks.changed(task)
        return (
            f"已转入后台：任务 {task.id}，子 Agent {agent.name} 继续运行，完成后你会收到 <task-notification>；"
            "在此之前可以继续其他工作，不要重复做同一件事。"
        ), {"task": task.id, "background": True, "detached": True}

    def _isolation(self, agent: AgentDef, isolation: str) -> tuple[bool, str]:
        """(run in a worktree, note for the result)."""
        if isolation != "worktree" and agent.isolation != "worktree":
            return False, ""
        if effective_mode(agent, self.main.registry.permissions.mode) == "plan":
            return False, PLAN_NO_WORKTREE
        return True, ""

    def _manager(self, failure):
        from ..worktree import WorktreeError

        try:
            return self.conversation.worktree_manager()
        except WorktreeError as exc:
            raise failure("worktree_unavailable", str(exc)) from None

    def _full(self) -> str:
        return (
            f"后台任务已达上限（{self.tasks.max_background} 个）；等待已有任务完成（task_output）"
            "或停止其中一个（task_stop）后再启动。"
        )

    async def hook_agent(self, hook, event, fields):
        """A background definition sub-agent for an agent action; its result is only logged."""
        from ..hooks.actions import ActionResult, render, short

        action = hook.action
        if self.main.registry is None:
            return ActionResult("skipped", "当前会话未启用工具")
        agent = self.catalog.get(action.agent_type)
        if agent is None:
            return ActionResult("failed", f"没有名为 {action.agent_type} 的子 Agent 类型")
        if self.tasks.full():
            return ActionResult("failed", "后台任务已达上限")
        prompt = render(action.prompt, fields)
        isolate, note = self._isolation(agent, "none")
        manager = None
        if isolate:
            from ..worktree import WorktreeError

            try:
                manager = self.conversation.worktree_manager()
            except WorktreeError as exc:
                return ActionResult("failed", str(exc))
        task = self.tasks.new(
            origin="hook",
            agent="subagent",
            agent_type=agent.name,
            description=f"Hook {hook.id}",
            prompt=prompt,
            background=True,
            isolated=isolate,
        )
        try:
            if isolate:
                self._check_provider(agent.model, f"子 Agent {agent.name} ")
            else:
                loop, owned = self.definition_loop(agent, task, nested=True)
        except ProviderError as exc:
            del self.tasks.tasks[task.id]
            return ActionResult("failed", str(exc))
        if isolate:
            job = self.tasks.launch(task, lambda: self.run_isolated(task, agent, prompt, manager, nested=True))
        else:
            job = self.tasks.launch(task, lambda: noted(note, self.drive(task, loop, prompt, owned=owned)))
        try:
            text = await asyncio.wait_for(asyncio.shield(job), timeout=action.timeout)
        except TimeoutError:
            await self.tasks.stop(task.id)
            return ActionResult("timeout", f"子 Agent {task.id} 超过 {action.timeout:g} 秒，已停止")
        except asyncio.CancelledError:
            await self.tasks.stop(task.id)
            raise
        self.conversation._hook_notice(f"Hook {hook.id} 的子 Agent（任务 {task.id}）{STATUS_LABELS[task.status]}。")
        if task.status != "completed":
            return ActionResult("failed", short(text, 300))
        return ActionResult("ok", detail=f"任务 {task.id}：" + short(text, 300))


def fork_history(history: list[Message]) -> list[Message]:
    """The history up to the last assistant message, then the same placeholder for each of its calls."""
    history = deepcopy(history)
    index = next((i for i in range(len(history) - 1, -1, -1) if history[i].role == "assistant"), None)
    if index is None:
        return []
    assistant = history[index]
    results = tuple(ToolResult(call.id, call.name, FORK_PLACEHOLDER) for call in assistant.tool_calls)
    return history[: index + 1] + ([Message("tool", (), tool_results=results)] if results else [])
