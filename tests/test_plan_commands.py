"""Read-only plan mode, exit_plan_mode approval, command risk tiers."""

import asyncio

import pytest

from mosscode import cli
from mosscode.conversation import Conversation
from mosscode.permissions import CHOICES, COMMAND_CHOICES, PLAN_CHOICES, RISKY_CHOICES, ApprovalRequest, parse_rule
from mosscode.prompts import mode_reminder
from test_agent import SequenceProvider, call, response
from test_permission_modes import RemoteTool
from test_permissions import approve, registry

PLAN_TOOLS = {"read_file", "glob", "grep", "execute_command", "exit_plan_mode"}
CATASTROPHIC = ["rm -rf /", "Remove-Item C:\\ -Recurse -Force", "format C:", "dd if=x of=/dev/sda", "shutdown -h now"]
RISKY = ["rm -rf build", "Remove-Item build -Recurse -Force", "git reset --hard", "git push --force", "git clean -fd"]


def simulate(reg, name="execute_command"):
    executed = []

    async def fake(params):
        executed.append(params.model_dump())
        return "executed", {}
    reg.get(name).execute = fake
    return executed


def plan_registry(tmp_path, entered_from="default"):
    reg = registry(tmp_path, entered_from)
    reg.permissions.set_mode("plan")
    return reg



def test_plan_definitions_are_read_only_plus_exit_plan_mode(tmp_path):
    reg = registry(tmp_path)
    reg.register(RemoteTool(tmp_path))
    assert {d.name for d in reg.definitions(plan=True)} == PLAN_TOOLS
    normal = {d.name for d in reg.definitions()}
    assert "exit_plan_mode" not in normal and {"write_file", "execute_command", "mcp__demo__echo"} <= normal
    assert "exit_plan_mode" not in {d.name for d in reg.definitions(read_only=True)}


@pytest.mark.parametrize("request_", [
    call("write_file", path="a", content="x"),
    call("edit_file", path="a", old_text="old", new_text="new"),
    call("execute_command", command="echo hi"),
    call("mcp__demo__echo", text="x"),
])
async def test_plan_blocks_non_read_tools_from_bypass_and_with_allow_rules(tmp_path, request_):
    (tmp_path / "a").write_text("old")
    reg = plan_registry(tmp_path, "bypassPermissions")
    reg.register(RemoteTool(tmp_path))
    reg.permissions.layers[2] = (parse_rule("Write(**)", "allow"), parse_rule("Edit(**)", "allow"),
                                 parse_rule("Bash(*)", "allow"))
    executed = simulate(reg, request_.name)
    result = await reg.execute(request_)
    assert result.code == "plan_blocked" and "exit_plan_mode" in result.output
    assert not executed and (tmp_path / "a").read_text() == "old"
    assert reg.permissions.requests.empty()
    assert not (await reg.execute(call("read_file", path="a"))).is_error



async def test_exit_plan_mode_outside_plan_is_not_in_plan(tmp_path):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    result = await reg.execute(call("exit_plan_mode", plan="步骤"))
    assert result.code == "not_in_plan" and reg.permissions.requests.empty()


async def test_exit_plan_mode_headless_requires_approval_and_stays_in_plan(tmp_path):
    reg = plan_registry(tmp_path)
    result = await reg.execute(call("exit_plan_mode", plan="步骤"))
    assert result.code == "approval_required" and reg.permissions.mode == "plan"


@pytest.mark.parametrize("entered_from,choice,after", [
    ("acceptEdits", "execute", "acceptEdits"),
    ("default", "execute", "default"),
    ("bypassPermissions", "execute", "bypassPermissions"),
    ("default", "accept_edits", "acceptEdits"),
    ("default", "deny", "plan"),
])
async def test_plan_choices_set_the_next_mode(tmp_path, entered_from, choice, after):
    reg = plan_registry(tmp_path, entered_from)
    # Rules never decide whether planning ends.
    reg.permissions.layers[2] = (parse_rule("Bash(*)", "allow"),)
    result, request = await approve(reg, call("exit_plan_mode", plan="## 步骤\n1. 修改"), choice)
    assert request.choices == PLAN_CHOICES and request.operation.tool == "exit_plan_mode"
    assert "1. 修改" in request.operation.preview
    assert reg.permissions.mode == after
    if choice == "deny":
        assert result.code == "permission_denied" and "继续规划" in result.output
    else:
        assert not result.is_error and "已退出计划模式" in result.output
        assert result.details["plan"] == "## 步骤\n1. 修改"


async def test_plan_approval_is_void_if_mode_changed_meanwhile(tmp_path):
    reg = plan_registry(tmp_path)
    reg.permissions.interactive = True
    task = asyncio.create_task(reg.execute(call("exit_plan_mode", plan="步骤")))
    request = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    reg.permissions.set_mode("acceptEdits")
    reg.permissions.respond(request.id, "execute")
    assert (await task).code == "not_in_plan" and reg.permissions.mode == "acceptEdits"


async def test_plan_approval_switches_tools_within_one_task(tmp_path):
    reg = plan_registry(tmp_path, "acceptEdits")
    reg.permissions.interactive = True
    provider = SequenceProvider(response(calls=[call("write_file", "early", path="a", content="x")]),
                                response(calls=[call("exit_plan_mode", "p", plan="写入 a")]),
                                response(calls=[call("write_file", "w", path="a", content="planned")]),
                                response("完成"))
    conv = Conversation(provider, registry=reg)
    asked = []
    async for event in conv.reply("规划并实施"):
        if isinstance(event, ApprovalRequest):
            asked.append(event.choices)
            reg.permissions.respond(event.id, "execute")
    assert asked == [PLAN_CHOICES]
    codes = [r.code for m in conv.history if m.role == "tool" for r in m.tool_results]
    assert codes == ["plan_blocked", None, None]
    assert (tmp_path / "a").read_text() == "planned" and conv.mode == "normal"
    assert [{t.name for t in r[2]} == PLAN_TOOLS for r in provider.requests] == [True, True, False, False]
    # The environment supplement follows the switch within the same task.
    assert '"acceptEdits"' in provider.requests[2][1].supplements[0].text


async def test_headless_cli_prints_plan(tmp_path, capsys):
    reg = plan_registry(tmp_path)
    provider = SequenceProvider(response(calls=[call("exit_plan_mode", plan="## 步骤\n1. 修改 a")]), response("等待批准"))
    await cli._prompt(Conversation(provider, registry=reg), "规划")
    out = capsys.readouterr().out
    assert "计划：\n## 步骤\n1. 修改 a" in out and "approval_required" not in out.split("计划：")[0]
    assert reg.permissions.mode == "plan"



@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions"])
@pytest.mark.parametrize("command", CATASTROPHIC)
async def test_catastrophic_commands_are_hard_denied_in_every_mode(tmp_path, mode, command):
    reg = registry(tmp_path, mode)
    reg.permissions.interactive = True
    reg.permissions.layers[2] = (parse_rule("Bash(*)", "allow"),)
    reg.permissions.session.append(parse_rule(f"Bash({command})", "allow"))
    executed = simulate(reg)
    result = await reg.execute(call("execute_command", command=command))
    assert result.code == "dangerous_command" and not executed and reg.permissions.requests.empty()



@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions"])
@pytest.mark.parametrize("command", RISKY)
async def test_risky_commands_always_ask_headless(tmp_path, mode, command):
    reg = registry(tmp_path, mode)
    reg.permissions.layers[2] = (parse_rule("Bash(*)", "allow"),)
    reg.permissions.session.append(parse_rule(f"Bash({command})", "allow"))
    executed = simulate(reg)
    result = await reg.execute(call("execute_command", command=command))
    assert result.code == "approval_required" and not executed


async def test_risky_command_offers_once_or_deny_and_asks_every_time(tmp_path):
    reg = registry(tmp_path, "bypassPermissions")
    reg.permissions.layers[2] = (parse_rule("Bash(*)", "allow"),)
    executed = simulate(reg)
    command = call("execute_command", command="git reset --hard")
    result, request = await approve(reg, command, "once")
    assert request.choices == RISKY_CHOICES and request.operation.risk == "丢弃 Git 改动"
    assert "高风险命令" in request.operation.preview
    assert not result.is_error and len(executed) == 1
    result, request = await approve(reg, command, "deny")
    assert result.code == "permission_denied" and len(executed) == 1
    assert not reg.permissions.session


@pytest.mark.parametrize("choice", ["session", "permanent", "execute"])
async def test_respond_rejects_choices_the_request_does_not_offer(tmp_path, choice):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    simulate(reg)
    task = asyncio.create_task(reg.execute(call("execute_command", command="rm -rf build")))
    request = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    with pytest.raises(ValueError):
        reg.permissions.respond(request.id, choice)
    assert reg.permissions.respond(request.id, "deny")
    assert (await task).code == "permission_denied"
    assert not (tmp_path / ".mosscode").exists()


async def test_deny_rule_still_wins_over_risky_confirmation(tmp_path):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    reg.permissions.layers[2] = (parse_rule("Bash(git reset *)", "deny"),)
    executed = simulate(reg)
    result = await reg.execute(call("execute_command", command="git reset --hard"))
    assert result.code == "permission_denied" and not executed and reg.permissions.requests.empty()


async def test_command_choices_offer_a_prefix_only_when_one_can_be_derived(tmp_path):
    reg = registry(tmp_path)
    simulate(reg)
    _, request = await approve(reg, call("execute_command", command="python -m pytest -q"), "once")
    assert request.choices == COMMAND_CHOICES and request.operation.prefix == "python -m pytest"
    _, request = await approve(reg, call("execute_command", command="python script.py"), "once")
    assert request.choices == CHOICES and not request.operation.risk



def test_plan_reminder_points_to_exit_plan_mode():
    text = mode_reminder("plan", "normal").text
    assert "exit_plan_mode" in text and "只能读取" in text and "审批后" not in text
