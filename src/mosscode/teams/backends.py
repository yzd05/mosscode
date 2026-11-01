"""Ch15 BE1–BE7: where a member runs — in the lead's process, or in a terminal pane of its own.

A pane member is another MossCode process (`--team-dir/--team-member`). The lead starts it, waits for
it to write its pid into the team record, and later reads its status and heartbeat from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
from typing import Callable, Mapping

from ..worktree.meta import process_alive
from .lock import TeamError

IN_PROCESS, WINDOWS_TERMINAL, TMUX = "in-process", "windows-terminal", "tmux"
LABELS = {IN_PROCESS: "进程内", WINDOWS_TERMINAL: "Windows Terminal 窗格", TMUX: "tmux 窗格"}
IN_PROCESS_REASON = "进程内运行，与 Lead 共用这个窗口"
START_TIMEOUT = 20.0  # BE6: seconds for a pane member to write its pid.
HEARTBEAT = 5.0  # BE6: seconds between a pane member's heartbeats.
FRESH = 15.0  # ID3: a heartbeat at most this old means the member is still at work.
LAUNCH_TIMEOUT = 15.0  # Seconds for wt/tmux itself to return.

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Choice:
    backend: str
    reason: str


def _tmux_missing(env: Mapping[str, str], which) -> str:
    if not env.get("TMUX"):
        return "MossCode 不在 tmux 会话中（环境变量 TMUX 为空）"
    if not which("tmux"):
        return "找不到 tmux 命令"
    return ""


def _wt_missing(env: Mapping[str, str], which) -> str:
    if not env.get("WT_SESSION"):
        return "MossCode 不在 Windows Terminal 中运行（环境变量 WT_SESSION 为空）"
    if not which("wt"):
        return "找不到 wt.exe"
    return ""


def _but(missing: str) -> str:
    return ("但 " if missing[:1].isascii() else "但") + missing  # 但 MossCode…, 但找不到…


def detect_backend(mode: str, env: Mapping[str, str] | None = None, which=shutil.which) -> Choice:
    """BE2/BE3: auto picks tmux, then Windows Terminal, then in-process; an explicit pane backend that
    cannot run is an error, never a quiet fallback."""
    env = os.environ if env is None else env
    if mode == IN_PROCESS:
        return Choice(IN_PROCESS, "teammate_mode: in-process；" + IN_PROCESS_REASON)
    if mode == TMUX:
        missing = _tmux_missing(env, which)
        if missing:
            raise TeamError("backend_unavailable", f"teammate_mode 是 tmux，{_but(missing)}；没有改用其他后端。"
                                                   "在 tmux 中启动 MossCode，或把 teammate_mode 改为 auto / in-process。")
        return Choice(TMUX, "teammate_mode: tmux")
    if mode == WINDOWS_TERMINAL:
        missing = _wt_missing(env, which)
        if missing:
            raise TeamError("backend_unavailable", f"teammate_mode 是 windows-terminal，{_but(missing)}；没有改用其他后端。"
                                                   "在 Windows Terminal 中启动 MossCode，或把 teammate_mode 改为 auto / in-process。")
        return Choice(WINDOWS_TERMINAL, "teammate_mode: windows-terminal")
    if not _tmux_missing(env, which):
        return Choice(TMUX, "auto：检测到 tmux")
    if not _wt_missing(env, which):
        return Choice(WINDOWS_TERMINAL, "auto：检测到 Windows Terminal")
    return Choice(IN_PROCESS, "auto：不在 tmux 或 Windows Terminal 中；" + IN_PROCESS_REASON)


def member_arguments(team_dir: Path, name: str, config: str | Path | None = None,
                     provider: str | None = None) -> list[str]:
    """BE5: what follows `python -m mosscode`; the configuration by path, never a key."""
    arguments = ["--team-dir", str(team_dir), "--team-member", name]
    if config:
        arguments += ["--config", str(Path(config).resolve())]
    if provider:
        arguments += ["--provider", provider]
    return arguments


def heartbeat_age(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def member_alive(pid: int | None, heartbeat: str | None, fresh: float = FRESH) -> bool:
    """ID3: its process runs and it reported recently."""
    age = heartbeat_age(heartbeat)
    return bool(pid) and process_alive(int(pid)) and age is not None and age <= fresh


def kill_tree(pid: int, run: Run = subprocess.run) -> None:
    """BE7: the member and whatever it started."""
    if not pid or pid == os.getpid():
        return
    if os.name == "nt":
        run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=LAUNCH_TIMEOUT)
        return
    try:
        group = os.getpgid(pid)
    except OSError:
        return
    try:
        if group != os.getpgid(0):
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class PaneBackend:
    name = ""

    def __init__(self, run: Run = subprocess.run, which=shutil.which, python: str | None = None):
        self.run, self.which = run, which
        self.python = python or sys.executable

    def command(self, cwd: Path, arguments: list[str]) -> list[str]:
        raise NotImplementedError

    def launch(self, cwd: Path, arguments: list[str]) -> str | None:
        """Start the member's pane; the pane id when the terminal reports one."""
        command = self.command(cwd, arguments)
        try:
            done = self.run(command, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TeamError("backend_failed", f"无法启动{LABELS[self.name]}：{exc}") from None
        if done.returncode != 0:
            output = ((done.stderr or "") + (done.stdout or "")).strip()
            raise TeamError("backend_failed", f"{LABELS[self.name]}启动失败（退出码 {done.returncode}）"
                                              + (f"：{output}" if output else "。"))
        return self.pane(done.stdout or "")

    def pane(self, output: str) -> str | None:
        return None

    def kill(self, pid: int | None, pane: str | None) -> None:
        if pid:
            try:
                kill_tree(int(pid), self.run)
            except (OSError, subprocess.SubprocessError):
                pass


class WindowsTerminalBackend(PaneBackend):
    """E2: `-w 0` is the window MossCode runs in. E3/E4: the pane runs mosscode.teams.pane, which always
    exits 0, so the pane closes after a normal exit and after taskkill alike."""

    name = WINDOWS_TERMINAL

    def command(self, cwd: Path, arguments: list[str]) -> list[str]:
        wt = self.which("wt") or "wt"
        rest = ["-w", "0", "split-pane", "-d", str(cwd), "--", self.python, "-m", "mosscode.teams.pane", *arguments]
        return [wt, *(escape_wt(item) for item in rest)]


def escape_wt(argument: str) -> str:
    """wt splits its command line at every `;` (a new subcommand); `\\;` is a literal one."""
    return argument.replace(";", "\\;")


class TmuxBackend(PaneBackend):
    name = TMUX

    def command(self, cwd: Path, arguments: list[str]) -> list[str]:
        tmux = self.which("tmux") or "tmux"
        return [tmux, "split-window", "-h", "-P", "-F", "#{pane_id} #{pane_pid}", "-c", str(cwd),
                shlex.join([self.python, "-m", "mosscode", *arguments])]

    def pane(self, output: str) -> str | None:
        parts = output.strip().split()
        return parts[0] if parts else None

    def kill(self, pid: int | None, pane: str | None) -> None:
        super().kill(pid, pane)
        if pane:
            try:
                self.run([self.which("tmux") or "tmux", "kill-pane", "-t", pane], capture_output=True,
                         timeout=LAUNCH_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                pass


def pane_backend(name: str, run: Run = subprocess.run, which=shutil.which, python: str | None = None) -> PaneBackend:
    backend = {WINDOWS_TERMINAL: WindowsTerminalBackend, TMUX: TmuxBackend}[name]
    return backend(run, which, python)
