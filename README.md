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

### 权限、MCP 与项目指令

| 文件 | 用途 | 说明 |
| --- | --- | --- |
| `~/.mosscode/permissions.yaml`、`.mosscode/permissions.yaml`、`.mosscode/permissions.local.yaml` | 权限规则（用户、项目、本机） | [工具权限](docs/reference/permissions.md)，示例 [permissions.example.yaml](permissions.example.yaml) |
| `~/.mosscode/mcp.yaml`、`.mosscode/mcp.yaml` | MCP Server | [MCP 外部工具](docs/reference/mcp.md)，示例 [mcp.example.yaml](mcp.example.yaml) |
| `AGENTS.md`（或 `CLAUDE.md`）、`.mosscode/AGENTS.md`、`~/.mosscode/AGENTS.md` | 项目指令，追加到系统提示；单独一行的 `@路径` 引用其他文件 | [系统提示与缓存](docs/reference/prompts.md#项目指令agentsmd) |

## 使用

```powershell
.\.venv\Scripts\python.exe -m mosscode                    # 交互界面
.\.venv\Scripts\python.exe -m mosscode --provider claude  # 指定 Provider
.\.venv\Scripts\python.exe -m mosscode --list-providers
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
| `/help` | 显示本地命令与快捷键 |
| `/clear` | 开始新会话：清空屏幕与终端滚动历史、模型上下文和 Token 统计；权限档位、会话授权和 MCP 连接保留，AGENTS.md 重新加载 |
| `/compact [说明]` | 压缩上下文：较早的对话换成摘要，近期消息和用户原话保留；说明用于指定摘要侧重点 |
| `/model` | 在输入框下方列出配置的提供商（当前项带 ✓），↑ / ↓ 选择、Enter 切换、Esc 收起 |
| `/model 名称` | 直接切换到指定提供商；对话历史保留 |
| `/plan` | 只读调查与规划，模型提交计划经批准后执行 |
| `/plan 任务` | 切入计划模式并立即调查、生成计划 |
| `/permissions` | 查看权限档位说明 |
| `/permissions 档位` | 切换到 default、acceptEdits、plan 或 bypassPermissions |
| `/mcp` | 列出 MCP Server 的状态（连接中、已连接及工具数、失败原因）与配置问题 |
| `/mcp 名称` | 列出该 Server 的工具，只读工具标注 `[只读]` |
| `/exit` | 退出并清理资源 |

界面、输入排队、状态栏与配色的细节见 [终端界面](docs/reference/interface.md)。

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

`-p`（`--prompt`）运行一个任务后退出，不等待审批：需要确认的调用返回 approval_required，模型据此调整。`--output-format` 可选 `text`、`json`、`stream-json`；json 格式中 `is_error` 为真时退出码为 1，配置或连接错误在任何格式下都以 1 退出。格式说明见 [模型提供商](docs/reference/providers.md#无界面模式与输出格式)。

## 架构

```
cli.py ──► tui.py（Textual 界面）或 -p（无界面）
             └──► conversation.py（启动上下文与 MCP、对外接口）
                    └──► agent.py（Agent Loop）
                           ├──► compaction.py、context.py（上下文估算与压缩）
                           ├──► providers.py（三种协议、重试、缓存标记）
                           ├──► prompts.py（系统提示、环境补充、模式提醒）
                           └──► tools/（注册、校验、授权、执行）
                                  ├──► permissions.py、security.py（档位、规则、命令风险）
                                  └──► mcp_client.py、mcp_config.py（MCP）
```

一次任务：Agent Loop 用“稳定系统提示 + 历史 + 本次环境补充”请求模型，把流式事件交给界面；回复中的工具调用经校验和授权后执行（相邻只读调用最多 4 个并发），纯文本结果写回历史，再请求模型，直到回复不再调用工具或遇到停止条件（取消、轮数上限、连续未知工具等）。TUI 与 `-p` 共用 `Conversation`；在 Python 中可连续调用 `Conversation.ask()` 保持多轮历史，结束后 `await conversation.close()`。

| 模块 | 职责 |
| --- | --- |
| `agent.py` | Agent Loop：请求、工具执行、排队消息、停止条件、计划模式、压缩时机 |
| `context.py`、`compaction.py`、`tool_budget.py`、`session.py` | 上下文估算与阈值、摘要压缩、大结果存盘、会话目录 |
| `providers.py` | Anthropic Messages、OpenAI Responses、Chat Completions 适配，首事件前重试，缓存断点 |
| `prompts.py` | 七个固定模块、工具清单、`<environment>` 与 `<system-reminder>` 补充、AGENTS.md、git 快照 |
| `tools/` | `files.py`、`search.py`、`process.py`、`plan.py` 等工具；`execution.py` 分批并发；`base.py` 注册与执行边界 |
| `permissions.py`、`security.py` | 档位、分层规则、审批请求、只读命令白名单、危险命令分级、项目路径检查 |
| `mcp_client.py`、`mcp_config.py` | MCP 配置、后台连接与发现、工具适配 |
| `tui.py`、`appearance.py`、`scrollback.py` | 界面、命令与提示、原生滚动历史 |
| `config.py`、`cli.py` | 配置加载与命令行入口 |

### 文档

| 主题 | 文档 |
| --- | --- |
| 当前行为的完整说明 | [界面](docs/reference/interface.md)、[工具与 Agent Loop](docs/reference/tools.md)、[权限](docs/reference/permissions.md)、[MCP](docs/reference/mcp.md)、[模型提供商](docs/reference/providers.md)、[系统提示与缓存](docs/reference/prompts.md)、[上下文管理](docs/reference/context.md) |
| 设计评审 | [对照 Claude Code 与 Pi 的完善建议](docs/research/claude-code-pi-gap-review.md) |
| 各阶段规格与验证 | `docs/specs/`：`chapter-*` 为各章，`refinement-*` 为完善阶段 1–6，每个目录含 `spec.md` 与 `validation.md` |
| 人工验收 | [完善阶段 1–6 与上下文管理验收指南](docs/acceptance.md)：自动测试、离线检查和真实模型用例，验收项目由 `scripts/acceptance_setup.py` 生成 |

### 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py   # Windows 真实控制台烟测，需在交互式终端运行
.\.venv\Scripts\python.exe scripts/verify_resize.py            # 窗口宽度变化后的重绘检查，会弹出并自动关闭一个控制台窗口
.\.venv\Scripts\python.exe scripts/verify_keys.py              # 经控制台输入队列检查换行、历史和 /clear，同样弹出窗口
```

测试使用模拟模型、模拟 SSE 和无界面 TUI，不读取 API 配置，也不发送请求；真实服务的联调需要自行配置。界面预览脚本见 [终端界面](docs/reference/interface.md#预览与烟测脚本)。

目前不支持会话持久化与恢复、网络限制、资源配额和审计日志。
