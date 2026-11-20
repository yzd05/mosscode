"""The built-in slash commands. Handlers reach the screen only through ctx.ui."""

from __future__ import annotations

from .builtin_cmds.automation import GOAL_CLEAR_WORDS as GOAL_CLEAR_WORDS
from .builtin_cmds.automation import GOAL_ENDED as GOAL_ENDED
from .builtin_cmds.automation import GOAL_USAGE as GOAL_USAGE
from .builtin_cmds.automation import LOOP_DETAILS as LOOP_DETAILS
from .builtin_cmds.automation import LOOP_HEADLESS_NOTICE as LOOP_HEADLESS_NOTICE
from .builtin_cmds.automation import WORKTREE_USAGE as WORKTREE_USAGE
from .builtin_cmds.automation import _ensure_loops as _ensure_loops
from .builtin_cmds.automation import _goal as _goal
from .builtin_cmds.automation import _goal_reads as _goal_reads
from .builtin_cmds.automation import _loop as _loop
from .builtin_cmds.automation import _loop_names as _loop_names
from .builtin_cmds.automation import _when as _when
from .builtin_cmds.automation import _worktree as _worktree
from .builtin_cmds.automation import _worktree_names as _worktree_names
from .builtin_cmds.automation import _worktree_reads as _worktree_reads
from .builtin_cmds.automation import clean_text as clean_text
from .builtin_cmds.automation import goal_report as goal_report
from .builtin_cmds.automation import loop_report as loop_report
from .builtin_cmds.automation import worktree_manager as worktree_manager
from .builtin_cmds.automation import worktree_report as worktree_report
from .builtin_cmds.common import CLEARED as CLEARED
from .builtin_cmds.common import DEFAULT_MUTED_COLOR as DEFAULT_MUTED_COLOR
from .builtin_cmds.common import MODE_CHOICES as MODE_CHOICES
from .builtin_cmds.common import PERMISSION_HELP as PERMISSION_HELP
from .builtin_cmds.common import PLAN_ENTERED as PLAN_ENTERED
from .builtin_cmds.common import TASK_OPTIONS as TASK_OPTIONS
from .builtin_cmds.common import USAGE_WIDTH as USAGE_WIDTH
from .builtin_cmds.common import _first_line as _first_line
from .builtin_cmds.common import _help as _help
from .builtin_cmds.common import _muted as _muted
from .builtin_cmds.common import choice_row as choice_row
from .builtin_cmds.common import confirm_tasks as confirm_tasks
from .builtin_cmds.common import dispatch as dispatch
from .builtin_cmds.common import help_text as help_text
from .builtin_cmds.common import run_skill as run_skill
from .builtin_cmds.common import typed as typed
from .builtin_cmds.extensions import ACTION_LABELS as ACTION_LABELS
from .builtin_cmds.extensions import OUTCOME_LABELS as OUTCOME_LABELS
from .builtin_cmds.extensions import TRUST_OPTIONS as TRUST_OPTIONS
from .builtin_cmds.extensions import _agent_names as _agent_names
from .builtin_cmds.extensions import _agents as _agents
from .builtin_cmds.extensions import _hook_names as _hook_names
from .builtin_cmds.extensions import _hooks as _hooks
from .builtin_cmds.extensions import _mcp as _mcp
from .builtin_cmds.extensions import _plugin_names as _plugin_names
from .builtin_cmds.extensions import _plugins as _plugins
from .builtin_cmds.extensions import _server_names as _server_names
from .builtin_cmds.extensions import _skill_names as _skill_names
from .builtin_cmds.extensions import _skills as _skills
from .builtin_cmds.extensions import _task_names as _task_names
from .builtin_cmds.extensions import _tasks as _tasks
from .builtin_cmds.extensions import _todo as _todo
from .builtin_cmds.extensions import agents_line as agents_line
from .builtin_cmds.extensions import agents_report as agents_report
from .builtin_cmds.extensions import hooks_line as hooks_line
from .builtin_cmds.extensions import hooks_report as hooks_report
from .builtin_cmds.extensions import mcp_report as mcp_report
from .builtin_cmds.extensions import offer_trust as offer_trust
from .builtin_cmds.extensions import skills_report as skills_report
from .builtin_cmds.extensions import tasks_report as tasks_report
from .builtin_cmds.extensions import todo_report as todo_report
from .builtin_cmds.extensions import trust_text as trust_text
from .builtin_cmds.runtime import CATALOG_FIELDS as CATALOG_FIELDS
from .builtin_cmds.runtime import MODEL_DETAILS as MODEL_DETAILS
from .builtin_cmds.runtime import SANDBOX_OFF_OPTIONS as SANDBOX_OFF_OPTIONS
from .builtin_cmds.runtime import THINKING_CHOICES as THINKING_CHOICES
from .builtin_cmds.runtime import THINKING_HELP as THINKING_HELP
from .builtin_cmds.runtime import _context_line as _context_line
from .builtin_cmds.runtime import _do as _do
from .builtin_cmds.runtime import _image_line as _image_line
from .builtin_cmds.runtime import _mcp_line as _mcp_line
from .builtin_cmds.runtime import _model as _model
from .builtin_cmds.runtime import _model_line as _model_line
from .builtin_cmds.runtime import _permissions as _permissions
from .builtin_cmds.runtime import _plan as _plan
from .builtin_cmds.runtime import _provider_names as _provider_names
from .builtin_cmds.runtime import _sandbox as _sandbox
from .builtin_cmds.runtime import _status as _status
from .builtin_cmds.runtime import _thinking as _thinking
from .builtin_cmds.runtime import _thinking_line as _thinking_line
from .builtin_cmds.runtime import _usage_line as _usage_line
from .builtin_cmds.runtime import _web_line as _web_line
from .builtin_cmds.runtime import resolve_model_target as resolve_model_target
from .builtin_cmds.runtime import status_report as status_report
from .builtin_cmds.runtime import thinking_detail as thinking_detail
from .builtin_cmds.session import REWIND_ACTIONS as REWIND_ACTIONS
from .builtin_cmds.session import REWIND_LISTED as REWIND_LISTED
from .builtin_cmds.session import REWIND_TALK as REWIND_TALK
from .builtin_cmds.session import _clear as _clear
from .builtin_cmds.session import _compact as _compact
from .builtin_cmds.session import _fork_session as _fork_session
from .builtin_cmds.session import _memory as _memory
from .builtin_cmds.session import _note_names as _note_names
from .builtin_cmds.session import _rename as _rename
from .builtin_cmds.session import _resume as _resume
from .builtin_cmds.session import _rewind as _rewind
from .builtin_cmds.session import _session as _session
from .builtin_cmds.session import extraction_state as extraction_state
from .builtin_cmds.session import memory_report as memory_report
from .builtin_cmds.session import open_sessions as open_sessions
from .builtin_cmds.session import rewind_report as rewind_report
from .builtin_cmds.session import session_report as session_report
from .builtin_cmds.teams import DELETE_OPTIONS as DELETE_OPTIONS
from .builtin_cmds.teams import SWARM_DETAILS as SWARM_DETAILS
from .builtin_cmds.teams import SWARM_USAGE as SWARM_USAGE
from .builtin_cmds.teams import TEAM_DETAILS as TEAM_DETAILS
from .builtin_cmds.teams import TEAM_USAGE as TEAM_USAGE
from .builtin_cmds.teams import _exit as _exit
from .builtin_cmds.teams import _swarm as _swarm
from .builtin_cmds.teams import _swarm_names as _swarm_names
from .builtin_cmds.teams import _swarm_reads as _swarm_reads
from .builtin_cmds.teams import _team as _team
from .builtin_cmds.teams import _team_names as _team_names
from .builtin_cmds.teams import _team_reads as _team_reads
from .builtin_cmds.teams import swarm_report as swarm_report
from .builtin_cmds.teams import team_report as team_report
from .commands import Command, CommandRegistry
from .commands import CommandContext as CommandContext
from .commands import Parsed as Parsed
from .permissions import MODE_LABELS as MODE_LABELS
from .reasoning import LEVEL_LABELS

BUILTIN = CommandRegistry(
    (
        Command(
            "help",
            "查看命令和快捷键",
            "/help [命令]",
            "local",
            argument="optional",
            argument_hint="[命令]",
            handler=_help,
        ),
        Command("clear", "开始新会话：清空模型上下文和 Token 统计", "/clear", "local", handler=_clear),
        Command(
            "compact",
            "压缩上下文：较早的对话换成摘要，近期消息保留",
            "/compact [说明]",
            "local",
            argument="optional",
            argument_hint="[说明]",
            handler=_compact,
        ),
        Command(
            "resume",
            "恢复本项目之前的会话（支持按关键词或 ID 筛选）",
            "/resume [ID|关键词]",
            "local",
            argument="optional",
            argument_hint="[ID|关键词]",
            handler=_resume,
        ),
        Command(
            "rewind",
            "把文件或对话退回到之前某条消息发出前的状态",
            "/rewind",
            "local",
            aliases=("checkpoint",),
            handler=_rewind,
        ),
        Command("session", "查看当前会话的 ID、存档位置和消息数", "/session", "local", handler=_session),
        Command(
            "rename",
            "重命名当前会话的标题",
            "/rename <标题>",
            "local",
            argument="required",
            argument_hint="<标题>",
            handler=_rename,
        ),
        Command(
            "fork",
            "从当前会话分出新分支继续探索，原会话保留",
            "/fork [标题]",
            "local",
            argument="optional",
            argument_hint="[标题]",
            handler=_fork_session,
        ),
        Command(
            "memory",
            "查看已加载的项目指令和记忆笔记",
            "/memory [名称]",
            "local",
            argument="optional",
            argument_hint="[名称]",
            handler=_memory,
            complete=_note_names,
        ),
        Command(
            "model",
            "切换提供商与模型，对话历史保留",
            "/model [名称|模型]",
            "local",
            argument="optional",
            argument_hint="[名称|模型]",
            handler=_model,
            complete=_provider_names,
            details=MODEL_DETAILS,
        ),
        Command(
            "thinking",
            "查看或设置本会话的思考强度",
            "/thinking [档位]",
            "local",
            argument="optional",
            argument_hint="[档位]",
            handler=_thinking,
            complete=lambda ctx: [(value, LEVEL_LABELS[value]) for value in THINKING_CHOICES],
        ),
        Command(
            "plan",
            "只读调查和规划，提交计划经批准后执行",
            "/plan [任务]",
            "local",
            argument="optional",
            argument_hint="[任务]",
            handler=_plan,
        ),
        Command(
            "do",
            "退出计划模式，回到进入前的权限档位",
            "/do [任务]",
            "local",
            argument="optional",
            argument_hint="[任务]",
            handler=_do,
        ),
        Command(
            "permissions",
            "查看或切换工具权限档位",
            "/permissions [档位]",
            "local",
            aliases=("permission",),
            argument="optional",
            argument_hint="[档位]",
            handler=_permissions,
            complete=lambda ctx: MODE_CHOICES,
        ),
        Command(
            "status", "查看模型、权限、上下文、用量和连接状态", "/status", "local", handler=_status, while_running=True
        ),
        Command(
            "sandbox",
            "查看命令沙箱的边界和最近的拦截，或在本会话中打开、关闭",
            "/sandbox [on|off]",
            "local",
            argument="optional",
            argument_hint="[on|off]",
            handler=_sandbox,
            complete=lambda ctx: [("on", "打开沙箱"), ("off", "关闭沙箱（需确认）")],
        ),
        Command(
            "mcp",
            "查看 MCP Server 的连接状态与工具",
            "/mcp [名称]",
            "local",
            argument="optional",
            argument_hint="[名称]",
            handler=_mcp,
            complete=_server_names,
        ),
        Command(
            "skills",
            "查看、卸载、重新扫描 Skill",
            "/skills [名称|reload]",
            "local",
            aliases=("skill",),
            argument="optional",
            argument_hint="[名称|reload]",
            handler=_skills,
            complete=_skill_names,
            details="/skills unload <名称>：卸载已激活的 Skill 及其专属工具，释放 <active-skills> 上下文",
        ),
        Command(
            "hooks",
            "查看、重新加载 Hook",
            "/hooks [id|reload]",
            "local",
            aliases=("hook",),
            argument="optional",
            argument_hint="[id|reload]",
            handler=_hooks,
            complete=_hook_names,
        ),
        Command(
            "agents",
            "查看、重新加载子 Agent 定义",
            "/agents [名称|reload]",
            "local",
            aliases=("agent",),
            argument="optional",
            argument_hint="[名称|reload]",
            handler=_agents,
            complete=_agent_names,
        ),
        Command(
            "plugins",
            "查看已安装的插件",
            "/plugins [名称]",
            "local",
            aliases=("plugin",),
            argument="optional",
            argument_hint="[名称]",
            handler=_plugins,
            complete=_plugin_names,
        ),
        Command(
            "tasks",
            "查看、停止本会话的子 Agent、后台命令与定时巡检任务",
            "/tasks [id|stop id]",
            "local",
            aliases=("task",),
            argument="optional",
            argument_hint="[id|stop id]",
            handler=_tasks,
            complete=_task_names,
            while_running=True,
        ),
        Command("todo", "查看模型当前的完整任务清单", "/todo", "local", handler=_todo, while_running=True),
        Command(
            "goal",
            "设置目标，自动续跑到达成；不带参数查看或继续",
            "/goal [条件|clear]",
            "local",
            argument="optional",
            argument_hint="[条件|clear]",
            handler=_goal,
            while_running=_goal_reads,
            details="/goal <条件>：设置目标并立即开始，每轮结束后另一次模型调用判断是否达成，未达成时自动继续\n"
            "/goal：查看目标；目标已暂停时继续\n/goal clear（stop、off）：清除目标；/clear 也会清除",
        ),
        Command(
            "loop",
            "按固定间隔在会话空闲时自动执行提示词或斜杠命令",
            # Short enough to keep the /help column at its usual width; LOOP_DETAILS lists list/stop.
            "/loop [5m] <提示词>",
            "local",
            argument="optional",
            argument_hint="[5m] <提示词>",
            handler=_loop,
            complete=_loop_names,
            while_running=True,
            details=LOOP_DETAILS,
        ),
        Command(
            "worktree",
            "查看、创建、删除 git 工作树",
            "/worktree [子命令]",
            "local",
            aliases=("worktrees",),
            argument="optional",
            argument_hint="[子命令]",
            handler=_worktree,
            complete=_worktree_names,
            while_running=_worktree_reads,
        ),
        Command(
            "team",
            "查看、停止、删除 Agent 团队",
            "/team [子命令]",
            "local",
            aliases=("teams",),
            argument="optional",
            argument_hint="[子命令]",
            handler=_team,
            complete=_team_names,
            while_running=_team_reads,
            details=TEAM_DETAILS,
        ),
        Command(
            "swarm",
            "开启黑板 Swarm 协调模式并派发多 Agent 团队任务",
            "/swarm [on|off|任务]",
            "local",
            argument="optional",
            argument_hint="[on|off|任务]",
            handler=_swarm,
            complete=_swarm_names,
            while_running=_swarm_reads,
            details=SWARM_DETAILS,
        ),
        Command("exit", "退出 MossCode", "/exit", "local", aliases=("quit",), handler=_exit),
    )
)
