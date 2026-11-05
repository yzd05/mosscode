"""Match hooks to an event, run them in order, log and report."""

import asyncio
import json
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .actions import ActionResult, public_url, render, run_command, run_http, short, stdin_json
from .config import Hook, HookConfig, HookFile, TrustStore, load_hooks
from .events import EVENTS

FILE_TOOLS = {"read_file", "write_file", "edit_file", "glob", "grep"}
RESULT_LIMIT = 64 * 1024
RECENT = 50


@dataclass(frozen=True)
class HookEvent:
    name: str
    agent: str = "main"
    fields: Mapping[str, str] = field(default_factory=dict)  # Condition fields, as strings.
    payload: Mapping[str, Any] = field(default_factory=dict)  # The rest of the stdin JSON (args, result ...).
    file_path: Path | None = None  # MOSSCODE_FILE_PATH / ${FILE_PATH}
    tool: str = ""
    agent_type: str = ""  # "" in the main conversation, the definition name, "fork" or "skill:<name>".
    nested: bool = False  # A tool event of a sub-agent started by an agent action.


@dataclass(frozen=True)
class HookOutcome:
    rejection: tuple[str, str] | None = None  # (hook id, message for the model)
    notes: tuple[tuple[str, str], ...] = ()  # (hook id, injected text)
    ran: tuple[str, ...] = ()  # Synchronous hooks that ran, in order.


NOTHING = HookOutcome()


@dataclass(frozen=True)
class HookRun:
    time: str
    hook: str
    source: str
    event: str
    agent: str
    action: str
    outcome: str
    duration_ms: int
    exit_code: int | None = None
    status: int | None = None
    detail: str = ""

    def record(self) -> dict:
        entry = {key: value for key, value in self.__dict__.items() if value is not None}
        return entry


def _canonical(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def tool_event(
    name: str,
    agent: str,
    tool: str,
    args: Mapping[str, Any],
    operation,
    root: Path,
    result=None,
    *,
    agent_type: str = "",
) -> HookEvent:
    """Fields of the tool events: path and command are what the permission rules see."""
    args = args if isinstance(args, Mapping) else {}
    path = command = ""
    file_path = None
    if operation is not None and tool in FILE_TOOLS:
        path = operation.target
        file_path = Path(path) if operation.outside else root / path
    elif operation is not None and tool == "execute_command":
        command = operation.target
    fields = {
        "tool": tool,
        "path": path,
        "command": command,
        **{f"args.{key}": _canonical(value) for key, value in args.items()},
    }
    payload = {"tool": tool, "args": dict(args), "path": path, "command": command}
    if name == "permission_request" and operation is not None:
        fields["risk"] = operation.risk
    if result is not None:
        fields["error"] = "true" if result.is_error else "false"
        fields["code"] = result.code or ""
        content = result.output.encode("utf-8")[:RESULT_LIMIT].decode("utf-8", errors="ignore")
        payload["result"] = {"content": content, "is_error": result.is_error, "code": result.code}
    return HookEvent(name, agent, fields, payload, file_path, tool, agent_type)


class HookEngine:
    def __init__(
        self,
        root: Path,
        *,
        home: Path | None = None,
        shell=None,
        enabled: bool = True,
        trust_project: bool = False,
        config: HookConfig | None = None,
        plugins=None,
    ):
        self.root = Path(root).resolve()
        self.home = home
        self.shell = shell
        self.enabled = enabled
        self.trust_project = trust_project
        self.store = TrustStore(self.root, home)
        self.plugins = plugins  # None reads the installed ones on every (re)load.
        self.config = config if config is not None else self._load()
        self.declined: set[Path] = set()  # Files the user chose not to enable this run.
        self.recent: deque[HookRun] = deque(maxlen=RECENT)
        self.session = None
        self.session_id = ""
        self.on_notice: Callable[[str], None] | None = None
        self.notices: list[str] = []  # When nobody listens yet.
        # Runs an agent action; set by the Conversation.
        self.agent_runner: Callable[[Hook, HookEvent, Mapping[str, str]], Awaitable[ActionResult]] | None = None
        self._once: set[str] = set()
        self._noticed: set[str] = set()  # Per turn.
        self._agent_noticed: set[str] = set()  # Per session.
        self._background: set[asyncio.Task] = set()

    # Configuration ---------------------------------------------------------------------------

    @property
    def hooks(self) -> tuple[Hook, ...]:
        return self.config.hooks if self.enabled else ()

    @property
    def problems(self) -> tuple[str, ...]:
        return self.config.problems

    @property
    def untrusted(self) -> tuple[HookFile, ...]:
        return self.config.untrusted

    def pending_trust(self) -> tuple[HookFile, ...]:
        """Untrusted files the user has not declined during this run."""
        return tuple(file for file in self.untrusted if file.path not in self.declined)

    def wants(self, event: str) -> bool:
        return any(hook.event == event for hook in self.hooks)

    def trust(self, file: HookFile) -> None:
        if file.digest is not None:
            self.store.trust(file.path.name, file.digest)
        self.declined.discard(file.path)
        self.config = replace(
            self.config, files=tuple(replace(f, trusted=True) if f.path == file.path else f for f in self.config.files)
        )

    def decline(self, file: HookFile) -> None:
        self.declined.add(file.path)

    def _load(self) -> HookConfig:
        if not self.enabled:
            return HookConfig()
        return load_hooks(
            self.root, self.home, store=self.store, trust_project=self.trust_project, plugins=self.plugins
        )

    def reload(self) -> tuple[list[str], list[str]]:
        """Reread the files; once state is kept by id. Returns (added, removed) ids."""
        before = {hook.id for hook in self.hooks}
        self.declined.clear()
        self.config = self._load()
        after = {hook.id for hook in self.hooks}
        return sorted(after - before), sorted(before - after)

    def find(self, hook_id: str) -> Hook | None:
        return next((hook for file in self.config.files for hook in file.hooks if hook.id == hook_id), None)

    def status(self, hook: Hook) -> str:
        file = next((f for f in self.config.files if f.path == hook.path), None)
        if file is not None and not file.trusted:
            return "未确认"
        if hook.once and hook.id in self._once:
            return "once 已执行"
        return "已启用"

    # Sessions and turns -----------------------------------------------------------------------

    def use_session(self, session) -> None:
        self.session = session
        self.session_id = getattr(session, "id", "") or ""

    def new_session(self, session=None) -> None:
        """Once resets with every new session (/clear, resume)."""
        self._once.clear()
        self._agent_noticed.clear()
        if session is not None:
            self.use_session(session)

    def begin_turn(self) -> None:
        self._noticed.clear()

    @property
    def log_path(self) -> Path | None:
        return self.session.directory / "hooks.log" if self.session is not None else None

    # Running ---------------------------------------------------------------------------------

    async def fire(self, event: HookEvent) -> HookOutcome:
        """Run the matching hooks in order. Never raises, except CancelledError."""
        if not self.enabled:
            return NOTHING
        # subagent_start/stop carry the sub-agent's own agent_type in their fields.
        fields = {"agent_type": event.agent_type, **event.fields, "event": event.name, "agent": event.agent}
        notes, ran = [], []
        for hook in self.hooks:
            if hook.event != event.name or (hook.condition is not None and not hook.condition.matches(fields)):
                continue
            if hook.once:
                if hook.id in self._once:
                    continue
                self._once.add(hook.id)  # Consumed when it starts, also if it fails.
            if hook.background:
                self.spawn(self._run(hook, event, fields))
                continue
            if hook.action is None:  # Static rejection without an action.
                self._log(hook, event, "reject", ActionResult("rejected", short(hook.reject)), 0)
                return HookOutcome(
                    (hook.id, f"Hook {hook.id} 拒绝了此调用：{hook.reject}"), tuple(notes), (*ran, hook.id)
                )
            blocking = hook.reject is None and hook.on_error == "block"
            result = await self._run(hook, event, fields, quiet=blocking)
            ran.append(hook.id)
            if hook.reject is not None:
                return HookOutcome((hook.id, f"Hook {hook.id} 拒绝了此调用：{hook.reject}"), tuple(notes), tuple(ran))
            if result.outcome == "rejected":
                return HookOutcome((hook.id, f"Hook {hook.id} 拒绝了此调用：{result.reason}"), tuple(notes), tuple(ran))
            if result.outcome in {"failed", "timeout"} and blocking:
                return HookOutcome(
                    (hook.id, f"Hook {hook.id} 执行失败，按配置阻止此调用：{result.detail}"), tuple(notes), tuple(ran)
                )
            if result.outcome == "ok" and result.output:
                notes.append((hook.id, result.output))
        return HookOutcome(None, tuple(notes), tuple(ran))

    def spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._settled)
        return task

    def _settled(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieved; _run never raises anything else.

    async def drain(self, timeout: float) -> None:
        """Wait for background hooks, then cancel the rest (their process trees are killed)."""
        pending = set(self._background)
        if not pending:
            return
        _, late = await asyncio.wait(pending, timeout=timeout)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)

    def _context(self, hook: Hook, event: HookEvent, fields) -> dict:
        context = {
            "event": event.name,
            "hook_id": hook.id,
            "session_id": self.session_id,
            "project_root": str(self.root),
            "agent": event.agent,
        }
        context.update({key: value for key, value in fields.items() if not key.startswith("args.")})
        context.update(event.payload)
        return context

    def _env(self, hook: Hook, event: HookEvent) -> dict[str, str]:
        env = {
            "MOSSCODE_HOOK_ID": hook.id,
            "MOSSCODE_HOOK_EVENT": event.name,
            "MOSSCODE_SESSION_ID": self.session_id,
            "MOSSCODE_PROJECT_ROOT": str(self.root),
            "MOSSCODE_AGENT": event.agent,
            "MOSSCODE_AGENT_TYPE": event.fields.get("agent_type", event.agent_type),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        if hook.plugin is not None:
            env.update(hook.plugin.env(self.root))
        if EVENTS[event.name].tool:
            env["MOSSCODE_TOOL_NAME"] = event.tool
            env["MOSSCODE_FILE_PATH"] = str(event.file_path) if event.file_path is not None else ""
        return env

    async def _execute(self, hook: Hook, event: HookEvent, fields) -> ActionResult:
        action = hook.action
        if action.type == "prompt":
            return ActionResult("ok", output=render(action.message, fields).strip()[:8000])
        if action.type == "agent":
            if event.nested:  # No sub-agent starts another one.
                return ActionResult("skipped", "由 Hook 启动的子 Agent 中不再运行 agent 动作")
            if self.agent_runner is None:
                return ActionResult("skipped", "当前运行方式不支持子 Agent")
            return await self.agent_runner(hook, event, fields)
        context = self._context(hook, event, fields)
        if action.type == "http":
            return await run_http(action, fields=fields, context=context)
        values = {
            "FILE_PATH": str(event.file_path) if event.file_path is not None else "",
            "PROJECT_ROOT": str(self.root),
            "PYTHON": sys.executable,
            "TOOL_NAME": event.tool,
            "EVENT": event.name,
        }
        return await run_command(
            action,
            root=self.root,
            shell=self.shell,
            env=self._env(hook, event),
            values=values,
            stdin=stdin_json(context),
            rejecting=event.name == "pre_tool_use",
        )

    async def _run(self, hook: Hook, event: HookEvent, fields, *, quiet: bool = False) -> ActionResult:
        started = time.monotonic()
        try:
            result = await self._execute(hook, event, fields)
        except asyncio.CancelledError:
            self._log(hook, event, hook.action.type, ActionResult("cancelled", "已取消"), started)
            raise
        except Exception as exc:  # A hook must never break the main flow.
            result = ActionResult("failed", f"内部错误：{type(exc).__name__}")
        self._log(hook, event, hook.action.type, result, started)
        if result.outcome in {"failed", "timeout"} and not quiet:
            self._notify(hook, f"Hook {hook.id}（{event.name}）失败：{result.detail.rstrip('。')}；已忽略。")
        elif result.outcome == "skipped" and hook.id not in self._agent_noticed:
            self._agent_noticed.add(hook.id)
            self._emit(f"Hook {hook.id}（{event.name}）的子 Agent 动作已跳过：{result.detail}。")
        return result

    def _notify(self, hook: Hook, text: str) -> None:
        if hook.id in self._noticed:
            return
        self._noticed.add(hook.id)
        self._emit(text)

    def _emit(self, text: str) -> None:
        if self.on_notice is not None:
            try:
                self.on_notice(text)
                return
            except Exception:
                pass
        self.notices.append(text)

    def take_notices(self) -> list[str]:
        notices, self.notices = self.notices, []
        return notices

    def _log(self, hook: Hook, event: HookEvent, action: str, result: ActionResult, started: float) -> None:
        duration = int((time.monotonic() - started) * 1000) if started else 0
        detail = result.detail
        if hook.action is not None and hook.action.type == "http":
            detail = detail.replace(hook.action.url, public_url(hook.action.url))
        run = HookRun(
            datetime.now().astimezone().isoformat(timespec="milliseconds"),
            hook.id,
            hook.source,
            event.name,
            event.agent,
            action,
            result.outcome,
            duration,
            result.exit_code,
            result.status,
            short(detail, 500),
        )
        self.recent.append(run)
        path = self.log_path
        if path is None:
            return
        try:
            self.session.ensure()
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(run.record(), ensure_ascii=False) + "\n")
        except OSError:
            pass  # The log is a convenience; a full disk must not break a turn.
