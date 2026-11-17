import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from mosscode.messages import ToolCall
from mosscode.tools import create_default_registry, output_text
from mosscode.tools.base import ExecutionCancelled
from mosscode.tools.output_text import DOUBLE_BYTE, OutputDecoder, console_encoding, decode_output
from mosscode.tools.process import Window
from mosscode.tools.shell import Shell, resolve_shell
from waiting import until


def call(name, **arguments):
    return ToolCall("call-test", name, json.dumps(arguments, ensure_ascii=False))


async def test_registry_schema_and_validation(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    assert len(registry.definitions()) == 8
    assert "web_fetch" in registry.names() and "web_search" not in registry.names()
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


def save_image(path, size=(40, 30), fmt="PNG"):
    from PIL import Image

    Image.new("RGB", size, "red").save(path, fmt)
    return path


async def test_read_file_attaches_images_recognized_by_their_header(tmp_path):
    from mosscode.images import size_label
    from mosscode.session import Session

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    registry.use_session(Session(tmp_path / "session"))
    size = save_image(tmp_path / "shot.png").stat().st_size
    result = await registry.execute(call("read_file", path="shot.png", offset=5, limit=1))
    assert not result.is_error and result.output == f"shot.png · 图片 · 40×30 · {size_label(size)}"
    (image,) = result.images
    assert (image.width, image.height, image.alt) == (40, 30, "shot.png") and "_images" not in result.details
    assert image.path == (tmp_path / "session" / "images" / image.name).resolve() and image.path.is_file()
    assert result.details["image"]["name"] == image.name
    save_image(tmp_path / "photo.txt", (3000, 1500), "JPEG")
    big = await registry.execute(call("read_file", path="photo.txt"))
    assert big.images[0].width == 2000 and big.output.endswith("（发送尺寸 2000×1000）")
    assert big.output.startswith("photo.txt · 图片 · 3000×1500 · ")
    save_image(tmp_path / "old.bmp", (5, 5), "BMP")
    assert (await registry.execute(call("read_file", path="old.bmp"))).images[0].media_type == "image/png"


async def test_read_file_image_errors(tmp_path, monkeypatch):
    from mosscode import images

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    broken = await registry.execute(call("read_file", path="broken.png"))
    assert broken.code == "image_invalid" and not broken.images
    save_image(tmp_path / "shot.png")
    registry.images = False
    unsupported = await registry.execute(call("read_file", path="shot.png"))
    assert unsupported.code == "image_unsupported" and "不支持图片" in unsupported.output and not unsupported.images
    registry.images = True
    monkeypatch.setattr(images, "MAX_FILE", 10)
    assert (await registry.execute(call("read_file", path="shot.png"))).code == "image_too_large"


def refuse_replace(monkeypatch, error):
    from mosscode import images

    def refuse(source, target):
        raise error(3, "系统找不到指定的路径。", str(source), None, str(target))

    monkeypatch.setattr(images.os, "replace", refuse)


async def test_read_file_reports_a_failed_image_store_not_a_missing_file(tmp_path, monkeypatch):
    from mosscode.session import Session

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    registry.use_session(Session(tmp_path / "session"))
    save_image(tmp_path / "shot.png")
    refuse_replace(monkeypatch, FileNotFoundError)
    result = await registry.execute(call("read_file", path="shot.png"))
    assert result.code == "image_store_failed" and not result.images
    assert result.output == "图片已读取，但无法保存到会话目录：系统找不到指定的路径。"
    assert list((tmp_path / "session" / "images").iterdir()) == []


async def test_a_failed_image_store_names_the_windows_path_limit(tmp_path, monkeypatch):
    from mosscode import images
    from mosscode.session import Session

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    registry.use_session(Session(tmp_path / "session"))
    save_image(tmp_path / "shot.png")
    monkeypatch.setattr(images, "WINDOWS", True)
    monkeypatch.setattr(images, "WINDOWS_MAX_PATH", len(str(tmp_path)))
    refuse_replace(monkeypatch, FileNotFoundError)
    result = await registry.execute(call("read_file", path="shot.png"))
    assert result.code == "image_store_failed" and "路径超过 Windows 的" in result.output
    assert str(tmp_path / "session" / "images") in result.output


async def test_reading_an_image_outside_the_project_still_needs_approval(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    save_image(tmp_path / "outside.png")
    registry = create_default_registry(root, permission_mode="default", load_permissions=False)
    result = await registry.execute(call("read_file", path="../outside.png"))
    assert result.code == "approval_required" and not result.images


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


@pytest.mark.skipif(not shutil.which("powershell"), reason="Windows PowerShell only")
def test_powershell_errors_point_at_the_command_not_the_prelude(tmp_path):
    import subprocess

    shell = Shell("powershell", shutil.which("powershell"), "PowerShell")
    missing = tmp_path / "no-such-file"
    # PowerShell 7's concise view omits the source-line format this test checks.
    result = subprocess.run(
        shell.args(f"$ErrorView = 'NormalView'\nGet-Item '{missing}'"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 1
    lines = result.stderr.splitlines()
    shown = [
        line
        for line, marker in zip(lines, lines[1:])
        if line.startswith("+ ") and marker.startswith("+ ") and marker[2:].lstrip().startswith("~")
    ]
    assert shown and all("Get-Item" in line and "OutputEncoding" not in line for line in shown), result.stderr


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


# What ping writes on Chinese Windows: the console code page (936), not UTF-8.
GBK_LINE = "来自 127.0.0.1 的回复: 字节=32 时间<1ms TTL=128\r\n"
GBK_BYTES = GBK_LINE.encode("gbk")


def code_page_script(data: bytes, utf8: str = "") -> str:
    """A command writing data as raw bytes, then utf8 encoded as UTF-8."""
    return f"import sys; sys.stdout.buffer.write({data!r} + {utf8.encode()!r}); sys.stdout.flush()"


def test_lines_that_are_not_utf8_decode_in_the_console_code_page():
    own = "[MossCode] 命令结束：退出码 0。\n"
    data = GBK_BYTES * 2 + own.encode() + "中文\n".encode()
    assert decode_output(data, encoding="gbk") == GBK_LINE * 2 + own + "中文\n"
    # Without a code page (systems other than Windows) every line is UTF-8, as before.
    plain = decode_output(data, encoding=None)
    assert "来自" not in plain and "�" in plain and plain.endswith(own + "中文\n")
    # Bytes that fit neither are UTF-8 with replacement rather than code-page garbage.
    assert decode_output(b"ok \xff\xfe\n", encoding="gbk") == "ok ��\n"


def test_a_cut_drops_half_a_code_page_character_instead_of_garbling_the_line():
    text = "正在 Ping 具有 32 字节的数据"
    data = text.encode("gbk")
    for skip in range(1, 8):
        start = decode_output(data[skip:], cut_start=True, encoding="gbk")
        end = decode_output(data[:-skip], cut_end=True, encoding="gbk")
        assert text.endswith(start) and len(text) - len(start) <= skip, (skip, start)
        assert text.startswith(end) and len(text) - len(end) <= skip, (skip, end)


def test_a_streams_chunks_decode_as_its_whole_output_wherever_they_split():
    data = GBK_BYTES * 3 + "[MossCode] 命令已转入后台。\n".encode() + GBK_BYTES
    whole = decode_output(data, encoding="gbk")
    for size in (1, 2, 3, 5, 8192):
        decoder = OutputDecoder("gbk")
        pieces = [decoder.decode(data[i : i + size]) for i in range(0, len(data), size)]
        assert "".join(pieces) + decoder.decode(b"", final=True) == whole, size
    # A line without a break still shows once it is long, and is not held whole.
    decoder = OutputDecoder("gbk")
    assert decoder.decode("中文".encode("gbk") * 3000) == "中文" * 3000 and not decoder.pending


def test_kept_command_output_in_the_code_page_is_readable_on_both_sides_of_the_cut(monkeypatch):
    monkeypatch.setattr(output_text, "console_encoding", lambda: "gbk")
    window = Window(101, 101)  # Odd sizes cut inside characters.
    for _ in range(50):
        window.add(GBK_BYTES)
    text, truncated = window.text()
    head, rest = text.split("\n…（中间省略 ")
    end = rest.split("）…\n", 1)[1]
    assert truncated and "�" not in text
    assert (GBK_LINE * 50).startswith(head) and (GBK_LINE * 50).endswith(end) and "来自" in end


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console code pages")
async def test_a_windows_program_writing_the_console_code_page_reads_as_text(tmp_path):
    encoding = console_encoding()
    if encoding is None:
        pytest.skip("the OEM code page is UTF-8")
    sample = "正在 Ping 具有 32 字节的数据" if encoding in DOUBLE_BYTE else "Résumé, naïve"
    try:
        data = (sample + "\n").encode(encoding)
    except UnicodeEncodeError:
        pytest.skip(f"{encoding} cannot encode the sample")
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    result = await registry.execute(
        call("execute_command", command=python_command(code_page_script(data, "中文\n")), timeout=10)
    )
    assert not result.is_error, result
    assert result.details["stdout"] == sample + "\n中文\n"


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
        await until(lambda: marker.exists() or task.done())
        if not marker.exists():
            pytest.fail(f"Command stopped before creating child: {task.result()}")
        pid = int(marker.read_text())
        assert process_alive(pid)
        if cancel:
            task.cancel()
            with pytest.raises(ExecutionCancelled) as error:
                await task
            assert error.value.result.code == "cancelled"
        else:
            assert (await task).code == "timeout"
        await until(lambda: not process_alive(pid), message=f"child process {pid} still alive")
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def test_edit_file_mixed_line_endings_preserve_untouched_lines_and_dominant_newline(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    mostly_crlf = tmp_path / "mixed_crlf.txt"
    mostly_crlf.write_bytes(b"head\r\nmid1\nmid2\r\ntail\r\n")
    await registry.execute(call("read_file", path="mixed_crlf.txt"))
    res = await registry.execute(
        call("edit_file", path="mixed_crlf.txt", old_text="mid1\nmid2", new_text="new1\nnew2\nnew3")
    )
    assert not res.is_error, res
    # Inserted newlines use dominant CRLF; untouched head/tail CRLFs stay CRLF.
    assert mostly_crlf.read_bytes() == b"head\r\nnew1\r\nnew2\r\nnew3\r\ntail\r\n"

    mostly_lf = tmp_path / "mixed_lf.txt"
    mostly_lf.write_bytes(b"l1\nl2\r\nl3\nl4\n")
    await registry.execute(call("read_file", path="mixed_lf.txt"))
    res_lf = await registry.execute(call("edit_file", path="mixed_lf.txt", old_text="l3\r\nl4", new_text="n3\nn4\nn5"))
    assert not res_lf.is_error, res_lf
    # Inserted newlines use dominant LF while untouched l2\r\n keeps its CRLF.
    assert mostly_lf.read_bytes() == b"l1\nl2\r\nn3\nn4\nn5\n"


async def test_edit_file_trailing_whitespace_and_indentation_fallbacks(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    trailing = tmp_path / "trailing.py"
    trailing.write_text("a = 1   \nb = 2\t\nc = 3\n", encoding="utf-8")
    await registry.execute(call("read_file", path="trailing.py"))
    res = await registry.execute(
        call("edit_file", path="trailing.py", old_text="a = 1\nb = 2", new_text="a = 10\nb = 20")
    )
    assert not res.is_error and trailing.read_text(encoding="utf-8") == "a = 10\nb = 20\nc = 3\n"

    # Multiple trailing-whitespace candidates require replace_all.
    dup = tmp_path / "dup.txt"
    dup.write_text("item  \nitem\t\n", encoding="utf-8")
    await registry.execute(call("read_file", path="dup.txt"))
    many = await registry.execute(call("edit_file", path="dup.txt", old_text="item\n", new_text="fixed\n"))
    assert many.code == "match_count" and many.details["matches"] == 2
    all_done = await registry.execute(
        call("edit_file", path="dup.txt", old_text="item\n", new_text="fixed\n", replace_all=True)
    )
    assert not all_done.is_error and all_done.details["matches"] == 2
    assert dup.read_text(encoding="utf-8") == "fixed\nfixed\n"

    # Uniform positive indentation delta (+4 spaces) with an empty line preserved cleanly.
    indented = tmp_path / "indent.py"
    indented.write_text(
        "class Demo:\n    def run(self):\n        x = 1\n\n        return x\n",
        encoding="utf-8",
    )
    await registry.execute(call("read_file", path="indent.py"))
    pos = await registry.execute(
        call(
            "edit_file",
            path="indent.py",
            old_text="def run(self):\n    x = 1\n\n    return x",
            new_text="def run(self):\n    x = 2\n\n    return x + 1",
        )
    )
    assert not pos.is_error, pos
    assert indented.read_text(encoding="utf-8") == (
        "class Demo:\n    def run(self):\n        x = 2\n\n        return x + 1\n"
    )

    # Uniform negative indentation delta (-4 spaces) when old_text was over-indented.
    neg = await registry.execute(
        call(
            "edit_file",
            path="indent.py",
            old_text="        def run(self):\n            x = 2",
            new_text="        def run(self):\n            x = 3",
        )
    )
    assert not neg.is_error, neg
    assert "    def run(self):\n        x = 3\n" in indented.read_text(encoding="utf-8")

    # Ambiguous indentation-offset candidates fail with match_count and replace_all does not guess indentation.
    ambig = tmp_path / "ambig.py"
    ambig.write_text(
        "def a():\n    x = 1\n    return x\n\ndef b():\n        x = 1\n        return x\n", encoding="utf-8"
    )
    await registry.execute(call("read_file", path="ambig.py"))
    ambig_err = await registry.execute(
        call("edit_file", path="ambig.py", old_text="x = 1\nreturn x\n", new_text="x = 2\nreturn x\n")
    )
    assert ambig_err.code == "match_count" and ambig_err.details["matches"] == 2
    no_all = await registry.execute(
        call("edit_file", path="ambig.py", old_text="x = 1\nreturn x\n", new_text="x = 2\nreturn x\n", replace_all=True)
    )
    assert no_all.code == "match_count" and no_all.details["matches"] == 0


async def test_edit_file_multi_edit_batch_and_atomic_rollback(tmp_path):
    from mosscode.credentials import PLACEHOLDER

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    target = tmp_path / "multi.py"
    original = "alpha = 1\nbeta = 2\ngamma = 3\ndelta = 1\n"
    target.write_text(original, encoding="utf-8")
    await registry.execute(call("read_file", path="multi.py"))

    # Validation errors on missing/conflicting arguments and secret placeholders inside edits.
    for bad in (
        call("edit_file", path="multi.py"),
        call("edit_file", path="multi.py", edits=[]),
        call("edit_file", path="multi.py", old_text="alpha = 1"),
        call(
            "edit_file",
            path="multi.py",
            old_text="alpha = 1",
            new_text="alpha = 2",
            edits=[{"old_text": "beta = 2", "new_text": "beta = 3"}],
        ),
    ):
        assert (await registry.execute(bad)).code == "invalid_arguments"

    secret_err = await registry.execute(
        call(
            "edit_file",
            path="multi.py",
            edits=[{"old_text": "alpha = 1", "new_text": f"alpha = '{PLACEHOLDER}'"}],
        )
    )
    assert secret_err.code == "secret_placeholder"

    # Atomic rollback when a later edit fails: disk stays untouched.
    failed = await registry.execute(
        call(
            "edit_file",
            path="multi.py",
            edits=[
                {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                {"old_text": "missing_line", "new_text": "never"},
            ],
        )
    )
    assert failed.code == "match_count" and target.read_text(encoding="utf-8") == original

    # Successful multi-edit applies sequentially, reports total match count, and previews cleanly.
    tool = registry.get("edit_file")
    params = tool.validate_arguments(
        {
            "path": "multi.py",
            "edits": [
                {"old_text": "= 1", "new_text": "= 10", "replace_all": True},
                {"old_text": "beta = 2", "new_text": "beta = 20\nextra = 25"},
            ],
        }
    )
    preview = tool.approval_preview(params)
    assert "+alpha = 10" in preview and "+extra = 25" in preview
    ok = await registry.execute(
        call(
            "edit_file",
            path="multi.py",
            edits=[
                {"old_text": "= 1", "new_text": "= 10", "replace_all": True},
                {"old_text": "beta = 2", "new_text": "beta = 20\nextra = 25"},
            ],
        )
    )
    assert not ok.is_error and ok.details["matches"] == 3
    assert target.read_text(encoding="utf-8") == "alpha = 10\nbeta = 20\nextra = 25\ngamma = 3\ndelta = 10\n"


async def test_edit_file_appends_syntax_diagnostic_without_rolling_back(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)

    py = tmp_path / "bad.py"
    py.write_text("x = 1\ny = 2\n", encoding="utf-8")
    await registry.execute(call("read_file", path="bad.py"))
    py_broken = await registry.execute(call("edit_file", path="bad.py", old_text="y = 2", new_text="def broken(:"))
    assert not py_broken.is_error
    assert py.read_text(encoding="utf-8") == "x = 1\ndef broken(:\n"
    assert "注意：修改后检测到语法错误（第 2 行：" in py_broken.output
    assert "第 2 行" in py_broken.details["syntax_error"]

    py_fixed = await registry.execute(call("edit_file", path="bad.py", old_text="def broken(:", new_text="y = 3"))
    assert not py_fixed.is_error and "syntax_error" not in py_fixed.details and "语法错误" not in py_fixed.output

    js = tmp_path / "cfg.json"
    js.write_text('{"a": 1}\n', encoding="utf-8")
    await registry.execute(call("read_file", path="cfg.json"))
    js_broken = await registry.execute(call("edit_file", path="cfg.json", old_text="1", new_text="1,"))
    assert not js_broken.is_error and js.read_text(encoding="utf-8") == '{"a": 1,}\n'
    assert "注意：修改后检测到语法错误" in js_broken.output and "syntax_error" in js_broken.details

    sh = tmp_path / "run.sh"
    sh.write_text("echo start\necho done\n", encoding="utf-8")
    await registry.execute(call("read_file", path="run.sh"))
    sh_broken = await registry.execute(
        call("edit_file", path="run.sh", old_text="echo done", new_text="if true; then\n  echo oops")
    )
    assert not sh_broken.is_error and "if true; then" in sh.read_text(encoding="utf-8")
    assert "注意：修改后检测到语法错误" in sh_broken.output and "syntax_error" in sh_broken.details
