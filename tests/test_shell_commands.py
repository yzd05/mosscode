"""! commands: run from the prompt without approval, kept in the context for the next request."""

import asyncio

from mosscode.builtin_commands import BUILTIN
from mosscode.cli import _headless_input
from mosscode.commands import parse_input
from mosscode.messages import SHELL
from mosscode.permissions import parse_rule
from mosscode.session import load_session
from mosscode.tools import create_default_registry
from mosscode.user_command import ran
from test_agent import SequenceProvider, response
from test_sessions import drain, make


def registry(root, mode="default"):
    return create_default_registry(root, permission_mode=mode, load_permissions=False)


def conversation(tmp_path, provider=None, mode="default"):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return make(tmp_path, provider or SequenceProvider(response("好")), registry=registry(root, mode)), root


def test_a_leading_bang_is_a_command_and_two_keep_it_out_of_the_context():
    parsed = parse_input("!git status", BUILTIN)
    assert (parsed.kind, parsed.text, parsed.word) == ("shell", "git status", "!")
    assert parse_input("  !! ls -la ", BUILTIN).word == "!!" and parse_input("!!ls", BUILTIN).text == "ls"
    assert parse_input("!", BUILTIN).text == "" and parse_input("a !b", BUILTIN).kind == "message"
    assert _headless_input("!ls")[0] == "!ls"  # -p speaks to the model only.


async def test_the_output_joins_the_history_without_a_reply_and_reaches_the_next_request(tmp_path):
    provider = SequenceProvider(response("看到了"))
    conv, _ = conversation(tmp_path, provider)
    run = await conv.run_command("echo 你好")
    assert run.exit_code == 0 and run.output == "你好" and run.shell
    assert provider.requests == []  # Running a command starts no reply.
    assert [m.synthetic for m in conv.history] == [SHELL]
    assert conv.history[0].id is None and ran(conv.history[0]) == "echo 你好"
    await drain(conv.reply("刚才输出了什么"))
    sent = provider.requests[0][0]
    assert sent[0].synthetic == SHELL and "<output>\n你好\n</output>" in sent[0].text and "退出码 0" in sent[0].text
    assert sent[1].text == "刚才输出了什么"
    restored = load_session(conv.session.transcript_path)
    assert [m.synthetic for m in restored.history] == [SHELL, None, None] and restored.unanswered == ()
    await conv.close()


async def test_a_command_left_last_stays_in_the_history_after_a_resume(tmp_path):
    from mosscode.tui import resumed_rows

    conv, _ = conversation(tmp_path)
    run = await conv.run_command("exit 3")
    assert run.exit_code == 3 and "退出码 3" in conv.history[0].text
    restored = load_session(conv.session.transcript_path)
    assert [m.synthetic for m in restored.history] == [SHELL] and restored.unanswered == ()
    assert [str(row) for row in resumed_rows(restored.history)] == ["! exit 3"]
    await conv.close()


async def test_double_bang_runs_without_touching_the_history(tmp_path):
    conv, root = conversation(tmp_path)
    run = await conv.run_command("echo x > made.txt", keep=False)
    assert run.exit_code == 0 and (root / "made.txt").is_file()
    assert conv.history == ()
    await conv.close()


async def test_no_approval_even_in_plan_mode_but_the_blacklist_and_deny_rules_hold(tmp_path):
    conv, root = conversation(tmp_path, mode="plan")
    assert (await conv.run_command("echo x > plan.txt")).exit_code == 0 and (root / "plan.txt").is_file()
    refused = await conv.run_command("rm -rf /")
    assert refused.exit_code is None and "黑名单" in refused.problem
    conv.registry.permissions.session.append(parse_rule("Bash(echo *)", "deny"))
    denied = await conv.run_command("echo y > denied.txt")
    assert denied.exit_code is None and "显式拒绝" in denied.problem and not (root / "denied.txt").exists()
    assert conv.history[0].synthetic == SHELL and len(conv.history) == 1  # Refused commands are not kept.
    await conv.close()


async def test_the_prompt_runs_commands_and_esc_stops_one(tmp_path):
    from textual.widgets import Static

    from mosscode.tui import MossCodeApp, PromptArea, ShellRow

    conv, root = conversation(tmp_path)
    (root / "a.txt").write_text("x", encoding="utf-8")
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        prefix = app.query_one("#input-prefix", Static)
        field.value = "!cat @a"
        await pilot.pause()
        assert str(prefix.render()) == "!" and app._hint_choices == []  # No @ completion in a command.
        field.value = "!echo 屏幕"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await app._reply_task
        await pilot.pause()
        row = app.query(ShellRow).last()
        assert (
            "! echo 屏幕" in str(row.transcript)
            and "  屏幕" in str(row.transcript)
            and "退出码 0" in str(row.transcript)
        )
        assert str(prefix.render()) == "❯" and [m.synthetic for m in conv.history] == [SHELL]
        field.value = "!!echo 不留"
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        assert "未加入对话" in str(app.query(ShellRow).last().transcript) and len(conv.history) == 1
        field.value = "!"
        await pilot.press("enter")
        await pilot.pause()
        assert field.value == "!" and "在 ! 后面输入" in str(app.query_one("#status", Static).render())
        sleep = "Start-Sleep 30" if conv.registry.get("execute_command").shell.kind == "powershell" else "sleep 30"
        field.value = "!" + sleep
        await pilot.press("enter")
        await pilot.pause()
        field.value = "!echo 运行中"
        await pilot.press("enter")
        await pilot.pause(0.5)  # The running timer leaves the hint up.
        assert field.value == "!echo 运行中" and "运行中不能执行" in str(app.query_one("#status", Static).render())
        task = app._reply_task
        await pilot.press("escape")
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        await pilot.pause()
        assert "已取消" in str(app.query(ShellRow).last().transcript) and len(conv.history) == 1
    await conv.close()
