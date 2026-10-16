# 完善阶段 4：交互（多行输入、消息排队、diff 结果、状态栏、命令、机器可读输出）

来源：[差距评审](../../research/claude-code-pi-gap-review.md) 的 U1–U5、L1、L4。U4 中的 Shift+Tab 已在 [阶段 3b](../refinement-03b-permissions/spec.md) 完成。`/mcp` 与 M2（不阻塞启动）关系更紧密，放到阶段 5。

## 现状

- 输入框是单行 `Input`：不能换行，粘贴多行内容会被压成一行，也没有输入历史。
- 生成期间输入框被禁用。要纠正方向，只能先取消任务。
- 编辑和写入的结果只显示前三行文字摘要。`details["diff"]` 已经生成，但界面没有使用。
- 状态栏的“自动模式/计划模式”和“权限：…”重复表达同一状态，也不显示模型。
- `/theme` 只有一个选项。计划由 `exit_plan_mode` 审批后，`/do` 基本是多余的。没有 `/clear` 和 `/model`，换模型要重启。
- 无界面模式只有 `--prompt` 和文本输出，不便于脚本评估任务完成率、轮次和 Token。
- 生成 Anthropic 和 Responses 请求时，如果助手消息不带原生数据，其中的工具调用会丢失（Chat Completions 正常）。目前不会触发这个问题，但切换模型后会触发。

## 需求

| # | 行为 |
| --- | --- |
| F1 | **多行输入**：Enter 发送；Shift+Enter、Ctrl+J、Alt+Enter 换行；不能区分 Shift+Enter 的终端，可在行尾输入 `\` 再按 Enter 换行。粘贴多行内容保持原样。输入框随内容增高，最多 8 行，超出后在框内滚动 |
| F2 | **输入历史**：光标在第一行时按 ↑ 调出更早的输入，在最后一行时按 ↓ 调出较新的输入；回到最新时恢复未发送的草稿。历史只保存在本次进程内，命令也计入，相邻重复只记一次。命令提示显示时，↑/↓ 仍用于选择提示 |
| F3 | **运行中排队（steering）**：生成期间输入框保持可用。Enter 提交的消息进入队列，输入框上方逐条显示（单行截断），状态栏显示“已排队 N 条”。队列在下一个迭代边界（本轮工具执行完、下一次请求模型前）整体并入为一条 user 消息，对话中逐条显示。如果模型这一轮直接给出最终回复，任务完成后把队列合并为下一次任务自动发送。取消、失败或因上限停止时，队列内容放回输入框，不自动发送。运行中输入的 `/` 命令不排队，保留在输入框并提示 |
| F4 | **diff 结果**：write_file（覆盖）和 edit_file 成功后，工具行在摘要下方显示带行号的彩色 diff（新增绿色、删除红色、上下文正常），多个 hunk 之间用 `⋯` 分隔，最多 20 行，超出部分注明省略行数。新建文件显示前 8 行内容。写入历史滚动区后保留颜色 |
| F5 | **状态栏**：最前面是权限档位标记——default 显示“默认权限”，acceptEdits 显示“⏵⏵ 接受编辑”，plan 显示“⏸ 计划模式”，bypassPermissions 显示“⏵⏵ 跳过审批”，未启用工具显示“未启用工具”。空闲且宽度不小于 80 列时附加“Shift+Tab 切换”。之后依次是模型名、运行阶段或提示、排队数、累计 Token。去掉单独的“自动模式/计划模式”标签 |
| F6 | **命令调整**：<br>• 新增 `/clear`：清空模型上下文与 Token 统计，重新加载项目指令和 git 快照，并在下次请求时重新发送模式提醒。保留权限档位、会话授权和 MCP 连接。终端滚动区中的历史不清除，运行中拒绝执行。<br>• 新增 `/model`：不带参数时在输入框下方打开提供商选择面板（↑/↓、Enter、Esc）；`/model 名称` 直接切换。切换后保留对话历史，运行中拒绝执行，未配置多个提供商时给出提示。<br>• 删除 `/theme` 和 `/do`。手动退出计划模式改用 Shift+Tab 或 `/permissions`。帮助和补全同步更新 |
| F7 | **切换模型时历史可移植**：切换后去掉历史中的协议原生数据（thinking 签名、加密推理等只对原模型有效），之后各协议从通用字段重建：Anthropic 生成 text 加 tool_use 块，Responses 生成消息加 function_call 项。Anthropic 请求合并相邻的同角色消息，因此工具结果后紧跟的排队消息合并为同一条 user 消息 |
| F8 | **机器可读的无界面输出**：<br>• `-p` 作为 `--prompt` 的别名。<br>• `--output-format text`（默认）保持现有输出。<br>• `json`：结束时输出一个 result 对象。<br>• `stream-json`：每行一个 JSON 事件，依次为 system/init、assistant、tool_result、retry，最后是 result。<br>• result 包含 subtype（completed/max_iterations/max_tokens/unknown_tools/error）、is_error、result（最终正文或停止说明）、num_turns、duration_ms、usage（输入、输出、缓存读写、请求数）、model、permission_mode。<br>• JSON 模式以 UTF-8 输出，is_error 为真时退出码为 1，`--show-usage` 只作用于文本模式。<br>• 启动提示在 stream-json 中作为 notice 事件输出，在 json 中输出到 stderr |

## 设计

| 模块 | 改动 |
| --- | --- |
| `messages.py` | 新增事件 `UserInput(texts)`：表示排队消息在迭代边界被并入 |
| `agent.py` | `pending: list[str]`；从第 2 轮起，在每轮请求模型前取出全部排队消息，追加一条 user 消息到历史，并 yield `UserInput`。`clear()` 清空历史、用量和已发送的模式提醒。`set_provider()` 替换 Provider，并去掉历史中的 native 和 protocol 字段 |
| `conversation.py` | `queue(text)`、`take_queued()`、`async clear()`（运行中报错；清空后重新加载项目上下文）、`async set_provider(provider)`（运行中报错；关闭旧 Provider） |
| `providers.py` | Anthropic 组装消息时合并相邻同角色消息；Anthropic 和 Responses 补上不带原生数据时的工具调用重建 |
| `cli.py` | `-p`、`--output-format`；JSON 事件由 `_prompt` 的同一事件循环生成，函数返回 result 供 main 决定退出码；`MossCodeApp` 接收 `settings` 和 `model` |
| `tui.py` | `PromptArea(TextArea)`：提供 value、clear、Submitted，负责换行与历史。`#queue` 显示排队消息。ThemePanel 改为 ModelPanel。ToolEntry 渲染 diff。状态栏改为档位标记。`_respond` 结束后处理队列（发送后续任务或放回输入框）。删除 /theme、/do |
| `appearance.py` | 更新命令提示与补全（`/model 名称`）、帮助文本，删除主题选项 |

### 排队的迭代边界

```
第 1 轮：请求模型 → 工具调用 → 执行
第 2 轮开始：pending 非空 → history += user("消息1\n\n消息2") → yield UserInput → 请求模型
……
最终回复（没有工具调用）→ 运行结束 → TUI：take_queued() 非空 → 作为新任务发送
```

并入的 user 消息在请求前就写入历史。如果随后请求失败，这条消息仍保留在历史中，与已执行的工具结果一样。

## 取舍

- **输入历史不持久化**：保持轻量，不增加写盘文件。重启后历史清空。
- ~~**`/clear` 不清除终端滚动区**：inline 模式的历史在终端原生滚动区中，程序无法安全清除。界面会加一条分隔说明。~~ 验收后修订：用户希望 `/clear` 像 Claude Code 一样清屏。窗口宽度重绘已经用 `\x1b[H\x1b[2J\x1b[3J` 清除屏幕和滚动区，`/clear` 复用同一方式，只保留启动区（见 [终端界面](../../reference/interface.md#本地命令的细节)）。
- **流式 JSON 不输出逐字增量**：Delta 只用于文本模式和 TUI。评估脚本只需要完整消息。
- **切换模型时去掉原生数据**：会损失旧模型的 thinking 内容在后续请求中的回放，换来跨协议、跨模型的兼容。只能在空闲时切换。

## 验收

| 验收 | 方法 |
| --- | --- |
| AC1（F1、F2） | Pilot 测试：<br>• Shift+Enter、Ctrl+J、行尾 `\` 可以换行，Enter 发送多行文本，模型收到原样内容。<br>• 粘贴多行内容被保留。<br>• ↑/↓ 浏览历史并恢复草稿；命令提示打开时 ↑/↓ 仍选择提示 |
| AC2（F3） | 假 Provider 测试：<br>• 第 1 轮工具执行期间排入两条消息，第 2 轮请求的末尾是包含两条内容的 user 消息，对话中显示两条 You。<br>• 在最终回复期间排队，任务完成后自动作为下一次任务发送。<br>• 取消后队列内容放回输入框；运行中输入 `/help` 不排队。<br>• 状态栏显示“已排队 2 条” |
| AC3（F4） | edit 和覆盖写入的工具行包含行号、`-旧`、`+新`；超过 20 行时注明省略；新建文件显示内容；颜色样式正确 |
| AC4（F5） | 状态栏中四个档位的标记、模型名、空闲时的 Shift+Tab 提示（窄屏省略）、Token 统计 |
| AC5（F6） | `/clear` 后下一次请求只含新消息，Token 统计清零，档位和会话授权保留，计划模式的提醒重新发送，运行中拒绝。`/model` 面板选择与 `/model 名称` 切换后，请求发往新 Provider，旧 Provider 已关闭，历史保留。`/theme`、`/do` 提示未知命令 |
| AC6（F7） | 去掉原生数据的历史（含工具调用和结果）在 Anthropic、Responses、Chat 三种协议下生成完整的工具调用与结果配对；Anthropic 请求中没有相邻的同角色消息 |
| AC7（F8） | `-p` 等价于 `--prompt`。json 输出单个合法对象，字段齐全。stream-json 每行都是合法 JSON，事件顺序正确，包含工具结果。出错时 is_error 为真、退出码为 1。text 模式输出不变 |
| 回归 | 全量 pytest 连续两次，compileall，Windows 控制台烟测（同步更新其中的 /theme、/do 场景） |
