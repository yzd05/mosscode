# MossCode

面向终端与编辑器的开源 AI Coding Agent：理解本地代码库、导航与修改文件、运行命令、检索代码并驱动完整的开发测试流程。支持 Anthropic Messages、OpenAI Responses 和 Chat Completions 三种协议，可接入完整 MCP、Skills、Hooks、子 Agent 与 Git 工作树团队。

## 安装

需要 Python 3.12 或更高版本。推荐用 [uv](https://docs.astral.sh/uv/) 或 [pipx](https://pipx.pypa.io/) 安装：

```bash
uv tool install mosscode
```

或

```bash
pipx install mosscode
```

升级使用 `uv tool upgrade mosscode`（或 `pipx upgrade mosscode`）。安装后提供 `moss` 和 `mosscode` 两个等价命令。

## 快速开始

```bash
moss                               # 启动终端交互界面（首次运行进入配置引导）
moss "梳理这个项目的核心模块"        # 启动并立即发送任务
moss -c                            # 恢复当前目录最近的会话
moss --sandbox                     # 开启系统级命令沙箱运行
moss -p "运行测试并总结失败原因"     # 无界面模式（支持 text / json / stream-json）
moss acp                           # 启动 ACP 服务，接入 PyCharm / IntelliJ / Zed
```

首次运行可直接跟随本机已有的 Claude Code / Codex 配置，或选用主流模型预设（DeepSeek、Kimi、GLM、通义千问、OpenRouter、Anthropic、OpenAI），可用 `moss setup --add` 追加配置，`moss doctor --ping` 检查环境与模型连通性。

## 核心功能

- **三端统一接入**：保留终端原生滚屏与文字选择的内联界面、脚本与 CI 无界面模式（`-p`），以及编辑器 ACP 服务端（`moss acp`）。
- **代码结构导航与可靠编辑**：支持全仓代码结构地图（`repo_map`）、文件符号大纲与按符号读取、多处原子替换与语法检查、全文检索、`@文件` 引用、图片输入与 `!命令` 直跑。
- **长程任务与多 Agent 协作**：提供只读规划（`/plan`）、目标驱动执行（`/goal`）、定时巡检（`/loop`）、后台命令与任务管理、子 Agent 及独立 Git 工作树团队协作（`/swarm`）。
- **会话管理与长期记忆**：支持长对话自动上下文减压、会话分支（`/fork`）、检查点文件与对话回退（`/rewind`），以及跨工作树共享的用户/项目记忆检索。
- **开放扩展与安全边界**：支持完整 MCP（工具、资源、`@server:uri` 与提示词命令）、Skills、生命周期 Hooks 与插件管理；内置四档权限、分层规则、高危命令拦截及 Windows / Linux / macOS 原生命令沙箱。

## 文档

- [项目主页与完整说明](https://github.com/yzd05/mosscode)
- [使用指南](https://github.com/yzd05/mosscode/blob/main/docs/usage.md)
- [参考文档](https://github.com/yzd05/mosscode/tree/main/docs/reference)
- [更新日志](https://github.com/yzd05/mosscode/blob/main/CHANGELOG.md)
- [问题反馈](https://github.com/yzd05/mosscode/issues)

MIT 许可证。
