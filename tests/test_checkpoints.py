"""File checkpoints and /rewind: snapshots before edits, restoring files, truncating the conversation."""

import json

import pytest

from mosscode import checkpoints as checkpoints_module
from mosscode.checkpoints import Checkpoints, Snapshot
from mosscode.conversation import Notice
from mosscode.messages import ContentBlock, Message, ProviderError
from mosscode.serialization import message_from_json, message_to_json
from mosscode.session import Session, load_session
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response
from test_sessions import _reminders, drain, make, records


def tracker(tmp_path, notices=None):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    saved = []
    checkpoints = Checkpoints(
        root,
        Session(tmp_path / "session"),
        record=saved.append,
        on_notice=(notices if notices is not None else []).append,
    )
    return root, checkpoints, saved


def test_the_first_write_of_a_turn_is_saved_once_and_a_new_file_is_recorded_as_missing(tmp_path):
    root, checkpoints, saved = tracker(tmp_path)
    (root / "a.txt").write_bytes(b"one")
    checkpoints.before_write(root / "a.txt")  # No turn yet: nothing to attach it to.
    assert saved == []
    checkpoints.begin("m1")
    for _ in range(3):
        checkpoints.before_write(root / "a.txt")
    checkpoints.before_write(root / "sub" / "b.txt")
    assert [(s.message, s.path, s.blob is None) for s in saved] == [("m1", "a.txt", False), ("m1", "sub/b.txt", True)]
    assert (tmp_path / "session" / "checkpoints" / saved[0].blob).read_bytes() == b"one"
    checkpoints.begin("m2")
    checkpoints.before_write(root / "a.txt")
    assert len(saved) == 3 and checkpoints.marks == {"m1": 0, "m2": 2}


def test_same_contents_share_one_blob_and_large_files_are_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints_module, "TOO_LARGE", 4)
    root, checkpoints, saved = tracker(tmp_path)
    for name, data in (("a.txt", b"same"), ("b.txt", b"same"), ("big.bin", b"12345")):
        (root / name).write_bytes(data)
    checkpoints.begin("m1")
    for name in ("a.txt", "b.txt", "big.bin"):
        checkpoints.before_write(root / name)
    assert saved[0].blob == saved[1].blob and saved[2] == Snapshot("m1", "big.bin", None, too_large=True)
    assert len(list((tmp_path / "session" / "checkpoints").iterdir())) == 1
    (root / "big.bin").write_bytes(b"changed")
    result = checkpoints.restore("m1")
    assert result.skipped == [("big.bin", "超过 0 MB，没有保存快照")] and (root / "big.bin").read_bytes() == b"changed"


def test_restore_writes_back_each_file_as_it_was_and_deletes_files_created_since(tmp_path):
    root, checkpoints, _ = tracker(tmp_path)
    a, b = root / "a.txt", root / "new" / "b.txt"
    a.write_bytes(b"v1")
    checkpoints.begin("m1")
    checkpoints.before_write(a)
    a.write_bytes(b"v2")
    checkpoints.before_write(b)
    b.parent.mkdir()
    b.write_bytes(b"created")
    checkpoints.begin("m2")
    checkpoints.before_write(a)
    a.write_bytes(b"v3")
    assert [s.path for s in checkpoints.changed_after("m1")] == ["a.txt", "new/b.txt"]
    later = checkpoints.restore("m2")
    assert a.read_bytes() == b"v2" and b.exists() and later.restored == ["a.txt"]
    result = checkpoints.restore("m1")
    assert a.read_bytes() == b"v1" and not b.exists()
    assert (result.restored, result.deleted, result.skipped, result.failed) == (["a.txt"], ["new/b.txt"], [], [])
    assert checkpoints.restore("m1").paths == []  # Already as it was: nothing to report.


def test_one_file_that_cannot_be_restored_leaves_the_others(tmp_path):
    root, checkpoints, _ = tracker(tmp_path)
    (root / "a.txt").write_bytes(b"a")
    (root / "b.txt").write_bytes(b"b")
    checkpoints.begin("m1")
    checkpoints.before_write(root / "a.txt")
    checkpoints.before_write(root / "b.txt")
    (root / "a.txt").unlink()
    (root / "a.txt").mkdir()  # Something else took its place.
    (root / "b.txt").write_bytes(b"changed")
    result = checkpoints.restore("m1")
    assert [path for path, _ in result.failed] == ["a.txt"] and result.restored == ["b.txt"]
    assert (root / "b.txt").read_bytes() == b"b"


def test_a_snapshot_that_cannot_be_saved_lets_the_write_go_on_and_is_reported_once(tmp_path, monkeypatch):
    def full(self, turn, path):
        raise OSError(28, "磁盘空间不足")

    notices = []
    root, checkpoints, saved = tracker(tmp_path, notices)
    monkeypatch.setattr(Checkpoints, "_save", full)
    checkpoints.begin("m1")
    checkpoints.before_write(root / "a.txt")
    checkpoints.before_write(root / "b.txt")
    assert saved == [] and notices == ["检查点保存失败（a.txt：磁盘空间不足），这次改动之后不能用 /rewind 恢复它。"]


def test_sub_agents_in_the_workspace_share_the_checkpoints_and_others_get_none(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    _, checkpoints, _ = tracker(tmp_path)
    for name in ("write_file", "edit_file"):
        registry.get(name).checkpoints = checkpoints
    options = {"agent": "subagent", "agent_type": "general-purpose", "permissions": registry.permissions}
    shared = registry.for_agent(**options)
    member = registry.for_agent(**options, checkpoints=False)
    worktree = create_default_registry(tmp_path / "work", permission_mode="bypassPermissions", load_permissions=False)
    assert shared.get("write_file").checkpoints is checkpoints and shared.get("edit_file").checkpoints is checkpoints
    assert member.get("write_file").checkpoints is None and worktree.get("edit_file").checkpoints is None
    assert registry.get("write_file").checkpoints is checkpoints


def test_typed_messages_get_ids_that_survive_the_transcript():
    message = Message("user", (ContentBlock("text", "你好"),), id="0123456789ab")
    assert message_to_json(message)["id"] == "0123456789ab"
    assert message_from_json(message_to_json(message)).id == "0123456789ab"
    assert message == Message("user", (ContentBlock("text", "你好"),))  # Ids never change equality.
    assert "id" not in message_to_json(Message("user", (ContentBlock("text", "旧版"),)))


# Through the conversation -------------------------------------------------------------------


def editing(tmp_path):
    """Turn 1 reads, turn 2 edits a.txt and creates b.txt (one edit fails), turn 3 edits a.txt again."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("v1", encoding="utf-8")
    return SequenceProvider(
        response("先看", [call("read_file", "r1", path="a.txt")]),
        response("看过了"),
        response(
            "改",
            [
                call("edit_file", "e0", path="a.txt", old_text="没有", new_text="x"),
                call("edit_file", "e1", path="a.txt", old_text="v1", new_text="v2"),
                call("write_file", "w1", path="b.txt", content="new"),
            ],
        ),
        response("改好了"),
        response("再改", [call("edit_file", "e2", path="a.txt", old_text="v2", new_text="v3")]),
        response("又改好了"),
    )


async def three_turns(conv):
    for text in ("看一下", "改一下", "再改"):
        await conv.ask(text)
    return conv.rewind_points()


async def test_typed_messages_are_points_and_only_real_writes_leave_checkpoints(tmp_path):
    conv = make(tmp_path, editing(tmp_path))
    points = await three_turns(conv)
    assert [p.message.text for p in points] == ["再改", "改一下", "看一下"]
    assert [p.files for p in points] == [1, 2, 2]
    lines = records(conv.session.transcript_path)
    saved = [r for r in lines if r["type"] == "checkpoint"]
    assert [(r["path"], r["blob"] is None) for r in saved] == [("a.txt", False), ("b.txt", True), ("a.txt", False)]
    assert saved[0]["message"] == points[1].message.id and saved[2]["message"] == points[0].message.id
    users = [r["message"] for r in lines if r["type"] == "message" and r["message"]["role"] == "user"]
    assert [m["id"] for m in users] == [p.message.id for p in reversed(points)]
    await conv.close()


async def test_rewinding_code_and_conversation_restores_files_and_survives_resume(tmp_path):
    conv = make(tmp_path, editing(tmp_path))
    points = await three_turns(conv)
    project = tmp_path / "project"
    total = conv.total_usage
    rewound = await conv.rewind(points[1].message.id, code=True, conversation=True)
    assert (project / "a.txt").read_text(encoding="utf-8") == "v1" and not (project / "b.txt").exists()
    assert rewound.restoration.restored == ["a.txt"] and rewound.restoration.deleted == ["b.txt"]
    assert rewound.kept == 4 and rewound.message.text == "改一下" and len(conv.history) == 4
    assert conv.total_usage == total
    assert [p.message.text for p in conv.rewind_points()] == ["看一下"]
    last = records(conv.session.transcript_path)[-1]
    assert (last["type"], last["keep"], last["message"]) == ("rewind", 4, points[1].message.id)
    session_id = conv.session_id
    await conv.close()

    again = make(tmp_path, SequenceProvider())
    await drain(again.resume(session_id))
    assert [m.text for m in again.history] == [m.text for m in conv.history]
    (project / "a.txt").write_text("changed later", encoding="utf-8")
    point = again.rewind_points()[0]
    assert point.files == 2  # The abandoned turns' edits still count from the earlier message.
    await again.rewind(point.message.id, code=True, conversation=False)
    assert (project / "a.txt").read_text(encoding="utf-8") == "v1"
    await again.close()


async def test_rewinding_only_the_conversation_leaves_the_files(tmp_path):
    conv = make(tmp_path, editing(tmp_path))
    points = await three_turns(conv)
    rewound = await conv.rewind(points[0].message.id, code=False, conversation=True)
    assert rewound.restoration is None and len(conv.history) == 8
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "v3"
    await conv.close()


async def test_restoring_only_code_keeps_the_talk_reminds_once_and_needs_fresh_reads(tmp_path):
    provider = editing(tmp_path)
    conv = make(tmp_path, provider)
    points = await three_turns(conv)
    rewound = await conv.rewind(points[0].message.id, code=True, conversation=False)
    assert rewound.kept is None and rewound.message is None and len(conv.history) == 12
    assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "v2"
    provider.responses += [
        response("改", [call("edit_file", "e3", path="a.txt", old_text="v2", new_text="v4")]),
        response("需要先读"),
        response("好"),
    ]
    await conv.ask("继续")
    await conv.ask("然后")
    told = _reminders(provider.requests[-3])
    assert len(told) == 1 and "/rewind 把这些文件恢复到了之前的状态：a.txt" in told[0]
    assert provider.requests[-3][0][-1].text == told[0]
    assert _reminders(provider.requests[-1]) == told  # Carried in the history, not told again.
    refused = provider.requests[-2][0][-1].tool_results[0]
    assert refused.is_error and "先用 read_file 读取" in refused.output
    assert conv.session.transcript_path.read_text(encoding="utf-8").count("/rewind 把") == 1
    await conv.close()


async def test_rewind_waits_for_background_tasks_before_touching_files(tmp_path, monkeypatch):
    conv = make(tmp_path, editing(tmp_path))
    points = await three_turns(conv)
    monkeypatch.setattr(type(conv.tasks), "background", lambda self: [object()])
    with pytest.raises(ProviderError, match="后台任务正在运行"):
        await conv.rewind(points[0].message.id, code=True, conversation=True)
    await conv.rewind(points[0].message.id, code=False, conversation=True)  # The conversation alone is fine.
    with pytest.raises(ProviderError, match="不在当前上下文"):
        await conv.rewind(points[0].message.id, code=False, conversation=True)
    await conv.close()


async def test_a_failed_snapshot_is_announced_with_the_next_events(tmp_path, monkeypatch):
    def full(self, turn, path):
        raise OSError(28, "磁盘空间不足")

    monkeypatch.setattr(Checkpoints, "_save", full)
    project = tmp_path / "project"
    project.mkdir()
    provider = SequenceProvider(response("写", [call("write_file", "w1", path="b.txt", content="x")]), response("好"))
    conv = make(tmp_path, provider)
    events = await drain(conv.reply("写个文件"))
    assert (project / "b.txt").read_text(encoding="utf-8") == "x"
    assert [e.text for e in events if isinstance(e, Notice)] == [
        "检查点保存失败（b.txt：磁盘空间不足），这次改动之后不能用 /rewind 恢复它。"
    ]
    await conv.close()


def test_replay_marks_each_message_before_snapshots_that_raced_its_record(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [
        {"type": "message", "message": message_to_json(Message("user", (ContentBlock("text", "一"),), id="m1"))},
        {"type": "checkpoint", "message": "m1", "path": "a.txt", "blob": None},
        {"type": "checkpoint", "message": "m2", "path": "b.txt", "blob": None},  # Written before m2's record.
        {"type": "message", "message": message_to_json(Message("user", (ContentBlock("text", "二"),), id="m2"))},
        {"type": "checkpoint", "message": "m2", "path": "c.txt", "blob": "0" * 32, "too_large": False},
        {"type": "checkpoint", "message": "m2", "path": "d.txt", "blob": "../x"},  # Skipped as invalid.
        {"type": "rewind", "keep": 1, "message": "m2"},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    restored = load_session(path)
    assert restored.marks == {"m1": 0, "m2": 1} and [s.path for s in restored.snapshots] == ["a.txt", "b.txt", "c.txt"]
    assert restored.skipped == 1 and restored.unanswered == ("一",) and restored.history == []


# The command and the interface --------------------------------------------------------------


async def test_rewind_offers_only_the_conversation_when_no_file_changed_since(tmp_path):
    from test_commands import FakeUI, context, run

    conv = make(tmp_path, SequenceProvider(response("你好"), response("再见")))
    ui = await run(context(conv), "/rewind")
    assert ui.notes == ["当前上下文中没有可以回退的消息。"]
    await conv.ask("你好")
    await conv.ask("再见")
    ui = FakeUI()
    await run(context(conv, ui=ui), "/checkpoint")
    assert ui.calls[-1] == ("choose", "回退 · 选择回到哪条消息之前（最新的在最上面）", 2, 0)
    await ui.pick(1)
    assert ui.calls[-1] == ("choose", "回退到“你好”之前", 2, 0)  # 只恢复对话、取消
    await ui.pick(0)
    assert ("rewound", "你好") in ui.calls and conv.history == ()
    assert ui.notes[-1] == "└ 对话已退回到这条消息之前，原消息已放回输入框。"
    await conv.close()


async def test_double_escape_on_an_empty_idle_prompt_opens_rewind(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea
    from test_tui import send

    conv = make(tmp_path, editing(tmp_path))
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        for text in ("看一下", "改一下", "再改"):
            await send(app, pilot, text)
            await app._reply_task
        field.value = "草稿"
        await pilot.press("escape", "escape")
        assert not app.query_one("#model-panel").display  # Only an empty prompt.
        field.value = ""
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app.query_one("#model-panel").display and app._panel_rows == 3
        await pilot.press("down", "enter")  # 改一下 · 之后改动 2 个文件
        await pilot.pause()
        assert app._panel_rows == 4
        await pilot.press("enter")  # 恢复代码和对话
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert field.value == "改一下" and len(conv.history) == 4
        assert (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "v1"
        assert not (tmp_path / "project" / "b.txt").exists()


async def test_a_message_taken_back_keeps_its_images_without_reusing_a_taken_number(tmp_path):
    from mosscode.tui import Attachment, MossCodeApp, PromptArea, rewound_image
    from test_conversation import FakeProvider
    from test_sessions import image_session, png

    session = image_session(tmp_path)
    sent = session.store_image(png("blue"), "[图片 #1]")
    app = MossCodeApp(make(tmp_path, FakeProvider()), "test")
    async with app.run_test(size=(100, 30)):
        field = app.query_one("#prompt", PromptArea)
        field.attachments["[图片 #1]"] = Attachment(png("red"), "剪贴板")  # Another image took #1 here.
        field.restore_message("看 [图片 #1]", {"[图片 #1]": rewound_image(sent)})
        assert field.value == "看 [图片 #2]" and field.images_in(field.value)["[图片 #2]"].data == png("blue").data
