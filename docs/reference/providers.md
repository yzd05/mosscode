# 模型提供商

Provider 配置、协议、重试与无界面输出。

## 添加配置

在 `config.yaml` 的 `providers` 列表追加配置即可接入兼容已有协议的服务。每项绑定一个模型；同一服务的多个模型可分别配置，使用不同 `name`，共用 `base_url` 和凭据。

凭据字段三选一：

- `api_key_keyring: deepseek`（推荐）：密钥保存在系统凭据管理器中（Windows 凭据管理器、macOS 钥匙串、Linux Secret Service），用 `moss key set deepseek` 保存，配置只写名称，见[命令行](cli.md#key)。保存后立即生效，不用重开终端。
- `api_key_env: DEEPSEEK_API_KEY`：填写环境变量名称，并在启动进程的环境中设置该变量。例如 PowerShell 中先执行 `$env:DEEPSEEK_API_KEY = '你的密钥'` 再启动应用。`api_key_env` 不能填写 `sk-...`；误填时会给出字段说明，错误中不回显该值。
- `api_key: '你的密钥'`：明文写在配置文件中。

Anthropic 端点需要 bearer 认证时设 `auth_type: bearer`（默认 `api_key`）。

```yaml
  - name: my-gateway-model-a
    protocol: openai-chat-completions
    base_url: https://your-gateway.example/v1
    model: replace-with-service-model
    api_key_env: MY_GATEWAY_API_KEY
```

### 厂商预设

`preset` 代替协议、服务地址、默认模型和密钥变量名，一项配置只需两三行：

```yaml
  - name: deepseek
    preset: deepseek
  - name: kimi
    preset: kimi
    model: kimi-k3.1        # 换模型时只写 model
    api_key_keyring: kimi   # 不用环境变量时写明凭据
```

| preset | 协议 | 服务地址 | 默认模型 | 密钥变量 |
|--------|------|----------|----------|----------|
| `deepseek` | openai-chat-completions | `https://api.deepseek.com` | `deepseek-flash` | `DEEPSEEK_API_KEY` |
| `kimi` | openai-chat-completions | `https://api.moonshot.cn/v1` | `kimi-k3.1-pro` | `MOONSHOT_API_KEY` |
| `glm` | openai-chat-completions | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3` | `ZHIPUAI_API_KEY` |
| `qwen` | openai-chat-completions | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `DASHSCOPE_API_KEY` |
| `openrouter` | openai-chat-completions | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-5.5` | `OPENROUTER_API_KEY` |
| `anthropic` | anthropic-messages | 官方地址 | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| `openai` | openai-responses | 官方地址 | `gpt-5.6` | `OPENAI_API_KEY` |

配置中写出的字段优先，预设只填配置没写的部分；写了 `api_key`、`api_key_env` 或 `api_key_keyring` 时不再使用预设的变量名。默认模型都在[模型目录](#模型目录)中，上下文窗口和价格随之而来。

### 跟随 Claude Code 或 Codex

已经用 CC Switch、Magpie 等工具管理 Claude Code 或 Codex 的服务时，不必再填一遍地址和密钥，让 MossCode 读取它们的设置：

```yaml
  - name: cc
    follow: claude-code   # 或 codex
```

- `follow: claude-code` 读取 `~/.claude/settings.json` 的 `env`（设置了 `CLAUDE_CONFIG_DIR` 时读取那个目录）：`ANTHROPIC_BASE_URL` 为服务地址，`ANTHROPIC_AUTH_TOKEN`（bearer 认证）或 `ANTHROPIC_API_KEY` 为密钥，`ANTHROPIC_CUSTOM_HEADERS` 为额外请求头，模型取 `ANTHROPIC_MODEL` 或设置中的 `model`；`opus`、`sonnet`、`haiku` 这样的别名按 `ANTHROPIC_DEFAULT_*_MODEL` 换成实际模型名，模型名后的 `[1m]` 表示上下文窗口为 1M。设置中没有的变量从启动 MossCode 的环境中取，和 Claude Code 一致。协议为 anthropic-messages。
- `follow: codex` 读取 `~/.codex/config.toml`（设置了 `CODEX_HOME` 时读取那个目录）：`model`、`model_provider` 和 `[model_providers.<id>]` 中的 `base_url`、`wire_api`（`responses` 或 `chat`）、`http_headers`，以及当前 `profile` 的覆盖；密钥取 `env_key` 指定的环境变量，没有时取 `auth.json` 中的 `OPENAI_API_KEY`。

设置在启动时读取，在 `/model` 中选中这一项时重新读取：在 CC Switch 中切换了服务，回到 MossCode 执行 `/model`（只有这一项时直接重新读取）即可，对话历史保留。密钥只在内存中使用，不写入 MossCode 的配置，和其他密钥一样从输出中隐藏。

跟随的一项可以填写 `model`（覆盖跟随的模型）、`context_window`、`max_output_tokens`、`thinking` 等调整项，但不能填写 `protocol`、`base_url`、`headers`、`auth_type` 和凭据字段：这些只来自被跟随的设置，项目配置因此无法把你的密钥发往别处。用账号登录的 Claude Code、用 ChatGPT 账号登录的 Codex、`apiKeyHelper` 生成的密钥以及 Codex 内置的 `ollama` 等服务没有可共用的密钥或地址，启动时说明原因。

## 密钥保护

无论密钥放在哪里，MossCode 都不让它经由工具进入对话：

- **子进程不继承密钥变量**：配置中所有 `api_key_env`（各 Provider 和 `web_search`）指向的变量，不传给 `execute_command`、`!` 命令、Hook 命令和 Skill 工具启动的进程，模型执行 `env` 时看不到它们。MCP 服务器本来就只继承 PATH 等少数变量；团队成员进程照常继承，自己读取密钥。
- **模型运行的命令看不到其他密钥**：`execute_command` 和 Skill 工具启动的进程另外去掉两类变量：名称含 TOKEN、SECRET、PASSWORD、PASSWD、PASSPHRASE、CREDENTIAL、API_KEY、ACCESS_KEY、PRIVATE_KEY、COOKIE或以 `_KEY`、`_PAT` 结尾的（不区分大小写）；值形似私钥、JWT、常见服务令牌（`ghp_`、`github_pat_`、`glpat-`、`xox…-`、`sk-`、`npm_`、`AKIA`、`AIza` 开头）或带密码的 URL（如 `postgres://user:pass@host`）的。代理变量（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`）和 MossCode 为命令设置的 `MOSSCODE_*` 保留。命令确实需要某个变量时，在 `config.yaml` 中写 `command_env: {keep: [GITHUB_TOKEN]}`（变量名可用 `*`）；要额外去掉的写在 `command_env.drop`。`keep` 只在用户配置 `~/.mosscode/config.yaml` 中生效，项目的 `.mosscode/config.yaml` 只能追加 `drop`，见 [项目配置只能收紧](sandbox.md#项目配置只能收紧)。Hook 和 `!` 命令是你自己的命令，不受这一条影响。
- **输出中的密钥被隐藏**：MossCode 知道的每个密钥（明文 `api_key`、环境变量的值、凭据管理器中的值，以及名称含 auth、key、token、secret 的 `headers` 值），完整出现在工具结果、`!` 命令输出、Hook 输出、`@` 附件或你输入的消息中时，替换为 `[已隐藏的密钥]`，然后才显示、发给模型、写入会话存档或大结果文件。少于 8 个字符的值不替换。
- **不会写回占位符**：`write_file` 的内容或 `edit_file` 的 `new_text` 含有 `[已隐藏的密钥]` 时拒绝执行，避免模型把读到的占位符写回文件、覆盖真正的密钥。
- **明文提示**：配置文件位于项目目录中且含有明文 `api_key` 时，启动时提示改用 `moss key set`。
- **项目配置先确认**：项目的 `.mosscode/config.yaml` 可以把服务地址指向任何主机，并引用你保存的凭据；它在你看过服务地址和凭据名称并确认之后才会被使用，内容变化后重新确认，见 [项目配置需要确认](cli.md#项目配置需要确认)。

只识别完整的原文：被截断、编码或拆开的密钥不会被发现。这些措施防的是“顺手带出去”，挡不住以你的身份运行、专门窃取密钥的程序。

## 选择与切换

启动时优先使用 `--provider 配置名称`，其次使用显式设置的 `default_provider`，两者都会直接进入对话。都未设置时，只有一项配置直接进入；多项配置按 `name` 列出，用 ↑ / ↓ 移动、Enter 确认。需要每次启动手动选择时，删除或注释 `default_provider`。Esc、Ctrl+C 或 Ctrl+Q 可退出选择，确认前不创建模型客户端、不发起请求。进入对话后，启动区显示选中的 `name`，实际 API 请求仍使用该配置的 `model`。

无界面 `--prompt` 模式不打开列表，优先使用显式 Provider、默认配置；两者都没有时使用列表第一项。对话中可用 `/model` 切换到其他已配置的提供商，对话历史保留（见“终端界面与本地命令”）；选中[跟随](#跟随-claude-code-或-codex)的一项时重新读取它跟随的设置。

## 协议与兼容服务

`protocol` 选择协议，`base_url` 选择端点；模型和供应商名称没有白名单。

| protocol | 接口 | 默认端点 |
|----------|------|----------|
| anthropic-messages | Messages | Anthropic SDK 默认值 |
| openai-responses | Responses | OpenAI SDK 默认值 |
| openai-chat-completions | Chat Completions | OpenAI SDK 默认值 |

兼容服务使用对应协议适配器。`base_url` 填 SDK 请求前缀，不填写完整的 `/messages`、`/responses` 或 `/chat/completions` 地址。OpenAI 兼容服务通常需包含 `/v1`，具体以服务文档为准。可选 `headers` 支持额外认证头，勿把含凭据的配置提交到仓库。

支持简写：`anthropic` → Messages，`openai` → Responses，`openai-compat` → Chat Completions。Messages 的 thinking 支持 `false`、`true`（adaptive）或显式对象；手动 enabled 模式需配置预算。模型名不用于能力推断，服务拒绝的参数会显示错误。Chat Completions 的 `stream_usage` 和 `max_tokens_field` 可配置，以适应端点差异。

Responses 使用 `store=false` 并默认请求 `reasoning.encrypted_content`，多轮原样回传服务返回的不透明推理数据。兼容服务不支持该选项时可设 `include_encrypted_reasoning: false`；缺少加密数据的推理条目不回传，仍保留正文历史，无法保留隐式推理连续性。

## 思考强度

`/thinking off|low|medium|high` 在本会话中调整思考强度，`/thinking default` 回到配置文件的设置；不带参数时打开选择列表，每一项说明当前模型实际会收到什么。档位从下一次请求开始生效，对主对话和使用同一模型的子 Agent 都有效；`/model` 切换后沿用。档位只在本进程中有效：`/clear` 保留，退出后恢复会话时回到配置。运行中不能切换，命令留在输入框，回复结束后再发送。

| 协议 | off | low / medium / high |
| --- | --- | --- |
| Messages | `thinking: {type: disabled}` | `thinking: {type: adaptive}` 加 `output_config.effort`；目录标明不支持 adaptive 的较早 Claude 模型（如 Haiku 4.5），以及目录中没有、配置中用 `enabled` 预算的模型，改为 `budget_tokens` 2048 / 8192 / 16384，不超过 `max_output_tokens` 的一半 |
| Responses | `reasoning.effort: none`；模型不支持 none 但支持 minimal 时用 minimal | `reasoning.effort` |
| Chat Completions | `reasoning_effort: none`（规则同上） | `reasoning_effort` |
| DeepSeek（Chat Completions） | `thinking: {type: disabled}`（放在请求体中） | `thinking: {type: enabled}` 加 `reasoning_effort`；DeepSeek 把 medium 按 high 处理 |

设置档位后，覆盖参数替换配置中的 `thinking`；`default` 时请求与配置完全相同。切换档位会改变请求参数，Anthropic 的提示缓存在切换后的第一次请求不会命中。

**不支持时提示并忽略。** 档位保留，但当前模型的请求按配置发送，切换到支持的模型后生效；`/status` 的“思考”一行和状态栏会注明。判断依据是[模型目录](#模型目录)：目录中这个模型不支持思考（如 `gpt-4.1`），不能关闭思考的模型（Opus 5.5、Sonnet 5.5）上的 `off`，以及既不支持 none 也不支持 minimal 的 OpenAI 模型（如 `o3`）上的 `off`。目录中没有的模型照常发送，由服务决定；服务拒绝时显示它的错误，改用其他档位或 `/thinking default` 即可。目录不准确时用配置项 `reasoning` 指定：`false` 不接受 `/thinking`，`true` 表示模型支持思考（跳过目录的判断）。

```yaml
  - name: gateway
    protocol: openai-chat-completions
    model: my-reasoning-model
    base_url: https://your-gateway.example/v1
    reasoning: true
```

## 图片输入

配置项 `images` 表示模型是否接受图片输入。省略时先看[模型目录](#模型目录)中是否有明确记录；没有时，`anthropic-messages` 和 `openai-responses` 视为接受，`openai-chat-completions` 视为不接受，因为兼容服务的模型差别很大。与实际不符时明确填写：

```yaml
  - name: vision-gateway
    protocol: openai-chat-completions
    model: replace-with-vision-model
    base_url: https://your-gateway.example/v1
    api_key_env: MY_GATEWAY_API_KEY
    images: true
```

`images` 为假时，请求不会因为图片而失败：历史中的图片发送时改为文字 `替代文本（当前模型不支持图片，已省略）`，`read_file` 读取图片返回 `image_unsupported`，MCP 结果中的图片也只留下文字说明。图片文件仍在会话目录中，`/model` 切换到接受图片的模型后，历史中的图片重新随请求发送。

图片在发送时才从会话目录读取并编码为 base64，同一个 Provider 缓存最近使用的 32 张编码结果。每次请求最多带最近的 20 张图片，更早的改为 `替代文本（较早的图片已省略）`；图片文件已被删除时改为 `[图片已丢失：替代文本]`。三种协议的映射：

| 协议 | 消息中的图片 | 工具结果中的图片 |
| --- | --- | --- |
| Messages | `image` 块（base64），保留在原来的位置 | `tool_result.content` 为文本块加图片块，只有图片时文本为 `(see attached image)`；出错的结果不能带图片，这些图片放在同一条消息的所有结果之后，前面加“出错的工具结果中的图片：” |
| Responses | `input_image`（data URL） | `function_call_output.output` 为 `input_text` 和 `input_image` 组成的数组 |
| Chat Completions | `image_url`（data URL） | `tool` 消息只放文本；这一批工具消息之后追加一条用户消息“工具结果中的图片：”，后面跟这些图片 |

## 模型目录

MossCode 带有一份模型目录，记录各家模型的上下文窗口、输出上限、价格和是否接受图片。数据来自 [LiteLLM](https://github.com/BerriAI/litellm) 的 `model_prices_and_context_window.json`（MIT 许可，Copyright (c) 2023 Berri AI），只保留聊天模型和这几项数据，许可证声明随数据一起保存在包内的 `mosscode/data/models.json.gz` 中。只使用数据，不依赖 LiteLLM 程序包。

### 查找

启动时按配置查找当前模型，只做精确匹配，不猜相近的名字：

1. 配置了 `catalog_model` 时，只查它：先按条目名查，没有时把它当作模型名按下面的步骤查；都没有时报配置错误。
2. 根据 `base_url` 的主机名推断服务商（未配置 `base_url` 时按协议：Messages 为 `anthropic`，两种 OpenAI 协议为 `openai`），先查 `<服务商>/<模型名>`，再查属于该服务商的 `<模型名>`。
3. 查名为 `<模型名>` 的条目。
4. 模型名带服务商前缀（如经由网关时的 `anthropic/claude-…`）时，查去掉前缀、属于该服务商的条目。

能识别的主机：`api.anthropic.com`、`api.openai.com`、`api.deepseek.com`、`api.moonshot.cn`、`api.moonshot.ai`、`api.z.ai`、`open.bigmodel.cn`、`dashscope.aliyuncs.com`、`dashscope-intl.aliyuncs.com`、`generativelanguage.googleapis.com`、`api.x.ai`、`api.mistral.ai`、`api.minimax.io`、`api.minimaxi.com`、`ark.cn-beijing.volces.com`，以及本机 11434 端口（Ollama）。其他网关只按第 3、4 步查找。

服务给模型起了目录中没有的名字时，用 `catalog_model` 指定对应的条目：

```yaml
  - name: deepseek
    protocol: anthropic-messages
    base_url: https://api.deepseek.com/anthropic
    model: my-flash-alias
    catalog_model: deepseek/deepseek-v4-flash   # moss models flash 查找条目名
    api_key_env: DEEPSEEK_API_KEY
```

### 默认值

配置中填写的值总是优先，目录只补没填的项：

| 配置项 | 依次取 |
| --- | --- |
| `context_window` | 配置值；目录中的窗口；128,000（估算） |
| `max_output_tokens` | 配置值；目录中的输出上限与 32,000 中较小的一个；16,384 |
| `images` | 配置值；目录中明确记录的是否支持图片；按协议的默认值（见[图片输入](#图片输入)） |

有些模型的输出上限和整个窗口一样大，而输出预留要从窗口中扣除，所以从目录取值时最多取 32,000。手动思考预算按最终的 `max_output_tokens` 校验。目录记录的窗口太小、放不下 MossCode 的输出预留和压缩余量时（一些老模型只有 4K–40K），不使用它，按 128,000 估算并在启动区说明；本地服务实际开了更大的窗口时填写 `context_window`。

目录中找不到模型时，启动区提示一次，`/model` 切换到这样的 Provider 时也提示。`/status` 的模型一行注明哪些值来自目录和条目名，`moss doctor` 的每个 Provider 一行显示匹配到的条目，或者给出填写建议。

### 更新

包内的目录在发布时生成。新模型发布后不必等 MossCode 更新：运行 `moss models update` 从 LiteLLM 下载最新数据，写入 `~/.mosscode/models.json.gz`，以后优先使用它。`moss models <关键词>` 查找条目，见 [命令行](cli.md#models)。旧版本下载的目录不含思考相关的字段，仍然可用，只是 `/thinking` 无法判断哪些模型不支持，重新运行 `moss models update` 即可补上。用户目录中的文件损坏时，改用包内的目录并在启动区提示，重新运行 `moss models update` 即可修复。

### 本次运行的共享调用预算

可在顶层配置：

```yaml
run_budget:
  requests: 200
  tokens: 1000000
```

两项均为非负整数，0 或不填写表示不限制。主对话、子 Agent、Skill fork、进程内团队成员，以及摘要、目标判断、记忆提取，共享同一份额度。预算从本次启动到退出；切换模型、`/clear`、恢复会话不重置，`/status` 显示已用请求与 Token。

请求额度在调用前领取；服务适配器内部的 HTTP 重试仍属于同一次调用，空回复重发、自动续写和新摘要请求各占一次。Token 按服务返回的总输入和输出累计，达到后停止新的请求；在途调用仍可继续产生 Token，所以这不是 Token 或费用的硬封顶。启用 Token 限额时，若服务缺少完整用量或流中断，停止后续调用并说明无法核算。预算耗尽按错误结束，目标暂停，已执行的工具和结果保留；已经启动的命令仍遵守各自的时间限制和停止规则。

开启预算时，团队的 `auto` 使用进程内成员以共享额度；显式指定 tmux 或 Windows Terminal 窗格时报错，改用 `auto` 或 `in-process`。默认未开启预算时，团队后端行为不变。

### 费用估算

目录中有价格的模型，每次请求按服务报告的 Token 数估算费用：未缓存输入 × 输入价格 + 缓存读取 × 缓存读取价格 + 缓存写入 × 缓存写入价格 + 输出 × 输出价格。缺少缓存价格时按输入价格计算。`/status` 的用量一行显示“费用 约 $0.0123”；部分请求没有价格（服务没报告 Token 数，或切换到了目录中没有价格的模型）时显示“≥ $…”，全部没有时显示“无价格信息”。子 Agent 和团队成员的请求与 Token 一样计入。`-p` 的 `json` 结果中是 `total_cost_usd`。

这只是估算：只用基础价格，不考虑分段价格、错峰优惠、批量价格和地区差异；人民币计价的服务也按目录中的美元价格计算。以服务商的账单为准。

## 重试与错误

请求在收到任何流事件之前遇到限流（429）、过载（529 / overloaded_error）、5xx、超时或连接中断时自动重试，默认最多 4 次，指数退避并带随机抖动，遵守服务给出的 `retry-after`（最长 60 秒）；界面状态行与 `--prompt` 的 stderr 显示重试原因和等待时间，等待期间 Esc 可取消。收到首个事件后的断流不重试，因为正文可能已显示。认证失败、请求不被接受等错误立即报告；上下文超限错误（HTTP 400/413 且报错提到 prompt is too long、maximum context length 等）标记为 `context_overflow`，由 Agent Loop 压缩后重试一次。`max_retries: 0` 关闭重试；SDK 自身的重试保持关闭，避免叠加。没有自动协议降级。未完整接收的 assistant 响应不进入历史；已经完整接收并执行的工具调用与实际结果保留。取消表示停止接收并关闭流，不保证远程服务已经停止计费。

模型偶尔返回没有可用内容的回复：既没有正文也没有工具调用（只有空白或只有思考也算），或者还没写出任何正文和工具调用就碰到输出上限（多半是思考过长）。这样的回复不进入历史，MossCode 带一条说明重发同一请求：空回复时请模型继续（刚收到工具结果时请它根据结果继续），思考截断时请它缩短思考、直接给出回复或下一步工具调用。说明只随重发的请求发送，排在历史之后，不写入历史，不改变之前已缓存的前缀。每次请求最多重发 2 次，界面状态行和 `--prompt` 的 stderr 显示“模型返回了空回复，重新请求（1/2）”。仍然没有内容时本轮以错误结束，说明中写出停止原因、收到的内容块类型和建议（思考截断时建议调高 `max_output_tokens` 或降低思考强度）。没有内容且停止原因是服务拒绝（`refusal`、`content_filter`）时不重发，直接报告。被丢弃的回复同样计入用量和费用。已经写出正文后才碰到输出上限的回复照常保留，MossCode 随即自动发一条“从中断处直接接着写，不要重复”的消息，请模型续写，下一段显示为新的一条回复，两段之间有一行“↻ 回复达到输出上限，自动续写（1/3）”。这条消息不算你的输入，恢复会话时显示为同样的一行。每轮最多自动续写 3 次，仍被截断时本轮停止，提示已经续写的次数，可以输入“继续”或调高 `max_output_tokens`；`/goal` 只在这时才因截断暂停。续写的几段合起来是完整的回答：`--prompt` 的 text 格式在 stdout 连续输出，stderr 打印上面那行说明；json 的 `result`、子 Agent 和 Skill 返回的结果都是合起来的全文。带工具调用的回复被截断时不续写：调用不执行，模型收到“参数可能不完整”的结果后重试。

## 无界面模式与输出格式

`-p` 是 `--prompt` 的简写。`--output-format` 只用于无界面模式：

| 格式 | 输出 |
| --- | --- |
| `text`（默认） | 正文流式输出到 stdout，提示与 `--show-usage` 用量输出到 stderr，与以往一致；任务中的 `@路径`（见 [终端界面](interface.md#引用文件)）附上后，stderr 打印一行“已附上 …” |
| `json` | 结束时输出一个结果对象：`type`、`subtype`（`completed`、`max_iterations`、`repeated_calls`、`finish_check_failed`、`error` 等停止原因；`repeated_calls` 见[重复调用检测](tools.md#重复调用检测)）、`is_error`、`result`（最终正文或错误说明）、`num_turns`、`duration_ms`、`usage`、`total_cost_usd`（按[模型目录](#费用估算)估算的美元费用，没有价格时为 null）、`compactions`（成功压缩上下文的次数）、`model`、`permission_mode` |
| `stream-json` | 每行一个 JSON 事件：`system`（init：cwd、model、permission_mode、tools）、`assistant`（text、tool_calls、stop_reason、usage）、`tool_result`（有图片时带 `images`：文件名、媒体类型、宽、高，不含图片数据）、`retry`、`reply_retry`（没有可用内容的回复被丢弃并重发：`attempt`、`max_attempts`、`reason`）、`auto_continue`（回复写到一半碰到输出上限，自动续写：`attempt`、`max_attempts`）、`notice`、`files_attached`（`entries`：每个 `@` 引用附上了什么，与界面中的说明相同）、`compact`（subtype 为 started、finished、failed，见 [上下文管理](context.md#界面与无界面输出)），最后一行是与 json 格式相同的 `result` |

```powershell
moss -p "列出 src 下的模块" --output-format json
```

配置了[完成前验收](hooks.md#完成前验收)时，未通过的草稿在 stream-json 中带 `stop_reason: finish_check`；达到失败上限后，三种输出格式均以退出码 1 结束，JSON 结果为 `subtype: finish_check_failed`。

JSON 输出始终为 UTF-8，与控制台代码页无关。`is_error` 为真时进程以退出码 1 结束，便于脚本判断；无界面模式不等待审批，需要确认的调用返回 approval_required。

## 脚本调用

应用和无界面入口共用 `Conversation`；`Conversation.ask()` 可在 Python 脚本中连续调用以保持多轮历史，用完后调用 `await conversation.close()` 关闭 Provider 和 MCP 会话。会话在退出后清空。
