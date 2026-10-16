"""Stable facade: one independent AgentLoop owns state and history."""

import asyncio
from contextlib import aclosing
from copy import deepcopy
from pathlib import Path

from .agent import DEFAULT_MAX_ITERATIONS, AgentLoop, Provider
from .messages import Completed, ProviderError, RunStopped
from .prompts import git_snapshot, instructions_section, load_instructions
from .tools.base import ToolRegistry


class Conversation:
    def __init__(self, provider: Provider, system: str = "", registry: ToolRegistry | None = None,
                 *, max_iterations: int = DEFAULT_MAX_ITERATIONS, unknown_limit: int = 3,
                 mcp_manager=None, context_root: Path | None = None, instructions_home: Path | None = None):
        self.provider, self.system, self.registry = provider, system, registry
        self.mcp_manager = mcp_manager
        # Project files and git are product-level context; None keeps the core hermetic.
        self.context_root, self.instructions_home = context_root, instructions_home
        self._context_loaded = False
        self._mcp_background = False
        self._start_lock = asyncio.Lock()
        self._agent = AgentLoop(provider, system, registry, max_iterations=max_iterations, unknown_limit=unknown_limit)
        self.system = self._agent.system

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

    async def clear(self) -> tuple[str, ...]:
        """New session: empty model context and usage; permissions and MCP stay."""
        self._agent.clear()
        if self.context_root is None:
            return ()
        async with self._start_lock:
            self._context_loaded = True
            return tuple(await self._load_context())

    async def set_provider(self, provider: Provider):
        old = self.provider
        self._agent.set_provider(provider)
        self.provider = provider
        if old is not provider:
            await old.close()

    def set_permission_mode(self, mode: str):
        if self._busy:
            raise ProviderError("当前任务正在运行，不能切换权限模式。")
        if self.registry is None:
            raise ProviderError("当前会话未启用工具。")
        self.registry.permissions.set_mode(mode)

    async def _load_context(self) -> list[str]:
        root = self.context_root.resolve()
        files, problems = await asyncio.to_thread(load_instructions, root, self.instructions_home)
        self._agent.instructions = instructions_section(files)
        self._agent.git = await asyncio.to_thread(git_snapshot, root)

        def shown(path):
            try:
                return path.resolve().relative_to(root).as_posix()
            except ValueError:
                return str(path)
        notices = []
        if files:
            notices.append("已加载项目指令：" + "、".join(shown(f.path) + ("（已截断）" if f.truncated else "") for f in files))
        notices.extend("已跳过项目指令：" + problem for problem in problems)
        return notices

    async def start(self, *, wait_mcp: bool = True, on_mcp=None):
        """Idempotent; only the first call reports context notices.

        wait_mcp=False (the TUI) starts MCP discovery in the background: each server
        registers its tools when ready and reports through on_mcp(status). Once that
        has happened, later calls (every reply) no longer wait for MCP either.
        """
        notices = []
        async with self._start_lock:
            if self.context_root is not None and not self._context_loaded:
                self._context_loaded = True
                notices = await self._load_context()
        if self.mcp_manager is not None:
            if wait_mcp and not self._mcp_background:
                notices.extend(await self.mcp_manager.start(self.registry))
            elif not self._mcp_background:
                self._mcp_background = True
                notices.extend(self.mcp_manager.notices)  # Configuration problems are known now.
                self.mcp_manager.begin(self.registry, on_mcp)
        return tuple(notices)

    async def close(self):
        try:
            if self.mcp_manager is not None:
                await self.mcp_manager.close()
        finally:
            await self.provider.close()

    async def reply(self, prompt: str):
        await self.start()
        async with aclosing(self._agent.run(prompt)) as stream:
            async for event in stream:
                yield event

    async def ask(self, prompt: str) -> str:
        async with aclosing(self.reply(prompt)) as stream:
            async for event in stream:
                if isinstance(event, Completed):
                    return event.message.text
                if isinstance(event, RunStopped) and event.reason in {"unknown_tools", "max_iterations", "max_tokens"}:
                    return event.notice
        raise ProviderError("没有完整回复。")
