"""Bridge MossCode ApprovalRequest and QuestionRequest to ACP session/request_permission."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from acp import RequestError
from acp.schema import (
    AllowedOutcome,
    ContentToolCallContent,
    PermissionOption,
    PermissionOptionKind,
    TextContentBlock,
    ToolCallUpdate,
)

from ..permissions import Answer, ApprovalRequest, PermissionPolicy, Question, QuestionRequest
from .events import AcpEventMapper, text_chunk

if TYPE_CHECKING:
    from acp import Client

CHOICE_KINDS: dict[str, PermissionOptionKind] = {
    "once": "allow_once",
    "execute": "allow_once",
    "merge": "allow_once",
    "accept_edits": "allow_always",
    "session": "allow_always",
    "prefix": "allow_always",
    "permanent": "allow_always",
    "deny": "reject_once",
}


def _choice_label(choice: str, request: ApprovalRequest) -> str:
    op = request.operation
    is_plan = op.tool == "exit_plan_mode"
    if choice == "once":
        return "允许本次"
    if choice == "execute":
        return "批准计划并开始执行"
    if choice == "merge":
        return "批准合并成员分支"
    if choice == "accept_edits":
        return "批准计划并自动接受后续编辑 (acceptEdits)" if is_plan else "自动接受后续编辑 (acceptEdits)"
    if choice == "session":
        hint = op.prefix or op.server or op.skill or op.tool
        return f"本会话允许（{hint}）"
    if choice == "prefix":
        return f"始终允许 `{op.prefix} *`" if op.prefix else "始终允许此类命令"
    if choice == "permanent":
        return "永久允许（写入项目本地规则）"
    if choice == "deny":
        return "拒绝（继续规划）" if is_plan else "拒绝本次"
    return choice


def build_approval_options(request: ApprovalRequest) -> list[PermissionOption]:
    """Translate ApprovalRequest.choices into ACP PermissionOption items."""
    options: list[PermissionOption] = []
    for choice in request.choices:
        kind = CHOICE_KINDS.get(choice, "allow_once")
        options.append(
            PermissionOption(
                option_id=choice,
                name=_choice_label(choice, request),
                kind=kind,
            )
        )
    if not any(opt.option_id == "deny" for opt in options):
        options.append(PermissionOption(option_id="deny", name=_choice_label("deny", request), kind="reject_once"))
    return options


def build_question_options(question: Question) -> list[PermissionOption]:
    """Translate a single-select Question into ACP PermissionOption items."""
    options: list[PermissionOption] = []
    for idx, (label, description) in enumerate(question.options):
        name = f"{label} — {description}" if description else label
        options.append(PermissionOption(option_id=f"opt:{idx}", name=name, kind="allow_once"))
    options.append(
        PermissionOption(
            option_id="other",
            name="跳过 / 我在聊天框补充说明",
            kind="reject_once",
        )
    )
    return options


def format_multiselect_fallback(request: QuestionRequest) -> str:
    """Format a readable markdown prompt when ask_user contains multi_select questions."""
    lines = ["\n> **需要您在聊天框直接回复以下问题（含多选题）：**"]
    for idx, q in enumerate(request.questions, 1):
        tag = "（可多选）" if q.multi_select else "（单选）"
        header = f"[{q.header}] " if q.header else ""
        lines.append(f"> {idx}. {header}{q.question} {tag}")
        for opt_idx, (label, desc) in enumerate(q.options, 1):
            detail = f" — {desc}" if desc else ""
            lines.append(f">    - `{opt_idx}`: {label}{detail}")
    lines.append("")
    return "\n".join(lines)


async def handle_approval_request(
    client: Client,
    session_id: str,
    policy: PermissionPolicy,
    mapper: AcpEventMapper,
    request: ApprovalRequest,
) -> str:
    """Request permission from the ACP client and deliver the decision to PermissionPolicy."""
    tool_call = mapper.approval_tool_call_update(request)
    options = build_approval_options(request)
    fallback = "deny" if "deny" in request.choices else request.choices[-1]
    decision = fallback
    try:
        response = await client.request_permission(
            session_id=session_id,
            tool_call=tool_call,
            options=options,
        )
        outcome = response.outcome
        if isinstance(outcome, AllowedOutcome) and outcome.option_id in request.choices:
            decision = outcome.option_id
    except RequestError:
        decision = fallback
    except asyncio.CancelledError:
        decision = fallback
        raise
    finally:
        if request.id in policy.pending:
            policy.respond(request.id, decision)
    return decision


async def handle_question_request(
    client: Client,
    session_id: str,
    policy: PermissionPolicy,
    request: QuestionRequest,
) -> tuple[Answer | None, ...]:
    """Present ask_user questions over ACP session/request_permission or fallback for multi-select."""
    declined: tuple[Answer | None, ...] = (None,) * len(request.questions)
    if any(q.multi_select for q in request.questions):
        try:
            await client.session_update(
                session_id=session_id,
                update=text_chunk(format_multiselect_fallback(request)),
            )
        finally:
            if request.id in policy.pending:
                policy.answer(request.id, declined)
        return declined

    answers: list[Answer | None] = []
    try:
        for idx, q in enumerate(request.questions):
            prefix = f"[{request.source}] " if request.source else ""
            header = f"[{q.header}] " if q.header else ""
            title = f"{prefix}{header}{q.question}"
            tool_call = ToolCallUpdate(
                tool_call_id=f"question-{request.id}-{idx}",
                name="ask_user",
                title=title,
                kind="other",
                status="pending",
                content=[ContentToolCallContent(content=TextContentBlock(text=q.question))],
                raw_input={"question": q.question, "header": q.header},
            )
            options = build_question_options(q)
            response = await client.request_permission(
                session_id=session_id,
                tool_call=tool_call,
                options=options,
            )
            outcome = response.outcome
            if isinstance(outcome, AllowedOutcome) and outcome.option_id.startswith("opt:"):
                try:
                    opt_index = int(outcome.option_id.split(":", 1)[1])
                    label = q.options[opt_index][0]
                    answers.append(Answer(choices=(label,)))
                    continue
                except (ValueError, IndexError):
                    pass
            # User chose "other" or cancelled: leave this and remaining questions unanswered.
            answers.extend([None] * (len(request.questions) - len(answers)))
            break
    except RequestError:
        answers = list(declined)
    except asyncio.CancelledError:
        if request.id in policy.pending:
            policy.answer(request.id, declined)
        raise

    final_answers = tuple(answers) if len(answers) == len(request.questions) else declined
    if request.id in policy.pending:
        policy.answer(request.id, final_answers)
    return final_answers
