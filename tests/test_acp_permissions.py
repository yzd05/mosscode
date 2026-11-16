import asyncio

from acp import RequestError
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from mosscode.acp.events import AcpEventMapper
from mosscode.acp.permissions import (
    build_approval_options,
    build_question_options,
    handle_approval_request,
    handle_question_request,
)
from mosscode.permissions import (
    COMMAND_CHOICES,
    EDIT_CHOICES,
    MERGE_CHOICES,
    PLAN_CHOICES,
    RISKY_CHOICES,
    ApprovalRequest,
    Operation,
    PermissionPolicy,
    Question,
    QuestionRequest,
)


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.updates = []

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.requests.append((session_id, tool_call, options))
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return RequestPermissionResponse(outcome=outcome)

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


def test_build_approval_options_covers_all_choice_variants():
    req_edit = ApprovalRequest(
        "1",
        "c1",
        Operation("edit_file", "a.py", preview="diff"),
        "reason",
        EDIT_CHOICES,
    )
    assert [(o.option_id, o.kind) for o in build_approval_options(req_edit)] == [
        ("once", "allow_once"),
        ("session", "allow_always"),
        ("accept_edits", "allow_always"),
        ("permanent", "allow_always"),
        ("deny", "reject_once"),
    ]

    req_cmd = ApprovalRequest(
        "2",
        "c2",
        Operation("execute_command", "pytest -q", prefix="pytest"),
        "reason",
        COMMAND_CHOICES,
    )
    opts_cmd = build_approval_options(req_cmd)
    assert any(o.option_id == "prefix" and "pytest *" in o.name for o in opts_cmd)

    req_plan = ApprovalRequest(
        "3",
        "c3",
        Operation("exit_plan_mode", "提交计划", preview="plan"),
        "review",
        PLAN_CHOICES,
    )
    assert [(o.option_id, o.kind) for o in build_approval_options(req_plan)] == [
        ("execute", "allow_once"),
        ("accept_edits", "allow_always"),
        ("deny", "reject_once"),
    ]

    req_merge = ApprovalRequest(
        "4",
        "c4",
        Operation("team_merge", "worker"),
        "merge",
        MERGE_CHOICES,
    )
    assert [(o.option_id, o.kind) for o in build_approval_options(req_merge)] == [
        ("merge", "allow_once"),
        ("deny", "reject_once"),
    ]

    req_risk = ApprovalRequest(
        "5",
        "c5",
        Operation("execute_command", "rm -rf build", risk="递归删除"),
        "risky",
        RISKY_CHOICES,
    )
    assert [o.option_id for o in build_approval_options(req_risk)] == ["once", "deny"]


async def test_handle_approval_request_delivers_decision_and_fallbacks(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    mapper = AcpEventMapper(tmp_path)

    req = ApprovalRequest("id-1", "call-1", Operation("edit_file", "a.py"), "ask", EDIT_CHOICES)
    fut = policy.hold(req)
    client = _FakeClient([AllowedOutcome(option_id="accept_edits")])
    decision = await handle_approval_request(client, "s1", policy, mapper, req)
    assert decision == "accept_edits"
    assert fut.done() and fut.result() == "accept_edits"

    # Cancelled / DeniedOutcome falls back to deny.
    req2 = ApprovalRequest("id-2", "call-2", Operation("execute_command", "ls"), "ask", COMMAND_CHOICES)
    fut2 = policy.hold(req2)
    client2 = _FakeClient([DeniedOutcome()])
    decision2 = await handle_approval_request(client2, "s1", policy, mapper, req2)
    assert decision2 == "deny"
    assert fut2.result() == "deny"

    # RPC RequestError falls back to deny safely.
    req3 = ApprovalRequest("id-3", "call-3", Operation("execute_command", "ls"), "ask", COMMAND_CHOICES)
    fut3 = policy.hold(req3)
    client3 = _FakeClient([RequestError.internal_error()])
    decision3 = await handle_approval_request(client3, "s1", policy, mapper, req3)
    assert decision3 == "deny"
    assert fut3.result() == "deny"


async def test_handle_question_request_single_skip_and_multiselect(tmp_path):
    policy = PermissionPolicy(tmp_path, load=False)
    q1 = Question("Framework?", "fw", (("FastAPI", "Async"), ("Django", "Full")))
    q2 = Question("Database?", "db", (("Postgres", ""), ("SQLite", "")))

    # Build options check
    q_opts = build_question_options(q1)
    assert [o.option_id for o in q_opts] == ["opt:0", "opt:1", "other"]

    # Scenario 1: answer Q1 then choose "other" (skip) on Q2
    req = QuestionRequest("q-req-1", "c1", (q1, q2))
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    policy.pending[req.id] = fut
    policy._choices[req.id] = ()

    client = _FakeClient(
        [
            AllowedOutcome(option_id="opt:0"),
            AllowedOutcome(option_id="other"),
        ]
    )
    answers = await handle_question_request(client, "s1", policy, req)
    assert len(answers) == 2
    assert answers[0] is not None and answers[0].choices == ("FastAPI",)
    assert answers[1] is None
    assert fut.result() == answers

    # Scenario 2: multi-select emits formatted fallback prompt and resolves cleanly with (None,)
    qm = Question("Pick features", "feat", (("Auth", ""), ("Metrics", "")), multi_select=True)
    req_multi = QuestionRequest("q-req-2", "c2", (qm,))
    fut_multi = loop.create_future()
    policy.pending[req_multi.id] = fut_multi
    policy._choices[req_multi.id] = ()

    client_multi = _FakeClient([])
    ans_multi = await handle_question_request(client_multi, "s1", policy, req_multi)
    assert ans_multi == (None,)
    assert fut_multi.result() == (None,)
    assert len(client_multi.requests) == 0
    assert len(client_multi.updates) == 1
    assert "（可多选）" in client_multi.updates[0][1].content.text
