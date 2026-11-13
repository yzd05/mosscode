# 路线图

MossCode 已经做了什么、接下来做什么，以及为什么这样排。每项的完整设计在 `docs/specs/` 中，已发布的变化见 [更新日志](CHANGELOG.md)。

状态：**已实现**（已在 `main` 或开发分支上，有测试）、**进行中**、**待做**。优先级：**P0** 影响日常使用；**P1** 明显改善长任务或可维护性；**P2** 可选，按需要再做。

## 已实现

| 方面 | 内容 | 文档 |
| --- | --- | --- |
| Agent Loop | 单循环、明确的停止条件、空回复和思考截断后自动重发、正文截断后自动续写 | [工具与 Agent Loop](docs/reference/tools.md) |
| 模型协议 | Anthropic Messages、OpenAI Responses、Chat Completions；流式；提示缓存；思考强度；图片输入；随程序发布的模型目录与费用估算 | [模型提供商](docs/reference/providers.md) |
| 工具 | 读、写、改、命令、glob、grep，任务清单、向用户提问、记忆、网页读取与搜索；先读后改；只读调用并发 | [工具](docs/reference/tools.md#工具) |
| 安全 | 四个权限档位与分层规则；命令风险分级；四种沙箱后端与域名代理；密钥不进入命令环境、在结果中隐藏 | [权限](docs/reference/permissions.md)、[命令沙箱](docs/reference/sandbox.md) |
| 上下文 | 以服务用量为锚的估算；大结果存盘；压缩前先清理旧工具结果，再折叠最早的一段对话（历史不动，`/rewind` 能回到其中）；摘要压缩与熔断 | [上下文管理](docs/reference/context.md) |
| 会话 | 实时存档与恢复；检查点与 `/rewind` | [会话存档与恢复](docs/reference/sessions.md) |
| 长任务 | `todo_write`、`ask_user`、`/goal`；后台命令、Ctrl+B 转入后台 | [任务清单](docs/reference/tools.md#任务清单)、[目标](docs/reference/commands.md#目标) |
| 多 Agent | 定义式与 Fork 子 Agent、后台运行、git 工作树隔离、Agent 团队与分屏窗格 | [子 Agent](docs/reference/subagents.md)、[Agent 团队](docs/reference/teams.md) |
| 扩展 | MCP、Skill、Hook、插件（兼容 Claude Code 的插件格式） | [Skill](docs/reference/skills.md)、[Hook](docs/reference/hooks.md)、[插件](docs/reference/plugins.md) |
| 界面 | 内联显示与原生滚动历史；流式 Markdown；审批和提问面板替换输入框；运行状态行显示最近工具的成败；浅色与深色配色 | [终端界面](docs/reference/interface.md) |
| 平台与发布 | Windows、Linux、macOS 三平台 CI；PyPI 发布；`moss doctor`、`moss setup` | [命令行](docs/reference/cli.md) |

## 接下来

| 优先级 | 状态 | 内容 | 为什么 |
| --- | --- | --- | --- |
| P1 | 待做 | **MCP 资源和提示词**：`resources/list`、`resources/read`、`prompts/list`、`prompts/get` | 现在只用了 MCP 的工具，部分 Server 的主要内容是资源 |
| P2 | 待做 | **MCP stdio 分帧自动识别**：兼容用 `Content-Length` 分帧的旧式 Server | 只影响少数不规范的 Server |
| P2 | 待做 | **`/loop`**：每隔一段时间重发同一个提示词 | 轮询 CI、PR 评论一类任务；`/goal` 已覆盖“做到完成为止” |
| P2 | 待做 | **会话分叉与重命名**：`/fork`、`/rename` | 已有 `/rewind`，必要性不高 |
| P2 | 待做 | **上下文数字的来源标注**：区分服务返回、估算或两者相加 | 状态栏已用“约”表示含估算 |
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
