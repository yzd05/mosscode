"""Tools-only MCP sessions, each with a task owning its SDK context lifetime.

Discovery can run in the background: each server registers its tools as soon as
it is ready, so a slow server never holds up the prompt or the other servers.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import errno
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re

import anyio
import httpx
from jsonschema import SchemaError, validators
from referencing import Registry
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage

from .mcp_config import MCPSettings, ServerConfig
from .messages import ToolDefinition
from .tools.base import Tool, ToolFailure


def tool_name(server: str, tool: str) -> str:
    name = f"mcp__{server}__{tool}"
    if len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_]+", server + tool) and "__" not in server and "__" not in tool:
        return name
    digest = hashlib.sha256(json.dumps([server, tool]).encode()).hexdigest()[:12]
    clean = lambda value: re.sub(r"[^A-Za-z0-9_]", "_", value)
    return f"mcp__{clean(server)[:16]}__{clean(tool)[:22]}_{digest}"


class ToolsHTTPTransport(StreamableHTTPTransport):
    # SDK 1.30 also resumes interrupted POST/SSE responses via GET. This pinned
    # hook disables that path as well as the optional notification GET below.
    async def _handle_reconnection(self, ctx, last_event_id, retry_interval_ms, attempt=0):
        request = ctx.session_message.message.root
        await ctx.read_stream_writer.send(SessionMessage(types.JSONRPCMessage(types.JSONRPCError(
            jsonrpc="2.0", id=request.id,
            error=types.ErrorData(code=types.CONNECTION_CLOSED, message="MCP SSE 响应中断；不自动恢复连接。")))))


@asynccontextmanager
async def tools_http(url: str, client: httpx.AsyncClient):
    """Use SDK HTTP framing without its optional reconnecting notification GET.

    Tools use POST responses (JSON or SSE). A standalone GET stream is optional
    in MCP and unnecessary here; do not start the SDK's reconnect loop.
    """
    incoming, read = anyio.create_memory_object_stream(0)
    write, outgoing = anyio.create_memory_object_stream(0)
    transport = ToolsHTTPTransport(url)
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(transport.post_writer, client, outgoing, incoming, write, lambda: None, group)
            try:
                yield read, write
            finally:
                with anyio.CancelScope(shield=True):
                    if transport.session_id:
                        with suppress(Exception):
                            with anyio.move_on_after(2):
                                await transport.terminate_session(client)
                group.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            for stream in (incoming, read, write, outgoing):
                await stream.aclose()


def _check_schema(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("工具参数 Schema 必须为 object。")
    if len(json.dumps(schema).encode()) > 65536:
        raise ValueError("工具 Schema 过大。")
    def walk(value):
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith("#")):
                    raise ValueError("本章不加载外部 Schema 引用。")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(schema)
    validator = validators.validator_for(schema)
    validator.check_schema(schema)
    # An empty Registry has no network retrieval callback. Local fragments
    # resolve within the root resource; unknown resources fail closed.
    return validator(schema, registry=Registry())


@dataclass
class MCPArguments:
    values: dict

    def model_dump(self):
        return deepcopy(self.values)


def describe_failure(exc: BaseException, config: ServerConfig) -> str:
    """A safe category for /mcp; never the URL, headers, environment or server output."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, FileNotFoundError) or (isinstance(exc, OSError) and exc.errno == errno.ENOENT):
        return f"找不到命令 {config.command}" if config.type == "stdio" else "找不到所需文件"
    if isinstance(exc, PermissionError):
        return "没有权限启动命令" if config.type == "stdio" else "没有访问权限"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "连接超时"
    if isinstance(exc, httpx.TransportError):
        return "无法连接端点"
    if isinstance(exc, McpError):
        if exc.error.code == types.CONNECTION_CLOSED:
            return "Server 在初始化期间断开（进程可能已退出）"
        return f"协议错误（{exc.error.code}）"
    if isinstance(exc, SchemaError):
        return "工具 Schema 无效"
    if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], str) and exc.args[0].endswith("。"):
        return exc.args[0].rstrip("。")  # Our own discovery checks.
    if isinstance(exc, OSError):
        return "启动或连接失败（系统错误）"
    return f"连接或发现失败（{type(exc).__name__}）"


class MCPTool(Tool):
    permission_kind = "mcp"
    read_only = False  # Server annotations are hints, not grants, unless the user trusts them.
    timeout = 30.0

    def __init__(self, root: Path, connection, remote):
        super().__init__(root)
        self.connection = connection
        config = connection.config
        self.server_name, self.remote_name = config.name, remote.name
        self.timeout = config.timeout
        hint = getattr(remote.annotations, "readOnlyHint", None) if remote.annotations else None
        self.read_only = bool(config.trust_read_only_hint and hint is True)
        self.name = tool_name(self.server_name, self.remote_name)
        self.description = f"MCP Server {self.server_name} 的工具 {self.remote_name}。" + (remote.description or "")
        self.prompt_summary = f"外部 MCP 工具：{self.server_name} / {self.remote_name}" + ("（只读）" if self.read_only else "")
        self.schema = deepcopy(remote.inputSchema)
        self.validator = _check_schema(self.schema)

    def definition(self):
        return ToolDefinition(self.name, self.description, deepcopy(self.schema), self.prompt_summary)

    def validate_arguments(self, raw):
        try:
            if not isinstance(raw, dict) or len(json.dumps(raw, allow_nan=False).encode()) > 65536:
                raise ValueError
            errors = list(islice(self.validator.iter_errors(raw), 5))
            if errors:
                fields = [".".join(map(str, e.absolute_path)) or "参数对象" for e in errors[:5]]
                raise ToolFailure("invalid_arguments", "MCP 参数不符合工具 JSON Schema，问题字段：" + "、".join(fields),
                                  fields=fields)
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure("invalid_arguments", "无法校验 MCP JSON 参数或参数过大。") from None
        return MCPArguments(deepcopy(raw))

    async def execute(self, params):
        result = await self.connection.call(self.remote_name, params.model_dump())
        lines, unsupported = [], []
        for block in result.content:
            if block.type == "text":
                lines.append(block.text)
            else:
                unsupported.append(block.type)
        if result.structuredContent is not None:
            lines.append(json.dumps(result.structuredContent, ensure_ascii=False, allow_nan=False))
        if unsupported:
            lines.append("（本章未展示的 MCP 内容类型：" + ", ".join(sorted(set(unsupported))) + "；未自动读取资源。）")
        output = "\n".join(lines) or "MCP 工具未返回正文。"
        details = {"server": self.server_name, "tool": self.remote_name, "unsupported_types": unsupported}
        if result.isError:
            raise ToolFailure("mcp_tool_error", output, **details)
        return output, details


class ServerConnection:
    def __init__(self, root: Path, config: ServerConfig, startup_timeout=10.0):
        self.root, self.config = root.resolve(), config
        self.startup_timeout = config.startup_timeout or startup_timeout
        self.task = None
        self.session = None
        self.stop = asyncio.Event()
        self.ready = None
        self.state = "new"
        self.error = ""  # Safe reason shown by /mcp.

    async def start(self):
        if self.stop.is_set():
            return None
        if self.task is None:
            self.ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(self._own(), name=f"mcp:{self.config.name}")
        done, _ = await asyncio.wait([self.ready], timeout=self.startup_timeout)
        if not done:
            self.error = f"启动超时（{self.startup_timeout:g} 秒）"
            await self.close()
            return None
        return self.ready.result()

    async def _own(self):
        # AnyIO transport/session scopes must enter and exit in this same task.
        tools = ()
        try:
            async with AsyncExitStack() as stack:
                if self.config.type == "stdio":
                    errlog = stack.enter_context(open(os.devnull, "w"))
                    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                        command=self.config.command, args=list(self.config.args), env=self.config.env,
                        cwd=str(self.root)), errlog=errlog))
                else:
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        headers=self.config.headers, timeout=httpx.Timeout(30, connect=10),
                        follow_redirects=False, trust_env=False))
                    streams = await stack.enter_async_context(tools_http(self.config.url, client))
                # Discovery requests share the startup window; tool calls pass config.timeout.
                session = await stack.enter_async_context(ClientSession(
                    *streams, read_timeout_seconds=timedelta(seconds=max(self.config.timeout, self.startup_timeout))))
                await session.initialize()
                discovered, cursor, seen = [], None, set()
                for _ in range(100):
                    page = await session.list_tools(cursor=cursor)
                    discovered.extend(page.tools)
                    cursor = page.nextCursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise ValueError("重复分页游标。")
                    seen.add(cursor)
                else:
                    raise ValueError("工具列表分页过多。")
                tools = tuple(MCPTool(self.root, self, item) for item in discovered)
                if len({t.name for t in tools}) != len(tools):
                    raise ValueError("重复工具名。")
                self.session, self.state = session, "ready"
                self.ready.set_result(tools)
                await self.stop.wait()
        except asyncio.CancelledError:
            self.state = "closed"
        except Exception as exc:
            self.state = "failed"
            if not self.ready.done():
                self.error = describe_failure(exc, self.config)
        finally:
            self.session = None
            if self.state == "ready":
                self.state = "closed"
            if not self.ready.done():
                self.ready.set_result(None)

    async def call(self, name, arguments):
        if self.state != "ready" or self.session is None:
            raise ToolFailure("mcp_unavailable", "MCP Server 已不可用；本章不自动重连。", executed=False)
        try:
            return await self.session.call_tool(name, arguments, timedelta(seconds=self.config.timeout))
        except asyncio.CancelledError:
            # The server may already have acted. Close this session; never retry.
            self.stop.set()
            self.state = "closed"
            raise
        except McpError as exc:
            if exc.error.code == types.CONNECTION_CLOSED:
                self.state = "failed"
                self.stop.set()
                raise ToolFailure("mcp_disconnected", "MCP 连接中断；可能已有外部效果，未自动重试。") from None
            if exc.error.code == 408:
                self.stop.set()
                self.state = "closed"
                raise ToolFailure("timeout", "MCP 工具等待结果超时，可能已有外部效果。") from None
            raise ToolFailure("mcp_protocol_error", "MCP Server 返回协议错误；未自动重试，可能已有外部效果。",
                              rpc_code=exc.error.code) from None
        except Exception:
            self.state = "failed"
            self.stop.set()
            raise ToolFailure("mcp_disconnected", "MCP 连接中断或响应不合法；可能已有外部效果，未自动重试。") from None

    async def close(self):
        self.stop.set()
        if self.task is not None:
            # If startup is still waiting for a server, interrupt its own scopes.
            if self.state == "new" and not self.task.done():
                self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            if self.ready is not None and not self.ready.done():
                self.ready.set_result(None)


@dataclass(frozen=True)
class ServerStatus:
    name: str
    type: str
    state: str  # connecting / ready / failed / skipped / closed
    detail: str = ""
    tools: tuple = ()  # MCPTool objects once ready.

    def describe(self) -> str:
        if self.state == "ready":
            return f"已连接 · {len(self.tools)} 个工具"
        return {"new": "未启动", "connecting": "连接中…", "failed": f"失败：{self.detail}", "skipped": f"已跳过：{self.detail}",
                "closed": "已关闭（不自动重连）"}.get(self.state, self.state)

    def notice(self) -> str:
        if self.state == "ready":
            return f"MCP {self.name} 已连接 · 注册 {len(self.tools)} 个工具。"
        return f"MCP {self.name} {self.describe()}；可用 /mcp 查看。"


class MCPManager:
    def __init__(self, root: Path, settings: MCPSettings, *, startup_timeout=10.0):
        self.connections = [ServerConnection(root, c, startup_timeout) for c in settings.servers]
        self.notices = settings.notices
        self._outcomes: dict[str, ServerStatus] = {}
        self._tasks: list[asyncio.Task] = []
        self._reported = False
        self._closed = False

    @property
    def pending(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def statuses(self) -> tuple[ServerStatus, ...]:
        result = []
        for connection in self.connections:
            config = connection.config
            outcome = self._outcomes.get(config.name)
            if outcome is None:
                result.append(ServerStatus(config.name, config.type, "connecting" if self._tasks else "new"))
            elif outcome.state == "ready" and connection.state != "ready":
                # Disconnected or timed out after discovery; tools now report mcp_unavailable.
                result.append(ServerStatus(config.name, config.type, "closed", tools=outcome.tools))
            else:
                result.append(outcome)
        return tuple(result)

    def begin(self, registry, on_update=None) -> None:
        """Start discovery in the background; idempotent. on_update(status) runs as each server settles."""
        if self._tasks or self._closed:
            return
        self._tasks = [asyncio.create_task(self._connect(c, registry, on_update), name=f"mcp-start:{c.config.name}")
                       for c in self.connections]

    async def _connect(self, connection, registry, on_update):
        config = connection.config
        tools = await connection.start()
        if self._closed:
            return
        if tools is None:
            status = ServerStatus(config.name, config.type, "failed", connection.error or "连接或发现失败")
        elif any(registry.get(t.name) is not None for t in tools):
            await connection.close()
            status = ServerStatus(config.name, config.type, "skipped", "工具名与已注册工具冲突")
        else:
            # Synchronous: a model request never sees half of a server's tools.
            for tool in tools:
                registry.register(tool)
            status = ServerStatus(config.name, config.type, "ready", tools=tools)
        self._outcomes[config.name] = status
        if on_update is not None:
            on_update(status)

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)

    async def start(self, registry):
        """Discover and register everything, then report; only the first call reports."""
        if self._closed:
            return ()
        self.begin(registry)
        try:
            await self.wait()
        except BaseException:
            await self.close()
            raise
        if self._closed or self._reported:
            return ()
        self._reported = True
        return (*self.notices, *(status.notice() for status in self.statuses()))

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(c.close() for c in self.connections))
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
