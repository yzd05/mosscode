# 图片输入

日期：2026-11-03。状态：规格已定稿，未实现。

来源：
- 完善九第一版评审（2026-11-02）中用户的意见：“图片输入太复杂，先不做”。[完善九](../refinement-09-productization/spec.md) 的“不做”和[产品差距评审](../../research/product-gap-review.md)的“不建议照搬”都记录了这个决定。
- 0.1.0 发布后（2026-11-03），用户问 Claude Code 和 pi 有没有图片输入。我的回答是两者都有，并且可以分阶段做：先让模型能读图片文件，再支持粘贴和拖入。用户要求写这份规格。**本规格推翻上面“先不做”的决定**，那两处记录已改为指向本规格。
- 评审（同日）：用户同意 Q1–Q3 都按推荐方案定稿。
- Claude Code 文档（2026-11-03 查阅）：
  - `Read` 工具读取 PNG、JPEG、GIF、WebP，以图片内容返回给模型；过大的图片会缩放并重新压缩，以满足模型的尺寸限制；超过 128 MB 的图片会被拒绝，并提示用户先缩小。
  - 交互界面中可以粘贴图片：`Ctrl+V`，iTerm2 中是 `Cmd+V`，Windows 和 WSL 中是 `Alt+V`。粘贴的图片在光标处显示为 `[Image #N]`。
  - 也可以把图片拖进终端，或在消息中写图片路径。
  - 召回历史草稿时，图片随草稿一起恢复。
- pi 的文档与源码（2026-11-03 查阅）：
  - `read` 工具支持 jpg、png、gif、webp、bmp，图片作为附件发送。
  - 当前模型不支持图片时，图片从请求中省略，并在原处留一句说明。
  - 设置项：
    - `images.autoResize`：默认开启，最大 2000×2000。
    - `images.blockImages`：不向模型发送任何图片。
    - `terminal.showImages`、`terminal.images`：在 kitty、iTerm2 协议的终端中显示图片。
  - 粘贴：快捷键 `ctrl+v`，Windows 和 WSL 中是 `alt+v`；“Paste an image or drag it into a compatible terminal.”
  - 缩放：
    - 最大边长 2000 像素；base64 后不超过 4.5 MB，低于 Anthropic 的 5 MB 限制。
    - 先按 EXIF 方向转正。
    - 缩放后分别编码为 PNG 和 JPEG（质量 80），取较小的一个；仍然过大时逐步降低 JPEG 质量。
  - 剪贴板：
    - Linux：先用 `wl-paste --list-types` 和 `wl-paste --type`，失败再用 `xclip -selection clipboard -t TARGETS`。
    - Windows 和 WSL：调用 `powershell.exe -NoProfile -Command`，超时 5 秒。
    - macOS：系统剪贴板。
  - 三种协议的映射见下文 PV1–PV3。

## 问题

- **P1 模型看不到图片。** 用户想让模型看界面截图、报错截图或设计稿时，只能用文字描述。
- **P2 `read_file` 遇到图片只会报错。** 读取 PNG 等文件得到 `binary_file` 错误，模型无法查看仓库中的图片资源。
- **P3 MCP 的图片结果被丢弃。** 浏览器、截图类 MCP 服务器返回的 `image` 内容只显示为“未展示的 MCP 内容类型”。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `messages.py` | `ContentBlock.kind` 只有 `"text"` 和 `"thinking"`；`ToolResult` 只有文本 `output`，`model_text()` 是模型看到的全部内容 |
| F2 | `serialization.py` | `KINDS = {"text", "thinking"}`。读取会话时遇到其他 kind 会判定这一行不合法并跳过，因此旧版本读不了含图片的消息（见[取舍](#取舍)） |
| F3 | `providers.py` | `_blocks`、`_turns` 和三个 Provider 只生成文本：Anthropic 的 `tool_result.content` 是字符串；Responses 和 Chat Completions 的工具输出也是字符串 |
| F4 | `context.py` | `estimate_message` 只按文本估算；压缩时把 `message.text` 作为文本交给模型 |
| F5 | `tools/files.py` | `read_file` 只读 UTF-8；遇到 NUL 字节报 `binary_file`，解码失败报 `encoding_error` |
| F6 | `mcp_client.py:199-210` | 只处理 `text` 内容，其他类型记入 `unsupported_types`，并提示一行“本章未展示的 MCP 内容类型” |
| F7 | `config.py` | `ProviderConfig` 没有描述模型能力（是否支持图片）的字段 |
| F8 | `tui.py:192` | `PromptArea` 继承 `TextArea`，没有处理剪贴板图片。Textual 8.2.8 的 `events.Paste` 只带文本（括号粘贴）。在终端中拖入文件，实际是把文件路径作为文本粘贴进来 |
| F9 | `agent.py:337` | `Agent.run(prompt: str, ...)` 只接收文本 |
| F10 | `session.py:73` | 大结果写入会话目录的 `tool-results/`；这是“内容放文件、记录只放引用”的现有做法 |
| F11 | 依赖 | Pillow 13.0.0（2026-11-03 的最新版）许可证为 MIT-CMU，与 MIT 兼容；要求 Python ≥ 3.11；当前环境未安装 |
| F12 | 模型目录 | 产品差距评审 R1 计划引入 LiteLLM 的模型目录快照。该目录中每个模型有 `supports_vision` 字段，可用作“是否支持图片”的默认值 |
| F13 | 服务商 | Anthropic Messages、OpenAI Responses 支持在工具结果中放图片。Chat Completions 的 `tool` 消息只能放文本，图片要放到随后的 `user` 消息中。DeepSeek 等兼容服务的部分模型不接受图片，收到会报 400 |

## 目标

1. 模型能通过 `read_file` 查看图片文件，也能看到 MCP 工具返回的图片。
2. 用户能在输入框中粘贴截图，或把图片文件拖进终端，作为消息的一部分发给模型。
3. 当前模型不支持图片时，界面明确提示，请求不会因此失败。
4. 会话保存、恢复、压缩和上下文估算都能正确处理图片，不让会话文件膨胀。

## 需求

### 消息与会话（IM）

- **IM1** 消息内容可以包含图片块。每个图片块包含：
  - 媒体类型：`image/png`、`image/jpeg`、`image/gif` 或 `image/webp`；
  - 图片文件、宽、高；
  - 一段替代文本，例如 `[图片 #1]`，或工具结果中的文件路径。
- **IM2** 工具结果可以附带图片（`read_file`、MCP）。图片与文本输出并存，文本输出仍是模型能看到的说明。
- **IM3** 图片数据不写入会话 JSONL：
  - 处理后的图片按内容的 SHA-256 命名，保存在会话目录的 `images/` 中，相同图片只存一份；
  - JSONL 中只记录文件名、媒体类型、宽、高和替代文本。
- **IM4** 恢复会话时，图片文件缺失的块改为文本 `[图片已丢失：<替代文本>]`，会话继续可用，并在界面中提示一次。

### Provider（PV）

- **PV1** Anthropic Messages：
  - 消息中的图片转为 `{"type":"image","source":{"type":"base64","media_type":…,"data":…}}`，放在 `user` 内容中原来的位置。
  - 工具结果有图片时，`tool_result.content` 用块数组：先放文本，再放图片。只有图片时，文本为 `(see attached image)`。
  - Anthropic 不接受出错结果（`is_error`）中的图片。这种情况下，图片放在同一条 `user` 消息中所有 `tool_result` 之后，前面加一句说明。
- **PV2** OpenAI Responses：
  - 消息中的图片转为 `{"type":"input_image","image_url":"data:<mime>;base64,<data>"}`。
  - 工具结果有图片时，`function_call_output.output` 用 `input_text` 和 `input_image` 组成的数组。
- **PV3** OpenAI Chat Completions：
  - 消息中的图片转为 `{"type":"image_url","image_url":{"url":"data:…"}}`。
  - `tool` 消息只放文本；只有图片时，文本为 `(see attached image)`。
  - 这一批工具消息之后追加一条 `user` 消息，内容为 `工具结果中的图片：`，后面跟这些图片。
- **PV4** 图片在发送时才从文件读取并编码为 base64；同一个 Provider 实例缓存最近使用的编码结果，避免每一轮都重新编码整个历史。
- **PV5** 请求中最多保留最近 20 张图片，更早的改为替代文本加 `（较早的图片已省略）`。理由：
  - Anthropic 一次请求超过 20 张图片时，对每张图片的尺寸有更严的限制；
  - 旧截图很少还有用，却持续占用上下文。

### 模型能力（CF）

- **CF1** Provider 配置新增 `images`（布尔，可省略），表示模型是否接受图片输入。省略时的默认值见 [Q2](#决定)。
- **CF2** `images` 为假时：
  - 发送请求时，历史中的图片改为替代文本加 `（当前模型不支持图片，已省略）`；
  - `read_file` 读取图片返回错误 `image_unsupported`，说明当前模型不支持图片；
  - MCP 结果中的图片按同样方式替换为文字；
  - 粘贴图片时界面提示 `当前模型不支持图片（providers.<名称>.images）`，不插入图片。
- **CF3** `/model` 切换到支持图片的模型后，历史中的图片重新随请求发送，因为图片文件仍在会话目录中。

### 读取图片（RF）

- **RF1** `read_file` 按文件头识别图片（PNG、JPEG、GIF、WebP、BMP），不看扩展名。BMP 转换为 PNG。其他二进制文件仍报 `binary_file`。
- **RF2** 读取图片时，`offset`、`limit` 不起作用。文本输出为一行说明：路径、原始尺寸、文件大小；缩放过的，再写明缩放后的尺寸。图片附在结果中。
- **RF3** 路径检查、读取权限和 `read_file` 的其他规则不变，图片与文本文件走同一条检查路径。
- **RF4** 超过 50 MB 的文件不解码，返回 `image_too_large`，提示先缩小。Pillow 的解压炸弹检查超限时，也返回这个错误。
- **RF5** 解码失败（文件损坏、格式不完整）返回 `image_invalid`。

### 图片处理（IP）

- **IP1** 所有进入消息的图片都经过同一个处理函数，来源包括 `read_file`、MCP、粘贴和拖入：
  - 按 EXIF 方向转正；
  - 最大边长不超过 2000 像素；
  - base64 后不超过 4.5 MB。
- **IP2** 已在上述限制内、格式受支持、不是动图且没有 EXIF 旋转的图片原样保存，不重新编码。
- **IP3** 超出限制时：
  - 按比例缩小，分别编码为 PNG 和 JPEG（质量 80），取较小的一个；
  - 仍然超过时，依次用质量 70、60、50 再试；
  - 都不满足时，再把边长缩小到原来的 3/4 重试，最多三轮；
  - 最后仍超过时报 `image_too_large`。
- **IP4** 动图（GIF、WebP）只保留第一帧。Anthropic 本来也只读取第一帧；保留第一帧可以让大小可控。

### 粘贴与拖入（PA）

- **PA1** 输入框中按 `Ctrl+V` 读取剪贴板中的图片，Windows 中是 `Alt+V`：
  - 有图片时，在光标处插入 `[图片 #N]`，并在输入框上方列出本条草稿附带的图片（编号、来源、尺寸）；
  - 剪贴板中没有图片时，`Ctrl+V` 保持输入框原有的行为，`Alt+V` 提示“剪贴板中没有图片”。
  - Windows Terminal 自己处理 `Ctrl+V`，只粘贴文本，所以 Windows 中用 `Alt+V`；这与 Claude Code、pi 一致。
- **PA2** 剪贴板中是复制的文件（资源管理器、Finder）时，其中的图片文件按 PA3 处理。
- **PA3** 粘贴的文本中如果只有一个或多个已存在的图片文件路径，每个路径替换为 `[图片 #N]`：
  - 路径可以带引号；多个路径以空白分隔；
  - 这就是“拖入文件”在终端中的实际效果。
  - 只要文本中还有其他内容，就照常作为文本粘贴。
  - 用户手打的路径不转换，由模型决定是否调用 `read_file`。
- **PA4** `N` 在一个会话内递增，不因发送而重置，方便用户和模型用“图片 #3”指代某张图。
- **PA5** 发送时，只附带文本中仍然出现的 `[图片 #N]`：删掉占位文字就等于删掉图片。图片紧跟在各自的占位文字之后，与文本交错排列。
- **PA6** 读取剪贴板在后台线程中进行，超时 5 秒，不阻塞界面。读取失败时提示原因，例如 Linux 中缺少 `wl-paste` 或 `xclip`。
- **PA7** 召回历史草稿时（上箭头），图片随草稿一起恢复，只在同一进程内有效。
- **PA8** 粘贴是用户主动的操作，不需要授权。读取粘贴路径中的文件，也不经过工具权限：这与用户在消息中直接写入文件内容是同一回事。

### 上下文（CX）

- **CX1** 每张图片的 Token 估算值为 `min(ceil(宽 × 高 / 750), 1600)`。宽高取处理后的尺寸，公式来自 Anthropic 的计算方法；1600 是 Anthropic 对大图缩放后的上限，用于对各服务商偏高地估算。
- **CX2** 压缩时，交给模型的历史中，图片改为替代文本加尺寸；摘要不包含图片。压缩后，被压缩部分的图片不再发送，文件保留在会话目录中。
- **CX3** 工具结果预算（`tool_budget`）只计算文本，图片受 PV5 和 CX1 约束。

### 界面（UI）

- **UI1** 用户消息中的 `[图片 #N]` 保持原文显示，消息下方用一行暗色文字列出图片：编号、来源、尺寸、大小。用户消息的灰色背景不变。
- **UI2** 工具结果中的图片显示为一行说明，例如 `图片 · 3840×2160 · 2.1 MB（发送尺寸 2000×1125）`。终端内不渲染图片像素（见[不做](#不做)）。
- **UI3** `/status` 显示当前模型是否接受图片。

### 无界面模式与子 Agent（HL）

- **HL1** `-p` 模式中，`read_file` 和 MCP 的图片与交互模式相同。`stream-json` 输出只包含图片的元数据（文件名、媒体类型、尺寸），不包含图片数据。
- **HL2** 子 Agent 和团队成员使用自己的会话目录保存自己读到的图片。主 Agent 发给它们的任务说明仍是文本，不传递图片。

### 文档（DOC）

- **DOC1** 更新以下文档：
  - `docs/reference/tools.md`：`read_file` 的图片规则和新错误码；
  - `docs/reference/providers.md`：`images` 字段、默认值和三种协议的差异；
  - `docs/reference/interface.md`：`Ctrl+V` / `Alt+V`、拖入、占位文字；
  - `docs/reference/mcp.md`：图片内容；
  - `docs/reference/context.md`：图片的估算和压缩；
  - `docs/reference/sessions.md`：`images/` 目录。
- **DOC2** README 的“使用”一节加一句图片输入的用法。
- **DOC3** `CHANGELOG.md` 的 `[Unreleased]` 中记录新增功能。
- **DOC4** 完善九规格的“不做”和产品差距评审中的图片输入一行改为指向本规格（定稿时已完成）。

## 设计

### 模块

| 模块 | 职责 |
| --- | --- |
| `images.py`（新） | `ImageRef` 数据类；`load_image(path_or_bytes) -> ProcessedImage`，负责识别、转正、缩放和编码（IP）；`estimate_image_tokens`；Pillow 只在这里导入 |
| `messages.py` | `ContentBlock` 增加 `"image"` 类型和 `image: ImageRef \| None` 字段；`ToolResult` 增加 `images: tuple[ImageRef, ...] = ()` |
| `session.py` | `store_image(processed) -> ImageRef`：按内容哈希写入 `images/`；`image_path(ref)` |
| `serialization.py` | 序列化图片块和 `ToolResult.images`；读取时检查字段，文件缺失时按 IM4 处理 |
| `providers.py` | `_blocks`、`_turns` 按 PV1–PV5 生成图片内容；`images` 为假时替换为文字（CF2）；编码缓存 |
| `config.py` | `ProviderConfig.images` 及其默认值 |
| `context.py` | `estimate_message` 计入图片（CX1）；压缩时替换为文字（CX2） |
| `tools/files.py` | `read_file` 识别并读取图片（RF）。工具需要通过执行上下文访问会话和当前模型的能力，现有上下文是否已经提供，待调查 |
| `mcp_client.py` | `image` 内容解码 base64 后交给 `images.py` 处理 |
| `clipboard.py`（新） | `read_clipboard_image() -> bytes \| list[Path] \| None`：Windows、macOS 用 `PIL.ImageGrab.grabclipboard()`；Linux 先用 `wl-paste`，再用 `xclip`（Pillow 在 Linux 中也是调用这两个程序，自己调用可以给出更明确的错误） |
| `tui.py` | `PromptArea` 的按键与 `on_paste`、占位文字、附件列表；提交时组装内容块 |
| `agent.py`、`conversation.py` | `run` 接受 `str \| Sequence[ContentBlock]`，原有的字符串调用不变 |

### 数据流

```
粘贴/拖入 ─┐
read_file ─┼─> images.load_image ─> session.store_image ─> ImageRef
MCP 图片  ─┘                                                  │
                                                              v
               Message.content / ToolResult.images（JSONL 只记引用）
                                                              │
               Provider 发送时：images 为假 → 替代文字；
                               否则读文件 → base64 → 协议格式（PV1–PV3）
```

### 依赖

新增 Pillow（MIT-CMU）。作为必需依赖还是可选依赖，见 [Q1](#决定)。各主要平台和 Python 3.12–3.14 都有预编译 wheel，不需要编译环境。

### 取舍

- **图片放文件而不放 JSONL。** base64 让每张截图在 JSONL 中多出几 MB，恢复和搜索会话都会变慢；同一张图被读多次时，也会存多份。放文件的代价是：复制会话时要连 `images/` 一起复制。
- **旧版本读不了含图片的会话。** 旧版本会跳过含图片的消息行。这只在降级时发生，影响可以接受；不为此设计兼容格式。
- **统一的处理函数。** 四种来源经过同一条路径，限制、错误码和测试都只有一份。
- **2000 像素与 4.5 MB。** 与 pi 相同：低于 Anthropic 的单图限制（5 MB；一次超过 20 张图片时为 2000×2000），也不超过 OpenAI 高细节模式的处理尺寸。
- **不按扩展名识别。** 扩展名可能是错的，文件头更可靠；也避免把改名的二进制文件当作图片解码。

## 决定

| # | 问题 | 决定 | 曾考虑的其他选择 |
| --- | --- | --- | --- |
| Q1 | Pillow 作为必需依赖还是可选依赖 | **必需**。截图通常要缩放，没有 Pillow 时图片功能几乎不可用；安装 `mosscode` 后直接能用，更像产品。代价是安装包多出几 MB | 可选依赖 `mosscode[images]`：没有安装时，只发送已在限制内的 PNG、JPEG、GIF、WebP，其他情况提示安装 |
| Q2 | 省略 `images` 时的默认值 | **按协议决定**：`anthropic-messages`、`openai-responses` 默认支持，`openai-chat` 默认不支持。DeepSeek 等兼容服务大多走 Chat Completions，其中部分模型不接受图片。完善十引入模型目录后，改用目录中的 `supports_vision`，配置中的值始终优先 | 一律默认不支持，需要用户在配置中打开；或一律默认支持，由服务商的 400 错误提示用户 |
| Q3 | 实施时机 | **完善九 9B（网络工具）之后，作为单独一轮**。分两批，每批单独人工验收：第一批 T1–T6（模型能看图片，不改界面）；第二批 T7–T9（粘贴与拖入）。完善十（模型目录等）顺延 | 先做完善十，因为模型目录能解决 Q2 的默认值；或与 9B 合并为 0.2.0 |

## 不做

- 在终端中显示图片像素（kitty、iTerm2、Sixel 协议）：各终端支持不一，Windows Terminal 只支持 Sixel，收益小。
- WSL 中通过 `powershell.exe` 读取 Windows 剪贴板：WSL 中仍可拖入文件，或让模型读取文件。
- `-p` 的 `--image` 参数：无界面模式可以在提示中写路径，由模型调用 `read_file`。
- PDF、音频、视频输入。
- 把图片传给子 Agent 和团队成员（HL2）。
- 单独的“禁止发送图片”开关（pi 的 `blockImages`）：把 `images` 设为假即可达到同样效果。

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `images.py`：识别、转正、缩放、编码、估算（IP、CX1）；`pyproject.toml` 加入 Pillow | Q1 | `tests/test_images.py`：用 Pillow 现场生成测试图片，覆盖各格式、EXIF 旋转、超尺寸、超大小、BMP 转换、动图、损坏文件、解压炸弹，以及原样保留的情况 |
| T2 | 消息模型、`session.store_image`、序列化（IM） | T1 | `tests/test_sessions.py`：保存后恢复；相同图片只存一份；文件缺失时的替代文字 |
| T3 | `config.py` 的 `images` 字段及默认值（CF1） | Q2 | `tests/test_config.py` |
| T4 | 三个 Provider 的图片映射、编码缓存、PV5、CF2 的文字替换 | T2、T3 | `tests/test_providers.py`、`tests/test_tool_providers.py`：用模拟 SDK 断言三种协议的请求体，覆盖出错结果中的图片、只有图片的结果、超过 20 张、不支持图片的模型 |
| T5 | `read_file` 读取图片（RF）；`mcp_client.py` 的图片内容；上下文估算与压缩（CX） | T2、T4 | `tests/test_tools.py`（含路径检查在读取图片时同样生效）、`tests/test_mcp.py`、`tests/test_context.py`；`tests/test_agent.py` 中，用模拟模型走完“调用 `read_file` 读图片 → 下一轮请求中带图片”的完整流程 |
| T6 | 第一批的文档：tools、providers、mcp、context、sessions（DOC1 相关部分）、CHANGELOG | T5 | `tests/test_docs.py`；第一批人工验收 |
| T7 | `clipboard.py`（PA6） | T1 | `tests/test_clipboard.py`：用替身替换 Pillow 和子进程，覆盖三个平台的分支、超时、缺少工具；Windows 专用分支按 `sys.platform` 标记 |
| T8 | `PromptArea`：`Ctrl+V` / `Alt+V`、`on_paste` 的路径识别、占位文字与附件列表、提交组装、召回（PA、UI1）；`agent.run` 接受内容块 | T5、T7 | `tests/test_tui.py`、`tests/test_agent_tui.py`：无界面 TUI 中模拟按键和 `Paste` 事件，断言发送给模拟模型的内容块顺序；删除占位文字后不附带图片 |
| T9 | 界面说明行（UI2）、`/status`（UI3）、`interface.md`、README（DOC） | T8 | `tests/test_docs.py`；第二批人工验收 |

改动只运行上表中的相关测试文件，提交前由 CI 跑全量测试。

## 验收

用户在 Windows Terminal 中，使用自己的配置：

| # | 场景 | 期望 |
| --- | --- | --- |
| A1 | 让模型“看一下 `docs/ui/` 中某张预览图，描述布局” | 模型调用 `read_file`；结果行显示尺寸；模型的描述与图片内容一致 |
| A2 | 读取一张 4K 截图 | 结果行显示原始尺寸和发送尺寸，请求成功 |
| A3 | 使用不支持图片的模型（例如 DeepSeek）重复 A1 | `read_file` 返回 `image_unsupported`，界面没有报错，会话可以继续 |
| A4 | A1 之后 `/resume` 这个会话，继续提问图片中的细节 | 模型仍能看到图片 |
| A5 | 截图（Win+Shift+S）后在输入框按 `Alt+V`，输入问题后发送 | 输入框出现 `[图片 #1]` 和附件列表，模型回答与截图相符 |
| A6 | 从资源管理器把一张图片拖进输入框 | 插入 `[图片 #N]`，而不是路径文本 |
| A7 | 粘贴两张图片后删掉一个占位文字再发送 | 模型只收到一张图片 |
| A8 | 剪贴板中只有文字时按 `Alt+V` | 提示剪贴板中没有图片，输入框不变 |

Linux、macOS 上的粘贴由单元测试和 CI 覆盖，不做人工验收。
