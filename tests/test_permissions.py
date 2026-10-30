import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from mosscode.conversation import Conversation
from mosscode.messages import ToolCall, ToolProgress
from mosscode.permissions import (ApprovalRequest, Operation, PermissionConfigError, PermissionPolicy,
                                 Rule, parse_rule, path_matches)
from mosscode.security import SecurityError, check_command, command_risk, read_only_command
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response


def registry(root, mode="default", *, load=False, user=None):
    policy = PermissionPolicy(root, mode=mode, load=load, user_path=user)
    return create_default_registry(root, permissions=policy)


def write_config(path, **data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypassPermissions"])
async def test_hard_guards_cannot_be_granted(tmp_path, mode):
    reg = registry(tmp_path, mode)
    reg.permissions.layers[2] = (parse_rule("Bash(*)", "allow"), parse_rule("Write(**)", "allow"))
    for request, code in [
        (call("execute_command", command="rm -rf /"), "dangerous_command"),
        (call("execute_command", command="Remove-Item C:\\ -Recurse -Force"), "dangerous_command"),
        (call("execute_command", command="shutdown /s /t 0"), "dangerous_command"),
        (call("write_file", path="../outside.txt", content="bad"), "path_blocked"),
        (call("edit_file", path=str(tmp_path.parent / "outside.txt"), old_text="a", new_text="b"), "path_blocked"),
        (call("execute_command", command="git status", cwd=".."), "path_blocked"),
        (call("write_file", path=".mosscode/permissions.yaml", content="allow: []"), "policy_protected"),
        (call("write_file", path=".mosscode/permissions.local.yaml", content="mode: permissive"), "policy_protected"),
    ]:
        result = await reg.execute(request)
        # Plan mode refuses every non-read tool and non-read-only command before any other check.
        command = json.loads(request.arguments).get("command")
        blocked = mode == "plan" and not (command and read_only_command(command))
        assert result.code == ("plan_blocked" if blocked else code) and result.details["executed"] is False
    assert not (tmp_path.parent / "outside.txt").exists()
    assert not (tmp_path / ".mosscode").exists()


@pytest.mark.parametrize("command", ["Format-Volume -DriveLetter C", "format C:", "diskpart", "shutdown /s",
                                    "mkfs.ext4 /dev/sda", "dd if=x of=/dev/sda", "rm -r /", "rm -rf ~",
                                    "rm -rf --no-preserve-root /x", "Remove-Item C:\\ -Recurse",
                                    "Remove-Item $HOME -Recurse -Force", ":(){ :|:& };:", "Restart-Computer"])
def test_catastrophic_signatures(command):
    with pytest.raises(SecurityError) as error:
        check_command(command)
    assert error.value.code == "dangerous_command"


@pytest.mark.parametrize("command,reason", [
    ("rm -rf build", "递归强制删除"), ("rm -r -f a", "递归强制删除"), ("rm -f -r a", "递归强制删除"),
    ("Remove-Item a -Recurse -Force", "递归强制删除"), ("rmdir /s /q a", "递归删除"), ("del /s a", "递归删除"),
    ("git reset --hard", "丢弃 Git 改动"), ("git clean -fdx", "强制清理 Git 文件"),
    ("git push origin main --force", "强制推送"), ("git push -f", "强制推送"),
    ("pwsh -EncodedCommand abc", "编码命令执行"), ("iex 'x'", "动态表达式执行"),
])
def test_risky_signatures_are_not_hard_denied(command, reason):
    check_command(command)
    assert command_risk(command) == reason


@pytest.mark.parametrize("command", ["git status", "git reset --soft HEAD~1", "git clean -n", "git push origin main", "python -m pytest"])
def test_similar_safe_commands(command):
    check_command(command)
    assert command_risk(command) is None


@pytest.mark.parametrize("mode,read_action,write_action", [
    ("default", "allow", "ask"), ("acceptEdits", "allow", "allow"),
    ("plan", "allow", "ask"), ("bypassPermissions", "allow", "allow"),
])
def test_modes_and_explicit_denies(tmp_path, mode, read_action, write_action):
    policy = PermissionPolicy(tmp_path, mode=mode, load=False)
    operation = Operation("write_file", "src/a.py")
    assert policy.decide(Operation("read_file", "src/a.py"), read_only=True).action == read_action
    assert policy.decide(operation, read_only=False).action == write_action
    policy.layers[1] = (parse_rule("Write(src/**)", "allow"), parse_rule("Write(src/a.py)", "deny"))
    assert policy.decide(operation, read_only=False).action == "deny"
    policy.layers[2] = (parse_rule("Write(src/a.py)", "allow"),)
    assert policy.decide(operation, read_only=False).action == "allow"
    policy.session.append(Rule("write_file", "src/a.py", "deny", True))
    assert policy.decide(operation, read_only=False).action == "deny"


def test_config_layers_aliases_and_cwd(tmp_path):
    root, user = tmp_path / "project", tmp_path / "user.yaml"
    root.mkdir()
    write_config(user, mode="bypassPermissions", allow=["Read(**)", "Bash(git *)"])
    write_config(root / ".mosscode/permissions.yaml", mode="acceptEdits", deny=["Read(secret)"])
    write_config(root / ".mosscode/permissions.local.yaml", mode="default", allow=["Read(secret)"])
    policy = PermissionPolicy(root, load=True, user_path=user)
    assert policy.mode == "default"
    assert policy.decide(Operation("read_file", "secret"), read_only=True).action == "allow"
    policy.layers[2] = ()
    assert policy.decide(Operation("read_file", "secret"), read_only=True).action == "deny"
    assert parse_rule("Read(src/*.py)", "allow").tool == "read_file"
    grant = parse_rule({"rule": "Bash(git status)", "exact": True, "cwd": "src"}, "allow")
    assert grant.matches(Operation("execute_command", "git status", "src"))
    assert not grant.matches(Operation("execute_command", "git status", "."))
    assert PermissionPolicy(root, mode="plan", user_path=user).resume_mode == "default"


@pytest.mark.parametrize("data", [{"unknown": []}, {"mode": "typo"}, {"allow": "Bash(*)"},
                                  {"deny": ["Write()"]}, {"allow": ["Unknown(*)"]},
                                  {"allow": ["Read(../*)"]}, {"allow": [{"rule": "Read(*)", "exact": "yes"}]}])
def test_bad_config_is_not_silent_allow(tmp_path, data):
    path = tmp_path / "bad.yaml"
    write_config(path, **data)
    with pytest.raises(PermissionConfigError, match="权限配置"):
        PermissionPolicy(tmp_path, user_path=path)


def test_path_globs_and_compound_command_rules():
    assert path_matches("src/a.py", "src/*.py")
    assert not path_matches("src/nested/a.py", "src/*.py")
    assert path_matches("src/nested/a.py", "src/**/*.py")
    assert path_matches("src/a.py", "src/**/*.py")
    assert not path_matches("src-extra/a.py", "src/**")
    rule = parse_rule("Bash(git *)", "allow")
    assert rule.matches(Operation("execute_command", "git status", "."))
    for command in ["git status; python evil.py", "git status && python evil.py", "git status | python evil.py",
                    "git status > a", "git $(python evil.py)", "git `python evil.py`", "git status\npython evil.py"]:
        assert not rule.matches(Operation("execute_command", command, "."))
    assert parse_rule("Bash(git *)", "deny").matches(Operation("execute_command", "git status; echo x", "."))


async def approve(reg, request, choice):
    reg.permissions.interactive = True
    task = asyncio.create_task(reg.execute(request))
    pending = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    assert reg.permissions.respond(pending.id, choice)
    return await task, pending


async def test_once_session_and_permanent_scope(tmp_path):
    reg = registry(tmp_path)
    write = call("write_file", path="a[1].txt", content="one")
    result, pending = await approve(reg, write, "once")
    assert not result.is_error and pending.operation.target == "a[1].txt"
    assert not reg.permissions.session and not reg.permissions.local_file.exists()
    reg.permissions.interactive = False
    assert (await reg.execute(write)).code == "approval_required"
    result, _ = await approve(reg, write, "session")
    assert not result.is_error
    reg.permissions.interactive = False
    assert not (await reg.execute(call("write_file", path="a[1].txt", content="two"))).is_error
    assert (await reg.execute(call("write_file", path="a1.txt", content="bad"))).code == "approval_required"
    assert (await registry(tmp_path).execute(write)).code == "approval_required"
    result, _ = await approve(reg, call("write_file", path="persist.txt", content="saved"), "permanent")
    assert not result.is_error
    data = yaml.safe_load(reg.permissions.local_file.read_text(encoding="utf-8"))
    assert data["allow"] == [{"rule": "write_file(persist.txt)", "exact": True}]
    restored = registry(tmp_path, load=True, user=tmp_path / "absent-user.yaml")
    assert not (await restored.execute(call("read_file", path="persist.txt"))).is_error
    assert not (await restored.execute(call("write_file", path="persist.txt", content="restart"))).is_error
    assert (await restored.execute(call("write_file", path="other.txt", content="bad"))).code == "approval_required"


async def test_approval_wait_does_not_use_execution_timeout_and_cancel_has_no_effect(tmp_path, monkeypatch):
    reg = registry(tmp_path)
    tool = reg.get("write_file")
    tool.timeout = .01
    reg.permissions.interactive = True
    task = asyncio.create_task(reg.execute(call("write_file", path="new", content="yes")))
    request = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    await asyncio.sleep(.03)
    assert not task.done() and not (tmp_path / "new").exists()
    reg.permissions.respond(request.id, "once")
    assert not (await task).is_error
    task = asyncio.create_task(reg.execute(call("write_file", path="cancelled", content="no")))
    request = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not reg.permissions.pending and not (tmp_path / "cancelled").exists()
    assert not reg.permissions.respond(request.id, "once")


async def test_save_failure_denies_and_preserves_existing_rules(tmp_path, monkeypatch):
    reg = registry(tmp_path)
    path = reg.permissions.local_file
    write_config(path, mode="default", deny=["Read(secret)"])
    before = path.read_bytes()
    def fail(*args):
        raise OSError("no space")
    monkeypatch.setattr("mosscode.permissions.os.replace", fail)
    result, _ = await approve(reg, call("write_file", path="no", content="bad"), "permanent")
    assert result.code == "permission_save_failed" and not (tmp_path / "no").exists()
    assert path.read_bytes() == before and not list(path.parent.glob(".permissions-*"))


async def test_persistent_command_grant_binds_exact_command_and_cwd(tmp_path):
    (tmp_path / "src").mkdir()
    reg = registry(tmp_path)
    async def fake_execute(params):
        return "safe fake command", {}
    reg.get("execute_command").execute = fake_execute
    write_config(reg.permissions.local_file, deny=["Read(secret)"])
    result, _ = await approve(reg, call("execute_command", command="git fetch", cwd="src"), "permanent")
    assert not result.is_error
    data = yaml.safe_load(reg.permissions.local_file.read_text(encoding="utf-8"))
    assert data["deny"] == ["Read(secret)"]
    assert data["allow"][0] == {"rule": "execute_command(git fetch)", "exact": True, "cwd": "src"}
    restored = registry(tmp_path, load=True, user=tmp_path / "absent-user.yaml")
    restored.get("execute_command").execute = fake_execute
    assert not (await restored.execute(call("execute_command", command="git fetch", cwd="src"))).is_error
    assert (await restored.execute(call("execute_command", command="git fetch"))).code == "approval_required"
    assert (await restored.execute(call("execute_command", command="git fetch --all", cwd="src"))).code == "approval_required"


async def test_target_change_during_confirmation_does_not_grant_or_execute(tmp_path, monkeypatch):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    request = call("write_file", path="a", content="bad")
    task = asyncio.create_task(reg.execute(request))
    pending = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    original = reg.permissions.operation
    def changed(tool, params):
        op = original(tool, params)
        return Operation(op.tool, "different.txt", op.cwd, op.preview)
    monkeypatch.setattr(reg.permissions, "operation", changed)
    reg.permissions.respond(pending.id, "permanent")
    result = await task
    assert result.code == "target_changed" and not (tmp_path / "a").exists()
    assert not reg.permissions.local_file.exists()


async def test_new_deny_on_disk_is_respected_when_saving_grant(tmp_path):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    task = asyncio.create_task(reg.execute(call("write_file", path="a", content="bad")))
    pending = await asyncio.wait_for(reg.permissions.requests.get(), 2)
    write_config(reg.permissions.local_file, deny=["Write(a)"])
    reg.permissions.respond(pending.id, "permanent")
    assert (await task).code == "permission_denied"
    assert not (tmp_path / "a").exists()


async def test_denial_is_returned_to_model_and_loop_continues(tmp_path):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    provider = SequenceProvider(response(calls=[call("write_file", path="denied", content="bad")]),
                                response(calls=[call("read_file", path="allowed.txt")]), response("换一种做法完成"))
    (tmp_path / "allowed.txt").write_text("safe")
    conv = Conversation(provider, registry=reg)
    seen = []
    async for event in conv.reply("任务"):
        seen.append(event)
        if isinstance(event, ApprovalRequest):
            assert not (tmp_path / "denied").exists()
            reg.permissions.respond(event.id, "deny")
    assert provider.requests[1][0][-1].tool_results[0].code == "permission_denied"
    assert len(provider.requests) == 3 and len(conv.history) == 6
    assert not (tmp_path / "denied").exists()
    assert not reg.permissions.pending and not conv._busy


async def test_explicit_read_only_constraint_and_headless_never_wait(tmp_path):
    reg = registry(tmp_path, "bypassPermissions")
    assert (await reg.execute(call("write_file", path="blocked", content="no"), read_only=True)).code == "plan_blocked"
    reg.permissions.set_mode("default")
    assert (await asyncio.wait_for(reg.execute(call("write_file", path="blocked", content="no")), 1)).code == "approval_required"
    assert not reg.permissions.pending and not (tmp_path / "blocked").exists()


async def test_parallel_approvals_are_serialized_and_cancellation_pairs_results(tmp_path):
    reg = registry(tmp_path)
    reg.permissions.interactive = True
    # Reads outside the project ask, so two parallel reads both need approval.
    outside = [str(tmp_path.parent / name) for name in ("a", "b")]
    provider = SequenceProvider(response(calls=[call("read_file", "a", path=outside[0]),
                                                call("read_file", "b", path=outside[1])]))
    conv = Conversation(provider, registry=reg)
    arrived = asyncio.Event()
    async def consume():
        async for event in conv.reply("task"):
            if isinstance(event, ApprovalRequest):
                assert len(reg.permissions.pending) == 1
                arrived.set()
    task = asyncio.create_task(consume())
    await asyncio.wait_for(arrived.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not reg.permissions.pending and not conv._busy
    results = conv.history[-1].tool_results
    assert [r.call_id for r in results] == ["a", "b"] and all(r.code == "cancelled" for r in results)


async def test_same_prefix_directory_cannot_escape(tmp_path):
    root = tmp_path / "project"
    sibling = tmp_path / "project-extra"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret").write_text("secret")
    reg = registry(root)
    # A shared name prefix is still outside: reads ask, writes stay blocked.
    assert (await reg.execute(call("read_file", path=str(sibling / "secret")))).code == "approval_required"
    assert (await reg.execute(call("write_file", path=str(sibling / "new"), content="x"))).code == "path_blocked"


async def test_symlink_file_search_fallback_does_not_leak(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private_marker")
    try:
        (root / "link.txt").symlink_to(secret)
    except OSError:
        pytest.skip("当前 Windows 用户不能创建文件符号链接；目录 junction 另有实际测试。")
    reg = registry(root)
    # The link's target is outside: reads need approval like any outside path, writes never pass.
    assert (await reg.execute(call("read_file", path="link.txt"))).code == "approval_required"
    assert (await reg.execute(call("write_file", path="link.txt", content="bad"))).code == "path_blocked"
    reg.permissions.set_mode("bypassPermissions")
    assert (await reg.execute(call("write_file", path="link.txt", content="bad"))).code == "path_blocked"
    assert secret.read_text() == "private_marker"
    for fallback in (False, True):
        if fallback:
            monkeypatch.setattr("mosscode.tools.search.shutil.which", lambda _: None)
        result = await reg.execute(call("grep", pattern="private_marker"))
        assert not result.is_error and result.details["count"] == 0
        assert "private_marker" not in result.output


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 行为")
async def test_windows_junction_search_and_new_file_escape(tmp_path, monkeypatch):
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("junction_private_marker")
    link = root / "junction"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    assert created.returncode == 0, created.stderr
    try:
        reg = registry(root)
        # Through the junction the target is outside: reads need approval, writes never pass.
        for request in [call("read_file", path="junction/secret.txt"),
                        call("glob", pattern="**/*", path="junction"),
                        call("grep", pattern="private", path="junction")]:
            assert (await reg.execute(request)).code == "approval_required"
        assert (await reg.execute(call("write_file", path="junction/new.txt", content="bad"))).code == "path_blocked"
        reg.permissions.set_mode("bypassPermissions")
        for fallback in (False, True):
            if fallback:
                monkeypatch.setattr("mosscode.tools.search.shutil.which", lambda _: None)
            result = await reg.execute(call("grep", pattern="junction_private_marker"))
            assert not result.is_error and result.details["count"] == 0
            result = await reg.execute(call("glob", pattern="**/*.txt"))
            assert not result.is_error and result.details["count"] == 0
        assert not (outside / "new.txt").exists()
    finally:
        assert link.absolute().is_relative_to(tmp_path.absolute())
        os.rmdir(link)  # Remove this verified link only, never its target recursively.


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 行为")
async def test_project_anchor_cannot_move_after_startup(tmp_path):
    root, outside, backup = tmp_path / "project", tmp_path / "outside", tmp_path / "original"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    reg = registry(root)
    assert all(p.resolve().is_relative_to(tmp_path.resolve()) for p in (root, backup))
    root.rename(backup)
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(root), str(outside)], capture_output=True)
    assert created.returncode == 0, created.stderr
    try:
        assert (await reg.execute(call("read_file", path="secret.txt"))).code == "approval_required"
        assert (await reg.execute(call("write_file", path="new", content="bad"))).code == "path_blocked"
        assert not (outside / "new").exists()
    finally:
        assert root.absolute().is_relative_to(tmp_path.absolute())
        os.rmdir(root)
        assert all(p.resolve().is_relative_to(tmp_path.resolve()) for p in (root, backup))
        backup.rename(root)


def test_invalid_yaml_does_not_expose_source_or_load_empty_policy(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("allow: [SECRET_SOURCE_VALUE", encoding="utf-8")
    with pytest.raises(PermissionConfigError) as error:
        PermissionPolicy(tmp_path, user_path=path)
    assert "SECRET_SOURCE_VALUE" not in str(error.value)
