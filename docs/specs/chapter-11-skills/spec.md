# 第十一章：Skill 系统

日期：2026-10-26。状态：已实现（T1–T8），见 `docs/reference/skills.md`。

来源：课程第 11 章（理论、Python 源码解析、实战演练中的 Python Spec），用户的初步设计（三级存放、两阶段加载、两种执行模式、工具白名单、目录型 Skill、内置 commit / review / test），第十章规格中留给本章的事项（用户自定义命令、模板变量、隐藏命令 R4、自定义命令冲突时跳过并提示），以及 2026-10-26 对照现有代码的调查。本规格按 MossCode 的现状调整课程方案，调整内容逐条列在“对课程方案的调整”中。

## 目标

把可复用的操作规程（SOP）从源码搬进可编辑的 Markdown 文件。每个 Skill 是一个目录，核心是一份 `SKILL.md`，还可以带上专属工具（`tool.json` 声明，目录中的脚本实现），整套作为一个能力包；增删改 Skill 不需要改代码或重装。

- 用户可以用 `/<name> [参数]` 显式调用 Skill；
- 模型平时只看到 Skill 的名称和一句话描述，判断用户的请求与某个 Skill 相符时，调用 `load_skill` 加载完整规程和专属工具（两阶段加载）；
- 需要客观视角的 Skill（如代码审查）可以声明为 `fork`：在独立的上下文中运行，只把结果交回主对话；
- 工具白名单在启动时检查拼写，fork 模式下收窄可用工具。

## 现状

| 位置 | 现在的做法 | 与本章的关系 |
| --- | --- | --- |
| `builtin_commands.py` 的 `/review` | `prompt` 命令，模板 `REVIEW_TEMPLATE` 写在源码里 | 改模板要改代码；本章把它迁成内置 Skill |
| `commands.py` 的 `Command` | 两类：`local`、`prompt`；`hidden` 字段已预留（第十章 R4） | Skill 命令作为第三类登记；`user-invocable: false` 的 Skill 用 `hidden` |
| `CommandRegistry.register` | 冲突时抛出 `CommandConflict`，内置表在导入时构建 | Skill 是用户写的，冲突应跳过并提示，不能让程序崩溃 |
| `BUILTIN` | 模块级静态注册表；`tui.py` 直接使用，`cli.py` 的 `_headless_input` 在读取设置之前用它解析 `-p` | 每次运行要在内置命令之外加上 Skill 命令，解析前就要扫描 Skill |
| `prompts.Supplement` 与各提供商 | `environment` 类补充放在可缓存前缀中（Anthropic 的 system 块、Responses 开头的 developer、Chat 开头的 system），每轮重建；其他类（`system-reminder`）只随请求发送 | 已激活 Skill 的规程用同样方式放置 |
| `Conversation._load_context` | 启动、`/clear`、恢复会话时生成 `instructions`（项目指令 + 记忆索引），作为系统提示的稳定部分 | Skill 目录（名称与描述）加在这里 |
| `MCPTool` | 按 JSON Schema 校验参数（`jsonschema`），不信任自报的只读标记，名称 `mcp__server__tool`，启动后异步注册 | 专属工具照此实现；白名单中的 MCP 工具只能在调用时检查 |
| `ExecuteCommand`（`tools/process.py`） | 启动子进程、超时（默认 120 秒，最大 600 秒）后终止进程树、截断过长输出 | 专属工具的脚本复用这些机制 |
| 权限规则 | 规则按工具名匹配；MCP 工具的参数部分以规范化 JSON 做通配匹配 | 专属工具的规则沿用 MCP 的匹配方式 |
| `PermissionPolicy.memory_dirs` | 记忆目录内的 `read_file`、`glob` 不需要审批 | Skill 目录（规程引用的参考资料）沿用同一机制 |
| `ToolRegistry` | 只有 `register`，重名时报错 | 需要 `unregister`（`/clear` 移除专属工具）和 `subset`（fork 子 Agent 的工具） |
| 会话存档 | 记录类型 session / message / compact / provider；`load_session` 忽略未知类型 | 新增 `skill` 记录，旧版本读取时自动忽略 |
| `Message.command` | 提示词命令存展开后的全文，另存用户输入的命令原文；显示、标题、记忆提取使用原文 | Skill 命令沿用 |

## 对课程方案的调整

| # | 课程方案 | 调整 | 原因 |
| --- | --- | --- | --- |
| D1 | 启动时检查 `allowed_tools` 中的全部工具，缺失就跳过该 Skill | 分两段检查：启动时检查内置工具名和本 Skill 的专属工具名，写错的跳过该 Skill 并提示；`mcp__` 开头的名称在调用时检查（W1–W4） | MCP 工具在启动后异步注册，启动时检查会误杀引用 MCP 工具的 Skill；内置和专属工具启动时就能确定，拼写错误应尽早发现 |
| D2 | 已激活 Skill 的规程“钉在环境上下文” | 新增补充类型 `active-skills`，与 `environment` 一样放在可缓存前缀中、每轮重建；不进入历史，压缩后仍在 | 与现有的补充机制一致；放在历史里会被压缩摘要掉 |
| D3 | 激活状态只在内存中 | 激活时写一条 `skill` 存档记录；恢复会话时按记录重新激活规程、重新注册专属工具 | 否则 `-c` 恢复后模型看不到规程和工具，行为与恢复前不一致 |
| D4 | 子 Agent 不经过权限检查 | 子 Agent 共用主对话的权限策略：档位、规则、本次运行的授权、审批界面都一样 | 隔离的是上下文，不是权限；否则一个 fork Skill 就能绕过审批 |
| D5 | `fork_context: full` 先调用一次模型生成摘要 | `full` 复制主对话的完整历史（压缩后的历史即当前历史），`recent` 复制最近 3 轮 | 少一次模型调用，也不会因摘要丢失细节；超出窗口时子 Agent 照常自动压缩 |
| D6 | fork 结果作为一条 assistant 消息插入主历史 | 插入一对消息：用户消息（命令原文 + 一句说明）和助手消息（子 Agent 的最终回复） | 消息必须按用户、助手交替；只插 assistant 会破坏顺序 |
| D7 | 参数没有占位符时追加 `## User Request` | 追加 `补充要求：<参数>` | 与第十章提示词命令的拼接方式一致 |
| D8 | 名称冲突、解析失败打印到 stderr | 作为启动提示显示（与项目指令的问题提示相同）；`-p` 中打印到 stderr | 交互界面中 stderr 不可见 |
| D9 | `/skill` 无参数，只列名称和描述 | `/skills`（别名 `/skill`）：无参数列出名称、来源、模式、描述；`/skills <名称>` 显示详情；`/skills reload` 重新扫描 | 来源和覆盖关系是排查“为什么用的不是我改的那份”最需要的信息 |
| D10 | 只认课程的字段名 | 同时接受 Claude Code 的写法：`allowed-tools`、`argument-hint`、`disable-model-invocation`、`user-invocable`、`context: fork`；Claude Code 的工具名（`Read`、`Bash(git:*)` 等）换算为 MossCode 的工具名；未知字段忽略 | 用户可以直接放入为 Claude Code 写的 Skill（引用了 MossCode 没有的工具时按 W3 跳过） |
| D11 | 名称只看 frontmatter | `name` 必须与目录名相同，不同时跳过并提示 | 同一层中两个目录不会争用同一个名字；目录名就是用户看到的命令名 |
| D12 | 专属工具用 `references/` 下的脚本，30 秒固定超时，直接执行 | 命令中用 `${SKILL_DIR}`、`${PYTHON}` 定位脚本和解释器；超时默认 120 秒、可配置到 600 秒；每次调用都按权限档位审批，计划模式下不可用；工具名加 `skill__<skill>__` 前缀 | 与 `execute_command` 一致；脚本是任意代码，项目中的文件不能让自己免审批；前缀避免与内置工具和其他 Skill 重名 |
| D13 | `InstallSkill` 工具 | 不做 | 涉及下载和解压，留给分发与版本管理一起设计 |

## 需求

### Skill 文件

| # | 行为 |
| --- | --- |
| F1 | 每个 Skill 是一个目录，必须包含 `SKILL.md`；可选 `tool.json`（专属工具，见“专属工具”）；其他文件（参考资料、脚本）由规程或 `tool.json` 引用 |
| F2 | `SKILL.md` 以 YAML frontmatter 开头（第一行 `---`，到下一行 `---` 结束），其后是 Markdown 正文。文件按 UTF-8 读取（容忍 BOM），超过 64 KB 不加载 |
| F3 | 字段：<br>• `name`（必填）：`[a-z][a-z0-9-]*`，最长 64 个字符，必须与目录名相同；<br>• `description`（必填）：一句话，非空；<br>• `allowed_tools` / `allowed-tools`：工具名列表，也接受逗号或空格分隔的字符串；缺省表示不限。专属工具写短名称（如 `detect`）或全名。Claude Code 的工具名按下表换算：`Read`→`read_file`、`Write`→`write_file`、`Edit`→`edit_file`、`Bash`→`execute_command`、`Glob`→`glob`、`Grep`→`grep`；`Bash(git:*)` 这类带参数的写法只取工具名，参数部分忽略；<br>• `mode`：`inline`（缺省）或 `fork`；`context: fork` 等同 `mode: fork`；<br>• `fork_context`：`none`（缺省）、`recent`、`full`，只对 fork 有效；<br>• `model`：配置中的提供商名称（与 `/model` 的参数相同），只对 fork 有效；<br>• `argument-hint`：补全菜单中命令后显示的参数提示；<br>• `disable-model-invocation: true`：不列入模型可见的目录，`load_skill` 拒绝加载；<br>• `user-invocable: false`：不显示在补全菜单和 `/help`，但仍可输入调用 |
| F4 | 字段值不合法（如 `mode: async`）时跳过该 Skill 并提示，写明文件路径和字段；未知字段忽略 |
| F5 | 正文中的 `$ARGUMENTS` 替换为调用参数（无参数时替换为空）；正文没有 `$ARGUMENTS` 且参数非空时，在末尾追加 `\n\n补充要求：<参数>` |
| F6 | 送给模型的规程前面加来源说明：`Skill <name>（目录：<绝对路径>）`，有专属工具时再加一行“专属工具：skill__x__a、skill__x__b”，让规程能用相对路径引用目录中的文件 |

### 专属工具

| # | 行为 |
| --- | --- |
| X1 | `tool.json` 是一个 JSON 数组，每项：<br>• `name`（必填）：`[a-z][a-z0-9_]*`，最长 32 个字符，在本 Skill 内唯一；<br>• `description`（必填）：非空；<br>• `input_schema`（必填）：`type` 为 `object` 的 JSON Schema，按 `jsonschema` 检查其本身合法；<br>• `command`（必填）：非空字符串数组（argv），不经过 shell；<br>• `timeout`（可选）：秒，默认 120，最大 600 |
| X2 | 对模型的工具名为 `skill__<skill 名，- 换成 _>__<name>`，例如 `skill__test__detect`；全名最长 64 个字符（各提供商的工具名上限）。描述前加“Skill <name> 的专属工具。” |
| X3 | `tool.json` 不存在时没有专属工具；存在但不合法（JSON 错误、字段缺失或不合法、Schema 不合法、全名超长、重名）时跳过整个 Skill 并提示，写明文件路径和出错的项 |
| X4 | `command` 中的 `${SKILL_DIR}` 替换为 Skill 目录的绝对路径，`${PYTHON}` 替换为运行 MossCode 的 Python 解释器。替换后第一项含路径分隔符时必须是存在的文件，否则在 `PATH` 中查找；找不到时返回错误结果，不执行 |
| X5 | 执行：工作目录为项目根目录；参数对象以 UTF-8 JSON 写入 stdin 后关闭；环境变量继承 MossCode 的，另加 `MOSSCODE_SKILL_DIR`、`MOSSCODE_PROJECT_ROOT`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。stdout 按 UTF-8 解码（无法解码的字节替换）作为工具结果；退出码非 0 时为错误结果，包含退出码、stdout 末尾和 stderr 末尾；超时后终止进程树，返回超时错误。过长的结果按现有工具结果预算处理（存入会话目录，只回传预览） |
| X6 | 执行前按 `input_schema` 校验参数（与 MCP 工具相同），不合法时返回错误结果，不审批也不执行 |
| X7 | 权限：专属工具不是只读（`tool.json` 不能声明只读），每次调用经过权限策略：`default`、`acceptEdits` 下需要审批，`bypassPermissions` 下放行，计划模式下拒绝，无界面模式下没有规则放行时拒绝。审批界面显示 Skill 名、工具名、替换后的 argv 和参数 JSON。权限规则的写法与匹配方式与 MCP 工具相同（工具名精确匹配，参数部分以规范化 JSON 做通配匹配），审批时可以选择本次运行允许 |
| X8 | 注册时机：Skill 被使用时注册它的专属工具到主对话的工具表——`load_skill`、显式 inline 调用、恢复会话时按 `skill` 记录。fork 模式下专属工具只注册到子 Agent 的工具表。下一次请求起模型可以调用 |
| X9 | 移除时机：`/clear` 移除全部专属工具；`/skills reload` 后，仍存在的 Skill 按新的 `tool.json` 重新注册，已移除的 Skill 的工具随之移除 |
| X10 | 每次使用时与 `SKILL.md` 一起重新读取 `tool.json`；读取或校验失败时使用缓存版本并提示（与 L6 相同） |

### 工具白名单

| # | 行为 |
| --- | --- |
| W1 | 启动时（和 `/skills reload` 时）检查 `allowed_tools` 中的每个名称（Claude Code 名称已换算）：是内置工具（`read_file`、`write_file`、`edit_file`、`execute_command`、`glob`、`grep`、`memory`、`exit_plan_mode`）或本 Skill 的专属工具时通过 |
| W2 | `mcp__` 开头的名称启动时不检查 |
| W3 | 其他名称（拼写错误、其他 Skill 的专属工具、MossCode 没有的 Claude Code 工具如 `WebFetch`）：跳过该 Skill，启动提示写明“Skill foo：allowed_tools 中的 xxx 不是已知工具”；`load_skill` 是系统工具，写不写都通过，也不会被收窄 |
| W4 | 调用时（显式调用、`load_skill`、fork 启动）检查全部名称当前是否可用：MCP 工具未连接或名称错误、`memory` 因关闭记忆而不存在、整个会话未启用工具时，本次调用不执行，提示缺失的工具名（`load_skill` 返回错误结果）。专属工具此时会先注册，视为可用 |
| W5 | inline 模式：规程前加一行“本 Skill 建议使用的工具：a、b”，不收窄工具（Q5）。fork 模式：子 Agent 只提供列出的工具（K3） |

### 加载与覆盖

| # | 行为 |
| --- | --- |
| L1 | 三个位置，每个子目录是一个候选：<br>1. 内置：包内 `mosscode/skills/builtin/<name>/`；<br>2. 用户级：`~/.mosscode/skills/<name>/`；<br>3. 项目级：`<项目根目录>/.mosscode/skills/<name>/`（项目根目录即启动目录，与项目指令相同） |
| L2 | 同名时项目级覆盖用户级，用户级覆盖内置；被覆盖的版本记在详情中，不另行提示。覆盖以整个目录为单位，专属工具随之替换 |
| L3 | 目录不存在时静默跳过；子目录没有 `SKILL.md`、解析失败、名称不合法、`tool.json` 不合法、白名单检查失败时跳过该目录并提示，不影响其他 Skill；被跳过的项目级 Skill 不覆盖同名的用户级或内置版本 |
| L4 | Skill 名与内置命令的名称或别名（如 `help`、`quit`、`permission`）相同时，跳过该 Skill 并提示“与内置命令 /x 冲突”；不抛出异常 |
| L5 | 扫描在启动时进行，`/skills reload` 时重新进行。`-p` 在解析输入之前扫描，使 `-p "/<name>"` 可用 |
| L6 | 每次使用（显式调用或 `load_skill`）重新读取该 Skill 的 `SKILL.md` 和 `tool.json`。读取或解析失败、或 `name` 已改变时，使用启动时缓存的版本，并提示“已使用缓存版本”。名称和描述的变化在 `/skills reload` 后才进入命令表和模型可见的目录 |
| L7 | 不扫描时（无项目上下文的内部用法，如测试中 `context_root=None`）没有任何 Skill，行为与本章之前相同 |

### 显式调用

| # | 行为 |
| --- | --- |
| E1 | 每个加载成功的 Skill 登记为一条命令：名称为 Skill 名，描述为 `description`，类型为 `skill`，参数可选，参数提示取 `argument-hint`。补全菜单和 `/help` 中描述后显示 `[skill]` |
| E2 | inline：`/<name> 参数` 先按 W4 检查、按 X8 注册专属工具，再把“来源说明 + 白名单提示 + 规程”作为一条用户消息发给模型；`Message.command` 存命令原文；写一条 `skill` 存档记录（`pinned: false`）。显示、存档、恢复、标题、记忆提取与第十章的提示词命令相同 |
| E3 | fork：见“fork 模式” |
| E4 | `-p "/<name> 参数"` 与交互界面相同：inline 发送展开后的请求；fork 运行子 Agent，输出子 Agent 的最终回复 |

### 两阶段加载

| # | 行为 |
| --- | --- |
| A1 | 启用工具且至少有一个可由模型调用的 Skill 时，系统提示的稳定部分增加“Skill”一节：说明用法（用户请求与某个描述相符时先调用 `load_skill`，再按规程执行，不要凭描述猜测规程），随后每行一个 `- <name>：<description>` |
| A2 | 每条描述最多 250 个字符，超出截断；整节超过 8000 个字符时，按名称顺序列入，放不下的不列入并提示“有 n 个 Skill 未列入模型可见目录，仍可用命令调用” |
| A3 | 新增工具 `load_skill`，参数 `{"name": string, "arguments"?: string}`。只读，计划模式可用，不受 `allowed_tools` 限制，不需要审批 |
| A4 | `load_skill` 成功时：重新读取（L6），按 W4 检查，注册专属工具（X8），替换参数（F5），按调用顺序加入当前对话的已激活列表；已激活的同名 Skill 原位刷新。工具结果只有一行确认（“已加载 Skill test，规程已放入 <active-skills>，新增专属工具 skill__test__detect”），不重复规程全文 |
| A5 | `load_skill` 失败时返回错误结果，对话继续：未知名称（列出相近的名称）、`disable-model-invocation`、白名单中的工具当前不可用、文件读取失败且没有缓存 |
| A6 | 每轮请求都带上 `active-skills` 补充，内容为各已激活 Skill 的来源说明和规程；位于可缓存前缀中、`environment` 之后。没有已激活 Skill 时不发送。多个 Skill 可以同时激活 |
| A7 | 上下文估算包括 `active-skills` 的长度；已激活规程合计超过 40000 个字符时，`load_skill` 拒绝再加载并说明原因 |
| A8 | `/clear` 清空已激活列表并移除专属工具；压缩不影响已激活列表和专属工具 |
| A9 | 存档记录 `{"type":"skill","time":…,"name":…,"arguments":…,"pinned":true|false}`：`load_skill` 写 `pinned: true`，显式 inline 调用写 `pinned: false`。恢复会话时按记录顺序重新注册专属工具，`pinned` 的重新激活规程；Skill 已不存在或不能加载时跳过，并在恢复提示中写明 |
| A10 | `load_skill` 对 fork 模式的 Skill 也只是把规程放入已激活列表（与课程相同），结果中提示“此 Skill 设计为独立运行，需要隔离时请用户输入 /<name>”（Q2） |

### fork 模式

| # | 行为 |
| --- | --- |
| K1 | 只由显式命令触发（`/<name>` 或 `-p "/<name>"`）。主对话正在运行时不能触发（与其他命令相同） |
| K2 | 子 Agent 使用新的历史：<br>• `none`：只有一条用户消息，内容为展开后的规程；<br>• `recent`：复制主历史最近 3 轮（从一条非合成的用户消息开始，不拆开工具调用和结果），再加规程；<br>• `full`：复制主历史全部消息，再加规程 |
| K3 | 子 Agent 的工具：`allowed_tools` 为空时为主对话的工具加本 Skill 的专属工具；非空时只提供列出的工具（W4 检查不通过时不启动并提示）。子 Agent 不提供 `load_skill`（不嵌套 Skill） |
| K4 | 子 Agent 的系统提示与主对话相同（项目指令、记忆索引），但不含 Skill 目录；环境补充与主对话相同 |
| K5 | 权限：共用主对话的 `PermissionPolicy`（档位、规则、本次运行的授权、审批界面）。计划模式下子 Agent 同样只能读；无界面模式下需要审批的调用同样被拒绝 |
| K6 | 模型：`model` 为空时使用主对话当前的提供商；指定时使用该提供商，名称不存在时不启动并提示 |
| K7 | 显示：对话区先显示命令原文，再显示一行“在独立上下文中运行 Skill <name>”；子 Agent 的流式回复、工具调用、审批照常显示；结束时只产生一次完成事件 |
| K8 | 结果：子 Agent 完成后，在主历史末尾加一对消息并写入存档：<br>• 用户消息：`<命令原文>\n\n（以上命令在独立上下文中运行了 Skill <name>，结果见下一条回复。）`，`command` 为命令原文；<br>• 助手消息：子 Agent 的最终回复 |
| K9 | 失败或取消（模型错误、超过轮数、Esc）：同样加一对消息，助手消息为 `[Skill <name> 未完成：<原因>]`；主对话继续可用 |
| K10 | 用量：子 Agent 的 Token 计入主对话的累计用量（状态栏、`/status`）；主对话的上下文估算只增加加入的那一对消息 |
| K11 | 子 Agent 中的自动压缩按主对话的配置进行，压缩不写入主对话的存档 |

### 权限与安全

| # | 行为 |
| --- | --- |
| S1 | 已加载 Skill 的目录加入 `PermissionPolicy` 的可读目录：其中的 `read_file`、`glob` 不需要审批（与记忆目录相同），计划模式下也可用 |
| S2 | 运行 Skill 目录中的脚本：通过专属工具时按 X7 审批，通过 `execute_command` 时与其他命令一样按档位审批 |
| S3 | inline 模式的 `allowed_tools` 只是提示和存在性检查，不放行任何审批（Q5） |
| S4 | 项目级 Skill 与项目指令一样是项目内容：只在用户调用或模型加载时进入上下文，专属工具只在 Skill 被使用后注册，每次执行都需要审批（`bypassPermissions` 和用户写的规则除外） |
| S5 | `.mosscode/skills/` 不加入受保护文件：模型可以按用户要求编写或修改 Skill，写入照常审批 |

### /skills 命令

| # | 行为 |
| --- | --- |
| C1 | `/skills`（别名 `/skill`）：列出所有 Skill（含 `user-invocable: false` 的），每行：`/<name>`、来源（内置 / 用户 / 项目）、模式（inline / fork）、描述；末行提示“输入 /<名称> 调用”。没有 Skill 时显示“没有 Skill。可以在 .mosscode/skills/<名称>/SKILL.md 中添加。” |
| C2 | `/skills <名称>`：路径、来源、被覆盖的版本、模式、`fork_context`、`model`、`allowed_tools`、专属工具（全名、命令、超时）、是否对模型可见、是否已激活、正文首行 |
| C3 | `/skills reload`：重新扫描，更新命令表和模型可见的目录（下一次请求生效），按 X9 更新专属工具，报告新增、移除和出错的 Skill。已激活的 Skill 保持激活，已移除的从激活列表中去掉 |
| C4 | 参数补全：`reload` 和所有 Skill 名称 |
| C5 | `/status` 增加一行“已激活 Skill：a、b”（没有时不显示） |

## 内置 Skill

内置三个样板，分别演示 inline、fork 和专属工具。`/review` 的提示词命令删除，由内置 Skill 代替。都不写 `$ARGUMENTS`，带参数时按 F5 追加“补充要求：<参数>”。

### commit（inline）

需要知道本次对话做了什么，所以共享上下文。

```markdown
---
name: commit
description: 把与当前任务相关的改动提交为一次 git commit（不推送）
allowed_tools: [execute_command, read_file]
argument-hint: "[对提交说明的要求]"
---

请把当前仓库中与本次任务相关的改动提交为一次 git commit。

1. 用 git status 和 git diff HEAD 查看改动；不是 git 仓库或没有可提交的改动时，直接说明并结束。
2. 用 git log -n 5 --oneline 查看最近的提交说明，沿用仓库的语言和格式。
3. 只暂存与本次任务相关的文件，用 git add <路径> 逐个列出；不要用 git add -A 或 git add .。看起来含密钥或凭据的文件（如 .env）不要暂存，并提醒用户。
4. 写一条说明改动目的的提交说明，执行 git commit。不要 --amend，不要 --no-verify，不要推送。
5. 提交钩子失败时报告原因；修复后重新提交，不改写已有提交。
6. 完成后用 git status 和 git log -n 1 确认结果并汇报。
```

`git add`、`git commit` 都按档位审批，模型不能自行提交。

### review（fork）

```markdown
---
name: review
description: 审查当前仓库中尚未提交的改动（只读），按严重程度列出问题
mode: fork
fork_context: none
allowed_tools: [read_file, glob, grep, execute_command]
argument-hint: "[范围或侧重点]"
---

请审查当前仓库中尚未提交的改动。

1. 先用 git status 和 git diff HEAD 查看改动范围；不是 git 仓库或没有改动时，直接说明并结束。
2. 阅读改动涉及的上下文代码，找出正确性问题、边界条件、错误处理、安全隐患，以及与周边代码约定不一致的地方。
3. 只审查，不修改文件，不运行会改变仓库或环境状态的命令。
4. 按严重程度从高到低列出问题，每条写出 文件:行号、问题、触发场景和修改建议；没有发现问题时明确说明。
```

正文沿用第十章的 `REVIEW_TEMPLATE`。迁移后的变化：

- `/review` 不再继承主对话的上下文，审查者看不到之前的讨论（这是 fork 的目的）；
- 子 Agent 只有只读工具和命令执行，写入和编辑工具不提供；命令仍按档位审批；
- 主历史只增加命令和审查结果两条消息；
- 用户可以在项目或用户目录放一个 `review/SKILL.md` 覆盖它。

### test（inline，带专属工具）

目录结构：

```
test/
├── SKILL.md
├── tool.json
└── scripts/
    └── detect.py
```

`tool.json`：

```json
[
  {
    "name": "detect",
    "description": "检测项目类型，返回建议的测试命令及依据（只读取项目文件，不运行测试）",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": false},
    "command": ["${PYTHON}", "${SKILL_DIR}/scripts/detect.py"],
    "timeout": 30
  }
]
```

`detect.py` 只用标准库，在工作目录（项目根目录）中按文件判断，输出 JSON 数组 `[{"kind", "command", "evidence"}]`：

| 依据 | 建议命令 |
| --- | --- |
| `pyproject.toml` 含 `[tool.pytest`，或有 `pytest.ini`、`tox.ini`、`setup.cfg` 中的 pytest 配置、`tests/` 目录 | 有 `.venv` 时用其中的解释器 `-m pytest`，否则 `python -m pytest` |
| `package.json` 的 `scripts.test` | 按锁文件选 `pnpm test`、`yarn test` 或 `npm test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |
| `pom.xml` / `build.gradle(.kts)` | `mvn test` / 有 `gradlew` 时 `./gradlew test`，否则 `gradle test` |

都没有时输出空数组。

`SKILL.md`：

```markdown
---
name: test
description: 检测项目类型并运行测试，失败时分析原因
allowed_tools: [detect, execute_command, read_file, glob, grep]
argument-hint: "[测试范围]"
---

请运行本项目的测试。

1. 先调用 skill__test__detect 获取建议的测试命令；结果为空时，查看 README、AGENTS.md 等项目说明找测试方式，仍找不到就说明并结束。
2. 有补充要求时（如只测某个文件或用例）按要求缩小范围。
3. 运行测试命令。
4. 全部通过时汇报结果；有失败时阅读失败用例和相关代码，说明原因和修改建议。用户没有要求修复时不修改文件。
```

## 设计

### 模块

| 文件 | 职责 |
| --- | --- |
| `src/mosscode/skills/__init__.py`（新增） | `SkillDef`、`SkillToolDef`、`SkillCatalog`（扫描、覆盖、白名单启动检查、问题列表、重新读取、渲染规程）、`ActiveSkills` |
| `src/mosscode/skills/builtin/{commit,review,test}/`（新增） | 三个内置 Skill；`pyproject.toml` 增加 `package-data`（`SKILL.md`、`tool.json`、`scripts/*.py`），用 `importlib.resources` 定位 |
| `src/mosscode/tools/skill.py`（新增） | `LoadSkill` 工具；`SkillTool`（专属工具：参数校验参照 `MCPTool`，子进程复用 `tools/process.py` 的启动、超时和进程树终止） |
| `src/mosscode/tools/base.py` | `ToolRegistry.unregister(name)`；`ToolRegistry.subset(names, extra=())`：共用权限策略、项目根目录和会话的视图 |
| `src/mosscode/permissions.py` | `skill_dirs` 与 `memory_dirs` 同样处理；`SkillTool` 的 `Operation`（`permission_kind = "skill"`，目标为规范化参数 JSON，预览为 argv 和参数）；规则匹配中 MCP 工具的判断扩展到 `skill__` 前缀；计划模式拒绝 |
| `src/mosscode/commands.py` | `Command.kind` 增加 `"skill"`（带 `skill` 字段）；`CommandRegistry.with_skills(catalog) -> (registry, problems)`：复制内置表，逐个登记 Skill 命令，冲突的跳过并记录 |
| `src/mosscode/builtin_commands.py` | `dispatch` 增加 skill 分支；`/skills` 命令；`/help` 显示 Skill 类型；`/status` 的已激活行；删除 `/review` 命令和 `REVIEW_TEMPLATE` |
| `src/mosscode/prompts.py` | Skill 目录一节的生成；`active_skills(...) -> Supplement` |
| `src/mosscode/providers.py` | “放在可缓存前缀中的补充类型”由 `kind == "environment"` 改为属于 `PREFIX_KINDS = {"environment", "active-skills"}` |
| `src/mosscode/agent.py` | `AgentLoop` 持有 `ActiveSkills`，每轮生成补充，估算计入；`clear` 清空 |
| `src/mosscode/conversation.py` | 持有 `SkillCatalog`；`_load_context` 加入目录一节；`use_skill`（检查、注册专属工具、写存档记录）；`reply(..., fork=)`；`run_fork`；`/clear` 移除专属工具；恢复时重新注册和激活 |
| `src/mosscode/session.py` | `skill_record`；`load_session` 收集 `skill` 记录 |
| `src/mosscode/cli.py` | 启动时扫描 Skill，构建命令表传给界面和 `-p` 解析；`-p` 的 fork 分支 |
| `src/mosscode/tui.py` | `self.commands` 改用每次运行的命令表；`/skills reload` 后替换；fork 运行时的说明行；专属工具的审批显示 |

### 数据结构

```python
@dataclass(frozen=True)
class SkillToolDef:
    name: str                 # 短名称，如 detect
    full_name: str            # skill__test__detect
    description: str
    input_schema: dict
    command: tuple[str, ...]  # 未替换的 argv
    timeout: float = 120.0


@dataclass(frozen=True)
class SkillDef:
    name: str
    description: str
    body: str
    path: Path                      # SKILL.md
    source: Literal["builtin", "user", "project"]
    mode: Literal["inline", "fork"] = "inline"
    fork_context: Literal["none", "recent", "full"] = "none"
    allowed_tools: tuple[str, ...] = ()  # 已换算；专属工具已展开为全名
    model: str | None = None
    argument_hint: str = ""
    model_invocable: bool = True
    user_invocable: bool = True
    tools: tuple[SkillToolDef, ...] = ()
    overrides: tuple[Path, ...] = ()  # 被它覆盖的同名文件

    @property
    def directory(self) -> Path: ...

    def render(self, arguments: str) -> str:
        """来源说明 + 白名单提示（inline）+ 替换参数后的正文（F5、F6、W5）。"""


class SkillCatalog:
    @classmethod
    def load(cls, root: Path, home: Path) -> "SkillCatalog": ...  # 含 W1–W3 的启动检查
    skills: dict[str, SkillDef]
    problems: tuple[str, ...]  # 启动提示
    def fresh(self, name: str) -> tuple[SkillDef, str | None]: ...  # 重新读取；失败时返回缓存版本和提示
    def missing_tools(self, skill: SkillDef, registry) -> list[str]: ...  # W4
    def catalog_section(self) -> tuple[str, str | None]: ...  # A1/A2：一节文字和截断提示


@dataclass
class ActiveSkill:
    skill: SkillDef
    text: str  # 已渲染的规程


@dataclass(frozen=True)
class ForkRequest:
    skill: SkillDef
    prompt: str  # 已渲染的规程
```

`Command` 增加 `skill: str = ""`；`kind == "skill"` 时必须有 `skill`，不需要处理函数和模板。

### 数据流

显式 inline：

1. 输入 `/name 参数` → `parse_input`（每次运行的命令表）→ `dispatch` 的 skill 分支；
2. `catalog.fresh(name)` 重新读取；按 W4 检查（专属工具视为可用，缺失时提示并结束），通过后注册专属工具；
3. `ui.submit(rendered, command="/name 参数")` → `Conversation.reply` → 与提示词命令相同的路径；写 `skill` 记录（`pinned: false`）。

显式 fork：

1. 前两步同上，专属工具只放进子 Agent 的工具表；
2. `ui.submit(rendered, command=..., fork=ForkRequest(...))` → `Conversation.reply(..., fork=)` → `run_fork`；
3. 按 K2 构造历史，按 K3 用 `registry.subset` 构造工具，按 K6 选提供商，新建 `AgentLoop` 运行；
4. 转发子 Agent 的事件（K7），累加用量（K10）；
5. 结束后按 K8/K9 在主历史末尾加一对消息并写入存档。

模型触发：

1. 系统提示中的 Skill 目录 → 模型调用 `load_skill`；
2. 工具重新读取规程，按 W4 检查，注册专属工具，加入 `ActiveSkills`，通过回调写入 `skill` 记录（`pinned: true`）；
3. 下一轮 `_model` 生成 `active-skills` 补充（提供商把它放在前缀中），工具定义中出现专属工具。

专属工具调用：

1. 模型调用 `skill__test__detect` → 按 Schema 校验参数（X6）；
2. `PermissionPolicy.authorize`：计划模式拒绝；按规则和档位放行、拒绝或弹出审批（X7）；
3. 替换 `${SKILL_DIR}`、`${PYTHON}`，在项目根目录启动子进程，参数 JSON 写入 stdin（X4、X5）；
4. stdout 作为结果，退出码非 0 或超时为错误结果。

### 事件与界面

`run_fork` 复用 `AgentLoop.run` 的事件；`Conversation` 吞掉子 Agent 的 `RunStopped`，在加入那一对消息后发出自己的完成事件，使界面只看到一次结束（K7）。界面的取消（Esc）取消子 Agent 的任务，按 K9 处理。

## 取舍

| 选择 | 采用 | 未采用 | 理由 |
| --- | --- | --- | --- |
| 显式 inline 的注入方式 | 作为用户消息发送（与提示词命令相同） | 放入已激活列表 | 显式调用是一次性的请求，存档和重放已有现成机制；钉住规程会让它影响之后所有轮次 |
| 规程放置位置 | 可缓存前缀中的补充 | 工具结果中返回全文；每次放在请求末尾 | 工具结果会被压缩摘要掉；放在末尾每轮都按全价重发规程。代价是激活时前缀变化，之后的消息缓存失效一次 |
| 白名单检查时机 | 内置和专属工具启动时检查，MCP 工具调用时检查 | 全部启动时检查；全部调用时检查 | 全部启动时检查会误杀 MCP 工具；全部调用时检查则拼写错误要等到用时才发现 |
| inline 的 `allowed_tools` | 提示 + 存在性检查 | 收窄工具；自动放行这些工具（Claude Code 的语义） | inline Skill 没有明确的结束时间，不知道何时恢复全部工具，中途换工具集还会让缓存失效、历史中出现不可用的工具；自动放行则让项目文件扩大了权限 |
| 专属工具的权限 | 每次审批，计划模式拒绝 | 直接执行（课程）；允许 `tool.json` 自报只读 | 脚本是任意代码；克隆的仓库可以借自报只读绕过审批 |
| 专属工具的注册时机 | Skill 被使用后注册，`/clear` 移除 | 启动时注册全部 | 工具定义每轮都要发送，全部注册会占上下文、增加选错工具的机会，也违背两阶段加载。代价是注册时工具定义变化，整个提示缓存失效一次 |
| 专属工具的工具名 | `skill__<skill>__<tool>` | 直接用短名称 | 与内置工具、MCP 工具和其他 Skill 不会重名，权限规则也能按前缀识别 |
| fork 的权限 | 共用主对话的策略 | 子 Agent 免审批 | 隔离上下文不等于隔离权限（D4） |
| `full` 上下文 | 复制历史 | 先生成摘要 | 少一次调用，信息不丢（D5） |
| 子 Agent 的工具实例 | `subset` 共用主对话的工具实例 | 为子 Agent 新建工具 | 读取记录（编辑前须先读）在两边共享：子 Agent 读过的文件，主对话可以直接编辑。审查类 Skill 只读，影响小；新建实例要复制 MCP 连接，成本高 |
| 冲突处理 | 跳过并提示 | 抛异常 | 用户写的文件不能让程序启动失败（第十章取舍中已约定） |
| 名称与目录名 | 必须相同 | 以 frontmatter 为准 | D11 |

## 不做

- `InstallSkill` 下载安装、Skill 市场分发与版本管理（留给后续章节）；
- 模型通过工具触发 fork、子 Agent 中再调用 Skill（第十三章 SubAgent 再设计）；
- 专属工具的沙箱隔离（脚本以 MossCode 的权限运行，靠审批把关）；
- 白名单中的通配写法（如 `mcp__server__*`）；
- Skill 启用/禁用命令（删除或改名目录即可）；
- `$ARGUMENTS[0]`、`$1` 这类按位置取参数；
- 单文件 Skill（必须是目录）、`SKILL.md` 以外的格式；
- 运行中执行 Skill 命令（与其他命令相同，运行中拒绝）；
- 监视文件变化自动重新扫描（用 `/skills reload`）。

## 已确认的决定（2026-10-26）

Q3、Q4、Q6 按用户的初步设计调整后确认，Q1、Q2、Q5 按推荐值确认。

| # | 问题 | 决定 | 未采用的备选 |
| --- | --- | --- | --- |
| Q1 | fork 模式是否在本章实现 | 本章实现，只支持显式命令触发（K1–K11）；第十三章把子 Agent 的运行部分抽成通用的 SubAgent | 推迟到第十三章，本章遇到 `mode: fork` 时按 inline 运行并提示：本章更小，但 `/review` 无法获得独立上下文 |
| Q2 | 模型用 `load_skill` 加载 fork Skill 时 | 与课程相同，把规程放入已激活列表，结果中提示需要隔离时请用户输入命令（A10） | 拒绝加载：避免在主上下文中执行本应隔离的规程，但模型无法使用这类 Skill |
| Q3 | 内置 Skill | 内置 commit（inline）、review（fork，替换第十章的 `/review` 命令）、test（inline，带专属工具 `detect`） | 只内置 review |
| Q4 | 目录型 Skill 的专属工具 | 本章实现（X1–X10），每次调用按档位审批；`InstallSkill` 不做 | 推迟到后续章节 |
| Q5 | inline 模式的 `allowed_tools` | 只提示和检查存在（W5、S3） | 在 Skill 激活期间收窄工具；或按 Claude Code 的语义放行这些工具 |
| Q6 | 白名单检查时机 | 启动时检查内置工具和专属工具，MCP 工具调用时检查（W1–W4） | 全部启动时检查；全部调用时检查 |

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `skills` 包：`SKILL.md` 与 `tool.json` 解析（F1–F6、X1–X3）、三层扫描与覆盖（L1–L4）、白名单启动检查（W1–W3）、重新读取（L6、X10）、目录一节（A1、A2） | — | `tests/test_skills.py` 的解析与扫描部分（AC1–AC3） |
| T2 | 命令：`skill` 类命令、`with_skills`、`dispatch` 分支（inline）、`/skills`、`/help` 与补全的 `[skill]`；TUI 和 `-p` 使用每次运行的命令表 | T1 | `FakeUI` 测试、`tests/test_cli.py`（AC4、AC5） |
| T3 | 两阶段加载：`load_skill`、`ActiveSkills`、`active-skills` 补充与 `PREFIX_KINDS`、估算、`/clear`、`skill_dirs`、调用时检查（W4） | T1 | 提供商请求结构测试、计划模式测试（AC6–AC8） |
| T4 | 专属工具：`SkillTool`（校验、子进程、编码、超时、错误）、`ToolRegistry.unregister`、权限（X7）、注册与移除（X8、X9） | T1 | 用测试脚本的工具测试与权限测试（AC9–AC11） |
| T5 | 存档：`skill` 记录、恢复时重新注册和激活 | T3、T4 | 存档往返与旧版本兼容测试（AC12） |
| T6 | fork：`ToolRegistry.subset`、`ForkRequest`、`run_fork`、上下文复制、提供商选择、事件、用量、失败处理；TUI 与 `-p` 接入 | T2、T4 | 用假提供商的测试（AC13–AC15） |
| T7 | 内置 Skill：commit、review、test（含 `detect.py`）、`package-data`；删除 `/review` 命令和模板，更新受影响的第十章测试 | T2、T4、T6 | AC16；全量测试 |
| T8 | 文档：新增 `docs/reference/skills.md`（格式、字段、`tool.json`、白名单、权限）；更新 README、`docs/reference/commands.md`、`docs/acceptance.md`（M69–M71 改为 fork 行为，新增本章人工验收）；本规格状态改为已实现 | T1–T7 | `tests/test_docs.py`；全量测试 |

T1 完成后，T2、T3、T4 互不依赖。

## 验收

| # | 标准 | 方法 |
| --- | --- | --- |
| AC1 | 解析：<br>• 合法文件的各字段（含 Claude Code 写法：`allowed-tools: Read, Grep, Bash(git:*)` 换算为 `read_file`、`grep`、`execute_command`，`context: fork`）；<br>• 缺 `name` 或 `description`、名称不合法、名称与目录名不同、`mode` 非法、无 frontmatter、超过 64 KB → 跳过，提示含路径；<br>• `tool.json`：合法时生成全名；JSON 错误、缺字段、Schema 不合法、`command` 为空、全名超过 64 个字符、重名 → 跳过整个 Skill；<br>• `$ARGUMENTS` 替换、无占位符时追加“补充要求”、无参数时不追加 | 单元测试 |
| AC2 | 覆盖与启动检查：<br>• 三层都有 `foo` 时取项目级，详情列出另外两份；项目级 `foo` 出错被跳过时取用户级；<br>• 项目级 `help`、`quit` 被跳过并提示冲突；<br>• `allowed_tools` 含 `read_flie`、`WebFetch`、其他 Skill 的专属工具 → 跳过并提示；含本 Skill 的 `detect` 和 `mcp__x__y` → 通过；<br>• 一个坏目录不影响其他 Skill；目录不存在时没有提示 | 单元测试（临时目录作为 home 和项目） |
| AC3 | 重新读取：执行前修改正文或 `tool.json` → 用新版本；改成非法内容或删除文件 → 用缓存版本并提示；改 `description` → `/skills reload` 前目录不变，之后改变 | 单元测试 |
| AC4 | 显式 inline：`/foo 参数` → 对话显示 `/foo 参数`；模型收到来源说明 + 白名单提示 + 规程；存档含 `command` 和 `pinned: false` 的 `skill` 记录；恢复后重放显示命令原文；白名单中的 MCP 工具未连接时不发送并提示缺失的工具名 | `FakeUI` 测试 + TUI 测试 |
| AC5 | 补全与帮助：`/` 列出 Skill 命令并带 `[skill]`；`user-invocable: false` 的不列出但可调用；`/skills`、`/skills foo`（含专属工具）、`/skills reload` 的输出；`-p "/foo x"` 发出展开后的请求；`-p "/skills"` 退出码 2 | 单元测试、`tests/test_cli.py` |
| AC6 | 两阶段：系统提示含目录一节（`disable-model-invocation` 的不在其中，超长时截断并提示）；`load_skill` 后下一次请求中 `active-skills` 位于前缀区（三种协议分别检查位置）；同时激活两个 Skill 时按顺序都在；工具结果不含规程全文；未知名称返回错误并列出相近名称；超出 40000 字符时拒绝 | 提供商请求结构测试 |
| AC7 | 计划模式下 `load_skill` 可用且不需要审批；Skill 目录中的 `read_file` 不需要审批；通过 `execute_command` 运行 Skill 目录中的脚本仍需审批 | 权限测试 |
| AC8 | `/clear` 后下一次请求不含 `active-skills` 和专属工具定义；压缩后两者仍在 | 单元测试 |
| AC9 | 专属工具注册：启动后工具定义中没有 `skill__test__detect`；`load_skill test` 或 `/test` 后出现；fork Skill 的专属工具只出现在子 Agent 的请求中；`/skills reload` 后已删除 Skill 的工具消失 | 单元测试 |
| AC10 | 专属工具执行（测试脚本）：<br>• stdin 收到参数 JSON，工作目录为项目根目录，`${SKILL_DIR}`、`${PYTHON}` 已替换，环境变量齐全；<br>• 输出中文时结果正确（Windows 下不出现 GBK 乱码）；<br>• 退出码非 0 → 错误结果含 stderr；超时 → 超时错误且进程已结束；<br>• 参数不符合 Schema → 错误结果，脚本未运行；命令找不到 → 错误结果 | 工具测试 |
| AC11 | 专属工具权限：默认档位弹出审批（显示 argv 和参数）；拒绝后脚本未运行；“本次运行允许”后同一工具不再询问；计划模式下拒绝；`bypassPermissions` 下直接执行；无界面模式下拒绝；权限配置中的规则可以放行或拒绝 `skill__test__detect` | 权限测试 |
| AC12 | 存档：`load_skill` 后有 `pinned: true` 的记录；恢复后下一次请求含该规程和专属工具；`pinned: false` 的只恢复专属工具；Skill 已删除时恢复提示写明未恢复；没有 `skill` 记录的旧存档照常加载 | 存档测试 |
| AC13 | fork：子 Agent 的请求只含 K2 规定的历史和规程；工具按 `allowed_tools` 过滤且没有 `load_skill`；`model` 选择指定提供商；工具不可用或提供商不存在时不启动并提示 | 假提供商测试 |
| AC14 | fork 结束：主历史末尾增加一对消息，存档和重放正确；累计用量包含子 Agent 的用量；模型错误、超过轮数、取消时助手消息为 `[Skill x 未完成：…]`，之后主对话可以继续 | 假提供商测试 |
| AC15 | fork 的权限：计划模式下子 Agent 写入被拒绝；默认档位下命令弹出审批；无界面模式下需要审批的调用被拒绝 | 假提供商 + 权限测试 |
| AC16 | 内置 Skill：<br>• 没有用户和项目 Skill 时 `/skills` 列出 commit（inline）、review（fork）、test（inline）；<br>• `/review 安全` 以 fork 运行，子 Agent 收到的规程末尾为“补充要求：安全”，无参数时没有这一行；项目级 `review/SKILL.md` 覆盖内置版本；<br>• `detect.py` 对 pytest、npm/pnpm/yarn、go、cargo、maven/gradle 的样例目录和空目录输出正确的 JSON；<br>• 安装后的包中三个 Skill 的文件都存在 | 单元测试 |
| AC17 | 全量测试通过；README 二级标题不变，文档链接有效 | `pytest`、`tests/test_docs.py` |

人工验收（写进 `docs/acceptance.md`）：

- 在验收项目中放一个项目级 Skill（如“接口文档检查”），用自然语言提出相符的请求，确认模型先调用 `load_skill` 再按规程执行；`/clear` 后再问，确认需要重新加载；
- 在 Skill 的 `allowed_tools` 中写错一个工具名，重启后确认启动区提示并且 `/skills` 中没有它；写一个未配置的 `mcp__x__y`，确认启动正常、调用时提示该工具不可用；
- 真实模型下运行 `/test`：审批 `skill__test__detect` 时能看到命令和参数，检测结果正确，随后按建议命令运行测试；
- 真实模型下运行 `/commit`：只暂存相关文件，`git add` 和 `git commit` 都弹出审批，不推送；
- 真实模型下运行 `/review`：显示“在独立上下文中运行”，审查者不知道之前对话中的内容，审查结果回到主对话，之后追问“第一条问题怎么修”能接上；运行中按 Esc 取消，确认主对话可以继续；
- 修改 Skill 的正文后不重启直接调用，确认使用新正文；修改描述后 `/skills reload`；
- `-c` 恢复一个加载过 Skill 的会话，问与规程相关的问题，确认规程和专属工具仍然生效；
- Windows Terminal 中检查补全菜单的 `[skill]` 标记。
