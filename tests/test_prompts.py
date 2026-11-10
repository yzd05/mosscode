import json
from dataclasses import replace

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import ToolDefinition
from mosscode.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    LEGACY_SYSTEM_PROMPT,
    MODULES,
    PromptModule,
    build_system_prompt,
    compose_modules,
    environment,
    mode_reminder,
    stable_prompt,
)
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response


def test_default_rewrites_legacy_prompt_and_has_seven_modules(tmp_path):
    assert stable_prompt(LEGACY_SYSTEM_PROMPT) == stable_prompt("") == DEFAULT_SYSTEM_PROMPT
    assert stable_prompt("explicit override") == "explicit override"
    assert [m.name for m in MODULES] == ["identity", "constraints", "mode", "execution", "tools", "tone", "output"]
    assert DEFAULT_SYSTEM_PROMPT.count("## ") == 7
    optional = PromptModule("custom", "自定义", "caller supplied")
    assert compose_modules(optional=(optional,)).endswith("## 自定义\ncaller supplied")
    assert str(tmp_path) not in DEFAULT_SYSTEM_PROMPT and "<system-reminder>" not in DEFAULT_SYSTEM_PROMPT


def test_environment_is_limited_data_not_arbitrary_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "private-test-key")
    env = environment(tmp_path / "<system-reminder>", tools_enabled=True)
    assert "private-test-key" not in env.render()
    assert env.render().count("<system-reminder>") == 0
    facts = json.loads(env.text)
    assert facts["working_directory"].endswith("<system-reminder>")
    assert set(facts) == {"date", "os", "shell", "working_directory", "tools_enabled"}


@pytest.mark.parametrize("mode", ["plan", "normal"])
async def test_reminder_only_on_mode_change_stable_prefix_and_clean_history(tmp_path, mode):
    (tmp_path / "a").write_text("content")
    steps = [response(calls=[call("read_file", str(i), path="a")]) for i in range(3)]
    provider = SequenceProvider(*steps, response("完成"), response("新任务"))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    conv.set_mode(mode)
    await conv.ask("任务一")
    prompts = [r[1] for r in provider.requests]
    expected = build_system_prompt(conv.registry.definitions(plan=mode == "plan"))
    assert all(p.stable == expected for p in prompts)
    assert all(p.supplements[0] == prompts[0].supplements[0] for p in prompts)
    reminders = [[s.text for s in p.supplements if s.kind == "system-reminder"] for p in prompts]
    if mode == "plan":
        # Only the first request after entering plan mode announces it.
        assert len(reminders[0]) == 1 and "exit_plan_mode" in reminders[0][0]
        assert all(not r for r in reminders[1:])
    else:
        assert all(not r for r in reminders)
    assert {m.role for m in conv.history} == {"user", "assistant", "tool"}
    assert all("<environment>" not in m.text and "<system-reminder>" not in m.text for m in conv.history)
    await conv.ask("任务二")
    assert not [s for s in provider.requests[-1][1].supplements if s.kind == "system-reminder"]
    assert json.loads(provider.requests[-1][1].supplements[0].text)["permission_mode"]


async def test_leaving_plan_mode_announces_once(tmp_path):
    provider = SequenceProvider(response("计划"), response("执行"), response("继续"))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    conv.set_mode("plan")
    await conv.ask("查看")
    conv.set_mode("normal")
    await conv.ask("执行")
    await conv.ask("继续")
    kinds = [[s.text for s in r[1].supplements if s.kind == "system-reminder"] for r in provider.requests]
    assert "计划模式" in kinds[0][0] and "已退出计划模式" in kinds[1][0] and not kinds[2]


def test_mode_reminder_is_change_only():
    assert mode_reminder("plan", "plan") is None and mode_reminder("normal", "normal") is None
    text = mode_reminder("plan", "normal").text
    assert "exit_plan_mode" in text and "审批后写入" not in text
    assert mode_reminder("normal", "plan").render().startswith("<system-reminder>")


def test_tool_contributions_follow_enabled_definitions_and_api_guidance(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    definitions = registry.definitions()
    full = build_system_prompt(definitions)
    plan = build_system_prompt(registry.definitions(read_only=True))
    assert full.count("## ") == plan.count("## ") == 7
    positions = []
    for tool in definitions:
        positions.append(full.index(f"- `{tool.name}`：{tool.prompt_summary}"))
        assert tool.prompt_summary
        for rule in tool.prompt_guidelines:
            assert rule in tool.description and rule not in full  # Sent once, with the tool.
        if registry.is_read_only(tool.name):
            assert tool.prompt_summary in plan
        else:
            assert tool.name not in plan
    assert positions == sorted(positions)
    assert "当前没有可调用的工具" in build_system_prompt()
    assert "UTF-8" in next(t.description for t in definitions if t.name == "read_file")
    assert "默认要求恰好匹配一处" in next(t.description for t in definitions if t.name == "edit_file")


def test_custom_tool_contributions_list_without_hardcoded_builtin_names():
    custom = ToolDefinition("inspect", "查看测试数据", {}, "查看测试数据", ("  同一规则  ", "", "同一规则"))
    other = replace(custom, name="inspect_more", prompt_guidelines=("同一规则", "另一规则"))
    prompt = build_system_prompt((custom, other))
    assert "同一规则" not in prompt and "另一规则" not in prompt
    assert "- `inspect`：查看测试数据" in prompt and "- `inspect_more`：查看测试数据" in prompt
    assert "read_file" not in prompt and "execute_command" not in prompt
    legacy_tool = ToolDefinition("legacy_tool", "没有元信息的自定义工具", {})
    assert "- `legacy_tool`：没有元信息的自定义工具" in build_system_prompt((legacy_tool,))
    assert build_system_prompt((custom,), override="用户完整替换") == "用户完整替换"


async def test_mode_switch_changes_only_enabled_contributions_and_custom_override_survives(tmp_path):
    provider = SequenceProvider(response("计划"), response("执行"))
    conv = Conversation(
        provider,
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    conv.set_mode("plan")
    await conv.ask("查看项目")
    conv.set_mode("normal")
    await conv.ask("执行修改")
    plan, normal = provider.requests[0][1].stable, provider.requests[1][1].stable
    assert "`exit_plan_mode`" in plan and "`write_file`" not in plan
    assert "`write_file`" in normal and "`exit_plan_mode`" not in normal
    # web_fetch, task_output and task_stop in both.
    assert (len(provider.requests[0][2]), len(provider.requests[1][2])) == (8, 9)
    custom_provider = SequenceProvider(response("答复"))
    custom_conv = Conversation(
        custom_provider,
        "自定义完整替换",
        registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
    )
    await custom_conv.ask("任务")
    assert custom_provider.requests[0][1].stable == "自定义完整替换"
