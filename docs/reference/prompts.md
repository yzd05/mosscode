# 系统提示与缓存

系统提示的组成、请求补充、项目指令，以及缓存与用量统计。

## 系统提示

默认提示位于 `src/mosscode/prompts.py`，按身份、系统约束、任务模式、动作执行、工具使用、语气风格、文本输出排列，用空行分隔，含默认工具时约 1,400 字符。“未读就禁止修改”由工具程序检查（见 [先读后改](tools.md#先读后改)），提示只说明约定。固定模块不包含工作目录、日期或本次任务，模块顺序也不代表 API 以外的权限等级。

工具使用模块说明通用规则：优先专用工具；**互不依赖的只读调用在同一次回复中一起发出**，应用会并发执行（见 [结果、并发与历史](tools.md#结果并发与历史)）；截断结果不当作已检查。每个工具维护 prompt_summary 和 prompt_guidelines：摘要由构建器按启用工具生成“当前可用工具”清单；用法规则只追加在 API 工具描述中，不在系统提示中重复，因此每次请求只发送一遍，自定义 `system_prompt` 时也不会丢失。语气模块要求简洁：先给结论，不复述计划，不逐条叙述工具调用。自动模式列出六个本地工具和外部 MCP 工具；计划模式只列出三个只读工具、仅限只读命令的 `execute_command` 和 `exit_plan_mode`，同一工具集下稳定提示保持一致。执行指导覆盖调查范围、失败恢复、部分执行和适度验证。设计演变见 [第五章规格](../specs/chapter-05-prompts/spec.md)、[Pi 对照说明](../research/pi-prompt-design.md) 和 [阶段 6 规格](../specs/refinement-06-prompt-readme/spec.md)；[第五章默认提示示例](../specs/chapter-05-prompts/default-prompt.md) 是当时的版本，运行时以当前注册工具和自定义设置为准。

省略或留空 `system_prompt` 使用新版默认提示；旧示例 `You are MossCode, a helpful coding assistant.` 自动升级到新版。其他显式 `system_prompt` 仍完整替换稳定提示，不作为尾部附加模块。如果原配置填写了另一份旧提示，需要删除该字段才能使用新版默认内容。应用不自动重写含凭据的配置文件。

## 环境补充与模式提醒

环境补充提供当前日期、操作系统、shell、工具工作目录、是否启用工具、权限档位，以及启动时采集的 git 快照（分支和最多 30 行 `git status --short`；非仓库标为 `is_repo: false`，git 不可用或超时标为不可用，不影响启动）。快照不随后续修改刷新。不扫描环境变量或记忆。

模式提醒只在模式变化时发送一次：进入计划模式后的第一次请求说明计划模式规则，退出时说明已回到自动模式；当前档位持续体现在环境补充中。提醒是仅本次请求携带的 `<system-reminder>`，不进入用户历史或 UI；Responses 映射为 developer 消息，Chat 为 system 消息，Anthropic 附加在最后一条用户消息的末尾（缓存断点之后）。标签只标明用途，不赋予内容额外权限。

## 项目指令（AGENTS.md）

启动时加载指令文件并追加到稳定提示末尾（位于缓存前缀中）：先是 `~/.mosscode/AGENTS.md`，再从文件系统根目录向下到当前目录，每级目录取 `AGENTS.md`，没有时取 `CLAUDE.md`。越靠近工作目录的文件越靠后，冲突时以其为准；它们不能覆盖系统约束和权限规则。单个文件超过 32 KiB 截断并在提示中注明，非 UTF-8 文件跳过；加载结果在启动时以本地提示显示。自定义 `system_prompt` 时项目指令同样附加。指令在会话开始时读取一次，修改后需重新启动。

## 缓存

缓存由服务端处理，应用保持稳定提示和工具定义的前缀。Anthropic SDK 实际连接官方地址时，设置两个缓存断点：稳定 system 块末端，以及请求中最后一个历史块（上一次工具结果或用户消息），其后的环境块和模式提醒不标记。多轮工具循环因此可复用已发送的历史前缀，不必每轮重新计算全部输入。非官方 Messages 端点默认不附加缓存参数；确认支持后设 `prompt_cache: true`，设 false 可关闭显式标记。实际地址检查也覆盖 SDK 通过环境变量切换的端点。OpenAI/DeepSeek 等协议依赖服务自身的自动缓存，不发送 Anthropic 的标记，`prompt_cache` 不用于关闭服务自动缓存。计划模式与自动模式的工具列表不同，切换模式（包括批准计划后在同一任务内退出）会有一次缓存前缀变化。审批策略始终由应用执行。

## 用量

用量事件来自服务实际返回的统计，跨模型请求累计输入/输出 Token，不代表模型窗口容量。全部缺失显示 `—`，部分缺失显示已知下限 `≥`，不估算或伪造零。Chat 兼容服务需要支持并配置 `stream_usage: true` 才能返回流式用量；默认不强制启用，保留兼容端点差异。

Usage 读取 Responses 的 `input_tokens_details`、Chat 的 `prompt_tokens_details`，兼容 DeepSeek 的 `prompt_cache_hit_tokens/prompt_cache_miss_tokens`，并处理 Anthropic 的 `cache_read_input_tokens/cache_creation_input_tokens`。Anthropic 总输入包含普通输入、缓存写入和缓存读取；其他协议已提供总输入，不重复加缓存。流式累计字段覆盖前值。底栏显示会话累计缓存读取及已提供的写入：未知为“未提供”，部分缺失为 `≥` 下限，已知零保持零。

`--prompt --show-usage` 将每次模型请求的详细统计写到 stderr，stdout 仍是正文和工具结果。人工对比场景和三次真实请求的缓存探测入口见 [manual-evaluation.md](../specs/chapter-05-prompts/manual-evaluation.md)。默认测试和界面预览不调用真实 API。缓存命中并不保证任务质量或节省总成本，真实服务验证与定性比较仍需实际运行；目前不实现 Skill/记忆加载或自动评估。
