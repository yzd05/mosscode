"""The command sandbox: what commands the model runs may write, read and reach, enforced by the operating system.

Approval decides whether a command runs; the sandbox decides what it can touch. Only commands the model runs
(execute_command, Skill tools) are sandboxed. Backends: bubblewrap on Linux (bwrap.py), Seatbelt on macOS
(seatbelt.py), MXC on Windows (windows_mxc.py) and, where MXC cannot run, a write-restricted token
(windows_token.py), which confines writes only. A sandbox that is switched on but has no working backend reports
itself unavailable, and commands are refused rather than run without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..config import SandboxConfig

# Inside every writable root: what MossCode or git would later act on, so a command must not write it.
PROTECTED_NAMES = (".mosscode", ".claude", ".mcp.json")
GIT_PROTECTED = ("hooks", "config")
# Credentials in the home directory; read access is refused where a grant would otherwise reach them.
HOME_SECRETS = (
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".config/gh",
    ".git-credentials",
    ".netrc",
    ".pypirc",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".mosscode",
)
TOOL_DIRS = {"bin", "cmd", "scripts"}  # A PATH entry named so sits in a tool's own folder, which it needs.
PROXY_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"}
NETWORK_LABELS = {
    "proxy": "经 MossCode 的代理，新域名需要你确认",
    "none": "断开",
    "open": "不受沙箱限制（这个后端只限制写入）",
}
BACKEND_NAMES = {"bubblewrap": "bubblewrap", "seatbelt": "Seatbelt", "mxc": "MXC", "token": "受限令牌"}
PLATFORM_BACKENDS = {"linux": ("bubblewrap",), "darwin": ("seatbelt",), "win32": ("mxc", "token")}
# What a sandboxed command may read outside the project, as the model is told.
READS_ALL = "项目外可以读取整个文件系统，凭据目录（~/.ssh 等）和 MossCode 的配置除外"
READS = {"bubblewrap": READS_ALL, "seatbelt": READS_ALL, "token": "读取不受沙箱限制"}
# Who can reach a server a sandboxed command starts, as the model is told; without it, models start a dev server in
# the sandbox and then wait on it. bubblewrap gives each command its own network namespace with a loopback. MXC lets
# a container connect only to its proxy port, not even to its own listener, and connections from the host into it time
# out (a limitation MXC documents for its proxy mode; measured with and without the proxy). Seatbelt denies all
# networking but the proxy port. The restricted token leaves the network alone.
LOCAL_PORTS = {
    "bubblewrap": "沙箱中启动的服务器只有同一条命令能连接，其他命令（包括后台命令）和用户都连不上；"
    "需要从别处访问时，服务器和访问它的命令都以 outside_sandbox 运行",
    "token": "",
}
LOCAL_PORTS_CLOSED = (
    "命令不能连接本机端口（包括它自己启动的服务器），其他命令和用户也连不上沙箱中的服务器；"
    "开发服务器、要连本机端口的测试等以 outside_sandbox 运行，访问服务器的命令也是"
)
UNAVAILABLE_HINT = "可以用 /sandbox off 在本会话关闭沙箱（无界面模式用 --no-sandbox），修复后再打开。"


@dataclass(frozen=True)
class Policy:
    root: Path
    writable: tuple[Path, ...]
    readable: tuple[Path, ...]
    protected: tuple[Path, ...]  # Inside writable roots; read-only when they exist, removed if a command creates them.
    deny_read: tuple[Path, ...]
    network: str = "none"


@dataclass(frozen=True)
class Status:
    state: str  # "off", "full" (writes, reads and network enforced), "partial" (writes only) or "unavailable"
    backend: str = ""
    reason: str = ""

    @property
    def label(self) -> str:
        return {"full": "沙箱", "partial": "沙箱·部分", "unavailable": "沙箱不可用"}.get(self.state, "无沙箱")


@dataclass
class Prepared:
    """A command wrapped for its backend; finish() runs after it exits, whatever happened."""

    argv: list[str]
    env: dict[str, str]
    policy: Policy
    watched: tuple[Path, ...] = ()  # Protected paths that did not exist before the command.
    files: tuple[Path, ...] = ()
    notes: list[str] = field(default_factory=list)
    ui_limits: bool = False  # The backend leaves desktop and clipboard to the Job, as for unsandboxed commands.

    def finish(self) -> list[str]:
        for path in self.files:
            path.unlink(missing_ok=True)
        for path in self.watched:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                continue
            self.notes.append(f"命令创建了受保护的路径 {path}，已删除。")
        return self.notes


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _broad(path: Path, home: Path) -> bool:
    """Drive roots, the home directory and the folders above it, and AppData's own folders: never granted whole."""
    if path.parent == path or path == home or home.is_relative_to(path):
        return True
    return path.parent.name.lower() == "appdata" and path.parent.parent == home


def _install_root(target: Path) -> Path:
    # Scoop installs to <scoop>\apps\<app>\<version>; other launchers sit in their tool's bin folder.
    for parent in target.parents:
        if parent.parent.parent.name.lower() == "apps" and parent.parent != parent:
            return parent
    return target.parent.parent if target.parent.name.lower() in TOOL_DIRS else target.parent


def _shim_targets(folder: Path):
    try:
        shims = list(folder.glob("*.shim"))
    except OSError:
        return
    for shim in shims[:500]:
        try:
            for line in shim.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "path":
                    yield Path(value.strip().strip('"'))
        except (OSError, UnicodeError):
            continue


def readable_roots(root: Path, extra: tuple[str, ...] = (), *, home: Path | None = None) -> tuple[Path, ...]:
    """Folders a command needs to read to run the user's tools: PATH entries and their install folders."""
    home = _resolved(home or Path.home())
    found: dict[Path, None] = {}

    def add(path: Path, *, whole: bool = False) -> None:
        path = _resolved(path)
        if path.exists() and (whole or not _broad(path, home)):
            found[path] = None

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.strip():
            continue
        folder = _resolved(Path(entry.strip()))
        if not folder.is_dir():
            continue
        add(folder)
        if folder.name.lower() in TOOL_DIRS:
            add(folder.parent)
        for target in _shim_targets(folder):
            add(_install_root(_resolved(target)))
    # A project virtual environment points at its base interpreter.
    for venv in (root / ".venv", root / "venv"):
        try:
            lines = (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "home":
                base = Path(value.strip())
                add(base.parent if base.name.lower() in TOOL_DIRS else base)
    for name in (".gitconfig", ".config/git", ".mosscode/skills", ".mosscode/plugins"):
        add(home / name, whole=True)
    for path in extra:
        add(Path(path), whole=True)
    return tuple(found)


def _git_dirs(root: Path) -> tuple[Path, ...]:
    """The repository's .git folder; for a worktree also the main repository's, where commits are written."""
    git = root / ".git"
    if git.is_dir():
        return (git,)
    try:
        text = git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ()
    if not text.startswith("gitdir:"):
        return ()
    own = _resolved(root / text.removeprefix("gitdir:").strip())
    try:
        common = (own / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return (own,)
    return (own, _resolved(own / common))


def policy_for(
    root: Path,
    config: SandboxConfig,
    temp: Path,
    *,
    secrets: tuple[Path, ...] = (),
    home: Path | None = None,
    reads_all: bool = False,
) -> Policy:
    """secrets: files commands must not read wherever they are, such as the configuration file in use.

    reads_all: the backend lets commands read the whole filesystem and hides the denied paths (bubblewrap,
    Seatbelt), so every one of them is listed, even where it holds the private temp or a readable grant; the
    backend exposes those again. MXC instead reads only what is granted, so only denials inside a grant matter.
    """
    root = _resolved(root)
    home = _resolved(home or Path.home())
    git = _git_dirs(root)
    writable = dict.fromkeys((root, _resolved(temp), *(g for g in git if not g.is_relative_to(root))))
    writable.update(dict.fromkeys(_resolved(Path(p)) for p in config.allow_write))
    protected = [root / name for name in PROTECTED_NAMES]
    protected += [g / name for g in git for name in GIT_PROTECTED]
    readable = tuple(p for p in readable_roots(root, config.allow_read, home=home) if p not in writable)
    granted = (*writable, *readable)
    candidates = [home / name for name in HOME_SECRETS] + [_resolved(Path(p)) for p in config.deny_read]
    candidates += [_resolved(p) for p in secrets]
    deny = tuple(
        dict.fromkeys(
            p
            for p in candidates
            if p.exists()
            and (reads_all or (any(p.is_relative_to(g) for g in granted) and not _resolved(temp).is_relative_to(p)))
        )
    )
    return Policy(root, tuple(writable), readable, tuple(dict.fromkeys(protected)), deny)


def safe_python(writable: tuple[Path, ...] = ()) -> str | None:
    """An interpreter MossCode can run unsandboxed next to a command: never one inside a writable root.

    A project's .venv sits in the root a command may write; a command replacing its python would have the next
    command's unsandboxed launcher run its code. The base interpreter a virtual environment points at is outside.
    """
    candidates = [os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)]
    if os.name != "nt":
        candidates += [os.path.realpath(p) for p in ("/usr/bin/python3", "/bin/python3") if os.path.exists(p)]
    for path in candidates:
        if os.path.exists(path) and not any(Path(path).is_relative_to(root) for root in writable):
            return path
    return None


class Sandbox:
    def __init__(self, config: SandboxConfig | None = None, *, enabled: bool | None = None, source: Path | None = None):
        self.config = config or SandboxConfig()
        self.enabled = self.config.mode == "on" if enabled is None else enabled
        # The tools are built for the sandbox at startup (shell, parameters); turning it on later needs a restart.
        self.started = self.enabled
        self.source = source  # The configuration file in use; commands must not read it.
        self._status: Status | None = None
        self.events: list[str] = []  # Recent blocks and removals, shown by /sandbox.
        self.network = "none"  # "proxy" once the backend is known to confine commands to MossCode's proxy.
        self.network_note = ""  # Why the configured proxy is not in use.
        self.fallback_note = ""  # Why a weaker backend than the platform's best is in use.
        self.executable = ""
        self._open: list[Path] | None = None  # Folders Everyone may write, found once per session.

    def status(self) -> Status:
        if not self.enabled:
            return Status("off")
        if self._status is None:
            self._status = self._detect()
        return self._status

    def _detect(self) -> Status:
        system = "linux" if sys.platform.startswith("linux") else sys.platform
        choices = PLATFORM_BACKENDS.get(system, ())
        wanted = self.config.backend
        if not choices:
            return Status("unavailable", reason="这个系统没有可用的沙箱后端（支持 Linux、macOS 和 Windows）。")
        if wanted != "auto" and wanted not in choices:
            return Status(
                "unavailable",
                wanted,
                f"sandbox.backend 为 {wanted}，这个系统上不能使用；可选 auto、" + "、".join(choices) + "。",
            )
        if wanted == "token":
            return self._detect_token()
        status = getattr(self, "_detect_" + choices[0])()
        if status.state != "unavailable" or wanted != "auto" or len(choices) == 1:
            return status
        # Never silently weaker: the fallback says why the full backend is not in use, here and at startup.
        fallback = self._detect_token()
        if fallback.state == "unavailable":
            return status
        self.fallback_note = f"MXC 不可用（{status.reason.rstrip('。')}），改用受限令牌：只限制写入，命令仍需审批。"
        return fallback

    def _detect_bubblewrap(self) -> Status:
        from . import bwrap

        found = bwrap.find()
        if found is None:
            return Status("unavailable", "bubblewrap", bwrap.MISSING)
        problem = bwrap.check(found)
        if problem:
            return Status("unavailable", "bubblewrap", problem)
        self.executable = found
        self.network = "proxy" if self.config.network == "proxy" else "none"
        return Status("full", "bubblewrap")

    def _detect_seatbelt(self) -> Status:
        from . import seatbelt

        found = seatbelt.find()
        if found is None:
            return Status("unavailable", "seatbelt", "找不到 sandbox-exec（macOS 自带，位于 /usr/bin/sandbox-exec）。")
        problem = seatbelt.check(found)
        if problem:
            return Status("unavailable", "seatbelt", problem)
        self.executable = found
        self.network = "proxy" if self.config.network == "proxy" else "none"
        return Status("full", "seatbelt")

    def _detect_mxc(self) -> Status:
        from . import windows_mxc

        found = windows_mxc.find(self.config.wxc_exec)
        if found is None:
            return Status("unavailable", "mxc", windows_mxc.MISSING)
        problem = windows_mxc.check(found)
        if problem:
            return Status("unavailable", "mxc", problem)
        self.executable = self.wxc = found
        if self.config.network == "proxy":
            if windows_mxc.loopback_proxy(found):
                self.network = "proxy"
            else:
                self.network_note = "本机的 MXC 不支持把容器限制在一个回环代理端口上，沙箱中的命令断网。"
        return Status("full", "mxc")

    def _detect_token(self) -> Status:
        from . import windows_token

        problem = windows_token.check()
        if problem:
            return Status("unavailable", "token", problem)
        # Reads and connections are not confined, so a proxy would only add prompts to a command already approved.
        self.network = "open"
        return Status("partial", "token")

    @property
    def active(self) -> bool:
        return self.status().state in ("full", "partial")

    @property
    def full(self) -> bool:
        """Writes, reads and network all confined: what lets a command run without approval."""
        return self.status().state == "full"

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.record("用户在本会话中" + ("打开了沙箱。" if enabled else "关闭了沙箱，命令不再受沙箱限制。"))

    def badge(self) -> str:
        """The status bar's word: nothing for someone who never turned the sandbox on."""
        if not self.enabled:
            return "无沙箱" if self.started else ""
        return self.status().label

    def notices(self, root: Path | None = None) -> list[str]:
        """What the startup area says about the sandbox, besides it being on."""
        if not self.enabled:
            return []
        if not self.active:
            return ["沙箱不可用，模型的命令都会被拒绝：" + self.status().reason]
        notes = [note for note in (self.fallback_note, self.network_note, self.open_note(root or Path.cwd())) if note]
        return ["沙箱：" + note for note in notes]

    def open_note(self, root: Path) -> str:
        """The restricted token cannot keep commands out of folders whose ACL lets Everyone write."""
        if self.status().backend != "token":
            return ""
        if self._open is None:
            from . import windows_token

            try:
                self._open = windows_token.open_folders(self.policy(root))
            except OSError:
                self._open = []
        if not self._open:
            return ""
        shown = "、".join(str(p) for p in self._open[:5]) + (f" 等 {len(self._open)} 个" if len(self._open) > 5 else "")
        return f"{shown} 对 Everyone 可写，受限令牌挡不住命令写入这些目录；可以收紧它们的权限，或改用 MXC。"

    def report(self, root: Path) -> list[str]:
        """What /sandbox shows: state, boundaries and recent events."""
        status = self.status()
        if not self.enabled:
            lines = ["沙箱：关闭" + ("（本会话中被关闭）" if self.started else "")]
            if self.started:
                lines.append("  /sandbox on 重新打开。")
            else:
                lines.append("  在 config.yaml 中设 sandbox.mode: on，或用 --sandbox 启动后才能使用。")
        elif not self.active:
            lines = ["沙箱：不可用，模型的命令都会被拒绝", "  原因：" + status.reason, "  " + UNAVAILABLE_HINT]
        else:
            policy = self.policy(root)
            backend = BACKEND_NAMES.get(status.backend, status.backend)
            if status.state == "full":
                level = "写入、读取和网络都由系统强制"
            else:
                level = "只限制写入，读取和网络不受限，命令仍需审批"
            if status.backend in ("bubblewrap", "seatbelt"):
                readable = "整个文件系统，禁止读取的路径除外"
            elif status.backend == "token":
                readable = "不受限制"
            else:
                readable = f"系统目录和 {len(policy.readable)} 个工具目录"
            denied = "、".join(str(p) for p in policy.deny_read) or "无"
            lines = [
                f"沙箱：开启（{backend}，{level}）",
                "  可写：" + "、".join(str(p) for p in policy.writable),
                "  只读保护：" + "、".join(str(p) for p in policy.protected),
                "  项目外可读：" + readable,
                "  禁止读取：" + ("不支持（这个后端只限制写入）" if status.backend == "token" else denied),
                "  网络：" + NETWORK_LABELS[self.network] + (f"（{self.network_note}）" if self.network_note else ""),
                "  越界：" + ("禁止" if self.config.allow_unsandboxed == "never" else "模型带理由申请，每次询问"),
            ]
            for note in (self.fallback_note, self.open_note(root)):
                if note:
                    lines.append("  " + note)
        if self.events:
            lines.append("最近的记录：")
            lines.extend("  · " + event for event in self.events)
        return lines

    def record(self, note: str) -> None:
        self.events = [*self.events, note][-20:]

    def policy(self, root: Path) -> Policy:
        from ..command_env import private_temp

        reads_all = self.status().backend in ("bubblewrap", "seatbelt")
        secrets = (self.source,) if self.source else ()
        policy = policy_for(root, self.config, private_temp(), secrets=secrets, reads_all=reads_all)
        return replace(policy, network=self.network)

    def proxy_socket(self) -> Path | None:
        """Where a command's proxy listens when the backend reaches it through a Unix socket, not a port."""
        if self.status().backend != "bubblewrap":
            return None
        import secrets

        from ..command_env import private_temp

        return private_temp() / f"proxy-{secrets.token_hex(4)}.sock"

    def prepare(
        self, argv: list[str], cwd: Path, env: dict[str, str], root: Path, proxy: str | None = None
    ) -> Prepared:
        """proxy: this command's own listener, its only way out; without one the command has no network.

        Raises SandboxFailure when the backend cannot wrap the command; it then does not run.
        """
        backend = self.status().backend
        policy = self.policy(root)
        if backend != "token":
            # The user's proxy settings would point at a port the sandbox cannot reach.
            env = {k: v for k, v in env.items() if k.upper() not in PROXY_NAMES}
            if proxy is not None:
                if backend == "bubblewrap":
                    from .bwrap import BRIDGE_PORT

                    url = f"http://127.0.0.1:{BRIDGE_PORT}"
                else:
                    url = proxy
                env.update({name: url for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
        watched = tuple(p for p in policy.protected if not os.path.lexists(p))
        for git in _git_dirs(policy.root):
            # git creates hooks/ itself; making it now lets it be read-only rather than watched.
            if not (git / "hooks").exists():
                try:
                    (git / "hooks").mkdir()
                    watched = tuple(p for p in watched if p != git / "hooks")
                except OSError:
                    pass
        if backend == "mxc":
            from . import windows_mxc

            request = windows_mxc.write_request(subprocess.list2cmdline(argv), cwd, env, policy, proxy)
            return Prepared([str(self.wxc), str(request)], env, policy, watched, (request,))
        python = safe_python(policy.writable)
        if python is None:
            raise SandboxFailure("找不到可写目录之外的 Python 解释器，无法启动沙箱。")
        if backend == "bubblewrap":
            from ..command_env import private_temp
            from . import bwrap

            socket = proxy.removeprefix("unix:") if proxy is not None else None
            wrapped = bwrap.command(
                argv, cwd, policy, bwrap=self.executable, python=python, temp=private_temp(), socket=socket
            )
            return Prepared(wrapped, env, policy, watched)
        if backend == "seatbelt":
            from urllib.parse import urlsplit

            from . import seatbelt

            port = urlsplit(proxy).port if proxy is not None else None
            return Prepared(seatbelt.command(argv, policy, exe=self.executable, port=port), env, policy, watched)
        from . import windows_token

        wrapped = windows_token.command(argv, cwd, policy, python=python)
        return Prepared(wrapped, env, policy, watched, ui_limits=True)


class SandboxFailure(Exception):
    """The backend could not wrap a command; it did not run."""


_current = Sandbox()


def configure(config: SandboxConfig, *, override: bool | None = None, source: Path | None = None) -> Sandbox:
    """override: --sandbox / --no-sandbox for this run."""
    global _current
    _current = Sandbox(config, enabled=override, source=source)
    return _current


HINT_MARKER = "sandbox-hint-shown"
FIRST_START_HINT = (
    "命令沙箱默认关闭，模型的命令以你的权限运行（仍按权限档位审批）。用 moss --sandbox 启动，"
    "或在用户配置中设 sandbox.mode: on，可以把命令的写入限制在项目内并管住联网；"
    "moss doctor 显示这台机器能用哪种沙箱。这条提示只显示一次。"
)


def first_start_hint(sandbox: Sandbox, override: bool | None, home: Path | None = None) -> str | None:
    """The sandbox is opt-in, so whoever has not read about it is told once that it exists. Someone who has
    turned it on, or passed --sandbox / --no-sandbox, already knows: that counts as having seen the hint."""
    from ..paths import mosscode_home

    system = "linux" if sys.platform.startswith("linux") else sys.platform
    if system not in PLATFORM_BACKENDS:
        return None
    marker = mosscode_home(home) / HINT_MARKER
    if marker.exists():
        return None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        return None  # Shown only once it is remembered; otherwise it would come back at every start.
    if sandbox.started or override is not None:
        return None
    return FIRST_START_HINT


def current() -> Sandbox:
    return _current


def badge() -> str:
    return _current.badge()


DENIAL_SIGNS = (
    # The proxy's own refusal, and how clients report it.
    "mosscode 沙箱",
    "tunnel connection failed",
    "(403)",
    "403 forbidden",
    "access is denied",
    "拒绝访问",
    # .NET wording on Chinese Windows, e.g. Rename-Item's IOException under the restricted token.
    "访问被拒绝",
    "permission denied",
    # bubblewrap mounts everything outside the writable roots read-only.
    "read-only file system",
    "unauthorizedaccess",
    "eacces",
    "eperm",
    "operation not permitted",
    "could not resolve host",
    "getaddrinfo",
    "name resolution",
    "nameresolutionerror",
    "no such host",
    # With the network off, any failed connection is the sandbox's doing.
    "unable to connect to the remote server",
    "无法连接到远程服务器",
    "the remote name could not be resolved",
    "无法解析此远程名称",
    "failed to establish a new connection",
    "network is unreachable",
    # A socket the container may not use, e.g. connecting to a loopback port under MXC (WinError 10013).
    "forbidden by its access permissions",
    "访问套接字",
    "0xc0000142",
    "0xc0000135",
)


def reads_outside(backend: str) -> str:
    return READS.get(backend, "项目外只能读取系统目录、PATH 中的工具目录和配置的 allow_read")


def local_ports(backend: str) -> str:
    return LOCAL_PORTS.get(backend, LOCAL_PORTS_CLOSED)


def explain(details: dict) -> str:
    """What to tell the model after a sandboxed command failed; empty when nothing points at the sandbox."""
    code = details.get("exit_code")
    if code is None:
        return ""
    # PowerShell reports a denied cmdlet on stderr and still exits 0, so a clean exit counts only stderr.
    text = details.get("stderr", "") if code == 0 else details.get("stdout", "") + "\n" + details.get("stderr", "")
    if not any(sign in text.lower() for sign in DENIAL_SIGNS):
        return ""
    box = details.get("sandbox", {})
    writable = "、".join(box.get("writable", ()))
    network = {
        "proxy": "网络只经 MossCode 的代理连接用户允许的域名，结果末尾列出被拒绝的主机",
        "open": "网络不受沙箱限制",
    }.get(box.get("network"), "网络已断开")
    ports = local_ports(box.get("backend", ""))
    return (
        "\n\n命令在沙箱中运行，失败可能是沙箱拦截造成的（推测，依据是输出中的拒绝访问、无法解析主机或代理拒绝）："
        f"只能写入 {writable}；{network}；{reads_outside(box.get('backend', ''))}；{ports + '；' if ports else ''}"
        "受保护的 .mosscode、.git/hooks、.git/config 只读。"
        "先确认能否在边界内完成；确需越界时，以 outside_sandbox: true 重新调用，并在 justification 中说明理由，"
        "用户批准后在沙箱外运行。"
    )


def startup_failure(details: dict) -> str:
    """wxc-exec's own error, when the command never started."""
    stderr = details.get("stderr", "")
    if details.get("exit_code") not in (-1, 0xFFFFFFFF) or '{"error":' not in stderr:
        return ""
    import json

    try:
        error = json.loads(stderr[stderr.index('{"error":') :])["error"]
        message = f"{error.get('code', '')}: {error.get('message', '')}"
    except (ValueError, KeyError, TypeError):
        message = stderr.strip()[-400:]
    return f"沙箱没能启动命令（MXC 报告 {message}）。" + UNAVAILABLE_HINT
