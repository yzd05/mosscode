# `/thinking` 思考强度

日期：2026-11-04。状态：已实现，已验收（2026-11-04，M148–M152）。

来源：
- [产品差距评审](../../research/product-gap-review.md)的 B5，属于“完善十一：日常顺手”。B2 `!命令` 完成后接着做（2026-11-04）。
- pi 的 `/thinking` 和 Claude Code 按模型提供的 effort：同一个对话里随时调高调低思考强度，不用改配置重启。

## 问题

思考强度只能在配置文件里写死（而且只有 Anthropic Messages 能写 `thinking`）。简单问题想省 Token、难题想多想一会儿，都要改配置重启；OpenAI 和 Chat Completions 服务根本没有入口。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `config.py` | `thinking` 只允许 anthropic-messages：`adaptive`（可带 `effort`）、`enabled`（`budget_tokens` ≥ 1024 且小于 `max_output_tokens`）、`disabled` |
| F2 | `providers.py` | Anthropic 请求原样发送 `thinking`，`effort` 放进 `output_config`；Responses 和 Chat Completions 不发送任何思考参数，只读取返回的推理内容 |
| F3 | LiteLLM 模型数据 | 每个模型有 `supports_reasoning`、`supports_adaptive_thinking`、`thinking_always_on`（Opus 5.5、Sonnet 5.5 不能关闭思考）、`supports_none_reasoning_effort`、`supports_minimal_reasoning_effort`；不支持思考的模型（`gpt-4.1`、`deepseek/deepseek-chat`）不写这个字段。现在的模型目录没有保留这些字段 |
| F4 | Anthropic 文档 | 4.6 以后的模型用 `{type: adaptive}` 加 `output_config.effort`（low/medium/high/…）；Haiku 4.5 等旧模型用 `{type: enabled, budget_tokens}`；Opus 5.5、Sonnet 5.5 发送 `disabled` 返回 400 |
| F5 | OpenAI SDK 3.26 | Responses 的 `reasoning.effort` 和 Chat Completions 的 `reasoning_effort` 取值 `none/minimal/low/medium/high/xhigh/max`；`gpt-5` 不支持 `none`、支持 `minimal`，`gpt-5.1` 支持 `none` |
| F6 | DeepSeek 文档 | Chat Completions：`extra_body={"thinking": {"type": "enabled"/"disabled"}}` 加 `reasoning_effort`（medium 按 high 处理）。Anthropic 兼容接口：`thinking` 支持 enabled/disabled，adaptive 当作 enabled，忽略 `budget_tokens`，`output_config` 只支持 `effort` |
| F7 | `agents/runtime.py` | 和主对话同一模型的子 Agent 共用主对话的 Provider 对象；指定其他模型的子 Agent 和团队成员各自创建 Provider |

## 需求

| # | 要求 | 验收 |
| --- | --- | --- |
| R1 | `/thinking [档位]`（off、low、medium、high、default）设置本会话的思考强度；`default` 回到配置文件的设置（不发送覆盖参数）。不带参数时打开选择列表，当前档位打勾，每项说明在当前模型上会发送什么 | `/thinking high` 后下一次请求带上对应参数 |
| R2 | 按协议和模型映射（见设计中的表）；只在设置了档位时发送，`default` 时请求与现在完全相同 | 各协议的请求参数测试 |
| R3 | 当前模型不支持这个档位时提示原因并忽略：档位保留，但这个模型的请求按配置发送；切换到支持的模型后生效。判断依据：模型目录（或配置 `reasoning`）明确说不支持时才算不支持；目录中没有的模型照常发送，由服务决定 | `gpt-4.1` 上 `/thinking high` 提示不支持，请求不带参数；Opus 5.5 上 `/thinking off` 提示不能关闭 |
| R4 | 档位对主对话和共用同一模型的子 Agent 生效，从下一次请求开始；`/model` 切换后继续使用同一档位，新模型不支持时提示 | 切换模型后的提示 |
| R5 | 档位只在本进程有效：`/clear` 保留，退出后恢复会话不保留（与权限档位相同）；运行中不能切换（与其他命令一样提示稍后发送，文字留在输入框） | |
| R6 | `/status` 显示“思考”一行：档位和当前模型实际发送的参数，或“跟随配置”，或不支持的原因；设置了档位时状态栏在模型后显示 `思考 high` | |
| R7 | 配置 `reasoning: false` 让这个提供商不接受 `/thinking`；`reasoning: true` 用于目录没有标注、但其实支持的模型 | |

## 设计

### 映射

`reasoning.py`（新）的 `thinking_options(config, level)` 返回要合并进请求的参数和一句说明，或抛出 `Unsupported(原因)`。`info` 是 `config.catalog`。

| 协议 | off | low / medium / high |
| --- | --- | --- |
| anthropic-messages | `thinking: {type: disabled}`；目录说 `thinking_always_on` 时不支持 | 默认 `thinking: {type: adaptive}` + `output_config: {effort}`；目录中 Anthropic 自己的、不支持 adaptive 的模型（或目录没有、配置用 `enabled` 的模型）用 `thinking: {type: enabled, budget_tokens}`，预算 low 2048 / medium 8192 / high 16384，不超过 `max_output_tokens` 的一半，不足 1024 时不支持 |
| openai-responses | `reasoning: {effort: none}`；目录说不支持 none 但支持 minimal 时用 `minimal` 并说明不能完全关闭；两者都不支持时不支持 | `reasoning: {effort}` |
| openai-chat-completions | DeepSeek：`extra_body.thinking: {type: disabled}`；其他：同 Responses，放在 `reasoning_effort` | DeepSeek：`extra_body.thinking: {type: enabled}` + `reasoning_effort`（medium 说明按 high 处理）；其他：`reasoning_effort` |

“DeepSeek”指目录条目的提供商是 `deepseek`，或 `base_url` 的主机是 `api.deepseek.com`。

### 支持判断

1. 配置 `reasoning: false` → 不支持；`reasoning: true` → 支持（跳过目录判断）。
2. 目录有这个模型且 `supports_reasoning` 不是 true → 不支持。
3. 目录没有这个模型 → 照常发送。

### 状态与数据流

- **模型目录**：`FIELDS` 增加上述五个字段，`ModelInfo` 增加对应属性（默认 `None`）。旧版目录文件（字段是现有字段的前缀）照常读取，缺的字段为 `None`；随程序发布的目录重新生成。
- **Provider**：`SDKProvider.thinking: str | None`，请求时调用 `thinking_options`；不支持时不发送。Anthropic 的覆盖参数替换配置中的 `thinking`。
- **Conversation**：`thinking` 档位；`set_thinking(level)` 在空闲时设置并写到当前 Provider，返回说明或不支持的原因；`set_provider` 把档位带到新 Provider。
- **命令**：`/thinking`（`builtin_commands.py`），参数补全为五个档位；`/status` 增加一行；状态栏显示档位。

## 不做

- `xhigh`、`max`：各家含义不同，先提供四档；需要时在配置中写 `thinking.effort`。
- 保存到配置文件或会话存档、`-p` 的命令行参数。
- 快捷键：Shift+Tab 已用于切换权限档位。
- 团队成员和指定其他模型的子 Agent：它们用自己的 Provider，按配置发送。
- 修改思考内容的显示方式（Anthropic `display`）。

## 验收

自动测试覆盖 R1–R7 和映射表。手动验收见 [docs/acceptance.md](../../acceptance.md)。切换档位会改变请求参数，Anthropic 的提示缓存在切换后的第一次请求不会命中。
