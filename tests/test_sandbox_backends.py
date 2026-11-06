"""The bubblewrap, Seatbelt and restricted-token backends: their arguments, profiles and records everywhere, the
real enforcement where the platform has it, and how one backend is chosen."""

import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mosscode import sandbox
from mosscode.config import SandboxConfig
from mosscode.permissions import PermissionPolicy
from mosscode.sandbox import Prepared, Sandbox, Status, bwrap, explain, linux_exec, policy_for, seatbelt
from mosscode.sandbox import windows_token as token
from mosscode.tools import create_default_registry
from test_sandbox import command

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")


def project(tmp_path):
    """A git project and a home with credentials; the private temp sits in the hidden ~/.mosscode."""
    root = tmp_path / "proj"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".mosscode").mkdir()
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".netrc").write_text("machine x", encoding="utf-8")
    temp = home / ".mosscode" / "tmp" / "t1"
    temp.mkdir(parents=True)
    return root, home, temp


def wide_policy(tmp_path):
    root, home, temp = project(tmp_path)
    return policy_for(root, SandboxConfig(mode="on"), temp, home=home, reads_all=True), home, temp


def position(argv, *items, last=False):
    """Where a run of arguments starts."""
    found = [i for i in range(len(argv)) if argv[i : i + len(items)] == list(items)]
    assert found, items
    return found[-1] if last else found[0]


# bubblewrap ---------------------------------------------------------------------------------------


def test_bubblewrap_mounts_in_an_order_that_keeps_protected_paths_on_top(tmp_path):
    policy, home, temp = wide_policy(tmp_path)
    root = policy.root
    argv = bwrap.command(["echo", "hi"], root, policy, bwrap="bwrap", python="/usr/bin/python3", temp=temp)
    assert argv[0] == "bwrap" and {"--new-session", "--die-with-parent", "--unshare-net"} <= set(argv)
    whole = position(argv, "--ro-bind", "/", "/")
    private_tmp = position(argv, "--bind", str(temp), "/tmp")
    writable = position(argv, "--bind", str(root), str(root))
    hidden = position(argv, "--tmpfs", str(home / ".mosscode"))
    exposed = position(argv, "--bind", str(temp), str(temp), last=True)
    anchor = position(argv, "--bind", str(root / ".git"), str(root / ".git"))
    hooks = position(argv, "--ro-bind", str(root / ".git" / "hooks"), str(root / ".git" / "hooks"))
    assert whole < private_tmp < writable < hidden < exposed < anchor < hooks
    assert position(argv, "--tmpfs", str(home / ".ssh")) < exposed
    assert position(argv, "--ro-bind", "/dev/null", str(home / ".netrc"))
    for name in (".mosscode", ".git/config"):
        assert position(argv, "--ro-bind", str(root / name), str(root / name)) > anchor
    # Protected paths that do not exist yet are not mounted (prepare watches them instead).
    assert str(root / ".claude") not in argv
    assert argv[position(argv, "--chdir") + 1] == str(root)
    assert argv[-9:] == ["--", "/usr/bin/python3", "-I", "-S", "-c", bwrap.LAUNCHER, "exec", "--", "echo", "hi"][-9:]
    bridged = bwrap.command(["echo"], root, policy, bwrap="bwrap", python="py", temp=temp, socket="/s/p.sock")
    assert bridged[-5:] == ["bridge", "/s/p.sock", str(bwrap.BRIDGE_PORT), "--", "echo"]


def test_the_bridge_starts_the_exec_step_as_runnable_code(monkeypatch):
    # The launcher runs with -c, where there is no __file__; the bridge must hand the exec step its own source.
    namespace = {"__name__": "launcher"}
    exec(compile(bwrap.LAUNCHER, "<launcher>", "exec"), namespace)
    assert namespace["SOURCE"] == Path(linux_exec.__file__).read_text(encoding="utf-8")
    started = []

    class Child:
        def __init__(self, argv):
            started.append(argv)

        def wait(self):
            return 0

    namespace["subprocess"] = type("Fake", (), {"Popen": Child})
    namespace["threading"] = type("Fake", (), {"Thread": lambda **kwargs: type("T", (), {"start": lambda s: 0})()})
    with pytest.raises(SystemExit) as stop:
        namespace["bridge"]("unused.sock", 0, ["true"])
    assert stop.value.code == 0
    argv = started[0]
    assert argv[1:4] == ["-I", "-S", "-c"] and argv[-3:] == ["exec", "--", "true"]
    child = {"__name__": "child"}
    exec(compile(argv[4], "<exec step>", "exec"), child)
    assert child["SOURCE"] == namespace["SOURCE"]


def run_filter(program: bytes, nr: int, arch: int, arg0: int = 0) -> int:
    """Classic BPF, the few instructions the filter uses, over a seccomp_data record."""
    data = struct.pack("<iIQ6Q", nr, arch, 0, arg0, 0, 0, 0, 0, 0)
    ops = [struct.unpack("HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    pc = acc = 0
    while True:
        code, jt, jf, k = ops[pc]
        if code == 0x20:
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x15:
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x35:
            pc += 1 + (jt if acc >= k else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")


@pytest.mark.parametrize(
    "machine, arch, socket_nr",
    [("x86_64", 0xC000003E, 41), ("aarch64", 0xC00000B7, 198)],
)
def test_the_seccomp_filter_refuses_unix_sockets_and_other_abis(monkeypatch, machine, arch, socket_nr):
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)  # Windows' socket module has none.
    program = linux_exec.seccomp_program(machine)
    allow, refuse = linux_exec.RET_ALLOW, linux_exec.RET_EPERM
    assert run_filter(program, socket_nr, arch, arg0=1) == refuse  # socket(AF_UNIX, ...)
    assert run_filter(program, socket_nr, arch, arg0=2) == allow  # socket(AF_INET, ...)
    assert run_filter(program, 0, arch) == allow
    assert run_filter(program, linux_exec.IO_URING_SETUP, arch) == refuse
    assert run_filter(program, socket_nr, 0x40000003, arg0=1) == refuse  # 32-bit x86 system calls
    if machine == "x86_64":
        assert run_filter(program, socket_nr | linux_exec.X32_BIT, arch, arg0=1) == refuse


# Seatbelt -----------------------------------------------------------------------------------------


def test_the_seatbelt_profile_denies_after_it_allows(tmp_path):
    policy, home, temp = wide_policy(tmp_path)
    root, q = policy.root, seatbelt._q
    lines = seatbelt.profile(policy, port=41234).splitlines()
    assert lines[:3] == ["(version 1)", "(allow default)", "(deny file-write*)"]
    allow = next(i for i, line in enumerate(lines) if line.startswith("(allow file-write*"))
    assert f"(subpath {q(root)})" in lines[allow] and f"(subpath {q(temp)})" in lines[allow]
    assert '(subpath "/private/tmp")' in lines[allow] and '(literal "/dev/null")' in lines[allow]
    deny = next(i for i, line in enumerate(lines) if line.startswith("(deny file-write* "))
    assert deny > allow  # The later rule wins.
    for name in (".git/hooks", ".git/config", ".mosscode", ".claude"):
        assert f"(subpath {q(root / name)})" in lines[deny]
    # The folder holding protected paths cannot be renamed; the project root itself stays writable.
    assert f"(literal {q(root / '.git')})" in lines[deny] and f"(literal {q(root)})" not in lines[deny]
    reads = lines.index(next(line for line in lines if line.startswith("(deny file-read-data")))
    assert f"(subpath {q(home / '.ssh')})" in lines[reads] and f"(subpath {q(home / '.mosscode')})" in lines[reads]
    assert f"(subpath {q(temp)})" in lines[reads + 1] and lines[reads + 1].startswith("(allow file-read-data")
    assert lines[-2:] == ["(deny network*)", '(allow network-outbound (remote tcp "localhost:41234"))']
    assert seatbelt.profile(policy).splitlines()[-1] == "(deny network*)"
    assert seatbelt.command(["ls"], policy, exe="/usr/bin/sandbox-exec")[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert q('a"b\\c') == '"a\\"b\\\\c"'


# Restricted token: records and entries ------------------------------------------------------------


def test_token_entries_allow_the_roots_and_deny_existing_protected_paths(tmp_path):
    policy, home, temp = wide_policy(tmp_path)
    root = policy.root
    marks = token.entries(policy)
    assert marks[root] == marks[temp] == "write"
    assert {marks[root / ".git" / "hooks"], marks[root / ".git" / "config"], marks[root / ".mosscode"]} == {"protect"}
    assert root / ".claude" not in marks
    assert re.fullmatch(r"S-1-5-21-\d+-\d+-\d+-\d+", token.new_sid())


def test_the_acl_record_survives_damage_and_cleanup_drops_vanished_paths(tmp_path):
    home = tmp_path
    assert token.load(home) == {"version": 1, "roots": {}}
    token.record_path(home).parent.mkdir(parents=True)
    token.record_path(home).write_text("not json", encoding="utf-8")
    assert token.load(home) == {"version": 1, "roots": {}}
    gone = str(tmp_path / "gone")
    data = {"version": 1, "roots": {gone: {"sid": "S-1-5-21-1-2-3-4", "paths": {gone: "write"}}}}
    token.save(data, home)
    assert token.record_path(home) == home / ".mosscode" / "sandbox-acl.json" and token.load(home) == data
    assert token.cleanup(tmp_path / "other", home=home) == [] and token.load(home) == data
    assert token.cleanup(home=home) == [f"已移除：{gone}（1 处）"] and token.load(home)["roots"] == {}


def test_the_sandbox_subcommand_loads_no_interface_or_sdk(tmp_path):
    code = (
        "import sys\nfrom mosscode import launcher\n"
        "try:\n    launcher.main(['sandbox', 'cleanup'])\nexcept SystemExit:\n    pass\n"
        "print([m for m in ('textual', 'anthropic', 'openai', 'mosscode.cli', 'mosscode.tui') if m in sys.modules])"
    )
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]"


@pytest.mark.skipif(not WINDOWS, reason="only the Windows restricted token leaves access-control entries")
def test_sandbox_cleanup_reports_what_it_removed(tmp_path, capsys):
    from mosscode.sandbox_command import main

    gone = tmp_path / "gone"
    token.save({"version": 1, "roots": {str(gone): {"sid": "S-1-5-21-1-2-3-4", "paths": {}}}}, tmp_path)
    assert main(["cleanup", str(tmp_path / "other")], home=tmp_path) == 1
    assert "没有访问控制记录" in capsys.readouterr().out
    assert main(["cleanup", str(gone)], home=tmp_path) == 0
    assert f"已移除：{gone}" in capsys.readouterr().out
    assert main(["cleanup"], home=tmp_path) == 0 and "没有需要清理" in capsys.readouterr().out


@pytest.mark.skipif(WINDOWS, reason="Windows has the restricted token to clean up")
def test_sandbox_cleanup_elsewhere_has_nothing_to_do(capsys):
    from mosscode.sandbox_command import main

    assert main(["cleanup"]) == 0 and "没有需要清理" in capsys.readouterr().out


# Choosing a backend -------------------------------------------------------------------------------


def detect(monkeypatch, platform, backend="auto", **results):
    monkeypatch.setattr(sys, "platform", platform)
    box = Sandbox(SandboxConfig(mode="on", backend=backend))
    box._open = []  # No ACL scan in these tests.
    for name in ("bubblewrap", "seatbelt", "mxc", "token"):
        result = results.get(name)

        def fake(result=result, name=name):
            assert result is not None, f"{name} should not be tried"
            return result

        monkeypatch.setattr(box, f"_detect_{name}", fake)
    return box


MXC_MISSING = Status("unavailable", "mxc", "找不到 MXC 的 wxc-exec.exe。")


def test_windows_falls_back_to_the_restricted_token_and_says_so(monkeypatch):
    box = detect(monkeypatch, "win32", mxc=MXC_MISSING, token=Status("partial", "token"))
    assert box.status() == Status("partial", "token")
    assert box.active and not box.full and box.badge() == "沙箱·部分"
    assert "MXC 不可用（找不到 MXC 的 wxc-exec.exe）" in box.fallback_note and "只限制写入" in box.fallback_note
    assert box.notices() == ["沙箱：" + box.fallback_note]


def test_windows_prefers_mxc_and_reports_it_when_nothing_works(monkeypatch):
    box = detect(monkeypatch, "win32", mxc=Status("full", "mxc"))
    assert box.status().backend == "mxc" and not box.fallback_note
    broken = Status("unavailable", "token", "自检失败")
    assert detect(monkeypatch, "win32", mxc=MXC_MISSING, token=broken).status() == MXC_MISSING


def test_a_named_backend_is_used_alone(monkeypatch):
    assert detect(monkeypatch, "win32", "token", token=Status("partial", "token")).status().backend == "token"
    box = detect(monkeypatch, "win32", "mxc", mxc=MXC_MISSING)
    assert box.status() == MXC_MISSING and not box.fallback_note
    status = detect(monkeypatch, "linux", "seatbelt").status()
    assert status.state == "unavailable" and "sandbox.backend 为 seatbelt" in status.reason
    assert detect(monkeypatch, "linux", bubblewrap=Status("full", "bubblewrap")).status().backend == "bubblewrap"
    assert detect(monkeypatch, "darwin", seatbelt=Status("full", "seatbelt")).status().backend == "seatbelt"
    assert "没有可用的沙箱后端" in detect(monkeypatch, "freebsd14").status().reason


def test_the_partial_level_is_described_as_writes_only(tmp_path, monkeypatch):
    box = detect(monkeypatch, "win32", "token", token=Status("partial", "token"))
    box.network = "open"
    report = "\n".join(box.report(tmp_path))
    assert "受限令牌，只限制写入" in report and "禁止读取：不支持" in report and "网络：不受沙箱限制" in report
    details = {"exit_code": 1, "stderr": "Access is denied", "sandbox": {"network": "open", "backend": "token"}}
    hint = explain(details)
    assert "网络不受沙箱限制" in hint and "读取不受沙箱限制" in hint


@pytest.mark.parametrize("state, asked", [("partial", True), ("full", False)])
async def test_only_the_full_level_skips_approval(tmp_path, monkeypatch, state, asked):
    box = sandbox.configure(SandboxConfig(mode="on"))
    # MXC and the token switch Windows to PowerShell; elsewhere any backend name will do.
    backend = ("token" if state == "partial" else "mxc") if WINDOWS else "bubblewrap"
    monkeypatch.setattr(box, "_detect", lambda: Status(state, backend))
    monkeypatch.setattr(
        box,
        "prepare",
        lambda argv, cwd, env, root, proxy=None: Prepared(
            [sys.executable, "-c", "print('inside')"], env, box.policy(root)
        ),
    )
    reg = create_default_registry(tmp_path, permissions=PermissionPolicy(tmp_path, mode="default", load=False))
    result = await reg.execute(command("python -m pytest -q"))
    assert (result.code == "approval_required") is asked
    tool = reg.get("execute_command")
    assert any("只限制写入" in line for line in tool.prompt_guidelines) is asked
    if WINDOWS:  # Git Bash cannot start under a restricted token either.
        assert tool.command_shell.kind == "powershell"


# The real backends --------------------------------------------------------------------------------


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))


def bwrap_usable() -> bool:
    found = bwrap.find() if LINUX else None
    return bool(found) and not bwrap.check(found)


needs_bwrap = pytest.mark.skipif(not bwrap_usable(), reason="needs Linux with bubblewrap and user namespaces")


@pytest.fixture(scope="module")
def linux_box(tmp_path_factory):
    base = tmp_path_factory.mktemp("bwrap")
    root, home, temp = project(base)
    (home / ".ssh" / "id_test").write_text("SECRET-MARKER\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    outside = base / "outside"
    outside.mkdir()
    policy = policy_for(root, SandboxConfig(mode="on"), temp, home=home, reads_all=True)
    yield policy, home, temp, outside
    remove_tree(base)


def run_in_bwrap(box, argv, socket_path=None):
    policy, home, temp, _ = box
    wrapped = bwrap.command(
        argv, policy.root, policy, bwrap=bwrap.find(), python=sandbox.safe_python(), temp=temp, socket=socket_path
    )
    env = {**os.environ, "HOME": str(home), "http_proxy": f"http://127.0.0.1:{bwrap.BRIDGE_PORT}"}
    return subprocess.run(wrapped, capture_output=True, text=True, env=env, timeout=60)


LINUX_CASES = [
    ("write inside", "echo a > a.txt", True),
    ("write the private temp under the hidden ~/.mosscode", "echo a > {temp}/b.txt", True),
    ("write outside", "echo a > {outside}/b.txt", False),
    ("write a protected file", "echo a >> .git/config", False),
    ("create a hook", "echo a > .git/hooks/pre-commit", False),
    ("write through ..", "echo a >> .mosscode/../.git/config", False),
    ("delete a protected file", "rm .git/config", False),
    ("delete and recreate", "rm -rf .mosscode && mkdir .mosscode", False),
    ("rename the parent of protected paths", "mv .git g2", False),
    ("rename the protected folder", "mv .git/hooks .git/h2", False),
    ("hardlink a protected file", "ln .git/config cfg", False),
    ("write through a symlink", "ln -sf .git/config sl && echo x >> sl", False),
    ("unmount a protected file", "umount .git/config", False),
    ("read a credential", "cat {home}/.ssh/id_test", False),
    ("open a unix socket", "python3 -c 'import socket; socket.socket(socket.AF_UNIX)'", False),
    ("connect directly", "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 80), 3)'", False),
    ("commit", "git add a.txt && git -c user.name=t -c user.email=t@t commit -qm m", True),
]


@needs_bwrap
@pytest.mark.parametrize("label, script, allowed", LINUX_CASES, ids=[case[0] for case in LINUX_CASES])
def test_bubblewrap_enforces_the_boundary(linux_box, label, script, allowed):
    policy, home, temp, outside = linux_box
    done = run_in_bwrap(linux_box, ["sh", "-c", script.format(temp=temp, outside=outside, home=home)])
    assert (done.returncode == 0) is allowed, done.stderr
    assert "SECRET-MARKER" not in done.stdout


@needs_bwrap
def test_bubblewrap_reaches_the_proxy_only_through_the_bridge(linux_box):
    _, _, temp, _ = linux_box
    path = temp / "proxy-test.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nbridged")
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    fetch = "import os, urllib.request; print(urllib.request.urlopen('http://example.com/', timeout=5).read().decode())"
    try:
        done = run_in_bwrap(linux_box, ["python3", "-c", fetch], socket_path=str(path))
    finally:
        server.close()
        path.unlink(missing_ok=True)
    assert done.returncode == 0 and done.stdout.strip() == "bridged", done.stderr


def token_usable() -> bool:
    return WINDOWS and not token.check()


needs_token = pytest.mark.skipif(not token_usable(), reason="needs Windows where a write-restricted token runs")


@pytest.fixture(scope="module")
def token_box(tmp_path_factory):
    base = tmp_path_factory.mktemp("token")
    root, home, temp = project(base)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    outside = base / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k", encoding="utf-8")
    policy = policy_for(root, SandboxConfig(mode="on"), temp, home=home)
    python = sandbox.safe_python(policy.writable)
    prefix = token.command(["x"], root, policy, python=python, grants=token.Grants(home=home))[:-1]
    yield prefix, policy, python, outside, home
    lines = token.cleanup(home=home)
    assert lines and all(line.startswith("已移除") for line in lines)
    remove_tree(base)


def py(python, code):
    return [python, "-I", "-S", "-c", code]


def token_cases():
    """(label, argv builder, allowed); builders take (python, root, outside)."""
    git = lambda r: r / ".git"  # noqa: E731
    return [
        ("write inside", lambda p, r, o: py(p, f"open(r'{r / 'a.txt'}', 'w').write('a')"), True),
        ("write outside", lambda p, r, o: py(p, f"open(r'{o / 'b.txt'}', 'w').write('a')"), False),
        ("rename outside", lambda p, r, o: py(p, f"import os; os.rename(r'{o / 'keep.txt'}', r'{o / 'k2'}')"), False),
        ("write a protected file", lambda p, r, o: py(p, f"open(r'{git(r) / 'config'}', 'a').write('a')"), False),
        ("create in a protected folder", lambda p, r, o: py(p, f"open(r'{r / '.mosscode' / 'y'}', 'w')"), False),
        ("create a hook", lambda p, r, o: py(p, f"open(r'{git(r) / 'hooks' / 'pre-commit'}', 'w')"), False),
        ("case variant", lambda p, r, o: py(p, f"open(r'{r / '.GIT' / 'CONFIG'}', 'a').write('a')"), False),
        ("through ..", lambda p, r, o: py(p, f"open(r'{r / '.mosscode' / '..' / '.git' / 'config'}', 'a')"), False),
        ("8.3 short name", lambda p, r, o: ["cmd", "/c", "for %I in (.git\\config) do @echo x>> %~sI"], False),
        ("delete a protected file", lambda p, r, o: py(p, f"import os; os.remove(r'{git(r) / 'config'}')"), False),
        ("delete and recreate", lambda p, r, o: py(p, f"import shutil; shutil.rmtree(r'{r / '.mosscode'}')"), False),
        (
            "rename hooks",
            lambda p, r, o: py(p, f"import os; os.rename(r'{git(r) / 'hooks'}', r'{git(r) / 'h2'}')"),
            False,
        ),
        ("rename the parent", lambda p, r, o: py(p, f"import os; os.rename(r'{git(r)}', r'{r / 'g2'}')"), False),
        ("rename the root", lambda p, r, o: py(p, f"import os; os.rename(r'{r}', r'{r}-moved')"), False),
        ("hardlink", lambda p, r, o: py(p, f"import os; os.link(r'{git(r) / 'config'}', r'{r / 'cfg'}')"), False),
        (
            "symlink",
            lambda p, r, o: py(
                p, f"import os; os.symlink(r'{git(r) / 'config'}', r'{r / 'sl'}'); open(r'{r / 'sl'}', 'a').write('x')"
            ),
            False,
        ),
        (
            "junction outside",
            lambda p, r, o: ["cmd", "/c", f"mklink /J {r / 'j'} {o} && echo x> {r / 'j' / 'f.txt'}"],
            False,
        ),
        (
            "junction to hooks",
            lambda p, r, o: [
                "cmd",
                "/c",
                f"mklink /J {r / 'jh'} {git(r) / 'hooks'} && echo x> {r / 'jh' / 'pre-push'}",
            ],
            False,
        ),
        ("grant itself access", lambda p, r, o: ["icacls", str(git(r) / "config"), "/grant", "*S-1-1-0:F"], False),
        (
            "powershell writes inside",
            lambda p, r, o: ["powershell", "-NoProfile", "-Command", f"Set-Content -Path '{r / 'ps.txt'}' -Value hi"],
            True,
        ),
        (
            "commit",
            lambda p, r, o: ["cmd", "/c", "git add -A && git -c commit.gpgsign=false commit -qm m"],
            True,
        ),
    ]


@needs_token
@pytest.mark.parametrize("label, build, allowed", token_cases(), ids=[case[0] for case in token_cases()])
def test_the_restricted_token_enforces_the_boundary(token_box, label, build, allowed):
    prefix, policy, python, outside, _ = token_box
    done = subprocess.run(
        [*prefix, *build(python, policy.root, outside)], cwd=policy.root, capture_output=True, timeout=120
    )
    assert (done.returncode == 0) is allowed, done.stderr.decode("utf-8", "replace")
    assert (policy.root / ".git" / "config").exists() and (policy.root / ".mosscode").is_dir()


@needs_token
def test_the_capability_sid_is_on_the_roots_until_cleanup(token_box):
    prefix, policy, _, _, home = token_box
    sid = prefix[prefix.index("exec") + 1]
    assert token.has_ace(policy.root, sid) and token.has_ace(policy.root / ".git" / "config", sid)
    assert token.load(home)["roots"][str(policy.root)]["sid"] == sid


def test_the_approval_panel_says_the_sandbox_only_limits_writes(monkeypatch):
    from types import SimpleNamespace

    from mosscode.tui import _writes_only

    box = sandbox.configure(SandboxConfig(mode="on"))
    monkeypatch.setattr(box, "_detect", lambda: Status("partial", "token"))
    run = SimpleNamespace(tool="execute_command", unsandboxed=False)
    assert _writes_only(run) and _writes_only(SimpleNamespace(tool="skill__lint", unsandboxed=False))
    assert not _writes_only(SimpleNamespace(tool="execute_command", unsandboxed=True))
    assert not _writes_only(SimpleNamespace(tool="write_file", unsandboxed=False))
    box.set_enabled(False)
    assert not _writes_only(run)
