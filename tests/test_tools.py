import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from mosscode.messages import ToolCall
from mosscode.tools import create_default_registry
from mosscode.tools.base import ExecutionCancelled
from mosscode.tools.shell import resolve_shell


def call(name, **arguments):
    return ToolCall("call-test", name, json.dumps(arguments, ensure_ascii=False))


async def test_registry_schema_and_validation(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    assert len(registry.definitions()) == 6
    with pytest.raises(ValueError, match="重复"):
        registry.register(registry.get("read_file"))
    cases = [
        (ToolCall("x", "missing", "{}"), "unknown_tool"),
        (ToolCall("x", "write_file", '{"path":'), "invalid_json"),
        (call("write_file", path="a"), "invalid_arguments"),
        (call("read_file", path="a", offset="1"), "invalid_arguments"),
        (call("read_file", path="a", invented=True), "invalid_arguments"),
        (call("read_file", path="missing"), "not_found"),
        (call("edit_file", path="a", old_text="", new_text="x"), "invalid_arguments"),
        (call("execute_command", command="x", timeout=601), "invalid_arguments"),
    ]
    for request, code in cases:
        result = await registry.execute(request)
        assert result.is_error and result.code == code
        assert result.model_text().startswith(f"错误 [{code}]")
    assert not (tmp_path / "a").exists()


async def test_file_create_overwrite_read_page_and_unique_edit(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    path = tmp_path / "nested" / "demo.py"
    assert not (
        await registry.execute(call("write_file", path="nested/demo.py", content="一\r\n二\r\n三\r\n"))
    ).is_error
    page = await registry.execute(call("read_file", path=str(path), offset=2, limit=1))
    assert "     2\t二" in page.output
    assert page.details["next_offset"] == 3
    result = await registry.execute(call("edit_file", path=str(path), old_text="二", new_text="新"))
    assert not result.is_error
    assert path.read_bytes() == "一\r\n新\r\n三\r\n".encode()
    unchanged = path.read_bytes()
    for original, count in [("missing", 0), ("\r\n", 3)]:
        result = await registry.execute(call("edit_file", path=str(path), old_text=original, new_text="x"))
        assert result.code == "match_count" and result.details["matches"] == count
        assert path.read_bytes() == unchanged
    await registry.execute(call("write_file", path=str(path), content="aaaa"))
    overlap = await registry.execute(call("edit_file", path=str(path), old_text="aaa", new_text="x"))
    assert overlap.code == "match_count" and overlap.details["matches"] == 2
    assert path.read_text() == "aaaa"
    assert not list(path.parent.glob(".mosscode-*"))


async def test_paths_outside_root_and_binary_errors(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    registry = create_default_registry(root, permission_mode="bypassPermissions", load_permissions=False)
    result = await registry.execute(call("write_file", path="../outside.txt", content="外部"))
    assert result.code == "path_blocked" and not (tmp_path / "outside.txt").exists()
    (root / "binary").write_bytes(b"\0abc")
    assert (await registry.execute(call("read_file", path="binary"))).code == "binary_file"
    (root / "bad").write_bytes(b"\xff")
    assert (await registry.execute(call("read_file", path="bad"))).code == "encoding_error"
    assert (await registry.execute(call("write_file", path=".", content="x"))).code == "is_directory"


async def test_read_byte_budget_next_page_does_not_skip_unshown_lines(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    (tmp_path / "large.txt").write_text(
        "".join(f"line{i} " + "中" * 1000 + "\n" for i in range(1, 201)), encoding="utf-8"
    )
    page = await registry.execute(call("read_file", path="large.txt"))
    assert page.details["truncated"] and len(page.output.encode("utf-8")) < 65536
    next_offset = page.details["next_offset"]
    assert next_offset < 201
    second = await registry.execute(call("read_file", path="large.txt", offset=next_offset, limit=1))
    assert f"{next_offset:>6}\tline{next_offset} " in second.output


@pytest.mark.parametrize("fallback", [False, True])
async def test_search_patterns_filter_and_empty_results(tmp_path, monkeypatch, fallback):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    if fallback:
        original = shutil.which
        monkeypatch.setattr(shutil, "which", lambda name: None if name == "rg" else original(name))
    (tmp_path / "demo.py").write_text("first\n你好 needle\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("needle", encoding="utf-8")
    for folder in (".git", ".venv", "node_modules", "__pycache__"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "skip.py").write_text("needle", encoding="utf-8")
    result = await registry.execute(call("glob", pattern="**/*.py"))
    assert not result.is_error and result.details["count"] == 1
    assert "demo.py" in result.output and "skip.py" not in result.output
    result = await registry.execute(call("grep", pattern="needle", glob="*.py", output_mode="content"))
    assert not result.is_error and ":2: 你好 needle" in result.output
    assert result.details["count"] == 1
    assert not (await registry.execute(call("grep", pattern="no_match"))).is_error
    assert (await registry.execute(call("grep", pattern="["))).code == "search_failed"


SHELL = resolve_shell()


def python_command(script):
    # Syntax for the shell execute_command resolves to (Git Bash when present on Windows).
    if SHELL.kind == "powershell":
        return "& '" + sys.executable.replace("'", "''") + "' -c '" + script.replace("'", "''") + "'"
    return shlex.join([Path(sys.executable).as_posix(), "-c", script])


async def test_command_success_failure_output_cap_and_timeout(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    request = call(
        "execute_command", command=python_command("import sys; print('中文'); print('err',file=sys.stderr)"), timeout=10
    )
    success = await registry.execute(request)
    assert not success.is_error, success
    assert "中文" in success.output and success.details["stderr"].strip() == "err"
    failure = await registry.execute(
        call("execute_command", command=python_command("import sys; print('oops'); sys.exit(7)"))
    )
    assert failure.code == "nonzero_exit" and failure.details["exit_code"] == 7
    large = await registry.execute(
        call(
            "execute_command",
            command=python_command("import sys; print('a'*100000); print('b'*100000,file=sys.stderr)"),
        )
    )
    assert not large.is_error and large.details["truncated"]
    timeout = await registry.execute(
        call("execute_command", command=python_command("import time; time.sleep(30)"), timeout=0.3)
    )
    assert timeout.code == "timeout"


def process_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        api.CloseHandle(handle)


@pytest.mark.parametrize("cancel", [True, False])
async def test_command_cancel_or_timeout_kills_own_child(tmp_path, cancel):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    marker = tmp_path / "pid.txt"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    task = asyncio.create_task(
        registry.execute(call("execute_command", command=python_command(script), timeout=4 if not cancel else 30))
    )
    try:
        for _ in range(200):
            if marker.exists():
                break
            if task.done():
                pytest.fail(f"Command stopped before creating child: {task.result()}")
            await asyncio.sleep(0.02)
        assert marker.exists()
        pid = int(marker.read_text())
        assert process_alive(pid)
        if cancel:
            task.cancel()
            with pytest.raises(ExecutionCancelled) as error:
                await task
            assert error.value.result.code == "cancelled"
        else:
            assert (await task).code == "timeout"
        for _ in range(100):
            if not process_alive(pid):
                break
            await asyncio.sleep(0.01)
        assert not process_alive(pid)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
