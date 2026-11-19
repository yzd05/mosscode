# 路线图

MossCode 已经做了什么、接下来做什么，以及为什么这样排。每项的完整行为说明在 [参考文档](docs/reference/cli.md) 中，已发布的变化见 [更新日志](CHANGELOG.md)。

状态：**已实现**（已在 `main` 或开发分支上，有测试）、**进行中**、**待做**。优先级：**P0** 影响日常使用；**P1** 明显改善长任务或可维护性；**P2** 可选，按需要再做。

## 已实现

| 方面 | 内容 | 文档 |
| --- | --- | --- |
| Agent Loop | 单循环、明确的停止条件、空回复和思考截断后自动重发、正文截断后自动续写 | [工具与 Agent Loop](docs/reference/tools.md) |
| 模型协议 | Anthropic Messages、OpenAI Responses、Chat Completions（含相邻用户消息合并与尾部补充折叠以兼容 vLLM / SGLang 严格模板）；流式；提示缓存；思考强度；图片输入；随程序发布的模型目录与费用估算 | [模型提供商](docs/reference/providers.md) |
| 工具 | 读（含 AST 符号大纲 `outline` 与按符号读取 `symbol`）、写、改、命令、glob、grep、`repo_map` 代码结构地图（Tree-sitter + Personalized PageRank），任务清单、向用户提问、记忆、网页读取与搜索；先读后改；只读调用并发 | [工具](docs/reference/tools.md#工具) |
| 安全 | 四个权限档位与分层规则；命令风险分级；四种沙箱后端与域名代理；密钥不进入命令环境、在结果中隐藏 | [权限](docs/reference/permissions.md)、[命令沙箱](docs/reference/sandbox.md) |
| 上下文 | 以服务用量为锚的估算与来源标注（区分服务端返回、服务端 + 估算、估算）；会话期锁定 `<environment>` 日期与档位前缀以保持 KV 缓存命中；大结果存盘；压缩前先清理旧工具结果，再折叠最早的一段对话（历史不动，`/rewind` 能回到其中）；摘要压缩与熔断 | [上下文管理](docs/reference/context.md) |
| 记忆 | 用户级与项目级笔记；Git 多工作树共享仓库项目记忆；`pinned: true` 常驻笔记与项目优先淘汰；Okapi BM25 + 中文字元二元组检索（`search`、`read`、`list`、`save`、`delete`）与后台自动提取 | [记忆笔记](docs/reference/memory.md) |
| 会话 | 实时存档、恢复与关键词筛选；会话分支（`/fork`）与重命名（`/rename`）；文件与已激活 Skill 检查点及 `/rewind` 同步回退 | [会话存档与恢复](docs/reference/sessions.md) |
| 长任务 | `todo_write`、`ask_user`、`/goal`、`/loop` 会话内空闲定时巡检；后台命令、Ctrl+B 转入后台 | [任务清单](docs/reference/tools.md#任务清单)、[目标](docs/reference/commands.md#目标)、[定时巡检](docs/reference/commands.md#定时巡检loop) |
| 多 Agent | 定义式与 Fork 子 Agent（支持后台审批冒泡与撤回）、git 工作树隔离；单真源黑板（`board.jsonl`）Agent 团队、`@mention`/`#seq` 点对点接力、只读 `verifier` 角色、零 Token 停滞看门狗、`/swarm` 动态协调模式与分屏窗格 | [子 Agent](docs/reference/subagents.md)、[Agent 团队](docs/reference/teams.md) |
| 扩展 | MCP（工具、资源与模板、`@server:uri` 引用、`/mcp__…` 提示词命令、stdio `Content-Length`/JSONL 分帧自动识别）、Skill（64 KiB 激活预算、LRU 自动淘汰与按需卸载）、Hook（含 `pre_finish` 确定性验收）、插件（兼容 Claude Code 的插件格式） | [MCP](docs/reference/mcp.md)、[Skill](docs/reference/skills.md)、[Hook](docs/reference/hooks.md)、[插件](docs/reference/plugins.md) |
| IDE 接入 | Agent Client Protocol (ACP) 服务端（`moss acp`），支持 JetBrains、Zed 等编辑器多目录会话隔离与审批桥接 | [ACP 编辑器集成](docs/reference/acp.md) |
| 评估与预算 | 锁定 Harbor / Terminal-Bench 2.1 评测适配器与冻结基线、版本对照与 `run_budget` 共享额度 | [评估与版本对照](docs/reference/evaluations.md) |
| 界面 | 内联显示与原生滚动历史；流式 Markdown；审批和提问面板替换输入框；运行状态行显示最近工具的成败；浅色与深色配色 | [终端界面](docs/reference/interface.md) |
| 平台与发布 | Windows、Linux、macOS 三平台 CI；PyPI 发布；`moss doctor`、`moss setup` | [命令行](docs/reference/cli.md) |

## 接下来

| 优先级 | 状态 | 内容 | 为什么 |
| --- | --- | --- | --- |
| P2 | 待做 | **英文参考文档** | 目前只有 README 有英文版 |

## 不打算做

| 内容 | 原因 |
| --- | --- |
| 全屏（备用屏幕）渲染 | 会失去终端原生的滚动历史和文字选择，这是 MossCode 刻意保留的 |
| 常驻的工具结果清理（每轮检查、达到某个量就清理） | 每改一次历史中段，之后的提示缓存全部失效；只在本来就要压缩时清理 |
| 仅对某一种协议有效的服务端功能（如 Anthropic 的服务端上下文清理） | 三种协议的行为要保持一致 |
| 运行中切换配色 | 配色在启动时按终端背景决定，切换终端配色后重启即可 |

## 已知限制

- 路径检查是应用层限制；需要系统级隔离时打开命令沙箱，或在容器中运行。
- Windows 的旧版控制台（conhost）不回答背景色查询，浅色背景需要设 `MOSSCODE_THEME=light`。
- 团队的分屏窗格需要 Windows Terminal 或 tmux，否则成员在进程内运行。
