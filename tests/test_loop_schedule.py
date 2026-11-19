"""Tests for session-scoped /loop argument parsing, idle-only coalescing, jitter, and TaskManager integration."""

from __future__ import annotations

import asyncio
import time

import pytest

from mosscode.agents.tasks import ORIGIN_LABELS, Announcer, Task, TaskManager, task_line
from mosscode.loop_schedule import (
    DEFAULT_INTERVAL,
    DEFAULT_MAX_RUNS,
    LOOP_SYNTHETIC,
    MAX_ACTIVE_LOOPS,
    MAX_JITTER,
    MAX_PROMPT_CHARS,
    LoopScheduler,
    ParsedLoop,
    format_interval,
    format_loop_prompt,
    parse_interval,
    parse_loop_args,
)


def test_parse_loop_args_list_and_stop_variants():
    assert parse_loop_args("") == ParsedLoop(action="list")
    assert parse_loop_args("   ") == ParsedLoop(action="list")
    assert parse_loop_args("list") == ParsedLoop(action="list")
    assert parse_loop_args("  LIST ") == ParsedLoop(action="list")

    assert parse_loop_args("stop t1") == ParsedLoop(action="stop", target="t1")
    assert parse_loop_args("stop all") == ParsedLoop(action="stop", target="all")
    assert parse_loop_args("STOP ALL") == ParsedLoop(action="stop", target="all")
    assert parse_loop_args("cancel t12") == ParsedLoop(action="stop", target="t12")

    for bad in ("stop", "stop   ", "cancel", "stop t1 t2"):
        with pytest.raises(ValueError, match="stop"):
            parse_loop_args(bad)


def test_parse_loop_args_intervals_units_and_defaults():
    p_sec = parse_loop_args("30s 检查构建")
    assert p_sec == ParsedLoop(action="create", interval=30.0, interval_text="30 秒", prompt="检查构建", max_runs=100)

    p_min = parse_loop_args("5m /test")
    assert p_min == ParsedLoop(action="create", interval=300.0, interval_text="5 分钟", prompt="/test", max_runs=100)

    p_hour = parse_loop_args("2h 巡检日志")
    assert p_hour == ParsedLoop(action="create", interval=7200.0, interval_text="2 小时", prompt="巡检日志")

    p_day = parse_loop_args("1d 汇总日报")
    assert p_day == ParsedLoop(action="create", interval=86400.0, interval_text="1 天", prompt="汇总日报")

    p_zh_sec = parse_loop_args("30秒 检查服务")
    assert p_zh_sec == ParsedLoop(action="create", interval=30.0, interval_text="30 秒", prompt="检查服务")

    p_zh_min = parse_loop_args("5分钟 检查队列")
    assert p_zh_min == ParsedLoop(action="create", interval=300.0, interval_text="5 分钟", prompt="检查队列")

    p_zh_spaced = parse_loop_args("15 秒 检查端口")
    assert p_zh_spaced == ParsedLoop(action="create", interval=15.0, interval_text="15 秒", prompt="检查端口")

    p_trailing = parse_loop_args("check CI every 2m")
    assert p_trailing == ParsedLoop(action="create", interval=120.0, interval_text="2 分钟", prompt="check CI")

    p_trailing_word = parse_loop_args("check deploy every minute")
    assert p_trailing_word == ParsedLoop(action="create", interval=60.0, interval_text="1 分钟", prompt="check deploy")

    p_leading_every = parse_loop_args("every 45s check health")
    assert p_leading_every == ParsedLoop(action="create", interval=45.0, interval_text="45 秒", prompt="check health")

    p_omitted = parse_loop_args("检查 CI 是否完成")
    assert p_omitted == ParsedLoop(
        action="create",
        interval=DEFAULT_INTERVAL,
        interval_text="5 分钟",
        prompt="检查 CI 是否完成",
        max_runs=DEFAULT_MAX_RUNS,
    )

    # Numeric text glued to non-interval Chinese words stays part of the prompt.
    p_glued = parse_loop_args("10天内的错误日志汇总")
    assert p_glued == ParsedLoop(action="create", interval=300.0, interval_text="5 分钟", prompt="10天内的错误日志汇总")

    # Multiline prompts are preserved intact.
    p_multi = parse_loop_args("30s 检查以下内容：\n1. 构建\n2. 测试")
    assert p_multi.prompt == "检查以下内容：\n1. 构建\n2. 测试"
    assert p_multi.interval == 30.0


def test_parse_loop_args_max_runs_and_validation_bounds():
    assert parse_loop_args("--max-runs 3 30s 检查构建").max_runs == 3
    assert parse_loop_args("30s --max-runs 3 检查构建").max_runs == 3
    assert parse_loop_args("check CI every 2m --max-runs 4") == ParsedLoop(
        action="create",
        interval=120.0,
        interval_text="2 分钟",
        prompt="check CI",
        max_runs=4,
    )
    assert parse_loop_args("--count=5 /review") == ParsedLoop(
        action="create",
        interval=300.0,
        interval_text="5 分钟",
        prompt="/review",
        max_runs=5,
    )

    # Non-recursive slash command starting with /loopback is allowed.
    assert parse_loop_args("30s /loopback").prompt == "/loopback"

    # Below 10s rejected.
    for too_short in ("9s check", "0s check", "5秒 check", "check CI every 5s", "every second"):
        with pytest.raises(ValueError, match="轮询间隔至少为 10 秒"):
            parse_loop_args(too_short)

    # Above 7d rejected.
    for too_long in ("8d check", "169h check", "check CI every 10d", "8天 check"):
        with pytest.raises(ValueError, match="轮询间隔最多为 7 天"):
            parse_loop_args(too_long)

    # Nested /loop self-recursion rejected.
    for nested in ("/loop 5m hi", "30s /loop check", "/LOOP", "  30s   /Loop test  "):
        with pytest.raises(ValueError, match="不能在 /loop 中嵌套调用 /loop"):
            parse_loop_args(nested)

    # Empty or oversized prompts and invalid --max-runs rejected.
    for empty in ("30s", "every 5m", "--max-runs 3"):
        with pytest.raises(ValueError, match="不能为空"):
            parse_loop_args(empty)

    with pytest.raises(ValueError, match=str(MAX_PROMPT_CHARS)):
        parse_loop_args("30s " + ("a" * (MAX_PROMPT_CHARS + 1)))

    for bad_runs in ("--max-runs 0 30s hi", "--max-runs -2 30s hi", "--max-runs 1001 30s hi", "--max-runs abc 30s hi"):
        with pytest.raises(ValueError):
            parse_loop_args(bad_runs)

    for dangling in ("--max-runs", "30s hi --max-runs", "--count="):
        with pytest.raises(ValueError):
            parse_loop_args(dangling)

    with pytest.raises(ValueError, match="重复"):
        parse_loop_args("--max-runs 2 30s hi --count 3")


def test_interval_helpers_and_jitter_bounds():
    assert parse_interval("1.5m") == (90.0, "1.5 分钟")
    assert format_interval(30.0) == "30 秒"
    assert format_interval(300.0) == "5 分钟"
    assert format_interval(7200.0) == "2 小时"
    assert format_interval(86400.0) == "1 天"
    assert LOOP_SYNTHETIC == "loop"

    tasks = TaskManager()

    async def noop(_task: Task, _prompt: str) -> None:
        return None

    exact = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.0)
    assert exact.next_delay(10.0) == 10.0

    low = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.10, rng=lambda: 0.0)
    assert low.next_delay(10.0) == 10.0

    mid = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.10, rng=lambda: 0.5)
    assert mid.next_delay(20.0) == pytest.approx(21.0)

    capped = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.10, rng=lambda: 1.0)
    assert capped.next_delay(600.0) == pytest.approx(600.0 + MAX_JITTER)

    sampled = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.10)
    for _ in range(200):
        delay = sampled.next_delay(10.0)
        assert 10.0 <= delay <= 11.0


async def test_idle_only_coalescing_without_catchup_burst():
    tasks = TaskManager()
    busy = True
    fired: list[tuple[str, str, int]] = []
    first_fire = asyncio.Event()

    async def trigger(task: Task, prompt: str) -> None:
        assert task.loop is not None
        fired.append((task.id, prompt, task.loop.runs))
        first_fire.set()

    scheduler = LoopScheduler(tasks, is_busy=lambda: busy, trigger=trigger, jitter_ratio=0.0)
    task = scheduler.schedule(0.02, "0.02 秒", "检查 CI 状态", max_runs=5)
    try:
        # Hold busy across ~6 intervals; spurious notify_idle() while still busy must not fire.
        await asyncio.sleep(0.06)
        assert task.loop is not None
        assert task.loop.pending_fire is True
        assert task.loop.next_fire is None
        scheduler.notify_idle()  # Still busy!
        await asyncio.sleep(0.06)
        assert fired == []
        assert task.loop.pending_fire is True

        # Flip idle and notify: coalesced pending_fire triggers exactly once, not 6 times.
        busy = False
        scheduler.notify_idle()
        await asyncio.wait_for(first_fire.wait(), timeout=1.0)
        await asyncio.sleep(0.005)
        assert len(fired) == 1
        assert fired[0] == (task.id, "检查 CI 状态", 1)
        assert task.loop.pending_fire is False
        assert format_loop_prompt(task) == f"[定时任务 {task.id} · 每 0.02 秒 · 第 1 次]\n检查 CI 状态"
    finally:
        await scheduler.close()


async def test_simultaneous_loops_serialize_across_idle_windows():
    tasks = TaskManager()
    busy = True
    fired_order: list[str] = []
    triggered = asyncio.Event()

    async def trigger(task: Task, _prompt: str) -> None:
        nonlocal busy
        busy = True  # Firing a loop starts a session turn, making the session busy for peers.
        fired_order.append(task.id)
        triggered.set()

    scheduler = LoopScheduler(tasks, is_busy=lambda: busy, trigger=trigger, jitter_ratio=0.0)
    t1 = scheduler.schedule(0.015, "0.015 秒", "巡检 A", max_runs=2)
    t2 = scheduler.schedule(0.015, "0.015 秒", "巡检 B", max_runs=2)
    try:
        await asyncio.sleep(0.04)
        assert t1.loop is not None and t2.loop is not None
        assert t1.loop.pending_fire is True and t2.loop.pending_fire is True

        # First idle window: only one loop may fire; the other sees busy==True and waits.
        busy = False
        scheduler.notify_idle()
        await asyncio.wait_for(triggered.wait(), timeout=1.0)
        await asyncio.sleep(0.01)
        assert len(fired_order) == 1
        remaining = t2 if fired_order[0] == t1.id else t1
        assert remaining.loop is not None and remaining.loop.pending_fire is True

        # Second idle window: the second loop now fires.
        triggered.clear()
        busy = False
        scheduler.notify_idle()
        await asyncio.wait_for(triggered.wait(), timeout=1.0)
        assert set(fired_order) == {t1.id, t2.id}
    finally:
        await scheduler.close()


async def test_post_completion_spacing_and_max_runs_auto_completion():
    tasks = TaskManager()
    announcements: list[str] = []
    tasks.on_change = Announcer(announcements.append)
    events: list[tuple[float, float]] = []

    async def slow_trigger(_task: Task, _prompt: str) -> None:
        start = time.monotonic()
        await asyncio.sleep(0.05)  # Slower than the 0.025s interval!
        end = time.monotonic()
        events.append((start, end))

    scheduler = LoopScheduler(tasks, is_busy=lambda: False, trigger=slow_trigger, jitter_ratio=0.0)
    task = scheduler.schedule(0.025, "0.025 秒", "慢速检查", max_runs=2)
    assert announcements == []  # /loop start is reported by the /loop command, not duplicated by Announcer.
    assert task.job is not None
    await asyncio.wait_for(task.job, timeout=2.0)

    assert len(events) == 2
    gap_after_first_completion = events[1][0] - events[0][1]
    assert gap_after_first_completion >= 0.024
    assert task.status == "completed"
    assert task.loop is not None
    assert task.loop.runs == 2
    assert task.loop.end_reason == "已达最大运行次数 2"
    assert task.loop.next_fire is None
    assert task_line(task).endswith("已完成（已达最大运行次数 2）")
    assert len(announcements) == 1 and "已达最大运行次数 2" in announcements[0]


async def test_lifespan_auto_expiry_when_idle_and_when_busy():
    tasks = TaskManager()
    fired = 0

    async def trigger(_task: Task, _prompt: str) -> None:
        nonlocal fired
        fired += 1

    # Expires before a long interval ever fires.
    s_idle = LoopScheduler(tasks, is_busy=lambda: False, trigger=trigger, jitter_ratio=0.0, lifespan=0.04)
    t_idle = s_idle.schedule(10.0, "10 秒", "长间隔短寿命")
    assert t_idle.job is not None
    await asyncio.wait_for(t_idle.job, timeout=1.0)
    assert fired == 0
    assert t_idle.status == "completed"
    assert t_idle.loop is not None and t_idle.loop.end_reason == "已达 7 天有效期上限"

    # Expires while stuck waiting on a busy session without notify_idle().
    s_busy = LoopScheduler(tasks, is_busy=lambda: True, trigger=trigger, jitter_ratio=0.0, lifespan=0.05)
    t_busy = s_busy.schedule(0.015, "0.015 秒", "忙碌期过期")
    assert t_busy.job is not None
    await asyncio.wait_for(t_busy.job, timeout=1.0)
    assert fired == 0
    assert t_busy.status == "completed"
    assert t_busy.loop is not None
    assert t_busy.loop.pending_fire is False
    assert t_busy.loop.end_reason == "已达 7 天有效期上限"


async def test_task_manager_slots_stop_and_zero_leaked_tasks():
    baseline = set(asyncio.all_tasks())
    tasks = TaskManager(max_background=4)

    async def noop(_task: Task, _prompt: str) -> None:
        return None

    scheduler = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.0)

    # Create 6 loop tasks (> max_background=4); agent background slots remain completely free.
    loops = [scheduler.schedule(10.0, "10 秒", f"巡检 {i}") for i in range(6)]
    assert all(t.kind == "loop" and ORIGIN_LABELS[t.origin] == "/loop 定时" for t in loops)
    assert tasks.full() is False
    assert len(scheduler.active()) == 6

    # Adding 4 agent background tasks fills the agent cap; scheduler.stop(<agent_id>) leaves agents untouched.
    blocker = asyncio.Event()
    agents = []
    for i in range(4):
        agent_task = tasks.new(
            origin="agent",
            agent="subagent",
            agent_type="explore",
            description=f"子任务 {i}",
            prompt="p",
            background=True,
        )
        tasks.launch(agent_task, blocker.wait)
        agents.append(agent_task)
    assert tasks.full() is True
    assert await scheduler.stop(agents[0].id) == []
    assert agents[0].status == "running"

    # Direct TaskManager.stop cancels a loop immediately with stopped=True.
    target = loops[0]
    assert await tasks.stop(target.id) is True
    assert target.stopped is True
    assert target.status == "cancelled"
    assert target.result == "[定时巡检已停止]"
    assert target.loop is not None and target.loop.next_fire is None
    assert len(scheduler.active()) == 5
    assert len(scheduler.all_loops()) == 6

    # Fill up to MAX_ACTIVE_LOOPS and verify cap enforcement.
    while len(scheduler.active()) < MAX_ACTIVE_LOOPS:
        scheduler.schedule(10.0, "10 秒", "填充上限")
    with pytest.raises(ValueError, match=str(MAX_ACTIVE_LOOPS)):
        scheduler.schedule(10.0, "10 秒", "超出上限")

    # Stop single loop via scheduler.stop(id), then remaining via scheduler.close().
    stopped_one = await scheduler.stop(loops[1].id)
    assert stopped_one == [loops[1]]
    assert loops[1].status == "cancelled" and loops[1].stopped is True

    await scheduler.close()
    assert scheduler.active() == []

    # Self-stop from inside trigger must not deadlock.
    self_stopped = asyncio.Event()

    async def stop_self(task: Task, _prompt: str) -> None:
        await scheduler.stop(task.id)
        self_stopped.set()

    self_scheduler = LoopScheduler(tasks, is_busy=lambda: False, trigger=stop_self, jitter_ratio=0.0)
    t_self = self_scheduler.schedule(0.01, "0.01 秒", "自停止")
    await asyncio.wait_for(self_stopped.wait(), timeout=1.0)
    assert t_self.job is not None
    await asyncio.wait_for(t_self.job, timeout=1.0)
    assert t_self.status == "cancelled" and t_self.stopped is True

    # Cancel dummy agent tasks and verify zero leaked asyncio tasks remain.
    await tasks.cancel_all()
    await asyncio.sleep(0)
    leaked = set(asyncio.all_tasks()) - baseline
    assert leaked == set()


async def test_task_tools_reset_and_trigger_failure_handling(tmp_path):
    from types import SimpleNamespace

    from mosscode.tools.agent import TaskOutput, TaskOutputParams, TaskStop, TaskStopParams

    baseline = set(asyncio.all_tasks())
    tasks = TaskManager()
    runner = SimpleNamespace(tasks=tasks, main=SimpleNamespace(notifications=[]))
    out_tool = TaskOutput(tmp_path, runner)
    stop_tool = TaskStop(tmp_path, runner)

    async def noop(_task: Task, _prompt: str) -> None:
        return None

    scheduler = LoopScheduler(tasks, is_busy=lambda: False, trigger=noop, jitter_ratio=0.0)
    t1 = scheduler.schedule(10.0, "10 秒", "检查服务")
    out_text, out_details = await out_tool.execute(TaskOutputParams(task_id=t1.id))
    assert t1.id in out_text and out_details["status"] == "running"

    stop_text, stop_details = await stop_tool.execute(TaskStopParams(task_id=t1.id))
    assert "已停止" in stop_text and stop_details.get("stopped") is True
    assert t1.status == "cancelled" and t1.stopped is True

    # Resetting TaskManager cancels active loops and clears task registry.
    scheduler.schedule(10.0, "10 秒", "待清理 A")
    scheduler.schedule(10.0, "10 秒", "待清理 B")
    await tasks.reset()
    assert tasks.tasks == {} and scheduler.active() == []

    # Unexpected exception in trigger marks the loop task failed cleanly without leaking tasks.
    async def broken_trigger(_task: Task, _prompt: str) -> None:
        raise RuntimeError("boom")

    err_scheduler = LoopScheduler(tasks, is_busy=lambda: False, trigger=broken_trigger, jitter_ratio=0.0)
    t_err = err_scheduler.schedule(0.01, "0.01 秒", "异常触发")
    assert t_err.job is not None
    await asyncio.wait_for(t_err.job, timeout=1.0)
    assert t_err.status == "failed"
    assert t_err.result == "[定时巡检未完成：内部错误 RuntimeError]"
    assert t_err.loop is not None and t_err.loop.next_fire is None

    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) - baseline == set()
