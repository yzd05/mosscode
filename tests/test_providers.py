import asyncio
import json
from dataclasses import replace

import anthropic
import httpx2
import openai
import pytest

from mosscode.config import ProviderConfig
from mosscode.conversation import Conversation
from mosscode.messages import Completed, Delta, ProviderError, Retrying, RunStopped
from mosscode.providers import AnthropicProvider, ChatCompletionsProvider, ResponsesProvider


def frame(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()


class SSEBody(httpx2.AsyncByteStream):
    def __init__(self, chunks, hold=False):
        self.chunks = chunks
        self.hold = hold
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hold:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def anthropic_frames(reason="end_turn", stop=True):
    start = {"id": "msg_test", "type": "message", "role": "assistant", "content": [], "model": "custom-model", "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 0}}
    events = [
        ("message_start", {"message": start}),
        ("content_block_start", {"index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "考察"}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "signed-opaque-value"}}),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "你"}}),
        ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "好"}}),
        ("content_block_stop", {"index": 1}),
    ]
    if stop:
        events.extend([
            ("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 7}}),
            ("message_stop", {}),
        ])
    return [frame(kind, {"type": kind, **data}) for kind, data in events]


def response_frames(status="completed", stop=True):
    output = [{"type": "message", "id": "msg_test", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "你好", "annotations": []}]}]
    events = [("response.output_text.delta", {"delta": "你", "item_id": "msg_test", "output_index": 0, "content_index": 0}),
              ("response.output_text.delta", {"delta": "好", "item_id": "msg_test", "output_index": 0, "content_index": 0})]
    if stop:
        events.append((f"response.{status}", {"response": {"id": "resp_test", "created_at": 1, "object": "response", "status": status, "model": "custom-model", "output": output, "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}}}))
    return [frame(kind, {"type": kind, "sequence_number": index, **data}) for index, (kind, data) in enumerate(events)]


def chat_frames(reason="stop", stop=True):
    chunks = []
    for text in ("你", "好"):
        chunks.append(frame("message", {"id": "chat_test", "object": "chat.completion.chunk", "created": 1, "model": "custom-model", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}))
    if stop:
        chunks.append(frame("message", {"id": "chat_test", "object": "chat.completion.chunk", "created": 1, "model": "custom-model", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}))
        chunks.append(frame("message", {"id": "chat_test", "object": "chat.completion.chunk", "created": 1, "model": "custom-model", "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}}))
        chunks.append(b"data: [DONE]\n\n")
    return chunks


CASES = [
    ("anthropic-messages", AnthropicProvider, anthropic_frames, "/proxy/v1/messages"),
    ("openai-responses", ResponsesProvider, response_frames, "/proxy/v1/responses"),
    ("openai-chat-completions", ChatCompletionsProvider, chat_frames, "/proxy/v1/chat/completions"),
]


def adapter(protocol, cls, body_factory, status=200):
    requests = []
    bodies = []

    async def handler(request):
        requests.append((request, json.loads(request.content)))
        if status != 200:
            return httpx2.Response(status, json={"error": {"type": "authentication_error", "message": "private-test-key"}})
        body = body_factory()
        bodies.append(body)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk_cls = anthropic.AsyncAnthropic if protocol == "anthropic-messages" else openai.AsyncOpenAI
    base_url = "https://gateway.example/proxy" if protocol == "anthropic-messages" else "https://gateway.example/proxy/v1"
    sdk = sdk_cls(api_key="private-test-key", base_url=base_url, http_client=http, max_retries=0)
    config = ProviderConfig("gateway", protocol, "custom-model", thinking={"type": "adaptive"} if protocol == "anthropic-messages" else None)
    return cls(config, sdk), requests, bodies


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_real_sdk_streams_custom_endpoint_and_reuses_history(protocol, cls, frames, path):
    provider, requests, bodies = adapter(protocol, cls, lambda: SSEBody(frames()))
    try:
        conv = Conversation(provider, "system instruction")
        events = [event async for event in conv.reply("hello")]
        assert [e.text for e in events if isinstance(e, Delta) and e.kind == "text"] == ["你", "好"]
        final = next(e for e in events if isinstance(e, Completed))
        assert isinstance(final, Completed)
        assert final.message.text == "你好"
        assert (final.usage.input_tokens, final.usage.output_tokens) == (12, 7)
        assert bodies[0].closed
        assert await conv.ask("remember") == "你好"
        assert requests[1][0].url.path == path
        payload = requests[1][1]
        assert payload["model"] == "custom-model"
        assert "tools" not in payload
        if protocol == "anthropic-messages":
            assert payload["thinking"] == {"type": "adaptive"}
            signed = payload["messages"][1]["content"][0]
            assert signed["thinking"] == "考察"
            assert signed["signature"] == "signed-opaque-value"
        elif protocol == "openai-responses":
            assert payload["store"] is False
            assert payload["include"] == ["reasoning.encrypted_content"]
            assistant = next(m for m in payload["input"] if m.get("role") == "assistant")
            assert assistant["content"][0]["text"] == "你好"
        else:
            assert next(m for m in payload["messages"] if m["role"] == "assistant")["content"] == "你好"
            assert "stream_options" not in payload
    finally:
        await provider.close()


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_error_sse_is_a_safe_provider_failure(protocol, cls, frames, path):
    error_frame = frame("error", {"type": "error", "error": {"type": "invalid_request_error", "message": "private-test-key"}})
    provider, _, bodies = adapter(protocol, cls, lambda: SSEBody([error_frame]))
    try:
        conv = Conversation(provider)
        with pytest.raises(ProviderError) as error:
            await conv.ask("hello")
        assert "private-test-key" not in str(error.value)
        assert conv.history == ()
        assert bodies[0].closed
    finally:
        await provider.close()


@pytest.mark.parametrize("encrypted", [True, False])
async def test_responses_reasoning_replay_requires_encrypted_content(encrypted):
    chunks = response_frames()
    final = json.loads(chunks[-1].decode().split("data: ", 1)[1])
    reasoning = {"id": "rs_test", "type": "reasoning", "summary": [{"type": "summary_text", "text": "summary"}]}
    if encrypted:
        reasoning["encrypted_content"] = "opaque-signed-reasoning"
    final["response"]["output"].insert(0, reasoning)
    chunks[-1] = frame("response.completed", final)
    provider, requests, _ = adapter("openai-responses", ResponsesProvider, lambda: SSEBody(chunks))
    provider.config = replace(provider.config, include_encrypted_reasoning=encrypted)
    try:
        conv = Conversation(provider)
        await conv.ask("one")
        await conv.ask("two")
        payload = requests[1][1]
        reasoning_items = [item for item in payload["input"] if item.get("type") == "reasoning"]
        if encrypted:
            assert payload["include"] == ["reasoning.encrypted_content"]
            assert reasoning_items[0]["encrypted_content"] == "opaque-signed-reasoning"
        else:
            assert "include" not in payload
            assert not reasoning_items
        assert conv.history[1].text == "你好"
    finally:
        await provider.close()


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_truncated_sse_never_commits_partial_reply(protocol, cls, frames, path):
    provider, _, bodies = adapter(protocol, cls, lambda: SSEBody(frames(stop=False)))
    try:
        conv = Conversation(provider)
        with pytest.raises(ProviderError):
            await conv.ask("hello")
        assert conv.history == ()
        assert bodies[0].closed
    finally:
        await provider.close()


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_cancellation_closes_actual_sdk_http_body(protocol, cls, frames, path):
    provider, _, bodies = adapter(protocol, cls, lambda: SSEBody(frames(stop=False), hold=True))
    try:
        conv = Conversation(provider)
        task = asyncio.create_task(conv.ask("hello"))
        for _ in range(100):
            if bodies:
                break
            await asyncio.sleep(0.01)
        assert bodies
        await asyncio.wait_for(bodies[0].waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bodies[0].closed
        assert conv.history == ()
    finally:
        await provider.close()


@pytest.mark.parametrize("status", [401, 429, 400, 500])
@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_http_error_retry_policy_no_secret_leak(protocol, cls, frames, path, status):
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()), status=status)
    provider.config = replace(provider.config, max_retries=2)
    provider.retry_base_delay = 0
    try:
        events = []
        with pytest.raises(ProviderError) as error:
            async for event in Conversation(provider).reply("hello"):
                events.append(event)
        assert "private-test-key" not in str(error.value)
        assert str(status) in str(error.value)
        transient = status in {429, 500}
        # Transient failures retry up to max_retries; client errors fail at once.
        assert len(requests) == (3 if transient else 1)
        retries = [e for e in events if isinstance(e, Retrying)]
        assert [(e.attempt, e.max_attempts) for e in retries] == ([(1, 2), (2, 2)] if transient else [])
    finally:
        await provider.close()


@pytest.mark.parametrize("protocol,cls,frames,path", CASES)
async def test_output_budget_exhaustion_stops_with_text_preserved(protocol, cls, frames, path):
    def body():
        if protocol == "anthropic-messages":
            return SSEBody(frames(reason="max_tokens"))
        if protocol == "openai-responses":
            chunks = frames(status="incomplete")
            final = json.loads(chunks[-1].decode().split("data: ", 1)[1])
            final["response"]["incomplete_details"] = {"reason": "max_output_tokens"}
            chunks[-1] = frame("response.incomplete", final)
            return SSEBody(chunks)
        return SSEBody(frames(reason="length"))
    provider, requests, _ = adapter(protocol, cls, body)
    try:
        conv = Conversation(provider)
        events = [e async for e in conv.reply("hello")]
        assert next(e for e in events if isinstance(e, Completed)).message.text == "你好"
        stopped = events[-1]
        assert isinstance(stopped, RunStopped) and stopped.reason == "max_tokens" and "max_output_tokens" in stopped.notice
        assert [m.role for m in conv.history] == ["user", "assistant"] and conv.history[1].text == "你好"
        assert len(requests) == 1
    finally:
        await provider.close()


async def test_responses_incomplete_for_other_reasons_stays_an_error():
    chunks = response_frames(status="incomplete")
    final = json.loads(chunks[-1].decode().split("data: ", 1)[1])
    final["response"]["incomplete_details"] = {"reason": "content_filter"}
    chunks[-1] = frame("response.incomplete", final)
    provider, _, _ = adapter("openai-responses", ResponsesProvider, lambda: SSEBody(chunks))
    try:
        conv = Conversation(provider)
        with pytest.raises(ProviderError):
            await conv.ask("hello")
        assert conv.history == ()
    finally:
        await provider.close()
