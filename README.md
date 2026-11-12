<p align="center">
  <img src="docs/logo.svg" alt="MossCode" width="160" />
</p>

<h1 align="center">MossCode</h1>

<p align="center">
  <a href="https://pypi.org/project/mosscode/"><img src="https://img.shields.io/pypi/v/mosscode?style=for-the-badge&label=PyPI&color=5b7cfa&labelColor=3b3f55" alt="PyPI" /></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-7aa2ff?style=for-the-badge&logo=python&logoColor=white&labelColor=3b3f55" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/Windows%20%C2%B7%20Linux%20%C2%B7%20macOS-a9c1ff?style=for-the-badge&labelColor=3b3f55" alt="Windows · Linux · macOS" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e0e7ff?style=for-the-badge&labelColor=3b3f55" alt="MIT" /></a>
</p>

<p align="center">
  终端里的 coding agent。学习 Claude Code 的设计，权限可控，适合长任务。
</p>

---

中文 | [English](README.en.md) | [使用指南](docs/usage.md) | [参考文档](docs/reference/cli.md) | [对照 Claude Code](docs/claude-code-patterns.md) | [路线图](ROADMAP.md) | [更新日志](CHANGELOG.md) | [参与贡献](CONTRIBUTING.md)

MossCode 是在终端里工作的编码助手，用 Python 写成。设计上参考 Claude Code 与 pi：一个 Agent Loop 加本地工具和 MCP 工具，四档权限与只读的计划模式，支持 Anthropic Messages、OpenAI Responses 和 Chat Completions 三种协议。

### 核心能力

- **Agent Loop**：一轮中多步调用工具，相邻的只读调用并发；空回复和思考截断后自动重发，正文截断后自动续写。
- **三种协议**：流式输出、提示缓存、思考强度、图片输入；随程序发布的模型目录提供上下文窗口和费用估算。
- **工具**：读、写、改、命令、glob、grep，网页读取与搜索，`todo_write` 任务清单，`ask_user` 向你提问；先读后改，长命令可以转入后台。
- **权限与沙箱**：四个权限档位与分层规则，命令按风险分级；Linux、macOS、Windows 都有命令沙箱，联网经域名代理；密钥不进入命令环境。
- **长任务**：`/goal` 设定完成条件后自动续跑；大结果存盘，压缩前先清理旧工具结果、再折叠最早的一段对话，摘要压缩带熔断。
- **会话与记忆**：实时存档，`-c`、`/resume` 恢复，`/rewind` 把文件和对话退回到之前任一条消息；记忆笔记跨会话保留。
- **多 Agent**：定义式与 Fork 子 Agent、后台运行、git 工作树隔离；Agent 团队在 Windows Terminal 或 tmux 的分屏中并行工作。
- **扩展**：MCP、Skill、Hook 和插件，插件兼容 Claude Code 的格式。
- **界面**：内联显示，保留终端原生的滚动历史和文字选择；流式 Markdown；审批和提问面板出现在输入框的位置；自动适配浅色和深色终端。

## 安装

需要 Python 3.12 或更高版本。推荐用 [uv](https://docs.astral.sh/uv/) 安装成独立的命令：

```bash
uv tool install mosscode
moss doctor     # 检查安装、PATH、配置和依赖工具，不发送请求
```

安装后得到 `moss` 和 `mosscode` 两个相同的命令。PATH 提示、升级与卸载、各平台的说明、容器和命令沙箱见 [安装细节](docs/usage.md#安装细节)；从源码安装见 [参与贡献](CONTRIBUTING.md)。

## 配置

第一次运行 `moss` 时，会在终端中逐项询问协议、服务地址、模型、上下文窗口和凭据，写入 `~/.mosscode/config.yaml` 后直接进入界面；之后随时可以运行 `moss setup`。凭据默认保存在系统凭据管理器中，配置只写名称。

手动编辑时，一个最小的 Provider 是这样的，全部字段见 [config.example.yaml](config.example.yaml)：

```yaml
providers:
  - name: claude
    protocol: anthropic-messages      # 或 openai-responses、openai-chat-completions
    model: replace-with-service-model
    api_key_env: ANTHROPIC_API_KEY    # 环境变量名；或 api_key_keyring（moss key set 保存的名称）
```

项目级配置、常用选项，以及权限、MCP、Skill、Hook、子 Agent 和插件的配置文件见 [使用指南](docs/usage.md#配置)。

## 使用

### 快速开始

```bash
moss                         # 在当前目录启动交互界面
moss "解释这个项目的入口"      # 启动后立即发送这条消息
moss -c                      # 恢复本项目最近的会话
moss -p "列出 src 下的模块"    # 无界面模式：运行一个任务后退出
```

用自然语言描述任务，模型自行选择工具；需要写文件或运行命令时，按权限档位请你确认。

### 常用入口

- `/help`：命令与快捷键。
- `/model`、`/thinking`：切换模型和思考强度。
- `/plan 任务`：只读调查并提交计划，批准后执行；Shift+Tab 切换权限档位。
- `/goal 条件`：设定完成条件，每轮结束自动判断，未达成就继续。
- `/compact`、`/clear`：压缩上下文；开始新会话。
- `/resume`、`/rewind`：恢复会话；把文件和对话退回到之前某条消息。
- `/status`：模型、权限档位、上下文、用量和估算费用。
- `/mcp`、`/skills`、`/hooks`、`/agents`、`/plugins`：查看扩展。
- `/tasks`、`/team`：后台任务与 Agent 团队。
- `@文件` 附上文件内容，`!命令` 不经过模型直接执行，Esc 取消正在运行的任务。

全部按键和命令、权限档位、网络工具与无界面模式见 [使用指南](docs/usage.md#按键与命令)。

## 架构

```
cli.py ──► tui.py（Textual 界面）或 -p（无界面）
             └──► conversation.py（启动上下文与 MCP、对外接口）
                    └──► agent.py（Agent Loop）
                           ├──► compaction.py、context.py（上下文估算与压缩）
                           ├──► session.py（会话存档与恢复）、memory*.py（记忆笔记）
                           ├──► providers.py（三种协议、重试、缓存标记）
                           ├──► prompts.py（系统提示、环境补充、模式提醒）
                           └──► tools/（注册、校验、授权、执行）
                                  ├──► permissions.py、security.py（档位、规则、命令风险）
                                  ├──► mcp_client.py、mcp_config.py（MCP）
                                  ├──► skills/、tools/skill.py（Skill 与专属工具）
                                  ├──► hooks/（生命周期 Hook、工具调用拦截）
                                  └──► agents/、tools/agent.py（子 Agent 与后台任务）
                                         ├──► worktree/（git 工作树隔离）
                                         └──► teams/、tools/team.py（Agent 团队）
```

一次任务：Agent Loop 用“稳定系统提示 + 历史 + 本次环境补充”请求模型，把流式事件交给界面；回复中的工具调用经校验和授权后执行（相邻只读调用最多 4 个并发），纯文本结果写回历史，再请求模型，直到回复不再调用工具或遇到停止条件（取消、轮数上限、连续未知工具等）。TUI 与 `-p` 共用 `Conversation`；在 Python 中可连续调用 `Conversation.ask()` 保持多轮历史，结束后 `await conversation.close()`。

<details>
<summary>各模块的职责</summary>

| 模块 | 职责 |
| --- | --- |
| `agent.py` | Agent Loop：请求、工具执行、排队消息、停止条件、计划模式、压缩时机 |
| `context.py`、`compaction.py`、`tool_budget.py` | 上下文估算与阈值、摘要压缩、大结果存盘 |
| `session.py`、`serialization.py`、`paths.py` | 会话存档、重放与修复、锁、清理；项目键 |
| `memory.py`、`memory_extract.py`、`tools/memory.py` | 记忆笔记的存储、索引与校验，后台提取，memory 工具 |
| `providers.py` | Anthropic Messages、OpenAI Responses、Chat Completions 适配，首事件前重试，缓存断点 |
| `prompts.py` | 七个固定模块、工具清单、`<environment>` 与 `<system-reminder>` 补充、AGENTS.md、git 快照 |
| `tools/` | `files.py`、`search.py`、`process.py`、`plan.py` 等工具；`execution.py` 分批并发；`base.py` 注册与执行边界 |
| `permissions.py`、`security.py` | 档位、分层规则、审批请求、只读命令白名单、危险命令分级、项目路径检查 |
| `mcp_client.py`、`mcp_config.py` | MCP 配置、后台连接与发现、工具适配 |
| `skills/`、`tools/skill.py` | Skill 扫描与覆盖、目录与 `<active-skills>`、`load_skill` 和专属脚本工具；`skills/builtin/` 为内置 Skill |
| `hooks/` | Hook 文件读取与信任、条件、四种动作、按事件执行与日志；接入工具执行、Agent Loop 和会话 |
| `agents/`、`tools/agent.py` | 子 Agent 定义的加载与覆盖、定义式与 Fork 的运行时、后台任务管理与通知；`agent`、`task_output`、`task_stop` 工具；`agents/builtin/` 为内置定义 |
| `worktree/` | git 工作树的命名、创建与初始化（链接、复制）、改动检查、安全删除、元数据与自动清理 |
| `teams/`、`tools/team.py`、`coordinator.py` | Agent 团队：存储与锁、邮箱、共享任务、成员的一轮、Lead 侧运行时、合并、窗格后端与成员进程；团队工具；协调者模式 |
| `tui.py`、`appearance.py`、`scrollback.py` | 界面、命令与提示、原生滚动历史 |
| `launcher.py`、`cli.py` | 命令入口：参数、`--version` 和子命令不加载界面与 SDK；会话由 `cli.run` 启动 |
| `config.py`、`setup.py`、`doctor.py` | 配置加载、配置引导（`moss setup`）与诊断（`moss doctor`） |
| `credentials.py`、`key_command.py` | 系统凭据管理器（`moss key`），子进程环境与输出中的密钥隐藏 |
| `plugins.py`、`plugin_command.py` | 插件的记录、读取与变量替换，交给 Skill、子 Agent、MCP、Hook 的加载函数；`moss plugin` 的安装、marketplace、更新与删除 |

</details>

### 文档

- [使用指南](docs/usage.md)：安装细节、配置项、按键、命令、权限档位、无界面模式
- 参考文档，每项行为的完整说明：[命令行](docs/reference/cli.md)、[界面](docs/reference/interface.md)、[命令](docs/reference/commands.md)、[工具与 Agent Loop](docs/reference/tools.md)、[权限](docs/reference/permissions.md)、[命令沙箱](docs/reference/sandbox.md)、[在容器中运行](docs/reference/container.md)、[MCP](docs/reference/mcp.md)、[模型提供商](docs/reference/providers.md)、[系统提示与缓存](docs/reference/prompts.md)、[上下文管理](docs/reference/context.md)、[会话存档与恢复](docs/reference/sessions.md)、[记忆笔记](docs/reference/memory.md)、[Skill](docs/reference/skills.md)、[Hook](docs/reference/hooks.md)、[子 Agent](docs/reference/subagents.md)、[插件](docs/reference/plugins.md)、[Git 工作树](docs/reference/worktree.md)、[Agent 团队](docs/reference/teams.md)
- [对照 Claude Code](docs/claude-code-patterns.md)：逐项对照 Claude Code 的设计和 MossCode 的取舍
- [路线图](ROADMAP.md)：已完成和接下来要做的
- [更新日志](CHANGELOG.md)：每个版本的变化
- [参与贡献](CONTRIBUTING.md)、[开发说明](AGENTS.md)、[安全策略](SECURITY.md)

### 开发

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python -m pytest -q
ruff check && ruff format --check
```

MossCode 以 [MIT 许可证](LICENSE) 发布。
