# 更新日志

这里记录每个版本的用户可见变化。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)；1.0 之前，次版本号的变化可能包含不兼容的改动，会在条目中注明。

## [Unreleased]

### 新增

- `web_fetch` 读取网页：HTML 转为 Markdown，长页面按 `offset` 分段，同一会话缓存 15 分钟；拒绝本机和内网地址，连接固定到检查过的 IP；跨主机重定向交给模型决定；支持 `HTTPS_PROXY`。
- `web_search` 网络搜索，首先支持 Tavily，在 `config.yaml` 的 `web_search` 中配置；每个会话最多 100 次。
- 权限规则 `WebFetch(domain:…)` 和 `WebSearch`，审批时可按域名授权本会话或永久允许；`moss doctor` 和 `/status` 显示网络搜索的配置状态。

### 变更

- 团队文件改用操作系统锁（`filelock`）：持有锁的进程崩溃后锁立即释放，不再要等 10 秒过期。

## [0.1.0] - 2026-11-03

首个公开版本。

### 新增

- 终端交互界面（Textual 内联模式，完成的内容进入终端原生滚动历史）和无界面模式 `-p`，输出格式 `text`、`json`、`stream-json`。
- 三种模型协议：Anthropic Messages、OpenAI Responses、Chat Completions；多 Provider 切换，首个事件前自动重试，缓存断点。
- 首次运行的配置引导 `moss setup` 和诊断命令 `moss doctor`。
- 本地工具：读、写、编辑、命令、glob、grep，只读的计划模式；相邻的只读调用并发执行，大结果存盘。
- 四个权限档位（default、acceptEdits、plan、bypassPermissions），用户、项目和本机三层规则，命令风险分级和项目路径检查。
- MCP 外部工具：stdio 与 HTTP 传输，后台连接、工具发现和变更订阅。
- 上下文估算与自动压缩、会话实时存档与恢复（`-c`、`--resume`、`/resume`）。
- 记忆笔记：`memory` 工具、后台提取、用户级与项目级索引。
- 项目指令 `AGENTS.md`（兼容 `CLAUDE.md`），支持 `@路径` 引用。
- Skill：`SKILL.md` 规程与脚本工具，内置 `/commit`、`/review`、`/test`。
- Hook：会话、轮次、请求和工具调用前后的命令、提示注入、HTTP 和 Agent 动作，项目级 Hook 需确认后启用。
- 子 Agent：内置 explore、plan、general，自定义定义，Fork 方式，后台任务与通知。
- Git 工作树隔离：创建、初始化（链接与复制）、改动检查、安全删除和过期清理。
- Agent 团队：共享任务、成员邮箱、各自的工作树、Windows Terminal 分屏或进程内运行、确认后逐个合并。

[Unreleased]: https://github.com/yzd11/mosscode/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yzd11/mosscode/releases/tag/v0.1.0
