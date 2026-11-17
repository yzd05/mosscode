import asyncio
import json
import re
import subprocess
import sys
from contextlib import aclosing
from pathlib import Path
from typing import Any

import acp
import pytest
from acp import RequestError
from acp._transport import memory_transport_pair
from acp.agent.connection import AgentSideConnection
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AuthCapabilities,
    AvailableCommandsUpdate,
    BlobResourceContents,
    ClientCapabilities,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    FileEditToolCallContent,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    RequestPermissionResponse,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)

from mosscode import launcher
from mosscode.acp.app import MossCodeAcpAgent
from mosscode.acp.cli import parse_acp_args
from mosscode.acp.session import AcpLaunchOptions
from mosscode.config import render_config
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError, ToolCall
from mosscode.sandbox import HOME_SECRETS


class ScriptedProvider:
    def __init__(self, handler):
        self._handler = handler
        self.requests: list[tuple[Any, ...]] = []
        self.thinking: str | None = None
        self.closed = False
        self.calls = 0

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append((tuple(messages), system, tuple(tools), allow_tools, self.thinking))
        self.calls += 1
        async with aclosing(self._handler(self.calls, messages, self)) as gen:
            async for event in gen:
                yield event

    async def close(self):
        self.closed = True


class RecordingClient:
    def __init__(self, permission_decider=None):
        self.updates: list[tuple[str, Any]] = []
        self.permission_requests: list[tuple[str, Any, list[Any]]] = []
        self._decider = permission_decider or (lambda session_id, tool_call, options: DeniedOutcome())

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permission_requests.append((session_id, tool_call, options))
        result = self._decider(session_id, tool_call, options)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, RequestPermissionResponse):
            return result
        return RequestPermissionResponse(outcome=result)


def _write_user_config(home: Path, *, name: str = "fake", model: str = "fake-model", persist: bool = True) -> Path:
    cfg_dir = home / ".mosscode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.yaml"
    text = render_config(
        {
            "name": name,
            "protocol": "anthropic-messages",
            "model": model,
            "api_key": "sk-test-1234567890",
        }
    )
    if not persist:
        text += "persist_sessions: false\n"
    cfg.write_text(text, encoding="utf-8")
    return cfg


async def _connect_agent(agent: MossCodeAcpAgent, client: RecordingClient):
    left, right = memory_transport_pair()
    agent_conn = AgentSideConnection(agent, right, listening=False)
    client_conn = ClientSideConnection(client, left)
    agent_task = asyncio.create_task(agent_conn.listen())

    async def cleanup():
        await agent.close_all()
        await client_conn.close()
        await agent_conn.close()
        await asyncio.gather(agent_task, return_exceptions=True)

    return client_conn, cleanup


def test_registry_manifest_and_monochrome_icon():
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "registry" / "mosscode" / "agent.json"
    icon_path = root / "registry" / "mosscode" / "icon.svg"
    assert manifest_path.is_file()
    assert icon_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["id"] == "mosscode"
    assert manifest["license"] == "MIT"
    assert manifest["icon"] == "icon.svg"
    assert manifest["distribution"]["uvx"]["args"] == ["acp"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["distribution"]["uvx"]["package"] == f"mosscode=={manifest['version']}"

    svg = icon_path.read_text(encoding="utf-8")
    assert 'viewBox="0 0 16 16"' in svg
    assert 'width="16"' in svg and 'height="16"' in svg
    assert "currentColor" in svg
    assert not re.search(r"#[0-9a-fA-F]{3,8}", svg)


def test_launcher_lists_acp_and_stays_lazy(capsys):
    with pytest.raises(SystemExit):
        launcher.main(["--help"])
    assert "acp" in capsys.readouterr().out

    # Launching --version or -p must not import mosscode.acp or acp SDK.
    code = (
        "import sys\nfrom mosscode import launcher\n"
        "try:\n    launcher.main(['--version'])\nexcept SystemExit:\n    pass\n"
        "print([m for m in ('acp', 'mosscode.acp') if m in sys.modules])"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().splitlines()[-1] == "[]"

    parsed = parse_acp_args(
        [
            "--config",
            "c.yaml",
            "--provider",
            "p1",
            "--permission-mode",
            "acceptEdits",
            "--trust-project-config",
            "--no-sandbox",
        ]
    )
    assert parsed.config == "c.yaml"
    assert parsed.provider == "p1"
    assert parsed.permission_mode == "acceptEdits"
    assert parsed.trust_project_config is True
    assert parsed.sandbox is False


def test_subprocess_stdio_cold_start_and_clean_jsonrpc(tmp_path):
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    env = {
        **dict(__import__("os").environ),
        "HOME": str(empty_home),
        "USERPROFILE": str(empty_home),
    }
    req_init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
    req_new = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session/new",
        "params": {"cwd": str(work), "mcpServers": []},
    }
    stdin_reqs = [req_init, req_new]
    proc = subprocess.Popen(
        [sys.executable, "-m", "mosscode", "acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=work,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        msgs = []
        for req in stdin_reqs:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            assert line.strip(), "missing JSON-RPC response line"
            msgs.append(json.loads(line))
        proc.stdin.close()
        # Python 3.12 POSIX subprocess._communicate flushes self.stdin if non-None.
        proc.stdin = None
        _, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert len(msgs) == 2
    assert msgs[0]["id"] == 1
    assert msgs[0]["result"]["protocolVersion"] == acp.PROTOCOL_VERSION
    assert len(msgs[0]["result"]["authMethods"]) >= 1
    assert msgs[1]["id"] == 2
    assert msgs[1]["error"]["code"] == -32000


async def test_initialize_auth_required_and_authenticate(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home))
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        init = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="zed", version="1.0"),
        )
        assert init.protocol_version == acp.PROTOCOL_VERSION
        assert init.agent_info.name == "mosscode"
        assert init.agent_capabilities.load_session is True
        assert init.agent_capabilities.prompt_capabilities.image is True
        assert init.agent_capabilities.prompt_capabilities.embedded_context is True
        assert [m.id for m in init.auth_methods] == ["local-config"]
        assert init.auth_methods[0].field_meta and "terminal-auth" in init.auth_methods[0].field_meta

        init_term = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(auth=AuthCapabilities(terminal=True)),
            client_info=Implementation(name="zed", version="1.0"),
        )
        assert [m.id for m in init_term.auth_methods] == ["moss-setup-terminal", "local-config"]

        # Without any config file, session/new and session/list return -32000.
        with pytest.raises(RequestError) as exc_info:
            await conn.new_session(cwd=str(work), mcp_servers=[])
        assert exc_info.value.code == -32000

        with pytest.raises(RequestError) as list_auth_err:
            await conn.list_sessions(cwd=str(work))
        assert list_auth_err.value.code == -32000

        with pytest.raises(RequestError) as bad_auth:
            await conn.authenticate(method_id="unknown")
        assert bad_auth.value.code == -32602

        _write_user_config(home)
        await conn.authenticate(method_id="local-config")
        created = await conn.new_session(cwd=str(work), mcp_servers=[])
        assert created.session_id
        assert created.modes.current_mode_id == "default"
        assert any(o.id == "mode" for o in created.config_options)
    finally:
        await cleanup()


async def test_streaming_tools_diff_plan_and_completion(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    sample = work / "hello.txt"
    sample.write_text("alpha\nbeta\n", encoding="utf-8", newline="\n")
    _write_user_config(home)

    async def script(turn, messages, provider):
        if turn == 1:
            yield Delta("thinking", "Let's read and edit hello.txt and track todos.")
            yield Delta("text", "Working on it.")
            calls = (
                ToolCall("call-r", "read_file", '{"path": "hello.txt"}'),
                ToolCall(
                    "call-t",
                    "todo_write",
                    '{"todos": [{"content": "update file", "status": "in_progress"}, {"content": "verify", "status": "pending"}]}',
                ),
            )
            yield Completed(
                Message("assistant", (ContentBlock("text", "Working on it."),), tool_calls=calls), "tool_use"
            )
        elif turn == 2:
            calls = (ToolCall("call-e", "edit_file", '{"path": "hello.txt", "old_text": "beta", "new_text": "gamma"}'),)
            yield Completed(Message("assistant", (), tool_calls=calls), "tool_use")
        else:
            yield Delta("text", "All done.")
            yield Completed(Message("assistant", (ContentBlock("text", "All done."),)), "stop")

    provider = ScriptedProvider(script)
    opts = AcpLaunchOptions(home=home, permission_mode="acceptEdits", provider_factory=lambda _cfg: provider)
    agent = MossCodeAcpAgent(opts)
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])
        await asyncio.sleep(0)

        assert any(isinstance(u, AvailableCommandsUpdate) for _, u in client.updates)
        res = await conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="Update beta to gamma")])
        assert res.stop_reason == "end_turn"
        assert sample.read_text(encoding="utf-8") == "alpha\ngamma\n"

        updates = [u for sid, u in client.updates if sid == sess.session_id]
        assert any(isinstance(u, AgentThoughtChunk) for u in updates)
        assert any(isinstance(u, AgentMessageChunk) and "All done." in u.content.text for u in updates)
        assert any(isinstance(u, AgentPlanUpdate) and len(u.entries) == 2 for u in updates)
        starts = [u for u in updates if isinstance(u, ToolCallStart)]
        assert {s.name for s in starts} >= {"read_file", "todo_write", "edit_file"}

        edit_done = [
            u
            for u in updates
            if isinstance(u, ToolCallProgress) and u.tool_call_id == "call-e" and u.status == "completed"
        ]
        assert len(edit_done) == 1
        diffs = [c for c in (edit_done[0].content or ()) if isinstance(c, FileEditToolCallContent)]
        assert len(diffs) == 1
        assert diffs[0].old_text == "alpha\nbeta\n"
        assert diffs[0].new_text == "alpha\ngamma\n"
    finally:
        await cleanup()


async def test_permission_approval_and_exit_plan_mode_syncs_mode(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    target = work / "note.txt"
    target.write_text("v1\n", encoding="utf-8", newline="\n")
    _write_user_config(home)

    async def clean_script(turn, messages, provider):
        if turn == 1:
            calls = (ToolCall("call-plan", "exit_plan_mode", '{"plan": "# Plan\\n- edit note.txt"}'),)
            yield Completed(Message("assistant", (), tool_calls=calls), "tool_use")
        elif turn == 2:
            calls = (ToolCall("call-read", "read_file", '{"path": "note.txt"}'),)
            yield Completed(Message("assistant", (), tool_calls=calls), "tool_use")
        elif turn == 3:
            calls = (ToolCall("call-edit", "edit_file", '{"path": "note.txt", "old_text": "v1", "new_text": "v2"}'),)
            yield Completed(Message("assistant", (), tool_calls=calls), "tool_use")
        else:
            yield Completed(Message("assistant", (ContentBlock("text", "Finished"),)), "stop")

    def decide(_sid, tool_call, options):
        ids = [o.option_id for o in options]
        if tool_call.name == "exit_plan_mode":
            return AllowedOutcome(option_id="execute")
        if tool_call.name == "edit_file" and "accept_edits" in ids:
            return AllowedOutcome(option_id="accept_edits")
        return AllowedOutcome(option_id="once")

    agent = MossCodeAcpAgent(
        AcpLaunchOptions(home=home, permission_mode="plan", provider_factory=lambda _c: ScriptedProvider(clean_script))
    )
    client = RecordingClient(decide)
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])
        assert sess.modes.current_mode_id == "plan"

        res = await conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="Plan and run")])
        assert res.stop_reason == "end_turn"
        assert target.read_text(encoding="utf-8") == "v2\n"
        assert [tc.name for _, tc, _ in client.permission_requests] == ["exit_plan_mode", "edit_file"]

        mode_updates = [u.current_mode_id for _, u in client.updates if isinstance(u, CurrentModeUpdate)]
        assert "default" in mode_updates
        assert "acceptEdits" in mode_updates
        assert any(isinstance(u, ConfigOptionUpdate) for _, u in client.updates)
    finally:
        await cleanup()


async def test_ask_user_and_cancel_during_blocked_permission(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    target = work / "guard.txt"
    target.write_text("safe", encoding="utf-8")
    _write_user_config(home)

    async def script_ask(turn, messages, provider):
        if turn == 1:
            args = json.dumps(
                {
                    "questions": [
                        {
                            "header": "Target",
                            "question": "Which option?",
                            "options": [{"label": "OptA", "description": "A"}, {"label": "OptB", "description": "B"}],
                        }
                    ]
                }
            )
            yield Completed(Message("assistant", (), tool_calls=(ToolCall("call-ask", "ask_user", args),)), "tool_use")
        else:
            tool_msg = messages[-1].tool_results[0].output
            assert "OptA" in tool_msg
            yield Delta("text", "Got OptA")
            yield Completed(Message("assistant", (ContentBlock("text", "Got OptA"),)), "stop")

    agent1 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script_ask)))
    client1 = RecordingClient(lambda _s, _tc, _o: AllowedOutcome(option_id="opt:0"))
    conn1, cleanup1 = await _connect_agent(agent1, client1)
    try:
        await conn1.initialize(protocol_version=1)
        s1 = await conn1.new_session(cwd=str(work), mcp_servers=[])
        res1 = await conn1.prompt(session_id=s1.session_id, prompt=[TextContentBlock(text="Ask me")])
        assert res1.stop_reason == "end_turn"
        assert any(isinstance(u, AgentMessageChunk) and "Got OptA" in u.content.text for _, u in client1.updates)
    finally:
        await cleanup1()

    # Now verify cancellation while blocked in session/request_permission.
    perm_entered = asyncio.Event()

    async def slow_perm(_sid, _tc, _opts):
        perm_entered.set()
        await asyncio.sleep(30)
        return DeniedOutcome()

    async def script_cancel(turn, messages, provider):
        if provider.calls == 1:
            calls = (ToolCall("call-write", "write_file", '{"path": "guard.txt", "content": "dirty"}'),)
            yield Completed(Message("assistant", (), tool_calls=calls), "tool_use")
        else:
            yield Completed(Message("assistant", (ContentBlock("text", "Recovered"),)), "stop")

    agent2 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script_cancel)))
    client2 = RecordingClient(slow_perm)
    conn2, cleanup2 = await _connect_agent(agent2, client2)
    try:
        await conn2.initialize(protocol_version=1)
        s2 = await conn2.new_session(cwd=str(work), mcp_servers=[])
        prompt_task = asyncio.create_task(
            conn2.prompt(session_id=s2.session_id, prompt=[TextContentBlock(text="Overwrite file")])
        )
        await asyncio.wait_for(perm_entered.wait(), timeout=5)
        await conn2.cancel(session_id=s2.session_id)
        cancelled_res = await asyncio.wait_for(prompt_task, timeout=5)
        assert cancelled_res.stop_reason == "cancelled"
        assert target.read_text(encoding="utf-8") == "safe"

        next_res = await conn2.prompt(session_id=s2.session_id, prompt=[TextContentBlock(text="Continue")])
        assert next_res.stop_reason == "end_turn"
    finally:
        await cleanup2()


async def test_session_load_replay_list_and_close(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    _write_user_config(home)

    async def script1(turn, messages, provider):
        if turn == 1:
            calls = (ToolCall("call-t", "todo_write", '{"todos": [{"content": "Saved item", "status": "completed"}]}'),)
            yield Completed(
                Message("assistant", (ContentBlock("thinking", "Thinking saved"),), tool_calls=calls), "tool_use"
            )
        else:
            yield Completed(Message("assistant", (ContentBlock("text", "First turn reply"),)), "stop")

    agent1 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script1)))
    client1 = RecordingClient()
    conn1, cleanup1 = await _connect_agent(agent1, client1)
    saved_id = ""
    try:
        await conn1.initialize(protocol_version=1)
        s1 = await conn1.new_session(cwd=str(work), mcp_servers=[])
        saved_id = s1.session_id
        res = await conn1.prompt(session_id=saved_id, prompt=[TextContentBlock(text="Save this session")])
        assert res.stop_reason == "end_turn"
    finally:
        await cleanup1()

    # Load from a brand new agent instance.
    saw_history: list[str] = []

    async def script2(turn, messages, provider):
        saw_history.extend(m.text for m in messages if m.role == "user" and not m.synthetic)
        yield Completed(Message("assistant", (ContentBlock("text", "Resumed reply"),)), "stop")

    agent2 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script2)))
    client2 = RecordingClient()
    conn2, cleanup2 = await _connect_agent(agent2, client2)
    try:
        await conn2.initialize(protocol_version=1)
        listed = await conn2.list_sessions(cwd=str(work))
        assert any(s.session_id == saved_id for s in listed.sessions)

        loaded = await conn2.load_session(cwd=str(work), session_id=saved_id, mcp_servers=[])
        assert loaded.modes.current_mode_id == "default"
        updates = [u for sid, u in client2.updates if sid == saved_id]
        assert any(isinstance(u, UserMessageChunk) and "Save this session" in u.content.text for u in updates)
        assert any(isinstance(u, AgentThoughtChunk) and "Thinking saved" in u.content.text for u in updates)
        assert any(isinstance(u, AgentPlanUpdate) and u.entries[0].content == "Saved item" for u in updates)
        assert any(isinstance(u, AvailableCommandsUpdate) for u in updates)

        res2 = await conn2.prompt(session_id=saved_id, prompt=[TextContentBlock(text="Next message")])
        assert res2.stop_reason == "end_turn"
        assert "Save this session" in saw_history and "Next message" in saw_history

        await conn2.close_session(session_id=saved_id)
    finally:
        await cleanup2()


async def test_multi_cwd_isolation_and_security_trust_boundaries(tmp_path):
    home = tmp_path / "home"
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "who.txt").write_text("from-A\n", encoding="utf-8")
    (dir_b / "who.txt").write_text("from-B\n", encoding="utf-8")
    _write_user_config(home)

    # Create untrusted project MCP and hooks in dir_a: neither should run automatically.
    (dir_a / ".mosscode").mkdir()
    (dir_a / ".mosscode" / "hooks.yaml").write_text(
        "hooks:\n  - id: bad\n    event: turn_start\n    action:\n      type: command\n      command: exit 1\n",
        encoding="utf-8",
    )
    (dir_a / ".mosscode" / "mcp.yaml").write_text(
        "mcp_servers:\n  bad_srv:\n    type: stdio\n    command: nonexistent_mcp_binary\n",
        encoding="utf-8",
    )

    secret_file = home / HOME_SECRETS[0] / "id_test"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text("SECRET_KEY_MATERIAL", encoding="utf-8")

    observed_contexts: list[str] = []

    async def script(turn, messages, provider):
        if turn == 1 and "read who" in messages[-1].text:
            yield Completed(
                Message("assistant", (), tool_calls=(ToolCall("c1", "read_file", '{"path": "who.txt"}'),)),
                "tool_use",
            )
        else:
            observed_contexts.append("\n".join(m.text for m in messages if m.role == "user"))
            last_tool = next((m.tool_results[0].output for m in reversed(messages) if m.role == "tool"), "")
            out_text = last_tool or "checked"
            yield Delta("text", out_text)
            yield Completed(Message("assistant", (ContentBlock("text", out_text),)), "stop")

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sa = await conn.new_session(cwd=str(dir_a), mcp_servers=[])
        sb = await conn.new_session(cwd=str(dir_b), mcp_servers=[])

        ra = await conn.prompt(session_id=sa.session_id, prompt=[TextContentBlock(text="read who")])
        rb = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="read who")])
        assert ra.stop_reason == "end_turn" and rb.stop_reason == "end_turn"

        text_a = "".join(
            u.content.text for sid, u in client.updates if sid == sa.session_id and isinstance(u, AgentMessageChunk)
        )
        text_b = "".join(
            u.content.text for sid, u in client.updates if sid == sb.session_id and isinstance(u, AgentMessageChunk)
        )
        assert "from-A" in text_a
        assert "from-B" in text_b
        assert not agent.sessions[sa.session_id].conversation.mcp_manager.connections
        assert text_a.count("项目 MCP 服务器 bad_srv 未经确认") == 1
        assert text_a.count("hooks.yaml 未经确认") == 1

        # Passing ~/.ssh file URI via embedded context must be filtered and never leak to model.
        rc = await conn.prompt(
            session_id=sb.session_id,
            prompt=[
                TextContentBlock(text="Check this"),
                EmbeddedResourceContentBlock(
                    resource=TextResourceContents(uri=secret_file.resolve().as_uri(), text="SECRET_KEY_MATERIAL")
                ),
            ],
        )
        assert rc.stop_reason == "end_turn"
        assert "SECRET_KEY_MATERIAL" not in observed_contexts[-1]
        assert "受保护的凭据路径" in observed_contexts[-1]

        # Test slash commands and client mcpServers conversion cleanly.
        assert agent.sessions[sa.session_id].conversation.hooks.hooks == ()
        help_res = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="/help")])
        assert help_res.stop_reason == "end_turn"
        status_res = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="/status")])
        assert status_res.stop_reason == "end_turn"
        mcp_res = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="/mcp")])
        assert mcp_res.stop_reason == "end_turn"
        plan_res = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="/plan")])
        assert plan_res.stop_reason == "end_turn"
        assert agent.sessions[sb.session_id].current_mode == "plan"
        do_res = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="/do")])
        assert do_res.stop_reason == "end_turn"
        assert agent.sessions[sb.session_id].current_mode == "default"
        perm_res = await conn.prompt(
            session_id=sb.session_id, prompt=[TextContentBlock(text="/permissions acceptEdits")]
        )
        assert perm_res.stop_reason == "end_turn"
        assert agent.sessions[sb.session_id].current_mode == "acceptEdits"
        clear_res = await conn.prompt(session_id=sb.session_id, prompt=[TextContentBlock(text="/clear")])
        assert clear_res.stop_reason == "end_turn"
        assert agent.sessions[sb.session_id].conversation.history == ()

        # Client stdio mcpServers param works cleanly without failing session creation
        from mosscode.acp.session import convert_client_mcp_servers

        converted, notices = convert_client_mcp_servers(
            [McpServerStdio(name="ide_tools", command="echo", args=["ok"], env=[])]
        )
        assert "ide_tools" in converted and converted["ide_tools"].type == "stdio" and notices == []
    finally:
        await cleanup()


async def test_config_options_switching_and_provider_error_codes(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    cfg_dir = home / ".mosscode"
    cfg_dir.mkdir(parents=True)
    cfg_dir.joinpath("config.yaml").write_text(
        "default_provider: p1\nproviders:\n"
        "  - name: p1\n    protocol: anthropic-messages\n    model: m1\n    api_key: sk-11111111\n"
        "  - name: p2\n    protocol: openai-chat-completions\n    model: m2\n    api_key: sk-22222222\n",
        encoding="utf-8",
    )

    async def fail_script(turn, messages, provider):
        if False:
            yield Delta("text", "")
        raise ProviderError("认证或权限失败，请检查凭据与端点。（HTTP 401）")

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(fail_script)))
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])
        switched = await conn.set_config_option(config_id="model", session_id=sess.session_id, value="p2")
        assert next(o for o in switched.config_options if o.id == "model").current_value == "p2"
        thought = await conn.set_config_option(config_id="thought_level", session_id=sess.session_id, value="low")
        assert next(o for o in thought.config_options if o.id == "thought_level").current_value == "low"

        # /model <provider>/<model> overrides model and /model <provider> restores configured model; /rename updates title.
        overridden = await conn.prompt(
            session_id=sess.session_id, prompt=[TextContentBlock(text="/model p1/claude-sonnet-4-6")]
        )
        assert overridden.stop_reason == "end_turn"
        assert agent.sessions[sess.session_id].provider_config.name == "p1"
        assert agent.sessions[sess.session_id].provider_config.model == "claude-sonnet-4-6"
        restored_model = await conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="/model p1")])
        assert restored_model.stop_reason == "end_turn"
        assert agent.sessions[sess.session_id].provider_config.model == "m1"

        renamed = await conn.prompt(
            session_id=sess.session_id, prompt=[TextContentBlock(text="/rename ACP 会话自定义标题")]
        )
        assert renamed.stop_reason == "end_turn"
        listed = await conn.list_sessions(cwd=str(work))
        assert any(
            item.title == "ACP 会话自定义标题" and item.session_id == sess.session_id for item in listed.sessions
        )

        with pytest.raises(RequestError) as auth_err:
            await conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="hello")])
        assert auth_err.value.code == -32000
    finally:
        await cleanup()


async def test_wire_order_new_session_response_before_available_commands(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    _write_user_config(home)

    left, right = memory_transport_pair()
    wire_outgoing: list[dict[str, Any]] = []

    class ObservingTransport:
        def __init__(self, inner):
            self._inner = inner

        async def send(self, message: dict[str, Any]) -> None:
            wire_outgoing.append(dict(message))
            await self._inner.send(message)

        async def receive(self) -> dict[str, Any] | None:
            return await self._inner.receive()

        async def close(self) -> None:
            await self._inner.close()

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home))
    client = RecordingClient()
    agent_conn = AgentSideConnection(agent, ObservingTransport(right), listening=False)
    client_conn = ClientSideConnection(client, left)
    agent_task = asyncio.create_task(agent_conn.listen())
    try:
        await client_conn.initialize(protocol_version=1)
        created = await client_conn.new_session(cwd=str(work), mcp_servers=[])
        # Wait until AvailableCommandsUpdate arrives on the client.
        for _ in range(20):
            if any(isinstance(u, AvailableCommandsUpdate) for _, u in client.updates):
                break
            await asyncio.sleep(0.01)

        resp_idx = next(
            i
            for i, msg in enumerate(wire_outgoing)
            if "result" in msg
            and isinstance(msg["result"], dict)
            and msg["result"].get("sessionId") == created.session_id
        )
        cmd_idx = next(
            i
            for i, msg in enumerate(wire_outgoing)
            if msg.get("method") == "session/update"
            and msg.get("params", {}).get("update", {}).get("sessionUpdate") == "available_commands_update"
        )
        assert resp_idx < cmd_idx, f"NewSessionResponse ({resp_idx}) must precede AvailableCommandsUpdate ({cmd_idx})"
    finally:
        await agent.close_all()
        await client_conn.close()
        await agent_conn.close()
        await asyncio.gather(agent_task, return_exceptions=True)


async def test_fork_skill_review_and_context_attached_slash_commands(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    sample = work / "module.py"
    sample.write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    _write_user_config(home)

    seen_prompts: list[str] = []

    async def script(turn, messages, provider):
        seen_prompts.append("\n".join(m.text for m in messages if m.role == "user"))
        out = f"Reviewed turn {provider.calls}"
        yield Delta("text", out)
        yield Completed(Message("assistant", (ContentBlock("text", out),)), "stop")

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])

        # Local command with IDE-attached EmbeddedResourceContentBlock must NOT pollute parsed.argument.
        perm_res = await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                EmbeddedResourceContentBlock(
                    resource=TextResourceContents(uri=sample.resolve().as_uri(), text="def divide(a, b): return a / b")
                ),
                TextContentBlock(text="/permissions acceptEdits"),
            ],
        )
        assert perm_res.stop_reason == "end_turn"
        assert agent.sessions[sess.session_id].current_mode == "acceptEdits"

        # /review (mode: fork skill) with attached context block succeeds end-to-end.
        rev_res = await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                TextContentBlock(text="/review check zero division"),
                EmbeddedResourceContentBlock(
                    resource=TextResourceContents(uri=sample.resolve().as_uri(), text="def divide(a, b): return a / b")
                ),
            ],
        )
        assert rev_res.stop_reason == "end_turn"
        assert seen_prompts, "fork skill should have invoked provider"
        assert "check zero division" in seen_prompts[-1]
        assert "def divide(a, b): return a / b" in seen_prompts[-1]
        chunks = "".join(
            u.content.text for sid, u in client.updates if sid == sess.session_id and isinstance(u, AgentMessageChunk)
        )
        assert "Reviewed turn" in chunks

        # /plan with task + attached context block switches to plan mode and forwards context to model.
        plan_res = await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                TextContentBlock(text="/plan guard against b == 0"),
                EmbeddedResourceContentBlock(
                    resource=TextResourceContents(uri=sample.resolve().as_uri(), text="context_for_plan")
                ),
            ],
        )
        assert plan_res.stop_reason == "end_turn"
        assert agent.sessions[sess.session_id].current_mode == "plan"
        assert "guard against b == 0" in seen_prompts[-1]
        assert "context_for_plan" in seen_prompts[-1]
    finally:
        await cleanup()


async def test_bang_no_shell_escape_and_clear_keeps_session_id_on_reload(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    _write_user_config(home)

    received_user_messages: list[str] = []

    async def script(turn, messages, provider):
        received_user_messages.append([m.text for m in messages if m.role == "user" and not m.synthetic][-1])
        reply = f"Model saw: {received_user_messages[-1]}"
        yield Delta("text", reply)
        yield Completed(Message("assistant", (ContentBlock("text", reply),)), "stop")

    agent1 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client1 = RecordingClient()
    conn1, cleanup1 = await _connect_agent(agent1, client1)
    orig_id = ""
    try:
        await conn1.initialize(protocol_version=1)
        sess = await conn1.new_session(cwd=str(work), mcp_servers=[])
        orig_id = sess.session_id

        # !cmd and !!cmd in default mode and plan mode must NEVER execute a local shell command.
        marker = work / "pwned.txt"
        await conn1.prompt(session_id=orig_id, prompt=[TextContentBlock(text=f"!echo pwn > {marker.name}")])
        assert not marker.exists()
        assert received_user_messages[-1] == f"!echo pwn > {marker.name}"

        await conn1.set_session_mode(session_id=orig_id, mode_id="plan")
        await conn1.prompt(session_id=orig_id, prompt=[TextContentBlock(text=f"!!echo pwn2 > {marker.name}")])
        assert not marker.exists()
        assert received_user_messages[-1] == f"!!echo pwn2 > {marker.name}"

        # /clear extra must fail argument validation without clearing history.
        client1.updates.clear()
        bad_clear = await conn1.prompt(session_id=orig_id, prompt=[TextContentBlock(text="/clear extra")])
        assert bad_clear.stop_reason == "end_turn"
        err_text = "".join(u.content.text for _, u in client1.updates if isinstance(u, AgentMessageChunk))
        assert "不接受参数" in err_text
        assert len(agent1.sessions[orig_id].conversation.history) > 0

        # Valid /clear keeps the same session_id and truncates the transcript so subsequent turns stay under orig_id.
        ok_clear = await conn1.prompt(session_id=orig_id, prompt=[TextContentBlock(text="/clear")])
        assert ok_clear.stop_reason == "end_turn"
        assert agent1.sessions[orig_id].conversation.session_id == orig_id
        assert agent1.sessions[orig_id].conversation.history == ()

        post = await conn1.prompt(session_id=orig_id, prompt=[TextContentBlock(text="Post-clear message")])
        assert post.stop_reason == "end_turn"
    finally:
        await cleanup1()

    # Reloading orig_id on a fresh agent restores ONLY the post-/clear turn!
    agent2 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client2 = RecordingClient()
    conn2, cleanup2 = await _connect_agent(agent2, client2)
    try:
        await conn2.initialize(protocol_version=1)
        await conn2.load_session(cwd=str(work), session_id=orig_id, mcp_servers=[])
        user_replayed = [u.content.text for _, u in client2.updates if isinstance(u, UserMessageChunk)]
        assert user_replayed == ["Post-clear message"]
    finally:
        await cleanup2()


async def test_resume_session_silent_restore_and_list_sessions_metadata(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    _write_user_config(home)

    async def script1(turn, messages, provider):
        yield Completed(Message("assistant", (ContentBlock("text", "Saved answer"),)), "stop")

    agent1 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script1)))
    client1 = RecordingClient()
    conn1, cleanup1 = await _connect_agent(agent1, client1)
    saved_id = ""
    try:
        await conn1.initialize(protocol_version=1)
        s1 = await conn1.new_session(cwd=str(work), mcp_servers=[])
        saved_id = s1.session_id
        await conn1.prompt(session_id=saved_id, prompt=[TextContentBlock(text="Initial topic")])
    finally:
        await cleanup1()

    # Also create an untitled transcript (header only) to verify title -> None mapping.
    from mosscode.session import Session

    untitled_sess = Session.new(work.resolve(), home=home)
    untitled_id = untitled_sess.id
    untitled_sess.transcript_path.parent.mkdir(parents=True, exist_ok=True)
    untitled_sess.transcript_path.write_text(
        json.dumps({"type": "session", "v": 1, "id": untitled_id, "cwd": str(work.resolve())}) + "\n",
        encoding="utf-8",
    )

    saw_user_texts: list[str] = []

    async def script2(turn, messages, provider):
        saw_user_texts.extend(m.text for m in messages if m.role == "user" and not m.synthetic)
        yield Completed(Message("assistant", (ContentBlock("text", "Continued"),)), "stop")

    agent2 = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script2)))
    client2 = RecordingClient()
    conn2, cleanup2 = await _connect_agent(agent2, client2)
    try:
        await conn2.initialize(protocol_version=1)
        listed = await conn2.list_sessions(cwd=str(work))
        by_id = {s.session_id: s for s in listed.sessions}
        assert by_id[saved_id].title == "Initial topic"
        assert by_id[untitled_id].title is None
        # updated_at must carry timezone offset and second precision.
        assert re.search(r"[+-]\d{2}:\d{2}$|Z$", by_id[saved_id].updated_at or "")

        client2.updates.clear()
        resumed = await conn2.resume_session(session_id=saved_id, cwd=str(work), mcp_servers=[])
        assert resumed.modes.current_mode_id == "default"
        await asyncio.sleep(0.02)

        # No historical UserMessageChunk or AgentMessageChunk should be emitted by session/resume.
        assert not any(isinstance(u, (UserMessageChunk, AgentMessageChunk)) for _, u in client2.updates)
        assert any(isinstance(u, AvailableCommandsUpdate) for _, u in client2.updates)

        await conn2.prompt(session_id=saved_id, prompt=[TextContentBlock(text="Follow-up")])
        assert "Initial topic" in saw_user_texts and "Follow-up" in saw_user_texts
    finally:
        await cleanup2()

    # When persist_sessions is false, list_sessions returns [].
    home_nopersist = tmp_path / "home_nopersist"
    _write_user_config(home_nopersist, persist=False)
    agent3 = MossCodeAcpAgent(AcpLaunchOptions(home=home_nopersist))
    client3 = RecordingClient()
    conn3, cleanup3 = await _connect_agent(agent3, client3)
    try:
        await conn3.initialize(protocol_version=1)
        empty_list = await conn3.list_sessions(cwd=str(work))
        assert empty_list.sessions == []
    finally:
        await cleanup3()


async def test_uri_to_path_and_resource_security_hardening(tmp_path):
    from mosscode.acp.session import _uri_to_path

    home = tmp_path / "home"
    work = tmp_path / "work"
    outside_dir = tmp_path / "outside"
    work.mkdir()
    outside_dir.mkdir()
    _write_user_config(home)

    inside_literal_pct = work / "a%20b.txt"
    inside_literal_pct.write_text("PERCENT_FILE", encoding="utf-8")
    outside_file = outside_dir / "ext.txt"
    outside_file.write_text("OUTSIDE_SECRET_CONTENT", encoding="utf-8")

    # 1. Single-slash file:/... (JetBrains/JVM style) and uppercase FILE://...
    single_slash = "file:/" + outside_file.resolve().as_posix().lstrip("/")
    assert _uri_to_path(single_slash) == outside_file.resolve()
    upper_uri = "FILE://" + outside_file.resolve().as_uri()[len("file://") :]
    assert _uri_to_path(upper_uri) == outside_file.resolve()

    # 2. %2520 must decode once to literal "%20", never double-unquote to space.
    pct_uri = inside_literal_pct.resolve().as_uri()
    assert "%2520" in pct_uri
    assert _uri_to_path(pct_uri) == inside_literal_pct.resolve()

    # 3. Null byte (%00 or literal \0) and relative file: URIs fail closed (return None, no crash).
    assert _uri_to_path(pct_uri + "%00.txt") is None
    assert _uri_to_path("file:///bad\0path") is None
    assert _uri_to_path("file:relative/path.txt") is None
    if sys.platform == "win32":
        assert _uri_to_path("file:/C:/Windows/System32") is not None
    else:
        assert _uri_to_path("file://remote-host/share/file.txt") is None

    # 4. Create a link inside ~/.ssh pointing to outside_dir so resolved path is outside ~/.ssh!
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_link_dir = ssh_dir / "linked_out"
    try:
        ssh_link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(outside_dir), str(ssh_link_dir))
        else:
            raise
    symlinked_target = ssh_link_dir / "ext.txt"
    # Lexical path is inside ~/.ssh while resolved() points to outside_dir/ext.txt!
    symlink_uri = "file:/" + symlinked_target.as_posix().lstrip("/")

    # Valid tiny 1x1 PNG base64 for BlobResourceContents / ImageContentBlock URI checks.
    tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

    captured_messages: list[tuple[Message, ...]] = []

    async def script(turn, messages, provider):
        captured_messages.append(tuple(messages))
        yield Completed(Message("assistant", (ContentBlock("text", "ok"),)), "stop")

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])

        # Symlink inside ~/.ssh pointing outside ~/.ssh must still be refused!
        await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                TextContentBlock(text="check symlink"),
                EmbeddedResourceContentBlock(resource=TextResourceContents(uri=symlink_uri, text="LEAKED_VIA_SYMLINK")),
            ],
        )
        joined_1 = "\n".join(m.text for m in captured_messages[-1] if m.role == "user")
        assert "LEAKED_VIA_SYMLINK" not in joined_1
        assert "受保护的凭据路径" in joined_1

        # Malformed file: URI with %00 must fail closed and NEVER inject res.text!
        await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                TextContentBlock(text="check null"),
                EmbeddedResourceContentBlock(
                    resource=TextResourceContents(uri="file:///tmp/evil%00.txt", text="LEAKED_VIA_NULL_URI")
                ),
            ],
        )
        joined_2 = "\n".join(m.text for m in captured_messages[-1] if m.role == "user")
        assert "LEAKED_VIA_NULL_URI" not in joined_2
        assert "不合法的 file URI" in joined_2

        # Outside-workspace ResourceContentBlock must NOT emit @<abs_path> or auto-read outside_file!
        await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                TextContentBlock(text="inspect outside ref"),
                ResourceContentBlock(name="ext.txt", uri=outside_file.resolve().as_uri()),
            ],
        )
        joined_3 = "\n".join(m.text for m in captured_messages[-1] if m.role == "user")
        assert "OUTSIDE_SECRET_CONTENT" not in joined_3
        assert "外部文件引用" in joined_3

        # BlobResourceContents and ImageContentBlock with protected ~/.ssh file: URIs must be blocked!
        secret_img_uri = "file:/" + (ssh_dir / "secret.png").as_posix().lstrip("/")
        await conn.prompt(
            session_id=sess.session_id,
            prompt=[
                TextContentBlock(text="inspect images"),
                EmbeddedResourceContentBlock(
                    resource=BlobResourceContents(uri=secret_img_uri, mime_type="image/png", blob=tiny_png)
                ),
                ImageContentBlock(data=tiny_png, mime_type="image/png", uri=secret_img_uri),
            ],
        )
        last_user = [m for m in captured_messages[-1] if m.role == "user" and not m.synthetic][-1]
        assert not any(b.kind == "image" for b in last_user.content)
        assert "受保护的凭据路径" in last_user.text
    finally:
        await cleanup()


async def test_multi_session_sandbox_and_command_env_isolation(tmp_path):
    from mosscode.command_env import command_environment
    from mosscode.config import ProviderConfig, SandboxConfig, Settings
    from mosscode.sandbox import current as current_sandbox

    home = tmp_path / "home"
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    provider_cfg = ProviderConfig("fake", "anthropic-messages", "fake-model", api_key="sk-1234567890")
    settings_a = Settings(
        providers=(provider_cfg,),
        default_provider="fake",
        sandbox=SandboxConfig(mode="on"),
        command_env_drop=("CUSTOM_DROP_VAR",),
    )
    settings_b = Settings(
        providers=(provider_cfg,),
        default_provider="fake",
        sandbox=SandboxConfig(mode="off"),
        command_env_drop=(),
    )

    observed: dict[str, tuple[bool, bool]] = {}

    def make_provider(tag: str):
        async def script(turn, messages, provider):
            env = command_environment({"CUSTOM_DROP_VAR": "sensitive_val", "KEEP_REGULAR": "1"})
            observed[tag] = (current_sandbox().enabled, "CUSTOM_DROP_VAR" in env)
            yield Completed(Message("assistant", (ContentBlock("text", f"ran {tag}"),)), "stop")

        return ScriptedProvider(script)

    client = RecordingClient()
    from mosscode.acp.session import AcpSession

    sess_a = AcpSession.create(
        cwd=dir_a,
        client=client,
        options=AcpLaunchOptions(home=home, settings=settings_a, provider_factory=lambda _c: make_provider("A")),
    )
    # Creating Session B with sandbox=off and empty drop list must NOT overwrite Session A!
    sess_b = AcpSession.create(
        cwd=dir_b,
        client=client,
        options=AcpLaunchOptions(home=home, settings=settings_b, provider_factory=lambda _c: make_provider("B")),
    )
    try:
        assert sess_a.sandbox.enabled is True
        assert sess_b.sandbox.enabled is False
        assert sess_a.conversation.registry.get("execute_command").sandboxed is True
        assert sess_b.conversation.registry.get("execute_command").sandboxed is False

        await sess_a.prompt([TextContentBlock(text="check A")])
        await sess_b.prompt([TextContentBlock(text="check B")])
        # Run A again after B ran to prove B's prompt did not mutate A's state either.
        await sess_a.prompt([TextContentBlock(text="check A again")])

        assert observed["A"] == (True, False)
        assert observed["B"] == (False, True)
    finally:
        await sess_a.close()
        await sess_b.close()


async def test_busy_error_code_in_progress_after_approval_and_thinking_validation(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    target = work / "item.txt"
    target.write_text("old\n", encoding="utf-8")

    cfg_dir = home / ".mosscode"
    cfg_dir.mkdir(parents=True)
    cfg_dir.joinpath("config.yaml").write_text(
        "default_provider: p_no_think\nproviders:\n"
        "  - name: p_no_think\n    protocol: openai-chat-completions\n    model: m1\n"
        "    api_key: sk-11111111\n    reasoning: false\n",
        encoding="utf-8",
    )

    perm_gate = asyncio.Event()
    release_gate = asyncio.Event()

    async def gated_decider(_sid, _tc, _opts):
        perm_gate.set()
        await release_gate.wait()
        return AllowedOutcome(option_id="once")

    async def script(turn, messages, provider):
        if turn == 1:
            calls = (ToolCall("call-w", "write_file", '{"path": "created.txt", "content": "new\\n"}'),)
            yield Completed(Message("assistant", (), tool_calls=calls), "tool_use")
        else:
            yield Completed(Message("assistant", (ContentBlock("text", "done"),)), "stop")

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client = RecordingClient(gated_decider)
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])

        # Unsupported thinking level on reasoning=false provider raises -32602 Invalid params.
        with pytest.raises(RequestError) as bad_think:
            await conn.set_config_option(config_id="thought_level", session_id=sess.session_id, value="high")
        assert bad_think.value.code == -32602

        # Concurrent prompt / set_session_mode while busy raises -32001 (never -32600).
        running_prompt = asyncio.create_task(
            conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="write item")])
        )
        await asyncio.wait_for(perm_gate.wait(), timeout=5)

        with pytest.raises(RequestError) as busy_prompt:
            await conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="concurrent")])
        assert busy_prompt.value.code == -32001

        with pytest.raises(RequestError) as busy_mode:
            await conn.set_session_mode(session_id=sess.session_id, mode_id="plan")
        assert busy_mode.value.code == -32001

        release_gate.set()
        res = await asyncio.wait_for(running_prompt, timeout=5)
        assert res.stop_reason == "end_turn"

        # M4: after approval, ToolCallProgress(status="in_progress") is sent before status="completed".
        statuses = [
            u.status for _, u in client.updates if isinstance(u, ToolCallProgress) and u.tool_call_id == "call-w"
        ]
        assert statuses == ["in_progress", "completed"]
    finally:
        await cleanup()


async def test_background_notifications_synthetic_fork_and_auth_cwd_isolation(tmp_path, monkeypatch):
    from mosscode.messages import NOTIFICATION

    home = tmp_path / "home"
    work = tmp_path / "work"
    broken_proc_cwd = tmp_path / "broken_cwd"
    work.mkdir()
    broken_proc_cwd.mkdir()
    (broken_proc_cwd / ".mosscode").mkdir()
    # Untrusted / broken YAML in process Path.cwd() must not break authenticate() when user config is valid!
    (broken_proc_cwd / ".mosscode" / "config.yaml").write_text(":::invalid_yaml:::\n", encoding="utf-8")

    cfg_dir = home / ".mosscode"
    cfg_dir.mkdir(parents=True)
    cfg_dir.joinpath("config.yaml").write_text(
        "default_provider: main_p\ngoal_provider: eval_p\nproviders:\n"
        "  - name: main_p\n    protocol: anthropic-messages\n    model: m1\n    api_key: sk-11111111\n"
        "  - name: eval_p\n    protocol: anthropic-messages\n    model: m2\n    api_key: sk-22222222\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(broken_proc_cwd)

    created_models: list[str] = []

    def factory(cfg):
        created_models.append(cfg.name)

        async def script(turn, messages, provider):
            out = f"reply-{cfg.name}-{provider.calls}"
            yield Delta("text", out)
            yield Completed(Message("assistant", (ContentBlock("text", out),)), "stop")

        return ScriptedProvider(script)

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=factory))
    client = RecordingClient()
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        await conn.authenticate(method_id="local-config")
        sess = await conn.new_session(cwd=str(work), mcp_servers=[])
        acp_session = agent.sessions[sess.session_id]
        assert acp_session.conversation.goal_provider is not None
        assert "eval_p" in created_models

        # Queue a background task notification while session is idle, then run /review (fork mode)!
        acp_session.conversation._agent.notifications.append("<task-notification>bg-task-ok</task-notification>")
        res = await conn.prompt(session_id=sess.session_id, prompt=[TextContentBlock(text="/review check changes")])
        assert res.stop_reason == "end_turn"

        # Notification must be recorded in main history with synthetic=NOTIFICATION, never synthetic=None,
        # and must not be lost despite /review running in fork mode.
        history = acp_session.conversation.history
        notif_msgs = [m for m in history if "bg-task-ok" in m.text]
        assert len(notif_msgs) == 1
        assert notif_msgs[0].synthetic == NOTIFICATION
    finally:
        await cleanup()
