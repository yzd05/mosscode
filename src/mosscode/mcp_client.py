"""MCP sessions for tools, resources and prompts, each owning its SDK context lifetime.

Discovery can run in the background: each server registers its tools, resources and
prompts as soon as it is ready, so a slow server never holds up the prompt or the other servers.
"""

import asyncio
import base64
import errno
import hashlib
import json
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from itertools import islice
from pathlib import Path

import anyio
import httpx
from jsonschema import SchemaError, validators
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage
from pydantic import AnyUrl
from referencing import Registry

from .images import ImageError, image_ref, load_image
from .mcp_config import MCPSettings, ServerConfig
from .mcp_stdio import adaptive_stdio_client
from .messages import ToolDefinition
from .permissions import normalize_mcp_uri
from .tools.base import IMAGES, Tool, ToolFailure, image_context

MCP_TEXT_LIMIT = 65536
RESOURCE_TRUNCATION_NOTE = "\n…（MCP 资源内容超过 64 KB，已截断）"
PROMPT_TRUNCATION_NOTE = "\n…（MCP 提示词内容超过 64 KB，已截断）"


@dataclass(frozen=True)
class MCPResourceInfo:
    server: str
    uri: str
    name: str
    title: str = ""
    description: str = ""
    mime_type: str = ""
    size: int | None = None


@dataclass(frozen=True)
class MCPResourceTemplateInfo:
    server: str
    uri_template: str
    name: str
    title: str = ""
    description: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class MCPPromptArg:
    name: str
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class MCPPromptInfo:
    server: str
    name: str
    command_name: str  # Slash command name without leading '/', matching r"[a-z][a-z0-9_-]*"
    description: str = ""
    arguments: tuple[MCPPromptArg, ...] = ()


def tool_name(server: str, tool: str) -> str:
    name = f"mcp__{server}__{tool}"
    if len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_]+", server + tool) and "__" not in server and "__" not in tool:
        return name
    digest = hashlib.sha256(json.dumps([server, tool]).encode()).hexdigest()[:12]
    clean = lambda value: re.sub(r"[^A-Za-z0-9_]", "_", value)
    return f"mcp__{clean(server)[:16]}__{clean(tool)[:22]}_{digest}"


def prompt_command_name(server: str, prompt: str) -> str:
    """Deterministic lowercase command name matching commands.NAME (r'[a-z][a-z0-9_-]*', <= 64 chars)."""
    s, p = server.lower(), prompt.lower()
    name = f"mcp__{s}__{p}"
    if (
        len(name) <= 64
        and re.fullmatch(r"[a-z0-9_-]+", s)
        and re.fullmatch(r"[a-z0-9_-]+", p)
        and "__" not in s
        and "__" not in p
        and (server, prompt) == (s, p)
    ):
        return name
    digest = hashlib.sha256(json.dumps([server, prompt]).encode()).hexdigest()[:12]
    clean = lambda value: re.sub(r"[^a-z0-9_-]", "_", value)
    return f"mcp__{clean(s)[:16]}__{clean(p)[:22]}_{digest}"


def _decode_images(items: list[tuple[str, str]], accepts: bool, store) -> tuple[tuple, list[str]]:
    """Decode (alt, base64_data) pairs into ImageRefs and one status line per image."""
    refs, notes = [], []
    for alt, data in items:
        if not accepts:
            notes.append(f"{alt}（当前模型不支持图片，已省略）")
            continue
        try:
            image = load_image(base64.b64decode(data, validate=True))
        except ImageError as exc:
            notes.append(f"{alt}（{exc}）")
            continue
        except ValueError:
            notes.append(f"{alt}（图片数据不是有效的 base64。）")
            continue
        refs.append(image_ref(image, alt, store))
        notes.append(f"{alt} · 图片 · {image.describe()}")
    return tuple(refs), notes


def _safe_b64_len(blob: str) -> int:
    try:
        return len(base64.b64decode(blob, validate=True))
    except ValueError:
        try:
            padded = blob.strip()
            padded += "=" * (-len(padded) % 4)
            return len(base64.b64decode(padded))
        except Exception:
            return 0


class _TextCollector:
    def __init__(self, limit: int, note: str):
        self.limit = limit
        self.note = note
        self.parts: list[str] = []
        self.used = 0
        self.truncated = False

    def add(self, text: str, *, sep: str = "\n") -> None:
        if self.truncated:
            return
        prefix = sep if self.parts else ""
        encoded_prefix = prefix.encode("utf-8")
        encoded_text = text.encode("utf-8")
        if self.used + len(encoded_prefix) + len(encoded_text) <= self.limit:
            if self.parts and sep != "\n":
                self.parts[-1] = self.parts[-1] + sep + text
            else:
                self.parts.append(text)
            self.used += len(encoded_prefix) + len(encoded_text)
            return
        room = self.limit - self.used - len(encoded_prefix)
        if room > 0:
            clipped = encoded_text[:room].decode("utf-8", errors="ignore")
            if clipped:
                if self.parts and sep != "\n":
                    self.parts[-1] = self.parts[-1] + sep + clipped
                else:
                    self.parts.append(clipped)
        self.used = self.limit
        self.truncated = True

    def finish(self, empty: str = "") -> str:
        body = "\n".join(self.parts)
        if self.truncated:
            return body + self.note if body else self.note.lstrip("\n")
        return body or empty


class ToolsHTTPTransport(StreamableHTTPTransport):
    # SDK 1.30 also resumes interrupted POST/SSE responses via GET. This pinned
    # hook disables that path as well as the optional notification GET below.
    async def _handle_reconnection(self, ctx, last_event_id, retry_interval_ms, attempt=0):
        request = ctx.session_message.message.root
        await ctx.read_stream_writer.send(
            SessionMessage(
                types.JSONRPCMessage(
                    types.JSONRPCError(
                        jsonrpc="2.0",
                        id=request.id,
                        error=types.ErrorData(
                            code=types.CONNECTION_CLOSED, message="MCP SSE 响应中断；不自动恢复连接。"
                        ),
                    )
                )
            )
        )


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


def check_schema(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("工具参数 Schema 必须为 object。")
    if len(json.dumps(schema).encode()) > 65536:
        raise ValueError("工具 Schema 过大。")

    def walk(value):
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith("#")):
                    raise ValueError("不支持加载外部 Schema 引用。")
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


def json_arguments(validator, raw, kind: str) -> MCPArguments:
    """Arguments checked against a tool's JSON Schema (MCP tools, skill tools); at most 64 KB."""
    try:
        if not isinstance(raw, dict) or len(json.dumps(raw, allow_nan=False).encode()) > 65536:
            raise ValueError
        errors = list(islice(validator.iter_errors(raw), 5))
        if errors:
            fields = [".".join(map(str, e.absolute_path)) or "参数对象" for e in errors[:5]]
            raise ToolFailure(
                "invalid_arguments", f"{kind} 参数不符合工具 JSON Schema，问题字段：" + "、".join(fields), fields=fields
            )
    except ToolFailure:
        raise
    except Exception:
        raise ToolFailure("invalid_arguments", f"无法校验 {kind} JSON 参数或参数过大。") from None
    return MCPArguments(deepcopy(raw))


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
        self.prompt_summary = f"外部 MCP 工具：{self.server_name} / {self.remote_name}" + (
            "（只读）" if self.read_only else ""
        )
        self.schema = deepcopy(remote.inputSchema)
        self.validator = check_schema(self.schema)

    def definition(self):
        return ToolDefinition(self.name, self.description, deepcopy(self.schema), self.prompt_summary)

    def validate_arguments(self, raw):
        return json_arguments(self.validator, raw, "MCP")

    async def execute(self, params):
        accepts, store = image_context()
        result = await self.connection.call(self.remote_name, params.model_dump())
        lines, unsupported, pictures = [], [], []
        for block in result.content:
            if block.type == "text":
                lines.append(block.text)
            elif block.type == "image":
                pictures.append(block.data)
            else:
                unsupported.append(block.type)
        refs = ()
        if pictures:
            refs, notes = await asyncio.to_thread(self._images, pictures, accepts, store)
            lines.extend(notes)
        if result.structuredContent is not None:
            lines.append(json.dumps(result.structuredContent, ensure_ascii=False, allow_nan=False))
        if unsupported:
            lines.append("（未展示的 MCP 内容类型：" + ", ".join(sorted(set(unsupported))) + "；未自动读取资源。）")
        output = "\n".join(lines) or "MCP 工具未返回正文。"
        details = {"server": self.server_name, "tool": self.remote_name, "unsupported_types": unsupported}
        if refs:
            details[IMAGES] = refs
        if result.isError:
            raise ToolFailure("mcp_tool_error", output, **details)
        return output, details

    def _images(self, pictures: list[str], accepts: bool, store) -> tuple[tuple, list[str]]:
        """The images to attach and a line for each: its size, or why it was left out."""
        items = [(f"{self.server_name}/{self.remote_name} 的图片 {n}", data) for n, data in enumerate(pictures, 1)]
        return _decode_images(items, accepts, store)


async def _paginate(fetch, attr: str, label: str, *, tolerate_missing: bool = False) -> list:
    discovered, cursor, seen = [], None, set()
    for _ in range(100):
        try:
            page = await fetch(cursor=cursor)
        except McpError as exc:
            if tolerate_missing and exc.error.code == types.METHOD_NOT_FOUND:
                return []
            raise
        discovered.extend(getattr(page, attr))
        cursor = page.nextCursor
        if cursor is None:
            break
        if cursor in seen:
            raise ValueError("重复分页游标。")
        seen.add(cursor)
    else:
        raise ValueError(f"{label}分页过多。")
    return discovered


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
        self.tools: tuple = ()
        self.resources: tuple[MCPResourceInfo, ...] = ()
        self.resource_templates: tuple[MCPResourceTemplateInfo, ...] = ()
        self.prompts: tuple[MCPPromptInfo, ...] = ()

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
                    streams = await stack.enter_async_context(
                        adaptive_stdio_client(
                            StdioServerParameters(
                                command=self.config.command,
                                args=list(self.config.args),
                                env=self.config.env,
                                cwd=str(self.root),
                            ),
                            errlog=errlog,
                        )
                    )
                else:
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=self.config.headers,
                            timeout=httpx.Timeout(30, connect=10),
                            follow_redirects=False,
                            trust_env=False,
                        )
                    )
                    streams = await stack.enter_async_context(tools_http(self.config.url, client))
                # Discovery requests share the startup window; tool calls pass config.timeout.
                session = await stack.enter_async_context(
                    ClientSession(
                        *streams, read_timeout_seconds=timedelta(seconds=max(self.config.timeout, self.startup_timeout))
                    )
                )
                init = await session.initialize()
                caps = getattr(init, "capabilities", None)
                has_tools_cap = caps is not None and getattr(caps, "tools", None) is not None
                discovered = await _paginate(
                    session.list_tools, "tools", "工具列表", tolerate_missing=not has_tools_cap
                )
                tools = tuple(MCPTool(self.root, self, item) for item in discovered)
                if len({t.name for t in tools}) != len(tools):
                    raise ValueError("重复工具名。")

                resources: tuple[MCPResourceInfo, ...] = ()
                resource_templates: tuple[MCPResourceTemplateInfo, ...] = ()
                if caps is not None and getattr(caps, "resources", None) is not None:
                    raw_resources = await _paginate(
                        session.list_resources, "resources", "资源列表", tolerate_missing=True
                    )
                    resources = tuple(
                        MCPResourceInfo(
                            server=self.config.name,
                            uri=str(item.uri),
                            name=item.name or "",
                            title=getattr(item, "title", None) or "",
                            description=item.description or "",
                            mime_type=getattr(item, "mimeType", None) or "",
                            size=getattr(item, "size", None),
                        )
                        for item in raw_resources
                    )
                    raw_templates = await _paginate(
                        session.list_resource_templates,
                        "resourceTemplates",
                        "资源模板列表",
                        tolerate_missing=True,
                    )
                    resource_templates = tuple(
                        MCPResourceTemplateInfo(
                            server=self.config.name,
                            uri_template=str(item.uriTemplate),
                            name=item.name or "",
                            title=getattr(item, "title", None) or "",
                            description=item.description or "",
                            mime_type=getattr(item, "mimeType", None) or "",
                        )
                        for item in raw_templates
                    )

                prompts: tuple[MCPPromptInfo, ...] = ()
                if caps is not None and getattr(caps, "prompts", None) is not None:
                    raw_prompts = await _paginate(session.list_prompts, "prompts", "提示词列表", tolerate_missing=True)
                    prompts = tuple(
                        MCPPromptInfo(
                            server=self.config.name,
                            name=item.name,
                            command_name=prompt_command_name(self.config.name, item.name),
                            description=item.description or getattr(item, "title", None) or "",
                            arguments=tuple(
                                MCPPromptArg(
                                    name=arg.name,
                                    description=arg.description or "",
                                    required=bool(arg.required),
                                )
                                for arg in (item.arguments or ())
                            ),
                        )
                        for item in raw_prompts
                    )
                    if len({p.command_name for p in prompts}) != len(prompts):
                        raise ValueError("重复提示词名。")

                self.tools = tools
                self.resources = resources
                self.resource_templates = resource_templates
                self.prompts = prompts
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
            raise ToolFailure("mcp_unavailable", "MCP Server 已不可用，当前不自动重连。", executed=False)
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
            raise ToolFailure(
                "mcp_protocol_error", "MCP Server 返回协议错误；未自动重试，可能已有外部效果。", rpc_code=exc.error.code
            ) from None
        except Exception:
            self.state = "failed"
            self.stop.set()
            raise ToolFailure("mcp_disconnected", "MCP 连接中断或响应不合法；可能已有外部效果，未自动重试。") from None

    async def _send_read_only(self, request, result_type, label: str):
        if self.state != "ready" or self.session is None:
            raise ToolFailure("mcp_unavailable", "MCP Server 已不可用，当前不自动重连。", executed=False)
        try:
            return await self.session.send_request(
                request,
                result_type,
                request_read_timeout_seconds=timedelta(seconds=self.config.timeout),
            )
        except asyncio.CancelledError:
            self.stop.set()
            self.state = "closed"
            raise
        except McpError as exc:
            if exc.error.code == types.CONNECTION_CLOSED:
                self.state = "failed"
                self.stop.set()
                raise ToolFailure("mcp_disconnected", f"{label}时 MCP 连接中断。") from None
            if exc.error.code == 408:
                self.stop.set()
                self.state = "closed"
                raise ToolFailure("timeout", f"{label}等待结果超时。") from None
            if exc.error.code == -32002:
                raise ToolFailure(
                    "mcp_protocol_error", f"{label}失败：目标资源不存在（-32002）。", rpc_code=exc.error.code
                ) from None
            raise ToolFailure(
                "mcp_protocol_error",
                f"{label}失败：MCP Server 返回协议错误（{exc.error.code}）。",
                rpc_code=exc.error.code,
            ) from None
        except Exception:
            self.state = "failed"
            self.stop.set()
            raise ToolFailure("mcp_disconnected", f"{label}时 MCP 连接中断或响应不合法。") from None

    async def read_resource(self, uri: str):
        if self.state != "ready" or self.session is None:
            raise ToolFailure("mcp_unavailable", "MCP Server 已不可用，当前不自动重连。", executed=False)
        try:
            canonical = normalize_mcp_uri(uri)
            params = types.ReadResourceRequestParams(uri=AnyUrl(canonical))
        except Exception:
            raise ToolFailure("invalid_arguments", "MCP 资源 URI 格式不合法。", executed=False) from None
        return await self._send_read_only(
            types.ClientRequest(types.ReadResourceRequest(params=params)),
            types.ReadResourceResult,
            "读取 MCP 资源",
        )

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None):
        if self.state != "ready" or self.session is None:
            raise ToolFailure("mcp_unavailable", "MCP Server 已不可用，当前不自动重连。", executed=False)
        try:
            params = types.GetPromptRequestParams(name=name, arguments=arguments)
        except Exception:
            raise ToolFailure("invalid_arguments", "MCP 提示词参数不合法。", executed=False) from None
        return await self._send_read_only(
            types.ClientRequest(types.GetPromptRequest(params=params)),
            types.GetPromptResult,
            "获取 MCP 提示词",
        )

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
    resources: tuple[MCPResourceInfo, ...] = ()
    resource_templates: tuple[MCPResourceTemplateInfo, ...] = ()
    prompts: tuple[MCPPromptInfo, ...] = ()

    def _extra_counts(self) -> str:
        parts = []
        total_res = len(self.resources) + len(self.resource_templates)
        if total_res:
            parts.append(f" · {total_res} 个资源")
        if self.prompts:
            parts.append(f" · {len(self.prompts)} 个提示词")
        return "".join(parts)

    def describe(self) -> str:
        if self.state == "ready":
            return f"已连接 · {len(self.tools)} 个工具{self._extra_counts()}"
        return {
            "new": "未启动",
            "connecting": "连接中…",
            "failed": f"失败：{self.detail}",
            "skipped": f"已跳过：{self.detail}",
            "closed": "已关闭（不自动重连）",
        }.get(self.state, self.state)

    def notice(self) -> str:
        if self.state == "ready":
            return f"MCP {self.name} 已连接 · 注册 {len(self.tools)} 个工具{self._extra_counts()}。"
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
        # A server settles when it reports, which happens just before its task finishes.
        return sum(
            not task.done() and connection.config.name not in self._outcomes
            for task, connection in zip(self._tasks, self.connections)
        )

    def connection_for(self, server: str) -> ServerConnection | None:
        for connection in self.connections:
            if connection.config.name == server:
                return connection
        return None

    def has_server(self, server: str) -> bool:
        return self.connection_for(server) is not None

    def statuses(self) -> tuple[ServerStatus, ...]:
        result = []
        for connection in self.connections:
            config = connection.config
            outcome = self._outcomes.get(config.name)
            if outcome is None:
                result.append(ServerStatus(config.name, config.type, "connecting" if self._tasks else "new"))
            elif outcome.state == "ready" and connection.state != "ready":
                # Disconnected or timed out after discovery; tools now report mcp_unavailable.
                result.append(
                    ServerStatus(
                        config.name,
                        config.type,
                        "closed",
                        tools=outcome.tools,
                        resources=outcome.resources,
                        resource_templates=outcome.resource_templates,
                        prompts=outcome.prompts,
                    )
                )
            else:
                result.append(outcome)
        return tuple(result)

    def _ready_statuses(self, server: str | None = None) -> list[ServerStatus]:
        return [s for s in self.statuses() if s.state == "ready" and (server is None or s.name == server)]

    def list_resources(self, server: str | None = None) -> tuple[MCPResourceInfo, ...]:
        return tuple(item for status in self._ready_statuses(server) for item in status.resources)

    def list_resource_templates(self, server: str | None = None) -> tuple[MCPResourceTemplateInfo, ...]:
        return tuple(item for status in self._ready_statuses(server) for item in status.resource_templates)

    def list_prompts(self, server: str | None = None) -> tuple[MCPPromptInfo, ...]:
        return tuple(item for status in self._ready_statuses(server) for item in status.prompts)

    def _ready_connection(self, server: str) -> ServerConnection:
        connection = self.connection_for(server)
        if connection is None:
            raise ToolFailure("mcp_server_not_found", f"未配置 MCP Server：{server}。", server=server, executed=False)
        outcome = self._outcomes.get(server)
        if (
            self._closed
            or (outcome is not None and outcome.state != "ready")
            or connection.state != "ready"
            or connection.session is None
        ):
            raise ToolFailure(
                "mcp_unavailable", f"MCP Server {server} 已不可用，当前不自动重连。", server=server, executed=False
            )
        return connection

    async def read_resource(
        self,
        server: str,
        uri: str,
        *,
        accepts_images: bool = False,
        image_store=None,
    ) -> tuple[str, tuple, dict]:
        server = server.strip()
        uri = uri.strip()
        connection = self._ready_connection(server)
        result = await connection.read_resource(uri)
        entries: list[tuple[str, str | int]] = []
        pictures: list[tuple[str, str]] = []
        mime_types: list[str] = []
        for item in result.contents:
            item_uri = str(getattr(item, "uri", None) or uri)
            mime = (getattr(item, "mimeType", None) or "").strip()
            if mime:
                mime_types.append(mime)
            if hasattr(item, "text"):
                entries.append(("text", item.text))
            elif hasattr(item, "blob"):
                if mime.lower().startswith("image/"):
                    alt = f"{server}/{item_uri} 的图片 {len(pictures) + 1}"
                    entries.append(("image", len(pictures)))
                    pictures.append((alt, item.blob))
                else:
                    byte_len = _safe_b64_len(item.blob)
                    entries.append(
                        ("text", f"[二进制资源 {item_uri} · {mime or 'application/octet-stream'} · {byte_len} 字节]")
                    )
        refs = ()
        image_notes: list[str] = []
        if pictures:
            refs, image_notes = await asyncio.to_thread(_decode_images, pictures, accepts_images, image_store)
        collector = _TextCollector(MCP_TEXT_LIMIT, RESOURCE_TRUNCATION_NOTE)
        for kind, value in entries:
            collector.add(image_notes[value] if kind == "image" else value)
        output = collector.finish("（MCP 资源内容为空）")
        details = {
            "server": server,
            "uri": uri,
            "mime_types": mime_types,
            "truncated": collector.truncated,
        }
        return output, refs, details

    async def get_prompt(
        self,
        server: str,
        name: str,
        arguments: dict[str, str] | None = None,
        *,
        accepts_images: bool = False,
        image_store=None,
    ) -> tuple[str, tuple]:
        server = server.strip()
        name = name.strip()
        connection = self._ready_connection(server)
        clean_args = {str(k): str(v) for k, v in arguments.items()} if arguments else None
        result = await connection.get_prompt(name, clean_args)
        messages = list(getattr(result, "messages", None) or ())
        single_user = len(messages) == 1 and getattr(messages[0], "role", "user") == "user"
        pictures: list[tuple[str, str]] = []
        raw_messages: list[tuple[str, list[tuple[str, str | int]]]] = []
        for msg in messages:
            role = getattr(msg, "role", "user")
            header = "" if single_user else ("[用户]" if role == "user" else "[助手]")
            blocks = msg.content if isinstance(msg.content, (list, tuple)) else (msg.content,)
            msg_entries: list[tuple[str, str | int]] = []
            for block in blocks:
                kind = getattr(block, "type", None)
                if kind == "text" or (kind is None and hasattr(block, "text")):
                    msg_entries.append(("text", block.text))
                elif kind == "image" or hasattr(block, "data"):
                    alt = f"{server}/{name} 的图片 {len(pictures) + 1}"
                    msg_entries.append(("image", len(pictures)))
                    pictures.append((alt, block.data))
                elif kind == "resource" or hasattr(block, "resource"):
                    res = block.resource
                    res_uri = str(getattr(res, "uri", None) or "")
                    res_mime = (getattr(res, "mimeType", None) or "").strip()
                    if hasattr(res, "text"):
                        text = f"[嵌入资源 {res_uri}]\n{res.text}" if res_uri else res.text
                        msg_entries.append(("text", text))
                    elif hasattr(res, "blob"):
                        if res_mime.lower().startswith("image/"):
                            alt = f"{server}/{name} 资源 {res_uri or len(pictures) + 1} 的图片 {len(pictures) + 1}"
                            msg_entries.append(("image", len(pictures)))
                            pictures.append((alt, res.blob))
                        else:
                            byte_len = _safe_b64_len(res.blob)
                            label_uri = res_uri or name
                            msg_entries.append(
                                (
                                    "text",
                                    f"[二进制资源 {label_uri} · {res_mime or 'application/octet-stream'} · {byte_len} 字节]",
                                )
                            )
                elif kind == "resource_link" or hasattr(block, "uri"):
                    msg_entries.append(("text", f"[资源链接 {block.uri}]"))
                else:
                    msg_entries.append(("text", f"（未展示的提示词内容类型：{kind or type(block).__name__}）"))
            raw_messages.append((header, msg_entries))

        refs = ()
        image_notes: list[str] = []
        if pictures:
            refs, image_notes = await asyncio.to_thread(_decode_images, pictures, accepts_images, image_store)

        rendered_messages: list[str] = []
        for header, msg_entries in raw_messages:
            lines = [image_notes[val] if kind == "image" else val for kind, val in msg_entries]
            body = "\n".join(lines)
            if header:
                rendered_messages.append(f"{header}\n{body}" if body else header)
            elif body:
                rendered_messages.append(body)

        collector = _TextCollector(MCP_TEXT_LIMIT, PROMPT_TRUNCATION_NOTE)
        description = (getattr(result, "description", None) or "").strip()
        joined_body = "\n\n".join(rendered_messages)
        if description and description != joined_body:
            collector.add(description)
        for index, block_text in enumerate(rendered_messages):
            collector.add(
                block_text, sep="\n\n" if (index > 0 or (description and description != joined_body)) else "\n"
            )
        return collector.finish(), refs

    def begin(self, registry, on_update=None) -> None:
        """Start discovery in the background; idempotent. on_update(status) runs as each server settles."""
        if self._tasks or self._closed:
            return
        self._tasks = [
            asyncio.create_task(self._connect(c, registry, on_update), name=f"mcp-start:{c.config.name}")
            for c in self.connections
        ]

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
            status = ServerStatus(
                config.name,
                config.type,
                "ready",
                tools=tools,
                resources=connection.resources,
                resource_templates=connection.resource_templates,
                prompts=connection.prompts,
            )
        self._outcomes[config.name] = status
        if on_update is not None:
            on_update(status)

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)

    async def start(self, registry, *, problems_only: bool = False):
        """Discover and register everything, then report; only the first call reports.

        problems_only drops the "connected" lines, as startup output does; /mcp lists those servers.
        """
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
        statuses = [s for s in self.statuses() if not problems_only or s.state != "ready"]
        return (*self.notices, *(status.notice() for status in statuses))

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(c.close() for c in self.connections))
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
