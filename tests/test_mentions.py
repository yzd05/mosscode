"""@ mentions: finding them in a message, attaching what they hold, and completing them in the prompt."""

import json

import pytest

from mosscode import mentions as mentions_module
from mosscode.mentions import CONTENT_BUDGET, MENTION_LIMIT, FileIndex, attach, find_mentions, partial_mention
from mosscode.messages import MENTIONS, FilesAttached, ToolCall
from mosscode.permissions import parse_rule
from mosscode.session import load_session
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, response
from test_sessions import drain, make, records


def project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (root / "notes.txt").write_text("记录\n", encoding="utf-8")
    (root / "my notes.md").write_text("空格\n", encoding="utf-8")
    return root


def registry(root, mode="default"):
    return create_default_registry(root, permission_mode=mode, load_permissions=False)


def names(text, root):
    return [(m.name, m.offset, m.limit) for m in find_mentions(text, root)]


def test_mentions_are_existing_paths_after_whitespace_without_trailing_punctuation(tmp_path):
    root = project(tmp_path)
    text = '看 @src/a.py，再看 @notes.txt。还有 @"my notes.md" 和 @src 以及 @missing.txt、a@notes.txt @src/a.py'
    assert names(text, root) == [
        ("src/a.py", 1, None),
        ("notes.txt", 1, None),
        ("my notes.md", 1, None),
        ("src/", 1, None),
    ]
    assert names("@src/a.py#L2-3 @src/a.py#L3 @src/a.py#L3-2 @src#L1", root) == [
        ("src/a.py", 2, 2),
        ("src/a.py", 3, None),
    ]
    assert names(f"@{root / 'notes.txt'}", root) == [("notes.txt", 1, None)]


async def test_a_mentioned_file_is_read_like_read_file_and_counts_as_read(tmp_path):
    root = project(tmp_path)
    tools = registry(root, "bypassPermissions")
    attached = await attach("改一下 @src/a.py#L2-3", tools)
    text = attached.message.text
    assert attached.message.synthetic == MENTIONS and attached.message.role == "user"
    assert '<file path="src/a.py" lines="2-3">\n     2\ttwo\n     3\tthree\n（共 4 行' in text
    assert attached.entries == ("src/a.py（第 2–3 行，2 行）",)
    edit = ToolCall("e1", "edit_file", json.dumps({"path": "src/a.py", "old_text": "two", "new_text": "2"}))
    result = await tools.execute(edit)
    assert not result.is_error, result.output  # No "read it first".


async def test_directories_are_listed_and_denied_paths_are_named_only(tmp_path):
    root = project(tmp_path)
    tools = registry(root)
    tools.permissions.session.append(parse_rule("Read(notes.txt)", "deny"))
    attached = await attach("@src @notes.txt", tools)
    text = attached.message.text
    assert '<directory path="src/">\na.py\n</directory>' in text
    assert "记录" not in text and "显式拒绝" in text
    assert attached.entries[0] == "src/（目录，1 项）" and attached.entries[1].startswith("notes.txt（未读取：")


async def test_outside_the_project_needs_no_approval_but_deny_rules_still_hold(tmp_path):
    root = project(tmp_path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("外面\n", encoding="utf-8")
    tools = registry(root)
    attached = await attach(f"@{outside}", tools)
    assert "外面" in attached.message.text
    tools.permissions.session.append(parse_rule(f"Read({outside.as_posix()})", "deny"))
    tools.permissions.session.append(parse_rule("Read(src/**)", "deny"))
    attached = await attach(f"@{outside} @src/a.py#L1-2 @src", tools)
    assert "外面" not in attached.message.text and "one" not in attached.message.text
    assert attached.message.text.count("命中显式拒绝规则") == 3  # The directory is not listed either.
    assert all("未读取" in entry for entry in attached.entries)


async def test_too_many_or_too_large_mentions_are_named_without_contents(tmp_path, monkeypatch):
    root = project(tmp_path)
    for index in range(MENTION_LIMIT + 1):
        (root / f"f{index}.txt").write_text(f"内容{index}\n", encoding="utf-8")
    attached = await attach(" ".join(f"@f{index}.txt" for index in range(MENTION_LIMIT + 1)), registry(root))
    assert "内容9" in attached.message.text and "内容10" not in attached.message.text
    assert attached.entries[-1] == f"f10.txt（只给出路径：一条消息最多附上 {MENTION_LIMIT} 个引用）"
    monkeypatch.setattr(mentions_module, "CONTENT_BUDGET", 10)
    attached = await attach("@f0.txt @f1.txt", registry(root))
    assert "内容0" in attached.message.text and "内容1" not in attached.message.text
    assert CONTENT_BUDGET > 10


async def test_the_typed_message_stays_as_typed_and_the_contents_follow_it(tmp_path):
    root = project(tmp_path)
    provider = SequenceProvider(response("看过了"), response("好"))
    conv = make(tmp_path, provider, registry=registry(root, "bypassPermissions"))
    events = await drain(conv.reply("看 @src/a.py"))
    assert FilesAttached(("src/a.py（4 行）",)) in events
    sent = provider.requests[0][0]
    assert [m.text for m in sent[:1]] == ["看 @src/a.py"] and sent[1].synthetic == MENTIONS
    assert "     1\tone" in sent[1].text
    history = conv.history
    assert [m.synthetic for m in history] == [None, MENTIONS, None]
    assert history[0].id is not None and history[1].id is None
    restored = load_session(conv.session.transcript_path)
    assert [m.synthetic for m in restored.history] == [None, MENTIONS, None]
    await conv.ask("再说")
    rewound = await conv.rewind(conv.rewind_points()[-1].message.id, code=False, conversation=True)
    assert rewound.message.text == "看 @src/a.py" and conv.history == ()
    lines = records(conv.session.transcript_path)
    assert any(r["type"] == "rewind" and r["keep"] == 0 for r in lines)
    await conv.close()


async def test_forks_programs_and_messages_without_mentions_attach_nothing(tmp_path):
    root = project(tmp_path)
    provider = SequenceProvider(response("好"), response("好"))
    conv = make(tmp_path, provider, registry=registry(root, "bypassPermissions"))
    await drain(conv.reply("邮件 a@notes.txt"))
    await drain(conv.reply("@notes.txt", synthetic="task-notification"))
    assert all(m.synthetic != MENTIONS for m in conv.history)
    await conv.close()


@pytest.mark.parametrize("rg", [True, False], ids=["rg", "builtin"])
def test_the_index_follows_gitignore_and_ranks_by_fuzzy_match(tmp_path, monkeypatch, rg):
    root = project(tmp_path)
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "out.py").write_text("x", encoding="utf-8")
    (root / "src" / "tui.py").write_text("x", encoding="utf-8")
    if not rg:
        monkeypatch.setattr(mentions_module.shutil, "which", lambda name: None)
    elif mentions_module.shutil.which("rg") is None:
        pytest.skip("rg is not on PATH")
    index = FileIndex(root)
    assert index.stale
    index.load()
    assert not index.stale
    assert "build/out.py" not in index.paths and "src/" in index.paths
    assert index.candidates("tui")[0] == "src/tui.py"
    assert index.candidates("") == ["src/", ".gitignore", "my notes.md", "notes.txt"]
    assert index.candidates("src/") == ["src/a.py", "src/tui.py"]


def test_a_mention_being_typed_starts_after_whitespace():
    assert partial_mention("看 @src/t") == (2, "src/t")
    assert partial_mention('@"my no') == (0, "my no")
    assert partial_mention("@") == (0, "")
    assert partial_mention("a@b") is None and partial_mention("@a b") is None and partial_mention("@a，b") is None


async def test_the_prompt_completes_mentions_in_place_and_goes_into_directories(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea

    root = project(tmp_path)
    (root / "src" / "tui.py").write_text("x", encoding="utf-8")
    conv = make(tmp_path, SequenceProvider(response("好")), registry=registry(root, "bypassPermissions"))
    app = MossCodeApp(conv, "test")
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "看 @tu 然后"
        field.move_cursor((0, 5))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [hint.label for hint in app._hint_choices][:1] == ["src/tui.py"]
        await pilot.press("tab")
        assert field.value == "看 @src/tui.py 然后" and field.cursor_location == (0, 13)
        await pilot.pause()
        assert app._hint_choices == []  # A whole file's path leaves nothing to choose; Enter sends.
        field.value = "@sr"
        await pilot.pause()
        await pilot.press("enter")  # The directory: its entries come next.
        await pilot.pause()
        assert field.value == "@src/" and [hint.label for hint in app._hint_choices] == ["src/a.py", "src/tui.py"]
        field.value = "邮件 a@s"
        await pilot.pause()
        assert app._hint_choices == []
        field.value = "@notes"
        await pilot.pause()
        await pilot.press("escape")
        assert not app.query_one("#command-hints").display
    await conv.close()


async def test_a_resumed_session_shows_what_was_attached(tmp_path):
    from mosscode.tui import resumed_rows

    root = project(tmp_path)
    conv = make(tmp_path, SequenceProvider(response("好")), registry=registry(root, "bypassPermissions"))
    await conv.ask("@notes.txt @src")
    rows = [str(row) for row in resumed_rows(conv.history)]
    assert "└ 已附上 notes.txt、src/" in rows
    await conv.close()
