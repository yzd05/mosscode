"""Chapter 14: git worktrees and isolated sub-agents."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest
import yaml

from mosscode import cli
from mosscode.agents import AgentCatalog, AgentError, parse_agent
from mosscode.agents.runtime import WORKSPACE_TOOLS
from mosscode.builtin_commands import BUILTIN
from mosscode.commands import parse_input
from mosscode.config import ConfigError, load_settings
from mosscode.messages import ToolCall
from mosscode.prompts import instruction_candidates
from mosscode.tools import create_default_registry
from mosscode.worktree import (WorktreeChanged, WorktreeError, WorktreeManager, count_changes, flatten, load_meta,
                               read_head, validate_name)
from mosscode.worktree import git as worktree_git
from mosscode.worktree import manager as manager_module
from test_agent import call, response
from test_commands_stage10 import context, run
from test_sessions_stage9b import drain
from test_subagents_stage13 import (ScriptProvider, agents_dir, conversation, hook_engine, main_requests, project_of,
                                    settle, tool_results_of, write_agent)


# Helpers ------------------------------------------------------------------------------------------

def git(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def repo(tmp_path) -> Path:
    """A repository with one commit at tmp_path/project."""
    project = project_of(tmp_path)
    git(project, "init", "-q", "-b", "main")
    (project / "a.txt").write_text("main\n", encoding="utf-8")
    (project / ".gitignore").write_text("config.yaml\n.env\nnode_modules/\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    return project


def manager_of(project, **options) -> WorktreeManager:
    return WorktreeManager.discover(project, **options)


def branches(project) -> list[str]:
    return git(project, "branch", "--list", "mosscode/*", "--format=%(refname:short)").split()


# AC1: names ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["feature/a", "agent-1a2b3c4d", "v1.2", "a_b"])
def test_ac1_valid_names(name):
    assert validate_name(name) is None


@pytest.mark.parametrize("name", ["", "x" * 65, "..", "a/../b", "a..b", "a//b", "/a", "a/", ".hidden", "-x", "a.lock", "a.",
                                  "nul", "COM1.txt", "a b", "中文"])
def test_ac1_invalid_names(name):
    assert validate_name(name)


async def test_ac1_flat_names_and_case_clash(tmp_path):
    project = repo(tmp_path)
    manager = manager_of(project)
    created = await manager.create("team/alice")
    assert flatten("team/alice") == "team+alice" and created.meta.branch == "mosscode/team+alice"
    assert Path(created.meta.path) == project / ".mosscode" / "worktrees" / "team+alice"
    await manager.create("Demo")
    with pytest.raises(WorktreeError, match="大小写") as caught:
        await manager.create("demo")
    assert caught.value.code == "invalid_name"


# AC2/AC3: creating -------------------------------------------------------------------------------------

async def test_ac2_create_records_everything_and_keeps_the_main_status_clean(tmp_path):
    project = repo(tmp_path)
    manager = manager_of(project)
    created = await manager.create("demo")
    meta = created.meta
    assert Path(meta.path).is_dir() and branches(project) == ["mosscode/demo"]
    assert meta.base == git(project, "rev-parse", "HEAD") and meta.manual and not created.resumed
    assert load_meta(manager.meta_path("demo")).setup_done
    exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    await manager.create("other")
    assert (project / ".git" / "info" / "exclude").read_text(encoding="utf-8").count("/.mosscode/worktrees/") == 1
    assert "/.mosscode/worktrees/" in exclude
    assert git(project, "status", "--porcelain") == ""
    # The branch exists already: an error and nothing left behind.
    git(project, "branch", "mosscode/taken")
    with pytest.raises(WorktreeError) as caught:
        await manager.create("taken")
    assert caught.value.code == "branch_exists" and not (manager.base / "taken").exists()


def test_ac2_not_a_repository_or_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError) as caught:
        manager_of(plain)
    assert caught.value.code == "worktree_unavailable"
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(WorktreeError, match="没有任何提交") as caught:
        manager_of(empty)
    assert caught.value.code == "worktree_unavailable"


async def test_ac3_subdirectory_project(tmp_path):
    project = repo(tmp_path)
    (project / "pkg").mkdir()
    (project / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "pkg")
    manager = manager_of(project / "pkg")
    created = await manager.create("sub")
    assert manager.top == project.resolve() and manager.project_part == "pkg"
    assert manager.root_in(created.meta) == Path(created.meta.path) / "pkg"
    assert (manager.root_in(created.meta) / "m.py").is_file()


# AC4: resuming -----------------------------------------------------------------------------------------

async def test_ac4_resume_reads_the_filesystem_only(tmp_path, monkeypatch):
    project = repo(tmp_path)
    manager = manager_of(project)
    first = await manager.create("demo")
    git(project, "pack-refs", "--all")
    assert read_head(Path(first.meta.path)) == (first.meta.base, "refs/heads/mosscode/demo")

    def refuse(*args, **kwargs):
        raise AssertionError("git must not run")
    monkeypatch.setattr(worktree_git, "run_git", refuse)
    again = await manager.create("demo")
    assert again.resumed and again.meta.path == first.meta.path


async def test_ac4_broken_worktree_is_reported_not_deleted_and_setup_finishes(tmp_path):
    project = repo(tmp_path)
    manager = manager_of(project)
    created = await manager.create("demo")
    path = Path(created.meta.path)
    meta = load_meta(manager.meta_path("demo"))
    meta.setup_done = False
    (project / ".mosscode").mkdir(exist_ok=True)
    (project / ".mosscode" / "permissions.local.yaml").write_text("allow: []\n", encoding="utf-8")
    from mosscode.worktree.meta import save_meta
    save_meta(meta, manager.meta_path("demo"))
    again = await manager.create("demo")
    assert again.resumed and (path / ".mosscode" / "permissions.local.yaml").is_file()
    assert load_meta(manager.meta_path("demo")).setup_done
    (path / ".git").unlink()  # Hidden on Windows: replace rather than overwrite.
    (path / ".git").write_text("garbage", encoding="utf-8")
    with pytest.raises(WorktreeError) as caught:
        await manager.create("demo")
    assert caught.value.code == "worktree_invalid" and path.is_dir()


# AC5/AC6: setup ----------------------------------------------------------------------------------------

async def test_ac5_local_files_include_and_links(tmp_path):
    project = repo(tmp_path)
    (project / ".mosscode").mkdir()
    (project / ".mosscode" / "permissions.local.yaml").write_text("allow: []\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    (project / "config.yaml").write_text("secret: x\n", encoding="utf-8")
    (project / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
    (project / "node_modules" / "lib").mkdir(parents=True)
    (project / "node_modules" / "lib" / "index.js").write_text("x", encoding="utf-8")
    manager = manager_of(project, links=["node_modules", "missing"])
    created = await manager.create("demo")
    path = Path(created.meta.path)
    assert (path / ".mosscode" / "permissions.local.yaml").is_file()
    assert (path / ".env").read_text(encoding="utf-8") == "TOKEN=x\n" and not (path / "config.yaml").exists()
    link = path / "node_modules"
    assert link.is_symlink() or link.is_junction()
    assert (link / "lib" / "index.js").is_file() and created.meta.links == ["node_modules"]
    # Neither the copies nor the link are changes of the worktree.
    assert not manager.changes(created.meta).any


async def test_ac5_failed_step_is_a_warning(tmp_path, monkeypatch):
    project = repo(tmp_path)
    (project / ".worktreeinclude").write_text(".env\n", encoding="utf-8")

    def broken(*args):
        raise OSError("boom")
    monkeypatch.setattr(manager_module, "copy_included", broken)
    created = await manager_of(project).create("demo")
    assert Path(created.meta.path).is_dir() and any("boom" in item for item in created.warnings)


async def test_ac6_git_hooks_run_and_config_stays(tmp_path):
    project = repo(tmp_path)
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho ran > hook-ran.txt\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    config = (project / ".git" / "config").read_text(encoding="utf-8")
    created = await manager_of(project).create("demo")
    path = Path(created.meta.path)
    (path / "b.txt").write_text("b", encoding="utf-8")
    git(path, "add", "b.txt")
    git(path, "commit", "-q", "-m", "b")
    assert (path / "hook-ran.txt").is_file()
    assert (project / ".git" / "config").read_text(encoding="utf-8") == config


# AC7/AC8/AC9: changes and deleting -------------------------------------------------------------------------

async def test_ac7_change_counting(tmp_path):
    project = repo(tmp_path)
    created = await manager_of(project).create("demo")
    path, branch = Path(created.meta.path), created.meta.branch
    assert not count_changes(path, branch).any
    (path / "a.txt").write_text("changed\n", encoding="utf-8")
    (path / "new.txt").write_text("n", encoding="utf-8")
    changes = count_changes(path, branch)
    assert changes.uncommitted == 2 and set(changes.files) == {"a.txt", "new.txt"}
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "work")
    changes = count_changes(path, branch)
    assert changes.uncommitted == 0 and changes.commits == 1
    git(project, "merge", "-q", "--ff-only", branch)
    assert count_changes(path, branch).commits == 0
    (project / "c.txt").write_text("c", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "main only")
    assert not count_changes(path, branch).any
    broken = count_changes(tmp_path / "nowhere", branch)
    assert broken.any and broken.error


async def test_ac8_delete_keeps_link_targets_and_never_uses_worktree_remove(tmp_path, monkeypatch):
    project = repo(tmp_path)
    (project / "node_modules" / "lib").mkdir(parents=True)
    (project / "node_modules" / "lib" / "index.js").write_text("keep", encoding="utf-8")
    manager = manager_of(project, links=["node_modules"])
    created = await manager.create("demo")
    path = Path(created.meta.path)
    (path / "b.txt").write_text("work", encoding="utf-8")
    with pytest.raises(WorktreeChanged, match="--discard"):
        await manager.remove("demo")
    calls, original = [], worktree_git.run_git

    def recording(args, cwd, **kwargs):
        calls.append(list(args))
        return original(args, cwd, **kwargs)
    monkeypatch.setattr(worktree_git, "run_git", recording)
    changes = await manager.remove("demo", discard=True)
    assert changes.uncommitted == 1
    assert not any(args[:2] == ["worktree", "remove"] for args in calls)
    assert not path.exists() and branches(project) == []
    assert str(path) not in git(project, "worktree", "list")
    assert (project / "node_modules" / "lib" / "index.js").read_text(encoding="utf-8") == "keep"
    assert not manager.meta_path("demo").exists()
    with pytest.raises(WorktreeError) as caught:
        await manager.remove("demo")
    assert caught.value.code == "unknown_worktree"


async def test_ac9_failed_delete_is_retried_by_clean(tmp_path, monkeypatch):
    project = repo(tmp_path)
    manager = manager_of(project)
    created = await manager.ephemeral("t1")
    meta_path = manager.meta_path(created.meta.flat)
    original = shutil.rmtree

    def failing(*args, **kwargs):
        raise PermissionError(13, "busy")
    monkeypatch.setattr(manager_module.shutil, "rmtree", failing)
    finished = await manager.finish(created.meta)
    assert not finished.kept and "未完成" in finished.error
    assert load_meta(meta_path).removing
    assert "删除未完成" in manager.report(created.meta, finished)
    monkeypatch.setattr(manager_module.shutil, "rmtree", original)
    old = time.time() - 48 * 3600
    os.utime(meta_path, (old, old))
    report = await manager.clean()
    assert report.removed == [created.meta.name] and not Path(created.meta.path).exists()


# AC10: cleaning -----------------------------------------------------------------------------------------

async def test_ac10_clean_removes_only_old_unused_unchanged_ephemeral_worktrees(tmp_path):
    project = repo(tmp_path)
    manager = manager_of(project)
    old = time.time() - 48 * 3600
    stale = await manager.ephemeral("t1")
    fresh = await manager.ephemeral("t2")
    changed = await manager.ephemeral("t3")
    busy = await manager.ephemeral("t4")
    dead = await manager.ephemeral("t5")
    manual = await manager.create("keep-me")
    odd = await manager.create("agent-odd")
    for item in (stale, fresh, changed, busy, dead, manual, odd):
        meta = load_meta(manager.meta_path(item.meta.flat))
        meta.in_use = {"pid": os.getpid(), "task": "t4"} if item is busy else (
            {"pid": 999999999, "task": "t5"} if item is dead else None)
        if item is odd:
            meta.manual = False
        from mosscode.worktree.meta import save_meta
        save_meta(meta, manager.meta_path(item.meta.flat))
        if item is not fresh:
            os.utime(manager.meta_path(item.meta.flat), (old, old))
    (Path(changed.meta.path) / "work.txt").write_text("w", encoding="utf-8")
    (manager.base / "stray").mkdir()
    report = await manager.clean()
    assert sorted(report.removed) == sorted([stale.meta.name, dead.meta.name])
    reasons = dict(report.skipped)
    assert reasons[fresh.meta.name] == "未过期" and reasons[manual.meta.name] == "手动创建"
    assert reasons[busy.meta.name] == "使用中" and reasons["agent-odd"] == "名称不是临时格式"
    assert reasons[changed.meta.name].startswith("有改动")
    assert (manager.base / "stray").is_dir()


async def test_ac10_stale_hours_zero_starts_no_cleanup(tmp_path):
    repo(tmp_path)
    conv = conversation(tmp_path, ScriptProvider(lambda *a: response("好")), worktree_stale_hours=0)
    assert conv.start_worktree_cleanup(lambda report: None) is False
    await conv.close()


# AC11–AC15: isolated sub-agents ---------------------------------------------------------------------------

def isolated_script(kind, *, agent_type="writer", isolation=None, background=False):
    """The main agent calls one sub-agent; `kind` decides what the sub-agent does."""
    def script(messages, system, tools):
        if "SUB-" in system.stable:
            if len(messages) == 1:
                return kind(messages, system, tools)
            return response("子任务完成")
        if messages[-1].role == "user":
            args = {"description": "改文件", "prompt": "处理 a.txt", "agent_type": agent_type}
            if isolation:
                args["isolation"] = isolation
            if background:
                args["run_in_background"] = True
            return response(calls=[call("agent", "a1", **args)])
        return response("主对话完成")
    return script


async def test_ac11_writer_keeps_its_worktree_and_main_is_untouched(tmp_path):
    project = repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-WRITER")
    provider = ScriptProvider(isolated_script(lambda *a: response(calls=[
        call("write_file", "w1", path="b.txt", content="from worktree\n")])))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    result = tool_results_of(main_requests(provider)[1])[0]
    task = conv.tasks.get("t1")
    path = Path(task.worktree["path"])
    assert (path / "b.txt").read_text(encoding="utf-8") == "from worktree\n" and not (project / "b.txt").exists()
    assert "[工作树] 已保留：.mosscode/worktrees/agent-" in result.output
    assert f"git merge {task.worktree['branch']}" in result.output and "未提交 1 个文件" in result.output
    assert task.worktree["outcome"] == "已保留" and result.details["worktree"] == "已保留"
    await conv.close()


async def test_ac11_reader_worktree_is_deleted(tmp_path):
    project = repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\ntools: Read\n", "SUB-READER")
    provider = ScriptProvider(isolated_script(lambda *a: response(calls=[call("read_file", "r1", path="a.txt")])))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    result = tool_results_of(main_requests(provider)[1])[0]
    task = conv.tasks.get("t1")
    assert result.output.rstrip().endswith("[工作树] 无改动，已删除。")
    assert task.worktree["outcome"] == "已删除" and not Path(task.worktree["path"]).exists()
    assert branches(project) == []
    await conv.close()


async def test_ac12_boundaries(tmp_path):
    project = repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-W")
    outside = str(project / "c.txt")
    provider = ScriptProvider(isolated_script(lambda *a: response(calls=[
        call("write_file", "w1", path=outside, content="x"),
        call("write_file", "w2", path=".mosscode/permissions.yaml", content="allow: ['*']\n")])))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    child = [r for r in provider.requests if "SUB-W" in r[1].stable]
    messages, system, tools = child[0]
    task = conv.tasks.get("t1")
    root = Path(task.worktree["path"])
    first = messages[0].text
    assert first.startswith("<worktree-context>") and str(root) in first and str(project.resolve()) in first
    assert first.rstrip().endswith("处理 a.txt")
    environment = next(item.text for item in system.supplements if item.kind == "environment")
    assert json.loads(environment)["working_directory"] == str(root)
    assert {tool.name for tool in tools} <= WORKSPACE_TOOLS
    blocked, protected = tool_results_of(child[1])
    assert blocked.code == "path_blocked" and not (project / "c.txt").exists()
    assert protected.code == "policy_protected" and not (root / ".mosscode" / "permissions.yaml").exists()
    await conv.close()


async def test_ac13_parameter_fork_plan_and_definition(tmp_path):
    project = repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "", "SUB-PLAIN")
    provider = ScriptProvider(isolated_script(lambda *a: response(calls=[
        call("write_file", "w1", path="b.txt", content="x")]), isolation="worktree"))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    assert conv.tasks.get("t1").worktree is not None and not (project / "b.txt").exists()
    await conv.close()

    def fork(messages, system, tools):
        if messages[-1].role == "user":
            return response(calls=[call("agent", "f1", description="x", prompt="p", isolation="worktree")])
        return response("好")
    provider = ScriptProvider(fork)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    assert tool_results_of(provider.requests[1])[0].code == "isolation_unsupported"
    await conv.close()

    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-PLAN")
    before = sorted((project / ".mosscode" / "worktrees").glob("agent-*"))
    provider = ScriptProvider(isolated_script(lambda *a: response("只读看了看")))
    conv = conversation(tmp_path, provider, mode="plan")
    await drain(conv.reply("开始"))
    result = tool_results_of(main_requests(provider)[1])[0]
    assert result.output.startswith("（计划模式下未创建工作树") and conv.tasks.get("t1").worktree is None
    assert sorted((project / ".mosscode" / "worktrees").glob("agent-*")) == before
    await conv.close()

    with pytest.raises(AgentError, match="isolation"):
        parse_agent(write_agent(tmp_path / "defs", "bad", "isolation: docker\n"), "project")
    assert parse_agent(write_agent(tmp_path / "defs", "good", "isolation: worktree\n"), "project").isolation == "worktree"


async def test_ac13_outside_a_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-W")
    provider = ScriptProvider(isolated_script(lambda *a: response("x")))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    assert tool_results_of(main_requests(provider)[1])[0].code == "worktree_unavailable"
    assert not conv.tasks.tasks
    await conv.close()


async def test_ac14_background_cancel_and_parallel(tmp_path):
    project = repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-W")
    gate = asyncio.Event()

    async def slow(messages, system, tools):
        await gate.wait()
        return response("不会到这里")

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            if len(messages) == 1:
                name = "one.txt" if "第一" in messages[0].text else "two.txt"
                return response(calls=[call("write_file", "w", path=name, content=name)])
            return slow(messages, system, tools)
        if messages[-1].role == "user":
            return response(calls=[
                call("agent", "a1", description="一", prompt="第一", agent_type="writer", run_in_background=True),
                call("agent", "a2", description="二", prompt="第二", agent_type="writer", run_in_background=True)])
        return response("好")
    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    started = tool_results_of(main_requests(provider)[1])
    assert [r.details["task"] for r in started] == ["t1", "t2"]
    for _ in range(200):
        if all(len([r for r in provider.requests if "SUB-W" in r[1].stable and r[0][0].text.endswith(word)]) == 2
               for word in ("第一", "第二")):
            break
        await asyncio.sleep(0.05)
    one, two = conv.tasks.get("t1"), conv.tasks.get("t2")
    assert one.worktree["path"] != two.worktree["path"]
    assert (Path(one.worktree["path"]) / "one.txt").is_file() and not (Path(one.worktree["path"]) / "two.txt").exists()
    await conv.tasks.stop("t1")
    await conv.tasks.stop("t2")
    await settle(conv)
    assert one.status == "cancelled" and one.worktree["outcome"] == "已保留"
    assert "[工作树] 已保留" in one.result and Path(one.worktree["path"]).is_dir()
    assert not load_meta(Path(project / ".mosscode" / "worktrees" / ".meta" / f"{one.worktree['name']}.json")).in_use
    await conv.close()


async def test_ac15_dirty_main_directory_is_mentioned(tmp_path):
    project = repo(tmp_path)
    (project / "a.txt").write_text("uncommitted\n", encoding="utf-8")
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-W")
    provider = ScriptProvider(isolated_script(lambda *a: response("看完了")))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    child = [r for r in provider.requests if "SUB-W" in r[1].stable]
    assert "主目录有未提交的改动" in child[0][0][0].text
    await conv.close()


# AC16/AC17: search and instructions -----------------------------------------------------------------------

@pytest.mark.parametrize("ripgrep", [True, False])
async def test_ac16_search_skips_worktrees_unless_asked(tmp_path, monkeypatch, ripgrep):
    if ripgrep and shutil.which("rg") is None:
        pytest.skip("ripgrep 未安装")
    project = project_of(tmp_path)
    (project / "src").mkdir()
    (project / "src" / "main.txt").write_text("needle", encoding="utf-8")
    inner = project / ".mosscode" / "worktrees" / "agent-1a2b3c4d" / "src"
    inner.mkdir(parents=True)
    (inner / "copy.txt").write_text("needle", encoding="utf-8")
    tools = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    if not ripgrep:  # After the registry: the shell is found with shutil.which too.
        from mosscode.tools import search
        monkeypatch.setattr(search.shutil, "which", lambda name: None)

    async def execute(name, **args):
        return (await tools.execute(ToolCall("c", name, json.dumps(args)))).output
    assert "copy.txt" not in await execute("glob", pattern="**/*.txt")
    assert "copy.txt" not in await execute("grep", pattern="needle")
    assert "main.txt" in await execute("grep", pattern="needle")
    inside = ".mosscode/worktrees/agent-1a2b3c4d"
    assert "copy.txt" in await execute("glob", pattern="**/*.txt", path=inside)
    assert "copy.txt" in await execute("grep", pattern="needle", path=inside)


def test_ac17_instructions_in_a_worktree_come_once(tmp_path):
    project = tmp_path / "project"
    worktree = project / ".mosscode" / "worktrees" / "demo"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: x\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("main", encoding="utf-8")
    (worktree / "AGENTS.md").write_text("copy", encoding="utf-8")
    found = instruction_candidates(worktree, tmp_path / "home")
    assert found == [worktree / "AGENTS.md"]
    assert project / "AGENTS.md" in instruction_candidates(project, tmp_path / "home")


# AC18: hooks ------------------------------------------------------------------------------------------------

async def test_ac18_hook_fields(tmp_path):
    project = repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-W")
    hooks = hook_engine(tmp_path, {"id": "after", "event": "post_tool_use", "action": {"type": "command",
                                                                                     "command": "echo ok"}},
                        {"id": "hi", "event": "subagent_start", "action": {"type": "prompt", "message": "HI"}},
                        {"id": "bye", "event": "subagent_stop", "action": {"type": "command", "command": "echo bye"}})
    seen, original = [], hooks.fire

    async def fire(event):
        seen.append(event)
        return await original(event)
    hooks.fire = fire
    provider = ScriptProvider(isolated_script(lambda *a: response(calls=[
        call("write_file", "w1", path="b.txt", content="x")])))
    conv = conversation(tmp_path, provider, hooks=hooks)
    await drain(conv.reply("开始"))
    await hooks.drain(1)
    task = conv.tasks.get("t1")
    write = next(e for e in seen if e.name == "post_tool_use" and e.fields.get("tool") == "write_file")
    assert write.fields["worktree"] == task.worktree["relative"] and write.fields["path"] == "b.txt"
    assert write.file_path == Path(task.worktree["path"]) / "b.txt"
    stop = next(e for e in seen if e.name == "subagent_stop")
    start = next(e for e in seen if e.name == "subagent_start")
    assert stop.fields["worktree_kept"] == "true" and start.fields["worktree"] == task.worktree["relative"]
    agent = next(e for e in seen if e.name == "post_tool_use" and e.fields.get("tool") == "agent")
    assert "worktree" not in agent.fields
    await conv.close()


# AC19/AC21: commands ------------------------------------------------------------------------------------------

async def test_ac19_worktree_command(tmp_path, monkeypatch):
    project = repo(tmp_path)
    conv = conversation(tmp_path, ScriptProvider(lambda *a: response("好")))
    ctx = context(conv)
    ui = await run(ctx, "/worktree create demo")
    assert ui.notes[-1].startswith("└ 已创建工作树 demo：.mosscode/worktrees/demo")
    calls = []
    original = worktree_git.run_git
    monkeypatch.setattr(worktree_git, "run_git", lambda *a, **k: calls.append(a) or original(*a, **k))
    await run(ctx, "/worktree create demo")
    assert ui.notes[-1].startswith("└ 工作树 demo 已存在") and not calls
    monkeypatch.setattr(worktree_git, "run_git", original)
    (project / ".mosscode" / "worktrees" / "demo" / "work.txt").write_text("w", encoding="utf-8")
    await run(ctx, "/worktree")
    assert "demo  手动  有改动（未提交 1 个文件）" in ui.notes[-1]
    await run(ctx, "/worktree remove demo")
    assert ui.notes[-1].startswith("! 工作树 demo 有改动") and "work.txt" in ui.notes[-1]
    await run(ctx, "/worktree remove demo --discard")
    assert ui.notes[-1].startswith("└ 已删除工作树 demo") and branches(project) == []
    await run(ctx, "/worktree create a..b")
    assert ui.notes[-1].startswith("! 名称 a..b 不可用")
    await run(ctx, "/worktree clean")
    assert ui.notes[-1].startswith("└ 已删除 0 个")
    await run(ctx, "/worktree bogus")
    assert ui.notes[-1].startswith("用法：/worktree")
    command = BUILTIN.get("worktree")
    assert command.runs_while_running("") and command.runs_while_running("list")
    assert not command.runs_while_running("create x") and not command.runs_while_running("clean")
    await conv.close()


async def test_ac21_tasks_detail_and_headless(tmp_path):
    repo(tmp_path)
    write_agent(agents_dir(tmp_path), "writer", "isolation: worktree\n", "SUB-W")
    provider = ScriptProvider(isolated_script(lambda *a: response("看完了")))
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    ui = await run(context(conv), "/tasks t1")
    assert "工作树" in ui.notes[-1] and "已删除" in ui.notes[-1] and "mosscode/agent-" in ui.notes[-1]
    await run(context(conv, ui=ui), "/agents writer")
    assert "隔离" in ui.notes[-1] and "worktree" in ui.notes[-1]
    await conv.close()
    with pytest.raises(SystemExit) as caught:
        cli._headless_input("/worktree", BUILTIN)
    assert caught.value.code == 2
    assert parse_input("/worktree list", BUILTIN).command.name == "worktree"


# AC20: configuration ---------------------------------------------------------------------------------------

def write_config(tmp_path, **extra):
    provider = {"name": "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "MOSS_TEST_KEY"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"providers": [provider], **extra}), encoding="utf-8")
    return path


def test_ac20_configuration(tmp_path):
    settings = load_settings(write_config(tmp_path))
    assert settings.worktree_links == () and settings.worktree_stale_hours == 24.0
    settings = load_settings(write_config(tmp_path, worktree_links=["node_modules/", "a\\b", "node_modules"],
                                          worktree_stale_hours=0))
    assert settings.worktree_links == ("node_modules", "a/b") and settings.worktree_stale_hours == 0
    for bad in ({"worktree_links": ["/abs"]}, {"worktree_links": ["C:/abs"]}, {"worktree_links": ["../up"]},
                {"worktree_links": "node_modules"}, {"worktree_links": [".git/hooks"]},
                {"worktree_stale_hours": -1}, {"worktree_stale_hours": 9000}, {"worktree_stale_hours": "1"}):
        with pytest.raises(ConfigError, match="worktree_"):
            load_settings(write_config(tmp_path, **bad))


def test_ac21_tool_row_names_the_worktree():
    from mosscode.tui import describe_call
    name, detail, _ = describe_call(call("agent", "a1", description="改", prompt="p", agent_type="general",
                                         isolation="worktree"))
    assert (name, detail) == ("Agent", "general · 工作树 · 改")
    assert describe_call(call("agent", "a1", description="改", prompt="p"))[1] == "fork · 改"
