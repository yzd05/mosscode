"""Stable facade: one independent AgentLoop owns state and history."""

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .agent import DEFAULT_MAX_ITERATIONS, AgentLoop, Provider
from .context import ContextBudget, estimate_text
from .memory import INDEX_TOKENS, MemoryStore
from .memory_extract import MemoryExtractor
from .messages import CompactFailed, CompactFinished, Completed, Message, ProviderError, RunStopped
from .prompts import (display_path, gap_reminder, git_snapshot, instructions_section, load_instructions,
                      memory_section)
from .session import (Restored, Session, SessionLocked, Transcript, find_session, list_sessions, load_session,
                      provider_record)
from .tools.base import ToolRegistry
from .tools.memory import Memory


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
                 memory: MemoryStore | None = None, auto_memory: bool = False, memory_provider: Provider | None = None):
        self.provider, self.system, self.registry = provider, system, registry
        self.mcp_manager = mcp_manager
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
        self._agent.clear()
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
        self._rewind()
        notices = []
        if self.context_root is not None:
            async with self._start_lock:
                self._context_loaded = True
                notices = await self._load_context()
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

    async def reply(self, prompt: str):
        await self.start()
        async with aclosing(self._agent.run(prompt)) as stream:
            async for event in stream:
                self._observe(event)
                yield event
                for notice in self._drain():
                    yield notice

    async def ask(self, prompt: str) -> str:
        async with aclosing(self.reply(prompt)) as stream:
            async for event in stream:
                if isinstance(event, Completed):
                    return event.message.text
                if isinstance(event, RunStopped) and event.reason in {"unknown_tools", "max_iterations", "max_tokens"}:
                    return event.notice
        raise ProviderError("没有完整回复。")
