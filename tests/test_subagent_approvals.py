"""Background and Fork sub-agent approval bubbling, detach handoff, cancellation cleanup, and security bounds."""

import asyncio
from pathlib import Path

import yaml

from mosscode.agents import AgentCatalog
from mosscode.messages import NOTIFICATION, TasksNotified
from mosscode.permissions import ApprovalRequest
from mosscode.tools import create_default_registry
from test_agent import call, response
from test_sessions import drain, make
from test_subagents import (
    ScriptProvider,
    agents_dir,
    asked,
    main_requests,
    project_of,
    settle,
    tool_results_of,
    write_agent,
)


def in_fork(messages) -> bool:
    return any(m.role == "user" and "<fork-directive>" in m.text for m in messages)


def fork_task_text(messages) -> str:
    return next((m.text for m in messages if m.role == "user" and "<fork-directive>" in m.text), "")


def make_conversation(
    tmp_path: Path,
    provider,
    *,
    mode: str = "default",
    interactive: bool = True,
    load_permissions: bool = False,
    **options,
):
    project = project_of(tmp_path)
    registry = create_default_registry(project, permission_mode=mode, load_permissions=load_permissions)
    registry.permissions.interactive = interactive
    return make(tmp_path, provider, registry=registry, agents=AgentCatalog.load(project), **options)


async def test_fork_bubbles_edit_and_command_approvals_and_headless_refuses(tmp_path):
    project = project_of(tmp_path)
    target = project / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    fork_results = []

    def script(messages, system, tools):
        if in_fork(messages):
            last = messages[-1]
            if last.role == "user":
                return response(
                    calls=[call("edit_file", "fe1", path="target.txt", old_text="before", new_text="after")]
                )
            results = last.tool_results
            fork_results.extend(results)
            if results[0].call_id == "fe1":
                if results[0].is_error:
                    return response(f"范围：Fork 被拒 {results[0].code}")
                return response(calls=[call("execute_command", "fc1", command="mkdir fork_dir")])
            return response("范围：Fork 编辑并执行完成")
        if len(messages) == 1:
            return response(calls=[call("read_file", "r1", path="target.txt")])
        if messages[-1].role == "tool" and messages[-1].tool_results[0].call_id == "r1":
            return response(calls=[call("agent", "f1", description="修改并运行", prompt="更新 target.txt 并创建目录")])
        return response("主对话已派发")

    provider = ScriptProvider(script)
    conv = make_conversation(tmp_path, provider, mode="default", interactive=True)
    queue: asyncio.Queue[ApprovalRequest] = asyncio.Queue()
    conv.subagents.on_approval = queue.put_nowait

    await drain(conv.reply("开始"))
    task = conv.tasks.get("t1")
    assert task is not None and task.background and task.policy.interactive

    req1 = await asyncio.wait_for(queue.get(), timeout=5)
    assert req1.operation.tool == "edit_file" and req1.operation.target == "target.txt"
    assert req1.source == "子 Agent fork · 修改并运行"
    assert req1.id in task.pending_approvals
    assert target.read_text(encoding="utf-8") == "before\n"
    assert conv.registry.permissions.respond(req1.id, "once")

    req2 = await asyncio.wait_for(queue.get(), timeout=5)
    assert req2.operation.tool == "execute_command" and req2.operation.target == "mkdir fork_dir"
    assert req1.id not in task.pending_approvals and req2.id in task.pending_approvals
    assert not (project / "fork_dir").exists()
    assert conv.registry.permissions.respond(req2.id, "once")

    await settle(conv)
    assert task.status == "completed" and task.pending_approvals == set()
    assert target.read_text(encoding="utf-8") == "after\n"
    assert (project / "fork_dir").is_dir()
    assert len(conv.notifications) == 1 and "Fork 编辑并执行完成" in conv.notifications[0]
    await conv.close()

    # Without on_approval wired (headless), Fork never hangs and returns approval_required.
    target.write_text("before\n", encoding="utf-8")
    fork_results.clear()
    headless_provider = ScriptProvider(script)
    headless = make_conversation(tmp_path, headless_provider, mode="default", interactive=True)
    assert headless.subagents.on_approval is None
    await drain(headless.reply("开始"))
    await settle(headless)
    assert headless.tasks.get("t1").status == "completed"
    assert fork_results and fork_results[0].code == "approval_required"
    assert target.read_text(encoding="utf-8") == "before\n"
    await headless.close()


async def test_background_general_idle_and_busy_session_grant_isolation_and_deny(tmp_path):
    project = project_of(tmp_path)
    sub_results = []
    busy_entered = asyncio.Event()

    async def busy_main_step(conv):
        busy_entered.set()
        task = conv.tasks.get("t2")
        assert task is not None
        await asyncio.wait_for(task.job, timeout=5)
        return response(calls=[call("glob", "g_busy", pattern="*.txt")])

    holder = {}

    def script(messages, system, tools):
        first = asked(messages)
        if first == "空闲后台写入":
            if len(messages) == 1:
                return response(calls=[call("write_file", "w1", path="scoped.txt", content="v1")])
            last_res = messages[-1].tool_results[0]
            sub_results.append(last_res)
            if last_res.call_id == "w1":
                return response(calls=[call("write_file", "w2", path="scoped.txt", content="v2")])
            if last_res.call_id == "w2":
                return response(calls=[call("write_file", "w3", path="denied.txt", content="no")])
            return response(f"后台完成：{last_res.code}")
        if first == "繁忙后台写入":
            if len(messages) == 1:
                return response(calls=[call("write_file", "wb1", path="while_busy.txt", content="busy-ok")])
            return response("繁忙后台完成")
        last = messages[-1]
        if last.role == "user" and last.synthetic == NOTIFICATION:
            return response("已处理通知")
        if last.role == "user" and last.text == "空闲测试":
            return response(
                calls=[
                    call(
                        "agent",
                        "a_idle",
                        description="后台写文件",
                        prompt="空闲后台写入",
                        agent_type="general",
                        run_in_background=True,
                    )
                ]
            )
        if last.role == "user" and last.text == "繁忙测试":
            return response(
                calls=[
                    call(
                        "agent",
                        "a_busy",
                        description="忙时写文件",
                        prompt="繁忙后台写入",
                        agent_type="general",
                        run_in_background=True,
                    )
                ]
            )
        if last.role == "tool" and last.tool_results[0].call_id == "a_busy":
            return busy_main_step(holder["conv"])
        return response("主轮结束")

    provider = ScriptProvider(script)
    conv = holder["conv"] = make_conversation(tmp_path, provider, mode="default", interactive=True)
    queue: asyncio.Queue[tuple[ApprovalRequest, bool]] = asyncio.Queue()
    conv.subagents.on_approval = lambda req: queue.put_nowait((req, conv._busy))

    # 1) Main loop becomes idle, then background general asks, gets "session", re-writes without asking, then gets "deny".
    await drain(conv.reply("空闲测试"))
    assert not conv._busy
    t1 = conv.tasks.get("t1")
    assert t1 is not None and t1.background

    req1, was_busy_1 = await asyncio.wait_for(queue.get(), timeout=5)
    assert not was_busy_1
    assert req1.operation.tool == "write_file" and req1.operation.target == "scoped.txt"
    assert conv.registry.permissions.respond(req1.id, "session")

    # w2 writes scoped.txt again using t1's session grant, so the next approval request must be w3 (denied.txt).
    req2, was_busy_2 = await asyncio.wait_for(queue.get(), timeout=5)
    assert not was_busy_2
    assert req2.operation.target == "denied.txt"
    assert (project / "scoped.txt").read_text(encoding="utf-8") == "v2"
    assert len(t1.policy.session) == 1
    assert conv.registry.permissions.session == []
    write_tool = conv.registry.get("write_file")
    parent_op = conv.registry.permissions.operation(
        write_tool, write_tool.params_model(path="scoped.txt", content="parent")
    )
    assert conv.registry.permissions.decide(parent_op, read_only=False).action == "ask"

    assert conv.registry.permissions.respond(req2.id, "deny")
    await settle(conv)
    assert t1.status == "completed"
    assert not (project / "denied.txt").exists()
    assert sub_results[-1].is_error and sub_results[-1].code == "permission_denied"
    assert "后台完成：permission_denied" in t1.result
    await drain(conv.notify())

    # 2) Main loop stays busy while background general bubbles an approval and completes.
    async def answer_while_busy():
        await asyncio.wait_for(busy_entered.wait(), timeout=5)
        req_busy, was_busy = await asyncio.wait_for(queue.get(), timeout=5)
        assert was_busy and req_busy.operation.target == "while_busy.txt"
        assert conv.registry.permissions.respond(req_busy.id, "once")

    responder = asyncio.create_task(answer_while_busy())
    events = await drain(conv.reply("繁忙测试"))
    await responder
    assert (project / "while_busy.txt").read_text(encoding="utf-8") == "busy-ok"
    assert any(isinstance(e, TasksNotified) for e in events)
    assert conv.tasks.get("t2").status == "completed"
    await conv.close()


async def test_foreground_moved_to_background_mid_approval_wired_and_headless(tmp_path):
    project = project_of(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "", "SUB-BG-WRITER")

    def script(messages, system, tools):
        if "SUB-BG-WRITER" in system.stable:
            tag = asked(messages)
            if len(messages) == 1:
                return response(calls=[call("write_file", "w1", path=f"{tag}_1.txt", content="one")])
            first_res = messages[-1].tool_results[0]
            if first_res.call_id == "w1":
                if first_res.is_error:
                    return response(f"失败：{first_res.code}")
                return response(calls=[call("write_file", "w2", path=f"{tag}_2.txt", content="two")])
            return response("两步均完成")
        if messages[-1].role == "user" and messages[-1].synthetic is None:
            return response(
                calls=[call("agent", "a1", description="写文件", prompt=messages[-1].text, agent_type="writer")]
            )
        return response("主对话继续")

    # 1) Wired on_approval + Ctrl+B (detach_foreground) mid-approval: stays alive and completes both writes.
    provider = ScriptProvider(script)
    conv = make_conversation(tmp_path, provider, mode="default", interactive=True)
    bubbled: asyncio.Queue[ApprovalRequest] = asyncio.Queue()
    settled: list[str] = []
    conv.subagents.on_approval = bubbled.put_nowait
    conv.subagents.on_approval_settled = settled.append

    foreground_seen = []
    async for event in conv.reply("ctrlb"):
        if isinstance(event, ApprovalRequest):
            foreground_seen.append(event)
            assert conv.tasks.detach_foreground() == 1

    assert len(foreground_seen) == 1
    t1 = conv.tasks.get("t1")
    assert t1.background and t1.status == "running" and t1.policy.interactive
    assert not (project / "ctrlb_1.txt").exists()

    req1 = await asyncio.wait_for(bubbled.get(), timeout=5)
    assert req1.id == foreground_seen[0].id and bubbled.empty()
    assert conv.registry.permissions.respond(req1.id, "once")

    req2 = await asyncio.wait_for(bubbled.get(), timeout=5)
    assert req2.operation.target == "ctrlb_2.txt"
    assert (project / "ctrlb_1.txt").read_text(encoding="utf-8") == "one"
    assert conv.registry.permissions.respond(req2.id, "once")

    await settle(conv)
    assert t1.status == "completed" and "两步均完成" in t1.result
    assert (project / "ctrlb_2.txt").read_text(encoding="utf-8") == "two"
    assert settled == []
    await conv.close()

    # 2) Wired on_approval + auto_background timer mid-approval (using drain, ignoring foreground stream).
    auto_provider = ScriptProvider(script)
    auto_conv = make_conversation(tmp_path, auto_provider, mode="default", interactive=True, agent_auto_background=0.05)
    auto_queue: asyncio.Queue[ApprovalRequest] = asyncio.Queue()
    auto_conv.subagents.on_approval = auto_queue.put_nowait

    await drain(auto_conv.reply("timer"))
    t_auto = auto_conv.tasks.get("t1")
    assert t_auto.background and t_auto.status == "running" and t_auto.policy.interactive
    req_a1 = await asyncio.wait_for(auto_queue.get(), timeout=5)
    assert req_a1.operation.target == "timer_1.txt"
    assert auto_conv.registry.permissions.respond(req_a1.id, "once")
    req_a2 = await asyncio.wait_for(auto_queue.get(), timeout=5)
    assert req_a2.operation.target == "timer_2.txt"
    assert auto_conv.registry.permissions.respond(req_a2.id, "once")
    await settle(auto_conv)
    assert t_auto.status == "completed"
    assert (project / "timer_2.txt").read_text(encoding="utf-8") == "two"
    await auto_conv.close()

    # 3) Unwired on_approval (headless): moving to background mid-approval cleanly returns approval_required.
    headless_provider = ScriptProvider(script)
    headless = make_conversation(tmp_path, headless_provider, mode="default", interactive=True)
    headless_settled: list[str] = []
    headless.subagents.on_approval_settled = headless_settled.append

    seen_headless = []
    async for event in headless.reply("headless"):
        if isinstance(event, ApprovalRequest):
            seen_headless.append(event)
            assert headless.tasks.detach_foreground() == 1
    await settle(headless)
    assert "已转入后台" in tool_results_of(main_requests(headless_provider, "SUB-BG-WRITER")[1])[0].output
    t_headless = headless.tasks.get("t1")
    assert not t_headless.policy.interactive
    assert t_headless.result.startswith("失败：approval_required")
    assert not (project / "headless_1.txt").exists()
    assert headless_settled == [seen_headless[0].id]
    await headless.close()


async def test_stopping_subagent_with_pending_approval_settles_deny_and_withdraws_card(tmp_path):
    project = project_of(tmp_path)
    approval_ready = asyncio.Event()

    async def stop_after_card(task_id: str):
        await asyncio.wait_for(approval_ready.wait(), timeout=5)
        return response(calls=[call("task_stop", f"stop_{task_id}", task_id=task_id)])

    def script(messages, system, tools):
        if in_fork(messages):
            if messages[-1].role == "user":
                tag = "clear" if "待清" in fork_task_text(messages) else "fork"
                return response(calls=[call("write_file", "fw", path=f"orphan_{tag}.txt", content="x")])
            return response("不应到达")
        if asked(messages) == "后台待停":
            if len(messages) == 1:
                return response(calls=[call("write_file", "gw", path="orphan_bg.txt", content="x")])
            return response("不应到达")
        last = messages[-1]
        if last.role == "user" and last.text == "启动并停止Fork":
            return response(calls=[call("agent", "f1", description="待停Fork", prompt="写文件")])
        if last.role == "tool" and last.tool_results[0].call_id == "f1":
            return stop_after_card("t1")
        if last.role == "user" and last.text == "启动后台":
            return response(
                calls=[
                    call(
                        "agent",
                        "g1",
                        description="待停后台",
                        prompt="后台待停",
                        agent_type="general",
                        run_in_background=True,
                    )
                ]
            )
        if last.role == "user" and last.text == "启动待清Fork":
            return response(calls=[call("agent", "fc", description="待清Fork", prompt="待清写文件")])
        return response("好")

    provider = ScriptProvider(script)
    conv = make_conversation(tmp_path, provider, mode="default", interactive=True)
    captured: list[tuple[ApprovalRequest, asyncio.Future]] = []
    settled: list[str] = []

    def on_approval(req: ApprovalRequest) -> None:
        fut = conv.registry.permissions.pending[req.id]
        captured.append((req, fut))
        approval_ready.set()

    conv.subagents.on_approval = on_approval
    conv.subagents.on_approval_settled = settled.append

    # 1) Stop Fork via task_stop tool while ApprovalRequest is pending.
    await drain(conv.reply("启动并停止Fork"))
    assert len(captured) == 1
    req_fork, fut_fork = captured[0]
    assert fut_fork.done() and not fut_fork.cancelled() and fut_fork.result() == "deny"
    assert settled == [req_fork.id]
    t1 = conv.tasks.get("t1")
    assert t1.status == "cancelled" and t1.pending_approvals == set()
    assert not (project / "orphan_fork.txt").exists()
    assert conv.notifications == ()

    # 2) Stop background general sub-agent via tasks.stop() while ApprovalRequest is pending.
    approval_ready.clear()
    await drain(conv.reply("启动后台"))
    await asyncio.wait_for(approval_ready.wait(), timeout=5)
    req_bg, fut_bg = captured[1]
    t2 = conv.tasks.get("t2")
    assert req_bg.id in t2.pending_approvals and not fut_bg.done()
    assert await conv.tasks.stop("t2")
    assert fut_bg.done() and not fut_bg.cancelled() and fut_bg.result() == "deny"
    assert settled == [req_fork.id, req_bg.id]
    assert t2.status == "cancelled" and t2.pending_approvals == set()
    assert not (project / "orphan_bg.txt").exists()
    assert conv.notifications == ()

    # 3) Clearing the session while a Fork approval is pending also settles as deny and withdraws.
    approval_ready.clear()
    await drain(conv.reply("启动待清Fork"))
    await asyncio.wait_for(approval_ready.wait(), timeout=5)
    req_clear, fut_clear = captured[2]
    await conv.clear()
    assert fut_clear.done() and not fut_clear.cancelled() and fut_clear.result() == "deny"
    assert settled == [req_fork.id, req_bg.id, req_clear.id]
    assert not (project / "orphan_clear.txt").exists()
    await conv.close()


async def test_security_boundaries_plan_mode_and_deny_rules_never_emit_approvals(tmp_path):
    project = project_of(tmp_path)
    (project / "existing.txt").write_text("keep\n", encoding="utf-8")
    (project / "secret").mkdir(exist_ok=True)
    (project / "secret" / "key.txt").write_text("top-secret\n", encoding="utf-8")

    # 1) Plan mode blocks writes, edits, and non-read-only commands in both Fork and background general before asking.
    plan_outcomes: dict[str, list] = {"fork": [], "general": []}

    def plan_script(messages, system, tools):
        if in_fork(messages):
            if messages[-1].role == "user":
                return response(
                    calls=[
                        call("write_file", "pw", path="plan_new.txt", content="x"),
                        call("execute_command", "pc", command="mkdir plan_forbidden"),
                    ]
                )
            plan_outcomes["fork"].extend(messages[-1].tool_results)
            return response("范围：计划模式已拦截")
        if asked(messages) == "计划后台尝试写入":
            if all(m.role == "user" for m in messages):
                return response(
                    calls=[
                        call("write_file", "gw", path="plan_bg.txt", content="x"),
                        call("execute_command", "gc", command="mkdir plan_bg_forbidden"),
                    ]
                )
            plan_outcomes["general"].extend(messages[-1].tool_results)
            return response("计划后台结束")
        if all(m.role == "user" for m in messages):
            return response(
                calls=[
                    call("agent", "f1", description="计划Fork", prompt="尝试写入"),
                    call(
                        "agent",
                        "g1",
                        description="计划后台",
                        prompt="计划后台尝试写入",
                        agent_type="general",
                        run_in_background=True,
                    ),
                ]
            )
        return response("完成")

    plan_conv = make_conversation(tmp_path, ScriptProvider(plan_script), mode="plan", interactive=True)
    plan_bubbled: list[ApprovalRequest] = []
    plan_conv.subagents.on_approval = plan_bubbled.append
    await drain(plan_conv.reply("测试计划边界"))
    await settle(plan_conv)

    assert plan_bubbled == []
    assert [r.code for r in plan_outcomes["fork"]] == ["plan_blocked", "plan_blocked"]
    assert [r.code for r in plan_outcomes["general"]] == ["plan_blocked", "plan_blocked"]
    assert not (project / "plan_new.txt").exists() and not (project / "plan_forbidden").exists()
    await plan_conv.close()

    # 2) Explicit deny rules & protected files block before emitting any approval card in default mode.
    policy_dir = project / ".mosscode"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "permissions.yaml").write_text(
        yaml.safe_dump(
            {
                "mode": "default",
                "deny": ["Write(secret/**)", "Edit(secret/**)", "Bash(curl:*)"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    deny_outcomes: dict[str, list] = {"fork": [], "general": []}

    def deny_script(messages, system, tools):
        if in_fork(messages):
            if messages[-1].role == "user":
                return response(
                    calls=[
                        call("write_file", "dw1", path="secret/blocked.txt", content="x"),
                        call("execute_command", "dc1", command="echo ok && curl https://example.com"),
                        call("write_file", "dp1", path=".mosscode/hooks.yaml", content="hooks: []\n"),
                    ]
                )
            deny_outcomes["fork"].extend(messages[-1].tool_results)
            return response("范围：拒绝规则已生效")
        if asked(messages) == "拒绝规则后台检查":
            if len(messages) == 1:
                return response(
                    calls=[
                        call("write_file", "dw2", path="secret/bg.txt", content="x"),
                        call("execute_command", "dc2", command="curl https://example.com"),
                    ]
                )
            deny_outcomes["general"].extend(messages[-1].tool_results)
            return response("后台拒绝规则已生效")
        if len(messages) == 1:
            return response(
                calls=[
                    call("agent", "df", description="拒绝Fork", prompt="测试拒绝"),
                    call(
                        "agent",
                        "dg",
                        description="拒绝后台",
                        prompt="拒绝规则后台检查",
                        agent_type="general",
                        run_in_background=True,
                    ),
                ]
            )
        return response("完成")

    deny_conv = make_conversation(
        tmp_path, ScriptProvider(deny_script), mode="default", interactive=True, load_permissions=True
    )
    deny_bubbled: list[ApprovalRequest] = []
    deny_conv.subagents.on_approval = deny_bubbled.append
    await drain(deny_conv.reply("测试拒绝规则"))
    await settle(deny_conv)

    assert deny_bubbled == []
    assert [r.code for r in deny_outcomes["fork"]] == ["permission_denied", "permission_denied", "policy_protected"]
    assert [r.code for r in deny_outcomes["general"]] == ["permission_denied", "permission_denied"]
    assert not (project / "secret" / "blocked.txt").exists()
    assert not (project / ".mosscode" / "hooks.yaml").exists()
    await deny_conv.close()


async def test_sibling_forks_isolate_session_grants_and_selective_stop(tmp_path):
    project = project_of(tmp_path)

    def script(messages, system, tools):
        if in_fork(messages):
            task_prompt = fork_task_text(messages)
            if "分支甲" in task_prompt:
                if messages[-1].role == "user":
                    return response(calls=[call("write_file", "wa1", path="shared.txt", content="alpha-1")])
                if messages[-1].tool_results[0].call_id == "wa1":
                    return response(calls=[call("write_file", "wa2", path="shared.txt", content="alpha-2")])
                return response("范围：分支甲完成")
            if messages[-1].role == "user":
                return response(calls=[call("write_file", "wb1", path="shared.txt", content="beta-1")])
            return response("范围：分支乙完成")
        if len(messages) == 1:
            return response(
                calls=[
                    call("agent", "fa", description="甲任务", prompt="分支甲写入"),
                    call("agent", "fb", description="乙任务", prompt="分支乙写入"),
                ]
            )
        return response("已并发分出")

    conv = make_conversation(tmp_path, ScriptProvider(script), mode="default", interactive=True)
    queue: asyncio.Queue[ApprovalRequest] = asyncio.Queue()
    settled: list[str] = []
    conv.subagents.on_approval = queue.put_nowait
    conv.subagents.on_approval_settled = settled.append

    await drain(conv.reply("并发Fork"))
    t1, t2 = conv.tasks.get("t1"), conv.tasks.get("t2")
    assert t1 is not None and t2 is not None

    first = await asyncio.wait_for(queue.get(), timeout=5)
    if "甲任务" in first.source:
        assert conv.registry.permissions.respond(first.id, "session")
        second = await asyncio.wait_for(queue.get(), timeout=5)
        assert "乙任务" in second.source and second.operation.target == "shared.txt"
        # Stop only t2 while t2's approval is pending; t1 already completed using its isolated session grant.
        assert await conv.tasks.stop("t2")
        assert settled == [second.id]
    else:
        # t2 acquired the approval lock first: stop t2, which releases the lock so t1 asks next.
        assert await conv.tasks.stop("t2")
        assert settled == [first.id]
        second = await asyncio.wait_for(queue.get(), timeout=5)
        assert "甲任务" in second.source
        assert conv.registry.permissions.respond(second.id, "session")

    await settle(conv)
    assert t1.status == "completed" and t2.status == "cancelled"
    assert queue.empty()
    assert (project / "shared.txt").read_text(encoding="utf-8") == "alpha-2"
    assert conv.registry.permissions.session == []
    await conv.close()
