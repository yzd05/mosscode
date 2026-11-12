# 子 Agent

主对话可以把一段能独立完成的工作交给子 Agent：子 Agent 有自己的对话历史，在里面搜索、读取、修改、运行命令，最后只把结论交回主对话。大量的中间输出不会占用主对话的上下文；互不依赖的几件事可以同时进行。

子 Agent 有两种：

- **定义式**：`agent_type` 写一个 Agent 定义的名字（如 `explore`）。子 Agent 从空白对话开始，按定义里的角色和工具工作，主对话要在 `prompt` 中写清楚完整的任务和背景。
- **Fork**：省略 `agent_type`。子 Agent 继承主对话到目前为止的全部内容，在后台运行，完成后主对话收到通知。适合“接着刚才的讨论，把这一块单独做完”。

Skill 的 fork 模式（如 `/review`）也用同一套运行时，见 [Skill](skills.md#两种执行方式)。

定义式子 Agent 可以在独立的 git 工作树中运行，改动不碰主目录，见 [Git 工作树](worktree.md)。

## Agent 定义

一个定义是一个 Markdown 文件：YAML frontmatter 写配置，正文是子 Agent 的系统提示词。

```markdown
---
name: test-runner
description: 运行测试并修复失败。prompt 中写明要测的范围。
tools: Read, Grep, Glob, Bash, Edit
max_turns: 40
---

你是测试修复子 Agent。先运行指定范围的测试，逐个分析失败……
```

### 存放位置

| 位置 | 来源 |
| --- | --- |
| MossCode 自带 | 内置 |
| 已安装插件的 `agents/*.md` | 插件，见 [插件](plugins.md) |
| `~/.claude/agents/*.md` | 用户级（Claude Code 的目录，`claude_directories: true` 时读取） |
| `~/.mosscode/agents/*.md` | 用户级，所有项目可用 |
| `<项目>/.claude/agents/*.md` | 项目级（Claude Code 的目录，`claude_directories: true` 时读取） |
| `<项目>/.mosscode/agents/*.md` | 项目级，随仓库共享 |

同名定义按 内置 < 插件 < 用户 < 项目 覆盖，同一级中 `.mosscode/agents/` 覆盖 `.claude/agents/`，`/agents` 中标出被覆盖的个数。`claude_directories` 的说明见 [Skill](skills.md#存放位置)。同一目录中两个文件写了同一个 `name` 时，按文件名排序靠前的生效，另一个列为问题。

定义在会话开始时加载一次；修改文件后 `/agents reload` 重新加载，下一次请求起生效。

### 字段

| 字段 | 说明 |
| --- | --- |
| `name` | 必填。小写字母开头，只含小写字母、数字和 `-`。不能是 `fork` |
| `description` | 必填，最多 1024 个字符。模型据此决定什么时候用这个类型，要写清楚适用的任务和 `prompt` 里需要给什么 |
| `tools` | 工具白名单：列表或逗号分隔的字符串。不写表示主对话的全部工具（L1 的除外） |
| `disallowed_tools` | 工具黑名单，先于白名单生效 |
| `model` | 配置中的提供商名称（与 `/model` 的参数相同）；不写或写 `inherit` 使用当前提供商。Claude Code 的模型名 `sonnet`、`opus`、`haiku`、`fable` 和以 `claude-` 开头的名称，没有同名提供商时也使用当前提供商 |
| `max_turns` | 最多请求模型的次数，正整数；不写时与主对话的 `max_iterations` 相同，写了也不会超过它 |
| `permission_mode` | `default`、`acceptEdits`、`plan`、`bypassPermissions`，见 [权限](#权限) |
| `background` | `true` 时这个类型总在后台运行 |
| `isolation` | `worktree` 时总在独立的 git 工作树中运行，见 [Git 工作树](worktree.md#子-agent-隔离)；缺省 `none` |

- `disallowed_tools`、`max_turns`、`permission_mode` 也接受 Claude Code 的写法 `disallowedTools`、`maxTurns`、`permissionMode` 和连字符写法，所以 Claude Code 的 `.claude/agents/*.md` 复制过来大多可以直接用。
- 工具名接受权限规则的别名（`Read`、`Write`、`Edit`、`Bash`、`Glob`、`Grep` 等）和实际工具名，可以用 `*` 通配（`mcp__github__*`）。`Bash(git:*)` 这类写法只取括号前的工具名。
- 写了当前没有的工具名不影响加载（MCP Server 可能还没连上），在 `/agents <名称>` 中列出。
- Claude Code 的 `color`、`effort`、`skills`、`mcpServers`、`hooks`、`memory`、`initialPrompt` 不支持，读取时忽略，在 `/agents <名称>` 中列出。
- YAML 错误、缺少必填字段、取值非法或拼错的字段名（如 `disalowed_tools`）会让这个文件被跳过：启动时提示一行，`/agents` 列出路径和原因。
- `model` 指向不存在的提供商时定义照常加载，`/agents` 中标出；调用这个类型时返回错误，不会改用当前模型。Claude Code 的模型名例外：`/agents <名称>` 注明“model: sonnet 不是这里的 Provider，使用当前模型”，运行时使用当前模型。

### 内置定义

| 名称 | 用途 | 权限模式 | 最大轮数 |
| --- | --- | --- | --- |
| `explore` | 只读的代码探索：查找实现、理清调用链、了解项目结构 | `plan` | 30 |
| `plan` | 只读的方案规划：探索后给出分步实现方案 | `plan` | 30 |
| `general` | 通用，工具与主对话相同，用于过程不必留在主对话中的多步任务 | 继承会话 | 与主对话相同 |

在项目或用户目录中写同名文件可以替换它们。`explore` 和 `plan` 以 plan 档位运行，也可以用 `web_fetch` 查资料，每个新的域名照常确认；在后台运行时没有被规则放行的读取返回 `approval_required`。

## agent 工具

模型通过 `agent` 工具启动子 Agent。系统提示的“子 Agent”一节列出可用的类型（名称、描述、工具范围），工具本身的说明不随定义变化。

| 参数 | 说明 |
| --- | --- |
| `description` | 必填，3–10 个字的任务标签，显示在界面和 `/tasks` 中 |
| `prompt` | 必填，交给子 Agent 的任务说明 |
| `agent_type` | 定义名；省略或写 `fork` 为 Fork |
| `run_in_background` | 只对定义式有效，`true` 时在后台运行 |
| `isolation` | `none`（缺省）或 `worktree`：在独立的 git 工作树中运行，结束时没有改动就删除，有改动就保留并在结果末尾说明分支和合并方法。只对定义式有效 |

- **结果**：子 Agent 最后一条回复的正文，末尾附一行统计（工具调用次数、Token、用时）。达到轮数上限、模型出错或被取消时，结果以 `[子 Agent 未完成：原因]` 开头，后面附上它最后写的回复。结果过长时与其他工具一样，完整内容存入会话目录，模型只看到预览。
- **并行**：一次回复中相邻的多个 `agent` 调用同时运行，最多 4 个。
- **错误**：类型不存在返回 `unknown_agent_type`（列出可用的类型）；类型的 `model` 无效返回 `agent_unavailable`；后台任务已达上限返回 `too_many_tasks`；Fork 要求隔离返回 `isolation_unsupported`；不在 git 仓库中或无法创建工作树返回 `worktree_unavailable`。
- 计划模式中主对话也有 `agent` 工具，子 Agent 同样处于计划模式。
- `agent` 工具本身不需要审批；子 Agent 的每次工具调用各自按权限检查。

### Fork

- Fork 的第一个请求与主对话最近一次请求共用同一个缓存前缀：系统提示、工具定义和直到这次调用为止的历史逐字节相同，只在末尾多一条工具结果（固定文字“已在后台分出 Fork 处理。”）和一条用户消息（Fork 指令加 `prompt`）。同一条回复中的几个 Fork 只有最后这条消息不同，所以 Fork 的首个请求通常大部分命中缓存。
- Fork 指令要求它：不再创建子 Agent，不向用户提问，只做这项任务，最终报告以“范围：”开头，依次写范围、结果、涉及的关键文件和未完成的部分。
- Fork 总在后台运行，`agent` 调用立即返回任务 id。
- Fork 的工具定义与主对话相同（为了缓存），但调用 `agent`、`ask_user`、`load_skill`、`memory`、`exit_plan_mode`、`task_output`、`task_stop` 会返回 `subagent_forbidden`。

## 前台与后台

定义式默认在前台运行：`agent` 调用等子 Agent 完成，结果直接作为工具结果。以下情况在后台运行，`agent` 调用立即返回任务 id：

- `run_in_background: true`；
- 定义中写了 `background: true`；
- Fork。

前台的子 Agent 也可以转入后台，`agent` 调用随即以“已转入后台：任务 t3”返回，子 Agent 继续运行：

- 运行超过 120 秒时自动转入（配置项 `agent_auto_background`，单位秒，0 表示不自动转入）；
- 界面中按 **Ctrl+B**，当前所有前台子 Agent 都转入后台（主对话的前台命令也一起转入，见 [后台命令](tools.md#后台命令)）。

取消：Esc / Ctrl+C 取消主对话当前的任务时，其中的前台子 Agent 一起取消，工具结果为 `[子 Agent 未完成：已取消]`；后台子 Agent 不受影响，用 `/tasks stop <id>` 或由模型调用 `task_stop` 停止。

### 通知

后台任务结束时，主对话收到一条通知：

```text
<task-notification>
任务：t3
类型：explore
标签：查找配置加载
状态：已完成
用量：输入 12345 Token（缓存命中 9800），输出 678 Token，用时 41 秒
结果：
……
</task-notification>
以上是后台任务的结果；根据需要继续处理，或向用户报告。
```

- 主对话正忙时，通知在下一次请求模型之前加入上下文（与运行中排队的消息一样）；主对话空闲时，通知立即开启新的一轮。同时完成的几个通知合并为一轮。每个通知只送达一次。
- 通知在历史中是一条特殊的用户消息，界面上不显示为用户气泡：任务开始和结束时各显示一行“后台任务 t3（explore · 查找配置加载）已启动 / 已完成”，恢复会话时显示为“已收到后台任务通知：t3”。
- 被停止（`/tasks stop`、`task_stop`）或因 `/clear`、恢复会话、退出而取消的任务不发通知。
- 不想等通知时，模型可以用 `task_output`（参数 `task_id`，可选 `wait` 秒数，最多 300）查看状态和结果。

后台同时运行的子 Agent 最多 4 个（配置项 `agent_max_background`）。后台命令也是任务，与子 Agent 共用任务 id、`/tasks`、`task_output` 和 `task_stop`，但另有自己的上限，见 [后台命令](tools.md#后台命令)。

## 隔离与共享

每个子 Agent 独立的：

- 对话历史，需要时按自己的上下文窗口自动压缩；
- 文件读取记录：定义式从零开始，编辑文件前要先读取；Fork 从主对话已读文件的记录开始。子 Agent 改过的文件，主对话再次编辑前会被要求重新读取（`stale_read`）；
- 权限视图，见下；
- Token 用量：同时计入该任务和本会话的总用量（状态栏、`/status`）。

与主对话共享的：提供商连接（未指定 `model` 时）、MCP 连接、Hook、项目根目录、shell 配置和会话目录。隔离在工作树中的子 Agent 以工作树为根目录，只有内置工作区工具，见 [Git 工作树](worktree.md#运行)。

定义式子 Agent 的系统提示词是定义正文，加上环境信息（工作目录、平台、git 状态）和项目指令（`AGENTS.md` / `CLAUDE.md`）；不含 Skill 目录、Agent 目录、记忆和 `session_start` Hook 的注入。它的对话不进入主对话历史，也不参与自动记忆。

## 权限

子 Agent 与主对话共用规则文件、受保护文件、硬限制和审批界面，有自己的档位和“本会话允许”的授权：它从空授权开始，审批中授予的也不带回主对话。

有效档位在子 Agent 启动时确定，主对话之后切换档位不影响已在运行的子 Agent：

| 情况 | 有效档位 |
| --- | --- |
| 主对话处于 `plan` | 一律 `plan` |
| 定义没写 `permission_mode`，或 Fork | 主对话当前的档位 |
| 项目级定义 | 定义与主对话中更严格的一个（`plan` > `default` > `acceptEdits` > `bypassPermissions`） |
| 用户级、内置定义 | 定义中的档位 |

项目级定义随仓库而来，所以它只能收紧、不能放宽主对话的档位；用户级定义是你自己写的，可以放宽。

- 前台子 Agent 的审批照常弹出，面板标题注明来源，例如“需要授权 · 写入文件 · 子 Agent general · 批量改名”。
- 后台子 Agent 不能请求审批：需要审批的调用直接返回 `approval_required`，模型会收到“后台子 Agent 不能请求审批，可以添加允许规则或在前台运行”。前台子 Agent 正在等审批时转入后台，这次审批按拒绝处理。需要后台写文件时，可以在 `permissions.yaml` 中添加对应的 allow 规则。
- 拒绝规则、计划模式、受保护文件、危险命令检查和 `pre_tool_use` Hook 对子 Agent 一样生效。
- 子 Agent 不能使用 `agent`、`ask_user`、`load_skill`、`memory`、`exit_plan_mode`、`task_output`、`task_stop`：定义式的工具列表中没有它们，Fork 调用时返回 `subagent_forbidden`。

## 命令与界面

| 命令 | 作用 |
| --- | --- |
| `/agents` | 列出定义：名称、来源、描述，标出覆盖与不存在的模型，列出有问题的文件 |
| `/agents <名称>` | 详情：路径、覆盖的文件、模型、声明的与本会话实际的权限档位、最大轮数、隔离方式、白名单、黑名单、实际可用的工具（隔离时不可用的工具单独列出）、不支持的字段和不存在的工具、提示词首行 |
| `/agents reload` | 重新加载定义，报告新增、移除和修改的类型 |
| `/tasks` | 列出本会话的任务（子 Agent 和后台命令）：id、状态、前台/后台、类型与标签、用时、Token（命令显示输出大小） |
| `/tasks <id>` | 详情：来源、统计、用量（含缓存命中）、最近一次工具调用、工作树（路径、分支、保留或删除）、对话记录路径、任务内容和结果预览；命令任务显示命令、工作目录、沙箱、输出文件和最后几行 |
| `/tasks stop <id>` | 停止运行中的任务，不再收到它的通知；停止前台子 Agent 时，主 Agent 收到 `[子 Agent 未完成：已取消]` 并继续 |

- 隔离的子 Agent 的工具行标出“工作树”，进度前缀为工作树名称。
- 前台子 Agent 的工具行实时显示它调用工具的次数、最近一次调用、Token 和用时；子 Agent 自己的工具调用不单独成行。完成后显示结果的前几行和统计；后台运行时显示“后台 · 任务 t3”。
- 状态栏显示运行中的后台任务数（含后台命令）；有前台子 Agent 或前台命令时提示 `Ctrl+B 转入后台`。
- `/status` 中显示 Agent 定义数和运行中的后台任务数。
- `/clear`、恢复另一个会话、`/exit` 和 Ctrl+Q 时如果还有后台任务在运行，先弹出确认：“取消这些任务并继续”或“返回”（缺省，Esc 同）。确认后任务取消（最多等 5 秒），不再发通知。
- 任务属于会话：`/clear` 和恢复会话之后任务列表为空，恢复的会话不会重新运行旧任务。

## 无界面模式

- `-p` 的一轮结束后，如果还有后台任务（包括后台命令）在运行，MossCode 等它们完成，每批通知开启新的一轮，全部结束后才退出；Ctrl+C 取消所有任务并退出。
- 后台任务开始和结束时，stderr 打印一行（stream-json 中为 `{"type": "notice"}` 事件）。
- json / stream-json 的结果对象描述最后一轮；`usage` 是全部轮次（含子 Agent）的合计。
- `/tasks` 在主对话运行期间也可以使用（其他命令需等任务完成），用于查看进度或停止任务。
- `/agents`、`/tasks` 只能在交互界面中使用，`-p` 中输入以退出码 2 结束。

## 对话记录

保存会话时（`persist_sessions` 为真），每个子 Agent 的消息写入 `<会话目录>/subagents/<任务 id>.jsonl`，第一行是任务信息，之后的格式与主对话记录相同。主对话记录中为每个任务写开始和结束两条 `{"type": "task"}` 记录（id、类型、标签、状态、路径），恢复会话时跳过它们。

## Hook

- 子 Agent 中只触发工具事件（`pre_tool_use`、`post_tool_use`、`permission_request`）：定义式中 `agent` 为 `subagent`，Fork 中为 `fork`；`agent_type` 为类型名（Agent 的 Fork 为 `fork`，Skill fork 为 `skill:<名称>`）。
- `subagent_start`、`subagent_stop` 在启动它的一方触发；`subagent_start` 的 prompt 放在子 Agent 第一条用户消息之前。
- 隔离的子 Agent 的工具事件和 `subagent_start` / `subagent_stop` 带 `worktree` 字段，`subagent_stop` 另有 `worktree_kept`。
- `agent` 动作在后台启动一个定义式子 Agent，结果写入 `hooks.log` 并在 `/tasks` 中可见。

详见 [Hook](hooks.md)。

## 配置

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `agent_auto_background` | `120` | 前台子 Agent 运行多少秒后自动转入后台，0–3600，0 表示不自动转入 |
| `agent_max_background` | `4` | 同时运行的后台子 Agent 上限 |
| `command_max_background` | `8` | 同时运行的后台命令上限，0–64，0 表示关闭后台命令，见 [后台命令](tools.md#后台命令) |
| `command_auto_background` | `5` | 主对话的前台命令运行超过这么多秒就自动转入后台，0–600，0 表示不自动转，见 [后台命令](tools.md#后台命令) |
