"""Agent teams. Storage, locks, mailboxes and the shared task list."""

import json
import os
import threading
import time

import pytest

from mosscode.teams import (LEAD, Member, TeamError, TeamStore, list_teams, teams_root, validate_member_name,
                            validate_team_name)
from mosscode.teams import lock as lock_module
from mosscode.teams.board import TaskBoard, blocks
from mosscode.teams.lock import file_lock
from mosscode.teams.mailbox import KEEP_READ, Mailbox, Message, check


def text(sender: str, body: str = "hi", summary: str = "s") -> Message:
    return Message(sender, "text", body, summary)


@pytest.fixture
def board(tmp_path):
    return TaskBoard(tmp_path / "tasks.json")


ROSTER = ("alice", "bob")



@pytest.mark.parametrize("name", ["auth", "a", "team-2", "0x", "a" * 32])
def test_valid_names(name):
    assert validate_team_name(name) is None
    assert validate_member_name(name) is None


@pytest.mark.parametrize("name", ["", "Auth", "-a", "a_b", "a/b", "a.b", "a" * 33, "团队", "con", "nul"])
def test_invalid_names(name):
    assert validate_team_name(name)
    assert validate_member_name(name)


def test_member_names_lead_and_star_are_reserved(tmp_path):
    assert validate_member_name("lead") and validate_member_name("*")
    assert validate_team_name("lead") is None
    with pytest.raises(TeamError) as caught:
        TeamStore.at(tmp_path, "Bad")
    assert caught.value.code == "invalid_name"


def test_store_layout_and_roster(isolated_home, tmp_path):
    root = teams_root(tmp_path / "project")
    assert root.parent.parent.parent == isolated_home / ".mosscode"  # ~/.mosscode/projects/<key>/teams
    store = TeamStore.at(root, "auth")
    store.create("登录")
    with pytest.raises(TeamError) as caught:
        store.create()
    assert caught.value.code == "team_exists"
    store.modify(lambda team: team.members.append(Member("alice", "general", worktree={"branch": "b"})))
    data = json.loads(store.config.read_text(encoding="utf-8"))
    assert data["v"] == 1 and data["members"][0]["agent_id"] == "alice@auth"
    assert store.load().member("alice").worktree == {"branch": "b"}
    assert (store.directory / "mailbox").is_dir() and store.transcripts.is_dir()
    assert [item.name for item in list_teams(root)] == ["auth"]


def test_lead_holder(tmp_path):
    store = TeamStore.at(tmp_path, "auth")
    store.create()
    store.acquire(os.getpid(), "s1")
    assert store.acquire(os.getpid(), "s2").lead["session"] == "s2"  # The same process may take it again.
    assert store.release(os.getpid() + 1).lead["pid"] == os.getpid()  # Not the holder: unchanged.


def test_team_held_by_a_live_process(tmp_path, monkeypatch):
    import mosscode.teams as teams
    store = TeamStore.at(tmp_path, "auth")
    store.create()
    store.modify(lambda team: setattr(team, "lead", {"pid": 4242, "session": "", "since": ""}))
    monkeypatch.setattr(teams, "process_alive", lambda pid: pid == 4242)
    with pytest.raises(TeamError) as caught:
        store.acquire(os.getpid())
    assert caught.value.code == "team_held"
    monkeypatch.setattr(teams, "process_alive", lambda pid: False)
    assert store.acquire(os.getpid()).lead["pid"] == os.getpid()
    assert store.release(os.getpid()).lead is None


def test_broken_config_is_left_in_place(tmp_path):
    store = TeamStore.at(tmp_path, "auth")
    store.create()
    store.config.write_text("{", encoding="utf-8")
    with pytest.raises(TeamError) as caught:
        store.load()
    assert caught.value.code == "team_corrupt"
    assert store.config.read_text(encoding="utf-8") == "{"


# Mailboxes -------------------------------------------------------------------------------

def test_two_writers_lose_nothing(tmp_path):
    mailbox = Mailbox(tmp_path)
    errors = []

    def write(sender):
        try:
            for index in range(200):
                mailbox.send("lead", Message(sender, "text", str(index), "s"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(name,)) for name in ROSTER]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    messages = mailbox.read_unread("lead")
    assert len(messages) == 400
    for name in ROSTER:
        assert [m.body for m in messages if m.sender == name] == [str(i) for i in range(200)]
    assert not list(tmp_path.glob("*.lock")) and not list(tmp_path.glob("*.tmp"))


def test_stale_lock_is_taken_over(tmp_path):
    mailbox = Mailbox(tmp_path)
    lock = tmp_path / "bob.json.lock"
    lock.write_text("999 old", encoding="utf-8")
    old = time.time() - 11
    os.utime(lock, (old, old))
    mailbox.send("bob", text(LEAD))
    assert [m.body for m in mailbox.read_unread("bob")] == ["hi"]
    assert not lock.exists()


def test_busy_lock_is_an_error_and_nothing_changes(tmp_path, monkeypatch):
    mailbox = Mailbox(tmp_path)
    mailbox.send("bob", text(LEAD, "first"))
    before = (tmp_path / "bob.json").read_text(encoding="utf-8")
    sleeps = []
    monkeypatch.setattr(lock_module.time, "sleep", sleeps.append)
    with file_lock(tmp_path / "bob.json"):
        with pytest.raises(TeamError) as caught:
            mailbox.send("bob", text(LEAD, "second"))
    assert caught.value.code == "mailbox_busy"
    assert len(sleeps) == 10 and all(0.005 <= s <= 0.1 for s in sleeps)
    assert (tmp_path / "bob.json").read_text(encoding="utf-8") == before


def test_read_marks_read_and_trims(tmp_path):
    mailbox = Mailbox(tmp_path)
    for index in range(KEEP_READ + 30):
        mailbox.send("bob", text(LEAD, str(index)))
    first = mailbox.read_unread("bob")
    assert len(first) == KEEP_READ + 30 and all(m.read for m in first)
    assert mailbox.read_unread("bob") == []
    mailbox.send("bob", text("alice", "new"))
    kept = mailbox.peek("bob")
    assert len(kept) == KEEP_READ + 1
    assert kept[0].body == "30" and kept[-1].body == "new" and not kept[-1].read
    assert mailbox.unread_count("bob") == 1
    assert [m.body for m in mailbox.read_unread("bob")] == ["new"]


def test_message_fields(tmp_path):
    mailbox = Mailbox(tmp_path)
    mailbox.send("lead", Message("alice", "idle_notification",
                                 extra={"reason": "completed", "completed_tasks": ["1"], "summary": "done"}))
    raw = json.loads((tmp_path / "lead.json").read_text(encoding="utf-8"))[0]
    assert raw["id"].startswith("m-") and len(raw["id"]) == 10
    assert {"from", "type", "body", "summary", "timestamp", "read", "reason", "completed_tasks"} <= set(raw)
    message = mailbox.read_unread("lead")[0]
    assert message.extra["completed_tasks"] == ["1"] and message.sender == "alice"


def test_full_mailbox(tmp_path, monkeypatch):
    from mosscode.teams import mailbox as mailbox_module
    monkeypatch.setattr(mailbox_module, "UNREAD_LIMIT", 3)
    mailbox = Mailbox(tmp_path)
    for _ in range(3):
        mailbox.send("bob", text(LEAD))
    with pytest.raises(TeamError) as caught:
        mailbox.send("bob", text(LEAD))
    assert caught.value.code == "mailbox_full"
    assert len(mailbox.peek("bob")) == 3


@pytest.mark.parametrize("message", [
    Message("alice", "text", "body", ""),  # No summary.
    Message("alice", "text", "body", "x" * 81),
    Message("alice", "text", "x" * 20001, "s"),
    Message("alice", "plan_approval_response", extra={"request_id": "r", "approve": True, "feedback": ""}),
    Message("alice", "shutdown_request", extra={"reason": ""}),
    Message(LEAD, "shutdown_response", extra={"request_id": "r", "approve": True, "reason": ""}),
    Message(LEAD, "idle_notification", extra={"reason": "completed", "completed_tasks": [], "summary": ""}),
    Message(LEAD, "plan_approval_response", extra={"request_id": "r"}),  # Missing fields.
    Message("alice", "chat", "x", "s"),
])
def test_invalid_messages(message, tmp_path):
    with pytest.raises(TeamError) as caught:
        check(message)
    assert caught.value.code == "invalid_message"
    with pytest.raises(TeamError):
        Mailbox(tmp_path).send("bob", message)
    assert not (tmp_path / "bob.json").exists()


def test_valid_protocol_messages():
    check(Message(LEAD, "plan_approval_response", extra={"request_id": "r", "approve": False, "feedback": "no"}))
    check(Message(LEAD, "shutdown_request", extra={"reason": "done"}))
    check(Message("alice", "shutdown_response", extra={"request_id": "r", "approve": True, "reason": ""}))
    check(Message("alice", "plan_approval_request", extra={"request_id": "r", "plan": "p"}))


# The shared task list -------------------------------------------------------------------

def test_dependency_gates_progress(board):
    first = board.create("接口", actor=LEAD)
    second = board.create("前端", blocked_by=["1"], owner="bob", participants=ROSTER)
    assert (first.id, second.id) == ("1", "2")
    assert blocks("1", board.list()) == ["2"]
    with pytest.raises(TeamError) as caught:
        board.update("2", actor="bob", status="in_progress")
    assert caught.value.code == "task_blocked" and "1" in caught.value.message
    assert board.update("1", actor="alice", participants=ROSTER, owner="alice", status="in_progress").task.owner == "alice"
    done = board.update("1", actor="alice", status="completed")
    assert [task.id for task in done.ready] == ["2"]
    assert board.update("2", actor="bob", status="in_progress").task.status == "in_progress"


def test_member_claims_by_starting(board):
    board.create("a")
    task = board.update("1", actor="alice", status="in_progress").task
    assert task.owner == "alice"
    with pytest.raises(TeamError) as caught:
        board.create("b")
        board.update("2", actor=LEAD, status="in_progress")
    assert caught.value.code == "no_owner"


def test_cycles_and_unknown_dependencies(board):
    board.create("a")
    board.create("b", blocked_by=["1"])
    board.create("c", blocked_by=["2"])
    for task_id, dep in (("1", "3"), ("1", "1")):
        with pytest.raises(TeamError) as caught:
            board.update(task_id, actor=LEAD, add_blocked_by=[dep])
        assert caught.value.code == "dependency_cycle"
    with pytest.raises(TeamError) as caught:
        board.create("d", blocked_by=["9"])
    assert caught.value.code == "unknown_task"
    assert board.get("1").blocked_by == []


def test_ownership_and_direction(board):
    board.create("a", owner="alice", participants=ROSTER)
    with pytest.raises(TeamError) as caught:
        board.update("1", actor="bob", participants=ROSTER, owner="bob")
    assert caught.value.code == "not_owner"
    with pytest.raises(TeamError) as caught:
        board.update("1", actor="alice", participants=ROSTER, owner="bob")
    assert caught.value.code == "not_owner"
    with pytest.raises(TeamError) as caught:
        board.update("1", actor=LEAD, participants=ROSTER, owner="carol")
    assert caught.value.code == "unknown_member"
    board.update("1", actor="alice", status="completed")
    with pytest.raises(TeamError) as caught:
        board.update("1", actor="alice", status="pending")
    assert caught.value.code == "invalid_transition"
    with pytest.raises(TeamError):
        board.update("1", actor=LEAD, status="in_progress")
    assert board.update("1", actor=LEAD, status="pending").task.status == "pending"
    board.create("b", blocked_by=["1"])
    with pytest.raises(TeamError) as caught:
        board.update("2", actor="alice", remove_blocked_by=["1"])
    assert caught.value.code == "lead_only"
    assert [t.id for t in board.update("2", actor=LEAD, remove_blocked_by=["1"]).ready] == ["2"]


def test_limits(board, monkeypatch):
    from mosscode.teams import board as board_module
    with pytest.raises(TeamError) as caught:
        board.create("  ")
    assert caught.value.code == "invalid_task"
    with pytest.raises(TeamError):
        board.create("x" * 121)
    monkeypatch.setattr(board_module, "TASK_LIMIT", 2)
    board.create("a")
    board.create("b")
    with pytest.raises(TeamError) as caught:
        board.create("c")
    assert caught.value.code == "too_many_tasks"


def test_broken_tasks_file_is_kept_aside(tmp_path):
    notes = []
    board = TaskBoard(tmp_path / "tasks.json", notes.append)
    (tmp_path / "tasks.json").write_text("not json", encoding="utf-8")
    assert board.list() == []
    kept = list(tmp_path.glob("tasks.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "not json"
    assert notes and "已损坏" in notes[0]
    assert board.create("a").id == "1"


def test_broken_mailbox_is_kept_aside(tmp_path):
    notes = []
    mailbox = Mailbox(tmp_path, notes.append)
    (tmp_path / "bob.json").write_text('{"not": "a list"}', encoding="utf-8")
    mailbox.send("bob", text(LEAD))
    assert len(mailbox.peek("bob")) == 1
    assert list(tmp_path.glob("bob.json.corrupt-*")) and notes


def test_release_retries_while_a_waiter_reads_the_lock(tmp_path, monkeypatch):
    """Windows: deleting a file another thread has open fails for a moment; the lock must still go."""
    real, failures = os.remove, []

    def remove(path):
        if str(path).endswith(".lock") and len(failures) < 3:
            failures.append(path)
            raise PermissionError(32, "being used by another process")
        real(path)

    monkeypatch.setattr(lock_module.os, "remove", remove)
    with file_lock(tmp_path / "bob.json"):
        pass
    assert len(failures) == 3 and not (tmp_path / "bob.json.lock").exists()


# Members, messages, lead notifications, approvals ------------------------------------

import asyncio  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from mosscode.agents.runtime import GLOBAL_BAN  # noqa: E402
from mosscode.teams.member import TeamAccess  # noqa: E402
from mosscode.tools.team import LEAD_TOOLS, TEAM_TOOLS  # noqa: E402
from mosscode.worktree.meta import load_meta  # noqa: E402
from test_agent import call, response  # noqa: E402
from test_sessions import drain  # noqa: E402
from mosscode.messages import ToolCall  # noqa: E402
from test_subagents import ScriptProvider, conversation, project_of, settle  # noqa: E402
from test_worktree import branches, repo  # noqa: E402


def member_of(system) -> str | None:
    found = re.search(r"你是团队 \S+ 的成员 (\S+)，", system.stable)
    return found.group(1) if found else None


def tool_call(tool, call_id, **args) -> ToolCall:
    """call() takes name and id itself; team tools have parameters of those names."""
    return ToolCall(call_id, tool, json.dumps(args))


def names(tools) -> set[str]:
    return {tool.name for tool in tools}


class Script:
    """Members answer by name (members[name](messages, tools)); everything else goes to lead()."""

    def __init__(self, lead=None, **members):
        self.lead = lead or (lambda messages, tools: response("好"))
        self.members = members
        self.seen: dict[str, list] = {}  # name -> [(messages, tools)] of each request.

    def __call__(self, messages, system, tools):
        name = member_of(system)
        if name is None:
            return self.lead(messages, tools)
        self.seen.setdefault(name, []).append((messages, tools))
        return self.members[name](messages, tools)


def turns(messages) -> int:
    """Turns a member has started: one user message each."""
    return sum(1 for m in messages if m.role == "user")


async def until(predicate, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.02)


def team_conversation(tmp_path, script, **options):
    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, teams=True, **options)
    conv.teams.poll = 0.05
    conv.teams.stop_wait = 1.0  # A member that never stops is cancelled quickly.
    return provider, conv


def done(messages, tools):
    return response("完成")


async def test_lead_tools_from_the_start_and_never_in_print_mode(tmp_path):
    create = tool_call("team_create", "c1", name="auth")
    provider, conv = team_conversation(tmp_path, Script(lambda m, t: response(calls=[create]) if len(m) == 1
                                                        else response("建好了")))
    assert set(LEAD_TOOLS) <= set(conv.registry.names())
    await drain(conv.reply("建团队"))
    before, after = provider.requests[0][2], provider.requests[1][2]
    assert set(LEAD_TOOLS) <= names(before) and names(before) == names(after)
    assert conv.teams.current.name == "auth"
    await conv.close()
    (tmp_path / "p").mkdir()
    plain = conversation(tmp_path / "p", ScriptProvider(lambda *a: response("好")))  # -p: teams=False
    assert not set(LEAD_TOOLS) & set(plain.registry.names()) and plain.teams is None
    await plain.close()


async def test_subagents_never_see_team_tools_and_forks_are_refused(tmp_path):
    assert TEAM_TOOLS <= GLOBAL_BAN
    seen = []

    def forked(messages):
        return any("<fork-directive>" in m.text for m in messages if m.role == "user")

    def script(messages, system, tools):
        if forked(messages):
            if messages[-1].role == "tool":
                return response("被拒绝了")
            return response(calls=[call("team_task_list", "x1")])
        if len(messages) == 1 and messages[0].text == "看看":
            seen.append(names(tools))
            return response("看完了")
        if len(messages) == 1:
            return response(calls=[call("agent", "a1", description="看", prompt="看看", agent_type="general"),
                                   call("agent", "f1", description="分", prompt="分出去")])
        return response("好")
    provider, conv = team_conversation(tmp_path, script)
    await drain(conv.reply("开始"))
    await settle(conv)
    assert seen and not seen[0] & TEAM_TOOLS
    refused = next(r for r in provider.requests if forked(r[0]) and r[0][-1].role == "tool")
    assert refused[0][-1].tool_results[0].code == "subagent_forbidden"
    await conv.close()


async def test_ac14_ac15_spawn_in_a_worktree(tmp_path):
    project = repo(tmp_path)
    script = Script(alice=done)
    provider, conv = team_conversation(tmp_path, script)
    woken = []
    conv.on_tasks = lambda: woken.append(1)
    await conv.teams.create("auth", "登录")
    text, details = await conv.teams.spawn("alice", "general", "实现登录")
    assert "后端：in-process" in text and "mosscode/team-auth+alice" in text
    assert details["worktree"] == "mosscode/team-auth+alice" and branches(project) == ["mosscode/team-auth+alice"]
    # The record is complete; the worktree is in use while the member runs.
    config = conv.teams.current.config
    member = json.loads(config.read_text(encoding="utf-8"))["members"][0]
    assert (member["name"], member["agent_type"], member["backend"], member["prompt"], member["agent_id"]) == \
           ("alice", "general", "in-process", "实现登录", "alice@auth")
    assert member["worktree"]["name"] == "team-auth/alice" and Path(member["worktree"]["path"]).is_dir()
    manager = conv.worktree_manager()
    meta = load_meta(manager.meta_path(member["worktree"]["flat"]))
    assert meta.manual and meta.in_use["task"] == "团队 auth 成员 alice"
    # The first turn ends, the lead hears it and an idle lead starts a notification turn.
    await until(lambda: conv.notifications)
    update = conv.notifications[0]
    assert update.startswith("<team-update>") and "alice 空闲" in update and woken
    messages, tools = script.seen["alice"][0]
    assert "实现登录" in messages[0].text and "<incoming-messages>" in messages[0].text
    # Messages and tasks, nothing that creates or merges.
    assert {"send_message", "team_task_create", "team_task_get", "team_task_list", "team_task_update"} <= names(tools)
    assert not {"agent", "team_spawn", "team_merge", "team_create", "team_delete"} & names(tools)
    await until(lambda: conv.teams.runners["alice"].status == "idle")
    await conv.close()
    assert load_meta(manager.meta_path(member["worktree"]["flat"])).in_use is None
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["members"][0]["status"] == "stopped" and data["lead"] is None
    assert Path(data["members"][0]["worktree"]["path"]).is_dir()  # Kept across restarts.
    # A pane member marks it with its own pid, so it stays in use after the lead has gone.
    await manager.mark_in_use(member["worktree"]["flat"], "团队 auth 成员 alice", pid=4242)
    assert load_meta(manager.meta_path(member["worktree"]["flat"])).in_use == \
           {"pid": 4242, "task": "团队 auth 成员 alice"}


async def test_messages_during_a_turn_arrive_together(tmp_path):
    gate = asyncio.Event()

    async def alice(messages, tools):
        if turns(messages) == 1:
            await gate.wait()
        return response("收到")
    script = Script(alice=alice)
    provider, conv = team_conversation(tmp_path, script)
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "第一件事", isolation="none")
    await until(lambda: "alice" in script.seen)
    await conv.teams.send("alice", "text", "第二件事", "二")
    await conv.teams.send("alice", "text", "第三件事", "三")
    gate.set()
    await until(lambda: len(script.seen["alice"]) == 2)
    second = script.seen["alice"][1][0][-1].text
    assert "第二件事" in second and "第三件事" in second and second.count("[来自 lead") == 2
    await conv.close()


def test_broadcast_reaches_everyone_but_the_sender(tmp_path):
    store = TeamStore(tmp_path / "t")
    store.create()
    store.modify(lambda team: team.members.extend([Member("alice", "general"), Member("bob", "general"),
                                                   Member("carol", "general")]))
    assert TeamAccess(store, "alice").send("*", "text", "大家好", "问候") == ["bob", "carol"]
    assert [m.body for m in store.mailbox.peek("bob")] == ["大家好"] == [m.body for m in store.mailbox.peek("carol")]
    assert store.mailbox.peek("alice") == [] and store.mailbox.peek(LEAD) == []
    assert TeamAccess(store, LEAD).send("*", "text", "开工", "开工") == ["alice", "bob", "carol"]
    assert store.mailbox.peek(LEAD) == []
    with pytest.raises(TeamError) as error:
        TeamAccess(store, LEAD).send("*", "shutdown_request", reason="")
    assert error.value.code == "invalid_message"
    with pytest.raises(TeamError) as error:
        TeamAccess(store, "alice").send(LEAD, "idle_notification", reason="completed", completed_tasks=[])
    assert error.value.code == "invalid_message"  # Runtime only.


async def test_fifth_member(tmp_path):
    provider, conv = team_conversation(tmp_path, Script())
    await conv.teams.create("t")
    conv.teams.current.modify(lambda team: team.members.extend(Member(f"m{i}", "general") for i in range(4)))
    with pytest.raises(TeamError) as error:
        await conv.teams.spawn("e", "general", "做事", isolation="none")
    assert error.value.code == "too_many_members"
    await conv.close()


async def test_member_approval_while_the_lead_is_idle(tmp_path):
    def alice(messages, tools):
        if messages[-1].role == "user":
            return response(calls=[call("write_file", "w1", path="b.txt", content="成员写的\n")])
        return response("写好了")
    script = Script(alice=alice)
    provider, conv = team_conversation(tmp_path, script, mode="default", interactive=True)
    shown = []

    def approve(request):
        shown.append(request)
        conv.registry.permissions.respond(request.id, "once")
    conv.teams.on_approval = approve
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "写文件", isolation="none")
    await until(lambda: len(script.seen.get("alice", [])) == 2)
    assert shown and shown[0].source == "成员 alice" and shown[0].operation.tool == "write_file"
    assert (project_of(tmp_path) / "b.txt").read_text(encoding="utf-8") == "成员写的\n"
    await conv.close()


def plan_member(plans):
    """Writes first (blocked in plan mode), then submits plans in turn; writes once approved."""
    submitted = []

    def alice(messages, tools):
        last = messages[-1]
        if last.role == "user" and "已批准" in last.text:
            return response(calls=[call("write_file", "w9", path="done.txt", content="ok\n")])
        if last.role == "user" and turns(messages) == 1:
            return response(calls=[call("write_file", "w1", path="early.txt", content="x\n")])
        if last.role == "user" or (last.tool_results[0].name == "write_file"
                                   and last.tool_results[0].code == "plan_blocked"):
            submitted.append(1)
            return response(calls=[call("exit_plan_mode", f"p{len(submitted)}", plan=plans[len(submitted) - 1])])
        return response("等待")
    return alice


async def wait_plan(conv, previous=None):
    await until(lambda: conv.teams.plan_requests.get("alice") not in {None, previous})
    return conv.teams.plan_requests["alice"]


async def test_ac21_plan_approval_round_trip(tmp_path):
    script = Script(alice=plan_member(["计划一", "计划二"]))
    provider, conv = team_conversation(tmp_path, script)
    notes = []
    conv.teams.on_note = notes.append
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "先规划", require_plan_approval=True, isolation="none")
    first = await wait_plan(conv)
    runner = conv.teams.runners["alice"]
    # Plan mode blocks the write; the plan reaches the lead with its request id, no idle notification.
    blocked = script.seen["alice"][1][0][-1].tool_results[0]
    assert blocked.code == "plan_blocked" and not (project_of(tmp_path) / "early.txt").exists()
    await until(lambda: conv.notifications)
    update = "\n".join(conv.take_notifications())
    assert "计划一" in update and f"request_id={first}" in update and "空闲" not in update
    assert any("成员 alice 提交了计划，等待 Lead 审批" in note for note in notes)
    assert runner.policy.mode == "plan"
    # Rejected, resubmitted; the old id is no longer accepted.
    await until(lambda: runner.status == "idle")
    await conv.teams.send("alice", "plan_approval_response", request_id=first, approve=False, feedback="再细一点")
    second = await wait_plan(conv, first)
    assert second != first and runner.policy.mode == "plan"
    with pytest.raises(TeamError) as error:
        await conv.teams.send("alice", "plan_approval_response", request_id=first, approve=True, feedback="")
    assert error.value.code == "unknown_request"
    await until(lambda: runner.status == "idle")
    await conv.teams.send("alice", "plan_approval_response", request_id=second, approve=True, feedback="可以")
    await until(lambda: (project_of(tmp_path) / "done.txt").exists())
    assert runner.policy.mode == "bypassPermissions" and runner.approved
    assert any("Lead 批准了 alice 的计划" in note for note in notes)
    await conv.close()


async def test_approved_while_the_session_plans(tmp_path):
    script = Script(alice=plan_member(["计划"]))
    provider, conv = team_conversation(tmp_path, script)
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "先规划", require_plan_approval=True, isolation="none")
    request = await wait_plan(conv)
    runner = conv.teams.runners["alice"]
    await until(lambda: runner.status == "idle")
    conv.registry.permissions.set_mode("plan")
    await conv.teams.send("alice", "plan_approval_response", request_id=request, approve=True, feedback="")

    def approved():
        return [r[0][-1].text for r in script.seen["alice"] if r[0][-1].role == "user" and "已批准" in r[0][-1].text]
    await until(lambda: approved() and runner.status == "idle")
    assert "会话当前处于计划模式" in approved()[0] and runner.policy.mode == "plan"
    assert not (project_of(tmp_path) / "done.txt").exists()
    await conv.close()


async def test_idle_notification_carries_the_reply_and_newly_ready_tasks(tmp_path):
    def alice(messages, tools):
        if messages[-1].role == "user":
            return response(calls=[tool_call("team_task_update", "u1", id="1", status="in_progress")])
        if messages[-1].tool_results[0].call_id == "u1":
            return response(calls=[tool_call("team_task_update", "u2", id="1", status="completed")])
        return response("登录做完了")
    script = Script(alice=alice)
    provider, conv = team_conversation(tmp_path, script)
    await conv.teams.create("t")
    access = conv.teams.access()
    conv.teams.current.modify(lambda team: team.members.append(Member("bob", "general")))
    access.task_create("登录")
    access.task_create("退出", blocked_by=["1"], owner="bob")
    access.task_create("无关")
    await conv.teams.spawn("alice", "general", "做任务 1", isolation="none")
    await until(lambda: conv.notifications)
    update = conv.notifications[0]
    assert "alice 空闲，完成任务 1。最后回复：登录做完了" in update
    assert "现在就绪的任务：2（bob）。" in update and "3（" not in update
    await conv.close()


async def test_member_card_outlives_the_lead_turn(tmp_path):
    """In the UI, a member's request shown during a lead turn stays when that turn ends."""
    from mosscode.tui import MossCodeApp
    from test_tui import send
    gate = asyncio.Event()

    async def lead(messages, tools):
        await gate.wait()
        return response("主线完成")

    def alice(messages, tools):
        if messages[-1].role == "user":
            return response(calls=[call("write_file", "w1", path="b.txt", content="成员写的\n")])
        return response("写好了")
    script = Script(lead, alice=alice)
    provider, conv = team_conversation(tmp_path, script, mode="default")
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 32)) as pilot:
        await send(app, pilot, "主线任务")
        await conv.teams.create("t")
        await conv.teams.spawn("alice", "general", "写文件", isolation="none")
        for _ in range(200):
            await pilot.pause(.02)
            if app._approval_request is not None:
                break
        request = app._approval_request
        assert request is not None and request.source == "成员 alice"
        gate.set()
        await asyncio.wait_for(app._reply_task, 5)
        await pilot.pause()
        assert app._approval_request is request and app.query_one("#approval-panel").display
        await pilot.press("enter")
        await until(lambda: (project_of(tmp_path) / "b.txt").exists())
        assert not app.query_one("#approval-panel").display
        await conv.teams.close()


# Merging and the lead's guidance ----------------------------------------------

import subprocess  # noqa: E402

from mosscode.permissions import ApprovalRequest, Operation  # noqa: E402
from test_worktree import git  # noqa: E402


async def merge_team(tmp_path, *members, **scripts):
    """A repository, a team t and idle members in worktrees; the lead can be asked."""
    project = repo(tmp_path)
    script = Script(**{name: scripts.get(name, done) for name in members})
    provider, conv = team_conversation(tmp_path, script, mode="default", interactive=True)
    await conv.teams.create("t")
    for name in members:
        await conv.teams.spawn(name, "general", "做事")
    await until(lambda: all(conv.teams.runners[name].status == "idle" for name in members))
    return project, conv


def worktree_of(conv, name) -> Path:
    return Path(conv.teams.current.load().member(name).worktree["path"])


def commit_in(conv, name, path="a.txt", content=None) -> Path:
    worktree = worktree_of(conv, name)
    (worktree / path).write_text(content or f"{name}\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", f"{name} 改 {path}")
    return worktree


def answering(conv, choice):
    """Answers the lead's confirmations with choice; the list collects the requests."""
    seen = []

    async def answer():
        policy = conv.registry.permissions
        while True:
            request = await policy.requests.get()
            seen.append(request)
            policy.respond(request.id, choice)
    return seen, asyncio.get_running_loop().create_task(answer())


async def merged(conv, name, choice="merge"):
    seen, job = answering(conv, choice)
    try:
        return await conv.teams.merge(name), seen
    finally:
        job.cancel()


async def merge_error(conv, name, choice="merge"):
    with pytest.raises(TeamError) as error:
        await merged(conv, name, choice)
    return error.value


def head(project) -> str:
    return git(project, "rev-parse", "HEAD")


def clean(project) -> bool:
    return git(project, "status", "--porcelain", "--untracked-files=no") == "" and \
        not (project / ".git" / "MERGE_HEAD").exists()


def complete(store, task_id, owner):
    store.board.update(task_id, actor=owner, status="in_progress")
    store.board.update(task_id, actor=owner, status="completed")


async def test_dirty_main_directory(tmp_path):
    project, conv = await merge_team(tmp_path, "alice")
    commit_in(conv, "alice", "b.txt")
    (project / "a.txt").write_text("主目录改了\n", encoding="utf-8")
    before = head(project)
    seen, job = answering(conv, "merge")
    with pytest.raises(TeamError) as error:
        await conv.teams.merge("alice")
    job.cancel()
    assert error.value.code == "main_dirty" and "a.txt" in error.value.message and "问用户" in error.value.message
    assert not seen and head(project) == before
    assert (project / "a.txt").read_text(encoding="utf-8") == "主目录改了\n"
    # Untracked files do not block; they are listed on the card.
    git(project, "checkout", "--", "a.txt")
    (project / "notes.txt").write_text("草稿\n", encoding="utf-8")
    (text, details), seen = await merged(conv, "alice")
    assert details["merged"] and "notes.txt" in seen[0].operation.preview
    await conv.close()


async def test_uncommitted_and_busy_members(tmp_path):
    gate = asyncio.Event()

    async def bob(messages, tools):
        if turns(messages) == 2:
            await gate.wait()
        return response("好")
    project, conv = await merge_team(tmp_path, "alice", "bob", bob=bob)
    worktree = commit_in(conv, "alice", "b.txt")
    (worktree / "new.txt").write_text("没提交\n", encoding="utf-8")
    error = await merge_error(conv, "alice")
    assert error.code == "member_uncommitted" and "new.txt" in error.message
    commit_in(conv, "bob", "c.txt")
    await conv.teams.send("bob", "text", "再做一点", "再做")
    await until(lambda: conv.teams.runners["bob"].status == "running")
    assert (await merge_error(conv, "bob")).code == "member_busy"
    gate.set()
    await until(lambda: conv.teams.runners["bob"].status == "idle")
    (text, details), seen = await merged(conv, "bob")
    assert details["merged"]
    await conv.close()


async def test_merge_in_dependency_order(tmp_path):
    project, conv = await merge_team(tmp_path, "alice", "bob")
    store = conv.teams.current
    store.board.create("接口", owner="alice", participants=("alice", "bob"))
    store.board.create("页面", blocked_by=["1"], owner="bob", participants=("alice", "bob"))
    complete(store, "1", "alice")
    complete(store, "2", "bob")
    commit_in(conv, "alice", "api.txt")
    commit_in(conv, "bob", "page.txt")
    before = head(project)
    error = await merge_error(conv, "bob")
    assert error.code == "merge_order" and "先合并 alice" in error.message and head(project) == before
    (text, details), seen = await merged(conv, "alice")
    assert details["merged"]
    (text, details), seen = await merged(conv, "bob")
    assert details["merged"] and (project / "api.txt").exists() and (project / "page.txt").exists()
    await conv.close()


async def test_declined(tmp_path):
    project, conv = await merge_team(tmp_path, "alice")
    commit_in(conv, "alice", "b.txt")
    before = head(project)
    error = await merge_error(conv, "alice", "deny")
    assert error.code == "merge_declined" and "问用户" in error.message
    assert head(project) == before and not (project / "b.txt").exists()
    await conv.close()


async def test_merge_keeps_the_worktree_and_branch(tmp_path):
    project, conv = await merge_team(tmp_path, "alice")
    worktree = commit_in(conv, "alice", "b.txt", "新文件\n")
    before = head(project)
    (text, details), seen = await merged(conv, "alice")
    # Asked whatever the mode, with the branch, target, commits and stat.
    request = seen[0]
    assert request.operation.tool == "team_merge" and request.operation.target == "alice"
    assert request.choices == ("merge", "deny")
    preview = request.operation.preview
    assert "mosscode/team-t+alice → main" in preview and "alice 改 b.txt" in preview and "b.txt |" in preview
    # A merge commit with both parents; The worktree and branch stay.
    parents = git(project, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert parents[0] == before and len(parents) == 2
    assert details["commit"] == git(project, "rev-parse", "--short", "HEAD") and details["commit"] in text
    assert (project / "b.txt").read_text(encoding="utf-8") == "新文件\n"
    assert worktree.is_dir() and branches(project) == ["mosscode/team-t+alice"]
    assert (await merge_error(conv, "alice")).code == "nothing_to_merge"
    # The addendum names the main branch.
    assert conv.teams.current.load().member("alice").worktree["main"] == "main"
    assert "git merge main，" in conv.teams.runners["alice"].loop.system
    await conv.close()


async def test_ac29_conflict_then_resolved_in_the_worktree(tmp_path):
    project, conv = await merge_team(tmp_path, "alice", "bob")
    commit_in(conv, "alice", "a.txt", "alice 的版本\n")
    bob = commit_in(conv, "bob", "a.txt", "bob 的版本\n")
    await merged(conv, "alice")
    before = head(project)
    error = await merge_error(conv, "bob")
    # Aborted; the main directory is as it was.
    assert error.code == "merge_conflict" and "a.txt" in error.message and "git merge main" in error.message
    assert clean(project) and head(project) == before
    assert (project / "a.txt").read_text(encoding="utf-8") == "alice 的版本\n"
    # The member merges main, resolves and commits; the merge goes through.
    attempt = subprocess.run(["git", "merge", "main"], cwd=bob, capture_output=True, text=True)
    assert attempt.returncode != 0
    (bob / "a.txt").write_text("合并后的版本\n", encoding="utf-8")
    git(bob, "add", "a.txt")
    git(bob, "commit", "-q", "--no-edit")
    (text, details), seen = await merged(conv, "bob")
    assert details["merged"] and clean(project)
    assert (project / "a.txt").read_text(encoding="utf-8") == "合并后的版本\n"
    await conv.close()


async def test_merge_refusals_main_not_ready_no_worktree_not_confirmed(tmp_path):
    project, conv = await merge_team(tmp_path, "alice")
    commit_in(conv, "alice", "b.txt")
    git(project, "checkout", "-q", "--detach")
    assert (await merge_error(conv, "alice")).code == "main_not_ready"
    git(project, "checkout", "-q", "main")
    await conv.teams.spawn("reader", "general", "只读", isolation="none")
    assert (await merge_error(conv, "reader")).code == "no_worktree"
    assert (await merge_error(conv, "nobody")).code == "unknown_member"
    conv.registry.permissions.interactive = False
    assert (await merge_error(conv, "alice")).code == "merge_not_confirmed"
    assert not (project / "b.txt").exists()
    await conv.close()


async def test_lead_guide_only_in_the_lead_prompt(tmp_path):
    script = Script(lambda m, t: response("好"), alice=done)
    provider, conv = team_conversation(tmp_path, script)
    await drain(conv.reply("你好"))
    lead_system = provider.requests[0][1].stable
    assert "## Agent 团队" in lead_system and "team_merge" in lead_system and "至多 4 个" in lead_system
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    await until(lambda: "alice" in script.seen)
    member_system = next(r[1].stable for r in provider.requests if member_of(r[1]) == "alice")
    assert "## Agent 团队" not in member_system
    await conv.close()


async def test_merge_card_in_the_ui(tmp_path):
    from mosscode.tui import MossCodeApp
    provider, conv = team_conversation(tmp_path, Script())
    app = MossCodeApp(conv, "Demo")
    request = ApprovalRequest("r1", "", Operation("team_merge", "alice", preview="成员：alice"), "合并？",
                              ("merge", "deny"))
    async with app.run_test(size=(100, 32)) as pilot:
        app._show_approval(request)
        await pilot.pause()
        detail = str(app.query_one("#approval-detail").render())
        assert "合并成员 alice 的分支" in detail and "成员：alice" in detail
        options = app.query_one("#approval-options")
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert labels[0].startswith("合并") and labels[1].startswith("不合并")
        notes = []

        async def note(text, style=None):
            notes.append(text)
        app._local_note = note
        future = asyncio.get_running_loop().create_future()
        conv.registry.permissions.pending["r1"] = future
        await pilot.press("down", "enter")  # 不合并.
        await pilot.pause()
        assert future.result() == "deny" and any("直接在输入框告诉 Lead" in text for text in notes)
        conv.registry.permissions.pending.pop("r1")
        # A member asking while the window closes is refused instead of failing on a missing card.
        asking = ApprovalRequest("r2", "成员 alice", Operation("execute_command", "ls"), "运行？", ("once", "deny"))
        conv.registry.permissions.pending["r2"] = future = asyncio.get_running_loop().create_future()
        app._closing = True
        conv.teams.on_approval(asking)
        assert future.result() == "deny" and app._approval_request is None
        app._closing = False
        await conv.teams.close()


# Idle, reopening, revival, /team -----------------------------------------

import mosscode.teams as teams_module  # noqa: E402
from mosscode.builtin_commands import BUILTIN, help_text  # noqa: E402
from test_commands import context, run  # noqa: E402
from test_subagents import tool_results_of  # noqa: E402


def idle(conv, *members):
    """After a turn: a runner starts out idle before its first one."""
    def check():
        runners = [conv.teams.runners.get(name) for name in members]
        return all(runner is not None and runner.status == "idle" and runner.loop.history for runner in runners)
    return check


def lead_requests(provider):
    return [request for request in provider.requests if member_of(request[1]) is None]


async def test_lead_exit_stops_members_and_keeps_the_team(tmp_path):
    repo(tmp_path)
    gate = asyncio.Event()

    async def carol(messages, tools):
        await gate.wait()  # Never answers: cancelled after stop_wait.
        return response("好")
    script = Script(alice=done, bob=done, carol=carol)
    provider, conv = team_conversation(tmp_path, script)
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做 A")
    await conv.teams.spawn("bob", "general", "做 B", isolation="none")
    await conv.teams.spawn("carol", "general", "做 C", isolation="none")
    await until(idle(conv, "alice", "bob"))
    await until(lambda: conv.teams.runners["carol"].status == "running")
    store = conv.teams.current
    worktree = worktree_of(conv, "alice")
    started = time.monotonic()
    await conv.close()
    assert time.monotonic() - started < 5
    for name in ("alice", "bob"):
        last = script.seen[name][-1][0][-1].text
        assert "停止请求" in last and "Lead 正在退出" in last
    assert [m.read for m in store.mailbox.peek("carol") if m.type == "shutdown_request"] == [False]
    team = store.load()
    assert team.lead is None and {member.status for member in team.members} == {"stopped"}
    assert worktree.is_dir() and store.exists() and (store.transcripts / "alice.jsonl").is_file()
    answers = [m for m in store.mailbox.peek(LEAD) if m.type == "shutdown_response"]
    assert len(answers) == 2 and all(m.read for m in answers)  # Not news for the next lead.
    assert load_meta(conv.worktree_manager().meta_path(team.member("alice").worktree["flat"])).in_use is None
    gate.set()


async def test_ac33_reopen_then_revive_with_the_old_conversation(tmp_path):
    repo(tmp_path)

    def alice(messages, tools):
        return response("登录写好了" if turns(messages) == 1 else "收到")
    provider, conv = team_conversation(tmp_path, Script(alice=alice))
    await conv.teams.create("t", "登录")
    await conv.teams.spawn("alice", "general", "实现登录")
    await until(idle(conv, "alice"))
    await conv.close()
    # A new lead process opens it; nothing runs, the holder is this process.
    send = tool_call("send_message", "s1", to="alice", body="再加注销", summary="注销")
    script = Script(lambda m, t: response(calls=[send]) if m[-1].role == "user" and "让 alice 继续" in m[-1].text
                    else response("好"), alice=lambda m, t: response("注销也好了"))
    provider2, conv2 = team_conversation(tmp_path, script)
    notes = []
    conv2.teams.on_note = notes.append
    text, details = await conv2.teams.create("t")
    assert details["opened"] and "成员都处于停止状态" in text
    team = conv2.teams.current.load()
    assert team.lead["pid"] == os.getpid() and team.member("alice").status == "stopped"
    assert not conv2.teams.runners
    # A message revives it with its earlier conversation, in the same worktree.
    await drain(conv2.reply("让 alice 继续"))
    result = tool_results_of(next(r for r in lead_requests(provider2) if any(m.role == "tool" for m in r[0])))[0]
    assert "alice 原已停止，已从记录恢复（后端：in-process，" in result.output
    await until(lambda: "alice" in script.seen)
    messages = script.seen["alice"][0][0]
    assert "实现登录" in messages[0].text
    assert any(m.role == "assistant" and "登录写好了" in m.text for m in messages)
    assert "再加注销" in messages[-1].text and "停止请求" not in messages[-1].text  # Stale request dropped.
    assert any("成员 alice 已恢复（进程内）" in note for note in notes)
    meta = load_meta(conv2.worktree_manager().meta_path(team.member("alice").worktree["flat"]))
    assert meta.in_use is not None
    await until(idle(conv2, "alice"))
    await conv2.close()


async def test_team_held_and_team_busy(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def alice(messages, tools):
        await gate.wait()
        return response("好")
    provider, conv = team_conversation(tmp_path, Script(alice=alice))
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    await until(lambda: conv.teams.runners["alice"].status == "running")
    with pytest.raises(TeamError) as error:
        await conv.teams.create("other")
    assert error.value.code == "team_busy" and conv.teams.current.name == "t"
    gate.set()
    await until(idle(conv, "alice"))
    await conv.close()
    store = TeamStore.at(conv.teams.root, "t")
    store.modify(lambda team: setattr(team, "lead", {"pid": 4242, "session": "", "since": ""}))
    monkeypatch.setattr(teams_module, "process_alive", lambda pid: pid == 4242)
    provider2, conv2 = team_conversation(tmp_path, Script())
    for attempt in (conv2.teams.create("t"), conv2.teams.delete("t")):
        with pytest.raises(TeamError) as error:
            await attempt
        assert error.value.code == "team_held"
    assert conv2.teams.current is None and store.exists() and store.load().lead["pid"] == 4242
    await conv2.close()


async def test_corrupt_transcript_keeps_the_message(tmp_path):
    provider, conv = team_conversation(tmp_path, Script(alice=done))
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    await until(idle(conv, "alice"))
    await conv.close()
    provider2, conv2 = team_conversation(tmp_path, Script(alice=done))
    await conv2.teams.create("t")
    store = conv2.teams.current
    with (store.directory / store.load().member("alice").transcript).open("a", encoding="utf-8") as file:
        file.write("{坏的\n")
    with pytest.raises(TeamError) as error:
        await conv2.teams.send("alice", "text", "还在吗", "问")
    assert error.value.code == "revive_failed" and "已损坏" in error.value.message and "已写入邮箱" in error.value.message
    assert [m.body for m in store.mailbox.peek("alice") if not m.read] == ["还在吗"]
    assert "alice" not in conv2.teams.runners and store.load().member("alice").status == "stopped"
    await conv2.close()


async def test_team_commands(tmp_path):
    repo(tmp_path)
    provider, conv = team_conversation(tmp_path, Script(alice=done, bob=done))
    await conv.teams.create("t", "登录")
    await conv.teams.spawn("alice", "general", "做事")
    await conv.teams.spawn("bob", "general", "做事", isolation="none")
    conv.teams.access().task_create("登录页")
    await until(idle(conv, "alice", "bob"))
    store = conv.teams.current
    worktree = worktree_of(conv, "alice")
    ctx = context(conv)
    notes = ctx.ui.notes
    await run(ctx, "/team")
    assert "t  当前  成员 2（空闲 2）  持有者 本进程" in notes[-1]
    await run(ctx, "/team t")
    detail = notes[-1]
    assert "团队 t：登录" in detail and "alice  空闲  general  后端 进程内" in detail
    assert "工作树 team-t/alice，分支 mosscode/team-t+alice" in detail and "在主项目目录中工作" in detail
    assert "未读消息 0" in detail and "任务（1）" in detail and "登录页" in detail
    await run(ctx, "/team nosuch")
    assert notes[-1].startswith("! 本项目没有名为 nosuch 的团队")
    await run(ctx, "/team stop bob")
    assert "已请求成员 bob 停止" in notes[-1]
    await until(lambda: "bob" not in conv.teams.runners)
    await conv.teams.settle()
    assert store.load().member("bob").status == "stopped"
    await run(ctx, "/team")
    assert "成员 2（空闲 1、已停止 1）" in notes[-1]
    await run(ctx, "/team stop bob")
    assert "已经停止" in notes[-1]
    await run(ctx, "/team stop nobody")
    assert notes[-1].startswith("! 团队 t 中没有成员 nobody")
    # Also while a run is going.
    command = BUILTIN.get("team")
    assert "/team stop <成员>" in help_text(BUILTIN, "team") and "/team stop" not in help_text(BUILTIN)
    assert command.runs_while_running("") and command.runs_while_running("stop bob")
    assert not command.runs_while_running("delete t")
    # Asked first; cancelling keeps it.
    await run(ctx, "/team delete t")
    assert ctx.ui.calls[-1][0] == "choose"
    await ctx.ui.pick(1)
    assert "团队保留" in notes[-1] and store.exists()
    await run(ctx, "/team delete t")
    await ctx.ui.pick(0)
    assert "已删除团队 t" in notes[-1] and "team-t/alice" in notes[-1]
    assert not store.directory.exists() and conv.teams.current is None and not worktree.exists()
    await run(ctx, "/team")
    assert "还没有团队" in notes[-1]
    await conv.close()


@pytest.mark.parametrize("owner", ["runtime", "process"])
async def test_settle_with_a_finished_write_still_in_the_set(owner, monkeypatch):
    """A write that finished but has not yet left the set (its callback runs next iteration) must not spin."""
    from types import SimpleNamespace
    from mosscode.teams.process import MemberProcess
    from mosscode.teams.runtime import TeamRuntime
    settle = TeamRuntime.settle if owner == "runtime" else MemberProcess.settle
    gather, calls = asyncio.gather, []

    def counted(*jobs, **options):
        calls.append(jobs)
        assert len(calls) < 100, "settle() spun on a finished write"
        return gather(*jobs, **options)
    monkeypatch.setattr(asyncio, "gather", counted)
    holder = SimpleNamespace(_writes=set())
    job = asyncio.get_running_loop().create_future()
    holder._writes.add(job)
    job.add_done_callback(holder._writes.discard)
    job.set_result(None)  # Done; the discard callback is only scheduled.
    await settle(holder)
    assert calls == []


async def test_member_that_does_not_stop_is_forced(tmp_path):
    gate = asyncio.Event()

    async def alice(messages, tools):
        await gate.wait()
        return response("好")
    provider, conv = team_conversation(tmp_path, Script(alice=alice))
    notes = []
    conv.teams.on_note = notes.append
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    await until(lambda: conv.teams.runners["alice"].status == "running")
    text = await conv.teams.stop_member("alice")
    assert "1 秒内没有停止时强制停止" in text
    await until(lambda: any("没有在 1 秒内停止，已强制停止（进程内）" in note for note in notes), timeout=5)
    assert "alice" not in conv.teams.runners
    assert conv.teams.current.load().member("alice").status == "stopped"
    with pytest.raises(TeamError) as error:
        await conv.teams.send("alice", "shutdown_request", reason="再停一次")
    assert error.value.code == "member_stopped"
    gate.set()
    await conv.close()


async def test_team_context_after_clear(tmp_path):
    provider, conv = team_conversation(tmp_path, Script(alice=done))
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    await until(idle(conv, "alice"))
    conv.take_notifications()
    await conv.clear()
    assert conv.teams.current.name == "t" and conv.teams.runners["alice"].status == "idle"
    await drain(conv.reply("下一步"))
    first = lead_requests(provider)[-1][0][-1]
    assert first.text.startswith("<team-context>") and "团队 t 的 Lead" in first.text
    assert "alice（general，空闲）" in first.text and first.text.endswith("下一步") and first.shown == "下一步"
    await drain(conv.reply("再下一步"))
    assert "<team-context>" not in lead_requests(provider)[-1][0][-1].text
    await conv.close()


async def test_status_bar_counts_members_and_their_approvals(tmp_path):
    from mosscode.tui import MossCodeApp
    provider, conv = team_conversation(tmp_path, Script(alice=done))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(160, 32)) as pilot:
        await conv.teams.create("t")
        await conv.teams.spawn("alice", "general", "做事", isolation="none")
        await until(idle(conv, "alice"))
        await pilot.pause()
        assert "团队 t 运行 0 / 空闲 1" in str(app.query_one("#status").render())
        pending = conv.registry.permissions.pending
        pending["r1"] = asyncio.get_running_loop().create_future()
        conv.teams.askers["r1"] = "alice"  # Counted by asker, as TeamRuntime._approval records it.
        app._show_approval(ApprovalRequest("r1", "", Operation("write_file", "b.txt"), "写？", ("once", "deny"),
                                           source="成员 alice"))
        await pilot.pause()
        assert "成员审批 1" in str(app.query_one("#status").render())
        pending.pop("r1").cancel()
        await conv.teams.close()


# Coordinator mode ----------------------------------------------------------------------

import dataclasses  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import mosscode.cli as cli  # noqa: E402
from mcp import types as mcp_types  # noqa: E402
from mosscode import coordinator  # noqa: E402
from mosscode.builtin_commands import status_report  # noqa: E402
from mosscode.mcp_client import MCPTool, ServerConnection  # noqa: E402
from mosscode.mcp_config import ServerConfig  # noqa: E402
from mosscode.tools.skill import SkillTool  # noqa: E402
from test_cli import setup_cli  # noqa: E402


def coordinator_cli(monkeypatch, tmp_path, args, *, configured, env):
    """cli.main with coordinator_mode and the variable as given; returns the conversation and stderr lines."""
    provider, _ = setup_cli(monkeypatch, args)
    loaded = cli.load_settings(None)
    monkeypatch.setattr(cli, "load_settings", lambda path: dataclasses.replace(loaded, coordinator_mode=configured))
    if env is None:
        monkeypatch.delenv(coordinator.ENV, raising=False)
    else:
        monkeypatch.setenv(coordinator.ENV, env)
    monkeypatch.chdir(tmp_path)
    seen = {}

    class Chat:
        def __init__(self, conversation, name, **kwargs):
            seen["conversation"] = conversation

        def run(self, **kwargs):
            pass

    monkeypatch.setattr(cli, "MossCodeApp", Chat)
    built = []
    original = cli.Conversation

    def capture(*a, **k):
        conversation = original(*a, **k)
        built.append(conversation)
        return conversation
    monkeypatch.setattr(cli, "Conversation", capture)
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    return built[0], provider


@pytest.mark.parametrize("value,expected", [("1", True), ("TRUE", True), ("Yes", True), ("0", False), ("", False)])
def test_environment_values(value, expected):
    assert coordinator.env_on({coordinator.ENV: value}) is expected


@pytest.mark.parametrize("configured,env,notice", [(True, None, coordinator.NO_ENV), (False, "1", coordinator.NO_CONFIG)])
def test_one_switch_is_not_enough(monkeypatch, tmp_path, configured, env, notice):
    conv, _ = coordinator_cli(monkeypatch, tmp_path, [], configured=configured, env=env)
    assert conv.coordinator is False and conv.registry.withheld is None
    assert notice in conv._notices
    assert "write_file" in conv.registry.names() and coordinator.SECTION not in conv._agent.team_guide
    assert "MOSSCODE_COORDINATOR_MODE" in notice


def test_neither_switch_says_nothing(monkeypatch, tmp_path):
    conv, _ = coordinator_cli(monkeypatch, tmp_path, [], configured=False, env=None)
    assert conv.coordinator is False and not any("协调者" in notice for notice in conv._notices)


def test_ac44_both_switches(monkeypatch, tmp_path):
    conv, _ = coordinator_cli(monkeypatch, tmp_path, [], configured=True, env="yes")
    assert conv.coordinator is True and not any("协调者" in notice for notice in conv._notices)
    offered = {d.name for d in conv.registry.definitions()}
    assert not {"write_file", "edit_file"} & offered
    assert {"execute_command", "read_file", "agent", "task_output", "task_stop", *LEAD_TOOLS} <= offered


def test_print_mode_ignores_it(monkeypatch, tmp_path, capsys):
    conv, _ = coordinator_cli(monkeypatch, tmp_path, ["-p", "hello"], configured=True, env="1")
    assert conv.coordinator is False and conv.teams is None
    assert coordinator.HEADLESS in capsys.readouterr().err
    assert "write_file" in {d.name for d in conv.registry.definitions()}


def skill_tool(root) -> SkillTool:
    skill = SimpleNamespace(name="deploy", directory=root)
    spec = SimpleNamespace(name="run", full_name="skill__deploy__run", description="d",
                           input_schema={"type": "object", "properties": {}}, command=["python"], timeout=5)
    return SkillTool(root, skill, spec)


async def test_lead_offers_no_write_mcp_or_skill_tools_but_members_keep_them(tmp_path):
    lead = Script(lambda m, t: response("好"), alice=done)
    provider, conv = team_conversation(tmp_path, lead, coordinator=True)
    root = conv.registry.permissions.root
    connection = ServerConnection(root, ServerConfig("docs", "stdio", "python"))
    conv.registry.register(MCPTool(root, connection, mcp_types.Tool(name="write", inputSchema={"type": "object"})))
    conv.registry.register(skill_tool(root))
    await drain(conv.reply("你好"))
    offered = names(provider.requests[0][2])
    assert not {"write_file", "edit_file", "mcp__docs__write", "skill__deploy__run"} & offered
    assert {"execute_command", "read_file", "agent", "load_skill", *LEAD_TOOLS} - {"load_skill"} <= offered
    assert "## 协调者模式" in provider.requests[0][1].stable and "派发后不要自己动手" in provider.requests[0][1].stable
    refused = await conv.registry.execute(tool_call("write_file", "w1", path="a.txt", content="x"))
    assert refused.is_error and refused.code == "coordinator_withheld" and not (root / "a.txt").exists()
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做事", isolation="none")
    await until(lambda: "alice" in lead.seen)
    member_tools = names(lead.seen["alice"][0][1])
    assert {"write_file", "edit_file"} <= member_tools  # Members implement.
    member_system = next(r[1].stable for r in provider.requests if member_of(r[1]) == "alice")
    assert "## 协调者模式" not in member_system
    await conv.close()


async def test_without_agents_it_is_ignored(tmp_path):
    from mosscode.conversation import Conversation
    from mosscode.tools import create_default_registry
    registry = create_default_registry(project_of(tmp_path), load_permissions=False)
    plain = Conversation(ScriptProvider(lambda *a: response("好")), registry=registry, teams=True, coordinator=True)
    assert plain.coordinator is False and coordinator.NO_AGENTS in plain._notices
    await plain.close()


async def test_status_bar_and_status_command(tmp_path):
    from mosscode.tui import MossCodeApp
    provider, conv = team_conversation(tmp_path, Script(), coordinator=True)
    assert coordinator.STATUS in status_report(context(conv))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(160, 32)) as pilot:
        await pilot.pause()
        assert coordinator.BADGE in str(app.query_one("#status").render())
    await conv.close()
    (tmp_path / "plain").mkdir()
    provider, plain = team_conversation(tmp_path / "plain", Script())
    assert coordinator.STATUS not in status_report(context(plain))
    await plain.close()
