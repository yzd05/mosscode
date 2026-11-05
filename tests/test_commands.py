"""The command registry, parsing, built-in commands and prompt commands."""

import asyncio

import pytest
from rich.console import Console

from mosscode import cli
from mosscode.builtin_commands import BUILTIN, dispatch, help_text, session_report, status_report
from mosscode.commands import Command, CommandConflict, CommandContext, CommandRegistry, parse_input, unknown_note
from mosscode.config import ProviderConfig, Settings, WebSearchConfig
from mosscode.conversation import Conversation
from mosscode.memory import MemoryStore
from mosscode.memory_extract import conversation_text
from mosscode.messages import ContentBlock, Message
from mosscode.serialization import message_from_json, message_to_json
from mosscode.session import list_sessions, load_session
from mosscode.tools import create_default_registry
from test_cli import setup_cli
from test_conversation import FakeProvider
from test_sessions import assistant, make, records, user, write_session

REVIEW_TEMPLATE = "请审查当前仓库中尚未提交的改动。\n\n1. 先用 git status 和 git diff HEAD 查看改动范围。"
# The built-in /review is a skill now; prompt commands are still tested with this one.
PROMPTS = CommandRegistry(
    [
        *BUILTIN.visible(),
        Command(
            "review",
            "让模型审查尚未提交的改动（只读）",
            "/review [范围或侧重点]",
            "prompt",
            argument="optional",
            argument_hint="[范围或侧重点]",
            template=REVIEW_TEMPLATE,
        ),
    ]
)


async def noop(ctx, argument):
    pass


class FakeUI:
    """Records what commands ask of the interface."""

    def __init__(self, provider_name="main", model="model-a"):
        self.provider_name, self.model = provider_name, model
        self.notes, self.calls = [], []
        self.pick = self.cancel = None

    async def note(self, text, style=None):
        self.notes.append(text)

    async def submit(self, prompt, *, command=None, fork=None):
        self.calls.append(("submit", prompt, command) if fork is None else ("submit", prompt, command, fork))

    def refresh_status(self):
        self.calls.append(("refresh",))

    async def clear_screen(self):
        self.calls.append(("clear_screen",))

    def choose(self, title, options, current, on_pick, on_cancel=None):
        self.calls.append(("choose", title, len(options), current))
        self.pick, self.cancel = on_pick, on_cancel

    async def compact(self, instructions):
        self.calls.append(("compact", instructions))

    def resume(self, session_id):
        self.calls.append(("resume", session_id))

    async def switch_model(self, name):
        self.calls.append(("switch_model", name))

    async def rewound(self, message):
        self.calls.append(("rewound", message.shown))

    def exit(self):
        self.calls.append(("exit",))


def context(conversation, settings=None, ui=None):
    return CommandContext(conversation, ui or FakeUI(), BUILTIN, settings)


async def run(ctx, text):
    parsed = parse_input(text, BUILTIN)
    if parsed.kind == "unknown":
        await ctx.ui.note(unknown_note(parsed, BUILTIN))
    else:
        assert parsed.kind == "command", parsed
        await dispatch(ctx, parsed)
    return ctx.ui


def tools(tmp_path, mode="default"):
    return create_default_registry(tmp_path, permission_mode=mode, load_permissions=False)


@pytest.mark.parametrize(
    "second",
    [
        Command("help", "x", "/help", "local", handler=noop),
        Command("other", "x", "/other", "local", aliases=("help",), handler=noop),
        Command("other", "x", "/other", "local", aliases=("h",), handler=noop),
        Command("h", "x", "/h", "local", handler=noop),
    ],
)
def test_names_and_aliases_must_be_unique(second):
    registry = CommandRegistry([Command("help", "x", "/help", "local", aliases=("h",), handler=noop)])
    with pytest.raises(CommandConflict) as exc:
        registry.register(second)
    assert "/help" in str(exc.value) and f"/{second.name}" in str(exc.value)


@pytest.mark.parametrize(
    "command",
    [
        Command("Help", "x", "/Help", "local", handler=noop),
        Command("1st", "x", "/1st", "local", handler=noop),
        Command("ok", "x", "/ok", "local", aliases=("Ok",), handler=noop),
        Command("ok", "x", "/ok", "local", aliases=("ok",), handler=noop),
        Command("ok", "x", "/ok", "local"),
        Command("ok", "x", "/ok", "prompt", template="  "),
    ],
)
def test_malformed_commands_are_rejected(command):
    with pytest.raises(CommandConflict):
        CommandRegistry([command])


def test_builtin_registry_has_twenty_two_visible_commands():
    assert [c.name for c in BUILTIN.visible()] == [
        "help",
        "clear",
        "compact",
        "resume",
        "rewind",
        "session",
        "memory",
        "model",
        "thinking",
        "plan",
        "do",
        "permissions",
        "status",
        "mcp",
        "skills",
        "hooks",
        "agents",
        "plugins",
        "tasks",
        "worktree",
        "team",
        "exit",
    ]
    assert BUILTIN.get("/PERMISSION").name == "permissions" and BUILTIN.get("quit").name == "exit"


def test_parsing():
    assert parse_input("  \n ", BUILTIN).kind == "empty"
    assert parse_input("/Help", BUILTIN).command.name == "help"
    parsed = parse_input("/permission plan", BUILTIN)
    assert (parsed.command.name, parsed.argument) == ("permissions", "plan")
    assert parse_input("/model DeepSeek V4.1 Flash", BUILTIN).argument == "DeepSeek V4.1 Flash"
    for text in ("/etc/hosts 怎么改", "/ 你好", "/tmp/a.py"):
        assert parse_input(text, BUILTIN).kind == "message" and parse_input(text, BUILTIN).text == text
    assert parse_input("//api 怎么设计", BUILTIN).text == "/api 怎么设计"
    unknown = parse_input("/compcat", BUILTIN)
    assert unknown.kind == "unknown"
    assert unknown_note(unknown, BUILTIN).startswith("是不是想输入 /compact？未知命令 /compcat。")
    assert "未知命令 /zzzz。输入 /help 查看命令" in unknown_note(parse_input("/zzzz", BUILTIN), BUILTIN)
    assert parse_input("/compact\n重点看并发", BUILTIN).argument == "重点看并发"
    assert unknown_note(parse_input("/", BUILTIN), BUILTIN) == "输入 /help 查看命令。"


def test_completion(tmp_path):
    store = MemoryStore(tmp_path / "u", tmp_path / "p")
    store.save("project", "style-guide", "feedback", "代码风格", "正文")
    conv = Conversation(FakeProvider(), registry=tools(tmp_path), memory=store)
    ctx = context(conv, Settings((ProviderConfig("only", "anthropic-messages", "m"),), "only"))
    assert len(BUILTIN.matches(ctx, "/")) == 22
    hidden = CommandRegistry([*BUILTIN.visible(), Command("debug", "x", "/debug", "local", hidden=True, handler=noop)])
    assert len(hidden.matches(ctx, "/")) == 22 and hidden.get("/debug") is not None
    assert [tuple(h) for h in BUILTIN.matches(ctx, "/q")] == [("/exit", "退出 MossCode（别名 /quit）", "/exit")]
    assert [h.value for h in BUILTIN.matches(ctx, "/permissions ")] == [
        "/permissions default",
        "/permissions acceptEdits",
        "/permissions plan",
        "/permissions bypassPermissions",
    ]
    assert [h.value for h in BUILTIN.matches(ctx, "/memory ")] == ["/memory style-guide"]
    assert BUILTIN.matches(ctx, "/model ") == [] and not BUILTIN.takes_completion(ctx, "/model")
    assert BUILTIN.takes_completion(ctx, "/permissions") and BUILTIN.takes_completion(ctx, "/memory")
    assert BUILTIN.matches(ctx, "/compact [说明]\n") == [] and BUILTIN.matches(ctx, "hello") == []
    assert BUILTIN.matches(ctx, "/comp")[0].label == "/compact [说明]"


async def test_migrated_commands_keep_their_behaviour(tmp_path):
    conv = Conversation(FakeProvider(), registry=tools(tmp_path))
    two = Settings(
        (
            ProviderConfig("main", "anthropic-messages", "model-a"),
            ProviderConfig("other", "openai-chat-completions", "model-b"),
        ),
        "main",
    )
    ui = await run(context(conv, two), "/permissions acceptEdits")
    assert conv.registry.permissions.mode == "acceptEdits" and ("refresh",) in ui.calls
    assert ui.notes == ["└ 权限档位：接受编辑（acceptEdits）；硬限制和显式拒绝仍生效。"]
    ui = await run(context(conv), "/permission")
    assert ui.notes[0].startswith("/permissions default：")
    ui = await run(context(conv, two), "/model")
    assert ui.calls == [("choose", "切换模型 · 对话历史保留", 2, 0)]
    await ui.pick(1)
    assert ui.calls[-1] == ("switch_model", "other")
    ui = await run(context(conv, two), "/model missing")
    assert ui.notes == ["未找到提供商 missing。可用：main、other"]
    ui = await run(context(conv), "/model")
    assert ui.notes == ["当前模型：main（model-a）。在配置文件的 providers 中添加更多提供商后可以切换。"]
    ui = await run(context(conv), "/resume")
    assert ui.notes == ["会话存档已关闭（persist_sessions: false），不能恢复会话。"]
    ui = await run(context(conv), "/compact 保留路径")
    assert ui.calls == [("compact", "保留路径")]
    ui = await run(context(conv), "/mcp")
    assert ui.notes[0].startswith("未配置 MCP Server。")
    ui = await run(context(conv), "/QUIT")
    assert ui.calls == [("exit",)]
    ui = await run(context(conv), "/clear")
    assert ui.calls == [("refresh",), ("clear_screen",)] and ui.notes[0].startswith("└ 已开始新会话")
    ui = await run(context(conv), "/exit now")
    assert ui.notes == ["/exit 不接受参数。用法：/exit"] and ui.calls == []


async def test_resume_picker_and_argument(tmp_path):
    saved = write_session(tmp_path, [user("旧会话"), assistant("好")])
    conv = make(tmp_path, FakeProvider())
    ui = await run(context(conv), "/resume")
    assert ui.calls == [("choose", "恢复会话 · 本项目最近的会话", 1, 0)]
    await ui.pick(0)
    assert ui.calls[-1] == ("resume", saved.id)
    ui = await run(context(conv), f"/resume {saved.id}")
    assert ui.calls == [("resume", saved.id)]
    ui = await run(context(conv), "/resume 20990101-000000-zzzz")
    assert ui.notes[0].startswith("未找到会话 20990101-000000-zzzz。") and ui.calls == []
    await conv.close()


# /plan and /do ----------------------------------------------------------------------------------


async def test_do_returns_to_the_mode_plan_was_entered_from(tmp_path):
    conv = Conversation(FakeProvider(), registry=tools(tmp_path, "acceptEdits"))
    ui = await run(context(conv), "/do 继续")
    assert ui.notes == ["当前不在计划模式。任务未发送。"] and ui.calls == []
    await run(context(conv), "/plan")
    assert conv.registry.permissions.mode == "plan"
    ui = await run(context(conv), "/do 按计划执行")
    assert conv.registry.permissions.mode == "acceptEdits"
    assert ui.notes == ["└ 已退出计划模式，回到接受编辑（acceptEdits）。"]
    assert ui.calls == [("refresh",), ("submit", "按计划执行", None)]
    ui = await run(context(Conversation(FakeProvider())), "/do")
    assert ui.notes == ["当前会话未启用工具。"]
    ui = await run(context(Conversation(FakeProvider())), "/plan")
    assert ui.notes == ["当前会话未启用工具，不能进入计划模式。"]


def test_status_without_requests_tools_or_mcp():
    conv = Conversation(FakeProvider(), provider_info=("main", "anthropic-messages", "model-a"))
    report = status_report(context(conv))
    assert report.splitlines()[0] == "状态"
    assert "  模型        main（anthropic-messages / model-a）" in report
    assert "  权限档位    未启用工具" in report
    assert "  上下文      尚未发送请求" in report and "  本会话用量  尚未发送请求" in report
    assert "  会话        无" in report and "  项目指令    未找到" in report
    assert "  MCP         未配置" in report and "  记忆        关闭（memory: false）" in report
    assert "  网络        未启用" in report


async def test_status_after_a_request(tmp_path):
    from mosscode.context import ContextBudget

    conv = Conversation(
        FakeProvider(), registry=tools(tmp_path, "acceptEdits"), budget=ContextBudget.for_provider(None, 4_096)
    )
    async for _ in conv.reply("hello"):
        pass
    conv.instruction_files = ("AGENTS.md", "CLAUDE.md")
    report = status_report(context(conv))
    assert "  权限档位    ⏵⏵ 接受编辑（acceptEdits）" in report
    assert f"/ {conv.budget.effective:,} Token" in report and "已压缩 0 次；窗口未知，按 128K 估算" in report
    assert "· 请求 1 次 · 费用 无价格信息" in report and "  项目指令    2 个文件（/memory 查看）" in report
    assert "  网络        读取网页可用；搜索未配置（配置文件中的 web_search）" in report


def test_status_names_the_configured_search_service(tmp_path):
    registry = create_default_registry(
        tmp_path, load_permissions=False, web_search=WebSearchConfig("tavily", api_key="tvly-secret")
    )
    report = status_report(context(Conversation(FakeProvider(), registry=registry)))
    assert "  网络        读取网页、搜索（tavily）可用" in report and "tvly-secret" not in report


async def test_session_report_and_help_for_one_command(tmp_path):
    conv = make(tmp_path, FakeProvider())
    async for _ in conv.reply("第一条消息"):
        pass
    report = session_report(conv, conv.sessions())
    assert report.splitlines()[0] == f"会话 {conv.session_id}"
    assert str(conv.session.transcript_path) in report
    assert "  标题    第一条消息" in report and "  消息    2 条" in report
    assert report.endswith("/resume 恢复其他会话。")
    (tmp_path / "off").mkdir()
    off = make(tmp_path / "off", FakeProvider(), persist=False)
    assert "未存档（persist_sessions: false）" in session_report(off, off.sessions())
    ui = await run(context(conv), "/session")
    assert ui.notes[0].startswith("会话 ")
    shown = help_text(PROMPTS, "review")
    assert shown.splitlines()[:3] == [
        "/review [范围或侧重点]",
        "让模型审查尚未提交的改动（只读）",
        "提示词命令：把预设的请求发给模型",
    ]
    assert "模板首行：请审查当前仓库中尚未提交的改动。" in shown
    assert "别名：/quit" in help_text(BUILTIN, "exit") and help_text(BUILTIN, "nope").startswith("未知命令 /nope")
    listing = help_text(BUILTIN)
    assert "/permissions [档位]    查看或切换工具权限档位（别名 /permission）" in listing and "\n\n按键\n" in listing
    await conv.close()
    await off.close()


def test_message_command_round_trips_and_old_records_load():
    message = Message("user", (ContentBlock("text", REVIEW_TEMPLATE),), command="/review 安全")
    data = message_to_json(message)
    assert data["command"] == "/review 安全" and message_from_json(data) == message
    plain = message_to_json(Message("user", (ContentBlock("text", "hi"),)))
    assert "command" not in plain and message_from_json(plain).command is None
    assert message.shown == "/review 安全" and message_from_json(plain).shown == "hi"
    assert conversation_text([message]) == "[用户]\n/review 安全"


async def test_review_in_the_tui_is_shown_saved_titled_and_replayed(tmp_path):
    from mosscode.tui import ChatEntry, MossCodeApp, resumed_rows
    from test_tui import send

    provider = FakeProvider()
    conv = make(tmp_path, provider)
    app = MossCodeApp(conv, "test", commands=PROMPTS)
    async with app.run_test() as pilot:
        await send(app, pilot, "/review 安全")
        await asyncio.wait_for(app._reply_task, timeout=5)
        entries = [entry for entry in app.query(ChatEntry) if entry.speaker == "You"]
        assert [entry.body for entry in entries] == ["/review 安全"]
    sent = provider.requests[0][-1]
    assert sent.text == REVIEW_TEMPLATE + "\n\n补充要求：安全" and sent.command == "/review 安全"
    saved = [r for r in records(conv.session.transcript_path) if r.get("type") == "message"]
    assert saved[0]["message"]["command"] == "/review 安全"
    path = conv.session.transcript_path
    await conv.close()
    summary = next(s for s in list_sessions(tmp_path / "project", home=tmp_path / "home") if s.path == path)
    assert summary.title == "/review 安全"
    restored = load_session(path)
    assert restored.history[0].shown == "/review 安全"
    console = Console(record=True, width=100)
    console.print(resumed_rows(restored.history, "Shell")[0])
    assert "/review 安全" in console.export_text() and "请审查" not in console.export_text()


# Commands in the TUI ---------------------------------------------------------------------------


async def test_argument_check_running_commands_and_forced_messages(tmp_path):
    from mosscode.tui import MossCodeApp, PromptArea
    from test_tui import send

    provider = FakeProvider()
    conv = Conversation(provider)
    app = MossCodeApp(conv, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "hello")
        await asyncio.wait_for(app._reply_task, timeout=5)
        history = conv.history
        await send(app, pilot, "/clear extra")
        assert str(app.query(".local-note")[-1].render()) == "/clear 不接受参数。用法：/clear"
        assert conv.history == history
        provider.hold = True
        await send(app, pilot, "next")
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        field = app.query_one("#prompt", PromptArea)
        await send(app, pilot, "/compact")  # /status and /tasks run during a task; others wait.
        assert field.value == "/compact" and conv.queued == ()
        field.clear()
        await send(app, pilot, "//x 不是命令")
        assert conv.queued == ("/x 不是命令",)
        app._reply_task.cancel()
        await pilot.pause()


async def test_plan_and_do_update_the_status_badge(tmp_path):
    from textual.widgets import Static

    from mosscode.tui import MossCodeApp
    from test_tui import send

    provider = FakeProvider()
    conv = Conversation(provider, registry=tools(tmp_path, "acceptEdits"))
    app = MossCodeApp(conv, "test")
    status = lambda: str(app.query_one("#status", Static).render())
    async with app.run_test() as pilot:
        await send(app, pilot, "/do 任务")
        assert str(app.query(".local-note")[-1].render()) == "当前不在计划模式。任务未发送。"
        await send(app, pilot, "/plan")
        assert status().startswith("⏸ 计划模式")
        await send(app, pilot, "/do")
        assert status().startswith("⏵⏵ 接受编辑") and conv.registry.permissions.mode == "acceptEdits"
        assert "已退出计划模式，回到接受编辑（acceptEdits）" in str(app.query(".local-note")[-1].render())
        assert provider.requests == []


async def test_tab_completes_a_unique_prefix_after_escape():
    from mosscode.tui import MossCodeApp, PromptArea

    app = MossCodeApp(Conversation(FakeProvider()), "test")
    async with app.run_test() as pilot:
        field = app.query_one("#prompt", PromptArea)
        field.value = "/sta"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app._hint_choices
        await pilot.press("tab")
        await pilot.pause()
        assert field.value == "/status"


def test_headless_prompt_commands(monkeypatch, capsys):
    monkeypatch.setattr(cli, "BUILTIN", PROMPTS)
    provider, _ = setup_cli(monkeypatch, ["-p", "/review 只看 cli"])
    cli.main()
    sent = provider.requests[0][-1]
    assert sent.text == REVIEW_TEMPLATE + "\n\n补充要求：只看 cli" and sent.command == "/review 只看 cli"
    for text in ("/etc/hosts 是什么", "//api 怎么设计"):
        provider, _ = setup_cli(monkeypatch, ["-p", text])
        cli.main()
        assert provider.requests[0][-1].text == (text[1:] if text.startswith("//") else text)
        assert provider.requests[0][-1].command is None
    capsys.readouterr()


@pytest.mark.parametrize(
    "text,error",
    [
        ("/clear", "/clear 只能在交互界面中使用。"),
        ("/QUIT", "/exit 只能在交互界面中使用。"),
        ("/compcat", "是不是想输入 /compact？未知命令 /compcat。"),
    ],
)
def test_headless_local_and_unknown_commands_exit_with_code_2(monkeypatch, capsys, text, error):
    provider, calls = setup_cli(monkeypatch, ["-p", text])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and capsys.readouterr().err.startswith(error)
    assert calls["created"] == [] and provider.requests == []


def test_status_names_the_catalog_entry_behind_the_model_settings(tmp_path):
    from mosscode.catalog import ModelInfo

    entry = ModelInfo("deepseek/deepseek-v4-flash", "deepseek", 1_000_000, 393_216, None, None, None, None, True)
    config = ProviderConfig(
        "main",
        "anthropic-messages",
        "model-a",
        context_window=1_000_000,
        max_output_tokens=32_000,
        catalog=entry,
        from_catalog=("max_output_tokens", "context_window"),
    )
    report = status_report(context(Conversation(FakeProvider()), Settings((config,), "main")))
    assert "main（anthropic-messages / model-a）；窗口、输出上限来自模型目录（deepseek/deepseek-v4-flash）" in report
