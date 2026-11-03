"""Member panel and view (PL, MV), team windows (WN) and pane approvals in the lead (FA)."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from rich.console import Console

from mosscode.member_ui import compact_rows, elapsed
from mosscode.messages import ContentBlock, RunStopped, ToolCall, ToolProgress, ToolResult
from mosscode.messages import Message as ChatMessage
from mosscode.permissions import ApprovalRequest, Operation, PermissionPolicy
from mosscode.serialization import message_to_json
from mosscode.teams import LEAD, Member, TeamError, TeamStore
from mosscode.teams.backends import WINDOWS_TERMINAL, team_window
from mosscode.teams.mailbox import Message
from mosscode.teams.member import TeamAccess, approval_from_record, approval_record, render_incoming
from mosscode.teams.process import MemberProcess
from mosscode.teams.runtime import MemberRow, PaneRunner
from mosscode.teams.view import COMPACTED, Feed, TranscriptTail, message_lines
from test_agent import response
from test_teams import Script, team_conversation, until


def plain(lines) -> str:
    return "\n".join(line.plain for line in lines)


def rendered(renderable, width=100) -> str:
    console = Console(width=width, color_system=None)
    with console.capture() as captured:
        console.print(renderable)
    return captured.get()


def chat(role, text="", **fields) -> ChatMessage:
    return ChatMessage(role, (ContentBlock("text", text),) if text else (), **fields)


def record(message: ChatMessage) -> str:
    return json.dumps({"type": "message", "message": message_to_json(message)}, ensure_ascii=False) + "\n"


# A member's conversation as lines ------------------------------------------------------


def test_transcript_messages_as_lines():
    incoming = render_incoming(
        [
            Message(LEAD, "text", "实现登录\n细节", "初始任务"),
            Message("bob", "shutdown_request", extra={"reason": "收工"}),
        ]
    )
    lines = plain(message_lines(chat("user", incoming)))
    assert lines.splitlines()[0] == "← lead · 消息：初始任务"
    assert lines.splitlines()[1].startswith("← bob · ")
    call = ToolCall("c1", "edit_file", '{"path": "a.py"}')
    assert plain(message_lines(chat("assistant", "我来改", tool_calls=(call,)))) == "我来改"  # Calls go to Feed.
    results = message_lines(
        chat(
            "tool",
            tool_results=(
                ToolResult("c1", "edit_file", "已修改 a.py\n第二行"),
                ToolResult("c2", "read_file", "没有", is_error=True),
            ),
        )
    )
    assert plain(results).splitlines() == ["  └ 已修改 a.py", "  └ 没有"]
    assert plain(message_lines(chat("user", "摘要", synthetic="compact"))) == COMPACTED
    feed = Feed()
    assert plain(feed.record({"type": "compact"})) == COMPACTED
    assert feed.record({"type": "teammate", "name": "a"}) == [] and feed.record({"type": "message"}) == []


def test_parallel_results_sit_under_their_calls():
    calls = tuple(ToolCall(f"c{index}", "read_file", f'{{"path": "{name}"}}') for index, name in enumerate("abc"))
    feed = Feed()
    lines = feed.record(json.loads(record(chat("assistant", "我先看看", tool_calls=calls))))
    assert plain(lines) == "我先看看" and plain(feed.running).count("● read_file") == 3
    results = tuple(ToolResult(f"c{index}", "read_file", name.upper()) for index, name in enumerate("abc"))
    lines = feed.record(json.loads(record(chat("tool", tool_results=results[:2]))))
    assert plain(lines).splitlines() == ['● read_file {"path": "a"}', "  └ A", '● read_file {"path": "b"}', "  └ B"]
    assert plain(feed.running) == '● read_file {"path": "c"}'  # Still running: shown apart.
    lines = feed.record(json.loads(record(chat("tool", tool_results=results[2:]))))
    assert plain(lines).splitlines() == ['● read_file {"path": "c"}', "  └ C"] and not feed.running
    # A call whose result never came is written as it was, before what follows.
    feed.record(json.loads(record(chat("assistant", tool_calls=calls[:1]))))
    assert plain(feed.record(json.loads(record(chat("assistant", "算了"))))).splitlines() == [
        '● read_file {"path": "a"}',
        "算了",
    ]


def test_events_of_a_pane_member_pair_calls_and_results():
    feed = Feed()
    one, two = ToolCall("c1", "read_file", '{"path": "a"}'), ToolCall("c2", "read_file", '{"path": "b"}')
    assert feed.event([Message(LEAD, "text", "看看", "初始任务")])[0].plain == "← lead · 消息：初始任务"
    assert feed.event(ToolProgress(one, "running")) == [] and feed.event(ToolProgress(two, "running")) == []
    assert feed.running_text().plain.splitlines() == ['● read_file {"path": "a"}', '● read_file {"path": "b"}']
    lines = feed.event(ToolProgress(two, "finished", ToolResult("c2", "read_file", "B")))
    assert plain(lines).splitlines() == ['● read_file {"path": "b"}', "  └ B"]
    lines = feed.event(RunStopped("cancelled", "已中断", 1))
    assert plain(lines).splitlines() == ['● read_file {"path": "a"}', "已中断"] and not feed.running


def test_tail_reads_what_was_added(tmp_path):
    path = tmp_path / "a.jsonl"
    tail = TranscriptTail(path)
    assert tail.read() == []  # Not there yet.
    first = record(chat("assistant", "一"))
    path.write_text('{"type": "teammate"}\n' + first, encoding="utf-8")
    assert [item["type"] for item in tail.read()] == ["teammate", "message"]
    assert tail.read() == []
    second = record(chat("assistant", "二"))
    with path.open("a", encoding="utf-8") as stream:  # A line still being written waits for its end.
        stream.write(second[:10])
    assert tail.read() == []
    with path.open("a", encoding="utf-8") as stream:
        stream.write(second[10:] + "not json\n")
    assert [plain(Feed().record(item)) for item in tail.read()] == ["二"]
    path.write_text(first, encoding="utf-8")  # Replaced by a shorter file: start over.
    assert [plain(Feed().record(item)) for item in tail.read()] == ["一"]


# Panel rows --------------------------------------------------------------------------


def row(name, status="idle", **fields) -> MemberRow:
    return MemberRow(name, status, fields.pop("pane", False), **fields)


def test_compact_panel_folds_to_four_lines():
    rows = [
        row("a", "running", since=0.0, last_tool="edit_file a.py", task="登录"),
        row("b", "running", since=0.0),
        row("c"),
        row("d"),
        row("e"),
        row("f", "stopped"),
    ]
    grid, lines = compact_rows(rows, {}, 80.0)
    text = rendered(grid)
    assert lines == 4 and "空闲 3：c、d、e" in text and "已停止 1" in text
    assert "登录 · edit_file a.py" in text and "1m20s" in text
    grid, lines = compact_rows(rows + [row("g", "running", pane=True)], {"g": 1}, 80.0)
    text = rendered(grid)
    assert lines == 4 and "另有 4 个成员 · Ctrl+T 展开" in text  # idle 3 + stopped 1.
    assert "待审批" in rendered(compact_rows([row("g", "running", pane=True)], {"g": 1}, 0.0)[0])
    assert "窗格" in rendered(compact_rows([row("g", pane=True)], {}, 0.0)[0])
    narrow = rendered(compact_rows([row("a", "running", since=0.0, task="很长的任务标题" * 10)], {}, 5.0)[0], 40)
    assert len(narrow.splitlines()) == 1 and "5s" in narrow  # Cut, never wrapped.
    assert (elapsed(5), elapsed(80), elapsed(3725)) == ("5s", "1m20s", "1h02m")


async def test_rows_of_both_backends(tmp_path):
    gate = asyncio.Event()

    async def alice(messages, tools):
        await gate.wait()
        return response("完成")

    provider, conv = team_conversation(tmp_path, Script(alice=alice))
    await conv.teams.create("t")
    await conv.teams.spawn("alice", "general", "做 A", isolation="none")
    store = conv.teams.current
    task = TeamAccess(store, LEAD).task_create("实现登录", owner="alice")
    TeamAccess(store, "alice").task_update(task.id, status="in_progress")
    store.modify(
        lambda team: team.members.extend(
            [
                Member("bob", "general", WINDOWS_TERMINAL, status="idle", pid=os.getpid(), window="mosscode-t-1"),
                Member("carol", "general"),
            ]
        )
    )
    pane = PaneRunner(store, "bob", SimpleNamespace(name=WINDOWS_TERMINAL), os.getpid(), None, status="running")
    pane.last_tool = "execute_command npm test"
    conv.teams.runners["bob"] = pane
    await until(lambda: conv.teams.runners["alice"].status == "running")
    rows = {item.name: item for item in await conv.teams.rows()}
    assert list(rows) == ["alice", "bob", "carol"]
    assert rows["alice"].status == "running" and rows["alice"].task == "实现登录" and not rows["alice"].pane
    assert rows["alice"].since is not None
    assert (
        rows["bob"].pane
        and rows["bob"].last_tool == "execute_command npm test"
        and rows["bob"].window == "mosscode-t-1"
    )
    assert rows["carol"].status == "stopped" and rows["carol"].since is None
    del conv.teams.runners["bob"]
    gate.set()
    await conv.close()


# Team windows -----------------------------------------------------------------------


def test_team_window_names(tmp_path):
    one, other = tmp_path / "p1" / "teams" / "demo", tmp_path / "p2" / "teams" / "demo"
    name = team_window(one)
    assert name.startswith("mosscode-demo-") and len(name) == len("mosscode-demo-") + 6
    assert team_window(one) == name and team_window(other) != name  # Same team name, other project.


# Pane approvals in the lead ------------------------------------------------------------


def test_take_only_the_kinds_asked_and_the_model_cannot_answer(tmp_path):
    store = TeamStore(tmp_path / "t")
    store.create()
    store.modify(lambda team: team.members.append(Member("bob", "general")))
    access = TeamAccess(store, LEAD)
    access.send("bob", "text", "继续", "s")
    access.send("bob", "permission_response", runtime=True, request_id="r1", choice="once")
    with pytest.raises(TeamError):  # RUNTIME_ONLY: never from the model.
        access.send("bob", "permission_response", request_id="r1", choice="once")
    with pytest.raises(TeamError):  # LEAD_ONLY.
        TeamAccess(store, "bob").send(LEAD, "permission_response", runtime=True, request_id="r1", choice="once")
    taken = store.mailbox.take("bob", {"permission_response"})
    assert [(m.type, m.extra) for m in taken] == [("permission_response", {"request_id": "r1", "choice": "once"})]
    assert [m.type for m in store.mailbox.read_unread("bob")] == ["text"]


def test_record_round_trip():
    request = ApprovalRequest(
        "r1", "c1", Operation("execute_command", "npm test", cwd="/w"), "需要确认", ("once", "session", "deny")
    )
    back = approval_from_record(json.loads(json.dumps(approval_record(request))), "成员 bob（窗格）")
    assert back == ApprovalRequest("r1", "c1", request.operation, "需要确认", request.choices, "成员 bob（窗格）")
    assert approval_from_record({"id": "r", "operation": {}, "choices": ["sudo"]}, "x") is None
    assert approval_from_record({"nothing": 1}, "x") is None


def pane_store(tmp_path, name="bob") -> TeamStore:
    store = TeamStore(tmp_path / "teams" / "t")
    store.create()
    store.modify(
        lambda team: team.members.append(Member(name, "general", WINDOWS_TERMINAL, status="idle", pid=os.getpid()))
    )
    return store


async def test_member_takes_the_leads_answer(tmp_path):
    store = pane_store(tmp_path)
    policy = PermissionPolicy(tmp_path, load=False)
    process = MemberProcess(store, "bob", SimpleNamespace(registry=SimpleNamespace(permissions=policy)), poll=0.05)
    settled = []
    process.on_settled = lambda request_id, choice: settled.append((request_id, choice))
    request = ApprovalRequest("r1", "c1", Operation("execute_command", "npm test"), "需要确认", ("once", "deny"))
    future = policy.hold(request)  # As _ask registers it.
    process._approval(request)
    await process.settle()
    assert [item["id"] for item in store.load().member("bob").approvals] == ["r1"]
    access = TeamAccess(store, LEAD)
    access.send("bob", "text", "别的消息", "s")
    access.send("bob", "permission_response", runtime=True, request_id="r1", choice="once")
    assert await asyncio.wait_for(future, 2) == "once"
    await until(lambda: settled)
    await process.settle()
    assert settled == [("r1", "once")] and store.load().member("bob").approvals == []
    assert [m.type for m in store.mailbox.read_unread("bob")] == ["text"]  # Only the answer was taken.
    # Answered in its own window: out of the record at once, nothing from the lead.
    second = ApprovalRequest("r2", "c2", Operation("execute_command", "npm run lint"), "需要确认", ("once", "deny"))
    policy.hold(second)
    process._approval(second)
    await process.settle()
    policy.respond("r2", "deny")
    process.answered_here()
    await until(lambda: len(settled) == 2)
    await process.settle()
    assert settled[1] == ("r2", None) and store.load().member("bob").approvals == []


def publish(store, name, *requests) -> None:
    def write(team):
        team.member(name).approvals = [approval_record(request) for request in requests]

    store.modify(write)


def asking(request_id="r1") -> ApprovalRequest:
    return ApprovalRequest(request_id, "c1", Operation("execute_command", "npm test"), "需要确认", ("once", "deny"))


async def attached(tmp_path, **options):
    provider, conv = team_conversation(tmp_path, Script(), mode="default", **options)
    await conv.teams.create("t")
    store = conv.teams.current
    store.modify(
        lambda team: team.members.append(Member("bob", "general", WINDOWS_TERMINAL, status="idle", pid=os.getpid()))
    )
    runner = conv.teams._attach(store, store.load().member("bob"))
    return conv, store, runner


async def stop_attached(conv, runner):
    runner.job.cancel()  # The stand-in is this process: never stop() it.
    await asyncio.gather(runner.job, return_exceptions=True)
    conv.teams.runners.pop("bob", None)
    await conv.close()


async def test_lead_shows_and_answers_a_pane_request(tmp_path):
    conv, store, runner = await attached(tmp_path)
    shown = []
    conv.teams.on_approval = shown.append
    publish(store, "bob", asking())
    await until(lambda: shown)
    request = shown[0]
    assert request.source == "成员 bob（窗格）" and request.id == "r1" and conv.teams.asker("r1") == "bob"
    assert conv.registry.permissions.respond("r1", "once")
    await until(lambda: store.mailbox.peek("bob"))
    answer = store.mailbox.peek("bob")[-1]
    assert (answer.sender, answer.type, answer.extra) == (
        LEAD,
        "permission_response",
        {"request_id": "r1", "choice": "once"},
    )
    await until(lambda: "r1" not in conv.registry.permissions.pending)
    await stop_attached(conv, runner)


async def test_answered_in_its_window_or_stopped_withdraws_the_card(tmp_path):
    conv, store, runner = await attached(tmp_path)
    shown, withdrawn, notes = [], [], []
    conv.teams.on_approval, conv.teams.on_withdraw, conv.teams.on_note = shown.append, withdrawn.append, notes.append
    publish(store, "bob", asking("r1"), asking("r2"))
    await until(lambda: len(shown) == 2)
    publish(store, "bob", asking("r2"))  # r1 answered in the member's window.
    await until(lambda: withdrawn == ["r1"])
    await until(lambda: "r1" not in conv.registry.permissions.pending)  # Released once the relay wakes.
    assert "成员 bob 的审批已在它的窗口中回答" in notes
    assert not store.mailbox.peek("bob")  # Nothing is sent for it.
    store.modify(lambda team: setattr(team.member("bob"), "status", "stopped"))  # The member ends.
    await until(lambda: withdrawn == ["r1", "r2"])
    assert any("已停止，它的审批已撤下" in note for note in notes)
    await until(lambda: not conv.registry.permissions.pending)  # The relay releases it as it ends.
    await stop_attached(conv, runner)


def test_turn_drops_left_over_answers(tmp_path):
    from mosscode.teams.member import MemberRunner

    store = pane_store(tmp_path)
    TeamAccess(store, LEAD).send("bob", "permission_response", runtime=True, request_id="old", choice="once")
    turns = []
    runner = MemberRunner(
        TeamAccess(store, "bob"), "bob", agent_mode=lambda mode: mode, session_mode=lambda: "default", poll=0.01
    )

    async def turn(messages):
        turns.append(messages)

    runner.turn = turn

    async def main():
        job = asyncio.get_running_loop().create_task(runner.serve())
        await asyncio.sleep(0.1)
        runner.shutdown = True
        runner.wake()
        await asyncio.wait_for(job, 2)

    asyncio.run(main())
    assert turns == [] and store.mailbox.unread_count("bob") == 0


# The panel and the view in the lead's window ---------------------------------------------------


async def pause_until(pilot, predicate, steps=300):
    for _ in range(steps):
        if predicate():
            return
        await pilot.pause(0.02)
    assert predicate(), "timed out"


def view_text(app) -> str:
    log = app.query_one("#member-log")
    return "".join(str(line.text) for line in log.lines).replace(" ", "")


async def test_t4_panel_and_view_of_an_in_process_member(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea

    gate = asyncio.Event()

    async def alice(messages, tools):
        await gate.wait()
        return response("登录做完了")

    def bob(messages, tools):
        return response("好")

    provider, conv = team_conversation(tmp_path, Script(alice=alice, bob=bob))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one("#prompt", PromptArea).value = "草稿"
        await conv.teams.create("t")
        await conv.teams.spawn("alice", "general", "实现登录", isolation="none")
        await conv.teams.spawn("bob", "general", "写文档", isolation="none")
        panel = app.query_one("#member-panel")
        await pause_until(
            pilot,
            lambda: (
                panel.display
                and {row.name for row in panel.rows} == {"alice", "bob"}
                and panel.rows[0].status == "running"
            ),
        )
        text = rendered(app.query_one("#member-compact").content)
        assert "alice" in text and "运行中" in text
        assert "Ctrl+T 成员" in str(app.query_one("#status").render())
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pause_until(pilot, lambda: panel.expanded and app.focused is app.query_one("#member-list"))
        assert app.query_one("#prompt", PromptArea).value == "草稿"
        await pilot.press("enter")  # Alice, the first row.
        view = app.query_one("#member-view")
        await pause_until(pilot, lambda: view.display and view.name_shown == "alice")
        assert not app.query_one("#chat").display and not app.query_one("#input-row").display
        assert not panel.display and "成员 alice · 团队 t · 运行中" in str(app.query_one("#member-head").render())
        # The loop commits a turn's messages with its first reply.
        gate.set()  # alice's reply arrives while the view is open.
        await pause_until(pilot, lambda: "登录做完了" in view_text(app))
        assert "←lead·消息：初始任务" in view_text(app)
        await pilot.press("right")  # The next member.
        await pause_until(pilot, lambda: view.name_shown == "bob" and view_text(app).endswith("初始任务好"))
        await pilot.press("escape")  # Back to the panel.
        await pilot.pause()
        assert not view.display and app.query_one("#chat").display and app.query_one("#input-row").display
        assert panel.expanded and panel.selected == "bob"
        await pilot.press("escape")
        await pilot.pause()
        assert not panel.expanded and isinstance(app.focused, PromptArea)
        assert app.query_one("#prompt", PromptArea).value == "草稿"
        await conv.teams.close()


async def test_x_twice_stops_the_member(tmp_path):
    from mosscode.tui import MossCodeApp

    async def alice(messages, tools):
        await asyncio.sleep(30)
        return response("不会到这里")

    provider, conv = team_conversation(tmp_path, Script(alice=alice))
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 30)) as pilot:
        await conv.teams.create("t")
        await conv.teams.spawn("alice", "general", "做事", isolation="none")
        panel = app.query_one("#member-panel")
        await pause_until(pilot, lambda: panel.display and panel.rows)
        await pilot.press("ctrl+t")
        await pilot.press("x")
        await pilot.pause()
        assert "再按 x 停止成员 alice" in str(app.query_one("#member-help").render())
        assert "alice" in conv.teams.runners
        await pilot.press("x")
        await pause_until(pilot, lambda: "alice" not in conv.teams.runners)  # Stopped (forced after stop_wait).
        await conv.teams.close()


async def test_view_of_a_pane_member_follows_its_transcript(tmp_path):
    from mosscode.tui import MossCodeApp

    conv, store, runner = await attached(tmp_path)
    app = MossCodeApp(conv, "Demo")
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one("#member-panel")
        app._schedule_members()
        await pause_until(pilot, lambda: panel.display and panel.rows)
        await app._open_view("bob")
        await pause_until(pilot, lambda: "还没有对话记录" in view_text(app))
        path = store.transcripts / "bob.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record(chat("assistant", "窗格成员的回复")), encoding="utf-8")
        await pause_until(pilot, lambda: "窗格成员的回复" in view_text(app))  # Polled.
        assert app.query_one("#member-view").display
        await stop_attached(conv, runner)


async def test_forwarded_card_in_the_view_and_withdrawn(tmp_path):
    from mosscode.tui import MossCodeApp

    conv, store, runner = await attached(tmp_path, interactive=True)
    app = MossCodeApp(conv, "Demo")
    notes = []
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one("#member-panel")
        app._schedule_members()
        await pause_until(pilot, lambda: panel.display and panel.rows)
        await app._open_view("bob")
        publish(store, "bob", asking("r1"))
        await pause_until(pilot, lambda: app._approval_request is not None)
        assert app.query_one("#approval-panel").display and app.query_one("#member-view").display
        assert "成员 bob（窗格）" in str(app.query_one("#approval-detail").render())
        assert "成员审批 1" in str(app.query_one("#status").render())
        await pilot.press("enter")  # Once: goes to the member.
        await pause_until(pilot, lambda: store.mailbox.peek("bob"))
        assert store.mailbox.peek("bob")[-1].extra == {"request_id": "r1", "choice": "once"}
        assert app.focused is app.query_one("#member-log")  # Back to the view.
        publish(store, "bob", asking("r2"))
        await pause_until(pilot, lambda: app._approval_request is not None and app._approval_request.id == "r2")
        conv.teams.on_note = notes.append
        publish(store, "bob")  # Answered in the member's window.
        await pause_until(pilot, lambda: not app.query_one("#approval-panel").display)
        assert app._approval_request is None and any("已在它的窗口中回答" in note for note in notes)
        await stop_attached(conv, runner)
