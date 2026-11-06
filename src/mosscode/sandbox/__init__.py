"""The command sandbox: what commands the model runs may write, read and reach, enforced by the operating system.

Approval decides whether a command runs; the sandbox decides what it can touch. Only commands the model runs
(execute_command, Skill tools) are sandboxed. The one backend so far is MXC on Windows (windows_mxc.py); on other
systems a sandbox that is switched on reports itself unavailable, and commands are refused rather than run
without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
NETWORK_LABELS = {"proxy": "经 MossCode 的代理，新域名需要你确认", "none": "断开"}
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
    state: str  # "off", "full" or "unavailable"
    backend: str = ""
    reason: str = ""

    @property
    def label(self) -> str:
        return {"full": "沙箱", "unavailable": "沙箱不可用"}.get(self.state, "无沙箱")


@dataclass
class Prepared:
    """A command wrapped for its backend; finish() runs after it exits, whatever happened."""

    argv: list[str]
    env: dict[str, str]
    policy: Policy
    watched: tuple[Path, ...] = ()  # Protected paths that did not exist before the command.
    files: tuple[Path, ...] = ()
    notes: list[str] = field(default_factory=list)

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
    root: Path, config: SandboxConfig, temp: Path, *, secrets: tuple[Path, ...] = (), home: Path | None = None
) -> Policy:
    """secrets: files commands must not read wherever they are, such as the configuration file in use."""
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
            if p.exists() and any(p.is_relative_to(g) for g in granted) and not _resolved(temp).is_relative_to(p)
        )
    )
    return Policy(root, tuple(writable), readable, tuple(dict.fromkeys(protected)), deny)


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

    def status(self) -> Status:
        if not self.enabled:
            return Status("off")
        if self._status is None:
            self._status = self._detect()
        return self._status

    def _detect(self) -> Status:
        if os.name != "nt":
            return Status(
                "unavailable", reason="这个系统的沙箱后端还没有实现（计划中：Linux bubblewrap、macOS Seatbelt）。"
            )
        from . import windows_mxc

        found = windows_mxc.find(self.config.wxc_exec)
        if found is None:
            return Status("unavailable", "mxc", windows_mxc.MISSING)
        problem = windows_mxc.check(found)
        if problem:
            return Status("unavailable", "mxc", problem)
        self.wxc = found
        if self.config.network == "proxy":
            if windows_mxc.loopback_proxy(found):
                self.network = "proxy"
            else:
                self.network_note = "本机的 MXC 不支持把容器限制在一个回环代理端口上，沙箱中的命令断网。"
        return Status("full", "mxc")

    @property
    def active(self) -> bool:
        return self.status().state == "full"

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.record("用户在本会话中" + ("打开了沙箱。" if enabled else "关闭了沙箱，命令不再受沙箱限制。"))

    def badge(self) -> str:
        """The status bar's word: nothing for someone who never turned the sandbox on."""
        if not self.enabled:
            return "无沙箱" if self.started else ""
        return self.status().label

    def report(self, root: Path) -> list[str]:
        """What /sandbox shows: state, boundaries and recent events."""
        status = self.status()
        if not self.enabled:
            lines = ["沙箱：关闭" + ("（本会话中被关闭）" if self.started else "")]
            if self.started:
                lines.append("  /sandbox on 重新打开。")
            else:
                lines.append("  在 config.yaml 中设 sandbox.mode: on，或用 --sandbox 启动后才能使用。")
        elif status.state != "full":
            lines = ["沙箱：不可用，模型的命令都会被拒绝", "  原因：" + status.reason, "  " + UNAVAILABLE_HINT]
        else:
            policy = self.policy(root)
            lines = [
                f"沙箱：开启（{status.backend.upper()}，写入、读取和网络都由系统强制）",
                "  可写：" + "、".join(str(p) for p in policy.writable),
                "  只读保护：" + "、".join(str(p) for p in policy.protected),
                f"  项目外可读：系统目录和 {len(policy.readable)} 个工具目录",
                "  禁止读取：" + ("、".join(str(p) for p in policy.deny_read) or "无"),
                "  网络：" + NETWORK_LABELS[self.network] + (f"（{self.network_note}）" if self.network_note else ""),
                "  越界：" + ("禁止" if self.config.allow_unsandboxed == "never" else "模型带理由申请，每次询问"),
            ]
        if self.events:
            lines.append("最近的记录：")
            lines.extend("  · " + event for event in self.events)
        return lines

    def record(self, note: str) -> None:
        self.events = [*self.events, note][-20:]

    def policy(self, root: Path) -> Policy:
        from ..command_env import private_temp

        policy = policy_for(root, self.config, private_temp(), secrets=(self.source,) if self.source else ())
        return replace(policy, network=self.network)

    def prepare(
        self, argv: list[str], cwd: Path, env: dict[str, str], root: Path, proxy: str | None = None
    ) -> Prepared:
        """proxy: this command's own listener, its only way out; without one the command has no network."""
        from . import windows_mxc

        policy = self.policy(root)
        # The user's proxy settings would point at a port the container cannot reach.
        env = {k: v for k, v in env.items() if k.upper() not in PROXY_NAMES}
        if proxy is not None:
            env.update({name: proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
        watched = tuple(p for p in policy.protected if not os.path.lexists(p))
        for git in _git_dirs(policy.root):
            # git creates hooks/ itself; making it now lets it be read-only rather than watched.
            if not (git / "hooks").exists():
                try:
                    (git / "hooks").mkdir()
                    watched = tuple(p for p in watched if p != git / "hooks")
                except OSError:
                    pass
        request = windows_mxc.write_request(subprocess.list2cmdline(argv), cwd, env, policy, proxy)
        return Prepared([str(self.wxc), str(request)], env, policy, watched, (request,))


_current = Sandbox()


def configure(config: SandboxConfig, *, override: bool | None = None, source: Path | None = None) -> Sandbox:
    """override: --sandbox / --no-sandbox for this run."""
    global _current
    _current = Sandbox(config, enabled=override, source=source)
    return _current


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
    "permission denied",
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
    "0xc0000142",
    "0xc0000135",
)


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
    network = (
        "网络只经 MossCode 的代理连接用户允许的域名，结果末尾列出被拒绝的主机"
        if box.get("network") == "proxy"
        else "网络已断开"
    )
    return (
        "\n\n命令在沙箱中运行，失败可能是沙箱拦截造成的（推测，依据是输出中的拒绝访问、无法解析主机或代理拒绝）："
        f"只能写入 {writable}；{network}；项目外只能读取系统目录、PATH 中的工具目录和配置的 allow_read；"
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
