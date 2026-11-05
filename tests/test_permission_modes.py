import asyncio
import io
import json

import pytest
from rich.console import Console
from textual.widgets import Static

from mosscode.conversation import Conversation
from mosscode.messages import ToolCall
from mosscode.permissions import MODES, PermissionConfigError, PermissionPolicy, parse_rule
from mosscode.tools import create_default_registry
from mosscode.tools.base import Params, Tool
from mosscode.tui import MossCodeApp, PlanEntry
from test_agent import SequenceProvider, call, response
from test_permission_tui import wait_approval
from test_tui import send


class RemoteArgs(Params):
    text: str


class RemoteTool(Tool):
    name = "mcp__demo__echo"
    permission_kind = "mcp"
    params_model = RemoteArgs
    description = "Fake external tool"

    async def execute(self, params):
        return params.text, {}


CASES = [
    ("read_file", {"path": "a"}, {m for m in MODES}),
    ("write_file", {"path": "a", "content": "x"}, {"acceptEdits", "bypassPermissions"}),
    ("edit_file", {"path": "a", "old_text": "old", "new_text": "new"}, {"acceptEdits", "bypassPermissions"}),
    ("execute_command", {"command": 'bash -c "echo 3 > hello.txt"'}, {"bypassPermissions"}),
    ("mcp__demo__echo", {"text": "x"}, {"bypassPermissions"}),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name,args,allowed", CASES)
async def test_matrix_reaches_actual_execution_boundary(tmp_path, mode, name, args, allowed):
    reg = create_default_registry(tmp_path, permission_mode=mode, load_permissions=False)
    reg.register(RemoteTool(tmp_path))
    executed = []

    async def simulated(params):
        executed.append(params.model_dump())
        return "executed", {}

    reg.get(name).execute = simulated
    result = await reg.execute(ToolCall("matrix", name, json.dumps(args)))
    assert bool(executed) == (mode in allowed)
    if mode == "plan" and name != "read_file":
        assert result.code == "plan_blocked"
        # execute_command is offered in plan mode, but only for read-only commands.
        assert (name in [d.name for d in reg.definitions(plan=True)]) == (name == "execute_command")
    elif mode not in allowed:
        assert result.code == "approval_required"
    else:
        assert not result.is_error


@pytest.mark.parametrize("mode", MODES)
def test_denies_and_layer_order_remain_in_force(tmp_path, mode):
    policy = PermissionPolicy(tmp_path, mode=mode, load=False)
    from mosscode.permissions import Operation

    op = Operation("write_file", "a")
    policy.layers[0] = (parse_rule("Write(a)", "allow"),)
    policy.layers[1] = (parse_rule("Write(a)", "deny"),)
    assert policy.decide(op, read_only=False).action == "deny"
    policy.layers[2] = (parse_rule("Write(a)", "allow"),)
    assert policy.decide(op, read_only=False).action == "allow"
    policy.session.append(parse_rule("Write(a)", "deny"))
    assert policy.decide(op, read_only=False).action == "deny"


def test_yaml_modes_alias_and_plan_resume(tmp_path):
    config = tmp_path / "user.yaml"
    for mode in MODES:
        config.write_text(f"mode: {mode}\n", encoding="utf-8")
        policy = PermissionPolicy(tmp_path, user_path=config)
        assert policy.mode == mode
    for removed, replacement in (("permissive", "bypassPermissions"), ("strict", "default")):
        config.write_text(f"mode: {removed}\n", encoding="utf-8")
        with pytest.raises(PermissionConfigError, match=f"{removed} 已移除，请改用 {replacement}"):
            PermissionPolicy(tmp_path, user_path=config)
    policy = PermissionPolicy(tmp_path, mode="acceptEdits", load=False)
    with pytest.raises(PermissionConfigError):
        policy.mode = "permissive"
    policy.set_mode("acceptEdits")
    conv = Conversation(SequenceProvider(), registry=create_default_registry(tmp_path, permissions=policy))
    conv.set_mode("plan")
    conv.set_mode("plan")
    assert policy.mode == "plan" and policy.resume_mode == "acceptEdits"
    conv.set_mode("normal")
    assert policy.mode == "acceptEdits" and conv.mode == "normal"
    conv.set_permission_mode("plan")
    assert conv.mode == "plan"
    conv.set_permission_mode("default")
    assert conv.mode == "normal"
    policy.set_mode("plan")
    started = Conversation(
        SequenceProvider(), registry=create_default_registry(tmp_path, permission_mode="plan", load_permissions=False)
    )
    assert started.mode == "plan"
    started.set_mode("normal")
    assert started.registry.permissions.mode == "default"


async def test_accept_edits_auto_file_write_but_bash_write_waits_for_approval(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="acceptEdits", load_permissions=False)
    executed = []

    async def fake_command(params):
        executed.append(params.command)
        return "command simulated", {}

    reg.get("execute_command").execute = fake_command
    provider = SequenceProvider(
        response(
            calls=[
                call("write_file", "w", path="a", content="one"),
                call("execute_command", "c", command='bash -c "echo 3 > hello.txt"'),
            ]
        ),
        response("完成"),
    )
    app = MossCodeApp(Conversation(provider, registry=reg), "Demo")
    async with app.run_test(size=(100, 34)) as pilot:
        await send(app, pilot, "写入文件并用 Bash 写入另一个文件")
        await wait_approval(app, pilot)
        assert (tmp_path / "a").read_text() == "one"
        assert not executed and app._approval_request.operation.tool == "execute_command"
        assert "⏵⏵ 接受编辑" in str(app.query_one("#status", Static).render())
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 3)
        assert len(executed) == 1
        assert all(not r.is_error for r in app.conversation.history[2].tool_results)
        assert reg.permissions.mode == "acceptEdits" and not reg.permissions.pending


@pytest.mark.parametrize("approve", [True, False])
async def test_plan_is_approved_in_tui_then_writes_in_same_task(tmp_path, approve):
    reg = create_default_registry(tmp_path, permission_mode="acceptEdits", load_permissions=False)
    plan = "## 步骤\n1. 写入 `a`\n2. 运行测试"
    submit = response(calls=[call("exit_plan_mode", "p", plan=plan)])
    if approve:
        provider = SequenceProvider(
            submit, response(calls=[call("write_file", "w", path="a", content="planned")]), response("完成")
        )
    else:
        provider = SequenceProvider(submit, response("修改计划"), response("执行阶段"))
    conv = Conversation(provider, registry=reg)
    conv.set_mode("plan")
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 34)) as pilot:
        await send(app, pilot, "先规划再写入")
        await wait_approval(app, pilot)
        assert app._approval_request.choices == ("execute", "accept_edits", "deny")
        assert app.query_one("#approval-options").option_count == 3
        console = Console(width=80, record=True, file=io.StringIO())
        console.print(app.query_one(PlanEntry).transcript)
        shown = console.export_text()
        assert "计划" in shown and "写入 a" in shown and "运行测试" in shown
        assert {t.name for t in provider.requests[0][2]} == {
            "read_file",
            "glob",
            "grep",
            "execute_command",
            "exit_plan_mode",
            "web_fetch",
        }
        await pilot.press("enter" if approve else "escape")
        await asyncio.wait_for(app._reply_task, 3)
        results = [r for m in conv.history if m.role == "tool" for r in m.tool_results]
        if approve:
            assert [r.code for r in results] == [None, None]
            assert (tmp_path / "a").read_text() == "planned"
            assert reg.permissions.mode == "acceptEdits" and conv.mode == "normal"
            assert len(provider.requests[1][2]) == 7
        else:
            assert results[0].code == "permission_denied" and not (tmp_path / "a").exists()
            assert conv.mode == "plan" and reg.permissions.mode == "plan"
            await send(app, pilot, "/permissions acceptEdits")
            assert reg.permissions.mode == "acceptEdits" and conv.mode == "normal"


@pytest.mark.parametrize("mode", MODES)
def test_cli_accepts_four_modes(monkeypatch, capsys, tmp_path, mode):
    from mosscode import cli
    from mosscode.mcp_config import MCPSettings
    from test_cli import setup_cli

    setup_cli(monkeypatch, ["--prompt", "hello", "--permission-mode", mode])
    original = create_default_registry
    observed = []

    def make_registry(*, permission_mode, shell="auto", web_search=None):
        reg = original(
            tmp_path, permission_mode=permission_mode, load_permissions=False, shell=shell, web_search=web_search
        )
        observed.append(reg.permissions.mode)
        return reg

    monkeypatch.setattr(cli, "create_default_registry", make_registry)
    monkeypatch.setattr(cli, "load_mcp_settings", lambda root, **_: MCPSettings())
    cli.main()
    assert observed == [mode]


@pytest.mark.parametrize("mode", ["strict", "permissive"])
def test_cli_rejects_removed_modes(monkeypatch, capsys, mode):
    from mosscode import cli
    from test_cli import setup_cli

    setup_cli(monkeypatch, ["--prompt", "hello", "--permission-mode", mode])
    with pytest.raises(SystemExit):
        cli.main()
    assert "已移除" in capsys.readouterr().err
