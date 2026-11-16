import asyncio
from contextlib import aclosing

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.stream_closed = False
        self.fail = False
        self.hold = False
        self.started = asyncio.Event()

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append(tuple(messages))
        self.stream_closed = False
        try:
            yield Delta("text", "part")
            self.started.set()
            if self.hold:
                await asyncio.Event().wait()
            if self.fail:
                raise ProviderError("test failure")
            yield Delta("text", "ial")
            yield Completed(Message("assistant", (ContentBlock("text", "partial"),)), "stop")
        finally:
            self.stream_closed = True

    async def close(self):
        self.closed = True


async def test_multiturn_and_read_only_history_snapshot():
    provider = FakeProvider()
    conv = Conversation(provider)
    assert await conv.ask("one") == "partial"
    assert await conv.ask("two") == "partial"
    assert [m.text for m in provider.requests[1]] == ["one", "partial", "two"]
    assert len(conv.history) == 4
    assert provider.stream_closed


async def test_failure_rolls_back_entire_turn():
    provider = FakeProvider()
    conv = Conversation(provider)
    await conv.ask("good")
    provider.fail = True
    with pytest.raises(ProviderError):
        await conv.ask("bad")
    assert [m.text for m in conv.history] == ["good", "partial"]
    provider.fail = False
    await conv.ask("retry")
    assert [m.text for m in provider.requests[-1]] == ["good", "partial", "retry"]


async def test_cancel_closes_stream_releases_busy_and_avoids_commit():
    provider = FakeProvider()
    provider.hold = True
    conv = Conversation(provider)
    task = asyncio.create_task(conv.ask("cancelled"))
    await provider.started.wait()
    with pytest.raises(ProviderError, match="已有请求"):
        await conv.ask("concurrent")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.stream_closed
    assert conv.history == ()
    provider.hold = False
    assert await conv.ask("next") == "partial"


async def test_consumer_can_close_after_first_delta():
    provider = FakeProvider()
    conv = Conversation(provider)
    async with aclosing(conv.reply("one")) as stream:
        while not isinstance(await anext(stream), Delta):
            pass
    assert provider.stream_closed
    assert conv.history == ()
    assert await conv.ask("two") == "partial"
