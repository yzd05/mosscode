"""End-to-end integration tests for Code Intelligence (repo_map + AST outline/symbol) and /loop scheduler."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from textual.widgets import Static

from mosscode.agents import AgentCatalog
from mosscode.agents.runtime import WORKSPACE_TOOLS
from mosscode.compaction import clear_results, collapse_end, released_reads
from mosscode.context import estimate_messages
from mosscode.conversation import Conversation
from mosscode.goal import conversation_tail
from mosscode.messages import Completed, ContentBlock, Message, ToolCall, ToolResult
from mosscode.permissions import PermissionPolicy
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp, PromptArea
from test_agent import response


def call(tool_name: str, call_id: str, **kwargs) -> ToolCall:
    return ToolCall(call_id, tool_name, json.dumps(kwargs, ensure_ascii=False))


async def _send(app: MossCodeApp, pilot, text: str) -> None:
    field = app.query_one("#prompt", PromptArea)
    await app.on_prompt_area_submitted(PromptArea.Submitted(field, text))
    await pilot.pause()


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)


async def test_e2e_agent_workflow_repomap_outline_symbol_edit_and_subagents_teams(tmp_path: Path):
    project = tmp_path / "repo"
    (project / "auth").mkdir(parents=True)
    (project / "secrets").mkdir(parents=True)
    (project / "unrelated").mkdir(parents=True)
    (project / ".mosscode").mkdir(parents=True)

    (project / ".mosscode" / "permissions.yaml").write_text(
        'deny:\n  - "Read(secrets/**)"\n',
        encoding="utf-8",
    )

    py_file = project / "auth" / "service.py"
    py_file.write_text(
        "def helper_banner() -> str:\n"
        '    return "v1"\n\n\n'
        "class AuthService:\n"
        "    def login(self, token: str) -> bool:\n"
        "        return validateToken(token)\n",
        encoding="utf-8",
    )

    ts_file = project / "auth" / "validator.ts"
    ts_file.write_text(
        "export class TokenValidator {\n"
        "  validateToken(token: string): boolean {\n"
        "    return token.length > 3;\n"
        "  }\n"
        "}\n\n"
        "export function validateToken(token: string): boolean {\n"
        "  return new TokenValidator().validateToken(token);\n"
        "}\n",
        encoding="utf-8",
    )

    (project / "secrets" / "vault.py").write_text(
        'class UltraSecretKeyVault:\n    def master_signing_material(self) -> str:\n        return "never-index"\n',
        encoding="utf-8",
    )
    (project / "unrelated" / "noise.py").write_text(
        "def compute_unrelated_metrics(items: list[int]) -> int:\n    return sum(items)\n",
        encoding="utf-8",
    )
    _init_git_repo(project)

    assert "repo_map" in WORKSPACE_TOOLS

    seen_tools_by_agent: dict[str, set[str]] = {}
    explore_step = 0
    plan_step = 0
    worktree_step = 0
    verifier_step = 0
    main_step = 0

    class WorkflowProvider:
        def __init__(self) -> None:
            self.closed = 0

        async def stream(self, messages, system, tools=(), *, allow_tools=True):
            nonlocal explore_step, plan_step, worktree_step, verifier_step, main_step
            tool_names = {t.name for t in tools}
            sys_text = system.stable if hasattr(system, "stable") else str(system)
            if "<team-member>" in sys_text:
                seen_tools_by_agent["verifier"] = tool_names
                verifier_step += 1
                if verifier_step == 1:
                    yield response(calls=[call("repo_map", "vf-rm", query="validateToken")])
                    return
                if verifier_step == 2:
                    yield response(
                        calls=[
                            call(
                                "post_board",
                                "vf-post",
                                summary="验收通过",
                                body="@lead repo_map 验证通过",
                            )
                        ]
                    )
                    return
                yield response("审查完成")
                return

            if "代码探索子 Agent" in sys_text:
                seen_tools_by_agent["explore"] = tool_names
                explore_step += 1
                if explore_step == 1:
                    yield response(calls=[call("repo_map", "ex-rm", query="AuthService")])
                    return
                yield response("探索结论：AuthService 位于 auth/service.py。")
                return

            if "规划子 Agent" in sys_text:
                seen_tools_by_agent["plan"] = tool_names
                plan_step += 1
                if plan_step == 1:
                    yield response(calls=[call("repo_map", "pl-rm", query="TokenValidator")])
                    return
                yield response("规划结论：更新 auth/service.py 与 auth/validator.ts。")
                return

            if any("<worktree-context>" in m.text for m in messages if m.role == "user"):
                seen_tools_by_agent["worktree"] = tool_names
                worktree_step += 1
                if worktree_step == 1:
                    yield response(calls=[call("repo_map", "wt-rm", query="validateToken")])
                    return
                yield response("工作树子 Agent 已验证 repo_map。")
                return

            seen_tools_by_agent["main"] = tool_names
            main_step += 1
            if main_step == 1:
                yield response(
                    calls=[
                        call(
                            "repo_map",
                            "m1-map",
                            query="validateToken",
                            focus_files=["auth/service.py"],
                            max_tokens=1500,
                        )
                    ]
                )
                return
            if main_step == 2:
                yield response(
                    calls=[
                        call("read_file", "m2-py-outline", path="auth/service.py", outline=True),
                        call("read_file", "m2-ts-outline", path="auth/validator.ts", outline=True),
                    ]
                )
                return
            if main_step == 3:
                # Editing right after outline=True must fail with not_read!
                yield response(
                    calls=[
                        call(
                            "edit_file",
                            "m3-blocked-edit",
                            path="auth/service.py",
                            old_text="return validateToken(token)",
                            new_text="return bool(token) and validateToken(token)",
                        )
                    ]
                )
                return
            if main_step == 4:
                yield response(
                    calls=[
                        call("read_file", "m4-py-sym", path="auth/service.py", symbol="AuthService.login"),
                        call(
                            "read_file",
                            "m4-ts-sym",
                            path="auth/validator.ts",
                            symbol="TokenValidator.validateToken",
                        ),
                    ]
                )
                return
            if main_step == 5:
                yield response(
                    calls=[
                        call(
                            "edit_file",
                            "m5-py-edit",
                            path="auth/service.py",
                            old_text="return validateToken(token)",
                            new_text="return bool(token) and validateToken(token)",
                        ),
                        call(
                            "edit_file",
                            "m5-ts-edit",
                            path="auth/validator.ts",
                            old_text="return token.length > 3;",
                            new_text="return token.trim().length >= 8;",
                        ),
                    ]
                )
                return
            if main_step == 6:
                yield response(
                    calls=[
                        call(
                            "agent",
                            "m6-explore",
                            agent_type="explore",
                            description="探索认证模块",
                            prompt="用 repo_map 查看认证模块",
                        ),
                        call(
                            "agent",
                            "m6-plan",
                            agent_type="plan",
                            description="规划认证演进",
                            prompt="用 repo_map 规划修改",
                        ),
                        call(
                            "agent",
                            "m6-wt",
                            agent_type="general",
                            isolation="worktree",
                            description="隔离验证地图",
                            prompt="在独立工作树中调用 repo_map",
                        ),
                    ]
                )
                return
            if main_step == 7:
                yield response(calls=[call("team_create", "m7-team", name="qa", description="验证 repo_map")])
                return
            if main_step == 8:
                yield response(
                    calls=[
                        call(
                            "team_spawn",
                            "m8-verifier",
                            name="checker",
                            agent_type="general",
                            role="verifier",
                            prompt="运行 repo_map 并在黑板点名 @lead",
                        )
                    ]
                )
                return
            yield response("全链路代码智能与子 Agent 验证完成")

        async def close(self) -> None:
            self.closed += 1

    policy = PermissionPolicy(project, mode="acceptEdits", load=True, user_path=tmp_path / "user-perms.yaml")
    policy.interactive = True
    registry = create_default_registry(project, permissions=policy)
    catalog = AgentCatalog.load(project, home=tmp_path / "home", plugins=())
    conv = Conversation(
        WorkflowProvider(),
        registry=registry,
        agents=catalog,
        context_root=project,
        sessions_home=tmp_path / "home",
        teams=True,
        teammate_mode="in-process",
    )
    try:
        answer = await conv.ask("重构认证校验逻辑并派子 Agent 与审查员核对")
        assert "验证完成" in answer

        # Wait briefly for the in-process verifier team member to finish its repo_map turn.
        assert conv.teams is not None
        for _ in range(60):
            if verifier_step >= 2:
                break
            await asyncio.sleep(0.02)

        all_results = {r.call_id: r for m in conv.history if m.role == "tool" for r in m.tool_results}

        # 1) repo_map ranked auth/service.py & auth/validator.ts and excluded denied secrets/vault.py.
        rm_out = all_results["m1-map"].output
        assert "auth/service.py:" in rm_out
        assert "auth/validator.ts:" in rm_out
        assert "UltraSecretKeyVault" not in rm_out
        assert "master_signing_material" not in rm_out
        assert "secrets" not in rm_out

        # 2) read_file(outline=True) returned [Lstart-Lend] outlines and did NOT unlock edit_file.
        assert "[L5-L7] class AuthService:" in all_results["m2-py-outline"].output
        assert "[L1-L5] export class TokenValidator" in all_results["m2-ts-outline"].output
        assert all_results["m3-blocked-edit"].is_error
        assert all_results["m3-blocked-edit"].code == "not_read"

        # 3) read_file(symbol=...) returned exact lines AND unlocked edit_file for both Python and TypeScript.
        assert not all_results["m4-py-sym"].is_error
        assert "def login(self, token: str) -> bool:" in all_results["m4-py-sym"].output
        assert not all_results["m4-ts-sym"].is_error
        assert "validateToken(token: string): boolean" in all_results["m4-ts-sym"].output
        assert not all_results["m5-py-edit"].is_error
        assert not all_results["m5-ts-edit"].is_error
        assert "return bool(token) and validateToken(token)" in py_file.read_text(encoding="utf-8")
        assert "return token.trim().length >= 8;" in ts_file.read_text(encoding="utf-8")

        # 4) explore, plan, worktree-isolated sub-agent, and verifier team member all called repo_map cleanly.
        for role_key in ("main", "explore", "plan", "worktree", "verifier"):
            assert "repo_map" in seen_tools_by_agent[role_key], f"{role_key} missing repo_map"
        assert "探索结论" in all_results["m6-explore"].output
        assert "规划结论" in all_results["m6-plan"].output
        assert "工作树子 Agent 已验证 repo_map" in all_results["m6-wt"].output
    finally:
        await conv.close()


async def test_e2e_headless_tui_loop_coalescing_spacing_tasks_stop_and_zero_leaked_tasks(tmp_path: Path):
    baseline = set(asyncio.all_tasks())

    slow_turn_started = asyncio.Event()
    release_slow_turn = asyncio.Event()
    first_loop_entered = asyncio.Event()
    release_first_loop = asyncio.Event()
    second_loop_done = asyncio.Event()

    turn_records: list[tuple[str | None, str]] = []
    loop_task_id_holder: list[str] = []

    class LoopE2EProvider:
        async def stream(self, messages, system, tools=(), *, allow_tools=True):
            last = messages[-1]
            idx = len(turn_records)
            turn_records.append((last.synthetic, last.text))
            if idx == 0:
                slow_turn_started.set()
                await release_slow_turn.wait()
                yield Completed(
                    Message(role="assistant", content=(ContentBlock("text", "慢任务已完成"),)),
                    "stop",
                )
                return
            if idx == 1:
                first_loop_entered.set()
                await release_first_loop.wait()
                yield Completed(
                    Message(role="assistant", content=(ContentBlock("text", "第 1 次巡检正常"),)),
                    "stop",
                )
                return
            if idx == 2:
                # Model inspects the running loop via task_output and stops it via task_stop!
                tid = loop_task_id_holder[0]
                yield response(
                    calls=[
                        call("task_output", "to-loop", task_id=tid, wait=5),
                        call("task_stop", "ts-loop", task_id=tid),
                    ]
                )
                return
            second_loop_done.set()
            yield Completed(
                Message(role="assistant", content=(ContentBlock("text", "已查看并停止定时巡检"),)),
                "stop",
            )

        async def close(self) -> None:
            return None

    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv = Conversation(LoopE2EProvider(), registry=reg)
    app = MossCodeApp(conv, "fake", model="fake-model")

    async with app.run_test(size=(120, 34)) as pilot:
        # 1) Create a /loop via user slash command, verify status bar + /tasks + /loop stop.
        await _send(app, pilot, "/loop 10s 检查构建流水线")
        assert conv.loops is not None
        assert len(conv.loops.active()) == 1
        cmd_loop = conv.loops.active()[0]
        assert "定时 1" in str(app.query_one("#status", Static).render())

        await _send(app, pilot, f"/tasks {cmd_loop.id}")
        notes_text = "\n".join(str(w.render()) for w in app.query(".local-note"))
        assert "定时巡检" in notes_text and "每 10 秒" in notes_text and "检查构建流水线" in notes_text

        await _send(app, pilot, f"/loop stop {cmd_loop.id}")
        assert conv.loops.active() == []
        assert "定时 " not in str(app.query_one("#status", Static).render())

        # 2) Start a slow foreground turn, schedule a fast loop (0.03s), and let ~6 intervals pass while busy.
        await _send(app, pilot, "开始长耗时重构")
        await asyncio.wait_for(slow_turn_started.wait(), timeout=2.0)

        fast_loop = conv.loops.schedule(0.03, "0.03 秒", "自动检查健康度", max_runs=5)
        loop_task_id_holder.append(fast_loop.id)
        await pilot.pause(0.20)

        # Must coalesce to pending_fire=True with ZERO triggers while busy!
        assert fast_loop.loop is not None
        assert fast_loop.loop.pending_fire is True
        assert fast_loop.loop.runs == 0
        assert len(turn_records) == 1

        await _send(app, pilot, "/tasks")
        tasks_summary = "\n".join(str(w.render()) for w in app.query(".local-note"))
        assert "等待空闲触发" in tasks_summary

        # 3) Release the busy turn -> coalesced wakeup fires ONCE on idle; while that loop turn itself runs
        #    longer than interval (0.10s > 0.03s), post-completion spacing prevents mid-turn pending_fire!
        release_slow_turn.set()
        await asyncio.wait_for(first_loop_entered.wait(), timeout=3.0)
        await pilot.pause(0.10)
        assert fast_loop.loop.runs == 1
        assert fast_loop.loop.pending_fire is False
        assert len(turn_records) == 2
        assert turn_records[1][0] == "loop" and "自动检查健康度" in turn_records[1][1]

        # 4) Release first loop turn -> after post-completion interval, second run calls task_output + task_stop!
        release_first_loop.set()
        await asyncio.wait_for(second_loop_done.wait(), timeout=3.0)
        await pilot.pause(0.05)

        assert fast_loop.status == "cancelled" and fast_loop.stopped is True
        assert conv.loops.active() == []
        tool_results = {r.call_id: r for m in conv.history if m.role == "tool" for r in m.tool_results}
        assert not tool_results["to-loop"].is_error
        assert "自动检查健康度" in tool_results["to-loop"].output
        assert not tool_results["ts-loop"].is_error
        assert tool_results["ts-loop"].details.get("stopped") is True

    # After MossCodeApp exits, zero asyncio tasks may remain leaked!
    await asyncio.sleep(0.02)
    leaked = set(asyncio.all_tasks()) - baseline
    assert leaked == set()


def test_compaction_and_goal_seams_for_loop_turns_and_outline_reads():
    # 1) TURN_STARTS includes "loop" so ContextCollapse cleanly segments earlier /loop turns.
    big = "巡检输出详情 " * 600
    history = [
        Message("user", (ContentBlock("text", "开启监控"),)),
        Message("assistant", (ContentBlock("text", big),)),
        Message("user", (ContentBlock("text", "[定时任务 t1 · 每 5 分钟 · 第 1 次]\n检查服务"),), synthetic="loop"),
        Message("assistant", (ContentBlock("text", big),)),
        Message("user", (ContentBlock("text", "[定时任务 t1 · 每 5 分钟 · 第 2 次]\n检查服务"),), synthetic="loop"),
        Message("assistant", (ContentBlock("text", "一切正常"),)),
    ]
    total_est = estimate_messages(history)
    end = collapse_end(history, (), cut=4, estimate=total_est, target=total_est - 500)
    assert end in (2, 4)

    # 2) goal.conversation_tail renders synthetic="loop" cleanly as [定时巡检].
    tail_rendered = conversation_tail(history)
    assert "[定时巡检]" in tail_rendered
    assert "[附加内容 · loop]" not in tail_rendered

    # 3) outline=True in later history never prevents released_reads / clear_results from forgetting FileState!
    file_path = "/repo/sample.py"
    read_history = [
        Message("user", (ContentBlock("text", "读取文件"),)),
        Message(
            "tool",
            (),
            tool_results=(
                ToolResult(
                    "c-full",
                    "read_file",
                    "1\t" + ("x = 1\n" * 400),
                    details={"path": file_path, "lines": 400},
                ),
            ),
        ),
        Message("user", (ContentBlock("text", "查看大纲"),)),
        Message(
            "tool",
            (),
            tool_results=(
                ToolResult(
                    "c-outline",
                    "read_file",
                    "[L1-L10] def foo():",
                    details={"path": file_path, "outline": True, "symbols": 1},
                ),
            ),
        ),
    ]
    assert released_reads(read_history, 0, 2) == (file_path,)
    cleared = clear_results(read_history, cut=2, session=None)
    assert cleared.paths == (file_path,)
