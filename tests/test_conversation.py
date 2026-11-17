import asyncio
from contextlib import aclosing

import pytest

from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.stream_closed = False
        self.fail = False
        self.hold = False
        self.started = asyncio.Event()

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append(tuple(messages))
        self.stream_closed = False
        try:
            yield Delta("text", "part")
            self.started.set()
            if self.hold:
                await asyncio.Event().wait()
            if self.fail:
                raise ProviderError("test failure")
            yield Delta("text", "ial")
            yield Completed(Message("assistant", (ContentBlock("text", "partial"),)), "stop")
        finally:
            self.stream_closed = True

    async def close(self):
        self.closed = True


async def test_multiturn_and_read_only_history_snapshot():
    provider = FakeProvider()
    conv = Conversation(provider)
    assert await conv.ask("one") == "partial"
    assert await conv.ask("two") == "partial"
    assert [m.text for m in provider.requests[1]] == ["one", "partial", "two"]
    assert len(conv.history) == 4
    assert provider.stream_closed


async def test_failure_rolls_back_entire_turn():
    provider = FakeProvider()
    conv = Conversation(provider)
    await conv.ask("good")
    provider.fail = True
    with pytest.raises(ProviderError):
        await conv.ask("bad")
    assert [m.text for m in conv.history] == ["good", "partial"]
    provider.fail = False
    await conv.ask("retry")
    assert [m.text for m in provider.requests[-1]] == ["good", "partial", "retry"]


async def test_cancel_closes_stream_releases_busy_and_avoids_commit():
    provider = FakeProvider()
    provider.hold = True
    conv = Conversation(provider)
    task = asyncio.create_task(conv.ask("cancelled"))
    await provider.started.wait()
    with pytest.raises(ProviderError, match="已有请求"):
        await conv.ask("concurrent")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.stream_closed
    assert conv.history == ()
    provider.hold = False
    assert await conv.ask("next") == "partial"


async def test_consumer_can_close_after_first_delta():
    provider = FakeProvider()
    conv = Conversation(provider)
    async with aclosing(conv.reply("one")) as stream:
        while not isinstance(await anext(stream), Delta):
            pass
    assert provider.stream_closed
    assert conv.history == ()
    assert await conv.ask("two") == "partial"


async def test_fork_skill_stops_owned_background_commands_on_completion_and_cancel(tmp_path):
    import json
    import sys

    from mosscode.messages import ToolCall
    from mosscode.session import Session
    from mosscode.skills import ForkRequest, SkillDef
    from mosscode.tools import create_default_registry

    skill_dir = tmp_path / "skills" / "bg-fork"
    skill_dir.mkdir(parents=True)
    skill = SkillDef(
        name="bg-fork",
        description="Fork skill with background command",
        body="执行后台命令",
        path=skill_dir / "SKILL.md",
        source="project",
        mode="fork",
    )
    sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'

    # Case 1: Forked skill completes while its background command is still running -> command is stopped.
    class CompletingForkProvider:
        def __init__(self):
            self.turn = 0

        async def stream(self, messages, system, tools=(), *, allow_tools=True):
            self.turn += 1
            if self.turn == 1:
                args = json.dumps({"command": sleep_cmd, "run_in_background": True})
                call = ToolCall("bg1", "execute_command", args)
                yield Completed(Message("assistant", (), tool_calls=(call,)), "tool_calls")
            else:
                yield Delta("text", "fork finished")
                yield Completed(Message("assistant", (ContentBlock("text", "fork finished"),)), "stop")

        async def close(self):
            pass

    registry1 = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv1 = Conversation(
        CompletingForkProvider(),
        registry=registry1,
        session=Session.new(tmp_path, home=tmp_path / "home1"),
        command_max_background=4,
    )
    async for _ in conv1.reply("运行", command="/bg-fork", fork=ForkRequest(skill, "运行")):
        pass
    assert conv1.commands.running() == []
    cmd_tasks_1 = [t for t in conv1.tasks.tasks.values() if t.kind == "command"]
    assert len(cmd_tasks_1) == 1 and cmd_tasks_1[0].status == "cancelled"
    await conv1.close()

    # Case 2: Forked skill is cancelled mid-flight while its background command is running -> command is stopped.
    second_turn_started = asyncio.Event()

    class HangingForkProvider:
        def __init__(self):
            self.turn = 0

        async def stream(self, messages, system, tools=(), *, allow_tools=True):
            self.turn += 1
            if self.turn == 1:
                args = json.dumps({"command": sleep_cmd, "run_in_background": True})
                call = ToolCall("bg2", "execute_command", args)
                yield Completed(Message("assistant", (), tool_calls=(call,)), "tool_calls")
            else:
                second_turn_started.set()
                await asyncio.Event().wait()
                yield Completed(Message("assistant", (ContentBlock("text", "never"),)), "stop")

        async def close(self):
            pass

    registry2 = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    conv2 = Conversation(
        HangingForkProvider(),
        registry=registry2,
        session=Session.new(tmp_path, home=tmp_path / "home2"),
        command_max_background=4,
    )

    async def consume():
        async for _ in conv2.reply("运行", command="/bg-fork", fork=ForkRequest(skill, "运行")):
            pass

    runner = asyncio.create_task(consume())
    await asyncio.wait_for(second_turn_started.wait(), timeout=5)
    assert len(conv2.commands.running()) == 1
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert not conv2._busy
    assert conv2.commands.running() == []
    cmd_tasks_2 = [t for t in conv2.tasks.tasks.values() if t.kind == "command"]
    assert len(cmd_tasks_2) == 1 and cmd_tasks_2[0].status == "cancelled"
    await conv2.close()


async def test_runtime_factory_assembles_workspace_conversation_and_acp_switch_model(tmp_path):
    from mosscode.acp.session import AcpLaunchOptions, AcpSession
    from mosscode.config import ProviderConfig, Settings
    from mosscode.runtime_factory import assemble_workspace_extensions, build_conversation, claude_notice, with_model

    home = tmp_path / "home"
    work = tmp_path / "work"
    (work / ".claude" / "skills" / "demo").mkdir(parents=True)
    (work / ".claude" / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n")

    assert "1 个 Skill" in (claude_notice(work) or "")

    p1 = ProviderConfig("p1", "anthropic-messages", "custom-model", api_key="sk-1")
    p2 = ProviderConfig("p2", "openai-responses", "gpt-4.1-mini", api_key="sk-2", context_window=256_000)
    settings = Settings((p1, p2), "p1")

    enriched = with_model(p1, "claude-sonnet-4-6", home=home)
    assert (
        enriched.model == "claude-sonnet-4-6" and enriched.catalog is not None and enriched.context_window is not None
    )
    # Explicit user context_window (not from_catalog) is preserved across model override.
    preserved = with_model(p2, "o4-mini", home=home)
    assert preserved.model == "o4-mini" and preserved.context_window == 256_000

    components = assemble_workspace_extensions(work, settings, home=home, ask=False)
    assert components.registry.get("read_file") is not None
    assert components.agents.get("explore") is not None

    created = []

    def factory(cfg: ProviderConfig):
        created.append(cfg)
        return FakeProvider()

    conv = build_conversation(
        settings,
        p1,
        root=work,
        registry=components.registry,
        skills=components.skills,
        agents=components.agents,
        mcp_manager=components.mcp_manager,
        hooks=components.hooks,
        home=home,
        notices=components.notices,
        provider_factory=factory,
    )
    assert conv.memory is not None
    assert conv.provider_info == ("p1", "anthropic-messages", "custom-model")
    await conv.close()

    class DummyClient:
        async def session_update(self, session_id, update, **kwargs):
            pass

    acp_sess = AcpSession.create(
        cwd=work,
        client=DummyClient(),
        options=AcpLaunchOptions(home=home, settings=settings, provider_factory=factory),
    )
    await acp_sess.switch_model("p1", "claude-sonnet-4-6")
    assert acp_sess.provider_config.model == "claude-sonnet-4-6"
    assert acp_sess.settings.select("p1").model == "claude-sonnet-4-6"
    assert acp_sess.conversation._fork_provider_config("p1").model == "claude-sonnet-4-6"
    await acp_sess.conversation.close()
