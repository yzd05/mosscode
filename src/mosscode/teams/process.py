"""A member in a pane of its own — another MossCode process, started by the lead with
`--team-dir <team> --team-member <name>`.

It writes its pid into the team record, then a heartbeat every HEARTBEAT seconds; it reads its mailbox
every POLL seconds, takes the session mode from its record each turn and writes its status there.
It never receives a key: the configuration is loaded again from the lead's path.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

from . import Member, TeamError, TeamStore
from .backends import HEARTBEAT
from .lock import now
from .member import POLL, MemberRunner, approval_record

MISSING_ENV = "窗格中缺少环境变量 {name}：设为用户环境变量，或改用 in-process"


def missing_key(config) -> str:
    """A key that should come from the environment but does not."""
    env = getattr(config, "api_key_env", None)
    if env and not os.environ.get(env):
        return MISSING_ENV.format(name=env)
    return ""


def fail(store: TeamStore, name: str, text: str) -> int:
    """The lead reads the error from the record and reports backend_failed."""

    def write(team):
        member = team.member(name)
        if member is not None:
            member.error, member.status, member.pid = text, "stopped", None

    try:
        store.modify(write)
    except TeamError:
        pass
    print(f"MossCode：{text}", file=sys.stderr, flush=True)
    return 1


class MemberProcess:
    """The member side of a pane: its runner, its record and its heartbeat."""

    def __init__(
        self,
        store: TeamStore,
        name: str,
        conversation,
        *,
        heartbeat: float = HEARTBEAT,
        poll: float = POLL,
        interactive: bool = False,
    ):
        self.store, self.name, self.conversation = store, name, conversation
        self.heartbeat, self.poll, self.interactive = heartbeat, poll, interactive
        self.runner: MemberRunner | None = None
        self.mode = "default"
        self.exit_code = 0
        self.on_event: Callable[[object], None] | None = None  # The pane's window.
        self.on_approval: Callable[[object], None] | None = None
        # A request settled elsewhere (answered in the lead's window: the choice; else None).
        self.on_settled: Callable[[str, str | None], None] | None = None
        self._asking: dict[str, object] = {}  # Requests waiting for an answer, here or from the lead.
        self._answers: asyncio.Task | None = None
        self._nudge = asyncio.Event()
        self._writes: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    @property
    def team(self) -> str:
        return self.store.name

    def _record(self) -> Member:
        member = self.store.load().member(self.name)
        if member is None:
            raise TeamError("unknown_member", f"团队 {self.team} 中没有成员 {self.name}。")
        return member

    def _session_mode(self) -> str:
        """The lead writes the session mode into the record; read at the start of each turn."""
        try:
            self.mode = self._record().mode or self.mode
        except TeamError:
            pass
        return self.mode

    def _write(self, change: Callable[[Member], None]) -> None:
        async def write():
            async with self._lock:

                def apply(team):
                    member = team.member(self.name)
                    if member is not None:
                        change(member)

                try:
                    await asyncio.to_thread(self.store.modify, apply)
                except TeamError:
                    pass

        job = asyncio.get_running_loop().create_task(write())
        self._writes.add(job)
        job.add_done_callback(self._writes.discard)

    async def settle(self) -> None:
        while pending := [job for job in self._writes if not job.done()]:  # See TeamRuntime.settle in runtime.py.
            await asyncio.gather(*pending, return_exceptions=True)

    def _status(self, state: str, fields: dict) -> None:
        def change(member: Member) -> None:
            member.status = state
            member.last_tool = fields.get("last_tool", "") if state == "running" else ""
            if "usage" in fields:
                member.usage = fields["usage"]
            if "approved" in fields:
                member.approved = fields["approved"]

        self._write(change)

    async def prepare(self) -> MemberRunner:
        """The same runner as an in-process member, restored from its transcript."""
        from ..agents import FORK
        from .runtime import build_runner, load_history, restore_member

        member = await asyncio.to_thread(self._record)
        conversation = self.conversation
        await conversation.start()
        agent = conversation.subagents.catalog.get(member.agent_type) if member.agent_type != FORK else None
        if agent is None:
            raise TeamError("backend_failed", f"Agent 定义 {member.agent_type} 不存在。")
        if agent.model and not conversation._is_current(agent.model):
            config = conversation._fork_provider_config(agent.model)
            problem = missing_key(config) if config is not None else ""
            if problem:
                raise TeamError("backend_failed", problem)
        history = await asyncio.to_thread(load_history, self.store, member)
        if member.worktree:  # In use while this process lives, even after the lead has gone.
            try:
                await conversation.worktree_manager().mark_in_use(
                    member.worktree["flat"], f"团队 {self.team} 成员 {self.name}", pid=os.getpid()
                )
            except Exception:
                pass
        root = Path.cwd().resolve() if member.worktree else None
        relative = (member.worktree or {}).get("relative", "")
        self.mode = member.mode or "default"
        runner = await build_runner(
            conversation,
            self.store,
            member,
            agent,
            root,
            relative,
            on_approval=self._approval,
            poll=self.poll,
            session_mode=self._session_mode,
        )
        restore_member(runner, member, history)
        runner.on_status = self._status
        runner.on_event = self._event
        self.runner = runner
        return runner

    def _approval(self, request) -> None:
        """Shown in this window, and published in the record for the lead to show too."""
        self._asking[request.id] = request
        item = approval_record(request)

        def publish(member: Member) -> None:
            if all(entry.get("id") != request.id for entry in member.approvals if isinstance(entry, dict)):
                member.approvals.append(item)

        self._write(publish)
        if self._answers is None or self._answers.done():
            self._answers = asyncio.get_running_loop().create_task(self._watch_answers())
        if self.on_approval is not None:
            self.on_approval(request)

    def answered_here(self) -> None:
        """This window answered a request: take it out of the record now rather than at the next poll."""
        self._nudge.set()

    async def _watch_answers(self) -> None:
        """While a request waits, take the lead's answers (and only those) from the mailbox; a request
        answered either way leaves the record, which tells the lead to withdraw its card."""
        policy = self.conversation.registry.permissions
        while self._asking:
            try:
                await asyncio.wait_for(self._nudge.wait(), self.poll)
            except TimeoutError:
                pass
            self._nudge.clear()
            try:
                answers = await asyncio.to_thread(self.store.mailbox.take, self.name, {"permission_response"})
            except TeamError:
                answers = []
            remote: dict[str, str] = {}
            for message in answers:
                request_id, choice = message.extra.get("request_id"), message.extra.get("choice")
                if request_id in self._asking:
                    try:
                        if policy.respond(request_id, choice):
                            remote[request_id] = choice
                    except ValueError:
                        pass
            done = {
                request_id
                for request_id in self._asking
                if (future := policy.pending.get(request_id)) is None or future.done()
            }
            if not done:
                continue
            for request_id in done:
                del self._asking[request_id]
                if self.on_settled is not None:
                    self.on_settled(request_id, remote.get(request_id))

            def drop(member: Member, done: set = done) -> None:  # Runs later; keep this round's set.
                member.approvals = [
                    entry for entry in member.approvals if not isinstance(entry, dict) or entry.get("id") not in done
                ]

            self._write(drop)

    def _event(self, event) -> None:
        if self.on_event is not None:
            self.on_event(event)

    async def _beat(self) -> None:
        """Every HEARTBEAT seconds while it runs."""
        while True:
            await asyncio.sleep(self.heartbeat)
            pid = os.getpid()

            def beat(member: Member, pid: int = pid) -> None:
                member.heartbeat, member.pid = now(), pid

            self._write(beat)

    async def serve(self) -> int:
        """Prepare, announce the pid, run until a shutdown_request ends it; the record says stopped after."""
        try:
            runner = await self.prepare()
        except (TeamError, Exception) as exc:  # Whatever stops it from starting goes to the lead.
            message = exc.message if isinstance(exc, TeamError) else f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(fail, self.store, self.name, message)
            self.exit_code = 1
            return 1
        pid = os.getpid()

        def started(member: Member) -> None:
            member.pid, member.heartbeat, member.error = pid, now(), ""
            member.status, member.approvals = "idle", []  # None left from a process that died.

        self._write(started)
        await self.settle()
        beat = asyncio.get_running_loop().create_task(self._beat())
        try:
            runner.start()
            runner.wake()
            await runner.job
        finally:
            beat.cancel()
            if self._answers is not None:
                self._answers.cancel()
            await asyncio.gather(beat, *([self._answers] if self._answers else []), return_exceptions=True)
            await runner.close()

            def stopped(member: Member) -> None:
                member.status, member.pid, member.last_tool, member.approvals = "stopped", None, "", []

            self._write(stopped)
            await self.settle()
            try:
                await self.conversation.close()
            except Exception:
                pass
        return 0


def member_conversation(settings, config, project: Path, *, interactive: bool):
    """Like the CLI's, without teams, MCP or memory extraction; hooks as in -p."""
    from ..agents import AgentCatalog
    from ..conversation import Conversation
    from ..hooks import HookEngine
    from ..providers import create_provider
    from ..session import Session
    from ..tools import create_default_registry

    registry = create_default_registry(project, shell=settings.shell)
    registry.permissions.interactive = interactive
    tool = registry.get("execute_command")
    hooks = HookEngine(project, shell=getattr(tool, "shell", None), enabled=True, trust_project=False)
    conversation = Conversation(
        create_provider(config),
        settings.system_prompt,
        registry,
        max_iterations=settings.max_iterations,
        context_root=project,
        budget=config.budget(),
        session=Session.new(project),
        auto_compact=settings.auto_compact,
        persist=False,
        provider_configs=settings.providers,
        provider_factory=create_provider,
        provider_info=(config.name, config.protocol, config.model),
        hooks=hooks,
        agents=AgentCatalog.load(project),
        worktree_links=settings.worktree_links,
        worktree_stale_hours=0,
    )
    conversation._session_started = True  # A member is not a new session of the user's (no session_start hooks).
    return conversation


def run_member(team_dir: str, name: str, config_path: str | None, provider: str | None) -> int:
    """cli.main with --team-dir/--team-member: the pane's window, or plain lines when not on a terminal."""
    from ..config import ConfigError, find_config, load_settings

    store = TeamStore(Path(team_dir))
    try:
        team = store.load()
    except TeamError as exc:
        print(f"MossCode：{exc.message}", file=sys.stderr)
        return 1
    if team.member(name) is None:
        print(f"MossCode：团队 {team.name} 中没有成员 {name}。", file=sys.stderr)
        return 1
    try:
        settings = load_settings(config_path if config_path else find_config())
        config = settings.select(provider)
    except ConfigError as exc:
        return fail(store, name, str(exc))
    problem = missing_key(config)
    if problem:
        return fail(store, name, problem)
    project = Path(team.project) if team.project else Path.cwd()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        conversation = member_conversation(settings, config, project, interactive=interactive)
    except Exception as exc:
        return fail(store, name, f"{type(exc).__name__}: {exc}")
    process = MemberProcess(store, name, conversation, heartbeat=HEARTBEAT, poll=POLL, interactive=interactive)
    if interactive:
        from .pane_app import MemberApp

        MemberApp(process).run()
        return process.exit_code
    process.on_event = _print_event
    return asyncio.run(process.serve())


def _print_event(event) -> None:
    """Without a terminal (tests, logs): one line per message batch, tool call and reply."""
    from ..messages import Completed, ToolProgress

    if isinstance(event, list):
        for message in event:
            print(f"[{message.sender} · {message.type}] {message.summary or message.body[:80]}", flush=True)
    elif isinstance(event, ToolProgress) and event.phase == "running":
        print(f"● {event.call.name}", flush=True)
    elif isinstance(event, Completed) and event.message.text:
        print(event.message.text, flush=True)
