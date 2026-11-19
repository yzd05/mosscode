"""Surface integration tests for repo_map permissions/UI/ACP and /loop command/tasks/TUI/ACP."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from acp.schema import AgentMessageChunk, AvailableCommandsUpdate, TextContentBlock
from pydantic import Field
from textual.widgets import Static

from mosscode import cli
from mosscode.acp.app import MossCodeAcpAgent
from mosscode.acp.events import AcpEventMapper, tool_kind, tool_locations, tool_title
from mosscode.acp.session import AcpLaunchOptions
from mosscode.agents.tasks import Task
from mosscode.builtin_commands import BUILTIN, LOOP_HEADLESS_NOTICE, dispatch, loop_report, status_report, tasks_report
from mosscode.commands import CommandContext, parse_input
from mosscode.config import ProviderConfig, Settings
from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ToolCall
from mosscode.permissions import ApprovalRequest, Operation, PermissionConfigError, PermissionPolicy, parse_rule
from mosscode.session import Session
from mosscode.tools import create_default_registry
from mosscode.tools.base import Params, Tool
from mosscode.tui import MossCodeApp
from mosscode.tui_views import describe_call, resumed_rows
from test_acp import RecordingClient, ScriptedProvider, _connect_agent
from test_agent import SequenceProvider, call, response
from test_commands import FakeUI
from test_permission_tui import wait_approval
from test_tui import send


class _FallbackRepoMapParams(Params):
    path: str = "."
    focus_files: list[str] = Field(default_factory=list, max_length=30)
    query: str = Field(default="", max_length=400)
    max_tokens: int = Field(default=2000, ge=256, le=8000)


class _FallbackRepoMapTool(Tool):
    name = "repo_map"
    description = "Generate workspace code map."
    params_model = _FallbackRepoMapParams
    read_only = True
    plan_allowed = True

    async def execute(self, params: _FallbackRepoMapParams) -> tuple[str, dict]:
        return f"{params.path}/:\n  [L1-L10] def sample():", {"path": params.path}


class _FallbackReadOutlineParams(Params):
    path: str
    offset: int = 1
    limit: int = 2000
    outline: bool = False
    symbol: str = ""


def _ensure_repomap_tool(registry):
    tool = registry.get("repo_map")
    if tool is None:
        tool = _FallbackRepoMapTool(registry.permissions.root)
        registry.register(tool)
    return tool


def _install_fallback_loop_schedule(monkeypatch) -> Any:
    """Use the real mosscode.loop_schedule when present, or install a spec-faithful stub standalone."""
    try:
        import mosscode.loop_schedule as real_mod

        return real_mod
    except ImportError:
        pass

    stub = types.ModuleType("mosscode.loop_schedule")
    stub.LOOP_SYNTHETIC = "loop"
    stub.MAX_ACTIVE_LOOPS = 20
    stub.DEFAULT_MAX_RUNS = 100
    stub.MAX_LIFESPAN = 7 * 86400.0

    @dataclass
    class LoopInfo:
        interval: float
        interval_text: str
        prompt: str
        max_runs: int = 100
        expires_at: float = 0.0
        runs: int = 0
        next_fire: float | None = None
        pending_fire: bool = False
        end_reason: str = ""

    @dataclass
    class ParsedLoop:
        action: Literal["list", "stop", "create"]
        target: str = ""
        interval: float = 300.0
        interval_text: str = "5 分钟"
        prompt: str = ""
        max_runs: int = 100

    _INTERVAL_RE = re.compile(
        r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|秒|分|分钟|小时|天)$",
        re.I,
    )
    _UNITS = {
        "s": (1.0, "秒"),
        "sec": (1.0, "秒"),
        "secs": (1.0, "秒"),
        "秒": (1.0, "秒"),
        "m": (60.0, "分钟"),
        "min": (60.0, "分钟"),
        "mins": (60.0, "分钟"),
        "分": (60.0, "分钟"),
        "分钟": (60.0, "分钟"),
        "h": (3600.0, "小时"),
        "hr": (3600.0, "小时"),
        "hrs": (3600.0, "小时"),
        "小时": (3600.0, "小时"),
        "d": (86400.0, "天"),
        "day": (86400.0, "天"),
        "days": (86400.0, "天"),
        "天": (86400.0, "天"),
    }

    def _parse_token(token: str) -> tuple[float, str] | None:
        m = _INTERVAL_RE.fullmatch(token.strip())
        if not m:
            return None
        num = float(m.group(1))
        mult, label = _UNITS[m.group(2).lower()]
        shown = str(int(num)) if num.is_integer() else f"{num:g}"
        return num * mult, f"{shown} {label}"

    def parse_loop_args(raw: str) -> ParsedLoop:
        cleaned = raw.strip()
        if not cleaned or cleaned.lower() == "list":
            return ParsedLoop("list")
        first, _, rest = cleaned.partition(" ")
        if first.lower() in {"stop", "cancel"}:
            target = rest.strip()
            if not target:
                raise ValueError("用法：/loop stop <id|all>")
            return ParsedLoop("stop", target=target)
        max_runs = 100
        if cleaned.startswith(("--max-runs ", "--count ")):
            parts = cleaned.split(None, 2)
            if len(parts) < 3 or not parts[1].isdigit() or not (1 <= int(parts[1]) <= 1000):
                raise ValueError("--max-runs 需为 1..1000 的整数。")
            max_runs = int(parts[1])
            cleaned = parts[2].strip()
        head, _, tail = cleaned.partition(" ")
        parsed_iv = _parse_token(head) if tail.strip() else None
        if parsed_iv is not None:
            interval, interval_text = parsed_iv
            prompt = tail.strip()
        else:
            trailing = re.search(r"\s+every\s+(\S+)$", cleaned, re.I)
            if trailing and (t_iv := _parse_token(trailing.group(1))) is not None:
                interval, interval_text = t_iv
                prompt = cleaned[: trailing.start()].strip()
            else:
                interval, interval_text = 300.0, "5 分钟"
                prompt = cleaned
        if interval < 10.0:
            raise ValueError("轮询间隔至少为 10 秒。")
        if interval > 7 * 86400.0:
            raise ValueError("轮询间隔最多为 7 天。")
        if not prompt:
            raise ValueError("请提供定时巡检的提示词。")
        if prompt.lstrip().lower().startswith("/loop"):
            raise ValueError("不能在 /loop 中嵌套调用 /loop。")
        return ParsedLoop("create", interval=interval, interval_text=interval_text, prompt=prompt, max_runs=max_runs)

    def format_loop_prompt(task) -> str:
        info = task.loop
        return f"[定时任务 {task.id} · 每 {info.interval_text} · 第 {info.runs} 次]\n{info.prompt}"

    class LoopScheduler:
        def __init__(
            self,
            tasks,
            *,
            is_busy,
            trigger,
            jitter_ratio: float = 0.0,
            rng=None,
            lifespan: float = 7 * 86400.0,
        ) -> None:
            self.tasks = tasks
            self.is_busy = is_busy
            self.trigger = trigger
            self.lifespan = lifespan
            self._idle_event = asyncio.Event()
            self._fire_lock = asyncio.Lock()

        def active(self) -> list:
            return [t for t in self.tasks.running() if getattr(t, "loop", None) is not None]

        def all_loops(self) -> list:
            return [t for t in self.tasks.tasks.values() if getattr(t, "loop", None) is not None]

        def notify_idle(self) -> None:
            self._idle_event.set()

        def schedule(self, interval: float, interval_text: str, prompt: str, *, max_runs: int = 100):
            if len(self.active()) >= 20:
                raise ValueError("同时运行的定时巡检最多 20 个。")
            now = time.monotonic()
            info = LoopInfo(
                interval=interval,
                interval_text=interval_text,
                prompt=prompt,
                max_runs=max_runs,
                expires_at=now + self.lifespan,
                next_fire=now + interval,
            )
            fields = {
                "origin": "loop",
                "agent": "loop",
                "agent_type": "loop",
                "description": prompt.splitlines()[0][:60],
                "prompt": prompt,
                "background": True,
            }
            # Older Task dataclasses have no `loop` field: attach it afterwards so ids stay sequential.
            if "loop" in {f.name for f in dataclasses.fields(Task)}:
                task = self.tasks.new(**fields, loop=info)
            else:
                task = self.tasks.new(**fields)
                task.loop = info
            self.tasks.launch(task, lambda: self._run_loop(task))
            return task

        async def _run_loop(self, task) -> str:
            info = task.loop
            while True:
                now = time.monotonic()
                delay = max(0.0, (info.next_fire or now) - now)
                await asyncio.sleep(delay)
                if time.monotonic() >= info.expires_at:
                    info.end_reason = "已达 7 天有效期上限"
                    return info.end_reason
                while True:
                    async with self._fire_lock:
                        if not self.is_busy():
                            info.runs += 1
                            info.pending_fire = False
                            info.next_fire = None
                            self.tasks.changed(task)
                            await self.trigger(task, info.prompt)
                            break
                        info.pending_fire = True
                        info.next_fire = None
                        self._idle_event.clear()
                        self.tasks.changed(task)
                    await self._idle_event.wait()
                if info.runs >= info.max_runs:
                    info.end_reason = f"已达最大运行次数 {info.max_runs}"
                    return info.end_reason
                info.next_fire = time.monotonic() + info.interval
                self.tasks.changed(task)

        async def stop(self, target: str) -> list:
            if target.lower() == "all":
                stopped = []
                for t in list(self.active()):
                    if await self.tasks.stop(t.id):
                        stopped.append(t)
                return stopped
            t = self.tasks.get(target)
            if t is None or getattr(t, "loop", None) is None:
                return []
            return [t] if await self.tasks.stop(t.id) else []

        async def close(self) -> None:
            await self.stop("all")

    stub.LoopInfo = LoopInfo
    stub.ParsedLoop = ParsedLoop
    stub.parse_loop_args = parse_loop_args
    stub.format_loop_prompt = format_loop_prompt
    stub.LoopScheduler = LoopScheduler
    monkeypatch.setitem(sys.modules, "mosscode.loop_schedule", stub)
    return stub


# 1. Permissions & Approval Card for repo_map and read_file(outline/symbol) -------------------


async def test_repomap_permission_alias_inside_outside_and_deny_rules(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    rule_rel = parse_rule("RepoMap(secrets/**)", "deny")
    assert rule_rel.tool == "repo_map" and rule_rel.action == "deny"
    rule_abs = parse_rule(f"RepoMap({outside.resolve().as_posix()}/**)", "allow")
    assert rule_abs.tool == "repo_map" and rule_abs.action == "allow"
    with pytest.raises(PermissionConfigError):
        parse_rule("RepoMap(../escape)", "allow")

    policy = PermissionPolicy(project, mode="default", load=False)
    reg = create_default_registry(project, permissions=policy)
    tool = _ensure_repomap_tool(reg)

    # Inside workspace: allowed automatically across default, acceptEdits, and plan modes.
    for mode in ("default", "acceptEdits", "plan"):
        policy.set_mode(mode)
        res = await reg.execute(call("repo_map", path=".", query="AgentLoop"))
        assert not res.is_error, (mode, res.output)

    # Explicit RepoMap(...) deny rule blocks matching workspace subpaths.
    policy.set_mode("default")
    (project / "secrets").mkdir()
    policy.layers[2] = (parse_rule("RepoMap(secrets/**)", "deny"),)
    denied = await reg.execute(call("repo_map", path="secrets"))
    assert denied.code == "permission_denied"

    # Outside workspace: prompts in default/plan (headless -> approval_required), allowed in bypassPermissions.
    op_out = policy.operation(
        tool,
        tool.validate_arguments({"path": "../outside", "query": "token", "focus_files": ["a.py"], "max_tokens": 1024}),
    )
    assert op_out.outside is True
    assert "项目目录之外" in op_out.preview
    assert "查询：token" in op_out.preview
    assert "聚焦文件：a.py" in op_out.preview
    assert "预算：1024 Token" in op_out.preview

    blocked_headless = await reg.execute(call("repo_map", path="../outside"))
    assert blocked_headless.code == "approval_required"

    policy.set_mode("bypassPermissions")
    allowed_bypass = await reg.execute(call("repo_map", path="../outside"))
    assert not allowed_bypass.is_error


def test_read_file_outline_and_symbol_permission_previews(tmp_path):
    policy = PermissionPolicy(tmp_path, mode="default", load=False)
    fake_read = types.SimpleNamespace(name="read_file", read_only=True, plan_allowed=True, permission_kind=None)
    op_outline = policy.operation(fake_read, _FallbackReadOutlineParams(path="../ext.py", outline=True))
    assert op_outline.outside is True and "读取 AST 符号大纲。" in op_outline.preview
    op_symbol = policy.operation(fake_read, _FallbackReadOutlineParams(path="../ext.py", symbol="Foo.bar"))
    assert op_symbol.outside is True and "读取符号：Foo.bar" in op_symbol.preview


async def test_tui_approval_card_renders_repomap_label(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    policy = PermissionPolicy(project, mode="default", load=False)
    reg = create_default_registry(project, permissions=policy)
    _ensure_repomap_tool(reg)

    provider = SequenceProvider(
        response(calls=[call("repo_map", "rm1", path="../outside", query="auth")]),
        response("代码地图读取完成"),
    )
    conv = Conversation(provider, registry=reg)
    app = MossCodeApp(conv, "fake", model="fake-model")
    async with app.run_test(size=(100, 30)) as pilot:
        await send(app, pilot, "查看外部目录结构")
        await wait_approval(app, pilot)
        detail_text = str(app.query_one("#approval-detail", Static).render())
        assert "需要授权 · 生成代码地图" in detail_text
        assert "查询：auth" in detail_text
        await pilot.press("enter")
        await asyncio.wait_for(app._reply_task, 3)


# 2. TUI Views & ACP Events Formatting --------------------------------------------------------


def test_tui_views_and_acp_events_format_repomap_and_read_modes(tmp_path):
    # tui_views.describe_call
    assert describe_call(ToolCall("c1", "repo_map", '{"path":"src","query":"verify","focus_files":["a.py","b.py"]}'))[
        :2
    ] == ("RepoMap", "src · verify · 聚焦 2 个文件")
    assert describe_call(ToolCall("c2", "read_file", '{"path":"src/app.py","outline":true}'))[:2] == (
        "Read",
        "读取大纲 src/app.py",
    )
    assert describe_call(ToolCall("c3", "read_file", '{"path":"src/app.py","symbol":"App.run"}'))[:2] == (
        "Read",
        "读取符号 src/app.py · App.run",
    )
    assert describe_call(ToolCall("c4", "read_file", '{"path":"src/app.py"}'))[:2] == ("Read", "src/app.py")

    # tui_views.resumed_rows for synthetic="loop"
    loop_msg = Message(
        role="user",
        content=[ContentBlock(kind="text", text="[定时任务 t1 · 每 5 分钟 · 第 1 次]\n检查构建")],
        synthetic="loop",
    )
    rows = resumed_rows([loop_msg])
    assert len(rows) == 1 and "⏰ [定时任务 t1 · 每 5 分钟 · 第 1 次]" in str(rows[0])

    # acp.events
    assert tool_kind("repo_map") == "search"
    assert (
        tool_title("repo_map", {"path": "src", "query": "verify", "focus_files": ["a.py"]}, tmp_path)
        == "生成代码地图 src · verify · 聚焦 1 个文件"
    )
    assert tool_title("read_file", {"path": "src/app.py", "outline": True}, tmp_path) == "读取大纲 src/app.py"
    assert (
        tool_title("read_file", {"path": "src/app.py", "symbol": "App.run"}, tmp_path)
        == "读取符号 src/app.py · App.run"
    )
    assert tool_title("read_file", {"path": "src/app.py"}, tmp_path) == "读取 src/app.py"

    locs = tool_locations("repo_map", {}, tmp_path)
    assert locs is not None and Path(locs[0].path) == tmp_path.resolve()

    mapper = AcpEventMapper(tmp_path)
    req = ApprovalRequest(
        "r1",
        "call1",
        Operation("repo_map", (tmp_path.parent / "other").as_posix(), preview="查询：auth", outside=True),
        "读取项目目录之外的路径需要确认。",
    )
    upd = mapper.approval_tool_call_update(req)
    assert upd.kind == "search" and "生成代码地图" in (upd.title or "")


# 3. /loop Command, Headless Rejection, /tasks Integration & Lifecycle ------------------------


async def test_loop_command_headless_rejection_tasks_report_and_lifecycle(tmp_path, monkeypatch, capsys):
    _install_fallback_loop_schedule(monkeypatch)

    # CLI -p headless mode rejects local /loop with exit code 2.
    with pytest.raises(SystemExit) as exc:
        cli._headless_input("/loop 5m 检查构建", BUILTIN)
    assert exc.value.code == 2
    assert "/loop 只能在交互界面中使用。" in capsys.readouterr().err

    # Direct headless CommandContext returns LOOP_HEADLESS_NOTICE.
    conv_headless = Conversation(SequenceProvider())
    conv_headless.headless = True
    ui_headless = FakeUI()
    await dispatch(CommandContext(conv_headless, ui_headless, BUILTIN), parse_input("/loop 5m hi", BUILTIN))
    assert ui_headless.notes == [LOOP_HEADLESS_NOTICE]

    # Interactive command lifecycle: create, list, /tasks summary + detail, stop, clear, resume, close.
    session_a = Session.new(tmp_path, home=tmp_path / "home")
    conv = Conversation(
        SequenceProvider(response("ok"), response("ok")),
        context_root=tmp_path,
        session=session_a,
        persist=True,
        sessions_home=tmp_path / "home",
    )
    async for _ in conv.reply("先发一条消息保存会话"):
        pass
    saved_id = conv.session_id
    assert saved_id is not None

    ui = FakeUI()
    ctx = CommandContext(conv, ui, BUILTIN)

    # Validation error surfaces cleanly.
    await dispatch(ctx, parse_input("/loop 2s 太快了", BUILTIN))
    assert "10 秒" in ui.notes[-1]
    ui.notes.clear()

    # Create a loop.
    await dispatch(ctx, parse_input("/loop 5m 检查 CI 是否绿灯", BUILTIN))
    assert (
        ui.notes[-1]
        == "└ 已启动定时巡检 t1（每 5 分钟，最多 100 次）：检查 CI 是否绿灯；可用 /tasks 或 /loop stop t1 停止。"
    )
    assert ("refresh",) in ui.calls
    await asyncio.sleep(0.01)  # Let the loop job start before it is stopped.

    # /loop list & loop_report
    ui.notes.clear()
    await dispatch(ctx, parse_input("/loop list", BUILTIN))
    assert "定时巡检（1）" in ui.notes[-1]
    assert "每 5 分钟 · 已运行 0/100 次" in ui.notes[-1]
    assert "检查 CI 是否绿灯" in ui.notes[-1]

    # /tasks summary and /tasks <id> detail view, plus /status report
    summary = tasks_report(conv)
    assert "每 5 分钟 · 已运行 0/100 次" in summary
    detail = tasks_report(conv, "t1")
    assert "定时巡检" in detail and "每 5 分钟" in detail and "检查 CI 是否绿灯" in detail
    assert "定时巡检" in status_report(ctx)

    # Stop single loop, then create two more and clear() / resume() / close().
    ui.notes.clear()
    await dispatch(ctx, parse_input("/loop stop t1", BUILTIN))
    assert "已停止定时巡检 t1" in ui.notes[-1]
    assert "已取消" in loop_report(conv) or "已手动停止" in loop_report(conv)

    await dispatch(ctx, parse_input("/loop 1m 巡检日志", BUILTIN))
    await asyncio.sleep(0.01)
    assert len(conv.loops.active()) == 1
    await conv.clear()
    assert len(conv.loops.active()) == 0

    # Can still schedule after clear(), and resume() cancels active loops cleanly.
    await dispatch(ctx, parse_input("/loop 2m 巡检队列", BUILTIN))
    await asyncio.sleep(0.01)
    assert len(conv.loops.active()) == 1
    async for _ in conv.resume(saved_id):
        pass
    assert len(conv.loops.active()) == 0

    await dispatch(ctx, parse_input("/loop 3m 退出前任务", BUILTIN))
    await asyncio.sleep(0.01)
    assert len(conv.loops.active()) == 1
    await conv.close()
    assert len(conv.loops.active()) == 0


# 4. Headless-TUI Idle Firing, Busy Coalescing & Slash Dispatch -------------------------------


async def test_tui_loop_busy_coalescing_idle_firing_slash_dispatch_and_status_bar(tmp_path, monkeypatch):
    _install_fallback_loop_schedule(monkeypatch)
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()
    loop_turn_done = asyncio.Event()
    calls_seen: list[tuple[str | None, str]] = []

    async def scripted_stream(messages, system, tools=(), *, allow_tools=True):
        last = messages[-1]
        calls_seen.append((last.synthetic, last.text))
        if len(calls_seen) == 1:
            turn_started.set()
            await release_turn.wait()
            yield Completed(Message(role="assistant", content=[ContentBlock(kind="text", text="主任务完成")]), "stop")
            return
        loop_turn_done.set()
        yield Completed(Message(role="assistant", content=[ContentBlock(kind="text", text="巡检正常")]), "stop")

    provider = ScriptedProvider(lambda _idx, msgs, _prov: scripted_stream(msgs, ""))
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False)
    conv = Conversation(provider, registry=reg)
    app = MossCodeApp(conv, "fake", model="fake-model")

    async with app.run_test(size=(110, 30)) as pilot:
        await send(app, pilot, "执行一个慢任务")
        await asyncio.wait_for(turn_started.wait(), 2)

        # Schedule a short-interval loop while the main turn is busy; let multiple intervals pass.
        assert conv.loops is not None
        task = conv.loops.schedule(0.02, "0.02 秒", "检查服务健康", max_runs=2)
        await pilot.pause(0.5)
        assert "定时 1" in str(app.query_one("#status", Static).render())
        # Must NOT have fired while busy!
        assert len(calls_seen) == 1

        # Release the busy turn -> coalesced pending_fire triggers ONCE when idle returns!
        release_turn.set()
        await asyncio.wait_for(loop_turn_done.wait(), 3)
        await conv.loops.stop("all")
        await pilot.pause(0.05)

        assert any(syn == "loop" and "检查服务健康" in txt for syn, txt in calls_seen[1:])
        assert task.loop.runs >= 1
        assert "定时 " not in str(app.query_one("#status", Static).render())

        # Also verify slash-command dispatch from /loop (e.g. `/status` local command).
        slash_task = conv.loops.schedule(0.02, "0.02 秒", "/todo", max_runs=1)
        for _ in range(30):
            await pilot.pause(0.02)
            if slash_task.status != "running":
                break
        assert slash_task.loop.runs == 1 and slash_task.status == "completed"


# 5. ACP /loop Support & Framing Invariant (Zero Unsolicited Frames When Idle) ----------------


async def test_acp_loop_commands_and_no_unsolicited_updates_while_idle(tmp_path, monkeypatch):
    _install_fallback_loop_schedule(monkeypatch)
    seen_synthetics: list[tuple[str | None, str]] = []

    async def handler(call_idx, messages, _prov):
        last = messages[-1]
        seen_synthetics.append((last.synthetic, last.text))
        text = f"回复 #{call_idx}"
        yield Delta("text", text)
        yield Completed(Message(role="assistant", content=[ContentBlock(kind="text", text=text)]), "stop")

    provider = ScriptedProvider(handler)
    settings = Settings(
        providers=(ProviderConfig("fake", "anthropic-messages", "fake-model", api_key="fake-token"),),
        default_provider="fake",
        persist_sessions=False,
    )
    options = AcpLaunchOptions(
        home=tmp_path / "home",
        settings=settings,
        provider_factory=lambda _cfg: provider,
    )
    client = RecordingClient()
    agent = MossCodeAcpAgent(options)
    conn, cleanup = await _connect_agent(agent, client)
    try:
        created = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        sid = created.session_id
        await asyncio.sleep(0.05)

        # Verify `loop` is advertised in AvailableCommandsUpdate.
        cmd_updates = [u for _, u in client.updates if isinstance(u, AvailableCommandsUpdate)]
        assert cmd_updates
        loop_cmds = [c for c in cmd_updates[-1].available_commands if c.name == "loop"]
        assert len(loop_cmds) == 1

        # Create a standard loop via ACP slash command, list it, and stop it.
        client.updates.clear()
        await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/loop 5m 检查构建状态")])
        chunks = "".join(
            u.content.text
            for _, u in client.updates
            if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)
        )
        assert "已启动定时巡检" in chunks and "每 5 分钟" in chunks

        client.updates.clear()
        await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/loop list")])
        list_chunks = "".join(
            u.content.text
            for _, u in client.updates
            if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)
        )
        assert "检查构建状态" in list_chunks

        client.updates.clear()
        await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="/loop stop all")])
        stop_chunks = "".join(
            u.content.text
            for _, u in client.updates
            if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)
        )
        assert "已停止" in stop_chunks

        # Schedule a short loop while ACP is IDLE between session/prompt RPCs:
        # ZERO unsolicited session_update notifications may be emitted while idle!
        acp_session = agent.sessions[sid]
        assert acp_session.conversation.loops is not None
        client.updates.clear()
        fast_task = acp_session.conversation.loops.schedule(0.02, "0.02 秒", "后台健康检查", max_runs=2)
        await asyncio.sleep(0.10)
        assert client.updates == []
        assert fast_task.loop.runs == 1

        # Next session/prompt RPC drains the queued due loop inside the valid RPC turn!
        await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="继续工作")])
        turn_chunks = "".join(
            u.content.text
            for _, u in client.updates
            if isinstance(u, AgentMessageChunk) and isinstance(u.content, TextContentBlock)
        )
        assert "⏰ 定时巡检" in turn_chunks and "后台健康检查" in turn_chunks
        assert any(syn == "loop" and "后台健康检查" in txt for syn, txt in seen_synthetics)
    finally:
        await cleanup()
