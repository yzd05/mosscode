"""Background commands: run_in_background, ctrl+b, limits, notifications and who they end with."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from mosscode.agents.tasks import TaskManager, task_line
from mosscode.messages import NOTIFICATION
from mosscode.permissions import BACKGROUND_COMMAND_REASON, PermissionPolicy
from mosscode.tools import create_default_registry, output_text
from mosscode.tools.background import BackgroundCommands, CommandLog, notification, report
from mosscode.tools.output_text import DOUBLE_BYTE, console_encoding
from test_agent import call, response
from test_subagents import ScriptProvider, agents_dir, conversation, settle, write_agent
from test_tools import GBK_BYTES, GBK_LINE, code_page_script, process_alive, python_command
from waiting import until

SLEEPER = "import sys,time; print('ready', flush=True); time.sleep(60)"


def setup(tmp_path, *, limit=8, time_limit=None):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    tasks = TaskManager()
    session = SimpleNamespace(directory=tmp_path / "session", ensure=lambda: None)
    commands = BackgroundCommands(tasks, limit=limit, time_limit=time_limit, session=lambda: session)
    registry.commands = commands
    registry.get("execute_command").allow_background(time_limit)
    return registry, tasks, commands


def run(**arguments):
    return call("execute_command", "c1", **arguments)


async def finished(tasks, task_id):
    task = tasks.get(task_id)
    await asyncio.wait({task.job}, timeout=20)
    return task


async def test_starts_at_once_and_its_output_goes_to_the_log(tmp_path):
    registry, tasks, _ = setup(tmp_path)
    script = "import sys,time; print('first', flush=True); time.sleep(1); print('second'); sys.exit(3)"
    result = await registry.execute(run(command=python_command(script), run_in_background=True, description="测试"))
    assert not result.is_error, result
    assert result.details["task"] == "t1" and "已在后台启动命令：任务 t1" in result.output
    task = tasks.get("t1")
    log = task.command.log.path
    assert log.parent == tmp_path / "session" / "commands" and str(log) in result.output
    assert task.status == "running" and task.background and task.label == "命令 · 测试"
    await until(lambda: "first" in log.read_text(encoding="utf-8"))  # Readable while it runs.
    await finished(tasks, "t1")
    assert task.status == "failed" and task.command.exit_code == 3
    text = log.read_text(encoding="utf-8")
    assert "second" in text and "命令结束：退出码 3" in text
    assert task_line(task) == "后台任务 t1（命令 · 测试）失败，退出码 3"
    message = notification(task)
    assert "状态：失败，退出码 3" in message and "second" in message and str(log) in message
    assert "命令结束" not in message  # The tail was taken before MossCode's own lines.
    assert "最后 50 行输出" in report(task) and "退出码 3" in report(task)


async def test_a_clean_exit_completes(tmp_path):
    registry, tasks, _ = setup(tmp_path)
    await registry.execute(run(command=python_command("print('ok')"), run_in_background=True))
    task = await finished(tasks, "t1")
    assert task.status == "completed" and task.result == "退出码 0"
    assert len(task.description) == 40 and task.description.endswith("…")  # The command, cut short.


async def test_task_stop_ends_the_whole_tree(tmp_path):
    registry, tasks, _ = setup(tmp_path)
    marker = tmp_path / "pid.txt"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    await registry.execute(run(command=python_command(script), run_in_background=True))
    await until(marker.exists)
    pid = int(marker.read_text())
    assert process_alive(pid)
    assert await tasks.stop("t1")
    task = tasks.get("t1")
    assert task.status == "cancelled" and task.result == "[命令已停止]" and task.notified
    assert "命令已被停止" in task.command.log.path.read_text(encoding="utf-8")
    await until(lambda: not process_alive(pid), message=f"child process {pid} still alive")


async def test_the_limit_refuses_one_more(tmp_path):
    registry, tasks, _ = setup(tmp_path, limit=1)
    assert not (await registry.execute(run(command=python_command(SLEEPER), run_in_background=True))).is_error
    refused = await registry.execute(run(command=python_command(SLEEPER), run_in_background=True))
    assert refused.code == "too_many_tasks" and "task_stop" in refused.output
    assert len(tasks.tasks) == 1
    await tasks.cancel_all()


async def test_commands_do_not_count_against_the_sub_agent_limit(tmp_path):
    registry, tasks, _ = setup(tmp_path)
    tasks.max_background = 1
    await registry.execute(run(command=python_command(SLEEPER), run_in_background=True))
    assert not tasks.full()
    await tasks.cancel_all()


async def test_time_limit_ends_it(tmp_path):
    registry, tasks, _ = setup(tmp_path, time_limit=0.5)
    result = await registry.execute(run(command=python_command(SLEEPER), run_in_background=True))
    assert "时间限制 0 秒" in result.output or "时间限制 1 秒" in result.output
    task = await finished(tasks, "t1")
    assert task.status == "failed" and "超过时间限制" in task.result and task.command.end_reason == "time_limit"


async def test_its_own_timeout_is_the_limit_in_the_background(tmp_path):
    registry, tasks, _ = setup(tmp_path)
    await registry.execute(run(command=python_command(SLEEPER), run_in_background=True, timeout=0.5))
    task = await finished(tasks, "t1")
    assert task.command.end_reason == "time_limit"


async def test_output_over_the_limit_ends_it(tmp_path, monkeypatch):
    monkeypatch.setattr("mosscode.tools.background.LOG_LIMIT", 10000)
    registry, tasks, _ = setup(tmp_path)
    script = "import time\nwhile True:\n    print('x'*1000, flush=True)\n    time.sleep(0.001)"
    await registry.execute(run(command=python_command(script), run_in_background=True))
    task = await finished(tasks, "t1")
    assert task.status == "failed" and task.command.end_reason == "output_limit"
    assert task.command.log.size >= 10000 and "输出超过" in task.result


async def test_timeouts_foreground_and_background(tmp_path):
    registry, _, _ = setup(tmp_path)
    tool = registry.get("execute_command")
    assert tool.validate_arguments({"command": "x"}).timeout == 120
    assert tool.validate_arguments({"command": "x", "run_in_background": True}).timeout is None
    assert tool.validate_arguments({"command": "x", "run_in_background": True, "timeout": 3600}).timeout == 3600
    refused = await registry.execute(run(command="x", timeout=601))
    assert refused.code == "invalid_arguments" and "run_in_background" in refused.output
    assert (await registry.execute(run(command="x", run_in_background=True, timeout=3601))).code == "invalid_arguments"
    schema = next(d for d in registry.definitions() if d.name == "execute_command")
    assert "run_in_background" in str(schema)


async def test_without_background_commands_the_parameter_is_absent(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    schema = next(d for d in registry.definitions() if d.name == "execute_command")
    assert "run_in_background" not in str(schema)
    assert (await registry.execute(run(command="x", run_in_background=True))).code == "invalid_arguments"


async def test_ctrl_b_moves_a_running_command_to_the_background(tmp_path):
    registry, tasks, commands = setup(tmp_path)
    script = "import sys,time; print('before', flush=True); print('err', file=sys.stderr, flush=True); time.sleep(60)"
    running = asyncio.create_task(registry.execute(run(command=python_command(script), timeout=60)))
    await until(lambda: commands.foreground_count() == 1)
    await asyncio.sleep(1)  # Let the first lines arrive.
    assert commands.detach_foreground() == 1
    result = await asyncio.wait_for(running, 5)
    assert not result.is_error and result.details["moved"]
    assert (
        "用户按 Ctrl+B 把命令转入了后台：任务 t1" in result.output
        and "不要用 sleep 或 task_output 的 wait" in result.output
    )
    task = tasks.get("t1")
    assert task.status == "running" and commands.foreground_count() == 0
    text = task.command.log.path.read_text(encoding="utf-8")
    assert "before" in text and "stderr:\nerr" in text and "命令已转入后台" in text
    await tasks.stop("t1")


async def background_code_page_output(tmp_path, encoding: str, sample: str):
    registry, tasks, _ = setup(tmp_path)
    data = (sample + "\r\n").encode(encoding) * 3
    await registry.execute(run(command=python_command(code_page_script(data, "done\n")), run_in_background=True))
    task = await finished(tasks, "t1")
    # Strict UTF-8, as read_file reads it; MossCode's own lines follow in the same encoding.
    text = task.command.log.path.read_bytes().decode("utf-8")
    assert text.startswith((sample + "\r\n") * 3 + "done\n") and "命令结束：退出码 0" in text
    assert sample in notification(task) and sample in report(task) and "�" not in notification(task)


async def test_code_page_output_reaches_the_log_and_notification_as_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(output_text, "console_encoding", lambda: "gbk")
    await background_code_page_output(tmp_path, "gbk", GBK_LINE.strip())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console code pages")
async def test_the_windows_console_code_page_reaches_the_log_as_utf8(tmp_path):
    encoding = console_encoding()
    if encoding is None:
        pytest.skip("the OEM code page is UTF-8")
    sample = "正在 Ping 具有 32 字节的数据" if encoding in DOUBLE_BYTE else "Résumé, naïve"
    try:
        sample.encode(encoding)
    except UnicodeEncodeError:
        pytest.skip(f"{encoding} cannot encode the sample")
    await background_code_page_output(tmp_path, encoding, sample)


async def test_ctrl_b_keeps_code_page_output_readable_across_the_move(tmp_path, monkeypatch):
    monkeypatch.setattr(output_text, "console_encoding", lambda: "gbk")
    registry, tasks, commands = setup(tmp_path)
    marker, later = tmp_path / "go", "正在 Ping 具有 32 字节的数据"
    script = (
        f"import os,sys,time\nout=sys.stdout.buffer\nout.write({GBK_BYTES!r}); out.flush()\n"
        f"while not os.path.exists({str(marker)!r}):\n    time.sleep(0.05)\n"
        f"out.write({(later + chr(10)).encode('gbk')!r}); out.flush(); time.sleep(60)"
    )
    running = asyncio.create_task(registry.execute(run(command=python_command(script), timeout=60)))
    await until(lambda: commands.foreground_count() == 1)
    await asyncio.sleep(1)  # Let the first line arrive.
    assert commands.detach_foreground() == 1
    assert (await asyncio.wait_for(running, 5)).details["moved"]
    marker.write_text("")
    log = tasks.get("t1").command.log.path
    await until(lambda: later in log.read_bytes().decode("utf-8"))
    text = log.read_bytes().decode("utf-8")
    assert text.startswith(GBK_LINE) and "命令已转入后台" in text and "�" not in text
    await tasks.stop("t1")


async def test_a_stopped_commands_unfinished_line_still_reaches_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(output_text, "console_encoding", lambda: "gbk")
    registry, tasks, _ = setup(tmp_path)
    prompt = "按 q 退出"  # No line break: the decoder holds it until the line ends or the stream does.
    script = f"import sys,time; sys.stdout.buffer.write({prompt.encode('gbk')!r}); sys.stdout.flush(); time.sleep(60)"
    await registry.execute(run(command=python_command(script), run_in_background=True))
    await asyncio.sleep(1.5)  # Let the prompt arrive; it stays held in the decoder.
    log = tasks.get("t1").command.log.path
    assert prompt not in log.read_bytes().decode("utf-8")
    await tasks.stop("t1")
    text = log.read_bytes().decode("utf-8")
    assert text.startswith(prompt + "\n[MossCode] 命令已被停止。"), text


def test_last_lines_read_a_code_page_tail_cut_inside_a_character(tmp_path, monkeypatch):
    monkeypatch.setattr(output_text, "console_encoding", lambda: "gbk")
    # Two lines and all but the first byte of the one before: the kept tail starts inside 来.
    monkeypatch.setattr("mosscode.tools.background.KEEP", 3 * len(GBK_BYTES) - 1)
    log = CommandLog(tmp_path / "t1.log")
    for _ in range(20):
        log.write(GBK_BYTES)
    log.close()
    line = GBK_LINE.strip()
    assert log.last_lines(3, 2000) == f"{line[1:]}\n{line}\n{line}"


async def test_ctrl_b_leaves_a_sub_agents_command_alone(tmp_path):
    registry, _, commands = setup(tmp_path)
    registry.task_id = "t9"
    running = asyncio.create_task(registry.execute(run(command=python_command("print('x')"), timeout=30)))
    result = await asyncio.wait_for(running, 20)
    assert not result.is_error and commands.foreground_count() == 0


async def test_stop_owned_ends_only_that_owners_commands(tmp_path):
    registry, tasks, commands = setup(tmp_path)
    await registry.execute(run(command=python_command(SLEEPER), run_in_background=True))
    registry.task_id = "t9"
    await registry.execute(run(command=python_command(SLEEPER), run_in_background=True))
    await commands.stop_owned("t9")
    assert tasks.get("t2").status == "cancelled" and tasks.get("t1").status == "running"
    await tasks.cancel_all()


async def test_background_network_requests_ask_nobody(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    policy.interactive = True
    decision = await policy.connect("c", "example.com", 443, "curl", background=True)
    assert decision.action == "deny" and decision.reason == BACKGROUND_COMMAND_REASON
    assert policy.requests.empty()


# Through the conversation ------------------------------------------------------------------


def background_script(command, *, agent_command=None):
    """The main agent starts one background command; a notification gets a reply."""

    def script(messages, system, tools):
        last = messages[-1]
        if "SUB-W" in system.stable:
            if last.role == "user":
                return response(calls=[call("execute_command", "s1", command=agent_command, run_in_background=True)])
            return response("子 Agent 完成")
        if last.role == "user" and last.synthetic == NOTIFICATION:
            return response("收到通知")
        if last.role == "user":
            if agent_command is not None:
                return response(calls=[call("agent", "a1", description="子任务", prompt="p", agent_type="w")])
            return response(calls=[call("execute_command", "c1", command=command, run_in_background=True)])
        return response("已启动")

    return ScriptProvider(script)


async def test_headless_waits_and_the_notification_starts_a_turn(tmp_path):
    from mosscode.cli import _turns

    provider = background_script(python_command("import sys; print('done'); sys.exit(3)"))
    conv = conversation(tmp_path, provider)
    async for _ in _turns(conv, "开始", command=None, fork=None):
        pass
    last = provider.requests[-1][0][-1]
    assert last.synthetic == NOTIFICATION and "退出码 3" in last.text and "done" in last.text
    assert conv.history[-1].text == "收到通知" and not conv.tasks.running()
    log = conv.tasks.get("t1").command.log.path
    assert log.parent == conv.session.directory / "commands"
    records = conv.session.transcript_path.read_text(encoding="utf-8")
    assert '"origin":"command"' in records and '"exit_code":3' in records
    await conv.close()


async def test_headless_time_limit_ends_a_command_that_never_ends(tmp_path):
    from mosscode.cli import _turns

    provider = background_script(python_command(SLEEPER))
    conv = conversation(tmp_path, provider, command_time_limit=0.5)
    async for _ in _turns(conv, "开始", command=None, fork=None):
        pass
    last = provider.requests[-1][0][-1]
    assert last.synthetic == NOTIFICATION and "超过时间限制" in last.text
    await conv.close()


async def test_a_sub_agents_command_ends_with_it_and_notifies_nobody(tmp_path):
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")
    provider = background_script("", agent_command=python_command(SLEEPER))
    conv = conversation(tmp_path, provider)
    async for _ in conv.reply("开始"):
        pass
    await settle(conv)
    command = conv.tasks.get("t2")
    assert command.kind == "command" and command.command.owner == "t1"
    assert command.status == "cancelled" and not conv.notifications
    await conv.close()


async def test_zero_turns_background_commands_off(tmp_path):
    conv = conversation(tmp_path, background_script("x"), command_max_background=0)
    assert conv.commands is None and conv.registry.commands is None
    schema = next(d for d in conv.registry.definitions() if d.name == "execute_command")
    assert "run_in_background" not in str(schema)
    await conv.close()


async def test_closing_the_conversation_ends_background_commands(tmp_path):
    conv = conversation(tmp_path, background_script(python_command(SLEEPER)))
    async for _ in conv.reply("开始"):
        pass
    task = conv.tasks.get("t1")
    assert task.status == "running" and "task_output" in conv.registry.names()
    await conv.close()
    assert task.status == "cancelled"


@pytest.mark.parametrize("value", [-1, 65, "8", True])
def test_config_rejects_bad_limits(tmp_path, value):
    import yaml

    from mosscode.config import ConfigError, load_settings
    from test_config import write_config

    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "command_max_background": value}), encoding="utf-8")
    with pytest.raises(ConfigError, match="command_max_background"):
        load_settings(path)


def test_config_default_and_zero(tmp_path):
    import yaml

    from mosscode.config import load_settings
    from test_config import write_config

    path = write_config(tmp_path)
    assert load_settings(path).command_max_background == 8
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "command_max_background": 0}), encoding="utf-8")
    assert load_settings(path).command_max_background == 0


async def test_tasks_command_lists_and_shows_a_command(tmp_path):
    from test_commands import FakeUI, context
    from test_commands import run as run_command

    conv = conversation(tmp_path, background_script(python_command(SLEEPER)))
    async for _ in conv.reply("开始"):
        pass
    await until(lambda: "ready" in conv.tasks.get("t1").command.log.path.read_text(encoding="utf-8"))
    ui = FakeUI()
    ctx = context(conv, ui=ui)
    await run_command(ctx, "/tasks")
    assert "t1" in ui.notes[-1] and "输出 " in ui.notes[-1] and "Token" not in ui.notes[-1]
    await run_command(ctx, "/tasks t1")
    detail = ui.notes[-1]
    assert "类型      命令" in detail and "不在沙箱中" in detail and "ready" in detail
    assert str(conv.tasks.get("t1").command.log.path) in detail and "/tasks stop t1" in detail
    assert "对话记录" not in detail and "Token" not in detail
    await conv.close()


async def test_tui_ctrl_b_moves_a_foreground_command(tmp_path):
    from mosscode.tui import MossCodeApp, ToolEntry
    from test_subagents import wait_until

    def script(messages, system, tools):
        if messages[-1].role == "user":
            return response(calls=[call("execute_command", "c1", command=python_command(SLEEPER), timeout=60)])
        return response("已转入后台")

    conv = conversation(tmp_path, ScriptProvider(script))
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._start_request("跑")
        await wait_until(pilot, lambda: conv.commands.foreground_count() == 1)
        await wait_until(pilot, lambda: "Ctrl+B" in str(app.query_one("#status").render()))
        shown, status = [], app._status
        app._status = lambda note: (shown.append(note), status(note))[1]
        await pilot.press("ctrl+b")
        assert shown[0] == "已把 1 个命令转入后台"  # The scripted reply ends too fast to read it off the bar.
        await wait_until(pilot, lambda: not app._task_running, limit=10)
        rows = [row for row in app.query(ToolEntry) if row.operation == "execute_command"]
        assert rows and "后台 · 任务 t1" in rows[0].transcript.plain
        assert conv.tasks.get("t1").status == "running" and conv.history[-1].text == "已转入后台"
        await conv.tasks.stop("t1")
    await conv.close()
