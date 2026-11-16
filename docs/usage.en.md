# Usage guide

[README](../README.en.md) · [中文](usage.md)

The README covers the essentials. This page has the installation details, the common options, the configuration files, keys, commands and a short description of each feature. The full reference documentation is in Chinese, starting with the [command line](reference/cli.md).

## Installation details

This installs two identical commands, `moss` and `mosscode`, usable from any directory. If uv warns that `~/.local/bin` (`%USERPROFILE%\.local\bin` on Windows) is not on PATH, run `uv tool update-shell` and open a new terminal. `moss doctor` checks the installation, PATH, configuration and helper tools without sending any request.

Upgrade with `uv tool upgrade mosscode` and uninstall with `uv tool uninstall mosscode`. `pipx install mosscode` also works.

MossCode runs on Windows, Linux and macOS. Day-to-day development happens on Windows (Windows Terminal), and CI runs the full test suite on all three systems.

- **Search**: ripgrep (`rg`) on PATH is used when present, with a built-in fallback.
- **Commands on Windows**: Git for Windows is recommended, because commands run in Git Bash when it is available.
- **Agent teams**: split panes need Windows Terminal or tmux; otherwise members run in-process.

It can also run in a Docker container, so that commands only reach the mounted project directory ([running in a container](reference/container.md)).

A command sandbox can also let the operating system limit what the model's commands may write, read and connect to ([command sandbox](reference/sandbox.md)):

- Linux: bubblewrap;
- macOS: Seatbelt;
- Windows: MXC, or a restricted token that only limits writes.

Installing from source, development and tests: [contributing](../CONTRIBUTING.md).

## Configuration

### Models

On first run without a configuration, `moss` asks in the terminal for the protocol, endpoint, model, context window and credential, writes `~/.mosscode/config.yaml`, and opens the interface. `moss setup` runs the same steps at any time and leaves an existing configuration alone.

Credentials go to the system credential store by default (Windows Credential Manager, macOS Keychain, Linux Secret Service), and the configuration only stores their name. You can also reference an environment variable, or put the key in the file in plain text.

To add providers or change other options, edit the file by hand; [config.example.yaml](../config.example.yaml) documents every field:

```powershell
New-Item -ItemType Directory -Force $HOME\.mosscode | Out-Null
Copy-Item config.example.yaml $HOME\.mosscode\config.yaml
```

`~/.mosscode/config.yaml` is the user-level configuration and applies everywhere. A configuration for one project goes in that project's `.mosscode/config.yaml`.

Without `--config`, MossCode uses the first file that exists among `./.mosscode/config.yaml` and `~/.mosscode/config.yaml`. A `config.yaml` in the working directory itself is never read.

A project configuration has two restrictions:

- It cannot loosen the command sandbox or the commands' environment (`sandbox`, `command_env`); those come from the user-level file.
- It needs your confirmation the first time it is used and whenever it changes.

```yaml
providers:
  - name: claude
    protocol: anthropic-messages      # or openai-responses, openai-chat-completions
    model: replace-with-service-model
    api_key_env: ANTHROPIC_API_KEY    # an environment variable; or api_key_keyring (a name saved with moss key set), or api_key
    # max_output_tokens: 32000        # per-reply output limit; defaults to the model catalog's value (at most 32000)
    # context_window: 200000          # usually unnecessary; set it when the catalog does not know the model
    # catalog_model: claude-sonnet-4-5  # the catalog entry to use when the model name is an alias
    # base_url: https://your-gateway.example/v1
```

Keep credentials in the system store (`moss key set NAME`) or in environment variables, not in the repository or the chat. Commands run by the model do not inherit those variables, and keys that appear in tool results are masked.

Context windows, output limits, image support and prices come from a model catalog shipped with MossCode (data from LiteLLM). `moss models` shows what matched, and `moss models update` downloads the latest data.

Compatible services, multiple providers, retries and protocol options are described in [model providers](reference/providers.md).

### Common options

Common top-level options:

| Option | Default | Meaning |
| --- | --- | --- |
| `default_provider` | none | Provider used at start-up; with several and none set, you choose at start-up |
| `max_iterations` | 200 | Most model requests in one task |
| `shell` | `auto` | Git Bash first on Windows, PowerShell if not found; or `bash`, `powershell`, or a path to bash.exe |
| `system_prompt` | built-in | Replaces the built-in system prompt entirely |
| `auto_compact` | `true` | Compact older conversation automatically near the context window ([context](reference/context.md)) |
| `persist_sessions` | `true` | Save the session as it goes, for resuming ([sessions](reference/sessions.md)) |
| `memory` | `true` | Memory notes: an index in the system prompt, the `memory` tool and background extraction ([memory](reference/memory.md)) |
| `auto_memory` | `true` | Extract things worth remembering in the background after a task; `false` keeps only the `memory` tool |
| `memory_provider` | current provider | Provider used for background extraction, for example a cheaper model |
| `worktree_links` | `[]` | Paths (relative to the repository root) linked into every git worktree, such as `node_modules` or `.venv` |
| `worktree_stale_hours` | `24` | Hours after which unchanged worktrees left by sub-agents are removed; `0` turns this off |
| `teammate_mode` | `auto` | Where team members run: `auto`, `in-process`, `windows-terminal`, `tmux` |
| `team_max_members` | `4` | Members per team, 1–8 |
| `coordinator_mode` | `false` | Coordinator mode switch; also needs `MOSSCODE_COORDINATOR_MODE=1` |
| `todo_tool` | `true` | Offer `todo_write`, the task list the model keeps during multi-step work, shown above the prompt |
| `goal_max_turns` | `30` | Turns `/goal` runs on its own before pausing, 1–1000 |
| `goal_provider` | `memory_provider` or the current provider | Provider that judges whether a `/goal` is met |
| `claude_directories` | `false` | Also read Claude Code's `.claude/skills/` and `.claude/agents/` |
| `sandbox` | `mode: off` | Command sandbox; `mode: on` runs the model's commands in operating-system isolation |
| `web_search` | none | Search service, such as `{provider: tavily, api_key_env: TAVILY_API_KEY}`; without it there is no search tool |

### Configuration files

Permissions, MCP, project instructions, skills, hooks, sub-agents, plugins and worktrees each have their own files:

| File | Purpose | Details |
| --- | --- | --- |
| `~/.mosscode/permissions.yaml`, `.mosscode/permissions.yaml`, `.mosscode/permissions.local.yaml` | Permission rules (user, project, local) | [permissions](reference/permissions.md), example [permissions.example.yaml](../permissions.example.yaml) |
| `~/.mosscode/mcp.yaml`, `.mosscode/mcp.yaml` | MCP servers | [MCP](reference/mcp.md), example [mcp.example.yaml](../mcp.example.yaml) |
| `AGENTS.md` (or `CLAUDE.md`), `.mosscode/AGENTS.md`, `~/.mosscode/AGENTS.md` | Project instructions appended to the system prompt; a line `@path` includes another file | [system prompt and caching](reference/prompts.md) |
| `~/.mosscode/skills/<name>/`, `.mosscode/skills/<name>/`, shared `.agents/skills/` | Skills: a `SKILL.md` procedure and optional `tool.json` script tools | [skills](reference/skills.md) |
| `~/.mosscode/hooks.yaml`, `.mosscode/hooks.yaml`, `.mosscode/hooks.local.yaml` | Hooks around sessions, turns, requests and tool calls; project hooks need confirmation | [hooks](reference/hooks.md) |
| `~/.mosscode/agents/<name>.md`, `.mosscode/agents/<name>.md` | Sub-agent definitions: name, description, tools, model, turns and permission mode in the front matter, the system prompt in the body | [sub-agents](reference/subagents.md) |
| `~/.mosscode/plugins.yaml`, `.mosscode/plugins.yaml` | Plugins bundling skills, commands, sub-agents, MCP and hooks; compatible with Claude Code plugins | [plugins](reference/plugins.md) |
| `.worktreeinclude` (repository root) | Ignored files, in `.gitignore` syntax, to copy into new worktrees, such as `.env` | [git worktrees](reference/worktree.md) |

## Starting MossCode

```powershell
moss                          # start the interface in the current directory
moss "explain this project's entry point"   # start and send this message
moss --provider claude        # choose a provider
moss --list-providers
moss -c                       # resume this project's latest session
moss --resume                 # pick a session to resume
```

All options, subcommands and exit codes: [command line](reference/cli.md).

## Keys and commands

Describe the task in plain language, for example "read README.md and explain the entry point", and the model picks the tools. The interface is drawn inline in the terminal, and finished output goes into the terminal's own scrollback.

| Key | Action |
| --- | --- |
| Enter | Send; while a task runs, the message is queued and joins the conversation before the next request |
| Shift+Enter, Ctrl+J, `\` at line end | New line |
| Alt+V (also Ctrl+V on macOS and Linux), or drag an image file into the terminal | Attach an image, shown as `[图片 #1]` |
| `@` | List project files with fuzzy matching; `@path` attaches the file's content when sent (`@file#L10-20` attaches those lines) |
| `!command`, `!!command` | Run a command without the model; `!` adds its output to the conversation for the model's next turn, `!!` only runs it |
| ↑ / ↓ | Input history; choose a suggestion when suggestions are open |
| Tab | Complete commands and arguments |
| Shift+Tab | Cycle permission modes default → acceptEdits → plan |
| Esc / Ctrl+C | Cancel generation; with queued messages, the first Esc only brings them back to the prompt; during an approval, Esc refuses the call |
| Esc Esc | When idle with an empty prompt, open `/rewind` |
| Ctrl+Q | Quit |

| Command | What it does |
| --- | --- |
| `/help [command]` | Commands and keys; with a name, that command's usage and aliases |
| `/clear` | Start a new session: clear the screen, scrollback, model context and token counts; mode, session approvals and MCP connections stay |
| `/compact [note]` | Compact the context: older conversation becomes a summary, recent messages and your own words stay |
| `/resume [ID]` | Resume a session of this project, from a list or by ID |
| `/rewind` | Pick an earlier message and roll files, conversation or both back to before it was sent |
| `/session` | The current session's ID, file, title and message count |
| `/memory [name]` | Loaded project instruction files and memory notes; with a name, that note in full |
| `/model [name]` | Switch provider from a list or by name; the conversation is kept |
| `/thinking [level]` | Thinking effort for this session (off, low, medium, high, or `default`) |
| `/plan [task]` | Read-only investigation and planning; the plan runs after you approve it |
| `/do [task]` | Leave plan mode |
| `/permissions [mode]` | Explain or switch the permission mode |
| `/sandbox [on\|off]` | Sandbox boundaries and recent blocks; switch it for this session |
| `/mcp [name]` | MCP server status, or one server's tools |
| `/status` | Model, mode, context, usage and estimated cost, session, MCP, memory, hooks and sub-agents |
| `/skills`, `/hooks`, `/agents`, `/plugins` | List and inspect skills, hooks, sub-agent definitions and plugins |
| `/tasks [id\|stop id]` | Background sub-agents and commands |
| `/worktree` | MossCode's git worktrees: list, create, remove, clean |
| `/team` | Agent teams of this project |
| `/todo` | The model's current task list |
| `/goal [condition\|clear]` | Set a completion condition the session keeps working towards |
| `/<skill> [arguments]` | Run a skill, such as the built-in `/commit`, `/init`, `/review`, `/test` |
| `/exit` | Quit and clean up |

Built-in commands run locally without asking the model; skill commands send the skill's procedure to the model. Details: [commands](reference/commands.md) and [terminal interface](reference/interface.md).

## Sessions and memory

Sessions are saved as they go under `~/.mosscode/projects/<project key>/sessions/`. After quitting or a crash, continue with `-c`, `--resume [ID]` or `/resume`. `write_file` and `edit_file` save a checkpoint before changing a file, so `/rewind` can roll files and conversation back. Sessions are kept for 30 days.

Say "remember …" and the model writes a note with the `memory` tool. After a task, corrections and preferences you gave are also extracted in the background, from the conversation only, never from tool output. Notes are Markdown files whose index enters the system prompt at the next start.

## Permission modes

Without a matching rule, the mode decides; deny rules apply in every mode.

| Mode | Read-only tools and commands | File writes and edits | Other commands |
| --- | --- | --- | --- |
| `default` | allowed | asked | asked |
| `acceptEdits` | allowed | allowed | asked |
| `plan` | allowed | unavailable | unavailable |
| `bypassPermissions` | allowed | allowed | allowed |

An approval can allow once, for the session, or permanently (saved to `.mosscode/permissions.local.yaml`). Some commands are handled separately:

- Catastrophic commands, such as deleting the root directory or formatting a disk, are always refused.
- High-risk commands such as `git reset --hard`, `git push --force` and `rm -rf` are confirmed every time.

Path checks are an application-level limit, not a sandbox.

**Plan mode**: enter with `/plan task` or Shift+Tab. The model may only read and run read-only commands, then submits a plan with `exit_plan_mode`; once approved, the same task continues with all tools.

**Agent teams**: for larger tasks that split into parallel parts, the model can lead a team.

- Shared tasks record their dependencies.
- Up to four members each work in their own git worktree and report by message.
- When all are done, their branches are merged one by one after you confirm.
- In Windows Terminal, members appear in split panes.

## Web tools

`web_fetch` reads a web page (HTML turned into Markdown, long pages in parts). `web_search` is available when a search service is configured (`web_search` in `config.yaml`; Tavily for now).

Both send addresses or queries outside, so each new domain and each search needs confirmation. Local and private network addresses are always refused.

## Security notes

- Path checks are an application-level limit, not an operating-system sandbox: an approved command runs with your permissions, so read it before you confirm. For isolation, run MossCode in Docker or a dev container ([running in a container](reference/container.md)); a container isolates files and processes, not the network.
- Web pages and search results are untrusted data, and the model is told not to follow instructions in them; still read its answers with care after it visits an unfamiliar site.
- Hooks, MCP configuration and plugin declarations in a project start programs: project hooks and project plugins need your confirmation before they run, and `.mcp.json` is not read by default. Look at the files under `.mosscode/` before opening an unfamiliar repository.
- Resource quotas and audit logs are not supported yet.

## Headless mode

```powershell
moss -p "list the modules under src"
moss -p "explain the entry point" --show-usage
moss -p "list the modules under src" --output-format json
Get-Content error.log | moss -p "explain this error"
```

`-p` (`--prompt`) runs one task and exits; piped input is appended to the task. It never waits for approval: calls that need one return `approval_required` and the model adjusts.

`--output-format` is `text`, `json` or `stream-json`. Unconfirmed project hooks and plugins are not loaded unless trusted with `--trust-project-hooks` or `--trust-project-plugins`.
