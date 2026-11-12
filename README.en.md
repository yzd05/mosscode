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
  A coding agent for the terminal. Designed after Claude Code, with permissions you control, built for long tasks.
</p>

---

[中文](README.md) | English | [Usage guide](docs/usage.en.md) | [Reference (Chinese)](docs/reference/cli.md) | [Claude Code patterns](docs/claude-code-patterns.md) | [Roadmap](ROADMAP.md) | [Changelog](CHANGELOG.md) | [Contributing](CONTRIBUTING.md)

MossCode is a coding assistant that works in your terminal, written in Python. It is designed after Claude Code and pi: one agent loop with local and MCP tools, four permission modes including a read-only plan mode, and three protocols: Anthropic Messages, OpenAI Responses and Chat Completions. The interface and the detailed reference documentation are in Chinese.

### Core features

- **Agent loop**: many tool steps per turn, with adjacent read-only calls run concurrently; empty replies and replies cut off while thinking are retried, and replies cut off mid-text are continued automatically.
- **Three protocols**: streaming, prompt caching, thinking effort and image input; a model catalog shipped with MossCode supplies context windows and cost estimates.
- **Tools**: read, write, edit, commands, glob, grep, web fetch and search, the `todo_write` task list and `ask_user` questions; files must be read before they are edited, and long commands can move to the background.
- **Permissions and sandbox**: four permission modes with layered rules and risk levels for commands; command sandboxes on Linux, macOS and Windows, with network access through a domain proxy; keys never reach the commands' environment.
- **Long tasks**: `/goal` keeps working until a condition is met; large results go to disk, old tool results are cleared before compaction, and summarizing compaction has a circuit breaker.
- **Sessions and memory**: sessions are saved as they go and resumed with `-c` or `/resume`; `/rewind` rolls files and conversation back to before any earlier message; memory notes carry across sessions.
- **Multiple agents**: defined and forked sub-agents, background runs and git worktree isolation; agent teams work in parallel in Windows Terminal or tmux split panes.
- **Extensions**: MCP, skills, hooks and plugins, compatible with Claude Code's plugin format.
- **Interface**: drawn inline, keeping the terminal's own scrollback and text selection; Markdown rendered while it streams; approval and question cards take the prompt's place; light and dark terminals each get their own palette.

## Install

Python 3.12 or later is required. The recommended way is to install it as a standalone command with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install mosscode
moss doctor     # checks the installation, PATH, configuration and helper tools; sends no request
```

This installs two identical commands, `moss` and `mosscode`. PATH warnings, upgrading and uninstalling, notes for each platform, containers and the command sandbox: [installation details](docs/usage.en.md#installation-details). Installing from source: [contributing](CONTRIBUTING.md).

## Configure

On first run, `moss` asks in the terminal for the protocol, endpoint, model, context window and credential, writes `~/.mosscode/config.yaml` and opens the interface; `moss setup` runs the same steps at any time. Credentials go to the system credential store by default, and the configuration only stores their name.

When editing by hand, a minimal provider looks like this; [config.example.yaml](config.example.yaml) documents every field:

```yaml
providers:
  - name: claude
    protocol: anthropic-messages      # or openai-responses, openai-chat-completions
    model: replace-with-service-model
    api_key_env: ANTHROPIC_API_KEY    # an environment variable; or api_key_keyring (a name saved with moss key set)
```

Project configuration, common options, and the files for permissions, MCP, skills, hooks, sub-agents and plugins: [usage guide](docs/usage.en.md#configuration).

## Use

### Quick start

```bash
moss                                        # start the interface in the current directory
moss "explain this project's entry point"   # start and send this message
moss -c                                     # resume this project's latest session
moss -p "list the modules under src"        # headless: run one task and exit
```

Describe the task in plain language and the model picks the tools; writing files or running commands asks for your confirmation according to the permission mode.

### Common commands

- `/help`: commands and keys.
- `/model`, `/thinking`: switch model and thinking effort.
- `/plan task`: investigate read-only and submit a plan that runs once approved; Shift+Tab cycles permission modes.
- `/goal condition`: set a completion condition, checked after every turn; work continues until it is met.
- `/compact`, `/clear`: compact the context; start a new session.
- `/resume`, `/rewind`: resume a session; roll files and conversation back to an earlier message.
- `/status`: model, permission mode, context, usage and estimated cost.
- `/mcp`, `/skills`, `/hooks`, `/agents`, `/plugins`: inspect extensions.
- `/tasks`, `/team`: background tasks and agent teams.
- `@file` attaches a file's content, `!command` runs a command without the model, Esc cancels the running task.

All keys and commands, permission modes, web tools and headless mode: [usage guide](docs/usage.en.md#keys-and-commands).

## Architecture

```
cli.py ──► tui.py (Textual interface) or -p (headless)
             └──► conversation.py (context and MCP start-up, public interface)
                    └──► agent.py (agent loop)
                           ├──► compaction.py, context.py (context estimates and compaction)
                           ├──► session.py (session files and resuming), memory*.py (memory notes)
                           ├──► providers.py (three protocols, retries, cache markers)
                           ├──► prompts.py (system prompt, environment, mode reminders)
                           └──► tools/ (registration, validation, approval, execution)
                                  ├──► permissions.py, security.py (modes, rules, command risk)
                                  ├──► mcp_client.py, mcp_config.py (MCP)
                                  ├──► skills/, tools/skill.py (skills and their tools)
                                  ├──► hooks/ (lifecycle hooks, tool-call interception)
                                  └──► agents/, tools/agent.py (sub-agents and background tasks)
                                         ├──► worktree/ (git worktree isolation)
                                         └──► teams/, tools/team.py (agent teams)
```

One task works like this:

1. The agent loop requests the model with the stable system prompt, the history and this request's environment, and passes the streamed events to the interface.
2. Tool calls in the reply are validated, approved and run. Up to four adjacent read-only calls run at once.
3. The plain-text results go back into the history, and the model is requested again.
4. This repeats until a reply calls no tools or a stop condition is reached (cancel, turn limit, repeated unknown tools, and so on).

The interface and `-p` share `Conversation`. From Python, call `Conversation.ask()` repeatedly to keep a multi-turn history, then `await conversation.close()`.

<details>
<summary>What each module does</summary>

| Module | Responsibility |
| --- | --- |
| `agent.py` | Agent loop: requests, tool runs, queued messages, stop conditions, plan mode, when to compact |
| `context.py`, `compaction.py`, `tool_budget.py` | Context estimates and thresholds, summary compaction, storing large results on disk |
| `session.py`, `serialization.py`, `paths.py` | Session files, replay and repair, locks, cleanup; project keys |
| `memory.py`, `memory_extract.py`, `tools/memory.py` | Memory notes, background extraction, the memory tool |
| `providers.py` | Anthropic Messages, OpenAI Responses and Chat Completions adapters, retries before the first event, cache breakpoints |
| `prompts.py` | Fixed prompt modules, tool list, `<environment>` and `<system-reminder>`, AGENTS.md, git snapshot |
| `tools/` | Tools such as `files.py`, `search.py`, `process.py`, `plan.py`; `execution.py` batches and runs calls concurrently; `base.py` registration and execution boundary |
| `permissions.py`, `security.py` | Modes, layered rules, approval requests, read-only command list, dangerous-command levels, project path checks |
| `mcp_client.py`, `mcp_config.py` | MCP configuration, background connection and discovery, tool adapters |
| `skills/`, `tools/skill.py` | Skill discovery and overrides, `load_skill` and script tools; built-in skills |
| `hooks/` | Hook files and trust, conditions, actions, per-event execution and logs |
| `agents/`, `tools/agent.py` | Sub-agent definitions, defined and forked runs, background tasks and notifications |
| `worktree/` | Git worktree naming, creation, initialisation, change checks, safe removal, cleanup |
| `teams/`, `tools/team.py`, `coordinator.py` | Agent teams: storage and locks, mailboxes, shared tasks, member turns, merging, pane back ends; coordinator mode |
| `tui.py`, `appearance.py`, `scrollback.py` | Interface, commands and suggestions, native scrollback |
| `launcher.py`, `cli.py` | Entry point; `--version` and subcommands load neither the interface nor the SDKs |
| `config.py`, `setup.py`, `doctor.py` | Configuration, `moss setup` and `moss doctor` |
| `credentials.py`, `key_command.py` | System credential store (`moss key`), masking keys in child environments and output |
| `plugins.py`, `plugin_command.py` | Plugin records and loading; `moss plugin` install, marketplaces, updates and removal |

</details>

### Documentation

- [Usage guide](docs/usage.en.md): installation details, options, keys, commands, permission modes, headless mode
- [Reference documentation](docs/reference/cli.md) (Chinese): the full description of current behaviour, starting with the [command line](docs/reference/cli.md) and the [terminal interface](docs/reference/interface.md)
- [Claude Code patterns](docs/claude-code-patterns.md) (Chinese): Claude Code's design and MossCode's choices, topic by topic
- [Roadmap](ROADMAP.md) (Chinese): what is done and what comes next
- [Changelog](CHANGELOG.md) (Chinese): changes by version
- [Contributing](CONTRIBUTING.md), [development notes](AGENTS.md), [security policy](SECURITY.md)

### Development

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python -m pytest -q
ruff check && ruff format --check
```

MossCode is released under the [MIT license](LICENSE).
