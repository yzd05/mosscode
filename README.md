<p align="center">
  <img src="docs/logo.svg" alt="MossCode" width="128" />
</p>

<p align="center">
  <a href="https://github.com/yzd05/mosscode/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/yzd05/mosscode/ci.yml?branch=main&style=flat-square&label=CI" alt="CI" /></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square" alt="Platform" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-brightgreen?style=flat-square" alt="MIT License" /></a>
</p>

# MossCode

中文 | [English](README.en.md)

MossCode 是运行在终端里的开源 coding agent，通过自然语言理解本地代码库、读写编辑文件、执行命令、检索代码并处理 Git 工作流。设计参考 Claude Code 与 Pi。

你可以在[终端内联界面](docs/reference/interface.md)中交互使用（保留终端原生的滚动历史与文字选择），在脚本和自动化中通过 [`-p` 无界面模式](docs/usage.md#无界面模式)运行，或通过 [ACP 协议](docs/reference/acp.md)接入 PyCharm、IntelliJ IDEA 与 Zed。

支持 Anthropic Messages、OpenAI Responses 和 Chat Completions 三种[模型协议](docs/reference/providers.md)；提供四档[权限控制](docs/reference/permissions.md)与各平台原生[命令沙箱](docs/reference/sandbox.md)，支持保护提示缓存的[上下文管理](docs/reference/context.md)、[会话存档与回退](docs/reference/sessions.md)以及跨会话[记忆笔记](docs/reference/memory.md)，并可通过 [MCP](docs/reference/mcp.md)、[Skills](docs/reference/skills.md)、[Hooks](docs/reference/hooks.md)、[子 Agent](docs/reference/subagents.md)、[工作树团队](docs/reference/teams.md)和[插件](docs/reference/plugins.md)扩展。完整说明见[使用指南](docs/usage.md)与[对照 Claude Code](docs/claude-code-patterns.md)。

## 安装

需要 Python 3.12 或更高版本。推荐使用 [uv](https://docs.astral.sh/uv/) 安装：

```bash
uv tool install git+https://github.com/yzd05/mosscode.git
```

发布版本也可从 PyPI 安装：`uv tool install mosscode`。安装后进入任意项目目录运行：

```bash
cd /path/to/project
moss
```

运行 `moss doctor` 可离线检查安装、PATH、配置与沙箱环境。升级、卸载及各平台依赖见[安装细节](docs/usage.md#安装细节)。

## 配置

首次运行 `moss` 会进入交互式配置引导（之后随时可运行 `moss setup`）。本机已配好 Claude Code 或 Codex 时，选择“跟随”即可直接复用其服务地址、模型与密钥；内置 DeepSeek、Kimi、GLM、通义千问、OpenRouter、Anthropic、OpenAI 预设，密钥默认保存在系统凭据管理器中（`moss key set`）。

手动编辑 `~/.mosscode/config.yaml` 的最小示例（全部字段见 [config.example.yaml](config.example.yaml) 与[配置说明](docs/usage.md#配置)）：

```yaml
providers:
  - name: deepseek
    preset: deepseek
    model: deepseek-chat
    api_key_env: DEEPSEEK_API_KEY
```

## 使用

命令行入口：

```bash
moss                           # 启动终端交互界面
moss "解释这个项目的入口模块"     # 启动后立即发送消息
moss -c                        # 恢复当前项目最近的会话
moss -p "运行测试并总结失败原因"  # 无界面模式（支持 text / json / stream-json）
moss acp                       # 启动 ACP 服务（stdio JSON-RPC，供 IDE 接入）
```

在交互界面中输入 `/help` 查看全部命令与快捷键：常用如 `/plan`（只读调查与计划审批）、`/model` 与 `/thinking`（切换模型和思考强度）、`/rewind`（回退文件改动与对话）、`/goal`（按完成条件持续执行）、`@文件`（附加文件内容）、`!命令`（直接运行命令）。详见[按键与命令](docs/usage.md#按键与命令)与 [ACP 编辑器集成](docs/reference/acp.md)。

## 架构

源码位于 `src/mosscode/`。`launcher.py` 负责轻量命令入口（`--version`、`--help` 与子命令不导入界面和模型 SDK）；终端界面（`tui.py`）、`-p` 与 `acp/` 共用 `Conversation`（`conversation.py`），由 `AgentLoop`（`agent.py`）配合 `providers.py` 与 `tools/` 完成每轮模型请求、权限检查、沙箱隔离与工具执行。

<details>
<summary>模块职责</summary>

| 模块 | 职责 |
| :--- | :--- |
| `launcher.py`、`cli.py`、`runtime_factory.py` | 命令行入口、参数解析与共享会话装配 |
| `conversation.py` | 会话生命周期、项目指令与上下文加载、MCP 启动与对外接口 |
| `agent.py` | Agent Loop：模型请求、工具分批并发执行、停止条件与压缩时机 |
| `context.py`、`compaction.py`、`tool_budget.py` | 上下文估算、旧工具输出清理、分段折叠、摘要压缩与大结果落盘 |
| `session.py`、`checkpoints.py` | 会话 JSONL 实时存档与恢复、文件快照与 `/rewind` 回退 |
| `memory.py`、`memory_extract.py` | 用户级与项目级记忆笔记、索引生成与后台提取 |
| `providers.py`、`catalog.py` | 三种模型协议适配、重试、提示缓存标记与模型价格目录 |
| `prompts.py` | 系统提示组装、环境信息、模式提醒与 `AGENTS.md` 加载 |
| `tools/` | 内置工具实现、参数校验与只读并发调度 |
| `permissions.py`、`security.py`、`sandbox/` | 权限档位与规则、命令风险分级、各平台命令沙箱与域名代理 |
| `mcp_client.py`、`skills/`、`hooks/`、`plugins.py` | MCP 客户端、Skill 与脚本工具、生命周期 Hook、插件管理 |
| `agents/`、`worktree/`、`teams/` | 子 Agent 运行时、Git 工作树管理、分屏团队通信与分支合并 |
| `tui.py`、`tui_views.py`、`windows_inline.py`、`scrollback.py` | 内联终端界面、组件视图、Windows 控制台适配、流式 Markdown 与原生滚屏 |
| `acp/` | ACP 编辑器服务端，多目录会话隔离与权限桥接 |

</details>

从仓库源码运行与测试：

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python -m pytest -q
ruff check && ruff format --check
```

更多说明见[参与贡献](CONTRIBUTING.md)、[AGENTS.md](AGENTS.md)、[评估与版本对照](docs/reference/evaluations.md)、[路线图](ROADMAP.md)、[更新日志](CHANGELOG.md)与[安全策略](SECURITY.md)。

[MIT](LICENSE)
