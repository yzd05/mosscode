"""The command sandbox: policy, backend requests, approvals, fail-closed and, on Windows with MXC, the real thing."""

import asyncio
import functools
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from mosscode import sandbox
from mosscode.config import ConfigError, SandboxConfig, find_config, load_settings, project_notice
from mosscode.messages import ToolCall
from mosscode.permissions import UNSANDBOXED_CHOICES, PermissionPolicy, parse_rule
from mosscode.sandbox import Policy, Prepared, Status, explain, policy_for, readable_roots, startup_failure, windows_mxc
from mosscode.tools import create_default_registry
from mosscode.tools.base import ToolFailure
from mosscode.tools.process import run_process

WINDOWS = sys.platform == "win32"


def write_config(path, **sections):
    provider = {"name": "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "MOSS_TEST_PROVIDER"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"providers": [provider], **sections}), encoding="utf-8")
    return path


def settings(tmp_path, **sandbox_block):
    return load_settings(write_config(tmp_path / "config.yaml", sandbox=sandbox_block))


def turn_on(monkeypatch, state="full", **config):
    """A sandbox that reports the given state without running a backend."""
    box = sandbox.configure(SandboxConfig(mode="on", **config))
    reason = "" if state == "full" else "测试：后端不可用。"
    monkeypatch.setattr(box, "_detect", lambda: Status(state, "mxc", reason))
    return box


def registry(root, mode="default"):
    return create_default_registry(root, permissions=PermissionPolicy(root, mode=mode, load=False))


def command(text, **extra):
    return ToolCall("c", "execute_command", json.dumps({"command": text, **extra}))


async def approve(reg, request, choice):
    reg.permissions.interactive = True
    task = asyncio.create_task(reg.execute(request))
    pending = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    assert reg.permissions.respond(pending.id, choice)
    return await task, pending


def fake_backend(monkeypatch, box, output="inside"):
    """Commands 'run in the sandbox' as a plain Python process that prints a marker."""
    calls = []

    def prepare(argv, cwd, env, root, proxy=None):
        calls.append(argv)
        policy = box.policy(root)
        return Prepared([sys.executable, "-c", f"print({output!r})"], env, policy)

    monkeypatch.setattr(box, "prepare", prepare)
    return calls


# Configuration ---------------------------------------------------------------------------------


def test_sandbox_configuration(tmp_path):
    parsed = settings(tmp_path, mode="on", allow_write=["D:/cache"], deny_read=["~/secrets"], allow_unsandboxed="never")
    assert parsed.sandbox == SandboxConfig(
        "on", allow_write=("D:/cache",), deny_read=("~/secrets",), allow_unsandboxed="never"
    )
    assert settings(tmp_path).sandbox.mode == "off"
    assert settings(tmp_path, backend="token").sandbox.backend == "token"


@pytest.mark.parametrize(
    "block",
    [
        {"mode": "partial"},
        {"allow_unsandboxed": "always"},
        {"allow_write": "/tmp"},
        {"network": "all"},
        {"wxc_exec": ""},
        {"backend": "docker"},
    ],
)
def test_invalid_sandbox_configuration(tmp_path, block):
    with pytest.raises(ConfigError, match="sandbox"):
        settings(tmp_path, **block)


WIDENING = {
    "mode": "off",
    "allow_write": ["/"],
    "allow_read": ["~"],
    "allow_unsandboxed": "ask",
    "network": "proxy",
    "backend": "token",
    "wxc_exec": "./wxc-exec.exe",
}


def test_a_project_file_cannot_widen_the_sandbox_or_the_command_environment(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    strict = {"mode": "on", "allow_write": ["D:/cache"], "allow_unsandboxed": "never", "network": "none"}
    write_config(
        home / ".mosscode" / "config.yaml",
        sandbox={**strict, "deny_read": ["~/secrets"]},
        command_env={"keep": ["CI_TOKEN"], "drop": ["OLD"]},
    )
    project = write_config(
        repo / ".mosscode" / "config.yaml",
        sandbox={**WIDENING, "deny_read": ["~/more"]},
        command_env={"keep": ["CI_TOKEN", "*"], "drop": ["EXTRA"]},
    )
    assert find_config(repo, home) == project
    loaded = load_settings(project, home)
    assert loaded.sandbox == SandboxConfig(
        "on", allow_write=("D:/cache",), deny_read=("~/secrets", "~/more"), allow_unsandboxed="never", network="none"
    )
    assert (loaded.command_env_keep, loaded.command_env_drop) == (("CI_TOKEN",), ("OLD", "EXTRA"))
    assert set(loaded.ignored) == {f"sandbox.{key}" for key in WIDENING} | {"command_env.keep"}
    notice = project_notice(loaded)
    assert "已忽略" in notice and "sandbox.allow_write" in notice and "command_env.keep" in notice


def test_a_project_file_can_tighten_the_sandbox(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    tighter = {"mode": "on", "allow_unsandboxed": "never", "network": "none", "deny_read": ["~/more"]}
    project = write_config(repo / ".mosscode" / "config.yaml", sandbox=tighter, command_env={"drop": ["EXTRA"]})
    loaded = load_settings(project, home)  # No user file: its defaults are the base.
    assert loaded.sandbox == SandboxConfig("on", deny_read=("~/more",), allow_unsandboxed="never", network="none")
    assert loaded.command_env_drop == ("EXTRA",) and loaded.ignored == () and project_notice(loaded) is None
    # Repeating what the user already allows is not a widening.
    write_config(home / ".mosscode" / "config.yaml", sandbox={"allow_write": ["D:/cache"], "backend": "token"})
    same = write_config(repo / ".mosscode" / "config.yaml", sandbox={"allow_write": ["D:/cache"], "backend": "token"})
    assert load_settings(same, home).ignored == ()


def test_a_project_file_is_recognised_however_it_is_named(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    project = write_config(repo / ".mosscode" / "config.yaml", sandbox={"allow_write": ["/"]})
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    paths = [repo / "sub" / ".." / ".mosscode" / "config.yaml", Path("..") / ".mosscode" / "config.yaml", str(project)]
    if WINDOWS:
        paths.append(repo / ".MossCode" / "Config.YAML")
    for path in paths:
        loaded = load_settings(path, home)
        assert loaded.sandbox.allow_write == () and loaded.ignored == ("sandbox.allow_write",), path
    # Without a user file nothing in the project's file is taken as the user's word.
    assert load_settings(project).sandbox.allow_write == ()


def test_the_users_own_file_and_one_they_point_at_set_the_sandbox_freely(tmp_path):
    home = tmp_path / "home"
    user = write_config(home / ".mosscode" / "config.yaml", sandbox=WIDENING, command_env={"keep": ["*"]})
    loaded = load_settings(user, home)
    assert loaded.sandbox.allow_write == ("/",) and loaded.command_env_keep == ("*",) and loaded.ignored == ()
    chosen = write_config(tmp_path / "repo" / "config.yaml", sandbox=WIDENING)
    assert load_settings(chosen, home).sandbox.backend == "token" and load_settings(chosen, home).ignored == ()


@pytest.mark.parametrize("content", ["providers: [", "- not a mapping", "sandbox: {mode: maybe}"])
def test_a_broken_user_file_does_not_leave_the_sandbox_to_the_project(tmp_path, content):
    home, repo = tmp_path / "home", tmp_path / "repo"
    project = write_config(repo / ".mosscode" / "config.yaml", sandbox={"mode": "off"})
    (home / ".mosscode").mkdir(parents=True)
    (home / ".mosscode" / "config.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="用户配置"):
        load_settings(project, home)


def test_doctor_lists_the_project_keys_that_were_ignored(tmp_path):
    from mosscode.doctor import WARN, check_config

    home, repo = tmp_path / "home", tmp_path / "repo"
    write_config(repo / ".mosscode" / "config.yaml", sandbox={"allow_write": ["/"]})
    items = check_config(cwd=repo, home=home)
    assert [item.level for item in items if "sandbox.allow_write" in item.text] == [WARN]


# Policy -------------------------------------------------------------------------------------------


def test_policy_writes_the_project_and_temp_and_protects_control_files(tmp_path):
    root, temp, home = tmp_path / "project", tmp_path / "temp", tmp_path / "home"
    for folder in (root / ".git", temp, home):
        folder.mkdir(parents=True)
    policy = policy_for(root, SandboxConfig(allow_write=(str(tmp_path / "cache"),)), temp, home=home)
    assert policy.writable == (root.resolve(), temp.resolve(), (tmp_path / "cache").resolve())
    names = {p.relative_to(root.resolve()).as_posix() for p in policy.protected}
    assert names == {".mosscode", ".claude", ".mcp.json", ".git/hooks", ".git/config"}
    assert policy.network == "none"


def test_a_worktree_may_write_the_main_repository_but_not_its_hooks(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "w").mkdir(parents=True)
    (main / ".git" / "worktrees" / "w" / "commondir").write_text("../..\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'w'}\n", encoding="utf-8")
    policy = policy_for(tree, SandboxConfig(), tmp_path / "temp", home=tmp_path)
    common = (main / ".git").resolve()
    assert common in policy.writable
    assert common / "hooks" in policy.protected and common / "config" in policy.protected


def test_secrets_are_unreadable_where_a_grant_reaches_them(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".aws").mkdir()
    root = tmp_path / "project"
    root.mkdir()
    (root / "config.yaml").write_text("api_key: x\n", encoding="utf-8")
    config = SandboxConfig(allow_read=(str(home),), deny_read=(str(root / "absent"),))
    policy = policy_for(root, config, tmp_path / "temp", secrets=(root / "config.yaml",), home=home)
    assert set(policy.deny_read) == {
        (home / ".ssh").resolve(),
        (home / ".aws").resolve(),
        (root / "config.yaml").resolve(),
    }
    # Nothing to refuse where nothing is granted, and a missing path is not listed.
    narrow = policy_for(root, SandboxConfig(), tmp_path / "temp", home=home)
    assert narrow.deny_read == ()


def test_readable_roots_follow_path_shims_and_virtual_environments(tmp_path, monkeypatch):
    home = tmp_path / "home"
    tool = tmp_path / "tools" / "node"
    (tool / "bin").mkdir(parents=True)
    scoop = tmp_path / "scoop"
    (scoop / "shims").mkdir(parents=True)
    app = scoop / "apps" / "git" / "2.45.2"
    (app / "cmd").mkdir(parents=True)
    (scoop / "shims" / "git.shim").write_text(f'path = "{app / "cmd" / "git.exe"}"\n', encoding="utf-8")
    base = tmp_path / "python312"
    base.mkdir()
    root = tmp_path / "project"
    (root / ".venv").mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    entries = [str(tool / "bin"), str(scoop / "shims"), str(home), str(Path(tmp_path.anchor))]
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    found = readable_roots(root, home=home)
    for expected in (tool / "bin", tool, scoop / "shims", app, base):
        assert expected.resolve() in found
    # The home folder and drive roots are never granted whole, even when listed in PATH.
    assert home.resolve() not in found and Path(tmp_path.anchor).resolve() not in found


# Backend requests -------------------------------------------------------------------------------


def test_mxc_request_shape(tmp_path):
    root = tmp_path / "project"
    (root / ".mosscode").mkdir(parents=True)
    policy = Policy(
        root,
        writable=(root,),
        readable=(tmp_path / "tools",),
        protected=(root / ".mosscode", root / ".git" / "hooks"),
        deny_read=(tmp_path / "secret",),
    )
    request = windows_mxc.request("cmd /c echo hi", root, {"A": "1"}, policy)
    assert request["version"] == "1.0.0" and request["containment"] == "processcontainer"
    assert request["network"]["egress"]["default"] == "deny"
    assert request["network"]["ingress"]["hostLoopback"] == "deny"  # A local proxy would be a way out.
    assert request["ui"]["disable"] is False and request["ui"]["clipboard"] == "none"
    assert request["telemetry"] == {"enabled": False}
    assert request["process"]["env"] == ["A=1"]
    filesystem = request["filesystem"]
    assert filesystem["readwritePaths"] == [str(root)]
    # A protected path that does not exist cannot be listed; it is watched instead.
    assert filesystem["readonlyPaths"] == [str(tmp_path / "tools"), str(root / ".mosscode")]
    assert filesystem["deniedPaths"] == [str(tmp_path / "secret")]


def test_prepare_wraps_the_command_and_cleans_up(tmp_path, isolated_home, monkeypatch):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    box = turn_on(monkeypatch)
    box.wxc = Path("wxc-exec.exe")
    prepared = box.prepare(["cmd", "/c", "echo hi"], root, {"HTTPS_PROXY": "http://127.0.0.1:7897", "A": "1"}, root)
    request = Path(prepared.argv[1])
    assert prepared.argv[0] == "wxc-exec.exe"
    assert request.parent == isolated_home / ".mosscode" / "sandbox" / "requests"
    assert "HTTPS_PROXY" not in prepared.env  # With the network off, a proxy is the only way out.
    data = json.loads(request.read_text(encoding="utf-8"))
    assert "A=1" in data["process"]["env"] and data["process"]["commandLine"] == 'cmd /c "echo hi"'
    # git creates hooks/ lazily; making it now lets the backend hold it read-only.
    assert (root / ".git" / "hooks").is_dir()
    assert str((root / ".git" / "hooks").resolve()) in data["filesystem"]["readonlyPaths"]
    assert root.resolve() / ".mosscode" in prepared.watched
    (root / ".mosscode").mkdir()
    (root / ".mosscode" / "permissions.yaml").write_text("allow: []\n", encoding="utf-8")
    notes = prepared.finish()
    assert not request.exists() and not (root / ".mosscode").exists()
    assert len(notes) == 1 and ".mosscode" in notes[0]
    assert prepared.finish() == notes  # Idempotent.


async def test_run_process_finishes_the_backend_even_when_the_command_fails(tmp_path, monkeypatch):
    box = turn_on(monkeypatch)
    leftover = tmp_path / "request.json"
    leftover.write_text("{}", encoding="utf-8")
    policy = Policy(tmp_path, (tmp_path,), (), (), ())

    def prepare(argv, cwd, env, root, proxy=None):
        return Prepared([str(tmp_path / "missing.exe")], env, policy, files=(leftover,))

    monkeypatch.setattr(box, "prepare", prepare)
    with pytest.raises((OSError, ToolFailure)):
        await run_process(["x"], tmp_path, sandbox_root=tmp_path)
    assert not leftover.exists()

    def working(argv, cwd, env, root, proxy=None):
        return Prepared([sys.executable, "-c", "print('ok')"], env, policy)

    monkeypatch.setattr(box, "prepare", working)
    details = await run_process(["x"], tmp_path, check=False, confined=True, sandbox_root=tmp_path)
    assert details["stdout"].strip() == "ok"
    assert details["sandbox"] == {"writable": [str(tmp_path)], "notes": [], "network": "none", "backend": "mxc"}
    json.dumps(details)  # Details are stored with the session.


def test_explain_only_when_the_output_looks_like_a_block():
    details = {"exit_code": 1, "stdout": "", "stderr": "Access is denied.", "sandbox": {"writable": ["F:/p"]}}
    hint = explain(details)
    assert "F:/p" in hint and "outside_sandbox" in hint and "justification" in hint
    assert explain({**details, "stderr": "Invoke-WebRequest : 无法连接到远程服务器"})
    assert explain({**details, "stderr": "SyntaxError"}) == ""
    assert explain(
        {**details, "stderr": "Rename-Item : 对路径“F:\\p\\.git”的访问被拒绝。\n    + CategoryInfo : WriteError"}
    )
    assert explain({**details, "stderr": "touch: cannot touch '/home/u/x': Read-only file system"})
    # PowerShell exits 0 after a denied cmdlet; only stderr counts then.
    assert explain({**details, "exit_code": 0})
    assert explain({**details, "exit_code": 0, "stdout": "Access is denied.", "stderr": ""}) == ""
    assert explain({**details, "exit_code": None}) == ""


def test_startup_failure_reports_the_launcher_error():
    stderr = '{"error":{"code":"backend_error","message":"CreateProcess failed"}}'
    message = startup_failure({"exit_code": 4294967295, "stderr": stderr})
    assert "backend_error" in message and "/sandbox off" in message
    assert startup_failure({"exit_code": 1, "stderr": "Access is denied."}) == ""


# Approvals ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions"])
async def test_sandboxed_commands_need_no_approval(tmp_path, monkeypatch, mode):
    box = turn_on(monkeypatch)
    calls = fake_backend(monkeypatch, box)
    reg = registry(tmp_path, mode)
    result = await reg.execute(command("python -m pytest -q"))
    assert not result.is_error and result.output.strip() == "inside" and len(calls) == 1


async def test_a_denial_reported_with_exit_code_zero_still_gets_the_hint(tmp_path, monkeypatch):
    box = turn_on(monkeypatch)
    script = "import sys; print('0'); sys.stderr.write('Get-Content : Access is denied')"
    monkeypatch.setattr(
        box,
        "prepare",
        lambda argv, cwd, env, root, proxy=None: Prepared([sys.executable, "-c", script], env, box.policy(root)),
    )
    result = await registry(tmp_path).execute(command("(Get-Content ~/.ssh/known_hosts).Count"))
    assert not result.is_error and "outside_sandbox" in result.output
    assert any("可能被拦截" in event for event in box.events)


async def test_deny_rules_and_risky_commands_still_apply_in_the_sandbox(tmp_path, monkeypatch):
    box = turn_on(monkeypatch)
    fake_backend(monkeypatch, box)
    reg = registry(tmp_path)
    reg.permissions.session.append(parse_rule("Bash(npm publish:*)", "deny"))
    assert (await reg.execute(command("npm publish"))).code == "permission_denied"
    assert (await reg.execute(command("rm -rf build"))).code == "approval_required"
    assert (await reg.execute(command("rm -rf ~"))).code == "dangerous_command"


async def test_leaving_the_sandbox_is_asked_even_when_approvals_are_skipped(tmp_path, monkeypatch):
    box = turn_on(monkeypatch)
    calls = fake_backend(monkeypatch, box)
    reg = registry(tmp_path, "bypassPermissions")
    reg.permissions.session.append(parse_rule("Bash(echo:*)", "allow"))
    outside = command("echo outside", outside_sandbox=True, justification="需要联网安装依赖")
    assert (await reg.execute(outside)).code == "approval_required"
    result, pending = await approve(reg, outside, "once")
    assert pending.choices == UNSANDBOXED_CHOICES and pending.operation.unsandboxed
    assert "需要联网安装依赖" in pending.operation.preview and "完整权限" in pending.operation.preview
    assert not result.is_error and result.output.strip() == "outside" and not calls
    result, _ = await approve(reg, outside, "session")
    assert not result.is_error and not reg.permissions.local_file.exists()
    reg.permissions.interactive = False
    assert not (await reg.execute(command("echo outside", outside_sandbox=True, justification="同上"))).is_error
    assert (await reg.execute(command("git push", outside_sandbox=True, justification="x"))).code == (
        "approval_required"
    )


async def test_a_refused_request_says_so(tmp_path, monkeypatch):
    turn_on(monkeypatch)
    reg = registry(tmp_path)
    result, _ = await approve(reg, command("echo x", outside_sandbox=True, justification="x"), "deny")
    assert result.code == "permission_denied" and "沙箱外" in result.output


async def test_leaving_the_sandbox_needs_a_reason_and_can_be_forbidden(tmp_path, monkeypatch):
    turn_on(monkeypatch, allow_unsandboxed="never")
    reg = registry(tmp_path, "bypassPermissions")
    assert (await reg.execute(command("echo x", outside_sandbox=True))).code == "invalid_arguments"
    result = await reg.execute(command("echo x", outside_sandbox=True, justification="需要"))
    assert result.code == "sandbox_required"


async def test_commands_are_refused_when_the_sandbox_cannot_run(tmp_path, monkeypatch):
    turn_on(monkeypatch, state="unavailable")
    reg = registry(tmp_path, "bypassPermissions")
    result = await reg.execute(command("echo x"))
    assert result.code == "sandbox_unavailable" and "/sandbox off" in result.output
    # The user's own ! commands are theirs to run.
    tool = reg.get("execute_command")
    assert reg.refusal(tool, tool.validate_arguments({"command": "echo x"})) is None


async def test_turning_the_sandbox_off_restores_normal_approval(tmp_path, monkeypatch):
    box = turn_on(monkeypatch)
    calls = fake_backend(monkeypatch, box)
    reg = registry(tmp_path)
    box.set_enabled(False)
    assert (await reg.execute(command("echo x"))).code == "approval_required"
    assert not calls and box.badge() == "无沙箱" and "关闭" in box.events[-1]


def test_the_command_tool_describes_the_sandbox_only_when_it_is_on(tmp_path, monkeypatch):
    plain = registry(tmp_path).get("execute_command")
    assert "outside_sandbox" not in json.dumps(plain.params_model.model_json_schema())
    assert not any("沙箱" in line for line in plain.prompt_guidelines)
    turn_on(monkeypatch)
    tool = registry(tmp_path).get("execute_command")
    assert "outside_sandbox" in json.dumps(tool.params_model.model_json_schema())
    assert any("outside_sandbox" in line for line in tool.prompt_guidelines)
    if WINDOWS:  # Git Bash cannot start in an MXC container.
        assert tool.command_shell.kind == "powershell"


def test_status_badge(monkeypatch):
    assert sandbox.badge() == ""
    turn_on(monkeypatch)
    assert sandbox.badge() == "沙箱"
    turn_on(monkeypatch, state="unavailable")
    assert sandbox.badge() == "沙箱不可用"


def test_team_members_inherit_a_sandbox_override(tmp_path, monkeypatch):
    from mosscode.teams.backends import member_arguments

    assert "--sandbox" not in member_arguments(tmp_path, "a") and "--no-sandbox" not in member_arguments(tmp_path, "a")
    sandbox.configure(SandboxConfig(), override=True)
    assert member_arguments(tmp_path, "a")[-1] == "--sandbox"
    sandbox.configure(SandboxConfig(mode="on"), override=False)
    assert member_arguments(tmp_path, "a")[-1] == "--no-sandbox"


def test_command_line_flags():
    from mosscode.launcher import parse_arguments

    assert parse_arguments([]).sandbox is None
    assert parse_arguments(["--sandbox"]).sandbox is True
    assert parse_arguments(["--no-sandbox"]).sandbox is False


def test_doctor_reports_the_sandbox(tmp_path, monkeypatch):
    from mosscode.doctor import OK, WARN, check_sandbox

    path = tmp_path / "config.yaml"
    missing = sandbox.Sandbox(SandboxConfig(mode="on"))
    monkeypatch.setattr(missing, "_detect", lambda: Status("unavailable", "bubblewrap", "找不到 bubblewrap。"))
    settings(tmp_path, mode="on")
    items = check_sandbox(str(path), probe=missing)
    assert items[0].text.startswith("沙箱：开启") and items[1].level == WARN and "找不到 bubblewrap" in items[1].text
    settings(tmp_path)
    items = check_sandbox(str(path), probe=missing)
    assert all(item.level == OK for item in items) and "现在不可用" in items[1].text
    working = sandbox.Sandbox(SandboxConfig(mode="on"))
    monkeypatch.setattr(working, "_detect", lambda: Status("full", "bubblewrap"))
    working.executable = "/usr/bin/bwrap"
    items = check_sandbox(str(path), probe=working)
    assert "bubblewrap（/usr/bin/bwrap），写入、读取和网络都由系统强制，自检通过" in items[1].text


# The real backend ---------------------------------------------------------------------------------


@functools.cache  # Looked up once: tests replace the home folder, which is one of the places it is found in.
def wxc_exec() -> Path | None:
    configured = os.environ.get("MOSSCODE_TEST_WXC")
    return Path(configured).resolve() if configured else windows_mxc.find(None)


needs_mxc = pytest.mark.skipif(
    not WINDOWS or wxc_exec() is None, reason="needs Windows and wxc-exec.exe (MOSSCODE_TEST_WXC or PATH)"
)


@needs_mxc
async def test_mxc_enforces_the_boundaries(tmp_path, isolated_home):
    root = tmp_path / "project"
    (root / ".mosscode").mkdir(parents=True)
    (root / ".mosscode" / "permissions.yaml").write_text("allow: []\n", encoding="utf-8")
    (root / ".git").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Fully offline; the proxy mode has its own test (where DNS lookups still work, see docs).
    box = sandbox.configure(SandboxConfig(mode="on", wxc_exec=str(wxc_exec()), network="none"))
    assert box.status().state == "full", box.status().reason
    reg = registry(root)
    requests = isolated_home / ".mosscode" / "sandbox" / "requests"
    script = "; ".join(
        f"try {{ Set-Content -LiteralPath '{target}' -Value x -ErrorAction Stop; '{name} written' }} "
        f"catch {{ '{name} refused' }}"
        for name, target in {
            "inside": root / "inside.txt",
            "outside": outside / "x.txt",
            "permissions": root / ".mosscode" / "permissions.yaml",
            "hook": root / ".git" / "hooks" / "pre-commit",
            "request": requests / "forged.json",
            "home": isolated_home / "x.txt",
        }.items()
    )
    script += "; try { [Net.Dns]::GetHostAddresses('example.com') | Out-Null; 'dns ok' } catch { 'dns refused' }"
    result = await reg.execute(command(script))
    assert not result.is_error, result.output
    for expected in (
        "inside written",
        "outside refused",
        "permissions refused",
        "hook refused",
        "request refused",
        "home refused",
        "dns refused",
    ):
        assert expected in result.output, result.output
    assert (root / "inside.txt").exists() and not (outside / "x.txt").exists()
    assert (root / ".mosscode" / "permissions.yaml").read_text(encoding="utf-8") == "allow: []\n"
    assert not list(requests.glob("*.json"))  # The request is removed after the command.


@needs_mxc
async def test_mxc_removes_protected_paths_a_command_created(tmp_path, isolated_home):
    root = tmp_path / "project"
    root.mkdir()
    sandbox.configure(SandboxConfig(mode="on", wxc_exec=str(wxc_exec())))
    reg = registry(root)
    result = await reg.execute(command("New-Item -ItemType Directory .mosscode | Out-Null; Set-Content .mcp.json '{}'"))
    assert not result.is_error, result.output
    assert not (root / ".mosscode").exists() and not (root / ".mcp.json").exists()
    assert "已删除" in result.output and len(sandbox.current().events) == 2
