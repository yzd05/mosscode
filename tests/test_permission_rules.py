"""Permissions: read-only commands, prefix rules, accept-edits choice, diff previews, removed modes."""

import asyncio
import io

import pytest
import yaml
from rich.console import Console
from textual.widgets import Static

from mosscode.agent import AgentLoop
from mosscode.conversation import Conversation
from mosscode.messages import ProviderError
from mosscode.permissions import (COMMAND_CHOICES, EDIT_CHOICES, RISKY_CHOICES, Operation, PermissionConfigError,
                                  PermissionPolicy, normalize_mode, parse_rule)
from mosscode.security import command_prefix, read_only_command
from mosscode.tui import MossCodeApp, preview_text
from test_agent import SequenceProvider, call, response
from test_permission_tui import make_app, wait_approval
from test_permissions import approve, registry
from test_plan_commands import plan_registry, simulate
from test_tui import send


# Read-only commands -------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git status", "git status --short", "git diff HEAD~1 -- src", "git log --oneline -5", "git show HEAD:src/a.py",
    "git diff main..feature", "git branch", "git branch -a -v", "git remote -v", "git rev-parse HEAD",
    "ls", "ls -la src", "pwd", "cat README.md", "head -n 5 a.txt", "tail -n 20 log.txt", "wc -l src/a.py",
    "grep -rn TODO src", "rg -n foo --glob '*.py'", "find . -name '*.py'", "Get-ChildItem src", "gci -Recurse",
    "Get-Content README.md",
])
def test_read_only_commands(command):
    assert read_only_command(command)


@pytest.mark.parametrize("command", [
    "git commit -m x", "git push", "git branch -D old", "git branch new-feature", "git remote add x url",
    "git -c core.pager=evil log", "git diff --output=out.txt", "git log --ext-diff",
    "cat /etc/passwd", "cat ../secret", "cat ~/.ssh/id_rsa", "ls C:/Users", "head src/../../x",
    "grep -f /etc/passwd x", "rg --pre ./run.sh foo", "rg --pre=sh foo", "rg -z foo", "tail -f log.txt",
    "find . -delete", "find . -exec rm {} ;", "git status && rm a", "cat a | sh", "ls > out.txt",
    "cat $HOME/x", "echo hi", "python -V", "rm a", "",
])
def test_not_read_only_commands(command):
    assert not read_only_command(command)


@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
async def test_read_only_command_runs_without_approval_but_deny_rules_win(tmp_path, mode):
    reg = registry(tmp_path, mode)
    executed = simulate(reg)
    assert not (await reg.execute(call("execute_command", command="git status"))).is_error
    assert (await reg.execute(call("execute_command", command="git fetch"))).code == "approval_required"
    reg.permissions.layers[2] = (parse_rule("Bash(git status)", "deny"),)
    assert (await reg.execute(call("execute_command", command="git status"))).code == "permission_denied"
    assert [e["command"] for e in executed] == ["git status"]


async def test_plan_mode_runs_read_only_commands_only(tmp_path):
    reg = plan_registry(tmp_path, "bypassPermissions")
    executed = simulate(reg)
    assert "execute_command" in {d.name for d in reg.definitions(plan=True)}
    assert not (await reg.execute(call("execute_command", command="git log -3"))).is_error
    blocked = await reg.execute(call("execute_command", command="python -m pytest"))
    assert blocked.code == "plan_blocked" and "只读命令" in blocked.output
    assert (await reg.execute(call("execute_command", command="cat ../outside"))).code == "plan_blocked"
    assert [e["command"] for e in executed] == ["git log -3"]


# Prefix rules -------------------------------------------------------------------------------

def test_prefix_rule_matching():
    allow = parse_rule("Bash(pytest:*)", "allow")
    matches = lambda rule, command: rule.matches(Operation("execute_command", command, "."))
    assert matches(allow, "pytest") and matches(allow, "pytest tests/a.py -q")
    assert not matches(allow, "pytestx") and not matches(allow, "pytest && rm a") and not matches(allow, "py.test")
    deny = parse_rule("Bash(git push:*)", "deny")
    assert matches(deny, "git push") and matches(deny, "git status && git push origin main")
    assert matches(deny, "echo $(git push)") and not matches(deny, "git pushx") and not matches(deny, "git pull")
    multi = parse_rule("Bash(python -m pytest:*)", "allow")
    assert matches(multi, "python -m pytest tests") and not matches(multi, "python -m pip install x")


# Prefix derivation and the prefix choice ----------------------------------------------------

@pytest.mark.parametrize("command,prefix", [
    ("pytest tests/a.py -q", "pytest"), ("python -m pytest -q", "python -m pytest"),
    ("npm run test -- --watch", "npm run test"), ("git commit -m msg", "git commit"),
    ("cargo test --release", "cargo test"), ("make build", "make build"), ("mypy src", "mypy src"),
    ("python script.py", None), ("python -m http.server", None), ("node app.js", None), ("bash -c 'x'", None),
    ("env A=1 pytest", None), ("xargs rm", None), ("git -C x status", None), ("npm", None),
    ("pytest && rm a", None), ("sudo apt update", None), ("./run.sh", None),
])
def test_command_prefix(command, prefix):
    assert command_prefix(command) == prefix


async def test_prefix_choice_is_saved_and_reused_after_reload(tmp_path):
    reg = registry(tmp_path)
    executed = simulate(reg)
    result, request = await approve(reg, call("execute_command", command="pytest tests/a.py"), "prefix")
    assert request.choices == COMMAND_CHOICES and request.operation.prefix == "pytest" and not result.is_error
    data = yaml.safe_load(reg.permissions.local_file.read_text(encoding="utf-8"))
    assert data["allow"] == ["Bash(pytest:*)"]
    restored = registry(tmp_path, load=True, user=tmp_path / "absent.yaml")
    executed = simulate(restored)
    assert not (await restored.execute(call("execute_command", command="pytest tests/b.py -q"))).is_error
    assert (await restored.execute(call("execute_command", command="pytest; rm a"))).code == "approval_required"
    assert len(executed) == 1


async def test_risky_and_underivable_commands_offer_no_prefix(tmp_path):
    reg = registry(tmp_path)
    simulate(reg)
    _, request = await approve(reg, call("execute_command", command="git push --force"), "once")
    assert request.choices == RISKY_CHOICES and not request.operation.prefix
    _, request = await approve(reg, call("execute_command", command="python build.py"), "once")
    assert "prefix" not in request.choices
    task = asyncio.create_task(reg.execute(call("execute_command", command="python build.py")))
    pending = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    with pytest.raises(ValueError):
        reg.permissions.respond(pending.id, "prefix")
    reg.permissions.respond(pending.id, "deny")
    await task


# Accept edits from an approval, Shift+Tab ---------------------------------------------------

async def test_accept_edits_choice_switches_mode_for_later_edits_only(tmp_path):
    reg = registry(tmp_path)
    executed = simulate(reg)
    result, request = await approve(reg, call("write_file", path="a.txt", content="one"), "accept_edits")
    assert request.choices == EDIT_CHOICES and not result.is_error
    assert reg.permissions.mode == "acceptEdits" and not reg.permissions.session
    reg.permissions.interactive = False
    assert not (await reg.execute(call("write_file", path="b.txt", content="two"))).is_error
    assert (await reg.execute(call("execute_command", command="python build.py"))).code == "approval_required"
    assert (tmp_path / "b.txt").read_text() == "two" and not executed


async def test_shift_tab_cycles_modes_and_waits_while_running(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        yield response("完成")
    app, policy = make_app(tmp_path, SequenceProvider(slow))
    async with app.run_test(size=(100, 30)) as pilot:
        seen = []
        for _ in range(4):
            await pilot.press("shift+tab")
            seen.append(policy.mode)
        assert seen == ["acceptEdits", "plan", "default", "acceptEdits"]
        assert "⏵⏵ 接受编辑" in str(app.query_one("#status", Static).render())
        policy.set_mode("bypassPermissions")
        await pilot.press("shift+tab")
        assert policy.mode == "default"
        await send(app, pilot, "任务")
        await asyncio.wait_for(started.wait(), 2)
        await pilot.press("shift+tab")
        assert policy.mode == "default" and "运行中不能切换" in str(app.query_one("#status", Static).render())
        release.set()
        await asyncio.wait_for(app._reply_task, 3)


# Diff previews ------------------------------------------------------------------------------

async def test_edit_and_overwrite_previews_are_diffs(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    reg = registry(tmp_path)
    await reg.execute(call("read_file", path="a.py"))
    result, request = await approve(reg, call("edit_file", path="a.py", old_text="two", new_text="TWO"), "once")
    preview = request.operation.preview
    assert "--- a/a.py" in preview and "-two" in preview and "+TWO" in preview and not result.is_error
    _, request = await approve(reg, call("write_file", path="a.py", content="one\nTWO\nfour\n"), "deny")
    assert "-three" in request.operation.preview and "+four" in request.operation.preview
    _, request = await approve(reg, call("write_file", path="new.py", content="x = 1\n"), "deny")
    assert request.operation.preview.startswith("新建文件 new.py（1 行）") and "+x = 1" in request.operation.preview


async def test_preview_falls_back_and_truncates(tmp_path):
    (tmp_path / "a.py").write_text("one\n", encoding="utf-8")
    reg = registry(tmp_path)
    _, request = await approve(reg, call("edit_file", path="a.py", old_text="missing", new_text="x"), "deny")
    assert request.operation.preview.startswith("原文：\nmissing")
    _, request = await approve(reg, call("write_file", path="big.py", content="line\n" * 40), "deny")
    lines = request.operation.preview.splitlines()
    assert len(lines) == 18 and "共 40 行，仅显示前 16 行" in lines[-1]
    (tmp_path / "b.py").write_text("".join(f"{i}\n" for i in range(60)), encoding="utf-8")
    _, request = await approve(reg, call("write_file", path="b.py", content="x\n"), "deny")
    assert "仅显示前 16 行" in request.operation.preview.splitlines()[-1]


def test_diff_lines_are_colored():
    text = preview_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n context")
    styles = {span.style for span in text.spans}
    assert len(styles) == 3  # removed, added, headers
    plain = preview_text("原文：\n-not a diff")
    assert not plain.spans


async def test_tui_approval_shows_colored_diff_and_five_choices(tmp_path):
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    provider = SequenceProvider(response(calls=[call("read_file", "r", path="a.py")]),
                                response(calls=[call("edit_file", "e", path="a.py", old_text="old", new_text="new")]),
                                response("完成"))
    app, policy = make_app(tmp_path, provider)
    async with app.run_test(size=(100, 36)) as pilot:
        await send(app, pilot, "修改")
        await wait_approval(app, pilot)
        detail = app.query_one("#approval-detail", Static).render()
        console = Console(width=100, record=True, file=io.StringIO())
        console.print(detail)
        shown = console.export_text()
        assert "-old" in shown and "+new" in shown
        assert app.query_one("#approval-options").option_count == 5
        assert "本会话自动接受编辑" in str(app.query_one("#approval-options").get_option_at_index(2).prompt)
        await pilot.press("down", "down", "enter")
        await asyncio.wait_for(app._reply_task, 3)
        assert (tmp_path / "a.py").read_text() == "new\n" and policy.mode == "acceptEdits"


# Removed modes, single mode state -----------------------------------------------------------

@pytest.mark.parametrize("removed,replacement", [("strict", "default"), ("permissive", "bypassPermissions")])
def test_removed_modes_fail_with_replacement(tmp_path, removed, replacement):
    with pytest.raises(PermissionConfigError, match=replacement):
        normalize_mode(removed)
    with pytest.raises(PermissionConfigError):
        PermissionPolicy(tmp_path, mode=removed, load=False)


def test_without_tools_plan_mode_is_unavailable():
    loop = AgentLoop(SequenceProvider(), "", None)
    loop.set_mode("normal")
    with pytest.raises(ProviderError, match="未启用工具"):
        loop.set_mode("plan")
    assert loop.mode == "normal" and not hasattr(loop, "_mode")
