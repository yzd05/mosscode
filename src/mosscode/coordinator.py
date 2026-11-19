"""Coordinator mode, a role for the lead — it plans and delegates, members implement.

A role constraint, not a security boundary: execute_command can still write files, and the permission
rules apply as always.
"""

from __future__ import annotations

from collections.abc import Mapping

ENV = "MOSSCODE_COORDINATOR_MODE"
ON_VALUES = ("1", "true", "yes")
BADGE = "[协调]"
STATUS = "协调者模式（配置与环境变量均已开启）"
DYNAMIC_STATUS = "协调者模式（/swarm 已开启）"
SWARM_ON_NOTE = "已开启 Swarm 协调模式（可用 /swarm off 关闭）"
SWARM_OFF_NOTE = "已关闭 Swarm 协调模式，Lead 已恢复完整工具。"
SWARM_UNAVAILABLE = "Swarm 协调模式需要交互界面（TUI 或 ACP）并启用工具与 Agent 团队。"
NO_ENV = f"协调者模式未开启：配置中 coordinator_mode 为 true，但环境变量 {ENV} 没有设为 1、true 或 yes。"
NO_CONFIG = f"协调者模式未开启：已设置环境变量 {ENV}，但配置中 coordinator_mode 不是 true。"
HEADLESS = "协调者模式只用于交互界面，本次运行按普通模式。"
NO_AGENTS = "协调者模式需要工具和 Agent 团队，本次运行按普通模式。"
WITHHELD = "协调者模式下 Lead 不使用 {name}：把实施交给团队成员或子 Agent，或用 execute_command 运行只读检查。"
WRITE_TOOLS = frozenset({"write_file", "edit_file"})

SECTION = """## 协调者模式

你是协调者（Lead）：负责调查、无重叠切分、派发编组、验收与合并，不亲自改代码，派发后不要自己动手。没有 write_file、edit_file、MCP 工具和 Skill 脚本工具；execute_command 只用于只读的查看和检查。按黑板 Swarm 四步规程工作：
1. **调查与无重叠切分**：先用只读工具摸清接口与边界（或派只读子 Agent 并行调查；子 Agent 只做调查和运行测试，不派它改文件），将文件修改划分为**严格互不相交的文件集合**，严禁两名成员同时编辑同一文件。
2. **一键建队与黄金编组**：`team_create` 建团队 + `team_task_create` 建任务（标注范围、完成标准与 `blocked_by` 依赖）+ 同一轮并发 `team_spawn` 拉起各独立工作树 `worker`、合流 `integrator` 以及只读对抗审查员 `verifier`（传 `role: "verifier"`）。
3. **黑板 `#seq` 定序接力**：指示成员用 `post_board` 配合 `@integrator` / `@verifier` 点对点交接并引用 `#seq` 编号（接收方用 `read_board(seqs=[...])` 查看原文，不要由 Lead 传话搬运），全部审查通过后再点名 `@lead`。
4. **最终合流与收尾**：收到 `@lead` 验收通过后，按任务依赖顺序执行 `team_merge`（每次由用户确认），运行最终只读检查（测试、`git log`、`git diff`），并在黑板发一条不带 `@` 的 `[done] <摘要>` 收尾后向用户汇报。"""

SWARM_SECTION = SECTION
SWARM_TURN_PROMPT = (
    "请以 Swarm 协调者身份处理以下任务：先用只读工具摸清边界并按严格不重叠的文件集合切分任务，"
    '再用 team_create / team_task_create / team_spawn 组建含 worker、integrator 与只读 verifier（role: "verifier"）的团队，'
    "让成员在黑板上通过 post_board / read_board 按 #seq 点对点接力，待 @lead 验收通过后执行 team_merge 并以 [done] 收尾。\n\n"
)


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
