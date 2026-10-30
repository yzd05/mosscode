"""Acceptance tests for refinement 02 (docs/specs/refinement-02-tools/spec.md, AC1–AC8)."""

import os
from pathlib import Path
import shutil

import pytest
import yaml
from pydantic import ValidationError

from mosscode.config import ConfigError, load_settings
from mosscode.permissions import PermissionConfigError, PermissionPolicy, parse_rule
from mosscode.prompts import environment
from mosscode.tools import create_default_registry, shell_label
from mosscode.tools.process import CommandParams
from mosscode.tools.shell import ShellNotFound, _is_wsl, git_bash, resolve_shell
from test_permissions import approve
from test_tools import SHELL, call, python_command


BS = chr(92)  # A literal backslash, kept out of escape-processing paths.


def registry(root, mode="bypassPermissions", **options):
    return create_default_registry(root, permission_mode=mode, load_permissions=False, **options)


def policy_registry(root, mode="default", *, load=False, user=None):
    policy = PermissionPolicy(root, mode=mode, load=load, user_path=user)
    return create_default_registry(root, permissions=policy)


def no_rg(monkeypatch):
    original = shutil.which
    monkeypatch.setattr("mosscode.tools.search.shutil.which", lambda name: None if name == "rg" else original(name))


# AC1 read_file output


async def test_read_line_numbers_defaults_and_notes(tmp_path):
    reg = registry(tmp_path)
    (tmp_path / "a.txt").write_text("一\n二\n", encoding="utf-8")
    page = await reg.execute(call("read_file", path="a.txt"))
    assert page.output == "     1\t一\n     2\t二"
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert (await reg.execute(call("read_file", path="empty.txt"))).output == "（文件为空）"
    beyond = await reg.execute(call("read_file", path="a.txt", offset=5))
    assert not beyond.is_error and "超出文件末尾" in beyond.output and "共 2 行" in beyond.output
    (tmp_path / "long.txt").write_text("x" * 2500 + "\nshort\n", encoding="utf-8")
    long = await reg.execute(call("read_file", path="long.txt"))
    assert "本行过长" in long.output and long.details["long_lines"] == 1 and "     2\tshort" in long.output
    (tmp_path / "many.txt").write_text("".join(f"{i}\n" for i in range(1, 2501)), encoding="utf-8")
    many = await reg.execute(call("read_file", path="many.txt"))
    assert many.details["lines"] == 2000 and many.details["total_lines"] == 2500
    assert many.details["next_offset"] == 2001 and "offset=2001" in many.output


# AC2 reads outside the project


async def test_outside_read_asks_and_writes_stay_blocked(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "notes.txt"
    outside.write_text("外部内容", encoding="utf-8")
    reg = policy_registry(root)
    request = call("read_file", path=str(outside))
    assert (await reg.execute(request)).code == "approval_required"
    result, pending = await approve(reg, request, "once")
    assert "外部内容" in result.output
    assert pending.operation.outside and pending.operation.target == outside.resolve().as_posix()
    assert "项目目录之外" in pending.operation.preview and "确认" in pending.reason
    for denied in (call("write_file", path=str(tmp_path / "new.txt"), content="x"),
                   call("edit_file", path=str(outside), old_text="外部", new_text="x")):
        assert (await reg.execute(denied)).code == "path_blocked"
    assert outside.read_text(encoding="utf-8") == "外部内容" and not (tmp_path / "new.txt").exists()
    # bypassPermissions reads directly; relative allow rules never cover outside paths.
    assert "外部内容" in (await registry(root).execute(request)).output
    relaxed = policy_registry(root)
    relaxed.permissions.layers[2] = (parse_rule("Read(**)", "allow"),)
    assert (await relaxed.execute(request)).code == "approval_required"


async def test_outside_read_permanent_rule_reloads(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "docs").mkdir()
    outside = tmp_path / "docs" / "guide.md"
    outside.write_text("guide", encoding="utf-8")
    reg = policy_registry(root)
    result, _ = await approve(reg, call("read_file", path=str(outside)), "permanent")
    assert result.output == "     1\tguide"
    data = yaml.safe_load(reg.permissions.local_file.read_text(encoding="utf-8"))
    assert data["allow"] == [{"rule": f"read_file({outside.resolve().as_posix()})", "exact": True}]
    restored = policy_registry(root, load=True, user=tmp_path / "absent-user.yaml")
    assert (await restored.execute(call("read_file", path=str(outside)))).output == "     1\tguide"
    assert (await restored.execute(call("grep", pattern="guide", path=str(outside.parent)))).code == "approval_required"
    pattern = outside.parent.resolve().as_posix() + "/**"
    assert parse_rule(f"Grep({pattern})", "allow").pattern == pattern
    with pytest.raises(PermissionConfigError):
        parse_rule(f"Write({pattern})", "allow")
    with pytest.raises(PermissionConfigError):
        parse_rule("Read(../secret)", "allow")


# AC3 / AC4 / AC5 editing


async def test_replace_all_hint_and_crlf_multiline(tmp_path):
    reg = registry(tmp_path)
    path = tmp_path / "names.py"
    path.write_text("old = 1\nprint(old)\nold += 1\n", encoding="utf-8")
    await reg.execute(call("read_file", path="names.py"))
    many = await reg.execute(call("edit_file", path="names.py", old_text="old", new_text="new"))
    assert many.code == "match_count" and many.details["matches"] == 3 and "replace_all" in many.output
    done = await reg.execute(call("edit_file", path="names.py", old_text="old", new_text="value", replace_all=True))
    assert not done.is_error and done.details["matches"] == 3 and "第 1–3 行" in done.output
    assert path.read_text(encoding="utf-8") == "value = 1\nprint(value)\nvalue += 1\n"
    crlf = tmp_path / "win.txt"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    await reg.execute(call("read_file", path="win.txt"))
    edited = await reg.execute(call("edit_file", path="win.txt", old_text="a\nb", new_text="x\ny\nz"))
    assert not edited.is_error, edited
    assert crlf.read_bytes() == b"x\r\ny\r\nz\r\nc\r\n"


async def test_read_before_modify_and_stale_detection(tmp_path):
    reg = registry(tmp_path)
    path = tmp_path / "code.py"
    path.write_text("a = 1\n", encoding="utf-8")
    edit = call("edit_file", path="code.py", old_text="a = 1", new_text="a = 2")
    assert (await reg.execute(edit)).code == "not_read"
    assert (await reg.execute(call("write_file", path="code.py", content="x"))).code == "not_read"
    await reg.execute(call("read_file", path="code.py"))
    path.write_text("a = 1\nb = 2\n", encoding="utf-8")  # External change after the read.
    assert (await reg.execute(edit)).code == "stale_read"
    assert path.read_text(encoding="utf-8") == "a = 1\nb = 2\n"
    await reg.execute(call("read_file", path="code.py"))
    assert not (await reg.execute(edit)).is_error
    # Its own writes keep the record current, so consecutive edits work.
    assert not (await reg.execute(call("edit_file", path="code.py", old_text="b = 2", new_text="b = 3"))).is_error
    assert not (await reg.execute(call("write_file", path="fresh.py", content="new\n"))).is_error
    assert path.read_text(encoding="utf-8") == "a = 2\nb = 3\n"


async def test_external_append_in_another_encoding_reports_stale_read_first(tmp_path):
    # Windows PowerShell 5.1 Add-Content appends in the ANSI code page (GBK on Chinese systems).
    reg = registry(tmp_path)
    path = tmp_path / "notes.txt"
    path.write_text("笔记\n第二条记录\n", encoding="utf-8")
    await reg.execute(call("read_file", path="notes.txt"))
    with path.open("ab") as stream:
        stream.write("外部追加\r\n".encode("gbk"))
    before = path.read_bytes()
    edited = await reg.execute(call("edit_file", path="notes.txt", old_text="笔记", new_text="标题"))
    assert edited.code == "stale_read" and path.read_bytes() == before
    assert (await reg.execute(call("read_file", path="notes.txt"))).code == "encoding_error"


async def test_edit_summary_keeps_diff_local(tmp_path):
    reg = registry(tmp_path)
    (tmp_path / "a.txt").write_text("一\n二\n三\n", encoding="utf-8")
    await reg.execute(call("read_file", path="a.txt"))
    result = await reg.execute(call("edit_file", path="a.txt", old_text="二", new_text="新"))
    assert result.output == "已修改 a.txt：替换 1 处，第 2 行。"
    assert "-二" in result.details["diff"] and "+新" in result.details["diff"]
    assert "+新" not in result.model_text() and "@@" not in result.model_text()
    overwrite = await reg.execute(call("write_file", path="a.txt", content="一\n"))
    assert overwrite.output == "已覆盖 a.txt：3 行 → 1 行。" and "-三" in overwrite.details["diff"]


# AC6 command output and timeout


async def test_command_keeps_head_and_tail(tmp_path):
    reg = registry(tmp_path)
    script = "import sys; sys.stdout.write('HEAD' + 'x' * 100000 + 'TAIL')"
    result = await reg.execute(call("execute_command", command=python_command(script), timeout=30))
    # Without a session directory, layer 1 (chapter 8) keeps a head-and-tail preview.
    assert not result.is_error and result.details["truncated"]
    assert "\nHEAD" in result.output and result.output.rstrip().endswith("TAIL")
    assert "中间省略" in result.output and len(result.output.encode("utf-8")) < 40000


def test_command_timeout_bounds():
    assert CommandParams(command="x").timeout == 120
    assert CommandParams(command="x", timeout=600).timeout == 600
    with pytest.raises(ValidationError):
        CommandParams(command="x", timeout=601)


# AC7 shell selection


@pytest.mark.skipif(SHELL.kind == "powershell", reason="需要 bash")
async def test_bash_syntax_exit_code_utf8_and_backslashes(tmp_path):
    reg = registry(tmp_path)
    result = await reg.execute(call("execute_command", command="echo $((1+2)); echo 中文; exit 4", timeout=30))
    assert result.code == "nonzero_exit" and result.details["exit_code"] == 4
    assert "3" in result.output and "中文" in result.output
    # MSYS re-parses its command line; the command must reach bash unchanged.
    text = "a" + BS * 2 + "b"
    echoed = await reg.execute(call("execute_command", command=f"printf '%s' '{text}'", timeout=30))
    assert echoed.output.strip() == text


@pytest.mark.skipif(os.name != "nt", reason="Windows shell 选择")
def test_windows_auto_prefers_git_bash():
    found = git_bash()
    if found is None:
        pytest.skip("本机没有 Git Bash")
    shell = resolve_shell("auto")
    assert shell.kind == "bash" and shell.label == "Git Bash" and Path(shell.path) == found


def test_explicit_shell_choices(tmp_path, monkeypatch):
    with pytest.raises(ShellNotFound):
        resolve_shell(str(tmp_path / "missing" / "bash.exe"))
    if os.name == "nt":
        system = tmp_path / "Windows"
        (system / "System32").mkdir(parents=True)
        fake = system / "System32" / "bash.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("SystemRoot", str(system))
        assert _is_wsl(fake)
        with pytest.raises(ShellNotFound, match="WSL"):
            resolve_shell(str(fake))


@pytest.mark.skipif(shutil.which("pwsh") is None and shutil.which("powershell") is None, reason="需要 PowerShell")
async def test_configured_powershell_runs_commands(tmp_path):
    reg = registry(tmp_path, shell="powershell")
    assert shell_label(reg) == "PowerShell"
    result = await reg.execute(call("execute_command", command="Write-Output ('ok' + (1 + 2))", timeout=60))
    assert not result.is_error and result.output.strip() == "ok3"


def test_shell_label_reaches_prompt_and_tool_description(tmp_path):
    reg = registry(tmp_path)
    label = shell_label(reg)
    assert label == SHELL.label
    assert f"在 {label} 中执行命令" in reg.get("execute_command").description
    assert f'"shell": "{label}"' in environment(tmp_path, tools_enabled=True, shell=label).text


def test_config_shell_key(tmp_path):
    path = tmp_path / "config.yaml"
    base = "providers:\n- name: test\n  protocol: openai\n  model: x\n"
    path.write_text(base, encoding="utf-8")
    assert load_settings(path).shell == "auto"
    path.write_text(base + "shell: powershell\n", encoding="utf-8")
    assert load_settings(path).shell == "powershell"
    path.write_text(base + "shell: 3\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


# AC8 search


def ignore_tree(root):
    (root / ".gitignore").write_text("build/\n*.log\n!keep.log\n", encoding="utf-8")
    for name, text in {"build/out.py": "needle", "x.log": "needle", "keep.log": "needle",
                       "src/main.py": "first\nNeedle one\nneedle two\nlast\n", "src/gen.py": "needle",
                       "src/.gitignore": "gen.py\n"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


async def test_fallback_respects_gitignore(tmp_path, monkeypatch):
    no_rg(monkeypatch)
    ignore_tree(tmp_path)
    reg = registry(tmp_path)
    found = await reg.execute(call("grep", pattern="needle"))
    assert sorted(found.output.splitlines()) == ["keep.log", "src/main.py"]
    files = await reg.execute(call("glob", pattern="**/*"))
    assert "build/out.py" not in files.output and "x.log" not in files.output and "src/gen.py" not in files.output
    explicit = await reg.execute(call("grep", pattern="needle", path="build"))
    assert explicit.output == "build/out.py"
    nested = await reg.execute(call("grep", pattern="needle", path="src"))
    assert nested.output == "src/main.py"


async def test_grep_output_modes(tmp_path, monkeypatch):
    no_rg(monkeypatch)
    ignore_tree(tmp_path)
    reg = registry(tmp_path)
    content = await reg.execute(call("grep", pattern="needle", path="src", output_mode="content"))
    assert content.output == "src/main.py:3: needle two"
    folded = await reg.execute(call("grep", pattern="needle", path="src", output_mode="content",
                                    ignore_case=True, context=1))
    assert folded.output.splitlines() == ["src/main.py-1- first", "src/main.py:2: Needle one",
                                          "src/main.py:3: needle two", "src/main.py-4- last"]
    assert folded.details["matches"] == 2
    count = await reg.execute(call("grep", pattern="needle", output_mode="count", ignore_case=True))
    assert sorted(count.output.splitlines()) == ["keep.log:1", "src/main.py:2"]
    limited = await reg.execute(call("grep", pattern="needle", ignore_case=True, head_limit=1))
    assert limited.details["truncated"] and len(limited.output.splitlines()) == 2
    assert "head_limit" in limited.output


async def test_glob_and_grep_list_newest_first(tmp_path, monkeypatch):
    no_rg(monkeypatch)
    for index, name in enumerate(["old.txt", "mid.txt", "new.txt"]):
        path = tmp_path / name
        path.write_text("needle", encoding="utf-8")
        os.utime(path, (1_600_000_000 + index * 1000,) * 2)
    reg = registry(tmp_path)
    assert (await reg.execute(call("glob", pattern="*.txt"))).output.splitlines() == ["new.txt", "mid.txt", "old.txt"]
    assert (await reg.execute(call("grep", pattern="needle"))).output.splitlines() == ["new.txt", "mid.txt", "old.txt"]


def test_fallback_brace_expansion():
    from mosscode.tools.search_worker import expand_braces
    assert expand_braces("{src,docs}/**/*") == ["src/**/*", "docs/**/*"]
    assert expand_braces("*.{py,md}") == ["*.py", "*.md"]
    assert expand_braces("a{b,c{d,e}}f") == ["abf", "acdf", "acef"]
    assert expand_braces("{,a}b") == ["b", "ab"]
    # Unbalanced or comma-free braces stay literal; later pairs still expand.
    assert expand_braces("{a,b") == ["{a,b"] and expand_braces("a}") == ["a}"]
    assert expand_braces("{a{b,c}") == ["{ab", "{ac"] and expand_braces("x{y}z") == ["x{y}z"]


async def test_fallback_glob_and_grep_expand_braces(tmp_path, monkeypatch):
    no_rg(monkeypatch)
    for name in ["src/a.py", "src/deep/b.md", "docs/c.md", "other/d.py", "e.txt", "{f}.txt"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle", encoding="utf-8")
    reg = registry(tmp_path)

    async def glob(pattern):
        return sorted((await reg.execute(call("glob", pattern=pattern))).output.splitlines())

    assert await glob("{src,docs}/**/*") == ["docs/c.md", "src/a.py", "src/deep/b.md"]
    assert await glob("*.{py,md}") == ["docs/c.md", "other/d.py", "src/a.py", "src/deep/b.md"]
    assert await glob("{src/**/*.{py,md},e.txt}") == ["e.txt", "src/a.py", "src/deep/b.md"]
    assert await glob("{f}.txt") == ["{f}.txt"]
    found = await reg.execute(call("grep", pattern="needle", glob="*.{md,txt}"))
    assert sorted(found.output.splitlines()) == ["docs/c.md", "e.txt", "src/deep/b.md", "{f}.txt"]
