"""Sub-agents (definitions, the agent tool, fork, tasks, notifications, hooks)."""

import asyncio
import json
from pathlib import Path

import pytest

from mosscode.agents import AgentCatalog, AgentError, parse_agent
from mosscode.agents.runtime import FORK_PLACEHOLDER, effective_mode
from mosscode.agents.tasks import Task
from mosscode.hooks import HookEngine
from mosscode.messages import NOTIFICATION, REMINDER, ApprovalRequest, Delta, TasksNotified, Usage
from mosscode.permissions import PermissionPolicy, stricter
from mosscode.prompts import PREFIX_KINDS
from mosscode.tools import create_default_registry
from mosscode.tools.execution import partition_calls
from mosscode.tools.files import FileState
from test_agent import call, response
from test_sessions import drain, make

# Helpers ------------------------------------------------------------------------------------------


def write_agent(base: Path, name: str, front: str = "", body: str = "你是测试用的子 Agent。") -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{name}.md"
    path.write_text(f"---\nname: {name}\ndescription: 测试 {name}\n{front}---\n{body}\n", encoding="utf-8")
    return path


class ScriptProvider:
    """Answers by who is asking: script(messages, system, tools) returns a response, or awaits one."""

    def __init__(self, script):
        self.script, self.requests, self.closed = script, [], 0

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append((tuple(messages), system, tuple(tools)))
        item = self.script(list(messages), system, tuple(tools))
        if asyncio.iscoroutine(item):
            item = await item
        if isinstance(item, Exception):
            raise item
        if item.message.text:
            yield Delta("text", item.message.text)
        yield item

    async def close(self):
        self.closed += 1


def project_of(tmp_path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


def agents_dir(tmp_path) -> Path:
    return project_of(tmp_path) / ".mosscode" / "agents"


def conversation(tmp_path, provider, *, mode="bypassPermissions", interactive=False, hooks=None, **options):
    project = project_of(tmp_path)
    registry = create_default_registry(project, permission_mode=mode, load_permissions=False)
    registry.permissions.interactive = interactive
    return make(tmp_path, provider, registry=registry, agents=AgentCatalog.load(project), hooks=hooks, **options)


def is_fork(messages) -> bool:
    return messages[-1].role == "user" and "<fork-directive>" in messages[-1].text


def asked(messages) -> str:
    """The prompt a sub-agent was given (its first user message)."""
    return messages[0].text


async def settle(conv):
    jobs = [task.job for task in conv.tasks.tasks.values() if task.job is not None]
    if jobs:
        await asyncio.wait(jobs, timeout=10)


def tool_results_of(request):
    """The tool results of the last tool message a request carried (notifications may follow it)."""
    return next(m for m in reversed(request[0]) if m.role == "tool").tool_results


def main_requests(provider, marker="SUB-"):
    return [r for r in provider.requests if marker not in r[1].stable and not is_fork(r[0])]


# Definitions --------------------------------------------------------------------------------


def test_three_places_override_and_problems(tmp_path, isolated_home):
    project = project_of(tmp_path)
    user = isolated_home / ".mosscode" / "agents"
    write_agent(user, "explore", body="USER-EXPLORE")
    write_agent(agents_dir(tmp_path), "explore", body="PROJECT-EXPLORE")
    write_agent(agents_dir(tmp_path), "alias", "disallowedTools: Bash\nmaxTurns: 3\npermissionMode: plan\ncolor: red\n")
    write_agent(agents_dir(tmp_path), "snake", "disallowed_tools: [Bash]\nmax_turns: 3\npermission_mode: plan\n")
    write_agent(agents_dir(tmp_path), "bad-field", "disalowed_tools: Bash\n")
    write_agent(agents_dir(tmp_path), "zero", "max_turns: 0\n")
    (agents_dir(tmp_path) / "empty.md").write_text("---\nname: empty\ndescription: x\n---\n", encoding="utf-8")
    (agents_dir(tmp_path) / "fork.md").write_text("---\nname: fork\ndescription: x\n---\n正文\n", encoding="utf-8")
    (agents_dir(tmp_path) / "z-copy.md").write_text("---\nname: snake\ndescription: x\n---\n正文\n", encoding="utf-8")
    catalog = AgentCatalog.load(project)
    assert {"explore", "plan", "general", "alias", "snake"} <= set(catalog.names())
    explore = catalog.get("explore")
    assert explore.prompt == "PROJECT-EXPLORE" and explore.source == "project" and len(explore.overrides) == 2
    alias, snake = catalog.get("alias"), catalog.get("snake")
    assert (
        (alias.disallowed_tools, alias.max_turns, alias.permission_mode)
        == (snake.disallowed_tools, snake.max_turns, snake.permission_mode)
        == (("execute_command",), 3, "plan")
    )
    assert alias.unsupported == ("color",)
    problems = "\n".join(catalog.problems)
    for name in ("bad-field.md", "zero.md", "empty.md", "fork.md", "z-copy.md"):
        assert name in problems
    assert "disalowed_tools" in problems


def test_tool_names_and_matching(tmp_path):
    path = write_agent(tmp_path, "t", "tools: Read, mcp__github__*\ndisallowed_tools: mcp__github__delete\n")
    agent = parse_agent(path, "project")
    assert agent.tools == ("read_file", "mcp__github__*")
    assert agent.allows("read_file") and agent.allows("mcp__github__list")
    assert not agent.allows("mcp__github__delete") and not agent.allows("write_file")
    assert agent.unknown_tools(["read_file"]) == ["mcp__github__*", "mcp__github__delete"]
    with pytest.raises(AgentError):
        parse_agent(write_agent(tmp_path, "m", "permission_mode: dontAsk\n"), "project")


async def test_unknown_type_and_missing_provider(tmp_path):
    write_agent(agents_dir(tmp_path), "remote", "model: nowhere\n")

    def script(messages, system, tools):
        if messages[-1].role == "user":
            return response(
                calls=[
                    call("agent", "a1", description="x", prompt="p", agent_type="nope"),
                    call("agent", "a2", description="x", prompt="p", agent_type="remote"),
                ]
            )
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    results = tool_results_of(provider.requests[1])
    assert results[0].code == "unknown_agent_type" and "explore" in results[0].output
    assert results[1].code == "agent_unavailable" and "nowhere" in results[1].output
    assert conv.tasks.tasks == {}
    await conv.close()


# A definition sub-agent --------------------------------------------------------------------------


async def test_definition_runs_in_a_blank_conversation(tmp_path):
    write_agent(agents_dir(tmp_path), "worker", "tools: Read, Grep\n", "SUB-WORKER")
    (project_of(tmp_path) / "AGENTS.md").write_text("PROJECT-RULE", encoding="utf-8")
    (project_of(tmp_path) / "a.txt").write_text("hello", encoding="utf-8")

    def script(messages, system, tools):
        if "SUB-WORKER" in system.stable:
            if len(messages) == 1:
                return response("读一下", [call("read_file", "r1", path="a.txt")], Usage(10, 2))
            return response("WORKER-DONE", usage=Usage(20, 3))
        if messages[-1].role == "user":
            return response(
                calls=[call("agent", "a1", description="读文件", prompt="读取 a.txt", agent_type="worker")],
                usage=Usage(5, 1),
            )
        return response("主对话完成", usage=Usage(5, 1))

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    child = [r for r in provider.requests if "SUB-WORKER" in r[1].stable]
    messages, system, tools = child[0]
    assert [m.role for m in messages] == ["user"] and asked(messages) == "读取 a.txt"
    assert "PROJECT-RULE" in system.stable and "## 子 Agent" not in system.stable
    assert {tool.name for tool in tools} == {"read_file", "grep"}
    main = main_requests(provider)
    assert "## 子 Agent" in main[0][1].stable
    assert {"agent", "task_output", "task_stop"} <= {tool.name for tool in main[0][2]}
    result = tool_results_of(main[1])[0]
    assert result.output.startswith("WORKER-DONE") and "工具调用 1 次" in result.output and not result.is_error
    # Usage counted in the task and the session; its own transcript.
    task = conv.tasks.get("t1")
    assert task.status == "completed" and task.usage.input_tokens == 30
    assert conv.total_usage.input_tokens == 40
    records = [json.loads(line) for line in task.transcript.read_text(encoding="utf-8").splitlines()]
    assert records[0]["type"] == "subagent" and records[0]["agent_type"] == "worker"
    assert [r["message"]["role"] for r in records[1:]] == ["user", "assistant", "tool", "assistant"]
    assert all(m.role != "tool" or m.tool_results[0].name != "read_file" for m in conv.history)
    await conv.close()


async def test_a_sub_agent_keeps_its_images_beside_its_own_transcript(tmp_path_factory):
    from PIL import Image

    from mosscode.session import load_session

    # .../projects/<key>/sessions/<id>/subagents/t1/images/<sha>.png is the deepest path the suite writes; under
    # tmp_path (named after this long test) an in-repo basetemp of 10+ characters pushes it past Windows' MAX_PATH.
    tmp_path = tmp_path_factory.mktemp("img")
    write_agent(agents_dir(tmp_path), "worker", "tools: Read\n", "SUB-WORKER")
    Image.new("RGB", (4, 3), "red").save(project_of(tmp_path) / "shot.png")

    def script(messages, system, tools):
        if "SUB-WORKER" in system.stable:
            if len(messages) == 1:
                return response(calls=[call("read_file", "r1", path="shot.png")])
            return response("看到了")
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a1", description="看图", prompt="看 shot.png", agent_type="worker")])
        return response("完成")

    conv = conversation(tmp_path, ScriptProvider(script))
    await drain(conv.reply("开始"))
    task = conv.tasks.get("t1")
    (image,) = [i for m in load_session(task.transcript).history for r in m.tool_results for i in r.images]
    assert image.path == task.transcript.with_suffix("") / "images" / image.name and image.path.is_file()
    assert load_session(task.transcript).lost_images == 0
    assert not (conv.session.directory / "images").exists()
    await conv.close()


async def test_max_turns_and_global_limit(tmp_path):
    write_agent(agents_dir(tmp_path), "loop", "max_turns: 2\n", "SUB-LOOP")
    write_agent(agents_dir(tmp_path), "big", "max_turns: 999\n", "SUB-BIG")

    def script(messages, system, tools):
        if "SUB-" in system.stable:
            return response("还在找", [call("glob", f"g{len(messages)}", pattern="*.txt")])
        if messages[-1].role == "user":
            return response(
                calls=[
                    call("agent", "a1", description="x", prompt="p", agent_type="loop"),
                    call("agent", "a2", description="x", prompt="p", agent_type="big"),
                ]
            )
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, max_iterations=3)
    await drain(conv.reply("开始"))
    results = tool_results_of(main_requests(provider)[1])
    assert results[0].output.startswith("[子 Agent 未完成：达到最大轮数 2]") and "还在找" in results[0].output
    assert results[1].output.startswith("[子 Agent 未完成：达到最大轮数 3]")
    assert conv.tasks.get("t1").status == "failed"
    await conv.close()


# Fork -------------------------------------------------------------------------------------------


async def test_fork_shares_the_prefix_and_is_refused_nesting(tmp_path):
    def script(messages, system, tools):
        if is_fork(messages):
            if "任务：嵌套" in messages[-1].text:
                return response(calls=[call("agent", "n1", description="x", prompt="p")])
            return response("范围：" + messages[-1].text[-3:])
        if messages[-1].role == "tool" and messages[-1].tool_results[0].call_id == "n1":
            return response("范围：嵌套被拒绝")
        if len(messages) == 1:
            return response(
                "分两个出去",
                [
                    call("agent", "f1", description="写测试", prompt="写测试"),
                    call("agent", "f2", description="写文档", prompt="写文档"),
                    call("agent", "f3", description="嵌套", prompt="嵌套"),
                ],
            )
        return response("已分出")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("讨论一下"))
    main = main_requests(provider)
    started = tool_results_of(main[1])
    assert [r.details.get("task") for r in started] == ["t1", "t2", "t3"]
    assert all(r.details["background"] for r in started)
    await settle(conv)
    forks = [r for r in provider.requests if is_fork(r[0])]
    first, second = forks[0], forks[1]
    parent_messages, parent_system, parent_tools = main[0]
    # The same system prompt, prefix supplements and tools; history up to the assistant message.
    for messages, system, tools in (first, second):
        assert system.stable == parent_system.stable and tools == parent_tools
        prefix = lambda s: [item for item in s.supplements if item.kind in PREFIX_KINDS]
        assert prefix(system) == prefix(parent_system)
        assert messages[:1] == parent_messages and messages[1] == conv.history[1]
        assert [r.output for r in messages[2].tool_results] == [FORK_PLACEHOLDER] * 3
    assert first[0][:-1] == second[0][:-1] and first[0][-1] != second[0][-1]
    nested = next(r for r in provider.requests if r[0][-1].role == "tool" and r[0][-1].tool_results[0].call_id == "n1")
    assert nested[0][-1].tool_results[0].code == "subagent_forbidden"
    assert {conv.tasks.get(i).status for i in ("t1", "t2", "t3")} == {"completed"}
    await conv.close()


# File state -------------------------------------------------------------------------------------


async def test_file_state_per_sub_agent(tmp_path):
    from test_agent import call as make_call

    project = project_of(tmp_path)
    (project / "a.txt").write_text("one", encoding="utf-8")
    main = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    assert not (await main.execute(make_call("read_file", "r", path="a.txt"))).is_error
    fresh = main.for_agent(agent="subagent", agent_type="w", permissions=main.permissions)
    edited = await fresh.execute(make_call("edit_file", "e", path="a.txt", old_text="one", new_text="two"))
    assert edited.is_error  # Never read in this sub-agent.
    fork = main.for_agent(agent="fork", agent_type="fork", permissions=main.permissions, state=main.file_state().copy())
    assert not (await fork.execute(make_call("edit_file", "e", path="a.txt", old_text="one", new_text="two"))).is_error
    stale = await main.execute(make_call("edit_file", "e", path="a.txt", old_text="two", new_text="three"))
    assert stale.code == "stale_read"
    assert main.file_state() is not fork.file_state() and isinstance(fresh.file_state(), FileState)


# Permissions --------------------------------------------------------------------------------


def test_effective_mode():
    from mosscode.agents import AgentDef

    def agent(source, mode):
        return AgentDef("x", "d", "p", Path("x.md"), source, permission_mode=mode)

    assert effective_mode(agent("user", "bypassPermissions"), "plan") == "plan"
    assert effective_mode(agent("project", "bypassPermissions"), "default") == "default"
    assert effective_mode(agent("project", "plan"), "default") == "plan"
    assert effective_mode(agent("user", "bypassPermissions"), "default") == "bypassPermissions"
    assert effective_mode(agent("builtin", None), "acceptEdits") == "acceptEdits"
    assert effective_mode(None, "acceptEdits") == "acceptEdits"
    assert stricter("acceptEdits", "default") == "default"


async def test_views_keep_their_own_grants_and_background_never_asks(tmp_path):
    policy = PermissionPolicy(tmp_path, mode="default", load=False)
    policy.interactive = True
    background = policy.view("default", interactive=False, label="子 Agent x")
    registry = create_default_registry(tmp_path, permissions=background)
    result = await registry.execute(call("write_file", "w", path="a.txt", content="x"))
    assert result.code == "approval_required" and "后台子 Agent" in result.output
    assert policy.requests.empty() and background.requests.empty() and not policy.pending
    foreground = policy.view("default", interactive=True, label="子 Agent x")
    foreground.session.append(object())
    assert policy.session == [] and background.session == []


async def test_plan_session_blocks_general_writes(tmp_path):
    def script(messages, system, tools):
        if (
            "你是通用" in system.stable
            or "general" in system.stable.lower()
            and len(messages) == 1
            and messages[0].text == "写文件"
        ):
            pass
        # The child's first request: its prompt, then the plan-mode reminder it inherits.
        if asked(messages) == "写文件" and all(m.role == "user" for m in messages):
            return response(calls=[call("write_file", "w", path="a.txt", content="x")])
        if messages[0].text == "写文件":
            return response("SUB-结束")
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a1", description="写", prompt="写文件", agent_type="general")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, mode="plan")
    await drain(conv.reply("开始"))
    child = [r for r in provider.requests if r[0][0].text == "写文件"]
    assert child[0][0][-1].synthetic == REMINDER and "计划模式" in child[0][0][-1].text
    assert tool_results_of(child[1])[0].code == "plan_blocked"
    assert "write_file" not in {tool.name for tool in child[0][2]}
    await conv.close()


async def test_foreground_approval_goes_to_the_shared_queue_and_detach_denies(tmp_path):
    write_agent(agents_dir(tmp_path), "writer", "", "SUB-WRITER")

    def script(messages, system, tools):
        if "SUB-WRITER" in system.stable:
            if len(messages) == 1:
                return response(calls=[call("write_file", "w", path=f"{asked(messages)}.txt", content="x")])
            return response(
                "写完：" + messages[-1].tool_results[0].code if messages[-1].tool_results[0].is_error else "写完"
            )
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a1", description="写一个", prompt="one", agent_type="writer")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, mode="default", interactive=True)
    requests = []
    async for event in conv.reply("开始"):
        if isinstance(event, ApprovalRequest):
            requests.append(event)
            conv.registry.permissions.respond(event.id, "session")
    assert len(requests) == 1 and requests[0].source == "子 Agent writer · 写一个"
    assert (project_of(tmp_path) / "one.txt").exists()
    assert conv.registry.permissions.session == []  # Its grant stays in the sub-agent.
    # Moving to the background answers a waiting approval with "deny".
    provider.requests.clear()
    stream = conv.reply("再来")
    async for event in stream:
        if isinstance(event, ApprovalRequest):
            assert conv.tasks.detach_foreground() == 1
    await settle(conv)
    assert "已转入后台" in tool_results_of(main_requests(provider)[1])[0].output
    assert conv.tasks.get("t2").result.startswith("写完：approval_required")
    await conv.close()


# Parallel ---------------------------------------------------------------------------------------


def test_adjacent_agent_calls_group(tmp_path):
    conv = conversation(tmp_path, ScriptProvider(lambda *a: response("x")))
    calls = [
        call("agent", "a"),
        call("agent", "b"),
        call("read_file", "r"),
        call("glob", "g"),
        call("agent", "c"),
        call("write_file", "w"),
        call("agent", "d"),
    ]
    assert list(partition_calls(calls, conv.registry)) == [[0, 1], [2, 3], [4], [5], [6]]


async def test_three_agents_run_at_once(tmp_path):
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")
    arrived, gate = [], asyncio.Event()

    async def child(messages):
        arrived.append(asked(messages))
        if len(arrived) == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), 5)
        return response("结果 " + asked(messages))

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            return child(messages)
        if messages[-1].role == "user":
            return response(
                calls=[call("agent", f"a{i}", description="x", prompt=str(i), agent_type="w") for i in range(3)]
            )
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    results = tool_results_of(main_requests(provider)[1])
    assert [r.output.split("\n")[0] for r in results] == ["结果 0", "结果 1", "结果 2"]
    await conv.close()


# Background and notifications -------------------------------------------------------------


async def test_background_limits_and_auto_background(tmp_path):
    write_agent(agents_dir(tmp_path), "slow", "", "SUB-SLOW")
    write_agent(agents_dir(tmp_path), "bg", "background: true\n", "SUB-BG")
    gate = asyncio.Event()

    async def wait():
        await gate.wait()
        return response("慢完成")

    def script(messages, system, tools):
        if "SUB-" in system.stable:
            return wait()
        if messages[-1].role == "user" and messages[-1].text == "开始":
            return response(
                calls=[
                    call("agent", "a1", description="x", prompt="p", agent_type="slow", run_in_background=True),
                    call("agent", "a2", description="x", prompt="p", agent_type="bg"),
                    call("agent", "a3", description="x", prompt="p", agent_type="slow", run_in_background=True),
                ]
            )
        if messages[-1].role == "user":
            return response(calls=[call("agent", "b1", description="前台", prompt="p", agent_type="slow")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, agent_max_background=2, agent_auto_background=0.3)
    await drain(conv.reply("开始"))
    results = tool_results_of(main_requests(provider)[1])
    assert [r.details.get("task") for r in results[:2]] == ["t1", "t2"] and results[2].code == "too_many_tasks"
    provider.requests.clear()
    await drain(conv.reply("前台"))
    result = tool_results_of(main_requests(provider)[1])[0]
    assert "已转入后台：任务 t3" in result.output and conv.tasks.get("t3").background
    gate.set()
    await settle(conv)
    assert len(conv.notifications) == 3
    await conv.close()


async def test_notifications_idle_and_busy(tmp_path):
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")
    gate = asyncio.Event()

    async def child():
        await gate.wait()
        return response("后台结论")

    async def busy_main(conv):
        gate.set()
        await settle(conv)
        return response(calls=[call("glob", "g", pattern="*")])

    holder = {}

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            return child()
        last = messages[-1]
        if last.role == "user" and last.synthetic == NOTIFICATION:
            return response("收到通知")
        if last.role == "user" and last.text == "空闲":
            return response(
                calls=[call("agent", "a1", description="空闲任务", prompt="p", agent_type="w", run_in_background=True)]
            )
        if last.role == "user" and last.text == "忙":
            return response(
                calls=[call("agent", "b1", description="忙任务", prompt="p", agent_type="w", run_in_background=True)]
            )
        if last.role == "tool" and last.tool_results[0].call_id == "b1":
            return busy_main(holder["conv"])
        return response("好")

    provider = ScriptProvider(script)
    conv = holder["conv"] = conversation(tmp_path, provider)
    ready = []
    conv.on_tasks = lambda: ready.append(len(conv.notifications))
    await drain(conv.reply("空闲"))
    assert ready == []
    gate.set()
    await settle(conv)
    assert ready == [1]
    events = await drain(conv.notify())
    sent = provider.requests[-1][0]
    assert sent[-1].synthetic == NOTIFICATION and "<task-notification>" in sent[-1].text
    assert "任务：t1" in sent[-1].text and "后台结论" in sent[-1].text and "已完成" in sent[-1].text
    assert conv.notifications == () and await drain(conv.notify()) == []
    # Busy: merged at the next iteration boundary, once.
    gate.clear()
    ready.clear()
    events = await drain(conv.reply("忙"))
    assert any(isinstance(e, TasksNotified) for e in events) and ready == []
    merged = [m for m in conv.history if m.synthetic == NOTIFICATION]
    assert len(merged) == 2 and "任务：t2" in merged[1].text
    await conv.close()


async def test_stopped_tasks_never_notify(tmp_path):
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            return asyncio.Event().wait()
        last = messages[-1]
        if last.role == "user":
            return response(
                calls=[call("agent", "a1", description="x", prompt="p", agent_type="w", run_in_background=True)]
            )
        if last.tool_results[0].call_id == "a1":
            return response(calls=[call("task_stop", "s1", task_id="t1"), call("task_output", "o1", task_id="t1")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    stop, output = tool_results_of(main_requests(provider)[2])
    assert stop.output == "已停止任务 t1。" and "已取消" in output.output
    assert conv.tasks.get("t1").status == "cancelled" and conv.notifications == ()
    await conv.close()


# Transcripts, cancellation and session switches -------------------------------------------


async def test_no_transcript_without_persistence_and_resume_ignores_tasks(tmp_path):
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            return response("子结论")
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a1", description="x", prompt="p", agent_type="w")])
        return response("好")

    conv = conversation(tmp_path, ScriptProvider(script), persist=False)
    await drain(conv.reply("开始"))
    assert conv.tasks.get("t1").transcript is None
    await conv.close()
    saved = conversation(tmp_path, ScriptProvider(script))
    await drain(saved.reply("开始"))
    session_id = saved.session_id
    lines = [json.loads(line) for line in saved.session.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in lines if r.get("type") == "task"] == ["start", "end"]
    await saved.close()
    restored = conversation(tmp_path, ScriptProvider(script))
    await drain(restored.resume(session_id))
    assert [m.role for m in restored.history] == ["user", "assistant", "tool", "assistant"]
    assert restored.tasks.tasks == {}
    await restored.close()


async def test_cancel_foreground_and_clear_cancels_background(tmp_path):
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            return hang()
        if messages[-1].role == "user" and asked(messages[-1:]) == "后台":
            return response(
                calls=[call("agent", "a1", description="x", prompt="p", agent_type="w", run_in_background=True)]
            )
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a2", description="x", prompt="p", agent_type="w")])
        return response("好")

    conv = conversation(tmp_path, ScriptProvider(script))
    await drain(conv.reply("后台"))
    background = conv.tasks.get("t1")
    turn = asyncio.create_task(drain(conv.reply("前台")))
    started.clear()
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)
    foreground = conv.tasks.get("t2")
    assert foreground.status == "cancelled" and background.status == "running"
    tool = [m for m in conv.history if m.role == "tool"][-1].tool_results[0]
    assert tool.output.startswith("[子 Agent 未完成：已取消]")
    await conv.clear()
    assert background.status == "cancelled" and conv.tasks.tasks == {} and conv.notifications == ()
    await conv.close()


# Hooks -------------------------------------------------------------------------------


def hook_engine(tmp_path, *hooks):
    import yaml

    home = tmp_path / "hook-home"
    (home / ".mosscode").mkdir(parents=True, exist_ok=True)
    (home / ".mosscode" / "hooks.yaml").write_text(
        yaml.safe_dump({"hooks": list(hooks)}, allow_unicode=True), encoding="utf-8"
    )
    return HookEngine(project_of(tmp_path), home=home)


async def test_hook_fields_and_subagent_events(tmp_path):
    write_agent(agents_dir(tmp_path), "explorer", "", "SUB-EXP")
    hooks = hook_engine(
        tmp_path,
        {
            "id": "no-glob",
            "event": "pre_tool_use",
            "if": "agent == subagent && agent_type == explorer && tool == glob",
            "reject": "子 Agent 不能 glob",
        },
        {
            "id": "hello",
            "event": "subagent_start",
            "if": "agent_type == explorer",
            "action": {"type": "prompt", "message": "START-NOTE {{task_id}}"},
        },
        {"id": "turn", "event": "turn_start", "action": {"type": "prompt", "message": "TURN-NOTE"}},
        {"id": "bye", "event": "subagent_stop", "action": {"type": "command", "command": "echo bye"}},
    )
    seen = []
    original = hooks.fire

    async def fire(event):
        seen.append((event.name, event.agent, dict(event.fields), event.agent_type))
        return await original(event)

    hooks.fire = fire

    def script(messages, system, tools):
        if "SUB-EXP" in system.stable:
            if len(messages) == 1:
                return response(calls=[call("glob", "g", pattern="*")])
            return response("找完")
        if messages[-1].role == "user":
            return response(calls=[call("agent", "a1", description="找", prompt="找文件", agent_type="explorer")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, hooks=hooks)
    await drain(conv.reply("开始"))
    await hooks.drain(1)
    child = [r for r in provider.requests if "SUB-EXP" in r[1].stable]
    first = child[0][0][0].text
    assert first.index("START-NOTE t1") < first.index("找文件")
    assert tool_results_of(child[1])[0].code == "hook_rejected"
    for _, _, supplements in ((r[0], r[1], r[1].supplements) for r in child):
        assert "TURN-NOTE" not in " ".join(item.text for item in supplements)
    names = [(name, agent) for name, agent, *_ in seen]
    assert ("pre_tool_use", "subagent") in names and ("turn_start", "subagent") not in names
    start = next(item for item in seen if item[0] == "subagent_start")
    stop = next(item for item in seen if item[0] == "subagent_stop")
    assert start[1] == "main" and start[2]["agent_type"] == "explorer" and stop[2]["status"] == "completed"
    assert next(item for item in seen if item[:2] == ("pre_tool_use", "subagent"))[3] == "explorer"
    await conv.close()


async def test_agent_action_runs_in_the_background(tmp_path):
    write_agent(agents_dir(tmp_path), "checker", "", "SUB-CHECK")
    hooks = hook_engine(
        tmp_path,
        {
            "id": "review",
            "event": "post_tool_use",
            "if": "tool == glob",
            "action": {"type": "agent", "agent_type": "checker", "prompt": "检查 {{tool}}"},
        },
    )

    def script(messages, system, tools):
        if "SUB-CHECK" in system.stable:
            if len(messages) == 1:
                return response(calls=[call("glob", "g2", pattern="*")])
            return response("检查通过")
        if messages[-1].role == "user":
            return response(calls=[call("glob", "g1", pattern="*")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, hooks=hooks)
    await drain(conv.reply("开始"))
    await hooks.drain(5)
    task = conv.tasks.get("t1")
    assert (
        task.origin == "hook"
        and task.status == "completed"
        and asked([r for r in provider.requests if "SUB-CHECK" in r[1].stable][0][0]) == "检查 glob"
    )
    assert all("检查通过" not in m.text for m in conv.history)
    log = [json.loads(line) for line in hooks.log_path.read_text(encoding="utf-8").splitlines()]
    outcomes = [(r["hook"], r["outcome"]) for r in log]
    assert ("review", "ok") in outcomes and ("review", "skipped") in outcomes  # The nested one.
    assert "检查通过" in next(r["detail"] for r in log if r["outcome"] == "ok")
    assert conv.tasks.get("t2") is None
    await conv.close()


async def test_agent_action_timeout(tmp_path):
    write_agent(agents_dir(tmp_path), "checker", "", "SUB-CHECK")
    hooks = hook_engine(
        tmp_path,
        {
            "id": "review",
            "event": "post_tool_use",
            "action": {"type": "agent", "agent_type": "checker", "prompt": "检查", "timeout": 1},
        },
    )

    def script(messages, system, tools):
        if "SUB-CHECK" in system.stable:
            return asyncio.Event().wait()
        if messages[-1].role == "user":
            return response(calls=[call("glob", "g1", pattern="*")])
        return response("好")

    conv = conversation(tmp_path, ScriptProvider(script), hooks=hooks)
    await drain(conv.reply("开始"))
    await hooks.drain(5)
    assert conv.tasks.get("t1").status == "cancelled"
    log = [json.loads(line) for line in hooks.log_path.read_text(encoding="utf-8").splitlines()]
    assert log[-1]["outcome"] == "timeout"
    await conv.close()


def test_task_label():
    task = Task("t1", "agent", "subagent", "explore", "查找配置", "p")
    assert task.label == "explore · 查找配置" and task.status == "running"


# Commands ---------------------------------------------------------------------------


def hanging_conversation(tmp_path, gate=None):
    """The main agent starts one background sub-agent of type w, which waits for the gate."""
    write_agent(agents_dir(tmp_path), "w", "", "SUB-W")
    gate = gate or asyncio.Event()

    async def child():
        await gate.wait()
        return response("子结论")

    def script(messages, system, tools):
        if "SUB-W" in system.stable:
            return child()
        last = messages[-1]
        if last.role == "user" and last.synthetic == NOTIFICATION:
            return response("收到通知")
        if last.role == "user":
            return response(
                calls=[
                    call(
                        "agent",
                        "a1",
                        description="子任务",
                        prompt="p",
                        agent_type="w",
                        run_in_background=last.text != "前台",
                    )
                ]
            )
        return response("主对话完成")

    provider = ScriptProvider(script)
    return conversation(tmp_path, provider), provider, gate


async def test_agents_command(tmp_path):
    from test_commands import FakeUI, context, run

    write_agent(
        agents_dir(tmp_path), "worker", "tools: Read, mcp__x__y\nmodel: nowhere\ncolor: red\n", "第一行\n第二行"
    )
    write_agent(agents_dir(tmp_path), "bad", "max_turns: 0\n")
    conv = conversation(tmp_path, ScriptProvider(lambda *a: response("x")))
    ui = FakeUI()
    ctx = context(conv, ui=ui)
    await run(ctx, "/agents")
    listing = ui.notes[-1]
    assert "worker" in listing and "[模型 nowhere 不存在]" in listing and "explore" in listing
    assert "! Agent 定义已跳过" in listing and "bad.md" in listing
    await run(ctx, "/agents worker")
    detail = ui.notes[-1]
    for part in (
        "可用工具",
        "read_file",
        "mcp__x__y",
        "color",
        "本会话实际为 bypassPermissions",
        "提示词首行    第一行",
        "没有该提供商",
    ):
        assert part in detail, part
    assert "write_file" not in detail.split("可用工具")[1].splitlines()[0]
    await run(ctx, "/agents nope")
    assert ui.notes[-1].startswith("未找到 Agent 定义 nope")
    write_agent(agents_dir(tmp_path), "extra", "", "EXTRA")
    (agents_dir(tmp_path) / "worker.md").unlink()
    await run(ctx, "/agents reload")
    assert "新增 extra" in ui.notes[-1] and "移除 worker" in ui.notes[-1]
    assert conv.agents.get("extra") is not None and "extra" in conv._agent.agent_catalog
    await conv.close()


async def test_tasks_command_and_status(tmp_path):
    from test_commands import FakeUI, context, run

    conv, provider, gate = hanging_conversation(tmp_path)
    ui = FakeUI()
    ctx = context(conv, ui=ui)
    await run(ctx, "/tasks")
    assert ui.notes[-1] == "本会话还没有任务。"
    await drain(conv.reply("开始"))
    await run(ctx, "/tasks")
    assert "t1" in ui.notes[-1] and "运行中  后台  w · 子任务" in ui.notes[-1]
    await run(ctx, "/status")
    assert "子 Agent" in ui.notes[-1] and "后台任务 1 个运行中" in ui.notes[-1]
    await run(ctx, "/tasks t1")
    assert "任务内容" in ui.notes[-1] and "/tasks stop t1" in ui.notes[-1] and "缓存命中" in ui.notes[-1]
    await run(ctx, "/tasks stop t1")
    assert ui.notes[-1].startswith("└ 已停止任务 t1")
    await run(ctx, "/tasks stop t1")
    assert "已结束（已取消）" in ui.notes[-1]
    await run(ctx, "/tasks t9")
    assert ui.notes[-1].startswith("没有任务 t9")
    assert conv.notifications == ()
    await conv.close()


def test_m3_commands_exit_2_in_headless_mode(capsys):
    from mosscode.cli import _headless_input

    for text in ("/agents", "/tasks stop t1"):
        with pytest.raises(SystemExit) as exc:
            _headless_input(text)
        assert exc.value.code == 2
    assert "只能在交互界面中使用" in capsys.readouterr().err


async def test_clear_and_exit_ask_first(tmp_path):
    from test_commands import FakeUI, context, run

    conv, provider, gate = hanging_conversation(tmp_path)
    ui = FakeUI()
    ctx = context(conv, ui=ui)
    await drain(conv.reply("开始"))
    await run(ctx, "/clear")
    title = ui.calls[-1][1]
    assert ui.calls[-1][0] == "choose" and "1 个后台任务" in title and ui.calls[-1][3] == 1
    await ui.pick(1)  # 返回
    assert conv.tasks.get("t1").status == "running" and ("clear_screen",) not in ui.calls
    await run(ctx, "/exit")
    await ui.pick(0)
    assert ui.calls[-1] == ("exit",) and conv.tasks.get("t1").status == "cancelled"
    assert ui.notes[-1] == "└ 已取消 1 个后台任务。"
    await run(ctx, "/clear")  # Nothing running: no question.
    assert ("clear_screen",) in ui.calls
    await conv.close()


# -p --------------------------------------------------------------------------------------------


async def test_headless_waits_and_delivers_notifications(tmp_path):
    from mosscode.cli import _task_notices, _turns

    conv, provider, gate = hanging_conversation(tmp_path)
    said = []
    _task_notices(conv, lambda kind, text: said.append((kind, text)))
    asyncio.get_running_loop().call_later(0.2, gate.set)
    events = []
    async for event in _turns(conv, "开始", command=None, fork=None):
        events.append(event)
    assert said == [("notice", "后台任务 t1（w · 子任务）已启动"), ("notice", "后台任务 t1（w · 子任务）已完成")]
    last = provider.requests[-1][0][-1]
    assert last.synthetic == NOTIFICATION and "子结论" in last.text
    assert conv.history[-1].text == "收到通知" and not conv.tasks.running()
    await conv.close()


# The TUI ------------------------------------------------------------------------------


async def wait_until(pilot, check, limit=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not check():
        assert loop.time() < end, "timed out"
        await pilot.pause(0.05)


async def test_tui_ctrl_b_detaches_and_the_notification_starts_a_turn(tmp_path):
    from mosscode.tui import MossCodeApp, ToolEntry

    conv, provider, gate = hanging_conversation(tmp_path)
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._start_request("前台")
        await wait_until(pilot, lambda: conv.tasks.get("t1") is not None)
        task = conv.tasks.get("t1")
        assert not task.background
        await wait_until(pilot, lambda: "Ctrl+B" in str(app.query_one("#status").render()))
        await pilot.press("ctrl+b")
        await wait_until(pilot, lambda: not app._task_running)
        rows = [row for row in app.query(ToolEntry) if row.operation == "agent"]
        assert rows and "后台 · 任务 t1" in rows[0].transcript.plain
        assert "后台任务 1" in str(app.query_one("#status").render())
        gate.set()
        await wait_until(pilot, lambda: conv.history and conv.history[-1].text == "收到通知")
        await wait_until(pilot, lambda: not app._task_running)
        notes = [str(note.render()) for note in app.query(".local-note")]
        assert "└ 后台任务 t1（w · 子任务）已启动" in notes and "└ 后台任务 t1（w · 子任务）已完成" in notes
        assert sum(m.synthetic == NOTIFICATION for m in conv.history) == 1


def test_resumed_rows_show_notifications_as_one_line():
    from mosscode.messages import ContentBlock, Message
    from mosscode.tui import resumed_rows

    text = "<task-notification>\n任务：t2\n类型：fork\n</task-notification>"
    rows = resumed_rows([Message("user", (ContentBlock("text", text),), synthetic=NOTIFICATION)])
    assert rows[0].plain == "└ 已收到后台任务通知：t2"


def test_latest_call_keeps_the_file_name(tmp_path, monkeypatch):
    from mosscode.agents.runtime import describe_call

    monkeypatch.chdir(tmp_path)
    inside = str(tmp_path / "src" / ("a" * 70) / "util.py")
    assert (
        describe_call(call("read_file", "c1", path=str(tmp_path / "src" / "util.py")))
        == f"read_file {Path('src/util.py')}"
    )
    assert describe_call(call("read_file", "c2", path=inside)).endswith("util.py")
    assert describe_call(call("grep", "c3", pattern="old_name", path=str(tmp_path))) == "grep old_name"
    assert describe_call(call("execute_command", "c4", command="git status")) == "execute_command git status"


async def test_exit_after_cancelling_drops_late_notes(tmp_path):
    from mosscode.tui import MossCodeApp

    conv, provider, gate = hanging_conversation(tmp_path)
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._start_request("开始")
        await wait_until(pilot, lambda: conv.tasks.background() and not app._task_running)
        await pilot.press("ctrl+q")
        await wait_until(pilot, lambda: app.query_one("#model-panel").display)
        app.query_one("#models").highlighted = 0
        await pilot.press("enter")  # 取消这些任务并继续: the cancelled task's note arrives after exit began.
        await wait_until(pilot, lambda: app._exit)
    assert conv.tasks.get("t1").status == "cancelled"


async def test_tasks_and_status_work_during_a_run(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea

    conv, provider, gate = hanging_conversation(tmp_path)
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._start_request("前台")
        await wait_until(pilot, lambda: conv.tasks.get("t1") is not None)
        field = app.query_one("#prompt", PromptArea)
        field.post_message(PromptArea.Submitted(field, "/help"))  # Other commands still wait.
        await pilot.pause()
        assert "其他命令请在完成后发送" in str(app.query_one("#status").render())
        for command in ("/status", "/tasks", "/tasks stop t1"):
            field.post_message(PromptArea.Submitted(field, command))
            await pilot.pause()
        await wait_until(pilot, lambda: not app._task_running)
        notes = [str(note.render()) for note in app.query(".local-note")]
        assert any(note.startswith("任务（1）") for note in notes)
        assert "└ 已停止任务 t1（w · 子任务）；不会再收到它的通知。" in notes
        assert conv.tasks.get("t1").status == "cancelled" and conv.history[-1].text == "主对话完成"
        assert any("子 Agent" in note and "权限" in note for note in notes)  # /status
