"""End-to-end integration tests across KV-cache invariants, Memory 2.0 worktree sharing, and Skills/Context UX."""

from __future__ import annotations

import json
import subprocess
from datetime import date as real_date
from pathlib import Path
from types import SimpleNamespace

import mosscode.agent as agent_module
import mosscode.prompts as prompts_module
from mosscode.builtin_commands import _context_line, dispatch, status_report
from mosscode.commands import CommandContext, parse_input
from mosscode.config import ProviderConfig, Settings
from mosscode.context import ContextSnapshot, estimate_text
from mosscode.messages import ToolCall, ToolProgress
from mosscode.paths import git_common_root, project_dir, project_key
from mosscode.providers import ChatCompletionsProvider
from mosscode.runtime_factory import assemble_workspace_extensions, build_conversation
from mosscode.tui_views import describe_call, memory_line
from test_commands import FakeUI
from test_sessions import drain


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


class ScriptedChatClient:
    """Deterministic async Chat Completions client recording every wire payload."""

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self.payloads: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.base_url = "http://localhost:8000/v1"

    async def _create(self, **kwargs):
        self.payloads.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
        spec = self._turns.pop(0)

        async def _gen():
            text = spec.get("text", "")
            calls = spec.get("tool_calls", ())
            finish = spec.get("finish_reason", "tool_calls" if calls else "stop")
            usage = spec.get("usage")
            if text:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=text, tool_calls=None, refusal=None, reasoning_content=None),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                )
            for idx, item in enumerate(calls):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                refusal=None,
                                reasoning_content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=idx,
                                        id=item["id"],
                                        function=SimpleNamespace(
                                            name=item["name"],
                                            arguments=json.dumps(item["arguments"], ensure_ascii=False),
                                        ),
                                    )
                                ],
                            ),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None, refusal=None, reasoning_content=None),
                        finish_reason=finish,
                    )
                ],
                usage=None,
            )
            if usage is not None:
                yield SimpleNamespace(choices=[], usage=dict(usage))

        class _Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            def __aiter__(self):
                return _gen()

        return _Stream()

    async def close(self) -> None:
        return None


def _write_skill(base: Path, name: str, body: str, *, with_tool: bool = False) -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    header = f"---\nname: {name}\ndescription: {name} 规程\n---\n"
    (directory / "SKILL.md").write_text(header + body, encoding="utf-8")
    if with_tool:
        spec = [
            {
                "name": "check",
                "description": f"{name} 专属检查工具",
                "input_schema": {"type": "object", "properties": {}},
                "command": ["${PYTHON}", "-c", "print('checked')"],
            }
        ]
        (directory / "tool.json").write_text(json.dumps(spec), encoding="utf-8")
    return directory


async def test_e2e_main_repo_pinned_note_shared_with_real_git_worktree_via_runtime_factory(tmp_path: Path):
    home = tmp_path / "home"
    main_repo = tmp_path / "repo-main"
    main_repo.mkdir()
    _git(main_repo, "init")
    _git(main_repo, "config", "user.email", "dev@example.com")
    _git(main_repo, "config", "user.name", "Dev")
    (main_repo / "README.md").write_text("# MossCode Worktree E2E\n", encoding="utf-8")
    _git(main_repo, "add", "README.md")
    _git(main_repo, "commit", "-m", "initial")

    worktree_repo = tmp_path / "repo-wt-feature"
    _git(main_repo, "worktree", "add", "-b", "feat/memory-wt", str(worktree_repo))

    provider_cfg = ProviderConfig(
        name="local-vllm",
        protocol="openai-chat-completions",
        model="qwen-coder",
        api_key="test-only",
        context_window=100_000,
        max_output_tokens=20_000,
        max_retries=0,
    )
    settings = Settings(providers=(provider_cfg,), default_provider="local-vllm")

    # 1. Assemble main repo Conversation via runtime_factory and save a pinned architectural note.
    main_client = ScriptedChatClient(
        [
            {
                "tool_calls": [
                    {
                        "id": "call_save_1",
                        "name": "memory",
                        "arguments": {
                            "action": "save",
                            "scope": "project",
                            "name": "kv-cache-invariant",
                            "type": "project",
                            "description": "跨午夜与非计划权限切换必须保持前缀字节稳定",
                            "body": "日期与权限档位变更统一追加在消息尾部。\n原因：避免破坏服务端 KV 缓存\n适用方式：全局",
                            "pinned": True,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 3_000, "completion_tokens": 80},
            },
            {
                "text": "已将核心架构约束保存为常驻项目笔记。",
                "usage": {"prompt_tokens": 3_200, "completion_tokens": 40},
            },
        ]
    )
    main_provider = ChatCompletionsProvider(provider_cfg, main_client)
    main_ws = assemble_workspace_extensions(
        main_repo, settings, home=home, permission_mode="default", ask=False, no_hooks=True
    )
    main_conv = build_conversation(
        settings,
        provider_cfg,
        root=main_repo,
        registry=main_ws.registry,
        skills=main_ws.skills,
        agents=main_ws.agents,
        mcp_manager=main_ws.mcp_manager,
        hooks=main_ws.hooks,
        home=home,
        notices=main_ws.notices,
        provider=main_provider,
    )

    events = await drain(main_conv.reply("请记住 KV 缓存前缀不变量，设为常驻项目笔记"))
    save_finished = next(e for e in events if isinstance(e, ToolProgress) and e.phase == "finished")
    assert not save_finished.result.is_error
    assert save_finished.result.details["pinned"] is True
    assert "✎ 已记住：kv-cache-invariant（项目级）" == memory_line(save_finished.result.details).plain

    # 2. Assemble linked git worktree Conversation via runtime_factory (in read-only `plan` mode!).
    assert git_common_root(worktree_repo) == main_repo.resolve()
    assert project_key(worktree_repo) != project_key(main_repo)
    assert project_dir(worktree_repo, home) != project_dir(main_repo, home)

    wt_client = ScriptedChatClient(
        [
            {
                "tool_calls": [
                    {
                        "id": "call_search_1",
                        "name": "memory",
                        "arguments": {"action": "search", "query": "KV 缓存前缀稳定", "limit": 5},
                    },
                    {
                        "id": "call_read_1",
                        "name": "memory",
                        "arguments": {"action": "read", "name": "kv-cache-invariant"},
                    },
                    {
                        "id": "call_list_1",
                        "name": "memory",
                        "arguments": {"action": "list", "scope": "project"},
                    },
                ],
                "usage": {"prompt_tokens": 3_400, "completion_tokens": 90},
            },
            {
                "text": "已在工作树中检索并读取主仓库保存的常驻架构笔记。",
                "usage": {"prompt_tokens": 3_900, "completion_tokens": 50},
            },
            {
                "text": "后续轮次无多余更新提醒。",
                "usage": {"prompt_tokens": 4_000, "completion_tokens": 30},
            },
        ]
    )
    wt_provider = ChatCompletionsProvider(provider_cfg, wt_client)
    wt_ws = assemble_workspace_extensions(
        worktree_repo, settings, home=home, permission_mode="plan", ask=False, no_hooks=True
    )
    wt_conv = build_conversation(
        settings,
        provider_cfg,
        root=worktree_repo,
        registry=wt_ws.registry,
        skills=wt_ws.skills,
        agents=wt_ws.agents,
        mcp_manager=wt_ws.mcp_manager,
        hooks=wt_ws.hooks,
        home=home,
        notices=wt_ws.notices,
        provider=wt_provider,
    )

    assert wt_conv.memory.directory("project") == main_conv.memory.directory("project")

    wt_events = await drain(wt_conv.reply("检索并读取关于 KV 缓存前缀的架构笔记"))
    assert "[kv-cache-invariant](kv-cache-invariant.md) [常驻]" in wt_conv.memory_instructions

    finished_tools = [e for e in wt_events if isinstance(e, ToolProgress) and e.phase == "finished"]
    assert [e.result.details["action"] for e in finished_tools] == ["search", "read", "list"]
    assert all(not e.result.is_error for e in finished_tools)

    search_res, read_res, list_res = (e.result for e in finished_tools)
    assert search_res.details["results"][0]["name"] == "kv-cache-invariant"
    assert search_res.details["results"][0]["pinned"] is True
    assert "[常驻]" in search_res.output
    assert "pinned: true" in read_res.output and "避免破坏服务端 KV 缓存" in read_res.output
    assert list_res.details["count"] == 1 and "kv-cache-invariant" in list_res.output

    # TUI call & result formatters render read/search/list accurately.
    assert describe_call(ToolCall("s", "memory", '{"action":"search","query":"KV 缓存"}'))[:2] == (
        "Memory",
        "search KV 缓存",
    )
    assert "检索记忆：KV 缓存前缀稳定（1 条匹配）" in memory_line(search_res.details).plain
    assert "已读取笔记：kv-cache-invariant" in memory_line(read_res.details).plain
    assert "列出记忆笔记：共 1 条（项目级）" in memory_line(list_res.details).plain

    # Read-only memory actions must NOT queue a "笔记 ... 已更新或删除" reminder on the next turn!
    await drain(wt_conv.reply("继续规划"))
    last_wire_msgs = wt_client.payloads[-1]["messages"]
    assert not any("已更新或删除" in str(m.get("content", "")) for m in last_wire_msgs)

    # 3. Verify Agent Team worktree members (both in-process `build_runner` and pane `member_conversation`)
    #    receive canonical repository `memory_instructions` and `memory_dirs` without approval prompts!
    from mosscode.teams import Member, TeamStore
    from mosscode.teams.process import member_conversation
    from mosscode.teams.runtime import build_runner

    team_store = TeamStore.at(tmp_path / "teams-root", "wt-team")
    team_store.create("worktree memory test")
    member = Member(
        "worker-1",
        "general",
        transcript="transcripts/worker-1.jsonl",
        worktree={"path": str(worktree_repo), "branch": "feat/memory-wt"},
    )
    team_store.modify(lambda t: t.members.append(member))
    await main_conv.clear()
    agent_def = main_conv.subagents.catalog.get("general")
    inproc_runner = await build_runner(main_conv, team_store, member, agent_def, worktree_repo, "repo-wt-feature")
    assert "[kv-cache-invariant](kv-cache-invariant.md) [常驻]" in inproc_runner.loop.instructions
    note_path = main_conv.memory.directory("project") / "kv-cache-invariant.md"
    read_note = await inproc_runner.loop.registry.execute(
        ToolCall("rn", "read_file", json.dumps({"path": str(note_path)}))
    )
    assert not read_note.is_error and "避免破坏服务端 KV 缓存" in read_note.output
    await inproc_runner.close()

    pane_conv = member_conversation(settings, provider_cfg, worktree_repo, interactive=False, home=home)
    try:
        assert pane_conv.memory is not None
        assert pane_conv.memory.directory("project") == main_conv.memory.directory("project")
        await pane_conv._load_context()
        assert "[kv-cache-invariant](kv-cache-invariant.md) [常驻]" in pane_conv.memory_instructions
    finally:
        await pane_conv.close()

    await main_conv.close()
    await wt_conv.close()


async def test_e2e_multiturn_chat_completions_midnight_modes_skills_rewind_and_status_sources(
    tmp_path: Path, monkeypatch
):
    current = {"day": real_date(2026, 11, 17)}

    class FakeDate:
        @staticmethod
        def today():
            return current["day"]

    monkeypatch.setattr(prompts_module, "date", FakeDate)
    monkeypatch.setattr(agent_module, "date", FakeDate)

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    skills_dir = project / ".mosscode" / "skills"
    _write_skill(skills_dir, "guide-sop", "代码审查规程正文 " * 80)
    _write_skill(skills_dir, "heavy-sop", "重型部署规程\n" + ("x" * 60_000), with_tool=True)

    provider_cfg = ProviderConfig(
        name="local-vllm",
        protocol="openai-chat-completions",
        model="qwen-coder",
        context_window=100_000,
        max_output_tokens=20_000,  # effective = 80,000, auto_threshold = 67,000
        max_retries=0,
    )
    settings = Settings(providers=(provider_cfg,), default_provider="local-vllm", persist_sessions=True)

    client = ScriptedChatClient(
        [
            # Turn 1 (Day 1, default): provider omits usage -> status shows `估算`
            {"text": "第一轮：服务端未返回用量的本地网关回复。", "usage": None},
            # Turn 2 (Day 1, default): provider reports usage -> status transitions to `服务端返回`
            {"text": "第二轮：已返回精确 Token 用量。", "usage": {"prompt_tokens": 12_000, "completion_tokens": 200}},
            # Turn 3 (Day 2 after midnight + acceptEdits): loads `guide-sop` and finishes with usage
            {
                "tool_calls": [{"id": "sk_1", "name": "load_skill", "arguments": {"name": "guide-sop"}}],
                "usage": {"prompt_tokens": 12_500, "completion_tokens": 100},
            },
            {
                "text": "第三轮：跨午夜并切换为 acceptEdits，已加载 guide-sop。",
                "usage": {"prompt_tokens": 14_000, "completion_tokens": 300},
            },
            # Turn 4: first attempt returns empty reply (triggers <reply-recovery>), retry loads heavy-sop (~60 KB)
            {"text": "", "usage": {"prompt_tokens": 13_800, "completion_tokens": 0}},
            {
                "tool_calls": [{"id": "sk_2", "name": "load_skill", "arguments": {"name": "heavy-sop"}}],
                "usage": {"prompt_tokens": 13_900, "completion_tokens": 100},
            },
            {
                "text": "第四轮：已加载 60KB 重型规程与专属工具。",
                "usage": {"prompt_tokens": 30_000, "completion_tokens": 200},
            },
        ]
    )
    provider = ChatCompletionsProvider(provider_cfg, client)
    ws = assemble_workspace_extensions(
        project, settings, home=home, permission_mode="default", ask=False, no_hooks=True
    )
    conv = build_conversation(
        settings,
        provider_cfg,
        root=project,
        registry=ws.registry,
        skills=ws.skills,
        agents=ws.agents,
        mcp_manager=ws.mcp_manager,
        hooks=ws.hooks,
        home=home,
        notices=ws.notices,
        provider=provider,
    )

    # Pre-turn snapshot includes exact `<environment>` tokens even before Turn 1 starts!
    pre_snap = conv.context_snapshot()
    assert pre_snap.source == "estimated"
    assert pre_snap.tokens >= estimate_text(conv._environment().text) > 0
    assert _context_line(conv) == "尚未发送请求"

    # Turn 1 -> `估算`
    await drain(conv.reply("开始第一轮"))
    snap_t1 = conv.context_snapshot()
    assert snap_t1.source == "estimated" and snap_t1.anchored_tokens is None
    line_t1 = _context_line(conv)
    assert "Token（" in line_t1 and "，估算），自动压缩阈值 67,000；已压缩 0 次" in line_t1

    # Turn 2 -> `服务端返回` (12,000 + 200 = 12,200)
    await drain(conv.reply("继续第二轮"))
    snap_t2 = conv.context_snapshot()
    assert snap_t2 == ContextSnapshot(tokens=12_200, source="reported", anchored_tokens=12_200, delta_tokens=0)
    assert _context_line(conv) == "12,200 / 80,000 Token（15%，服务端返回），自动压缩阈值 67,000；已压缩 0 次"

    # Cross midnight (2026-11-17 -> 2026-11-18) AND switch `default -> acceptEdits`!
    current["day"] = real_date(2026, 11, 18)
    conv.set_permission_mode("acceptEdits")
    await drain(conv.reply("跨午夜后第三轮"))

    # Verify KV-cache prefix invariance across Payloads 0, 1, and 2 (Turn 1, Turn 2, and Turn 3 call 1)!
    p0, p1, p2 = client.payloads[0], client.payloads[1], client.payloads[2]
    assert p0["messages"][:2] == p1["messages"][:2] == p2["messages"][:2]
    assert p0["tools"] == p1["tools"] == p2["tools"]
    env_json = json.loads(p2["messages"][1]["content"].removeprefix("<environment>\n").removesuffix("\n</environment>"))
    assert env_json["date"] == "2026-11-17"
    assert env_json["permission_mode"] == "default"

    # Tail user message in Payload 2 coalesced user prompt + mode reminder + midnight date reminder into ONE user turn!
    tail_user = p2["messages"][-1]
    assert tail_user["role"] == "user"
    assert "跨午夜后第三轮" in tail_user["content"]
    assert "当前权限档位已切换为 acceptEdits。" in tail_user["content"]
    assert "当前日期已变为 2026-11-18。" in tail_user["content"]

    # After Turn 3 completes: `guide-sop` is active and anchored (`服务端返回` = 14,300).
    assert conv.active_skills == ("guide-sop",)
    assert conv.context_snapshot() == ContextSnapshot(
        tokens=14_300, source="reported", anchored_tokens=14_300, delta_tokens=0
    )
    assert _context_line(conv) == "14,300 / 80,000 Token（18%，服务端返回），自动压缩阈值 67,000；已压缩 0 次"

    # Unload `guide-sop` via `/skills unload guide-sop` -> transitions to `服务端 + 估算` with negative delta!
    ui = FakeUI()
    parsed = parse_input("/skills unload guide-sop", ws.commands)
    await dispatch(CommandContext(conv, ui, ws.commands), parsed)
    assert conv.active_skills == ()
    snap_after_unload = conv.context_snapshot()
    assert snap_after_unload.source == "mixed"
    assert snap_after_unload.anchored_tokens == 14_300 and snap_after_unload.delta_tokens < 0
    report_mixed = status_report(CommandContext(conv, ui, ws.commands))
    assert f"服务端 14,300 + 估算 {snap_after_unload.delta_tokens:+,}" in report_mixed

    # Re-load `guide-sop` before Turn 4 so `/rewind` before Turn 4 can verify full skill/tool snapshot restoration!
    conv._load_skill("guide-sop", "")
    assert conv.active_skills == ("guide-sop",)

    # Turn 4: empty reply triggers `<reply-recovery>` retry, then loads 60 KB `heavy-sop` + exclusive tool.
    await drain(conv.reply("第四轮加载重型规程"))
    assert conv.active_skills == ("guide-sop", "heavy-sop")
    assert conv.registry.get("skill__heavy_sop__check") is not None

    # Check every single wire payload obeys strict chat-template rules:
    # 1) No `system` message ever appears after the first non-system message.
    # 2) No two consecutive `user` messages ever appear.
    for idx, payload in enumerate(client.payloads):
        roles = [m["role"] for m in payload["messages"]]
        first_non_sys = next(i for i, r in enumerate(roles) if r != "system")
        assert all(r != "system" for r in roles[first_non_sys:]), (idx, roles)
        assert all(not (a == b == "user") for a, b in zip(roles, roles[1:])), (idx, roles)

    # Specifically verify the retry payload folded `<reply-recovery>` into the trailing `user` message.
    retry_payload = client.payloads[5]
    assert "<reply-recovery>" in retry_payload["messages"][-1]["content"]
    assert retry_payload["messages"][-1]["role"] == "user"

    # `/rewind` before Turn 4 restores `active_skills == ("guide-sop",)`, unregisters `skill__heavy_sop__check`,
    # and invalidates the context meter back to `估算`!
    turn4_point = conv.rewind_points()[0]
    assert turn4_point.message.text == "第四轮加载重型规程"
    await conv.rewind(turn4_point.message.id, code=False, conversation=True)
    assert conv.active_skills == ("guide-sop",)
    assert conv.registry.get("skill__heavy_sop__check") is None
    snap_rewound = conv.context_snapshot()
    assert snap_rewound.source == "estimated" and snap_rewound.anchored_tokens is None
    assert "，估算），自动压缩阈值 67,000；已压缩 0 次" in _context_line(conv)

    await conv.close()


async def test_e2e_fork_subagent_preserves_parent_prefix_after_midnight_and_mode_switch(tmp_path: Path, monkeypatch):
    from test_agent import SequenceProvider, call, response

    current = {"day": real_date(2026, 11, 17)}

    class FakeDate:
        @staticmethod
        def today():
            return current["day"]

    monkeypatch.setattr(prompts_module, "date", FakeDate)
    monkeypatch.setattr(agent_module, "date", FakeDate)

    home = tmp_path / "home"
    project = tmp_path / "fork-project"
    project.mkdir()

    provider_cfg = ProviderConfig("mock", "anthropic-messages", "claude-sonnet-5-5")
    settings = Settings(providers=(provider_cfg,), default_provider="mock")
    provider = SequenceProvider(
        response("第一天主对话回复"),
        response(
            calls=[
                call(
                    "agent",
                    "fork_1",
                    description="并行核查",
                    prompt="核查代码约束并回报",
                )
            ]
        ),
        response("范围：核查\n结果：完成\n涉及的关键文件：无\n未完成的部分：无"),
        response("已汇总 Fork 子 Agent 结论"),
    )
    ws = assemble_workspace_extensions(
        project, settings, home=home, permission_mode="default", ask=False, no_hooks=True
    )
    conv = build_conversation(
        settings,
        provider_cfg,
        root=project,
        registry=ws.registry,
        skills=ws.skills,
        agents=ws.agents,
        mcp_manager=ws.mcp_manager,
        hooks=ws.hooks,
        home=home,
        notices=ws.notices,
        provider=provider,
    )

    await conv.ask("第一天主任务")
    current["day"] = real_date(2026, 11, 18)
    conv.set_permission_mode("acceptEdits")
    await conv.ask("跨午夜后派发 Fork 子 Agent")

    # Request 0: Day 1 main; Request 1: Day 2 main (spawns fork); Request 2: Fork sub-agent; Request 3: Day 2 main finish.
    assert len(provider.requests) == 4
    main_req = provider.requests[1]
    fork_req = provider.requests[2]

    # Fork sub-agent reuses main's exact system prompt, `<environment>` (frozen date & mode), and tool definitions!
    assert fork_req[1] == main_req[1]
    assert fork_req[2] == main_req[2]
    fork_env = json.loads(fork_req[1].supplements[0].text)
    assert fork_env["date"] == "2026-11-17"
    assert fork_env["permission_mode"] == "default"

    # Fork history starts with main's exact message prefix and does NOT re-emit standing date/mode reminders!
    main_msgs, fork_msgs = main_req[0], fork_req[0]
    assert fork_msgs[: len(main_msgs)] == main_msgs
    trailing_reminders = [m for m in fork_msgs[len(main_msgs) :] if m.synthetic == "reminder"]
    assert trailing_reminders == []

    await conv.close()
