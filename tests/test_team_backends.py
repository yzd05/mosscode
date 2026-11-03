"""Pane backends. wt and tmux are command doubles; the member process is real."""

import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from mosscode.config import ConfigError, load_settings
from mosscode.teams import LEAD, Member, TeamError, TeamStore
from mosscode.teams.backends import (IN_PROCESS, TMUX, WINDOWS_TERMINAL, TmuxBackend, WindowsTerminalBackend,
                                     detect_backend, escape_wt, kill_tree, member_alive, member_arguments,
                                     team_window)
from mosscode.teams.lock import now
from mosscode.teams.mailbox import Message
from mosscode.teams.runtime import PaneRunner
from mosscode.worktree.meta import process_alive
from test_teams import Script, done, team_conversation, until
from test_worktree import repo


def which(*found):
    return lambda name: f"/bin/{name}" if name in found else None


# Auto --------------------------------------------------------------------------------------

def test_auto_order_and_reasons():
    choice = detect_backend("auto", {"TMUX": "/tmp/s,1,0", "WT_SESSION": "x"}, which("tmux", "wt"))
    assert (choice.backend, choice.reason) == (TMUX, "auto：检测到 tmux")  # tmux first.
    choice = detect_backend("auto", {"TMUX": "/tmp/s,1,0"}, which("tmux"))
    assert (choice.backend, choice.reason) == (TMUX, "auto：检测到 tmux")
    choice = detect_backend("auto", {"TMUX": "/tmp/s,1,0", "WT_SESSION": "x"}, which("wt"))  # No tmux binary.
    assert (choice.backend, choice.reason) == (WINDOWS_TERMINAL, "auto：检测到 Windows Terminal")
    choice = detect_backend("auto", {"WT_SESSION": "x"}, which())  # No wt.exe.
    assert choice.backend == IN_PROCESS and choice.reason.startswith("auto：")
    choice = detect_backend("auto", {}, which("tmux", "wt"))
    assert choice.backend == IN_PROCESS and "不在 tmux 或 Windows Terminal 中" in choice.reason
    assert detect_backend("in-process", {"TMUX": "x"}, which("tmux")).backend == IN_PROCESS


async def test_auto_reason_in_the_result_and_team_detail(tmp_path):
    provider, conv = team_conversation(tmp_path, Script(alice=done))
    conv.teams.teammate_mode, conv.teams.env, conv.teams.which = "auto", {}, which()
    await conv.teams.create("t")
    text, details = await conv.teams.spawn("alice", "general", "做 A", isolation="none")
    assert details["backend"] == IN_PROCESS and "auto：不在 tmux 或 Windows Terminal 中" in text
    record = conv.teams.current.load().member("alice")
    assert record.backend == IN_PROCESS and record.backend_reason.startswith("auto：")
    assert "auto：不在 tmux 或 Windows Terminal 中" in conv.teams.detail("t")
    await conv.close()


# Explicit and missing ----------------------------------------------------------------------

@pytest.mark.parametrize("mode, env, found, missing", [
    ("tmux", {}, which("tmux"), "TMUX 为空"),
    ("tmux", {"TMUX": "x"}, which(), "找不到 tmux"),
    ("windows-terminal", {}, which("wt"), "WT_SESSION 为空"),
    ("windows-terminal", {"WT_SESSION": "x"}, which(), "找不到 wt.exe"),
])
def test_explicit_backend_unavailable(mode, env, found, missing):
    with pytest.raises(TeamError) as caught:
        detect_backend(mode, env, found)
    assert caught.value.code == "backend_unavailable" and missing in caught.value.message
    assert "没有改用其他后端" in caught.value.message


async def test_spawn_creates_nothing(tmp_path):
    project = repo(tmp_path)
    provider, conv = team_conversation(tmp_path, Script(alice=done))
    conv.teams.teammate_mode, conv.teams.env, conv.teams.which = "tmux", {}, which()
    await conv.teams.create("t")
    with pytest.raises(TeamError) as caught:
        await conv.teams.spawn("alice", "general", "做 A")
    assert caught.value.code == "backend_unavailable"
    assert conv.teams.current.load().members == [] and not conv.teams.runners
    assert not (project / ".mosscode" / "worktrees").exists()  # No worktree, no in-process member.
    await conv.close()


def test_config_values(tmp_path):
    def config(extra):
        path = tmp_path / "c.yaml"
        path.write_text("providers:\n  - {name: p, protocol: openai-compat, model: m, api_key: k}\n" + extra,
                        encoding="utf-8")
        return path
    settings = load_settings(config(""))
    assert (settings.teammate_mode, settings.coordinator_mode) == ("auto", False)
    settings = load_settings(config("teammate_mode: windows-terminal\ncoordinator_mode: true\n"))
    assert (settings.teammate_mode, settings.coordinator_mode) == ("windows-terminal", True)
    for bad in ("teammate_mode: iterm2\n", "coordinator_mode: yes-please\n", "teammate_mode: 1\n"):
        with pytest.raises(ConfigError):
            load_settings(config(bad))


# Command lines -----------------------------------------------------------------------------

def test_windows_terminal_command():
    backend = WindowsTerminalBackend(which=which("wt"), python="py")
    cwd = Path("work;tree")
    arguments = member_arguments(Path("teams") / "t", "alice", None, "deep;seek")
    command = backend.command(cwd, arguments, member="alice", team="t", window="mosscode-t-abc123")
    # A tab of the team's window, titled for the member; the lead's window is not split.
    assert command[:9] == ["/bin/wt", "-w", "mosscode-t-abc123", "new-tab", "--title", "alice · t",
                           "--suppressApplicationTitle", "-d", str(cwd).replace(";", "\\;")]
    assert command[9:12] == ["--", "py", "-m"] and command[12] == "mosscode.teams.pane"
    rest = command[13:]
    assert rest[:4] == ["--team-dir", str(Path("teams") / "t"), "--team-member", "alice"]
    assert rest[4:] == ["--provider", "deep\\;seek"]  # wt splits at a bare ;
    assert escape_wt("a;b;c") == "a\\;b\\;c"


def test_config_path_is_passed_never_a_key(tmp_path):
    config = tmp_path / "config.yaml"
    arguments = member_arguments(tmp_path / "t", "alice", config, "p")
    assert arguments == ["--team-dir", str(tmp_path / "t"), "--team-member", "alice", "--config",
                         str(config.resolve()), "--provider", "p"]


def test_tmux_command_pane_and_kill():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "%7 4242\n", "")
    backend = TmuxBackend(run, which("tmux"), python="py")
    command = backend.command(Path("/w"), ["--team-dir", "/t", "--team-member", "bob"], member="bob", team="t")
    # A window of its own, not switched to.
    assert command[:10] == ["/bin/tmux", "new-window", "-d", "-n", "bob", "-P", "-F", "#{pane_id} #{pane_pid}",
                            "-c", str(Path("/w"))]
    assert command[10] == "py -m mosscode --team-dir /t --team-member bob"
    assert backend.launch(Path("/w"), []) == "%7"
    backend.kill(None, "%7")
    assert calls[-1] == ["/bin/tmux", "kill-pane", "-t", "%7"]


def test_launch_failure_is_backend_failed():
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "no server running")
    with pytest.raises(TeamError) as caught:
        TmuxBackend(run, which("tmux")).launch(Path("."), [])
    assert caught.value.code == "backend_failed" and "no server running" in caught.value.message


# Doubles for a pane: a fake terminal that starts a stand-in process and writes its record -------------

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


class Terminal:
    """wt/tmux double. child(store, name) plays the member process; kills go to the real taskkill."""

    def __init__(self, child=None):
        self.child, self.calls, self.processes = child, [], []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == "taskkill":
            return subprocess.run(command, **kwargs)
        if "kill-pane" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        tmux = "new-window" in command
        if self.child is not None:
            words = shlex.split(command[-1]) if tmux else command
            arguments = words[words.index("--team-dir"):]
            store = TeamStore(Path(arguments[1]))
            self.child(self, store, arguments[3])
        return subprocess.CompletedProcess(command, 0, "%1 4242\n" if tmux else "", "")

    def start(self, store, name, **fields):
        process = subprocess.Popen(SLEEPER)
        self.processes.append(process)

        def write(team):
            member = team.member(name)
            member.pid, member.heartbeat, member.status = process.pid, now(), "idle"
            for key, value in fields.items():
                setattr(member, key, value)
        store.modify(write)
        return process

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def pane_conversation(tmp_path, terminal, mode="windows-terminal"):
    provider, conv = team_conversation(tmp_path, Script(alice=done))
    conv.teams.teammate_mode, conv.teams.run = mode, terminal
    conv.teams.env, conv.teams.which = {"WT_SESSION": "x", "TMUX": "x"}, which("wt", "tmux")
    conv.teams.start_timeout = 0.5
    return conv


# No pid in time ----------------------------------------------------------------------------

async def test_no_pid_is_backend_failed_and_stopped(tmp_path):
    repo(tmp_path)
    terminal = Terminal()
    conv = pane_conversation(tmp_path, terminal)
    await conv.teams.create("t")
    with pytest.raises(TeamError) as caught:
        await conv.teams.spawn("alice", "general", "做 A")
    assert caught.value.code == "backend_failed" and "没有启动" in caught.value.message
    assert "没有改用其他后端" in caught.value.message
    member = conv.teams.current.load().member("alice")
    assert member.status == "stopped" and member.pid is None and member.backend == WINDOWS_TERMINAL
    assert Path(member.worktree["path"]).is_dir() and not conv.teams.runners  # Kept, nothing in-process.
    assert "new-tab" in terminal.calls[0] and "split-pane" not in terminal.calls[0]
    await conv.close()


async def test_child_error_is_reported(tmp_path):
    def child(terminal, store, name):
        def write(team):
            team.member(name).error = "窗格中缺少环境变量 DEEPSEEK_API_KEY：设为用户环境变量，或改用 in-process"
        store.modify(write)
    conv = pane_conversation(tmp_path, Terminal(child))
    conv.teams.start_timeout = 10
    await conv.teams.create("t")
    started = time.monotonic()
    with pytest.raises(TeamError) as caught:
        await conv.teams.spawn("alice", "general", "做 A", isolation="none")
    assert time.monotonic() - started < 5  # The error ends the wait.
    assert caught.value.code == "backend_failed" and "缺少环境变量 DEEPSEEK_API_KEY" in caught.value.message
    assert conv.teams.current.load().member("alice").status == "stopped"
    await conv.close()


async def test_pane_member_forced_after_shutdown_request(tmp_path):
    terminal = Terminal(lambda terminal, store, name: terminal.start(store, name))
    conv = pane_conversation(tmp_path, terminal)
    notes = []
    conv.teams.on_note = notes.append
    try:
        await conv.teams.create("t")
        text, details = await conv.teams.spawn("alice", "general", "做 A", isolation="none")
        assert details["backend"] == WINDOWS_TERMINAL and "teammate_mode: windows-terminal" in text
        runner = conv.teams.runners["alice"]
        assert isinstance(runner, PaneRunner) and runner.status == "idle"
        detail = conv.teams.detail("t")
        window = team_window(conv.teams.current.directory)
        assert "Windows Terminal 标签页" in detail and f"在团队窗口 {window} 中" in detail
        assert conv.teams.current.load().member("alice").window == window
        assert any(f"在团队窗口 {window} 中" in note for note in notes)
        assert terminal.calls[0][terminal.calls[0].index("-w") + 1] == window
        store = conv.teams.current
        assert store.mailbox.peek("alice")[0].body == "做 A"  # It reads its own mailbox.
        process = terminal.processes[0]
        await conv.teams.stop_member("alice")  # The stand-in never answers.
        await until(lambda: process.poll() is not None, timeout=15)
        await until(lambda: any("已强制停止（Windows Terminal 标签页）" in note for note in notes))  # The last step.
        assert "alice" not in conv.teams.runners
        assert [m.type for m in store.mailbox.peek("alice")][-1] == "shutdown_request"
        member = store.load().member("alice")
        assert member.status == "stopped" and member.pid is None
        if os.name == "nt":
            assert any(call[:2] == ["taskkill", "/PID"] for call in terminal.calls)
    finally:
        terminal.cleanup()
        await conv.close()


async def test_lead_exit_ends_pane_processes(tmp_path):
    terminal = Terminal(lambda terminal, store, name: terminal.start(store, name))
    conv = pane_conversation(tmp_path, terminal, mode="tmux")
    try:
        await conv.teams.create("t")
        await conv.teams.spawn("alice", "general", "做 A", isolation="none")
        store = conv.teams.current
        process = terminal.processes[0]
        await conv.close()
        await until(lambda: process.poll() is not None, timeout=15)
        team = store.load()
        assert team.lead is None and team.member("alice").status == "stopped"
        member = team.member("alice")
        assert member.pid is None and store.mailbox.peek("alice")[-1].type == "shutdown_request"  # Asked first.
        assert ["/bin/tmux", "kill-pane", "-t", "%1"] in terminal.calls  # Tmux closes the pane too.
    finally:
        terminal.cleanup()


async def test_live_pane_member_is_kept_on_reopen(tmp_path):
    terminal = Terminal()
    conv = pane_conversation(tmp_path, terminal)
    try:
        await conv.teams.create("t")
        await conv.teams.close()
        store = TeamStore(conv.teams.root / "t")
        live = subprocess.Popen(SLEEPER)
        terminal.processes.append(live)
        store.modify(lambda team: team.members.extend([
            Member("alice", "general", WINDOWS_TERMINAL, "auto：检测到 Windows Terminal", "running", pid=live.pid,
                   heartbeat=now()),
            Member("bob", "general", WINDOWS_TERMINAL, "", "idle", pid=live.pid, heartbeat="2020-01-01T00:00:00+00:00"),
            Member("carol", "general", IN_PROCESS, "", "idle")]))
        assert member_alive(live.pid, now()) and not member_alive(live.pid, "2020-01-01T00:00:00+00:00")
        text, details = await conv.teams.create("t")
        assert details["live"] == ["alice"] and "仍在窗格中运行的成员 alice" in text
        team = conv.teams.current.load()
        assert [m.status for m in team.members] == ["running", "stopped", "stopped"]
        assert isinstance(conv.teams.runners["alice"], PaneRunner) and set(conv.teams.runners) == {"alice"}
    finally:
        terminal.cleanup()
        await conv.close()


async def test_session_mode_is_written_for_pane_members(tmp_path):
    terminal = Terminal(lambda terminal, store, name: terminal.start(store, name))
    conv = pane_conversation(tmp_path, terminal)
    try:
        await conv.teams.create("t")
        await conv.teams.spawn("alice", "general", "做 A", isolation="none")
        store = conv.teams.current
        assert store.load().member("alice").mode == "bypassPermissions"
        conv.registry.permissions.set_mode("plan")
        await until(lambda: store.load().member("alice").mode == "plan")
    finally:
        terminal.cleanup()
        await conv.close()


# The member process itself -----------------------------------------------------------------

DRIVER = '''
import re, sys
import mosscode.providers as providers
import mosscode.teams.process as process
from mosscode.messages import Completed, ContentBlock, Message, ToolCall
import json

process.HEARTBEAT = 0.3


class Scripted:
    def __init__(self, config):
        self.turn = 0

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        last = messages[-1]
        if last.role == "user" and "停止请求" in last.text:
            request = re.search(r"停止请求 · id=([^\\]]+)\\]", last.text).group(1)
            call = ToolCall("c2", "send_message", json.dumps({"to": "lead", "type": "shutdown_response",
                                                              "request_id": request, "approve": True}))
        elif last.role == "user":
            call = ToolCall("c1", "send_message", json.dumps({"to": "lead", "body": "登录做完了", "summary": "完成"}))
        else:
            call = None
        if call is not None:
            yield Completed(Message("assistant", (), tool_calls=(call,)), "tool_calls")
        else:
            yield Completed(Message("assistant", (ContentBlock("text", "好"),)), "stop")

    async def close(self):
        pass


providers.create_provider = lambda config: Scripted(config)
from mosscode.cli import main
sys.argv = ["mosscode", *sys.argv[1:]]
main()
'''


def child_env(tmp_path):
    env = dict(os.environ)
    home = tmp_path / "child-home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    env.pop("MOSSCODE_COORDINATOR_MODE", None)
    return env


def member_setup(tmp_path, key_line="api_key: test-only"):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("providers:\n  - name: p\n    protocol: openai-compat\n    model: m\n    " + key_line + "\n",
                      encoding="utf-8")
    store = TeamStore(tmp_path / "teams" / "t")
    store.create()
    store.acquire(os.getpid(), "", str(project))
    store.modify(lambda team: team.members.append(
        Member("alice", "general", WINDOWS_TERMINAL, "test", "stopped", transcript="transcripts/alice.jsonl",
               mode="bypassPermissions")))
    store.mailbox.send("alice", Message(LEAD, "text", "实现登录", "初始任务"))
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    command = [sys.executable, str(driver), "--team-dir", str(store.directory), "--team-member", "alice",
               "--config", str(config)]
    return project, store, command


def wait_for(predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.1)


def test_member_process_runs_a_turn_and_stops_on_request(tmp_path):
    project, store, command = member_setup(tmp_path)
    child = subprocess.Popen(command, cwd=project, env=child_env(tmp_path), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(lambda: store.load().member("alice").pid or child.poll() is not None)
        assert child.poll() is None, child.communicate()[1].decode("utf-8", "replace")
        # Its turn: read the mailbox, reply with send_message, an idle notification after.
        wait_for(lambda: any(m.type == "idle_notification" for m in store.mailbox.peek(LEAD)))
        lead = store.mailbox.peek(LEAD)
        assert [(m.type, m.body) for m in lead if m.type == "text"] == [("text", "登录做完了")]
        assert store.mailbox.peek("alice")[0].read
        first = store.load().member("alice").heartbeat
        wait_for(lambda: store.load().member("alice").heartbeat != first, timeout=10)  # Heartbeat.
        member = store.load().member("alice")
        assert member.status == "idle" and member_alive(member.pid, member.heartbeat)
        assert (store.directory / "transcripts" / "alice.jsonl").is_file()
        # A shutdown_request: it answers and exits; the pane would close.
        store.mailbox.send("alice", Message(LEAD, "shutdown_request", extra={"reason": "测试结束"}))
        assert child.wait(timeout=30) == 0
        answers = [m for m in store.mailbox.peek(LEAD) if m.type == "shutdown_response"]
        assert len(answers) == 1 and answers[0].extra["approve"] is True
        member = store.load().member("alice")
        assert member.status == "stopped" and member.pid is None
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


def test_missing_environment_variable(tmp_path):
    project, store, command = member_setup(tmp_path, "api_key_env: MOSSCODE_TEST_MISSING_KEY")
    env = child_env(tmp_path)
    env.pop("MOSSCODE_TEST_MISSING_KEY", None)
    done = subprocess.run(command, cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert done.returncode == 1
    member = store.load().member("alice")
    assert member.error == "窗格中缺少环境变量 MOSSCODE_TEST_MISSING_KEY：设为用户环境变量，或改用 in-process"
    assert member.pid is None and member.status == "stopped"
    assert "MOSSCODE_TEST_MISSING_KEY" in done.stderr.decode("utf-8", "replace")


def test_pane_wrapper_always_exits_zero(tmp_path):
    done = subprocess.run([sys.executable, "-m", "mosscode.teams.pane", "--team-dir", str(tmp_path / "none"),
                           "--team-member", "alice"], capture_output=True, timeout=60, env=child_env(tmp_path))
    assert done.returncode == 0  # The member failed (no team), the pane still closes.


def test_member_arguments_are_hidden_from_help():
    done = subprocess.run([sys.executable, "-m", "mosscode", "--help"], capture_output=True, timeout=60)
    assert b"--team-dir" not in done.stdout and b"--team-member" not in done.stdout


@pytest.mark.skipif(os.name != "nt", reason="taskkill is Windows-only")
def test_kill_tree_ends_the_process_and_its_children(tmp_path):
    parent = subprocess.Popen([sys.executable, "-c",
                               "import subprocess, sys, time; "
                               "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
                               "time.sleep(120)"])
    try:
        time.sleep(1.0)
        kill_tree(parent.pid)
        assert parent.wait(timeout=15) != 0
        assert not process_alive(parent.pid)
    finally:
        if parent.poll() is None:
            parent.kill()


# The member's own window ----------------------------------------------------------------------

class StubProcess:
    name, team = "alice", "t"

    def __init__(self):
        from types import SimpleNamespace
        self.on_event = self.on_approval = self.runner = None
        self.exit_code, self.answered = 0, []
        self.release = asyncio.Event()
        respond = lambda request_id, choice: self.answered.append((request_id, choice))  # noqa: E731
        self.conversation = SimpleNamespace(registry=SimpleNamespace(permissions=SimpleNamespace(respond=respond)))

    def answered_here(self):
        pass

    async def serve(self):
        from mosscode.permissions import ApprovalRequest, Operation
        self.on_event([Message(LEAD, "text", "实现登录", "初始任务")])
        self.on_approval(ApprovalRequest("r1", "c1", Operation("execute_command", "npm test"), "需要确认"))
        self.on_approval(ApprovalRequest("r2", "c2", Operation("execute_command", "npm run lint"), "需要确认"))
        await self.release.wait()
        return 0


async def test_member_window_answers_approvals_and_refuses_new_tasks():
    from textual.widgets import RichLog, Static
    from mosscode.teams.pane_app import INPUT_HINT, MemberApp
    process = StubProcess()
    app = MemberApp(process)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.title == "成员 alice · 团队 t"
        assert str(app.query_one("#title", Static).render()) == "成员 alice · 团队 t"
        assert app.query_one("#approval").display
        app.query_one("#input").focus()
        await pilot.press("1", "enter")  # The first request, its first choice.
        assert process.answered == [("r1", "once")]
        await pilot.press("escape")  # Esc refuses the next one.
        assert process.answered[-1] == ("r2", "deny") and not app.query_one("#approval").display
        app.query_one("#input").focus()
        await pilot.press("h", "i", "enter")  # Not a new task.
        lines = "".join(str(line.text) for line in app.query_one("#log", RichLog).lines).replace(" ", "")  # Wrapped rows.
        assert "←lead·消息：初始任务" in lines and INPUT_HINT.replace(" ", "") in lines
        process.release.set()
        await pilot.pause()
    assert process.exit_code == 0


async def test_interrupt_ends_the_turn_and_tells_the_lead(tmp_path):
    from test_agent import call, response
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'

    def slow(messages, tools):
        if messages[-1].role == "tool":
            return response("不该到这里")
        return response(calls=[call("execute_command", "c1", command=command)])
    provider, conv = team_conversation(tmp_path, Script(alice=slow))
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    runner = conv.teams.runners["alice"]
    await until(lambda: runner.last_tool)  # The command is running.
    assert runner.interrupt()
    await until(lambda: conv.notifications)
    update = "\n".join(conv.take_notifications())
    assert "alice 被用户中断" in update and "用户在成员窗格中中断了这一轮" in update
    assert not runner.interrupt()  # Nothing left to interrupt: it is idle.
    await conv.close()
