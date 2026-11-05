# 插件安装与 .claude 目录

日期：2026-11-04。状态：Q1–Q6 按推荐方案确定（2026-11-04）；12A 已实现，M158–M163 用 DeepSeek V4.1 Flash 验收通过（2026-11-04，无界面模式与命令行，界面中的显示由自动测试覆盖）；12B 未开始。

来源：
- [产品差距评审](../../research/product-gap-review.md)的 D3（插件安装）和 D2（读取 `.claude/agents/`），属于“完善十二：生态与安全”。评审中 D1 计划的“可选读取 `.claude/skills/`”当时没有做，一并放在这里。
- 用户的要求（2026-11-04）：先写 D3 和 D2 的规格。
- Claude Code 文档（2026-11-04 查阅）：[Plugin manifest reference](https://code.claude.com/docs/en/plugins-reference)、[Plugin commands reference](https://code.claude.com/docs/en/plugins/cli-reference.md)。
- pi 文档（2026-11-04 下载）：`packages/coding-agent/docs/packages.md`。

## 问题

1. **Skill、子 Agent、Hook、MCP 只能一个个复制。** 别人做好的一组配置（例如一个“代码评审”包：两个 Skill、一个评审子 Agent、一个格式化 Hook、一个 MCP 服务器）要手动复制到四个位置，更新时再复制一遍，也说不清装过哪些、来自哪里。
2. **Claude Code 生态里的插件用不上。** Claude Code 的插件已经形成了分发习惯（git 仓库、marketplace 列表），其中的 Skill 和子 Agent 格式 MossCode 基本兼容，但没有办法安装。
3. **同时用 Claude Code 的人要维护两份。** 项目里 `.claude/agents/`、`.claude/skills/` 中的定义 MossCode 不读取，要复制到 `.mosscode/`；复制过来的子 Agent 写着 `model: sonnet` 时，运行就报“没有名为 sonnet 的提供商”。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `skills/__init__.py` | `skill_dirs` 按优先级从低到高：内置、`~/.agents/skills`、`~/.mosscode/skills`、`.agents/skills`、`.mosscode/skills`；后者覆盖同名的前者，被覆盖的版本记在 `overrides` 中，`/skills <名称>` 列出。`Source` 只有 builtin / user / project |
| F2 | `agents/__init__.py` | `agent_dirs`：内置、`~/.mosscode/agents`、`.mosscode/agents`，覆盖方式同 Skill。已经接受 Claude Code 的字段写法（`disallowedTools`、`maxTurns`、`permissionMode`），`color`、`effort`、`skills`、`mcpServers`、`hooks`、`memory`、`initialPrompt` 忽略并提示；其他未知字段使整个文件被跳过 |
| F3 | `agents/runtime.py` | `model` 按 Provider 名称查找，找不到时子 Agent 不运行，报“没有名为 X 的提供商”。Claude Code 的 `model: sonnet / opus / haiku` 因此不能直接用 |
| F4 | `hooks/config.py` | Hook 读三个文件：`~/.mosscode/hooks.yaml`（直接生效）、`.mosscode/hooks.yaml`、`.mosscode/hooks.local.yaml`（按文件内容的 SHA-256 确认后生效，记录在 `~/.mosscode/projects/<项目键>/hooks-trust.json`）；`-p` 中未确认的不加载，`--trust-project-hooks` 本次信任。id 重复时先加载的生效 |
| F5 | `mcp_config.py` | 用户 `mcp.yaml` < `.mcp.json`（需 `import_mcp_json: true`）< 项目 `mcp.yaml`，同名整项覆盖；`_read_mcp_json` 已能把 Claude Code 的 `mcpServers` 格式转换过来，并展开 `${VAR}` / `${VAR:-默认值}` |
| F6 | `skills/__init__.py`、`docs/reference/skills.md` | Skill 正文支持 `$ARGUMENTS`、`argument-hint`、`allowed-tools`、`disable-model-invocation`，与 Claude Code 的命令文件（`commands/*.md`）字段一致；Skill 名同时是命令名，只能是 `[a-z0-9-]`，不能含 `:` |
| F7 | `cli.py`、`teams/process.py`、`doctor.py` | Skill、子 Agent、MCP 在 `cli.py` 中加载，团队成员进程在 `teams/process.py` 中再加载一次子 Agent，Hook 在 `hooks/engine.py` 中加载；四个加载函数都接受 `root` 和 `home` |
| F8 | `launcher.py` | 子命令表 `SUBCOMMANDS = ("setup", "doctor", "models", "key")`，子命令不导入界面和 SDK |
| F9 | 本机 | git 2.45（Windows）。GitHub Actions 的三个系统都自带 git |
| F10 | Claude Code 插件 | 根目录约定：`.claude-plugin/plugin.json`（可选，只有 `name` 必填）、`skills/<名称>/SKILL.md`、`commands/*.md`、`agents/*.md`、`hooks/hooks.json`、`.mcp.json`，另有 `bin/`、`.lsp.json`、`output-styles/`、`monitors/`、`themes/`、`workflows/`、`settings.json`。清单中 `skills` 追加目录，`commands`、`agents` 替换默认目录，`hooks`、`mcpServers` 与默认文件合并；路径必须以 `./` 开头且不能离开插件根目录。组件名加插件名前缀（`deploy-tools:reviewer`）。组件中可以写 `${CLAUDE_PLUGIN_ROOT}`（插件安装目录）、`${CLAUDE_PLUGIN_DATA}`（`~/.claude/plugins/data/<id>/`，跨版本保留）、`${CLAUDE_PROJECT_DIR}`；`userConfig` 声明需要用户填写的值，用 `${user_config.KEY}` 引用 |
| F11 | Claude Code 安装 | 只从 marketplace 安装（`claude plugin install 名称@marketplace`），marketplace 是含 `.claude-plugin/marketplace.json` 的仓库，条目的 `source` 可以是仓库内的相对路径，也可以是另一个 git 仓库。范围 user / project / local 写入对应的 settings 文件；`--plugin-dir` 只在本次会话加载本地目录 |
| F12 | pi 的包 | `pi install` 接受 npm、git（`@ref` 固定版本）、URL（按 git 处理）、本地路径（原地加载，不复制）；个人安装记在 `~/.pi/agent/settings.json`，`-l` 记在项目的 `.pi/settings.json`，项目中的包在信任项目后才安装和加载；标签和提交是固定版本，`update` 不移动它们。约定目录 `skills/` 与 Agent Skills 相同 |

## 需求

### D2：读取 .claude 目录（C）

| # | 需求 | 验收 |
| --- | --- | --- |
| C1 | `config.yaml` 顶层新增 `claude_directories`（默认 `false`）。为 `true` 时，Skill 另外读取 `~/.claude/skills/`、`.claude/skills/`，子 Agent 另外读取 `~/.claude/agents/`、`.claude/agents/` | 开启后 `/skills`、`/agents` 列出 `.claude` 中的定义，来源分别标为用户、项目 |
| C2 | 同一级中的优先级：`.claude` < `.agents` < `.mosscode`（MossCode 自己的目录最优先），被覆盖的版本照常列出 | 同名时 `.mosscode` 的生效 |
| C3 | 子 Agent 的 `model` 是 `sonnet`、`opus`、`haiku`、`fable`、`inherit`，或以 `claude-` 开头，并且没有同名的 Provider 时，按继承处理（使用当前模型），不再报错；`/agents <名称>` 注明“model: sonnet 不是这里的 Provider，使用当前模型”。对所有来源生效，不只 `.claude` | Claude Code 写的 `model: sonnet` 子 Agent 能运行 |
| C4 | `claude_directories` 为 `false` 而 `.claude/agents/` 或 `.claude/skills/` 中有定义时，启动区提示一次“发现 .claude 中的 N 个子 Agent / Skill，在 config.yaml 中设置 claude_directories: true 即可使用”；`-p` 中不提示 | |

### D3：插件（P）

一个插件是一个目录，按约定存放组件。来源是 git 仓库或本机目录。

**插件内容**

| # | 需求 | 验收 |
| --- | --- | --- |
| P1 | 插件根目录可以有 `.claude-plugin/plugin.json`（Claude Code 清单）。读取其中的 `name`、`version`、`description`、`skills`、`commands`、`agents`、`mcpServers` 和路径规则（F10）；`hooks` 等其他字段不读取，在插件详情中列为“未支持” | Claude Code 的插件仓库可以直接安装 |
| P2 | 没有清单时按约定目录读取，名称取 git 仓库名或目录名 | |
| P3 | 读取的组件：`skills/<名称>/`（与 Agent Skills 相同，`tool.json` 照常可用；根目录有 `SKILL.md` 且没有 `skills/` 时整个插件是一个 Skill）；`commands/*.md`（每个文件作为一个只能由用户调用的 inline Skill，名称取文件名，`description`、`argument-hint`、`allowed-tools` 照用，`model` 忽略）；`agents/*.md`（只读这一层，与 C3 相同处理 `model`）；`.mcp.json`（Claude Code 格式）和 `mcp.yaml`（MossCode 格式）；`hooks.yaml`（MossCode 格式） | 一个包含全部五种组件的测试插件，安装后都能用 |
| P4 | Claude Code 的 `hooks/hooks.json` 不加载（格式与 MossCode 的 Hook 不同，见评审 D4）；`bin/`、`.lsp.json`、`output-styles/`、`monitors/`、`themes/`、`workflows/`、`settings.json`、`userConfig` 也不加载。这些在安装时和 `/plugins` 中列为“未支持的组件”，不影响其他组件 | |
| P5 | 插件组件中的 `${MOSSCODE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_ROOT}` 替换为插件目录，`${MOSSCODE_PLUGIN_DATA}` / `${CLAUDE_PLUGIN_DATA}` 替换为 `~/.mosscode/plugins/data/<名称>/`（第一次用到时创建，更新时保留），`${CLAUDE_PROJECT_DIR}` 替换为项目根目录。替换发生在 Skill、命令和子 Agent 的正文，MCP 的 `command`、`args`、`env`、`url`、`headers`，Hook 的命令中；Hook 命令、MCP stdio 服务器和插件 Skill 的专属工具另外以环境变量得到前两个值 | 插件的 MCP 服务器用 `${CLAUDE_PLUGIN_ROOT}/server.py` 能启动 |
| P6 | 引用 `${user_config.…}` 的组件不加载，说明“插件需要填写配置（userConfig），MossCode 不支持” | |

**名称与优先级**

| # | 需求 | 验收 |
| --- | --- | --- |
| P7 | 插件的组件不加前缀，按原名注册（见 Q2）。优先级：内置 < 插件 < 用户级 < 项目级，用户和项目自己的定义总是覆盖插件中的同名定义；两个插件中同名时，`plugins.yaml` 中靠后的覆盖靠前的。被覆盖的版本照常在 `/skills`、`/agents` 中列出，来源显示为“插件 名称” | 同名时项目级的 Skill 生效，`/skills x` 列出被覆盖的插件版本 |
| P8 | MCP：插件 < 用户 `mcp.yaml` < `.mcp.json` < 项目 `mcp.yaml`。Hook：按 用户 < 插件 < 项目 < local 的顺序加载，id 重复时先加载的生效（现有规则） | |

**安装与管理（用户级）**

| # | 需求 | 验收 |
| --- | --- | --- |
| P9 | `moss plugin install <来源> [--ref 分支/标签/提交] [--name 名称] [-y]`。来源可以是：git 地址（`https://`、`ssh://`、`git@`）；`owner/repo`（本机没有这个目录时视为 GitHub 仓库）；本机目录（原地使用，不复制，适合开发插件）。git 来源克隆到 `~/.mosscode/plugins/repos/<名称>/` | 从本机 git 仓库和本机目录都能安装 |
| P10 | 仓库根目录不是插件、但有 `.claude-plugin/marketplace.json` 时，视为 marketplace：`<来源>#<插件名>` 安装其中一个；不写 `#` 时列出其中的插件和描述，退出码 1。条目的 `source` 支持仓库内的相对路径（安装同一个仓库的子目录）和另一个 git 仓库（`github`、`url` 两种写法）；npm 等其他来源说明不支持。条目中直接声明的组件字段不读取，只读插件目录本身（见 Q3） | 从一个 marketplace 仓库安装其中的插件 |
| P11 | 安装前列出插件内容：名称、版本、来源和提交、每个 Skill / 命令 / 子 Agent 的名称、每个 MCP 服务器的启动命令或地址、每条 Hook 的事件和命令、未支持的组件；然后询问“安装？[y/N]”。`-y` 跳过询问；stdin 不是终端又没有 `-y` 时不安装，退出码 1 | 选 N 后磁盘上没有留下任何文件 |
| P12 | 名称只能是小写字母、数字和 `-`，以字母或数字开头，最长 64 个字符（与 Claude Code 的建议一致）；不合法或与已安装的插件重名时说明原因，可用 `--name` 指定 | |
| P13 | 安装记录写在 `~/.mosscode/plugins.yaml`（来源、`ref`、仓库内路径、是否启用），用户可以手动编辑；提交以克隆目录为准。启动时不访问网络：记录中有、磁盘上没有的插件提示“运行 moss plugin update <名称>” | |
| P14 | `moss plugin list`：每个插件的名称、版本、来源、提交（前 7 位）、是否启用、组件数量和问题 | |
| P15 | `moss plugin update [名称] [-y]`：git 来源按记录的 `ref` 拉取；`ref` 是标签或提交时不移动（与 pi 相同），是分支或未指定时更新到远端最新。有变化时列出新旧提交和组件的增减（尤其是新增或改变的 Hook 命令、MCP 命令），询问后替换；本机目录来源无需更新 | 更新到新提交后新增的 Skill 可用 |
| P16 | `moss plugin remove <名称>` 删除记录、克隆目录和数据目录；本机目录来源只删除记录 | |
| P17 | `moss plugin disable|enable <名称>`：只改记录中的启用状态，停用的插件不加载 | |
| P18 | 对话中 `/plugins` 列出已安装的插件（同 P14），`/plugins <名称>` 显示组件和问题。安装、更新、删除只在命令行中进行（需要联网和确认）；变化在下一次启动 moss 时生效，界面中提示这一点 | |
| P19 | `moss doctor` 增加插件一节：git 是否可用（有 git 来源的插件时）、每个插件是否完整、组件问题 | |
| P20 | 团队成员和子 Agent 使用与主会话相同的插件（成员进程按同样的规则加载） | |

**项目级插件（第二批，见 Q4）**

| # | 需求 | 验收 |
| --- | --- | --- |
| P21 | `moss plugin install … --project` 把声明写入 `.mosscode/plugins.yaml`（随仓库共享，只含来源、`ref`、路径），克隆位置与用户级相同 | |
| P22 | 别人克隆仓库后，启动时提示“项目声明了 N 个插件，运行 moss plugin install 安装”；`moss plugin install` 不带参数时安装项目声明的全部插件（逐个显示内容并确认） | |
| P23 | 项目级插件与项目 Hook 一样需要确认：界面中第一次加载时弹出面板列出插件内容，“信任并启用”记住声明内容和各插件提交的 SHA-256；声明或提交变化后重新确认。`-p` 中未确认的不加载，stderr 说明原因；`--trust-project-plugins` 本次信任 | 修改 `.mosscode/plugins.yaml` 后再次询问 |
| P24 | 同名时项目级插件覆盖用户级插件 | |

## 设计

### 模块

| 位置 | 内容 |
| --- | --- |
| `src/mosscode/plugins.py`（新） | `PluginRecord`（来源、ref、路径、启用）和读写 `plugins.yaml`；`read_plugin(directory)` 解析清单和约定目录，得到 `Plugin`（名称、版本、根目录、各组件的位置、未支持的组件、问题）；`installed(home, root)` 返回启用的插件列表，供四个加载函数使用；变量替换 |
| `src/mosscode/plugin_command.py`（新） | `moss plugin` 子命令：解析来源、调用 git（`subprocess`，不引入新依赖）、marketplace 解析、显示内容和确认、写记录。与 `key_command.py` 一样不导入界面和 SDK |
| `skills/__init__.py`、`agents/__init__.py` | `Source` 增加 `plugin`（`SOURCE_LABELS` 为“插件”）；`skill_dirs`、`agent_dirs` 接受插件目录和 `claude_directories`，按 C2、P7 排序；`commands/*.md` 转为 inline Skill；子 Agent 的 `model` 别名处理（C3）放在 `parse_agent` 之后、运行之前，记一条说明 |
| `hooks/config.py` | `hook_paths` 在用户文件之后加入每个插件的 `hooks.yaml`，来源为 `plugin`；用户级插件视为已信任 |
| `mcp_config.py` | `load_mcp_settings` 先读插件的 `.mcp.json`（复用 `_read_mcp_json`）和 `mcp.yaml`，再按现有顺序覆盖 |
| `config.py` | `Settings.claude_directories: bool = False` |
| `cli.py`、`teams/process.py`、`hooks/engine.py`、`doctor.py` | 读取一次插件列表，传给加载函数；新增 `/plugins` 命令 |
| `launcher.py` | `SUBCOMMANDS` 加 `plugin` |

### 安装流程

1. 解析来源：存在的本机目录 → 本机；`https://` / `ssh://` / `git@` → git；`owner/repo` → `https://github.com/owner/repo.git`；`#名称` 后缀为 marketplace 中的插件。
2. git 来源克隆到 `~/.mosscode/plugins/.tmp/<随机>/`：指定分支或标签时 `--depth 1 --branch`；指定提交时完整克隆后 `checkout`。
3. 根目录是插件（有清单或任一约定组件）就用它；否则读 `marketplace.json`（P10）。
4. `read_plugin` 解析，显示内容并确认（P11）。
5. 移动到 `repos/<名称>/`，写入 `plugins.yaml`。任一步失败或取消都删除临时目录。

目录名只用插件名，避免 Windows 的路径长度限制。

### 记录格式

```yaml
# ~/.mosscode/plugins.yaml
plugins:
  - name: review-tools
    source: https://github.com/example/review-tools.git
    ref: v1.2.0            # 可选：分支、标签或提交
  - name: formatter
    source: https://github.com/example/marketplace.git
    path: plugins/formatter   # marketplace 中的子目录
  - name: my-plugin
    source: F:/dev/my-plugin  # 本机目录，原地使用
    enabled: false
```

### 安全

- 插件中的 Hook 和 MCP 服务器会以用户的权限运行程序，所以安装和更新前列出这些命令并确认（P11、P15）；用户级插件经过这次确认后视为可信，与用户级 Hook 相同。
- 项目级插件来自别人的仓库，按 P23 单独确认，与项目 Hook 的做法一致。
- 插件 Skill 的专属工具、MCP 工具照常按权限档位审批，插件不改变权限规则。
- 启动时不联网、不自动安装或更新。

## 分批

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| 12A | C1–C4，P1–P20 | 用户级插件和 `.claude` 目录可以独立使用和验收 |
| 12B | P21–P24 | 项目级插件需要信任面板和声明同步，依赖 12A 的加载逻辑 |

12B 之后再做 E2（容器运行文档）。

## 不做

- npm 来源、插件市场的浏览和搜索、自动更新。
- Claude Code 的 `hooks/hooks.json`（评审 D4）、`bin/`、LSP、输出样式、监视器、主题、工作流、`settings.json`、`userConfig`；pi 的扩展（TypeScript）和提示模板。
- 插件之间的依赖（Claude Code 的 `dependencies`）。
- 组件名加插件名前缀（Q2 选替代方案时再做）。
- 在对话中安装插件。

## 待定问题

| # | 问题 | 推荐 | 替代 |
| --- | --- | --- | --- |
| Q1 | 插件格式 | 兼容 Claude Code 的插件布局（P1–P3），另加 MossCode 自己的 `hooks.yaml`、`mcp.yaml`：现成的 Claude Code 插件仓库可以直接装，MossCode 用户也能用熟悉的格式写插件 | 只用评审原案的 MossCode 约定（`skills/`、`agents/`、`hooks.yaml`、`mcp.yaml`）：实现更简单，但 Claude Code 的插件要改造后才能用 |
| Q2 | 组件命名 | 不加前缀，按 P7 的优先级覆盖：命令短，Skill 名就是命令名的规则不变；冲突在 `/skills`、`/agents` 中可见 | 像 Claude Code 一样加前缀（`/review-tools:check`）：不会冲突，但要放宽命令名规则，输入更长，模型看到的 Skill 名也变长 |
| Q3 | marketplace 仓库 | 支持（P10），只认相对路径和 git 来源：大部分 Claude Code 插件以这种方式发布 | 不支持，只能安装根目录就是插件的仓库：实现更简单，但很多插件装不了 |
| Q4 | 项目级插件 | 分到 12B，先做用户级：信任面板和声明同步工作量较大，先让用户级插件被用起来 | 与 12A 一起做；或者不做项目级，团队共享靠 README 说明安装命令 |
| Q5 | `.claude` 目录默认是否读取 | 默认不读，`claude_directories: true` 开启，与 `import_mcp_json` 一致（评审的原方案）；未开启但发现定义时提示一次（C4） | 默认读取：同时用两种工具的人不用设置，但 `.claude` 中为 Claude Code 写的定义（例如依赖它特有工具的）会出现在 MossCode 里 |
| Q6 | 安装和更新时是否确认 | 列出内容并询问（P11、P15），`-y` 跳过 | 不询问，直接安装：更快，但 Hook 和 MCP 命令在用户没看到的情况下就会运行 |

## 验收

自动测试：在临时目录中用 `git init` 建插件仓库和 marketplace 仓库（不访问网络），覆盖安装、`--ref`、marketplace、取消、更新（分支移动、标签不动）、删除、停用、四类组件加载、变量替换、优先级、`claude_directories` 开关和 `model` 别名；子命令不导入界面（沿用现有的导入检查）。

手动验收（Windows Terminal，真实模型），编号接在 M157 之后：

| # | 操作 | 期望 |
| --- | --- | --- |
| M158 .claude 目录 | 在测试项目的 `.claude/agents/` 放一个 `model: sonnet` 的子 Agent，`.claude/skills/` 放一个 Skill；先不开启，再设置 `claude_directories: true` | 未开启时启动区提示；开启后 `/agents`、`/skills` 列出它们，让模型使用这个子 Agent 能正常运行（用当前模型） |
| M159 安装本机插件 | `moss plugin install <测试插件目录>`，内含 Skill、命令、子 Agent、MCP（`${CLAUDE_PLUGIN_ROOT}`）、`hooks.yaml` 和一个 `hooks/hooks.json` | 安装前列出全部内容，`hooks/hooks.json` 列为未支持；确认后 `moss plugin list` 显示；启动后五种组件都能用 |
| M160 从 git 安装与更新 | 从本机 git 仓库安装（`--ref` 一个标签），再在仓库中新增一个 Skill 并打新标签；`moss plugin update` | 标签固定时不更新；改记录为分支后更新，列出新旧提交和新增的 Skill |
| M161 marketplace | 从一个公开的 Claude Code marketplace 仓库（验收时选定）不带 `#` 安装，再带 `#插件名` 安装 | 第一次列出可选插件，退出码 1；第二次安装成功，其中的 Skill 和子 Agent 可用 |
| M162 覆盖与停用 | 在 `.mosscode/skills/` 中放一个与插件同名的 Skill；之后 `moss plugin disable` 插件 | 项目级版本生效，`/skills 名称` 列出被覆盖的插件版本；停用后插件的组件都不出现 |
| M163 删除 | `moss plugin remove` | 记录、克隆目录和数据目录都删除；本机目录来源的目录本身保留 |
| M164 项目级插件（12B） | `--project` 安装后，在另一个用户目录（`HOME` 指向临时目录）中启动 | 提示安装；安装后弹出信任面板，信任后加载；修改 `.mosscode/plugins.yaml` 后再次询问 |
