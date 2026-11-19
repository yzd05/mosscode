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

[中文](README.md) | English

MossCode is an open-source AI coding agent for terminals and IDEs. It understands local repositories, navigates and edits code, runs commands, and drives end-to-end development and testing workflows through natural language.

- **Three Unified Interfaces**: Work interactively in the [inline terminal UI](docs/reference/interface.md) (preserving native scrollback and text selection), run scripts and CI pipelines in [`-p` headless mode](docs/usage.en.md#headless-mode), or connect PyCharm, IntelliJ IDEA, and Zed via the [ACP server](docs/reference/acp.md).
- **Code Navigation & Reliable Editing**: Repository-wide structural code maps (`repo_map`), symbol outline and per-symbol reading, atomic multi-edit with syntax diagnostics, fast search, `@file` mentions, image input, and `!cmd` shell shortcuts (see [tools](docs/reference/tools.md)).
- **Long-Horizon Tasks & Multi-Agent Workflows**: Read-only planning (`/plan`), goal-driven execution (`/goal`), recurring checks (`/loop`), background tasks, [sub-agents](docs/reference/subagents.md), and Git-worktree [`/swarm` collaboration](docs/reference/teams.md).
- **Session Rollback & Persistent Memory**: Automatic context management for long sessions, [session branching and file/chat rollback](docs/reference/sessions.md) (`/fork`, `/rewind`), and searchable user/project [memory notes](docs/reference/memory.md) shared across worktrees.
- **Multi-Protocol Models & Extensions**: Built-in adapters for Anthropic Messages, OpenAI Responses, and Chat Completions ([providers](docs/reference/providers.md)); full [MCP](docs/reference/mcp.md) support (tools, resources, `@server:uri`, and prompt commands), [Skills](docs/reference/skills.md), [Hooks](docs/reference/hooks.md), and [plugins](docs/reference/plugins.md).
- **Layered Permissions & Native Sandboxing**: Four [permission modes](docs/reference/permissions.md), layered allow/deny rules, risky-command guards, and native OS [command sandboxes](docs/reference/sandbox.md) on Windows, Linux, and macOS with domain-level network control.

See the [usage guide](docs/usage.en.md) for full instructions.

## Install

Requires Python 3.12 or newer. Install from PyPI with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install mosscode
```

To install the latest development snapshot directly from GitHub: `uv tool install git+https://github.com/yzd05/mosscode.git`. Start MossCode inside any project directory:

```bash
cd /path/to/project
moss
```

Run `moss doctor` (or `moss doctor --ping`) to verify installation, configuration, and model connectivity. Upgrading, uninstalling, and platform notes: [installation details](docs/usage.en.md#installation-details).

## Configure

Running `moss` for the first time opens an interactive setup wizard (`moss setup --add` appends more providers later). You can follow an existing local Claude Code or Codex setup, or pick a built-in preset for DeepSeek, Kimi, GLM, Qwen, OpenRouter, Anthropic, or OpenAI, storing API keys in the system keyring by default (`moss key set`).

Minimal `~/.mosscode/config.yaml` when configuring by hand (see [config.example.yaml](config.example.yaml) and [configuration](docs/usage.en.md#configuration)):

```yaml
providers:
  - name: deepseek
    preset: deepseek
    model: deepseek-chat
    api_key_env: DEEPSEEK_API_KEY
```

## Use

Command-line entry points:

```bash
moss                                        # interactive terminal UI
moss "summarize the core modules"           # start and send a prompt immediately
moss -c                                     # resume the latest session in this directory
moss --sandbox                              # run with native OS command sandboxing
moss -p "run tests and summarize failures"  # headless mode (text / json / stream-json)
moss acp                                    # ACP server for JetBrains IDEs and Zed
```

Type `/help` inside MossCode for all commands and keybindings, including `/plan` (read-only planning), `/swarm` (multi-agent collaboration), `/goal` (run until a condition is met), `/loop` (recurring checks), `/rewind` (roll back files and conversation), `/model` and `/thinking` (switch model and reasoning effort), `@file` (attach files or MCP resources), and `!cmd` (run shell commands directly). See [keys and commands](docs/usage.en.md#keys-and-commands).

## Architecture

Source code lives in `src/mosscode/`. The terminal UI (`tui.py`), headless CLI (`cli.py`), and IDE server (`acp/`) share a single session and agent core (`conversation.py`, `agent.py`), coordinating model protocol adapters (`providers.py`), tools and repository indexing (`tools/`, `repomap.py`), permissions and OS sandboxes (`permissions.py`, `sandbox/`), extensions (`mcp_client.py`, `skills/`, `hooks/`), and sub-agents/worktree teams (`agents/`, `teams/`, `worktree/`).

Run and test from source:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python -m pytest -q
ruff check && ruff format --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), [evaluations](docs/reference/evaluations.md) (Chinese), [ROADMAP.md](ROADMAP.md), [CHANGELOG.md](CHANGELOG.md), and [SECURITY.md](SECURITY.md).

[MIT](LICENSE)
