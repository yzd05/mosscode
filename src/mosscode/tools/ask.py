"""ask_user: multiple-choice questions the user answers in a panel while the turn waits."""

from pydantic import Field

from ..permissions import Answer, Question
from .base import CURRENT_CALL, CURRENT_REGISTRY, Params, Tool, ToolFailure

MAX_QUESTIONS = 4
OTHER = "其他（自己输入）"
UNAVAILABLE = "当前无法询问用户，请按最合理的假设继续，并在最终回复中写明假设。"
DECLINED = "用户没有回答，按你的判断继续，或在最终回复中说明需要用户决定的事项。"


class OptionParam(Params):
    label: str = Field(min_length=1, max_length=30)
    description: str = Field(default="", max_length=200)


class QuestionParam(Params):
    question: str = Field(min_length=1, max_length=1000)
    header: str = Field(min_length=1, max_length=12)
    options: list[OptionParam] = Field(min_length=2, max_length=4)
    multi_select: bool = False


class AskParams(Params):
    questions: list[QuestionParam] = Field(min_length=1, max_length=MAX_QUESTIONS)


def describe_answer(answer: Answer | None) -> str:
    if answer is None:
        return "未回答"
    parts = ["、".join(answer.choices)] if answer.choices else []
    if answer.other:
        parts.append(f"用户自己输入：{answer.other}")
    return "；".join(parts)


def answers_text(questions: tuple[Question, ...], answers: tuple[Answer | None, ...]) -> str:
    """The tool result: every question with its answer, then what to do with them."""
    if all(answer is None for answer in answers):
        return DECLINED
    lines = ["用户的回答："]
    for number, (question, answer) in enumerate(zip(questions, answers, strict=True), 1):
        lines.append(f"{number}. {question.header}：{question.question}")
        lines.append(f"   回答：{describe_answer(answer)}")
    if any(answer is not None and answer.other for answer in answers):
        # Free text may be "wait" or "explain first"; the model should follow it rather than the options.
        lines.append("\n“用户自己输入”的内容是用户的原话，照着做。")
    if any(answer is None for answer in answers):
        lines.append("\n未回答的问题按你的判断处理，并在最终回复中说明你的选择。")
    return "\n".join(lines)


class AskUser(Tool):
    name = "ask_user"
    # It changes nothing; the user's answer is the result. No approval, plan mode included.
    read_only = True
    description = (
        "向用户提出 1–4 个选择题并等待回答，本轮在回答后继续。每题 2–4 个选项，界面另外提供“其他”"
        "让用户自己输入；multi_select 为 true 时可以多选。header 是最多 12 字的短标签，如“部署环境”。"
        "返回每题的回答；用户可能不回答。"
    )
    prompt_summary = "向用户提选择题"
    prompt_guidelines = (
        "需求有歧义、存在需要用户决定的取舍，或缺少无法自己查到的信息时，用 ask_user 提问，不要猜；"
        "能从代码、文档或命令输出中查到的，先自己查。",
        "选项要具体、互斥，推荐的选项放第一个并在 label 末尾注明“（推荐）”；不要添加“其他”选项，界面会自动提供。",
        "不要用 ask_user 征求是否继续或确认计划；计划模式中用 exit_plan_mode 提交计划。",
    )
    params_model = AskParams

    async def execute(self, params: AskParams):
        registry = CURRENT_REGISTRY.get()
        policy = getattr(registry, "permissions", None)
        for item in params.questions:
            labels = [option.label.strip() for option in item.options]
            if len(set(labels)) != len(labels):
                raise ToolFailure("invalid_question", f"问题“{item.header}”中有重复的选项。")
        questions = tuple(
            Question(
                " ".join(item.question.split()),
                item.header.strip(),
                tuple((option.label.strip(), " ".join(option.description.split())) for option in item.options),
                item.multi_select,
            )
            for item in params.questions
        )
        call = CURRENT_CALL.get()
        answers = None if policy is None else await policy.ask_questions(call.id if call else "", questions)
        if answers is None:
            raise ToolFailure("ask_unavailable", UNAVAILABLE)
        answers = tuple(answers[: len(questions)]) + (None,) * max(0, len(questions) - len(answers))
        return answers_text(questions, answers), {
            "questions": [
                {"header": q.header, "question": q.question, "multi_select": q.multi_select} for q in questions
            ],
            "answers": [None if a is None else {"choices": list(a.choices), "other": a.other} for a in answers],
        }
