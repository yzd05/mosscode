"""Windows backend: MXC (Microsoft eXecution Containers) through its wxc-exec.exe launcher.

Requests use the published 1.0.0 contract, whose telemetry stays off unless a run opts in. The launcher is not
shipped with MossCode; users install it themselves. A command in the container can read only what is granted
(system folders are readable by default), so the tools it runs need their folders listed as read-only paths.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

MISSING = (
    "找不到 MXC 的 wxc-exec.exe。请从 https://github.com/microsoft/mxc/releases 下载 mxc-release-binaries.zip，"
    "把其中的 x64/wxc-exec.exe（ARM 设备用 arm64/）放到 PATH 中、~/.mosscode/bin/，或在 config.yaml 的 "
    "sandbox.wxc_exec 中填写路径。"
)
PROBE_TIMEOUT = 60


def _home() -> Path:
    return Path.home() / ".mosscode"


def find(configured: str | None = None) -> Path | None:
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    found = shutil.which("wxc-exec")
    if found:
        return Path(found)
    fallback = _home() / "bin" / "wxc-exec.exe"
    return fallback if fallback.is_file() else None


def _cache_key(wxc: Path) -> str:
    stat = wxc.stat()
    return f"{wxc.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{platform.version()}"


def check(wxc: Path) -> str:
    """An empty string when MXC enforces writes, reads and the network here; otherwise why not.

    The result is cached per launcher and Windows build, since a probe and a test run take about a second.
    """
    try:
        key = _cache_key(wxc)
    except OSError as exc:
        return f"无法读取 {wxc}：{exc.strerror or exc}。"
    stored = _cached()
    if isinstance(stored.get(key), str):
        return stored[key]
    problem = _probe(wxc) or _self_test(wxc)
    if not problem:  # Failures are retried next time: they are often fixed by an update or a restart.
        _store(key, problem)
    return problem


def loopback_proxy(wxc: Path) -> bool:
    """Whether a container can be held to one loopback port, where MossCode's proxy listens (no packaged proxy)."""
    try:
        key = "proxy|" + _cache_key(wxc)
    except OSError:
        return False
    stored = _cached()
    if isinstance(stored.get(key), bool):
        return stored[key]
    try:
        report = _report(wxc)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    probes = report.get("probes") if isinstance(report, dict) else None
    supported = isinstance(probes, dict) and probes.get("baseContainerSupportsIdentitylessLoopbackProxy") is True
    _store(key, supported)
    return supported


def _cached() -> dict:
    try:
        stored = json.loads((_home() / "sandbox-probe.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _store(key: str, value) -> None:
    # Only the current launcher and Windows build matter; older entries are dropped.
    current = key.removeprefix("proxy|")
    kept = {k: v for k, v in _cached().items() if k.removeprefix("proxy|") == current}
    try:
        _home().mkdir(parents=True, exist_ok=True)
        (_home() / "sandbox-probe.json").write_text(json.dumps({**kept, key: value}, ensure_ascii=False), "utf-8")
    except OSError:
        pass


def _report(wxc: Path):
    done = subprocess.run([str(wxc), "--probe"], capture_output=True, timeout=PROBE_TIMEOUT, creationflags=0x08000000)
    return json.loads(done.stdout.decode("utf-8", "replace"))


def _probe(wxc: Path) -> str:
    try:
        report = _report(wxc)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return f"wxc-exec --probe 失败：{exc}。"
    tier = report.get("tier") if isinstance(report, dict) else None
    if tier != "base-container" or report.get("needsDaclAugmentation"):
        return (
            f"MXC 在本机的隔离级别是 {tier or '未知'}，沙箱需要 base-container（Windows 11 24H2/25H2 的 26x00.9278 "
            "或 26H2 的 26300.9550 及以上更新）。"
        )
    return ""


def _self_test(wxc: Path) -> str:
    """Run one command in a container: it must write inside its folder and be refused outside it."""
    _home().mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="sandbox-test-", dir=_home()))
    try:
        inside, outside = base / "inside", base / "outside"
        inside.mkdir()
        outside.mkdir()
        request = {
            **_base_request(),
            "process": {
                "commandLine": f'cmd.exe /d /c "echo x> "{inside / "a.txt"}" & echo x> "{outside / "b.txt"}""',
                "cwd": str(inside),
                "timeout": PROBE_TIMEOUT * 1000,
            },
            "filesystem": {"readwritePaths": [str(inside)]},
        }
        path = base / "request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        subprocess.run([str(wxc), str(path)], capture_output=True, timeout=PROBE_TIMEOUT, creationflags=0x08000000)
        if not (inside / "a.txt").exists():
            return "MXC 自检失败：容器中的命令没能写入允许的目录。"
        if (outside / "b.txt").exists():
            return "MXC 自检失败：容器中的命令写入了不允许的目录。"
        return ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"MXC 自检失败：{exc}。"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _base_request() -> dict:
    return {
        "version": "1.0.0",
        "containment": "processcontainer",
        # Hosts' loopback stays closed: a local proxy there would carry commands to the internet.
        "network": {"egress": {"default": "deny"}, "ingress": {"default": "deny", "hostLoopback": "deny"}},
        # PowerShell and most console programs fail to start without user-interface access.
        "ui": {"disable": False, "clipboard": "none", "injection": False},
        "telemetry": {"enabled": False},
        "fallback": {"allowDaclMutation": False},
    }


def request(command_line: str, cwd: Path, env: dict[str, str], policy, proxy: str | None = None) -> dict:
    """proxy: MossCode's loopback proxy for this command, then the container's only way out."""
    existing = [p for p in policy.protected if p.exists()]
    network = {}
    if proxy is not None:
        # Proxy-only egress: WFP lets the container reach that one loopback port and nothing else. Without a
        # packaged proxy identity MXC requires host loopback and private-network capability to be granted.
        network = {
            "network": {"egress": {"default": "deny"}, "ingress": {"default": "allow", "hostLoopback": "allow"}},
            "runtimeConfig": {"networkProxy": proxy},
        }
    return {
        **_base_request(),
        **network,
        "process": {
            "commandLine": command_line,
            "cwd": str(cwd),
            "env": [f"{name}={value}" for name, value in env.items()],
        },
        "filesystem": {
            "readwritePaths": [str(p) for p in policy.writable],
            "readonlyPaths": [str(p) for p in (*policy.readable, *existing)],
            "deniedPaths": [str(p) for p in policy.deny_read],
        },
    }


def write_request(command_line: str, cwd: Path, env: dict[str, str], policy, proxy: str | None = None) -> Path:
    """Requests live where no sandboxed command can write, so one command cannot rewrite another's policy."""
    folder = _home() / "sandbox" / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{os.getpid()}-{secrets.token_hex(8)}.json"
    path.write_text(json.dumps(request(command_line, cwd, env, policy, proxy), ensure_ascii=False), encoding="utf-8")
    return path
