# 插件

插件把一组 Skill、命令、子 Agent、MCP 服务器和 Hook 放在一个目录里，用一条命令安装到用户级，所有项目都能用。格式兼容 Claude Code 的插件，现成的 Claude Code 插件仓库和 marketplace 仓库可以直接安装。

插件只在命令行中安装和管理（`moss plugin …`），变化在下一次启动 moss 时生效；启动时不访问网络，不自动安装或更新。

## 插件目录

一个插件是一个目录，按约定存放组件：

| 位置 | 内容 |
| --- | --- |
| `.claude-plugin/plugin.json` | 可选的清单，见 [plugin.json](#pluginjson) |
| `skills/<名称>/SKILL.md` | Skill，与 [Skill](skills.md#skillmd) 相同，`tool.json` 专属工具照常可用。根目录有 `SKILL.md` 而没有 `skills/` 时，整个插件就是一个 Skill |
| `commands/*.md` | 命令：每个文件是一条只能由用户输入的 `/名称` 命令（名称取文件名），作为 inline Skill 运行 |
| `agents/*.md` | 子 Agent 定义，与 [子 Agent](subagents.md#agent-定义) 相同，只读这一层 |
| `.mcp.json` | MCP 服务器，Claude Code 格式（`mcpServers` 对象，或直接是服务器表） |
| `mcp.yaml` | MCP 服务器，MossCode 格式，见 [MCP](mcp.md#配置) |
| `hooks.yaml` | Hook，MossCode 格式，见 [Hook](hooks.md) |

**命令文件**的 frontmatter 可以省略。`description` 缺省时取正文第一行；`argument-hint`、`allowed-tools` 照用，`allowed-tools` 中 MossCode 没有的工具名去掉并提示；`model` 忽略并提示。命令不列入模型可见的 Skill 目录。

**不加载的内容**：Claude Code 的 `hooks/hooks.json`（格式与 MossCode 的 Hook 不同）、`bin/`、`.lsp.json`、`output-styles/`、`monitors/`、`themes/`、`workflows/`、`settings.json`，以及清单中其他组件字段（如 `hooks`、`userConfig`、`dependencies`）。它们在安装时和 `/plugins` 中列为“未支持的组件”，不影响其他组件。

### plugin.json

读取 `name`、`version`、`description` 和组件路径 `skills`、`commands`、`agents`、`mcpServers`；`author`、`homepage`、`repository`、`license`、`keywords` 等元数据忽略。

- 路径可以是一个字符串或列表，必须以 `./` 开头并留在插件目录内，否则忽略这一项并列为问题。
- `skills` 中的目录在 `skills/` 之外另外读取；`commands`、`agents` 写了就只读写出的位置（可以是目录或单个 `.md` 文件）。
- `mcpServers` 可以是 `.mcp.json` 格式的文件路径，也可以直接写服务器表。
- 清单不是有效的 JSON 时整个插件不加载。

### 变量

组件中可以引用插件的位置：

| 变量 | 值 |
| --- | --- |
| `${MOSSCODE_PLUGIN_ROOT}`、`${CLAUDE_PLUGIN_ROOT}` | 插件目录 |
| `${MOSSCODE_PLUGIN_DATA}`、`${CLAUDE_PLUGIN_DATA}` | `~/.mosscode/plugins/data/<名称>/`，第一次用到时创建，更新插件时保留，删除插件时一起删除 |
| `${CLAUDE_PROJECT_DIR}` | 项目根目录（启动目录） |

替换发生在 Skill、命令和子 Agent 的正文，Skill 专属工具的命令行，MCP 的 `command`、`args`、`env`、`url`、`headers`，以及 Hook 的命令中；路径一律用 `/` 分隔。Hook 命令、MCP stdio 服务器和插件 Skill 的专属工具还以同名环境变量得到这些值。

引用 `${user_config.…}` 的组件不加载，提示“插件需要填写配置（userConfig），MossCode 不支持”。

## 名称与优先级

插件的组件按原名注册，不加插件名前缀。同名时：

| 组件 | 优先级（后者覆盖前者） |
| --- | --- |
| Skill、命令、子 Agent | 内置 < 插件 < 用户级 < 项目级 |
| MCP 服务器 | 插件 < 用户 `mcp.yaml` < `.mcp.json` < 项目 `mcp.yaml` |
| Hook | 追加合并，按 用户级 < 插件 < 项目级 < 本地 的顺序加载，`id` 重复时先加载的生效 |

两个插件中同名时，`plugins.yaml` 中靠后的覆盖靠前的。被覆盖的版本照常在 `/skills 名称`、`/agents 名称` 中列出，来源显示为“插件 名称”。命令与内置命令同名时跳过，与 Skill 相同。

插件的 `.mcp.json` 不需要 `import_mcp_json`：安装时已经看过并确认了其中的命令。团队成员和子 Agent 使用与主会话相同的插件。

## moss plugin

| 写法 | 行为 |
| --- | --- |
| `moss plugin install <来源> [--ref 引用] [--name 名称] [-y]` | 安装一个插件 |
| `moss plugin list` | 每个插件的名称、版本、状态、来源、提交（前 7 位）、组件数量和问题 |
| `moss plugin update [名称] [-y]` | 更新 git 来源的插件；省略名称时更新全部 |
| `moss plugin remove <名称>` | 删除记录、克隆目录和数据目录；本机目录来源只删除记录，目录本身保留 |
| `moss plugin disable <名称>`、`moss plugin enable <名称>` | 停用或启用：只改记录，停用的插件保留文件、不加载 |

失败时说明原因，退出码为 1；取消安装或更新时退出码也是 1，磁盘上不留下文件。

### 来源

| 写法 | 处理 |
| --- | --- |
| 本机目录，如 `F:/dev/my-plugin` | 原地使用，不复制，改了文件下次启动就生效，适合开发插件；不能加 `--ref` |
| git 地址：`https://…`、`ssh://…`、`git@host:…`、`file://…` | 克隆到 `~/.mosscode/plugins/repos/<名称>/` |
| `owner/repo`（本机没有这个目录时） | GitHub 仓库 `https://github.com/owner/repo.git` |

`--ref` 可以是分支、标签或提交；不写时用仓库的默认分支。git 来源需要 PATH 中有 `git`，git 不会弹出密码提示：私有仓库请先配置好 SSH 密钥或凭据管理器。

插件名默认取 `plugin.json` 的 `name`，没有时取仓库名或目录名。名称只能是小写字母、数字和 `-`，以字母或数字开头，最长 64 个字符；不合法或与已安装的插件重名时用 `--name` 指定。

### marketplace

仓库根目录不是插件、但有 `.claude-plugin/marketplace.json` 时，它是一个 marketplace：

```powershell
moss plugin install owner/marketplace            # 列出其中的插件和描述，退出码 1
moss plugin install owner/marketplace#formatter  # 安装其中名为 formatter 的插件
```

条目的 `source` 支持仓库内的相对路径（安装同一个仓库的子目录，可配合 `metadata.pluginRoot`），以及另一个 git 仓库（`{"source": "github", "repo": …}`、`{"source": "url", "url": …}`、`{"source": "git-subdir", "url": …, "path": …}`，可带 `ref` 或 `sha`）；npm 等其他来源提示不支持。条目中直接声明的组件字段不读取，只读插件目录本身。

### 确认

安装前列出插件的内容：名称、版本、来源和提交、每个 Skill、命令和子 Agent 的名称、每个 MCP 服务器的启动命令或地址、每条 Hook 的事件和命令、未支持的组件和问题，然后询问“安装？[y/N]”。路径显示为安装后的位置。

插件中的 Hook 和 MCP 服务器会以你的权限运行程序，请看过再确认。`-y` 跳过询问；stdin 不是终端又没有 `-y` 时不安装。

### 更新

`moss plugin update` 对 git 来源的插件按记录的 `ref` 拉取：

- `ref` 是标签或提交时不移动，提示“固定在 …”；要跟随分支，把 `plugins.yaml` 中的 `ref` 改为分支名。
- `ref` 是分支或没有写时更新到远端最新；没有新提交时提示“已是最新”。
- 有变化时列出新旧提交和组件的增减（`+`、`-`），询问后替换克隆目录；数据目录保留。
- 克隆目录丢失（例如手动删除）时重新克隆。
- 本机目录来源原地使用，无需更新。

### plugins.yaml

安装记录写在 `~/.mosscode/plugins.yaml`，可以手动编辑：

```yaml
plugins:
  - name: review-tools
    source: https://github.com/example/review-tools.git
    ref: v1.2.0               # 可选：分支、标签或提交
  - name: formatter
    source: https://github.com/example/marketplace.git
    path: plugins/formatter   # marketplace 中的子目录
  - name: my-plugin
    source: F:/dev/my-plugin  # 本机目录，原地使用
    enabled: false
```

插件按文件中的顺序加载。当前提交以克隆目录为准，不写入记录。某一条记录有误时跳过这一条并在启动区提示；记录中有、磁盘上没有的插件提示“运行 moss plugin update <名称>”。

## /plugins

对话中 `/plugins`（别名 `/plugin`）列出已安装的插件，内容同 `moss plugin list`；`/plugins 名称` 显示这个插件的全部组件、问题和状态。安装、更新、删除和停用在命令行中进行，界面中会提示这一点。

`moss doctor` 的插件一节检查：有 git 来源的插件时 git 是否可用、每个插件是否在磁盘上、清单能否读取、组件的问题。

## 安全

- 安装和更新前列出 Hook 和 MCP 的命令并确认；确认后用户级插件视为可信，与 `~/.mosscode/hooks.yaml` 相同，Hook 不再单独确认。
- 插件 Skill 的专属工具、MCP 工具照常按权限档位审批，插件不改变权限规则。
- 本机目录来源原地使用，目录中的改动不再经过确认，只安装你信任的目录。
