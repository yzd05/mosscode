import asyncio
import json
from contextlib import aclosing

import pytest

from mosscode.agent import AgentLoop
from mosscode.conversation import Conversation
from mosscode.messages import (
    REMINDER,
    Completed,
    ContentBlock,
    Delta,
    IterationProgress,
    Message,
    ProviderError,
    ToolCall,
    ToolProgress,
    Usage,
    UsageUpdate,
)
from mosscode.tools import create_default_registry
from mosscode.tools.base import Params, Tool, ToolRegistry


def response(text="", calls=(), usage=Usage()):
    return Completed(
        Message("assistant", (ContentBlock("text", text),) if text else (), tool_calls=tuple(calls)),
        "tool_calls" if calls else "stop",
        usage,
    )


def call(name, id="call", **args):
    return ToolCall(id, name, json.dumps(args))


class SequenceProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = 0

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append((tuple(messages), system, tuple(tools), allow_tools))
        try:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                async for event in item():
                    yield event
            else:
                if item.message.text:
                    yield Delta("text", item.message.text)
                yield item
        finally:
            self.closed += 1

    async def close(self):
        pass


async def test_dependent_read_edit_read_loop_and_live_events(tmp_path):
    (tmp_path / "demo.txt").write_text("old")
    provider = SequenceProvider(
        response("先读取", [call("read_file", "r1", path="demo.txt")], Usage(10, 2)),
        response("修改", [call("edit_file", "edit", path="demo.txt", old_text="old", new_text="new")], Usage(30, 3)),
        response("验证", [call("read_file", "r2", path="demo.txt")]),
        response("**已完成**", usage=Usage(50, 4)),
        response("下一轮"),
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    events = [e async for e in conv.reply("修改并检查")]
    assert [e.iteration for e in events if isinstance(e, IterationProgress)] == [1, 2, 3, 4]
    assert [e.text for e in events if isinstance(e, Delta)] == ["先读取", "修改", "验证", "**已完成**"]
    assert [m.role for m in conv.history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "old" in provider.requests[1][0][-1].tool_results[0].output
    assert "new" in provider.requests[3][0][-1].tool_results[0].output
    assert (tmp_path / "demo.txt").read_text() == "new"
    assert all(r[3] for r in provider.requests)
    assert events[-1].reason == "completed" and events[-1].iterations == 4
    assert len([e for e in events if isinstance(e, Completed)]) == 1
    totals = conv.total_usage
    assert (totals.input_tokens, totals.output_tokens, totals.missing_input, totals.requests) == (90, 9, 1, 4)
    assert await conv.ask("继续") == "下一轮"
    assert len(provider.requests[-1][0]) == 9
    assert conv.total_usage.requests == 5


async def test_stream_forwards_before_completion_and_closes_without_commit(tmp_path):
    gate = asyncio.Event()

    async def held():
        yield Delta("text", "正在规划")
        await gate.wait()
        yield response(calls=[call("write_file", path="a", content="x")])

    provider = SequenceProvider(held)
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    async with aclosing(conv.reply("任务")) as events:
        assert isinstance(await anext(events), IterationProgress)
        delta = await asyncio.wait_for(anext(events), 1)
        assert delta.text == "正在规划"
        assert conv.history == () and not list(tmp_path.iterdir())
    assert provider.closed == 1 and not conv._busy


@pytest.mark.parametrize("close_at", [UsageUpdate, ToolProgress])
async def test_close_after_committed_calls_always_pairs_results(tmp_path, close_at):
    provider = SequenceProvider(response(calls=[call("write_file", path="a", content="x")]))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    async with aclosing(conv.reply("任务")) as events:
        async for event in events:
            if isinstance(event, close_at):
                break
    assert not (tmp_path / "a").exists()
    assert conv.history[-1].tool_results[0].code == "not_executed"
    assert not conv._busy


async def test_limit_executes_last_batch_then_can_continue(tmp_path):
    provider = SequenceProvider(
        response(calls=[call("write_file", "w1", path="a", content="first")]),
        response(calls=[call("write_file", "w2", path="a", content="last")]),
        response("后续任务完成"),
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        max_iterations=2,
    )
    events = [e async for e in conv.reply("任务")]
    assert events[-1].reason == "max_iterations"
    assert len(provider.requests) == 2 and len(conv.history) == 5
    assert (tmp_path / "a").read_text() == "last"
    assert not conv.history[-1].tool_results[0].is_error
    assert await conv.ask("继续") == "后续任务完成"
    assert provider.requests[-1][0][-2].role == "tool"


async def test_consecutive_all_unknown_rounds_reset_on_registered_tool(tmp_path):
    unknown = response(calls=[call("invented")])
    mixed = response(calls=[call("invented"), call("read_file", "read", path="missing")])
    provider = SequenceProvider(unknown, unknown, mixed, unknown, unknown, unknown)
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    events = [e async for e in conv.reply("任务")]
    assert events[-1].reason == "unknown_tools" and events[-1].iterations == 6
    assert len(provider.requests) == 6
    assert [r.code for r in conv.history[6].tool_results] == ["unknown_tool", "not_found"]
    assert len(conv.history) == 13 and not conv._busy


@pytest.mark.parametrize("bad", ["missing_id", "duplicate", "incomplete", "error"])
async def test_stream_failure_never_executes_incomplete_or_invalid_calls(tmp_path, bad):
    write = call("write_file", path="a", content="x")

    async def incomplete():
        yield Delta("text", "部分")
        if bad == "error":
            raise ProviderError("模拟断流")

    item = (
        incomplete
        if bad in {"incomplete", "error"}
        else response(calls=[ToolCall("", write.name, write.arguments)] if bad == "missing_id" else [write, write])
    )
    provider = SequenceProvider(item)
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    events = []
    with pytest.raises(ProviderError):
        async for event in conv.reply("任务"):
            events.append(event)
    assert events[-1].reason == "error"
    assert conv.history == () and not list(tmp_path.iterdir())
    assert provider.closed == 1 and not conv._busy


async def test_cancel_model_after_tool_results_emits_stop_and_preserves_history(tmp_path):
    started = asyncio.Event()

    async def held():
        yield Delta("thinking", "继续判断")
        started.set()
        await asyncio.Event().wait()

    provider = SequenceProvider(response(calls=[call("write_file", path="a", content="x")]), held, response("继续成功"))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    events = []

    async def consume():
        async for event in conv.reply("任务"):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(ProviderError, match="不能切换"):
        conv.set_mode("plan")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[-1].reason == "cancelled" and not events[-1].results[0].is_error
    assert len(conv.history) == 3 and provider.closed == 2
    assert await conv.ask("继续") == "继续成功"


async def test_plan_offers_only_read_tools_and_blocks_writes_and_command(tmp_path):
    (tmp_path / "a").write_text("old")
    provider = SequenceProvider(
        response(
            calls=[
                call("write_file", "w", path="a", content="new"),
                call("edit_file", "e", path="a", old_text="old", new_text="new"),
                call("execute_command", "c", command="unsupported-command"),
                call("read_file", "r", path="a"),
            ]
        ),
        response("实施计划"),
        response(calls=[call("write_file", path="a", content="new")]),
        response("完成"),
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    conv.set_mode("plan")
    assert await conv.ask("先规划") == "实施计划"
    assert (tmp_path / "a").read_text() == "old"
    assert [r.code for r in conv.history[3].tool_results] == ["plan_blocked"] * 3 + [None]
    assert conv.mode == "plan"
    assert all(
        {t.name for t in r[2]}
        == {
            "read_file",
            "glob",
            "grep",
            "execute_command",
            "exit_plan_mode",
            "web_fetch",
            "todo_write",
            "task_output",
            "task_stop",
        }
        for r in provider.requests
    )
    rules = provider.requests[0][0][-1]
    assert rules.synthetic == REMINDER and "计划模式" in rules.text and conv.history[1] == rules
    conv.set_mode("normal")
    assert await conv.ask("执行") == "完成"
    assert (tmp_path / "a").read_text() == "new" and len(provider.requests[-1][2]) == 10


async def test_unexpected_stream_error_is_safe_stop_and_keeps_completed_tools(tmp_path):
    provider = SequenceProvider(
        response(calls=[call("write_file", path="a", content="x")]), ValueError("private-test-secret")
    )
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    events = []
    with pytest.raises(ProviderError, match="内部错误") as error:
        async for event in conv.reply("任务"):
            events.append(event)
    assert "private-test-secret" not in str(error.value)
    assert events[-1].reason == "error" and not events[-1].results[0].is_error
    assert (tmp_path / "a").read_text() == "x" and len(conv.history) == 3 and not conv._busy


class WorkParams(Params):
    key: str


class ControlledTool(Tool):
    params_model = WorkParams
    description = "可控测试工具"

    def __init__(self, root, name, read_only, entered, release, log):
        super().__init__(root)
        self.name, self.read_only = name, read_only
        self.entered, self.release, self.log = entered, release, log
        self.active = set()

    async def execute(self, params):
        self.active.add(params.key)
        self.log.append(("start", params.key))
        self.entered[params.key].set()
        try:
            await self.release[params.key].wait()
            self.log.append(("end", params.key))
            return params.key, {}
        finally:
            self.active.remove(params.key)


def controlled(tmp_path, keys):
    entered = {k: asyncio.Event() for k in keys}
    release = {k: asyncio.Event() for k in keys}
    log = []
    read = ControlledTool(tmp_path, "read", True, entered, release, log)
    write = ControlledTool(tmp_path, "write", False, entered, release, log)
    from mosscode.permissions import PermissionPolicy

    policy = PermissionPolicy(tmp_path, mode="bypassPermissions", load=False)
    return ToolRegistry([read, write], permissions=policy), read, write, entered, release, log


async def test_parallel_reads_complete_live_but_history_order_and_write_fences_hold(tmp_path):
    registry, read, write, entered, release, log = controlled(tmp_path, ["a", "b", "w", "c"])
    provider = SequenceProvider(
        response(
            calls=[
                call("read", "a", key="a"),
                call("read", "b", key="b"),
                call("write", "w", key="w"),
                call("read", "c", key="c"),
            ]
        ),
        response("完成"),
    )
    conv = Conversation(provider, registry=registry)
    events = []
    finished_b = asyncio.Event()

    async def consume():
        async for e in conv.reply("任务"):
            events.append(e)
            if isinstance(e, ToolProgress) and e.phase == "finished" and e.call.id == "b":
                finished_b.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(asyncio.gather(entered["a"].wait(), entered["b"].wait()), 2)
        assert read.active == {"a", "b"} and not entered["w"].is_set()
        release["b"].set()
        await asyncio.wait_for(finished_b.wait(), 2)
        assert not entered["w"].is_set() and not entered["c"].is_set()
        release["a"].set()
        await asyncio.wait_for(entered["w"].wait(), 2)
        assert not read.active and write.active == {"w"} and not entered["c"].is_set()
        release["w"].set()
        await asyncio.wait_for(entered["c"].wait(), 2)
        release["c"].set()
        await asyncio.wait_for(task, 2)
        assert [r.call_id for r in conv.history[2].tool_results] == ["a", "b", "w", "c"]
        assert [e.call.id for e in events if isinstance(e, ToolProgress) and e.phase == "finished"] == [
            "b",
            "a",
            "w",
            "c",
        ]
        assert [e.call.id for e in events if isinstance(e, ToolProgress) and e.phase == "running"] == [
            "a",
            "b",
            "w",
            "c",
        ]
    finally:
        for gate in release.values():
            gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_at_most_four_reads_and_cancel_settles_all_jobs(tmp_path):
    keys = [str(i) for i in range(7)]
    registry, read, _, entered, release, _ = controlled(tmp_path, keys)
    provider = SequenceProvider(response(calls=[call("read", key, key=key) for key in keys]))
    conv = Conversation(provider, registry=registry)
    events = []

    async def consume():
        async for e in conv.reply("任务"):
            events.append(e)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(asyncio.gather(*(entered[k].wait() for k in keys[:4])), 2)
        assert len(read.active) == 4 and not any(entered[k].is_set() for k in keys[4:])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not read.active and not conv._busy
        results = conv.history[2].tool_results
        assert [r.call_id for r in results] == keys
        assert [r.code for r in results] == ["cancelled"] * 4 + ["not_executed"] * 3
        assert events[-1].reason == "cancelled" and events[-1].results == results
    finally:
        for gate in release.values():
            gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_iteration_cap_requires_positive_integer(limit):
    with pytest.raises(ValueError):
        AgentLoop(None, "", None, max_iterations=limit)


async def test_an_image_read_by_a_tool_reaches_the_next_request_and_follows_the_model(tmp_path):
    from types import SimpleNamespace

    from PIL import Image

    Image.new("RGB", (30, 20), "red").save(tmp_path / "shot.png")
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    provider = SequenceProvider(response(calls=[call("read_file", "r1", path="shot.png")]), response("红色"))
    conv = Conversation(provider, registry=registry)
    assert await conv.ask("看图") == "红色"
    (result,) = provider.requests[1][0][-1].tool_results
    assert [(i.width, i.height, i.alt) for i in result.images] == [(30, 20, "shot.png")]
    assert result.output.startswith("shot.png · 图片 · 30×20 · ")
    # A text-only model: the same read is refused instead of sending an image it cannot take.
    text_only = SequenceProvider(response(calls=[call("read_file", "r2", path="shot.png")]), response("好"))
    text_only.config = SimpleNamespace(accepts_images=False)
    await conv.set_provider(text_only)
    assert await conv.ask("再看") == "好"
    (refused,) = text_only.requests[1][0][-1].tool_results
    assert refused.code == "image_unsupported" and not refused.images
