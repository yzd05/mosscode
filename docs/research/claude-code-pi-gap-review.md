# 第 2–7 章完善建议：对照 Claude Code 与 Pi

日期：2026-10-15。范围：UI、工具、系统提示词、Agent Loop、权限、MCP；暂不涉及上下文压缩、会话持久化。依据是当前 `src/mosscode` 源码的逐文件阅读，以及 Claude Code 的公开工具契约和 [Pi 调查](pi-harness-sources.md)。本文只是评审和规划，还没有做任何代码改动。

目标定位：轻量级的终端 coding agent，代码量小、便于学习和修改，作为 Claude Code 风格架构的参考实现。因此建议分成两类：一类补齐“真实 agent 必需”的行为，一类删减为兼容和过度防御而增加的复杂度。

## 总体判断

架构骨架是对的：单一 AgentLoop、统一消息与事件、Provider 适配集中、工具在执行边界统一校验和授权、TUI 和 `--prompt` 共用同一个内核。差距主要不在架构，而在**模型实际看到什么、要花多少钱，以及日常使用时的摩擦**：

1. 模型侧：工具结果的格式、缓存方式、输出上限、重试，会直接影响任务成功率和成本，用户却看不到。
2. 工具契约：read、edit、shell、grep 的参数和行为与主流 agent 不一致。模型对 Claude Code 风格的契约最熟悉，契约越接近，误用越少。
3. 交互摩擦：单行输入，生成期间不能输入；`default` 档下每条命令都要审批；没有 diff 展示。这些让它“用起来不像 coding agent”。
4. 部署形态：配置只从当前目录读取 `config.yaml`，也不加载项目指令（AGENTS.md / CLAUDE.md），因此还不能在任意项目目录里直接运行。

## 一、模型侧正确性与成本（优先级最高）

| # | 现状 | 问题 | 建议 |
|---|------|------|------|
| A1 | `ToolResult.serialize()` 把结果包成 `{"ok","output","code","details"}` JSON 发给模型（[messages.py:77](../../src/mosscode/messages.py)、[providers.py:19](../../src/mosscode/providers.py)） | 换行被转义，代码难读，还多耗 token。`execute_command` 的 details 里又带一份 stdout/stderr（[process.py:83](../../src/mosscode/tools/process.py)），**命令输出发送了两次** | 模型只收到纯文本 `output`；失败时设置 `is_error`，再加一行简短错误码。`details` 只给 UI 和测试用 |
| A2 | Anthropic 只在系统提示上设缓存断点（[providers.py:111](../../src/mosscode/providers.py)）；环境信息和模式提醒放在断点之后，而且每轮会变化 | 多轮工具循环中，历史消息从不命中缓存。轮次越多，费用越接近“每轮全价重发” | 在最后一条消息上加第二个 `cache_control` 断点（Claude Code 的做法）。会变化的提醒移到最新一条 user/tool_result 消息里，用 `<system-reminder>` 包裹，系统提示保持完全稳定 |
| A3 | 默认 `max_output_tokens: 4096`；`stop_reason == "max_tokens"` 直接报错（[providers.py:161](../../src/mosscode/providers.py)） | `write_file` 一写大文件就会被截断，然后整轮报错。这是真实任务中的常见失败 | 示例配置默认提高到 16k–32k。遇到 `max_tokens`：如果工具调用不完整，就返回一个错误结果让模型重试；如果是正文，就保留已有内容并提示 |
| A4 | SDK `max_retries=0`，README 写明不自动重试 | 429、529/overloaded、连接重置都会让长任务直接中断 | **在收到第一个流事件之前**，对 429/5xx/连接错误做指数退避重试（如 3–5 次）。这时还没有执行任何副作用，重试是安全的。已开始流式输出后仍按现在的方式报错 |
| A5 | 默认最多 25 轮（[agent.py:245](../../src/mosscode/agent.py)） | 真实修 bug 加跑测试，常常超过 25 次工具往返；Claude Code 和 Pi 都不设这么低的上限 | 改为可配置，默认 100–200（或不设上限，靠 Esc 取消）。保留“连续未知工具”这条保护 |

## 二、系统提示词

| # | 建议 | 理由 |
|---|------|------|
| P1 | **加载项目指令**：项目根目录及其父目录中的 `AGENTS.md`（兼容 `CLAUDE.md`），加上 `~/.mosscode/AGENTS.md`，作为单独的稳定段落放在系统提示之后 | Claude Code 和 Pi 都有。这是用户定制 agent 行为成本最低的方式，代码量也很小 |
| P2 | 环境信息补充：是否为 git 仓库、当前分支、启动时 `git status --short` 的摘要（有截断）、模型名 | Claude Code 在会话开始时提供 git 快照，模型会少做几次探测 |
| P3 | 模式提醒只在**模式变化时**注入一次，取消“每 5 轮重复一次” | 周期性提醒是噪声，还会破坏缓存（见 A2）。Claude Code 只在状态变化时插入 reminder |
| P4 | 在提示中明确鼓励**并行调用彼此独立的只读工具**，并要求简洁回复、不复述计划 | 执行层已经支持 4 路并发读取，但提示里没有说，模型很少会用到 |
| P5 | 整体精简：七个模块中有不少重复的表述（权限、证据、未验证），可以压缩约 30%。具体用法规则仍由工具贡献（保留现有的 Pi 式设计） | Pi 的系统提示很短，主要依靠工具描述。短提示也让定制版 `system_prompt` 更容易写 |

## 三、工具契约

| # | 工具 | 建议 |
|---|------|------|
| T1 | read_file | 输出格式改为 `cat -n` 风格（右对齐行号 + Tab），默认 2000 行。读取项目外的文件改为 Ask，不再硬拒绝，因为查看依赖库源码是常见需求 |
| T2 | edit_file | 增加 `replace_all`。**强制“先读再改”**：记录每个文件最后一次读取时的 mtime/哈希，没读过、或读过之后文件被外部改动过，就拒绝编辑和覆盖写，提示重新读取。成功后返回 diff 片段，供 UI 展示 |
| T3 | write_file | 覆盖已有文件时同样做读取检查。新建文件不受限制 |
| T4 | execute_command | 默认超时 120s，最大 600s（目前最大 120s，跑测试和构建不够用）。**输出截断改为保留头部和尾部**：现在只保留前 32 KiB，而 pytest 和编译器的失败摘要恰好在末尾 |
| T5 | execute_command（Windows） | 如果检测到 Git Bash，优先使用 bash，否则退回 PowerShell，并允许在配置中指定。模型写 bash 的准确率明显高于 PowerShell，Claude Code 在 Windows 上也用 Git Bash |
| T6 | grep | 去掉 `--no-ignore`，遵守 `.gitignore`（目前会搜到 `.test-artifacts` 之类的目录）。增加 `output_mode`（默认 `files_with_matches`，另有 `content`、`count`）、`-i`、`context`、`head_limit` |
| T7 | glob | 遵守 `.gitignore`，结果按修改时间倒序排列（最近改动的文件更相关） |
| T8 | 新增（可选） | `todo_write`：长任务的进度清单，显示在 UI 中。Pi 没有这个工具，Claude Code 有；建议做成可选，放在后面 |

## 四、Agent Loop

| # | 建议 | 理由 |
|---|------|------|
| L1 | **消息排队 / steering**：生成期间允许输入，消息在下一次迭代边界作为 user 消息并入（Pi 的 steering/follow-up，Claude Code 的排队消息） | 用户发现模型方向错了，可以纠正而不必取消；这也是 UI 改进的前提 |
| L2 | 合并模式状态：现在 `AgentLoop._mode` 和 `permissions.mode` 是两份状态，靠 `resume_mode` 同步（[agent.py:265](../../src/mosscode/agent.py)）。改为 plan 只是权限模式之一，loop 直接读取 | 减少一份状态，`/plan` 和 `/do` 的逻辑也更简单 |
| L3 | 重试、`max_tokens` 处理放在 Provider 边界（见 A3、A4），loop 不感知 | 保持 loop 简单 |
| L4 | 无界面模式：增加 `-p` 别名，以及 `--output-format json` / `stream-json` | 后续做任务评估（完成率、轮次、token）时直接可用；Claude Code 和 Pi 都有类似的机器可读输出 |

## 五、权限与计划模式

| # | 建议 | 理由 |
|---|------|------|
| R1 | **只读命令白名单自动放行**：`git status/diff/log/show`、`ls`、`pwd`、`rg`、`pytest --collect-only` 等，前提是通过现有的 `simple_command` 检查（没有管道、重定向、变量） | 现在 `default` 档下连 `git status` 都要审批，摩擦很大 |
| R2 | **前缀规则**：`Bash(pytest:*)`、`Bash(npm run test:*)`。审批时多提供一个选项：“本会话 / 永久允许以 `pytest` 开头的命令” | 现在只有完全一致的精确匹配，`pytest tests/a.py` 和 `pytest tests/b.py` 要分别审批，永久规则会越积越多 |
| R3 | 编辑审批增加“本会话允许所有编辑”（选中后切换到 acceptEdits）；Shift+Tab 在 default → acceptEdits → plan 之间循环 | 与 Claude Code 一致，几乎不用改代码 |
| R4 | **计划模式改为 Claude Code 语义**：只开放只读工具（包括 R1 中的只读命令），不再提供“经审批后写入”。新增 `exit_plan_mode(plan)` 工具，弹出“执行计划 / 继续规划”；选择执行后恢复之前的模式并继续 | 现在“计划模式里也能经审批写入”与“自动模式”区别不大，提示词和提醒还要专门解释。专门的退出工具让计划有明确的交付点，也可以去掉 `/do` 的拼接提示 |
| R5 | 危险命令分两级：系统级灾难操作（删除根目录、格式化、写块设备、fork 炸弹）保持硬拒绝；`git reset --hard`、`git push --force`、`git clean -f`、`rm -rf <目录>` 改为**每次强制询问**（任何规则和 bypass 都不能自动放行） | 现在这些操作即使用户明确要求也“不能授权”，用户只能退出程序手动执行 |
| R6 | 审批面板的 edit/write 预览改为统一 diff | 用户靠 diff 做判断，比对照原文和新文本快得多 |
| R7 | 删除旧版 `strict` 模式和 `permissive` 别名 | 轻量化；这些只为兼容旧配置而存在，项目还没有外部用户 |

## 六、MCP

| # | 建议 |
|---|------|
| M1 | 规则支持整个 Server：`mcp__server` 或 `mcp__server__*` 一次放行该 Server 的全部工具（Claude Code 支持） |
| M2 | 发现过程不阻塞输入：启动时可以先打字，工具就绪后再加入下一轮请求；增加 `/mcp` 命令，查看各 Server 的状态和工具列表 |
| M3 | 每个 Server 可配置 `trust_read_only_hint: true`，把带 readOnlyHint 的工具视为只读，允许在计划模式中使用并自动放行。默认仍保守 |
| M4 | 兼容读取 Claude Code 格式的项目文件 `.mcp.json`（`mcpServers`），方便复用现有配置。可选 |
| M5 | MCP 结果复用 A1 的纯文本格式；超时可以按 Server 配置 |

## 七、UI

| # | 建议 |
|---|------|
| U1 | 输入框改为多行 TextArea：Enter 发送，Shift+Enter 或 Ctrl+J 换行；支持粘贴多行内容；↑/↓ 浏览输入历史 |
| U2 | 生成期间输入框保持可用，提交的内容进入队列（配合 L1），状态栏显示“已排队 N 条” |
| U3 | edit/write 结果以彩色 diff 显示（红删绿增，带行号），这是 Claude Code 最有辨识度的界面元素 |
| U4 | 状态栏显示权限模式，例如 `⏵⏵ accept edits on (shift+tab 切换)`，以及模型名和累计 token |
| U5 | 命令调整：新增 `/clear`（新会话）、`/model`（切换 Provider）、`/mcp`；删除只有一个选项的 `/theme`；在计划模式改用 `exit_plan_mode` 后，`/do` 可以去掉 |
| U6 | 可选：`@路径` 文件补全，`!命令` 直接执行 shell 并把输出放入上下文 |

## 八、部署与配置

| # | 建议 |
|---|------|
| C1 | 配置查找顺序：`--config` → `<项目>/.mosscode/config.yaml` → `~/.mosscode/config.yaml`。现在只读当前目录的 `config.yaml`，换一个项目目录就无法启动 |
| C2 | README 精简为“安装 → 配置 → 使用 → 架构概览”；把各章细节移到对应规格文档，避免 README 和规格文档重复维护同一事实 |

## 建议的推进顺序

每个阶段都可以独立验收，并保持现有 157 个测试的回归。

| 阶段 | 内容 | 依赖 | 验收要点 |
|------|------|------|----------|
| 1 模型侧 | A1–A5、P1–P3、C1 | 无 | 模拟 Provider 断言发出的请求结构（纯文本结果、两个缓存断点、reminder 位置）；`max_tokens` 和重试场景；在另一个目录中启动 |
| 2 工具 | T1–T7 | 阶段 1 的结果格式 | 读后改与外部修改检测；头尾截断；grep 遵守 .gitignore；Windows 下的 bash 选择 |
| 3 权限与计划 | R1–R7、L2 | 阶段 2（diff 生成） | 只读命令免审批；前缀规则匹配与保存；计划模式只读，`exit_plan_mode` 审批后恢复原模式；危险命令强制询问 |
| 4 交互 | U1–U5、L1、L4 | 阶段 3 | 多行输入与历史；运行中排队并在迭代边界并入；diff 渲染；Shift+Tab；JSON 输出 |
| 5 MCP | M1–M5 | 阶段 3 的规则格式 | 整个 Server 的规则；不阻塞输入的启动；`/mcp` |

## 需要你决定的取舍

以下几项会改变功能语义。括号内是推荐选项：

1. Windows 默认 shell：（推荐）检测到 Git Bash 时使用 bash，否则用 PowerShell；或者保持 PowerShell。
2. 项目外的文件：（推荐）读取改为 Ask，写入保持硬拒绝；或者读写都保持硬拒绝。
3. 计划模式：（推荐）只读加 `exit_plan_mode`；或者保留现在“经审批可写”的语义。
4. 危险命令：（推荐）灾难级硬拒绝，其余强制询问；或者全部保持硬拒绝。
