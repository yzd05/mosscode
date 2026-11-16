"""Linux backend: bubblewrap.

The whole filesystem is mounted read-only, writable roots are bound back writable, credentials are hidden under
empty mounts, and the command gets its own PID, IPC and network namespaces. Its network namespace has only
loopback: with the proxy on, linux_exec.py bridges a loopback port to MossCode's proxy on a Unix socket.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MISSING = "找不到 bubblewrap（bwrap）。请用系统的包管理器安装，例如 sudo apt install bubblewrap、sudo dnf install bubblewrap。"
BRIDGE_PORT = 3128  # Inside the command's own network namespace, so it cannot clash with anything.
_LAUNCHER_FILE = Path(__file__).with_name("linux_exec.py")
# Read once at import: in an editable install this file sits in a writable root, and a command must not be able
# to rewrite what the next command runs before its seccomp filter.
_TEXT = _LAUNCHER_FILE.read_text(encoding="utf-8")
LAUNCHER = f"SOURCE = {_TEXT!r}\n{_TEXT}"


def find() -> str | None:
    return shutil.which("bwrap")


def check(bwrap: str) -> str:
    """An empty string when bubblewrap can build the namespaces here; otherwise why not."""
    from . import safe_python

    python = safe_python()
    if python is None:
        return "找不到用来启动沙箱的 Python 解释器。"
    argv = [
        bwrap,
        *_namespaces(),
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--",
        python,
        "-I",
        "-S",
        "-c",
        LAUNCHER,
        "exec",
        "--",
        "true",
    ]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bubblewrap 自检失败：{exc}。"
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        return (
            f"bubblewrap 无法创建沙箱（{detail or f'退出码 {done.returncode}'}）。常见原因：系统禁止非特权用户命名空间"
            "（Ubuntu 23.10 起的 AppArmor 限制、kernel.unprivileged_userns_clone=0），或在不允许嵌套命名空间的容器中运行。"
        )
    return ""


def _namespaces() -> list[str]:
    return [
        "--new-session",  # No TIOCSTI: a command cannot type into the terminal MossCode runs in.
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--unshare-cgroup-try",
    ]


def _under(path: Path, folders) -> bool:
    return any(path != folder and path.is_relative_to(folder) for folder in folders)


def command(
    argv: list[str],
    cwd: Path,
    policy,
    *,
    bwrap: str,
    python: str,
    temp: Path | None,
    socket: str | None = None,
) -> list[str]:
    """socket: MossCode's proxy for this command; without it the command has no network at all."""
    out = [bwrap, *_namespaces(), "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    if temp is not None:
        # Programs that ignore TMPDIR still get a private /tmp; first, so roots under /tmp are bound over it.
        out += ["--bind", str(temp), "/tmp"]
    for path in policy.writable:
        out += ["--bind", str(path), str(path)]
    hidden = [p for p in policy.deny_read if p.is_dir()]
    for path in policy.deny_read:
        out += ["--tmpfs", str(path)] if path.is_dir() else ["--ro-bind", "/dev/null", str(path)]
    # What a hidden folder holds that commands still need: the private temp under ~/.mosscode, Skill folders.
    for path in policy.writable:
        if _under(path, hidden):
            out += ["--bind", str(path), str(path)]
    for path in policy.readable:
        if _under(path, hidden):
            out += ["--ro-bind", str(path), str(path)]
    # A mount point cannot be renamed or removed, so the folders between a writable root and a protected path
    # become mount points too: otherwise `mv .git x` would carry the protected hooks off where they are writable.
    anchors: dict[Path, None] = {}
    existing = [p for p in policy.protected if os.path.lexists(p)]
    for path in existing:
        root = next((r for r in policy.writable if path.is_relative_to(r)), None)
        if root is None:
            continue
        for parent in reversed(path.parents):
            if parent.is_relative_to(root) and parent != root:
                anchors[parent] = None
    for path in anchors:
        out += ["--bind", str(path), str(path)]
    for path in existing:
        out += ["--ro-bind", str(path), str(path)]
    out += ["--chdir", str(cwd), "--", python, "-I", "-S", "-c", LAUNCHER]
    if socket is not None:
        return [*out, "bridge", socket, str(BRIDGE_PORT), "--", *argv]
    return [*out, "exec", "--", *argv]
