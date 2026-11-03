# 模型提供商

Provider 配置、协议、重试与无界面输出。

## 添加配置

在 `config.yaml` 的 `providers` 列表追加配置即可接入兼容已有协议的服务。每项绑定一个模型；同一服务的多个模型可分别配置，使用不同 `name`，共用 `base_url` 和凭据。

凭据字段二选一：`api_key: '你的密钥'` 直接填写密钥；`api_key_env: DEEPSEEK_API_KEY` 填写环境变量名称，并在启动进程的环境中设置该变量。例如 PowerShell 中先执行 `$env:DEEPSEEK_API_KEY = '你的密钥'` 再启动应用。`api_key_env` 不能填写 `sk-...`；误填时会给出字段说明，错误中不回显该值。推荐用环境变量保存凭据。Anthropic 端点需要 bearer 认证时设 `auth_type: bearer`（默认 `api_key`）。

```yaml
  - name: my-gateway-model-a
    protocol: openai-chat-completions
    base_url: https://your-gateway.example/v1
    model: replace-with-service-model
    api_key_env: MY_GATEWAY_API_KEY
```

## 选择与切换

启动时优先使用 `--provider 配置名称`，其次使用显式设置的 `default_provider`，两者都会直接进入对话。都未设置时，只有一项配置直接进入；多项配置按 `name` 列出，用 ↑ / ↓ 移动、Enter 确认。需要每次启动手动选择时，删除或注释 `default_provider`。Esc、Ctrl+C 或 Ctrl+Q 可退出选择，确认前不创建模型客户端、不发起请求。进入对话后，启动区显示选中的 `name`，实际 API 请求仍使用该配置的 `model`。

无界面 `--prompt` 模式不打开列表，优先使用显式 Provider、默认配置；两者都没有时使用列表第一项。对话中可用 `/model` 切换到其他已配置的提供商，对话历史保留（见“终端界面与本地命令”）。

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

## 图片输入

配置项 `images` 表示模型是否接受图片输入。省略时，`anthropic-messages` 和 `openai-responses` 视为接受；`openai-chat-completions` 视为不接受，因为兼容服务的模型差别很大。与默认值不符时明确填写：

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

## 重试与错误

请求在收到任何流事件之前遇到限流（429）、过载（529 / overloaded_error）、5xx、超时或连接中断时自动重试，默认最多 4 次，指数退避并带随机抖动，遵守服务给出的 `retry-after`（最长 60 秒）；界面状态行与 `--prompt` 的 stderr 显示重试原因和等待时间，等待期间 Esc 可取消。收到首个事件后的断流不重试，因为正文可能已显示。认证失败、请求不被接受等错误立即报告；上下文超限错误（HTTP 400/413 且报错提到 prompt is too long、maximum context length 等）标记为 `context_overflow`，由 Agent Loop 压缩后重试一次。`max_retries: 0` 关闭重试；SDK 自身的重试保持关闭，避免叠加。没有自动协议降级。未完整接收的 assistant 响应不进入历史；已经完整接收并执行的工具调用与实际结果保留。取消表示停止接收并关闭流，不保证远程服务已经停止计费。

## 无界面模式与输出格式

`-p` 是 `--prompt` 的简写。`--output-format` 只用于无界面模式：

| 格式 | 输出 |
| --- | --- |
| `text`（默认） | 正文流式输出到 stdout，提示与 `--show-usage` 用量输出到 stderr，与以往一致 |
| `json` | 结束时输出一个结果对象：`type`、`subtype`（`completed`、`max_iterations`、`error` 等停止原因）、`is_error`、`result`（最终正文或错误说明）、`num_turns`、`duration_ms`、`usage`、`compactions`（成功压缩上下文的次数）、`model`、`permission_mode` |
| `stream-json` | 每行一个 JSON 事件：`system`（init：cwd、model、permission_mode、tools）、`assistant`（text、tool_calls、stop_reason、usage）、`tool_result`（有图片时带 `images`：文件名、媒体类型、宽、高，不含图片数据）、`retry`、`notice`、`compact`（subtype 为 started、finished、failed，见 [上下文管理](context.md#界面与无界面输出)），最后一行是与 json 格式相同的 `result` |

```powershell
moss -p "列出 src 下的模块" --output-format json
```

JSON 输出始终为 UTF-8，与控制台代码页无关。`is_error` 为真时进程以退出码 1 结束，便于脚本判断；无界面模式不等待审批，需要确认的调用返回 approval_required。

## 脚本调用

应用和无界面入口共用 `Conversation`；`Conversation.ask()` 可在 Python 脚本中连续调用以保持多轮历史，用完后调用 `await conversation.close()` 关闭 Provider 和 MCP 会话。会话在退出后清空。
