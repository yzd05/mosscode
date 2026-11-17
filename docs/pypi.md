# MossCode

运行在终端与编辑器里的 AI coding agent：读写与批量修改文件、运行命令、搜索代码，按权限档位与系统沙箱约束执行。支持 Anthropic Messages、OpenAI Responses 和 Chat Completions 三种协议，可接入 MCP 工具、Skill、生命周期 Hook、子 Agent、Git 工作树与 Agent 团队。

## 安装

需要 Python 3.12 或更高版本。推荐用 [uv](https://docs.astral.sh/uv/) 或 [pipx](https://pipx.pypa.io/) 安装成独立的命令：

```
uv tool install mosscode
```

或

```
pipx install mosscode
```

升级用 `uv tool upgrade mosscode`（或 `pipx upgrade mosscode`）。安装后得到 `moss` 和 `mosscode` 两个相同的命令。

## 第一次运行

```
moss
```

找不到配置时，`moss` 进入配置引导：可直接跟随本机已配好的 Claude Code 或 Codex 设置，或从 DeepSeek、Kimi、GLM、通义千问、OpenRouter、Anthropic、OpenAI 预设中选择，写入 `~/.mosscode/config.yaml` 后进入界面；之后可用 `moss setup --add` 追加更多 Provider。凭据默认保存在系统凭据管理器中（`moss key set`），也可以放在环境变量中。`moss doctor` 离线检查安装、配置和依赖工具，加 `--ping` 可向模型发送最小探针验证连通性。

```
moss "解释这个项目的入口"          # 启动后立即发送
moss -c                            # 恢复本项目最近的会话
moss -p "列出 src 下的模块"        # 无界面模式，运行一个任务后退出
moss --sandbox                     # 开启原生命令沙箱运行
moss acp                           # 启动 ACP 服务，接入 Zed、JetBrains IDE 等编辑器
```

## 功能

- 本地工具与编辑容错：读、写、支持多处原子替换与缩进/行尾容错的 `edit_file`（附语法诊断）、命令、glob、grep；相邻只读调用并发执行，运行中排队新消息自动跳过未开始的批次优先响应。
- 权限与原生命令沙箱：四个权限档位与三层规则、危险命令 AST 级风险分级确认、只读计划模式；支持系统级命令沙箱（Windows MXC / 受限令牌、Linux bubblewrap、macOS Seatbelt）与按域名授权的回环网络代理。
- 编辑器与扩展生态：内置 Agent Client Protocol (`moss acp`) 服务端；支持 MCP 外部工具、兼容 Agent Skills 与 Claude Code 插件格式的 Skill / 插件系统、生命周期与完成前验收 Hook（`pre_finish`）。
- 长任务与多 Agent：任务清单 `todo_write`、交互式提问 `ask_user`、自主目标循环 `/goal`、前台超时自动转后台命令、子 Agent、Git 工作树隔离和分屏 Agent 团队。
- 上下文与会话管理：以服务端用量为锚的上下文估算、工具结果清理、旧对话折叠与自动压缩；会话实时存档、关键词恢复（`/resume`）、分支派生（`/fork`）、自定义重命名（`/rename`）、检查点回退（`/rewind`）与跨会话记忆笔记。
- 终端内联体验：保留终端原生滚动历史与文本选择，`@` 引用文件、`!` 直接执行命令、剪贴板/拖拽图片输入，`Ctrl+O` 随时展开思考过程与完整工具输出。

支持 Windows、Linux 和 macOS。搜索优先使用 PATH 中的 [ripgrep](https://github.com/BurntSushi/ripgrep)，没有时退回内置实现。

## 文档

- [项目主页与完整说明](https://github.com/yzd05/mosscode)
- [使用指南](https://github.com/yzd05/mosscode/blob/main/docs/usage.md)
- [参考文档](https://github.com/yzd05/mosscode/tree/main/docs/reference)
- [更新日志](https://github.com/yzd05/mosscode/blob/main/CHANGELOG.md)
- [问题反馈](https://github.com/yzd05/mosscode/issues)

MIT 许可证。
