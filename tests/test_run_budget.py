"""Run budgets are shared across model work, including child agents and summaries."""

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import ProviderError, Usage
from mosscode.teams import TeamError
from test_agent import SequenceProvider, call, response
from test_context import chat, seeded, summary_reply
from test_goal import verdict
from test_subagents import ScriptProvider, agents_dir, conversation, write_agent
from waiting import until


async def test_concurrent_children_spend_the_parents_request_allowance(tmp_path):
    write_agent(agents_dir(tmp_path), "child", body="SUB-BUDGET")

    def script(messages, system, tools):
        if "SUB-BUDGET" in system.stable:
            return response("子任务完成")
        return response(
            calls=[call("agent", f"a{n}", agent_type="child", description="读代码", prompt="读代码") for n in range(2)]
        )

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider, run_budget={"requests": 3})
    try:
        with pytest.raises(ProviderError, match="请求预算 3 次"):
            await conv.ask("处理两个子任务")
        assert len(provider.requests) == 3
        assert len(conv.tasks.tasks) == 2 and all(t.status == "completed" for t in conv.tasks.tasks.values())
        assert conv.history[-1].role == "tool" and len(conv.history[-1].tool_results) == 2
    finally:
        await conv.close()


@pytest.mark.parametrize("usage,reason", [(Usage(90, 10), "Token 预算 100"), (Usage(None, 10), "未报告完整用量")])
async def test_token_allowance_survives_clear_and_preserves_unknown_usage(usage, reason):
    provider = SequenceProvider(response("一", usage=usage), response("不应请求"))
    conv = Conversation(provider, run_budget={"tokens": 100})
    assert await conv.ask("开始") == "一"
    await conv.clear()
    with pytest.raises(ProviderError, match=reason):
        await conv.ask("继续")
    assert len(provider.requests) == 1
    await conv.close()


async def test_a_summary_uses_the_same_request_allowance():
    provider = SequenceProvider(summary_reply(), response("不应请求"))
    conv = seeded(provider, chat(8), auto_compact=False, run_budget={"requests": 1})
    [event async for event in conv.compact()]
    with pytest.raises(ProviderError, match="请求预算 1 次"):
        await conv.ask("继续")
    assert len(provider.requests) == 1 and provider.requests[0][3] is False
    await conv.close()


async def test_goal_judging_cannot_start_after_the_shared_budget_is_spent():
    work, judge = SequenceProvider(response("改完了")), SequenceProvider(verdict("met", "通过"))
    conv = Conversation(work, goal_provider=judge, run_budget={"requests": 1})
    conv.set_goal("测试通过")
    [event async for event in conv.reply("开始")]
    step = await conv.goal_after_turn()
    assert step.action == "paused" and "请求预算 1 次" in step.note and not judge.requests
    await conv.close()


async def test_a_budgeted_team_keeps_members_in_the_shared_process(tmp_path):
    conv = conversation(
        tmp_path,
        ScriptProvider(lambda *args: response("好")),
        teams=True,
        teammate_mode="auto",
        run_budget={"requests": 1},
    )
    try:
        assert conv.teams._choose().backend == "in-process"
        await conv.teams.create("budget", "共享额度")
        await conv.teams.spawn("member", "general", "读代码", isolation="none")
        await until(lambda: conv.teams.runners["member"].status == "idle")
        with pytest.raises(ProviderError, match="请求预算 1 次"):
            await conv.ask("继续")
        assert len(conv.provider.requests) == 1
        conv.teams.teammate_mode = "tmux"
        with pytest.raises(TeamError, match="共享调用预算需要进程内成员"):
            conv.teams._choose()
    finally:
        await conv.close()
