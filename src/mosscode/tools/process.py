"""Bounded asynchronous subprocess I/O, including process-tree cleanup."""

import asyncio
import os
import signal
import sys
from pathlib import Path

from pydantic import Field, model_validator

from ..command_env import command_environment
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
    confined: bool = False,
    sandbox_root: Path | None = None,
    network: str | None = None,
):
    """input, when given, is written to the child's stdin, which is then closed; otherwise stdin is empty.

    confined is for what the model runs (commands, Skill tools): secrets are removed from its environment, it gets
    a private temp directory, and on Windows its Job may not read the clipboard or touch the desktop.

    sandbox_root runs it in the sandbox with that root's policy; details then carry what the sandbox did. network is
    the URL of the proxy listening for this command, its only way out; without it a sandboxed command is offline.
    """
    # Commands, hooks and skill tools never inherit the variables holding API keys.
    env = command_environment(env) if confined else child_environment(env)
    prepared = None
    gate_python = sys.executable
    if sandbox_root is not None:
        from ..sandbox import UNAVAILABLE_HINT, SandboxFailure, current, safe_python

        try:
            prepared = current().prepare(args, cwd, env, sandbox_root, network)
        except SandboxFailure as exc:
            raise ToolFailure("sandbox_failed", f"{exc}{UNAVAILABLE_HINT}") from None
        args, env = prepared.argv, prepared.env
        # The gate runs unsandboxed; a project's .venv python is something the last command could have replaced.
        gate_python = safe_python(prepared.policy.writable) or ""
        if not gate_python:
            raise ToolFailure("sandbox_failed", "找不到可写目录之外的 Python 解释器，无法启动沙箱。" + UNAVAILABLE_HINT)
    try:
        # MXC cannot start its container inside a Job with UI limits; the container has its own.
        ui_limits = confined and (prepared is None or prepared.ui_limits)
        details = await _run(
            args,
            cwd,
            check=check,
            limit=limit,
            tail=tail,
            env=env,
            input=input,
            ui_limits=ui_limits,
            gate_python=gate_python,
        )
    finally:
        notes = prepared.finish() if prepared is not None else []
    if prepared is not None:
        details["sandbox"] = {
            "writable": [str(p) for p in prepared.policy.writable],
            "notes": notes,
            "network": "open" if prepared.policy.network == "open" else "proxy" if network else "none",
            "backend": current().status().backend,
        }
    return details


async def _run(args, cwd, *, check, limit, tail, env, input, ui_limits, gate_python=None):
    options = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
    # Windows commands wait on a one-byte gate until Job assignment completes.
    # This closes the race in which a fast shell could spawn before assignment.
    # os.read takes exactly the gate byte, so the child inherits the rest of the pipe as its stdin.
    inherit = "" if input is not None else ",stdin=subprocess.DEVNULL"
    gate = f"import os,subprocess,sys; os.read(0,1); sys.exit(subprocess.call(sys.argv[1:]{inherit}))"
    command = [gate_python or sys.executable, "-I", "-S", "-c", gate, *args] if os.name == "nt" else args
    piped = os.name == "nt" or input is not None
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
            process._mosscode_job = WindowsJob(process.pid, ui_limits=ui_limits)
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


class SandboxCommandParams(CommandParams):
    outside_sandbox: bool = False
    justification: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason(self):
        if self.outside_sandbox and not (self.justification or "").strip():
            raise ValueError("outside_sandbox 为 true 时必须在 justification 中说明理由")
        return self


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
        from ..sandbox import current, reads_outside

        self.shell = shell if isinstance(shell, Shell) else resolve_shell(shell or "auto")  # Hooks use it too.
        sandbox = current()
        self.sandboxed = sandbox.enabled
        self.command_shell = self.shell
        status = sandbox.status() if self.sandboxed else None
        if status is not None and status.backend in ("mxc", "token") and self.shell.kind != "powershell":
            # MSYS2 programs, Git Bash included, cannot start inside an MXC container or under a restricted token.
            self.command_shell = resolve_shell("powershell")
        if self.sandboxed:
            self.params_model = SandboxCommandParams
        self.description = (
            f"在 {self.command_shell.label} 中执行命令，返回 stdout、stderr 和退出码。cwd 可指定工作目录；"
            "timeout 以秒计，默认 120、最大 600。输出过长时保留开头和结尾。"
        )
        self.prompt_guidelines = (
            "execute_command 用于测试、构建和专用工具未覆盖的任务；已有专用工具时优先用于读取、搜索、写入和编辑。",
            "execute_command 的命令遵循当前 shell 的语法，正确引用含空格的路径。依据退出码和输出判断结果，不能仅因有输出就宣布成功。",
            *((SHELL_GUIDELINES[label],) if (label := self.command_shell.label) in SHELL_GUIDELINES else ()),
            "git status/diff/log/show、ls、cat、grep 等只读命令（参数限于项目内，不含管道和重定向）无需审批，计划模式中也可使用；其他命令按权限档位审批。",
            "execute_command 超时或取消可能留下部分效果；重复有副作用的命令前先检查状态，不通过增大超时掩盖持续失败。",
        )
        if status is not None and status.state == "partial":
            self.prompt_guidelines += (
                "execute_command 的命令在沙箱中运行，但沙箱只限制写入：只能写项目目录和临时目录，"
                ".mosscode、.git/hooks、.git/config 只读；读取和网络不受沙箱限制，命令仍按权限档位审批。"
                "被沙箱拦截且确实需要写项目外的文件时，以 outside_sandbox: true 重新调用并在 justification 中写明理由，"
                "用户批准后在沙箱外运行；能在边界内完成的不要申请。",
            )
        elif self.sandboxed:
            if sandbox.network == "proxy":
                network = (
                    "网络只经 MossCode 的代理（已写入 HTTP_PROXY/HTTPS_PROXY）连接域名，新域名由用户确认，"
                    "不能直连 IP、本机或内网，git 的 SSH 远程不可用；"
                )
                escape = "写项目外的文件、连接被拒绝的域名"
            else:
                network = "网络断开；"
                escape = "联网安装依赖、写项目外的文件"
            self.prompt_guidelines += (
                f"execute_command 的命令在沙箱中运行，无需审批：只能写项目目录和临时目录；{network}"
                f"{reads_outside(status.backend if status else '')}；.mosscode、.git/hooks、.git/config 只读。"
                f"被沙箱拦截且确实需要越界时（如{escape}），"
                "以 outside_sandbox: true 重新调用并在 justification 中写明理由，用户批准后在沙箱外运行；能在边界内完成的不要申请。",
            )

    async def execute(self, params: CommandParams):
        from ..sandbox import UNAVAILABLE_HINT, current, explain, startup_failure

        cwd = self.path(params.cwd) if params.cwd is not None else self.root
        sandbox = current()
        inside = sandbox.enabled and not getattr(params, "outside_sandbox", False)
        if inside and not sandbox.active:
            raise ToolFailure("sandbox_unavailable", sandbox.status().reason + UNAVAILABLE_HINT)
        shell = self.command_shell
        proxy = self._proxy(params.command) if inside and sandbox.network == "proxy" else None
        socket = sandbox.proxy_socket() if proxy is not None else None
        script = params.command
        if proxy is not None and shell.kind == "powershell":
            # Windows PowerShell 5.1 ignores HTTPS_PROXY and would try the system proxy, which the sandbox blocks.
            script = POWERSHELL_PROXY + script
        try:
            details = await run_process(
                shell.args(script),
                cwd,
                check=False,
                limit=COMMAND_HEAD,
                tail=COMMAND_TAIL,
                env=shell.env(params.command),
                confined=True,
                sandbox_root=self.root if inside else None,
                network=await proxy.start(socket) if proxy is not None else None,
            )
        finally:
            if proxy is not None:
                await proxy.close()
        if inside and (failure := startup_failure(details)):
            sandbox.record(failure)
            raise ToolFailure("sandbox_failed", failure, **details)
        notes = details.get("sandbox", {}).get("notes", [])
        if proxy is not None:
            notes = [*notes, *proxy.summary()]
        for note in notes:
            sandbox.record(note)
        output = details["stdout"]
        if details["stderr"]:
            output += ("\n" if output else "") + "stderr:\n" + details["stderr"]
        if notes:
            output += ("\n" if output else "") + "沙箱：" + " ".join(notes)
        hint = explain(details) if inside else ""
        if hint:
            sandbox.record(f"可能被拦截：{safe_command(params.command)}")
        if details["exit_code"] != 0:
            # The model sees only output, so the failing command's streams must be in it.
            raise ToolFailure(
                "nonzero_exit",
                f"命令退出码为 {details['exit_code']}。" + (f"\n{output}" if output else "") + hint,
                **details,
            )
        return (output or "命令完成，无输出。") + hint, details

    def _proxy(self, command: str):
        """The listener a sandboxed command reaches the network through; new domains go to the user."""
        from ..sandbox.proxy import NetworkProxy
        from .base import CURRENT_CALL, CURRENT_REGISTRY
        from .web import environment_proxy, system_resolver

        registry = CURRENT_REGISTRY.get()
        call = CURRENT_CALL.get()

        async def approve(host: str, port: int) -> str:
            if registry is None:
                return "无法请求审批"
            decision = await registry.permissions.connect(call.id if call else "", host, port, command)
            return "" if decision.action == "allow" else decision.reason or "未被允许"

        return NetworkProxy(approve, resolver=system_resolver, upstream=environment_proxy)


# Set before the command so Invoke-WebRequest and WebClient in Windows PowerShell 5.1 use the sandbox proxy.
POWERSHELL_PROXY = (
    "if ($env:HTTPS_PROXY) { [System.Net.WebRequest]::DefaultWebProxy = "
    "New-Object System.Net.WebProxy($env:HTTPS_PROXY) }\n"
)


def safe_command(command: str, limit: int = 120) -> str:
    line = " ".join(command.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
