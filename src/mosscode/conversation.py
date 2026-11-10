"""Stable facade: one independent AgentLoop owns state and history."""

import asyncio
import difflib
from collections.abc import Mapping
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from .agent import DEFAULT_MAX_ITERATIONS, AgentLoop, Provider
from .agents import AgentCatalog
from .agents.runtime import AUTO_BACKGROUND, GLOBAL_BAN, MAX_BACKGROUND, SubAgents
from .checkpoints import Checkpoints, Restoration
from .config import ProviderConfig
from .context import ContextBudget, estimate_text
from .goal import (
    DEFAULT_MAX_TURNS,
    FAILURE_LIMIT,
    IDLE_LIMIT,
    Goal,
    GoalStep,
    Verdict,
    check_condition,
    continue_prompt,
    evaluate,
    goal_reminder,
)
from .images import ImageRef, ProcessedImage, image_ref
from .memory import INDEX_TOKENS, MemoryStore
from .memory_extract import MemoryExtractor
from .mentions import attach
from .messages import (
    NOTIFICATION,
    CompactFailed,
    CompactFinished,
    Completed,
    ContentBlock,
    FilesAttached,
    Message,
    ProviderError,
    RunStopped,
    ToolProgress,
    UsageUpdate,
)
from .prompts import (
    display_path,
    gap_reminder,
    git_snapshot,
    hook_context,
    instructions_section,
    load_instructions,
    memory_section,
    rewind_reminder,
)
from .reasoning import LEVELS, Unsupported, thinking_options
from .session import (
    Restored,
    Session,
    SessionLocked,
    Transcript,
    checkpoint_record,
    find_session,
    goal_record,
    list_sessions,
    load_session,
    provider_record,
    skill_record,
)
from .skills import ACTIVE_LIMIT, LOAD_SKILL, ForkRequest, SkillCatalog, SkillDef, missing_tools
from .tools.agent import AgentTool, TaskOutput, TaskStop
from .tools.base import ToolFailure, ToolRegistry
from .tools.memory import Memory
from .tools.skill import LoadSkill, SkillTool
from .user_command import CommandRun, command_message, run_user_command


@dataclass(frozen=True)
class Notice:
    """A local note raised outside the model's events, e.g. a transcript write failure."""

    text: str


@dataclass(frozen=True)
class MemoryUpdated:
    """The background extractor wrote or deleted notes."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class RewindPoint:
    """A typed message /rewind can go back to, and how many files the edit tools changed after it."""

    message: Message
    files: int


@dataclass(frozen=True)
class Rewound:
    restoration: Restoration | None  # None when the files were left alone.
    kept: int | None  # Messages left in the history; None when the conversation was left alone.
    message: Message | None  # The message rewound past, for the prompt.


@dataclass(frozen=True)
class Resumed:
    """A session was restored; shown before any compaction it needs."""

    restored: Restored
    messages: int
    notices: tuple[str, ...]


class Conversation:
    def __init__(
        self,
        provider: Provider,
        system: str = "",
        registry: ToolRegistry | None = None,
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        unknown_limit: int = 3,
        mcp_manager=None,
        context_root: Path | None = None,
        instructions_home: Path | None = None,
        budget: ContextBudget | None = None,
        session=None,
        auto_compact: bool = True,
        notices: tuple[str, ...] = (),
        persist: bool = False,
        provider_info: tuple[str, str, str] | None = None,
        sessions_home: Path | None = None,
        memory: MemoryStore | None = None,
        auto_memory: bool = False,
        memory_provider: Provider | None = None,
        skills: SkillCatalog | None = None,
        provider_configs=(),
        provider_factory=None,
        hooks=None,
        agents: AgentCatalog | None = None,
        agent_auto_background: float = AUTO_BACKGROUND,
        agent_max_background: int = MAX_BACKGROUND,
        command_max_background: int = 8,
        command_time_limit: float | None = None,
        worktree_links=(),
        worktree_stale_hours: float = 24.0,
        teams: bool = False,
        team_max_members: int = 4,
        teammate_mode: str = "in-process",
        team_launch: dict | None = None,
        coordinator: bool = False,
        goal_max_turns: int = DEFAULT_MAX_TURNS,
        goal_provider: Provider | None = None,
    ):
        self.provider, self.system, self.registry = provider, system, registry
        # The worktree manager is built on first use; it needs a git repository.
        self.worktree_links, self.worktree_stale_hours = tuple(worktree_links), worktree_stale_hours
        self._worktrees = None
        self._worktree_cleanup: asyncio.Task | None = None
        # The HookEngine; None runs no hooks at all.
        self.hooks = hooks
        self._session_started = False
        self.mcp_manager = mcp_manager
        self.skills = skills if skills is not None else SkillCatalog()  # Empty without a scan.
        self._skill_tools: dict[str, tuple[str, ...]] = {}  # Skill name -> its tools registered here.
        # A fork skill's model names one of these; the factory builds it (create_provider by default).
        self.provider_configs, self.provider_factory = tuple(provider_configs), provider_factory
        # Project files and git are product-level context; None keeps the core hermetic.
        self.context_root, self.instructions_home = context_root, instructions_home
        self._context_loaded = False
        self.instruction_files: tuple[str, ...] = ()
        self._mcp_background = False
        self._start_lock = asyncio.Lock()
        self._notices = list(notices)  # Reported once, by the first start().
        self._raised: list[str] = []  # Notices raised while running, delivered as Notice events.
        self._agent = AgentLoop(provider, system, registry, max_iterations=max_iterations, unknown_limit=unknown_limit)
        self.system = self._agent.system
        if budget is not None:
            self._agent.budget = budget
        self._agent.auto_compact = auto_compact
        # (name, protocol, model) of the current provider: written to the transcript on a switch.
        self.provider_info = provider_info
        self.thinking: str | None = None  # The /thinking level for this process; None follows the configuration.
        # Transcripts need a session from Session.new; persist=False keeps only stored results.
        self.persist = persist and session is not None and session.transcript_path is not None
        self.sessions_home = sessions_home
        self._transcript: Transcript | None = None
        self._agent.on_commit = self._record
        if hooks is not None:
            hooks.on_notice = self._hook_notice
            self._agent.hooks = hooks
            if registry is not None:
                registry.hooks = hooks
        self._use_session(session)
        # /goal: one per session; the evaluator uses goal_provider, else memory_provider, else the current one.
        self.goal: Goal | None = None
        self.last_goal: tuple[str, str, str] | None = None  # (condition, event, reason) of the latest ended goal.
        self.goal_max_turns, self.goal_provider = goal_max_turns, goal_provider
        self._goal_turn: tuple[str, bool] | None = None  # (how the latest turn ended, whether it called tools).
        # Notes are injected only when context is loaded (context_root); None turns memory off.
        self.memory = memory
        self.memory_provider = memory_provider
        self.on_memory = None  # Set by the TUI: receives MemoryUpdated and Notice from the background.
        self.extractor: MemoryExtractor | None = None
        self._cursor = 0  # History index up to which messages went to the extractor.
        self._cut: int | None = None  # Cut of the compaction in progress, once its messages were handed over.
        if memory is not None:
            if registry is not None:
                if registry.get("memory") is None:
                    registry.register(Memory(registry.permissions.root, memory))
                registry.permissions.memory_dirs = tuple(path.resolve() for path in memory.dirs.values())
            if auto_memory:
                self.extractor = MemoryExtractor(
                    memory,
                    lambda: self.memory_provider or self.provider,
                    on_result=lambda r: self._emit(MemoryUpdated(r.names)),
                    on_notice=lambda text: self._emit(Notice(text)),
                    on_usage=self._agent._usage,
                )
                self._agent.on_compact = self._handover
        self._notices.extend(self._offer_skills())
        # Sub-agents. The agent tools are offered only with a catalog (None keeps the core hermetic);
        # the runtime itself always exists, Skill fork runs on it.
        self.project_instructions = ""  # The project files only, for definition sub-agents.
        self.on_tasks = None  # Set by the UI: background results wait while the main agent is idle.
        self.subagents = SubAgents(
            self, agents, auto_background=agent_auto_background, max_background=agent_max_background
        )
        self.agents_enabled = agents is not None
        if hooks is not None:
            hooks.agent_runner = self.subagents.hook_agent
        self._notices.extend(self._offer_agents())
        # Background commands share the task list; command_max_background 0 runs every command in the foreground.
        self.commands = None
        tool = registry.get("execute_command") if registry is not None else None
        if command_max_background > 0 and hasattr(tool, "allow_background"):
            from .tools.background import BackgroundCommands

            self.commands = BackgroundCommands(
                self.tasks,
                limit=command_max_background,
                time_limit=command_time_limit,
                session=lambda: self.session,
                record=self._record,
            )
            registry.commands = self.commands
            tool.allow_background(command_time_limit)
            root = registry.permissions.root
            for task_tool in (TaskOutput(root, self.subagents), TaskStop(root, self.subagents)):
                if registry.get(task_tool.name) is None:
                    registry.register(task_tool)
        # The lead's team tools from the start, in the interactive UI with tools and agents.
        self.teams = None
        self._team_context_due = False  # After /clear or /resume, once before the next message.
        if teams and self.registry is not None and self.agents_enabled:
            from .teams.runtime import TeamRuntime
            from .tools.team import lead_tools

            self.teams = TeamRuntime(
                self, home=sessions_home, max_members=team_max_members, teammate_mode=teammate_mode, launch=team_launch
            )
            for tool in lead_tools(self.registry.permissions.root, self.teams):
                if self.registry.get(tool.name) is None:
                    self.registry.register(tool)
            self._agent.team_guide = self.teams.guide()
        # Decided at startup (the CLI checks both switches), fixed for the session.
        self.coordinator = False
        if coordinator:
            from .coordinator import NO_AGENTS, SECTION, withheld

            if self.teams is None:
                self._notices.append(NO_AGENTS)
            else:
                self.coordinator = True
                self.registry.withheld = withheld
                self._agent.team_guide += "\n\n" + SECTION

    def _use_session(self, session) -> None:
        if self._transcript is not None:
            self._transcript.close()
        self.session = session
        # Stored tool results and compaction records; without it large results are only cut to a preview.
        self._agent.session = session
        if self.hooks is not None:
            self.hooks.use_session(session)
        if self.registry is not None and session is not None:
            self.registry.use_session(session)
        root = self.context_root or Path.cwd()
        self._transcript = Transcript(session, root.resolve()) if self.persist else None
        self._use_checkpoints(session)

    def _use_checkpoints(self, session) -> None:
        """This session's file snapshots for /rewind; sub-agent copies of the file tools share them."""
        self.checkpoints = None
        if self.registry is not None and session is not None:
            self.checkpoints = Checkpoints(
                self.registry.permissions.root,
                session,
                record=lambda s: self._record(checkpoint_record(s.message, s.path, s.blob, s.too_large)),
                on_notice=lambda text: self._raised.append(text),  # From a tool thread: joins the next events.
            )
        self._agent.checkpoints = self.checkpoints
        if self.registry is not None:
            for name in ("write_file", "edit_file"):
                tool = self.registry.get(name)
                if tool is not None and hasattr(tool, "checkpoints"):
                    tool.checkpoints = self.checkpoints

    def _record(self, record: dict) -> None:
        if self._transcript is not None:
            notice = self._transcript.append(record)
            if notice:
                self._raised.append(notice)

    def _emit(self, event) -> None:
        if self.on_memory is not None:
            self.on_memory(event)

    # Hooks --------------------------------------------------------------------------------

    def _hook_notice(self, text: str) -> None:
        """Background failures go out at once; without a listener they join the next events."""
        if self.on_memory is not None:
            self._emit(Notice(text))
        else:
            self._raised.append(text)

    async def _fire(self, name: str, fields: dict):
        if self.hooks is None or not self.hooks.wants(name):
            return ()
        from .hooks import HookEvent

        return (await self.hooks.fire(HookEvent(name, fields=fields))).notes

    async def _session_start(self, source: str) -> None:
        """Its output is the hook-context prefix of the whole session."""
        self._session_started = True
        if self.hooks is not None:
            self._agent.hook_context = hook_context(await self._fire("session_start", {"source": source}))

    async def _session_end(self, reason: str) -> None:
        if self._session_started:
            self._session_started = False
            await self._fire("session_end", {"reason": reason})

    async def _turn_end(self, reason: str, notice: str) -> None:
        if reason == "error":
            await self._fire("error", {"error": notice})  # Before turn_end; hook failures never fire it.
        await self._fire("turn_end", {"reason": reason})

    def _turn_end_later(self, reason: str) -> None:
        """A cancelled turn cannot wait: its end hooks run in the background."""
        if self.hooks is not None and self.hooks.wants("turn_end"):
            self.hooks.spawn(self._fire("turn_end", {"reason": reason}))

    # Background extraction ---------------------------------------------------------

    def _extract(self) -> None:
        """After a completed task: everything since the cursor goes to the extractor."""
        if self.extractor is None:
            return
        history = self._agent.history
        messages, self._cursor = history[self._cursor :], len(history)
        self.extractor.schedule(messages)

    def _handover(self, removed) -> None:
        """Before a compaction: messages about to be summarized that the extractor has not seen yet."""
        if self._cursor < len(removed):
            self.extractor.schedule(removed[self._cursor :])
        self._cursor = max(self._cursor, len(removed))
        self._cut = len(removed)

    def _observe(self, event) -> None:
        if isinstance(event, CompactFinished) and self._cut is not None:
            self._cursor, self._cut = 1 + self._cursor - self._cut, None  # Points past the boundary.
        elif isinstance(event, CompactFailed):
            self._cut = None
        elif isinstance(event, RunStopped) and event.reason == "completed":
            self._extract()

    def _rewind(self) -> None:
        """/clear and resume: nothing already in the history is extracted again."""
        self._cursor, self._cut = len(self._agent.history), None
        if self.extractor is not None:
            self.extractor.reset()

    def _drain(self):
        notices, self._raised = self._raised, []
        return [Notice(text) for text in notices]

    @property
    def session_id(self) -> str | None:
        return self.session.id if self.session is not None else None

    @property
    def history(self):
        return tuple(deepcopy(self._agent.history))

    @property
    def _busy(self):
        return self._agent.busy

    @property
    def mode(self):
        return self._agent.mode

    @property
    def total_usage(self):
        return self._agent.total_usage

    @property
    def budget(self) -> ContextBudget:
        return self._agent.budget

    @property
    def compactions(self) -> int:
        return self._agent.compactions

    @property
    def auto_compact_paused(self) -> bool:
        return self._agent.auto_paused

    def context_estimate(self) -> int | None:
        """Approximate size of the next request; None until the first usage report."""
        if self._agent.meter.anchor is None and not self._agent.history:
            return None
        return self._agent.estimate()

    def context_ratio(self) -> float | None:
        estimate = self.context_estimate()
        return None if estimate is None else estimate / self.budget.effective

    async def compact(self, instructions: str = ""):
        """Manual /compact while idle; events report progress and the outcome."""
        async with aclosing(self._agent.compact(instructions)) as stream:
            async for event in stream:
                self._observe(event)
                yield event
                for notice in self._drain():
                    yield notice

    @property
    def todos(self) -> tuple:
        """The main agent's checklist (todo_write), for the interface."""
        return self.registry.todos.items if self.registry is not None else ()

    def rewind_points(self) -> list[RewindPoint]:
        """The typed messages still in the context, newest first."""
        points = []
        for message in reversed(self._agent.history):
            if message.role == "user" and message.synthetic is None and message.id is not None:
                files = len(self.checkpoints.changed_after(message.id)) if self.checkpoints is not None else 0
                points.append(RewindPoint(deepcopy(message), files))
        return points

    async def rewind(self, message_id: str, *, code: bool, conversation: bool) -> Rewound:
        """Put the files back as they were before the message, drop it and what followed, or both."""
        if self._busy:
            raise ProviderError("当前任务正在运行，不能回退。")
        history = self._agent.history
        index = next((i for i, m in enumerate(history) if m.role == "user" and m.id == message_id), None)
        if index is None:
            raise ProviderError("这条消息已不在当前上下文中。")
        restoration = None
        if code:
            if self.checkpoints is None:
                raise ProviderError("当前会话没有文件检查点。")
            running = self.tasks.background()
            if running:
                raise ProviderError(
                    f"有 {len(running)} 个后台任务正在运行，恢复代码前请等待它们结束，或用 /tasks 停止。"
                )
            restoration = await asyncio.to_thread(self.checkpoints.restore, message_id)
        message, kept = None, None
        if conversation:
            message = deepcopy(history[index])
            self._agent.truncate(index, message_id)
            self._cursor, self._cut = min(self._cursor, index), None
            kept = index
        elif restoration is not None:
            if self.registry is not None:
                self.registry.forget_reads()  # What the model read may no longer be on disk.
            if restoration.paths:
                self._agent.remind(rewind_reminder(restoration.paths))
        return Rewound(restoration, kept, message)

    def set_mode(self, mode: str):
        self._agent.set_mode(mode)

    def queue(self, text: str, images: Mapping[str, ProcessedImage] | None = None):
        """Steer a running task; the text joins at the next iteration boundary."""
        if text.strip():
            self._agent.pending.append(text)
            self._agent.pending_images.update(self._image_refs(images, text))

    def take_queued(self) -> tuple[str, ...]:
        """The texts go back to the prompt, whose placeholders still name their images."""
        texts, self._agent.pending, self._agent.pending_images = tuple(self._agent.pending), [], {}
        return texts

    def _image_refs(self, images: Mapping[str, ProcessedImage] | None, text: str) -> dict[str, ImageRef]:
        """Stored in the session when the message is sent, and only those whose placeholder is still in text."""
        store = self.registry if self.registry is not None else self.session
        return {mark: image_ref(image, mark, store) for mark, image in (images or {}).items() if mark in text}

    @property
    def queued(self) -> tuple[str, ...]:
        return tuple(self._agent.pending)

    def _next_session(self):
        """/clear ends this session: the next message starts a new ID and directory."""
        if self.session is None or self.session.transcript_path is None:
            return
        root = self.context_root or Path.cwd()
        self._use_session(Session.new(root, home=self.sessions_home))

    async def clear(self) -> tuple[str, ...]:
        """New session: empty model context and usage; permissions and MCP stay."""
        if self._busy:
            raise ProviderError("当前任务正在运行，不能清空会话。")
        await self.tasks.reset()  # Tasks belong to the session.
        self.clear_goal()  # Recorded in the session being left.
        self.last_goal = None
        await self._session_end("clear")
        self._agent.clear()  # Active skills too.
        self._drop_skills()
        self._next_session()
        self._rewind()
        if self.hooks is not None:
            self.hooks.new_session(self.session)  # Once resets before the new session_start.
        notices = []
        if self.context_root is not None:
            async with self._start_lock:
                self._context_loaded = True
                notices = await self._load_context()
        await self._session_start("clear")
        self._team_context_due = self.teams is not None  # The team keeps running.
        return tuple(notices)

    async def set_provider(
        self, provider: Provider, budget: ContextBudget | None = None, info: tuple[str, str, str] | None = None
    ):
        old = self.provider
        if self.thinking is not None:
            provider.thinking = self.thinking
        self._agent.set_provider(provider, budget)
        self.provider = provider
        if info is not None:
            self.provider_info = info
            if self._agent.history:
                self._record(provider_record(*info))
        if old is not provider:
            await old.close()

    @property
    def provider_config(self) -> ProviderConfig | None:
        config = getattr(self.provider, "config", None)
        return config if isinstance(config, ProviderConfig) else None

    def set_thinking(self, level: str | None) -> None:
        """The /thinking level from the next request on; sub-agents on the same provider follow it."""
        if self._busy:
            raise ProviderError("当前任务正在运行，不能切换思考强度。")
        if level is not None and level not in LEVELS:
            raise ProviderError(f"未知的思考强度 {level}。")
        self.thinking = level
        self.provider.thinking = level

    def thinking_problem(self) -> str | None:
        """Why the current model is not sent the level, or None when it is (or no level is set)."""
        if self.thinking is None:
            return None
        config = self.provider_config
        if config is None:
            return "无法确定当前模型"
        try:
            thinking_options(config, self.thinking)
        except Unsupported as exc:
            return str(exc)
        return None

    def set_permission_mode(self, mode: str):
        if self._busy:
            raise ProviderError("当前任务正在运行，不能切换权限模式。")
        if self.registry is None:
            raise ProviderError("当前会话未启用工具。")
        self.registry.permissions.set_mode(mode)

    # Skills ---------------------------------------------------------------------------------

    @property
    def active_skills(self) -> tuple[str, ...]:
        return self._agent.active_skills.names

    def _offer_skills(self) -> list[str]:
        """The catalog section and load_skill, when tools are on and the model may load a skill."""
        if self.registry is None:
            return []
        section, notice = self.skills.catalog_section()
        self._agent.skill_catalog = section
        if section and self.registry.get(LOAD_SKILL) is None:
            self.registry.register(LoadSkill(self.registry.permissions.root, self._load_skill))
        elif not section and self.registry.get(LOAD_SKILL) is not None:
            self.registry.unregister(LOAD_SKILL)
        return [notice] if notice else []

    def _missing(self, skill: SkillDef) -> list[str]:
        """The tools it lists must be registered now."""
        return missing_tools(skill, self.registry.names() if self.registry is not None else ())

    def _use_skill_dir(self, skill: SkillDef) -> None:
        """Its files can be read without approval once it is in use."""
        if self.registry is None:
            return
        policy = self.registry.permissions
        directory = skill.directory.resolve()
        if directory not in policy.skill_dirs:
            policy.skill_dirs += (directory,)

    def _register_skill_tools(self, skill: SkillDef) -> None:
        """The version just read replaces whatever this skill registered before."""
        self._remove_skill_tools(skill.name)
        if self.registry is None or not skill.tools:
            return
        root = self.registry.permissions.root
        for spec in skill.tools:
            self.registry.unregister(spec.full_name)
            self.registry.register(SkillTool(root, skill, spec))
        self._skill_tools[skill.name] = tuple(spec.full_name for spec in skill.tools)

    def _drop_skills(self) -> None:
        """/clear and resume: no skill is in use any more."""
        if self.registry is not None:
            self.registry.permissions.skill_dirs = ()
        for name in list(self._skill_tools):
            self._remove_skill_tools(name)

    def _remove_skill_tools(self, name: str) -> None:
        for full_name in self._skill_tools.pop(name, ()):
            if self.registry is not None:
                self.registry.unregister(full_name)

    def prepare_skill(self, name: str, arguments: str = "") -> tuple[SkillDef, str, tuple[str, ...]]:
        """Read the skill again and check its tools; returns the skill, the request and notices.

        A fork skill's tools go to its sub-agent only; the caller sends a ForkRequest.
        """
        if self.skills.get(name) is None:
            raise ProviderError(f"未找到 Skill {name}。输入 /skills 查看。")
        skill, notice = self.skills.fresh(name)
        missing = self._missing(skill)
        if missing:
            raise ProviderError(
                f"Skill {name} 未运行：工具不可用：{'、'.join(missing)}。"
                "MCP 工具需要对应的 Server 已连接（/mcp 查看）。"
            )
        self._use_skill_dir(skill)
        if skill.mode == "fork":
            if skill.model and self._fork_provider_config(skill.model) is None and not self._is_current(skill.model):
                names = "、".join(config.name for config in self.provider_configs) or "无"
                raise ProviderError(f"Skill {name} 未运行：没有名为 {skill.model} 的提供商（可用：{names}）。")
            return skill, skill.render(arguments, fork=True), (notice,) if notice else ()
        self._register_skill_tools(skill)
        self._record(skill_record(name, arguments, False))
        return skill, skill.render(arguments), (notice,) if notice else ()

    def _load_skill(self, name: str, arguments: str) -> tuple[str, dict]:
        """The load_skill tool; failures are tool errors and the run goes on."""
        visible = [item.name for item in self.skills.skills.values() if item.model_invocable]
        if self.skills.get(name) is None:
            close = difflib.get_close_matches(name, visible, n=3, cutoff=0.5)
            hint = f"相近的名称：{'、'.join(close)}。" if close else f"可加载：{'、'.join(visible) or '无'}。"
            raise ToolFailure("skill_unknown", f"没有名为 {name} 的 Skill。{hint}")
        skill, notice = self.skills.fresh(name)
        if not skill.model_invocable:
            raise ToolFailure("skill_unavailable", f"Skill {name} 只能由用户输入 /{name} 调用，不能由模型加载。")
        missing = self._missing(skill)
        if missing:
            raise ToolFailure("skill_unavailable", f"Skill {name} 需要的工具当前不可用：{'、'.join(missing)}。")
        text = skill.render(arguments)
        active = self._agent.active_skills
        if active.size(without=name) + len(text) > ACTIVE_LIMIT:
            raise ToolFailure(
                "skill_too_large",
                f"已加载的 Skill 规程合计将超过 {ACTIVE_LIMIT} 个字符，未加载 {name}。"
                "可以请用户 /clear 后再加载，或直接按已加载的规程继续。",
            )
        refreshed = name in active
        active.activate(skill, arguments, text)
        self._use_skill_dir(skill)
        self._register_skill_tools(skill)
        self._record(skill_record(name, arguments, True))
        if notice:
            self._raised.append(notice)
        lines = [f"已{'刷新' if refreshed else '加载'} Skill {name}，规程已放入 <active-skills>，下一次请求起生效。"]
        if skill.tools:
            lines.append("新增专属工具：" + "、".join(tool.full_name for tool in skill.tools))
        if skill.mode == "fork":
            lines.append(f"此 Skill 设计为独立运行，需要隔离上下文时请用户输入 /{name}。")
        if notice:
            lines.append(notice)
        return "\n".join(lines), {"skill": name, "arguments": arguments, "refreshed": refreshed}

    def use_skills(self, catalog: SkillCatalog) -> tuple[str, ...]:
        """The catalog after /skills reload; removed skills leave the active list and take their tools."""
        self.skills = catalog
        active = self._agent.active_skills
        for name in active.names:
            if catalog.get(name) is None:
                active.remove(name)
        for name in list(self._skill_tools):
            if (skill := catalog.get(name)) is None:
                self._remove_skill_tools(name)
            else:
                self._register_skill_tools(skill)
        return tuple(self._offer_skills())

    def _restore_skills(self, records: tuple[tuple[str, str, bool], ...]) -> list[str]:
        """Tools of every skill the session used, SOPs of the pinned ones; the rest is reported."""
        used, pinned = {}, {}
        for name, arguments, is_pinned in records:
            used[name] = None
            if is_pinned:
                pinned[name] = arguments  # The latest arguments; the first load keeps its place.
        notices, lost = [], []
        active = self._agent.active_skills
        for name in used:
            if self.skills.get(name) is None:
                lost.append(f"{name}（已不存在）")
                continue
            skill, notice = self.skills.fresh(name)
            if notice:
                notices.append(notice)
            if name in pinned:
                text = skill.render(pinned[name])
                if not skill.model_invocable:
                    lost.append(f"{name}（已不能由模型加载，规程未放回）")
                elif active.size(without=name) + len(text) > ACTIVE_LIMIT:
                    lost.append(f"{name}（规程合计超过 {ACTIVE_LIMIT} 个字符，未放回）")
                else:
                    active.activate(skill, pinned[name], text)
            self._use_skill_dir(skill)
            self._register_skill_tools(skill)
        if lost:
            notices.append("未恢复的 Skill：" + "、".join(lost) + "。")
        return notices

    # Sub-agents --------------------------------------------------------------------------

    @property
    def agents(self) -> AgentCatalog:
        return self.subagents.catalog

    @property
    def tasks(self):
        return self.subagents.tasks

    def _offer_agents(self) -> list[str]:
        """The catalog section and the three tools, when tools are on."""
        if self.registry is None or not self.agents_enabled:
            return []
        self._agent.agent_catalog = self.agents.catalog_section()
        root = self.registry.permissions.root
        for tool in (AgentTool(root, self.subagents), TaskOutput(root, self.subagents), TaskStop(root, self.subagents)):
            if self.registry.get(tool.name) is None:
                self.registry.register(tool)
        problems = self.agents.problems
        return [f"{len(problems)} 个 Agent 定义有问题，已跳过；输入 /agents 查看。"] if problems else []

    def use_agents(self, catalog: AgentCatalog) -> tuple[str, ...]:
        """/agents reload; the next request sees the new catalog."""
        self.subagents.catalog = catalog
        return tuple(self._offer_agents())

    def _tasks_ready(self) -> None:
        """A background result waits in notifications; an idle main agent starts a turn for it."""
        if not self._busy and self.on_tasks is not None:
            self.on_tasks()

    def take_notifications(self) -> tuple[str, ...]:
        texts, self._agent.notifications = tuple(self._agent.notifications), []
        return texts

    @property
    def notifications(self) -> tuple[str, ...]:
        return tuple(self._agent.notifications)

    async def notify(self):
        """A turn whose first message is the waiting notifications, merged into one."""
        texts = self.take_notifications()
        if not texts:
            return
        async with aclosing(self.reply("\n\n".join(texts), synthetic=NOTIFICATION)) as stream:
            async for event in stream:
                yield event

    async def cancel_tasks(self, wait: float | None = None) -> int:
        """Cancel every running task; their notifications are never sent."""
        from .agents.tasks import CANCEL_WAIT

        count = await self.tasks.cancel_all(CANCEL_WAIT if wait is None else wait)
        self._agent.notifications = []
        return count

    # Resuming -------------------------------------------------------------------------------

    def _sessions_root(self) -> Path:
        return self.context_root or Path.cwd()

    def sessions(self, limit: int = 20):
        return list_sessions(self._sessions_root(), home=self.sessions_home, limit=limit) if self.persist else []

    def unavailable(self) -> str | None:
        """Why /resume cannot work in this run, or None."""
        if not self.persist:
            return "会话存档已关闭（persist_sessions: false），不能恢复会话。"
        return None

    def resolve_session(self, session_id: str | None) -> str:
        """'' or None means the most recent session of this project."""
        reason = self.unavailable()
        if reason:
            raise ProviderError(reason)
        if session_id:
            if find_session(self._sessions_root(), session_id, home=self.sessions_home) is None:
                recent = "、".join(s.id for s in self.sessions(5)) or "无"
                raise ProviderError(f"未找到会话 {session_id}。最近的会话：{recent}")
            return session_id
        recent = [s for s in self.sessions() if s.id != self.session_id]
        if not recent:
            raise ProviderError("本项目没有可恢复的会话。")
        return recent[0].id

    async def resume(self, session_id: str):
        """Restore a saved session, then compact it if it no longer fits. Yields Resumed first."""
        if self._busy:
            raise ProviderError("当前任务正在运行，不能恢复会话。")
        reason = self.unavailable()
        if reason:
            raise ProviderError(reason)
        if session_id == self.session_id and self._agent.history:
            raise ProviderError("这已是当前会话。")
        path = find_session(self._sessions_root(), session_id, home=self.sessions_home)
        if path is None:
            raise ProviderError(f"未找到会话 {session_id}。")
        session = Session.from_transcript(path)
        transcript = Transcript(session, self._sessions_root().resolve())
        try:
            transcript.open_existing()  # Lock the new one before letting go of the current one.
        except SessionLocked as exc:
            raise ProviderError(str(exc)) from None
        except OSError as exc:
            raise ProviderError(f"无法打开会话存档：{exc.strerror or exc}") from None
        try:
            provider = self.provider_info[:2] if self.provider_info else None
            restored = await asyncio.to_thread(load_session, path, provider)
        except OSError as exc:
            transcript.close()
            raise ProviderError(f"无法读取会话存档：{exc.strerror or exc}") from None
        await self.tasks.reset()  # Their end records go to the session being left.
        await self._session_end("resume")
        if self._transcript is not None:
            self._transcript.close()
        self.session, self._agent.session = session, session
        if self.hooks is not None:
            self.hooks.new_session(session)
        if self.registry is not None:
            self.registry.use_session(session)
            # Approvals belong to the run that granted them, never to a restored transcript.
            self.registry.permissions.session.clear()
        self._transcript = transcript
        self._use_checkpoints(session)
        if self.checkpoints is not None:
            self.checkpoints.load(restored.snapshots, restored.marks)
        self._agent.restore(restored.history, gap_reminder(restored.last_time))
        if self.registry is not None:
            self.registry.todos.restore(restored.todos)
        self.goal, self.last_goal, self._goal_turn = None, None, None
        if restored.goal is not None:
            # Counted afresh and never started on its own: the user's next message or /goal resumes it.
            self.goal = Goal(restored.goal, self.goal_max_turns, self._goal_tokens(), paused="恢复的会话")
        self._sync_goal()
        self._drop_skills()
        self._rewind()
        notices = []
        if self.context_root is not None:
            async with self._start_lock:
                self._context_loaded = True
                notices = await self._load_context()
        notices.extend(self._restore_skills(restored.skills))
        if restored.skipped:
            notices.append(f"跳过了 {restored.skipped} 行无法读取的记录。")
        if restored.lost_images:
            notices.append(f"{restored.lost_images} 张图片的文件已丢失，已改为文字说明。")
        if restored.interrupted:
            notices.append(f"{restored.interrupted} 个工具调用在中断时没有结果，已标记为“会话中断”；模型会先检查状态。")
        if self.goal is not None:
            notices.append("会话有未完成的目标，已暂停：发送消息或输入 /goal 继续，/goal clear 清除。")
        if restored.unanswered:
            notices.append(f"最后 {len(restored.unanswered)} 条消息没有得到回复，已放回输入框，可以修改后重新发送。")
        await self._session_start("resume")
        self._team_context_due = self.teams is not None
        notices.extend(self._raised)
        self._raised = []
        yield Resumed(restored, sum(1 for m in restored.history if m.role != "tool"), tuple(notices))
        async with aclosing(self._agent.compact_restored()) as events:
            async for event in events:
                self._observe(event)
                yield event
                for notice in self._drain():
                    yield notice

    # Context ----------------------------------------------------------------------------------

    async def _load_context(self) -> list[str]:
        root = self.context_root.resolve()
        files, problems = await asyncio.to_thread(load_instructions, root, self.instructions_home)
        sections = [instructions_section(files)]
        self.project_instructions = sections[0]
        if self.memory is not None:
            sections.append(await asyncio.to_thread(self._memory_section))
        self._agent.instructions = "\n\n".join(part for part in sections if part)
        self._agent.git = await asyncio.to_thread(git_snapshot, root)

        def shown(item):
            notes = [f"引用 {'、'.join(display_path(root, path) for path in item.includes)}"] if item.includes else []
            notes += ["已截断"] if item.truncated or item.overflow else []
            return display_path(root, item.path) + "".join(f"（{note}）" for note in notes)

        # Listed by /memory rather than announced at startup; only problems are announced.
        self.instruction_files = tuple(shown(f) for f in files)
        cut = [display_path(root, f.path) for f in files if f.truncated or f.overflow]
        return problems + ([f"项目指令过长，已截断：{'、'.join(cut)}；可用 /memory 查看已加载的文件。"] if cut else [])

    def _memory_section(self) -> str:
        """Indexes regenerated from the notes, then frozen for the session."""
        try:
            self.memory.refresh()
        except OSError:
            pass  # A read-only home still gets the notes it can read.
        # The limit bounds the whole injected section, the fixed explanation included.
        limit = INDEX_TOKENS - estimate_text(memory_section("-"))
        return memory_section(self.memory.index_section(limit))

    async def start(self, *, wait_mcp: bool = True, on_mcp=None):
        """Idempotent; only the first call reports context notices.

        wait_mcp=False (the TUI) starts MCP discovery in the background: each server
        registers its tools when ready and reports through on_mcp(status). Once that
        has happened, later calls (every reply) no longer wait for MCP either.
        """
        notices, self._notices = self._notices, []
        async with self._start_lock:
            if self.context_root is not None and not self._context_loaded:
                self._context_loaded = True
                notices = notices + await self._load_context()
            if not self._session_started:
                await self._session_start("new")
        if self.mcp_manager is not None:
            if wait_mcp and not self._mcp_background:
                notices.extend(await self.mcp_manager.start(self.registry, problems_only=True))
            elif not self._mcp_background:
                self._mcp_background = True
                notices.extend(self.mcp_manager.notices)  # Configuration problems are known now.
                self.mcp_manager.begin(self.registry, on_mcp)
        return tuple(notices)

    def worktree_manager(self):
        """The git repository of the project; WorktreeError outside one."""
        if self._worktrees is None:
            from .worktree import WorktreeManager

            root = self.registry.permissions.root if self.registry is not None else (self.context_root or Path.cwd())
            self._worktrees = WorktreeManager.discover(
                Path(root), links=self.worktree_links, stale_hours=self.worktree_stale_hours
            )
        return self._worktrees

    def start_worktree_cleanup(self, on_report) -> bool:
        """In the background; nothing outside a repository or when turned off."""
        if self._worktree_cleanup is not None or self.worktree_stale_hours <= 0:
            return False

        async def run() -> None:
            from .worktree import WorktreeError

            try:
                manager = await asyncio.to_thread(self.worktree_manager)
            except WorktreeError:
                return
            await manager.keep_clean(on_report)

        self._worktree_cleanup = asyncio.ensure_future(run())
        return True

    async def close(self):
        try:
            if self._worktree_cleanup is not None:
                self._worktree_cleanup.cancel()
                await asyncio.gather(self._worktree_cleanup, return_exceptions=True)
            if self.teams is not None:
                await self.teams.close()
            await self.tasks.cancel_all()
            if self.hooks is not None:
                try:
                    # session_end's synchronous hooks get 10 seconds, background ones 3 more.
                    await asyncio.wait_for(self._session_end("exit"), 10)
                except TimeoutError:
                    pass
                await self.hooks.drain(3)
            if self.extractor is not None:
                await self.extractor.close()
            if self.mcp_manager is not None:
                await self.mcp_manager.close()
        finally:
            if self._transcript is not None:
                self._transcript.close()
            await self.provider.close()
            if self.memory_provider is not None and self.memory_provider is not self.provider:
                await self.memory_provider.close()
            if self.goal_provider is not None and self.goal_provider not in (self.provider, self.memory_provider):
                await self.goal_provider.close()

    async def reply(
        self,
        prompt: str,
        *,
        command: str | None = None,
        fork: ForkRequest | None = None,
        synthetic: str | None = None,
        images: Mapping[str, ProcessedImage] | None = None,
    ):
        """images: pasted pictures by their placeholder in prompt."""
        try:
            async with aclosing(self._reply(prompt, command, fork, synthetic, images)) as stream:
                async for event in stream:
                    yield event
        finally:
            if self._agent.notifications:  # Arrived after the last iteration boundary.
                self._tasks_ready()

    async def _team_context(self, prompt: str, command: str | None) -> tuple[str, str | None]:
        """The lead's next message after /clear or /resume carries the team it still leads; shown as typed."""
        self._team_context_due = False
        try:
            context = await asyncio.to_thread(self.teams.context)
        except Exception:  # A broken team record must not block the message.
            return prompt, command
        if not context:
            return prompt, command
        return f"{context}\n\n{prompt}", command or prompt

    async def _reply(
        self,
        prompt: str,
        command: str | None,
        fork: ForkRequest | None,
        synthetic: str | None,
        images: Mapping[str, ProcessedImage] | None = None,
    ):
        await self.start()
        self._goal_turn = None
        if synthetic is None:
            self.resume_goal()  # The user's own message: automatic turns run again afterwards.
        command = command or (f"/{fork.skill.name}" if fork else None)
        if self.hooks is not None:
            if self._busy:
                raise ProviderError("已有请求正在运行，请等待或取消。")
            self.hooks.begin_turn()
            # With every request of this turn; a fork's sub-agent never sees it.
            self._agent.turn_notes = await self._fire("turn_start", {"prompt": command or prompt})
            for notice in self._drain():
                yield notice
        if self._team_context_due and not fork and synthetic is None:
            prompt, command = await self._team_context(prompt, command)
        refs = await asyncio.to_thread(self._image_refs, images, prompt) if images else None
        attached = None
        if not fork and synthetic is None and self.registry is not None and "@" in prompt:
            attached = await attach(prompt, self.registry)
            if attached is not None:
                yield FilesAttached(attached.entries)
        run = (
            self._fork(fork, command)
            if fork
            else self._agent.run(
                prompt,
                command=command,
                synthetic=synthetic,
                images=refs,
                attachments=attached.message if attached is not None else None,
            )
        )
        stopped, tools = None, False
        try:
            async with aclosing(run) as stream:
                async for event in stream:
                    if isinstance(event, RunStopped):
                        stopped = event
                    elif isinstance(event, ToolProgress):
                        tools = True
                    self._observe(event)
                    yield event
                    for notice in self._drain():
                        yield notice
        except ProviderError as exc:
            self._goal_turn = ("error", tools)
            self._agent.turn_notes = ()
            await self._turn_end("error", str(exc))
            raise
        except BaseException:  # Cancelled, or closed by the consumer.
            self._goal_turn = ("cancelled", tools)
            self._agent.turn_notes = ()
            self._turn_end_later("cancelled")
            raise
        self._goal_turn = (stopped.reason if stopped else "completed", tools)
        self._agent.turn_notes = ()
        if self.hooks is not None:
            await self._turn_end(stopped.reason if stopped else "completed", stopped.notice if stopped else "")
            for notice in self._drain():
                yield notice

    # Goal -------------------------------------------------------------------------------------

    def _goal_tokens(self) -> int:
        usage = self._agent.total_usage
        return usage.input_tokens + usage.output_tokens

    def goal_tokens(self) -> int:
        """Tokens spent since the goal was set, its evaluations included."""
        return self._goal_tokens() - self.goal.tokens if self.goal is not None else 0

    def _sync_goal(self) -> None:
        self._agent.goal_note = goal_reminder(self.goal)

    def set_goal(self, condition: str) -> Goal:
        """Replaces any goal; ValueError for an empty or too long condition."""
        condition = check_condition(condition)
        if self._busy:
            raise ProviderError("当前任务正在运行，不能设置目标。")
        self.clear_goal()
        self.goal = Goal(condition, self.goal_max_turns, self._goal_tokens())
        self._record(goal_record("set", condition))
        self._sync_goal()
        return self.goal

    def clear_goal(self, event: str = "cleared", reason: str = "") -> Goal | None:
        goal, self.goal = self.goal, None
        if goal is not None:
            self.last_goal = (goal.condition, event, reason)
            self._record(goal_record(event, goal.condition, reason))
        self._sync_goal()
        return goal

    def pause_goal(self, reason: str) -> GoalStep:
        goal = self.goal
        if goal is None:
            return GoalStep("none")
        goal.paused = reason
        return GoalStep("paused", f"◎ 目标已暂停：{reason}。发送消息或输入 /goal 继续，/goal clear 清除。")

    def resume_goal(self) -> bool:
        """After a pause; a goal paused at its turn limit gets as many turns again."""
        goal = self.goal
        if goal is None or not goal.paused:
            return False
        if goal.turns >= goal.budget:
            goal.budget = goal.turns + self.goal_max_turns
        goal.paused, goal.idle, goal.failures = "", 0, 0
        self._sync_goal()
        return True

    async def goal_after_turn(self) -> GoalStep:
        """Judge the goal after a turn: another turn, the end of the goal, a pause, or a wait for background work."""
        goal, turn, self._goal_turn = self.goal, self._goal_turn, None
        if goal is None or turn is None or goal.paused:
            return GoalStep("none")
        reason, tools = turn
        goal.turns += 1
        goal.idle = 0 if tools else goal.idle + 1
        stops = {
            "cancelled": "本轮已取消",
            "error": "本轮出错",
            "max_iterations": "本轮达到最大迭代轮数",
            "max_tokens": "回复达到输出上限被截断",
            "unknown_tools": "模型连续调用不存在的工具",
        }
        if reason != "completed":
            return self.pause_goal(stops.get(reason, "本轮没有正常结束"))
        if self.tasks.running():
            # Their notification starts a turn of its own; the goal is judged after that one.
            return GoalStep("deferred")
        provider = self.goal_provider or self.memory_provider or self.provider
        try:
            verdict = await evaluate(provider, goal.condition, self._agent.history, self._agent._usage)
        except Exception as exc:  # A failed or unreadable evaluation counts as not met.
            if self.goal is not goal:
                return GoalStep("none")
            goal.failures += 1
            if goal.failures >= FAILURE_LIMIT:
                return self.pause_goal(f"连续 {FAILURE_LIMIT} 次无法判断目标是否达成（{exc}）")
            verdict = Verdict("not_met", "")
        else:
            goal.failures = 0
        if self.goal is not goal:  # Cleared or replaced while the evaluation ran.
            return GoalStep("none")
        if verdict.verdict == "met":
            self.clear_goal("achieved", verdict.reason)
            return GoalStep("met", "◎ 目标已达成" + (f"：{verdict.reason}" if verdict.reason else ""))
        if verdict.verdict == "impossible":
            self.clear_goal("impossible", verdict.reason)
            return GoalStep("impossible", f"◎ 目标无法达成，已清除：{verdict.reason or '没有给出原因'}")
        goal.reason = verdict.reason
        self._sync_goal()
        if goal.idle >= IDLE_LIMIT:
            return self.pause_goal(f"连续 {IDLE_LIMIT} 轮没有调用工具")
        if goal.turns >= goal.budget:
            return self.pause_goal(f"已自动运行 {goal.turns} 轮，达到上限")
        note = "◎ 目标尚未达成" + (f"：{verdict.reason}" if verdict.reason else "") + "，继续下一轮"
        return GoalStep("continue", note, continue_prompt(verdict.reason))

    # Fork skills ------------------------------------------------------------------------

    def _is_current(self, name: str) -> bool:
        return self.provider_info is not None and self.provider_info[0] == name

    def _fork_provider_config(self, name: str):
        return next((config for config in self.provider_configs if config.name == name), None)

    def _fork_history(self, context: str) -> list[Message]:
        """None, the last 3 turns (from a real user message, calls kept with their results), or all."""
        history = deepcopy(self._agent.history)
        if context == "full":
            return history
        if context == "recent":
            starts = [i for i, m in enumerate(history) if m.role == "user" and m.synthetic is None]
            return history[starts[-3] :] if starts else []
        return []

    def _fork_registry(self, skill: SkillDef, task) -> ToolRegistry | None:
        """The main tools and its own, or only those it lists; never load_skill or another skill tool.

        Its own file tools (a copy of the main reads when it sees some history) and permission view.
        """
        if self.registry is None:
            return None
        policy = self.subagents.policy(None, task)
        root = policy.root
        own = [SkillTool(root, skill, spec) for spec in skill.tools]
        listed = set(skill.allowed_tools) - GLOBAL_BAN if skill.allowed_tools else None
        state = self.registry.file_state()
        state = state.copy() if state is not None and skill.fork_context != "none" else None
        return self.registry.for_agent(
            agent="fork",
            agent_type=task.agent_type,
            permissions=policy,
            state=state,
            keep=lambda name: name not in GLOBAL_BAN and (listed is None or name in listed),
            extra=[tool for tool in own if listed is None or tool.name in listed],
        )

    def _fork_provider(self, skill: SkillDef):
        """(provider, budget, owned); owned providers are closed after the run."""
        if not skill.model or self._is_current(skill.model):
            return self.provider, self._agent.budget, False
        config = self._fork_provider_config(skill.model)
        if config is None:
            raise ProviderError(f"Skill {skill.name} 未运行：没有名为 {skill.model} 的提供商。")
        factory = self.provider_factory
        if factory is None:
            from .providers import create_provider as factory
        try:
            return factory(config), config.budget(), True
        except Exception as exc:
            raise ProviderError(f"Skill {skill.name} 未运行：无法创建提供商 {skill.model}：{exc}") from None

    def _fork_result(self, skill: SkillDef, command: str, text: str) -> None:
        """A user message (the command) and the assistant's answer, as one exchange."""
        note = f"{command}\n\n（以上命令在独立上下文中运行了 Skill {skill.name}，结果见下一条回复。）"
        self._agent._append(Message("user", (ContentBlock("text", note),), command=command))
        self._agent._append(Message("assistant", (ContentBlock("text", text),)))

    async def _fork(self, fork: ForkRequest, command: str):
        """Run the SOP in a sub-agent with its own history; only the result joins this one."""
        main, skill = self._agent, fork.skill
        if main.busy:
            raise ProviderError("已有请求正在运行，请等待或取消。")
        provider, budget, owned = self._fork_provider(skill)
        tasks = self.subagents.tasks
        task = tasks.new(
            origin="skill", agent="fork", agent_type=f"skill:{skill.name}", description=command, prompt=fork.prompt
        )
        child = AgentLoop(
            provider,
            "",
            self._fork_registry(skill, task),
            max_iterations=main.max_iterations,
            unknown_limit=main.unknown_limit,
        )
        child.system, child.instructions, child.git = main.system, main.instructions, main.git  # No catalog.
        child.budget, child.auto_compact, child.session = budget, main.auto_compact, self.session
        child.history = self._fork_history(skill.fork_context)
        self.subagents.attach(task, child)  # Usage to the task and this conversation; the task keeps a transcript.
        main.busy = True
        stopped, final, ended = None, None, False

        def end(text: str) -> None:
            nonlocal ended
            self._fork_result(skill, command, text)
            ended = True
            if task.status == "running" and not (
                stopped is not None and stopped.reason in {"completed", "max_tokens"} and final is not None
            ):
                task.status = "failed"
            tasks.settle(task, text)

        try:
            try:
                prompt = await self.subagents.start_hooks(task, fork.prompt)
                async with aclosing(child.run(prompt)) as events:
                    async for event in events:
                        if isinstance(event, RunStopped):
                            stopped = event  # The one RunStopped comes after the exchange is added.
                            continue
                        if isinstance(event, Completed):
                            final = event.message.text
                        if isinstance(event, UsageUpdate):
                            event = replace(event, total=main.total_usage)  # The session total, as before.
                        elif isinstance(event, ToolProgress) and event.phase == "running":
                            task.tool_calls += 1
                        yield event
            except asyncio.CancelledError:
                task.status = "cancelled"
                end(f"[Skill {skill.name} 未完成：已取消]")
                yield RunStopped(
                    "cancelled",
                    f"已取消 Skill {skill.name}；主对话可以继续。",
                    stopped.iterations if stopped else 0,
                    stopped.results if stopped else (),
                )
                raise
            except ProviderError as exc:
                end(f"[Skill {skill.name} 未完成：{exc}]")
                raise
            if stopped is not None and stopped.reason in {"completed", "max_tokens"} and final is not None:
                end(final or "（Skill 没有输出正文。）")
            else:
                end(f"[Skill {skill.name} 未完成：{stopped.notice if stopped is not None else '没有完整回复。'}]")
            if stopped is not None:
                yield stopped
        finally:
            if not ended:  # Closed from outside, e.g. the consumer was cancelled between events.
                task.status = "cancelled"
                end(f"[Skill {skill.name} 未完成：已取消]")
            main.busy = False
            if owned:
                await provider.close()

    async def ask(self, prompt: str) -> str:
        async with aclosing(self.reply(prompt)) as stream:
            async for event in stream:
                if isinstance(event, Completed):
                    return event.message.text
                if isinstance(event, RunStopped) and event.reason in {"unknown_tools", "max_iterations", "max_tokens"}:
                    return event.notice
        raise ProviderError("没有完整回复。")

    async def run_command(self, command: str, *, keep: bool = True) -> CommandRun:
        """A ! command; with keep, the command and its output join the history for the next request."""
        await self.start()
        if self._busy:
            raise ProviderError("已有请求正在运行，请等待或取消。")
        if self.registry is None:
            return CommandRun(command, "", None, "当前没有可用的工具。")
        run = await run_user_command(command, self.registry)
        if keep and run.exit_code is not None:
            self._agent._append(command_message(run))
        return run
