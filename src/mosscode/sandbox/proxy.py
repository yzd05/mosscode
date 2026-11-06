"""The sandbox's network: a per-command HTTP proxy on the loopback, the container's only way out.

MXC lets a container reach exactly one loopback address and port ("proxy-only egress"), so each sandboxed command
gets a listener of its own here and the port tells whose connection it is. Every host goes through approve() first
(the user's WebFetch domain rules, then the user); addresses are checked as web_fetch checks them, and a TLS client
hello that names a different host than the one approved closes the tunnel.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import unquote, urlsplit

from ..security import public_address

HEAD_LIMIT = 64 * 1024
HEAD_SECONDS = 30
CONNECT_SECONDS = 20
HELLO_SECONDS = 10
HELLO_LIMIT = 16 * 1024 + 5  # One TLS record.
DROPPED_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "host"}

# approve(host, port) -> "" when the connection may go ahead, otherwise why not.
Approve = Callable[[str, int], Awaitable[str]]
Resolver = Callable[[str, int], Awaitable[list[str]]]


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Incomplete(Exception):
    pass


def normalize_host(host: str) -> str:
    host = host.strip().removeprefix("[").removesuffix("]").rstrip(".")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise Refused(400, f"主机名不合法：{host}") from None


def check_host(host: str) -> None:
    """Local names and addresses are refused before anyone is asked."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        local = not public_address(host)
    else:
        local = "." not in host or host == "localhost" or host.endswith(".localhost")
    if local:
        raise Refused(403, f"{host} 是本机或内网地址，沙箱中的命令不能连接")


def tls_server_name(data: bytes) -> str | None:
    """The server name in a TLS ClientHello at the start of data; None when data is not one or names nothing.

    Raises Incomplete while the first record has not fully arrived.
    """
    if not data or data[0] != 0x16:
        return None
    if len(data) < 5:
        raise Incomplete
    length = int.from_bytes(data[3:5], "big")
    if len(data) < 5 + length:
        raise Incomplete
    record = data[5 : 5 + length]
    try:
        if record[0] != 1:  # Not a ClientHello.
            return None
        pos = 4 + 2 + 32  # Handshake header, version, random.
        pos += 1 + record[pos]  # Session id.
        pos += 2 + int.from_bytes(record[pos : pos + 2], "big")  # Cipher suites.
        pos += 1 + record[pos]  # Compression methods.
        if pos + 2 > len(record):
            return None
        end = min(len(record), pos + 2 + int.from_bytes(record[pos : pos + 2], "big"))
        pos += 2
        while pos + 4 <= end:
            kind = int.from_bytes(record[pos : pos + 2], "big")
            size = int.from_bytes(record[pos + 2 : pos + 4], "big")
            pos += 4
            if kind == 0:  # server_name: a list of (type, name).
                item, stop = pos + 2, min(end, pos + size)
                while item + 3 <= stop:
                    name_type, name_size = record[item], int.from_bytes(record[item + 1 : item + 3], "big")
                    item += 3
                    if name_type == 0:
                        return record[item : item + name_size].decode("ascii", "replace").lower().rstrip(".")
                    item += name_size
                return None
            pos += size
    except IndexError:
        return None
    return None


def _response(status: int, reason: str) -> bytes:
    phrase = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 502: "Bad Gateway"}.get(status, "Error")
    body = f"MossCode 沙箱：{reason}\n".encode()
    head = (
        f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode("ascii") + body


def _target(line: str) -> tuple[str, str, int, bytes | None]:
    """(method, host, port, request line for the origin or None for CONNECT)."""
    try:
        method, target, version = line.split(" ")
    except ValueError:
        raise Refused(400, "请求行不合法") from None
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise Refused(400, f"CONNECT 目标不合法：{target}")
        return "CONNECT", normalize_host(host), int(port), None
    try:
        parts = urlsplit(target)
        port = parts.port or 80
    except ValueError:
        raise Refused(400, f"请求地址不合法：{target}") from None
    if parts.scheme.lower() != "http" or not parts.hostname:
        raise Refused(400, "代理只接受 CONNECT 和 http:// 的绝对地址")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return method, normalize_host(parts.hostname), port, f"{method} {path} {version}".encode("latin-1")


class NetworkProxy:
    def __init__(
        self,
        approve: Approve,
        *,
        resolver: Resolver,
        upstream: Callable[[str], str | None] = lambda host: None,
        connect=asyncio.open_connection,
    ):
        self.approve = approve
        self.resolver = resolver
        self.upstream = upstream
        self.connect = connect
        self.allowed: list[str] = []
        self.refused: dict[str, str] = {}
        self._decisions: dict[str, asyncio.Future[str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=HEAD_LIMIT)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for decision in self._decisions.values():
            decision.cancel()

    def summary(self) -> list[str]:
        notes = []
        if self.allowed:
            notes.append("联网：已连接 " + "、".join(self.allowed) + "。")
        for host, reason in self.refused.items():
            notes.append(f"联网：{host} 被拒绝（{reason}）。")
        return notes

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self._handle(reader, writer)
        except Refused as refused:
            try:
                writer.write(_response(refused.status, refused.reason))
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except asyncio.CancelledError:
            # close() ends connections still open after the command (keep-alive); asyncio's server would log a
            # cancelled handler as an error, so it ends normally instead.
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
        lines = head.decode("latin-1").split("\r\n")
        method, host, port, origin_line = _target(lines[0])
        try:
            check_host(host)
            reason = await self._decide(host, port)
            if reason:
                raise Refused(403, reason)
            upstream_reader, upstream_writer = await self._open(host, port)
        except Refused as refused:
            self.refused.setdefault(host, refused.reason)
            raise
        if host not in self.allowed:
            self.allowed.append(host)
        downstream = None
        try:
            if origin_line is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                # The server may speak first (SSH); only the client's own first bytes are checked.
                downstream = asyncio.ensure_future(_pipe(upstream_reader, writer))
                upstream_writer.write(await self._client_hello(reader, host))
            else:
                # One host per connection: the Host header is rewritten and the connection closes after it.
                kept = [
                    line for line in lines[1:] if line and line.split(":", 1)[0].strip().lower() not in DROPPED_HEADERS
                ]
                netloc = host if port == 80 else f"{host}:{port}"
                rebuilt = [origin_line.decode("latin-1"), f"Host: {netloc}", *kept, "Connection: close", "", ""]
                upstream_writer.write("\r\n".join(rebuilt).encode("latin-1"))
                downstream = asyncio.ensure_future(_pipe(upstream_reader, writer))
            await upstream_writer.drain()
            await asyncio.gather(_pipe(reader, upstream_writer), downstream)
        finally:
            if downstream is not None and not downstream.done():
                downstream.cancel()
            upstream_writer.close()

    async def _decide(self, host: str, port: int) -> str:
        """One question per host and command, however many connections ask at once."""
        decision = self._decisions.get(host)
        if decision is None:
            decision = asyncio.ensure_future(self._ask(host, port))
            self._decisions[host] = decision
        return await asyncio.shield(decision)

    async def _ask(self, host: str, port: int) -> str:
        try:
            return await self.approve(host, port)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # A broken approval path must refuse, not let the connection through.
            return f"审批失败：{exc}"

    async def _open(self, host: str, port: int):
        proxy = self.upstream(host)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses = [host] if literal is not None else []
        if literal is None:
            try:
                addresses = await self.resolver(host, port)
            except (OSError, UnicodeError):
                if proxy is None:
                    raise Refused(502, f"无法解析域名 {host}") from None
        if any(not public_address(address) for address in addresses):
            raise Refused(403, f"{host} 解析到本机或内网地址，沙箱中的命令不能连接")
        if proxy is not None:
            return await self._through(proxy, host, port)
        failure = None
        for address in addresses:
            try:
                # The address just checked, so a second DNS answer cannot point elsewhere.
                return await asyncio.wait_for(self.connect(address, port), CONNECT_SECONDS)
            except (OSError, TimeoutError) as exc:
                failure = exc
        raise Refused(502, f"无法连接 {host}:{port}（{failure}）")

    async def _through(self, proxy: str, host: str, port: int):
        """A tunnel through the user's own HTTP proxy, which resolves and connects for us."""
        parts = urlsplit(proxy)
        if parts.scheme.lower() != "http" or not parts.hostname:
            raise Refused(502, f"上游代理 {parts.scheme}:// 不受支持，只支持 http://")
        try:
            reader, writer = await asyncio.wait_for(self.connect(parts.hostname, parts.port or 80), CONNECT_SECONDS)
        except (OSError, TimeoutError) as exc:
            raise Refused(502, f"无法连接上游代理 {parts.hostname}:{parts.port or 80}（{exc}）") from None
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if parts.username is not None:
            secret = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
            request += f"Proxy-Authorization: Basic {base64.b64encode(secret).decode('ascii')}\r\n"
        writer.write((request + "\r\n").encode("latin-1"))
        try:
            await writer.drain()
            answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_SECONDS)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            raise Refused(502, "上游代理没有响应") from None
        status = answer.split(b" ", 2)[1:2]
        if status != [b"200"]:
            writer.close()
            line = answer.split(b"\r\n", 1)[0].decode("latin-1")
            raise Refused(502, f"上游代理拒绝了 {target}：{line}")
        return reader, writer

    async def _client_hello(self, reader: asyncio.StreamReader, host: str) -> bytes:
        """The client's first bytes; a TLS hello for another host than the approved one ends the tunnel."""
        data = await reader.read(HELLO_LIMIT)  # No deadline: an idle client sends nothing to check.
        while True:
            try:
                name = tls_server_name(data)
                break
            except Incomplete:
                if len(data) >= HELLO_LIMIT:
                    name = None
                    break
                more = await asyncio.wait_for(reader.read(HELLO_LIMIT - len(data)), HELLO_SECONDS)
                if not more:
                    name = None
                    break
                data += more
        if name is not None and name != host:
            self.refused.setdefault(name, f"TLS 握手中的主机名与申请连接的 {host} 不一致")
            raise ConnectionAbortedError
        return data


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        writer.close()
