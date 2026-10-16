# Pi harness：给 MossCode 的一手资料调查

调查日期：2026-10-08（Asia/Hong_Kong）。范围：官方文档、作者文章和公开源码；未运行 Pi，未评估 MewCode 附件实现。本文将已证实的机制、历史观点与 MossCode 设计建议分开。

## 项目身份与版本范围

用户提到的 Pi Agent，本文按 Mario Zechner 的 Pi coding agent 理解。旧仓库 `badlogic/pi-mono` 当前重定向到 `earendil-works/pi`。官方将其定位为可定制的最小 agent harness，提供 TypeScript SDK、CLI 与独立支持库。[官方仓库](https://github.com/earendil-works/pi)

本次读取的 `main/packages/coding-agent/package.json` 标示 `1.1.0`、作者 Mario Zechner 和 MIT 许可。这是所读工作分支的文件版本，不能据此声称 npm 最新发布为 1.1.0。未取得可绑定全部读取文件的 HEAD SHA；下列 `main` 链接可能随开发变化。[package.json](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json)

先用 Context7 解析 `Pi coding agent`，选择官方仓库索引 `/earendil-works/pi`，分别查询架构、会话/压缩、扩展/权限，再通过官方网页与源码交叉核对。Context7 本次列出的版本为 v0.87.1、v0.99.0，与当前 main 不同，因此没有把索引版本当成当前版本。

## 已证实的架构边界

| 层 | 证据中的职责 | 对 MossCode 的借鉴（设计建议） |
|---|---|---|
| `pi-ai` | 多供应商模型 API、标准消息与流式事件、工具声明/参数验证、用量统计、可序列化上下文 | 统一内部消息，供应商适配集中在模型边界；MossCode 按用户要求先覆盖 Anthropic 与 OpenAI |
| `pi-agent-core` | 状态、调用循环、工具执行、事件流、取消和消息队列 | 单一模型—工具循环，不依赖 CLI 组件；策略经明确接口接入 |
| `pi-coding-agent` | 组装模型和 agent，增加工作目录工具、会话、上下文资源、扩展与多种入口 | 应用层负责项目资源、持久化和产品行为 |
| `pi-tui` | 终端组件、输入、差量渲染等显示机制 | UI 消费事件，不承担执行或会话规则；不必自己重写 TUI 框架 |

来源：[pi-ai README](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md)、[agent README](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[How Pi Works](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/how-pi-works.md)、[tui README](https://github.com/earendil-works/pi/blob/main/packages/tui/README.md)。

当前 monorepo 还含 `pi-durable` 等包。存在新包不意味着入门项目需要复刻；学习经典四层边界已足够说明核心设计。[包列表](https://github.com/earendil-works/pi#packages)

### 模型之外的可靠执行机制

`packages/agent/src/agent-loop.ts` 的 `runLoop()` 调用模型、提取工具调用、执行并回填结果；`prepareToolCall()` 先验证参数，再经过 `beforeToolCall`，阻断返回错误结果。`executePreparedToolCall()` 接收取消信号并记录结果；事件供外层订阅，`afterToolCall` 能后处理。源码同时支持顺序/并行调用与 steering/follow-up，不意味着业务上应该将所有工具并发。[agent-loop.ts](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts)

`createCodingTools()` 的基础工具组合是 `read/bash/edit/write`；源码另有 `powershell/grep/find/ls`，所以“四个工具”描述基础组合，不是整个产品的工具总数。最小集合应由项目任务决定。[tools/index.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/index.ts)

## 最值得借鉴：运行历史与模型上下文分离

会话存储是带 `id/parentId` 的 JSONL 追加树，当前叶子确定活跃分支。`buildSessionProjection()` 根据活跃路径、上下文编辑和压缩，构造下一次请求的模型消息；状态专用 `CustomEntry` 不进入模型上下文。`SessionManager` 的历史记录与请求投影职责明确分开。[session-manager.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/session-manager.ts)

压缩按上下文预算触发，保留最近消息并追加摘要/边界记录，不删除原历史；可手动调用或经扩展替换摘要策略。默认摘要还记录读过和修改过的文件。压缩摘要会有信息损失，源码机制并不证明摘要完整可靠。[compaction 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)、[compaction.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/compaction/compaction.ts)

设计推断：MossCode 可先做线性 JSONL 记录 + 独立 `ContextBuilder`，再增加分支；这保留了“运行事实可查、送给模型的上下文可控”的思想，不必第一版实现整棵会话树。回放事件不能自动等同重新执行工具；会话分支也不能自动恢复磁盘文件，这些须单独定义。

## 扩展、权限与当前 MCP：不要沿用历史描述

扩展在 Pi 进程内运行；可注册工具、命令、事件、供应商、UI，也可变换上下文和阻断工具。`tool_call` 处理失败会阻断调用；`ctx.executeTool()` 的嵌套调用也经过同一校验和 hooks。扩展不是沙箱。[extensions 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)

官方明确：Pi 没有内置的文件系统、进程、网络、凭据访问限制系统，默认沿用启动它的用户和进程权限；更强边界需要外部容器/沙箱。项目资源信任流程与每次工具调用权限限制是两件事。[官方 Permissions & Containerization](https://github.com/earendil-works/pi#permissions--containerization)、[How Pi Works：Trust and permissions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/how-pi-works.md#trust-and-permissions)

当前安全文档进一步明确：工作目录不是路径限制；项目 trust 防止未获信任的项目资源自动加载，但不限制启用工具的访问能力。`AGENTS.md/CLAUDE.md` 等上下文文件仍可能被加载，所以“信任提示”不能当作防注入或 OS 权限边界。本文仅转述相关机制，不采用历史文章将防护泛称为无用的观点。[security.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)

官方 `permission-gate.ts` 示例按危险 shell 命令模式弹出确认。这说明 hooks 可以接策略，不构成对任意脚本行为的完备限制。[permission-gate.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/permission-gate.ts)

当前 Pi **已内建 MCP**：官方说明支持 stdio 和 streamable HTTP；工具可直接暴露、延迟查找或由 codemode 脚本调用。MCP 调用也通过工具流水线和权限 hooks；MCP 本身不是 agent 循环，也不提供统一隔离保证。[MCP 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)

官方站点说明 Codemode 在 JavaScript 沙箱中组织工具调用。此处“脚本运行沙箱”不能推导为被调用 shell/文件工具也自动隔离。当前站点仍将 sub-agents、plan mode 和 permission popups 作为可扩展工作流，而非强制内置流程。[pi.dev](https://pi.dev/)

Mario 的 2025-11-30 文章介绍最小 prompt、最小工具、可观察交互和上下文控制，也陈述当时不做 MCP、默认不弹权限确认等取舍。这是设计历史和作者个人选择；尤其“No MCP”已不适合描述当前 Pi，更不能由它证明“安全约束无意义”。[作者原文](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)

## 如何理解 harness（本文归纳，不是 Pi 的正式统一定义）

Harness 是模型外部让代理任务实际运行的环境：消息/模型适配、模型—工具反馈循环、工具校验与执行、取消、上下文构造、持久化、策略接入与可观察事件。Skill 是按需加载的任务知识；MCP 是工具/资源协议；多代理是一种可选工作流。三者都不能单独替代 harness。

“极简”意味着稳定的小核心与可组合策略，并非移除所有约束。可在模型边界验证协议，在执行边界落实权限与预算，在产品层选择工作流。按任务给模型表达、选择和修正空间，与强制工具结果结构和取消机制并不冲突。以上是依据源码边界作出的设计推断。

## 给主项目比较的结论

1. 可以同时学习 MewCode 的工程路径与 Pi 的边界思想；是否冲突取决于是否保留一个清晰的运行核心，而不是项目来源数量。
2. 如果 MewCode 的 agent 同时直接依赖 memory、permission、hook，可以保持一个循环，逐步把会话/上下文/策略拆成契约；不要拼两套循环或维护两份独立会话真相。
3. Python 重新实现这些边界是合理的学习路径；不需要把 TypeScript Pi SDK 硬接进 Python。若以 Pi 为实际运行后端，官方 RPC 已支持非 Node 应用，但这会改变项目的自研深度，需明确选择。[RPC 入口说明](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/how-pi-works.md#interfaces)
4. 面试价值应落在可演示的可靠性与取舍：同一核心驱动 CLI/无 UI 测试，取消可终止模型和工具，失败工具有对应结果，压缩可审计，权限拒绝不会偷偷执行，实际任务有成功率/耗时/用量记录。这些是 MossCode 的建议验收，不是声称 Pi 已替它完成。
5. 公开 Pi 文档和源码足够开展当前架构比较，无需用户再提供代码。只有用户特指某一版本、fork 或私人改动时才需其链接/快照。

## 建议阅读顺序

`packages/agent/src/agent-loop.ts` → `packages/agent/src/types.ts` → `packages/coding-agent/src/core/session-manager.ts` → `packages/coding-agent/src/core/compaction/compaction.ts` → `packages/coding-agent/src/core/extensions/types.ts` → `packages/coding-agent/examples/extensions/permission-gate.ts`。

先追一条请求从输入到工具结果及下次模型请求的路径，再研究策略挂接；不需要把当前整个 monorepo、TUI 或新 durable runtime 一次读完。
