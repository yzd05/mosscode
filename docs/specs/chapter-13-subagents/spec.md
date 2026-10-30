# 第十三章：子 Agent

日期：2026-10-29。状态：已实现（实现与规格的差异见“实现说明”）。

来源：
- 课程第 13 章：理论学习、Python 源码解析、实战演练。
- 用户的初步设计：
  - 统一的 Agent 工具，用类型参数在定义式与 Fork 式之间路由，工具列表保持稳定；
  - 角色为带 YAML frontmatter 的 Markdown，多来源、同名覆盖；
  - 定义式从空白对话开始，Fork 式继承父对话以命中缓存；
  - 隔离运行时状态，共享基础设施；
  - 运行到完成、不交互、结果异步通知；
  - 多层工具过滤防止无限嵌套；
  - 后台任务管理器，三种转入后台的方式，Fork 一律后台；
  - 不做 worktree 隔离、多 Agent 团队、后台任务的跨会话持久化。
- 对用户初步设计的评审建议（18 条），用户确认“按推荐写规格”。
- 当天对照 Skill fork（`Conversation._fork`）、权限（`PermissionPolicy`）、Hook（第十二章）实现的调查。

本规格按 MossCode 的现状调整课程方案和用户的初步设计，调整逐条列在“对课程方案的调整”中。

## 目标

让主 Agent 把一段可以独立完成的工作交给子 Agent，主对话只收到结论。中间的搜索、阅读、试错留在子 Agent 自己的上下文里，不占用主对话的窗口。

两种子 Agent：

- **定义式**：预先写好的角色，例如只读探索、制定计划。从空白对话开始，带固定的系统提示词、工具范围、模型、轮数上限和权限模式。
- **Fork 式**：临时的分身，继承父对话的全部历史和工具，适合“基于刚才讨论的内容，另外去做一件事”。首个请求与父对话的缓存前缀相同。

子 Agent 运行到完成为止，不向用户提问。它可以在前台运行（主 Agent 等待结果），也可以在后台运行（主 Agent 继续工作，完成时收到通知）。

## 现状

| 位置 | 现在的做法 | 与本章的关系 |
| --- | --- | --- |
| `AgentLoop`（`agent.py`） | 一次 `run` 为一轮。字段包括 `history`、`busy`、`total_usage`、`pending`（运行中追加的用户输入，在下一次请求前合并为一条用户消息）、`budget`、`session`、`auto_compact`、`hooks`、`max_iterations`（默认 200）；事件为 `RunStopped`、`Completed` 等 | 子 Agent 就是另一个 `AgentLoop`；后台通知在主 Agent 忙时经 `pending` 合并 |
| Skill fork（`Conversation._fork`） | 前台、同步：<br>• 新建子 `AgentLoop`，复制 `system`、`instructions`、`git`，不带 Skill 目录（K4），所以缓存前缀与主对话不同；<br>• 共用 `budget`、`auto_compact`、`session`，`total_usage` 共用同一个对象；<br>• 历史来自 `_fork_history`（none / recent / full）；<br>• 工具来自 `_fork_registry`（`registry.subset`，去掉 `load_skill`）；<br>• 模型来自 `_fork_provider`（按 `provider_configs` 中的名字）；<br>• 结果作为一问一答追加到主历史 | 子 Agent 的运行时由这里抽出；Skill fork 迁移到同一个运行时 |
| `ToolRegistry.subset`（`tools/base.py`） | 子 Agent 的工具视图：同一批工具实例、同一个 `PermissionPolicy`、同一个会话，设置 `agent = "fork"` | 工具实例和权限策略都需要按子 Agent 分开（I2、P1） |
| `create_default_registry` | `read_file`、`write_file`、`edit_file` 共用一个 `FileState`，记录读取或写入时的 `(mtime_ns, size)` | 定义式子 Agent 需要空的 `FileState`，Fork 需要父对话的副本（I2） |
| `PermissionPolicy`（`permissions.py`） | `layers`（用户、项目、本地三层规则）、`mode`（default / acceptEdits / plan / bypassPermissions）、`session`（本会话授权）、`interactive`、`requests`（界面消费的审批队列）、`_ask()`；非交互时返回 `approval_required` | 子 Agent 使用策略视图：共用规则文件和审批队列，模式与本会话授权各自独立（P1–P6） |
| 计划模式 | `definitions(plan=True)` 只提供只读工具、只读命令和 `exit_plan_mode`；写入被 `plan_blocked` 拒绝 | 内置 `explore`、`plan` 用计划模式保证只读（F8） |
| `execute_batch`（`tools/execution.py`） | 相邻的只读调用最多 4 个并行，其他调用串行 | 相邻的 `agent` 调用也需要并行（G6） |
| `tool_budget` | 过大的工具结果写入会话目录的 `tool-results/`，历史中只保留开头和末尾的预览；只在进入历史前决定一次 | 子 Agent 的结果与后台通知复用同一套限制（G5、B5） |
| `prompts.Supplement` | `PREFIX_KINDS`（`environment`、`active-skills`、`hook-context`）放在可缓存前缀中；其他类型（`system-reminder`、`hook-notification`）只随当次请求发送 | Agent 目录用新的前缀类型 `agents`（G2）；Fork 复制全部前缀补充（C1） |
| Skill 目录（`skills/__init__.py`） | 来源为 `builtin`、`user`、`project`，后加载的同名定义覆盖前者；frontmatter 同时接受 snake_case 和 Claude Code 的写法 | Agent 定义沿用这套来源、覆盖与字段别名的处理（F1–F4） |
| `load_skill` 与 fork 模式的 Skill | 模型加载 fork 模式的 Skill 时按 inline 处理，并提示由用户输入 `/<name>` 运行 | 子 Agent 中不提供 `load_skill`（L1） |
| Hook（`hooks/`） | `HookEvent.agent` 为 `main` 或 `fork`；fork 中只触发工具事件（E3）；`agent` 动作是占位，`HookEngine.agent_runner` 留有接口 | 增加 `subagent` 取值与 `agent_type` 字段、`subagent_start` / `subagent_stop` 事件，并实现 `agent` 动作（H1–H6） |
| 会话目录（`session.py`） | `<id>.jsonl` 为对话记录，`<id>/` 目录中存放 `tool-results/`、`compact-<n>.md`、`hooks.log`，首次写入时才创建 | 子 Agent 的对话记录写入 `<id>/subagents/<任务 id>.jsonl`（M5） |
| 记忆提取（`MemoryExtractor`） | 每轮结束后在后台从主对话中提取记忆 | 子 Agent 的对话不参与提取（I5） |
| TUI 按键（`tui.py`） | `escape` 取消生成，`ctrl+c` 取消任务，`shift+tab` 切换模式 | 新增 `ctrl+b` 转入后台（B3） |
| `-p`（`cli.py`） | 一轮结束后关闭会话退出；提示走 stderr，stream-json 输出 `notice` | 需要等待后台任务完成并处理其通知（B7） |

## 对课程方案的调整

| # | 课程方案或初步设计 | 调整 | 原因 |
| --- | --- | --- | --- |
| D1 | `model` 取 `inherit` / `haiku` / `sonnet` / `opus` | 取 `inherit` 或 `config.yaml` 中某个提供商的 `name`，与 Skill 的 `model` 一致；Fork 一律继承 | MossCode 支持多家提供商，`haiku` 等名字只对 Anthropic 有意义；Fork 换模型就无法命中缓存 |
| D2 | `permissionMode` 可设为任意模式，含 `dontAsk`、`bypassPermissions` | 只有现有四种模式。主对话处于计划模式时子 Agent 一律为计划模式；项目级定义取定义与会话模式中更严格的一个；用户级和内置定义可以放宽。拒绝规则、硬检查和 Hook 拦截始终生效（P2–P4） | 项目级定义随仓库克隆而来，不能借 `bypassPermissions` 绕过审批；`dontAsk` 在 MossCode 中由后台的非交互审批覆盖 |
| D3 | 后台子 Agent 的工具收窄到固定的白名单（`ASYNC_AGENT_ALLOWED_TOOLS`） | 不设静态白名单。后台子 Agent 的权限策略为非交互：需要审批的调用直接返回 `approval_required`，由规则允许的调用照常执行（P5） | MossCode 的所有工具都经过同一个权限策略；静态白名单会挡住 MCP 的只读工具，又与权限规则重复 |
| D4 | 子 Agent 与父 Agent 共享文件读取缓存 | 每个子 Agent 有自己的文件工具实例：定义式从空的 `FileState` 开始，Fork 复制父对话的 `FileState`（I2） | 共享时，子 Agent 读过的文件会让父 Agent 跳过“先读后改”的检查，反之亦然 |
| D5 | ESC 把前台子 Agent 转入后台 | `ctrl+b` 转入后台，`escape` 保持“取消生成”（B3） | `escape` 在 MossCode 中已有含义，改变它会让用户误把任务转入后台 |
| D6 | Agent 定义有四个来源：项目、用户、内置、插件 | 三个来源：项目 > 用户 > 内置（F1） | MossCode 没有插件机制 |
| D7 | 内置 Explore、Plan、general-purpose、Verification | 内置 `explore`、`plan`、`general`。`explore` 和 `plan` 使用计划模式保证只读；不提供 Verification（F8） | 只读靠模式保证，比靠提示词和黑名单可靠；Verification 需要更多配置开关，作为后续工作 |
| D8 | frontmatter 为 camelCase：`disallowedTools`、`maxTurns`、`permissionMode` | snake_case：`disallowed_tools`、`max_turns`、`permission_mode`；同时接受 camelCase 和连字符写法（F3） | 与 Skill、Hook 的字段风格一致，又能直接使用 Claude Code 的定义文件 |
| D9 | 嵌套保护只靠从子 Agent 的工具列表中移除 Agent 工具 | 定义式子 Agent 的工具列表中没有 `agent`；Fork 的工具列表与父对话完全相同，但调用 `agent` 时在执行阶段被拒绝（L1、L2） | Fork 要保持工具定义与父对话一致才能命中缓存 |
| D10 | 后台完成时，若主 Agent 空闲，通知等到用户下一次输入才送达 | 主 Agent 空闲时立即用通知开启新的一轮；忙时在下一次请求前合并（B6） | 用户不需要为了让模型看到结果再输入一句话（Q1） |
| D11 | Hook 中子 Agent 只用 `agent == fork` 区分 | `agent` 取 `main`、`fork`、`subagent`；新增 `agent_type` 字段；原有 `agent == fork` 的条件对 Skill fork 和 Agent 的 Fork 式都命中（H1） | 定义式与 Fork 式需要分别拦截，又要兼容第十二章已写好的条件（Q3） |
| D12 | Skill fork 有自己的子 Agent 实现 | Skill fork 迁移到本章的运行时，用户可见的行为不变（S1–S3） | 两套实现会在隔离、用量、Hook 标记上逐渐分叉 |

## 需求

### Agent 定义文件

- **F1** 来源与覆盖：
  - 内置：`src/mosscode/agents/builtin/*.md`；
  - 用户级：`~/.mosscode/agents/*.md`；
  - 项目级：`.mosscode/agents/*.md`；
  - 同名时项目级覆盖用户级，用户级覆盖内置；只读取目录下一层的 `.md` 文件。
- **F2** 格式：YAML frontmatter 加正文，正文就是子 Agent 的系统提示词，不能为空。
- **F3** 字段：

  | 字段 | 别名 | 必填 | 说明 |
  | --- | --- | --- | --- |
  | `name` | — | 是 | 小写字母、数字、连字符，最长 64 个字符；不能是 `fork` |
  | `description` | — | 是 | 什么时候使用，最长 1024 个字符；进入 Agent 目录 |
  | `tools` | — | 否 | 白名单，列表或逗号分隔的字符串 |
  | `disallowed_tools` | `disallowedTools`、`disallowed-tools` | 否 | 黑名单，写法同上 |
  | `model` | — | 否 | `inherit`（默认）或提供商名（D1） |
  | `max_turns` | `maxTurns`、`max-turns` | 否 | 正整数，即子 Agent 的 `max_iterations`；超过全局 `max_iterations` 时取全局值 |
  | `permission_mode` | `permissionMode`、`permission-mode` | 否 | `default`、`acceptEdits`、`plan`、`bypassPermissions` |
  | `background` | — | 否 | 布尔值；为真时该类型总在后台运行 |

- **F4** 工具名：
  - 可以写 MossCode 的工具名（`read_file`、`execute_command` 等）、权限规则的别名（`Read`、`Write`、`Edit`、`Bash`、`Shell`、`Glob`、`Grep`）或 MCP 通配（`mcp__github__*`）；
  - 别名展开为对应的工具名；
  - 不存在的工具名不使定义失效，在 `/agents <name>` 中提示（MCP 工具可能尚未连接）。
- **F5** 校验：
  - YAML 错误、缺少必填字段、取值非法、未知字段（如 `disalowed_tools`）→ 跳过该文件，在 `/agents` 中列出路径与原因，启动时提示一行；
  - Claude Code 特有的字段（`color`、`effort`、`skills`、`mcpServers`、`hooks`、`memory`、`isolation`、`initialPrompt`）忽略，在详情中提示“未支持”（`isolation` 后来由[第十四章](../chapter-14-worktree/spec.md)实现）；
  - 同一来源中两个文件的 `name` 相同 → 按文件名排序取第一个，另一个列为问题。
- **F6** `model` 指向不存在的提供商时，定义照常加载，在 `/agents` 中标记；调用该类型时返回错误，不回退到当前模型。
- **F7** 加载时机：会话开始时加载一次，作为本会话的快照；`/agents reload` 重新加载，从下一次请求起生效。
- **F8** 内置定义：

  | 名称 | 用途 | 工具 | 权限模式 | 其他 |
  | --- | --- | --- | --- | --- |
  | `explore` | 只读探索：查找实现、理清调用链、了解项目结构 | 全部（计划模式下只提供只读工具和只读命令） | `plan` | `max_turns: 30`；提示词要求并行搜索、报告中带文件路径 |
  | `plan` | 只读规划：探索后给出分步方案 | 同上 | `plan` | `max_turns: 30`；报告末尾列出 3–5 个关键文件 |
  | `general` | 需要完整能力、但不想占用主上下文的多步任务 | 全部 | 跟随会话 | 提示词要求做完、报告简洁 |

  内置定义可被同名的用户级或项目级定义覆盖。

### Agent 工具

- **G1** 一个工具 `agent`，参数固定：

  | 参数 | 必填 | 说明 |
  | --- | --- | --- |
  | `description` | 是 | 3–10 个字的任务标签，显示在界面和任务列表中 |
  | `prompt` | 是 | 交给子 Agent 的完整任务说明 |
  | `agent_type` | 否 | 定义名；省略或写 `fork` 时为 Fork 式 |
  | `run_in_background` | 否 | 只对定义式有效；Fork 式一律后台 |

  工具的说明文字中不列出 Agent 类型，定义变化不改变工具定义。
- **G2** Agent 目录：
  - 作为稳定系统提示词的一节，放在 Skill 目录之后（`AgentLoop.agent_catalog`），列出每个定义的 `name`、`description`、工具范围摘要；
  - 内容随 F7 的快照变化，同一会话中不随请求变化；
  - 没有任何可用定义时仍说明 Fork 式的用法。
- **G3** 定义式的运行：
  - 系统提示词 = 定义正文 + 环境信息（工作目录、平台、git 状态）+ 项目说明（`instructions`）；
  - 不含 Skill 目录、Agent 目录、记忆、`hook-context`；
  - 历史为空，第一条用户消息就是 `prompt`。
- **G4** 结果：
  - 子 Agent 最后一条回复的正文作为结果；
  - 正常完成时结果末尾附一行统计：工具调用次数、Token、用时；
  - 未完成（达到轮数上限、模型错误、被取消）时结果以 `[子 Agent 未完成：原因]` 开头，后面附上最后一条回复的正文（如有）。
- **G5** 结果长度沿用 `tool_budget`：超过单条结果的上限时，完整内容写入会话目录，结果只保留预览。
- **G6** 并行：一次回复中相邻的多个 `agent` 调用并发执行，最多 4 个，与相邻只读调用的分组规则相同。
- **G7** 错误（返回给模型的工具错误）：
  - `agent_type` 不存在 → `unknown_agent_type`，列出可用的类型；
  - 该类型的 `model` 无效 → `agent_unavailable`；
  - 后台任务数已达上限（默认 4，配置项 `agent_max_background`）→ `too_many_tasks`。
- **G8** 计划模式下主对话也提供 `agent` 工具；此时子 Agent 也处于计划模式（P2）。

### Fork 式

- **C1** 缓存前缀一致：Fork 的首个请求中，以下内容与父对话最近一次请求逐字节相同：
  - 系统提示词，包括全部前缀补充（Skill 目录、Agent 目录、`active-skills`、`hook-context`）；
  - 工具定义，按父对话当前的模式计算；
  - 历史，直到含这次 `agent` 调用的那条助手消息为止。
- **C2** 历史的末尾：
  - 在那条助手消息之后，追加一条工具结果消息：其中每个调用的结果都是同一段固定文字（“已在后台分出 Fork 处理”），不含任务 id；
  - 再追加一条用户消息：Fork 指令加 `prompt`；
  - 同一条助手消息中的多个 Fork，只有最后这条用户消息不同。
- **C3** Fork 指令要求：
  - 不再创建子 Agent；
  - 不向用户提问；
  - 不超出 `prompt` 的范围；
  - 最终报告以“范围：”开头，依次写范围、结果、涉及的关键文件、未完成的部分。
- **C4** Fork 使用父对话的提供商实例和 `budget`。
- **C5** Fork 一律在后台运行，`agent` 调用立即返回任务 id。
- **C6** 子 Agent 中不能 Fork（L2）。

### 隔离与共享

- **I1** 每个子 Agent 独立的状态：
  - 对话历史；
  - 权限策略视图（P1）；
  - 文件读取状态（I2）；
  - Token 用量（I4）；
  - 压缩状态：按自己的 `budget` 自动压缩自己的历史。
- **I2** 文件工具：
  - 每个子 Agent 有自己的 `read_file`、`write_file`、`edit_file` 实例；
  - 定义式从空的 `FileState` 开始；
  - Fork 从父对话 `FileState` 的副本开始；
  - 子 Agent 写入后，父 Agent 再次编辑同一文件时按 mtime 检查得到 `stale_read`。
- **I3** 共享的基础设施：
  - 提供商实例（`model: inherit` 时）；
  - Hook 引擎；
  - MCP 连接；
  - 会话目录；
  - 项目根目录与 shell 配置。
- **I4** 用量：
  - 子 Agent 的每次请求，用量同时计入该任务和会话总量；
  - 主对话的上下文估算不包含子 Agent 的历史。
- **I5** 子 Agent 的对话不参与记忆提取，也不写入主对话的历史；主对话只收到结果或通知。

### 权限

- **P1** 子 Agent 使用 `PermissionPolicy` 的视图：
  - 共用三层规则文件、受保护文件列表和审批队列 `requests`；
  - `mode`、`interactive` 和本会话授权 `session` 为自己的；
  - 本会话授权从空开始，不继承主对话已有的授权，子 Agent 中授予的也不回流到主对话。
- **P2** 主对话处于计划模式时，子 Agent 一律为计划模式。
- **P3** 其他情况，有效模式为：
  - 定义未写 `permission_mode`：主对话当前的模式；
  - 项目级定义：定义与主对话模式中更严格的一个，严格程度为 `plan` > `default` > `acceptEdits` > `bypassPermissions`（Q2）；
  - 用户级与内置定义：定义中的模式；
  - Fork：主对话当前的模式。

  模式在子 Agent 创建时确定，主对话之后切换模式不影响已在运行的子 Agent。
- **P4** 拒绝规则、硬检查（计划模式、受保护文件、危险命令）和 `pre_tool_use` Hook 在任何模式下对子 Agent 生效。
- **P5** 后台子 Agent 的权限视图为非交互：需要审批的调用返回 `approval_required`，原因中说明“后台子 Agent 不能请求审批，可以添加允许规则或在前台运行”。
- **P6** 前台子 Agent 的审批进入共用的审批队列，审批面板的标题标明来源，例如“子 Agent explore · 查找配置加载”。
- **P7** 前台子 Agent 转入后台时，正在等待的审批按“拒绝”处理，该工具返回 `approval_required`。

### 工具过滤

- **L1** 全局禁止，对所有子 Agent 生效：
  - `agent`：防止嵌套；
  - `load_skill`：它修改主对话的 `active-skills`；
  - `memory`：记忆只按用户的要求写入；
  - `exit_plan_mode`：计划审批属于主对话；
  - `task_output`、`task_stop`：任务管理属于主对话。
- **L2** 定义式从工具定义中移除 L1 的工具；Fork 的工具定义与父对话相同，调用 L1 的工具时返回 `subagent_forbidden`，原因中说明这是子 Agent。
- **L3** 定义层：先按 `disallowed_tools` 排除，再按 `tools` 取交集。
- **L4** 子 Agent 中运行 fork 模式的 Skill 被拒绝（子 Agent 本来就没有 `load_skill`，这一条覆盖 Hook 的 `agent` 动作等其他入口）。
- **L5** 由 Hook 的 `agent` 动作启动的子 Agent，其工具事件不再执行 `agent` 动作（H5）。

### 前台、后台与通知

- **B1** 定义式默认在前台运行：`agent` 调用等待子 Agent 完成，结果作为工具结果返回。
- **B2** 以下情况在后台运行，`agent` 调用立即返回任务 id 和“完成时会收到通知”：
  - `run_in_background: true`；
  - 定义中 `background: true`；
  - Fork 式。
- **B3** 前台转入后台：
  - 自动：运行超过 120 秒（配置项 `agent_auto_background`，单位为秒，0 表示不自动转入）；
  - 手动：TUI 中按 `ctrl+b`，把当前所有前台子 Agent 转入后台；
  - 转入后台后，`agent` 调用以“已转入后台：任务 id”返回，子 Agent 继续运行，权限视图改为非交互（P5、P7）。
- **B4** 取消：
  - `escape`、`ctrl+c` 取消主对话的当前任务，同时取消其中的前台子 Agent，工具结果为 `[子 Agent 未完成：已取消]`；
  - 后台子 Agent 不受影响，用 `/tasks stop <id>` 或模型的 `task_stop` 停止。
- **B5** 通知内容：
  - 一个 `<task-notification>` 块，包含任务 id、类型、标签、状态、结果、用量与用时；
  - 结果长度按 G5 限制；
  - 作为 `synthetic` 为 `task-notification` 的用户消息进入历史，界面上显示为一行提示，不显示为用户气泡。
- **B6** 送达时机（Q1）：
  - 主 Agent 忙：通知进入 `pending`，在下一次请求前与其他追加输入一起合并；
  - 主 Agent 空闲：立即用通知开启新的一轮；同时完成的多个通知合并为一轮；
  - 每个通知只送达一次。
- **B7** `-p`：
  - 一轮结束后若仍有后台任务在运行，等待它们完成，每个通知按 B6 开启新的一轮；
  - 所有任务结束且通知都已送达后退出；
  - `ctrl+c` 取消所有任务并退出；
  - stderr 和 stream-json 中有任务开始与结束的提示（`{"type":"notice"}`）。
- **B8** 会话切换与退出：`/clear`、`/resume`、退出时若有运行中的后台任务，TUI 弹出确认面板（取消这些任务并继续 / 返回）；确认后取消任务，最多等待 5 秒，任务记为“已取消”，不再送达通知。

### 任务管理

- **M1** 任务管理器属于会话，记录每个子 Agent：
  - 任务 id（`t` 加序号，如 `t3`）、来源（`agent` 工具、Skill fork、Hook）、类型、标签、`prompt`；
  - 状态：`running`、`completed`、`failed`、`cancelled`；
  - 前台或后台、开始与结束时间、用量、工具调用次数、最近一次工具调用；
  - 结果、对话记录路径、通知是否已送达。
- **M2** 模型侧的工具（两者只在主对话中提供）：
  - `task_output`：参数 `task_id`、`wait`（秒，默认 0，最多 300）；返回状态与结果，运行中时返回进度；
  - `task_stop`：参数 `task_id`；停止运行中的任务，状态记为 `cancelled`，不再送达通知。
- **M3** `/tasks` 命令：
  - `/tasks`：列出本会话的任务，含 id、类型、标签、状态、用时、Token；
  - `/tasks <id>`：详情，含 `prompt`、结果预览、用量（含缓存命中的 Token）、对话记录路径；
  - `/tasks stop <id>`：停止任务；
  - 主对话运行期间也可以输入（第十章 P8 的例外，`/status` 同样可以；实现中由 `Command.while_running` 标记）；
  - 在 `-p` 中输入时退出码为 2。
- **M4** 任务不跨会话：`/clear` 和 `/resume` 之后列表为空；恢复的会话不重新运行旧任务。
- **M5** 对话记录：
  - 每个子 Agent 的消息写入 `<会话目录>/subagents/<任务 id>.jsonl`，格式与主对话记录相同；
  - 主对话记录中写一条任务记录（id、类型、标签、开始与结束、状态、路径），恢复时不重放；
  - 不保存会话（`persist_sessions: false`）时不写。

### Hook

- **H1** 事件字段：
  - `agent` 取值为 `main`、`fork`（Skill fork 与 Agent 的 Fork 式）、`subagent`（定义式）；
  - 新增 `agent_type`：主对话为空串，定义式为定义名，Agent 的 Fork 式为 `fork`，Skill fork 为 `skill:<name>`；
  - 第十二章写好的 `agent == fork` 条件含义不变（Q3）。
- **H2** 子 Agent 中只触发工具事件，与第十二章的 E3 一致。
- **H3** 新事件，在主对话一侧触发（`agent` 为启动它的一方）：

  | 事件 | 时机 | 字段 | 动作 |
  | --- | --- | --- | --- |
  | `subagent_start` | 子 Agent 发出第一个请求之前 | `task_id`、`agent_type`、`description`、`background` | command、http、prompt。prompt 和 feedback 放入该子 Agent 的第一条用户消息之前 |
  | `subagent_stop` | 子 Agent 结束之后、结果返回或通知送达之前 | `task_id`、`agent_type`、`status`、`background` | command、http、agent；一律后台执行 |

- **H4** `agent` 动作：
  - 新增字段 `agent_type`，默认 `general`；
  - 以 `prompt` 的渲染结果启动一个后台的定义式子 Agent，权限视图为非交互；
  - 结果不送达模型：写入 `hooks.log`，在 `/tasks` 中可见，界面提示一行；
  - `timeout` 到期时停止任务，记为 `timeout`。
- **H5** 由 `agent` 动作启动的子 Agent，其工具事件中的 `agent` 动作记为 `skipped`，不再启动子 Agent。
- **H6** `agent` 动作受 G7 的后台任务上限约束，超出时记为失败。

### Skill fork 的迁移

- **S1** Skill fork 改用本章的运行时：
  - 有自己的文件工具实例与权限视图；
  - 用量按 I4 计入；
  - 对话记录按 M5 写入；
  - 在 `/tasks` 中可见。
- **S2** 用户可见的行为不变：前台、同步，`fork_context` 的 none / recent / full，`allowed_tools`，`model`，结果作为一问一答追加，取消后主对话可以继续。第十一章的测试不修改即可通过。
- **S3** Skill fork 不转入后台，`ctrl+b` 和自动转入后台对它无效。

### 界面与命令

- **U1** 工具行：
  - 前台：`agent` 的工具行显示类型、标签，运行中实时更新工具调用次数、最近一次工具调用、Token、用时；
  - 子 Agent 的每次工具调用不单独显示为工具行；
  - 完成后显示结果的前几行与统计；
  - 后台或转入后台：显示“后台 · 任务 t3”。
- **U2** 状态栏显示运行中的后台任务数；有前台子 Agent 时提示 `ctrl+b 转入后台`。
- **U3** 后台任务开始与结束时各显示一行提示：“后台任务 t3（explore · 查找配置加载）已启动 / 已完成”；Hook 启动的任务不提示（结果写入 `hooks.log`）。
- **U4** `/agents` 命令：
  - `/agents`：列出定义，含来源、被覆盖的标记、问题文件；
  - `/agents <name>`：详情，含字段、实际可用的工具、有效权限模式、未支持的字段与不存在的工具；
  - `/agents reload`：重新加载并报告变化；
  - 在 `-p` 中输入时退出码为 2。
- **U5** `/status` 中显示 Agent 定义数与后台任务数。

## 设计

### 模块

| 模块 | 职责 |
| --- | --- |
| `agents/__init__.py` | `AgentDef`、`parse_agent`（frontmatter、别名、校验）、`AgentCatalog`（三层加载、覆盖、问题列表、`reload`、Agent 目录 `catalog_section`）、`agent_dirs` |
| `agents/builtin/*.md` | 内置 `explore`、`plan`、`general` |
| `agents/runtime.py` | `SubAgents`：`definition_loop`、`fork_loop`（组装子 `AgentLoop`）、`drive`（运行到完成、转发审批、计进度、生成结果文本）、`call`（`agent` 工具的入口，前台等待或转入后台）、`hook_agent`（Hook 的 `agent` 动作）、`notification`；`SubagentLog`（对话记录）；`effective_mode`、`fork_history` |
| `agents/tasks.py` | `Task`、`TaskManager`（创建、前台转后台、停止、等待、通知队列、关闭时取消） |
| `tools/agent.py` | `AgentTool`、`TaskOutput`、`TaskStop` |
| `tools/base.py` | `ToolRegistry.for_agent(...)`：按子 Agent 复制文件工具实例并换上新的 `FileState`，换上权限视图，设置 `agent` 与 `agent_type`；`subset` 改为基于它 |
| `tools/files.py` | `FileState.copy()` |
| `tools/execution.py` | 相邻的 `agent` 调用并发分组（G6） |
| `permissions.py` | `PermissionPolicy.view(mode, interactive, label)`：共用 `layers`、`requests`、`_approval_lock`，自有 `mode`、`interactive`、`session`；`stricter(a, b)` |
| `agent.py` | `AgentLoop.notifications`：在下一次请求前合并为 `synthetic="task-notification"` 的用户消息；`agent_catalog` 字段 |
| `conversation.py` | 持有 `AgentCatalog` 与 `TaskManager`；`_fork` 改为经 `SubAgents.drive` 运行；空闲时由通知开启新的一轮；`/clear`、`resume()`、`close()` 中处理运行中的任务 |
| `hooks/` | `agent` 取值、`agent_type` 字段、`subagent_start` / `subagent_stop` 事件、`agent` 动作的 `agent_type` 字段；`agent_runner` 由 `Conversation` 提供 |
| `config.py` | `agent_auto_background`（默认 120）、`agent_max_background`（默认 4） |
| `builtin_commands.py` | `/agents`、`/tasks`、`/status` 的补充、B8 的确认 |
| `tui.py` | `ctrl+b`、`agent` 工具行的进度、状态栏、通知提示、子 Agent 审批面板的标题 |
| `cli.py` | B7 的等待与通知 |

### 数据结构

```python
@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    prompt: str                      # 正文，即系统提示词
    path: Path
    source: Literal["builtin", "user", "project"]
    tools: tuple[str, ...] = ()      # 已展开别名；空表示不限制
    disallowed_tools: tuple[str, ...] = ()
    model: str | None = None         # None 表示 inherit
    max_turns: int | None = None
    permission_mode: str | None = None
    background: bool = False
    unsupported: tuple[str, ...] = ()  # 忽略的 Claude Code 字段
    overrides: tuple[Path, ...] = ()   # 被它覆盖的同名文件

@dataclass
class Task:
    id: str
    origin: Literal["agent", "skill", "hook"]
    agent: Literal["fork", "subagent"]
    agent_type: str
    description: str
    prompt: str
    call_id: str = ""                # 发起它的 agent 调用，供工具行显示进度
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    background: bool = False
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    tool_calls: int = 0
    last_tool: str = ""
    result: str | None = None
    transcript: Path | None = None
    notified: bool = False
    stopped: bool = False            # 被 /tasks stop 或 task_stop 停止，不送达通知
    job: asyncio.Task | None = None
    detach: asyncio.Event = field(default_factory=asyncio.Event)  # 前台转后台的信号
    policy: PolicyView | None = None  # 转入后台时改为非交互
```

### 数据流

定义式、前台：

1. 主 `AgentLoop` 执行 `agent` 调用 → `AgentTool.execute` 查找定义；不存在则返回 `unknown_agent_type`。
2. `SubAgents.definition_loop`：
   - 系统提示词按 G3 组装；
   - 工具 = `registry.for_agent(...)` 后依次按 L1、L3 过滤；
   - 权限视图按 P2、P3 确定模式，前台为交互；
   - 提供商按 `model` 选择。
3. `TaskManager.new` 创建 `Task`，`launch` 在 `asyncio.Task` 中运行 `SubAgents.drive`；`drive` 先触发 `subagent_start`。
4. `AgentTool` 同时等待两件事：子 Agent 完成，或 `detach` 被设置（`ctrl+b` 或 120 秒到期）。
   - 完成：触发 `subagent_stop`，结果按 G4、G5 返回；
   - `detach`：权限视图改为非交互，返回“已转入后台”；之后的完成走后台路径。
5. 运行中的进度经工具的进度回调更新工具行（U1）。

后台（含 Fork、转入后台后的定义式）：

1. 子 Agent 完成 → 触发 `subagent_stop` → `TaskManager` 生成通知。
2. 主 Agent 忙 → 通知进入 `AgentLoop.notifications`，在下一次请求前合并；空闲 → `Conversation.on_tasks` 回调，界面调用 `Conversation.notify()` 开启一轮，第一条消息是通知。
3. TUI 由 `Conversation` 的回调得知需要开启新的一轮（与 `on_memory` 同样经界面线程调度）；`-p` 在主循环中等待 `TaskManager`。

Fork：

1. `AgentTool.execute` 在没有 `agent_type` 时进入 Fork 路径；若当前是子 Agent → `subagent_forbidden`。
2. `SubAgents.fork_loop`：
   - 复制主 `AgentLoop` 的系统提示词、项目说明、git 状态、Skill 与 Agent 目录、`active-skills`、`hook-context`，请求按与主对话相同的方式组装（C1）；工具定义按同一个模式计算；
   - 历史 = 主历史的深拷贝，截至当前助手消息，后接 C2 的工具结果与指令消息；
   - 工具实例为 `for_agent` 的副本，`FileState` 为复制品；
   - L1 的工具保留定义，执行时拒绝。
3. 一律后台，返回任务 id。

### 事件与界面

- 子 Agent 的 `AgentLoop` 事件不直接进入主事件流；`drive` 把它们汇总为任务状态（工具调用次数、最近一次工具调用），经 `TaskManager.on_change` 更新工具行与状态栏。
- 前台子 Agent 的审批请求由 `drive` 放入主对话的 `requests` 队列，界面像主对话的审批一样显示；请求的 `source` 为权限视图的 `label`（P6）。
- 新的提示经现有的 `Notice` 与后台提示路径送到 TUI、stderr 和 stream-json。

## 取舍

- **Fork 的工具列表包含禁止的工具。** 从 Fork 的工具定义中移除 `agent` 最直观，但工具定义变化会让整个缓存前缀失效。代价是模型可能尝试调用，执行阶段会明确拒绝，Fork 指令中也说明了这一点。
- **项目级定义不需要信任确认。** Hook 的项目级文件需要确认，是因为它能不经审批执行命令。Agent 定义只是提示词和工具范围；项目级定义不能放宽权限（P3），子 Agent 的每次调用仍经过权限策略和 Hook，所以不弹信任面板。
- **用户级定义可以放宽模式。** 用户自己写的 `acceptEdits` 或 `bypassPermissions` 是有意的选择；主对话处于计划模式时仍一律为计划模式，保证“规划时不改文件”。
- **本会话授权不继承。** 继承可以少弹几次审批，但用户在主对话中授权的范围是按当时的任务给出的；子 Agent 在后台运行时也看不到审批，继承会让后台任务获得用户没有意识到的权限。
- **空闲时自动开启一轮。** 结果能及时被处理；代价是用户正在输入时会看到模型开始回复。用户输入的内容不受影响，发送后进入 `pending`。
- **`explore` 与 `plan` 用计划模式。** 计划模式已经保证不写文件、只运行只读命令，比 `disallowed_tools` 加提示词可靠；代价是这两个类型不能运行带副作用的检查命令，这正是它们的用途所决定的。
- **结果不进入主历史的原文。** 主对话只看到结果或通知；完整过程在 `subagents/<id>.jsonl` 中，需要时由用户查看，或由模型用 `read_file` 读取。

## 不做

- worktree 隔离、多 Agent 团队与 Agent 间的消息传递。
- 后台任务的跨会话持久化与恢复。
- 插件来源的 Agent 定义。
- Verification 内置 Agent。
- 子 Agent 再创建子 Agent（嵌套）。
- 子 Agent 向用户提问。
- 子 Agent 的 Skill 预加载（Claude Code 的 `skills` 字段）、专属 MCP 服务器（`mcpServers`）、专属 Hook（`hooks`）、持久记忆（`memory`）。
- 运行中向子 Agent 追加指令。
- 模型直接以 Fork 方式运行 fork 模式的 Skill（仍由用户输入 `/<name>`）。

## 实现说明

与上文设计的差异（行为与验收标准不变）：

- Agent 目录不是前缀补充，而是与 Skill 目录并列的稳定系统提示词一节（G2），同一会话中不随请求变化，不影响缓存。
- 没有 `snapshot_request()`：Fork 复制主 `AgentLoop` 中决定请求内容的字段，按同样的方式组装请求。AC4 比较 Fork 与主对话首个请求的稳定系统提示词、前缀补充、工具定义和历史（交给提供商之前的结构），没有按三种协议分别序列化比较；三种协议由同一份结构生成。
- Agent 目录（`AgentCatalog.catalog_section`）和 Fork 指令（`agents/runtime.py`）不在 `prompts.py` 中。
- 运行时是一个 `SubAgents` 类，而不是 `SubAgentSpec` 加 `build_*` 函数；模型侧工具的类名为 `TaskOutput`、`TaskStop`。
- `subagent_start` / `subagent_stop` 只在有 Hook 订阅时触发；`subagent_stop` 在后台执行，不阻塞结果返回。
- B8 的确认面板只统计后台任务；前台子 Agent 随主对话的任务一起取消。

## 已确认的决定（2026-10-29）

| # | 问题 | 决定 |
| --- | --- | --- |
| Q1 | 后台任务完成时主 Agent 空闲，是立即开启新的一轮，还是等用户下一次输入？ | 立即开启（B6） |
| Q2 | 项目级定义能否设置比会话更宽松的权限模式？ | 不能，取更严格的一个（P3）；用户级与内置定义可以 |
| Q3 | Hook 条件 `agent == fork` 是否保持原含义？ | 保持；定义式另用 `agent == subagent`，细分用 `agent_type`（H1） |
| Q4 | 后台子 Agent 是否另设工具白名单？ | 不设，由非交互的权限视图约束（D3、P5） |
| Q5 | 转入后台的按键 | `ctrl+b`；`escape` 保持取消（D5） |

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `agents/__init__.py`：定义解析、别名、校验、三层加载与覆盖；内置三个定义 | — | 单元测试（AC1、AC2） |
| T2 | 隔离的基础：`FileState.copy()`、`ToolRegistry.for_agent`、`PermissionPolicy.view` 与 `stricter`；`subset` 改为基于 `for_agent` | — | 工具与权限测试（AC5、AC6） |
| T3 | `agents/runtime.py`、`agents/tasks.py`：定义式与 Fork 的组装、运行、结果、用量、对话记录、前台转后台、停止、关闭时取消 | T1、T2 | 假提供商测试（AC3、AC4、AC7、AC9、AC11） |
| T4 | `tools/agent.py`、`execution.py` 分组、`prompts.py`（`agents` 前缀补充、Fork 指令、通知）、`agent.py`（`pending` 的通知条目、请求快照） | T3 | 假提供商测试（AC3、AC8、AC10） |
| T5 | `conversation.py`：目录与任务管理器、通知开启新的一轮、Skill fork 迁移、`/clear` / `resume()` / `close()` | T4 | `Conversation` 测试 + 第十一章测试（AC10、AC12、AC13） |
| T6 | Hook：`agent` 取值、`agent_type`、`subagent_start` / `subagent_stop`、`agent` 动作 | T3 | Hook 测试（AC14、AC15） |
| T7 | 界面与命令行：`/agents`、`/tasks`、`/status`；TUI 工具行、状态栏、`ctrl+b`、审批面板标题、B8 确认面板；`-p` 等待与提示；配置项 | T5、T6 | `FakeUI`、`run_test`、`tests/test_cli.py`（AC16–AC18） |
| T8 | 文档：新增 `docs/reference/subagents.md`；更新 README、`docs/reference/commands.md`、`docs/reference/hooks.md`、`docs/reference/permissions.md`、`docs/acceptance.md`；本规格状态改为已实现 | T1–T7 | `tests/test_docs.py`；全量测试（AC19） |

T1 与 T2 互不依赖；T3 完成后，T4 与 T6 互不依赖。

## 验收

| # | 标准 | 方法 |
| --- | --- | --- |
| AC1 | 定义加载：<br>• 项目级覆盖用户级、用户级覆盖内置，`/agents` 标记被覆盖的定义；<br>• 缺 `name`、正文为空、`name: fork`、`max_turns: 0`、未知字段 → 跳过并列出路径与原因；<br>• `disallowedTools`、`maxTurns`、`permissionMode` 等别名与 snake_case 结果相同；<br>• `color` 等 Claude Code 字段被忽略并列为未支持；<br>• 同来源重名时取文件名排序的第一个 | 单元测试（临时目录作为 home 和项目） |
| AC2 | 工具名：`Read` 展开为 `read_file`，`mcp__x__*` 匹配该服务器的工具，不存在的名字在详情中提示且定义可用；`model` 指向不存在的提供商时定义加载、调用返回 `agent_unavailable` | 单元测试 |
| AC3 | 定义式运行（假提供商）：<br>• 首个请求的系统提示词含定义正文、环境信息、项目说明，不含 Skill 目录与 Agent 目录；历史只有 `prompt`；<br>• 工具定义中没有 L1 的工具，`tools` / `disallowed_tools` 生效；<br>• 结果是最后一条回复加统计；<br>• 达到 `max_turns` 时结果以 `[子 Agent 未完成` 开头；`max_turns` 超过全局值时取全局值；<br>• 结果过大时写入会话目录，工具结果为预览 | 假提供商测试 |
| AC4 | Fork（假提供商记录请求）：<br>• 首个请求的系统提示词、工具定义、截至当前助手消息的历史与主对话最近一次请求相同（序列化后比较），三种协议分别检查；<br>• 同一条助手消息中两个 Fork 的请求只有最后一条用户消息不同；<br>• Fork 调用 `agent`、`memory` 返回 `subagent_forbidden`；<br>• 调用立即返回任务 id | 提供商请求结构测试 |
| AC5 | 文件状态：<br>• 定义式子 Agent 未读文件就编辑 → 要求先读；<br>• Fork 编辑父对话读过的文件 → 成功；<br>• 子 Agent 写入后父 Agent 编辑同一文件 → `stale_read`；<br>• 子 Agent 读过的文件不影响父 Agent 的检查 | 工具测试 |
| AC6 | 权限：<br>• 主对话为计划模式时，`general` 的写入被 `plan_blocked` 拒绝；<br>• 项目级定义写 `bypassPermissions`、会话为 `default` → 有效模式为 `default`；同样的用户级定义 → `bypassPermissions`；<br>• `bypassPermissions` 下拒绝规则与受保护文件仍生效；<br>• 子 Agent 中授予的本会话授权不出现在主对话，反之亦然；<br>• 后台子 Agent 需要审批的调用返回 `approval_required`，审批队列中没有请求 | 权限测试 |
| AC7 | 前台审批：前台子 Agent 的审批请求出现在共用队列，标签含类型与标签；转入后台时等待中的审批按拒绝处理 | 权限 + 任务测试 |
| AC8 | 并行：一次回复中三个相邻的 `agent` 调用并发运行（用假提供商的阻塞点确认同时在运行），结果按调用顺序返回 | 工具执行测试 |
| AC9 | 转入后台：<br>• `run_in_background: true`、定义中 `background: true` 立即返回任务 id；<br>• 设置 `detach` 后工具返回“已转入后台”，子 Agent 继续并完成；<br>• `agent_auto_background: 1` 时运行超过 1 秒自动转入后台，`0` 时不转入；<br>• 第 5 个后台任务返回 `too_many_tasks` | 任务测试 |
| AC10 | 通知：<br>• 主 Agent 忙时，通知在下一次请求前合并，消息的 `synthetic` 为 `task-notification`；<br>• 空闲时立即开启一轮，两个同时完成的通知合并为一轮；<br>• 每个通知只送达一次；`task_stop` 停止的任务不送达 | `Conversation` 测试 |
| AC11 | 用量与记录：子 Agent 每次请求的用量计入任务与会话总量；`subagents/<id>.jsonl` 存在且可解析；`persist_sessions: false` 时不写；主对话记录中的任务记录在恢复时不重放 | 任务测试 + 会话测试 |
| AC12 | 取消与会话切换：<br>• 取消主对话任务时前台子 Agent 被取消，后台的不受影响；<br>• `/clear`、`resume()`、`close()` 取消运行中的任务并记为 `cancelled`，之后任务列表为空 | `Conversation` 测试 |
| AC13 | Skill fork 迁移：第十一章的测试全部通过；Skill fork 出现在 `/tasks` 中；Skill fork 的 Hook 事件 `agent == fork`、`agent_type == skill:<name>` | 现有测试 + Hook 测试 |
| AC14 | Hook 字段与事件：<br>• 定义式子 Agent 的工具调用 `agent == subagent`、`agent_type == explore`；Agent 的 Fork 式 `agent == fork`；<br>• `subagent_start` 在第一次请求前触发，其 prompt 出现在子 Agent 第一条用户消息之前；<br>• `subagent_stop` 的 `status` 正确；<br>• 子 Agent 中不触发 `turn_start`、`pre_send` | 假提供商测试 |
| AC15 | `agent` 动作：启动后台子 Agent，结果写入 `hooks.log` 且未进入主历史；该子 Agent 的工具事件中的 `agent` 动作记为 `skipped`；超时后任务为 `cancelled`、日志为 `timeout` | Hook 测试 |
| AC16 | 命令：<br>• `/agents` 列表、详情（实际工具、有效模式）、`reload` 报告变化；<br>• `/tasks` 列表、详情、`stop`；<br>• `-p "/agents"`、`-p "/tasks"` 退出码 2 | `FakeUI` 测试、`tests/test_cli.py` |
| AC17 | TUI：前台子 Agent 的工具行实时更新并在完成后显示统计；`ctrl+b` 转入后台后工具行显示任务 id；状态栏显示后台任务数；有运行中任务时 `/clear` 弹出确认面板 | `run_test` 测试 |
| AC18 | `-p`：后台任务完成后开启新的一轮并输出结果，所有任务结束后退出；stderr 与 stream-json 有任务开始与结束的提示 | `tests/test_cli.py`（假提供商） |
| AC19 | 全量测试通过；README 二级标题不变，文档链接有效 | `pytest`、`tests/test_docs.py` |

人工验收（写进 `docs/acceptance.md`）：

- 在验收项目中让模型“用 explore 找出配置是在哪里加载的”，确认工具行实时显示子 Agent 的进度，完成后主对话只收到结论；
- 让模型“分两个子 Agent 分别看 `src/` 和 `tests/` 的结构”，确认两个子 Agent 同时运行；
- 让模型用 `general` 做一个较长的任务，运行中按 `ctrl+b`，确认工具行变为“后台 · 任务 t…”，可以继续对话，完成后出现通知提示并自动开启一轮；
- 讨论一段代码后让模型“Fork 一个去写测试”，确认立即返回任务 id；用 DeepSeek（或开启 `prompt_cache` 的提供商）时在 `/tasks <id>` 中确认 Fork 的缓存命中 Token 接近父对话的上下文长度；
- 在计划模式下让模型用 `general` 修改文件，确认被计划模式拒绝；
- 前台子 Agent 执行需要审批的命令，确认审批面板标题标明子 Agent；后台子 Agent 执行同样的命令，确认返回需要审批、没有弹出面板；
- 在 `.mosscode/agents/` 写一个同名的 `explore` 覆盖内置定义，`/agents reload` 后确认 `/agents` 标记覆盖、调用使用新定义；
- `/tasks`、`/tasks <id>`、`/tasks stop <id>` 的显示与效果；有运行中任务时 `/clear`，确认弹出确认面板；
- `-p` 中让模型启动一个后台子 Agent，确认等待其完成后才退出；
- Windows Terminal 中检查工具行进度、状态栏、确认面板与 `/agents`、`/tasks` 的显示。
