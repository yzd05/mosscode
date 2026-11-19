"""KV-cache prefix invariants, context source attribution, and strict chat-template normalization."""

import io
import json
from datetime import date as real_date
from types import SimpleNamespace

import pytest
from PIL import Image

import mosscode.agent as agent_module
import mosscode.prompts as prompts_module
from mosscode.compaction import COLLAPSE
from mosscode.config import ProviderConfig
from mosscode.context import ContextMeter, ContextSnapshot, estimate_message
from mosscode.conversation import Conversation
from mosscode.images import image_ref, load_image
from mosscode.messages import REMINDER, Collapse, ContentBlock, Message, ToolCall, ToolResult, Usage
from mosscode.prompts import PromptRequest, Supplement, environment, mode_reminder
from mosscode.providers import ChatCompletionsProvider, _coalesce_adjacent_users
from mosscode.skills import SkillDef
from mosscode.tools import create_default_registry
from mosscode.tools.base import Params, Tool
from test_agent import SequenceProvider, response


class CapturedPayload(Exception):
    pass


def text_msg(role: str, value: str, *, synthetic: str | None = None) -> Message:
    return Message(role, (ContentBlock("text", value),), synthetic=synthetic)


def make_image(color: str = "blue", alt: str = "[图片 #1]"):
    buf = io.BytesIO()
    Image.new("RGB", (4, 2), color).save(buf, "PNG")
    return image_ref(load_image(buf.getvalue()), alt)


async def capture_chat_payload(
    messages: list[Message],
    system: str | PromptRequest = "系统指令",
    *,
    accepts_images: bool = True,
) -> dict:
    seen: list[dict] = []

    async def create(**kwargs):
        seen.append(kwargs)
        raise CapturedPayload()

    endpoint = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=endpoint), base_url="")
    config = ProviderConfig("vllm", "openai-chat-completions", "qwen-coder", max_retries=0, images=accepts_images)
    provider = ChatCompletionsProvider(config, client)
    with pytest.raises(CapturedPayload):
        async for _ in provider.stream(messages, system):
            pass
    return seen[0]


def test_environment_today_override_and_non_plan_mode_reminder(tmp_path):
    env = environment(tmp_path, tools_enabled=True, permission_mode="default", today="2026-11-17")
    facts = json.loads(env.text)
    assert facts["date"] == "2026-11-17"
    assert facts["permission_mode"] == "default"

    assert mode_reminder("default", "default") is None
    assert mode_reminder("default", "") is None
    assert mode_reminder("acceptEdits", "default") == Supplement(
        "system-reminder", "当前权限档位已切换为 acceptEdits。"
    )
    assert mode_reminder("bypassPermissions", "acceptEdits") == Supplement(
        "system-reminder", "当前权限档位已切换为 bypassPermissions。"
    )
    assert "exit_plan_mode" in mode_reminder("plan", "acceptEdits").text
    assert "已退出计划模式" in mode_reminder("acceptEdits", "plan").text


async def test_crossing_midnight_preserves_environment_prefix_and_appends_date_reminder_once(tmp_path, monkeypatch):
    current = {"day": real_date(2026, 11, 17)}

    class FakeDate:
        @staticmethod
        def today():
            return current["day"]

    monkeypatch.setattr(prompts_module, "date", FakeDate)
    monkeypatch.setattr(agent_module, "date", FakeDate)

    provider = SequenceProvider(
        response("第一天回复"),
        response("第二天首轮"),
        response("第二天次轮"),
        response("第三天首轮"),
        response("清空后首轮"),
    )
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False)
    conv = Conversation(provider, registry=reg)

    assert conv._agent.session_date == "2026-11-17"
    await conv.ask("第一天任务")

    current["day"] = real_date(2026, 11, 18)
    await conv.ask("跨午夜继续")
    await conv.ask("同一天再次提问")

    current["day"] = real_date(2026, 11, 19)
    await conv.ask("再跨一天")

    envs = [req[1].supplements[0].render() for req in provider.requests]
    assert envs[0] == envs[1] == envs[2] == envs[3]
    assert json.loads(provider.requests[3][1].supplements[0].text)["date"] == "2026-11-17"

    sent_msgs = [req[0] for req in provider.requests]
    # Append-only prefix invariant across all four turns.
    for earlier, later in zip(sent_msgs[:3], sent_msgs[1:4]):
        assert later[: len(earlier)] == earlier

    reminders_per_turn = [[m.text for m in msgs if m.synthetic == REMINDER] for msgs in sent_msgs]
    assert reminders_per_turn[0] == []
    assert len(reminders_per_turn[1]) == 1
    assert "当前日期已变为 2026-11-18。" in reminders_per_turn[1][0]
    assert sent_msgs[1][-1].synthetic == REMINDER
    # Not repeated on the second turn of Day 2.
    assert reminders_per_turn[2] == reminders_per_turn[1]
    assert sent_msgs[2][-1].text == "同一天再次提问"
    # Updated once again when crossing into Day 3.
    assert len(reminders_per_turn[3]) == 2
    assert "当前日期已变为 2026-11-19。" in reminders_per_turn[3][1]

    # /clear starts a fresh session locked to the new wall-clock date.
    await conv.clear()
    assert conv._agent.session_date == "2026-11-19"
    await conv.ask("新会话")
    assert json.loads(provider.requests[4][1].supplements[0].text)["date"] == "2026-11-19"
    assert [m for m in provider.requests[4][0] if m.synthetic == REMINDER] == []


async def test_switching_non_plan_permission_modes_preserves_prefix_and_notifies_at_tail(tmp_path):
    provider = SequenceProvider(
        response("默认档位"),
        response("接受编辑档位"),
        response("免审档位"),
        response("再次免审"),
    )
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False)
    conv = Conversation(provider, registry=reg)

    await conv.ask("第一轮")
    conv.set_permission_mode("acceptEdits")
    await conv.ask("第二轮")
    conv.set_permission_mode("bypassPermissions")
    await conv.ask("第三轮")
    await conv.ask("第四轮")

    prompts = [req[1] for req in provider.requests]
    tools = [req[2] for req in provider.requests]
    assert prompts[0] == prompts[1] == prompts[2] == prompts[3]
    assert tools[0] == tools[1] == tools[2] == tools[3]
    assert json.loads(prompts[2].supplements[0].text)["permission_mode"] == "default"

    sent_msgs = [req[0] for req in provider.requests]
    for earlier, later in zip(sent_msgs, sent_msgs[1:]):
        assert later[: len(earlier)] == earlier

    reminders = [[m.text for m in msgs if m.synthetic == REMINDER] for msgs in sent_msgs]
    assert reminders[0] == []
    assert len(reminders[1]) == 1 and "当前权限档位已切换为 acceptEdits。" in reminders[1][0]
    assert len(reminders[2]) == 2 and "当前权限档位已切换为 bypassPermissions。" in reminders[2][1]
    assert reminders[3] == reminders[2]

    # Entering and exiting plan mode still updates tool definitions and announces plan rules cleanly.
    provider.responses.extend([response("规划完毕"), response("恢复执行")])
    conv.set_mode("plan")
    await conv.ask("第五轮计划")
    assert "exit_plan_mode" in {t.name for t in provider.requests[4][2]}
    assert json.loads(provider.requests[4][1].supplements[0].text)["permission_mode"] == "plan"
    assert "已进入计划模式" in provider.requests[4][0][-1].text

    conv.set_mode("normal")
    await conv.ask("第六轮执行")
    assert "write_file" in {t.name for t in provider.requests[5][2]}
    assert json.loads(provider.requests[5][1].supplements[0].text)["permission_mode"] == "bypassPermissions"
    assert "已退出计划模式" in provider.requests[5][0][-1].text


async def test_pre_turn_one_mode_switches_sync_environment_without_spurious_reminder(tmp_path):
    provider = SequenceProvider(response("首轮回复"), response("第二轮回复"))
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv = Conversation(provider, registry=reg)

    # Toggle into plan mode and then switch to default BEFORE any message is ever sent.
    conv.set_mode("plan")
    conv.set_permission_mode("default")
    assert json.loads(conv._environment().text)["permission_mode"] == "default"

    await conv.ask("首轮明确任务")
    first_msgs, first_prompt, _ = provider.requests[0][:3]
    assert json.loads(first_prompt.supplements[0].text)["permission_mode"] == "default"
    assert [m for m in first_msgs if m.synthetic == REMINDER] == []
    assert first_msgs[-1].text == "首轮明确任务"

    # Once history is non-empty, switching default -> acceptEdits freezes <environment> and appends tail reminder.
    conv.set_permission_mode("acceptEdits")
    await conv.ask("次轮继续")
    second_msgs, second_prompt, _ = provider.requests[1][:3]
    assert second_prompt == first_prompt
    assert "当前权限档位已切换为 acceptEdits。" in second_msgs[-1].text


class ExtraParams(Params):
    value: str = "x"


class ExtraTool(Tool):
    name = "extra_dynamic_tool"
    description = "Dynamic test tool"
    params_model = ExtraParams
    read_only = True

    async def execute(self, params):
        return params.value, {}


async def test_context_meter_and_agent_snapshot_source_transitions(tmp_path):
    meter = ContextMeter()
    h1 = [text_msg("user", "abcd" * 10), text_msg("assistant", "efgh" * 5)]
    snap0 = meter.measure(h1, fixed=lambda: "abcd" * 20, extra_tokens=10)
    assert snap0 == ContextSnapshot(
        tokens=20 + sum(estimate_message(m) for m in h1) + 10,
        source="estimated",
        anchored_tokens=None,
        delta_tokens=0,
    )
    assert meter.estimate(h1, fixed=lambda: "abcd" * 20, extra_tokens=10) == snap0.tokens

    meter.record(Usage(input_tokens=4000, output_tokens=150), len(h1), extra_tokens=10)
    snap_reported = meter.measure(h1, extra_tokens=10)
    assert snap_reported == ContextSnapshot(tokens=4150, source="reported", anchored_tokens=4150, delta_tokens=0)

    h2 = [*h1, text_msg("user", "abcd" * 25)]
    added = estimate_message(h2[-1])
    snap_mixed_msg = meter.measure(h2, extra_tokens=10)
    assert snap_mixed_msg == ContextSnapshot(
        tokens=4150 + added, source="mixed", anchored_tokens=4150, delta_tokens=added
    )

    # Even if a message addition and extra shrinkage cancel out to delta == 0, uncounted history stays "mixed".
    snap_cancelled_delta = meter.measure(h2, extra_tokens=10 - added)
    assert snap_cancelled_delta.source == "mixed" and snap_cancelled_delta.delta_tokens == 0

    meter.invalidate()
    assert meter.measure(h2, fixed=lambda: "abcd" * 4).source == "estimated"

    # Full AgentLoop lifecycle: estimated -> reported -> mixed -> estimated (on tool-set change).
    provider = SequenceProvider(response("收到", usage=Usage(input_tokens=8000, output_tokens=200)))
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False)
    conv = Conversation(provider, registry=reg)
    loop = conv._agent

    initial = loop.context_snapshot()
    assert initial.source == "estimated" and initial.anchored_tokens is None and initial.tokens == loop.estimate()

    await conv.ask("检查上下文快照")
    env_rendered = provider.requests[0][1].supplements[0].render()
    reported = loop.context_snapshot()
    assert reported == ContextSnapshot(tokens=8200, source="reported", anchored_tokens=8200, delta_tokens=0)
    assert loop.context_snapshot(env_text=env_rendered) == reported

    # Adding pending/extra messages or activating a skill transitions to "mixed".
    extra_user = text_msg("user", "补充问题")
    mixed_from_msg = loop.context_snapshot(extra=(extra_user,))
    assert mixed_from_msg.source == "mixed"
    assert mixed_from_msg.anchored_tokens == 8200
    assert mixed_from_msg.delta_tokens == estimate_message(extra_user)
    assert mixed_from_msg.tokens == 8200 + estimate_message(extra_user)

    skill = SkillDef("demo", "示例规程", "执行步骤", tmp_path / "SKILL.md", tmp_path)
    loop.active_skills.activate(skill, "", "Skill demo\n\n步骤正文")
    mixed_from_skill = loop.context_snapshot()
    assert mixed_from_skill.source == "mixed" and mixed_from_skill.delta_tokens > 0

    # Changing the exposed tool set (entering plan mode or registering a new tool) invalidates back to "estimated".
    conv.set_mode("plan")
    after_plan = loop.context_snapshot()
    assert after_plan.source == "estimated" and after_plan.anchored_tokens is None

    # Re-anchor with an active skill, then verify equal-token skill replacement still reports "mixed".
    provider.responses.append(response("计划回复", usage=Usage(input_tokens=6000, output_tokens=100)))
    await conv.ask("看计划")
    assert loop.context_snapshot().source == "reported"
    loop.active_skills.clear()
    other_skill = SkillDef("other", "另一规程", "不同正文", tmp_path / "SKILL.md", tmp_path)
    loop.active_skills.activate(other_skill, "", "Skill demo\n\n步骤其他")
    assert loop.context_snapshot().source == "mixed"

    # Re-anchor, then invalidate via dynamic tool registration and tool schema modification.
    provider.responses.extend(
        [
            response("再次锚定", usage=Usage(input_tokens=6100, output_tokens=80)),
            response("注册工具后锚定", usage=Usage(input_tokens=6500, output_tokens=90)),
        ]
    )
    await conv.ask("继续计划")
    assert loop.context_snapshot().source == "reported"
    reg.register(ExtraTool(tmp_path))
    after_tool_reg = loop.context_snapshot()
    assert after_tool_reg.source == "estimated" and after_tool_reg.anchored_tokens is None

    await conv.ask("调用前检查")
    assert loop.context_snapshot().source == "reported"
    reg.get("extra_dynamic_tool").description = "Updated schema description for MCP reload"
    assert loop.context_snapshot().source == "estimated"


def test_coalesce_adjacent_users_preserves_text_and_multimodal_order():
    entries = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一段"},
        {"role": "user", "content": ""},
        {"role": "user", "content": "第二段"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": "前置说明"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        },
        {"role": "user", "content": "<system-reminder>\n提醒\n</system-reminder>"},
    ]
    original_copy = json.loads(json.dumps(entries, ensure_ascii=False))
    merged = _coalesce_adjacent_users(entries)
    assert entries == original_copy  # Input entries are never mutated in place.
    assert [m["role"] for m in merged] == ["system", "user", "assistant", "user"]
    assert merged[1]["content"] == "第一段\n\n第二段"
    assert merged[3]["content"] == [
        {"type": "text", "text": "前置说明"},
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "<system-reminder>\n提醒\n</system-reminder>"},
    ]


async def test_chat_completions_merges_collapsed_multimodal_users_and_folds_trailing_supplements():
    img = make_image("red", "[图片 #1]")
    tool_img = make_image("green", "chart.png")

    collapse_summary = Collapse(2, text_msg("user", "（折叠摘要：前两轮对话）", synthetic=COLLAPSE)).summary
    multimodal_user = Message(
        "user",
        (ContentBlock("text", "请分析 [图片 #1]"), ContentBlock("image", "[图片 #1]", img)),
    )
    reminder_msg = text_msg(
        "user", "<system-reminder>\n当前日期已变为 2026-11-18。\n</system-reminder>", synthetic=REMINDER
    )
    assistant_call = Message("assistant", (), tool_calls=(ToolCall("c1", "read_file", '{"path":"chart.png"}'),))
    tool_msg = Message("tool", (), tool_results=(ToolResult("c1", "read_file", "chart.png", images=(tool_img,)),))
    queued_user = text_msg("user", "顺便检查坐标轴")

    prompt = PromptRequest(
        "稳定系统提示",
        (
            Supplement("environment", '{"date":"2026-11-17"}'),
            Supplement("reply-recovery", "上一次回复是空的，请继续。"),
        ),
    )
    payload = await capture_chat_payload(
        [collapse_summary, multimodal_user, reminder_msg, assistant_call, tool_msg, queued_user],
        prompt,
        accepts_images=True,
    )
    wire = payload["messages"]
    roles = [m["role"] for m in wire]
    assert roles == ["system", "system", "user", "assistant", "tool", "user"]

    # Never place "system" after any non-system message, and never emit adjacent "user" messages.
    first_non_system = next(i for i, r in enumerate(roles) if r != "system")
    assert all(r != "system" for r in roles[first_non_system:])
    assert all(not (a == b == "user") for a, b in zip(roles, roles[1:]))

    # First user turn preserves exact order: collapse summary -> user text -> image -> system-reminder.
    first_user_parts = wire[2]["content"]
    assert [p["type"] for p in first_user_parts] == ["text", "text", "image_url", "text"]
    assert "折叠摘要" in first_user_parts[0]["text"]
    assert first_user_parts[1]["text"] == "请分析 [图片 #1]"
    assert first_user_parts[2]["image_url"]["url"] == f"data:image/png;base64,{img.base64()}"
    assert first_user_parts[3]["text"].startswith("<system-reminder>")

    # Second user turn merges tool image batch + queued user text + trailing reply-recovery supplement.
    tail_user_parts = wire[5]["content"]
    tail_texts = [p["text"] for p in tail_user_parts if p["type"] == "text"]
    assert tail_texts == [
        "工具结果中的图片：",
        "chart.png",
        "顺便检查坐标轴",
        "<reply-recovery>\n上一次回复是空的，请继续。\n</reply-recovery>",
    ]
    assert [p["image_url"]["url"] for p in tail_user_parts if p["type"] == "image_url"] == [
        f"data:image/png;base64,{tool_img.base64()}"
    ]

    # Trailing recovery right after a text-only tool result also becomes "user" rather than "system".
    after_tool_payload = await capture_chat_payload(
        [
            text_msg("user", "读文件"),
            Message("assistant", (), tool_calls=(ToolCall("r1", "read_file", '{"path":"a.py"}'),)),
            Message("tool", (), tool_results=(ToolResult("r1", "read_file", "print(1)"),)),
        ],
        prompt,
    )
    after_tool_roles = [m["role"] for m in after_tool_payload["messages"]]
    assert after_tool_roles == ["system", "system", "user", "assistant", "tool", "user"]
    assert after_tool_payload["messages"][-1]["content"].startswith("<reply-recovery>")
