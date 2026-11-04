"""Bounded asynchronous subprocess I/O, including process-tree cleanup."""

import asyncio
import os
import signal
import sys
from pathlib import Path

from pydantic import Field

from ..credentials import child_environment
from .base import Params, Tool, ToolFailure
from .shell import Shell, resolve_shell

OUTPUT_LIMIT = 32768
# Command output keeps both ends: failure summaries are usually at the end.
COMMAND_HEAD, COMMAND_TAIL = 1024 * 1024, 1024 * 1024  # Memory guard; layer 1 stores large output.


async def terminate_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        # A Job Object owns only this invocation and its descendants. Closing it
        # terminates descendants even if the original shell has already exited.
        job = getattr(process, "_mosscode_job", None)
        if job is not None:
            job.close()
        elif process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def _decode_cut(data: bytes, *, start: bool) -> str:
    """Decode one side of a cut without a broken character at the cut edge."""
    if start:
        skip = 0
        while skip < min(3, len(data)) and 0x80 <= data[skip] < 0xC0:
            skip += 1
        return data[skip:].decode("utf-8", errors="replace")
    for trim in range(min(3, len(data)) + 1):
        try:
            return data[: len(data) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


async def _feed(stream, data: bytes) -> None:
    """Write stdin and close it; a child that exits without reading is not an error."""
    try:
        if data:
            stream.write(data)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()


async def run_process(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    limit: int = OUTPUT_LIMIT,
    tail: int = 0,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
):
    """input, when given, is written to the child's stdin, which is then closed; otherwise stdin is empty."""
    options = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
    # Windows commands wait on a one-byte gate until Job assignment completes.
    # This closes the race in which a fast shell could spawn before assignment.
    # os.read takes exactly the gate byte, so the child inherits the rest of the pipe as its stdin.
    inherit = "" if input is not None else ",stdin=subprocess.DEVNULL"
    gate = f"import os,subprocess,sys; os.read(0,1); sys.exit(subprocess.call(sys.argv[1:]{inherit}))"
    command = [sys.executable, "-c", gate, *args] if os.name == "nt" else args
    piped = os.name == "nt" or input is not None
    env = child_environment(env)  # Commands, hooks and skill tools never inherit the variables holding API keys.
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if piped else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **options,
        )
    )
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await spawn
        await terminate_tree(process)
        raise
    if os.name == "nt":
        from .windows_job import WindowsJob

        try:
            process._mosscode_job = WindowsJob(process.pid)
        except OSError:
            await terminate_tree(process)
            raise ToolFailure("process_cleanup_unavailable", "无法建立命令进程的生命周期边界，命令已终止。") from None
    feeder = None
    if piped:
        feeder = asyncio.create_task(_feed(process.stdin, (b"\n" if os.name == "nt" else b"") + (input or b"")))

    async def drain(stream):
        # Keep the first `limit` bytes and, with `tail`, a rolling window of the
        # bytes after them; head + end is the whole stream when nothing was dropped.
        head, end = bytearray(), bytearray()
        total = 0
        while chunk := await stream.read(8192):
            total += len(chunk)
            room = max(0, limit - len(head))
            head.extend(chunk[:room])
            if tail:
                end.extend(chunk[room:])
                del end[: max(0, len(end) - tail)]
        omitted = total - len(head) - len(end)
        if omitted <= 0:
            return (bytes(head) + bytes(end)).decode("utf-8", errors="replace"), False
        if not tail:
            return head.decode("utf-8", errors="replace"), True
        marker = f"\n…（中间省略 {omitted} 字节）…\n"
        return _decode_cut(bytes(head), start=False) + marker + _decode_cut(bytes(end), start=True), True

    stdout_task = asyncio.create_task(drain(process.stdout))
    stderr_task = asyncio.create_task(drain(process.stderr))
    try:
        await process.wait()
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        details = {
            "stdout": stdout[0],
            "stderr": stderr[0],
            "exit_code": process.returncode,
            "truncated": stdout[1] or stderr[1],
        }
        if check and process.returncode != 0:
            raise ToolFailure("nonzero_exit", f"命令退出码为 {process.returncode}。", **details)
        return details
    finally:
        await terminate_tree(process)
        await asyncio.gather(stdout_task, stderr_task, *(task for task in (feeder,) if task), return_exceptions=True)


class CommandParams(Params):
    command: str = Field(min_length=1)
    cwd: str | None = None
    timeout: float = Field(default=120.0, gt=0, le=600, allow_inf_nan=False)


SHELL_GUIDELINES = {
    "Git Bash": "当前 shell 是 Windows 上的 Git Bash：使用 bash 语法和正斜杠路径（如 C:/Users 或 /c/Users），空设备写 /dev/null 而不是 NUL；Windows 原生程序仍可直接调用。",
    "PowerShell": "当前 shell 是 PowerShell：使用 PowerShell 语法；Windows PowerShell 5 不支持 && 和 ||，用 ; 分隔并检查 $LASTEXITCODE。",
}


class ExecuteCommand(Tool):
    name = "execute_command"
    prompt_summary = "执行测试、构建等 shell 命令"
    params_model = CommandParams
    plan_commands = True

    def __init__(self, root: Path, shell: Shell | str | None = None):
        super().__init__(root)
        self.shell = shell if isinstance(shell, Shell) else resolve_shell(shell or "auto")
        self.description = (
            f"在 {self.shell.label} 中执行命令，返回 stdout、stderr 和退出码。cwd 可指定工作目录；"
            "timeout 以秒计，默认 120、最大 600。输出过长时保留开头和结尾。"
        )
        self.prompt_guidelines = (
            "execute_command 用于测试、构建和专用工具未覆盖的任务；已有专用工具时优先用于读取、搜索、写入和编辑。",
            "execute_command 的命令遵循当前 shell 的语法，正确引用含空格的路径。依据退出码和输出判断结果，不能仅因有输出就宣布成功。",
            *((SHELL_GUIDELINES[self.shell.label],) if self.shell.label in SHELL_GUIDELINES else ()),
            "git status/diff/log/show、ls、cat、grep 等只读命令（参数限于项目内，不含管道和重定向）无需审批，计划模式中也可使用；其他命令按权限档位审批。",
            "execute_command 超时或取消可能留下部分效果；重复有副作用的命令前先检查状态，不通过增大超时掩盖持续失败。",
        )

    async def execute(self, params: CommandParams):
        cwd = self.path(params.cwd) if params.cwd is not None else self.root
        details = await run_process(
            self.shell.args(params.command),
            cwd,
            check=False,
            limit=COMMAND_HEAD,
            tail=COMMAND_TAIL,
            env=self.shell.env(params.command),
        )
        output = details["stdout"]
        if details["stderr"]:
            output += ("\n" if output else "") + "stderr:\n" + details["stderr"]
        if details["exit_code"] != 0:
            # The model sees only output, so the failing command's streams must be in it.
            raise ToolFailure(
                "nonzero_exit", f"命令退出码为 {details['exit_code']}。" + (f"\n{output}" if output else ""), **details
            )
        return output or "命令完成，无输出。", details
