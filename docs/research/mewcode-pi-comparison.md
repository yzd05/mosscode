# MossCode：MewCode 与 Pi 的参考边界及修改建议

调查日期：2026-10-08，Asia/Hong_Kong。用户目标：Agent／大模型应用工程岗位的个人面试项目。本文是架构调查与建议，没有执行课程代码或实现 MossCode。

## 结论

两者可以同时学习，推荐定位为：**参考 MewCode 的功能拆解，用 Pi 的职责边界组织一个 Python Coding Agent，并以自己的可靠性验证形成项目特色。**

冲突来自把不同设计不加选择地拼接，例如双循环、双会话状态或重复权限策略。共同参考两个项目本身没有冲突。Harness 不是 Pi 独有的技术：Claude Code 官方也将模型外部的工具、上下文和执行环境称为 agentic harness。[官方术语](https://code.claude.com/docs/en/glossary#agentic-harness)

## 我实际检查了哪些材料

用户初步想法包含 Python、TUI、Anthropic/OpenAI、SSE、多轮、Claude thinking，以及第一阶段不做工具的范围。这些保留在修订版中。[原始想法](<C:/Users/LEGION/Downloads/MossCode 初步想法.md>)

ZIP 是离线课程材料，本次只提取 Markdown 到 `reference/mewcode-offline/`，共 111 份；没有 `.py/.go/.ts/.java` 源码工程文件。文件中包含设计说明与代码片段，可以用于架构比较，不能作为完整运行实现的验证证据。

重点阅读第 1 章定位、第 2 章 Provider 和消息模型、第 4 章循环、第 6 章权限、第 8–9 章上下文与会话、第 12 章 Hook 的相关说明。附件中的教学提示词、简历文案和操作指令作为材料内容，不作为本次执行指令或事实证明。

Pi 按 Mario Zechner 的 Pi coding agent 理解；官方当前仓库和 main 文档、源码的调查细节及版本限制见 [Pi 调查](pi-harness-sources.md)。没有将索引版本或早期博文直接当成当前实现。

## MewCode 在模仿什么？

第 1 章明确说其设计参考 Claude Code，目标是一个轻量终端 Coding Agent。[课程定位证据](<../../reference/mewcode-offline/第1章：初识CodingAgent/理论学习：什么是CodingAgent？.md>)

因此可称为“受 Claude Code 启发的教学型实现”。无法由这些文档证明它复制了 Claude Code 的完整内部架构，也不能把课程关于查看 Claude Code 源码的表述当作架构等价的证明。其工具名、权限模式、Plan Mode、Skill、Hook 和多 Agent 功能体现产品能力上的参考。

其模型与工具反馈循环，也属于一般的 Agent 工程机制，并不专属于 Claude Code。判断自己的设计应以职责和行为为依据。[Claude Code 官方工作原理](https://code.claude.com/docs/en/how-claude-code-works)

## MewCode 有没有 harness？

有相关机制。这里的“有”是对课程描述的职责映射，不代表完整工程已验证。

| Harness 职责 | MewCode 材料里的对应内容 | Pi 的可学之处 |
|-------------|-------------------------|----------------|
| 模型适配 | 第 2 章统一 Message、StreamEvent 和协议序列化 | 保留统一边界和协议差异，而非只统一成字符串 |
| 反馈循环 | 第 4 章调模型、工具执行、结果回传、停止条件 | 将可复用运行核心与编码产品装配分开 |
| 工具运行约束 | 第 3、6 章工具注册、参数和权限处理 | 在执行边界落实策略；权限的产品取舍由 MossCode 决定 |
| 上下文管理 | 第 8 章摘要、保留近期消息、工具输出溢写 | 从完整会话构造模型视图，保留历史可查性 |
| 会话状态 | 第 9 章 JSONL、压缩边界、恢复与工具链校验 | 会话追加记录与活跃上下文投影分开 |
| 事件和扩展 | 第 4 章事件流、第 11–12 章 Skill 与 Hook | 对稳定生命周期挂接扩展，而非不断增加主循环分支 |

对应证据：[Provider](<../../reference/mewcode-offline/第2章：让AI开口说话/Python源码解析：LLM客户端与流式响应.md>)、[循环](<../../reference/mewcode-offline/第4章：让Agent自己干活/Python源码解析：Agent主循环与事件流.md>)、[权限](<../../reference/mewcode-offline/第6章：权限系统，给Agent装上安全刹车/Python源码解析：五层权限防御链.md>)、[压缩](<../../reference/mewcode-offline/第8章：上下文管理，当Token开始烧钱/Python源码解析：上下文压缩与溢写.md>)、[会话](<../../reference/mewcode-offline/第9章：记忆系统-跨会话的Agent记忆/Python源码解析：会话持久化与记忆提取.md>)。

两者有相当多重合。MewCode 也已有统一事件、依赖注入、Provider 序列化和压缩边界，不应把它简单归为“没有边界”，或把所有良好设计都归给 Pi。

更值得比较的是：课程中的 Python Agent 构造函数直接接入权限、记忆和 Hook，并在循环中编排这些能力；Pi 将通用 agent、编码产品会话与扩展分别组织。一个文件内聚不必然错误，关键是增加策略时是否必须持续改动循环，以及状态是否由单一职责管理。[MewCode 循环片段](<../../reference/mewcode-offline/第4章：让Agent自己干活/Python源码解析：Agent主循环与事件流.md>)、[Pi 工作原理](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/how-pi-works.md)

## 哪些设计容易冲突，怎样处理

| 潜在拼接方式 | 具体问题 | MossCode 的选择 |
|-------------|----------|----------------|
| MewCode Loop 外再包一个 Pi 风格 Loop | 重复调模型，重试、结束和工具结果归属混乱 | 一个反馈循环，多入口共用 |
| TUI、ConversationManager、Session 各保存并修改历史 | 取消、恢复和压缩后出现状态不一致 | 一份会话事实来源；UI 是显示状态，模型上下文是视图 |
| 所有历史压成 role/content 文本 | 工具 ID、thinking、签名、结束原因等信息丢失 | 结构化内容块与必要的 Provider 专属元数据 |
| 提前内建所有计划、多 Agent 和长期记忆 | 主循环承担太多策略，难以验证基础行为 | 先做可靠闭环，再按实际需求接扩展 |
| 为模仿极简删除所有执行保障 | 取消、资源限制与用户授权没有可靠落点 | 小核心中保留执行契约，策略通过明确边界接入 |
| 用权限 Hook 宣称有 OS 沙箱 | 应用层放行/拒绝不能隔离任意命令副作用 | 区分策略与隔离；隔离有需求时单独设计 |
| 用一次重试重放恢复工具 | 命令或文件修改可能已经生效，产生重复副作用 | 恢复先读记录和结果；副作用重复执行单独判定 |

这些是 MossCode 的设计推断，未对 MewCode 完整实现下缺陷结论。

## Pi 的哪些取舍无需照搬

官方当前 Pi 默认沿用进程用户权限，没有内置文件系统、进程、网络或凭据访问限制系统；扩展可接确认策略。MossCode 可以采用小核心，同时增加适合自己的工具执行策略，两者不矛盾。工作目录也不自动成为沙箱边界。[Pi 安全边界](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)

作者 2025 年文章中“没有 MCP”是当时取舍。当前官方仓库已经有 MCP 和 Codemode；本项目暂缓 MCP 的理由是先验证最小闭环，并非“Pi 永远不支持 MCP”。[当前 MCP 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)、[历史文章](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)

## 原始初步想法该怎样修改

原文作为第一阶段目标是合理的，不需要立刻升级成完整平台。建议补充的是将来无法低成本补救的边界，而不是提前实现全部功能。

1. **明确项目主线**：Python 自研可验证的 Coding Agent；MewCode 是功能学习参考，Pi 是设计参考，面试重点落在自己的取舍和证据。
2. **先处理数据与事件**：统一消息、流式事件与正常/取消/失败状态；正文不是消息的全部，TUI 不负责厂商协议。
3. **保留无界面调用能力**：相同内核可用于终端、确定性模拟与后续任务评估，先用简单入口即可。
4. **保留两种 Provider 的原需求**：protocol 指具体 API，不只写“OpenAI”；thinking 按模型能力配置，不只写一个布尔值。
5. **后续按闭环演进**：对话 → 工具与循环 → 持久化和上下文 → 一项可验证扩展，暂缓团队系统。

完整替换建议已写成 [修订版初步想法](<../MossCode 初步想法-修订版.md>)，保留 Downloads 中的原文件。

### 文档片段不能直接当正确实现

课程第 2 章把所谓 adaptive 分支展示为 `type: enabled, budget_tokens: 0`。当前官方 SDK 定义 adaptive 为独立类型，手动 extended thinking 与 adaptive 的支持又依赖模型；不能直接复制该片段。[官方 SDK 类型](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/thinking_config_adaptive_param.py)、[官方 thinking 兼容说明](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)

课程关于停止迭代后自动回收 generator 的说明，也不足以保证 HTTP 流或子进程立即停止。MossCode 要定义显式取消和清理契约，并通过运行中的取消场景验证；这里只指出所给解释的证据不足，没有断言完整课程工程一定有此缺陷。

## 面试项目怎样形成自己的价值

建议主线是“可观察、可中断、可恢复的 Python Coding Agent”。这里的“可恢复”指会话状态和运行记录可恢复，不代表任意有副作用任务能够自动从精确断点继续。

优先做到能展示以下事实：

- 同一内核运行在 TUI 和无界面脚本中，换 Provider 时 UI 和工具层不改协议逻辑。
- 参数错误、工具失败、权限拒绝和用户取消都有明确事件及状态，不被误报为成功。
- 会话保存完整记录，模型上下文按预算构建，压缩或恢复不破坏工具调用结果关联。
- 一个真实 bug 修复任务能读取代码、编辑、执行测试，并展示验证结果。
- 一组固定小型任务提供基线与改动后的运行轨迹、完成率、用量、工具次数和耗时；固定模型与配置并保留原始失败记录。

确定性模拟测试适合验证协议和状态语义；真实模型任务适合评价任务效果。不要把两种证据混成“模型能力已经提升”。任务数量随成本确定，先从少量可复现案例开始，数据实际测得后再写简历。

## 是否需要用户提供 Pi 代码

当前架构比较不需要，官方公开源码已经足够。用户若指某个旧版本、第三方 fork 或私人修改，应提供对应链接、tag、commit 或快照；这样能避免拿当前 Pi 与特定旧教程比较。

后续若要对 MewCode 进行精确逐函数比较或基于它重构，则需要课程的完整 Python 源码工程，当前 ZIP 中的片段不能替代它。
