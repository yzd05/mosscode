<p align="center">
  <img src="docs/logo.svg" alt="MossCode" width="128" />
</p>

<p align="center">
  <a href="https://pypi.org/project/mosscode/"><img src="https://img.shields.io/pypi/v/mosscode?style=flat-square&label=PyPI&color=006dad" alt="PyPI" /></a>
  <a href="https://github.com/yzd05/mosscode/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/yzd05/mosscode/ci.yml?branch=main&style=flat-square&label=CI" alt="CI" /></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square" alt="Platform" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-brightgreen?style=flat-square" alt="MIT License" /></a>
</p>

# MossCode

中文 | [English](README.en.md)

MossCode 是面向终端与编辑器的开源 AI Coding Agent，通过自然语言理解本地代码库、检索与编辑代码、执行命令并处理完整的开发与测试工作流。

- **三端统一体验**：支持保留原生滚动历史与文字选择的[终端内联界面](docs/reference/interface.md)、面向脚本和自动化的 [`-p` 无界面模式](docs/usage.md#无界面模式)，以及接入 PyCharm、IntelliJ IDEA 与 Zed 的 [ACP 编辑器服务](docs/reference/acp.md)。
- **代码导航与精准编辑**：提供全仓代码结构地图（`repo_map`）、按符号大纲或函数名读取、多处原子修改与语法检查、全文搜索，支持 `@文件` 引用、图片输入与 `!命令` 直跑（见[工具说明](docs/reference/tools.md)）。
- **长程执行与多 Agent 协作**：支持只读调研与计划审批（`/plan`）、按目标条件持续推进（`/goal`）、定时巡检（`/loop`）、后台任务，以及[子 Agent](docs/reference/subagents.md) 与基于独立 Git 工作树的 [`/swarm` 团队协作](docs/reference/teams.md)。
- **会话回退与长期记忆**：提供长对话自动上下文管理、[会话分支与检查点回退](docs/reference/sessions.md)（`/fork`、`/rewind`），以及跨会话、跨工作树共享的[记忆检索与管理](docs/reference/memory.md)。
- **多模型协议与开放扩展**：兼容 Anthropic Messages、OpenAI Responses 与 Chat Completions 三种[模型协议](docs/reference/providers.md)；支持完整 [MCP](docs/reference/mcp.md)（工具、资源、`@server:uri` 与提示词命令）、[Skills](docs/reference/skills.md)、[Hooks](docs/reference/hooks.md) 与[插件](docs/reference/plugins.md)。
- **分级权限与系统沙箱**：内置四档[权限控制](docs/reference/permissions.md)、分层规则与高危命令拦截，支持 Windows、Linux、macOS 原生[命令沙箱](docs/reference/sandbox.md)与网络域名管控。

完整说明见[使用指南](docs/usage.md)。

## 安装

需要 Python 3.12 或更高版本。推荐使用 [uv](https://docs.astral.sh/uv/) 从 PyPI 安装：

```bash
uv tool install mosscode
```

也可直接从仓库安装最新开发版：`uv tool install git+https://github.com/yzd05/mosscode.git`。进入任意项目目录即可启动：

```bash
cd /path/to/project
moss
```

运行 `moss doctor`（或 `moss doctor --ping`）可检查安装环境、配置与模型连通性。更多安装与升级方式见[安装细节](docs/usage.md#安装细节)。

## 配置

首次运行 `moss` 会启动交互式配置向导（后续可用 `moss setup --add` 追加配置）：可直接跟随复用本机已有的 Claude Code / Codex 配置，或选用 DeepSeek、Kimi、GLM、通义千问、OpenRouter、Anthropic、OpenAI 预设，密钥默认存入系统凭据管理器（`moss key set`）。

手动编写 `~/.mosscode/config.yaml` 的最小示例（完整字段见 [config.example.yaml](config.example.yaml) 与[配置说明](docs/usage.md#配置)）：

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
moss "梳理这个项目的核心模块"     # 启动并立即发送任务
moss -c                        # 恢复当前目录最近的会话
moss --sandbox                 # 开启系统级命令沙箱运行
moss -p "运行测试并总结失败原因"  # 无界面模式（支持 text / json / stream-json）
moss acp                       # 启动 ACP 服务（供 JetBrains IDE / Zed 接入）
```

交互界面中输入 `/help` 查看全部命令与快捷键，常用包括：`/plan`（只读规划）、`/swarm`（多 Agent 协作）、`/goal`（按条件持续执行）、`/loop`（定时巡检）、`/rewind`（回退文件与对话）、`/model` 与 `/thinking`（切换模型与思考强度）、`@文件`（附加文件或 MCP 资源）、`!命令`（直接执行终端命令）。详见[按键与命令](docs/usage.md#按键与命令)。

## 架构

源码位于 `src/mosscode/`。终端交互界面（`tui.py`）、无界面模式（`cli.py`）与编辑器服务（`acp/`）共用同一套会话与循环核心（`conversation.py`、`agent.py`），统一调度模型协议层（`providers.py`）、工具与代码索引（`tools/`、`repomap.py`）、权限与系统沙箱（`permissions.py`、`sandbox/`）、扩展体系（`mcp_client.py`、`skills/`、`hooks/`）以及子 Agent 与工作树团队（`agents/`、`teams/`、`worktree/`）。

从源码运行与测试：

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python -m pytest -q
ruff check && ruff format --check
```

更多说明见[参与贡献](CONTRIBUTING.md)、[AGENTS.md](AGENTS.md)、[评估与版本对照](docs/reference/evaluations.md)、[路线图](ROADMAP.md)、[更新日志](CHANGELOG.md)与[安全策略](SECURITY.md)。

[MIT](LICENSE)
