"""Session transcripts, resume and cleanup."""

import io
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mosscode import session as session_module
from mosscode.config import ConfigError, load_settings
from mosscode.context import ContextBudget
from mosscode.conversation import Conversation, Notice, Resumed
from mosscode.images import load_image
from mosscode.messages import (
    REMINDER,
    CompactFailed,
    CompactFinished,
    CompactStarted,
    Completed,
    ContentBlock,
    Message,
    ProviderError,
    ToolCall,
    ToolResult,
)
from mosscode.paths import SESSION_ID, project_key
from mosscode.serialization import message_from_json, message_to_json
from mosscode.session import (
    INTERRUPTED,
    MARKER,
    Lock,
    Session,
    Transcript,
    compact_record,
    list_sessions,
    load_session,
    message_record,
    project_sessions,
    provider_record,
    prune_all,
    repair,
)
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response
from test_context import NO_SUMMARY, chat, summary_reply
from waiting import until

SMALL = ContextBudget.for_provider(60_000, 4_096)  # Auto threshold 27,000.
INFO = ("main", "anthropic-messages", "model-a")


def user(text, synthetic=None):
    return Message("user", (ContentBlock("text", text),), synthetic=synthetic)


def assistant(text="", calls=(), protocol=None, native=()):
    content = (ContentBlock("text", text),) if text else ()
    return Message("assistant", content, protocol, tuple(native), tuple(calls))


def results(*items):
    return Message("tool", (), tool_results=tuple(items))


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make(tmp_path, provider, *, persist=True, registry=None, **options):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir(exist_ok=True)
    registry = registry or create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    return Conversation(
        provider,
        registry=registry,
        context_root=project,
        instructions_home=home,
        session=Session.new(project, home=home),
        persist=persist,
        sessions_home=home,
        provider_info=options.pop("provider_info", INFO),
        **options,
    )


def write_session(tmp_path, messages, *, when=None, extra=()):
    """A saved session of these messages, last active at `when`."""
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir(exist_ok=True)
    session = Session.new(project, home=home)
    transcript = Transcript(session, project)
    for item in [*(message_record(m) for m in messages), *extra]:
        if when is not None:
            item = {**item, "time": when.isoformat()}
        assert transcript.append(item) is None
    transcript.close()
    return session


async def drain(stream):
    return [event async for event in stream]


def test_messages_round_trip_and_odd_details_never_fail(tmp_path):
    call_a = ToolCall("c1", "edit_file", json.dumps({"path": "a.txt"}))
    history = [
        user("你好"),
        Message(
            "assistant",
            (ContentBlock("thinking", "想一想"), ContentBlock("text", "好的")),
            "anthropic-messages",
            ({"type": "thinking", "signature": "sig"},),
            (call_a,),
        ),
        results(
            ToolResult("c1", "edit_file", "已修改", details={"diff": "-a\n+b", "path": "a.txt", "lines": [1, 2]}),
        ),
        user("摘要", synthetic="compact"),
    ]
    for message in history:
        assert message_from_json(json.loads(json.dumps(message_to_json(message)))) == message
    odd = ToolResult(
        "c2", "read_file", "x", details={"path": tmp_path / "a.txt", "set": {1}, "obj": object(), 3: (1, 2)}
    )
    data = json.loads(json.dumps(message_to_json(results(odd)), ensure_ascii=False))
    details = data["tool_results"][0]["details"]
    assert details["path"] == str(tmp_path / "a.txt") and details["3"] == [1, 2] and isinstance(details["obj"], str)
    session = write_session(tmp_path, [user("看"), assistant(calls=[ToolCall("c2", "read_file", "{}")]), results(odd)])
    restored = load_session(session.transcript_path)
    assert restored.skipped == 0 and restored.history[-1].tool_results[0].details["path"] == str(tmp_path / "a.txt")


def test_strict_loading_rejects_bad_fields():
    for bad in (
        {"role": "robot"},
        {"role": "user", "content": [{"kind": "image"}]},
        {"role": "tool", "tool_results": [{"call_id": "x", "name": "n", "output": "o", "is_error": "no"}]},
        {"role": "assistant", "tool_calls": [{"id": 1, "name": "n", "arguments": "{}"}]},
        "text",
    ):
        with pytest.raises(ValueError):
            message_from_json(bad)


def png(color="red"):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 4), color).save(buffer, "PNG")
    return load_image(buffer.getvalue())


def image_session(tmp_path):
    (tmp_path / "project").mkdir(exist_ok=True)
    return Session.new(tmp_path / "project", home=tmp_path / "home")


def test_images_are_stored_once_by_content_and_transcripts_hold_references(tmp_path):
    session = image_session(tmp_path)
    first, again = session.store_image(png(), "[图片 #1]"), session.store_image(png(), "a.png")
    other = session.store_image(png("blue"), "b.png")
    files = sorted(p.name for p in (session.directory / "images").iterdir())
    assert files == sorted({first.name, other.name}) and first.name == again.name and first.read() == png().data
    message = Message("user", (ContentBlock("text", "看 "), ContentBlock("image", "[图片 #1]", first)))
    tool = results(ToolResult("c1", "read_file", "a.png", images=(again,)))
    for item in (message, tool):
        data = json.loads(json.dumps(message_to_json(item)))
        assert message_from_json(data) == item
        assert first.name in json.dumps(data) and "base64" not in json.dumps(data)
    assert message_to_json(message)["content"][1]["image"] == {
        "name": first.name,
        "media_type": "image/png",
        "width": 8,
        "height": 4,
        "size": len(png().data),
        "alt": "[图片 #1]",
    }
    assert "images" not in message_to_json(results(ToolResult("c2", "glob", "x")))["tool_results"][0]


def test_resumed_images_read_their_files_and_missing_ones_become_text(tmp_path):
    session = image_session(tmp_path)
    kept, gone = session.store_image(png(), "[图片 #1]"), session.store_image(png("blue"), "b.png")
    lost_block = session.store_image(png("green"), "[图片 #2]")
    history = [
        Message("user", (ContentBlock("image", "[图片 #1]", kept), ContentBlock("image", "[图片 #2]", lost_block))),
        assistant(calls=[ToolCall("c1", "read_file", "{}")]),
        results(ToolResult("c1", "read_file", "b.png", images=(gone, kept))),
    ]
    transcript = Transcript(session, tmp_path / "project")
    for message in history:
        transcript.append(message_record(message))
    transcript.close()
    gone.path.unlink()
    lost_block.path.unlink()
    restored = load_session(session.transcript_path)
    assert restored.skipped == 0 and restored.lost_images == 2
    content = restored.history[0].content
    assert content[0].image.read() == kept.read() and content[1] == ContentBlock("text", "[图片已丢失：[图片 #2]]")
    result = restored.history[2].tool_results[0]
    assert result.images == (kept,) and result.output == "b.png\n[图片已丢失：b.png]"


async def test_pasted_images_follow_their_placeholders_and_are_stored_only_when_sent(tmp_path):
    def kinds(message):
        return [(block.kind, block.text) for block in message.content]

    async def steer():
        conv.queue("再看 [图片 #3]", {"[图片 #3]": png("green"), "[图片 #4]": png("white")})
        yield response(calls=[call("glob", "g1", pattern="*.md")])

    provider = SequenceProvider(steer, response("看到了"), response("还记得"))
    conv = make(tmp_path, provider)
    images = {"[图片 #1]": png(), "[图片 #2]": png("blue"), "[图片 #5]": png("black")}
    await drain(conv.reply("比较 [图片 #1] 和 [图片 #2]，[图片 #1] 再提一次，[图片 #9] 不存在", images=images))
    first = provider.requests[0][0][-1]
    assert kinds(first) == [
        ("text", "比较 [图片 #1]"),
        ("image", "[图片 #1]"),
        ("text", " 和 [图片 #2]"),
        ("image", "[图片 #2]"),
        ("text", "，[图片 #1] 再提一次，[图片 #9] 不存在"),
    ]
    assert first.text == "比较 [图片 #1] 和 [图片 #2]，[图片 #1] 再提一次，[图片 #9] 不存在"
    queued = provider.requests[1][0][-1]
    assert kinds(queued) == [("text", "再看 [图片 #3]"), ("image", "[图片 #3]")]
    # Only images still named in the text were stored: #4 and #5 were dropped from their drafts.
    stored = {p.name for p in (conv.session.directory / "images").iterdir()}
    assert stored == {png(c).name for c in ("red", "blue", "green")}
    restored = load_session(conv.session.transcript_path)
    assert kinds(restored.history[0]) == kinds(first) and restored.lost_images == 0
    assert restored.history[0].content[1].image.read() == png().data
    await conv.close()


async def test_resume_reports_lost_images_once(tmp_path):
    session = image_session(tmp_path)
    image = session.store_image(png(), "[图片 #1]")
    transcript = Transcript(session, tmp_path / "project")
    transcript.append(message_record(Message("user", (ContentBlock("image", "[图片 #1]", image),))))
    transcript.append(message_record(assistant("看到了")))
    transcript.close()
    image.path.unlink()
    conv = make(tmp_path, SequenceProvider())
    resumed = (await drain(conv.resume(session.id)))[0]
    assert [n for n in resumed.notices if "图片" in n] == ["1 张图片的文件已丢失，已改为文字说明。"]
    await conv.close()


@pytest.mark.parametrize(
    "image",
    [
        {"name": "../../secret.png"},
        {"name": "A" * 32 + ".png"},
        {"name": "0" * 32 + ".png", "media_type": "image/jpeg"},
        {"name": "0" * 32 + ".svg", "media_type": "image/svg+xml"},
        {"width": -1},
        {"size": "1"},
    ],
)
def test_image_references_that_could_point_elsewhere_are_rejected(image):
    data = {"name": "0" * 32 + ".png", "media_type": "image/png", "width": 1, "height": 1, "size": 1, "alt": "x"}
    block = {"kind": "image", "text": "x", "image": data | image}
    with pytest.raises(ValueError):
        message_from_json({"role": "user", "content": [block]})
    assert message_from_json({"role": "user", "content": [block | {"image": data}]}).content[0].image.width == 1


async def test_records_follow_history_commits_and_batches_are_final(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "a.txt").write_text("alpha", encoding="utf-8")
    provider = SequenceProvider(
        response("先读", [call("read_file", "r1", path="a.txt"), call("glob", "g1", pattern="*.txt")]),
        response("再读", [call("read_file", "r2", path="a.txt")]),
        response("完成"),
    )
    conv = make(tmp_path, provider)
    path = conv.session.transcript_path
    assert not path.exists()
    assert await conv.ask("读文件") == "完成"
    lines = records(path)
    assert list(lines[0]) == ["type", "v", "id", "cwd", "created", "mosscode"]
    assert lines[0]["type"] == "session" and lines[0]["id"] == conv.session_id and SESSION_ID.fullmatch(conv.session_id)
    saved = [message_from_json(r["message"]) for r in lines[1:]]
    assert all(r["type"] == "message" for r in lines[1:]) and saved == list(conv.history)
    assert [m.role for m in saved] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert [r.call_id for r in saved[2].tool_results] == ["r1", "g1"] and not saved[2].tool_results[0].is_error
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n") and "\\u" not in raw.splitlines()[1]  # ensure_ascii=False
    await conv.close()


async def test_no_messages_no_file_and_half_lines_are_cut(tmp_path):
    conv = make(tmp_path, SequenceProvider())
    await conv.close()
    assert not conv.session.transcript_path.exists() and not conv.session.directory.exists()
    session = write_session(tmp_path, [user("一"), assistant("二")])
    with session.transcript_path.open("ab") as stream:
        stream.write(b'{"type":"message","mess')
    transcript = Transcript(session, tmp_path)
    transcript.open_existing()
    assert transcript.append(message_record(user("三"))) is None
    transcript.close()
    lines = records(session.transcript_path)
    assert [message_from_json(r["message"]).text for r in lines[1:]] == ["一", "二", "三"]


async def test_write_failure_is_reported_once_and_the_chat_goes_on(tmp_path, monkeypatch):
    def full(self):
        raise OSError(28, "磁盘空间不足")

    monkeypatch.setattr(Transcript, "_create", full)
    conv = make(tmp_path, SequenceProvider(response("一"), response("二")))
    first = await drain(conv.reply("你好"))
    notices = [e for e in first if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].text == "会话存档写入失败：磁盘空间不足，本会话之后的消息不会被保存。"
    assert any(isinstance(e, Completed) for e in first)
    second = await drain(conv.reply("继续"))
    assert not any(isinstance(e, Notice) for e in second) and len(conv.history) == 4
    assert not conv.session.transcript_path.exists()


async def test_a_session_in_use_cannot_be_resumed_until_released(tmp_path):
    saved = write_session(tmp_path, [user("一"), assistant("二")])
    first, second = make(tmp_path, SequenceProvider()), make(tmp_path, SequenceProvider())
    await drain(first.resume(saved.id))
    with pytest.raises(ProviderError, match="该会话正在另一个 MossCode 中使用"):
        await drain(second.resume(saved.id))
    summary = next(s for s in list_sessions(tmp_path / "project", home=tmp_path / "home") if s.id == saved.id)
    assert summary.locked
    await first.close()
    events = await drain(second.resume(saved.id))
    assert isinstance(events[0], Resumed) and second.session_id == saved.id
    await second.close()


async def test_persist_off_writes_nothing_and_explains(tmp_path):
    saved = write_session(tmp_path, [user("一"), assistant("二")])
    conv = make(tmp_path, SequenceProvider(response("好")), persist=False)
    await conv.ask("你好")
    assert not conv.session.transcript_path.exists()
    assert not conv.session.transcript_path.with_suffix(".lock").exists()
    assert "persist_sessions: false" in conv.unavailable()
    with pytest.raises(ProviderError, match="persist_sessions"):
        await drain(conv.resume(saved.id))


def test_settings_accept_the_new_switches(tmp_path):
    path = tmp_path / "config.yaml"
    base = "providers:\n  - {name: a, protocol: anthropic, model: m}\n  - {name: b, protocol: openai, model: n}\n"
    path.write_text(base + "persist_sessions: false\nmemory_provider: b\nauto_memory: false\n", encoding="utf-8")
    settings = load_settings(path)
    assert not settings.persist_sessions and settings.memory and not settings.auto_memory
    assert settings.memory_provider == "b"
    for extra in ("persist_sessions: 1\n", "memory_provider: missing\n", "memory: yes please\n"):
        path.write_text(base + extra, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(path)


def _raw_session(tmp_path, lines):
    project, home = tmp_path / "project", tmp_path / "home"
    session = Session.new(project, home=home)
    session.transcript_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines) + "\n"
    session.transcript_path.write_text(text, encoding="utf-8")
    return session.transcript_path


def test_replay_skips_bad_lines_and_applies_compact_and_provider_records(tmp_path):
    native = ({"type": "thinking", "signature": "sig"},)
    boundary = user("（摘要）", synthetic="compact")
    path = _raw_session(
        tmp_path,
        [
            message_record(user("u1")),
            message_record(assistant("a1", protocol="anthropic-messages", native=native)),
            "{bad json",
            {"type": "message", "message": {"role": "robot"}},
            {"type": "compact", "keep": -1},
            {"type": "from-the-future", "x": 1},
            message_record(user("u2")),
            message_record(assistant("a2", protocol="anthropic-messages", native=native)),
            compact_record(boundary, 2, None),
            message_record(user("u3")),
            message_record(assistant("a3", protocol="anthropic-messages", native=native)),
        ],
    )
    same = load_session(path, ("main", "anthropic-messages"))
    assert same.skipped == 3 and not same.stripped
    assert [m.text for m in same.history] == ["（摘要）", "u2", "a2", "u3", "a3"] and same.history[-1].native == native
    other = load_session(path, ("other", "openai-responses"))
    assert other.stripped and all(not m.native and m.protocol is None for m in other.history)
    whole = load_session(
        _raw_session(
            tmp_path, [message_record(user("u")), message_record(assistant("a")), compact_record(boundary, 99, None)]
        )
    )
    assert [m.text for m in whole.history] == ["（摘要）", "u", "a"]


def test_provider_records_strip_earlier_messages_and_name_the_model(tmp_path):
    native = ({"type": "thinking", "signature": "sig"},)
    path = _raw_session(
        tmp_path,
        [
            message_record(user("u1")),
            message_record(assistant("a1", protocol="openai-responses", native=native)),
            provider_record("main", "anthropic-messages", "model-a"),
            message_record(user("u2")),
            message_record(assistant("a2", protocol="anthropic-messages", native=native)),
        ],
    )
    restored = load_session(path, ("main", "anthropic-messages"))
    assert not restored.stripped and restored.history[1].native == () and restored.history[3].native == native
    assert load_session(path, ("second", "anthropic-messages")).stripped


THREE = [ToolCall(f"c{i}", "read_file", json.dumps({"path": f"{i}.txt"})) for i in (1, 2, 3)]


def test_missing_results_are_filled_orphans_dropped_and_trailing_users_returned(tmp_path):
    history, filled = repair([user("做"), assistant("我先看三个文件", THREE)])
    assert filled == 3 and history[1].text == "我先看三个文件"
    assert [(r.call_id, r.code, r.output) for r in history[2].tool_results] == [
        (f"c{i}", "interrupted", INTERRUPTED) for i in (1, 2, 3)
    ]
    partial, filled = repair(
        [
            user("做"),
            assistant("", THREE),
            results(ToolResult("c2", "read_file", "二"), ToolResult("zz", "read_file", "孤立")),
        ]
    )
    assert filled == 2 and [r.call_id for r in partial[2].tool_results] == ["c1", "c2", "c3"]
    assert partial[2].tool_results[1].output == "二"
    orphan, _ = repair([user("一"), results(ToolResult("x", "read_file", "孤立")), assistant("二")])
    assert [m.role for m in orphan] == ["user", "assistant"]
    path = _raw_session(
        tmp_path, [message_record(m) for m in (user("做"), assistant("看", THREE), user("排队一"), user("排队二"))]
    )
    restored = load_session(path)
    assert restored.unanswered == ("排队一", "排队二") and restored.interrupted == 3
    assert [m.role for m in restored.history] == ["user", "assistant", "tool"]


def test_a_reminder_left_without_a_reply_does_not_hide_the_unanswered_messages(tmp_path):
    reminder = Message("user", (ContentBlock("text", "<todo-list>\n[>] 1. 甲\n</todo-list>"),), synthetic=REMINDER)
    lines = [user("做"), assistant("看", THREE), user("排队一"), reminder]
    restored = load_session(_raw_session(tmp_path, [message_record(m) for m in lines]))
    assert restored.unanswered == ("排队一",)
    assert [m.role for m in restored.history] == ["user", "assistant", "tool"]
    # A reminder that has its reply stays.
    lines = [user("做"), reminder, assistant("好")]
    restored = load_session(_raw_session(tmp_path, [message_record(m) for m in lines]))
    assert restored.unanswered == () and [m.synthetic for m in restored.history] == [None, REMINDER, None]


@pytest.mark.parametrize("case", range(3))
async def test_every_protocol_accepts_the_repaired_history(case):
    from test_providers import CASES, SSEBody, adapter

    protocol, cls, frames, _ = CASES[case]
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()))
    history, _ = repair([user("做"), assistant("我先看三个文件", THREE)])
    try:
        conv = Conversation(provider, "system")
        conv._agent.restore(history)
        assert await conv.ask("继续") == "你好"
        payload = json.dumps(requests[0][1], ensure_ascii=False)
        assert all(payload.count(f'"c{i}"') >= 2 for i in (1, 2, 3)) and INTERRUPTED in payload
    finally:
        await provider.close()


def test_lists_twenty_newest_with_titles_counts_and_locks(tmp_path, monkeypatch):
    long = "很长的标题" * 20
    sessions = []
    for index in range(22):
        first = [user("（摘要）", synthetic="compact")] if index == 0 else []
        sessions.append(write_session(tmp_path, [*first, user(f"{long}{index}\n第二行"), assistant("好")]))
    for index, item in enumerate(sessions):
        stamp = (datetime.now() - timedelta(hours=100 - index)).timestamp()
        os.utime(item.transcript_path, (stamp, stamp))
    lock = Lock(sessions[-1].transcript_path.with_suffix(".lock"))
    lock.acquire()
    parsed = []
    real = json.loads
    monkeypatch.setattr(session_module.json, "loads", lambda data: parsed.append(1) or real(data))
    try:
        listed = list_sessions(tmp_path / "project", home=tmp_path / "home")
    finally:
        lock.release()
    assert [s.id for s in listed] == [s.id for s in reversed(sessions)][:20]
    assert listed[0].locked and not any(s.locked for s in listed[1:])
    assert listed[0].title == (long + "21")[:40] and listed[0].messages == 2
    assert len(parsed) <= 2 * len(listed)  # Titles only: message counts never parse JSON.
    assert all(not s.path.with_suffix("").exists() for s in listed)


async def test_empty_project_and_unknown_ids_are_explained(tmp_path):
    conv = make(tmp_path, SequenceProvider())
    with pytest.raises(ProviderError, match="本项目没有可恢复的会话"):
        conv.resolve_session(None)
    saved = write_session(tmp_path, [user("一"), assistant("二")])
    assert conv.resolve_session(None) == saved.id
    with pytest.raises(ProviderError, match=f"未找到会话 20990101-000000-abcd。最近的会话：{saved.id}"):
        conv.resolve_session("20990101-000000-abcd")


@pytest.mark.parametrize("auto_compact", [True, False])
async def test_an_oversized_session_is_compacted_before_input(tmp_path, auto_compact):
    saved = write_session(tmp_path, chat(8))
    conv = make(tmp_path, SequenceProvider(summary_reply()), budget=SMALL, auto_compact=auto_compact)
    events = await drain(conv.resume(saved.id))
    assert isinstance(events[0], Resumed)
    assert [type(e) for e in events[1:]] == [CompactStarted, CompactFinished]
    assert conv.history[0].synthetic == "compact" and conv.compactions == 1 and conv._agent.compact_failures == 0
    lines = records(saved.transcript_path)
    assert lines[-1]["type"] == "compact" and lines[-1]["keep"] == len(conv.history) - 1
    assert load_session(saved.transcript_path).history == list(conv.history)
    await conv.close()


async def test_failed_compaction_still_restores(tmp_path):
    saved = write_session(tmp_path, chat(8))
    conv = make(tmp_path, SequenceProvider(NO_SUMMARY), budget=SMALL)
    events = await drain(conv.resume(saved.id))
    failed = events[-1]
    assert isinstance(failed, CompactFailed) and failed.code == "restore"
    assert failed.reason.startswith("恢复的会话超出上下文窗口，压缩失败：") and failed.reason.endswith(
        "可以 /compact 重试或 /clear"
    )
    assert len(conv.history) == 16 and conv._agent.compact_failures == 0
    await conv.close()


def _reminders(request):
    """The reminders a request carries, all of them in its messages: nothing rides on the request alone."""
    assert all(item.kind == "environment" for item in request[1].supplements)
    return [m.text for m in request[0] if m.synthetic == REMINDER]


@pytest.mark.parametrize("hours,reminded", [(7, True), (5, False)])
async def test_gap_reminder_once_and_kept_in_the_history(tmp_path, hours, reminded):
    when = datetime.now().astimezone() - timedelta(hours=hours)
    saved = write_session(tmp_path, [user("一"), assistant("二")], when=when)
    provider = SequenceProvider(response("三"), response("四"))
    conv = make(tmp_path, provider)
    await drain(conv.resume(saved.id))
    await conv.ask("继续")
    await conv.ask("再继续")
    first, second = provider.requests
    told = _reminders(first)
    assert len(told) == reminded and all("距上次对话已过去约 7 小时" in t for t in told)
    assert [m.text for m in first[0]] == ["一", "二", "继续", *told]
    # The second request repeats the first and its reply, and tells nothing new.
    assert second[0][: len(first[0])] == first[0] and _reminders(second) == told
    assert ("距上次对话" in saved.transcript_path.read_text(encoding="utf-8")) is reminded
    await conv.close()


async def test_reads_session_rules_and_usage_reset_but_the_mode_stays(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "a.txt").write_text("alpha", encoding="utf-8")
    saved = write_session(tmp_path, [user("一"), assistant("二")])
    provider = SequenceProvider(
        response("", [call("read_file", "r", path="a.txt")]),
        response("读了"),
        response("", [call("edit_file", "e", path="a.txt", old_text="alpha", new_text="beta")]),
        response("没改成"),
    )
    registry = create_default_registry(tmp_path / "project", permission_mode="acceptEdits", load_permissions=False)
    conv = make(tmp_path, provider, registry=registry)
    await conv.ask("读")
    registry.permissions.session.append("sentinel")
    await drain(conv.resume(saved.id))
    assert registry.permissions.session == [] and registry.permissions.mode == "acceptEdits"
    assert conv.total_usage.requests == 0
    await conv.ask("改")
    edit = conv.history[-2].tool_results[0]
    assert edit.code == "not_read" and (tmp_path / "project" / "a.txt").read_text(encoding="utf-8") == "alpha"
    await conv.close()


async def test_resume_appends_to_the_same_file_and_clear_starts_a_new_one(tmp_path):
    from test_context import Dump, dump

    project = tmp_path / "project"
    project.mkdir()
    registry = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    registry.register(Dump(project))
    first = make(tmp_path, SequenceProvider(response("", [dump("d", 60_000)]), response("存好了")), registry=registry)
    await first.ask("生成大结果")
    stored = first.history[2].tool_results[0].details["persisted"]
    old_id, directory = first.session_id, first.session.directory
    await first.close()
    conv = make(tmp_path, SequenceProvider(response("记得"), response("新的")))
    await drain(conv.resume(old_id))
    assert conv.session.directory == directory and Path(stored).is_file()
    await conv.ask("还记得吗")
    texts = [message_from_json(r["message"]).text for r in records(directory.with_suffix(".jsonl"))[1:]]
    assert texts[-2:] == ["还记得吗", "记得"]
    await conv.clear()
    assert conv.session_id != old_id and conv.session.directory != directory
    await conv.ask("新话题")
    assert (
        conv.session.transcript_path.is_file() and records(conv.session.transcript_path)[1]["message"]["role"] == "user"
    )
    assert old_id in {s.id for s in list_sessions(project, home=tmp_path / "home")}
    await conv.close()


async def test_model_switch_writes_a_provider_record(tmp_path):
    conv = make(tmp_path, SequenceProvider(response("一")))
    await conv.ask("你好")
    await conv.set_provider(SequenceProvider(), info=("other", "openai-responses", "model-b"))
    last = records(conv.session.transcript_path)[-1]
    assert last["type"] == "provider" and (last["name"], last["protocol"], last["model"]) == (
        "other",
        "openai-responses",
        "model-b",
    )
    await conv.close()


def test_cleanup_removes_old_sessions_together_and_skips_the_rest(tmp_path):
    home = tmp_path / "home"
    now = datetime.now()
    base = project_sessions(tmp_path / "project", home)
    base.mkdir(parents=True)

    def make_session(name, days):
        transcript, directory, lock = base / f"{name}.jsonl", base / name, base / f"{name}.lock"
        transcript.write_text("{}\n", encoding="utf-8")
        directory.mkdir()
        (directory / MARKER).write_text("", encoding="utf-8")
        lock.write_bytes(b"")
        stamp = (now - timedelta(days=days)).timestamp()
        for item in (transcript, directory / MARKER):
            os.utime(item, (stamp, stamp))
        return transcript, directory, lock

    old = make_session("20260101-000000-aaaa", 31)
    recent = make_session("20260101-000000-bbbb", 29)
    busy = make_session("20260101-000000-cccc", 31)
    stray = base / "notes.jsonl"
    stray.write_text("{}\n", encoding="utf-8")
    stamp = (now - timedelta(days=60)).timestamp()
    os.utime(stray, (stamp, stamp))
    legacy = home / ".mosscode" / "sessions" / "20250101-000000-dddd"
    legacy.mkdir(parents=True)
    (legacy / MARKER).write_text("", encoding="utf-8")
    os.utime(legacy / MARKER, (stamp, stamp))
    held = Lock(busy[2])
    held.acquire()
    try:
        assert prune_all(home, now) == 2
    finally:
        held.release()
    assert not any(p.exists() for p in old) and all(p.exists() for p in recent)
    assert busy[0].exists() and busy[1].exists() and stray.exists() and not legacy.exists()


def test_project_keys_are_readable_hashed_and_case_folded_on_windows(tmp_path):
    key = project_key(tmp_path / "My Project")
    assert key.endswith("-" + key.rsplit("-", 1)[1]) and len(key.rsplit("-", 1)[1]) == 8
    assert key.rsplit("-", 1)[0].endswith("my-project" if os.name == "nt" else "My-Project")
    assert project_key(tmp_path / "a-b") != project_key(tmp_path / "a" / "b")


def _cli(monkeypatch, capsys, args, provider):
    from mosscode import cli
    from test_cli import setup_cli

    setup_cli(monkeypatch, args)
    monkeypatch.setattr(cli, "create_provider", lambda config: provider)
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_continue_and_resume_answer_in_the_restored_context(monkeypatch, capsys):
    code, out, _ = _cli(
        monkeypatch, capsys, ["-p", "记住数字 42", "--output-format", "json"], SequenceProvider(response("记住了"))
    )
    first = json.loads(out)
    assert code == 0 and SESSION_ID.fullmatch(first["session_id"])
    provider = SequenceProvider(response("是 42"))
    code, out, _ = _cli(monkeypatch, capsys, ["--continue", "-p", "数字是几", "--output-format", "json"], provider)
    assert code == 0 and json.loads(out)["session_id"] == first["session_id"]
    assert [m.text for m in provider.requests[0][0]] == ["记住数字 42", "记住了", "数字是几"]
    provider = SequenceProvider(response("还是 42"))
    code, out, _ = _cli(
        monkeypatch,
        capsys,
        ["--resume", first["session_id"], "-p", "再说一次", "--output-format", "stream-json"],
        provider,
    )
    events = [json.loads(line) for line in out.splitlines()]
    assert events[0] == {"type": "session", "id": first["session_id"], "resumed": True}
    assert len(provider.requests[0][0]) == 5


def test_cli_rejects_resume_without_id_in_print_mode_and_unknown_ids(monkeypatch, capsys):
    code, _, err = _cli(monkeypatch, capsys, ["--resume", "-p", "hi"], SequenceProvider())
    assert code == 2 and "--continue" in err
    code, _, err = _cli(monkeypatch, capsys, ["--resume", "20990101-000000-abcd", "-p", "hi"], SequenceProvider())
    assert code == 1 and "未找到会话" in err
    code, _, err = _cli(monkeypatch, capsys, ["--continue", "-p", "hi"], SequenceProvider())
    assert code == 1 and "本项目没有可恢复的会话" in err


async def test_tui_lists_resumes_renders_and_returns_unanswered_text(tmp_path):
    from rich.console import Console
    from textual.widgets import OptionList, Static

    from mosscode.tui import MossCodeApp, PromptArea
    from test_tui import send

    old = write_session(tmp_path, [user("第一个会话"), assistant("好")])
    target = write_session(
        tmp_path,
        [
            user("修改 notes.txt"),
            assistant("先读", [ToolCall("r", "read_file", '{"path": "notes.txt"}')]),
            user("排队的追问"),
        ],
    )
    stamp = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(old.transcript_path, (stamp, stamp))
    conv = make(tmp_path, SequenceProvider())
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(100, 40)) as pilot:
        await send(app, pilot, "/resume")
        options = app.query_one("#models", OptionList)
        assert app.query_one("#model-panel").display and options.option_count == 2 and options.highlighted == 0
        assert "修改 notes.txt" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")

        def rendered():
            console = Console(record=True, width=100, file=io.StringIO())
            for widget in app.query(Static):
                console.print(getattr(widget, "transcript", None) or widget.render())
            return console.export_text()

        await until(lambda: not app._task_running and "最后 1 条消息没有得到回复" in rendered())
        shown = rendered()
        assert "已恢复会话 · 修改 notes.txt · 2 条消息" in shown
        assert "Read" in shown and "notes.txt" in shown
        assert "1 个工具调用在中断时没有结果" in shown and "最后 1 条消息没有得到回复" in shown
        assert app.query_one("#prompt", PromptArea).value == "排队的追问"
        assert conv.session_id == target.id
    await conv.close()


async def test_tui_refuses_resume_while_running(tmp_path):
    import asyncio

    from mosscode.tui import MossCodeApp, PromptArea
    from test_conversation import FakeProvider
    from test_tui import send

    provider = FakeProvider()
    provider.hold = True
    conv = make(tmp_path, provider)
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(100, 30)) as pilot:
        await send(app, pilot, "你好")
        await asyncio.wait_for(provider.started.wait(), 2)
        await send(app, pilot, "/resume")
        assert not app.query_one("#model-panel").display
        assert app.query_one("#prompt", PromptArea).value == "/resume"
        assert "命令请在完成后发送" in app._status_note
        app.action_cancel_task()
        await pilot.pause()
    await conv.close()


async def test_cleared_results_are_restored_as_notes(tmp_path):
    from mosscode.messages import ResultsCleared
    from test_context import seeded, steps

    history = steps(8)
    conv = seeded(SequenceProvider(response("好")), history, session=Session.create(tmp_path / "live"))
    committed = []
    conv._agent.on_commit = committed.append
    events = [e async for e in conv.reply("继续")]
    assert any(isinstance(e, ResultsCleared) for e in events)
    clears = [record for record in committed if record["type"] == "clear"]
    assert len(clears) == 1 and sorted(clears[0]["results"]) == ["c0", "c1", "c2", "c3", "c4", "c5"]
    lines = [message_record(m) for m in history] + [json.loads(json.dumps(clears[0])), message_record(user("继续"))]
    restored = load_session(_raw_session(tmp_path, lines))
    assert restored.skipped == 0 and restored.history[: len(history)] == list(conv.history[: len(history)])
    assert (
        "已清理" in restored.history[2].tool_results[0].output
        and restored.history[2].tool_results[0].details["cleared"]
    )
    # A record that cannot be applied is skipped and the results stay whole.
    broken = [message_record(m) for m in history] + [{"type": "clear", "results": {"c0": {"output": 3}}}]
    whole = load_session(_raw_session(tmp_path / "other", broken))
    assert whole.skipped == 1 and whole.history == history
