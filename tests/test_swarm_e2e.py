"""End-to-end integration tests for L1 Sub-Agent Approval Bubbling & L2 Single-Truth Blackboard Swarm."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from acp.schema import AllowedOutcome, DeniedOutcome, TextContentBlock

from mosscode import coordinator
from mosscode.acp.app import MossCodeAcpAgent
from mosscode.acp.events import tool_kind, tool_title
from mosscode.acp.session import AcpLaunchOptions
from mosscode.agents import AgentCatalog
from mosscode.agents.runtime import GLOBAL_BAN
from mosscode.agents.runtime import describe_call as subagent_describe_call
from mosscode.config import ProviderConfig, Settings, render_config
from mosscode.messages import NOTIFICATION, REMINDER, ToolCall
from mosscode.runtime_factory import build_conversation
from mosscode.skills import SkillCatalog
from mosscode.teams.member import TeamAccess
from mosscode.teams.watchdog import MAX_PEER_HOPS
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp, PromptArea
from mosscode.tui_views import describe_call as tui_describe_call
from test_acp import RecordingClient, _connect_agent
from test_agent import call, response
from test_subagents import ScriptProvider, settle
from test_team_backends import Terminal, pane_conversation
from test_teams import Script, idle, tool_call, turns, until
from test_worktree import git, repo


def _provider_Cfg() -> ProviderConfig:
    return ProviderConfig(
        name="fake",
        protocol="anthropic-messages",
        model="fake-model",
        api_key="sk-test-1234567890",
    )


def _write_acp_config(home: Path) -> None:
    cfg_dir = home / ".mosscode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
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


def test_cross_module_seams_describe_call_acp_titles_and_factory_max_members(tmp_path):
    assert {"post_board", "read_board"} <= GLOBAL_BAN

    post_call = ToolCall("c1", "post_board", json.dumps({"body": "@verifier 请审查 #2\n详细内容", "summary": ""}))
    assert tui_describe_call(post_call)[:2] == ("PostBoard", "@verifier 请审查 #2")
    assert subagent_describe_call(post_call) == "post_board @verifier 请审查 #2 详细内容"

    post_summary = ToolCall("c2", "post_board", json.dumps({"body": "正文", "summary": "合流完成"}))
    assert tui_describe_call(post_summary)[:2] == ("PostBoard", "合流完成")
    assert subagent_describe_call(post_summary) == "post_board 合流完成"

    read_seqs = ToolCall("c3", "read_board", json.dumps({"seqs": [1, 3]}))
    assert tui_describe_call(read_seqs)[:2] == ("ReadBoard", "#1 #3")
    assert subagent_describe_call(read_seqs) == "read_board #1 #3"

    read_since = ToolCall("c4", "read_board", json.dumps({"since": 4}))
    assert tui_describe_call(read_since)[:2] == ("ReadBoard", "since #4")
    assert subagent_describe_call(read_since) == "read_board since #4"

    read_recent = ToolCall("c5", "read_board", json.dumps({"limit": 10}))
    assert tui_describe_call(read_recent)[:2] == ("ReadBoard", "最近 10 条")

    assert tool_kind("post_board") == "other"
    assert tool_kind("read_board") == "read"
    assert tool_title("post_board", {"summary": "请求复审"}, tmp_path) == "发布黑板 请求复审"
    assert tool_title("read_board", {"seqs": [1, 2]}, tmp_path) == "读取黑板 #1 #2"

    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = _provider_Cfg()
    settings = Settings(providers=(cfg,), default_provider="fake")
    assert settings.team_max_members == 6

    conv = build_conversation(
        settings,
        cfg,
        root=project,
        registry=create_default_registry(project, load_permissions=False),
        skills=SkillCatalog(),
        agents=AgentCatalog.load(project, home=home),
        home=home,
        teams=True,
        provider=ScriptProvider(lambda *_a: response("ok")),
    )
    assert conv.teams is not None and conv.teams.max_members == 6


async def test_swarm_command_in_headless_tui_runs_four_member_worktree_pipeline_merge_and_done(tmp_path):
    project = repo(tmp_path)
    git(project, "config", "user.name", "t")
    git(project, "config", "user.email", "t@example.com")
    git(project, "config", "commit.gpgsign", "false")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg = _provider_Cfg()
    settings = Settings(
        providers=(cfg,),
        default_provider="fake",
        persist_sessions=False,
        teammate_mode="in-process",
    )

    lead_turns: list[str] = []
    verifier_seen_tools: list[set[str]] = []

    def lead_script(messages, tools):
        offered = {t.name for t in tools}
        assert not ({"write_file", "edit_file"} & offered)
        assert {"team_create", "team_spawn", "team_merge", "post_board", "read_board"} <= offered

        last = messages[-1]
        if last.role == "user":
            lead_turns.append(last.text)
            if len(lead_turns) == 1:
                return response(calls=[tool_call("team_create", "tc1", name="upgrade", description="双模块升级")])
            assert "<team-update>" in last.text and "对抗审查通过" in last.text
            return response(calls=[tool_call("team_merge", "tm1", member="integrator")])

        ids = {r.call_id: r for r in last.tool_results}
        if "tc1" in ids:
            return response(
                calls=[
                    tool_call("team_task_create", "tk1", subject="实现 mod_a.py (A=20)"),
                    tool_call("team_task_create", "tk2", subject="实现 mod_b.py (B=22)"),
                    tool_call("team_task_create", "tk3", subject="合流并验收 A+B=42", blocked_by=["1", "2"]),
                ]
            )
        if "tk1" in ids:
            return response(
                calls=[
                    tool_call(
                        "team_spawn",
                        "sp_int",
                        name="integrator",
                        role="integrator",
                        prompt="等待 author-a 与 author-b 在黑板交接后合流它们的工作树分支，送 @verifier 审查（目标 A+B=42）。",
                    ),
                    tool_call(
                        "team_spawn",
                        "sp_ver",
                        name="verifier",
                        role="verifier",
                        prompt="只读审查 integrator 分支，核对 mod_a.py 与 mod_b.py 中 A+B 是否等于 42，通过后点名 @lead。",
                    ),
                    tool_call(
                        "team_spawn",
                        "sp_a",
                        name="author-a",
                        role="worker",
                        prompt="在工作树新建 mod_a.py（A = 20）并 git commit，完成后更新任务 1 并点名 @integrator。",
                    ),
                    tool_call(
                        "team_spawn",
                        "sp_b",
                        name="author-b",
                        role="worker",
                        prompt="在工作树新建 mod_b.py（B = 22）并 git commit，完成后更新任务 2 并点名 @integrator。",
                    ),
                ]
            )
        if "sp_a" in ids:
            return response("已建队并派发 4 名成员，等待 @lead 验收通知。")
        if "tm1" in ids:
            assert not ids["tm1"].is_error, ids["tm1"].output
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "pb_done",
                        body="[done] 已合入 integrator 分支，A+B=42 验收通过",
                        summary="Swarm 验收合流完成",
                    )
                ]
            )
        return response("全部合并并收尾完成。")

    def author_a(messages, tools):
        last = messages[-1]
        if last.role == "user":
            return response(calls=[call("write_file", "wa1", path="mod_a.py", content="A = 20\n")])
        cid = last.tool_results[0].call_id
        if cid == "wa1":
            return response(
                calls=[
                    call(
                        "execute_command",
                        "ca1",
                        command='git add mod_a.py && git commit -m "feat: add mod_a"',
                    )
                ]
            )
        if cid == "ca1":
            return response(
                calls=[tool_call("team_task_update", "ta1", id="1", owner="author-a", status="in_progress")]
            )
        if cid == "ta1":
            return response(calls=[tool_call("team_task_update", "ta2", id="1", status="completed")])
        if cid == "ta2":
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "pa1",
                        body="@integrator mod_a.py 已提交到分支 mosscode/team-upgrade+author-a（对应 #3），请忽略 `@lead`",
                        summary="author-a 提交完成",
                    )
                ]
            )
        return response("author-a 完成")

    def author_b(messages, tools):
        last = messages[-1]
        if last.role == "user":
            # Deliberately write B = 21 first so verifier catches A + B = 41 != 42 and sends back for rework!
            return response(calls=[call("write_file", "wb1", path="mod_b.py", content="B = 21\n")])
        cid = last.tool_results[0].call_id
        if cid == "wb1":
            return response(
                calls=[
                    call(
                        "execute_command",
                        "cb1",
                        command='git add mod_b.py && git commit -m "feat: add mod_b"',
                    )
                ]
            )
        if cid == "cb1":
            return response(
                calls=[tool_call("team_task_update", "tb1", id="2", owner="author-b", status="in_progress")]
            )
        if cid == "tb1":
            return response(calls=[tool_call("team_task_update", "tb2", id="2", status="completed")])
        if cid == "tb2":
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "pb1",
                        body="@integrator mod_b.py 已提交到分支 mosscode/team-upgrade+author-b（对应 #4）",
                        summary="author-b 提交完成",
                    )
                ]
            )
        return response("author-b 完成")

    def integrator(messages, tools):
        last = messages[-1]
        if turns(messages) == 1:
            return response("等待 author-a 与 author-b 全部交接")

        latest_user = next(m.text for m in reversed(messages) if m.role == "user")
        # Rework turn triggered by @verifier:
        if "审查未通过" in latest_user:
            if last.role == "user":
                return response(calls=[call("read_file", "ri_b", path="mod_b.py")])
            cid = last.tool_results[0].call_id
            if cid == "ri_b":
                return response(
                    calls=[call("edit_file", "ei_b", path="mod_b.py", old_text="B = 21", new_text="B = 22")]
                )
            if cid == "ei_b":
                return response(
                    calls=[
                        call(
                            "execute_command",
                            "ci_fix",
                            command='git add mod_b.py && git commit -m "fix: update B to 22"',
                        )
                    ]
                )
            if cid == "ci_fix":
                return response(calls=[tool_call("team_task_update", "ti_done", id="3", status="completed")])
            if cid == "ti_done":
                return response(
                    calls=[
                        tool_call(
                            "post_board",
                            "pi_recheck",
                            body="@verifier 已在 integrator 分支修复 mod_b.py 为 B = 22，请复审",
                            summary="已修复 B=22 请求复审",
                        )
                    ]
                )
            return response("复审交接完成")

        # Handoff turn(s) from authors: check board to see if both author-a and author-b have posted.
        if last.role == "user":
            return response(calls=[tool_call("read_board", "rb_all", since=0, limit=50)])
        cid = last.tool_results[0].call_id
        if cid == "rb_all":
            board_out = last.tool_results[0].output
            if "author-a 提交完成" not in board_out or "author-b 提交完成" not in board_out:
                return response("仍有一名作者未完成，继续等待")
            return response(
                calls=[
                    call(
                        "execute_command",
                        "ci_merge",
                        command=(
                            "git merge --no-edit mosscode/team-upgrade+author-a "
                            "&& git merge --no-edit mosscode/team-upgrade+author-b"
                        ),
                    )
                ]
            )
        if cid == "ci_merge":
            assert not last.tool_results[0].is_error, last.tool_results[0].output
            return response(
                calls=[tool_call("team_task_update", "ti_claim", id="3", owner="integrator", status="in_progress")]
            )
        if cid == "ti_claim":
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "pi_verify",
                        body="@verifier 已合流 author-a 与 author-b 到 mosscode/team-upgrade+integrator，请审查 #1",
                        summary="合流完成请求初审",
                    )
                ]
            )
        return response("已送初审")

    def verifier(messages, tools):
        verifier_seen_tools.append({t.name for t in tools})
        last = messages[-1]
        if turns(messages) == 1:
            return response("等待 integrator 送审")
        if last.role == "user":
            return response(
                calls=[
                    tool_call("read_board", "rv_brief", seqs=[1]),
                    call(
                        "execute_command",
                        "cv_show",
                        command=(
                            "git show mosscode/team-upgrade+integrator:mod_a.py "
                            "&& git show mosscode/team-upgrade+integrator:mod_b.py"
                        ),
                    ),
                ]
            )
        results = {r.call_id: r for r in last.tool_results}
        if "cv_show" in results:
            shown = results["cv_show"].output
            if "B = 21" in shown:
                return response(
                    calls=[
                        tool_call(
                            "post_board",
                            "pv_reject",
                            body="@integrator 审查未通过：mod_b.py 当前为 B = 21（A+B=41 != 42，见 #1），请修复",
                            summary="打回返工 B=21",
                        )
                    ]
                )
            assert "A = 20" in shown and "B = 22" in shown
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "pv_pass",
                        body="@lead 对抗审查通过：mosscode/team-upgrade+integrator 中 A=20、B=22（A+B=42），可合并",
                        summary="对抗审查通过",
                    )
                ]
            )
        return response("审查完毕")

    script = Script(
        lead=lead_script,
        **{
            "author-a": author_a,
            "author-b": author_b,
            "integrator": integrator,
            "verifier": verifier,
        },
    )
    provider = ScriptProvider(script)
    registry = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    conv = build_conversation(
        settings,
        cfg,
        root=project,
        registry=registry,
        skills=SkillCatalog(),
        agents=AgentCatalog.load(project, home=home),
        home=home,
        teams=True,
        provider=provider,
    )
    conv.teams.poll = 0.03
    conv.teams.stop_wait = 1.0

    baseline_tasks = set(asyncio.all_tasks())
    app = MossCodeApp(conv, "fake", settings=settings, model="fake-model")
    try:
        async with app.run_test(size=(150, 36)) as pilot:
            await pilot.pause()
            assert conv.coordinator_mode is False

            prompt_area = app.query_one("#prompt", PromptArea)
            prompt_area.value = "/swarm 并行实现 mod_a.py 与 mod_b.py 并合流验收"
            await pilot.pause()
            if app._hint_choices:
                await pilot.press("escape")
            await pilot.press("enter")

            # Wait until the entire peer pipeline finishes, wakes Lead on Turn 2, and team_merge opens #approval-panel!
            await until(
                lambda: app._approval_request is not None and app._approval_request.operation.tool == "team_merge",
                timeout=45.0,
            )
            await pilot.pause()
            assert conv.coordinator_mode is True
            assert coordinator.BADGE in str(app.query_one("#status").render())
            # Lead ran Turn 1 (spawn) and Turn 2 (<team-update> -> team_merge): ZERO intermediate Lead turns!
            assert len(lead_turns) == 2

            # Approve the team_merge card in the TUI.
            await pilot.press("enter")
            await until(
                lambda: (
                    conv.teams.current is not None and conv.teams.current.load().done_seq > 0 and not app._generating
                ),
                timeout=10.0,
            )
            await pilot.pause()

            assert len(lead_turns) == 2
            assert (project / "mod_a.py").read_text(encoding="utf-8") == "A = 20\n"
            assert (project / "mod_b.py").read_text(encoding="utf-8") == "B = 22\n"

            assert verifier_seen_tools
            for offered in verifier_seen_tools:
                assert not ({"write_file", "edit_file"} & offered)

            store = conv.teams.current
            assert store is not None
            posts, head_seq = store.blackboard.read_since(0, limit=50)
            assert head_seq >= 8
            assert posts[-1].kind == "done" and "[done]" in posts[-1].body
            for member_name in ("author-a", "author-b", "integrator", "verifier"):
                assert store.mailbox.peek(member_name) == []
    finally:
        await conv.close()

    await asyncio.sleep(0.05)
    leaked = [t for t in asyncio.all_tasks() - baseline_tasks if not t.done()]
    assert leaked == []


async def test_agent_fork_approval_bubbling_and_task_stop_withdrawal_in_tui_and_acp(tmp_path):
    # --- Part A: Headless TUI in default mode (Fork edit approval + task_stop orphan withdrawal) ---
    tui_root = tmp_path / "tui_ws"
    project = tui_root / "project"
    project.mkdir(parents=True)
    home = tui_root / "home"
    home.mkdir(parents=True)
    target_file = project / "fork_edit.txt"
    target_file.write_text("old-line\n", encoding="utf-8")
    orphan_file = project / "orphan.txt"
    orphan_file.write_text("untouched\n", encoding="utf-8")

    def tui_script(messages, system, tools):
        messages = [m for m in messages if m.synthetic != REMINDER]
        fork_msg = next((m.text for m in messages if m.role == "user" and "<fork-directive>" in m.text), "")
        if fork_msg:
            last = messages[-1]
            if "编辑 fork_edit.txt" in fork_msg:
                if last.role == "user":
                    return response(
                        calls=[
                            call(
                                "edit_file",
                                "fe_ok",
                                path="fork_edit.txt",
                                old_text="old-line",
                                new_text="new-line",
                            )
                        ]
                    )
                return response("范围：已完成 fork_edit.txt 修改")
            if last.role == "user":
                return response(
                    calls=[
                        call(
                            "edit_file",
                            "fe_stop",
                            path="orphan.txt",
                            old_text="untouched",
                            new_text="modified",
                        )
                    ]
                )
            return response("范围：不应到达")

        last = messages[-1]
        if last.role == "user" and last.synthetic == NOTIFICATION:
            return response("已收到 Fork 完成通知")
        if last.role == "user" and "第一个 Fork" in last.text:
            return response(calls=[call("read_file", "r0", path="fork_edit.txt")])
        if last.role == "tool" and last.tool_results[0].call_id == "r0":
            return response(calls=[call("agent", "f_ok", description="编辑文件", prompt="编辑 fork_edit.txt")])
        if last.role == "user" and "第二个 Fork" in last.text:
            return response(calls=[call("read_file", "r1", path="orphan.txt")])
        if last.role == "tool" and last.tool_results[0].call_id == "r1":
            return response(calls=[call("agent", "f_stop", description="待取消编辑", prompt="编辑 orphan.txt")])
        return response("已派发 Fork")

    cfg = _provider_Cfg()
    settings = Settings(
        providers=(cfg,),
        default_provider="fake",
        persist_sessions=False,
        teammate_mode="in-process",
    )
    conv = build_conversation(
        settings,
        cfg,
        root=project,
        registry=create_default_registry(project, permission_mode="default", load_permissions=False),
        skills=SkillCatalog(),
        agents=AgentCatalog.load(project, home=home),
        home=home,
        teams=True,
        provider=ScriptProvider(tui_script),
    )
    app = MossCodeApp(conv, "fake", settings=settings, model="fake-model")
    try:
        async with app.run_test(size=(130, 32)) as pilot:
            await pilot.pause()

            # 1) Fork 1 bubbles edit_file approval into TUI _member_approval -> user presses Enter ("once").
            prompt_area = app.query_one("#prompt", PromptArea)
            prompt_area.value = "启动第一个 Fork"
            await pilot.press("enter")
            await until(
                lambda: app._approval_request is not None and app._approval_request.operation.target == "fork_edit.txt",
                timeout=5.0,
            )
            await pilot.pause()
            assert app.query_one("#approval-panel").display is True
            await pilot.press("enter")

            await until(lambda: target_file.read_text(encoding="utf-8") == "new-line\n", timeout=5.0)
            await settle(conv)
            await until(lambda: not app._generating and not conv.notifications, timeout=5.0)

            # 2) Fork 2 bubbles edit_file approval -> /tasks stop withdraws the unanswered card immediately!
            prompt_area.value = "启动第二个 Fork"
            await pilot.press("enter")
            await until(
                lambda: app._approval_request is not None and app._approval_request.operation.target == "orphan.txt",
                timeout=5.0,
            )
            await until(lambda: not app._generating, timeout=5.0)
            await pilot.pause()
            t2 = next(t for t in conv.tasks.running() if "待取消编辑" in t.label)
            assert app.query_one("#approval-panel").display is True

            stopped_task = await conv.tasks.stop(t2.id)
            assert stopped_task is not None
            await pilot.pause()
            assert app._approval_request is None
            assert app.query_one("#approval-panel").display is False
            assert orphan_file.read_text(encoding="utf-8") == "untouched\n"
    finally:
        await conv.close()

    # --- Part B: ACP session/request_permission for Agent Fork + task_stop withdrawal ---
    acp_home = tmp_path / "acp_home"
    acp_work = tmp_path / "acp_work"
    acp_work.mkdir()
    _write_acp_config(acp_home)
    acp_target = acp_work / "acp_fork.txt"
    acp_target.write_text("before-acp\n", encoding="utf-8")
    acp_orphan = acp_work / "acp_orphan.txt"
    acp_orphan.write_text("keep-me\n", encoding="utf-8")

    orphan_perm_entered = asyncio.Event()
    orphan_perm_release = asyncio.Event()

    async def acp_decider(_sid, tool_call_upd, _options):
        raw = tool_call_upd.raw_input or {}
        target = str(raw.get("target") or raw.get("path") or "")
        if target.endswith("acp_orphan.txt"):
            orphan_perm_entered.set()
            await orphan_perm_release.wait()
            return DeniedOutcome()
        return AllowedOutcome(option_id="once")

    async def acp_script(messages, system, tools):
        filtered = [m for m in messages if m.synthetic != REMINDER]
        fork_msg = next((m.text for m in filtered if m.role == "user" and "<fork-directive>" in m.text), "")
        if fork_msg:
            last = filtered[-1]
            if "编辑 acp_fork.txt" in fork_msg:
                if last.role == "user":
                    return response(
                        calls=[
                            call(
                                "edit_file",
                                "af1",
                                path="acp_fork.txt",
                                old_text="before-acp",
                                new_text="after-acp",
                            )
                        ]
                    )
                return response("范围：ACP Fork 编辑完成")
            if last.role == "user":
                return response(
                    calls=[
                        call(
                            "edit_file",
                            "af2",
                            path="acp_orphan.txt",
                            old_text="keep-me",
                            new_text="changed",
                        )
                    ]
                )
            return response("范围：已结束")

        last = filtered[-1]
        if last.role == "user" and last.synthetic == NOTIFICATION:
            return response("ACP 已处理通知")
        if last.role == "user" and "run-fork-1" in last.text:
            return response(
                calls=[
                    call("read_file", "ar1", path="acp_fork.txt"),
                    call("read_file", "ar2", path="acp_orphan.txt"),
                ]
            )
        if last.role == "tool" and any(r.call_id == "ar1" for r in last.tool_results):
            return response(calls=[call("agent", "ag1", description="ACP改文件", prompt="编辑 acp_fork.txt")])
        if last.role == "user" and "run-fork-2-and-stop" in last.text:
            return response(calls=[call("agent", "ag2", description="ACP待停任务", prompt="编辑 acp_orphan.txt")])
        if last.role == "tool" and any(r.call_id == "ag2" for r in last.tool_results):
            task_id = last.tool_results[0].details["task"]
            await asyncio.wait_for(orphan_perm_entered.wait(), timeout=5.0)
            return response(calls=[call("task_stop", "ts1", task_id=task_id)])
        return response("ACP 主轮完成")

    agent = MossCodeAcpAgent(
        AcpLaunchOptions(
            home=acp_home, permission_mode="default", provider_factory=lambda _c: ScriptProvider(acp_script)
        )
    )
    client = RecordingClient(acp_decider)
    conn, cleanup = await _connect_agent(agent, client)
    try:
        await conn.initialize(protocol_version=1)
        created = await conn.new_session(cwd=str(acp_work), mcp_servers=[])
        sid = created.session_id
        acp_session = agent.sessions[sid]

        res1 = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="run-fork-1")])
        assert res1.stop_reason == "end_turn"
        assert acp_target.read_text(encoding="utf-8") == "after-acp\n"
        assert any(tc.name == "edit_file" for _, tc, _ in client.permission_requests)

        res2 = await conn.prompt(session_id=sid, prompt=[TextContentBlock(text="run-fork-2-and-stop")])
        assert res2.stop_reason == "end_turn"
        assert acp_session._bg_approval_tasks == {}
        assert acp_orphan.read_text(encoding="utf-8") == "keep-me\n"
    finally:
        orphan_perm_release.set()
        await cleanup()


async def test_pane_crash_recovery_stall_watchdog_and_ping_pong_breaker_e2e(tmp_path):
    baseline_tasks = set(asyncio.all_tasks())
    terminal = Terminal(lambda term, store, name: term.start(store, name, status="running"))
    conv = pane_conversation(tmp_path, terminal)
    conv.teams.poll = 0.03
    conv.teams.stall_after = 0.06
    try:
        await conv.teams.create("resilience")
        store = conv.teams.current
        assert store is not None

        # 1) Spawn pane member `pane-worker`, assign task #1 in_progress & task #0 completed, then crash its process!
        await conv.teams.spawn("pane-worker", "general", "执行窗格任务", isolation="none")
        access = conv.teams.access()
        done_task = access.task_create("已完成前置工作", owner="pane-worker")
        live_task = access.task_create("核心编译任务", owner="pane-worker")
        TeamAccess(store, "pane-worker").task_update(done_task.id, status="in_progress")
        TeamAccess(store, "pane-worker").task_update(done_task.id, status="completed")
        TeamAccess(store, "pane-worker").task_update(live_task.id, status="in_progress")

        terminal.processes[0].kill()
        terminal.processes[0].wait(timeout=5)

        await until(lambda: "pane-worker" not in conv.teams.runners, timeout=5.0)
        await until(lambda: any("[crash]" in n for n in conv.notifications), timeout=5.0)

        crash_notes = conv.take_notifications()
        assert any("pane-worker" in n and live_task.id in n for n in crash_notes)
        assert store.board.get(done_task.id).status == "completed"
        recovered = store.board.get(live_task.id)
        assert (recovered.status, recovered.owner) == ("pending", "")

        # 2) All runners are now idle/stopped while live_task is still pending -> Stall Watchdog fires [stall] ONCE!
        await until(lambda: any("[stall]" in n for n in conv.notifications), timeout=5.0)
        stall_notes = conv.take_notifications()
        assert len(stall_notes) == 1 and "[stall]" in stall_notes[0] and live_task.id in stall_notes[0]
        await asyncio.sleep(0.18)
        assert conv.take_notifications() == ()

        # 3) Switch to in-process mode to spawn ping-pong peers `ping-a` & `ping-b` -> hits 12-hop circuit breaker!
        def make_pong(peer: str):
            def _member(messages, tools):
                last = messages[-1]
                if turns(messages) == 1 and "开始协作" not in last.text:
                    return response("就绪")
                if last.role == "user":
                    return response(calls=[tool_call("post_board", "hop", body=f"@{peer} 请接手", summary="互踢交接")])
                return response("交接结束")

            return _member

        ping_script = Script(**{"ping-a": make_pong("ping-b"), "ping-b": make_pong("ping-a")})
        conv.provider = ScriptProvider(ping_script)
        conv._agent.provider = conv.provider
        conv.teams.teammate_mode = "in-process"

        await conv.teams.spawn("ping-a", "general", "待命", isolation="none")
        await conv.teams.spawn("ping-b", "general", "待命", isolation="none")
        await until(idle(conv, "ping-a", "ping-b"))
        await conv.teams.settle()
        await conv.teams._flush_lead(store)
        conv.take_notifications()

        await conv.teams.post_board("@ping-a 开始协作")
        await until(lambda: any("[circuit-breaker]" in n for n in conv.notifications), timeout=10.0)
        await until(idle(conv, "ping-a", "ping-b"))

        breaker_notes = conv.take_notifications()
        assert any("[circuit-breaker]" in n and str(MAX_PEER_HOPS) in n for n in breaker_notes)
        assert store.load().peer_hops == MAX_PEER_HOPS == 12

        # Lead intervenes, completes live_task, and posts [done] -> resets peer_hops and silences Watchdog!
        TeamAccess(store, "ping-a").task_update(live_task.id, owner="ping-a", status="in_progress")
        TeamAccess(store, "ping-a").task_update(live_task.id, status="completed")
        assert store.load().peer_hops == 0
        await conv.teams.post_board("[done] 故障恢复与收尾完成")
        assert store.load().done_seq > 0
        await asyncio.sleep(0.15)
        assert conv.take_notifications() == ()
    finally:
        terminal.cleanup()
        await conv.close()

    assert conv.teams._watcher is None
    await asyncio.sleep(0.05)
    leaked = [t for t in asyncio.all_tasks() - baseline_tasks if not t.done()]
    assert leaked == []
