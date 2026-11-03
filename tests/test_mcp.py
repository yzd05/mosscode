import asyncio
import json
import re
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from mcp import types

from mosscode.conversation import Conversation
from mosscode.mcp_client import MCPManager, MCPTool, ServerConnection, tool_name
from mosscode.mcp_config import MCPConfigError, MCPSettings, ServerConfig, load_mcp_settings
from mosscode.messages import ToolCall
from mosscode.permissions import ApprovalRequest, parse_rule
from mosscode.session import Session
from mosscode.tools import create_default_registry
from mosscode.tools.base import ExecutionCancelled, ToolFailure
from test_agent import SequenceProvider, response
from waiting import until

FIXTURE = Path(__file__).parent / "fixtures/mcp_stdio_server.py"


def stdio_config(name, log, mode="normal"):
    return ServerConfig(
        name, "stdio", sys.executable, (str(FIXTURE), str(log), mode), {"MOSS_MCP_FIXTURE": "test-only"}
    )


PICTURE_TEXT = "iVBORw0KGgo"


def call(name, **args):
    return ToolCall("call_1", name, json.dumps(args))


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_config_merge_replaces_server_and_expands_only_values(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("MCP_TEST_SECRET", "fake-secret")
    write_yaml(
        user,
        {
            "mcp_servers": {
                "shared": {"type": "stdio", "command": "old", "args": ["old"]},
                "local": {"type": "stdio", "command": "python", "env": {"TOKEN": "${MCP_TEST_SECRET}"}},
            }
        },
    )
    write_yaml(
        tmp_path / ".mosscode/mcp.yaml",
        {
            "mcp_servers": {
                "shared": {
                    "type": "http",
                    "url": "http://localhost:8000/mcp",
                    "headers": {"Authorization": "Bearer ${MCP_TEST_SECRET}"},
                }
            }
        },
    )
    settings = load_mcp_settings(tmp_path, user_path=user)
    by_name = {s.name: s for s in settings.servers}
    shared, local = by_name["shared"], by_name["local"]
    assert shared.type == "http" and shared.command is None and shared.args == ()
    assert shared.headers["Authorization"] == "Bearer fake-secret"
    assert local.env == {"TOKEN": "fake-secret"}
    assert "fake-secret" not in repr(settings)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "http", "url": "http://u:fake-secret@localhost"},
        {"type": "http", "url": "https://localhost", "headers": {"Token": "${MCP_NOT_SET}"}},
        {"type": "stdio", "command": "python", "args": "bad"},
        {"type": "http", "url": "https://localhost", "headers": {"Token": "fake-secret\r\n"}},
        {"type": "stdio", "command": "python", "url": "http://localhost"},
    ],
)
def test_invalid_server_is_isolated_safe(tmp_path, monkeypatch, bad):
    monkeypatch.delenv("MCP_NOT_SET", raising=False)
    path = tmp_path / "user.yaml"
    write_yaml(path, {"mcp_servers": {"bad": bad, "good": {"type": "stdio", "command": "python"}}})
    settings = load_mcp_settings(tmp_path, user_path=path)
    assert [s.name for s in settings.servers] == ["good"]
    assert len(settings.notices) == 1 and "fake-secret" not in str(settings.notices)


def test_empty_and_malformed_configuration(tmp_path):
    user = tmp_path / "missing.yaml"
    assert load_mcp_settings(tmp_path, user_path=user) == MCPSettings()
    user.write_text("mcp_servers: [fake-secret", encoding="utf-8")
    with pytest.raises(MCPConfigError) as e:
        load_mcp_settings(tmp_path, user_path=user)
    assert "fake-secret" not in str(e.value)


async def test_mcp_images_are_attached_stored_and_left_out_for_text_only_models(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    reg.use_session(Session(tmp_path / "session"))
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("one", tmp_path / "one.log"),)), startup_timeout=5)
    try:
        await manager.start(reg)
        shot = await reg.execute(call(tool_name("one", "mixed"), text="picture"))
        assert not shot.is_error and [(i.width, i.height, i.media_type) for i in shot.images] == [(3, 2, "image/png")]
        assert "one/mixed 的图片 1 · 图片 · 3×2 · " in shot.output and "_images" not in shot.details
        assert shot.images[0].path.parent == (tmp_path / "session" / "images").resolve()
        assert shot.images[0].path.is_file() and PICTURE_TEXT not in shot.output
        failed = await reg.execute(call(tool_name("one", "mixed"), text="picture-error"))
        assert failed.is_error and failed.code == "mcp_tool_error" and len(failed.images) == 1
        reg.images = False
        text_only = await reg.execute(call(tool_name("one", "mixed"), text="picture"))
        assert not text_only.images and "one/mixed 的图片 1（当前模型不支持图片，已省略）" in text_only.output
    finally:
        await manager.close()


def test_names_are_stable_bounded_and_collision_resistant():
    assert tool_name("notes", "search") == "mcp__notes__search"
    names = [
        tool_name(s, t)
        for s, t in [
            ("a-b", "echo"),
            ("a_b", "echo"),
            ("a", "b__c"),
            ("a__b", "c"),
            ("a" * 64, "x" * 100),
            ("a", "工具"),
        ]
    ]
    assert len(set(names)) == len(names)
    assert all(len(n) <= 64 and re.fullmatch(r"[A-Za-z0-9_]+", n) for n in names)


async def test_stdio_real_handshake_pagination_reuse_errors_and_correlation(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("one", tmp_path / "one.log"),)), startup_timeout=5)
    try:
        notices = await manager.start(reg)
        assert "4 个工具" in notices[0]
        assert await manager.start(reg) == ()
        names = [d.name for d in reg.definitions()]
        assert all(tool_name("one", n) in names for n in ["echo", "fail", "slow", "mixed"])
        first, second = await asyncio.gather(
            reg.execute(call(tool_name("one", "echo"), text="delayed")),
            reg.execute(call(tool_name("one", "echo"), text="fast")),
        )
        assert first.output == "delayed" and second.output == "fast"
        bad = await reg.execute(call(tool_name("one", "echo"), text=7))
        assert bad.code == "invalid_arguments"
        assert len((tmp_path / "one.log").read_text().splitlines()) == 2
        failed = await reg.execute(call(tool_name("one", "fail"), text="failure"))
        assert failed.is_error and failed.code == "mcp_tool_error" and failed.output == "failure"
        mixed = await reg.execute(call(tool_name("one", "mixed"), text="data"))
        assert '"received": "data"' in mixed.output and "one/mixed 的图片 1（不是可识别的图片" in mixed.output
        assert "aGVsbG8=" not in mixed.model_text() and not mixed.images
        protocol = await reg.execute(call(tool_name("one", "echo"), text="protocol-error"))
        assert protocol.code == "mcp_protocol_error" and protocol.details["rpc_code"] == -32602
        assert "fixture-private-error" not in protocol.output
        assert manager.connections[0].state == "ready"
        assert json.loads((tmp_path / "one.log").read_text().splitlines()[0])["env"] == "test-only"
    finally:
        await manager.close()
    assert (tmp_path / "one.closed").exists()
    assert manager.connections[0].task.done()
    await manager.close()


async def test_stdio_fixture_round_trips_chinese_text_under_the_locale_code_page(tmp_path):
    # PYTHONUTF8=0 keeps the locale code page (GBK on Chinese Windows) for the server's pipes.
    config = ServerConfig("zh", "stdio", sys.executable, (str(FIXTURE), str(tmp_path / "zh.log")), {"PYTHONUTF8": "0"})
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((config,)), startup_timeout=5)
    try:
        await manager.start(reg)
        for text in ("你好", "再见"):
            result = await reg.execute(call(tool_name("zh", "echo"), text=text))
            assert not result.is_error and result.output == text
        assert manager.connections[0].state == "ready"
    finally:
        await manager.close()


@contextmanager
def http_server(*, sse=False, status=200, broken_sse=False):
    events = []
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            events.append((data, dict(self.headers)))
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "id" not in data:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            method = data["method"]
            if method == "initialize":
                result = {
                    "protocolVersion": data["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": "echo", "inputSchema": schema}]}
            else:
                result = {"content": [{"type": "text", "text": data["params"]["arguments"]["text"]}]}
            payload = json.dumps({"jsonrpc": "2.0", "id": data["id"], "result": result}).encode()
            use_sse = sse and method == "tools/call"
            if use_sse:
                payload = b"event: message\ndata: " + payload + b"\n\n"
            if use_sse and broken_sse:
                payload = b"id: fixture-event\nevent: message\ndata:\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if use_sse else "application/json")
            self.send_header("Mcp-Session-Id", "fixture-session")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            events.append(("GET", {}))
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self):
            events.append(("DELETE", dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", events
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("sse", [False, True])
async def test_streamable_http_json_and_sse(tmp_path, sse):
    with http_server(sse=sse) as (url, events):
        reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
        manager = MCPManager(
            tmp_path, MCPSettings((ServerConfig("web", "http", url=url, headers={"X-Test": "test-only"}),))
        )
        try:
            assert "已连接" in (await manager.start(reg))[0]
            result = await reg.execute(call("mcp__web__echo", text="remote"))
            assert result.output == "remote" and not result.is_error
            calls = [e for e in events if isinstance(e[0], dict) and e[0]["method"] == "tools/call"]
            assert calls[0][1]["mcp-session-id"] == "fixture-session"
            assert calls[0][1]["X-Test"] == "test-only"
            assert all(e[0] != "GET" for e in events)
        finally:
            await manager.close()
        assert any(e[0] == "DELETE" for e in events)


async def test_failure_isolation_startup_timeout_and_closed_connection(tmp_path):
    with http_server(status=503) as (url, events):
        manager = MCPManager(
            tmp_path,
            MCPSettings(
                (
                    stdio_config("good", tmp_path / "good.log"),
                    stdio_config("bad", tmp_path / "bad.log", "repeat"),
                    ServerConfig("web", "http", url=url),
                )
            ),
            startup_timeout=4,
        )
        reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
        try:
            notices = await manager.start(reg)
            assert len([n for n in notices if "已连接" in n]) == 1
            assert (await reg.execute(call("mcp__good__echo", text="okay"))).output == "okay"
            assert len([e for e in events if isinstance(e[0], dict) and e[0]["method"] == "initialize"]) == 1
        finally:
            await manager.close()
    hanging = ServerConnection(tmp_path, stdio_config("hang", tmp_path / "hang.log", "hang"), 0.4)
    assert await hanging.start() is None
    assert hanging.task.done()
    await hanging.close()


async def test_mcp_permissions_plan_default_and_tool_level_persistent_grant(tmp_path):
    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"),)))
    try:
        await manager.start(reg)
        name = "mcp__notes__echo"
        assert not reg.get(name).read_only  # Ignore readOnlyHint=true.
        assert name not in [d.name for d in reg.definitions(read_only=True)]
        blocked = await reg.execute(call(name, text="../outside"), read_only=True)
        assert blocked.code == "plan_blocked"
        assert (await reg.execute(call(name, text="A"))).code == "approval_required"
        assert not (tmp_path / "calls.log").exists()
        reg.permissions.interactive = True
        job = asyncio.create_task(reg.execute(call(name, text="A")))
        request = await asyncio.wait_for(reg.permissions.requests.get(), 2)
        assert request.operation.target == '{"text":"A"}' and request.operation.cwd is None
        assert "外部 MCP" in request.operation.preview
        assert not (tmp_path / "calls.log").exists()
        reg.permissions.respond(request.id, "permanent")
        assert (await job).output == "A"
        reg.permissions.interactive = False
        # The grant covers this tool with any arguments (Claude Code semantics), saved as a bare name.
        assert (await reg.execute(call(name, text="a"))).output == "a"
        assert yaml.safe_load((tmp_path / ".mosscode/permissions.local.yaml").read_text(encoding="utf-8"))["allow"] == [
            name
        ]
        fresh = create_default_registry(tmp_path)
        fresh.register(reg.get(name))
        assert (await fresh.execute(call(name, text="different"))).output == "different"
        # Hand-written exact rules still pin the arguments.
        exact = create_default_registry(tmp_path, load_permissions=False)
        exact.register(reg.get(name))
        exact.permissions.layers[2] = (parse_rule({"rule": name + '({"text":"A"})', "exact": True}, "allow"),)
        assert (await exact.execute(call(name, text="A"))).output == "A"
        assert (await exact.execute(call(name, text="a"))).code == "approval_required"  # JSON is case-sensitive.
        reg.permissions.layers[2] = (parse_rule(name + "(*)", "deny"),)
        reg.permissions.mode = "bypassPermissions"
        assert (await reg.execute(call(name, text="A"))).code == "permission_denied"
    finally:
        await manager.close()


async def test_cancel_pending_and_running_remote_operations_preserves_history(tmp_path):
    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"),)))
    await manager.start(reg)
    try:
        name = "mcp__notes__echo"
        reg.permissions.interactive = True
        provider = SequenceProvider(response(calls=[call(name, text="cancel")]))
        conversation = Conversation(provider, registry=reg)

        async def consume():
            async for e in conversation.reply("task"):
                if isinstance(e, ApprovalRequest):
                    asyncio.current_task().cancel()

        task = asyncio.create_task(consume())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not reg.permissions.pending and not (tmp_path / "calls.log").exists()
        assert conversation.history[-1].tool_results[0].code == "cancelled"
        reg.permissions.mode = "bypassPermissions"
        request = asyncio.create_task(reg.execute(call("mcp__notes__slow", text="slow")))
        await until((tmp_path / "calls.log").exists)
        request.cancel()
        with pytest.raises(ExecutionCancelled) as e:
            await request
        assert e.value.result.code == "cancelled"
        assert (await reg.execute(call(name, text="next"))).code == "mcp_unavailable"
    finally:
        await manager.close()


async def test_agent_loop_discovers_then_calls_remote_tool(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("agent", tmp_path / "calls.log"),)))
    provider = SequenceProvider(response(calls=[call("mcp__agent__echo", text="value")]), response("done"))
    conversation = Conversation(provider, registry=reg, mcp_manager=manager)
    try:
        assert await conversation.ask("task") == "done"
        assert conversation.history[2].tool_results[0].output == "value"
        assert provider.requests[1][0][-1].role == "tool"
    finally:
        await conversation.close()


def test_schema_refs_and_malformed_remote_schema(tmp_path):
    connection = ServerConnection(tmp_path, ServerConfig("schema", "stdio", "python"))
    schema = {
        "type": "object",
        "$defs": {"str": {"type": "string"}},
        "properties": {"text": {"$ref": "#/$defs/str"}},
        "required": ["text"],
    }
    tool = MCPTool(tmp_path, connection, types.Tool(name="ref", inputSchema=schema))
    assert tool.definition().parameters == schema
    assert tool.validate_arguments({"text": "okay"}).model_dump() == {"text": "okay"}
    with pytest.raises(ToolFailure):
        tool.validate_arguments({"text": 42})
    schema["properties"]["text"]["$ref"] = "https://example.com/schema"
    with pytest.raises(ValueError):
        MCPTool(tmp_path, connection, types.Tool(name="ref", inputSchema=schema))


async def test_local_tools_cannot_edit_mcp_config(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    result = await reg.execute(call("write_file", path=".mosscode/mcp.yaml", content="malicious"))
    assert result.code == "policy_protected"
    assert not (tmp_path / ".mosscode/mcp.yaml").exists()


async def test_disconnect_timeout_and_large_error_are_not_retried(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = MCPManager(
        tmp_path,
        MCPSettings((stdio_config("broken", tmp_path / "broken.log"), stdio_config("timed", tmp_path / "timed.log"))),
    )
    try:
        await manager.start(reg)
        large = await reg.execute(call("mcp__broken__echo", text="large-error"))
        assert large.code == "mcp_tool_error" and large.details["truncated"]
        assert len(large.output.encode()) < 65600
        disconnected = await reg.execute(call("mcp__broken__echo", text="disconnect"))
        assert disconnected.code == "mcp_disconnected"
        assert (await reg.execute(call("mcp__broken__echo", text="again"))).code == "mcp_unavailable"
        reg.get("mcp__timed__slow").timeout = 0.1
        timed = await reg.execute(call("mcp__timed__slow", text="slow"))
        assert timed.code == "timeout"
        assert (await reg.execute(call("mcp__timed__echo", text="again"))).code == "mcp_unavailable"
        assert len((tmp_path / "broken.log").read_text().splitlines()) == 2
        assert len((tmp_path / "timed.log").read_text().splitlines()) == 1
    finally:
        await manager.close()


async def test_interrupted_http_sse_never_resumes_or_reposts(tmp_path):
    with http_server(sse=True, broken_sse=True) as (url, events):
        reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
        manager = MCPManager(tmp_path, MCPSettings((ServerConfig("web", "http", url=url),)))
        try:
            await manager.start(reg)
            result = await asyncio.wait_for(reg.execute(call("mcp__web__echo", text="once")), 3)
            assert result.code == "mcp_disconnected"
            assert (await reg.execute(call("mcp__web__echo", text="again"))).code == "mcp_unavailable"
            assert all(e[0] != "GET" for e in events)
            assert sum(isinstance(e[0], dict) and e[0]["method"] == "tools/call" for e in events) == 1
        finally:
            await manager.close()


async def test_concurrent_start_and_cancelled_initialization_cleanup(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("same", tmp_path / "same.log"),)))
    try:
        notices = await asyncio.gather(manager.start(reg), manager.start(reg))
        assert sorted(map(len, notices)) == [0, 1]
        assert len([d for d in reg.definitions() if d.name.startswith("mcp__same__")]) == 4
    finally:
        await manager.close()
    unopened = MCPManager(tmp_path, MCPSettings((stdio_config("never", tmp_path / "never.log"),)))
    await unopened.close()
    assert await unopened.start(reg) == () and unopened.connections[0].task is None
    hanging = MCPManager(tmp_path, MCPSettings((stdio_config("hang", tmp_path / "hang.log", "hang"),)))
    task = asyncio.create_task(hanging.start(reg))
    await until((tmp_path / "hang.pid").exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hanging.connections[0].task.done()


async def test_tui_startup_discovery_and_keyboard_approval(tmp_path):
    from textual.widgets import Static

    from mosscode.tui import MossCodeApp, PromptArea, ToolEntry
    from test_permission_tui import wait_approval
    from test_tui import send

    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"),)))
    provider = SequenceProvider(response(calls=[call("mcp__notes__echo", text="value")]), response("**完成**"))
    app = MossCodeApp(Conversation(provider, registry=reg, mcp_manager=manager), "Demo")
    async with app.run_test(size=(100, 32)) as pilot:
        await asyncio.wait_for(app._startup_task, 5)
        assert not app.query_one("#prompt", PromptArea).disabled
        await asyncio.wait_for(manager.wait(), 5)  # Discovery runs in the background.
        await pilot.pause()
        assert not any("已连接" in str(n.render()) for n in app.query(".local-note"))  # /mcp lists it
        await send(app, pilot, "调用外部工具")
        await wait_approval(app, pilot)
        assert "mcp__notes__echo" in str(app.query_one("#approval-detail", Static).render())
        assert "value" in app.query_one(ToolEntry).detail
        assert not (tmp_path / "calls.log").exists()
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 3)
        assert app.conversation.history[2].tool_results[0].output == "value"
        assert not reg.permissions.pending
    assert manager.connections[0].task.done()


async def test_headless_startup_does_not_wait_for_mcp_approval(tmp_path, capsys):
    from mosscode.cli import _prompt

    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("notes", tmp_path / "calls.log"),)))
    provider = SequenceProvider(response(calls=[call("mcp__notes__echo", text="value")]), response("需授权"))
    conv = Conversation(provider, registry=reg, mcp_manager=manager)
    await asyncio.wait_for(_prompt(conv, "task"), 5)
    output = capsys.readouterr()
    assert "已连接" not in output.err and "需授权" in output.out
    assert conv.history[2].tool_results[0].code == "approval_required"
    assert not (tmp_path / "calls.log").exists() and manager.connections[0].task.done()


async def test_tui_exit_while_connecting_cleans_startup_task(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea

    reg = create_default_registry(tmp_path, load_permissions=False)
    manager = MCPManager(tmp_path, MCPSettings((stdio_config("hang", tmp_path / "hang.log", "hang"),)))
    app = MossCodeApp(Conversation(SequenceProvider(), registry=reg, mcp_manager=manager), "Demo")
    async with app.run_test():
        await until((tmp_path / "hang.pid").exists)
        assert not app.query_one("#prompt", PromptArea).disabled  # Never blocked by MCP.
    assert app._startup_task.done() and manager.connections[0].task.done()
    assert not reg.permissions.pending
