import asyncio
import json

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import ProviderError
from mosscode.tools import create_default_registry
from test_providers import CASES, SSEBody, adapter, frame
from waiting import until


@pytest.mark.parametrize("protocol,cls,plain_frames,path", CASES)
async def test_actual_sdk_repeated_tool_rounds_and_plan_filter(tmp_path, protocol, cls, plain_frames, path):
    second_tools = [
        b.replace(b"write-id", b"write-second").replace(b"read-id", b"read-second") for b in tool_frames(protocol)
    ]
    batches = iter([tool_frames(protocol), second_tools, plain_frames(), plain_frames()])
    provider, requests, bodies = adapter(protocol, cls, lambda: SSEBody(next(batches)))
    try:
        conv = Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        )
        assert await conv.ask("multi-step") == "你好"
        assert len(requests) == 3 and len(conv.history) == 6
        third = requests[2][1]
        if protocol == "anthropic-messages":
            results = [b["tool_use_id"] for m in third["messages"] for b in m["content"] if b["type"] == "tool_result"]
            assert third["messages"][3]["content"][0]["signature"] == "signed"
        elif protocol == "openai-responses":
            results = [b["call_id"] for b in third["input"] if b.get("type") == "function_call_output"]
        else:
            results = [m["tool_call_id"] for m in third["messages"] if m["role"] == "tool"]
            assistants = [m for m in third["messages"] if m["role"] == "assistant"]
            assert assistants[-1]["reasoning_content"] == "先写再读"
        assert results == ["write-id", "read-id", "write-second", "read-second"]
        conv.set_mode("plan")
        assert await conv.ask("plan") == "你好"
        definitions = requests[-1][1]["tools"]
        names = {d["function"]["name"] if protocol == "openai-chat-completions" else d["name"] for d in definitions}
        plan_tools = {"read_file", "glob", "grep", "execute_command", "exit_plan_mode", "web_fetch"}
        assert names == plan_tools | {"task_output", "task_stop"}  # The task tools of background commands.
        assert all(body.closed for body in bodies)
    finally:
        await provider.close()


def calls(invalid=False):
    path = "sample-中.txt"
    first = '{"path":' if invalid else json.dumps({"path": path, "content": "你好\n"})
    return [("write-id", "write_file", first), ("read-id", "read_file", json.dumps({"path": path}))]


def tool_frames(protocol, invalid=False, stop=True):
    requested = calls(invalid)
    if protocol == "anthropic-messages":
        start = {
            "id": "msg-tools",
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
            (
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            ),
            ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "先写再读"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "signed"}}),
            ("content_block_stop", {"index": 0}),
        ]
        for index, (id_, name, arguments) in enumerate(requested, 1):
            events.append(
                (
                    "content_block_start",
                    {"index": index, "content_block": {"type": "tool_use", "id": id_, "name": name, "input": {}}},
                )
            )
            for piece in [arguments[:7], arguments[7:20], arguments[20:]]:
                events.append(
                    (
                        "content_block_delta",
                        {"index": index, "delta": {"type": "input_json_delta", "partial_json": piece}},
                    )
                )
            events.append(("content_block_stop", {"index": index}))
        if stop:
            events.extend(
                [
                    (
                        "message_delta",
                        {"delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 5}},
                    ),
                    ("message_stop", {}),
                ]
            )
        return [frame(kind, {"type": kind, **data}) for kind, data in events]
    if protocol == "openai-responses":
        events = []
        final_items = []
        for index, (call_id, name, arguments) in enumerate(requested):
            item = {
                "type": "function_call",
                "id": f"item-{index}",
                "call_id": call_id,
                "name": name,
                "arguments": "",
                "status": "in_progress",
            }
            events.append(("response.output_item.added", {"output_index": index, "item": dict(item)}))
            for piece in [arguments[:7], arguments[7:20], arguments[20:]]:
                events.append(
                    (
                        "response.function_call_arguments.delta",
                        {"output_index": index, "item_id": item["id"], "delta": piece},
                    )
                )
            item.update(arguments=arguments, status="completed")
            final_items.append(item)
        if stop:
            events.append(
                (
                    "response.completed",
                    {
                        "response": {
                            "id": "resp-tool",
                            "created_at": 1,
                            "object": "response",
                            "status": "completed",
                            "model": "custom-model",
                            "output": final_items,
                            "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
                        }
                    },
                )
            )
        return [
            frame(kind, {"type": kind, "sequence_number": index, **data}) for index, (kind, data) in enumerate(events)
        ]
    chunks = []

    def chunk(delta, reason=None):
        return frame(
            "message",
            {
                "id": "chat-tools",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "custom-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            },
        )

    # Interleave argument fragments from two calls. Names may be fragmented too.
    for piece_index in range(3):
        for index, (id_, name, arguments) in enumerate(requested):
            pieces = [arguments[:7], arguments[7:20], arguments[20:]]
            function = {"arguments": pieces[piece_index]}
            if piece_index < 2:
                function["name"] = name[:5] if piece_index == 0 else name[5:]
            value = {"index": index, "function": function}
            if piece_index == 0:
                value.update(id=id_, type="function")
            chunks.append(
                chunk({"tool_calls": [value], "reasoning_content": "先写再读" if piece_index == index == 0 else ""})
            )
    if stop:
        chunks.extend([chunk({}, "tool_calls"), b"data: [DONE]\n\n"])
    return chunks


@pytest.mark.parametrize("protocol,cls,plain_frames,path", CASES)
async def test_actual_sdk_tool_schema_stream_execution_and_replay(tmp_path, protocol, cls, plain_frames, path):
    batches = iter([tool_frames(protocol), plain_frames(), plain_frames()])
    provider, requests, bodies = adapter(protocol, cls, lambda: SSEBody(next(batches)))
    try:
        conv = Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        )
        assert await conv.ask("write then read") == "你好"
        assert len(requests) == 2
        assert (tmp_path / "sample-中.txt").read_text(encoding="utf-8") == "你好\n"
        assert len(requests[0][1]["tools"]) == 9
        second = requests[1][1]
        if protocol == "anthropic-messages":
            assert second.get("tool_choice") != {"type": "none"}
            assert second["messages"][1]["content"][0]["signature"] == "signed"
            results = second["messages"][2]["content"]
            assert [r["tool_use_id"] for r in results] == ["write-id", "read-id"]
            assert results[1]["content"].startswith("     1\t你好")
        elif protocol == "openai-responses":
            assert second.get("tool_choice") != "none"
            results = [item for item in second["input"] if item.get("type") == "function_call_output"]
            assert [r["call_id"] for r in results] == ["write-id", "read-id"]
        else:
            assert second.get("tool_choice") != "none"
            assistant = next(item for item in second["messages"] if item["role"] == "assistant")
            assert assistant["reasoning_content"] == "先写再读"
            results = [item for item in second["messages"] if item["role"] == "tool"]
            assert [r["tool_call_id"] for r in results] == ["write-id", "read-id"]
        assert all(body.closed for body in bodies)
        assert await conv.ask("next") == "你好"
        assert len(requests) == 3
        assert all(not result.is_error for result in conv.history[2].tool_results)
    finally:
        await provider.close()


@pytest.mark.parametrize("protocol,cls,plain_frames,path", CASES)
async def test_malformed_json_is_tool_error_not_empty_parameters(tmp_path, protocol, cls, plain_frames, path):
    batches = iter([tool_frames(protocol, invalid=True), plain_frames()])
    provider, _, _ = adapter(protocol, cls, lambda: SSEBody(next(batches)))
    try:
        conv = Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        )
        assert await conv.ask("bad") == "你好"
        assert [r.code for r in conv.history[2].tool_results] == ["invalid_json", "not_found"]
        assert not list(tmp_path.iterdir())
    finally:
        await provider.close()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("protocol,cls,plain_frames,path", CASES)
async def test_incomplete_tool_response_has_no_side_effects(tmp_path, protocol, cls, plain_frames, path, cancel):
    provider, _, bodies = adapter(protocol, cls, lambda: SSEBody(tool_frames(protocol, stop=False), hold=cancel))
    try:
        conv = Conversation(
            provider,
            registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
        )
        task = asyncio.create_task(conv.ask("write"))
        if cancel:
            await until(lambda: bodies)
            await asyncio.wait_for(bodies[0].waiting.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderError):
                await task
        assert conv.history == ()
        assert not list(tmp_path.iterdir())
        assert bodies[0].closed
    finally:
        await provider.close()
