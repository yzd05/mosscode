# 任务清单、向用户提问与目标模式

日期：2026-11-10。状态：Q1–Q12 按推荐决定（2026-11-10，用户确认了 Q2 常驻面板、Q5 1–4 题、Q7 独立判断、Q8 默认 30 轮，其余按推荐）；实现中。实现顺序：任务清单 → 向用户提问 → 目标模式。任务清单（T1–T4 及其文档）、向用户提问（T5–T6 及其文档）已完成，相关测试通过，尚未用真实模型验收。

来源：
- MiniCode 对比（`docs/research/minicode-comparison.md`）中列出的差距：任务清单（`update_plan`）、`ask_user`、`/goal`。
- 用户要求：先做任务清单，再做 `/goal` 和 `ask_user`；`ask_user` 最好像 Claude Code 的 AskUserQuestion 那样提供选项。
- 参考实现：Claude Code 的任务清单、AskUserQuestion 和 `/goal`（F10–F13），MiniCode 的 `update_plan`、`ask_user` 和 `/goal`（F14–F16）。

## 问题

- **长任务容易跑偏、漏步骤。** 模型做十几步的任务时，没有一个它自己维护、每次都能看到的清单；压缩上下文之后，原来的计划只剩摘要里的一句话。用户也看不到“现在做到第几步”。
- **模型不能正式地向用户提问。** 遇到需要用户决定的取舍，模型只能在最终回复里用文字提问，然后结束这一轮。用户要自己打字回答，选项也没有结构。
- **长目标要用户一轮一轮地推。** “把这个模块迁移到新 API，直到测试全部通过”这样的任务，模型每结束一轮就要用户输入“继续”。现在只有最大迭代轮数（`max_iterations`），它限制的是一轮之内的模型请求次数，不能跨轮继续。

## 已确认的事实

### 现有代码

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `prompts.py` | `Supplement(kind, text)` 渲染为 `<kind>…</kind>`。`PREFIX_KINDS`（`environment`、`active-skills`、`hook-context`）属于缓存前缀；其他种类只随本次请求发送，三种协议都放在历史之后、缓存断点之后（`providers.py` 中 Anthropic 在最后一条消息的断点之后，Responses 和 Chat 追加在消息末尾） |
| F2 | `agent.py` `_model` | 每次请求组合 `prefix`（环境、已加载 Skill、`session_start` Hook）和 `extra`（模式切换提醒、恢复后的一次性提醒 `_gap`、`turn_start`/`pre_send` Hook 输出）。`extra` 不进历史，也不写入会话记录 |
| F3 | `messages.py` `RunStopped` | 一轮的结束原因：`completed`、`max_iterations`、`max_tokens`、`unknown_tools`、`cancelled`、`error`，带 `notice` 和已执行工具的结果 |
| F4 | `tools/base.py` | 工具是 `Tool` 子类（`name`、`description`、`params_model`、`prompt_guidelines`、`read_only`、`plan_allowed` 等），在注册表 `ToolRegistry` 中注册。执行时上下文变量 `CURRENT_REGISTRY` 指向当前注册表 |
| F5 | `tools/base.py` `for_agent` | 子 Agent 的注册表是新的 `ToolRegistry`；工具实例默认共享，带 `state` 属性的工具会被复制并换上新的 `FileState`。压缩后 `forget_reads` 对所有工具的 `state` 调用 `clear()`，所以新工具的状态**不能**叫 `state` |
| F6 | `session.py` | 会话记录是 JSONL，类型有 `message`、`compact`、`rewind`、`checkpoint`、`skill`（已加载的 Skill，恢复时重新固定）、`provider` 等。恢复时按顺序重放 |
| F7 | `permissions.py`、`tui.py` | 审批经 `PermissionPolicy._ask` 交给界面，界面在输入框上方显示审批面板（`#approval-panel`，`OptionList` 选项），审批之间用 `_approval_lock` 排队。`confirm()` 可以提出任意选项，但选项必须属于 `ALL_CHOICES`；非交互（`-p`、后台子 Agent）时返回 `None` |
| F8 | `tools/plan.py` | `exit_plan_mode` 是现有的“在本轮中等待用户”的工具：权限层弹出审阅面板，用户的选择决定工具结果 |
| F9 | `hooks/events.py`、`config.py` | `turn_end` Hook 只能通知外部，不能让模型继续工作；没有类似 Claude Code Stop Hook 的“阻止结束”。`memory_provider` 可以指定另一个 Provider 做后台记忆提取，没有配置时用当前 Provider |

### 参考实现（2026-11-10 查阅官方文档）

| # | 项目 | 做法 |
| --- | --- | --- |
| F10 | Claude Code 任务清单 | 工具 `TodoWrite`（整表替换，状态 pending / in_progress / completed），新版拆为 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate`。界面中 Ctrl+T 展开或收起任务清单，一次最多显示 5 项；清单在压缩后保留，恢复会话时保留展开状态。**v2.1.268 起只在 Opus 4.7、Sonnet 4.6、Haiku 4.5 及更早的模型上默认提供**：官方说明较新的模型不写清单也能跟住多步工作，而工具定义和提醒会占上下文；`CLAUDE_CODE_ENABLE_TODO_TOOLS=1` 可以重新打开。子 Agent 只在主会话有这些工具时才有。来源：[tools-reference](https://code.claude.com/docs/en/tools-reference#task-tool-availability)、[interactive-mode](https://code.claude.com/docs/en/interactive-mode#task-list) |
| F11 | Claude Code AskUserQuestion | 向用户提出选择题，用户选一个选项，或在“Other”行、备注栏中自己输入；自己输入的回答会以中性措辞转给模型，让模型照着做（包括“先等等”“先解释”）。问题默认一直等待；可以设置 `askUserQuestionTimeout`（60 秒、5 分钟、10 分钟），到时提交已选的选项，并告诉模型用户可能不在，由它自行判断。来源：[tools-reference](https://code.claude.com/docs/en/tools-reference#askuserquestion-tool-behavior) |
| F12 | Claude Code `/goal` | `/goal <条件>` 设置完成条件（最多 4,000 字符），并立即以条件为指令开始一轮。**每轮结束后由另一个小模型判断**：只看对话中已经出现的内容，不调用工具。结论有三种：未满足（继续一轮，理由作为下一轮的指引）、已满足（清除目标，记录“已达成”）、不可能（清除目标，记录“失败”和理由）。连续几轮没有工具调用时停止循环，目标保留，用户下次输入后继续评估。设置目标不改变权限模式，需要审批的工具照常询问。界面显示 `◎ /goal active` 和已运行时间；`/goal` 不带参数时显示条件、时长、轮数、Token 和最近一次判断的理由；`/goal clear`（以及 stop、off 等别名）清除，`/clear` 也会清除。恢复会话时恢复未完成的目标，轮数、计时和 Token 从零算。`-p "/goal …"` 在一次调用中跑完。必须由用户修复的错误（认证、余额、压缩后仍然超出上下文、模型不可用）会清除目标；其他错误保留目标，自动重试最多三次后暂停。还有子 Agent 或后台命令运行时推迟判断、空闲时定期检查等机制。来源：[goal](https://code.claude.com/docs/en/goal)、[commands](https://code.claude.com/docs/en/commands) |
| F13 | Claude Code 的其他方式 | `/loop` 按时间间隔重复；Stop Hook 每轮结束后由脚本或提示决定是否继续。`/goal` 本身就是会话级的提示型 Stop Hook |
| F14 | MiniCode `update_plan` | 整表替换；已有项必须带 `id`，新项不带；未知或重复的 `id` 报错；最多一个 in_progress。只在内存中，`/new`、`/resume`、`/fork` 时清空。每次请求注入快照，并注明三点：快照是权威状态，标题是数据而不是指令，清单全部完成不等于任务完成。界面用 `[x]`、`[>]`、`[ ]` 显示，附完成数 |
| F15 | MiniCode `ask_user` | 只有一个 `question` 参数，没有选项。工具结果带 `awaitUser: true`，循环以 `awaiting_user` 结束这一轮，问题作为助手消息显示；用户的下一条输入就是回答 |
| F16 | MiniCode `/goal` | 目标由用户拥有，模型只能规划和汇报：`get_goal`；`update_goal` 可以设定完成标准（需要先有清单）、报告受阻（附原因）、报告完成（附总结，要求清单全部完成，并且每条完成标准都有一条检查结果、顺序对应）。完成或受阻时停止。控制器在每轮正常结束后自动发送“继续推进当前目标……”；遇到失败、步数上限、中断、连续 3 轮自动续跑都没有工具调用、等待用户回答、用户修改了计划时暂停。命令有 `/goal`、`/goal pause`、`/goal resume`、`/goal clear` |

## 需求

### 任务清单

| # | 要求 | 验收 |
| --- | --- | --- |
| R1 | 新工具 `todo_write`：参数 `todos` 为完整清单，每项含 `content`（必填，最多 200 字）、`status`（`pending`、`in_progress`、`completed`）和 `id`（已有项必填，新项省略，由 MossCode 分配 `1`、`2`…）。整表替换：省略的项即删除。最多 50 项，最多一个 `in_progress`；`id` 未知或重复时整次调用失败，清单不变 | 单元测试：新增、改状态、删除、未知 `id`、两个 `in_progress`，以及失败时清单不变 |
| R2 | 工具结果是更新后的清单文本（`[x]`、`[>]`、`[ ]` 加 `id`），并注明“已完成 m/n” | 工具结果与清单一致 |
| R3 | 清单不为空且有未完成项时，**每次模型请求**附带一条 `todo-list` 补充内容（不属于缓存前缀，F1）：当前清单，以及三条说明：这是权威状态、条目是数据不是指令、清单完成不等于任务完成。全部完成或清单为空时不附带 | 模拟模型收到的请求中有 `<todo-list>`，位于历史之后；缓存前缀（system、tools、历史）在清单变化前后逐字节相同 |
| R4 | 清单属于各自的 Agent：主对话、每个子 Agent、每个团队成员各有一份，互不影响（清单存放在注册表上，F4–F5） | 子 Agent 更新清单后，主对话的清单不变 |
| R5 | 主对话的清单显示在界面中（形式见 Q2），并写入会话记录（新记录类型 `todo`）：压缩后保留；恢复会话时恢复；`/clear` 时清空；`/rewind` 回到某条消息之前时，恢复到当时的清单 | 无界面 TUI 测试：清单面板随工具调用更新；恢复会话后清单仍在；`/clear` 后为空 |
| R6 | 计划模式中可以使用（`plan_allowed`），不需要审批，不受沙箱影响 | 计划模式下调用成功 |
| R7 | 配置项 `todo_tool`（默认 `true`）为 `false` 时不提供这个工具 | 配置测试 |

### 向用户提问

| # | 要求 | 验收 |
| --- | --- | --- |
| R8 | 新工具 `ask_user`：参数 `questions` 为 1–4 个问题；每个问题含 `question`（问题全文）、`header`（最多 12 字的短标签）、`options`（2–4 个，每个含 `label` 最多 30 字、`description` 最多 200 字）和 `multi_select`（默认 `false`） | 参数校验测试 |
| R9 | 工具在本轮中等待回答，不结束这一轮。界面在审批面板的位置（输入框下方）显示提问面板：逐题显示选项，最后一项固定为“其他（自己输入）”；多选题可以勾选多项。选择“其他”后焦点回到输入框，用户输入的文字作为回答。Esc 拒绝回答 | 无界面 TUI 测试：选择选项、多选、“其他”自由输入、Esc 拒绝，各得到对应的工具结果 |
| R10 | 工具结果逐题列出回答。用户自己输入的回答原样转给模型，并说明这是用户的原话，照着做（F11）；拒绝时结果为“用户没有回答，按你的判断继续，或在最终回复中说明需要用户决定的事项” | 结果文本测试 |
| R11 | 无法询问时（`-p`、后台子 Agent、团队成员）不等待，立即返回错误结果：“当前无法询问用户，请按最合理的假设继续，并在最终回复中写明假设”。**实现时调整**：`-p`、子 Agent（含前台）和团队成员中不提供这个工具（省掉一次必然失败的调用；子 Agent 的问题应由主对话问用户），错误结果只在主对话无法弹出面板时出现 | 测试：非交互时得到该结果；默认注册表、子 Agent 视图中没有该工具 |
| R12 | 提问与审批共用排队（F7）：同一时间只显示一个面板。等待回答时状态栏显示“等待回答”；Esc 只拒绝当前问题，不取消整轮 | TUI 测试：审批进行中时提问排在其后 |
| R13 | 问题和回答在对话中留下记录（问题列表和用户的选择），恢复会话后可以看到 | 恢复后的回放中有问答行 |

### 目标模式

| # | 要求 | 验收 |
| --- | --- | --- |
| R14 | `/goal <条件>` 设置目标（条件最多 4,000 字），立即以条件为指令开始一轮；已有目标时替换。同一会话只有一个目标 | 命令测试 |
| R15 | 每轮正常结束后判断条件是否满足（判断方式见 Q7）。未满足时自动开始下一轮，判断理由作为下一轮的提示；满足时清除目标并显示“目标已达成”；判断为不可能时清除目标并显示原因 | 模拟模型与模拟判断：未满足 → 继续，满足 → 停止 |
| R16 | 自动续跑在这些情况下暂停（目标保留，用户输入后继续）：本轮被取消（Esc）、出错、达到 `max_iterations`、截断（`max_tokens`）；连续 3 轮没有工具调用；达到轮数上限（Q8）。暂停时显示原因和继续方法 | 每种暂停原因各一个测试 |
| R17 | `/goal` 不带参数显示当前目标：条件、已运行时间、轮数、Token 用量、最近一次判断的理由；没有目标时显示最近一次达成的目标或“没有目标”。`/goal clear` 清除；`/clear` 也清除 | 命令输出测试 |
| R18 | 目标不改变权限模式；需要审批的工具照常询问，`ask_user` 照常等待回答 | TUI 测试：目标进行中审批照常弹出 |
| R19 | 状态栏显示目标进行中和已运行时间。目标写入会话记录，恢复会话时恢复未完成的目标（计时、轮数从零算），恢复后不自动开始，等用户输入或执行 `/goal` 时继续 | 恢复测试 |
| R20 | 有运行中的子 Agent 或后台命令时，本轮结束先不判断，等它们的通知触发的那一轮结束后再判断（F12） | 测试：后台任务运行时不续跑 |

## 设计

### 任务清单

- **存放位置**：`ToolRegistry.todos`（新的 `TodoList` 对象，属性名不用 `state`，见 F5）。`TodoWrite.execute` 通过 `CURRENT_REGISTRY` 找到当前注册表，所以子 Agent 和成员的注册表天然各有一份，`for_agent` 新建注册表时清单为空。
- **注入**：`AgentLoop._model` 的 `extra` 中加入 `todo_reminder(self.registry.todos)`，它返回 `Supplement("todo-list", …)` 或 `None`。`estimate` 不计入它（与其他请求级补充内容一致，体积有限：50 项 × 200 字）。
- **记录与恢复**：清单变化时 `AgentLoop` 调用 `on_commit({"type": "todo", "todos": […]})`。恢复时取最后一条 `todo` 记录；`rewind` 记录之后按保留的消息找回当时的清单（实现时查看 `rewind` 的重放逻辑，**待调查**）。`clear()` 清空清单。
- **界面**：见 Q2。工具行显示为 `Todo  已完成 2/5`，不展开清单全文。
- **提示**：工具的 `prompt_guidelines` 写明什么时候用：三步以上的任务开始时建清单，开始一项前把它标为 in_progress，做完立即标为 completed；简单问答和一两步的修改不建清单。

### 向用户提问

- **工具**：`AskUser(Tool)`，`read_only = True`（可以和读工具一起执行；不修改任何东西），`plan_allowed`。`execute` 通过注册表上的新入口 `registry.questions`（由会话设置；为 `None` 表示无法询问，R11）提交问题并等待结果。
- **等待通道**：在 `PermissionPolicy` 中新增 `ask_questions(call_id, questions)`，复用 `_approval_lock` 和 `pending` 的等待方式，但不经过 `ALL_CHOICES`（选项是任意文字）。界面收到 `QuestionRequest` 事件，显示提问面板。这样审批与提问自然排队（R12），`-p` 中 `interactive` 为假时直接返回 `None`。
- **界面**：复用审批面板的位置和 `OptionList`。一个问题一页，标题显示 `header` 和“第 i/n 题”；单选题回车即选，多选题空格勾选、回车确认；最后一项是“其他（自己输入）”，选中后输入框显示提示“输入你的回答，回车提交”，提交的文字作为本题回答，不作为普通消息发送。
- **取消**：Esc 拒绝当前问题（R10）。用户在等待回答时按 Esc 两次或 Ctrl+C 取消整轮，与审批中取消相同。

### 目标模式

- **控制器**：新模块 `goal.py`，`GoalState`（条件、开始时间、轮数、Token 基线、最近理由、状态）和 `GoalController`。会话层（`conversation.py`）在一轮结束（收到 `RunStopped`）后调用控制器，控制器决定是继续、暂停还是结束；TUI 在空闲时用现有的“自动开始一轮”的方式（后台任务通知使用的 `_tasks_ready` 路径）发起下一轮。下一轮的用户消息是合成消息（`synthetic="goal"`），在界面中显示为一行 `◎ 继续目标：<理由摘要>`，不显示为用户的灰底消息。
- **判断**：见 Q7。推荐方式下，判断请求发给 `goal_provider`（新配置项；没有时用 `memory_provider`，再没有时用当前 Provider），内容是条件和对话尾部（最近若干条消息，工具结果截断），要求返回 JSON `{"verdict": "met" | "not_met" | "impossible", "reason": "…"}`。解析失败时视为未满足，并计入连续失败次数，连续 3 次失败时暂停。
- **注入**：目标进行中，每次请求附带一条 `goal` 补充内容（请求级，不进缓存前缀）：条件、已用轮数和上一次判断的理由。
- **与任务清单的关系**：互不依赖。目标进行中模型通常会用清单拆分步骤，但判断只看条件和对话，不看清单是否全部完成（F14 的第三条说明同样适用）。

## 任务

| # | 任务 | 改动位置 | 验证 |
| --- | --- | --- | --- |
| T1 | `TodoList` 与 `todo_write` 工具，注册表上的清单，配置项 `todo_tool` | `tools/todo.py`（新）、`tools/base.py`、`tools/__init__.py`、`config.py` | `tests/test_todo.py`（新） |
| T2 | 请求级 `todo-list` 补充内容；缓存前缀不变 | `prompts.py`、`agent.py` | `tests/test_todo.py`、`tests/test_prompts.py`、`tests/test_prompt_cache.py`（如有，**待确认文件名**） |
| T3 | `todo` 会话记录：写入、恢复、`/clear`、`/rewind` | `session.py`、`conversation.py` | `tests/test_sessions.py` |
| T4 | 界面：清单显示（按 Q2）、工具行 | `tui.py` | `tests/test_tui*.py` 中相关文件 |
| T5 | `ask_user` 工具、等待通道、`-p` 和子 Agent 中的错误结果 | `tools/ask.py`（新）、`permissions.py`、`conversation.py`、`cli.py` | `tests/test_ask_user.py`（新） |
| T6 | 提问面板：单选、多选、其他、Esc、与审批排队、回放 | `tui.py` | 无界面 TUI 测试 |
| T7 | `goal.py`：状态、判断、暂停规则；`/goal` 命令 | `goal.py`（新）、`commands.py`、`conversation.py` | `tests/test_goal.py`（新） |
| T8 | TUI 自动续跑、状态栏、恢复 | `tui.py`、`session.py` | 无界面 TUI 测试 |
| T9 | 文档与 CHANGELOG | `docs/reference/tools.md`、`commands.md`、`interface.md`、`sessions.md`、README 的配置表、`CHANGELOG.md` | `tests/test_docs.py` |
| T10 | 真实模型验收（DeepSeek）：长任务建清单、提问面板、一个小目标跑到达成 | `.test-artifacts/` 中的驱动脚本 | 记录在本规格的验收结果中 |

## 验收

1. 相关测试文件通过；`ruff check`、`ruff format --check` 通过。
2. 缓存：清单和目标的变化不改变缓存前缀（T2 的测试逐字节比较）。
3. 真实模型（T10）：
   - 给一个五步以上的任务：模型建立清单，界面中清单随进度更新，最终全部完成。
   - 给一个有歧义的请求：模型调用 `ask_user`，面板出现，选择一个选项后模型照着继续；再用“其他”输入一次。
   - `/goal` 一个可验证的小目标（如“`tests/test_x.py` 全部通过”）：自动续跑若干轮后显示“目标已达成”；中途按 Esc 能暂停，输入后继续。

## 不做

- **共享清单**（Claude Code 的 `CLAUDE_CODE_TASK_LIST_ID`）和团队共享的任务板：团队已有自己的任务协作方式。
- **`/loop`**（按时间间隔重复）：与目标模式是不同的需求，另立规格。
- **提问超时自动继续**（Claude Code 的 `askUserQuestionTimeout`）：第一版一直等待；需要时再加。
- **空闲定期检查**（Claude Code 在后台任务长时间运行时的 check-in）：第一版只做 R20 的“推迟判断”。
- **子 Agent 的清单显示**：子 Agent 有自己的清单（R4），但第一版只在主界面显示主对话的清单。

## 待定问题

| # | 问题 | 推荐 | 替代 |
| --- | --- | --- | --- |
| Q1 | 工具形式：一个整表替换的 `todo_write`，还是 Claude Code 新版的四个工具（创建、查看、更新、列出） | **一个 `todo_write`，整表替换**：只占一个工具定义，模型一次调用就能同时改多项；`id` 规则（F14）防止模型改错项 | 四个工具：单项操作更省输出，但工具定义多，DeepSeek 这类模型更容易漏调用 |
| Q2 | 主对话的清单在界面中怎么显示 | **输入框上方的常驻面板**：有未完成项时显示，最多 5 行（当前项、之后的待办项，超出时显示“另有 n 项”），全部完成后显示一行“✓ 已完成 n 项”，下一条用户消息后收起；Ctrl+T 展开或收起完整清单（实现时改为 `/todo` 查看完整清单：Ctrl+T 已用于团队成员面板；面板显示标题和最多 4 项未完成的项） | 只在对话中显示：每次更新时打印一份完整清单（简单，但会刷屏）；或只在 `/todo` 命令中查看 |
| Q3 | 是否默认开启 | **默认开启**，`todo_tool: false` 关闭：MossCode 主要用 DeepSeek 等模型，清单的收益大于工具定义占的上下文（F10 中 Claude Code 只对最新的 Claude 模型关闭） | 默认关闭，用户按需打开 |
| Q4 | 子 Agent 和团队成员是否有 `todo_write` | **有，各自一份，不显示**（R4）：长的子任务同样受益；Fork 子 Agent 不继承主对话的清单 | 只有主对话有 |
| Q5 | `ask_user` 一次能问几个问题、每题几个选项 | **1–4 个问题，每题 2–4 个选项，另加“其他”**（与 Claude Code 相同） | 一次只问一个问题（实现和界面更简单） |
| Q6 | `ask_user` 等待时是在本轮中等待，还是像 MiniCode 那样结束这一轮 | **在本轮中等待**：回答作为工具结果返回，模型直接继续；不打断缓存，也不需要新的结束原因 | 结束这一轮（`awaiting_user`），用户的下一条消息作为回答：实现简单，但用户要自己打字，没有选项 |
| Q7 | 目标是否达成由谁判断 | **另一次独立的模型调用判断**（Claude Code，F12）：干活的模型不能自己宣布完成；用 `goal_provider` / `memory_provider` 可以换成便宜的模型；每轮多一次小请求 | 模型自己汇报（MiniCode，F16）：`update_goal` 报告完成时附检查结果；没有额外请求，但模型可能过早宣布完成 |
| Q8 | 自动续跑的上限 | **默认最多 30 轮**（配置项 `goal_max_turns`），另有“连续 3 轮没有工具调用就暂停”；到达上限时暂停，用户输入或 `/goal` 后再续 30 轮 | 不设硬上限，靠用户在条件里写“最多 20 轮”（Claude Code）；或另加 Token 上限 |
| Q9 | 目标进行中按 Esc | **暂停**：取消本轮，目标保留，状态栏显示“目标已暂停”；用户的下一条消息照常处理，结束后恢复自动续跑；`/goal clear` 才清除 | 清除目标 |
| Q10 | `-p` 是否支持目标 | **第一版不支持**：`-p` 现在不处理斜杠命令；需要时另加 `--goal <条件>` 参数 | 第一版就加 `--goal` |
| Q11 | 目标进行中的权限 | **不改变权限模式**（Claude Code）：想无人值守就先切换到自动审批的模式；文档写明 | 进入目标时提示用户选择是否切换模式 |
| Q12 | 判断模型看什么 | **条件加对话尾部**：最近的助手回复全文、最近 20 次工具调用的名称和截断后的结果（各 1,000 字）、Token 上限 16k；不调用工具 | 发送完整历史（准确，但费用随对话增长） |
