import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest

from mosscode.config import ProviderConfig
from mosscode.conversation import Conversation
from mosscode.messages import (
    Completed,
    ContentBlock,
    Delta,
    Message,
    ProviderError,
    Retrying,
    RunStopped,
    ToolCall,
    ToolResult,
)
from mosscode.providers import AnthropicProvider, ChatCompletionsProvider, ResponsesProvider
from waiting import until


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
    start = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "custom-model",
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 0},
    }
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
        events.extend(
            [
                (
                    "message_delta",
                    {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 7}},
                ),
                ("message_stop", {}),
            ]
        )
    return [frame(kind, {"type": kind, **data}) for kind, data in events]


def response_frames(status="completed", stop=True):
    output = [
        {
            "type": "message",
            "id": "msg_test",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "你好", "annotations": []}],
        }
    ]
    events = [
        ("response.output_text.delta", {"delta": "你", "item_id": "msg_test", "output_index": 0, "content_index": 0}),
        ("response.output_text.delta", {"delta": "好", "item_id": "msg_test", "output_index": 0, "content_index": 0}),
    ]
    if stop:
        events.append(
            (
                f"response.{status}",
                {
                    "response": {
                        "id": "resp_test",
                        "created_at": 1,
                        "object": "response",
                        "status": status,
                        "model": "custom-model",
                        "output": output,
                        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
                    }
                },
            )
        )
    return [frame(kind, {"type": kind, "sequence_number": index, **data}) for index, (kind, data) in enumerate(events)]


def chat_frames(reason="stop", stop=True):
    chunks = []
    for text in ("你", "好"):
        chunks.append(
            frame(
                "message",
                {
                    "id": "chat_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "custom-model",
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                },
            )
        )
    if stop:
        chunks.append(
            frame(
                "message",
                {
                    "id": "chat_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "custom-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                },
            )
        )
        chunks.append(
            frame(
                "message",
                {
                    "id": "chat_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "custom-model",
                    "choices": [],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
                },
            )
        )
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
            return httpx2.Response(
                status, json={"error": {"type": "authentication_error", "message": "private-test-key"}}
            )
        body = body_factory()
        bodies.append(body)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk_cls = anthropic.AsyncAnthropic if protocol == "anthropic-messages" else openai.AsyncOpenAI
    base_url = (
        "https://gateway.example/proxy" if protocol == "anthropic-messages" else "https://gateway.example/proxy/v1"
    )
    sdk = sdk_cls(api_key="private-test-key", base_url=base_url, http_client=http, max_retries=0)
    config = ProviderConfig(
        "gateway", protocol, "custom-model", thinking={"type": "adaptive"} if protocol == "anthropic-messages" else None
    )
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
    error_frame = frame(
        "error", {"type": "error", "error": {"type": "invalid_request_error", "message": "private-test-key"}}
    )
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
        await until(lambda: bodies)
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
async def test_output_budget_exhaustion_continues_then_stops_with_text_preserved(protocol, cls, frames, path):
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
        assert (
            isinstance(stopped, RunStopped) and stopped.reason == "max_tokens" and "已自动续写 3 次" in stopped.notice
        )
        # Every cut piece is kept, each followed by the request to go on; the service accepts that history.
        assert [m.role for m in conv.history] == ["user", "assistant"] * 4
        assert [m.synthetic for m in conv.history if m.role == "user"] == [None, "continue", "continue", "continue"]
        assert [m.text for m in conv.history if m.role == "assistant"] == ["你好"] * 4
        assert len(requests) == 4
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


# Images ---------------------------------------------------------------------------------------


class Captured(Exception):
    pass


def picture(color="red", alt="[图片 #1]"):
    import io

    from PIL import Image

    from mosscode.images import image_ref, load_image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 2), color).save(buffer, "PNG")
    return image_ref(load_image(buffer.getvalue()), alt)


async def request(cls, protocol, history, **options):
    seen = []

    async def create(**kwargs):
        seen.append(kwargs)
        raise Captured()

    endpoint = SimpleNamespace(create=create)
    client = SimpleNamespace(messages=endpoint, responses=endpoint, chat=SimpleNamespace(completions=endpoint))
    client.base_url = ""
    provider = options.pop("provider", None) or cls(
        ProviderConfig("p", protocol, "m", max_retries=0, **options), client
    )
    if provider.client is not client:
        provider.client = client
    with pytest.raises(Captured):
        async for _ in provider.stream(history, "系统"):
            pass
    return seen[0]


def image_history(*results):
    calls = tuple(ToolCall(r.call_id, r.name, "{}") for r in results)
    user = Message("user", (ContentBlock("text", "看 [图片 #1] 这里"), ContentBlock("image", "[图片 #1]", picture())))
    return [user, Message("assistant", (), tool_calls=calls), Message("tool", (), tool_results=results)]


def data_of(ref):
    return ref.base64()


async def test_anthropic_images_in_user_messages_and_tool_results():
    shot, failed = picture("blue", "a.png"), picture("green", "b.png")
    history = image_history(
        ToolResult("c1", "read_file", "a.png · 4×2", images=(shot,)),
        ToolResult("c2", "mcp__x__shot", "", images=(shot,)),
        ToolResult("c3", "mcp__x__shot", "截图失败", True, "mcp_error", images=(failed,)),
    )
    turns = (await request(AnthropicProvider, "anthropic-messages", history))["messages"]
    user = turns[0]["content"]
    assert [b["type"] for b in user] == ["text", "image"] and user[0]["text"] == "看 [图片 #1] 这里"
    source = {"type": "base64", "media_type": "image/png", "data": data_of(history[0].content[1].image)}
    assert user[1]["source"] == source
    blocks = turns[2]["content"]
    assert [b["type"] for b in blocks] == ["tool_result"] * 3 + ["text", "text", "image"]
    assert blocks[0]["content"][0] == {"type": "text", "text": "a.png · 4×2"}
    assert blocks[0]["content"][1]["source"]["data"] == data_of(shot)
    assert blocks[1]["content"][0]["text"] == "(see attached image)"
    assert blocks[2]["content"] == "错误 [mcp_error]：截图失败" and blocks[2]["is_error"]
    assert [b.get("text") for b in blocks[3:5]] == ["出错的工具结果中的图片：", "b.png"]
    assert blocks[5]["source"]["data"] == data_of(failed)


async def test_responses_images_are_input_images_and_tool_outputs_become_arrays():
    shot = picture("blue", "a.png")
    history = image_history(ToolResult("c1", "read_file", "", images=(shot,)), ToolResult("c2", "glob", "a.png"))
    items = (await request(ResponsesProvider, "openai-responses", history))["input"]
    user = next(i for i in items if i.get("role") == "user")
    url = f"data:image/png;base64,{data_of(history[0].content[1].image)}"
    assert user["content"] == [
        {"type": "input_text", "text": "看 [图片 #1] 这里"},
        {"type": "input_image", "image_url": url},
    ]
    outputs = [i for i in items if i.get("type") == "function_call_output"]
    assert outputs[0]["output"] == [
        {"type": "input_text", "text": "(see attached image)"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{data_of(shot)}"},
    ]
    assert outputs[1]["output"] == "a.png"


async def test_chat_tool_images_follow_the_tool_messages_as_one_user_message():
    first, second = picture("blue", "a.png"), picture("green", "b.png")
    history = image_history(
        ToolResult("c1", "read_file", "a.png", images=(first,)), ToolResult("c2", "read_file", "", images=(second,))
    )
    messages = (await request(ChatCompletionsProvider, "openai-chat-completions", history, images=True))["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert messages[1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{data_of(history[0].content[1].image)}"},
    }
    assert [m["content"] for m in messages[3:5]] == ["a.png", "(see attached image)"]
    extra = messages[5]["content"]
    assert [p.get("text") for p in extra if p["type"] == "text"] == ["工具结果中的图片：", "a.png", "b.png"]
    assert [p["image_url"]["url"] for p in extra if p["type"] == "image_url"] == [
        f"data:image/png;base64,{data_of(first)}",
        f"data:image/png;base64,{data_of(second)}",
    ]


@pytest.mark.parametrize(
    "cls,protocol,options",
    [
        (ChatCompletionsProvider, "openai-chat-completions", {}),
        (AnthropicProvider, "anthropic-messages", {"images": False}),
        (ResponsesProvider, "openai-responses", {"images": False}),
    ],
)
async def test_models_without_image_input_get_alt_text_instead(cls, protocol, options):
    history = image_history(ToolResult("c1", "read_file", "a.png", images=(picture("blue", "a.png"),)))
    body = json.dumps(await request(cls, protocol, history, **options), ensure_ascii=False)
    assert "base64" not in body and "image_url" not in body and '"image"' not in body
    assert "[图片 #1]（当前模型不支持图片，已省略）" in body
    assert "a.png\\na.png（当前模型不支持图片，已省略）" in body


async def test_only_the_latest_twenty_images_are_sent():
    refs = [picture((i, 0, 0), f"{i}.png") for i in range(21)]  # With the user's image, 22 in all.
    results = tuple(ToolResult(f"c{i}", "read_file", f"{i}.png", images=(ref,)) for i, ref in enumerate(refs))
    turns = (await request(AnthropicProvider, "anthropic-messages", image_history(*results)))["messages"]
    body = json.dumps(turns, ensure_ascii=False)
    assert body.count('"type": "image"') == 20
    assert "[图片 #1]（较早的图片已省略）" in body and "0.png\\n0.png（较早的图片已省略）" in body
    assert "1.png（较早的图片已省略）" not in body and data_of(refs[1]) in body


async def test_images_are_encoded_once_and_a_lost_file_becomes_text(tmp_path):
    from mosscode.images import ImageRef

    ref = picture("blue", "a.png")
    path = tmp_path / ref.name
    path.write_bytes(ref.read())
    stored = ImageRef(ref.name, ref.media_type, ref.width, ref.height, ref.size, "a.png", path=path)
    gone = ImageRef("0" * 32 + ".png", "image/png", 4, 2, 10, "b.png", path=tmp_path / "missing.png")
    history = image_history(ToolResult("c1", "read_file", "a.png", images=(stored,)))
    provider = AnthropicProvider(ProviderConfig("p", "anthropic-messages", "m", max_retries=0), SimpleNamespace())
    await request(AnthropicProvider, "anthropic-messages", history, provider=provider)
    path.unlink()  # Already encoded: the next request needs no file.
    history[2] = Message("tool", (), tool_results=(ToolResult("c1", "read_file", "a.png", images=(stored, gone)),))
    turns = (await request(AnthropicProvider, "anthropic-messages", history, provider=provider))["messages"]
    content = turns[2]["content"][0]["content"]
    assert content[0]["text"] == "a.png\n[图片已丢失：b.png]" and content[1]["source"]["data"] == data_of(ref)
