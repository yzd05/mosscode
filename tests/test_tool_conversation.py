import asyncio
from contextlib import aclosing
import json

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError, ResponseBoundary, ToolCall, ToolProgress
from mosscode.tools import create_default_registry


class ToolProvider:
    def __init__(self, calls, *, repeat=False, fail=False, hold=False):
        self.calls = calls
        self.requests = []
        self.repeat = repeat
        self.fail = fail
        self.hold = hold
        self.continuing = asyncio.Event()

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append((tuple(messages), tuple(tools), allow_tools))
        if len(self.requests) == 1:
            yield Completed(Message("assistant", (), tool_calls=self.calls), "tool_calls")
        else:
            self.continuing.set()
            if self.hold:
                await asyncio.Event().wait()
            yield Delta("text", "结果")
            if self.fail:
                raise ProviderError("test final failure")
            yield Completed(Message("assistant", (ContentBlock("text", "结果完成"),),
                                    tool_calls=self.calls if self.repeat else ()), "stop")

    async def close(self):
        pass


def write_call(name="write", content="one"):
    return ToolCall(name, "write_file", json.dumps({"path": "result.txt", "content": content}))


async def test_write_read_barrier_then_natural_completion(tmp_path):
    provider = ToolProvider((write_call(), ToolCall("read", "read_file", '{"path":"result.txt"}')))
    conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False))
    events = [e async for e in conv.reply("write then read")]
    assert next(e for e in events if isinstance(e, Completed)).message.text == "结果完成"
    assert len(provider.requests) == 2
    assert provider.requests[1][2] is True
    assert len(provider.requests[0][1]) == 6
    results = conv.history[2].tool_results
    assert [r.call_id for r in results] == ["write", "read"]
    assert "     1\tone" in results[1].output
    assert [e.phase for e in events if isinstance(e, ToolProgress)] == ["running", "finished", "running", "finished"]
    assert len(conv.history) == 4


@pytest.mark.parametrize("failure", ["final_error", "cancel", "repeat"])
async def test_side_effect_and_results_survive_failure_and_limit(tmp_path, failure):
    provider = ToolProvider((write_call(),), fail=failure == "final_error", hold=failure == "cancel", repeat=failure == "repeat")
    conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False), max_iterations=2)
    if failure == "final_error":
        with pytest.raises(ProviderError):
            await conv.ask("write")
    elif failure == "cancel":
        task = asyncio.create_task(conv.ask("write"))
        await asyncio.wait_for(provider.continuing.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert "最大迭代轮数 2" in await conv.ask("write")
        assert not conv.history[-1].tool_results[0].is_error
    assert (tmp_path / "result.txt").read_text() == "one"
    assert not conv.history[2].tool_results[0].is_error
    assert len(provider.requests) == 2
    assert not conv._busy


async def test_generator_close_pairs_pending_calls_without_executing(tmp_path):
    provider = ToolProvider((write_call(), write_call("write2", "two")))
    conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False))
    async with aclosing(conv.reply("write")) as stream:
        while not isinstance(await anext(stream), ResponseBoundary):
            pass
    assert not (tmp_path / "result.txt").exists()
    assert [r.code for r in conv.history[2].tool_results] == ["not_executed", "not_executed"]
    assert len(provider.requests) == 1


async def test_invalid_parameters_error_goes_to_model_then_next_turn(tmp_path):
    provider = ToolProvider((ToolCall("bad", "write_file", '{"path":'),))
    conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False))
    assert await conv.ask("bad") == "结果完成"
    assert provider.requests[1][0][-1].tool_results[0].code == "invalid_json"
    assert not list(tmp_path.iterdir())
    provider.calls = ()
    await conv.ask("next")
    assert provider.requests[-1][0][2].tool_results[0].call_id == "bad"


async def test_complete_write_remains_recorded_if_cancelled_during_file_io(tmp_path, monkeypatch):
    import threading
    from mosscode.tools import files
    started, release = threading.Event(), threading.Event()
    original = files.atomic_write

    def slow_write(path, content):
        started.set()
        release.wait(3)
        original(path, content)

    monkeypatch.setattr(files, "atomic_write", slow_write)
    conv = Conversation(ToolProvider((write_call(), write_call("later", "two"))), registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False))
    task = asyncio.create_task(conv.ask("write"))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "result.txt").read_text() == "one"
    assert not conv.history[2].tool_results[0].is_error
    assert conv.history[2].tool_results[1].code == "not_executed"
