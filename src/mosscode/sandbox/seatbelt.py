"""macOS backend: Seatbelt through sandbox-exec, with a profile generated per command.

The profile follows Gemini CLI's proxied profiles: allow by default, then deny every write outside the writable
roots, writes to protected paths, reads of credentials and all networking except MossCode's proxy port. In
Seatbelt profiles a later rule takes precedence over an earlier one for the same operation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Devices every program writes to; temp folders macOS hands out itself (confstr), which tools use whatever
# TMPDIR says.
DEVICES = ('(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")')
DEVICE_PATTERNS = ('(regex #"^/dev/tty")', '(regex #"^/dev/fd/")', '(regex #"^/dev/ptmx$")', '(regex #"^/dev/ttys")')
SYSTEM_TEMP = ("/private/tmp", "/private/var/folders")


def find() -> str | None:
    return SANDBOX_EXEC if os.path.exists(SANDBOX_EXEC) else shutil.which("sandbox-exec")


def check(exe: str) -> str:
    try:
        done = subprocess.run(
            [exe, "-p", "(version 1)(allow default)", "/usr/bin/true"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"sandbox-exec 自检失败：{exc}。"
    if done.returncode != 0:
        return "sandbox-exec 无法运行：" + (done.stderr.decode("utf-8", "replace").strip()[-300:] or "未知原因") + "。"
    return ""


def _q(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _anchors(policy) -> list[Path]:
    """Folders between a writable root and a protected path: renaming one would carry the protected path off."""
    found: dict[Path, None] = {}
    for path in policy.protected:
        root = next((r for r in policy.writable if path.is_relative_to(r)), None)
        if root is None:
            continue
        for parent in path.parents:
            if parent.is_relative_to(root) and parent != root:
                found[parent] = None
    return list(found)


def profile(policy, *, port: int | None = None) -> str:
    """port: MossCode's proxy on localhost for this command; without it every connection is refused."""
    writable = [f"(subpath {_q(p)})" for p in policy.writable]
    writable += [f"(subpath {_q(p)})" for p in SYSTEM_TEMP]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write* " + " ".join([*writable, *DEVICES, *DEVICE_PATTERNS]) + ")",
    ]
    protected = [f"(subpath {_q(p)})" for p in policy.protected]
    # Without the folders themselves (literal): files can still be created in them, only not the folder moved.
    protected += [f"(literal {_q(p)})" for p in _anchors(policy)]
    if protected:
        lines.append("(deny file-write* " + " ".join(protected) + ")")
    if policy.deny_read:
        lines.append("(deny file-read-data " + " ".join(f"(subpath {_q(p)})" for p in policy.deny_read) + ")")
        exposed = [
            p for p in (*policy.writable, *policy.readable) if any(p.is_relative_to(d) for d in policy.deny_read)
        ]
        if exposed:
            lines.append("(allow file-read-data " + " ".join(f"(subpath {_q(p)})" for p in exposed) + ")")
    # Unix sockets too: the Docker daemon, ssh-agent and the like are reached through socket files.
    lines.append("(deny network*)")
    if port is not None:
        lines.append(f'(allow network-outbound (remote tcp "localhost:{port}"))')
    return "\n".join(lines) + "\n"


def command(argv: list[str], policy, *, exe: str, port: int | None = None) -> list[str]:
    return [exe, "-p", profile(policy, port=port), *argv]
