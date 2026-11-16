from pathlib import Path

from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    FileEditToolCallContent,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)

from mosscode import credentials
from mosscode.acp.events import (
    EXCLUDED_ACP_COMMANDS,
    AcpEventMapper,
    build_available_commands,
    build_config_options,
    build_mode_state,
    map_stop_reason,
    replay_history_updates,
    todos_to_plan_update,
    tool_kind,
    tool_title,
)
from mosscode.builtin_commands import BUILTIN
from mosscode.config import ProviderConfig, Settings
from mosscode.messages import (
    REMINDER,
    AutoContinue,
    CompactFailed,
    CompactFinished,
    CompactStarted,
    ContentBlock,
    ContextCollapsed,
    Delta,
    FilesAttached,
    Message,
    Notice,
    ReplyRetry,
    ResultsCleared,
    Retrying,
    RunStopped,
    TasksNotified,
    ToolCall,
    ToolProgress,
    ToolResult,
    UserInput,
)
from mosscode.permissions import ApprovalRequest, Operation
from mosscode.skills import SkillCatalog
from mosscode.todo import TodoItem


def test_tool_kind_and_titles_cover_builtin_mcp_and_skills(tmp_path):
    assert tool_kind("read_file") == "read"
    assert tool_kind("write_file") == "edit"
    assert tool_kind("edit_file") == "edit"
    assert tool_kind("glob") == "search"
    assert tool_kind("grep") == "search"
    assert tool_kind("web_search") == "search"
    assert tool_kind("web_fetch") == "fetch"
    assert tool_kind("execute_command") == "execute"
    assert tool_kind("exit_plan_mode") == "switch_mode"
    assert tool_kind("todo_write") == "think"
    assert tool_kind("agent") == "think"
    assert tool_kind("ask_user") == "other"
    assert tool_kind("mcp__github__create_issue") == "other"

    assert tool_title("read_file", {"path": "src/a.py"}, tmp_path) == "读取 src/a.py"
    assert tool_title("execute_command", {"command": "pytest -q"}, tmp_path) == "运行 `pytest -q`"
    assert tool_title("mcp__github__search_code", {}, tmp_path) == "github/search_code"
    assert tool_title("skill__deploy__run", {}, tmp_path) == "deploy/run"
    assert (
        tool_title("read_file", {"path": "a.py"}, tmp_path, source="子 Agent explore") == "[子 Agent explore] 读取 a.py"
    )


def test_event_mapper_streams_deltas_and_buffers_notices_inside_code_fences(tmp_path):
    mapper = AcpEventMapper(tmp_path)

    thoughts = mapper.map_event(Delta("thinking", "step 1"))
    assert len(thoughts) == 1 and isinstance(thoughts[0], AgentThoughtChunk)
    assert thoughts[0].content.text == "step 1"

    open_fence = mapper.map_event(Delta("text", "```python\nprint(1)\n"))
    assert len(open_fence) == 1 and isinstance(open_fence[0], AgentMessageChunk)

    # Notice inside an open markdown fence is buffered so it does not split syntax.
    assert mapper.map_event(Notice("queued notice")) == []
    assert mapper.map_event(Retrying(1, 2, 1.5, "限流")) == []
    assert mapper.map_event(ReplyRetry("空回复", 1, 2)) == []
    assert mapper.map_event(AutoContinue(1, 3)) == []
    assert mapper.map_event(CompactStarted(False)) == []
    assert mapper.map_event(ResultsCleared(2, 1000)) == []
    assert mapper.map_event(ContextCollapsed(4, 2000, 800, 0.2)) == []
    assert mapper.map_event(CompactFinished(5, 300, 0.5)) == []
    assert mapper.map_event(CompactFailed("skip", 0, False, "nothing", True)) == []
    assert mapper.map_event(FilesAttached(("a.py",))) == []
    assert mapper.map_event(TasksNotified(("done",))) == []

    close_fence = mapper.map_event(Delta("text", "```"))
    assert len(close_fence) == 2
    assert close_fence[0].content.text == "```"
    flushed = close_fence[1].content.text
    assert "queued notice" in flushed
    assert "自动续写" in flushed
    assert "已压缩上下文" in flushed
    assert "a.py" in flushed
    assert "后台任务通知" in flushed

    user_updates = mapper.map_event(UserInput(("follow up",)))
    assert len(user_updates) == 1 and isinstance(user_updates[0], UserMessageChunk)


def test_event_mapper_produces_diff_and_locations_for_edit_and_write(tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("line1\nold\nline3\n", encoding="utf-8", newline="\n")
    mapper = AcpEventMapper(tmp_path)

    call_edit = ToolCall("c1", "edit_file", '{"path": "demo.py", "old_text": "old", "new_text": "new"}')
    running_updates = mapper.map_event(ToolProgress(call_edit, "running"))
    assert len(running_updates) == 1 and isinstance(running_updates[0], ToolCallStart)
    assert running_updates[0].kind == "edit"
    assert running_updates[0].locations and running_updates[0].locations[0].line == 2
    assert Path(running_updates[0].locations[0].path) == target.resolve()

    # Apply the edit before emitting finished phase, as execute_batch does.
    target.write_text("line1\nnew\nline3\n", encoding="utf-8", newline="\n")
    result_edit = ToolResult("c1", "edit_file", "ok", details={"path": str(target.resolve()), "diff": "@@ ..."})
    finished_updates = mapper.map_event(ToolProgress(call_edit, "finished", result_edit, 0.1))
    assert len(finished_updates) == 1 and isinstance(finished_updates[0], ToolCallProgress)
    progress = finished_updates[0]
    assert progress.status == "completed"
    diffs = [c for c in (progress.content or ()) if isinstance(c, FileEditToolCallContent)]
    assert len(diffs) == 1
    assert diffs[0].old_text == "line1\nold\nline3\n"
    assert diffs[0].new_text == "line1\nnew\nline3\n"

    # New file via write_file sets old_text=None.
    created_file = tmp_path / "new.txt"
    call_write = ToolCall("c2", "write_file", '{"path": "new.txt", "content": "hello\\n"}')
    mapper.map_event(ToolProgress(call_write, "running"))
    created_file.write_text("hello\n", encoding="utf-8", newline="\n")
    result_write = ToolResult(
        "c2", "write_file", "created", details={"path": str(created_file.resolve()), "created": True}
    )
    write_done = mapper.map_event(ToolProgress(call_write, "finished", result_write, 0.1))
    write_diffs = [c for c in (write_done[0].content or ()) if isinstance(c, FileEditToolCallContent)]
    assert len(write_diffs) == 1
    assert write_diffs[0].old_text is None
    assert write_diffs[0].new_text == "hello\n"


def test_event_mapper_oversized_file_falls_back_gracefully(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 25000, encoding="utf-8")
    mapper = AcpEventMapper(tmp_path)
    call = ToolCall("c1", "write_file", '{"path": "big.txt", "content": "new"}')
    mapper.map_event(ToolProgress(call, "running"))
    big.write_text("new", encoding="utf-8")
    done = mapper.map_event(
        ToolProgress(call, "finished", ToolResult("c1", "write_file", "overwritten", details={"path": str(big)}), 0.1)
    )
    assert isinstance(done[0], ToolCallProgress)
    assert not any(isinstance(c, FileEditToolCallContent) for c in (done[0].content or ()))


def test_approval_tool_call_update_correlates_by_call_id_and_source(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    mapper = AcpEventMapper(tmp_path)

    call1 = ToolCall("call-1", "edit_file", '{"path": "a.txt", "old_text": "before", "new_text": "after"}')
    call2 = ToolCall("call-2", "edit_file", '{"path": "b.txt", "old_text": "x", "new_text": "y"}')
    mapper.map_event(ToolProgress(call1, "running"))
    mapper.map_event(ToolProgress(call2, "running"))

    req1 = ApprovalRequest(
        id="req1",
        call_id="call-1",
        operation=Operation("edit_file", "a.txt", preview="diff"),
        reason="needs approval",
        source="子 Agent worker",
    )
    update = mapper.approval_tool_call_update(req1)
    assert update.tool_call_id == "call-1"
    assert "[子 Agent worker]" in (update.title or "")
    diffs = [c for c in (update.content or ()) if isinstance(c, FileEditToolCallContent)]
    assert len(diffs) == 1 and diffs[0].new_text == "after"

    # Network proxy request during execute_command gets distinct card id.
    net_req = ApprovalRequest(
        id="net1",
        call_id="cmd-9",
        operation=Operation("web_fetch", "example.com", preview="connect example.com:443", network=True),
        reason="沙箱中的命令要连接一个新的域名。",
    )
    net_update = mapper.approval_tool_call_update(net_req)
    assert net_update.tool_call_id == "cmd-9:net1"
    assert net_update.kind == "fetch"


def test_commands_modes_config_options_plans_stop_reasons_and_replay(tmp_path):
    skill_dir = tmp_path / ".mosscode" / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy service\n---\nRun deploy\n",
        encoding="utf-8",
        newline="\n",
    )
    catalog = SkillCatalog.load(tmp_path, tmp_path / "home", plugins=())
    assert "deploy" in catalog.skills
    registry, skipped = BUILTIN.with_skills(catalog)
    assert skipped == ()
    cmd_update = build_available_commands(registry)
    assert isinstance(cmd_update, AvailableCommandsUpdate)
    names = {c.name for c in cmd_update.available_commands}
    assert {"help", "status", "compact", "clear", "mcp", "skills", "agents", "hooks", "permissions", "deploy"} <= names
    assert names.isdisjoint(EXCLUDED_ACP_COMMANDS)

    mode_state = build_mode_state("acceptEdits")
    assert mode_state.current_mode_id == "acceptEdits"
    assert [m.id for m in mode_state.available_modes] == ["default", "acceptEdits", "plan", "bypassPermissions"]

    p1 = ProviderConfig("claude", "anthropic-messages", "claude-sonnet-4-6", api_key="k1")
    p2 = ProviderConfig("deepseek", "openai-chat-completions", "deepseek-chat", api_key="k2", reasoning=False)
    settings = Settings(providers=(p1, p2), default_provider="claude")
    opts = build_config_options("default", settings, p1, "medium")
    ids = [o.id for o in opts]
    assert ids == ["mode", "model", "thought_level"]
    opts_no_think = build_config_options("plan", settings, p2, None)
    assert [o.id for o in opts_no_think] == ["mode", "model"]

    plan = todos_to_plan_update([TodoItem("1", "task 1", "in_progress"), TodoItem("2", "task 2", "completed")])
    assert isinstance(plan, AgentPlanUpdate)
    assert [(e.content, e.status) for e in plan.entries] == [("task 1", "in_progress"), ("task 2", "completed")]

    assert map_stop_reason(None) == "end_turn"
    assert map_stop_reason(RunStopped("completed", "", 1)) == "end_turn"
    assert map_stop_reason(RunStopped("max_tokens", "cut", 2)) == "max_tokens"
    assert map_stop_reason(RunStopped("max_iterations", "limit", 10)) == "max_turn_requests"
    assert map_stop_reason(RunStopped("cancelled", "bye", 1)) == "cancelled"

    history = [
        Message("user", (ContentBlock("text", "internal"),), synthetic=REMINDER),
        Message("user", (ContentBlock("text", "hello"),)),
        Message(
            "assistant",
            (ContentBlock("thinking", "hmm"), ContentBlock("text", "checking")),
            tool_calls=(ToolCall("t1", "read_file", '{"path": "a.py"}'),),
        ),
        Message(
            "tool",
            (),
            tool_results=(ToolResult("t1", "read_file", "1\tcode", details={"path": str(tmp_path / "a.py")}),),
        ),
    ]
    replayed = replay_history_updates(history, tmp_path)
    assert isinstance(replayed[0], UserMessageChunk)
    assert isinstance(replayed[1], AgentThoughtChunk)
    assert isinstance(replayed[2], AgentMessageChunk)
    assert isinstance(replayed[3], ToolCallStart) and replayed[3].tool_call_id == "t1"
    assert isinstance(replayed[4], ToolCallProgress) and replayed[4].status == "completed"


def test_split_code_fences_across_delta_chunks_and_reset_on_stop(tmp_path):
    mapper = AcpEventMapper(tmp_path)

    u1 = mapper.map_event(Delta("text", "``"))
    assert len(u1) == 1 and u1[0].content.text == "``"
    assert mapper._fence_open is False

    u2 = mapper.map_event(Delta("text", "`py\ncode\n``"))
    assert len(u2) == 1 and u2[0].content.text == "`py\ncode\n``"
    assert mapper._fence_open is True

    # Notice arriving before the closing backtick is buffered.
    assert mapper.map_event(Notice("split fence notice")) == []

    u3 = mapper.map_event(Delta("text", "`\n"))
    assert len(u3) == 2
    assert u3[0].content.text == "`\n"
    assert "split fence notice" in u3[1].content.text
    assert mapper._fence_open is False

    # Unclosed fence with no buffered notices still resets on RunStopped / flush_notices.
    mapper.map_event(Delta("text", "```python\nx = 1\n"))
    assert mapper._fence_open is True
    mapper.map_event(RunStopped("completed", "", 1))
    assert mapper._fence_open is False
    assert mapper.map_event(Notice("next turn notice")) != []


def test_diff_and_run_stopped_redact_registered_api_keys(tmp_path):
    secret = "sk-test-do-not-show-123"
    credentials.forget_all()
    credentials.remember(secret)
    try:
        target = tmp_path / "cfg.py"
        target.write_text(f'KEY = "{secret}"\nMODE = "dev"\n', encoding="utf-8", newline="\n")
        mapper = AcpEventMapper(tmp_path)

        call = ToolCall(
            "call-sec",
            "edit_file",
            f'{{"path": "cfg.py", "old_text": "MODE = \\"dev\\"", "new_text": "MODE = \\"prod\\"  # {secret}"}}',
        )
        mapper.map_event(ToolProgress(call, "running"))

        req = ApprovalRequest("req-sec", "call-sec", Operation("edit_file", "cfg.py", preview=f"raw {secret}"), "ask")
        approval_update = mapper.approval_tool_call_update(req)
        approval_diffs = [c for c in (approval_update.content or ()) if isinstance(c, FileEditToolCallContent)]
        assert len(approval_diffs) == 1
        assert secret not in (approval_diffs[0].old_text or "")
        assert secret not in approval_diffs[0].new_text
        assert credentials.PLACEHOLDER in (approval_diffs[0].old_text or "")
        assert credentials.PLACEHOLDER in approval_diffs[0].new_text

        target.write_text(f'KEY = "{secret}"\nMODE = "prod"  # {secret}\n', encoding="utf-8", newline="\n")
        done = mapper.map_event(
            ToolProgress(
                call, "finished", ToolResult("call-sec", "edit_file", "ok", details={"path": str(target)}), 0.1
            )
        )
        done_diffs = [c for c in (done[0].content or ()) if isinstance(c, FileEditToolCallContent)]
        assert len(done_diffs) == 1
        assert secret not in (done_diffs[0].old_text or "")
        assert secret not in done_diffs[0].new_text
        assert credentials.PLACEHOLDER in done_diffs[0].new_text

        stopped_updates = mapper.map_event(RunStopped("error", f"failed with key {secret}", 1))
        assert len(stopped_updates) == 1
        assert secret not in stopped_updates[0].content.text
        assert credentials.PLACEHOLDER in stopped_updates[0].content.text
    finally:
        credentials.forget_all()


def test_acp_markdown_formatting_for_help_and_reports(tmp_path):
    from mosscode.acp.formatting import (
        escape_markdown_inline,
        format_help_markdown,
        format_permissions_markdown,
        format_skills_markdown,
        format_terminal_report_markdown,
        summarize_command_description,
    )

    skill_dir = tmp_path / ".mosscode" / "skills" / "eli5"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: eli5\ndescription: Explain a topic like I'm a 5 year old. "
        "Use when the user types /eli5 <topic> or asks for a dead-simple explainer.\n---\nExplain simply\n",
        encoding="utf-8",
        newline="\n",
    )
    catalog = SkillCatalog.load(tmp_path, tmp_path / "home", plugins=())
    registry, _ = BUILTIN.with_skills(catalog)

    overview = format_help_markdown(registry, "", catalog)
    assert "### 可用命令" in overview
    assert "#### 会话与上下文\n\n- `/help [命令]`" in overview
    assert "#### Skill 与提示词命令\n\n" in overview
    # Verbose multi-sentence skill prompt is summarized in overview and autocomplete.
    assert "Explain a topic like I'm a 5 year old." in overview
    assert "Use when the user types" not in overview
    # Terminal-only commands and keybindings are excluded from ACP /help.
    assert "`/exit`" not in overview
    assert "`/resume [ID]`" not in overview
    assert "`/rewind`" not in overview
    assert "Shift+Tab" not in overview
    assert "查看命令和快捷键" not in overview

    cmd_update = build_available_commands(registry)
    eli5_cmd = next(c for c in cmd_update.available_commands if c.name == "eli5")
    assert eli5_cmd.description == "Explain a topic like I'm a 5 year old."

    # Detailed /help <skill> preserves full description with HTML angle brackets escaped.
    detail = format_help_markdown(registry, "eli5", catalog)
    assert "### `/eli5 [参数]`" in detail
    assert r"\<topic\>" in detail

    # Terminal-only command query gives ACP-specific guidance.
    excluded_help = format_help_markdown(registry, "resume", catalog)
    assert "仅用于终端交互界面" in excluded_help
    assert "历史会话" in excluded_help

    skills_md = format_skills_markdown(catalog)
    assert "### 已加载 Skill" in skills_md
    assert "Use when the user types" not in skills_md

    perms_md = format_permissions_markdown("default", invalid_arg="typo")
    assert "未知权限档位 `typo`" in perms_md
    assert "Shift+Tab" not in perms_md

    assert escape_markdown_inline("用法：`/tasks stop <id>` 或 <名称>") == r"用法：`/tasks stop <id>` 或 \<名称\>"
    assert summarize_command_description("First sentence here. Second long explanation.") == "First sentence here."

    raw_report = "Hook test\n  文件    .mosscode/hooks.yaml\n  配置\n    event: turn_start\n    command: echo ok"
    md_report = format_terminal_report_markdown(raw_report)
    assert "### Hook test" in md_report
    assert "- **文件**：.mosscode/hooks.yaml" in md_report
    assert "```text\nevent: turn_start\ncommand: echo ok\n```" in md_report
