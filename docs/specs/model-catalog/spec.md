# 模型目录

日期：2026-11-03。状态：已实现，待手动验收。

来源：
- [产品差距评审](../../research/product-gap-review.md)的 A1 和 R1，排在“完善十：上手与安全网”的第一项。用户在图片输入完成后选择先做这一项（2026-11-03）。
- [图片输入](../image-input/spec.md)的决定 Q2：引入模型目录后，是否支持图片的默认值改用目录中的 `supports_vision`，配置中的值仍然优先。
- 评审（同日）：用户同意 Q1–Q4 都按推荐方案定稿。
- LiteLLM 的 `model_prices_and_context_window.json`（2026-11-03 从 GitHub 的 main 分支下载，3.0 MB）。MossCode 只使用这份数据，不引入 LiteLLM 包。仓库根目录的 LICENSE 规定：`enterprise/` 目录以外的内容使用 MIT 许可证，版权人为 Berri AI。
- pi 内置了模型目录（上下文窗口、输出上限、价格），在 `/model` 中直接选择。Claude Code 只支持 Anthropic 自己的模型，界面显示本次会话的费用（`/cost`），无界面输出的结果中有 `total_cost_usd` 字段。

## 问题

- **P1 上下文窗口要用户自己填。** 不填就按 128K 估算。对于 1M 窗口的模型（DeepSeek V4、Claude 5.5、GPT-5.5），这会让压缩提前很多；窗口小于 128K 的模型则可能在压缩前就超出上限。`moss setup` 的第 4 步也要求用户去服务文档里查窗口大小。
- **P2 输出上限固定为 16,384。** 一些模型能输出更长的内容，但默认值用不上。
- **P3 只显示 Token 数，不显示费用。** 用户不知道一次会话大约花了多少钱。
- **P4 是否支持图片只按协议猜。** 例如 `openai-chat-completions` 一律默认不支持图片，但其中有支持图片的模型；在其他协议下，也有不支持图片的模型。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `config.py` | `ProviderConfig`：`context_window: int \| None = None`；`max_output_tokens: int = 16384`；`images: bool \| None = None`。`accepts_images` 在 `images` 为 None 时按协议决定。解析时校验手动思考预算必须小于 `max_output_tokens` |
| F2 | `context.py` | `ContextBudget.for_provider` 在没有配置窗口时用 `DEFAULT_CONTEXT_WINDOW`（128,000），并把 `configured` 设为假。有效窗口 = `window − max(max_output_tokens, 20,000)`。类的 docstring 写着“窗口只来自配置，不来自模型表”，本规格改变这个设计 |
| F3 | 启动提示 | `cli.py:482` 和 `tui.py:1900`：未配置 `context_window` 时提示“按 128K 估算上下文”；`/status` 的上下文一行也会附上这句说明 |
| F4 | `setup.py` | 第 4/5 步询问上下文窗口，回车按 128K 估算；摘要中显示窗口 |
| F5 | 用量 | `UsageTotals` 累计输入、输出、缓存读写的 Token 数和请求次数，以及缺失计数（服务没有报告某一项时）。`/status` 的“本会话用量”和 `-p` 的 `result.usage` 都来自它。目前没有任何价格信息 |
| F6 | 子命令 | `launcher._subcommand` 支持 `setup` 和 `doctor`；运行子命令时不导入界面和 SDK |
| F7 | 打包 | 使用 setuptools，`[tool.setuptools.package-data]` 声明随包发布的数据文件 |
| F8 | LiteLLM 数据 | 共 4,561 个条目，其中 `mode` 为 `chat` 或 `responses` 的有 3,468 个，来自 105 个服务商。每个条目都有 `litellm_provider`；有的条目以服务商为前缀（`deepseek/deepseek-v4-flash`、`moonshot/kimi-k3`、`zai/glm-5.3`），有的没有前缀（`claude-sonnet-5-5`、`gpt-5.5`、`deepseek-v4-flash`）。同一个模型常见于多个服务商（Bedrock、Azure、Vertex、各类转售商），窗口和价格可能不同 |
| F9 | 字段 | 本规格用到的字段：`max_input_tokens`、`max_output_tokens`、`input_cost_per_token`、`output_cost_per_token`、`cache_read_input_token_cost`、`cache_creation_input_token_cost`、`supports_vision`，价格单位是美元每 Token。另有分段价格（`…_above_272k_tokens`）、批量和优先级价格、DeepSeek 的错峰价格（`off_peak_pricing`），本规格不使用。`supports_vision` 只出现在 1,864 个条目中（1,468 个为真，396 个为假），缺少该字段的条目不代表不支持 |
| F10 | 体积 | 只保留 chat、responses 条目和上面 7 个字段时，JSON 约 740 KB，gzip 压缩后约 48 KB |
| F11 | 样例 | `deepseek-v4-flash`：窗口 1,000,000，输出上限 393,216，支持图片。`deepseek-v4-pro`：不支持图片。`claude-opus-5-5`：窗口 1,000,000，输出上限 128,000。`moonshot/kimi-k3`：窗口和输出上限都是 1,048,576。`deepseek-chat`：窗口 131,072，输出上限 8,192 |
| F12 | 用户的配置 | `moss doctor` 显示用户的默认 Provider 使用 `api.deepseek.com`，模型名为 `deepseek-flash`。目录中没有这个名字，只有 `deepseek-v4-flash` 和其他服务商的 `deepseek-v4.1-flash`。因此按名字查找对这位用户无效，需要 [Q1](#决定) 的别名 |

## 目标

1. 用户只填写模型名时，上下文窗口、输出上限和是否支持图片都有可靠的默认值；配置中写明的值始终优先。
2. 查不到模型时，程序明确说明找不到哪个名字、改用了什么值，以及如何指定。
3. `/status` 和无界面输出能显示估算费用。
4. 目录随包发布，不联网也能使用，也可以手动更新。

## 需求

### 目录（MC）

- **MC1** 包内带一份目录快照 `mosscode/data/models.json.gz`。它由 LiteLLM 的数据精简而来：只保留 chat、responses 条目和 F9 中的 7 个字段，另外记录来源 URL、下载日期和 LiteLLM 的许可证声明。
- **MC2** 用户目录中如果有 `~/.mosscode/models.json.gz`（由 MC5 写入），就以它为准，否则使用包内快照。文件损坏时退回包内快照，并在启动区提示一次。
- **MC3** 目录在第一次查询时加载（解压加内存索引），之后在进程中缓存。`--version`、`--help` 和不需要模型的子命令都不加载目录。
- **MC4** 查找顺序：
  1. 如果配置了 `catalog_model`（Q1），就只查这个名字，不再按下面的规则查找。
  2. 根据 `base_url` 的主机名（未配置时用协议的官方地址）推断服务商，查 `<服务商>/<模型名>`；再查名为 `<模型名>`、并且 `litellm_provider` 等于该服务商的条目。主机名与服务商的对应表见[设计](#设计)。
  3. 查名为 `<模型名>` 的条目（不带前缀）。这类条目大多是第一方服务商的模型。
  4. 模型名本身带前缀时（例如 `deepseek/deepseek-v4-flash`），按完整名称查找。
  5. 以上都没有结果时视为找不到。**不做模糊匹配**：名字相近的模型窗口可能相差很大，猜错比不猜更糟。
- **MC5** 新增 `moss models update`：从 LiteLLM 的固定地址下载数据，按 MC1 精简后原子写入 `~/.mosscode/models.json.gz`，输出条目数和新旧差异的数量。下载遵守 `HTTPS_PROXY`，失败时不影响原有文件。发布前用同一段代码重新生成包内快照。
- **MC6** 新增 `moss models [关键词]`：列出名字中包含关键词的条目，显示服务商、窗口、输出上限、输入和输出价格（每百万 Token）、是否支持图片，最多 30 条。不带关键词时，显示每个已配置的 Provider 匹配到了哪个条目，或者没有匹配。用于查找 `catalog_model` 该填什么。

### 默认值（DF）

- **DF1** `context_window`：配置值，否则目录中的 `max_input_tokens`，否则 128,000。
- **DF2** `max_output_tokens`：配置值，否则取目录中的 `max_output_tokens` 和上限中的较小者，否则 16,384。上限见 [Q2](#决定)。
- **DF3** `images`：配置值，否则目录中明确写出的 `supports_vision`，否则按协议决定（现有行为）。
- **DF4** 手动思考预算的校验（F1）使用 DF2 得到的值。
- **DF5** 窗口和输出上限来自目录时，不再提示“按 128K 估算”；改在 `/status` 的模型一行注明“窗口、输出上限来自模型目录（<条目名>）”。
- **DF6** 找不到模型时，启动区提示一次：“模型目录中没有 <模型名>，按 128K 估算上下文；可以在配置中填写 context_window，或用 catalog_model 指定目录中的条目（`moss models <关键词>` 查找）”。这条提示取代原来的 128K 提示。
- **DF7** `/model` 切换后，用新 Provider 的结果重新计算（与现有的窗口切换行为相同）。

### 费用（CO）

- **CO1** 每次请求结束时，按当时所用模型的价格计算费用：未缓存输入 × 输入价 + 缓存读 × 缓存读价 + 缓存写 × 缓存写价 + 输出 × 输出价。服务没有拆分时，未缓存输入 = 输入 − 缓存读 − 缓存写。缺少缓存价格时按输入价计算。
- **CO2** 累计到 `UsageTotals.cost_usd`。没有价格的请求单独计数：全部请求都没有价格时显示“无价格信息”；只有部分请求有价格时显示为“≥ $x”。
- **CO3** 只使用基础价格，不考虑分段价格、错峰价格、批量价格和地区差异，所以显示为“约 $0.0123”。人民币计价的服务（例如 DeepSeek）也按目录中的美元价格计算。文档说明这只是估算，以服务商账单为准。
- **CO4** 费用显示的位置见 [Q3](#决定)。
- **CO5** 后台记忆提取、子 Agent 和团队成员的请求，与 Token 用量一样处理：现在计入哪个累计，费用就计入哪个累计，不额外合并。

### 上手（ON）

- **ON1** `moss setup` 的第 3 步填完模型名后，立即在目录中查找（服务商按第 2 步的地址推断）。找到时，第 4 步显示“模型目录：窗口 1M，回车采用”，并且不把窗口写进配置（以后随目录更新）。找不到时，提示与 DF6 相同，并保留原来的询问。
- **ON2** `moss doctor` 的 Provider 一行加上目录的匹配结果：“目录：deepseek-v4-flash（1M / 32K）”，或“目录中没有 deepseek-flash”。找不到时给出建议：填写 `catalog_model` 或 `context_window`。

### 文档（DOC）

- **DOC1** `providers.md` 新增“模型目录”一节，内容包括：查找顺序、`catalog_model`、默认值、更新方式、费用估算的局限，以及 LiteLLM 的来源和许可证。`context.md` 的配置表和 F2 中“不维护模型窗口表”的说法同步修改。`cli.md` 补充 `moss models`。
- **DOC2** `config.example.yaml`、README 的配置一节：`context_window` 改为“通常不必填写，模型目录中没有该模型时再填”，并给出 `catalog_model` 的例子。
- **DOC3** 包内附带 LiteLLM 的 MIT 许可证声明（MC1）；`CHANGELOG.md` 的 `[Unreleased]` 记一条。

## 设计

### 模块

| 模块 | 职责 |
| --- | --- |
| `catalog.py`（新） | `ModelInfo`（条目名、服务商、窗口、输出上限、4 个价格、`vision: bool \| None`）；`load_catalog()`（MC2、MC3）；`lookup(model, base_url, protocol, alias) -> ModelInfo \| None`（MC4）；`slim(raw) -> dict`（MC1、MC5 共用）；`HOST_PROVIDERS` |
| `config.py` | `ProviderConfig` 新增 `catalog_model: str \| None`，以及用于记录来源的 `catalog: ModelInfo \| None`（不参与比较和 repr）。解析时调用 `lookup`，按 DF1–DF3 填入默认值；记录哪些值来自目录，供 DF5 和 ON2 使用 |
| `context.py` | `ContextBudget.configured` 在窗口来自目录时也为真；修改 docstring |
| `usage.py`、`messages.py` | `UsageTotals` 新增 `cost_usd: float` 和 `missing_cost: int`；`request_cost(usage, info)`（CO1） |
| `agent.py` | 累计用量时同时累计费用，价格取当前 Provider 的 `config.catalog` |
| `builtin_commands.py` | `/status` 的模型一行（DF5）和费用（CO4） |
| `cli.py`、`tui.py` | DF6 的提示取代 128K 提示；`-p` 结果中的费用（Q3） |
| `models_command.py`（新） | `moss models`、`moss models update`（MC5、MC6）；由 `launcher._subcommand` 调用 |
| `setup.py`、`doctor.py` | ON1、ON2 |
| `scripts/update_models.py`（新，不随包发布） | 发布前刷新包内快照：调用 `catalog.slim` |

主机名与服务商的对应（MC4 第 2 步）：

| 主机名 | 服务商 |
| --- | --- |
| `api.anthropic.com` | `anthropic` |
| `api.openai.com` | `openai` |
| `api.deepseek.com` | `deepseek` |
| `api.moonshot.cn`、`api.moonshot.ai` | `moonshot` |
| `api.z.ai` | `zai` |
| `open.bigmodel.cn` | `bigmodel`（找不到时再查 `zai`） |
| `dashscope.aliyuncs.com`、`dashscope-intl.aliyuncs.com` | `dashscope` |
| `generativelanguage.googleapis.com` | `gemini` |
| `api.x.ai` | `xai` |
| `api.mistral.ai` | `mistral` |
| `api.minimax.io`、`api.minimaxi.com` | `minimax` |
| `ark.cn-beijing.volces.com` | `volcengine` |
| `localhost:11434`、`127.0.0.1:11434` | `ollama` |

这张表在实现时逐个与目录中的 `litellm_provider` 核对，目录中没有的服务商不写进表里。

### 数据流

```
config.yaml ─> _provider() ─> catalog.lookup ─> ProviderConfig（窗口、输出上限、图片的值，以及它们的来源）
                                                    │
                     ContextBudget（窗口）    providers（max_tokens、图片）
                                                    │
                     每次请求的 Usage ─> request_cost ─> UsageTotals.cost_usd ─> /status、-p 结果
```

### 依赖

不新增依赖。解压用标准库 `gzip`，`moss models update` 用已有的 `httpx` 下载。

### 取舍

- **精确匹配，不做模糊匹配。** 代价是像 `deepseek-flash` 这样的别名需要用户写一行 `catalog_model`。好处是不会给出错误的窗口：窗口偏大，请求会超出服务的上限；窗口偏小，压缩会太早。
- **快照随包发布，不在运行时联网。** 启动不依赖网络；目录会随时间过时，需要用 `moss models update` 或升级 MossCode 来更新。
- **只用基础价格。** 分段价格和错峰价格让计算复杂很多，而费用本来就只是估算；文档写明以账单为准。
- **gzip 压缩的 JSON，而不是 SQLite。** 数据只读，加载后全部放在内存里；48 KB 的文件解析只需几毫秒，不必引入数据库。

## 决定

| # | 问题 | 决定 | 曾考虑的其他选择 |
| --- | --- | --- | --- |
| Q1 | 目录中没有该模型名时（例如 `deepseek-flash`），如何指定条目 | **新增 `catalog_model`**。Provider 新增可选字段 `catalog_model: deepseek-v4-flash`，只用于查目录，不影响发给服务的模型名。`moss doctor` 和启动提示会建议填写它 | 不新增字段，只能手动填写 `context_window` 和 `max_output_tokens`：配置项少一个，但拿不到价格，图片能力也只能手动设置 |
| Q2 | 输出上限默认值的上限 | **32,000**：目录值和 32,000 取较小者（与 Claude Code 对 Opus、Sonnet 的默认值相同）。需要更长输出时在配置中写明。目录中只有窗口、没有输出上限时，仍用 16,384 | 直接使用目录值：`kimi-k3` 的输出上限等于窗口，有效窗口会变成负数；DeepSeek V4 会预留 393K，压缩阈值降到约 600K。或者保持 16,384，只从目录取窗口：最稳妥，但 P2 没有解决 |
| Q3 | 费用显示在哪里 | **`/status` 和 `-p` 结果**。`/status` 的“本会话用量”加一句“约 $0.0123”；`-p` 的 json 和 stream-json 结果加 `total_cost_usd`（与 Claude Code 的字段名相同，无价格时为 null） | 另外在状态栏的 Token 用量后显示费用：随时可见，但状态栏已经比较长，窄窗口下会被截断；或者新增 `/cost` 命令：与 Claude Code 一致，但内容和 `/status` 重复 |
| Q4 | 是否提供 `moss models update` | **提供**（MC5）：模型更新很快，不必等 MossCode 发版 | 只随发版更新快照：少一个联网的命令，但新模型要等下一个版本 |

## 不做

- `/model` 中从目录选择模型并自动生成 Provider（pi 的做法）：目录中有 105 个服务商，地址和凭据仍需用户配置，收益不大。可以以后再做。
- 分段价格、错峰价格、批量价格和人民币计价。
- 用目录中的其他字段（`supports_reasoning`、`supports_prompt_caching`、`deprecation_date`）改变行为。
- 模型即将下线时提醒。
- models.dev 等其他数据源。

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `catalog.py`：`slim`、加载与缓存、`lookup`、主机名表；`scripts/update_models.py` 生成包内快照；`package-data` | Q1 | `tests/test_catalog.py`：用小型替身目录覆盖查找顺序的每一步、带前缀的名字、`catalog_model`、找不到、用户目录优先、文件损坏退回；快照可以解析，并包含样例条目（F11） |
| T2 | `config.py` 的 DF1–DF4 和来源记录；`context.py`；DF5、DF6 的提示 | T1、Q2 | `tests/test_config.py`、`tests/test_context.py`：配置值优先；目录值；找不到时用旧默认值；输出上限取较小值；思考预算按新的上限校验；图片能力的三种来源 |
| T3 | 费用：`request_cost`、`UsageTotals`、`/status`、`-p` 结果 | T2、Q3 | `tests/test_usage.py`、`tests/test_commands.py`、无界面测试：缓存拆分、缺少缓存价格、部分请求没有价格（显示 ≥）、全部请求都没有价格 |
| T4 | `moss models`、`moss models update`；`setup`、`doctor` | T1、Q4 | `tests/test_models_command.py`：用本地 HTTP 测试服务模拟下载，覆盖原子写入、下载失败时保留原文件、关键词列表、Provider 匹配表；`tests/test_setup.py`、`tests/test_doctor.py` |
| T5 | 文档：DOC1–DOC3 | T2–T4 | `tests/test_docs.py` |

只运行上表中的相关测试文件。

## 验收

用户在 Windows Terminal 中，使用自己的配置：

| # | 场景 | 期望 |
| --- | --- | --- |
| B1 | 不改配置，运行 `moss doctor` | DeepSeek 一行显示“目录：deepseek/deepseek-flash（1M / 32K）”（实现时发现目录中有带服务商前缀的 `deepseek/deepseek-flash`，按主机名即可匹配，不需要 `catalog_model`），没有 128K 警告 |
| B2 | 不改配置，启动并运行 `/status`，对话一轮后再运行一次 | 启动区不再提示 128K；`/status` 的模型一行注明窗口、输出上限、图片能力来自模型目录（deepseek/deepseek-flash）；对话后“本会话用量”显示“费用 约 $…” |
| B3 | `moss models kimi` | 列出 kimi 相关条目，含窗口、价格和图片支持 |
| B4 | `moss models update` | 下载成功，显示条目数；再次运行 `moss models` 能正常显示 |
| B5 | `moss -p "你好" --output-format json` | 结果中有 `total_cost_usd` |

## 实现中的调整

- 目录记录的窗口放不下 MossCode 的输出预留和压缩余量时（LiteLLM 中约 490 个老模型，窗口 4K–42K），不使用它，按 128K 估算并在提示中说明原因；否则这些配置会在启动时报错，而本地服务可能实际开了更大的窗口。
- `catalog_model` 先按条目名精确查找，找不到时按 MC4 第 2–4 步解析（例如 `deepseek-v4-flash` 配合 DeepSeek 的地址），仍找不到时报配置错误。
- `tokens_label` 先按十进制取整（128,000 → 128K），再按二进制（393,216 → 384K）。
- `scripts/export_public.py` 检查 `.gz` 文件解压后的内容，包内快照可以随公开仓库导出。
- 子 Agent 后台任务的用量一行也显示费用（CO5）。
