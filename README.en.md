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

[中文](README.md) | English

MossCode is an open-source coding agent that lives in your terminal, understands your local codebase, reads and edits files, runs commands, searches code, and handles Git workflows through natural language. Its design draws on Claude Code and Pi.

Use MossCode interactively in the [inline terminal UI](docs/reference/interface.md) (keeping native scrollback and text selection), automate it in scripts and CI with [`-p` headless mode](docs/usage.en.md#headless-mode), or connect it to PyCharm, IntelliJ IDEA, and Zed over [ACP](docs/reference/acp.md).

It supports Anthropic Messages, OpenAI Responses, and Chat Completions; provides four [permission modes](docs/reference/permissions.md) and native [command sandboxes](docs/reference/sandbox.md) on Linux, macOS, and Windows; manages long sessions with cache-preserving [context compaction](docs/reference/context.md), [session rollback](docs/reference/sessions.md), and [memory notes](docs/reference/memory.md); and extends via [MCP](docs/reference/mcp.md), [Skills](docs/reference/skills.md), [Hooks](docs/reference/hooks.md), [sub-agents](docs/reference/subagents.md), [worktree teams](docs/reference/teams.md), and Claude Code-compatible [plugins](docs/reference/plugins.md). See the [usage guide](docs/usage.en.md) and [Claude Code comparison](docs/claude-code-patterns.md) (Chinese) for details.

## Install

Requires Python 3.12 or newer. Install with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/yzd05/mosscode.git
```

Tagged releases can also be installed from PyPI with `uv tool install mosscode`. Start MossCode in any project directory:

```bash
cd /path/to/project
moss
```

Run `moss doctor` to check installation, PATH, configuration, and sandbox backends offline. Upgrading, uninstalling, and platform notes: [installation details](docs/usage.en.md#installation-details).

## Configure

Running `moss` for the first time launches an interactive setup wizard (`moss setup` runs it again anytime). If Claude Code or Codex is already configured locally, select **follow** to reuse its endpoint, model, and API key. Built-in presets cover DeepSeek, Kimi, GLM, Qwen, OpenRouter, Anthropic, and OpenAI, storing credentials in the system keyring by default (`moss key set`).

Minimal `~/.mosscode/config.yaml` when editing by hand (see [config.example.yaml](config.example.yaml) and [configuration](docs/usage.en.md#configuration) for all options):

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
moss "explain this project's entry point"   # start and send a prompt immediately
moss -c                                     # resume the latest session in this directory
moss -p "run tests and summarize failures"  # headless mode (text / json / stream-json)
moss acp                                    # ACP server (stdio JSON-RPC for IDEs)
```

Type `/help` inside MossCode for commands and shortcuts, including `/plan` (read-only investigation and plan approval), `/model` and `/thinking` (switch model and reasoning effort), `/rewind` (roll back files and conversation), `/goal` (continue across turns until a condition is met), `@file` (attach file contents), and `!cmd` (run a shell command directly). See [keys and commands](docs/usage.en.md#keys-and-commands) and [ACP integration](docs/reference/acp.md) (Chinese).

## Architecture

Source code lives in `src/mosscode/`. `launcher.py` provides a fast CLI entry point without importing the UI or model SDKs. The terminal UI (`tui.py`), `-p`, and `acp/` share `Conversation` (`conversation.py`), while `AgentLoop` (`agent.py`) coordinates `providers.py` and `tools/` for model streaming, permission checks, sandboxing, and tool execution.

<details>
<summary>Main modules</summary>

| Module | Responsibility |
| :--- | :--- |
| `launcher.py`, `cli.py`, `runtime_factory.py` | CLI entry point, argument parsing, shared runtime assembly |
| `conversation.py` | Session lifecycle, project instructions, MCP startup, public API |
| `agent.py` | Agent loop: model requests, concurrent read-only batches, stop conditions, compaction |
| `context.py`, `compaction.py`, `tool_budget.py` | Token estimation, tool-output clearing, turn collapsing, summary compaction, disk spill |
| `session.py`, `checkpoints.py` | Live JSONL session files, file snapshots, and `/rewind` rollback |
| `memory.py`, `memory_extract.py` | User and project memory notes, index generation, background extraction |
| `providers.py`, `catalog.py` | Protocol adapters, retries, prompt cache markers, model pricing catalog |
| `prompts.py` | System prompt sections, environment block, mode reminders, `AGENTS.md` loader |
| `tools/` | Built-in tools, parameter validation, concurrent scheduling |
| `permissions.py`, `security.py`, `sandbox/` | Permission modes and rules, command risk analysis, OS sandboxes, domain proxy |
| `mcp_client.py`, `skills/`, `hooks/`, `plugins.py` | MCP client, skills and script tools, lifecycle hooks, plugin manager |
| `agents/`, `worktree/`, `teams/` | Sub-agent runtime, Git worktrees, split-pane teams, branch merging |
| `tui.py`, `tui_views.py`, `windows_inline.py`, `scrollback.py` | Inline Textual UI, widgets/views, Windows console driver, streaming Markdown, native scrollback |
| `acp/` | ACP server for editors, multi-directory session isolation, permission bridging |

</details>

Run and test from source:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python -m pytest -q
ruff check && ruff format --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), [evaluations](docs/reference/evaluations.md) (Chinese), [ROADMAP.md](ROADMAP.md), [CHANGELOG.md](CHANGELOG.md), and [SECURITY.md](SECURITY.md).

[MIT](LICENSE)
