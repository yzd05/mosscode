# 更新日志

这里记录每个版本的用户可见变化。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)；1.0 之前，次版本号的变化可能包含不兼容的改动，会在条目中注明。

## [Unreleased]

### 新增

- `web_fetch` 读取网页：HTML 转为 Markdown，长页面按 `offset` 分段，同一会话缓存 15 分钟；拒绝本机和内网地址，连接固定到检查过的 IP；跨主机重定向交给模型决定；支持 `HTTPS_PROXY`。
- `web_search` 网络搜索，首先支持 Tavily，在 `config.yaml` 的 `web_search` 中配置；每个会话最多 100 次。
- 权限规则 `WebFetch(domain:…)` 和 `WebSearch`，审批时可按域名授权本会话或永久允许；`moss doctor` 和 `/status` 显示网络搜索的配置状态。
- 图片输入：`read_file` 可以查看图片（PNG、JPEG、GIF、WebP、BMP），MCP 工具返回的图片也会发给模型；图片自动转正、缩放和压缩，保存在会话目录的 `images/` 中，会话记录只保存引用。Provider 新增 `images` 配置，不接受图片的模型收到文字说明。新增依赖 Pillow。
- 粘贴和拖入图片：Alt+V（可用时也包括 Ctrl+V）附加剪贴板中的截图或复制的图片文件，拖入或粘贴图片路径也会附加；输入框中显示为 `[图片 #N]`，删掉占位符即取消；排队消息和调出的历史消息保留图片；`/status` 显示当前模型能否接收图片。
- 模型目录：随程序附带 LiteLLM 的模型数据，`context_window`、`max_output_tokens`（最多 32,000）和 `images` 未配置时按模型自动取值，不再一律按 128K 估算；模型名是别名时用新配置项 `catalog_model` 指定条目。新增 `moss models [关键词]` 查找条目、`moss models update` 下载最新数据；`/status` 和 `-p` 的 json 结果（`total_cost_usd`）显示按目录价格估算的费用；`moss setup` 和 `moss doctor` 显示目录的匹配结果。
- 内置 Skill `/init`：分析项目后生成或补充 `AGENTS.md`，写入前显示改动并按档位审批。
- 检查点与 `/rewind`（别名 `/checkpoint`）：`write_file`、`edit_file` 改文件前自动保存快照（包括主工作区中的子 Agent），可以把文件、对话或两者退回到之前某条消息发出前的状态，回退后的消息放回输入框；空闲且输入框为空时连按两次 Esc 打开。检查点随会话存档保存，恢复会话后仍可回退。
- Skill 另外从 [Agent Skills](https://agentskills.io/specification) 的共享目录 `~/.agents/skills/`、`.agents/skills/` 读取；按规范检查 `SKILL.md`，不认识的字段只提示；`/skills <名称>` 显示 `license`、`compatibility`；白名单中的 `WebFetch`、`WebSearch` 换算为 `web_fetch`、`web_search`。

### 变更

- 团队文件改用操作系统锁（`filelock`）：持有锁的进程崩溃后锁立即释放，不再要等 10 秒过期。
- 命令的灾难级和高风险特征也按 bash 语法（tree-sitter）拆出的子命令检查，引号、转义、`$(…)` 和子 shell 不再能绕过；显式 deny 规则同样按子命令匹配。
- Skill 名改按 Agent Skills 规范：可以以数字开头；以 `-` 结尾或含 `--` 的名称不再接受（不兼容）。
- 状态栏不再显示 Token 数、缓存读写数和模型名（`/model` 切换后仍显示新模型），改为“上下文 约 9K / 1M（1%）”和会话累计的“缓存命中 N%”；Token 数和费用见 `/status`。
- Skill 不符合 Agent Skills 规范的写法（如不认识的字段）启动时不再提示，改在 `/skills` 中列出。
- 没有 ripgrep 时，内置搜索用 `pathspec` 匹配 `.gitignore`，转义（如 `\[draft\].md`）和行尾空格等写法与 git 一致。

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
