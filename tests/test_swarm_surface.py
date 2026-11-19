"""Tests for /swarm, dynamic coordinator mode, L1 TUI/ACP approval wiring, and ACP team support."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    DeniedOutcome,
    TextContentBlock,
)
from mcp import types as mcp_types
from textual.widgets import Static

from mosscode import coordinator
from mosscode.acp.app import MossCodeAcpAgent
from mosscode.acp.session import AcpLaunchOptions
from mosscode.agents import AgentCatalog
from mosscode.builtin_commands import BUILTIN, dispatch, status_report
from mosscode.commands import CommandContext, parse_input
from mosscode.config import render_config
from mosscode.conversation import Conversation
from mosscode.mcp_client import MCPTool, ServerConnection
from mosscode.mcp_config import ServerConfig
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError, ToolCall, Usage
from mosscode.permissions import ApprovalRequest, Operation
from mosscode.tools import create_default_registry
from mosscode.tools.skill import SkillTool
from mosscode.tui import MossCodeApp
from test_acp import RecordingClient, ScriptedProvider, _connect_agent
from test_commands import FakeUI


class _ReplyProvider:
    def __init__(self):
        self.requests: list[tuple[Any, ...]] = []
        self.thinking: str | None = None

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append((tuple(messages), system, tuple(tools), allow_tools))
        yield Delta("text", "ok")
        yield Completed(
            Message("assistant", (ContentBlock("text", "ok"),)),
            "stop",
            usage=Usage(input_tokens=1200, output_tokens=20),
        )

    async def close(self):
        pass


def _make_team_conversation(tmp_path: Path, provider=None, *, coordinator_on: bool = False) -> Conversation:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    registry = create_default_registry(project, load_permissions=False)
    agents = AgentCatalog.load(project, home=home)
    return Conversation(
        provider or _ReplyProvider(),
        registry=registry,
        context_root=project,
        sessions_home=home,
        agents=agents,
        teams=True,
        teammate_mode="in-process",
        coordinator=coordinator_on,
    )


def _skill_tool(root: Path) -> SkillTool:
    skill = SimpleNamespace(name="deploy", directory=root, plugin=None)
    spec = SimpleNamespace(
        name="run",
        full_name="skill__deploy__run",
        description="run deploy",
        input_schema={"type": "object", "properties": {}},
        command=["python"],
        timeout=5,
    )
    return SkillTool(root, skill, spec)


def _write_acp_config(home: Path) -> Path:
    cfg_dir = home / ".mosscode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.yaml"
    cfg.write_text(
        render_config(
            {
                "name": "fake",
                "protocol": "anthropic-messages",
                "model": "fake-model",
                "api_key": "sk-test-1234567890",
            }
        ),
        encoding="utf-8",
    )
    return cfg


async def _run_cmd(conv: Conversation, text: str, ui: FakeUI | None = None) -> FakeUI:
    target_ui = ui or FakeUI()
    ctx = CommandContext(conv, target_ui, BUILTIN, None)
    parsed = parse_input(text, BUILTIN)
    assert parsed.kind == "command"
    await dispatch(ctx, parsed)
    return target_ui


def test_coordinator_playbook_and_backwards_compatible_api():
    assert coordinator.BADGE == "[协调]"
    assert coordinator.WRITE_TOOLS == frozenset({"write_file", "edit_file"})
    assert coordinator.decide(True, {coordinator.ENV: "1"}, interactive=True) == (True, "")
    assert coordinator.decide(True, {}, interactive=True) == (False, coordinator.NO_ENV)
    assert coordinator.decide(False, {coordinator.ENV: "yes"}, interactive=True) == (False, coordinator.NO_CONFIG)
    assert coordinator.decide(True, {coordinator.ENV: "true"}, interactive=False) == (False, coordinator.HEADLESS)
    assert "## 协调者模式" in coordinator.SECTION
    assert "派发后不要自己动手" in coordinator.SECTION
    for expected in ("post_board", "read_board", 'role: "verifier"', "team_merge", "[done]"):
        assert expected in coordinator.SECTION
    assert coordinator.SWARM_SECTION == coordinator.SECTION


async def test_dynamic_coordinator_mode_toggles_tools_playbook_and_reanchors_meter(tmp_path):
    provider = _ReplyProvider()
    conv = _make_team_conversation(tmp_path, provider)
    root = conv.registry.permissions.root
    connection = ServerConnection(root, ServerConfig("docs", "stdio", "python"))
    conv.registry.register(MCPTool(root, connection, mcp_types.Tool(name="write", inputSchema={"type": "object"})))
    conv.registry.register(_skill_tool(root))

    try:
        # Turn 1 in normal mode: write/edit/MCP/Skill tools are offered and usage anchors ContextMeter.
        async with aclosing(conv.reply("normal turn")) as stream:
            async for _ in stream:
                pass
        assert conv.coordinator_mode is False
        normal_tools = {t.name for t in provider.requests[-1][2]}
        assert {"write_file", "edit_file", "mcp__docs__write", "skill__deploy__run"} <= normal_tools
        assert conv._agent.meter.anchor is not None

        # Enable coordinator mode dynamically: tools withheld, playbook injected, ContextMeter invalidated.
        assert conv.set_coordinator_mode(True) is True
        assert conv.set_coordinator_mode(True) is False
        assert conv.coordinator is True and conv.coordinator_mode is True
        assert conv._agent.meter.anchor is None and conv._agent._anchored_tools is None

        offered_coord = {d.name for d in conv.registry.definitions()}
        assert not ({"write_file", "edit_file", "mcp__docs__write", "skill__deploy__run"} & offered_coord)
        assert {"read_file", "execute_command", "team_create", "team_spawn", "team_merge"} <= offered_coord

        refused = await conv.registry.execute(ToolCall("c-w", "write_file", '{"path": "x.txt", "content": "no"}'))
        assert refused.is_error and refused.code == "coordinator_withheld"
        assert not (root / "x.txt").exists()

        # Sub-agent / member registries built via for_agent still receive write_file and edit_file.
        member_reg = conv.registry.for_agent(
            agent="teammate",
            agent_type="general",
            permissions=conv.registry.permissions,
        )
        assert {"write_file", "edit_file"} <= {d.name for d in member_reg.definitions()}

        async with aclosing(conv.reply("coord turn")) as stream:
            async for _ in stream:
                pass
        assert "## 协调者模式" in provider.requests[-1][1].stable
        assert "post_board" in provider.requests[-1][1].stable
        assert conv._agent.meter.anchor is not None

        # Turning coordinator mode OFF restores withheld tools, removes playbook, and invalidates anchor again.
        assert conv.set_coordinator_mode(False) is True
        assert conv.coordinator is False and conv.coordinator_mode is False
        assert conv._agent.meter.anchor is None and conv._agent._anchored_tools is None
        restored = {d.name for d in conv.registry.definitions()}
        assert {"write_file", "edit_file", "mcp__docs__write", "skill__deploy__run"} <= restored
        assert "## 协调者模式" not in conv._agent.team_guide

        ok_res = await conv.registry.execute(ToolCall("c-w2", "write_file", '{"path": "ok.txt", "content": "yes"}'))
        assert ok_res.is_error is False or ok_res.code != "coordinator_withheld"

        # run_swarm_turn enables coordinator mode and sets /swarm command label on the user message.
        async with aclosing(conv.run_swarm_turn("协调修复队列模块")) as stream:
            async for _ in stream:
                pass
        assert conv.coordinator_mode is True
        assert conv.history[-2].command == "/swarm 协调修复队列模块"
        conv.set_coordinator_mode(False)

        # Busy conversations reject toggling, and conversations without teams reject enabling.
        conv._agent.busy = True
        with pytest.raises(ProviderError, match="正在运行"):
            conv.set_coordinator_mode(True)
        conv._agent.busy = False

        headless_conv = Conversation(_ReplyProvider(), registry=create_default_registry(root, load_permissions=False))
        with pytest.raises(ProviderError, match="交互界面"):
            headless_conv.set_coordinator_mode(True)
        await headless_conv.close()
    finally:
        await conv.close()


async def test_swarm_and_team_commands_with_blackboard_entries_and_tui_badge(tmp_path):
    provider = _ReplyProvider()
    conv = _make_team_conversation(tmp_path, provider)
    try:
        # Headless / non-team conversation shows clear Chinese notice on /swarm on and /swarm <task>.
        plain = Conversation(_ReplyProvider())
        plain_ui = await _run_cmd(plain, "/swarm 重构认证模块")
        assert plain_ui.calls == []
        assert any("交互界面" in n for n in plain_ui.notes)
        await plain.close()

        # /swarm status when off, then /swarm <task> turns coordinator ON across turns until /swarm off.
        ui0 = await _run_cmd(conv, "/swarm")
        assert "未开启" in ui0.notes[-1]

        ui_task = await _run_cmd(conv, "/swarm 拆分并修复解析器")
        assert conv.coordinator_mode is True
        assert any("已开启 Swarm 协调模式（可用 /swarm off 关闭）" in n for n in ui_task.notes)
        assert ("submit", "拆分并修复解析器", "/swarm 拆分并修复解析器") in ui_task.calls
        assert coordinator.DYNAMIC_STATUS in status_report(CommandContext(conv, FakeUI(), BUILTIN, None))

        # Create a team and attach mock blackboard entries via store.blackboard to verify /swarm status and /team.
        await conv.teams.create("alpha", "解析器修复")
        store = conv.teams.current
        assert store is not None
        fake_posts = [
            SimpleNamespace(seq=1, author="lead", summary="拆分任务 A 与 B", body="brief"),
            SimpleNamespace(seq=2, author="worker-a", summary="完成 parser.py @verifier", body="done #1"),
        ]
        store.blackboard = SimpleNamespace(read_since=lambda since=0, limit=20: (fake_posts, 2))

        ui_status = await _run_cmd(conv, "/swarm status")
        assert "已开启" in ui_status.notes[-1]
        assert "alpha" in ui_status.notes[-1] and "#2 · worker-a: 完成 parser.py @verifier" in ui_status.notes[-1]

        ui_team_list = await _run_cmd(conv, "/team")
        assert "#2 · worker-a: 完成 parser.py @verifier" in ui_team_list.notes[-1]

        ui_team_detail = await _run_cmd(conv, "/team alpha")
        assert "#1 · lead: 拆分任务 A 与 B" in ui_team_detail.notes[-1]
        assert "#2 · worker-a: 完成 parser.py @verifier" in ui_team_detail.notes[-1]

        # TUI status badge reflects /swarm off and /swarm on dynamically.
        app = MossCodeApp(conv, "test")
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.pause()
            assert coordinator.BADGE in str(app.query_one("#status", Static).render())

            await _run_cmd(conv, "/swarm off", ui=app)
            await pilot.pause()
            assert conv.coordinator_mode is False
            assert coordinator.BADGE not in str(app.query_one("#status", Static).render())

            await _run_cmd(conv, "/swarm on", ui=app)
            await pilot.pause()
            assert conv.coordinator_mode is True
            assert coordinator.BADGE in str(app.query_one("#status", Static).render())
    finally:
        await conv.close()


async def test_tui_subagent_approval_bubbling_and_withdrawal(tmp_path):
    conv = _make_team_conversation(tmp_path)
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        assert conv.subagents.on_approval == app._member_approval
        assert conv.subagents.on_approval_settled == app._withdraw_approval

        policy = conv.registry.permissions
        fut1 = asyncio.get_running_loop().create_future()
        fut2 = asyncio.get_running_loop().create_future()
        policy.pending["req-1"] = fut1
        policy._choices["req-1"] = ("once", "session", "deny")
        policy.pending["req-2"] = fut2
        policy._choices["req-2"] = ("once", "deny")

        req1 = ApprovalRequest(
            "req-1",
            "call-1",
            Operation("edit_file", "a.py", preview="-old\n+new"),
            "修改工作区文件",
            ("once", "session", "deny"),
            source="子 Agent t1（fork · 修改 a.py）",
        )
        req2 = ApprovalRequest(
            "req-2",
            "call-2",
            Operation("write_file", "b.py"),
            "新建工作区文件",
            ("once", "deny"),
            source="子 Agent t2（general · 新建 b.py）",
        )

        # Both arrive from background sub-agents: req-1 shows immediately, req-2 queues behind it.
        conv.subagents.on_approval(req1)
        conv.subagents.on_approval(req2)
        await pilot.pause()
        assert app.query_one("#approval-panel").display is True
        assert app._approval_request is not None and app._approval_request.id == "req-1"
        assert [r.id for r in app._approval_queue] == ["req-2"]

        # Simulate task_stop cancelling t1 -> on_approval_settled("req-1") withdraws req-1 and promotes req-2!
        fut1.cancel()
        policy.pending.pop("req-1", None)
        conv.subagents.on_approval_settled("req-1")
        await pilot.pause()
        assert app._approval_request is not None and app._approval_request.id == "req-2"
        assert app.query_one("#approval-panel").display is True

        # User approves req-2 in TUI; then settling req-2 is idempotent; panel closes cleanly.
        await pilot.press("enter")
        await pilot.pause()
        assert fut2.result() == "once"
        policy.pending.pop("req-2", None)
        conv.subagents.on_approval_settled("req-2")
        await pilot.pause()
        assert app._approval_request is None
        assert app.query_one("#approval-panel").display is False
    await conv.close()


async def test_acp_swarm_team_commands_background_approvals_and_idle_turn_framing(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    _write_acp_config(home)

    seen_tools_by_turn: list[set[str]] = []
    bg_perm_entered = asyncio.Event()
    bg_perm_release = asyncio.Event()

    async def decider(_sid, tool_call, options):
        if tool_call.name == "team_delete":
            return AllowedOutcome(option_id="delete")
        if tool_call.tool_call_id.startswith("sub:cancel-me"):
            bg_perm_entered.set()
            await bg_perm_release.wait()
            return DeniedOutcome()
        return AllowedOutcome(option_id="once")

    session_holder: dict[str, Any] = {}

    async def script(turn, messages, provider):
        last_user = next((m.text for m in reversed(messages) if m.role == "user"), "")
        seen_tools_by_turn.append({t.name for t in provider.requests[-1][2]})

        # Simulate a background Fork sub-agent requesting permission mid-turn, plus a second one withdrawn via task_stop.
        if "trigger-bg-approval" in last_user and turn == 1:
            acp_sess = session_holder["session"]
            policy = acp_sess.policy
            req_ok = ApprovalRequest(
                "bg-ok",
                "fork-call-1",
                Operation("write_file", "forked.txt", preview="+hello"),
                "写入文件",
                ("once", "deny"),
                source="子 Agent t1（fork）",
            )
            fut_ok = policy.hold(req_ok)
            acp_sess.conversation.subagents.on_approval(req_ok)
            choice = await asyncio.wait_for(fut_ok, timeout=5)
            policy.release(req_ok)
            acp_sess.conversation.subagents.on_approval_settled("bg-ok")

            # Also fire an approval that gets withdrawn by task_stop while request_permission is awaiting!
            req_cancel = ApprovalRequest(
                "bg-cancel",
                "cancel-me",
                Operation("edit_file", "other.txt"),
                "编辑文件",
                ("once", "deny"),
                source="子 Agent t2（fork）",
            )
            fut_cancel = policy.hold(req_cancel)
            acp_sess.conversation.subagents.on_approval(req_cancel)
            await asyncio.wait_for(bg_perm_entered.wait(), timeout=5)
            fut_cancel.cancel()
            policy.release(req_cancel)
            acp_sess.conversation.subagents.on_approval_settled("bg-cancel")
            await asyncio.sleep(0)
            assert "bg-cancel" not in acp_sess._bg_approval_tasks

            reply = f"bg-choice={choice}"
            yield Delta("text", reply)
            yield Completed(Message("assistant", (ContentBlock("text", reply),)), "stop")
            return

        yield Delta("text", f"done-{provider.calls}")
        yield Completed(Message("assistant", (ContentBlock("text", f"done-{provider.calls}"),)), "stop")

    agent = MossCodeAcpAgent(AcpLaunchOptions(home=home, provider_factory=lambda _c: ScriptedProvider(script)))
    client = RecordingClient(decider)
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        created = await conn.new_session(cwd=str(work), mcp_servers=[])
        sid = created.session_id
        acp_session = agent.sessions[sid]
        session_holder["session"] = acp_session

        # TeamRuntime is mounted in-process, and both /swarm and /team are advertised in available_commands.
        assert acp_session.conversation.teams is not None
        assert acp_session.conversation.teams.teammate_mode == "in-process"
        for _ in range(20):
            if any(isinstance(u, AvailableCommandsUpdate) for _, u in client.updates):
                break
            await asyncio.sleep(0.01)
        cmd_update = next(u for _, u in reversed(client.updates) if isinstance(u, AvailableCommandsUpdate))
        cmd_names = {c.name for c in cmd_update.available_commands}
        assert {"swarm", "team"} <= cmd_names

        # Background approval bridging and withdrawal during an active prompt turn.
        res_bg = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="trigger-bg-approval")])
        assert res_bg.stop_reason == "end_turn"
        chunks = "".join(u.content.text for s, u in client.updates if s == sid and isinstance(u, AgentMessageChunk))
        assert "bg-choice=once" in chunks
        assert any(tc.name == "write_file" for _, tc, _ in client.permission_requests)

        # /swarm <task> enables coordinator mode (withholding write_file/edit_file from Lead) and runs task;
        # /swarm off restores write_file/edit_file.
        seen_tools_by_turn.clear()
        res_swarm = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/swarm 协调多成员重构")])
        assert res_swarm.stop_reason == "end_turn"
        assert acp_session.conversation.coordinator_mode is True
        assert not ({"write_file", "edit_file"} & seen_tools_by_turn[-1])

        res_off = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/swarm off")])
        assert res_off.stop_reason == "end_turn"
        assert acp_session.conversation.coordinator_mode is False

        await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="normal after swarm off")])
        assert {"write_file", "edit_file"} <= seen_tools_by_turn[-1]

        # /team and /team delete <name> work over ACP.
        await acp_session.conversation.teams.create("acp-team", "测试团队")
        client.updates.clear()
        await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/team acp-team")])
        team_md = "".join(u.content.text for s, u in client.updates if s == sid and isinstance(u, AgentMessageChunk))
        assert "acp-team" in team_md

        del_res = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/team delete acp-team")])
        assert del_res.stop_reason == "end_turn"
        assert acp_session.conversation.teams.current is None

        # Strict idle framing invariant: while idle between prompt RPCs, team notes/updates and approvals
        # NEVER emit unsolicited session/update or session/request_permission frames!
        updates_before_idle = len(client.updates)
        perms_before_idle = len(client.permission_requests)

        acp_session._on_team_note("团队 idle-team：watchdog 停滞提醒")
        acp_session.conversation._agent.notifications.append("<team-update>\n团队 idle-team：[stall]\n</team-update>")
        acp_session.conversation._tasks_ready()

        deferred_req = ApprovalRequest(
            "idle-req",
            "idle-call",
            Operation("write_file", "later.txt"),
            "写入文件",
            ("once", "deny"),
            source="成员 bob",
        )
        idle_fut = acp_session.policy.hold(deferred_req)
        acp_session.conversation.teams.on_approval(deferred_req)
        await asyncio.sleep(0.05)

        assert len(client.updates) == updates_before_idle
        assert len(client.permission_requests) == perms_before_idle
        assert not idle_fut.done()

        # On the next prompt RPC, both the queued team update and deferred approval are processed cleanly!
        res_next = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="继续跟进")])
        assert res_next.stop_reason == "end_turn"
        assert await asyncio.wait_for(idle_fut, timeout=2) == "once"
        acp_session.policy.release(deferred_req)
        assert len(client.permission_requests) == perms_before_idle + 1
    finally:
        bg_perm_release.set()
        await cleanup()
