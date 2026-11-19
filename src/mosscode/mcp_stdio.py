"""Adaptive MCP stdio client transport supporting both JSONL and Content-Length framing.

Standard MCP stdio uses newline-delimited UTF-8 JSON messages (JSONL), while legacy or
LSP-derived servers frame messages with HTTP-style ``Content-Length: <bytes>\\r\\n\\r\\n``
headers. ``adaptive_stdio_client`` auto-detects peer framing on ``stdout``, switches
outgoing ``stdin`` writes accordingly, and transparently retries the initial
``initialize`` handshake in ``content-length`` mode when a strict server rejects JSONL.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, TextIO

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession
from mcp.client.stdio import (
    PROCESS_TERMINATION_TIMEOUT,
    StdioServerParameters,
    _create_platform_compatible_process,
    _get_executable_command,
    _terminate_process_tree,
    get_default_environment,
)
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)

MAX_FRAME_BYTES = 16 * 1024 * 1024  # 16 MiB cap per message payload/line.
MAX_HEADER_BYTES = 8 * 1024  # 8 KiB cap per header block.

FramingMode = Literal["auto", "jsonl", "content-length"]
WireFraming = Literal["jsonl", "content-length"]

__all__ = [
    "FramingMode",
    "MAX_FRAME_BYTES",
    "MAX_HEADER_BYTES",
    "StdioFramingError",
    "WireFraming",
    "adaptive_stdio_client",
    "connect_stdio_session",
    "encode_stdio_frame",
]


class StdioFramingError(ValueError):
    """Raised when an incoming MCP stdio header or frame violates framing limits."""


@dataclass(frozen=True)
class _ParsedFrame:
    framing: WireFraming
    payload: bytes


@dataclass
class _HandshakeState:
    write_mode: WireFraming
    detected: WireFraming | None
    can_retry: bool
    handshake_event: anyio.Event = field(default_factory=anyio.Event)
    pending_init: SessionMessage | None = None
    init_request_id: str | int | None = None
    received_valid: bool = False
    retry_needed: bool = False


def encode_stdio_frame(payload: bytes, framing: WireFraming) -> bytes:
    """Encode UTF-8 JSON payload bytes using either JSONL or Content-Length framing."""
    if framing == "content-length":
        return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
    return payload + b"\n"


def _is_header_start(first_line: bytes) -> bool:
    colon = first_line.find(b":")
    if colon <= 0:
        return False
    name = first_line[:colon].strip().lower()
    return name in (b"content-length", b"content-type")


def _next_frame(buf: bytearray) -> _ParsedFrame | None:
    """Extract the next complete frame from ``buf``, mutating ``buf`` in place."""
    while True:
        leading = 0
        size = len(buf)
        while leading < size and buf[leading] in (0x0D, 0x0A):
            leading += 1
        if leading:
            del buf[:leading]
        if not buf:
            return None

        first_nl = buf.find(b"\n")
        if first_nl == -1:
            # Check header block cap early if the prefix already starts a header.
            colon = buf.find(b":")
            if colon > 0 and bytes(buf[:colon]).strip().lower() in (b"content-length", b"content-type"):
                if len(buf) > MAX_HEADER_BYTES:
                    raise StdioFramingError("MCP stdio 消息头过大。")
            elif len(buf) > MAX_FRAME_BYTES:
                raise StdioFramingError("MCP stdio 消息帧超过 16 MB 上限。")
            return None

        first_line = bytes(buf[:first_nl]).rstrip(b"\r")
        if _is_header_start(first_line):
            pos = 0
            headers: list[bytes] = []
            header_end: int | None = None
            while True:
                nl = buf.find(b"\n", pos)
                if nl == -1:
                    break
                if nl > MAX_HEADER_BYTES:
                    raise StdioFramingError("MCP stdio 消息头过大。")
                line = bytes(buf[pos:nl]).rstrip(b"\r")
                pos = nl + 1
                if line == b"":
                    header_end = pos
                    break
                headers.append(line)

            if header_end is None:
                if len(buf) > MAX_HEADER_BYTES:
                    raise StdioFramingError("MCP stdio 消息头过大。")
                return None

            content_length: int | None = None
            for header in headers:
                colon = header.find(b":")
                if colon <= 0:
                    raise StdioFramingError("MCP stdio 消息头格式无效。")
                key = header[:colon].strip().lower()
                raw_val = header[colon + 1 :].strip()
                if key == b"content-length":
                    if raw_val.startswith(b"-") and raw_val[1:].isdigit():
                        raise StdioFramingError("MCP stdio Content-Length 不能为负数。")
                    if not raw_val.isdigit():
                        raise StdioFramingError("MCP stdio Content-Length 无效。")
                    parsed_len = int(raw_val)
                    if parsed_len > MAX_FRAME_BYTES:
                        raise StdioFramingError("MCP stdio Content-Length 超过 16 MB 上限。")
                    if content_length is not None and content_length != parsed_len:
                        raise StdioFramingError("MCP stdio Content-Length 重复且不一致。")
                    content_length = parsed_len

            if content_length is None:
                raise StdioFramingError("MCP stdio 消息头缺少 Content-Length。")

            if len(buf) - header_end < content_length:
                return None

            payload = bytes(buf[header_end : header_end + content_length])
            del buf[: header_end + content_length]
            return _ParsedFrame("content-length", payload)

        if first_nl > MAX_FRAME_BYTES:
            raise StdioFramingError("MCP stdio 消息帧超过 16 MB 上限。")
        payload = bytes(buf[:first_nl]).rstrip(b"\r")
        del buf[: first_nl + 1]
        if not payload.strip():
            continue
        return _ParsedFrame("jsonl", payload)


def _is_parse_error_payload(text: str) -> bool:
    """Return True if ``text`` is a JSON-RPC error object with code -32700 (Parse error)."""
    try:
        data = json.loads(text)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    return isinstance(error, dict) and error.get("code") == types.PARSE_ERROR


def _coerce_null_id_error(text: str, fallback_id: str | int | None) -> types.JSONRPCMessage | None:
    """Bind a JSON-RPC error with ``id: null`` to ``fallback_id`` so ClientSession does not hang."""
    if fallback_id is None:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("id", ...) is not None or not isinstance(data.get("error"), dict):
        return None
    patched = {**data, "id": fallback_id}
    with suppress(Exception):
        return types.JSONRPCMessage.model_validate(patched)
    return None


def _merged_environment(server: StdioServerParameters) -> dict[str, str]:
    default_env = get_default_environment()
    return {**default_env, **server.env} if server.env is not None else default_env


async def _spawn_process(server: StdioServerParameters, errlog: TextIO):
    command = _get_executable_command(server.command)
    return await _create_platform_compatible_process(
        command=command,
        args=server.args,
        env=_merged_environment(server),
        errlog=errlog,
        cwd=server.cwd,
    )


async def _close_process(process, *, grace_timeout: float = PROCESS_TERMINATION_TIMEOUT) -> None:
    """Close stdin, wait for graceful exit, escalate to process-tree kill, and close pipes."""
    with anyio.CancelScope(shield=True):
        if process.stdin:
            with suppress(Exception):
                await process.stdin.aclose()
        try:
            with anyio.fail_after(grace_timeout):
                await process.wait()
        except TimeoutError:
            with suppress(Exception):
                await _terminate_process_tree(process)
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                with anyio.move_on_after(1.0):
                    await process.wait()
        except ProcessLookupError:
            pass
        with suppress(Exception):
            await process.__aexit__(None, None, None)


@asynccontextmanager
async def adaptive_stdio_client(
    server: StdioServerParameters,
    errlog: TextIO = sys.stderr,
    *,
    framing: FramingMode = "auto",
):
    """Spawn an MCP stdio server and auto-detect JSONL vs Content-Length framing."""
    if framing not in ("auto", "jsonl", "content-length"):
        raise ValueError(f"不支持的 MCP stdio 分帧模式：{framing}。")

    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    try:
        initial_process = await _spawn_process(server, errlog)
    except BaseException:
        with anyio.CancelScope(shield=True):
            for stream in (read_stream, write_stream, read_stream_writer, write_stream_reader):
                await stream.aclose()
        raise

    proc_holder = [initial_process]

    def _encode_message(session_message: SessionMessage, mode: WireFraming) -> bytes:
        json_text = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
        payload = json_text.encode(encoding=server.encoding, errors=server.encoding_error_handler)
        return encode_stdio_frame(payload, mode)

    async def _run_attempt(process, state: _HandshakeState) -> None:
        assert process.stdout is not None, "Opened process is missing stdout"
        assert process.stdin is not None, "Opened process is missing stdin"

        async with anyio.create_task_group() as attempt_tg:

            def _trigger_retry() -> None:
                state.retry_needed = True
                state.can_retry = False
                state.handshake_event.set()
                attempt_tg.cancel_scope.cancel()

            def _settle_handshake(peer_framing: WireFraming | None = None) -> None:
                if peer_framing is not None and framing == "auto" and state.detected is None:
                    state.detected = peer_framing
                    state.write_mode = peer_framing
                state.can_retry = False
                state.pending_init = None
                state.init_request_id = None
                state.received_valid = True
                state.handshake_event.set()

            async def stdout_reader() -> None:
                buf = bytearray()
                try:
                    while True:
                        try:
                            chunk = await process.stdout.receive(65536)
                        except (anyio.EndOfStream, anyio.ClosedResourceError):
                            break
                        if not chunk:
                            break
                        buf.extend(chunk)

                        while True:
                            try:
                                frame = _next_frame(buf)
                            except StdioFramingError as exc:
                                state.can_retry = False
                                state.pending_init = None
                                state.handshake_event.set()
                                with suppress(anyio.ClosedResourceError):
                                    await read_stream_writer.send(exc)
                                return

                            if frame is None:
                                break

                            try:
                                text = frame.payload.decode(
                                    encoding=server.encoding,
                                    errors=server.encoding_error_handler,
                                )
                            except UnicodeDecodeError as exc:
                                if state.can_retry and state.pending_init is not None and not state.received_valid:
                                    continue
                                with suppress(anyio.ClosedResourceError):
                                    await read_stream_writer.send(exc)
                                continue

                            if (
                                state.can_retry
                                and state.pending_init is not None
                                and not state.received_valid
                                and _is_parse_error_payload(text)
                            ):
                                _trigger_retry()
                                return

                            try:
                                message = types.JSONRPCMessage.model_validate_json(text)
                            except Exception as exc:
                                if state.can_retry and state.pending_init is not None and not state.received_valid:
                                    continue
                                coerced = _coerce_null_id_error(text, state.init_request_id)
                                if coerced is not None:
                                    message = coerced
                                else:
                                    logger.debug("Failed to parse JSON-RPC message from stdio server", exc_info=True)
                                    with suppress(anyio.ClosedResourceError):
                                        await read_stream_writer.send(exc)
                                    continue

                            _settle_handshake(frame.framing)
                            with suppress(anyio.ClosedResourceError):
                                await read_stream_writer.send(SessionMessage(message))

                    # Handle EOF: retry if strict Content-Length server closed stdout on JSONL initialize.
                    if state.can_retry and state.pending_init is not None and not state.received_valid:
                        _trigger_retry()
                        return

                    # Check leftover bytes at EOF.
                    trailing = bytes(buf).strip(b"\r\n")
                    if trailing:
                        first_nl = trailing.find(b"\n")
                        head = (trailing[:first_nl] if first_nl != -1 else trailing).rstrip(b"\r")
                        if _is_header_start(head):
                            with suppress(anyio.ClosedResourceError):
                                await read_stream_writer.send(StdioFramingError("MCP stdio 消息帧在流结束前截断。"))
                        elif trailing.lstrip(b" \t").startswith((b"{", b"[")):
                            with suppress(Exception):
                                text = trailing.decode(server.encoding, errors=server.encoding_error_handler)
                                message = types.JSONRPCMessage.model_validate_json(text)
                                _settle_handshake("jsonl")
                                await read_stream_writer.send(SessionMessage(message))
                finally:
                    if not state.retry_needed:
                        with anyio.CancelScope(shield=True):
                            with suppress(Exception):
                                await read_stream_writer.aclose()

            async def stdin_writer() -> None:
                try:
                    # On retry, replay the buffered initialize request immediately in Content-Length mode.
                    if not state.can_retry and state.pending_init is not None:
                        replay = state.pending_init
                        state.pending_init = None
                        await process.stdin.send(_encode_message(replay, state.write_mode))

                    # On the initial auto-detect attempt, hold after sending initialize until framing settles.
                    if state.can_retry:
                        try:
                            first_msg = await write_stream_reader.receive()
                        except (anyio.EndOfStream, anyio.ClosedResourceError):
                            state.can_retry = False
                            state.handshake_event.set()
                            return

                        root = first_msg.message.root
                        is_init_req = isinstance(root, types.JSONRPCRequest) and root.method == "initialize"
                        if is_init_req:
                            state.pending_init = first_msg
                            state.init_request_id = root.id
                        else:
                            state.can_retry = False
                            state.handshake_event.set()

                        try:
                            await process.stdin.send(_encode_message(first_msg, state.write_mode))
                        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                            if is_init_req and not state.received_valid:
                                _trigger_retry()
                            return

                        if is_init_req:
                            await state.handshake_event.wait()
                            if state.retry_needed:
                                return

                    async for session_message in write_stream_reader:
                        root = session_message.message.root
                        if (
                            not state.received_valid
                            and state.init_request_id is None
                            and isinstance(root, types.JSONRPCRequest)
                            and root.method == "initialize"
                        ):
                            state.init_request_id = root.id
                        await process.stdin.send(_encode_message(session_message, state.write_mode))
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                    await anyio.lowlevel.checkpoint()

            attempt_tg.start_soon(stdout_reader)
            attempt_tg.start_soon(stdin_writer)

    async def _run_transport() -> None:
        initial_write: WireFraming = "content-length" if framing == "content-length" else "jsonl"
        initial_detected: WireFraming | None = None if framing == "auto" else framing
        state = _HandshakeState(
            write_mode=initial_write,
            detected=initial_detected,
            can_retry=(framing == "auto"),
        )
        if not state.can_retry:
            state.handshake_event.set()

        try:
            proc = proc_holder[0]
            await proc.__aenter__()
            await _run_attempt(proc, state)

            if state.retry_needed and state.pending_init is not None:
                await _close_process(proc, grace_timeout=0.2)
                proc_holder[0] = None

                retry_state = _HandshakeState(
                    write_mode="content-length",
                    detected="content-length",
                    can_retry=False,
                    pending_init=state.pending_init,
                    init_request_id=state.init_request_id,
                )
                retry_state.handshake_event.set()

                proc2 = await _spawn_process(server, errlog)
                proc_holder[0] = proc2
                await proc2.__aenter__()
                await _run_attempt(proc2, retry_state)
        finally:
            with anyio.CancelScope(shield=True):
                with suppress(Exception):
                    await read_stream_writer.aclose()

    cancelled = False
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_run_transport)
            try:
                yield read_stream, write_stream
            except BaseException as exc:
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    cancelled = True
                raise
            finally:
                tg.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            active_proc = proc_holder[0]
            proc_holder[0] = None
            if active_proc is not None:
                await _close_process(
                    active_proc,
                    grace_timeout=0.2 if cancelled else PROCESS_TERMINATION_TIMEOUT,
                )
            for stream in (read_stream, write_stream, read_stream_writer, write_stream_reader):
                with suppress(Exception):
                    await stream.aclose()


async def connect_stdio_session(
    stack: AsyncExitStack,
    params: StdioServerParameters,
    *,
    errlog: TextIO = sys.stderr,
    read_timeout_seconds: timedelta | None = None,
    framing: FramingMode = "auto",
) -> tuple[ClientSession, types.InitializeResult]:
    """Open an adaptive stdio transport + ClientSession on ``stack`` and initialize it."""
    streams = await stack.enter_async_context(adaptive_stdio_client(params, errlog=errlog, framing=framing))
    session = await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=read_timeout_seconds))
    result = await session.initialize()
    return session, result
