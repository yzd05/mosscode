"""Background commands: started with run_in_background or moved there with ctrl+b, they run as tasks.

They share the task list (ids, /tasks, task_output, task_stop, -p waiting) with sub-agents but have their own limit.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..agents.tasks import STATUS_LABELS, CommandInfo, Task, TaskManager
from .base import ToolFailure

DEFAULT_LIMIT, MAX_LIMIT = 8, 64
TIMEOUT_LIMIT = 3600  # The most a background command's own timeout may ask for.
HEADLESS_TIME_LIMIT = 600.0  # -p must end: a background command without its own timeout gets this much.
LOG_LIMIT = 256 * 1024 * 1024
KEEP = 64 * 1024  # The end of the output kept in memory, so notifications need not reread the file.
NOTIFY_LINES, NOTIFY_CHARS = 20, 2000
OUTPUT_LINES, OUTPUT_CHARS = 50, 8000
STOPPED = "[命令已停止]"


class CommandLog:
    """A background command's output file. MossCode writes it, so the command needs no access to the session."""

    def __init__(self, path: Path, *, limit: int | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "xb")  # noqa: SIM115 - open for the command's whole life.
        self.path = path
        self.limit = limit or LOG_LIMIT
        self.size = 0
        self.full = False
        self.on_full: Callable[[], None] | None = None
        self._tail = bytearray()

    def write(self, data: bytes) -> None:
        if self._file is None or self.full or not data:
            return
        room = self.limit - self.size
        if len(data) > room:
            data, self.full = data[:room], True
        self._append(data)
        if self.full and self.on_full is not None:
            self.on_full()

    def note(self, text: str) -> None:
        """A line of MossCode's own (moved to the background, refused domains, why it ended), past any limit."""
        if self._file is None:
            return
        start = b"" if not self._tail or self._tail.endswith(b"\n") else b"\n"
        self._append(start + f"[MossCode] {text}\n".encode())

    def _append(self, data: bytes) -> None:
        try:
            self._file.write(data)
            self._file.flush()  # Readers (read_file, the user) see output as it arrives.
        except (OSError, ValueError):
            return
        self.size += len(data)
        self._tail.extend(data)
        del self._tail[: max(0, len(self._tail) - KEEP)]

    def last_lines(self, lines: int, chars: int) -> str:
        from .process import _decode_cut

        data = bytes(self._tail)
        text = _decode_cut(data, start=True) if self.size > len(data) else data.decode("utf-8", errors="replace")
        text = "\n".join(text.rstrip("\n").splitlines()[-lines:])
        return text if len(text) <= chars else "…" + text[-(chars - 1) :]

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def size_text(size: int) -> str:
    if size < 1024:
        return f"{size} 字节"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / 1024 / 1024:.1f} MiB"


def status_text(task: Task) -> str:
    """运行中, 已停止, or the status with the exit code or why MossCode ended it."""
    if task.status == "running":
        return STATUS_LABELS["running"]
    if task.status == "cancelled":
        return "已停止"
    return f"{STATUS_LABELS[task.status]}，{task.result}"


def notification(task: Task) -> str:
    info = task.command
    lines = [
        "<task-notification>",
        f"任务：{task.id}",
        "类型：命令",
        f"标签：{task.description}",
        f"命令：{_short(info.command, 500)}",
        f"状态：{status_text(task)}",
        f"用时：{task.elapsed:.0f} 秒",
        f"输出文件：{info.log.path}（{size_text(info.log.size)}，用 read_file 读取）",
    ]
    if info.notes:
        lines.append("沙箱：" + " ".join(info.notes))
    lines += [
        f"最后 {NOTIFY_LINES} 行输出：",
        info.tail or "（无输出）",
        "</task-notification>",
        "以上是后台命令的结果；根据需要继续处理，或向用户报告。",
    ]
    return "\n".join(lines)


def report(task: Task) -> str:
    """task_output: the state, the output file and its last lines."""
    info = task.command
    head = f"任务 {task.id}（{task.label}）：{status_text(task)}，用时 {task.elapsed:.0f} 秒。"
    body = info.log.last_lines(OUTPUT_LINES, OUTPUT_CHARS)
    return (
        f"{head}\n命令：{_short(info.command, 500)}\n输出文件：{info.log.path}（{size_text(info.log.size)}）\n"
        f"最后 {OUTPUT_LINES} 行输出：\n{body or '（无输出）'}"
    )


def started(task: Task, *, moved: bool = False) -> str:
    info = task.command
    limit = f"时间限制 {info.time_limit:.0f} 秒；" if info.time_limit else ""
    if moved:
        # Without saying who moved it, models guess a reason ("a time limit") and then wait on it anyway.
        return (
            f"用户按 Ctrl+B 把命令转入了后台：任务 {task.id}，命令继续运行，输出（包括已收到的部分）写入 "
            f"{info.log.path}（用 read_file 读取）。{limit}用户不想等它结束：先继续其他工作，没有其他工作就结束本轮，"
            "不要用 sleep 或 task_output 的 wait 等它；命令结束时你会收到 <task-notification>。"
        )
    return (
        f"已在后台启动命令：任务 {task.id}，输出写入 {info.log.path}（用 read_file 读取）。{limit}"
        "命令结束时你会收到 <task-notification>；不要用 sleep 轮询。"
    )


def share(source, target) -> None:
    """A fresh registry (a sub-agent's or member's worktree) starts its background commands in the same list."""
    commands = getattr(source, "commands", None)
    target.commands = commands
    tool = target.get("execute_command")
    if commands is not None and tool is not None and hasattr(tool, "allow_background"):
        tool.allow_background(commands.time_limit)


class BackgroundCommands:
    def __init__(
        self,
        tasks: TaskManager,
        *,
        limit: int = DEFAULT_LIMIT,
        time_limit: float | None = None,
        session: Callable[[], object] = lambda: None,
        record: Callable[[dict], None] | None = None,
    ):
        self.tasks = tasks
        self.limit = limit
        self.time_limit = time_limit  # None: no limit (the interactive UI).
        self.session = session
        self.record = record
        self._foreground: set[asyncio.Event] = set()

    def running(self) -> list[Task]:
        return [task for task in self.tasks.running() if task.kind == "command"]

    def check(self) -> None:
        if len(self.running()) >= self.limit:
            raise ToolFailure(
                "too_many_tasks",
                f"后台命令已达上限（{self.limit} 个）；先用 task_output 查看、用 task_stop 停止不再需要的命令，再启动新的。",
            )

    # Starting ------------------------------------------------------------------------------

    def open(
        self,
        command: str,
        cwd: Path,
        *,
        description: str | None = None,
        owner: str = "",
        call_id: str = "",
        sandbox: str = "",
        time_limit: float | None = None,
    ) -> Task:
        """A running task with its output file; launch() hands it the process."""
        task = self.tasks.new(
            origin="command",
            agent="command",
            agent_type="命令",
            description=(description or "").strip() or _short(command, 40),
            prompt=command,
            call_id=call_id,
            background=True,
        )
        try:
            log = CommandLog(self._path(task.id))
        except OSError as exc:
            del self.tasks.tasks[task.id]
            raise ToolFailure("log_unavailable", f"无法创建输出文件：{exc}") from None
        task.command = CommandInfo(command, cwd, log, owner=owner, sandbox=sandbox, time_limit=time_limit)
        if self.record is not None:
            self.record(
                {
                    "type": "task",
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "id": task.id,
                    "event": "start",
                    "origin": "command",
                    "command": command,
                    "cwd": str(cwd),
                    "log": str(log.path),
                    "owner": owner,
                    "background": True,
                }
            )
        return task

    def launch(self, task: Task, process: asyncio.Task, finish: Callable[[dict | None], list[str]]) -> None:
        """finish(details) gives the sandbox notes; details is None when the command was ended by MossCode."""
        task.command.log.on_full = lambda: self._end(task, process, "output_limit")
        self.tasks.launch(task, lambda: self._supervise(task, process, finish))

    def _path(self, task_id: str) -> Path:
        session = self.session()
        if session is not None:
            session.ensure()
            folder = session.directory / "commands"
        else:
            folder = Path(tempfile.gettempdir()) / "mosscode-commands"
        path, n = folder / f"{task_id}.log", 2
        while path.exists():  # A resumed session numbers its tasks from t1 again.
            path, n = folder / f"{task_id}-{n}.log", n + 1
        return path

    # Running -------------------------------------------------------------------------------

    def _end(self, task: Task, process: asyncio.Task, reason: str) -> None:
        if not task.command.end_reason:
            task.command.end_reason = reason
        process.cancel()

    async def _supervise(self, task: Task, process: asyncio.Task, finish) -> str:
        info = task.command
        try:
            done, _ = await asyncio.wait({process}, timeout=info.time_limit)
            if not done:
                self._end(task, process, "time_limit")
            await asyncio.gather(process, return_exceptions=True)
        except asyncio.CancelledError:  # task_stop, /tasks stop, or the session ending.
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)
            info.log.note("命令已被停止。")
            info.log.close()
            task.result = STOPPED
            raise
        return self._conclude(task, process, finish)

    def _conclude(self, task: Task, process: asyncio.Task, finish) -> str:
        info, log = task.command, task.command.log
        details, error = None, ""
        if not process.cancelled():
            exc = process.exception()
            if exc is None:
                details = process.result()
                info.exit_code = details.get("exit_code")
            elif isinstance(exc, ToolFailure):
                error = str(exc)
            else:
                error = f"命令没能运行：{type(exc).__name__}: {exc}"
        info.tail = log.last_lines(NOTIFY_LINES, NOTIFY_CHARS)
        if details is not None:
            # Like a foreground command's stderr: where sandbox start failures and denials show.
            details = {**details, "stdout": "", "stderr": log.last_lines(200, 20000)}
        try:
            info.notes = finish(details)
        except Exception:  # Notes are a courtesy; the result stands without them.
            info.notes = []
        if info.end_reason == "time_limit":
            reason = f"超过时间限制（{info.time_limit:.0f} 秒），命令已被结束"
        elif info.end_reason == "output_limit":
            reason = f"输出超过 {log.limit // (1024 * 1024)} MiB，命令已被结束"
        elif error:
            reason = error
        else:
            reason = f"退出码 {info.exit_code}"
        for note in info.notes:
            log.note(note)
        log.note(f"命令结束：{reason}。")
        log.close()
        if info.end_reason or error or info.exit_code != 0:
            task.status = "failed"
        return reason

    async def stop_owned(self, owner: str) -> None:
        """A sub-agent or member ended: its commands end with it."""
        jobs = [self.tasks.stop(task.id) for task in self.running() if task.command.owner == owner]
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    # ctrl+b ------------------------------------------------------------------------------

    def foreground(self) -> asyncio.Event:
        """A foreground command of the main conversation waits on this as well as on its process."""
        event = asyncio.Event()
        self._foreground.add(event)
        return event

    def release(self, event: asyncio.Event) -> None:
        self._foreground.discard(event)

    def foreground_count(self) -> int:
        return sum(not event.is_set() for event in self._foreground)

    def detach_foreground(self) -> int:
        count = 0
        for event in self._foreground:
            if not event.is_set():
                event.set()
                count += 1
        return count


def _short(command: str, limit: int) -> str:
    line = " ".join(command.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
