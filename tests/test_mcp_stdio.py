"""Tests for adaptive MCP stdio framing (JSONL + Content-Length auto-detection)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import tracemalloc
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.shared.exceptions import McpError

from mosscode.mcp_stdio import (
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    StdioFramingError,
    _next_frame,
    adaptive_stdio_client,
    connect_stdio_session,
    encode_stdio_frame,
)

TOY_SERVER = textwrap.dedent(
    r"""
    import json
    import os
    import sys
    import time
    from pathlib import Path

    mode = sys.argv[1]
    log_path = Path(sys.argv[2])

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer


    def record(entry: dict) -> None:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


    record({"event": "start", "pid": os.getpid(), "cwd": os.getcwd(), "custom_env": os.environ.get("MOSS_TEST_VAR"), "leaked_env": os.environ.get("MOSS_UNLISTED_PARENT_VAR")})


    def send_cl(obj: dict, *, lf_only: bool = False, with_content_type: bool = False, chunk_size: int = 0) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        sep = "\n" if lf_only else "\r\n"
        headers = []
        if with_content_type:
            headers.append(f"Content-Type: application/vscode-jsonrpc; charset=utf-8{sep}")
        headers.append(f"Content-Length: {len(raw)}{sep}{sep}")
        frame = "".join(headers).encode("ascii") + raw
        if chunk_size > 0:
            for idx in range(0, len(frame), chunk_size):
                stdout.write(frame[idx : idx + chunk_size])
                stdout.flush()
                time.sleep(0.002)
        else:
            stdout.write(frame)
            stdout.flush()


    def send_jsonl(obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
        stdout.write(raw)
        stdout.flush()


    def read_strict_header_frame():
        headers = {}
        while True:
            line = stdin.readline()
            if not line:
                return None, None
            stripped = line.rstrip(b"\r\n")
            if stripped == b"":
                if headers:
                    break
                continue
            if b":" not in stripped:
                return "bad_header", stripped
            k, v = stripped.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
        length = int(headers[b"content-length"])
        body = b""
        while len(body) < length:
            part = stdin.read(length - len(body))
            if not part:
                return None, None
            body += part
        return "content-length", body


    def read_flexible_frame():
        while True:
            line = stdin.readline()
            if not line:
                return None, None
            if line in (b"\r\n", b"\n"):
                continue
            lower = line.lower()
            if lower.startswith((b"content-length:", b"content-type:")):
                headers = {}
                cur = line
                while True:
                    s = cur.rstrip(b"\r\n")
                    if s == b"":
                        break
                    k, v = s.split(b":", 1)
                    headers[k.strip().lower()] = v.strip()
                    cur = stdin.readline()
                    if not cur:
                        return None, None
                length = int(headers[b"content-length"])
                body = b""
                while len(body) < length:
                    part = stdin.read(length - len(body))
                    if not part:
                        return None, None
                    body += part
                return "content-length", body
            return "jsonl", line.rstrip(b"\r\n")


    if mode == "oversized":
        # Read initialize line/frame then emit an oversized Content-Length header without allocating 1 GB.
        stdin.readline()
        stdout.write(b"Content-Length: 999999999\r\n\r\n")
        stdout.flush()
        time.sleep(10)
        sys.exit(0)

    if mode == "always_err_null":
        stdin.readline()
        send_cl({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Always broken"}})
        time.sleep(10)
        sys.exit(0)

    if mode == "hang_sleep":
        time.sleep(60)
        sys.exit(0)

    while True:
        if mode == "strict_blocking":
            framing_kind, raw_body = read_strict_header_frame()
        elif mode in ("strict_eof", "strict_err_id", "strict_err_null", "strict_err_jsonl"):
            first = stdin.readline()
            if not first:
                break
            if first in (b"\r\n", b"\n"):
                continue
            if not first.lower().startswith((b"content-length:", b"content-type:")):
                record({"event": "rejected_non_cl", "pid": os.getpid()})
                if mode == "strict_eof":
                    sys.exit(1)
                if mode == "strict_err_id":
                    req_id = 0
                    try:
                        req_id = json.loads(first.decode("utf-8")).get("id", 0)
                    except Exception:
                        pass
                    send_cl({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32700, "message": "Parse error"}})
                    sys.exit(1)
                if mode == "strict_err_jsonl":
                    send_jsonl({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    sys.exit(1)
                if mode == "strict_err_null":
                    send_cl({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    # Stay open waiting for more to verify client still cleanly respawns.
                    time.sleep(5)
                    sys.exit(0)
            headers = {}
            cur = first
            while True:
                s = cur.rstrip(b"\r\n")
                if s == b"":
                    break
                k, v = s.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
                cur = stdin.readline()
                if not cur:
                    sys.exit(0)
            length = int(headers[b"content-length"])
            raw_body = stdin.read(length)
            framing_kind = "content-length"
        else:
            framing_kind, raw_body = read_flexible_frame()

        if framing_kind is None:
            break

        text_body = raw_body.decode("utf-8")
        msg = json.loads(text_body)
        method = msg.get("method", "")
        record(
            {
                "event": "recv",
                "pid": os.getpid(),
                "method": method,
                "framing": framing_kind,
                "byte_len": len(raw_body),
                "char_len": len(text_body),
            }
        )

        if "id" not in msg:
            continue

        req_id = msg["id"]
        params = msg.get("params") or {}
        if method == "initialize":
            res = {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"toy-{mode}", "version": "1.0"},
            }
        elif method == "tools/list":
            res = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo UTF-8 text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            arg_text = params["arguments"]["text"]
            res = {"content": [{"type": "text", "text": f"ack:{arg_text}"}], "isError": False}
        else:
            res = {}

        reply = {"jsonrpc": "2.0", "id": req_id, "result": res}
        if mode == "jsonl":
            send_jsonl(reply)
        elif mode == "flexible_cl_lf":
            send_cl(reply, lf_only=True, with_content_type=True)
        elif mode == "chunked_utf8_cl":
            send_cl(reply, with_content_type=True, chunk_size=5)
        else:
            send_cl(reply)

    record({"event": "exit", "pid": os.getpid()})
    """
)


def _server_params(
    script: Path,
    mode: str,
    log_path: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-u", str(script), mode, str(log_path)],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )


def _read_events(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture
def toy_script(tmp_path: Path) -> Path:
    script = tmp_path / "toy_mcp_server.py"
    script.write_text(TOY_SERVER, encoding="utf-8")
    return script


async def test_standard_jsonl_stdio_server_end_to_end_and_env_isolation(
    tmp_path: Path, toy_script: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOSS_UNLISTED_PARENT_VAR", "must-not-leak")
    log_path = tmp_path / "jsonl.log"
    workdir = tmp_path / "subcwd"
    workdir.mkdir()
    params = _server_params(
        toy_script,
        "jsonl",
        log_path,
        cwd=workdir,
        env={"MOSS_TEST_VAR": "expected-value"},
    )

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with AsyncExitStack() as stack:
            session, init = await connect_stdio_session(
                stack,
                params,
                errlog=errlog,
                read_timeout_seconds=timedelta(seconds=5),
            )
            assert init.serverInfo.name == "toy-jsonl"
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["echo"]
            called = await session.call_tool("echo", {"text": "hello jsonl"})
            assert called.content[0].text == "ack:hello jsonl"

    events = _read_events(log_path)
    starts = [e for e in events if e["event"] == "start"]
    assert len(starts) == 1
    assert Path(starts[0]["cwd"]).resolve() == workdir.resolve()
    assert starts[0]["custom_env"] == "expected-value"
    assert starts[0]["leaked_env"] is None

    recvs = [e for e in events if e["event"] == "recv"]
    assert [e["method"] for e in recvs] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert all(e["framing"] == "jsonl" for e in recvs)
    assert any(e["event"] == "exit" for e in events)


@pytest.mark.parametrize("mode", ["flexible_cl", "flexible_cl_lf"])
async def test_auto_switches_outgoing_writes_to_content_length_when_server_replies_with_headers(
    tmp_path: Path, toy_script: Path, mode: str
) -> None:
    log_path = tmp_path / f"{mode}.log"
    params = _server_params(toy_script, mode, log_path)

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with adaptive_stdio_client(params, errlog=errlog) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=5)) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["echo"]
                called = await session.call_tool("echo", {"text": "switched"})
                assert called.content[0].text == "ack:switched"

    recvs = [e for e in _read_events(log_path) if e["event"] == "recv"]
    assert [e["method"] for e in recvs] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    # Initial probe is JSONL; once server replies with Content-Length, all subsequent writes use Content-Length.
    assert recvs[0]["framing"] == "jsonl"
    assert [e["framing"] for e in recvs[1:]] == ["content-length", "content-length", "content-length"]


@pytest.mark.parametrize("strict_mode", ["strict_eof", "strict_err_id", "strict_err_null", "strict_err_jsonl"])
async def test_strict_content_length_server_transparently_retries_handshake_in_auto_mode(
    tmp_path: Path, toy_script: Path, strict_mode: str
) -> None:
    log_path = tmp_path / f"{strict_mode}.log"
    params = _server_params(toy_script, strict_mode, log_path)

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with AsyncExitStack() as stack:
            session, init = await connect_stdio_session(
                stack,
                params,
                errlog=errlog,
                read_timeout_seconds=timedelta(seconds=5),
            )
            assert init.serverInfo.name == f"toy-{strict_mode}"
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["echo"]
            called = await session.call_tool("echo", {"text": "retried-ok"})
            assert called.content[0].text == "ack:retried-ok"

    events = _read_events(log_path)
    rejected = [e for e in events if e["event"] == "rejected_non_cl"]
    assert len(rejected) == 1
    recvs = [e for e in events if e["event"] == "recv"]
    assert [e["method"] for e in recvs] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert all(e["framing"] == "content-length" for e in recvs)
    for start in [e for e in events if e["event"] == "start"]:
        assert not _pid_alive(start["pid"])


async def test_explicit_content_length_framing_speaks_headers_from_byte_zero_against_blocking_reader(
    tmp_path: Path, toy_script: Path
) -> None:
    log_path = tmp_path / "strict_blocking.log"
    params = _server_params(toy_script, "strict_blocking", log_path)

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with AsyncExitStack() as stack:
            session, init = await connect_stdio_session(
                stack,
                params,
                errlog=errlog,
                read_timeout_seconds=timedelta(seconds=5),
                framing="content-length",
            )
            assert init.serverInfo.name == "toy-strict_blocking"
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["echo"]
            called = await session.call_tool("echo", {"text": "explicit-cl"})
            assert called.content[0].text == "ack:explicit-cl"

    events = _read_events(log_path)
    assert len([e for e in events if e["event"] == "start"]) == 1
    recvs = [e for e in events if e["event"] == "recv"]
    assert all(e["framing"] == "content-length" for e in recvs)


async def test_multibyte_utf8_chinese_and_emoji_counts_bytes_not_characters_even_when_chunked(
    tmp_path: Path, toy_script: Path
) -> None:
    log_path = tmp_path / "utf8.log"
    # PYTHONUTF8=0 exercises pipe handling when Windows locale code page is non-UTF-8.
    params = _server_params(toy_script, "chunked_utf8_cl", log_path, env={"PYTHONUTF8": "0"})
    text = "你好 MossCode 🌿"

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with adaptive_stdio_client(params, errlog=errlog, framing="content-length") as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=8)) as session:
                await session.initialize()
                called = await session.call_tool("echo", {"text": text})
                assert called.content[0].text == f"ack:{text}"

    call_recv = [e for e in _read_events(log_path) if e.get("method") == "tools/call"][0]
    assert call_recv["framing"] == "content-length"
    # "你好" (+4 UTF-8 bytes over chars) + "🌿" (+3 UTF-8 bytes over chars) => byte_len == char_len + 7.
    assert call_recv["byte_len"] == call_recv["char_len"] + 7


async def test_oversized_content_length_rejected_cleanly_without_large_allocation(
    tmp_path: Path, toy_script: Path
) -> None:
    log_path = tmp_path / "oversized.log"
    params = _server_params(toy_script, "oversized", log_path)

    tracemalloc.start()
    try:
        before_current, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        # 1. Direct stream check: surfaces StdioFramingError immediately without allocating 1 GB.
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with adaptive_stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                init_req = types.JSONRPCMessage(
                    types.JSONRPCRequest(
                        jsonrpc="2.0",
                        id=1,
                        method="initialize",
                        params={
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"},
                        },
                    )
                )
                from mcp.shared.message import SessionMessage

                await write_stream.send(SessionMessage(init_req))
                first = await asyncio.wait_for(read_stream.receive(), timeout=3.0)
                assert isinstance(first, StdioFramingError)
                assert isinstance(first, ValueError)
                assert "16 MB" in str(first)
                with pytest.raises(anyio.EndOfStream):
                    await asyncio.wait_for(read_stream.receive(), timeout=2.0)

        _, peak = tracemalloc.get_traced_memory()
        assert peak - before_current < 8 * 1024 * 1024

        # 2. ClientSession check: closes session cleanly with CONNECTION_CLOSED instead of hanging.
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with AsyncExitStack() as stack:
                with pytest.raises(McpError) as exc_info:
                    await asyncio.wait_for(
                        connect_stdio_session(
                            stack,
                            params,
                            errlog=errlog,
                            read_timeout_seconds=timedelta(seconds=3),
                        ),
                        timeout=4.0,
                    )
                assert exc_info.value.error.code == types.CONNECTION_CLOSED
    finally:
        tracemalloc.stop()


def test_frame_parser_unit_edge_cases_and_limits() -> None:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"text": "你好 🌿"}}, ensure_ascii=False).encode("utf-8")
    encoded = encode_stdio_frame(payload, "content-length")
    buf = bytearray(b"\r\n\n" + encoded + b'\r\n{"jsonrpc":"2.0","id":2,"result":{}}\n')

    first = _next_frame(buf)
    assert first is not None and first.framing == "content-length" and first.payload == payload

    second = _next_frame(buf)
    assert second is not None and second.framing == "jsonl"
    assert json.loads(second.payload.decode("utf-8"))["id"] == 2
    assert _next_frame(buf) is None

    # Negative Content-Length rejected.
    with pytest.raises(StdioFramingError, match="负数"):
        _next_frame(bytearray(b"Content-Length: -5\r\n\r\n{}"))

    # Non-digit Content-Length rejected.
    with pytest.raises(StdioFramingError, match="无效"):
        _next_frame(bytearray(b"Content-Length: 12_3\r\n\r\n{}"))

    # Conflicting duplicate Content-Length rejected.
    with pytest.raises(StdioFramingError, match="不一致"):
        _next_frame(bytearray(b"Content-Length: 2\r\nContent-Length: 4\r\n\r\n{}"))

    # Missing Content-Length in Content-Type header block rejected.
    with pytest.raises(StdioFramingError, match="缺少 Content-Length"):
        _next_frame(bytearray(b"Content-Type: application/vscode-jsonrpc\r\n\r\n{}"))

    # Exact 16 MiB boundary waits for payload rather than raising; 16 MiB + 1 raises immediately.
    assert _next_frame(bytearray(f"Content-Length: {MAX_FRAME_BYTES}\r\n\r\n".encode("ascii"))) is None
    with pytest.raises(StdioFramingError, match="16 MB"):
        _next_frame(bytearray(f"Content-Length: {MAX_FRAME_BYTES + 1}\r\n\r\n".encode("ascii")))

    # Oversized header block (> 8 KiB) rejected before newline/blank line.
    with pytest.raises(StdioFramingError, match="消息头过大"):
        _next_frame(bytearray(b"Content-Type: " + b"x" * (MAX_HEADER_BYTES + 10)))


async def test_invalid_framing_mode_and_missing_executable_errors() -> None:
    with pytest.raises(ValueError, match="分帧模式"):
        async with adaptive_stdio_client(StdioServerParameters(command=sys.executable), framing="invalid"):  # type: ignore[arg-type]
            pass

    with pytest.raises((FileNotFoundError, OSError)):
        async with adaptive_stdio_client(StdioServerParameters(command="mosscode-nonexistent-stdio-binary-xyz")):
            pass


async def test_always_failing_parse_error_with_null_id_retries_at_most_once_without_hanging(
    tmp_path: Path, toy_script: Path
) -> None:
    log_path = tmp_path / "always_err.log"
    params = _server_params(toy_script, "always_err_null", log_path)

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with AsyncExitStack() as stack:
            with pytest.raises(McpError) as exc_info:
                await asyncio.wait_for(
                    connect_stdio_session(
                        stack,
                        params,
                        errlog=errlog,
                        read_timeout_seconds=timedelta(seconds=5),
                    ),
                    timeout=4.0,
                )
            assert exc_info.value.error.code == types.PARSE_ERROR

    starts = [e for e in _read_events(log_path) if e["event"] == "start"]
    assert len(starts) == 2


async def test_cancellation_terminates_hanging_child_promptly(tmp_path: Path, toy_script: Path) -> None:
    log_path = tmp_path / "hang.log"
    params = _server_params(toy_script, "hang_sleep", log_path)

    async def _connect_and_hang() -> None:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with AsyncExitStack() as stack:
                await connect_stdio_session(
                    stack,
                    params,
                    errlog=errlog,
                    read_timeout_seconds=timedelta(seconds=30),
                )

    task = asyncio.create_task(_connect_and_hang())
    for _ in range(100):
        if log_path.exists() and _read_events(log_path):
            break
        await asyncio.sleep(0.02)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3.0)
    assert loop.time() - t0 < 2.0
    for start in [e for e in _read_events(log_path) if e["event"] == "start"]:
        assert not _pid_alive(start["pid"])


async def test_drop_in_compatibility_with_mcp_manager_for_jsonl_and_content_length(
    tmp_path: Path, toy_script: Path
) -> None:
    from mosscode import mcp_client
    from mosscode.mcp_config import MCPSettings, ServerConfig
    from mosscode.messages import ToolCall
    from mosscode.tools import create_default_registry

    fixture = Path(__file__).parent / "fixtures/mcp_stdio_server.py"
    jsonl_log = tmp_path / "fixture.log"
    cl_log = tmp_path / "strict_cl.log"

    settings = MCPSettings(
        (
            ServerConfig("jsonl", "stdio", sys.executable, (str(fixture), str(jsonl_log))),
            ServerConfig("legacy", "stdio", sys.executable, ("-u", str(toy_script), "strict_eof", str(cl_log))),
        )
    )
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = mcp_client.MCPManager(tmp_path, settings, startup_timeout=6)
    try:
        notices = await manager.start(reg)
        assert len([n for n in notices if "已连接" in n]) == 2
        r1, r2 = await asyncio.gather(
            reg.execute(ToolCall("c1", "mcp__jsonl__echo", json.dumps({"text": "delayed"}))),
            reg.execute(ToolCall("c2", "mcp__legacy__echo", json.dumps({"text": "你好 🌿"}))),
        )
        assert not r1.is_error and r1.output == "delayed"
        assert not r2.is_error and r2.output == "ack:你好 🌿"
    finally:
        await manager.close()

    assert (tmp_path / "fixture.closed").exists()
