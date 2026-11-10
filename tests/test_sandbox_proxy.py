"""The sandbox's network proxy: tunnels, approvals, refusals and how execute_command uses it."""

import asyncio
import base64
import functools
import json
import os
import ssl
import sys
from pathlib import Path

import pytest
import yaml
from textual.widgets import OptionList

from mosscode import sandbox
from mosscode.config import ConfigError, SandboxConfig, load_settings
from mosscode.conversation import Conversation
from mosscode.messages import ToolCall
from mosscode.permissions import CHOICES, PermissionPolicy, parse_rule
from mosscode.sandbox import Prepared, Status, explain, windows_mxc
from mosscode.sandbox.proxy import NetworkProxy, tls_server_name
from mosscode.tools import create_default_registry
from mosscode.tui import MossCodeApp
from test_agent import SequenceProvider, call, response
from test_tui import send
from waiting import until

WINDOWS = sys.platform == "win32"
PUBLIC = "93.184.216.34"


def client_hello(host: str) -> bytes:
    """A real TLS ClientHello naming host, as any HTTPS client would send it first."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=host)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


class Origin:
    """A local server standing in for the internet: echoes, or answers one HTTP request."""

    def __init__(self, http: bool = False, upstream: bool = False):
        self.http = http
        self.upstream = upstream  # Acts as the user's own HTTP proxy: accepts CONNECT, then echoes.
        self.received: list[bytes] = []
        self.heads: list[bytes] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def _serve(self, reader, writer):
        try:
            if self.upstream:
                self.heads.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if self.http:
                self.heads.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
                return
            while data := await reader.read(65536):
                self.received.append(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


def make_proxy(origin, approve=None, *, upstream=None, resolve=None):
    asked, opened = [], []

    async def default_approve(host, port):
        asked.append(host)
        return ""

    async def resolver(host, port):
        if resolve is not None:
            return resolve(host)
        return {"example.com": [PUBLIC], "intranet.example": ["10.0.0.5"]}.get(host, [PUBLIC])

    async def connect(address, port):
        opened.append((address, port))
        return await asyncio.open_connection("127.0.0.1", origin.port)

    proxy = NetworkProxy(
        approve or default_approve, resolver=resolver, upstream=upstream or (lambda host: None), connect=connect
    )
    return proxy, asked, opened


async def open_tunnel(url, target):
    reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return reader, writer, head


async def read_all(reader):
    return await asyncio.wait_for(reader.read(), 5)


# The proxy ----------------------------------------------------------------------------------------


def test_the_server_name_is_read_from_a_client_hello():
    hello = client_hello("example.com")
    assert tls_server_name(hello) == "example.com"
    assert tls_server_name(b"GET / HTTP/1.1\r\n") is None


async def test_an_approved_tunnel_carries_tls_to_the_checked_address():
    async with Origin() as origin:
        proxy, asked, opened = make_proxy(origin)
        url = await proxy.start()
        reader, writer, head = await open_tunnel(url, "Example.COM:443")
        assert head.startswith(b"HTTP/1.1 200")
        hello = client_hello("example.com")
        writer.write(hello)
        assert await asyncio.wait_for(reader.readexactly(len(hello)), 5) == hello
        writer.close()
        await proxy.close()
    assert asked == ["example.com"] and opened == [(PUBLIC, 443)]
    assert proxy.summary() == ["联网：已连接 example.com。"]


async def test_a_tls_hello_for_another_host_closes_the_tunnel():
    async with Origin() as origin:
        proxy, asked, _ = make_proxy(origin)
        url = await proxy.start()
        reader, writer, head = await open_tunnel(url, "example.com:443")
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(client_hello("evil.example"))
        assert await read_all(reader) == b""
        await proxy.close()
    assert not origin.received  # Nothing reached the approved host under the other name.
    assert "evil.example" in proxy.refused and asked == ["example.com"]


@pytest.mark.skipif(sys.platform == "win32", reason="bubblewrap reaches the proxy through a Unix socket")
async def test_the_proxy_can_listen_on_a_unix_socket():
    import tempfile

    # Socket paths are limited to about 100 bytes; a short folder keeps clear of that.
    folder = Path(tempfile.mkdtemp(prefix="mc"))
    path = folder / "p.sock"
    async with Origin(http=True) as origin:
        proxy, asked, _ = make_proxy(origin)
        assert await proxy.start(path) == f"unix:{path}"
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert (await read_all(reader)).endswith(b"ok")
        writer.close()
        await proxy.close()
    assert asked == ["example.com"] and not path.exists()
    folder.rmdir()


async def test_a_refused_host_gets_403_and_nothing_is_contacted():
    async def deny(host, port):
        return "用户拒绝了这个域名"

    async with Origin() as origin:
        proxy, _, opened = make_proxy(origin, deny)
        url = await proxy.start()
        reader, writer, head = await open_tunnel(url, "example.com:443")
        body = await read_all(reader)
        await proxy.close()
    assert head.startswith(b"HTTP/1.1 403") and "用户拒绝了这个域名" in body.decode()
    assert not opened and proxy.summary() == ["联网：example.com 被拒绝（用户拒绝了这个域名）。"]


@pytest.mark.parametrize(
    ("target", "asks"),
    [("127.0.0.1:80", []), ("localhost:80", []), ("[::1]:443", []), ("intranet.example:443", ["intranet.example"])],
)
async def test_local_and_private_targets_are_refused(target, asks):
    async with Origin() as origin:
        proxy, asked, opened = make_proxy(origin)
        url = await proxy.start()
        _, _, head = await open_tunnel(url, target)
        await proxy.close()
    # A literal or local name is refused outright; a name is asked first and refused once it resolves privately.
    assert head.startswith(b"HTTP/1.1 403") and asked == asks and not opened


async def test_a_plain_http_request_is_rewritten_for_the_origin():
    async with Origin(http=True) as origin:
        proxy, _, opened = make_proxy(origin)
        url = await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        writer.write(
            b"GET http://example.com/path?q=1 HTTP/1.1\r\nHost: example.com\r\n"
            b"Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic eDp5\r\nAccept: */*\r\n\r\n"
        )
        answer = await read_all(reader)
        await proxy.close()
    head = origin.heads[0].decode()
    assert head.startswith("GET /path?q=1 HTTP/1.1\r\nHost: example.com\r\n")
    assert "Accept: */*" in head and "Connection: close" in head
    assert "Proxy-" not in head and answer.endswith(b"ok") and opened == [(PUBLIC, 80)]


async def test_the_users_own_proxy_is_used_upstream_with_its_credentials():
    def unresolvable(host):
        raise OSError("fake-ip DNS only answers inside the user's proxy")

    async with Origin(upstream=True) as origin:
        proxy, _, opened = make_proxy(
            origin, upstream=lambda host: "http://me:pa%20ss@upstream.test:7890", resolve=unresolvable
        )
        url = await proxy.start()
        reader, writer, head = await open_tunnel(url, "example.com:443")
        hello = client_hello("example.com")
        writer.write(hello)
        assert await asyncio.wait_for(reader.readexactly(len(hello)), 5) == hello
        writer.close()
        await proxy.close()
    sent = origin.heads[0].decode()
    assert head.startswith(b"HTTP/1.1 200") and opened == [("upstream.test", 7890)]
    assert sent.startswith("CONNECT example.com:443 HTTP/1.1")
    assert "Proxy-Authorization: Basic " + base64.b64encode(b"me:pa ss").decode() in sent


async def test_concurrent_connections_to_one_host_ask_once():
    gate = asyncio.Event()
    asked = []

    async def slow(host, port):
        asked.append(host)
        await gate.wait()
        return ""

    async with Origin() as origin:
        proxy, _, opened = make_proxy(origin, slow)
        url = await proxy.start()
        tunnels = [asyncio.create_task(open_tunnel(url, "example.com:443")) for _ in range(3)]
        await asyncio.sleep(0.2)
        gate.set()
        heads = [head for _, _, head in await asyncio.gather(*tunnels)]
        await proxy.close()
    assert all(head.startswith(b"HTTP/1.1 200") for head in heads) and asked == ["example.com"]
    assert len(opened) == 3


async def test_closing_withdraws_a_pending_question_and_drops_the_client():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def forever(host, port):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with Origin() as origin:
        proxy, _, _ = make_proxy(origin, forever)
        url = await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await asyncio.wait_for(started.wait(), 5)
        await proxy.close()
        assert await read_all(reader) == b""
    assert cancelled.is_set()


async def test_closing_an_open_tunnel_logs_no_error():
    errors = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        async with Origin() as origin:
            proxy, _, _ = make_proxy(origin)
            url = await proxy.start()
            reader, writer, head = await open_tunnel(url, "example.com:443")  # Kept alive, like curl's.
            assert head.startswith(b"HTTP/1.1 200")
            await proxy.close()
            assert await read_all(reader) == b""
            await asyncio.sleep(0.1)
    finally:
        loop.set_exception_handler(None)
    assert not errors


# Approvals ----------------------------------------------------------------------------------------


async def test_connect_follows_webfetch_rules_and_the_mode(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    assert (await policy.connect("c", "example.com", 443, "pip install x")).code == "approval_required"
    policy.session.append(parse_rule("WebFetch(domain:*.python.org)", "allow"))
    assert (await policy.connect("c", "pypi.python.org", 443, "pip install x")).action == "allow"
    bypass = PermissionPolicy(tmp_path, mode="bypassPermissions", load=False)
    assert (await bypass.connect("c", "example.com", 443, "curl.exe https://example.com")).action == "allow"
    bypass.session.append(parse_rule("WebFetch(domain:example.com)", "deny"))
    assert (await bypass.connect("c", "example.com", 443, "curl.exe https://example.com")).action == "deny"


async def ask(policy, host, choice):
    policy.interactive = True
    task = asyncio.create_task(policy.connect("c", host, 443, "pip install requests"))
    request = await asyncio.wait_for(policy.requests.get(), 2)
    assert policy.respond(request.id, choice)
    return await task, request


async def test_connect_asks_and_remembers_the_answer(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    decision, request = await ask(policy, "pypi.org", "once")
    assert decision.action == "allow" and request.choices == CHOICES
    assert request.operation.network and request.operation.tool == "web_fetch"
    assert "pip install requests" in request.operation.preview and "pypi.org:443" in request.operation.preview
    policy.interactive = False
    assert (await policy.connect("c", "pypi.org", 443, "pip")).action == "deny"  # Once is this command only.

    decision, _ = await ask(policy, "pypi.org", "session")
    policy.interactive = False
    assert (await policy.connect("c", "pypi.org", 443, "pip")).action == "allow"
    assert not policy.local_file.exists()

    decision, _ = await ask(policy, "files.pythonhosted.org", "permanent")
    assert decision.action == "allow"
    assert "WebFetch(domain:files.pythonhosted.org)" in policy.local_file.read_text(encoding="utf-8")

    decision, _ = await ask(policy, "evil.example", "deny")
    assert decision.code == "permission_denied"


# Configuration and the backend ----------------------------------------------------------------------


def test_network_configuration(tmp_path):
    path = tmp_path / "config.yaml"
    provider = {"name": "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "MOSS_TEST_PROVIDER"}
    for value, expected in ((None, "proxy"), ("none", "none")):
        block = {"mode": "on"} | ({"network": value} if value else {})
        path.write_text(yaml.safe_dump({"providers": [provider], "sandbox": block}), encoding="utf-8")
        assert load_settings(path).sandbox.network == expected
    path.write_text(yaml.safe_dump({"providers": [provider], "sandbox": {"network": "open"}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="sandbox.network"):
        load_settings(path)


def test_the_mxc_request_holds_the_container_to_the_proxy(tmp_path, monkeypatch):
    box = sandbox.configure(SandboxConfig(mode="on"))
    monkeypatch.setattr(box, "_detect", lambda: Status("full", "mxc"))
    box.wxc = tmp_path / "wxc-exec.exe"
    box.network = "proxy"
    root = tmp_path / "project"
    root.mkdir()
    url = "http://127.0.0.1:50123"
    prepared = box.prepare(["cmd", "/c", "echo"], root, {"HTTPS_PROXY": "http://127.0.0.1:7897"}, root, url)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert prepared.env[name] == url  # The user's own proxy is never handed to the command.
    data = json.loads(open(prepared.argv[1], encoding="utf-8").read())
    prepared.finish()
    assert data["runtimeConfig"]["networkProxy"] == url
    assert data["network"]["egress"]["default"] == "deny" and data["network"]["ingress"]["hostLoopback"] == "allow"
    assert "allowedProxyPeer" not in json.dumps(data)
    assert prepared.policy.network == "proxy"


def test_loopback_support_is_probed_once(tmp_path, monkeypatch):
    wxc = tmp_path / "wxc-exec.exe"
    wxc.write_bytes(b"x")
    calls = []

    def report(path):
        calls.append(path)
        return {"probes": {"baseContainerSupportsIdentitylessLoopbackProxy": True}}

    monkeypatch.setattr(windows_mxc, "_report", report)
    assert windows_mxc.loopback_proxy(wxc) and windows_mxc.loopback_proxy(wxc) and len(calls) == 1
    wxc.write_bytes(b"xy")  # A different launcher is probed again.
    monkeypatch.setattr(windows_mxc, "_report", lambda path: {"probes": {}})
    assert not windows_mxc.loopback_proxy(wxc)


def test_the_hint_describes_the_proxy():
    details = {"exit_code": 1, "stdout": "", "stderr": "Tunnel connection failed: 403 Forbidden"}
    details["sandbox"] = {"writable": ["F:/p"], "network": "proxy"}
    assert "代理" in explain(details) and "outside_sandbox" in explain(details)


# execute_command ----------------------------------------------------------------------------------


CLIENT = """
import socket, sys
port = int(sys.argv[1].rsplit(":", 1)[1])
with socket.create_connection(("127.0.0.1", port)) as s:
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\\r\\n\\r\\n")
    print(s.recv(4096).split(b"\\r\\n")[0].decode())
"""


async def test_a_sandboxed_command_reaches_the_network_only_through_its_proxy(tmp_path, monkeypatch):
    box = sandbox.configure(SandboxConfig(mode="on"))
    monkeypatch.setattr(box, "_detect", lambda: Status("full", "mxc"))
    box.network = "proxy"
    seen = []

    def prepare(argv, cwd, env, root, proxy=None):
        seen.append((argv, proxy))
        return Prepared([sys.executable, "-c", CLIENT, proxy], env, box.policy(root))

    monkeypatch.setattr(box, "prepare", prepare)
    reg = create_default_registry(tmp_path, permissions=PermissionPolicy(tmp_path, load=False))
    tool = reg.get("execute_command")
    assert any("MossCode 的代理" in line for line in tool.prompt_guidelines)
    result = await reg.execute(ToolCall("c", "execute_command", json.dumps({"command": "pip install requests"})))
    argv, proxy = seen[0]
    assert proxy.startswith("http://127.0.0.1:")
    assert ("DefaultWebProxy" in " ".join(argv)) == (tool.command_shell.kind == "powershell")
    assert "403" in result.output and "联网：example.com 被拒绝" in result.output
    assert result.details["sandbox"]["network"] == "proxy"
    # Recorded once, with the reason, for /sandbox.
    assert [event for event in box.events if "example.com" in event] == [
        "联网：example.com 被拒绝（此操作需要用户确认；无界面模式不会自动授权。）。"
    ]


async def test_without_the_proxy_commands_stay_offline(tmp_path, monkeypatch):
    box = sandbox.configure(SandboxConfig(mode="on", network="none"))
    monkeypatch.setattr(box, "_detect", lambda: Status("full", "mxc"))
    seen = []

    def prepare(argv, cwd, env, root, proxy=None):
        seen.append((argv, proxy))
        return Prepared([sys.executable, "-c", "print('ok')"], env, box.policy(root))

    monkeypatch.setattr(box, "prepare", prepare)
    reg = create_default_registry(tmp_path, permissions=PermissionPolicy(tmp_path, load=False))
    assert any("网络断开" in line for line in reg.get("execute_command").prompt_guidelines)
    result = await reg.execute(ToolCall("c", "execute_command", json.dumps({"command": "echo"})))
    assert seen[0][1] is None and "DefaultWebProxy" not in " ".join(seen[0][0])
    assert result.details["sandbox"]["network"] == "none"


# The real backend ---------------------------------------------------------------------------------


@functools.cache  # Looked up once: tests replace the home folder, which is one of the places it is found in.
def wxc_exec():
    configured = os.environ.get("MOSSCODE_TEST_WXC")
    return Path(configured).resolve() if configured else windows_mxc.find(None)


@pytest.mark.skipif(not WINDOWS or wxc_exec() is None, reason="needs Windows and wxc-exec.exe")
async def test_mxc_lets_a_command_out_only_through_its_proxy(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    box = sandbox.configure(SandboxConfig(mode="on", wxc_exec=str(wxc_exec())))
    assert box.status().state == "full", box.status().reason
    if box.network != "proxy":
        pytest.skip(box.network_note)
    listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    reg = create_default_registry(root, permissions=PermissionPolicy(root, load=False))
    script = (
        f"try {{ (New-Object Net.Sockets.TcpClient('127.0.0.1', {port})).Close(); 'direct ok' }} "
        "catch { 'direct refused' }; "
        "try { Invoke-WebRequest https://example.com -UseBasicParsing -TimeoutSec 20 | Out-Null; 'proxy ok' } "
        "catch { 'proxy refused: ' + $_.Exception.Message }"
    )
    try:
        result = await reg.execute(ToolCall("c", "execute_command", json.dumps({"command": script})))
    finally:
        listener.close()
    assert "direct refused" in result.output, result.output
    # -p style: nobody to ask, so the proxy refuses the new domain and says so.
    assert "proxy refused" in result.output and "联网：example.com 被拒绝" in result.output, result.output


# The approval card --------------------------------------------------------------------------------


def sandboxed_app(tmp_path, monkeypatch, client):

    box = sandbox.configure(SandboxConfig(mode="on"))
    monkeypatch.setattr(box, "_detect", lambda: Status("full", "mxc"))
    box.network = "proxy"
    monkeypatch.setattr(
        box,
        "prepare",
        lambda argv, cwd, env, root, proxy=None: Prepared([sys.executable, "-c", client, proxy], env, box.policy(root)),
    )
    provider = SequenceProvider(
        response(calls=[call("execute_command", command="pip install requests")]), response("好")
    )
    policy = PermissionPolicy(tmp_path, load=False)
    registry = create_default_registry(tmp_path, permissions=policy)
    return MossCodeApp(Conversation(provider, registry=registry), "Demo"), provider, policy


async def test_the_card_names_the_host_and_escape_refuses_it(tmp_path, monkeypatch):

    app, provider, policy = sandboxed_app(tmp_path, monkeypatch, CLIENT)
    async with app.run_test(size=(100, 32)) as pilot:
        await send(app, pilot, "装依赖")
        await until(lambda: app._approval_request or app._reply_task.done(), message="未显示审批")
        await pilot.pause()
        request = app._approval_request
        assert request is not None and request.operation.network and request.operation.target == "example.com"
        options = app.query_one("#approval-options", OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert "只在这条命令中连接 example.com" in labels[0] and "WebFetch(domain:example.com)" in labels[2]
        await pilot.press("escape")
        await asyncio.wait_for(app._reply_task, 10)
    result = provider.requests[1][0][-1].tool_results[0]
    assert "403" in result.output and "用户拒绝了这个域名" in result.output
    assert not policy.pending and not policy.session


async def test_the_card_goes_away_when_the_command_stops_asking(tmp_path, monkeypatch):

    impatient = (
        CLIENT.replace("    s.sendall", "    s.settimeout(1)\n    s.sendall").replace(
            "    print(s.recv", "    try:\n        print(s.recv"
        )
        + "    except TimeoutError:\n        print('gave up')\n"
    )
    app, provider, policy = sandboxed_app(tmp_path, monkeypatch, impatient)
    async with app.run_test(size=(100, 32)) as pilot:
        await send(app, pilot, "装依赖")
        await until(lambda: app._approval_request or app._reply_task.done(), message="未显示审批")
        await asyncio.wait_for(app._reply_task, 10)
        await until(lambda: app._approval_request is None, message="审批卡片没有撤下")
        assert not app.query_one("#approval-panel").display
    assert "gave up" in provider.requests[1][0][-1].tool_results[0].output and not policy.pending
