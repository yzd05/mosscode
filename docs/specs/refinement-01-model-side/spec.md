# 完善阶段 1：模型侧正确性与成本

日期：2026-10-15。依据：[第 2–7 章完善建议](../../research/claude-code-pi-gap-review.md) 的 A1–A5、P1–P3、C1。用户已确认四项取舍都采用推荐选项（与本阶段直接相关的只有计划模式提醒的注入方式），并授权实施。

## 目标

让模型看到的内容更干净、多轮工具循环可以命中缓存、输出上限和临时故障不再直接中断任务，并且可以在任意项目目录中启动，同时加载项目指令。

## 行为要求

### F1 工具结果以纯文本发送给模型
- 三种协议发送给模型的工具结果都是纯文本，不再包成 JSON。成功时就是 `output`；失败时是 `错误 [<code>]：<output>`。Anthropic 额外设置 `is_error`。
- `details` 只供 UI 和测试使用，不发送给模型。原来只放在 details 里的必要信息要写进 `output`：命令非零退出时的 stdout/stderr 和退出码、参数校验的字段问题、搜索失败的 stderr 摘要。
- 命令输出只发送一次。

### F2 缓存断点与提醒位置
- Anthropic：系统提示块上的缓存断点保持不变；另外在**最后一条历史消息的最后一个内容块**上加一个断点。环境信息作为第二个系统块，同一次运行中保持不变。
- 模式提醒不再放进系统块，而是作为仅本次请求使用的 `<system-reminder>` 文本块，追加在最后一条 user/tool 消息的末尾，位于缓存断点之后。它不写入 history。
- OpenAI 两种协议保持“环境在前、提醒在末尾”的顺序，不加 `cache_control`。

### F3 提醒只在模式变化时出现
- 取消每 5 轮一次的周期提醒。会话第一次进入计划模式，或从计划模式切回执行模式后，下一次运行的**第一个请求**带一次完整提醒；其余请求都不带。
- 当前模式持续体现在环境信息的 `permission_mode` 中。
- 删除 `reminder_interval` 参数。

### F4 输出上限
- 三种协议到达输出上限时（`max_tokens` / `incomplete: max_output_tokens` / `length`）统一归一为 `stop_reason == "max_tokens"` 的 Completed，不再抛出错误。
- 有工具调用时：保存 assistant 消息，但不执行任何调用；每个调用配一条失败结果 `output_truncated`，提示模型拆小输出后重试，然后继续下一轮。
- 只有正文时：保存已有正文，结束本次运行，返回 `RunStopped("max_tokens")` 和中文提示，界面显示该提示。
- `max_output_tokens` 的默认值从 4096 提高到 16384；示例配置按协议给出建议值。

### F5 临时故障重试
- 在**向调用方产生第一个流事件之前**，以下错误按指数退避（1、2、4、8 秒，加少量抖动；如果有 `retry-after` 则采用它，上限 60 秒）重试，默认最多 4 次：HTTP 408/409/429/5xx/529、连接错误、超时，以及流中 overloaded / api_error 类型的错误。
- 已经产生事件后出错，仍按原来的方式报错，不重试。401、403、400、404、422 不重试。
- 每次重试前产生一个 `Retrying` 事件（第几次、等待秒数、原因）。TUI 状态栏显示它，`--prompt` 输出到 stderr。等待期间可以取消。
- 配置项为 `max_retries`（非负整数，默认 4）。SDK 自身的 `max_retries` 仍为 0，由 MossCode 统一控制，因为只有它知道是否已经产生事件。

### F6 迭代上限
- 默认值从 25 提高到 200；可以通过配置顶层的 `max_iterations`（正整数）调整。“连续未知工具”保护不变。

### F7 项目指令
- 按顺序加载：`~/.mosscode/AGENTS.md`，然后从文件系统根目录到项目根目录逐级加载每一级的 `AGENTS.md`；某一级没有 `AGENTS.md` 时读取该级的 `CLAUDE.md`，每级最多一个。
- 每个文件最多 32 KiB，超出部分截断并标注。文件不可读或不是 UTF-8 时跳过，并给出提示。
- 以“项目指令”段落追加在稳定系统提示末尾（属于缓存前缀），用自定义 `system_prompt` 时同样追加。每段标注来源路径；说明越靠近工作目录的文件越具体，冲突时以它为准，但系统约束和权限仍然优先。
- 在会话第一次运行时加载并缓存，会话期间不重新读取。TUI 启动区显示已加载的文件，`--prompt` 输出到 stderr。

### F8 Git 环境快照
- 环境信息增加 `git`：是否为仓库、当前分支、`git status --short`（最多 30 行，超出时标注）。
- 第一次运行时采集一次（3 秒超时）。git 不可用或目录不是仓库时如实标注，不报错。

### F9 配置查找
- `--config` 不指定时，按顺序使用第一个存在的文件：`<cwd>/.mosscode/config.yaml`、`<cwd>/config.yaml`（兼容现有用法）、`~/.mosscode/config.yaml`。
- 都不存在时报错，并列出查找过的路径。指定了 `--config` 但文件不存在时，行为和现在一样。

## 边界

不涉及工具契约、权限、UI 交互、MCP 功能（这些在后续阶段）；不涉及上下文压缩与持久化。不改动用户本机的 `config.yaml`。

## 设计

| 模块 | 改动 |
|------|------|
| `messages.py` | `ToolResult.serialize()` 改为 `model_text()`；新增 `Retrying` 事件；RunStopped 增加 `max_tokens` 原因 |
| `providers.py` | 工具结果用纯文本；Anthropic 消息断点和提醒块；三种协议的输出上限归一；重试包装在 `SDKProvider.stream` |
| `prompts.py` | `<system-reminder>` 提醒；只在变化时提醒；加载项目指令；git 快照；删除 interval |
| `agent.py` | 处理 `max_tokens`；记录上次提醒时的模式；默认 200 轮；使用 Conversation 注入的指令和 git 快照 |
| `conversation.py` | `context_root` 不为空时在首次 `start()` 加载指令和 git 快照并返回提示；核心循环与测试默认不读取用户文件 |
| `tools/base.py`、`process.py`、`search.py` | 把必要的 details 信息写入 output |
| `config.py`、`cli.py` | `max_retries`、`max_iterations`、默认输出上限、配置查找 |
| `tui.py` | 显示 Retrying、`max_tokens` 停止提示、项目指令加载提示 |

## 验收

| 编号 | 场景 → 结果 |
|------|-------------|
| AC1（F1） | 三种协议的请求体中，工具结果是纯文本；失败带 `错误 [code]`；命令非零退出的 stdout 只出现一次 |
| AC2（F2） | Anthropic 请求的最后一个历史块带 `cache_control`，系统块中没有提醒；提醒在断点之后，且不在 history 中 |
| AC3（F3） | 进入计划模式后的第一次运行只有第 1 个请求带提醒；连续两次计划运行，第二次不带；退出计划模式后带一次 |
| AC4（F4） | 三种协议截断正文 → RunStopped("max_tokens")，正文保留；截断的工具调用 → 不执行，结果为 `output_truncated`，循环继续 |
| AC5（F5） | 先 529 再成功 → 产生 Retrying，最终成功；401 不重试；首个事件之后断流不重试；等待期间可以取消 |
| AC6（F6、F9） | 配置的 `max_iterations` 生效；三个查找位置的优先级正确；都不存在时错误中列出路径 |
| AC7（F7） | 用户级、父目录、项目目录的指令按顺序出现并带路径；`AGENTS.md` 优先于 `CLAUDE.md`；超长时截断；位于缓存前缀中 |
| AC8（F8） | 临时 git 仓库能显示分支和改动；非仓库标注为否；采集失败不中断 |
| 回归 | 全量 pytest、`compileall`；用模拟模型在 Windows 真实控制台做一次烟测 |
