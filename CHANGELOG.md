# 更新日志

这里记录每个版本的用户可见变化。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)；1.0 之前，次版本号的变化可能包含不兼容的改动，会在条目中注明。

## [Unreleased]

### 新增

- **浏览器一键登录（`moss login`）与本地临时 Web 配置面板（`moss config --web`）**：新增 `AuthProvider` 认证抽象，支持 **OpenRouter OAuth 2.0 PKCE（S256）** 与 **GitHub Copilot 设备码登录（RFC 8628）**（支持 `preset: copilot` / `auth_type: github-copilot`，会话令牌在距过期不足或等于 120 秒时，于下次使用时自动刷新，并集成至终端 `/connect` 向导；凭据管理器不可用时 fail-closed 不默认降级明文）；新增仅绑定 `127.0.0.1` 的带一次性启动令牌、端口源隔离会话令牌与 Host / Origin / CSP 保护的本地可视化配置页 `moss config --web`。
- **TUI 模型接入向导（`/connect`）与可搜索模型选择器（`/model` / `Alt+M`）**：新增交互式 `/connect`（别名 `/setup`）面板，自动探测本机运行中的 Ollama / LM Studio、检测外部工具配置或通过厂商预设/自定义端点接入，支持掩码密钥录入、在线 `/models` 探测、原子写盘（无损保留原 YAML 注释）与热重载立即切换；`/model` 升级为带实时模糊搜索、收藏置顶（`Ctrl+F`）、最近使用（MRU）、一键设为全局默认（`Ctrl+D`）和快捷跳转添加服务（`Ctrl+N`）的统一选择器。
- **更多厂商预设与本机免密钥模型**：新增 `gemini`、`ollama`、`lmstudio` 内置预设；支持 `auth_type: none` 免密钥模式，并严格限定仅本机回环地址（`localhost` / `127.0.0.1` / `::1`）生效（fail-closed）。

### 变更

- **冷启动按需导入优化**：将 Textual TUI、`mcp` SDK 与 `httpx` 从 CLI 启动热路径延迟加载，无界面 `-p` 与轻量命令冷启动显著提速且不拖入界面或网络重依赖。

## [0.3.1] - 2026-11-18

### 新增

- **代码结构地图与符号级读取**：新增只读工具 `repo_map`（支持 Python、JS/TS/TSX、Go、Rust、Java、C/C++、Bash 代码结构概览，遵守 `.gitignore` 与分层权限规则）；`read_file` 新增 `outline`（查看类与函数大纲及行号区间）与 `symbol`（按符号名精准读取定义代码段）。见 [工具与 Agent Loop](docs/reference/tools.md#代码结构地图与符号读取)。
- **会话级定时巡检（`/loop`）**：新增 `/loop [间隔] <提示词> | list | stop <id|all>` 斜杠命令，支持在会话空闲时按周期触发提示词或斜杠命令，统一接入 `/tasks` 与状态栏管理。见 [命令](docs/reference/commands.md#定时巡检loop)。
- **多 Agent 团队升级与 `/swarm` 命令**：新增 `/swarm [status|on|off|<任务>]` 动态协调模式，支持团队共享黑板（`post_board`、`read_board`）、成员间 `@` 点对点交接、只读审查角色（`verifier`）、成员异常退出任务回收与停滞巡检（默认活跃成员上限提升至 6），并在 ACP 编辑器服务中开放团队与 `/swarm` 支持；后台子 Agent 与 `Fork` 支持向 TUI 和 ACP 主界面冒泡权限审批并在停止时自动撤卡。见 [Agent 团队](docs/reference/teams.md#黑板与点对点接力)、[子 Agent](docs/reference/subagents.md#权限)。
- **完整 MCP 资源、提示词与分帧兼容**：新增 `list_mcp_resources`、`read_mcp_resource` 工具与 `@server:uri` 内联资源引用（支持文本与图片资源）；MCP 提示词自动注册为 `/mcp__<server>__<prompt>` 斜杠命令并在 TUI、`-p` 与 ACP 同步可用；stdio 传输自动兼容换行 JSON 与 `Content-Length` 分帧。见 [MCP 外部工具](docs/reference/mcp.md#资源与提示词)。
- **记忆检索、Skill 管理与上下文体验增强**：`memory` 工具新增 `search`、`read`、`list` 动作与 `pinned: true` 常驻标记，支持 Git 多工作树共享仓库项目记忆；已激活 Skill 总预算提升至 64 KiB，支持自动淘汰、按需卸载（`/skills unload`）及随 `/rewind` 同步恢复；`/status` 新增上下文数字来源说明，并增强 Chat Completions 对严格推理服务端模板的兼容性。见 [记忆笔记](docs/reference/memory.md)、[Skill](docs/reference/skills.md#调用方式)、[上下文管理](docs/reference/context.md#来源标注)。

## [0.3.0] - 2026-11-17

首个 GitHub 与 PyPI 公开发布版本。

### 新增

- **编辑器集成（ACP）**：新增 Agent Client Protocol 服务端入口 `moss acp`（兼容 `uvx mosscode acp`）与 ACP Registry 清单（`registry/mosscode/agent.json`、`icon.svg`），支持 stdio JSON-RPC 2.0、Terminal Auth 与 `-32000` 认证门禁、多目录并发会话隔离、`session/load` 历史重放、`modes` 与 `configOptions` 联动、结构化文件 Diff（含批量编辑）与行号定位、Todo/`plan` 同步、客户端 `mcpServers`、Slash 命令（含 `/rename` 与 `/model <提供商>/<模型>`）及审批桥接，沿用原生命令沙箱与项目信任边界。见 [ACP 编辑器集成](docs/reference/acp.md)。
- **原生系统级命令沙箱**：支持在 `config.yaml` 中设 `sandbox.mode: on` 或用 `--sandbox` / `--no-sandbox` 开启系统隔离（Windows 使用微软 MXC 或受限令牌，Linux 使用 bubblewrap + seccomp，macOS 使用 Seatbelt）。默认只允许写项目目录和私有临时目录，遮蔽 `~/.ssh` 等凭据目录，保护 `.mosscode`、`.git/hooks`、`.git/config`；联网统一经回环代理按 `WebFetch(domain:…)` 规则授权，支持 `sandbox.network: none` 完全断网。新增 `/sandbox` 与 `moss sandbox cleanup`。见 [命令沙箱](docs/reference/sandbox.md)。
- **文件编辑容错、批量替换与语法诊断**：`edit_file` 支持混合 CRLF/LF 行尾保留、忽略行尾空白与统一缩进偏移回退匹配；新增 `edits` 参数支持单文件多处原子替换（任一处匹配失败整批不写盘）；编辑 `.py`、`.json`、`.sh`/`.bash` 新引入语法错误时自动在工具结果中附带诊断提示。见 [工具与 Agent Loop](docs/reference/tools.md)。
- **执行控制与后台命令**：新增后台命令（`execute_command` 的 `run_in_background`、`Ctrl+B` 前台转后台，以及主对话前台超过 30 秒自动转后台 `command_auto_background`），配套 `task_output`、`task_stop` 与 `/tasks`，后台命令失败且输出较长时同时返回首尾输出；子进程默认注入 `GIT_TERMINAL_PROMPT=0`、`GIT_EDITOR=true`、`PAGER=cat` 等非交互变量；工具分批执行期间收到用户排队新消息时，自动跳过未开始的后续工具批次优先响应（Steering）；新增重复调用检测（连续 3 轮相同调用提醒、5 轮停止）。
- **模型配置与快速切换**：支持 `follow: claude-code` / `codex` 自动跟随外部工具配置，内置 DeepSeek、Kimi、GLM、通义千问、OpenRouter、Anthropic、OpenAI 厂商预设；随包附带 LiteLLM 模型目录快照（自动补全上下文窗口、输出上限、图片能力与费用估算，提供 `moss models [关键词|update]`）；`/model` 支持 `/model <提供商>/<模型>`、`:<模型>` 与同厂商模型目录直接切换；`moss setup --add` 支持无损保留原文件注释追加 Provider；`moss doctor --ping` 支持在线连通性探测；支持系统凭据管理器（`moss key set|delete`）与全链路密钥脱敏保护。见 [模型提供商](docs/reference/providers.md)。
- **会话管理、回退与长任务**：新增检查点与 `/rewind`（文件与对话回退，支持双击 `Esc` 打开）、`/fork [标题]` 会话分支派生、`/rename <标题>` 会话重命名、`/resume [ID|关键词]` 模糊筛选恢复；新增任务清单 `todo_write`（`/todo`）、结构化提问面板 `ask_user`、自主目标循环 `/goal <条件>`、跨会话共享调用预算 `run_budget`（`requests` / `tokens`）以及完成前确定性验收 Hook `pre_finish`。见 [会话存档与恢复](docs/reference/sessions.md)、[命令](docs/reference/commands.md)。
- **上下文分层压缩与恢复增强**：自动压缩前优先清理旧工具结果，不足时先将最早的一段对话折叠为摘要（`context_collapse`，保留会话历史原貌与 `/rewind` 回退点）；新增空回复与思考截断自动重发恢复、正文达输出上限自动续写（最多 3 次）；记忆更新后注入旧内容失效提醒且定义式子 Agent 同步共享记忆索引。见 [上下文管理](docs/reference/context.md)。
- **多模态、终端交互与插件生态**：支持 `@` 文件/行段引用（`glob`、`grep` 与 `@` 默认跳过 `.mosscode/worktrees` 和 `.claude/worktrees`）、`!命令` / `!!命令` 直跑、剪贴板/拖拽图片输入与 `read_file` 读图、`/thinking` 思考强度调节、`Ctrl+O` 展开思考内容与完整工具输出/Diff、运行状态行显示最近 5 次工具成败标记；新增 `web_fetch` 与 `web_search`（Tavily）；支持 Agent Skills 共享目录（`~/.agents/skills/`、`.agents/skills/`）、内置 `/init` Skill，以及用户级与项目级插件管理（`moss plugin`，兼容 Claude Code 插件格式）。
- **基准评估与文档体系**：新增锁定 Harbor 0.26.0 / Terminal-Bench 2.1 的公开基准评估适配器、安装预检、预算控制与逐题版本对比脚本，`-p` JSON 追加统计完整性字段 `accounting`（见 [评估与版本对照](docs/reference/evaluations.md)）；补充中英文 README 与 [使用指南](docs/usage.md)、[路线图](ROADMAP.md) 及发版 Wheel 冒烟验收。

### 变更

- 团队状态文件改用操作系统级文件锁（`filelock`），持有进程异常退出后立即释放。
- 危险命令与显式 `deny` 规则升级为基于 `tree-sitter-bash` AST 拆分子命令检查，防止通过引号、转义、`$(…)` 或子 Shell 绕过。
- Skill 名称对齐 Agent Skills 规范：允许数字开头，不再接受 `-` 结尾或含连续 `--` 的名称（不兼容变更）；非致命规范提示改在 `/skills` 中展示。
- 状态栏精简为“上下文 约 X / Y（N%）”与“缓存命中 N%”，详细 Token 与费用归入 `/status`。
- 无 `ripgrep` 时的内置搜索改用 `pathspec` 完整对齐 Git `.gitignore` 语法；Windows `atomic_write` 增加文件监听器瞬时锁重试。

## 0.1.0 - 2026-11-03

早期私有孵化基线（未公开发包）。

### 新增

- 终端交互界面（Textual 内联模式，完成的内容进入终端原生滚动历史）和无界面模式 `-p`，输出格式 `text`、`json`、`stream-json`。
- 三种模型协议：Anthropic Messages、OpenAI Responses、Chat Completions；多 Provider 切换，首个事件前自动重试，缓存断点。
- 首次运行的配置引导 `moss setup` 和诊断命令 `moss doctor`。
- 本地工具：读、写、编辑、命令、glob、grep，只读的计划模式；相邻的只读调用并发执行，大结果存盘。
- 四个权限档位（default、acceptEdits、plan、bypassPermissions），用户、项目和本机三层规则，命令风险分级和项目路径检查。
- MCP 外部工具：stdio 与 HTTP 传输，后台连接、工具发现和变更订阅。
- 上下文估算与自动压缩、会话实时存档与恢复（`-c`、`--resume`、`/resume`）。
- 记忆笔记：`memory` 工具、后台提取、用户级与项目级索引。
- 项目指令 `AGENTS.md`（兼容 `CLAUDE.md`），支持 `@路径` 引用。
- Skill：`SKILL.md` 规程与脚本工具，内置 `/commit`、`/review`、`/test`。
- Hook：会话、轮次、请求和工具调用前后的命令、提示注入、HTTP 和 Agent 动作，项目级 Hook 需确认后启用。
- 子 Agent：内置 explore、plan、general，自定义定义，Fork 方式，后台任务与通知。
- Git 工作树隔离：创建、初始化（链接与复制）、改动检查、安全删除和过期清理。
- Agent 团队：共享任务、成员邮箱、各自的工作树、Windows Terminal 分屏或进程内运行、确认后逐个合并。

[Unreleased]: https://github.com/yzd05/mosscode/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/yzd05/mosscode/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/yzd05/mosscode/releases/tag/v0.3.0
