"""Stable facade: one independent AgentLoop owns state and history."""

import asyncio
from contextlib import aclosing
import difflib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .agent import DEFAULT_MAX_ITERATIONS, AgentLoop, Provider
from .context import ContextBudget, estimate_text
from .memory import INDEX_TOKENS, MemoryStore
from .memory_extract import MemoryExtractor
from .messages import CompactFailed, CompactFinished, Completed, ContentBlock, Message, ProviderError, RunStopped
from .prompts import (display_path, gap_reminder, git_snapshot, instructions_section, load_instructions,
                      memory_section)
from .skills import ACTIVE_LIMIT, LOAD_SKILL, ForkRequest, SkillCatalog, SkillDef, missing_tools
from .session import (Restored, Session, SessionLocked, Transcript, find_session, list_sessions, load_session,
                      provider_record, skill_record)
from .tools.base import ToolFailure, ToolRegistry
from .tools.memory import Memory
from .tools.skill import LoadSkill, SkillTool


@dataclass(frozen=True)
class Notice:
    """A local note raised outside the model's events, e.g. a transcript write failure."""
    text: str


@dataclass(frozen=True)
class MemoryUpdated:
    """The background extractor wrote or deleted notes (9c A8)."""
    names: tuple[str, ...]


@dataclass(frozen=True)
class Resumed:
    """A session was restored; shown before any compaction it needs (R14)."""
    restored: Restored
    messages: int
    notices: tuple[str, ...]


class Conversation:
    def __init__(self, provider: Provider, system: str = "", registry: ToolRegistry | None = None,
                 *, max_iterations: int = DEFAULT_MAX_ITERATIONS, unknown_limit: int = 3,
                 mcp_manager=None, context_root: Path | None = None, instructions_home: Path | None = None,
                 budget: ContextBudget | None = None, session=None, auto_compact: bool = True,
                 notices: tuple[str, ...] = (), persist: bool = False,
                 provider_info: tuple[str, str, str] | None = None, sessions_home: Path | None = None,
                 memory: MemoryStore | None = None, auto_memory: bool = False, memory_provider: Provider | None = None,
                 skills: SkillCatalog | None = None, provider_configs=(), provider_factory=None):
        self.provider, self.system, self.registry = provider, system, registry
        self.mcp_manager = mcp_manager
        self.skills = skills if skills is not None else SkillCatalog()  # L7: empty without a scan.
        self._skill_tools: dict[str, tuple[str, ...]] = {}  # Skill name -> its tools registered here (X8).
        # K6: a fork skill's model names one of these; the factory builds it (create_provider by default).
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
        # Transcripts need a session from Session.new; persist=False keeps only stored results.
        self.persist = persist and session is not None and session.transcript_path is not None
        self.sessions_home = sessions_home
        self._transcript: Transcript | None = None
        self._agent.on_commit = self._record
        self._use_session(session)
        # 9c: notes are injected only when context is loaded (context_root); None turns memory off.
        self.memory = memory
        self.memory_provider = memory_provider
        self.on_memory = None  # Set by the TUI: receives MemoryUpdated and Notice from the background.
        self.extractor: MemoryExtractor | None = None
        self._cursor = 0  # History index up to which messages went to the extractor (A2).
        self._cut: int | None = None  # Cut of the compaction in progress, once its messages were handed over.
        if memory is not None:
            if registry is not None:
                if registry.get("memory") is None:
                    registry.register(Memory(registry.permissions.root, memory))
                registry.permissions.memory_dirs = tuple(path.resolve() for path in memory.dirs.values())
            if auto_memory:
                self.extractor = MemoryExtractor(memory, lambda: self.memory_provider or self.provider,
                                                 on_result=lambda r: self._emit(MemoryUpdated(r.names)),
                                                 on_notice=lambda text: self._emit(Notice(text)),
                                                 on_usage=self._agent._usage)
                self._agent.on_compact = self._handover
        self._notices.extend(self._offer_skills())

    def _use_session(self, session) -> None:
        if self._transcript is not None:
            self._transcript.close()
        self.session = session
        # Stored tool results and compaction records; without it large results are only cut to a preview.
        self._agent.session = session
        if self.registry is not None and session is not None:
            self.registry.use_session(session)
        root = self.context_root or Path.cwd()
        self._transcript = Transcript(session, root.resolve()) if self.persist else None

    def _record(self, record: dict) -> None:
        if self._transcript is not None:
            notice = self._transcript.append(record)
            if notice:
                self._raised.append(notice)

    def _emit(self, event) -> None:
        if self.on_memory is not None:
            self.on_memory(event)

    # Background extraction (9c A1, A2) ---------------------------------------------------------

    def _extract(self) -> None:
        """After a completed task: everything since the cursor goes to the extractor."""
        if self.extractor is None:
            return
        history = self._agent.history
        messages, self._cursor = history[self._cursor:], len(history)
        self.extractor.schedule(messages)

    def _handover(self, removed) -> None:
        """Before a compaction: messages about to be summarized that the extractor has not seen yet."""
        if self._cursor < len(removed):
            self.extractor.schedule(removed[self._cursor:])
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

    def set_mode(self, mode: str):
        self._agent.set_mode(mode)

    def queue(self, text: str):
        """Steer a running task; the text joins at the next iteration boundary."""
        if text.strip():
            self._agent.pending.append(text)

    def take_queued(self) -> tuple[str, ...]:
        texts, self._agent.pending = tuple(self._agent.pending), []
        return texts

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
        self._agent.clear()  # A8: active skills too.
        self._drop_skills()
        self._next_session()
        self._rewind()
        if self.context_root is None:
            return ()
        async with self._start_lock:
            self._context_loaded = True
            return tuple(await self._load_context())

    async def set_provider(self, provider: Provider, budget: ContextBudget | None = None,
                           info: tuple[str, str, str] | None = None):
        old = self.provider
        self._agent.set_provider(provider, budget)
        self.provider = provider
        if info is not None:
            self.provider_info = info
            if self._agent.history:
                self._record(provider_record(*info))
        if old is not provider:
            await old.close()

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
        """A1/A3: the catalog section and load_skill, when tools are on and the model may load a skill."""
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
        """W4: the tools it lists must be registered now."""
        return missing_tools(skill, self.registry.names() if self.registry is not None else ())

    def _use_skill_dir(self, skill: SkillDef) -> None:
        """S1: its files can be read without approval once it is in use."""
        if self.registry is None:
            return
        policy = self.registry.permissions
        directory = skill.directory.resolve()
        if directory not in policy.skill_dirs:
            policy.skill_dirs += (directory,)

    def _register_skill_tools(self, skill: SkillDef) -> None:
        """X8/X10: the version just read replaces whatever this skill registered before."""
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
        """E2/W4: read the skill again and check its tools; returns the skill, the request and notices.

        A fork skill's tools go to its sub-agent only (X8); the caller sends a ForkRequest.
        """
        if self.skills.get(name) is None:
            raise ProviderError(f"未找到 Skill {name}。输入 /skills 查看。")
        skill, notice = self.skills.fresh(name)
        missing = self._missing(skill)
        if missing:
            raise ProviderError(f"Skill {name} 未运行：工具不可用：{'、'.join(missing)}。"
                                "MCP 工具需要对应的 Server 已连接（/mcp 查看）。")
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
        """A4/A5/A7/A10: the load_skill tool; failures are tool errors and the run goes on."""
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
            raise ToolFailure("skill_too_large", f"已加载的 Skill 规程合计将超过 {ACTIVE_LIMIT} 个字符，未加载 {name}。"
                              "可以请用户 /clear 后再加载，或直接按已加载的规程继续。")
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
        """C3/X9: the catalog after /skills reload; removed skills leave the active list and take their tools."""
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
        """A9: tools of every skill the session used, SOPs of the pinned ones; the rest is reported."""
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
        """R5-R13: restore a saved session, then compact it if it no longer fits. Yields Resumed first."""
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
        if self._transcript is not None:
            self._transcript.close()
        self.session, self._agent.session = session, session
        if self.registry is not None:
            self.registry.use_session(session)
            # Approvals belong to the run that granted them, never to a restored transcript.
            self.registry.permissions.session.clear()
        self._transcript = transcript
        self._agent.restore(restored.history, gap_reminder(restored.last_time))
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
        if restored.interrupted:
            notices.append(f"{restored.interrupted} 个工具调用在中断时没有结果，已标记为“会话中断”；模型会先检查状态。")
        if restored.unanswered:
            notices.append(f"最后 {len(restored.unanswered)} 条消息没有得到回复，已放回输入框，可以修改后重新发送。")
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
        """J1: indexes regenerated from the notes, then frozen for the session."""
        try:
            self.memory.refresh()
        except OSError:
            pass  # A read-only home still gets the notes it can read.
        # J3 bounds the whole injected section, the fixed explanation included.
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
        if self.mcp_manager is not None:
            if wait_mcp and not self._mcp_background:
                notices.extend(await self.mcp_manager.start(self.registry, problems_only=True))
            elif not self._mcp_background:
                self._mcp_background = True
                notices.extend(self.mcp_manager.notices)  # Configuration problems are known now.
                self.mcp_manager.begin(self.registry, on_mcp)
        return tuple(notices)

    async def close(self):
        try:
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

    async def reply(self, prompt: str, *, command: str | None = None, fork: ForkRequest | None = None):
        await self.start()
        run = self._fork(fork, command or f"/{fork.skill.name}") if fork else self._agent.run(prompt, command=command)
        async with aclosing(run) as stream:
            async for event in stream:
                self._observe(event)
                yield event
                for notice in self._drain():
                    yield notice

    # Fork skills (K1-K11) ------------------------------------------------------------------------

    def _is_current(self, name: str) -> bool:
        return self.provider_info is not None and self.provider_info[0] == name

    def _fork_provider_config(self, name: str):
        return next((config for config in self.provider_configs if config.name == name), None)

    def _fork_history(self, context: str) -> list[Message]:
        """K2: none, the last 3 turns (from a real user message, calls kept with their results), or all."""
        history = deepcopy(self._agent.history)
        if context == "full":
            return history
        if context == "recent":
            starts = [i for i, m in enumerate(history) if m.role == "user" and m.synthetic is None]
            return history[starts[-3]:] if starts else []
        return []

    def _fork_registry(self, skill: SkillDef) -> ToolRegistry | None:
        """K3: the main tools and its own, or only those it lists; never load_skill."""
        if self.registry is None:
            return None
        root = self.registry.permissions.root
        own = [SkillTool(root, skill, spec) for spec in skill.tools]
        if not skill.allowed_tools:
            names = [name for name in self.registry.names() if name != LOAD_SKILL]
            return self.registry.subset(names, own)
        listed = set(skill.allowed_tools) - {LOAD_SKILL}
        return self.registry.subset(listed, [tool for tool in own if tool.name in listed])

    def _fork_provider(self, skill: SkillDef):
        """K6: (provider, budget, owned); owned providers are closed after the run."""
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
        """K8/K9: a user message (the command) and the assistant's answer, as one exchange."""
        note = f"{command}\n\n（以上命令在独立上下文中运行了 Skill {skill.name}，结果见下一条回复。）"
        self._agent._append(Message("user", (ContentBlock("text", note),), command=command))
        self._agent._append(Message("assistant", (ContentBlock("text", text),)))

    async def _fork(self, fork: ForkRequest, command: str):
        """K1-K11: run the SOP in a sub-agent with its own history; only the result joins this one."""
        main, skill = self._agent, fork.skill
        if main.busy:
            raise ProviderError("已有请求正在运行，请等待或取消。")
        provider, budget, owned = self._fork_provider(skill)
        child = AgentLoop(provider, "", self._fork_registry(skill), max_iterations=main.max_iterations,
                          unknown_limit=main.unknown_limit)
        child.system, child.instructions, child.git = main.system, main.instructions, main.git  # K4: no catalog.
        child.budget, child.auto_compact, child.session = budget, main.auto_compact, self.session  # K11
        child.history = self._fork_history(skill.fork_context)
        child.total_usage = main.total_usage  # K10: its requests count towards this conversation.
        main.busy = True
        stopped, final, ended = None, None, False

        def end(text: str) -> None:
            nonlocal ended
            main.total_usage = child.total_usage
            self._fork_result(skill, command, text)
            ended = True

        try:
            try:
                async with aclosing(child.run(fork.prompt)) as events:
                    async for event in events:
                        if isinstance(event, RunStopped):
                            stopped = event  # K7: the one RunStopped comes after the exchange is added.
                            continue
                        if isinstance(event, Completed):
                            final = event.message.text
                        yield event
            except asyncio.CancelledError:
                end(f"[Skill {skill.name} 未完成：已取消]")
                yield RunStopped("cancelled", f"已取消 Skill {skill.name}；主对话可以继续。",
                                 stopped.iterations if stopped else 0, stopped.results if stopped else ())
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
