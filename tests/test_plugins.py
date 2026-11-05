"""Plugins and .claude directories: what loads, from where, in which order, and with which variables."""

import json
from pathlib import Path

import pytest
import yaml

from mosscode.agents import AgentCatalog
from mosscode.builtin_commands import agents_report
from mosscode.cli import claude_notice
from mosscode.config import ConfigError, load_settings
from mosscode.hooks.config import load_hooks
from mosscode.mcp_config import load_mcp_settings
from mosscode.plugins import (
    PluginRecord,
    data_dir,
    installed,
    load_records,
    read_plugin,
    records_path,
    save_records,
    source_kind,
)
from mosscode.skills import SkillCatalog, source_label
from mosscode.tools.skill import SkillTool
from test_agent import call, response
from test_sessions import drain
from test_subagents import ScriptProvider, conversation, project_of, tool_results_of, write_agent


def write_skill(base: Path, name: str, body: str = "做这件事。", front: str = "") -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试 {name}\n{front}---\n{body}\n", encoding="utf-8"
    )
    return directory


def make_plugin(root: Path, name: str = "kit", manifest: dict | None = None) -> Path:
    """A plugin with a skill, a command, a sub-agent, an MCP server and a hook."""
    root.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    write_skill(root / "skills", "lint", "脚本在 ${CLAUDE_PLUGIN_ROOT}/lint.py，数据在 ${MOSSCODE_PLUGIN_DATA}。")
    (root / "commands").mkdir(exist_ok=True)
    (root / "commands" / "ship.md").write_text(
        "---\nargument-hint: <版本>\nallowed-tools: Bash(git tag:*), Read, Task\n---\n# 发布版本\n发布 $ARGUMENTS\n",
        encoding="utf-8",
    )
    write_agent(root / "agents", "checker", body="检查 ${CLAUDE_PROJECT_DIR} 中的代码。")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"kit-server": {"command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}}}),
        encoding="utf-8",
    )
    (root / "hooks.yaml").write_text(
        yaml.safe_dump(
            {
                "hooks": [
                    {
                        "id": "kit-format",
                        "event": "turn_end",
                        "action": {"type": "command", "command": "python ${CLAUDE_PLUGIN_ROOT}/format.py"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def install_record(home: Path, root: Path, name: str = "kit", **options) -> None:
    records, _ = load_records(home)
    save_records([*records, PluginRecord(name, str(root), **options)], home)


# .claude directories --------------------------------------------------------------------------


def test_claude_directories_are_read_only_when_switched_on(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    write_skill(project / ".claude" / "skills", "cc-skill")
    write_skill(home / ".claude" / "skills", "cc-user")
    write_agent(project / ".claude" / "agents", "cc-agent")
    off = SkillCatalog.load(project, home, builtin=tmp_path / "none")
    assert "cc-skill" not in off.skills and "cc-agent" not in AgentCatalog.load(project, home).agents
    on = SkillCatalog.load(project, home, builtin=tmp_path / "none", claude=True)
    assert on.get("cc-skill").source == "project" and on.get("cc-user").source == "user"
    assert AgentCatalog.load(project, home, claude=True).get("cc-agent").source == "project"


def test_mosscode_directories_win_over_agents_and_claude(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    for base in (".claude", ".agents", ".mosscode"):
        write_skill(project / base / "skills", "same", f"来自 {base}")
    write_agent(project / ".claude" / "agents", "same-agent", body="来自 .claude")
    write_agent(project / ".mosscode" / "agents", "same-agent", body="来自 .mosscode")
    skill = SkillCatalog.load(project, home, builtin=tmp_path / "none", claude=True).get("same")
    assert skill.body == "来自 .mosscode"
    assert [path.parts[-4] for path in skill.overrides] == [".claude", ".agents"]
    agent = AgentCatalog.load(project, home, claude=True).get("same-agent")
    assert agent.prompt == "来自 .mosscode" and agent.overrides[0].parts[-3] == ".claude"


def test_claude_notice_counts_unread_definitions(tmp_path):
    assert claude_notice(tmp_path) is None
    write_agent(tmp_path / ".claude" / "agents", "a")
    write_skill(tmp_path / ".claude" / "skills", "s")
    write_skill(tmp_path / ".claude" / "skills", "t")
    notice = claude_notice(tmp_path)
    assert "1 个子 Agent" in notice and "2 个 Skill" in notice and "claude_directories: true" in notice


def test_claude_directories_setting(tmp_path):
    path = tmp_path / "config.yaml"
    base = {"providers": [{"name": "p", "protocol": "openai-chat-completions", "model": "m", "api_key_env": "X"}]}
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    assert load_settings(path).claude_directories is False
    path.write_text(yaml.safe_dump({**base, "claude_directories": True}), encoding="utf-8")
    assert load_settings(path).claude_directories is True
    path.write_text(yaml.safe_dump({**base, "claude_directories": "yes"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="claude_directories"):
        load_settings(path)


async def test_claude_model_name_runs_on_the_current_model(tmp_path):
    write_agent(project_of(tmp_path) / ".mosscode" / "agents", "cc", "model: sonnet\n", "SUB-CC")
    write_agent(project_of(tmp_path) / ".mosscode" / "agents", "full", "model: claude-opus-5-5\n")

    def script(messages, system, tools):
        if "SUB-CC" in system.stable:
            return response("子任务完成")
        if messages[-1].role == "user" and len(messages) == 1:
            return response(calls=[call("agent", "a1", description="x", prompt="p", agent_type="cc")])
        return response("好")

    provider = ScriptProvider(script)
    conv = conversation(tmp_path, provider)
    await drain(conv.reply("开始"))
    result = tool_results_of(provider.requests[-1])[0]
    assert not result.is_error and "子任务完成" in result.output
    assert any("SUB-CC" in system.stable for _, system, _ in provider.requests)
    assert "model: sonnet 不是这里的 Provider，使用当前模型" in agents_report(conv, "cc")
    assert "不存在" not in agents_report(conv)
    await conv.close()


# Records and plugin directories ---------------------------------------------------------------


def test_source_kinds():
    assert source_kind("https://github.com/a/b.git") == "git"
    assert source_kind("git@github.com:a/b.git") == "git"
    assert source_kind("file:///F:/repo") == "git"
    assert source_kind("F:/dev/plugin") == "local" and source_kind("/home/me/plugin") == "local"


def test_records_round_trip_and_bad_entries_are_skipped(tmp_path):
    records = [PluginRecord("a", "https://x/a.git", ref="v1"), PluginRecord("b", "F:/b", path="sub", enabled=False)]
    save_records(records, tmp_path)
    assert load_records(tmp_path) == (records, [])
    records_path(tmp_path).write_text(
        yaml.safe_dump(
            {
                "plugins": [
                    {"name": "Bad Name", "source": "x"},
                    {"name": "ok", "source": "x", "path": "../escape"},
                    {"name": "dup", "source": "x"},
                    {"name": "dup", "source": "y"},
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded, problems = load_records(tmp_path)
    assert [record.source for record in loaded] == ["x"] and len(problems) == 3


def test_manifest_paths_and_unsupported_components(tmp_path):
    root = tmp_path / "p"
    (root / "extra-skills").mkdir(parents=True)
    (root / "custom-agents").mkdir()
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    (root / "bin").mkdir()
    manifest = {
        "name": "p",
        "version": "1.2.0",
        "description": "测试插件",
        "skills": "./extra-skills",
        "agents": ["./custom-agents", "../outside"],
        "hooks": "./hooks/hooks.json",
        "userConfig": {},
        "author": {"name": "x"},
    }
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    plugin = read_plugin(root, "p", tmp_path)
    assert plugin.version == "1.2.0" and plugin.description == "测试插件"
    assert plugin.skill_dirs == (root / "extra-skills",) and plugin.agents == (root / "custom-agents",)
    assert "hooks/hooks.json" in plugin.unsupported and "bin" in plugin.unsupported
    assert "plugin.json 的 hooks" in plugin.unsupported and "plugin.json 的 userConfig" in plugin.unsupported
    assert not any("author" in item for item in plugin.unsupported)
    assert len(plugin.problems) == 1 and "../outside" in plugin.problems[0]


def test_installed_skips_disabled_and_reports_missing(tmp_path):
    make_plugin(tmp_path / "kit")
    install_record(tmp_path, tmp_path / "kit")
    install_record(tmp_path, tmp_path / "kit", name="off", enabled=False)
    install_record(tmp_path, tmp_path / "gone", name="gone")
    result = installed(tmp_path)
    assert [plugin.name for plugin in result.plugins] == ["kit"]
    assert len(result.notices) == 1 and "moss plugin update gone" in result.notices[0]


# Loading the components -----------------------------------------------------------------------


def test_plugin_skills_commands_and_agents(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    make_plugin(tmp_path / "kit")
    install_record(home, tmp_path / "kit")
    catalog = SkillCatalog.load(project, home, builtin=tmp_path / "none")
    lint = catalog.get("lint")
    assert lint.source == "plugin" and source_label(lint) == "插件 kit"
    assert (tmp_path / "kit").as_posix() + "/lint.py" in lint.body
    assert data_dir("kit", home).as_posix() in lint.body and data_dir("kit", home).is_dir()
    ship = catalog.get("ship")
    assert not ship.model_invocable and ship.user_invocable and ship.mode == "inline"
    assert ship.description == "发布版本" and ship.argument_hint == "<版本>"
    assert ship.allowed_tools == ("execute_command", "read_file")
    assert any("Task" in warning for warning in ship.warnings)
    assert "ship" not in catalog.catalog_section()[0]
    agent = AgentCatalog.load(project, home).get("checker")
    assert agent.source == "plugin" and project.as_posix() in agent.prompt


def test_an_underscore_in_a_command_file_name_becomes_a_hyphen(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    root = make_plugin(tmp_path / "kit")
    (root / "commands" / "clean_gone.md").write_text("# 清理分支\n删除已合并的分支\n", encoding="utf-8")
    (root / "commands" / "Bad.md").write_text("# 不合法\n", encoding="utf-8")
    install_record(home, root)
    catalog = SkillCatalog.load(project, home, builtin=tmp_path / "none")
    assert catalog.get("clean-gone").description == "清理分支"
    assert catalog.get("clean_gone") is None
    assert any("Bad.md" in problem and "_ 视为 -" in problem for problem in catalog.problems)


def test_user_and_project_definitions_override_plugins(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    make_plugin(tmp_path / "kit")
    make_plugin(tmp_path / "later")
    write_skill(tmp_path / "later" / "skills", "lint", "后装的插件")
    install_record(home, tmp_path / "kit")
    install_record(home, tmp_path / "later", name="later")
    assert SkillCatalog.load(project, home, builtin=tmp_path / "none").get("lint").body == "后装的插件"
    write_skill(project / ".mosscode" / "skills", "lint", "项目自己的")
    write_agent(home / ".mosscode" / "agents", "checker", body="用户自己的")
    skill = SkillCatalog.load(project, home, builtin=tmp_path / "none").get("lint")
    assert skill.body == "项目自己的" and len(skill.overrides) == 2
    assert AgentCatalog.load(project, home).get("checker").prompt == "用户自己的"


def test_single_skill_plugin_and_reload_keeps_plugins(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    root = tmp_path / "solo-repo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: solo\ndescription: 单个\n---\n在 ${CLAUDE_PLUGIN_ROOT}\n", encoding="utf-8"
    )
    install_record(home, root, name="solo-plugin")
    catalog = SkillCatalog.load(project, home, builtin=tmp_path / "none")
    assert catalog.get("solo").plugin.name == "solo-plugin"
    assert catalog.reload().get("solo") is not None
    skill, problem = catalog.fresh("solo")
    assert problem is None and root.as_posix() in skill.body


def test_user_config_reference_skips_the_component(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    root = make_plugin(tmp_path / "kit")
    write_skill(root / "skills", "needs-key", "密钥 ${user_config.api_key}")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"keyed": {"command": "x", "env": {"K": "${user_config.key}"}}}}), encoding="utf-8"
    )
    install_record(home, root)
    catalog = SkillCatalog.load(project, home, builtin=tmp_path / "none")
    assert catalog.get("needs-key") is None and any("userConfig" in problem for problem in catalog.problems)
    settings = load_mcp_settings(project, user_path=home / "none.yaml", plugins=installed(home).plugins)
    assert settings.servers == () and any("userConfig" in notice for notice in settings.notices)


def test_plugin_mcp_servers_have_variables_and_lowest_priority(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    root = make_plugin(tmp_path / "kit")
    (root / "mcp.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"own": {"type": "http", "url": "https://example.com/mcp"}}}), encoding="utf-8"
    )
    install_record(home, root)
    plugins = installed(home).plugins
    servers = {
        server.name: server for server in load_mcp_settings(project, user_path=home / "x", plugins=plugins).servers
    }
    kit = servers["kit-server"]
    assert kit.args == (root.as_posix() + "/server.py",)
    assert kit.env["CLAUDE_PLUGIN_ROOT"] == str(root) and kit.env["MOSSCODE_PLUGIN_DATA"] == str(data_dir("kit", home))
    assert servers["own"].type == "http"
    user = tmp_path / "user-mcp.yaml"
    user.write_text(yaml.safe_dump({"mcp_servers": {"kit-server": {"type": "stdio", "command": "mine"}}}), "utf-8")
    replaced = load_mcp_settings(project, user_path=user, plugins=plugins)
    assert {server.name: server for server in replaced.servers}["kit-server"].command == "mine"


def test_plugin_hooks_load_trusted_between_user_and_project(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    root = make_plugin(tmp_path / "kit")
    install_record(home, root)
    (project / ".mosscode").mkdir(parents=True)
    (project / ".mosscode" / "hooks.yaml").write_text(
        yaml.safe_dump(
            {"hooks": [{"id": "kit-format", "event": "turn_end", "action": {"type": "command", "command": "echo x"}}]}
        ),
        encoding="utf-8",
    )
    config = load_hooks(project, home, trust_project=True)
    assert [file.source for file in config.files] == ["user", "plugin", "project", "local"]
    hook = config.hooks[0]
    assert hook.source == "plugin" and hook.plugin.name == "kit"
    assert hook.action.command == f"python {root.as_posix()}/format.py"
    assert hook.plugin.env(project)["CLAUDE_PROJECT_DIR"] == str(project)
    assert any("id 与之前加载的 Hook 重复" in problem for problem in config.problems)
    untrusted = load_hooks(project, home, trust_project=False)
    assert untrusted.files[1].trusted and not untrusted.files[2].trusted


def test_plugin_skill_tool_gets_variables_and_environment(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    root = make_plugin(tmp_path / "kit")
    (root / "skills" / "lint" / "tool.json").write_text(
        json.dumps(
            [
                {
                    "name": "run",
                    "description": "运行",
                    "input_schema": {"type": "object", "properties": {}},
                    "command": ["python", "${CLAUDE_PLUGIN_ROOT}/run.py", "${SKILL_DIR}"],
                }
            ]
        ),
        encoding="utf-8",
    )
    install_record(home, root)
    skill = SkillCatalog.load(project, home, builtin=tmp_path / "none").get("lint")
    tool = SkillTool(project, skill, skill.tools[0])
    assert tool.argv()[1] == root.as_posix() + "/run.py"
