# 应对模型的异常回复

日期：2026-11-10。状态：R1–R8 已实现，相关测试通过（`tests/test_reply_recovery.py`）；Q1 按推荐决定（2026-11-10，用户确认），R9–R13 已实现，相关测试通过。来源：[向 MiniCode 学习](../../research/minicode-comparison.md) 第一节第 6 项。

## 问题

模型偶尔返回没有任何可用内容的回复：没有正文也没有工具调用，或者在思考阶段就碰到输出上限、一个字也没写出来。目前 Agent Loop 把这样的回复当作正常结束写入历史。这有两个后果：

- 任务停在半路，用户看到“（服务返回了空正文）”，只能自己再发一次；`/goal` 自动续跑时，截断会让目标暂停。
- 空的 assistant 消息进入历史后，Anthropic Messages 拒绝之后的每一次请求（消息内容不能为空，文本块不能为空），会话从此无法继续。

## 已确认的事实

| # | 事实 |
|---|------|
| F1 | `agent.py` `run`：一次请求的回复由 `ResponseCollector` 收齐后才进入历史；`context_overflow` 已有“不写历史、压缩后重发一次”的先例 |
| F2 | 三种协议的停止原因：Messages 为 `end_turn`、`stop_sequence`、`refusal`、`tool_use`、`max_tokens`；Chat Completions 为 `stop`、`content_filter`、`tool_calls`、`length`（映射为 `max_tokens`）；Responses 为 `completed`、`max_tokens`，拒绝以正文返回 |
| F3 | Messages 在 `max_tokens` 时丢弃没有签名的 thinking 块，思考中途截断的回复因此内容为空，回放时退回为一个空文本块 |
| F4 | 取消时历史中的工具调用和结果总是成对：`execute_batch` 先占位，结束时为未完成的调用补上“调用已取消”或“本轮已停止；此调用未执行”。MiniCode 的这一项 MossCode 已经具备，不再改动 |
| F5 | `_model` 每次调用会消耗一次性的模式提醒和 `_gap` 提醒；重发前需要恢复，否则重发的请求缺少它们 |
| F6 | 现有 `Retrying` 事件表示请求发出前的网络重试，带等待时间；界面只在状态行显示 |
| F7 | MiniCode：空回复最多重试 2 次，提示模型“刚才的回复是空的”，区分是否刚执行过工具；思考截断最多续写 3 次；放弃时写出 `stop_reason` 和收到的内容块类型 |

## 需求

| # | 要求 | 验收 |
|---|------|------|
| R1 | **空回复**：回复没有非空白正文、没有工具调用，停止原因不是截断或拒绝时，丢弃这次回复（不进入历史），带一条说明重发同一请求。说明分两种：刚收到工具结果时提示“根据上方的工具结果继续”，否则提示“给出回复或调用工具” | 模拟模型先回空、再回正文：历史中只有正文回复；第二次请求带说明，缓存前缀与第一次逐字节相同 |
| R2 | **思考截断**：回复因输出上限停止，且没有正文和工具调用时，同样丢弃并重发，说明要求缩短思考、直接给出回复或下一步工具调用 | 模拟模型先回“只有思考 + max_tokens”，再回正文 |
| R3 | R1、R2 合计每次请求最多重发 2 次；仍然失败时停止本轮并报错，错误说明写出停止原因、收到的内容块类型和建议（思考截断时建议调高 `max_output_tokens` 或降低思考强度） | 连续 3 次空回复：本轮以 error 结束，说明中有停止原因和内容块类型，历史中没有空消息 |
| R4 | **拒绝**：没有内容且停止原因是 `refusal` 或 `content_filter` 时不重发，直接报错并说明是服务拒绝 | 单元测试 |
| R5 | 丢弃的回复计入用量和费用（它们已经产生了 Token） | 用量合计包含被丢弃的回复 |
| R6 | 重发前恢复一次性提醒（F5） | 带模式提醒的第一次请求被重发时，重发的请求仍带提醒 |
| R7 | 新事件 `ReplyRetry`：界面状态行显示“模型返回了空回复，重新请求（1/2）”，并清除被丢弃的回复已经显示的思考内容；`-p` 的 text 格式在 stderr 打印同样的话，`stream-json` 输出 `reply_retry` 事件 | TUI 测试与 `-p` 测试 |
| R8 | 子 Agent 和团队成员用同一个 Agent Loop，自动获得同样的处理 | 无需单独测试 |
| R9 | **正文截断后自动续写**（Q1）：回复已有正文、没有工具调用、因输出上限停止时，回复照常进入历史，随后追加一条合成用户消息（`synthetic="continue"`），请模型从中断处直接接着写、不重复、不道歉，并开始下一轮。每轮最多续写 3 次；第 4 次仍被截断时按原来的方式停止（`max_tokens`），提示中说明已经续写过几次 | 模拟模型连续截断两次后写完：历史为 user、assistant、continue、assistant、continue、assistant，本轮 completed；连续截断 4 次：本轮 max_tokens |
| R10 | 续写的几段合起来才是完整回答：`Completed` 新增 `before`（本轮此前续写的正文）和 `answer`（`before` 加本段正文）；`-p` 的结果、子 Agent 和 Skill fork 的返回、团队成员的回复、`Conversation.ask` 都取 `answer` | `-p` json 的 `result` 是完整的回答；子 Agent 的结果是完整的回答 |
| R11 | 界面：每段各显示为一条回复，段与段之间一行“↻ 回复达到输出上限，自动续写（1/3）”；截断的那段不显示“✻ 完成”；恢复会话时显示“↻ 回复达到输出上限，自动续写”（不带次数），团队成员视图同样，都不显示为用户消息。`-p` 的 text 格式正文连续输出，stderr 打印这行说明；`stream-json` 输出 `auto_continue` 事件 | TUI 测试、`-p` 测试、恢复测试 |
| R12 | 续写只发生在本轮之内；目标模式因此只在续写用尽后才因截断暂停 | 无需单独测试 |
| R13 | 合成的续写消息不算用户输入：不作为 `/rewind` 的目标，不进入记忆提取和会话标题 | 沿用现有“`synthetic` 不为空即非用户输入”的判断 |

## 设计

- `agent.py` 中，回复收齐后、写入历史前判断：有工具调用或有非空白正文即为正常回复；否则按停止原因分为拒绝（R4）、思考截断（R2）、空回复（R1）。
- 重发沿用 `context_overflow` 所在的内层循环：恢复一次性提醒，`yield ReplyRetry(...)`，把说明作为请求级补充内容（kind `reply-recovery`，不属于 `PREFIX_KINDS`，排在历史之后，不进入缓存前缀和历史）传给 `_model`，再次请求。
- 放弃时抛出 `ProviderError(说明, code="empty_reply")`，沿用现有的 error 结束路径：`RunStopped("error")`、`turn_end` Hook 的 `reason` 为 `error`、目标暂停（“本轮出错”）。本轮第一次请求就失败时用户的消息不进入历史，与其他请求错误相同。
- 截断的回复如果已经有正文，进入历史后自动续写（R9）；有工具调用的截断回复不变（调用不执行，模型重试）。

## 任务

| # | 内容 | 位置 | 验证 |
|---|------|------|------|
| T1 | 判断与重发、放弃时的说明 | `agent.py`、`messages.py` | `tests/test_agent.py` |
| T2 | 界面与 `-p` 显示 | `tui.py`、`cli.py` | `tests/test_agent_tui.py`、`tests/test_cli.py` |
| T4 | 自动续写、`Completed.answer` 与各处的完整回答 | `agent.py`、`messages.py`、`cli.py`、`conversation.py`、`agents/runtime.py`、`teams/member.py` | `tests/test_reply_recovery.py` |
| T5 | 续写的界面、恢复与 `-p` 显示 | `tui.py`、`cli.py` | `tests/test_reply_recovery.py` |
| T3 | 文档与 CHANGELOG | `docs/reference/providers.md`、`CHANGELOG.md` | `tests/test_docs.py` |

## 真实模型验收

- 2026-11-10，DeepSeek（项目 `config.yaml` 中配置的 Provider），`-p --output-format stream-json`，输出上限只在内存中调为 1500：第一段 2196 字在句中（“代价”）被截断，`auto_continue` 1/3 后第二段从“是边界变多”接着写完，没有重复或道歉；本轮 `completed`，共 2 次请求，`result` 与两段拼接逐字相同。
- 带工具调用、没有正文的回复不会被误判为空回复（R1–R8 验收时确认）。

## 不做

- **思考截断改为续写**（MiniCode 的做法）：MossCode 丢弃了没有签名的思考块（F3），无法从中断处接着写，所以改为带说明重发。

## 待定问题

| # | 问题 | 推荐 | 替代 |
|---|------|------|------|
| Q1 | 正文截断后是否自动续写 | **已决定（2026-11-10）**：自动发一条合成的“从中断处继续”，每轮最多 3 次，见 R9–R13 | 保持现状，由用户输入“继续” |
