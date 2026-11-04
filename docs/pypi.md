# MossCode

终端里的 coding agent：读写文件、运行命令、搜索代码，按权限档位请你确认后执行。支持 Anthropic Messages、OpenAI Responses 和 Chat Completions 三种协议，可以接入 MCP 工具、Skill、Hook、子 Agent 和 Agent 团队。

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

找不到配置时，`moss` 在终端里逐项询问协议、服务地址、模型和凭据，写入 `~/.mosscode/config.yaml` 后进入界面。凭据推荐保存在系统凭据管理器中（`moss key set`），也可以放在环境变量中，配置里只写名称。`moss doctor` 检查安装、配置和依赖工具，不发送请求。

```
moss "解释这个项目的入口"          # 启动后立即发送
moss -c                            # 恢复本项目最近的会话
moss -p "列出 src 下的模块"        # 无界面模式，运行一个任务后退出
```

## 功能

- 本地工具：读、写、编辑、命令、glob、grep；相邻的只读调用并发执行。
- 四个权限档位与分层规则，危险命令分级确认，只读的计划模式。
- MCP 外部工具、Skill、生命周期 Hook。
- 子 Agent、后台任务、git 工作树隔离和 Agent 团队。
- 上下文自动压缩、会话存档与恢复、记忆笔记、项目指令 `AGENTS.md`。
- 网页读取与搜索、图片输入；输入框中 `@` 引用文件、`!` 直接执行命令，检查点与 `/rewind` 退回改动。

支持 Windows、Linux 和 macOS。搜索优先使用 PATH 中的 [ripgrep](https://github.com/BurntSushi/ripgrep)，没有时退回内置实现。

## 文档

- [项目主页与完整说明](https://github.com/yzd11/mosscode)
- [参考文档](https://github.com/yzd11/mosscode/tree/main/docs/reference)
- [更新日志](https://github.com/yzd11/mosscode/blob/main/CHANGELOG.md)
- [问题反馈](https://github.com/yzd11/mosscode/issues)

MIT 许可证。
