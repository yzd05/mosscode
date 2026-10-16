"""Stage 5 acceptance: background MCP startup, /mcp, server rules, read-only trust, .mcp.json, timeouts."""

import asyncio
from dataclasses import replace
import json
import sys

import pytest
import yaml

from mosscode.conversation import Conversation
from mosscode.mcp_client import MCPManager, tool_name
from mosscode.mcp_config import MCPSettings, ServerConfig, load_mcp_settings
from mosscode.permissions import Operation, PermissionConfigError, PermissionPolicy, parse_rule
from mosscode.tools import create_default_registry
from mosscode.tools.execution import partition_calls
from test_agent import SequenceProvider, response
from test_mcp import call, http_server, stdio_config, write_yaml


def notes_text(app):
    return [str(note.render()) for note in app.query(".local-note")]


def status_text(app):
    from textual.widgets import Static
    return str(app.query_one("#status", Static).render())


async def until(predicate, pilot=None, timeout=8.0):
    for _ in range(int(timeout / .02)):
        if predicate():
            return
        await (pilot.pause(.02) if pilot else asyncio.sleep(.02))
    raise AssertionError("条件未满足")


# AC1: the prompt is usable at once; tools join the next request; notices wait for the task.
async def test_tui_background_startup_registers_between_requests(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea
    from test_tui import send
    reg = create_default_registry(tmp_path, load_permissions=False)
    hang = replace(stdio_config("hang", tmp_path / "hang.log", "hang"), startup_timeout=60)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"), hang)))
    app = None
    seen_mid_task = []

    async def first():
        # The server becomes ready while this task runs; its notice must wait.
        await until(lambda: reg.get("mcp__notes__echo") is not None)
        await asyncio.sleep(.2)
        seen_mid_task.append(any("MCP notes" in text for text in notes_text(app)))
        yield response("第一轮")

    provider = SequenceProvider(first, response("第二轮"))
    app = MossCodeApp(Conversation(provider, registry=reg, mcp_manager=manager), "Demo")
    async with app.run_test(size=(120, 32)) as pilot:
        await asyncio.wait_for(app._startup_task, 5)
        assert not app.query_one("#prompt", PromptArea).disabled
        assert "MCP 连接中 0/2" in status_text(app)
        await send(app, pilot, "一")
        await asyncio.wait_for(app._reply_task, 10)
        await pilot.pause()
        assert seen_mid_task == [False]
        assert any("MCP notes 已连接 · 注册 4 个工具" in text for text in notes_text(app))
        assert "mcp__notes__echo" not in [tool.name for tool in provider.requests[0][2]]
        assert "MCP 连接中 1/2" in status_text(app)
        await send(app, pilot, "二")
        await asyncio.wait_for(app._reply_task, 5)
        assert "mcp__notes__echo" in [tool.name for tool in provider.requests[1][2]]
        assert manager.pending == 1
    assert app._startup_task.done() and all(task.done() for task in manager._tasks)
    assert all(connection.task.done() for connection in manager.connections)


async def test_conversation_start_still_waits_by_default(tmp_path):
    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"),)))
    conv = Conversation(SequenceProvider(), registry=reg, mcp_manager=manager)
    try:
        notices = await asyncio.wait_for(conv.start(), 10)
        assert notices == ("MCP notes 已连接 · 注册 4 个工具。",) and reg.get("mcp__notes__echo")
        assert await conv.start() == () and manager.pending == 0
    finally:
        await conv.close()


# AC2: /mcp status, failure reasons without secrets, tools, hints and completion.
async def test_mcp_command_lists_status_reasons_and_tools(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea
    from test_tui import send
    reg = create_default_registry(tmp_path, load_permissions=False)
    with http_server(status=503) as (url, _):
        servers = (
            replace(stdio_config("notes", tmp_path / "calls.log"), trust_read_only_hint=True),
            ServerConfig("missing", "stdio", "mosscode-missing-command-s5"),
            ServerConfig("web", "http", url=url + "?token=fake-secret", headers={"Authorization": "Bearer fake-secret"}),
            replace(stdio_config("hang", tmp_path / "hang.log", "hang"), startup_timeout=.5),
        )
        manager = MCPManager(tmp_path, MCPSettings(servers, ("MCP bad 不可用：timeout 需在 0 到 600 秒之间。",)))
        app = MossCodeApp(Conversation(SequenceProvider(), registry=reg, mcp_manager=manager), "Demo")
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.wait_for(app._startup_task, 5)
            await asyncio.wait_for(manager.wait(), 15)
            await pilot.pause()
            await send(app, pilot, "/mcp")
            report = notes_text(app)[-1]
            assert "MCP Server（4）" in report
            assert "notes" in report and "已连接 · 4 个工具" in report
            assert "失败：找不到命令 mosscode-missing-command-s5" in report
            assert "失败：HTTP 503" in report
            assert "失败：启动超时（0.5 秒）" in report
            assert "! MCP bad 不可用" in report
            assert "fake-secret" not in report and "127.0.0.1" not in report
            await send(app, pilot, "/mcp notes")
            tools = notes_text(app)[-1]
            assert "mcp__notes__echo  [只读]" in tools and "mcp__notes__mixed" in tools
            await send(app, pilot, "/mcp nope")
            assert "未找到 MCP Server nope" in notes_text(app)[-1]
            field = app.query_one("#prompt", PromptArea)
            field.value = "/mc"
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert field.value == "/mcp "
            assert app._hint_choices == [(f"/mcp {name}", "查看该 Server 的工具") for name in ("notes", "missing", "web", "hang")]


async def test_mcp_command_without_servers_points_to_configuration():
    from mosscode.tui import MossCodeApp
    from test_conversation import FakeProvider
    from test_tui import send
    app = MossCodeApp(Conversation(FakeProvider()), "Demo")
    async with app.run_test() as pilot:
        await send(app, pilot, "/mcp")
        assert "未配置 MCP Server" in notes_text(app)[-1] and "mcp.yaml" in notes_text(app)[-1]


# AC3: server rules, any-argument tool rules, deny over bypass, hashed names, bad syntax.
def test_server_and_tool_rules_parse_and_match(tmp_path):
    echo = Operation("mcp__notes__echo", '{"text":"A"}', server="notes")
    other = Operation("mcp__other__echo", '{"text":"A"}', server="other")
    hashed = Operation(tool_name("my-server", "echo"), "{}", server="my-server")
    assert hashed.tool != "mcp__my-server__echo"
    for text in ("mcp__notes", "mcp__notes__*"):
        rule = parse_rule(text, "allow")
        assert rule.matches(echo) and not rule.matches(other)
    assert parse_rule("mcp__my-server", "allow").matches(hashed)
    assert parse_rule("mcp__notes__echo", "allow").matches(replace(echo, target='{"text":"anything"}'))
    assert not parse_rule("mcp__notes__echo", "allow").matches(Operation("mcp__notes__fail", "{}", server="notes"))
    assert not parse_rule("mcp__notes", "allow").matches(Operation("read_file", "a.txt"))
    for bad in ("mcp__", "mcp__bad_", "mcp__a__", "mcp__bad name"):
        with pytest.raises(PermissionConfigError):
            parse_rule(bad, "allow")
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    reg.permissions.layers[2] = (parse_rule("mcp__notes", "deny"),)
    assert reg.permissions.decide(echo, read_only=False).action == "deny"
    assert reg.permissions.decide(other, read_only=False).action == "allow"


def test_server_rule_loads_from_permission_file(tmp_path):
    write_yaml(tmp_path / ".mosscode/permissions.yaml", {"deny": ["mcp__notes__*"], "allow": ["mcp__web"]})
    decide = PermissionPolicy(tmp_path, user_path=tmp_path / "none.yaml").decide
    assert decide(Operation("mcp__notes__echo", "{}", server="notes"), read_only=False).action == "deny"
    assert decide(Operation("mcp__web__echo", "{}", server="web"), read_only=False).action == "allow"


# AC4: real stdio approval offers four choices worded as any-argument grants; session stays in session.
async def test_mcp_session_grant_covers_any_arguments_only_in_session(tmp_path):
    from mosscode.tui import choice_label
    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"),)))
    try:
        await manager.start(reg)
        name = "mcp__notes__echo"
        reg.permissions.interactive = True
        job = asyncio.create_task(reg.execute(call(name, text="A")))
        request = await asyncio.wait_for(reg.permissions.requests.get(), 2)
        assert request.choices == ("once", "session", "permanent", "deny")
        assert "任意参数" in choice_label(request, "session") and "任意参数" in choice_label(request, "permanent")
        reg.permissions.respond(request.id, "session")
        assert (await job).output == "A"
        reg.permissions.interactive = False
        assert (await reg.execute(call(name, text="other"))).output == "other"
        assert (await reg.execute(call("mcp__notes__mixed", text="x"))).code == "approval_required"
        assert not (tmp_path / ".mosscode/permissions.local.yaml").exists()
        fresh = create_default_registry(tmp_path)
        fresh.register(reg.get(name))
        assert (await fresh.execute(call(name, text="A"))).code == "approval_required"
    finally:
        await manager.close()


# AC5: trust_read_only_hint makes hinted tools read-only tools; deny still wins.
async def test_trusted_read_only_hint(tmp_path):
    reg = create_default_registry(tmp_path, load_permissions=False)
    config = replace(stdio_config("notes", tmp_path / "calls.log"), trust_read_only_hint=True)
    manager = MCPManager(tmp_path, MCPSettings((config,)))
    try:
        await manager.start(reg)
        name = "mcp__notes__echo"
        assert reg.get(name).read_only and "（只读）" in reg.get(name).prompt_summary
        assert (await reg.execute(call(name, text="free"))).output == "free"
        calls = [call(name, text="a"), call("mcp__notes__mixed", text="b"), call("write_file", path="x", content="")]
        assert list(partition_calls(calls, reg)) == [[0, 1], [2]]  # Trusted reads run together.
        reg.permissions.set_mode("plan")
        assert name in [d.name for d in reg.definitions(plan=True)]
        assert (await reg.execute(call(name, text="plan"))).output == "plan"
        reg.permissions.layers[2] = (parse_rule(name, "deny"),)
        assert (await reg.execute(call(name, text="x"))).code == "permission_denied"
    finally:
        await manager.close()


# AC6: .mcp.json is opt-in, converted, expanded and ranked between user and project yaml.
def test_mcp_json_import(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_S5_UNSET", raising=False)
    monkeypatch.setenv("MCP_S5_SET", "from-env")
    user = tmp_path / "home/mcp.yaml"
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "local": {"command": sys.executable, "args": ["${MCP_S5_UNSET:-fallback}", "${MCP_S5_SET}"]},
        "web": {"type": "http", "url": "https://localhost:8443/mcp"},
        "old": {"type": "sse", "url": "https://localhost:8443/sse"},
        "shared": {"command": "from-mcp-json"},
    }}), encoding="utf-8")
    write_yaml(user, {"mcp_servers": {"local": {"type": "stdio", "command": "from-user"}}})
    write_yaml(tmp_path / ".mosscode/mcp.yaml", {"mcp_servers": {"shared": {"type": "stdio", "command": "from-project"}}})
    ignored = load_mcp_settings(tmp_path, user_path=user)
    assert {s.name: s.command for s in ignored.servers} == {"local": "from-user", "shared": "from-project"}
    write_yaml(user, {"import_mcp_json": True, "mcp_servers": {"local": {"type": "stdio", "command": "from-user"}}})
    settings = load_mcp_settings(tmp_path, user_path=user)
    by_name = {s.name: s for s in settings.servers}
    assert set(by_name) == {"local", "web", "shared"}
    assert by_name["local"].type == "stdio" and by_name["local"].args == ("fallback", "from-env")
    assert by_name["web"].type == "http" and by_name["shared"].command == "from-project"
    assert len(settings.notices) == 1 and "old" in settings.notices[0] and "不受支持" in settings.notices[0]


async def test_model_cannot_write_mcp_json(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    result = await reg.execute(call("write_file", path=".mcp.json", content="{}"))
    assert result.code == "policy_protected" and not (tmp_path / ".mcp.json").exists()


# AC7: per-server tool timeout and startup timeout; out-of-range values become notices.
async def test_per_server_timeouts(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    quick = replace(stdio_config("notes", tmp_path / "calls.log"), timeout=.3)
    hang = replace(stdio_config("hang", tmp_path / "hang.log", "hang"), startup_timeout=.4)
    manager = MCPManager(tmp_path, MCPSettings((quick, hang)))
    try:
        started = asyncio.get_running_loop().time()
        notices = await manager.start(reg)
        assert asyncio.get_running_loop().time() - started < 5
        assert "MCP hang 失败：启动超时（0.4 秒）；可用 /mcp 查看。" in notices
        assert (await reg.execute(call("mcp__notes__echo", text="fast"))).output == "fast"
        assert (await reg.execute(call("mcp__notes__slow", text="x"))).code == "timeout"
    finally:
        await manager.close()


@pytest.mark.parametrize("option", [{"timeout": 0}, {"timeout": 601}, {"startup_timeout": 121},
                                    {"trust_read_only_hint": "yes"}])
def test_out_of_range_options_become_notices(tmp_path, option):
    user = tmp_path / "user.yaml"
    write_yaml(user, {"mcp_servers": {"bad": {"type": "stdio", "command": "python", **option},
                                      "good": {"type": "stdio", "command": "python", "timeout": 5}}})
    settings = load_mcp_settings(tmp_path, user_path=user)
    assert [s.name for s in settings.servers] == ["good"] and settings.servers[0].timeout == 5
    assert len(settings.notices) == 1 and settings.notices[0].startswith("MCP bad 不可用")
