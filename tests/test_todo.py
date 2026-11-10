"""The model's task checklist: todo_write, the per-request snapshot, transcript records and resume."""

import json

import pytest
import yaml

from mosscode.config import ConfigError, load_settings
from mosscode.session import Session, Transcript, compact_record, load_session, message_record, rewind_record
from mosscode.todo import TodoError, TodoItem, TodoList, render, todo_reminder
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response
from test_sessions import assistant, drain, make, records, user


def entries(*items):
    return [(item_id, content, status) for item_id, content, status in items]


def todo_call(id="t", *todos):
    return call("todo_write", id, todos=list(todos))


def test_ids_are_assigned_kept_and_items_left_out_are_removed():
    todos = TodoList()
    todos.replace(entries((None, "读代码", "in_progress"), (None, "写测试", "pending"), (None, "改文档", "pending")))
    assert [(i.id, i.status) for i in todos.items] == [("1", "in_progress"), ("2", "pending"), ("3", "pending")]
    todos.replace(entries(("1", "读代码", "completed"), ("3", "改文档", "in_progress"), (None, "运行测试", "pending")))
    # The removed id 2 is not reused, so a stale id from the model never hits a new item.
    assert [(i.id, i.content, i.status) for i in todos.items] == [
        ("1", "读代码", "completed"),
        ("3", "改文档", "in_progress"),
        ("4", "运行测试", "pending"),
    ]
    assert render(todos.items).splitlines() == ["[x] 1. 读代码", "[>] 3. 改文档", "[ ] 4. 运行测试", "已完成 1/3"]


@pytest.mark.parametrize(
    "bad",
    [
        entries(("9", "不存在的项", "pending")),
        entries(("1", "甲", "pending"), ("1", "甲", "completed")),
        entries((None, "甲", "in_progress"), (None, "乙", "in_progress")),
        entries((None, "   ", "pending")),
        entries((None, "长" * 201, "pending")),
        entries(*[(None, f"第 {n} 项", "pending") for n in range(51)]),
    ],
)
def test_an_invalid_list_changes_nothing(bad):
    todos = TodoList()
    todos.replace(entries((None, "甲", "pending")))
    before = todos.items
    with pytest.raises(TodoError):
        todos.replace(bad)
    assert todos.items == before
    assert todos.replace(entries((None, "乙", "pending")))[0].id == "2"  # The failed call used no ids.


def test_the_reminder_is_sent_only_while_something_is_unfinished():
    todos = TodoList()
    assert todo_reminder(todos) is None and todo_reminder(None) is None
    todos.replace(entries((None, "甲", "completed"), (None, "乙", "pending")))
    reminder = todo_reminder(todos)
    assert reminder.kind == "todo-list" and "[ ] 2. 乙" in reminder.text and "不是用户的指令" in reminder.text
    todos.replace(entries(("1", "甲", "completed"), ("2", "乙", "completed")))
    assert todo_reminder(todos) is None


async def test_the_tool_replaces_the_registry_list_and_reports_errors(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False)
    result = await reg.execute(
        todo_call("a", {"content": "读代码", "status": "in_progress"}, {"content": "写测试", "status": "pending"})
    )
    assert not result.is_error and "[>] 1. 读代码" in result.output and "已完成 0/2" in result.output
    assert result.details["total"] == 2 and [i.content for i in reg.todos.items] == ["读代码", "写测试"]
    # A model may send ids as numbers.
    result = await reg.execute(todo_call("b", {"id": 1, "content": "读代码", "status": "completed"}))
    assert not result.is_error and [(i.id, i.status) for i in reg.todos.items] == [("1", "completed")]
    result = await reg.execute(todo_call("c", {"id": "7", "content": "乱写", "status": "pending"}))
    assert result.is_error and result.code == "todo_invalid" and "清单未改变" in result.output
    assert [i.id for i in reg.todos.items] == ["1"]


async def test_plan_mode_offers_and_runs_it_without_approval(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="plan", load_permissions=False)
    assert "todo_write" in {d.name for d in reg.definitions(plan=True)}
    result = await reg.execute(todo_call("p", {"content": "调查", "status": "in_progress"}))
    assert not result.is_error and reg.todos.items[0].content == "调查"


def test_the_switch_leaves_the_tool_out(tmp_path):
    assert create_default_registry(tmp_path, load_permissions=False, todo=False).get("todo_write") is None
    path = tmp_path / "config.yaml"
    provider = {"name": "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "MOSS_TEST_KEY"}
    path.write_text(yaml.safe_dump({"providers": [provider], "todo_tool": False}), encoding="utf-8")
    assert load_settings(path).todo_tool is False
    path.write_text(yaml.safe_dump({"providers": [provider]}), encoding="utf-8")
    assert load_settings(path).todo_tool is True
    path.write_text(yaml.safe_dump({"providers": [provider], "todo_tool": "no"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="todo_tool"):
        load_settings(path)


async def test_a_sub_agent_view_has_its_own_list(tmp_path):
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    await reg.execute(todo_call("m", {"content": "主对话的项", "status": "pending"}))
    view = reg.for_agent(agent="subagent", agent_type="general", permissions=reg.permissions)
    assert view.todos is not reg.todos and view.todos.items == ()
    await view.execute(todo_call("s", {"content": "子 Agent 的项", "status": "in_progress"}))
    assert [i.content for i in reg.todos.items] == ["主对话的项"]
    assert [i.content for i in view.todos.items] == ["子 Agent 的项"]


async def test_snapshot_follows_the_history_and_leaves_the_cached_prefix_alone(tmp_path):
    from mosscode.conversation import Conversation

    provider = SequenceProvider(
        response("建清单", [todo_call("a", {"content": "甲", "status": "in_progress"})]),
        response("做完", [todo_call("b", {"id": "1", "content": "甲", "status": "completed"})]),
        response("完成"),
    )
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv = Conversation(provider, registry=reg)
    assert await conv.ask("开始") == "完成"
    kinds = [[s.kind for s in request[1].supplements] for request in provider.requests]
    assert "todo-list" not in kinds[0] and "todo-list" in kinds[1] and "todo-list" not in kinds[2]
    snapshot = next(s for s in provider.requests[1][1].supplements if s.kind == "todo-list")
    assert "[>] 1. 甲" in snapshot.text
    # The stable prompt and the tools are the same in every request: the snapshot never enters the cached prefix.
    assert len({request[1].stable for request in provider.requests}) == 1
    assert len({tuple(d.name for d in request[2]) for request in provider.requests}) == 1
    # Nor the history: no message carries it.
    assert all("<todo-list>" not in m.text for request in provider.requests for m in request[0])
    await conv.close()


async def test_the_list_is_recorded_restored_on_resume_and_emptied_by_clear(tmp_path):
    provider = SequenceProvider(
        response(
            "建清单", [todo_call("a", {"content": "甲", "status": "completed"}, {"content": "乙", "status": "pending"})]
        ),
        response("好"),
    )
    conv = make(tmp_path, provider)
    assert await conv.ask("开始") == "好"
    saved = [r for r in records(conv.session.transcript_path) if r["type"] == "todo"]
    assert len(saved) == 1 and saved[0]["keep"] == 2
    assert saved[0]["todos"] == [
        {"id": "1", "content": "甲", "status": "completed"},
        {"id": "2", "content": "乙", "status": "pending"},
    ]
    session_id = conv.session_id
    await conv.close()

    other = make(tmp_path, SequenceProvider())
    await drain(other.resume(session_id))
    assert [(i.id, i.status) for i in other.registry.todos.items] == [("1", "completed"), ("2", "pending")]
    other.registry.todos.replace(entries((None, "丙", "pending")))
    assert other.registry.todos.items[0].id == "3"  # Numbering goes on after the restored ids.
    await other.clear()
    assert other.registry.todos.items == ()
    await other.close()


async def test_rewinding_the_conversation_steps_the_list_back(tmp_path):
    provider = SequenceProvider(
        response("一", [todo_call("a", {"content": "甲", "status": "in_progress"})]),
        response("好"),
        response("二", [todo_call("b", {"id": "1", "content": "甲", "status": "completed"})]),
        response("好"),
    )
    conv = make(tmp_path, provider)
    await conv.ask("第一步")
    await conv.ask("第二步")
    assert conv.registry.todos.items[0].status == "completed"
    second = next(p for p in conv.rewind_points() if p.message.text == "第二步")
    await conv.rewind(second.message.id, code=False, conversation=True)
    assert [(i.content, i.status) for i in conv.registry.todos.items] == [("甲", "in_progress")]
    session_id = conv.session_id
    await conv.close()
    # The transcript replays to the same list.
    other = make(tmp_path, SequenceProvider())
    await drain(other.resume(session_id))
    assert [(i.content, i.status) for i in other.registry.todos.items] == [("甲", "in_progress")]
    await other.close()


def todo_line(keep, *items):
    return {"type": "todo", "time": "2026-11-10T10:00:00+08:00", "keep": keep, "todos": [i.to_json() for i in items]}


def test_loading_follows_rewinds_and_compactions(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    session = Session.new(project, home=home)
    transcript = Transcript(session, project)
    first, second = TodoItem("1", "甲", "in_progress"), TodoItem("1", "甲", "completed")
    lines = [
        message_record(user("一")),
        message_record(assistant("建", [])),
        todo_line(2, first),
        message_record(user("二")),
        message_record(assistant("改", [])),
        todo_line(4, second),
        rewind_record(3, "id"),  # Back to before the second change.
    ]
    for line in lines:
        assert transcript.append(line) is None
    transcript.close()
    assert load_session(session.transcript_path).todos == (first,)

    transcript = Transcript(session, project)
    transcript.open_existing()
    for line in [todo_line(4, second), compact_record(user("摘要"), 0, None), rewind_record(1, "id")]:
        assert transcript.append(line) is None
    transcript.close()
    # A compaction keeps the latest list; indexes from before it no longer count.
    assert load_session(session.transcript_path).todos == (second,)


def test_a_malformed_record_is_skipped(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    session = Session.new(project, home=home)
    transcript = Transcript(session, project)
    transcript.append(message_record(user("一")))
    transcript.append({"type": "todo", "keep": 1, "todos": [{"id": "1", "content": "甲", "status": "done"}]})
    transcript.close()
    restored = load_session(session.transcript_path)
    assert restored.todos == () and restored.skipped == 1


def test_records_round_trip_through_json():
    item = TodoItem("1", "甲", "pending")
    assert json.loads(json.dumps(todo_line(0, item)))["todos"] == [item.to_json()]


async def test_the_panel_above_the_prompt_follows_the_list_and_goes_after_the_next_request(tmp_path):
    from textual.widgets import Static

    from mosscode.conversation import Conversation
    from mosscode.tui import MossCodeApp, PromptArea, ToolEntry

    provider = SequenceProvider(
        response(
            "建清单",
            [
                todo_call(
                    "a",
                    {"content": "读代码", "status": "in_progress"},
                    *[{"content": f"第 {n} 步", "status": "pending"} for n in range(2, 7)],
                )
            ],
        ),
        response("先做到这里"),
        response(
            "全做完", [todo_call("b", *[{"id": str(n), "content": "完成", "status": "completed"} for n in (1, 2)])]
        ),
        response("好了"),
        response("别的事"),
    )
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    app = MossCodeApp(Conversation(provider, registry=reg), "Demo")
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        panel = app.query_one("#todo-panel", Static)
        assert not panel.display

        async def send(text):
            app.query_one("#prompt", PromptArea).value = text
            await pilot.press("enter")
            await app._reply_task
            await pilot.pause()

        await send("开始")
        assert "Todo" in str(app.query_one(ToolEntry).render()) and "已完成 0/6" in str(
            app.query_one(ToolEntry).render()
        )
        assert panel.display
        lines = str(panel.render()).splitlines()
        assert lines[0] == "清单 · 已完成 0/6" and lines[1] == "▶ 读代码" and lines[2] == "□ 第 2 步"
        assert len(lines) == 6 and "另有 2 项未完成" in lines[-1] and "/todo" in lines[-1]
        screen = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "▶ 读代码" in screen and "先做到这里" in screen

        await send("继续")
        assert panel.display and str(panel.render()) == "✓ 清单已完成 2 项"
        # The finished list stays until the user moves on.
        await send("下一件事")
        assert not panel.display


def test_the_todo_command_lists_every_item():
    from types import SimpleNamespace

    from mosscode.builtin_commands import todo_report

    assert "当前没有任务清单" in todo_report(SimpleNamespace(todos=()))
    items = tuple(TodoItem(str(n), f"第 {n} 步", "pending") for n in range(1, 8))
    report = todo_report(SimpleNamespace(todos=items)).splitlines()
    assert report[0] == "任务清单" and report[-2] == "[ ] 7. 第 7 步" and report[-1] == "已完成 0/7"
