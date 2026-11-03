"""Coordinator mode, a role for the lead — it plans and delegates, members implement.

A role constraint, not a security boundary: execute_command can still write files, and the permission
rules apply as always.
"""

from __future__ import annotations

from typing import Mapping

ENV = "MOSSCODE_COORDINATOR_MODE"
ON_VALUES = ("1", "true", "yes")
BADGE = "[协调]"
STATUS = "协调者模式（配置与环境变量均已开启）"
NO_ENV = f"协调者模式未开启：配置中 coordinator_mode 为 true，但环境变量 {ENV} 没有设为 1、true 或 yes。"
NO_CONFIG = f"协调者模式未开启：已设置环境变量 {ENV}，但配置中 coordinator_mode 不是 true。"
HEADLESS = "协调者模式只用于交互界面，本次运行按普通模式。"
NO_AGENTS = "协调者模式需要工具和 Agent 团队，本次运行按普通模式。"
WITHHELD = "协调者模式下 Lead 不使用 {name}：把实施交给团队成员或子 Agent，或用 execute_command 运行只读检查。"
WRITE_TOOLS = frozenset({"write_file", "edit_file"})

SECTION = """## 协调者模式

你是协调者：负责调查、拆分、派发和验证，不亲自改代码。没有 write_file、edit_file、MCP 工具和 Skill 脚本工具；execute_command 只用于只读的查看和检查。按四个阶段工作：
1. 调查：读代码，或派只读的子 Agent 并行调查，弄清改动范围。子 Agent 只做调查和运行测试，不派它改文件。
2. 综合：把工作拆成任务，用 team_task_create 写清每个任务的范围、完成标准和依赖（blocked_by）。
3. 实施：所有文件改动（哪怕只有一行）都由 team_spawn 创建的成员在自己的工作树中完成，合并时由用户确认。派发后不要自己动手，有问题用 send_message 沟通。
4. 验证：让成员或子 Agent 运行测试；team_merge 合并后，你可以自己运行只读的检查（测试、git log、git diff），再向用户汇报。"""


def env_on(env: Mapping[str, str]) -> bool:
    return env.get(ENV, "").strip().lower() in ON_VALUES


def decide(configured: bool, env: Mapping[str, str], *, interactive: bool) -> tuple[bool, str]:
    """On only with both switches, in the interactive UI; the notice says why not."""
    environment = env_on(env)
    if configured and not environment:
        return False, NO_ENV
    if environment and not configured:
        return False, NO_CONFIG
    if not configured:
        return False, ""
    if not interactive:
        return False, HEADLESS
    return True, ""


def withheld(tool) -> bool:
    """Kept in the registry for sub-agents and members, but not offered to the lead."""
    from .mcp_client import MCPTool
    from .tools.skill import SkillTool
    return tool.name in WRITE_TOOLS or isinstance(tool, (MCPTool, SkillTool))
