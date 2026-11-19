"""Blackboard single-truth routing, peer-to-peer handoffs, verifier role, watchdog, crash recovery & active slots."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from mosscode.agents import parse_agent
from mosscode.messages import ToolCall
from mosscode.teams import LEAD, Blackboard, TeamError
from mosscode.teams.blackboard import extract_mentions, extract_refs
from mosscode.teams.runtime import MAX_MEMBERS
from mosscode.teams.watchdog import MAX_PEER_HOPS
from mosscode.tools.team import TeamSpawnParams
from test_agent import call, response
from test_subagents import ScriptProvider, agents_dir, conversation, project_of, write_agent
from test_team_backends import Terminal, pane_conversation
from test_teams import Script, done, idle, names, tool_call, turns, until


def swarm_conversation(tmp_path, script, *, max_members: int = MAX_MEMBERS, **options):
    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, teams=True, team_max_members=max_members, **options)
    conv.teams.poll = 0.03
    conv.teams.stop_wait = 1.0
    return provider, conv


# 1. Blackboard #seq, code-aware @mention/#ref extraction & single-truth routing -------------------------


def test_blackboard_seq_monotonicity_reads_and_mention_extraction(tmp_path):
    board = Blackboard(tmp_path / "board.jsonl")
    roster = ("alice", "bob", "integrator", "verifier")

    code_heavy = (
        "请@integrator合流 #1 与 `#2`，忽略行内 `@bob` 和 `` `@alice` `` 以及 ```@verifier```。\n"
        "```python\n"
        "@dataclass\n"
        "@pytest.mark.asyncio\n"
        "def handler():\n"
        "    return '@alice @lead #99'\n"
        "```\n"
        "联系邮箱 dev@alice.example.com、装饰器 @app.route 与未知提及 @param 也不应触发唤醒。"
    )
    assert extract_mentions(code_heavy, roster, author="alice") == ("integrator",)
    assert extract_refs(code_heavy) == (1, 2)

    p1 = board.append("lead", "初始需求", kind="brief", explicit_mentions=("alice",), valid_members=roster)
    p2 = board.append("alice", code_heavy, valid_members=roster)
    p3 = board.append("integrator", "广播通知 @all 请查看 #2", valid_members=roster)
    p4 = board.append("lead", "[done] 全部合并完成", valid_members=roster)

    assert [p.seq for p in (p1, p2, p3, p4)] == [1, 2, 3, 4]
    assert p1.summary == "初始需求" and p1.kind == "brief"
    assert p2.mentions == ("integrator",) and p2.refs == (1, 2)
    assert p3.mentions == ("*",) and p3.refs == (2,)
    assert p4.kind == "done" and p4.mentions == ()

    since_posts, head = board.read_since(1, limit=2)
    assert [p.seq for p in since_posts] == [2, 3] and head == 4

    picked, head2 = board.read_seqs([1, 4, 999])
    assert [p.seq for p in picked] == [1, 4] and head2 == 4

    # '*' wakes team members except author, and never wakes lead unless lead was named.
    bob_unread, _ = board.unread_for("bob", 0)
    assert [p.seq for p in bob_unread] == [3]
    int_unread, _ = board.unread_for("integrator", 0)
    assert [p.seq for p in int_unread] == [2]
    lead_unread, _ = board.unread_for(LEAD, 0)
    assert lead_unread == []


def test_blackboard_concurrent_writers_preserve_monotonic_seqs(tmp_path):
    board = Blackboard(tmp_path / "board.jsonl")
    errors: list[Exception] = []

    def writer(author: str) -> None:
        try:
            for idx in range(25):
                board.append(author, f"{author}-{idx}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    posts, head = board.read_since(0, limit=200)
    assert head == 75 and [p.seq for p in posts] == list(range(1, 76))


async def test_single_truth_spawn_send_message_and_atomic_mid_turn_cursor(tmp_path):
    gate = asyncio.Event()

    async def alice(messages, tools):
        if turns(messages) == 1:
            await gate.wait()
            return response("第一轮完成")
        return response("第二轮完成")

    script = Script(alice=alice, bob=done)
    _, conv = swarm_conversation(tmp_path, script)
    await conv.teams.create("t")

    # Spawn prompt mentions @bob and @lead in prose instructions: brief must NOT wake bob or lead!
    _, details = await conv.teams.spawn(
        "alice", "general", "实现模块 A，完成后交接 @bob 并在最后通知 @lead", isolation="none"
    )
    store = conv.teams.current
    assert details["seq"] == 1
    assert store.load().member("alice").board_cursor == 1
    brief = store.blackboard.read_seqs([1])[0][0]
    assert brief.kind == "brief" and brief.mentions == ("alice",)
    assert store.mailbox.peek("alice") == []  # Single-truth: never duplicated into mailbox.

    await until(lambda: "alice" in script.seen)
    first_turn_prompt = script.seen["alice"][0][0][0].text
    assert "[黑板 #1 · lead]" in first_turn_prompt

    # While alice is mid-turn-1, send_message(type="text") arrives on board.jsonl (#2).
    await conv.teams.send("alice", "text", "补充要求 B", "补充")
    assert store.mailbox.peek("alice") == []
    direct = store.blackboard.read_seqs([2])[0][0]
    assert direct.kind == "direct" and direct.mentions == ("alice",)

    gate.set()
    await until(lambda: len(script.seen["alice"]) == 2)
    second_turn_prompt = script.seen["alice"][1][0][-1].text
    assert "[黑板 #2 · lead]" in second_turn_prompt and "[黑板 #1 · lead]" not in second_turn_prompt
    await until(idle(conv, "alice"))
    assert store.load().member("alice").board_cursor == 2
    await conv.close()


# 2. Peer-to-peer pipeline with zero intermediate Lead wakeups -------------------------------------------


async def test_peer_pipeline_author_integrator_verifier_with_zero_intermediate_lead_wakeups(tmp_path):
    project = project_of(tmp_path)

    def author(messages, tools):
        last = messages[-1]
        if last.role == "user":
            return response(calls=[call("write_file", "w1", path="calc.py", content="VALUE = 41\n")])
        if last.tool_results[0].call_id == "w1":
            return response(calls=[tool_call("team_task_update", "t1", id="1", status="in_progress")])
        if last.tool_results[0].call_id == "t1":
            return response(calls=[tool_call("team_task_update", "t2", id="1", status="completed")])
        if last.tool_results[0].call_id == "t2":
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "p1",
                        body="@integrator 初版 calc.py 已写好（参考 #1），同时忽略 `@lead` 代码示例",
                        summary="初版完成",
                    )
                ]
            )
        return response("作者完成")

    def integrator(messages, tools):
        last = messages[-1]
        turn_no = turns(messages)
        if turn_no == 1:
            # Spawn brief turn: wait for author's handoff without waking Lead? Wait: on spawn turn,
            # integrator reads task list and waits or let's check!
            return response("等待作者交接")
        if turn_no == 2:
            # First handoff from author: read briefs #1 and #3, then hand to @verifier (still buggy VALUE = 41).
            if last.role == "user":
                return response(calls=[tool_call("read_board", "rb1", seqs=[1, 3])])
            if last.tool_results[0].call_id == "rb1":
                assert "目标值为 42" in last.tool_results[0].output
                return response(
                    calls=[
                        tool_call("post_board", "p2", body="@verifier 已合流 #3，请审查 calc.py", summary="请求初审")
                    ]
                )
            return response("已交初审")
        # Turn 3: rework requested by verifier -> read calc.py, fix VALUE = 42 and hand back to @verifier.
        if last.role == "user":
            return response(calls=[call("read_file", "ri1", path="calc.py")])
        if last.tool_results[0].call_id == "ri1":
            return response(calls=[call("write_file", "w2", path="calc.py", content="VALUE = 42\n")])
        if last.tool_results[0].call_id == "w2":
            return response(
                calls=[tool_call("post_board", "p4", body="@verifier 已修复 VALUE = 42，请复审", summary="请求复审")]
            )
        return response("修复完毕")

    def verifier(messages, tools):
        last = messages[-1]
        turn_no = turns(messages)
        if turn_no == 1:
            return response("等待合流送审")
        if last.role == "user":
            return response(calls=[call("read_file", "rf1", path="calc.py")])
        if last.tool_results[0].call_id == "rf1":
            content = last.tool_results[0].output
            if "VALUE = 41" in content:
                return response(
                    calls=[
                        tool_call(
                            "post_board",
                            "p3",
                            body="@integrator 审查未通过：VALUE 应为 42 而非 41（对比 #1）",
                            summary="打回返工",
                        )
                    ]
                )
            return response(
                calls=[
                    tool_call(
                        "post_board",
                        "p5",
                        body="@lead 对抗审查通过：VALUE = 42 已核实，可合入主线",
                        summary="审查通过",
                    )
                ]
            )
        return response("审查结束")

    script = Script(author=author, integrator=integrator, verifier=verifier)
    _, conv = swarm_conversation(tmp_path, script)
    await conv.teams.create("swarm")
    access = conv.teams.access()
    access.task_create("实现 calc.py（目标值为 42）")

    # Spawn integrator & verifier first, let their initial setup turns settle and drain initial idle notifications.
    await conv.teams.spawn("integrator", "general", "负责合流与修复", role="integrator", isolation="none")
    await conv.teams.spawn("verifier", "general", "负责只读审查", role="verifier")
    await until(idle(conv, "integrator", "verifier"))
    await conv.teams.settle()
    await conv.teams._flush_lead(conv.teams.current)
    conv.take_notifications()

    lead_wakes: list[int] = []
    conv.on_tasks = lambda: lead_wakes.append(1)

    # Now spawn author (#3) -> author -> @integrator -> @verifier -> @integrator -> @verifier -> @lead!
    await conv.teams.spawn(
        "author",
        "general",
        "请实现 calc.py，要求目标值为 42，完成后点名 @integrator",
        role="worker",
        isolation="none",
    )

    await until(lambda: conv.notifications, timeout=10.0)
    await until(idle(conv, "author", "integrator", "verifier"))
    await asyncio.sleep(0.08)

    updates = conv.take_notifications()
    # Zero intermediate Lead wakeups during author -> integrator -> verifier -> integrator -> verifier!
    assert len(updates) == 1 and len(lead_wakes) == 1
    assert "对抗审查通过：VALUE = 42" in updates[0]
    assert "author 空闲" not in updates[0] and "integrator 空闲" not in updates[0]
    assert (project / "calc.py").read_text(encoding="utf-8") == "VALUE = 42\n"
    assert len(script.seen["integrator"]) >= 3 and len(script.seen["verifier"]) >= 3
    await conv.close()


# 3. Hard read-only verifier enforcement & permission boundary -------------------------------------------


async def test_verifier_hard_blocks_write_and_edit_and_keeps_command_permission_gate(tmp_path):
    project = project_of(tmp_path)
    (project / "target.py").write_text("ORIGINAL = 1\n", encoding="utf-8")

    # Even if a user-defined agent sets permission_mode: bypassPermissions, role='verifier' must NOT
    # loosen a 'default' session mode for non-read-only commands, and must strip write_file/edit_file!
    write_agent(
        agents_dir(tmp_path).parent.parent / "home-agents", "loose", front="permission_mode: bypassPermissions\n"
    )
    user_agent = parse_agent(
        write_agent(tmp_path / "user_agents", "auditor", front="permission_mode: bypassPermissions\n"),
        "user",
    )

    assert TeamSpawnParams(name="w", prompt="work").isolation == "worktree"

    def verifier(messages, tools):
        last = messages[-1]
        if last.role == "user":
            return response(
                calls=[
                    tool_call("read_board", "rb", seqs=[1]),
                    call("read_file", "rf", path="target.py"),
                ]
            )
        if any(r.call_id == "rf" for r in last.tool_results):
            return response(calls=[call("execute_command", "cmd1", command="echo touched")])
        return response("审查完成")

    script = Script(verifier=verifier)
    _, conv = swarm_conversation(tmp_path, script, mode="default", interactive=True)
    conv.subagents.catalog.agents["auditor"] = user_agent

    approvals = []

    def on_approval(req):
        approvals.append(req)
        conv.registry.permissions.respond(req.id, "deny")

    conv.teams.on_approval = on_approval
    await conv.teams.create("sec")

    # Execute via the TeamSpawn tool so auto isolation resolution is exercised too.
    spawn_tool = conv.registry.get("team_spawn")
    _, details = await spawn_tool.execute(
        spawn_tool.validate_arguments(
            {"name": "verifier", "agent_type": "auditor", "prompt": "审查 #1", "role": "verifier"}
        )
    )
    assert details["worktree"] is None  # Auto-defaults to isolation="none" for verifier!

    await until(idle(conv, "verifier"))
    runner = conv.teams.runners["verifier"]
    offered = names(script.seen["verifier"][0][1])

    assert "write_file" not in offered and "edit_file" not in offered
    assert {"read_file", "glob", "grep", "execute_command", "post_board", "read_board", "send_message"} <= offered
    assert "你是只读审查员（verifier），没有文件写入/编辑工具" in runner.loop.system

    # Direct execution of write_file / edit_file on verifier's registry is hard-rejected.
    w_res = await runner.loop.registry.execute(
        ToolCall("bad1", "write_file", json.dumps({"path": "hacked.py", "content": "x"}))
    )
    e_res = await runner.loop.registry.execute(
        ToolCall("bad2", "edit_file", json.dumps({"path": "target.py", "old_str": "1", "new_str": "2"}))
    )
    assert w_res.is_error and w_res.code == "unknown_tool" and not (project / "hacked.py").exists()
    assert e_res.is_error and e_res.code == "unknown_tool"
    assert (project / "target.py").read_text(encoding="utf-8") == "ORIGINAL = 1\n"

    # Non-read-only command in isolation='none' under 'default' mode prompted PermissionPolicy!
    assert approvals and approvals[0].operation.tool == "execute_command"
    await conv.close()


# 4. Crash task recovery on PaneRunner mid-task death ----------------------------------------------------


async def test_pane_crash_resets_in_progress_tasks_and_notifies_lead(tmp_path):
    terminal = Terminal(lambda term, store, name: term.start(store, name, status="running"))
    conv = pane_conversation(tmp_path, terminal)
    try:
        await conv.teams.create("crash-team")
        await conv.teams.spawn("alice", "general", "负责任务 1 和 2", isolation="none")
        store = conv.teams.current
        store.modify(
            lambda t: t.members.append(store.load().member("alice").__class__("bob", "general", status="idle"))
        )

        access = conv.teams.access()
        t1 = access.task_create("已完成任务", owner="alice")
        t2 = access.task_create("进行中任务", owner="alice")
        t3 = access.task_create("他人任务", owner="bob")

        from mosscode.teams.member import TeamAccess

        TeamAccess(store, "alice").task_update(t1.id, status="in_progress")
        TeamAccess(store, "alice").task_update(t1.id, status="completed")
        TeamAccess(store, "alice").task_update(t2.id, status="in_progress")
        TeamAccess(store, "bob").task_update(t3.id, status="in_progress")

        # Kill alice's pane process abruptly mid-task without shutdown_request!
        proc = terminal.processes[0]
        proc.kill()
        proc.wait(timeout=5)

        await until(lambda: "alice" not in conv.teams.runners, timeout=5.0)
        await until(lambda: conv.notifications, timeout=5.0)

        # alice's in_progress task is reset to pending & unassigned; completed and bob's tasks stay intact.
        after_t1 = store.board.get(t1.id)
        after_t2 = store.board.get(t2.id)
        after_t3 = store.board.get(t3.id)
        assert (after_t1.status, after_t1.owner) == ("completed", "alice")
        assert (after_t2.status, after_t2.owner) == ("pending", "")
        assert (after_t3.status, after_t3.owner) == ("in_progress", "bob")

        update = "\n".join(conv.take_notifications())
        assert "<team-update>" in update and "alice" in update and t2.id in update
        posts, _ = store.blackboard.read_since(0)
        assert any(p.kind == "watchdog" and "[crash]" in p.body and "alice" in p.body for p in posts)
    finally:
        terminal.cleanup()
        await conv.close()


# 5. Zero-token Stall Watchdog & MAX_PEER_HOPS = 12 circuit breaker --------------------------------------


async def test_stall_watchdog_deduplicates_wakes_missed_mentions_and_quiets_after_done(tmp_path):
    called = []

    def bob(messages, tools):
        called.append(turns(messages))
        return response("收到")

    script = Script(bob=bob)
    _, conv = swarm_conversation(tmp_path, script)
    await conv.teams.create("watch")
    store = conv.teams.current
    await conv.teams.spawn("bob", "general", "初始工作", isolation="none")
    await until(idle(conv, "bob"))
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    conv.take_notifications()

    # Step 1 of watchdog: external post to board.jsonl without calling runtime.wake("bob") -> watchdog wakes bob!
    store.blackboard.append(LEAD, "@bob 外部写入的唤醒测试", valid_members=("bob",))
    conv.teams.stall_after = 0.05
    await until(lambda: len(called) >= 2, timeout=5.0)
    await until(idle(conv, "bob"))
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    # Intentionally leave bob's idle <team-update> queued in conv.notifications (headless / ACP idle between RPCs)
    # and verify [stall] STILL fires after stall_after instead of resetting last_active forever!
    assert len(conv.notifications) == 1

    # Step 2 of watchdog: unfinished task with everyone idle -> fires [stall] ONCE and deduplicates!
    conv.teams.access().task_create("悬空未完成任务")
    await until(lambda: any("[stall]" in n for n in conv.notifications), timeout=5.0)
    drained = conv.take_notifications()
    stall_updates = [item for item in drained if "[stall]" in item]
    assert len(stall_updates) == 1

    # Wait 4x stall_after with unchanged state -> zero duplicate stall notifications!
    await asyncio.sleep(0.22)
    assert conv.take_notifications() == ()
    stall_posts = [p for p in store.blackboard.peek() if p.kind == "watchdog" and "[stall]" in p.body]
    assert len(stall_posts) == 1

    # Posting [done] sets done_seq and silences future stalls even when task state changes!
    await conv.teams.post_board("[done] 全部收尾")
    assert store.load().done_seq > 0
    conv.teams.access().task_create("后续备忘任务")
    await asyncio.sleep(0.18)
    assert conv.take_notifications() == ()

    # Clean teardown leaves zero background watcher tasks running.
    await conv.close()
    assert conv.teams._watcher is None


async def test_ping_pong_circuit_breaker_stops_at_max_peer_hops_and_escalates_to_lead(tmp_path):
    def ping(target: str):
        def handler(messages, tools):
            last = messages[-1]
            if turns(messages) == 1 and "开始互踢" not in last.text:
                return response("就绪")
            if last.role == "user":
                return response(calls=[tool_call("post_board", "pp", body=f"@{target} 继续处理", summary="交接")])
            return response("本轮结束")

        return handler

    script = Script(a=ping("b"), b=ping("a"))
    _, conv = swarm_conversation(tmp_path, script)
    conv.teams.poll = 0.005  # High-frequency poll to stress concurrent _read_inputs vs try_peer_handoff TOCTOU.
    await conv.teams.create("loop")
    await conv.teams.spawn("a", "general", "等待", isolation="none")
    await conv.teams.spawn("b", "general", "等待", isolation="none")
    await until(idle(conv, "a", "b"))
    await conv.teams.settle()
    await conv.teams._flush_lead(conv.teams.current)
    conv.take_notifications()

    # Kick off an infinite a <-> b ping-pong loop from Lead (resets peer_hops to 0, then a <-> b hops 12 times).
    await conv.teams.post_board("@a 开始互踢")
    await until(lambda: any("[circuit-breaker]" in n for n in conv.notifications), timeout=10.0)
    await until(idle(conv, "a", "b"))

    store = conv.teams.current
    assert store.load().peer_hops == MAX_PEER_HOPS == 12
    updates = conv.take_notifications()
    assert any("[circuit-breaker]" in item and "12" in item for item in updates)

    # Confirm the loop stays dead across multiple poll intervals (no runaway turns!).
    turns_before = (len(script.seen["a"]), len(script.seen["b"]))
    await asyncio.sleep(0.15)
    assert (len(script.seen["a"]), len(script.seen["b"])) == turns_before

    # Lead posting resets peer_hops to 0.
    await conv.teams.post_board("收到熔断告警，暂停互踢")
    assert store.load().peer_hops == 0
    await conv.close()


# 6. Active slot cap (6): stopped members free slots, reviving at cap raises too_many_members ------------


async def test_active_member_slot_cap_six_frees_stopped_slots_and_blocks_excess_revive(tmp_path):
    assert MAX_MEMBERS == 6
    members = {f"m{i}": done for i in range(8)}
    script = Script(**members)
    _, conv = swarm_conversation(tmp_path, script, max_members=MAX_MEMBERS)
    await conv.teams.create("cap")

    for idx in range(6):
        await conv.teams.spawn(f"m{idx}", "general", f"任务 {idx}", isolation="none")
    await until(idle(conv, *(f"m{idx}" for idx in range(6))))

    # 7th active member is rejected.
    with pytest.raises(TeamError) as exc_info:
        await conv.teams.spawn("m6", "general", "溢出任务", isolation="none")
    assert exc_info.value.code == "too_many_members"

    # Stopping 2 of the 6 active members frees 2 active slots.
    await conv.teams.stop_member("m0")
    await conv.teams.stop_member("m1")
    await until(lambda: "m0" not in conv.teams.runners and "m1" not in conv.teams.runners)
    await conv.teams.settle()

    # Now spawning m6 and m7 succeeds (total historical roster = 8, active = 6).
    await conv.teams.spawn("m6", "general", "新任务 6", isolation="none")
    await conv.teams.spawn("m7", "general", "新任务 7", isolation="none")
    await until(idle(conv, "m6", "m7"))
    assert len(conv.teams.current.load().members) == 8

    # Reviving stopped m0 while 6 members are already active returns too_many_members!
    with pytest.raises(TeamError) as revive_exc:
        await conv.teams.send("m0", "text", "尝试恢复", "恢复")
    assert revive_exc.value.code == "too_many_members"

    with pytest.raises(TeamError) as board_revive_exc:
        await conv.teams.post_board("@m0 尝试通过黑板恢复")
    assert board_revive_exc.value.code == "too_many_members"

    await conv.close()


async def test_peer_mention_auto_revives_stopped_member_and_escalates_when_slots_full(tmp_path):
    revived_seen: list[str] = []

    def worker_a(messages, tools):
        last = messages[-1]
        if turns(messages) == 1:
            return response("就绪")
        if last.role == "user" and "请唤醒 bob" in last.text:
            return response(calls=[tool_call("post_board", "p1", body="@bob 请接手后续工作", summary="交接给 bob")])
        if last.role == "user" and "请唤醒 carol" in last.text:
            return response(calls=[tool_call("post_board", "p2", body="@carol 请接手溢出工作", summary="交接给 carol")])
        return response("已交接")

    def bob(messages, tools):
        last = messages[-1]
        if turns(messages) == 1:
            return response("初始完成")
        if last.role == "user":
            revived_seen.append(last.text)
            if "请接手后续工作" in last.text:
                return response(
                    calls=[tool_call("post_board", "pb_lead", body="@lead bob 已恢复并完成", summary="bob 完成")]
                )
        return response("恢复轮结束")

    script = Script(a=worker_a, bob=bob, carol=done)
    _, conv = swarm_conversation(tmp_path, script, max_members=2)
    await conv.teams.create("revive-peer")
    store = conv.teams.current

    await conv.teams.spawn("a", "general", "等待", isolation="none")
    await conv.teams.spawn("bob", "general", "初始", isolation="none")
    await until(idle(conv, "a", "bob"))

    # Stop bob so 1 active slot is free, then spawn carol and stop carol too.
    await conv.teams.stop_member("bob")
    await until(lambda: "bob" not in conv.teams.runners)
    await conv.teams.settle()
    await conv.teams.spawn("carol", "general", "初始", isolation="none")
    await until(idle(conv, "carol"))
    await conv.teams.stop_member("carol")
    await until(lambda: "carol" not in conv.teams.runners)
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    conv.take_notifications()

    # 1) Peer `a` mentions stopped `@bob` while 1 of 2 slots is free -> bob auto-revives and reports to @lead!
    await conv.teams.post_board("@a 请唤醒 bob")
    await until(lambda: any("bob 已恢复并完成" in n for n in conv.notifications), timeout=5.0)
    await until(idle(conv, "a", "bob"))
    assert any("请接手后续工作" in item for item in revived_seen)
    conv.take_notifications()

    # 2) Now 2 of 2 slots (`a` and `bob`) are active; peer `a` mentions stopped `@carol` -> escalates to @lead!
    await conv.teams.post_board("@a 请唤醒 carol")
    await until(lambda: any("[revive-failed]" in n for n in conv.notifications), timeout=5.0)
    await until(idle(conv, "a", "bob"))
    escalation = "\n".join(conv.take_notifications())
    assert "carol" in escalation and "too_many_members" in escalation
    await conv.close()


async def test_dropped_baton_verifier_without_mention_on_turn_two_notifies_lead(tmp_path):
    def verifier(messages, tools):
        if turns(messages) == 1:
            return response("待命")
        # Turn 2+: verifier forgets to call post_board(@lead / @integrator) and replies in plain text!
        return response("我检查完了但忘了发黑板")

    script = Script(verifier=verifier)
    _, conv = swarm_conversation(tmp_path, script)
    await conv.teams.create("baton")
    store = conv.teams.current
    t1 = conv.teams.access().task_create("前序任务")
    TeamAccess_a = store.board
    TeamAccess_a.update(t1.id, actor="verifier", status="in_progress")
    TeamAccess_a.update(t1.id, actor="verifier", status="completed")

    # Turn 1 brief: verifier stands by quietly (0 Lead idle notifications).
    await conv.teams.spawn("verifier", "general", "审查任务", role="verifier")
    await until(idle(conv, "verifier"))
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    assert conv.take_notifications() == ()

    # Turn 2 after all tasks are completed: verifier finishes with plain text (no @mention) -> MUST notify Lead!
    await conv.teams.post_board("@verifier 请开始审查")
    await until(lambda: conv.notifications, timeout=5.0)
    updates = conv.take_notifications()
    assert any("verifier 空闲" in item and "我检查完了但忘了发黑板" in item for item in updates)
    await conv.close()


async def test_member_read_board_contiguous_scan_advances_cursor_and_avoids_duplicate_wakeup(tmp_path):
    turn2_paused = asyncio.Event()
    turn2_release = asyncio.Event()
    turn3_paused = asyncio.Event()
    turn3_release = asyncio.Event()
    bob_turn2_paused = asyncio.Event()
    bob_turn2_release = asyncio.Event()
    alice_reads: list[str] = []

    async def alice(messages, tools):
        last = messages[-1]
        turn_no = turns(messages)
        if turn_no == 1:
            return response("待命")
        if turn_no == 2:
            if last.role == "user":
                turn2_paused.set()
                await turn2_release.wait()
                return response(calls=[tool_call("read_board", "rb_all", since=0, limit=50)])
            alice_reads.append(last.tool_results[0].output)
            return response("已扫描全量黑板")
        if turn_no == 3:
            if last.role == "user":
                turn3_paused.set()
                await turn3_release.wait()
                return response(calls=[tool_call("read_board", "rb_seq", seqs=[1])])
            return response("只看了第一条")
        return response("收到后续未扫描消息")

    async def bob(messages, tools):
        last = messages[-1]
        turn_no = turns(messages)
        if turn_no == 1:
            return response("bob 待命")
        if turn_no == 2:
            if last.role == "user":
                bob_turn2_paused.set()
                await bob_turn2_release.wait()
                return response(calls=[tool_call("read_board", "rb_page", since=6, limit=2)])
            return response("已分页读取")
        return response("bob 收到未分页尾条")

    script = Script(alice=alice, bob=bob)
    _, conv = swarm_conversation(tmp_path, script)
    await conv.teams.create("cursor-scan")
    store = conv.teams.current

    # Turn 1 brief for alice (#1).
    await conv.teams.spawn("alice", "general", "等待通知", isolation="none")
    await until(idle(conv, "alice"))

    # Post #2 (@alice): wakes Turn 2; while paused before read_board(since=0, limit=50), post #3 (@alice).
    await conv.teams.post_board("@alice 第一条通知")
    await turn2_paused.wait()
    await conv.teams.post_board("@alice 第二条通知")
    turn2_release.set()
    await until(idle(conv, "alice"))
    await asyncio.sleep(0.08)

    assert alice_reads and "第二条通知" in alice_reads[0]
    assert store.load().member("alice").board_cursor >= 3
    assert turns(script.seen["alice"][-1][0]) == 2 and len(script.seen["alice"]) == 3

    # Post #4 (@alice): wakes Turn 3; random-access read_board(seqs=[1]) must NOT skip concurrent #5!
    await conv.teams.post_board("@alice 第三条检查点")
    await turn3_paused.wait()
    await conv.teams.post_board("@alice 第四条未扫描")
    turn3_release.set()

    await until(lambda: len(script.seen["alice"]) == 6, timeout=5.0)
    await until(idle(conv, "alice"))
    assert turns(script.seen["alice"][-1][0]) == 4
    assert "第四条未扫描" in script.seen["alice"][-1][0][-1].text
    assert store.load().member("alice").board_cursor >= 5

    # Spawn bob (#6); while bob Turn 2 pauses before read_board(since=6, limit=2), post #8 and #9.
    # Limit-truncated read_since(6, limit=2) returns [#7, #8] -> advances to 8 ONLY, still waking bob on #9!
    await conv.teams.spawn("bob", "general", "分页测试", isolation="none")
    await until(idle(conv, "bob"))
    await conv.teams.post_board("@bob 第七条起点")
    await bob_turn2_paused.wait()
    await conv.teams.post_board("@bob 批次A")
    await conv.teams.post_board("@bob 批次B")
    bob_turn2_release.set()

    await until(lambda: len(script.seen["bob"]) == 4, timeout=5.0)
    await until(idle(conv, "bob"))
    await asyncio.sleep(0.08)
    turn3_prompt = script.seen["bob"][-1][0][-1].text
    assert turns(script.seen["bob"][-1][0]) == 3 and "批次B" in turn3_prompt and "批次A" not in turn3_prompt
    assert len(script.seen["bob"]) == 4
    await conv.close()


async def test_integrator_suppresses_idle_when_upstream_peer_is_running_or_mention_queued(tmp_path):
    author_completed_task = asyncio.Event()
    author_release = asyncio.Event()
    int_turn3_paused = asyncio.Event()
    int_turn3_release = asyncio.Event()

    async def author(messages, tools):
        last = messages[-1]
        if last.role == "user":
            return response(calls=[tool_call("team_task_update", "ta1", id="1", owner="author", status="in_progress")])
        cid = last.tool_results[0].call_id
        if cid == "ta1":
            return response(calls=[tool_call("team_task_update", "ta2", id="1", status="completed")])
        if cid == "ta2":
            author_completed_task.set()
            await author_release.wait()
            return response("上游任务已标记完成并收尾")
        return response("author 完成")

    async def integrator(messages, tools):
        turn_no = turns(messages)
        if turn_no == 1:
            return response("待命")
        if turn_no == 2:
            return response("Turn2继续等待运行中的上游")
        if turn_no == 3:
            int_turn3_paused.set()
            await int_turn3_release.wait()
            return response("Turn3等待并发黑板消息")
        return response("Turn4掉棒提醒")

    script = Script(author=author, integrator=integrator)
    _, conv = swarm_conversation(tmp_path, script)
    await conv.teams.create("int-gate")
    store = conv.teams.current

    access = conv.teams.access()
    access.task_create("前置开发")
    access.task_create("合流验收", blocked_by=["1"])

    await conv.teams.spawn("integrator", "general", "负责合流", role="integrator", isolation="none")
    await until(idle(conv, "integrator"))
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    conv.take_notifications()

    # Case A: author completes Task 1 while still running -> integrator Turn 2 suppresses premature idle!
    await conv.teams.spawn("author", "general", "先完成任务1", isolation="none")
    await author_completed_task.wait()
    await conv.teams.post_board("@integrator 检查是否需等待")
    await until(lambda: len(script.seen["integrator"]) >= 2, timeout=5.0)
    await until(idle(conv, "integrator"))
    await asyncio.sleep(0.06)
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    assert conv.take_notifications() == ()

    # Let author finish idle and drain its idle notification.
    author_release.set()
    await until(idle(conv, "author"))
    await conv.teams.settle()
    await conv.teams._flush_lead(store)
    conv.take_notifications()

    # Case B: all upstream tasks completed and author idle, but unread @integrator arrives mid-Turn 3.
    # Turn 3 suppresses premature idle, Turn 4 picks up the unread mention and reports dropped baton to Lead!
    await conv.teams.post_board("@integrator 发起第3轮")
    await int_turn3_paused.wait()
    store.blackboard.append(LEAD, "@integrator 并发到达的待处理消息", valid_members=("author", "integrator"))
    int_turn3_release.set()

    await until(lambda: conv.notifications, timeout=5.0)
    await until(idle(conv, "integrator"))
    await asyncio.sleep(0.06)
    await conv.teams.settle()
    await conv.teams._flush_lead(store)

    updates = "\n".join(conv.take_notifications())
    assert "Turn3等待并发黑板消息" not in updates
    assert "integrator 空闲" in updates and "Turn4掉棒提醒" in updates
    await conv.close()
