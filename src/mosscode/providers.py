"""Three wire protocols, arbitrary compatible endpoints, no tool execution."""

import asyncio
import json
import random
import sys
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from typing import Any

from .config import ProviderConfig
from .images import ImageRef
from .messages import (
    Completed,
    ContentBlock,
    Delta,
    Message,
    ProviderError,
    Retrying,
    StreamEvent,
    ToolCall,
    ToolDefinition,
    ToolProgress,
    Usage,
)
from .prompts import PREFIX_KINDS, PromptRequest, as_prompt
from .usage import parse_usage, usage_data

# Anthropic tightens its per-image limits above 20 images a request, and old screenshots rarely still matter.
IMAGE_LIMIT = 20
ENCODED_CACHE = 32
UNSUPPORTED = "当前模型不支持图片，已省略"
EARLIER = "较早的图片已省略"
IMAGE_ONLY = "(see attached image)"
TOOL_IMAGES = "工具结果中的图片："
ERROR_IMAGES = "出错的工具结果中的图片："
Encode = Callable[[ImageRef], str]


def _has_images(messages: Sequence[Message]) -> bool:
    return any(b.image is not None for m in messages for b in m.content) or any(
        r.images for m in messages for r in m.tool_results
    )


def _image(ref: ImageRef, protocol: str, encode: Encode) -> dict[str, Any]:
    data = encode(ref)
    if protocol == "anthropic-messages":
        return {"type": "image", "source": {"type": "base64", "media_type": ref.media_type, "data": data}}
    url = f"data:{ref.media_type};base64,{data}"
    if protocol == "openai-responses":
        return {"type": "input_image", "image_url": url}
    return {"type": "image_url", "image_url": {"url": url}}


def _text(text: str, protocol: str) -> dict[str, Any]:
    return {"type": "input_text" if protocol == "openai-responses" else "text", "text": text}


def _labelled(header: str, refs: Sequence[ImageRef], protocol: str, encode: Encode) -> list[dict[str, Any]]:
    """Images outside their tool result, each after its alt text so the model can tell them apart."""
    parts = [_text(header, protocol)]
    for ref in refs:
        parts.extend((_text(ref.alt, protocol), _image(ref, protocol, encode)))
    return parts


def _parts(message: Message, protocol: str, encode: Encode) -> list[dict[str, Any]]:
    """Text and images in their original order; adjacent text joins into one part."""
    parts, text = [], ""
    for block in message.content:
        if block.image is not None:
            if text:
                parts.append(_text(text, protocol))
            parts.append(_image(block.image, protocol, encode))
            text = ""
        elif block.kind == "text":
            text += block.text
    if text:
        parts.append(_text(text, protocol))
    return parts


def _with_images(messages: Sequence[Message], accepts: bool, encode: Encode) -> list[Message]:
    """Images the request carries, already encoded; the rest become their alt text with the reason."""
    total = sum(1 for m in messages for b in m.content if b.image is not None)
    total += sum(len(r.images) for m in messages for r in m.tool_results)
    dropped = max(total - IMAGE_LIMIT, 0) if accepts else total
    seen = 0

    def note(ref: ImageRef) -> str | None:
        nonlocal seen
        seen += 1
        if seen <= dropped:
            return ref.placeholder(EARLIER if accepts else UNSUPPORTED)
        try:
            encode(ref)
        except OSError:
            return f"[图片已丢失：{ref.alt}]"
        return None

    prepared = []
    for message in messages:
        content = []
        for block in message.content:
            text = None if block.image is None else note(block.image)
            content.append(block if text is None else ContentBlock("text", text))
        results = []
        for result in message.tool_results:
            kept, notes = [], []
            for ref in result.images:
                text = note(ref)
                if text is None:
                    kept.append(ref)
                else:
                    notes.append(text)
            if notes:
                output = "\n".join([result.output, *notes] if result.output else notes)
                result = replace(result, output=output, images=tuple(kept))
            results.append(result)
        prepared.append(replace(message, content=tuple(content), tool_results=tuple(results)))
    return prepared


def _blocks(message: Message, protocol: str, encode: Encode) -> list[dict[str, Any]]:
    if message.role == "tool":
        blocks, failed = [], []
        for r in message.tool_results:
            content: Any = r.model_text()
            if r.images and not r.is_error:
                content = [{"type": "text", "text": content or IMAGE_ONLY}]
                content.extend(_image(ref, protocol, encode) for ref in r.images)
            failed.extend(r.images if r.is_error else ())
            blocks.append({"type": "tool_result", "tool_use_id": r.call_id, "content": content, "is_error": r.is_error})
        # Anthropic refuses images inside an error result: they follow all the results instead.
        return blocks + (_labelled(ERROR_IMAGES, failed, protocol, encode) if failed else [])
    if message.protocol == protocol and message.native:
        return list(deepcopy(message.native))
    if any(b.image is not None for b in message.content):
        return _parts(message, protocol, encode)
    # Portable form, e.g. after switching models: text plus rebuilt tool_use blocks.
    blocks = [{"type": "text", "text": message.text}] if message.text or not message.tool_calls else []
    blocks.extend(
        {"type": "tool_use", "id": c.id, "name": c.name, "input": _arguments(c.arguments)} for c in message.tool_calls
    )
    return blocks


def _arguments(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _turns(messages: Sequence[Message], protocol: str, encode: Encode) -> list[dict[str, Any]]:
    """Anthropic turns; adjacent same-role messages (tool results then queued text) merge."""
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = "user" if message.role == "tool" else message.role
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(_blocks(message, protocol, encode))
        else:
            turns.append({"role": role, "content": _blocks(message, protocol, encode)})
    return turns


def _tools(definitions: Sequence[ToolDefinition], protocol: str) -> list[dict[str, Any]]:
    if protocol == "anthropic-messages":
        return [
            {"name": d.name, "description": d.description, "input_schema": deepcopy(d.parameters)} for d in definitions
        ]
    functions = [
        {
            "type": "function",
            "name": d.name,
            "description": d.description,
            "parameters": deepcopy(d.parameters),
            "strict": False,
        }
        for d in definitions
    ]
    if protocol == "openai-chat-completions":
        return [{"type": "function", "function": {k: v for k, v in f.items() if k != "type"}} for f in functions]
    return functions


def _valid_calls(calls: Sequence[ToolCall]) -> None:
    ids = [call.id for call in calls]
    if any(not c.id or not c.name for c in calls) or len(set(ids)) != len(ids):
        raise ProviderError("工具调用缺少名称/ID 或出现重复 ID；未执行工具。")


def _errors(name: str) -> tuple[type, ...]:
    """The SDKs load only with a provider that needs them, and one never loaded raised nothing."""
    return tuple(getattr(sys.modules[sdk], name) for sdk in ("anthropic", "openai") if sdk in sys.modules)


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
RETRYABLE_BODY_TYPES = {"overloaded_error", "api_error", "rate_limit_error"}


def _body_type(exc) -> str | None:
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else None
    return error.get("type") if isinstance(error, dict) else None


def _retry_reason(exc) -> str | None:
    if isinstance(exc, _errors("APITimeoutError")):
        return "请求超时"
    if isinstance(exc, _errors("APIConnectionError")):
        return "连接中断"
    if isinstance(exc, _errors("APIStatusError")):
        if exc.status_code in RETRYABLE_STATUS or exc.status_code >= 500:
            return "服务限流" if exc.status_code == 429 else f"服务暂时不可用（HTTP {exc.status_code}）"
    if isinstance(exc, _errors("APIError")) and _body_type(exc) in RETRYABLE_BODY_TYPES:
        return "服务过载" if _body_type(exc) == "overloaded_error" else "服务流返回临时错误"
    return None


def _retry_delay(exc, attempt: int, base: float) -> float:
    response = getattr(exc, "response", None)
    try:
        advised = float(response.headers.get("retry-after")) if response is not None else None
    except (TypeError, ValueError):
        advised = None
    if advised is not None and advised >= 0:
        return min(advised, 60.0)
    return min(base * 2**attempt, 60.0) * (1 + random.random() * 0.25)


OVERFLOW_PATTERNS = (
    "prompt is too long",
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "too many tokens",
    "input is too long",
    "exceeds the context",
    "reduce the length",
    "input too long",
)


def _context_overflow(exc) -> bool:
    """Services word this differently; match the shared phrases, never echo the raw text."""
    status = getattr(exc, "status_code", None)
    if status not in {None, 400, 413, 422}:
        return False
    body = getattr(exc, "body", None)
    text = (str(exc) + " " + (json.dumps(body, ensure_ascii=False, default=str) if body is not None else "")).lower()
    return any(pattern in text for pattern in OVERFLOW_PATTERNS)


def _safe_error(exc) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, _errors("APIError")) and _context_overflow(exc):
        status = getattr(exc, "status_code", None)
        suffix = f"（HTTP {status}）" if status else ""
        return ProviderError(f"请求超出模型的上下文窗口{suffix}。", code="context_overflow")
    if isinstance(exc, _errors("APIStatusError")):
        status = exc.status_code
        if status in {401, 403}:
            detail = "认证或权限失败，请检查凭据与端点。"
        elif status == 429:
            detail = "服务限流，请稍后重新发送。"
        elif status in {400, 404, 422}:
            detail = "请求不被服务接受，请检查协议、端点、模型与可选能力配置。"
        else:
            detail = "服务请求失败，请稍后重新发送。"
        return ProviderError(f"{detail}（HTTP {status}）")
    if isinstance(exc, _errors("APITimeoutError")):
        return ProviderError("请求超时，请检查网络或增大 timeout。")
    if isinstance(exc, _errors("APIConnectionError")):
        return ProviderError("连接中断或无法连接，请检查网络与 base_url。")
    if isinstance(exc, _errors("APIError")):
        return ProviderError("服务流返回错误或无法解析的响应，请检查协议和服务配置。")
    if isinstance(exc, (KeyError, TypeError, ValueError, AttributeError)):
        return ProviderError("服务流数据格式不合法；未执行不完整的工具调用。")
    raise exc


class SDKProvider:
    def __init__(self, config: ProviderConfig, client: Any = None) -> None:
        self.config = config
        self.client = client if client is not None else self._client()
        # base64 by image name (a content hash): history images are sent again every turn.
        self._encoded: dict[str, str] = {}
        self._encoded_lock = threading.Lock()

    def _encode(self, ref: ImageRef) -> str:
        with self._encoded_lock:
            data = self._encoded.pop(ref.name, None)
        if data is None:
            data = ref.base64()
        with self._encoded_lock:
            self._encoded[ref.name] = data
            while len(self._encoded) > ENCODED_CACHE:
                del self._encoded[next(iter(self._encoded))]
        return data

    def _client(self) -> Any:
        config = self.config
        options: dict[str, Any] = {"timeout": config.timeout, "max_retries": 0}
        if config.base_url:
            options["base_url"] = config.base_url
        if config.headers:
            options["default_headers"] = config.headers
        key = config.credential()
        if config.protocol == "anthropic-messages":
            import anthropic

            if config.auth_type == "bearer":
                return anthropic.AsyncAnthropic(api_key=None, auth_token=key, **options)
            return anthropic.AsyncAnthropic(api_key=key, **options)
        import openai

        return openai.AsyncOpenAI(api_key=key, **options)

    async def close(self) -> None:
        await self.client.close()

    retry_base_delay = 1.0

    async def stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        if _has_images(messages):
            # Reading and encoding image files stays off the event loop.
            messages = await asyncio.to_thread(_with_images, messages, self.config.accepts_images, self._encode)
        attempt = 0
        while True:
            produced = False
            retry = None
            try:
                async with aclosing(self._stream(messages, system, tools, allow_tools=allow_tools)) as events:
                    async for event in events:
                        produced = True
                        yield event
                return
            except Exception as exc:
                # Only a request that produced nothing is safe to repeat: no text
                # was shown and no tool call can have been executed.
                retry = None if produced or attempt >= self.config.max_retries else _retry_reason(exc)
                if retry is None:
                    raise _safe_error(exc) from None
                delay = _retry_delay(exc, attempt, self.retry_base_delay)
            attempt += 1
            yield Retrying(attempt, self.config.max_retries, delay, retry)
            await asyncio.sleep(delay)

    async def _stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover


class AnthropicProvider(SDKProvider):
    async def _stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        config = self.config
        prompt = as_prompt(system)
        options: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_output_tokens,
            "messages": _turns(messages, config.protocol, self._encode),
            "stream": True,
        }
        blocks = [{"type": "text", "text": prompt.stable}] if prompt.stable else []
        official = str(getattr(self.client, "base_url", "")).rstrip("/") == "https://api.anthropic.com"
        cache = config.prompt_cache if config.prompt_cache is not None else official
        if blocks and cache:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        # Environment and active skills are stable within a run and belong to the cached prefix;
        # reminders are request-only and follow the last history breakpoint.
        blocks.extend(
            {"type": "text", "text": item.render()} for item in prompt.supplements if item.kind in PREFIX_KINDS
        )
        reminders = [
            {"type": "text", "text": item.render()} for item in prompt.supplements if item.kind not in PREFIX_KINDS
        ]
        turns = options["messages"]
        if turns and cache:
            turns[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        if turns and turns[-1]["role"] == "user":
            turns[-1]["content"].extend(reminders)
        else:
            blocks.extend(reminders)
        if blocks:
            options["system"] = blocks
        if tools:
            options["tools"] = _tools(tools, config.protocol)
            if not allow_tools:
                options["tool_choice"] = {"type": "none"}
        if config.thinking is not None:
            options["thinking"] = {k: v for k, v in config.thinking.items() if k != "effort"}
            if "effort" in config.thinking:
                options["output_config"] = {"effort": config.thinking["effort"]}
        ended = False
        raw_blocks: dict[int, dict[str, Any]] = {}
        arguments: dict[int, str] = {}
        reason = None
        usage_fields = {}
        stream = await self.client.messages.create(**options)
        async with stream:
            async for event in stream:
                if event.type == "message_start":
                    usage_fields = usage_data(event.message.usage)
                elif event.type == "content_block_start":
                    raw_blocks[event.index] = event.content_block.model_dump(exclude_none=True)
                    block = raw_blocks[event.index]
                    if block["type"] == "tool_use":
                        arguments[event.index] = ""
                        yield ToolProgress(ToolCall(block["id"], block["name"], ""), "parsing")
                elif event.type == "content_block_delta":
                    block = raw_blocks[event.index]
                    if event.delta.type == "text_delta":
                        block["text"] = block.get("text", "") + event.delta.text
                        yield Delta("text", event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + event.delta.thinking
                        yield Delta("thinking", event.delta.thinking)
                    elif event.delta.type == "signature_delta":
                        block["signature"] = block.get("signature", "") + event.delta.signature
                    elif event.delta.type == "input_json_delta":
                        arguments[event.index] += event.delta.partial_json
                elif event.type == "message_delta":
                    reason = event.delta.stop_reason
                    # Fields are cumulative snapshots; missing delta fields retain start values.
                    usage_fields.update({k: v for k, v in usage_data(event.usage).items() if v is not None})
                elif event.type == "message_stop":
                    ended = True
                elif event.type == "error":
                    raise ProviderError("Anthropic 服务流返回错误。")
            if not ended:
                raise ProviderError("Anthropic 流在完成信号前中断，请重新发送。")
            if reason not in {"end_turn", "stop_sequence", "refusal", "tool_use", "max_tokens"}:
                raise ProviderError("回复未正常完成（出现本章不支持的响应）。")
            if reason == "max_tokens":
                # A thinking block cut before its signature cannot be replayed.
                raw_blocks = {i: b for i, b in raw_blocks.items() if b["type"] != "thinking" or b.get("signature")}
            calls = []
            for index, block in raw_blocks.items():
                if block["type"] == "tool_use":
                    args = arguments.get(index) or json.dumps(block.get("input", {}), ensure_ascii=False)
                    calls.append(ToolCall(block["id"], block["name"], args))
                    try:
                        parsed = json.loads(args)
                        block["input"] = parsed if isinstance(parsed, dict) else {}
                    except ValueError:
                        # Only for valid protocol replay; the registry still gets
                        # the malformed original string and returns invalid_json.
                        block["input"] = {}
            _valid_calls(calls)
            raw = tuple(raw_blocks.values())
            if any(block["type"] not in {"text", "thinking", "redacted_thinking", "tool_use"} for block in raw):
                raise ProviderError("服务返回了本章不支持的内容。")
            content = tuple(
                ContentBlock("text" if b["type"] == "text" else "thinking", b.get("text", b.get("thinking", "")))
                for b in raw
                if b["type"] in {"text", "thinking"}
            )
            yield Completed(
                Message("assistant", content, config.protocol, raw, tuple(calls)),
                reason,
                parse_usage(usage_fields, config.protocol),
            )


def _incomplete_reason(response) -> str | None:
    details = getattr(response, "incomplete_details", None)
    return getattr(details, "reason", None)


class ResponsesProvider(SDKProvider):
    async def _stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        prompt = as_prompt(system)
        inputs: list[dict[str, Any]] = [
            {"role": "developer", "content": s.render()} for s in prompt.supplements if s.kind in PREFIX_KINDS
        ]
        for message in messages:
            if message.role == "tool":
                for r in message.tool_results:
                    output: Any = r.model_text()
                    if r.images:
                        output = [_text(output or IMAGE_ONLY, self.config.protocol)]
                        output.extend(_image(ref, self.config.protocol, self._encode) for ref in r.images)
                    inputs.append({"type": "function_call_output", "call_id": r.call_id, "output": output})
            elif message.role == "assistant" and message.protocol == self.config.protocol and message.native:
                inputs.extend(deepcopy(message.native))
            else:
                if any(b.image is not None for b in message.content):
                    content = _parts(message, self.config.protocol, self._encode)
                    inputs.append({"role": message.role, "content": content})
                elif message.text or not message.tool_calls:
                    inputs.append({"role": message.role, "content": message.text})
                inputs.extend(
                    {"type": "function_call", "call_id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in message.tool_calls
                )
        inputs.extend(
            {"role": "developer", "content": s.render()} for s in prompt.supplements if s.kind not in PREFIX_KINDS
        )
        options: dict[str, Any] = {
            "model": self.config.model,
            "input": inputs,
            "store": False,
            "max_output_tokens": self.config.max_output_tokens,
            "stream": True,
        }
        if prompt.stable:
            options["instructions"] = prompt.stable
        if tools:
            options["tools"] = _tools(tools, self.config.protocol)
            if not allow_tools:
                options["tool_choice"] = "none"
        if self.config.include_encrypted_reasoning:
            options["include"] = ["reasoning.encrypted_content"]
        arguments: dict[str, str] = {}
        final = None
        stream = await self.client.responses.create(**options)
        async with stream:
            async for event in stream:
                if event.type in {"response.output_text.delta", "response.refusal.delta"}:
                    yield Delta("text", event.delta)
                elif event.type == "response.reasoning_summary_text.delta":
                    yield Delta("thinking", event.delta)
                elif event.type == "response.output_item.added" and event.item.type == "function_call":
                    arguments[event.item.id] = event.item.arguments or ""
                    yield ToolProgress(ToolCall(event.item.call_id, event.item.name, event.item.arguments), "parsing")
                elif event.type == "response.function_call_arguments.delta":
                    arguments[event.item_id] = arguments.get(event.item_id, "") + event.delta
                elif event.type == "response.incomplete" and _incomplete_reason(event.response) == "max_output_tokens":
                    final, stop = event.response, "max_tokens"
                elif event.type in {"error", "response.failed", "response.incomplete"}:
                    raise ProviderError("Responses 请求失败或回复不完整，请检查服务与输出预算。")
                elif event.type == "response.completed":
                    final, stop = event.response, "completed"
                    if final.status != "completed":
                        raise ProviderError("Responses 回复未完成。")
                if final is not None:
                    raw = tuple(item.model_dump(exclude_none=True) for item in final.output)
                    content: list[ContentBlock] = []
                    calls = []
                    for item in raw:
                        if item["type"] == "message":
                            for block in item["content"]:
                                if block["type"] == "output_text":
                                    content.append(ContentBlock("text", block["text"]))
                                elif block["type"] == "refusal":
                                    content.append(ContentBlock("text", block["refusal"]))
                                else:
                                    raise ProviderError("服务返回了本章不支持的内容。")
                        elif item["type"] == "reasoning":
                            content.extend(ContentBlock("thinking", b["text"]) for b in item.get("summary", []))
                        elif item["type"] == "function_call":
                            streamed = arguments.get(item.get("id"))
                            if streamed:
                                if item["arguments"] and item["arguments"] != streamed:
                                    raise ProviderError("工具参数分片与最终响应不一致；未执行工具。")
                                item["arguments"] = streamed
                            calls.append(ToolCall(item["call_id"], item["name"], item["arguments"]))
                        else:
                            raise ProviderError("服务返回了本章不支持的内容。")
                    usage = parse_usage(final.usage, self.config.protocol)
                    # Stateless replay needs encrypted data, not a bare reasoning ID.
                    retained = tuple(
                        item for item in raw if item["type"] != "reasoning" or item.get("encrypted_content")
                    )
                    _valid_calls(calls)
                    yield Completed(
                        Message("assistant", tuple(content), self.config.protocol, retained, tuple(calls)), stop, usage
                    )
                    return
        raise ProviderError("Responses 流在完成信号前中断，请重新发送。")


class ChatCompletionsProvider(SDKProvider):
    async def _stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        prompt = as_prompt(system)
        history = [{"role": "system", "content": s.render()} for s in prompt.supplements if s.kind in PREFIX_KINDS]
        for message in messages:
            if message.role == "tool":
                history.extend(
                    {
                        "role": "tool",
                        "tool_call_id": r.call_id,
                        "content": r.model_text() or IMAGE_ONLY * bool(r.images),
                    }
                    for r in message.tool_results
                )
                # A tool message carries text only: the batch's images follow as one user message.
                images = [ref for r in message.tool_results for ref in r.images]
                if images:
                    history.append(
                        {"role": "user", "content": _labelled(TOOL_IMAGES, images, self.config.protocol, self._encode)}
                    )
            elif message.protocol == self.config.protocol and message.native:
                history.extend(deepcopy(message.native))
            else:
                content: Any = message.text
                if any(b.image is not None for b in message.content):
                    content = _parts(message, self.config.protocol, self._encode)
                entry: dict[str, Any] = {"role": message.role, "content": content}
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                        for c in message.tool_calls
                    ]
                history.append(entry)
        if prompt.stable:
            history.insert(0, {"role": "system", "content": prompt.stable})
        history.extend(
            {"role": "system", "content": s.render()} for s in prompt.supplements if s.kind not in PREFIX_KINDS
        )
        options: dict[str, Any] = {
            "model": self.config.model,
            "messages": history,
            self.config.max_tokens_field: self.config.max_output_tokens,
            "stream": True,
        }
        if self.config.stream_usage:
            options["stream_options"] = {"include_usage": True}
        if tools:
            options["tools"] = _tools(tools, self.config.protocol)
            if not allow_tools:
                options["tool_choice"] = "none"
        stream = await self.client.chat.completions.create(**options)
        text: list[str] = []
        thinking: list[str] = []
        reason = None
        usage = Usage()
        fragments: dict[int, dict[str, str]] = {}
        async with stream:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = parse_usage(chunk.usage, self.config.protocol)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if getattr(choice.delta, "function_call", None):
                    raise ProviderError("不支持缺少调用 ID 的旧式 function_call。")
                for fragment in choice.delta.tool_calls or ():
                    first = fragment.index not in fragments
                    call = fragments.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                    if fragment.id:
                        call["id"] += fragment.id
                    if fragment.function:
                        call["name"] += fragment.function.name or ""
                        call["arguments"] += fragment.function.arguments or ""
                    if first:
                        yield ToolProgress(ToolCall(call["id"], call["name"], call["arguments"]), "parsing")
                if choice.delta.content:
                    text.append(choice.delta.content)
                    yield Delta("text", choice.delta.content)
                if getattr(choice.delta, "refusal", None):
                    text.append(choice.delta.refusal)
                    yield Delta("text", choice.delta.refusal)
                reasoning = getattr(choice.delta, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning:
                    thinking.append(reasoning)
                    yield Delta("thinking", reasoning)
                if choice.finish_reason:
                    reason = choice.finish_reason
        if reason not in {"stop", "content_filter", "tool_calls", "length"}:
            raise ProviderError("Chat Completions 回复未完成，请检查服务连接。")
        content = [ContentBlock("text", "".join(text))]
        if thinking:
            content.insert(0, ContentBlock("thinking", "".join(thinking)))
        calls = tuple(ToolCall(**value) for _, value in sorted(fragments.items()))
        _valid_calls(calls)
        if reason == "tool_calls" and not calls:
            raise ProviderError("服务报告工具调用但未返回完整调用；未执行工具。")
        native: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
        if thinking:
            native["reasoning_content"] = "".join(thinking)
        if calls:
            native["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls
            ]
        stop = "max_tokens" if reason == "length" else reason
        yield Completed(Message("assistant", tuple(content), self.config.protocol, (native,), calls), stop, usage)


def create_provider(config: ProviderConfig) -> SDKProvider:
    adapters = {
        "anthropic-messages": AnthropicProvider,
        "openai-responses": ResponsesProvider,
        "openai-chat-completions": ChatCompletionsProvider,
    }
    return adapters[config.protocol](config)
