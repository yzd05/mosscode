"""ask_user: questions through the approval queue, the answers as the tool result, and who may ask."""

import asyncio

import pytest

from mosscode.agents.runtime import GLOBAL_BAN
from mosscode.conversation import Conversation
from mosscode.permissions import Answer, ApprovalRequest, Operation, PermissionPolicy, Question, QuestionRequest
from mosscode.tools import create_default_registry
from mosscode.tools.ask import DECLINED, UNAVAILABLE, answers_text
from test_agent import SequenceProvider, call, response

COLOR = {
    "question": "用哪种配色？",
    "header": "配色",
    "options": [{"label": "深色（推荐）", "description": "适合终端"}, {"label": "浅色"}],
}
PARTS = {
    "question": "要哪些部分？",
    "header": "范围",
    "options": [{"label": "接口"}, {"label": "界面"}, {"label": "文档"}],
    "multi_select": True,
}


def registry(tmp_path, *, interactive=True, mode="default"):
    reg = create_default_registry(tmp_path, permission_mode=mode, load_permissions=False, ask=True)
    reg.permissions.interactive = interactive
    return reg


async def ask(reg, *questions, answers=None):
    """Run the tool; answer the request it puts on the queue (None declines everything)."""
    job = asyncio.create_task(reg.execute(call("ask_user", "q", questions=list(questions))))
    request = await asyncio.wait_for(reg.permissions.requests.get(), 3)
    assert isinstance(request, QuestionRequest) and request.call_id == "q"
    reg.permissions.answer(request.id, answers if answers is not None else (None,) * len(request.questions))
    return request, await job


async def test_the_answers_come_back_question_by_question(tmp_path):
    reg = registry(tmp_path)
    request, result = await ask(reg, COLOR, PARTS, answers=(Answer(("深色（推荐）",)), Answer(("接口", "文档"))))
    assert [q.header for q in request.questions] == ["配色", "范围"]
    assert request.questions[0].options == (("深色（推荐）", "适合终端"), ("浅色", ""))
    assert request.questions[1].multi_select
    assert not result.is_error
    assert result.output.splitlines() == [
        "用户的回答：",
        "1. 配色：用哪种配色？",
        "   回答：深色（推荐）",
        "2. 范围：要哪些部分？",
        "   回答：接口、文档",
    ]
    assert result.details["answers"] == [
        {"choices": ["深色（推荐）"], "other": ""},
        {"choices": ["接口", "文档"], "other": ""},
    ]
    assert not reg.permissions.pending


async def test_typed_answers_are_passed_on_as_the_users_words(tmp_path):
    reg = registry(tmp_path)
    _, result = await ask(
        reg, COLOR, PARTS, answers=(Answer(other="先别改，解释一下区别"), Answer(("界面",), "还有测试"))
    )
    assert "回答：用户自己输入：先别改，解释一下区别" in result.output
    assert "回答：界面；用户自己输入：还有测试" in result.output
    assert "是用户的原话，照着做" in result.output


async def test_declining_leaves_the_decision_to_the_model(tmp_path):
    reg = registry(tmp_path)
    _, result = await ask(reg, COLOR, PARTS)
    assert not result.is_error and result.output == DECLINED
    _, result = await ask(reg, COLOR, PARTS, answers=(Answer(("浅色",)), None))
    assert "回答：浅色" in result.output and "回答：未回答" in result.output
    assert "未回答的问题按你的判断处理" in result.output


async def test_without_anyone_to_ask_the_model_is_told_to_assume(tmp_path):
    reg = registry(tmp_path, interactive=False)
    result = await reg.execute(call("ask_user", "q", questions=[COLOR]))
    assert result.is_error and result.code == "ask_unavailable" and result.output == UNAVAILABLE
    assert reg.permissions.requests.empty()


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [COLOR] * 5,
        [{**COLOR, "options": [{"label": "唯一"}]}],
        [{**COLOR, "options": [{"label": str(n)} for n in range(5)]}],
        [{**COLOR, "header": "超过十二个字的一个很长的标签"}],
        [{**COLOR, "options": [{"label": "长" * 31}, {"label": "短"}]}],
    ],
)
async def test_malformed_questions_are_rejected_without_asking(tmp_path, questions):
    reg = registry(tmp_path)
    result = await reg.execute(call("ask_user", "q", questions=questions))
    assert result.is_error and reg.permissions.requests.empty()


async def test_repeated_options_are_rejected(tmp_path):
    reg = registry(tmp_path)
    result = await reg.execute(
        call("ask_user", "q", questions=[{**COLOR, "options": [{"label": "甲"}, {"label": "甲 "}]}])
    )
    assert result.is_error and result.code == "invalid_question" and reg.permissions.requests.empty()


async def test_questions_wait_behind_an_approval_and_take_no_approval_choice(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    policy.interactive = True
    approval = asyncio.create_task(policy.confirm("c", Operation("team_merge", "x"), "确认", ("once", "deny")))
    first = await asyncio.wait_for(policy.requests.get(), 3)
    question = asyncio.create_task(policy.ask_questions("q", (Question("问？", "标签", (("甲", ""), ("乙", ""))),)))
    await asyncio.sleep(0.05)
    assert isinstance(first, ApprovalRequest) and policy.requests.empty()  # One panel at a time.
    with pytest.raises(ValueError):
        policy.answer(first.id, (None,))
    policy.respond(first.id, "once")
    assert await approval == "once"
    second = await asyncio.wait_for(policy.requests.get(), 3)
    assert isinstance(second, QuestionRequest)
    with pytest.raises(ValueError):
        policy.respond(second.id, "once")
    policy.answer(second.id, (Answer(("乙",)),))
    assert await question == (Answer(("乙",)),)


async def test_a_sub_agent_sent_to_the_background_stops_waiting(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    view = policy.view("default", interactive=True, label="子 Agent")
    job = asyncio.create_task(view.ask_questions("q", (Question("问？", "标签", (("甲", ""), ("乙", ""))),)))
    request = await asyncio.wait_for(view.requests.get(), 3)
    assert request.source == "子 Agent"
    view.to_background()
    assert await job is None


def test_only_the_interactive_main_conversation_offers_it(tmp_path):
    assert create_default_registry(tmp_path, load_permissions=False).get("ask_user") is None
    reg = registry(tmp_path)
    assert "ask_user" in GLOBAL_BAN  # Sub-agents and team members never get it.
    view = reg.for_agent(
        agent="subagent", agent_type="general", permissions=reg.permissions, keep=lambda n: n not in GLOBAL_BAN
    )
    assert view.get("ask_user") is None
    assert "ask_user" in {d.name for d in reg.definitions(plan=True)}


async def test_the_model_gets_the_answer_within_the_same_turn(tmp_path):
    provider = SequenceProvider(
        response("先问一下", [call("ask_user", "q", questions=[COLOR])]),
        response("按深色做"),
    )
    reg = registry(tmp_path, mode="plan")
    conv = Conversation(provider, registry=reg)
    asked = []
    async for event in conv.reply("改配色"):
        if isinstance(event, QuestionRequest):
            asked.append(event.questions[0].header)
            reg.permissions.answer(event.id, (Answer(("深色（推荐）",)),))
    assert asked == ["配色"]
    result = conv.history[3].tool_results[0]  # After the message and the plan-mode reminder that follows it.
    assert not result.is_error and "回答：深色（推荐）" in result.output
    assert conv.history[-1].text == "按深色做"
    await conv.close()


def test_answers_text_matches_the_questions():
    questions = (Question("问？", "标签", (("甲", ""), ("乙", ""))),)
    assert answers_text(questions, (Answer(("甲",)),)).endswith("回答：甲")


def ask_app(tmp_path, *questions):
    from mosscode.tui import MossCodeApp

    provider = SequenceProvider(
        response("问一下", [call("ask_user", "q", questions=list(questions))]), response("好的")
    )
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False, ask=True)
    return MossCodeApp(Conversation(provider, registry=reg), "Demo")


async def start(app, pilot):
    from textual.widgets import Static

    from mosscode.tui import PromptArea

    app.query_one("#prompt", PromptArea).value = "改配色"
    await pilot.press("enter")
    panel = app.query_one("#question-panel")
    for _ in range(100):
        await pilot.pause()
        if panel.display:
            break
    assert panel.display
    return str(app.query_one("#question-detail", Static).render())


def tool_result(app):
    return app.conversation.history[2].tool_results[0]


async def test_the_panel_takes_a_choice_and_ticked_options(tmp_path):
    from textual.widgets import OptionList

    from mosscode.tui import ToolEntry

    app = ask_app(tmp_path, COLOR, PARTS)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        detail = await start(app, pilot)
        assert detail.splitlines() == ["配色 · 第 1/2 题", "用哪种配色？"]
        options = app.query_one("#question-options", OptionList)
        prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert prompts == ["1. 深色（推荐） — 适合终端", "2. 浅色", "3. 其他（自己输入）"]
        assert app.focused is options and "等待回答" in str(app.query_one("#activity").render())
        assert "等待用户回答" in str(app.query_one(ToolEntry).render())
        await pilot.press("down", "enter")
        await pilot.pause()
        assert "范围 · 第 2/2 题 · 可多选" in str(app.query_one("#question-detail").render())
        await pilot.press("space", "down", "down", "space", "up", "space", "space")
        await pilot.pause()
        prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert prompts == ["[x] 接口", "[ ] 界面", "[x] 文档", "    其他（自己输入）"]
        await pilot.press("enter")
        await app._reply_task
        await pilot.pause()
        assert not app.query_one("#question-panel").display
        result = tool_result(app)
        assert "回答：浅色" in result.output and "回答：接口、文档" in result.output
        row = str(app.query_one(ToolEntry).render())
        assert "Ask(配色、范围)" in row and "配色：浅色" in row and "范围：接口、文档" in row
        assert app.conversation.history[-1].text == "好的"


async def test_other_takes_the_typed_answer_and_esc_leaves_the_rest(tmp_path):
    from mosscode.tui import PromptArea

    app = ask_app(tmp_path, COLOR, PARTS)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await start(app, pilot)
        field = app.query_one("#prompt", PromptArea)
        field.value = "草稿"
        row = app.query_one("#input-row")
        assert not row.display  # The question stands in the prompt's place while it is chosen from.
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        # "其他" is typed in the prompt, which comes back above the question.
        assert row.display and row.region.bottom <= app.query_one("#question-panel").region.y
        assert app.focused is field and field.value == ""
        assert "Esc 返回选项" in str(app.query_one("#question-help").render())
        await pilot.press("escape")  # Back to the options, the draft back in the prompt.
        await pilot.pause()
        assert field.value == "草稿" and app.query_one("#question-panel").display and not row.display
        await pilot.press("enter")  # "其他" is still highlighted.
        await pilot.pause()
        field.value = "先解释两种的区别"
        await pilot.press("enter")
        await pilot.pause()
        assert field.value == "草稿"
        assert "范围" in str(app.query_one("#question-detail").render())
        await pilot.press("escape")
        await app._reply_task
        await pilot.pause()
        output = tool_result(app).output
        assert "回答：用户自己输入：先解释两种的区别" in output and "回答：未回答" in output
        assert "是用户的原话，照着做" in output
        assert len(app.conversation.history) == 4  # The typed answer was not sent as a message.


async def test_esc_on_the_first_question_declines_and_ctrl_c_cancels_the_turn(tmp_path):
    app = ask_app(tmp_path, COLOR)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await start(app, pilot)
        await pilot.press("escape")
        await app._reply_task
        await pilot.pause()
        assert tool_result(app).output == DECLINED and not app.query_one("#question-panel").display

    app = ask_app(tmp_path, COLOR)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await start(app, pilot)
        await pilot.press("ctrl+c")
        await app._reply_task
        await pilot.pause()
        assert not app.query_one("#question-panel").display
        assert not app.conversation.registry.permissions.pending


async def test_the_answers_are_recorded_and_shown_after_resume(tmp_path):
    from mosscode.tui import resumed_rows
    from test_sessions import drain, make

    def shown(conv):
        return "\n".join(
            str(part) for row in resumed_rows(conv.history) if hasattr(row, "renderables") for part in row.renderables
        )

    provider = SequenceProvider(response("问一下", [call("ask_user", "q", questions=[COLOR])]), response("好的"))
    (tmp_path / "project").mkdir()
    conv = make(tmp_path, provider, registry=registry(tmp_path / "project", mode="bypassPermissions"))
    async for event in conv.reply("改配色"):
        if isinstance(event, QuestionRequest):
            conv.registry.permissions.answer(event.id, (Answer(("浅色",), "再暗一点"),))
    session_id = conv.session_id
    await conv.close()

    other = make(tmp_path, SequenceProvider())
    await drain(other.resume(session_id))
    assert "● Ask(配色)\n    └ 配色：浅色；“再暗一点”" in shown(other)
    await other.close()
