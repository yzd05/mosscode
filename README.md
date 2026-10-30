# MossCode

轻量级终端 coding agent，参考 Claude Code 与 Pi 的设计：单一 Agent Loop，六个本地工具（读、写、改、命令、glob、grep）加 MCP 外部工具，四档权限与只读计划模式，支持 Anthropic Messages、OpenAI Responses 和 Chat Completions 三种协议。代码量小，便于学习和修改。

## 安装

需要 Python 3.12。在 PowerShell 中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

需要复现锁定的依赖版本时，先安装 `requirements-lock.txt`，再执行 `pip install --no-deps -e .`。搜索优先使用 PATH 中的 ripgrep（`rg`），没有时自动退回内置实现；Windows 上建议安装 Git for Windows，命令工具会优先使用 Git Bash。

## 配置

### 模型

```powershell
New-Item -ItemType Directory -Force .mosscode | Out-Null
Copy-Item config.example.yaml .mosscode\config.yaml
```

编辑 `.mosscode/config.yaml`，填写实际的模型名称、协议和可选的 `base_url`。未指定 `--config` 时依次查找 `./.mosscode/config.yaml`、`./config.yaml`（旧位置）、`~/.mosscode/config.yaml`，第一个存在的生效。用户级配置放在 `~/.mosscode/config.yaml`，就可以在任意项目目录启动。

```yaml
providers:
  - name: claude
    protocol: anthropic-messages      # 或 openai-responses、openai-chat-completions
    model: replace-with-service-model
    api_key_env: ANTHROPIC_API_KEY    # 环境变量名；也可以用 api_key 直接填写
    max_output_tokens: 32000          # 单次回复的输出上限，默认 16384
    context_window: 200000            # 模型的上下文窗口；未配置时按 128K 估算
    # base_url: https://your-gateway.example/v1
```

凭据推荐放在环境变量中（`$env:ANTHROPIC_API_KEY = '你的凭据'`），不要提交到仓库，也不用发送到聊天。兼容服务、多 Provider、重试和协议选项见 [模型提供商](docs/reference/providers.md)。

常用的顶层选项：

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `default_provider` | 无 | 启动时直接使用的 Provider；未设置且有多项时启动时选择 |
| `max_iterations` | 200 | 单个任务最多的模型请求次数 |
| `shell` | `auto` | Windows 优先 Git Bash，找不到时用 PowerShell；可设 `bash`、`powershell` 或 bash.exe 路径 |
| `system_prompt` | 默认提示 | 填写后完整替换默认系统提示 |
| `auto_compact` | `true` | 接近上下文窗口时自动压缩较早的对话，见 [上下文管理](docs/reference/context.md) |
| `persist_sessions` | `true` | 把会话实时存档，用于恢复，见 [会话存档与恢复](docs/reference/sessions.md) |
| `memory` | `true` | 记忆笔记：索引注入系统提示、`memory` 工具和后台提取，见 [记忆笔记](docs/reference/memory.md) |
| `auto_memory` | `true` | 任务完成后在后台提取值得记住的内容；`false` 时只保留 `memory` 工具 |
| `memory_provider` | 当前 Provider | 后台提取使用的 Provider 名称，可以指定较便宜的模型 |
| `worktree_links` | `[]` | 链接进每个 git 工作树的路径（相对仓库根），如 `node_modules`、`.venv`，见 [Git 工作树](docs/reference/worktree.md#初始化) |
| `worktree_stale_hours` | `24` | 子 Agent 留下的无改动工作树多少小时后自动清理；`0` 关闭 |

### 权限、MCP、项目指令、Skill、Hook、子 Agent 与工作树

| 文件 | 用途 | 说明 |
| --- | --- | --- |
| `~/.mosscode/permissions.yaml`、`.mosscode/permissions.yaml`、`.mosscode/permissions.local.yaml` | 权限规则（用户、项目、本机） | [工具权限](docs/reference/permissions.md)，示例 [permissions.example.yaml](permissions.example.yaml) |
| `~/.mosscode/mcp.yaml`、`.mosscode/mcp.yaml` | MCP Server | [MCP 外部工具](docs/reference/mcp.md)，示例 [mcp.example.yaml](mcp.example.yaml) |
| `AGENTS.md`（或 `CLAUDE.md`）、`.mosscode/AGENTS.md`、`~/.mosscode/AGENTS.md` | 项目指令，追加到系统提示；单独一行的 `@路径` 引用其他文件 | [系统提示与缓存](docs/reference/prompts.md#项目指令agentsmd) |
| `~/.mosscode/skills/<名称>/`、`.mosscode/skills/<名称>/` | Skill：`SKILL.md` 规程和可选的 `tool.json` 脚本工具；项目级覆盖用户级和内置 | [Skill](docs/reference/skills.md) |
| `~/.mosscode/hooks.yaml`、`.mosscode/hooks.yaml`、`.mosscode/hooks.local.yaml` | Hook：在会话、轮次、请求和工具调用前后执行命令、注入提示、发送 HTTP 请求或拦截工具调用；项目级文件需确认后启用 | [Hook](docs/reference/hooks.md) |
| `~/.mosscode/agents/<名称>.md`、`.mosscode/agents/<名称>.md` | 子 Agent 定义：frontmatter 写名称、描述、工具范围、模型、轮数和权限档位，正文是系统提示词；项目级覆盖用户级和内置的 explore、plan、general | [子 Agent](docs/reference/subagents.md) |
| `.worktreeinclude`（仓库根） | 用 `.gitignore` 语法列出创建工作树时要复制过去的被忽略文件，如 `.env` | [Git 工作树](docs/reference/worktree.md#初始化) |

## 使用

```powershell
.\.venv\Scripts\python.exe -m mosscode                    # 交互界面
.\.venv\Scripts\python.exe -m mosscode --provider claude  # 指定 Provider
.\.venv\Scripts\python.exe -m mosscode --list-providers
.\.venv\Scripts\python.exe -m mosscode -c                 # 恢复本项目最近的会话
.\.venv\Scripts\python.exe -m mosscode --resume           # 启动后从列表中选择会话
```

用自然语言描述任务，例如“读取 README.md，说明项目入口”，模型自行选择工具。界面以内联方式显示在终端中，完成的内容进入终端原生滚动历史。

| 按键 | 作用 |
| --- | --- |
| Enter | 发送；运行中发送则排队，在下一轮请求前并入对话 |
| Shift+Enter、Ctrl+J、行尾 `\` | 换行 |
| ↑ / ↓ | 浏览输入历史；命令提示打开时选择提示 |
| Tab | 补全命令和参数 |
| Shift+Tab | 切换权限档位 default → acceptEdits → plan |
| Esc / Ctrl+C | 取消生成；有排队消息时第一次 Esc 只把排队消息取回输入框，任务继续；审批时 Esc 拒绝当前调用 |
| Ctrl+Q | 退出 |

| 命令 | 行为 |
|------|------|
| `/help [命令]` | 显示命令与快捷键；带命令名时显示该命令的用法和别名 |
| `/clear` | 开始新会话：清空屏幕与终端滚动历史、模型上下文和 Token 统计；权限档位、会话授权和 MCP 连接保留，AGENTS.md 重新加载 |
| `/compact [说明]` | 压缩上下文：较早的对话换成摘要，近期消息和用户原话保留；说明用于指定摘要侧重点 |
| `/resume` | 列出本项目最近的会话，↑ / ↓ 选择、Enter 恢复、Esc 收起 |
| `/resume ID` | 直接恢复指定会话 |
| `/session` | 显示当前会话的 ID、存档路径、标题和消息数 |
| `/memory` | 列出已加载的项目指令文件，以及用户级和项目级的记忆笔记及目录 |
| `/memory 名称` | 显示该笔记的路径和全文 |
| `/model` | 在输入框下方列出配置的提供商（当前项带 ✓），↑ / ↓ 选择、Enter 切换、Esc 收起 |
| `/model 名称` | 直接切换到指定提供商；对话历史保留 |
| `/plan` | 只读调查与规划，模型提交计划经批准后执行 |
| `/plan 任务` | 切入计划模式并立即调查、生成计划 |
| `/do [任务]` | 退出计划模式，回到进入前的档位；带任务时随后发给模型 |
| `/permissions`（别名 `/permission`） | 查看权限档位说明 |
| `/permissions 档位` | 切换到 default、acceptEdits、plan 或 bypassPermissions |
| `/mcp` | 列出 MCP Server 的状态（连接中、已连接及工具数、失败原因）与配置问题 |
| `/mcp 名称` | 列出该 Server 的工具，只读工具标注 `[只读]` |
| `/status` | 显示模型、权限档位、上下文、本会话用量、会话、MCP、记忆、Hook 和子 Agent 的状态 |
| `/skills`（别名 `/skill`） | 列出全部 Skill 及其来源、模式和描述；`/skills 名称` 查看详情，`/skills reload` 重新扫描 |
| `/hooks`（别名 `/hook`） | 列出 Hook 及其来源、条件、状态和最近的执行记录；`/hooks id` 查看详情，`/hooks reload` 重新加载 |
| `/agents`（别名 `/agent`） | 列出子 Agent 定义及其来源和描述；`/agents 名称` 查看实际可用的工具和有效权限档位，`/agents reload` 重新扫描 |
| `/tasks`（别名 `/task`） | 列出本会话的子 Agent 任务及其状态、用时和 Token；`/tasks id` 查看详情，`/tasks stop id` 停止 |
| `/worktree`（别名 `/worktrees`） | 列出 MossCode 管理的 git 工作树及其状态；`/worktree create 名称` 创建，`/worktree remove 名称 [--discard]` 删除，`/worktree clean` 清理过期的临时工作树 |
| `/<Skill 名> [参数]` | 运行 Skill，如内置的 `/commit`、`/review`（在独立上下文中审查未提交的改动）、`/test` |
| `/exit`（别名 `/quit`） | 退出并清理资源 |

内置命令都是本地命令，不请求模型；Skill 命令把 Skill 的规程发给模型，补全菜单中带 `[skill]` 标记。命令名不区分大小写。`/etc/hosts 怎么改` 这类不像命令的文字照常发给模型；要发送恰好像命令的文字，在开头再加一个 `/`，如 `//api 怎么设计`。解析、补全和添加命令的方法见 [命令](docs/reference/commands.md)。

界面、输入排队、状态栏与配色的细节见 [终端界面](docs/reference/interface.md)。

### 会话与记忆

会话实时存档到 `~/.mosscode/projects/<项目键>/sessions/`，退出或崩溃后用 `-c`、`--resume [ID]` 或 `/resume` 接着对话，中断时未完成的工具调用会被标记出来。存档保留 30 天。见 [会话存档与恢复](docs/reference/sessions.md)。

对模型说“记住……”时，它用 `memory` 工具写一条笔记；任务完成后，后台还会提取你纠正过的做法和偏好（只依据对话，不依据工具输出）。笔记是 `~/.mosscode/memory/`（用户级）和 `~/.mosscode/projects/<项目键>/memory/`（项目级）中的 Markdown 文件，下次启动时索引进入系统提示。见 [记忆笔记](docs/reference/memory.md)。

### 权限档位

没有匹配规则时按下表判断；规则中的 deny 在任何档位都生效。

| 档位 | 只读工具与只读命令 | 文件写入/编辑 | 其他命令 |
| --- | --- | --- | --- |
| `default` | 允许 | 询问 | 询问 |
| `acceptEdits` | 允许 | 允许 | 询问 |
| `plan` | 允许 | 不可用 | 不可用 |
| `bypassPermissions` | 允许 | 允许 | 允许 |

审批时可选本次允许、本会话允许或永久允许（保存到 `.mosscode/permissions.local.yaml`）。删除根目录、格式化磁盘等灾难级命令一律拒绝；`git reset --hard`、`git push --force`、`rm -rf` 等高风险命令每次都要确认。路径检查是应用层限制，不是系统沙箱。完整说明见 [工具权限](docs/reference/permissions.md)。

**计划模式**：`/plan 任务` 或 Shift+Tab 进入。模型只能读取和运行只读命令，调查后用 `exit_plan_mode` 提交计划；批准后在同一任务内继续执行。见 [工具与 Agent Loop](docs/reference/tools.md#计划模式)。

### 无界面模式

```powershell
.\.venv\Scripts\python.exe -m mosscode -p "列出 src 下的模块"
.\.venv\Scripts\python.exe -m mosscode -p "解释项目入口" --show-usage
.\.venv\Scripts\python.exe -m mosscode -p "列出 src 下的模块" --output-format json
```

`-p`（`--prompt`）运行一个任务后退出，不等待审批（可以加 `--resume ID` 或 `-c` 在已有会话上继续）：需要确认的调用返回 approval_required，模型据此调整。`-p "/test 只测 cli"` 这类 Skill 命令展开后执行（fork 方式的 Skill 在独立上下文中运行）；本地命令和未知命令以退出码 2 结束。`--output-format` 可选 `text`、`json`、`stream-json`；json 格式中 `is_error` 为真时退出码为 1，配置或连接错误在任何格式下都以 1 退出。未经确认的项目级 Hook 在 `-p` 中不启用（stderr 有提示），`--trust-project-hooks` 只在本次信任，`--no-hooks` 不加载任何 Hook。模型启动的后台子 Agent 在 `-p` 中照常运行：回复结束后等待它们完成，每个结果开启新的一轮，全部结束后才退出；stderr 提示任务的开始与结束。格式说明见 [模型提供商](docs/reference/providers.md#无界面模式与输出格式)。

## 架构

```
cli.py ──► tui.py（Textual 界面）或 -p（无界面）
             └──► conversation.py（启动上下文与 MCP、对外接口）
                    └──► agent.py（Agent Loop）
                           ├──► compaction.py、context.py（上下文估算与压缩）
                           ├──► session.py（会话存档与恢复）、memory*.py（记忆笔记）
                           ├──► providers.py（三种协议、重试、缓存标记）
                           ├──► prompts.py（系统提示、环境补充、模式提醒）
                           └──► tools/（注册、校验、授权、执行）
                                  ├──► permissions.py、security.py（档位、规则、命令风险）
                                  ├──► mcp_client.py、mcp_config.py（MCP）
                                  ├──► skills/、tools/skill.py（Skill 与专属工具）
                                  ├──► hooks/（生命周期 Hook、工具调用拦截）
                                  └──► agents/、tools/agent.py（子 Agent 与后台任务）
                                         └──► worktree/（git 工作树隔离）
```

一次任务：Agent Loop 用“稳定系统提示 + 历史 + 本次环境补充”请求模型，把流式事件交给界面；回复中的工具调用经校验和授权后执行（相邻只读调用最多 4 个并发），纯文本结果写回历史，再请求模型，直到回复不再调用工具或遇到停止条件（取消、轮数上限、连续未知工具等）。TUI 与 `-p` 共用 `Conversation`；在 Python 中可连续调用 `Conversation.ask()` 保持多轮历史，结束后 `await conversation.close()`。

| 模块 | 职责 |
| --- | --- |
| `agent.py` | Agent Loop：请求、工具执行、排队消息、停止条件、计划模式、压缩时机 |
| `context.py`、`compaction.py`、`tool_budget.py` | 上下文估算与阈值、摘要压缩、大结果存盘 |
| `session.py`、`serialization.py`、`paths.py` | 会话存档、重放与修复、锁、清理；项目键 |
| `memory.py`、`memory_extract.py`、`tools/memory.py` | 记忆笔记的存储、索引与校验，后台提取，memory 工具 |
| `providers.py` | Anthropic Messages、OpenAI Responses、Chat Completions 适配，首事件前重试，缓存断点 |
| `prompts.py` | 七个固定模块、工具清单、`<environment>` 与 `<system-reminder>` 补充、AGENTS.md、git 快照 |
| `tools/` | `files.py`、`search.py`、`process.py`、`plan.py` 等工具；`execution.py` 分批并发；`base.py` 注册与执行边界 |
| `permissions.py`、`security.py` | 档位、分层规则、审批请求、只读命令白名单、危险命令分级、项目路径检查 |
| `mcp_client.py`、`mcp_config.py` | MCP 配置、后台连接与发现、工具适配 |
| `skills/`、`tools/skill.py` | Skill 扫描与覆盖、目录与 `<active-skills>`、`load_skill` 和专属脚本工具；`skills/builtin/` 为内置 Skill |
| `hooks/` | Hook 文件读取与信任、条件、四种动作、按事件执行与日志；接入工具执行、Agent Loop 和会话 |
| `agents/`、`tools/agent.py` | 子 Agent 定义的加载与覆盖、定义式与 Fork 的运行时、后台任务管理与通知；`agent`、`task_output`、`task_stop` 工具；`agents/builtin/` 为内置定义 |
| `worktree/` | git 工作树的命名、创建与初始化（链接、复制）、改动检查、安全删除、元数据与自动清理 |
| `tui.py`、`appearance.py`、`scrollback.py` | 界面、命令与提示、原生滚动历史 |
| `config.py`、`cli.py` | 配置加载与命令行入口 |

### 文档

| 主题 | 文档 |
| --- | --- |
| 当前行为的完整说明 | [界面](docs/reference/interface.md)、[命令](docs/reference/commands.md)、[工具与 Agent Loop](docs/reference/tools.md)、[权限](docs/reference/permissions.md)、[MCP](docs/reference/mcp.md)、[模型提供商](docs/reference/providers.md)、[系统提示与缓存](docs/reference/prompts.md)、[上下文管理](docs/reference/context.md)、[会话存档与恢复](docs/reference/sessions.md)、[记忆笔记](docs/reference/memory.md)、[Skill](docs/reference/skills.md)、[Hook](docs/reference/hooks.md)、[子 Agent](docs/reference/subagents.md)、[Git 工作树](docs/reference/worktree.md) |
| 设计评审 | [对照 Claude Code 与 Pi 的完善建议](docs/research/claude-code-pi-gap-review.md) |
| 各阶段规格与验证 | `docs/specs/`：`chapter-*` 为各章，`refinement-*` 为完善阶段 1–6，每个目录含 `spec.md` 与 `validation.md` |
| 人工验收 | [完善阶段 1–6、上下文管理与第九至十四章验收指南](docs/acceptance.md)：自动测试、离线检查和真实模型用例，验收项目由 `scripts/acceptance_setup.py` 生成 |

### 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py   # Windows 真实控制台烟测，需在交互式终端运行
.\.venv\Scripts\python.exe scripts/verify_resize.py            # 窗口宽度变化后的重绘检查，会弹出并自动关闭一个控制台窗口
.\.venv\Scripts\python.exe scripts/verify_keys.py              # 经控制台输入队列检查换行、历史和 /clear，同样弹出窗口
```

测试使用模拟模型、模拟 SSE 和无界面 TUI，不读取 API 配置，也不发送请求；真实服务的联调需要自行配置。界面预览脚本见 [终端界面](docs/reference/interface.md#预览与烟测脚本)。

目前不支持网络限制、资源配额和审计日志。
