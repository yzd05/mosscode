"""Skills — parsing, places and overrides, re-reading, the model-visible catalog."""

import asyncio
import json
from pathlib import Path

import pytest

from mosscode.skills import SkillCatalog, SkillError, expand_arguments, missing_tools, parse_skill, split_tool_list
from waiting import until


def write_skill(
    base: Path, name: str, front: str = "", body: str = "按步骤执行。", *, tools=None, description: str | None = None
) -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    description = f"{name} 的说明" if description is None else description
    header = f"---\nname: {name}\ndescription: {description}\n{front}---\n"
    (directory / "SKILL.md").write_text(header + body, encoding="utf-8")
    if tools is not None:
        (directory / "tool.json").write_text(json.dumps(tools), encoding="utf-8")
    return directory


def tool(name="detect", **extra):
    return {
        "name": name,
        "description": "检测",
        "input_schema": {"type": "object", "properties": {}},
        "command": ["${PYTHON}", "${SKILL_DIR}/run.py"],
        **extra,
    }


@pytest.fixture
def places(tmp_path, isolated_home):
    builtin, project = tmp_path / "builtin", tmp_path / "project"
    builtin.mkdir()
    project.mkdir()
    user = isolated_home / ".mosscode" / "skills"
    return builtin, user, project / ".mosscode" / "skills", project


def load(places, **kwargs):
    builtin, _, _, project = places
    return SkillCatalog.load(project, builtin=builtin, **kwargs)


# Parsing ----------------------------------------------------------------------------------


def test_parse_fields_and_claude_code_spelling(tmp_path):
    directory = write_skill(
        tmp_path,
        "check-api",
        (
            "allowed-tools: Read, Grep, Bash(git commit:*)\ncontext: fork\nfork_context: recent\nmodel: fast\n"
            'argument-hint: "[路径]"\ndisable-model-invocation: true\nuser-invocable: false\nversion: 3\n'
        ),
    )
    skill = parse_skill(directory, "project")
    assert skill.allowed_tools == ("read_file", "grep", "execute_command")
    assert (skill.mode, skill.fork_context, skill.model, skill.argument_hint) == ("fork", "recent", "fast", "[路径]")
    assert not skill.model_invocable and not skill.user_invocable
    assert skill.body == "按步骤执行。" and skill.source == "project"


def test_parse_defaults_and_list_form(tmp_path):
    skill = parse_skill(
        write_skill(tmp_path, "a", "allowed_tools: [read_file, load_skill, mcp__docs__search]\n"), "user"
    )
    assert skill.mode == "inline" and skill.fork_context == "none" and skill.model is None
    assert skill.allowed_tools == ("read_file", "mcp__docs__search")  # load_skill is never narrowed
    assert skill.model_invocable and skill.user_invocable and skill.tools == ()


def test_split_tool_list_keeps_arguments_together():
    assert split_tool_list("Bash(git add:*), Bash(git status:*) Read") == [
        "Bash(git add:*)",
        "Bash(git status:*)",
        "Read",
    ]


@pytest.mark.parametrize(
    "front, body, message",
    [
        ("", "正文", "缺少 YAML frontmatter"),
        ("---\ndescription: x\n---\n", "正文", "缺少 name"),
        ("---\nname: bad\n---\n", "正文", "缺少 description"),
        ('---\nname: bad\ndescription: ""\n---\n', "正文", "description 必须是非空字符串"),
        ("---\nname: Bad\ndescription: x\n---\n", "正文", "name 只能含小写字母"),
        ("---\nname: other\ndescription: x\n---\n", "正文", "必须与目录名"),
        ("---\nname: bad\ndescription: x\nmode: async\n---\n", "正文", "mode 只能是"),
        ("---\nname: bad\ndescription: x\nmode: inline\ncontext: fork\n---\n", "正文", "矛盾"),
        ("---\nname: bad\ndescription: x\nuser-invocable: no thanks\n---\n", "正文", "true 或 false"),
        ("---\nname: bad\ndescription: x\nallowed_tools: 3\n---\n", "正文", "allowed_tools 必须是"),
        ("---\nname: bad\ndescription: x\n---\n", "", "正文为空"),
        ("---\nname: bad\ndescription: [x\n---\n", "正文", "不是合法的 YAML"),
        ("---\nname: bad\ndescription: x\n", "正文", "没有结束"),
    ],
)
def test_parse_rejects(tmp_path, front, body, message):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "SKILL.md").write_text(front + body, encoding="utf-8")
    with pytest.raises(SkillError, match=message) as info:
        parse_skill(directory, "project")
    assert str(directory) in str(info.value) or "Skill bad" in str(info.value)


@pytest.mark.parametrize("name", ["-pdf", "pdf-", "pdf--processing", "a" * 65, "pdf_processing"])
def test_names_follow_the_agent_skills_rules(tmp_path, name):
    with pytest.raises(SkillError, match="name 只能含小写字母"):
        parse_skill(write_skill(tmp_path, name), "project")


def test_names_may_start_with_a_digit(tmp_path):
    assert parse_skill(write_skill(tmp_path, "3d-print"), "project").name == "3d-print"


def test_agent_skills_fields_load_and_others_only_warn(tmp_path):
    front = (
        "license: Apache-2.0\ncompatibility: Requires git and   jq\n"
        'metadata:\n  author: example-org\n  version: "1.0"\nallowed-tools: Bash(git:*) Read WebFetch\n'
        "when_to_use: later\n"
    )
    skill = parse_skill(write_skill(tmp_path, "pdf-processing", front), "project")
    assert (skill.license, skill.compatibility) == ("Apache-2.0", "Requires git and jq")
    assert skill.allowed_tools == ("execute_command", "read_file", "web_fetch")
    assert len(skill.warnings) == 1 and "不认识的字段 when_to_use" in skill.warnings[0]


@pytest.mark.parametrize(
    "front, message",
    [
        ("license: 3\n", "license 应为非空字符串"),
        ("compatibility: " + "x" * 501 + "\n", "compatibility 应为"),
        ("metadata:\n  version: 1\n", "metadata 应为字符串到字符串的映射"),
        ("metadata: [a]\n", "metadata 应为字符串到字符串的映射"),
    ],
)
def test_bad_specification_fields_warn_and_still_load(tmp_path, front, message):
    skill = parse_skill(write_skill(tmp_path, "s", front), "project")
    assert skill.license == "" and skill.compatibility == ""
    assert len(skill.warnings) == 1 and message in skill.warnings[0]


def test_a_long_description_warns_but_loads(tmp_path):
    skill = parse_skill(write_skill(tmp_path, "s", description="x" * 1025), "project")
    assert len(skill.description) == 1025 and "超过 1024 个字符" in skill.warnings[0]


def test_parse_rejects_large_and_non_utf8(tmp_path):
    directory = write_skill(tmp_path, "big", body="x" * 70000)
    with pytest.raises(SkillError, match="64 KB"):
        parse_skill(directory, "user")
    (directory / "SKILL.md").write_bytes("---\nname: big\ndescription: 中文\n---\n正文".encode("gbk"))
    with pytest.raises(SkillError, match="UTF-8"):
        parse_skill(directory, "user")


def test_parse_accepts_bom(tmp_path):
    directory = tmp_path / "bom"
    directory.mkdir()
    (directory / "SKILL.md").write_bytes(b"\xef\xbb\xbf---\nname: bom\ndescription: x\n---\nbody")
    assert parse_skill(directory, "user").name == "bom"


def test_tool_json(tmp_path):
    directory = write_skill(
        tmp_path, "my-test", "allowed_tools: [detect, execute_command]\n", tools=[tool(timeout=30), tool("report")]
    )
    skill = parse_skill(directory, "builtin")
    assert [t.full_name for t in skill.tools] == ["skill__my_test__detect", "skill__my_test__report"]
    assert skill.tools[0].timeout == 30 and skill.tools[1].timeout == 120
    assert skill.allowed_tools == ("skill__my_test__detect", "execute_command")


@pytest.mark.parametrize(
    "tools, message",
    [
        ("{", "不是合法的 JSON"),
        ({"name": "x"}, "数组"),
        ([tool("Bad")], "name 须以小写字母开头"),
        ([tool(description="")], "description"),
        ([tool(input_schema={"type": "string"})], "input_schema 不合法"),
        ([tool(input_schema={"type": "object", "properties": 3})], "input_schema 不合法"),
        ([tool(command=[])], "command"),
        ([tool(command=["python", 3])], "command"),
        ([tool(timeout=0)], "timeout"),
        ([tool(timeout=601)], "timeout"),
        ([tool(), tool()], "重复"),
        ([tool("x" * 32)], "超过 64 个字符"),
    ],
)
def test_tool_json_rejects(tmp_path, tools, message):
    name = "n" * 30 if message.startswith("超过") else "t"
    directory = write_skill(tmp_path, name)
    (directory / "tool.json").write_text(tools if isinstance(tools, str) else json.dumps(tools), encoding="utf-8")
    with pytest.raises(SkillError, match=message) as info:
        parse_skill(directory, "project")
    assert "tool.json" in str(info.value)


def test_arguments():
    assert expand_arguments("检查 $ARGUMENTS 的接口", " src/api ") == "检查 src/api 的接口"
    assert expand_arguments("检查 $ARGUMENTS", "") == "检查 "
    assert expand_arguments("检查", "安全") == "检查\n\n补充要求：安全"
    assert expand_arguments("检查", "") == "检查"


def test_render(tmp_path):
    skill = parse_skill(
        write_skill(tmp_path, "t", "allowed_tools: [read_file, detect]\n", "跑 $ARGUMENTS", tools=[tool()]), "project"
    )
    text = skill.render("tests/a.py")
    assert text.splitlines()[:3] == [
        f"Skill t（目录：{tmp_path / 't'}）",
        "专属工具：skill__t__detect",
        "本 Skill 建议使用的工具：read_file、skill__t__detect",
    ]
    assert text.endswith("\n\n跑 tests/a.py")
    assert "建议使用的工具" not in skill.render("", fork=True)


# Places, overrides and start-time checks ------------------------------------------------


def test_project_overrides_user_overrides_builtin(places):
    builtin, user, project_skills, _ = places
    write_skill(builtin, "foo", description="内置")
    write_skill(user, "foo", description="用户")
    write_skill(project_skills, "foo", description="项目")
    catalog = load(places)
    foo = catalog.get("foo")
    assert (foo.description, foo.source) == ("项目", "project")
    assert foo.overrides == (builtin / "foo" / "SKILL.md", user / "foo" / "SKILL.md")
    assert catalog.problems == ()


def test_broken_project_skill_keeps_user_version(places):
    _, user, project_skills, _ = places
    write_skill(user, "foo", description="用户")
    write_skill(project_skills, "foo", "mode: async\n")
    write_skill(project_skills, "bar")
    (project_skills / "empty").mkdir()
    catalog = load(places)
    assert catalog.get("foo").description == "用户" and catalog.get("bar") is not None
    assert len(catalog.problems) == 2
    assert any("mode 只能是" in p for p in catalog.problems)
    assert any("没有 SKILL.md" in p and "empty" in p for p in catalog.problems)


def test_agents_skills_directories_are_read_with_mosscode_winning(places, isolated_home):
    builtin, user, project_skills, project = places
    shared_user, shared_project = isolated_home / ".agents" / "skills", project / ".agents" / "skills"
    write_skill(builtin, "foo", description="内置")
    write_skill(shared_user, "foo", description="共享用户")
    write_skill(shared_user, "only-user", description="共享用户")
    write_skill(user, "foo", description="用户")
    write_skill(shared_project, "foo", description="共享项目")
    write_skill(shared_project, "bar", description="共享项目")
    write_skill(project_skills, "bar", description="项目")
    write_skill(shared_project, "baz", "when_to_use: x\n", description="共享项目")
    catalog = load(places)
    assert catalog.get("only-user").source == "user"
    assert (catalog.get("foo").description, catalog.get("foo").source) == ("共享项目", "project")
    assert catalog.get("foo").overrides == (
        builtin / "foo" / "SKILL.md",
        shared_user / "foo" / "SKILL.md",
        user / "foo" / "SKILL.md",
    )
    assert catalog.get("bar").description == "项目"
    assert catalog.get("baz") is not None
    assert len(catalog.problems) == 1 and catalog.problems[0].startswith("Skill baz 提示：")


def test_reserved_names_are_skipped(places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "help")
    write_skill(project_skills, "quit")
    write_skill(project_skills, "ok")
    catalog = load(places, reserved={"help", "quit"})
    assert list(catalog.skills) == ["ok"]
    assert any("与内置命令 /help 冲突" in p for p in catalog.problems)
    assert any("/quit" in p for p in catalog.problems)


def test_start_time_whitelist_check(places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "typo", "allowed_tools: [read_flie]\n")
    write_skill(project_skills, "web", "allowed-tools: TodoWrite\n")
    write_skill(project_skills, "other", "allowed_tools: [skill__own__detect]\n")
    write_skill(project_skills, "own", "allowed_tools: [detect, mcp__x__y, memory]\n", tools=[tool()])
    catalog = load(places)
    assert list(catalog.skills) == ["own"]
    assert len(catalog.problems) == 3
    assert any("read_flie 不是已知工具" in p for p in catalog.problems)
    assert any("TodoWrite 不是已知工具" in p for p in catalog.problems)


def test_missing_places_are_silent(tmp_path):
    catalog = SkillCatalog.load(tmp_path / "nowhere", builtin=tmp_path / "none")
    assert catalog.skills == {} and catalog.problems == ()


def test_missing_tools_counts_own_tools(tmp_path):
    skill = parse_skill(
        write_skill(tmp_path, "s", "allowed_tools: [detect, read_file, mcp__x__y, memory]\n", tools=[tool()]), "user"
    )
    assert missing_tools(skill, {"read_file", "grep"}) == ["mcp__x__y", "memory"]


# Re-reading ----------------------------------------------------------------------------


def test_fresh_reads_again_and_falls_back(places):
    _, _, project_skills, _ = places
    directory = write_skill(project_skills, "foo", body="第一版")
    catalog = load(places)
    write_skill(project_skills, "foo", body="第二版", description="新说明")
    skill, notice = catalog.fresh("foo")
    assert skill.body == "第二版" and notice is None
    assert catalog.get("foo").description == "foo 的说明"  # The catalog changes on /skills reload only.
    (directory / "SKILL.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
    skill, notice = catalog.fresh("foo")
    assert skill.body == "第二版" and "已使用缓存版本" in notice
    (directory / "SKILL.md").unlink()
    assert catalog.fresh("foo")[0].body == "第二版"
    write_skill(project_skills, "foo", body="第三版", tools=[tool()])
    assert catalog.fresh("foo")[0].tools[0].full_name == "skill__foo__detect"
    (directory / "tool.json").write_text("[", encoding="utf-8")
    skill, notice = catalog.fresh("foo")
    assert skill.body == "第三版" and skill.tools and "tool.json" in notice


def test_catalog_section(places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "b", description="乙" * 300)
    write_skill(project_skills, "a")
    write_skill(project_skills, "hidden", "disable-model-invocation: true\n")
    text, notice = load(places).catalog_section()
    assert notice is None
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert lines[0] == "- a：a 的说明"
    assert lines[1] == "- b：" + "乙" * 249 + "…"
    assert "hidden" not in text and "load_skill" in text


def test_catalog_section_limit(places):
    _, _, project_skills, _ = places
    for index in range(40):
        write_skill(project_skills, f"s{index:02d}", description="说明" * 120)
    text, notice = load(places).catalog_section()
    assert len(text) < 8000 + 400
    assert notice and notice.startswith("有 ") and "仍可用命令调用" in notice


def test_empty_catalog_section():
    assert SkillCatalog().catalog_section() == ("", None)


# Commands ---------------------------------------------------------------------------


def commands_for(catalog):
    from mosscode.builtin_commands import BUILTIN

    return BUILTIN.with_skills(catalog)


def conversation_for(tmp_path, places, provider=None):
    from mosscode.builtin_commands import BUILTIN
    from test_conversation import FakeProvider
    from test_sessions import make

    return make(tmp_path, provider or FakeProvider(), skills=load(places, reserved=BUILTIN.names()))


async def run_command(conversation, text, ui=None):
    from mosscode.builtin_commands import dispatch
    from mosscode.commands import CommandContext, parse_input
    from test_commands import FakeUI

    if ui is None:
        ui = FakeUI()
        ui.registry = commands_for(conversation.skills)[0]
    parsed = parse_input(text, ui.registry)
    assert parsed.kind == "command", parsed
    await dispatch(CommandContext(conversation, ui, ui.registry), parsed)
    return ui


def test_skill_commands_are_registered_on_a_copy(places):
    from mosscode.builtin_commands import BUILTIN
    from mosscode.commands import CommandContext, parse_input

    _, _, project_skills, _ = places
    write_skill(project_skills, "foo", 'argument-hint: "[路径]"\n')
    write_skill(project_skills, "quiet", "user-invocable: false\n")
    commands, problems = commands_for(load(places))
    assert problems == () and BUILTIN.get("foo") is None
    command = commands.get("/foo")
    assert command.kind == "skill" and command.skill == "foo" and command.argument == "optional"
    assert command.usage == "/foo [路径]"
    ctx = CommandContext(None, None, commands)
    hints = commands.matches(ctx, "/fo")
    assert [(hint.value, hint.description, hint.label) for hint in hints] == [
        ("/foo", "foo 的说明 [skill]", "/foo [路径]")
    ]
    assert [hint.value for hint in commands.matches(ctx, "/qu")] == ["/exit"]  # The alias /quit, not the skill.
    assert parse_input("/quiet 现在", commands).command.skill == "quiet"  # Not listed, still callable.


def test_skill_name_taken_by_a_command_is_a_problem_not_an_error(tmp_path):
    from mosscode.builtin_commands import BUILTIN

    skill = parse_skill(write_skill(tmp_path, "help"), "project")
    commands, problems = BUILTIN.with_skills(SkillCatalog({"help": skill}))
    assert commands.get("help").kind == "local" and len(problems) == 1 and "/help" in problems[0]


async def test_inline_skill_sends_the_rendered_sop(tmp_path, places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "foo", "allowed-tools: Read, Grep\n", body="检查 $ARGUMENTS 的接口。")
    conversation = conversation_for(tmp_path, places)
    ui = await run_command(conversation, "/foo  src/api ")
    ((_, prompt, command),) = ui.calls
    assert command == "/foo src/api" and ui.notes == []
    assert prompt.startswith(f"Skill foo（目录：{project_skills / 'foo'}）\n")
    assert "本 Skill 建议使用的工具：read_file、grep" in prompt and prompt.endswith("\n\n检查 src/api 的接口。")
    await conversation.close()


async def test_inline_skill_with_a_missing_mcp_tool_is_not_sent(tmp_path, places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "docs", "allowed_tools: [read_file, mcp__docs__search]\n")
    conversation = conversation_for(tmp_path, places)
    ui = await run_command(conversation, "/docs")
    assert ui.calls == [] and "mcp__docs__search" in ui.notes[0] and "read_file" not in ui.notes[0]
    await conversation.close()


async def test_inline_skill_reads_the_file_again(tmp_path, places):
    _, _, project_skills, _ = places
    directory = write_skill(project_skills, "foo", body="旧规程")
    conversation = conversation_for(tmp_path, places)
    (directory / "SKILL.md").write_text("---\nname: foo\ndescription: x\n---\n新规程", encoding="utf-8")
    ui = await run_command(conversation, "/foo")
    assert ui.calls[0][1].endswith("新规程")
    (directory / "SKILL.md").write_text("坏了", encoding="utf-8")
    ui = await run_command(conversation, "/foo")
    assert ui.calls[0][1].endswith("新规程") and "已使用缓存版本" in ui.notes[0]
    await conversation.close()


async def test_skills_list_details_and_help(tmp_path, places):
    from mosscode.builtin_commands import help_text

    builtin, user, project_skills, _ = places
    write_skill(builtin, "foo", body="内置版")
    write_skill(
        project_skills, "foo", "context: fork\nfork_context: recent\n", body="第一步：读接口。\n第二步", tools=[tool()]
    )
    write_skill(user, "quiet", "user-invocable: false\ndisable-model-invocation: true\n")
    write_skill(project_skills, "bad", "mode: async\n")
    conversation = conversation_for(tmp_path, places)
    ui = await run_command(conversation, "/skills")
    lines = ui.notes[0].splitlines()
    assert lines[0] == "Skill（2）"
    assert lines[1].split() == ["/foo", "项目", "fork", "foo", "的说明"]
    assert lines[2].split()[:3] == ["/quiet", "用户", "inline"]
    assert lines[3].startswith("! Skill 已跳过：") and "bad" in lines[3]
    assert lines[-1].startswith("输入 /<名称> 调用")
    detail = (await run_command(conversation, "/skill foo")).notes[0]
    for text in (
        str(project_skills / "foo" / "SKILL.md"),
        str(builtin / "foo" / "SKILL.md"),
        "fork（fork_context: recent）",
        "规程首行    第一步：读接口。",
        "skill__foo__detect  ${PYTHON} ${SKILL_DIR}/run.py  超时 120 秒",
    ):
        assert text in detail
    assert "仍可输入调用" in (await run_command(conversation, "/skills quiet")).notes[0]
    assert (await run_command(conversation, "/skills nope")).notes[0] == "未找到 Skill nope。已加载：foo、quiet"
    commands = commands_for(conversation.skills)[0]
    shown = help_text(commands, "foo", conversation.skills).splitlines()
    assert shown[:3] == ["/foo [参数]", "foo 的说明 [skill]", "Skill：fork，来源项目（/skills foo 查看详情）"]
    assert shown[-1] == "规程首行：第一步：读接口。"
    assert help_text(commands, "quiet", conversation.skills).startswith("未知命令")
    assert "/foo [参数]" in help_text(commands) and "/quiet" not in help_text(commands)
    await conversation.close()


async def test_skills_without_any(tmp_path, places):
    conversation = conversation_for(tmp_path, places)
    ui = await run_command(conversation, "/skills")
    assert ui.notes == ["没有 Skill。可以在 .mosscode/skills/<名称>/SKILL.md 中添加。"]
    await conversation.close()


async def test_skills_reload_updates_the_run_table(tmp_path, places):
    from mosscode.builtin_commands import BUILTIN
    from mosscode.commands import CommandContext
    from test_commands import FakeUI

    _, _, project_skills, _ = places
    write_skill(project_skills, "old")
    conversation = conversation_for(tmp_path, places)
    ui = FakeUI()
    ui.registry = commands_for(conversation.skills)[0]
    write_skill(project_skills, "new")
    write_skill(project_skills, "help")
    (project_skills / "old" / "SKILL.md").unlink()
    await run_command(conversation, "/skills reload", ui)
    lines = ui.notes[0].splitlines()
    assert lines[0] == "└ 已重新扫描 Skill：共 1 个，新增 new，移除 old。"
    assert "与内置命令 /help 冲突" in lines[1] and "old" in lines[2]
    assert ui.registry.get("new").kind == "skill" and ui.registry.get("old") is None
    assert BUILTIN.get("new") is None and conversation.skills.get("new") is not None
    values = [hint.value for hint in ui.registry.matches(CommandContext(conversation, ui, ui.registry), "/skills ")]
    assert values == ["/skills reload", "/skills new"]
    await conversation.close()


async def test_skill_in_the_tui_is_shown_saved_and_replayed(tmp_path, places):
    from rich.console import Console

    from mosscode.session import load_session
    from mosscode.tui import ChatEntry, MossCodeApp, resumed_rows
    from test_conversation import FakeProvider
    from test_sessions import records
    from test_tui import send

    _, _, project_skills, _ = places
    write_skill(project_skills, "foo", body="按接口规范检查。")
    provider = FakeProvider()
    conversation = conversation_for(tmp_path, places, provider)
    app = MossCodeApp(conversation, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "/foo 只看 v2")
        await asyncio.wait_for(app._reply_task, timeout=5)
        entries = [entry for entry in app.query(ChatEntry) if entry.speaker == "You"]
        assert [entry.body for entry in entries] == ["/foo 只看 v2"]
    sent = provider.requests[0][-1]
    assert sent.command == "/foo 只看 v2" and sent.text.endswith("按接口规范检查。\n\n补充要求：只看 v2")
    saved = [r for r in records(conversation.session.transcript_path) if r.get("type") == "message"]
    assert saved[0]["message"]["command"] == "/foo 只看 v2"
    path = conversation.session.transcript_path
    await conversation.close()
    console = Console(record=True, width=100)
    console.print(resumed_rows(load_session(path).history, "Shell")[0])
    assert "/foo 只看 v2" in console.export_text() and "按接口规范" not in console.export_text()


def test_headless_skill(monkeypatch, capsys, tmp_path):
    from mosscode import cli
    from test_cli import setup_cli

    write_skill(tmp_path / ".mosscode" / "skills", "foo", body="规程正文")
    monkeypatch.chdir(tmp_path)
    provider, _ = setup_cli(monkeypatch, ["-p", "/foo x"])
    cli.main()
    sent = provider.requests[0][-1]
    assert sent.command == "/foo x" and sent.text.startswith("Skill foo（目录：")
    assert sent.text.endswith("规程正文\n\n补充要求：x")
    capsys.readouterr()


def test_headless_skills_command_and_missing_tool(monkeypatch, capsys, tmp_path):
    from mosscode import cli
    from test_cli import setup_cli

    write_skill(tmp_path / ".mosscode" / "skills", "docs", "allowed_tools: [mcp__docs__search]\n")
    write_skill(tmp_path / ".mosscode" / "skills", "help")
    monkeypatch.chdir(tmp_path)
    provider, calls = setup_cli(monkeypatch, ["-p", "/skills"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and calls["created"] == []
    err = capsys.readouterr().err  # Scan problems come first, then the reason for exit code 2.
    assert err.index("与内置命令 /help 冲突") < err.index("/skills 只能在交互界面中使用。")
    provider, calls = setup_cli(monkeypatch, ["-p", "/docs"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    err = capsys.readouterr().err
    assert exc.value.code == 1 and provider.requests == []
    assert "与内置命令 /help 冲突" in err and "mcp__docs__search" in err


def test_headless_unknown_skill_reports_why_it_was_skipped(monkeypatch, capsys, tmp_path):
    from mosscode import cli
    from test_cli import setup_cli

    write_skill(tmp_path / ".mosscode" / "skills", "docs", "allowed_tools: [read_file, reed_file]\n")
    monkeypatch.chdir(tmp_path)
    provider, calls = setup_cli(monkeypatch, ["-p", "/docs"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    err = capsys.readouterr().err
    assert exc.value.code == 2 and calls["created"] == []
    assert err.index("reed_file 不是已知工具") < err.index("未知命令 /docs")


# Two-stage loading ---------------------------------------------------------------------


def scripted(tmp_path, places, *responses, mode="bypassPermissions"):
    from mosscode.builtin_commands import BUILTIN
    from mosscode.tools import create_default_registry
    from test_agent import SequenceProvider
    from test_sessions import make

    provider = SequenceProvider(*responses)
    project = tmp_path / "project"
    registry = create_default_registry(project, permission_mode=mode, load_permissions=False)
    return provider, make(tmp_path, provider, registry=registry, skills=load(places, reserved=BUILTIN.names()))


def load_call(name, arguments=""):
    from mosscode.messages import ToolCall

    return ToolCall("load", "load_skill", json.dumps({"name": name, "arguments": arguments}))


def kinds(request):
    return [item.kind for item in request[1].supplements]


async def drain(stream):
    return [event async for event in stream]


async def test_catalog_and_load_skill_pin_the_sop(tmp_path, places):
    from mosscode.builtin_commands import status_report
    from mosscode.commands import CommandContext
    from test_agent import response
    from test_commands import FakeUI

    _, _, project_skills, _ = places
    write_skill(project_skills, "api-check", body="按接口规范检查 $ARGUMENTS。", description="检查接口")
    write_skill(project_skills, "manual", "disable-model-invocation: true\n")
    provider, conversation = scripted(
        tmp_path,
        places,
        response(calls=[load_call("api-check", "v2")]),
        response("好"),
        response("还在"),
        response("清空后"),
    )
    await drain(conversation.reply("检查接口"))
    first, second = provider.requests[:2]
    stable = first[1].stable
    assert "## Skill" in stable and "- api-check：检查接口" in stable and "manual" not in stable
    assert "load_skill" in [tool.name for tool in first[2]] and "active-skills" not in kinds(first)
    assert kinds(second)[:2] == ["environment", "active-skills"]
    pinned = second[1].supplements[1].text
    assert "按接口规范检查 v2。" in pinned and pinned.count("=== Skill api-check（目录：") == 1
    result = conversation.history[2].tool_results[0]
    assert not result.is_error and result.output.startswith("已加载 Skill api-check，规程已放入 <active-skills>")
    assert "按接口规范" not in result.output
    assert conversation.active_skills == ("api-check",)
    await drain(conversation.reply("继续"))
    assert kinds(provider.requests[2])[:2] == ["environment", "active-skills"]
    report = status_report(CommandContext(conversation, FakeUI(), None))
    assert "已激活 Skill  api-check" in report
    await conversation.clear()
    assert conversation.active_skills == ()
    await drain(conversation.reply("新问题"))
    assert "active-skills" not in kinds(provider.requests[3])
    assert "已激活 Skill" not in status_report(CommandContext(conversation, FakeUI(), None))
    await conversation.close()


async def test_active_skills_keep_their_order_and_refresh_in_place(tmp_path, places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "a", body="规程 A $ARGUMENTS")
    write_skill(project_skills, "b", body="规程 B")
    _, conversation = scripted(tmp_path, places)
    before = conversation._agent.estimate()
    conversation._load_skill("a", "1")
    conversation._load_skill("b", "")
    text, details = conversation._load_skill("a", "2")
    assert text.startswith("已刷新 Skill a") and details == {"skill": "a", "arguments": "2", "refreshed": True}
    assert conversation.active_skills == ("a", "b")
    texts = conversation._agent.active_skills.texts()
    assert texts[0].endswith("规程 A 2") and texts[1].endswith("规程 B")
    assert conversation._agent.estimate() > before  # The pinned SOPs are part of the estimate.
    await conversation.close()


async def test_load_skill_failures(tmp_path, places):
    from mosscode.tools.base import ToolFailure

    _, _, project_skills, _ = places
    write_skill(project_skills, "api-check")
    write_skill(project_skills, "manual", "disable-model-invocation: true\n")
    write_skill(project_skills, "docs", "allowed_tools: [mcp__docs__search]\n")
    write_skill(project_skills, "big", body="x" * 30000)
    write_skill(project_skills, "huge", body="y" * 15000)
    _, conversation = scripted(tmp_path, places)
    for name, code, text in (
        ("api-chek", "skill_unknown", "相近的名称：api-check"),
        ("manual", "skill_unavailable", "只能由用户输入 /manual"),
        ("docs", "skill_unavailable", "mcp__docs__search"),
    ):
        with pytest.raises(ToolFailure) as exc:
            conversation._load_skill(name, "")
        assert exc.value.code == code and text in str(exc.value)
    conversation._load_skill("big", "")
    with pytest.raises(ToolFailure) as exc:
        conversation._load_skill("huge", "")
    assert exc.value.code == "skill_too_large" and conversation.active_skills == ("big",)
    conversation._load_skill("big", "")  # Refreshing itself does not count twice.
    await conversation.close()


async def test_fork_skill_loaded_by_the_model_says_how_to_isolate(tmp_path, places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "deep", "context: fork\n", tools=[tool()])
    _, conversation = scripted(tmp_path, places)
    text, _ = conversation._load_skill("deep", "")
    assert "新增专属工具：skill__deep__detect" in text and "请用户输入 /deep" in text
    await conversation.close()


async def test_catalog_limit_is_a_startup_notice_and_no_tools_means_no_catalog(tmp_path, places):
    from mosscode.conversation import Conversation
    from test_conversation import FakeProvider

    _, _, project_skills, _ = places
    for index in range(40):
        write_skill(project_skills, f"skill-{index:02}", description="很长的描述" * 60)
    _, conversation = scripted(tmp_path, places)
    notices = await conversation.start()
    assert any("未列入模型可见的目录" in notice for notice in notices)
    await conversation.close()
    plain = Conversation(FakeProvider(), skills=load(places))
    assert plain._agent.skill_catalog == "" and plain.registry is None
    await plain.close()


async def test_compaction_keeps_active_skills(tmp_path, places):
    from test_agent import response
    from test_context import summary_reply

    _, _, project_skills, _ = places
    write_skill(project_skills, "a", body="规程 A")
    provider, conversation = scripted(
        tmp_path,
        places,
        response(calls=[load_call("a")]),
        response("好"),
        response("第二"),
        summary_reply(),
        response("压缩后"),
    )
    await drain(conversation.reply("开始"))
    await drain(conversation.reply("第二个问题"))
    await drain(conversation.compact())
    await drain(conversation.reply("继续"))
    assert conversation.compactions == 1
    assert kinds(provider.requests[-1])[:2] == ["environment", "active-skills"]
    await conversation.close()


async def test_reload_drops_removed_active_skills_and_load_skill(tmp_path, places):
    _, _, project_skills, _ = places
    write_skill(project_skills, "a")
    _, conversation = scripted(tmp_path, places)
    conversation._load_skill("a", "")
    (project_skills / "a" / "SKILL.md").unlink()
    ui = await run_command(conversation, "/skills reload")
    assert ui.notes[0].startswith("└ 已重新扫描 Skill：共 0 个，移除 a。")
    assert conversation.active_skills == () and conversation.registry.get("load_skill") is None
    assert conversation._agent.skill_catalog == ""
    await conversation.close()


async def test_permissions_for_load_skill_and_skill_files(tmp_path, places):
    _, user, _, _ = places
    write_skill(user, "a")
    script = user / "a" / "run.py"
    script.write_text("print(1)", encoding="utf-8")
    _, conversation = scripted(tmp_path, places, mode="default")
    policy = conversation.registry.permissions

    def decide(tool_name, **args):
        item = conversation.registry.get(tool_name)
        return policy.decide(policy.operation(item, item.validate_arguments(args)), read_only=item.read_only).action

    assert decide("read_file", path=str(user / "a" / "SKILL.md")) == "ask"
    assert decide("load_skill", name="a") == "allow"
    conversation._load_skill("a", "")
    assert decide("read_file", path=str(user / "a" / "SKILL.md")) == "allow"
    assert decide("glob", pattern="*.py", path=str(user / "a")) == "allow"
    assert decide("execute_command", command=f"python {script}") == "ask"
    conversation.set_mode("plan")
    assert "load_skill" in [d.name for d in conversation.registry.definitions(plan=True)]
    assert decide("load_skill", name="a") == "allow"
    assert decide("read_file", path=str(user / "a" / "SKILL.md")) == "allow"
    await conversation.clear()
    assert decide("read_file", path=str(user / "a" / "SKILL.md")) == "ask"
    await conversation.close()


@pytest.mark.parametrize("index", range(3))
async def test_active_skills_sit_in_the_prefix_for_every_protocol(index):
    from mosscode.messages import ContentBlock, Message
    from mosscode.prompts import PromptRequest, Supplement, active_skills
    from test_providers import CASES, SSEBody, adapter

    protocol, cls, frames, _ = CASES[index]
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()))
    active = active_skills(["Skill a（目录：x）\n\n规程 A"])
    prompt = PromptRequest("stable", (Supplement("environment", "env"), active, Supplement("system-reminder", "提醒")))
    try:
        _ = [e async for e in provider.stream([Message("user", (ContentBlock("text", "问题"),))], prompt)]
    finally:
        await provider.close()
    payload = requests[0][1]
    if protocol == "anthropic-messages":
        texts = [block["text"] for block in payload["system"]]
        assert texts[0] == "stable" and texts[1].startswith("<environment>") and texts[2].startswith("<active-skills>")
        assert payload["messages"][-1]["content"][-1]["text"].startswith("<system-reminder>")
    else:
        content = payload["input"] if protocol == "openai-responses" else payload["messages"]
        texts = [item["content"] for item in content]
        start = 0 if protocol == "openai-responses" else 1
        assert texts[start].startswith("<environment>") and texts[start + 1].startswith("<active-skills>")
        assert texts[start + 2] == "问题" and texts[-1].startswith("<system-reminder>")


# Skill tools ---------------------------------------------------------------------------

SCRIPT = """import json, os, sys, time
data = json.load(sys.stdin)
open(os.path.join(os.environ["MOSSCODE_SKILL_DIR"], "ran.txt"), "a", encoding="utf-8").write("ran")
if data.get("sleep"):
    time.sleep(data["sleep"])
    open(os.path.join(os.environ["MOSSCODE_SKILL_DIR"], "woke.txt"), "w", encoding="utf-8").write("woke")
if data.get("fail"):
    print("部分输出")
    print("出错了：找不到配置", file=sys.stderr)
    sys.exit(3)
print(json.dumps({"args": data, "cwd": os.getcwd(), "dir": os.environ["MOSSCODE_SKILL_DIR"],
                  "root": os.environ["MOSSCODE_PROJECT_ROOT"], "utf8": os.environ["PYTHONUTF8"],
                  "encoding": os.environ["PYTHONIOENCODING"], "text": "检测到 pytest"}, ensure_ascii=False))
"""

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "fail": {"type": "boolean"}, "sleep": {"type": "number"}},
    "additionalProperties": False,
}


def script_skill(base, name="foo", **extra):
    directory = write_skill(base, name, tools=[tool(input_schema=SCHEMA, **extra)])
    (directory / "run.py").write_text(SCRIPT, encoding="utf-8")
    return directory


def skill_registry(tmp_path, directory, mode="bypassPermissions"):
    from mosscode.tools import create_default_registry
    from mosscode.tools.skill import SkillTool

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    registry = create_default_registry(project, permission_mode=mode, load_permissions=False)
    skill = parse_skill(directory, "user")
    registry.register(SkillTool(project, skill, skill.tools[0]))
    return registry, skill.tools[0].full_name


def skill_call(name, **args):
    from mosscode.messages import ToolCall

    return ToolCall("c", name, json.dumps(args))


async def test_skill_tool_runs_the_script(tmp_path):
    directory = script_skill(tmp_path / "skills")
    registry, name = skill_registry(tmp_path, directory)
    definition = next(d for d in registry.definitions() if d.name == name)
    assert name == "skill__foo__detect" and definition.description.startswith("Skill foo 的专属工具。检测")
    assert definition.parameters == SCHEMA
    result = await registry.execute(skill_call(name, path="中文 目录"))
    assert not result.is_error, result.output
    data = json.loads(result.output)
    assert data["args"] == {"path": "中文 目录"} and data["text"] == "检测到 pytest"
    assert Path(data["cwd"]).resolve() == (tmp_path / "project").resolve()
    assert Path(data["dir"]) == directory.resolve() and Path(data["root"]) == (tmp_path / "project").resolve()
    assert (data["utf8"], data["encoding"]) == ("1", "utf-8")


async def test_skill_tool_failures(tmp_path):
    directory = script_skill(tmp_path / "skills")
    registry, name = skill_registry(tmp_path, directory)
    failed = await registry.execute(skill_call(name, fail=True))
    assert failed.is_error and failed.code == "nonzero_exit"
    assert "退出码为 3" in failed.output and "部分输出" in failed.output and "出错了：找不到配置" in failed.output
    (directory / "ran.txt").unlink()
    invalid = await registry.execute(skill_call(name, path=3))
    assert invalid.code == "invalid_arguments" and "path" in invalid.output and not (directory / "ran.txt").exists()
    for command in (["no-such-command-mosscode"], ["${SKILL_DIR}/missing.py"]):
        other = script_skill(tmp_path / f"skills-{len(command[0])}", "bar", command=command)
        registry, missing = skill_registry(tmp_path / f"p{len(command[0])}", other)
        result = await registry.execute(skill_call(missing))
        assert result.code == "command_not_found" and "未执行" in result.output


async def test_skill_tool_timeout_ends_the_process(tmp_path):
    directory = script_skill(tmp_path / "skills", timeout=1)
    registry, name = skill_registry(tmp_path, directory)
    result = await registry.execute(skill_call(name, sleep=3))
    assert result.code == "timeout" and (directory / "ran.txt").exists()
    await asyncio.sleep(3)
    assert not (directory / "woke.txt").exists()


async def test_skill_tool_permissions(tmp_path):
    import yaml

    from mosscode.permissions import parse_rule

    directory = script_skill(tmp_path / "skills")
    registry, name = skill_registry(tmp_path, directory, mode="default")
    policy = registry.permissions
    assert name not in [d.name for d in registry.definitions(plan=True)]
    denied = await registry.execute(skill_call(name, path="a"))
    assert denied.code == "approval_required" and not (directory / "ran.txt").exists()
    policy.interactive = True
    job = asyncio.create_task(registry.execute(skill_call(name, path="a")))
    request = await asyncio.wait_for(policy.requests.get(), 2)
    assert request.operation.skill == "foo" and request.operation.target == '{"path":"a"}'
    preview = request.operation.preview
    assert (
        preview.startswith("Skill foo 的专属工具 detect\n命令：")
        and str(directory.resolve()) in preview
        and "run.py" in preview
    )
    assert '参数：{"path":"a"}' in preview and not (directory / "ran.txt").exists()
    policy.respond(request.id, "deny")
    assert (await job).code == "permission_denied" and not (directory / "ran.txt").exists()
    job = asyncio.create_task(registry.execute(skill_call(name, path="a")))
    policy.respond((await asyncio.wait_for(policy.requests.get(), 2)).id, "session")
    assert not (await job).is_error
    policy.interactive = False
    assert not (await registry.execute(skill_call(name, path="other"))).is_error  # The whole tool, any arguments.
    policy.session.clear()
    policy.session.append(parse_rule(name, "allow"))
    policy.session.append(parse_rule(name + '({"path":"secret*"})', "deny"))
    assert not (await registry.execute(skill_call(name, path="b"))).is_error
    assert (await registry.execute(skill_call(name, path="secret.txt"))).code == "permission_denied"
    policy.set_mode("plan")
    assert (await registry.execute(skill_call(name, path="b"))).code == "plan_blocked"
    policy.set_mode("bypassPermissions")
    policy.session.clear()
    assert not (await registry.execute(skill_call(name, path="b"))).is_error
    # A permanent grant is saved as the bare tool name, like MCP tools.
    policy.set_mode("default")
    policy.interactive = True
    job = asyncio.create_task(registry.execute(skill_call(name, path="c")))
    policy.respond((await asyncio.wait_for(policy.requests.get(), 2)).id, "permanent")
    assert not (await job).is_error
    saved = yaml.safe_load((tmp_path / "project" / ".mosscode" / "permissions.local.yaml").read_text(encoding="utf-8"))
    assert saved["allow"] == [name]


async def test_skill_tools_are_registered_on_use_and_removed(tmp_path, places):
    from mosscode.messages import ToolCall

    _, _, project_skills, _ = places
    script_skill(project_skills, "foo")
    script_skill(project_skills, "bar")
    _, conversation = scripted(tmp_path, places)
    names = conversation.registry.names
    assert not any(name.startswith("skill__") for name in names())
    await run_command(conversation, "/foo")
    assert "skill__foo__detect" in names() and "skill__bar__detect" not in names()
    conversation._load_skill("bar", "")
    assert "skill__bar__detect" in names()
    await conversation.clear()
    assert not any(name.startswith("skill__") for name in names())
    conversation._load_skill("foo", "")
    conversation._load_skill("bar", "")
    (project_skills / "bar" / "SKILL.md").unlink()
    tools = json.loads((project_skills / "foo" / "tool.json").read_text(encoding="utf-8"))
    tools[0]["description"] = "新的说明"
    (project_skills / "foo" / "tool.json").write_text(json.dumps(tools), encoding="utf-8")
    await run_command(conversation, "/skills reload")
    assert "skill__bar__detect" not in names()
    assert conversation.registry.get("skill__foo__detect").description.endswith("新的说明")
    result = await conversation.registry.execute(ToolCall("c", "skill__foo__detect", "{}"))
    assert not result.is_error and json.loads(result.output)["text"] == "检测到 pytest"
    await conversation.close()


# Transcripts --------------------------------------------------------------------------------


async def test_resume_restores_skills(tmp_path, places):
    import shutil

    from mosscode.conversation import Resumed
    from test_agent import response
    from test_sessions import records

    _, _, project_skills, _ = places
    write_skill(project_skills, "pin", body="按规程 $ARGUMENTS", tools=[tool()])
    write_skill(project_skills, "inline", tools=[tool()])
    write_skill(project_skills, "gone", tools=[tool()])
    _, first = scripted(
        tmp_path,
        places,
        response(calls=[load_call("pin", "v1")]),
        response("好"),
        response(calls=[load_call("gone")]),
        response("好"),
        response("内联"),
    )
    await drain(first.reply("一"))
    await drain(first.reply("二"))
    await run_command(first, "/inline 参数")
    saved = [
        (r["name"], r["arguments"], r["pinned"]) for r in records(first.session.transcript_path) if r["type"] == "skill"
    ]
    assert saved == [("pin", "v1", True), ("gone", "", True), ("inline", "参数", False)]
    session_id = first.session_id
    await first.close()
    shutil.rmtree(project_skills / "gone")
    provider, second = scripted(tmp_path, places, response("恢复后"))
    events = await drain(second.resume(session_id))
    resumed = next(event for event in events if isinstance(event, Resumed))
    assert "未恢复的 Skill：gone（已不存在）。" in resumed.notices
    assert second.active_skills == ("pin",)
    await drain(second.reply("继续"))
    request = provider.requests[0]
    assert kinds(request)[:2] == ["environment", "active-skills"] and "按规程 v1" in request[1].supplements[1].text
    names = [definition.name for definition in request[2]]
    assert "skill__pin__detect" in names and "skill__inline__detect" in names and "skill__gone__detect" not in names
    await second.close()


def test_skill_records_load_and_old_sessions_still_work(tmp_path):
    from mosscode.session import load_session, skill_record
    from test_sessions import assistant, user, write_session

    messages = [user("问"), assistant("答")]
    old = load_session(write_session(tmp_path, messages).transcript_path)
    assert old.skills == () and old.skipped == 0 and len(old.history) == 2
    bad = {"type": "skill", "time": "2026-10-26T10:00:00+08:00", "name": "x", "arguments": "", "pinned": "yes"}
    path = write_session(
        tmp_path, messages, extra=(skill_record("a", "参数", True), bad, skill_record("b", "", False))
    ).transcript_path
    restored = load_session(path)
    assert restored.skills == (("a", "参数", True), ("b", "", False)) and restored.skipped == 1


# Fork skills ---------------------------------------------------------------------------


def fork_skill(base, name="rev", front="", **kwargs):
    return write_skill(base, name, "mode: fork\n" + front, body="审查 $ARGUMENTS", **kwargs)


async def fork_run(conversation, text):
    """/name args through the command layer, then the run the UI would start."""
    ui = await run_command(conversation, text)
    submitted = [item for item in ui.calls if item[0] == "submit"]
    assert len(submitted) == 1 and len(submitted[0]) == 4, ui.calls
    _, prompt, command, fork = submitted[0]
    events = await drain(conversation.reply(prompt, command=command, fork=fork))
    return ui, fork, events


def texts_of(messages):
    return [message.text for message in messages]


async def test_fork_runs_apart_and_adds_one_exchange(tmp_path, places):
    from mosscode.messages import Completed, RunStopped, Usage
    from mosscode.session import load_session
    from test_agent import response

    _, _, project_skills, _ = places
    fork_skill(project_skills, front="fork_context: recent\n", tools=[tool()])
    write_skill(project_skills, "visible")
    turns = [response(f"答{i}", usage=Usage(10, 1)) for i in range(4)]
    provider, conversation = scripted(
        tmp_path, places, *turns, response("审查结果", usage=Usage(100, 5)), response("好")
    )
    for text in "一二三四":
        await drain(conversation.reply(text))
    ui, fork, events = await fork_run(conversation, "/rev a.py")
    assert ui.notes == ["└ 在独立上下文中运行 Skill rev"] and fork.skill.name == "rev"
    messages, system, tools, _ = provider.requests[4]
    assert texts_of(messages)[:-1] == ["二", "答1", "三", "答2", "四", "答3"]  # fork_context recent: the last 3 turns.
    assert messages[-1].text.startswith("Skill rev（目录：") and messages[-1].text.endswith("审查 a.py")
    assert "## Skill" not in system.stable and system.supplements[0].kind == "environment"
    names = [definition.name for definition in tools]
    assert "skill__rev__detect" in names and "load_skill" not in names and "read_file" in names
    assert "skill__rev__detect" not in conversation.registry.names()  # The sub-agent only.
    stops = [event for event in events if isinstance(event, RunStopped)]
    assert [stop.reason for stop in stops] == ["completed"] and events[-1] is stops[0]
    assert [event.message.text for event in events if isinstance(event, Completed)] == ["审查结果"]
    user, answer = conversation.history[-2:]
    assert user.command == "/rev a.py" and user.shown == "/rev a.py"
    assert user.text == "/rev a.py\n\n（以上命令在独立上下文中运行了 Skill rev，结果见下一条回复。）"
    assert answer.role == "assistant" and answer.text == "审查结果" and len(conversation.history) == 10
    assert conversation.total_usage.requests == 5 and conversation.total_usage.input_tokens == 140
    await drain(conversation.reply("继续"))
    assert texts_of(provider.requests[5][0])[-3:] == [user.text, "审查结果", "继续"]
    restored = load_session(conversation.session.transcript_path)
    assert [m.shown for m in restored.history[-4:-2]] == ["/rev a.py", "审查结果"]
    await conversation.close()


async def test_fork_context_tools_and_model(tmp_path, places):
    from types import SimpleNamespace

    from test_agent import SequenceProvider, response

    _, _, project_skills, _ = places
    fork_skill(project_skills, "none", "allowed_tools: [read_file, skill__none__detect]\n", tools=[tool(), tool("x")])
    fork_skill(project_skills, "full", "fork_context: full\nmodel: other\n")
    fork_skill(project_skills, "lost", "allowed_tools: [mcp__srv__echo]\n")
    fork_skill(project_skills, "nomodel", "model: missing\n")
    provider, conversation = scripted(tmp_path, places, response("答"), response("无上下文"))
    other = SequenceProvider(response("另一个模型"))
    other.shut = 0

    async def close():
        other.shut += 1

    other.close = close
    conversation.provider_configs = (SimpleNamespace(name="other", budget=lambda: conversation._agent.budget),)
    conversation.provider_factory = lambda config: other
    await drain(conversation.reply("一"))
    await fork_run(conversation, "/none")
    messages, _, tools, _ = provider.requests[1]
    assert texts_of(messages) == [messages[-1].text] and "建议使用的工具" not in messages[-1].text
    assert [definition.name for definition in tools] == ["read_file", "skill__none__detect"]
    await fork_run(conversation, "/full")
    note = "/none\n\n（以上命令在独立上下文中运行了 Skill none，结果见下一条回复。）"
    assert texts_of(other.requests[0][0])[:4] == ["一", "答", note, "无上下文"]
    assert other.shut == 1 and len(provider.requests) == 2 and conversation.history[-1].text == "另一个模型"
    for text, problem in (
        ("/lost", "Skill lost 未运行：工具不可用：mcp__srv__echo"),
        ("/nomodel", "Skill nomodel 未运行：没有名为 missing 的提供商（可用：other）。"),
    ):
        ui = await run_command(conversation, text)
        assert not [item for item in ui.calls if item[0] == "submit"]
        assert len(ui.notes) == 1 and ui.notes[0].startswith(problem), ui.notes
    await conversation.close()


async def test_fork_failures_leave_a_marked_exchange(tmp_path, places):
    from mosscode.messages import ProviderError, RunStopped
    from test_agent import call, response

    _, _, project_skills, _ = places
    fork_skill(project_skills)
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        yield response("不会到达")

    provider, conversation = scripted(
        tmp_path,
        places,
        ProviderError("服务不可用"),
        response(calls=[call("no_such_tool", "u1")]),
        hang,
        response("主对话还在"),
    )
    with pytest.raises(ProviderError, match="服务不可用"):
        await fork_run(conversation, "/rev")
    assert conversation.history[-1].text == "[Skill rev 未完成：服务不可用]"
    conversation._agent.max_iterations = 1
    _, _, events = await fork_run(conversation, "/rev")
    assert [event.reason for event in events if isinstance(event, RunStopped)] == ["max_iterations"]
    assert conversation.history[-1].text.startswith("[Skill rev 未完成：已达最大迭代轮数 1")
    conversation._agent.max_iterations = 10
    ui = await run_command(conversation, "/rev")
    _, prompt, command, fork = ui.calls[-1]
    seen = []

    async def consume():
        async for event in conversation.reply(prompt, command=command, fork=fork):
            seen.append(event)

    task = asyncio.create_task(consume())
    await until(lambda: conversation._busy)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[-1].reason == "cancelled" and conversation.history[-1].text == "[Skill rev 未完成：已取消]"
    assert not conversation._busy
    roles = [message.role for message in conversation.history]
    assert roles == ["user", "assistant"] * 3
    await drain(conversation.reply("继续"))
    assert conversation.history[-1].text == "主对话还在"
    await conversation.close()


async def test_fork_shares_the_permission_policy(tmp_path, places):
    from mosscode.permissions import ApprovalRequest
    from test_agent import call, response

    _, _, project_skills, _ = places
    fork_skill(project_skills)
    provider, conversation = scripted(
        tmp_path,
        places,
        response(calls=[call("write_file", "w1", path="plan.txt", content="x")]),
        response("计划模式完"),
        response(calls=[call("execute_command", "c1", command="echo hi")]),
        response("无界面完"),
        response(calls=[call("execute_command", "c2", command="echo hi")]),
        response("审批完"),
        mode="plan",
    )
    policy = conversation.registry.permissions
    await fork_run(conversation, "/rev")
    assert provider.requests[1][0][-1].tool_results[0].code == "plan_blocked"
    assert not (tmp_path / "project" / "plan.txt").exists()
    policy.set_mode("default")
    await fork_run(conversation, "/rev")
    assert provider.requests[3][0][-1].tool_results[0].code == "approval_required"
    policy.interactive = True
    ui = await run_command(conversation, "/rev")
    _, prompt, command, fork = ui.calls[-1]
    asked = []
    async for event in conversation.reply(prompt, command=command, fork=fork):
        if isinstance(event, ApprovalRequest):  # The same approval flow as the main conversation.
            asked.append(event.operation.target)
            policy.respond(event.id, "deny")
    assert asked == ["echo hi"]
    assert provider.requests[5][0][-1].tool_results[0].code == "permission_denied"
    assert conversation.history[-1].text == "审批完"
    await conversation.close()


async def test_fork_in_the_tui(tmp_path, places):
    from mosscode.tui import ChatEntry, MossCodeApp
    from test_conversation import FakeProvider
    from test_mcp_manager import notes_text
    from test_tui import send

    _, _, project_skills, _ = places
    fork_skill(project_skills)
    provider = FakeProvider()
    conversation = conversation_for(tmp_path, places, provider)
    app = MossCodeApp(conversation, "test")
    async with app.run_test() as pilot:
        await send(app, pilot, "一")
        await asyncio.wait_for(app._reply_task, timeout=5)
        await send(app, pilot, "/rev x")
        await asyncio.wait_for(app._reply_task, timeout=5)
        await pilot.pause()
        assert [entry.body for entry in app.query(ChatEntry) if entry.speaker == "You"] == ["一", "/rev x"]
        assert "└ 在独立上下文中运行 Skill rev" in notes_text(app)
        assert not app.query_one("#prompt").disabled
    assert [len(messages) for messages in provider.requests] == [1, 1]  # fork_context none.
    assert [m.shown for m in conversation.history] == ["一", "partial", "/rev x", "partial"]
    await conversation.close()


def test_headless_fork_uses_its_model(monkeypatch, capsys, tmp_path):
    from mosscode import cli
    from test_cli import setup_cli

    fork_skill(tmp_path / ".mosscode" / "skills", front="model: second\n")
    monkeypatch.chdir(tmp_path)
    provider, calls = setup_cli(monkeypatch, ["-p", "/rev x", "--output-format", "json"])
    cli.main()
    out, err = capsys.readouterr()
    result = json.loads(out.strip().splitlines()[-1])
    assert result["subtype"] == "completed" and result["result"] == "partial" and result["usage"]["requests"] == 1
    assert calls["created"] == ["first", "second"] and "在独立上下文中运行 Skill rev" in err
    sent = provider.requests[0]
    assert len(sent) == 1 and sent[0].text.endswith("审查 x") and sent[0].command is None


# Built-in skills ----------------------------------------------------------------------------


def test_builtin_skills(tmp_path, isolated_home):
    from mosscode.builtin_commands import BUILTIN, help_text
    from mosscode.skills import builtin_dir

    catalog = SkillCatalog.load(tmp_path, reserved=BUILTIN.names())
    names = ("commit", "init", "review", "test")
    assert catalog.problems == () and [catalog.get(n).source for n in names] == ["builtin"] * 4
    commit, init, review, test = (catalog.get(name) for name in names)
    assert init.mode == "inline" and {"write_file", "edit_file"} <= set(init.allowed_tools)
    assert "不要安装依赖" in init.body and "不要重写整个文件" in init.body and "CLAUDE.md" in init.body
    assert commit.mode == "inline" and commit.allowed_tools == ("execute_command", "read_file")
    assert "不要用 git add -A" in commit.body and "不要推送" in commit.body
    assert (review.mode, review.fork_context) == ("fork", "none")
    assert review.allowed_tools == ("read_file", "glob", "grep", "execute_command")
    assert review.body.startswith("请审查当前仓库中尚未提交的改动。")
    assert test.allowed_tools[0] == "skill__test__detect" and [t.full_name for t in test.tools] == [
        "skill__test__detect"
    ]
    assert test.tools[0].command == ("${PYTHON}", "${SKILL_DIR}/scripts/detect.py") and test.tools[0].timeout == 30
    commands, problems = BUILTIN.with_skills(catalog)
    assert problems == () and commands.get("review").kind == "skill" and BUILTIN.get("review") is None
    assert "Skill：fork，来源内置" in help_text(commands, "review", catalog)
    # A project review/SKILL.md replaces the built-in one.
    write_skill(tmp_path / ".mosscode" / "skills", "review", body="我们自己的审查规程")
    replaced = SkillCatalog.load(tmp_path, reserved=BUILTIN.names()).get("review")
    assert replaced.source == "project" and replaced.mode == "inline" and replaced.overrides == (review.path,)
    # The wheel carries the files the catalog reads.
    import tomllib

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["mosscode.skills"]
    files = {
        path.relative_to(builtin_dir().parent).as_posix()
        for path in builtin_dir().rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert files and all(any(Path(name).match(pattern) for pattern in patterns) for name in files)


def detect_module():
    import importlib.util

    from mosscode.skills import builtin_dir

    spec = importlib.util.spec_from_file_location("detect", builtin_dir() / "test" / "scripts" / "detect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.detect


@pytest.mark.parametrize(
    "files,expected",
    [
        ({}, []),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n", ".venv/Scripts/python.exe": ""},
            [("python", ".venv/Scripts/python -m pytest")],
        ),
        ({"setup.cfg": "[tool:pytest]\n", ".venv/bin/python": ""}, [("python", ".venv/bin/python -m pytest")]),
        ({"tox.ini": "[pytest]\n"}, [("python", "python -m pytest")]),
        ({"pyproject.toml": "[project]\nname = 'x'\n"}, []),
        ({"package.json": '{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": ""}, [("node", "pnpm test")]),
        ({"package.json": '{"scripts": {"test": "jest"}}', "yarn.lock": ""}, [("node", "yarn test")]),
        ({"package.json": '{"scripts": {"test": "jest"}}'}, [("node", "npm test")]),
        ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}'}, []),
        ({"go.mod": "module x\n", "Cargo.toml": ""}, [("go", "go test ./..."), ("rust", "cargo test")]),
        ({"pom.xml": ""}, [("maven", "mvn test")]),
        ({"build.gradle.kts": "", "gradlew": ""}, [("gradle", "./gradlew test")]),
        ({"build.gradle": ""}, [("gradle", "gradle test")]),
    ],
)
def test_detect_suggests_test_commands(tmp_path, files, expected):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    found = detect_module()(tmp_path)
    assert [(item["kind"], item["command"]) for item in found] == expected
    assert all(item["evidence"] for item in found)


async def test_builtin_test_skill_runs_detect(tmp_path, isolated_home):
    from mosscode.messages import ToolCall
    from mosscode.tools import create_default_registry
    from mosscode.tools.skill import SkillTool

    (tmp_path / "tests").mkdir()
    skill = SkillCatalog.load(tmp_path).get("test")
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    registry.register(SkillTool(tmp_path, skill, skill.tools[0]))
    result = await registry.execute(ToolCall("c", "skill__test__detect", "{}"))
    assert not result.is_error, result.output
    assert json.loads(result.output) == [{"kind": "python", "command": "python -m pytest", "evidence": "tests/ 目录"}]
