"""Bounded asynchronous subprocess I/O, including process-tree cleanup."""

import asyncio
import os
import signal
import sys
from pathlib import Path

from pydantic import Field, model_validator

from ..command_env import command_environment
from ..credentials import child_environment
from . import output_text
from .background import TIMEOUT_LIMIT
from .base import Params, Tool, ToolFailure
from .output_text import OutputDecoder, decode_output
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


class Window:
    """The first `limit` bytes and, with `tail`, a rolling window of the bytes after them."""

    def __init__(self, limit: int, tail: int):
        self.limit, self.tail = limit, tail
        self.head, self.end = bytearray(), bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = max(0, self.limit - len(self.head))
        self.head.extend(chunk[:room])
        if self.tail:
            self.end.extend(chunk[room:])
            del self.end[: max(0, len(self.end) - self.tail)]

    @property
    def omitted(self) -> int:
        return self.total - len(self.head) - len(self.end)

    def marker(self) -> str:
        return f"\n…（中间省略 {self.omitted} 字节）…\n"

    def text(self, rest: OutputDecoder | None = None) -> tuple[str, bool]:
        """The kept text and whether anything was dropped; head + end is the whole stream when nothing was.

        rest, when given, decodes the last part and keeps a character the stream has not finished for what follows.
        """
        final = rest is None
        rest = rest or OutputDecoder()
        if self.omitted <= 0:
            return rest.decode(bytes(self.head) + bytes(self.end), final=final), False
        head = decode_output(bytes(self.head), cut_end=True)
        if not self.tail:
            return head, True
        return head + self.marker() + rest.decode(bytes(self.end), cut=True, final=final), True

    def raw(self) -> bytes:
        if self.omitted <= 0 or not self.tail:
            return bytes(self.head) + bytes(self.end)
        return bytes(self.head) + self.marker().encode() + bytes(self.end)


class Output:
    """Where a command's streams go: a window each, or once `log` is set, one file in arrival order."""

    def __init__(self, limit: int = OUTPUT_LIMIT, tail: int = 0, *, log=None):
        self.windows = {"stdout": Window(limit, tail), "stderr": Window(limit, tail)}
        self.log = log
        # Where commands may write in a code page (Windows), the log is written as UTF-8: the model reads it with
        # read_file, which reads only UTF-8, and MossCode's own lines in it are UTF-8 anyway. Converting as output
        # arrives keeps the file readable while the command runs; each stream has its own decoder so a character
        # split between chunks is not mixed with the other stream. Elsewhere the bytes go in as they were written.
        self.decoders = {name: OutputDecoder() for name in self.windows} if output_text.console_encoding() else None

    def write(self, stream: str, chunk: bytes) -> None:
        if self.log is None:
            self.windows[stream].add(chunk)
        elif self.decoders is None:
            self.log.write(chunk)
        else:
            self.log.write(self.decoders[stream].decode(chunk).encode())

    def end(self, stream: str) -> None:
        """The stream closed: whatever its decoder still held goes to the log."""
        if self.log is not None and self.decoders is not None:
            self.log.write(self.decoders[stream].decode(b"", final=True).encode())

    def move_to(self, log) -> None:
        """ctrl+b: what was kept goes to the file first (stdout, then stderr), later output after it."""
        if self.decoders is None:
            stdout, stderr = self.windows["stdout"].raw(), self.windows["stderr"].raw()
        else:
            stdout, stderr = (self.windows[name].text(self.decoders[name])[0].encode() for name in self.windows)
        log.write(stdout)
        if stderr:
            log.write((b"" if not stdout or stdout.endswith(b"\n") else b"\n") + b"stderr:\n" + stderr)
        log.note("命令已转入后台，之后的输出接在这里。")
        self.log = log


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
    output: Output | None = None,
):
    """input, when given, is written to the child's stdin, which is then closed; otherwise stdin is empty.

    confined is for what the model runs (commands, Skill tools): secrets are removed from its environment, it gets
    a private temp directory, and on Windows its Job may not read the clipboard or touch the desktop.

    sandbox_root runs it in the sandbox with that root's policy; details then carry what the sandbox did. network is
    the URL of the proxy listening for this command, its only way out; without it a sandboxed command is offline.

    output, when given, receives the streams instead of windows of limit and tail.
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
            output=output,
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


async def _run(args, cwd, *, check, limit, tail, env, input, ui_limits, gate_python=None, output=None):
    output = output if output is not None else Output(limit, tail)
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

    async def drain(name, stream):
        while chunk := await stream.read(8192):
            output.write(name, chunk)

    stdout_task = asyncio.create_task(drain("stdout", process.stdout))
    stderr_task = asyncio.create_task(drain("stderr", process.stderr))
    try:
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        stdout, stderr = output.windows["stdout"].text(), output.windows["stderr"].text()
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
        # Also when the command was stopped: an unfinished last line still reaches the log, before MossCode's notes.
        output.end("stdout")
        output.end("stderr")


DEFAULT_TIMEOUT, FOREGROUND_TIMEOUT = 120.0, 600


class CommandParams(Params):
    command: str = Field(min_length=1)
    cwd: str | None = None
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=FOREGROUND_TIMEOUT, allow_inf_nan=False)


class SandboxCommandParams(CommandParams):
    outside_sandbox: bool = False
    justification: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason(self):
        if self.outside_sandbox and not (self.justification or "").strip():
            raise ValueError("outside_sandbox 为 true 时必须在 justification 中说明理由")
        return self


class BackgroundCommandParams(CommandParams):
    # In the background timeout is the command's time limit; None there means the session's default.
    timeout: float | None = Field(default=None, gt=0, le=TIMEOUT_LIMIT, allow_inf_nan=False)
    run_in_background: bool = False
    description: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _foreground_timeout(self):
        if not self.run_in_background:
            if self.timeout is None:
                self.timeout = DEFAULT_TIMEOUT
            elif self.timeout > FOREGROUND_TIMEOUT:
                raise ValueError(
                    f"前台命令的 timeout 最大 {FOREGROUND_TIMEOUT} 秒；运行更久的命令用 run_in_background: true"
                )
        return self


class SandboxBackgroundCommandParams(BackgroundCommandParams, SandboxCommandParams):
    pass


BACKGROUND_GUIDELINE = (
    "开发服务器、监视进程（vite、tsc --watch 等）和预计超过几分钟的命令用 run_in_background: true 启动；"
    "不要用 &、nohup、setsid、Start-Process 把进程放到后台，命令结束时它们会被一起结束。"
    "后台命令的输出用 read_file 读取输出文件，运行中也可以读；它结束时你会收到 <task-notification>，不要用 sleep 轮询。"
)
NETWORK_NAMES = {"proxy": "经代理", "none": "断开", "open": "不受限制"}

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
        from ..sandbox import current, local_ports, reads_outside

        self.shell = shell if isinstance(shell, Shell) else resolve_shell(shell or "auto")  # Hooks use it too.
        sandbox = current()
        self.sandboxed = sandbox.enabled
        self.command_shell = self.shell
        status = sandbox.status() if self.sandboxed else None
        windows_backend = sys.platform == "win32" and status is not None and status.backend in ("mxc", "token")
        if windows_backend and self.shell.kind != "powershell":
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
            backend = status.backend if status else ""
            self.prompt_guidelines += (
                f"execute_command 的命令在沙箱中运行，无需审批：只能写项目目录和临时目录；{network}"
                f"{reads_outside(backend)}；{local_ports(backend)}；.mosscode、.git/hooks、.git/config 只读。"
                f"被沙箱拦截且确实需要越界时（如{escape}），"
                "以 outside_sandbox: true 重新调用并在 justification 中写明理由，用户批准后在沙箱外运行；能在边界内完成的不要申请。",
            )
        self.background_enabled = False
        self._description, self._guidelines = self.description, self.prompt_guidelines

    def allow_background(self, time_limit: float | None = None) -> None:
        """run_in_background and ctrl+b, when the session keeps background commands (registry.commands)."""
        self.background_enabled = True
        self.params_model = SandboxBackgroundCommandParams if self.sandboxed else BackgroundCommandParams
        default = f"省略时为 {time_limit:.0f} 秒" if time_limit else "省略时不限时"
        self.description = self._description + (
            "run_in_background 为 true 时命令在后台运行：立即返回任务 id 和输出文件路径，命令结束时你会收到 "
            f"<task-notification>；description 是 3–10 个字的任务标签；timeout 此时是时间限制，最大 {TIMEOUT_LIMIT}，"
            f"{default}。"
        )
        self.prompt_guidelines = (*self._guidelines, BACKGROUND_GUIDELINE)

    async def execute(self, params: CommandParams):
        from ..sandbox import UNAVAILABLE_HINT, current
        from .background import started
        from .base import CURRENT_CALL, CURRENT_REGISTRY

        cwd = self.path(params.cwd) if params.cwd is not None else self.root
        sandbox = current()
        inside = sandbox.enabled and not getattr(params, "outside_sandbox", False)
        if inside and not sandbox.active:
            raise ToolFailure("sandbox_unavailable", sandbox.status().reason + UNAVAILABLE_HINT)
        registry, call = CURRENT_REGISTRY.get(), CURRENT_CALL.get()
        commands = getattr(registry, "commands", None) if self.background_enabled else None
        background = getattr(params, "run_in_background", False)
        if background:
            if commands is None:
                raise ToolFailure("background_unavailable", "这里不能在后台运行命令。")
            commands.check()
        state = {"background": background}  # Read by the proxy: background commands ask nobody.
        proxy = self._proxy(params.command, state) if inside and sandbox.network == "proxy" else None
        owner = getattr(registry, "task_id", "")

        def open_task(time_limit: float | None):
            return commands.open(
                params.command,
                cwd,
                description=getattr(params, "description", None),
                owner=owner,
                call_id=call.id if call is not None else "",
                sandbox=f"{sandbox.status().backend}，网络{NETWORK_NAMES.get(sandbox.network, '')}" if inside else "",
                time_limit=time_limit,
            )

        def finish(details: dict | None) -> list[str]:
            failure, notes, hint = self._sandbox_report(params.command, inside, proxy, details)
            return [failure] if failure else [*notes, *([hint.strip()] if hint else [])]

        output = Output(COMMAND_HEAD, COMMAND_TAIL)
        if background:
            task = open_task(params.timeout if params.timeout is not None else commands.time_limit)
            output.log = task.command.log
            commands.launch(task, asyncio.create_task(self._spawn(params.command, cwd, inside, proxy, output)), finish)
            return started(task), {"task": task.id, "background": True, "log": str(task.command.log.path)}
        process = asyncio.create_task(self._spawn(params.command, cwd, inside, proxy, output))
        # Only the main conversation's commands move with ctrl+b; a sub-agent's end with it anyway.
        detach = commands.foreground() if commands is not None and not owner else None
        try:
            details = await self._wait(process, detach)
        finally:
            if detach is not None:
                commands.release(detach)
        if details is None:  # ctrl+b
            state["background"] = True
            try:
                task = open_task(commands.time_limit)
            except ToolFailure:
                process.cancel()
                await asyncio.gather(process, return_exceptions=True)
                raise
            output.move_to(task.command.log)
            commands.launch(task, process, finish)
            return started(task, moved=True), {
                "task": task.id,
                "background": True,
                "moved": True,
                "log": str(task.command.log.path),
            }
        failure, notes, hint = self._sandbox_report(params.command, inside, proxy, details)
        if failure:
            raise ToolFailure("sandbox_failed", failure, **details)
        text = details["stdout"]
        if details["stderr"]:
            text += ("\n" if text else "") + "stderr:\n" + details["stderr"]
        if notes:
            text += ("\n" if text else "") + "沙箱：" + " ".join(notes)
        if details["exit_code"] != 0:
            # The model sees only output, so the failing command's streams must be in it.
            raise ToolFailure(
                "nonzero_exit",
                f"命令退出码为 {details['exit_code']}。" + (f"\n{text}" if text else "") + hint,
                **details,
            )
        return (text or "命令完成，无输出。") + hint, details

    @staticmethod
    async def _wait(process: asyncio.Task, detach: asyncio.Event | None) -> dict | None:
        """The process's details, or None when ctrl+b moved it to the background (it keeps running)."""
        if detach is None:
            return await process
        waiter = asyncio.ensure_future(detach.wait())
        try:
            await asyncio.wait({process, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)
            raise
        finally:
            waiter.cancel()
        return process.result() if process.done() else None

    async def _spawn(self, command: str, cwd: Path, inside: bool, proxy, output: Output) -> dict:
        from ..sandbox import current

        shell = self.command_shell
        script = command
        if proxy is not None and shell.kind == "powershell":
            # Windows PowerShell 5.1 ignores HTTPS_PROXY and would try the system proxy, which the sandbox blocks.
            script = POWERSHELL_PROXY + script
        try:
            return await run_process(
                shell.args(script),
                cwd,
                check=False,
                limit=COMMAND_HEAD,
                tail=COMMAND_TAIL,
                env=shell.env(command),
                confined=True,
                sandbox_root=self.root if inside else None,
                network=await proxy.start(current().proxy_socket()) if proxy is not None else None,
                output=output,
            )
        finally:
            if proxy is not None:
                await proxy.close()

    def _sandbox_report(self, command: str, inside: bool, proxy, details: dict | None) -> tuple[str, list[str], str]:
        """(start failure, notes, hint) once a command ended, each recorded for /sandbox; details is None when
        MossCode ended it."""
        from ..sandbox import current, explain, startup_failure

        sandbox = current()
        if inside and details is not None and (failure := startup_failure(details)):
            sandbox.record(failure)
            return failure, [], ""
        notes = list((details or {}).get("sandbox", {}).get("notes", []))
        if proxy is not None:
            notes += proxy.summary()
        for note in notes:
            sandbox.record(note)
        hint = explain(details) if inside and details is not None else ""
        if hint:
            sandbox.record(f"可能被拦截：{safe_command(command)}")
        return "", notes, hint

    def _proxy(self, command: str, state: dict):
        """The listener a sandboxed command reaches the network through; new domains go to the user."""
        from ..sandbox.proxy import NetworkProxy
        from .base import CURRENT_CALL, CURRENT_REGISTRY
        from .web import environment_proxy, system_resolver

        registry = CURRENT_REGISTRY.get()
        call = CURRENT_CALL.get()

        async def approve(host: str, port: int) -> str:
            if registry is None:
                return "无法请求审批"
            decision = await registry.permissions.connect(
                call.id if call else "", host, port, command, background=state["background"]
            )
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
