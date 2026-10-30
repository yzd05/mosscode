# 第十章：命令系统

日期：2026-10-26。状态：已实现（2026-10-26）。用户已确认（见“已确认的决定”）；实现与原设计的差异见“实现说明”。

来源：用户的初步设计（命令注册中心、斜杠解析、三类执行模式、界面控制接口、入口分流、别名与 Tab 补全、十个内置命令），以及 2026-10-26 对照现有代码的评审意见。本规格按评审调整后的方案编写，调整内容逐条列在“对初步想法的调整”中。

## 目标

以 `/` 开头的输入不经过 Agent，直接执行本地操作，或者把预设的提示词送进对话。常用操作响应快、不花 Token、行为确定；清屏、查状态这类杂事不交给模型处理。

所有命令登记在一个注册中心里。解析、分流、补全、`/help` 和无界面模式都从这一份登记表读取，加命令时只改一处。

## 现状

MossCode 已有 10 个命令。这一章主要是把散落的实现收拢成注册中心，再新增 4 个命令。

| 位置 | 现在的做法 | 问题 |
| --- | --- | --- |
| `appearance.py` 的 `COMMAND_HINTS` | 补全菜单的命令和描述 | 与 `HELP_TEXT` 分别手写，两份要同步 |
| `appearance.py` 的 `HELP_TEXT` | `/help` 的命令和按键说明 | 同上 |
| `appearance.py` 的 `command_matches` | 补全匹配；`/quit` 别名、`/model` / `/mcp` / `/permissions` 的参数补全都写成特例 | 别名和参数补全不是数据，加一个就要改函数 |
| `tui.py` 的 `action_complete_hint` | Tab 后是否进入参数补全，按命令名特判 | 同上 |
| `tui.py` 的 `_command` | 约 70 行的 `if/elif` 链，直接操作 Textual 组件 | 命令逻辑绑在界面上，测试要启动整个 Textual 应用 |
| `tui.py` 的 `on_prompt_area_submitted` | 去掉前导空白后以 `/` 开头就当命令；运行中拒绝命令并保留输入 | `/etc/hosts 怎么改` 这种以路径开头的消息发不出去 |
| `cli.py` 的 `-p` | 原样发给模型 | 无界面模式用不了任何命令 |

现有命令：`/help`、`/clear`、`/compact [说明]`、`/resume [ID]`、`/memory [名称]`、`/model [名称]`、`/plan [任务]`、`/permissions [档位]`、`/mcp [名称]`、`/exit`（别名 `/quit`）。

可以直接复用的现有机制：

- **权限档位**：共有四个，`default`、`acceptEdits`、`plan`、`bypassPermissions`。进入 `plan` 时，`PermissionPolicy.resume_mode` 会记下原来的档位。`Agent.set_mode("normal")` 退出计划模式时回到这个档位。
- **状态栏徽标**：`MODE_BADGES` 已经按档位显示“默认权限 / ⏵⏵ 接受编辑 / ⏸ 计划模式 / ⏵⏵ 跳过审批”。
- **消息标记**：`Message.synthetic` 标记程序写入的消息（压缩边界）。会话标题、记忆提取和压缩都会跳过这类消息。

## 对初步想法的调整

| # | 初步想法 | 调整 | 原因 |
| --- | --- | --- | --- |
| D1 | 十个内置命令 | 保留现有的 `/resume`、`/model`、`/mcp`、`/exit`，新增 `/do`、`/status`、`/session`、`/review`，共 14 个 | 十个的清单里没有这四个现有命令，照做会删掉已有功能 |
| D2 | 命令名 `/permission`、`/session` | 保留现有的 `/permissions`，`/permission` 作为别名。`/session` 定义为“当前会话信息”，与 `/resume`（恢复旧会话）分开 | 名称与现有命令和 Claude Code 保持一致，避免两个命令做同一件事 |
| D3 | 状态栏显示 `[DEFAULT]` / `[PLAN]` | 保留现有的四档徽标；要求是命令改变档位后立即刷新状态栏 | 两个标记无法表示 `acceptEdits` 和 `bypassPermissions`，是退步 |
| D4 | `/do` 退回执行模式 | 回到进入计划模式之前的档位（复用 `resume_mode`），不固定回 `default` | 用户从“接受编辑”进入计划模式，退出时应该回到“接受编辑” |
| D5 | 三类：纯本地、影响界面、提示词 | 分派只看两类：`local`（处理函数通过界面接口执行）和 `prompt`（展开模板后作为一条用户消息发给模型） | 几乎所有本地命令都会改界面，“纯本地”和“影响界面”在分派时没有区别；`/compact` 会调用模型，但它不是对话轮次，归为 `local` |
| D6 | 命令名冲突时直接 panic 退出 | 注册时抛出 `CommandConflict`，内置表在模块导入时构建，冲突会让程序启动失败；另加单元测试，CI 中就能发现 | Python 中对应的是未捕获异常。内置命令是静态的，测试比运行时崩溃更早发现问题 |
| D7 | 第一个空格前是命令名 | 命令名必须是 `/` 加一个单词（字母开头，只含字母、数字、`-`、`_`）。不符合的输入（如 `/etc/hosts 怎么改`）当作普通消息发给模型；`//` 开头可以强制发送 | 不然以路径开头的问题永远发不出去 |
| D8 | 命令名转小写 | 只转换命令名，参数保持原样 | 提供商名、笔记名、会话 ID 区分大小写 |
| D9 | 未命中时引导用 `/help` | 另外给出最接近的命令（“是不是想输入 /compact？”） | 打错字是最常见的未命中原因 |
| D10 | 界面控制接口：显示消息、发送用户消息、切换模式、查 Token、刷状态 | 接口只负责界面：显示、发送、刷新状态、清屏、选择面板、压缩进度、退出。切换模式、查 Token 这类状态由 `Conversation` 提供 | `/model`、`/resume` 需要选择面板，原列表里没有。状态放在界面接口里，会在两种界面中各实现一遍 |
| D11 | 提示词命令 | 规定模板、参数拼接、对话中显示什么、会话存档与恢复后显示什么，以及记忆提取如何对待 | 否则恢复会话时会显示一大段模板；后台提取器也可能把模板里的要求当成用户偏好记下来 |
| D12 | 界面接口不绑定渲染框架 | `-p` 无界面模式支持提示词命令（`mosscode -p "/review"`），本地命令在 `-p` 中报错 | 否则这个接口只有一个实现；另一个实际收益是可以用假界面测试命令 |

## 需求

### 注册中心

| # | 行为 |
| --- | --- |
| R1 | **命令定义**：每条命令登记名称（不含 `/`，小写）、别名、一行描述、用法（如 `/compact [说明]`）和类型（`local` / `prompt`）。可选项包括参数提示（补全菜单中显示在命令后）、参数要求（`none` / `optional` / `required`）、是否隐藏、参数补全函数。`local` 命令必须有处理函数，`prompt` 命令必须有模板 |
| R2 | **冲突检测**：注册时检查名称和别名的格式（`[a-z][a-z0-9_-]*`），并检查它们（转小写后）是否与已登记的任何名称或别名重复。不合格时抛出 `CommandConflict`，错误信息写明两条冲突命令的名称。内置注册表在导入时构建，冲突会让程序启动失败 |
| R3 | **单一来源**：补全菜单、`/help`、参数补全、`-p` 中的命令识别都从注册表读取；`COMMAND_HINTS` 和 `HELP_TEXT` 中的命令部分删除，按键说明保留手写 |
| R4 | **隐藏命令**：不出现在补全菜单和 `/help` 列表中，但输入完整名称仍可执行。本章内置命令都不隐藏；此选项留给调试命令和后续 Skill 系统 |

### 解析与分流

| # | 行为 |
| --- | --- |
| P1 | **空输入早返回**：只含空白时什么都不做（现有行为） |
| P2 | **识别命令**：去掉前导空白后，第一个空白字符（空格、制表符、换行）前的部分是命令词。命令词匹配 `/[A-Za-z][A-Za-z0-9_-]*` 时按命令处理，之后的部分去掉首尾空白后是参数，可以跨行 |
| P3 | **普通消息**：命令词不匹配时（如 `/etc/hosts`、`/tmp/a.py`、`/`），整段输入原样作为普通消息发给模型。不过单独的 `/` 会显示补全菜单，按 Enter 时提示“输入 /help 查看命令” |
| P4 | **强制发送**：以 `//` 开头时去掉一个 `/`，其余原样作为普通消息发给模型。用于发送 `/api 这个接口怎么设计` 这类恰好像命令的文字 |
| P5 | **大小写不敏感**：命令词转小写后，先按名称、再按别名查找；参数不转换 |
| P6 | **未命中**：显示“未知命令 /xxx。输入 /help 查看命令；要把以 / 开头的文字发给模型，在开头再加一个 /。”有相近的名称或别名（`difflib`，阈值 0.6）时，在前面加“是不是想输入 /yyy？” |
| P7 | **参数校验**：参数要求为 `none` 却带了参数时，显示“/clear 不接受参数。用法：/clear”；为 `required` 却缺少参数时，显示用法。都不调用处理函数 |
| P8 | **运行中**：任务运行期间输入命令（第十三章起 `/tasks`、`/status` 例外，立即执行），保持现有行为，即提示“运行中只能排队消息，命令请在完成后发送”并保留输入。`//` 开头的强制发送和不像命令的 `/路径` 按普通消息排队 |
| P9 | **回显**：命令执行前，在对话中显示用户输入的原文（现有行为）。提示词命令显示命令原文，不显示展开后的模板 |

### 两类命令

| # | 行为 |
| --- | --- |
| K1 | **local**：用 `(CommandContext, 参数)` 调用处理函数。处理函数通过 `ctx.conversation` 读写会话状态，通过 `ctx.ui` 显示结果，不直接访问 Textual 组件 |
| K2 | **prompt**：把参数拼进模板，作为一条用户消息发起正常的 Agent 请求。工具、权限审批、计划模式、压缩和会话存档都与普通消息相同。拼接规则：有参数时在模板末尾追加一段“补充要求：<参数>”；没有参数时只发送模板 |
| K3 | **命令标记**：提示词命令生成的用户消息记录命令原文（`Message.command`，如 `/review 安全`），并写入会话存档。<br>• 对话中显示和恢复会话时重放的是命令原文；<br>• 会话标题取命令原文；<br>• 后台记忆提取把这条消息渲染为命令原文，不读模板，模板里的要求不会被当成用户偏好；<br>• 压缩和模型请求使用展开后的全文 |
| K4 | **兼容**：没有 `command` 字段的旧存档照常读取；新字段只在有值时写入 |

### 界面控制接口

| # | 行为 |
| --- | --- |
| U1 | `CommandUI` 协议（`typing.Protocol`）只包含内置命令实际用到的操作，见“设计”中的方法表 |
| U2 | TUI 的 `MossCodeApp` 实现完整协议。无界面模式不实现 `CommandUI`，只支持 `prompt` 命令（见 H1） |
| U3 | 命令改变权限档位、模型或会话后，调用 `ui.refresh_status()`，状态栏的档位徽标、模型名和上下文比例立即更新 |
| U4 | 测试使用记录调用的 `FakeUI`，命令逻辑可以不启动 Textual 就测试 |

### 补全

| # | 行为 |
| --- | --- |
| C1 | **菜单**：输入以 `/` 开头的单行文字时，按前缀列出匹配的命令，每行显示用法和描述（现有行为）。隐藏命令不参与 |
| C2 | **别名**：输入前缀匹配到别名时，菜单显示主命令，并在描述后注明“（别名 /quit）”；选中后补全为主命令名。现有的 `/q` → `/quit` 特例删除 |
| C3 | **Tab**：菜单打开时补全高亮项；只有一项匹配时它默认高亮，按 Tab 直接补全。菜单被 Esc 关闭后，如果输入是某个命令唯一的前缀，按 Tab 仍然补全 |
| C4 | **参数补全**：命令登记了补全函数时，Tab 补全命令名后追加空格并显示参数候选。代替现有的按名称特判。内置的补全函数：`/permissions` 档位、`/model` 提供商名（多于一个时）、`/mcp` Server 名（有连接时）、`/memory` 笔记名（新增） |

### 无界面模式

| # | 行为 |
| --- | --- |
| H1 | `-p` 的文字按 P2–P5 解析。`prompt` 命令展开后执行，结果输出与普通 `-p` 相同；`stream-json` 的事件不变 |
| H2 | `-p` 中的 `local` 命令以退出码 2 结束，stderr 显示“/clear 只能在交互界面中使用。”未知命令同样以退出码 2 结束，显示 P6 的提示 |

## 内置命令

| 命令 | 别名 | 类型 | 参数 | 行为 | 现状 |
| --- | --- | --- | --- | --- | --- |
| `/help [命令]` | — | local | optional | 不带参数时列出所有非隐藏命令（用法、描述、别名）和按键；带命令名时显示该命令的用法、别名、描述和类型，提示词命令附上模板的第一行 | 改为从注册表生成；带参数为新增 |
| `/clear` | — | local | none | 现有行为不变 | 迁移 |
| `/compact [说明]` | — | local | optional | 现有行为不变 | 迁移 |
| `/resume [ID]` | — | local | optional | 现有行为不变 | 迁移 |
| `/session` | — | local | none | 显示当前会话：ID、存档路径、标题、消息数、开始时间；`persist_sessions: false` 时说明未存档；最后一行提示“/resume 恢复其他会话” | 新增 |
| `/memory [名称]` | — | local | optional | 现有行为不变；新增笔记名补全 | 迁移 |
| `/model [名称]` | — | local | optional | 现有行为不变 | 迁移 |
| `/plan [任务]` | — | local | optional | 现有行为不变：进入计划模式，带任务时把任务发给模型 | 迁移 |
| `/do [任务]` | — | local | optional | 在计划模式中时，退出到进入前的档位，显示“已退出计划模式，回到 <档位>”；带任务时随后把任务发给模型。不在计划模式中时提示“当前不在计划模式”，不发送任务。未启用工具时提示“当前会话未启用工具” | 新增 |
| `/permissions [档位]` | `/permission` | local | optional | 现有行为不变 | 迁移，新增别名 |
| `/status` | — | local | none | 见下方“/status 的内容” | 新增 |
| `/mcp [名称]` | — | local | optional | 现有行为不变 | 迁移 |
| `/review [范围或侧重点]` | — | prompt | optional | 审查尚未提交的改动，模板见下方 | 新增 |
| `/exit` | `/quit` | local | none | 退出 | 迁移 |

### /status 的内容

```
状态
  模型        DeepSeek V4.1 Flash（anthropic-messages / deepseek-flash）
  权限档位    ⏵⏵ 接受编辑（acceptEdits）
  上下文      约 12,300 / 980,000 Token（1%），自动压缩阈值 784,000；已压缩 0 次
  本会话用量  输入 45,210（缓存命中 30,100）· 输出 3,020 · 请求 6 次
  会话        20261026-101500-a1b2
  工作目录    F:\code-storage\PyCharm\MossCode
  项目指令    3 个文件（/memory 查看）
  MCP         2 个已连接，1 个失败（/mcp 查看）
  记忆        开启，自动笔记开启
```

- 上下文一行在还没有请求时显示“尚未发送请求”；`context_window` 未配置时注明“按 128K 估算”。
- 未启用工具时，权限档位一行显示“未启用工具”；未配置 MCP 时，MCP 一行显示“未配置”。

### /review 的模板

```
请审查当前仓库中尚未提交的改动。

1. 先用 git status 和 git diff HEAD 查看改动范围；不是 git 仓库或没有改动时，直接说明并结束。
2. 阅读改动涉及的上下文代码，找出正确性问题、边界条件、错误处理、安全隐患，以及与周边代码约定不一致的地方。
3. 只审查，不修改文件，不运行会改变仓库或环境状态的命令。
4. 按严重程度从高到低列出问题，每条写出 文件:行号、问题、触发场景和修改建议；没有发现问题时明确说明。
```

带参数时追加“补充要求：<参数>”，例如 `/review 只看 src/mosscode/tui.py，重点关注并发` 。`/review` 在当前权限档位下运行：模板要求不修改文件，但这只是提示；写入和命令仍然按档位审批（命令级权限控制不在本章范围）。

## 设计

### 模块

| 文件 | 职责 |
| --- | --- |
| `src/mosscode/commands.py`（新增） | `Command`、`CommandRegistry`、`CommandConflict`、`parse_input`、`CommandContext`、`CommandUI` 协议、补全匹配 |
| `src/mosscode/builtin_commands.py`（新增） | 14 个内置命令的处理函数、补全函数、`/review` 模板、`/status` 与 `/session` 的报告，以及构建好的 `BUILTIN` 注册表 |
| `src/mosscode/appearance.py` | 删除 `COMMAND_HINTS`、`command_matches` 和 `HELP_TEXT` 的命令部分；按键说明改名为 `KEY_HELP` |
| `src/mosscode/tui.py` | 入口分流改用 `parse_input`；实现 `CommandUI`；删除 `_command` 链和 `action_complete_hint` 的特判；`_mcp_report`、`_memory_report` 移到 `builtin_commands.py` |
| `src/mosscode/messages.py`、`serialization.py` | `Message.command: str \| None` 及其序列化 |
| `src/mosscode/agent.py`、`conversation.py` | `run` / `reply` 接受 `command` 参数，构造用户消息时写入 |
| `src/mosscode/session.py`、`memory_extract.py`、`tui.py` 的重放 | 标题、提取渲染和重放使用 `command` |
| `src/mosscode/cli.py` | `-p` 按 H1、H2 处理 |

### 数据结构

```python
@dataclass(frozen=True)
class Command:
    name: str                                   # 小写，不含 "/"
    description: str                            # 一行，用于菜单和 /help
    usage: str                                  # "/compact [说明]"
    kind: Literal["local", "prompt"]
    aliases: tuple[str, ...] = ()
    argument: Literal["none", "optional", "required"] = "none"
    argument_hint: str = ""                     # 菜单中命令后的灰色提示，如 "[说明]"
    hidden: bool = False
    handler: Callable[[CommandContext, str], Awaitable[None]] | None = None   # local
    template: str = ""                                                          # prompt
    complete: Callable[[CommandContext], Sequence[tuple[str, str]]] | None = None   # 按前缀过滤由注册表完成


@dataclass(frozen=True)
class Parsed:
    kind: Literal["empty", "message", "command", "unknown"]
    text: str = ""            # message：要发给模型的文字（已处理 //）
    command: Command | None = None
    argument: str = ""
    word: str = ""            # 用户输入的命令词原文，用于回显和未命中提示


class CommandRegistry:
    def register(self, command: Command) -> None: ...      # R2，冲突时抛 CommandConflict
    def get(self, word: str) -> Command | None: ...        # 名称或别名，大小写不敏感
    def visible(self) -> list[Command]: ...                # 非隐藏，按登记顺序
    def suggest(self, word: str) -> str | None: ...        # P6
    def matches(self, ctx, value: str) -> list[Hint]: ...  # C1–C4；Hint 为 (value, description, label)
    def takes_completion(self, ctx, value: str) -> bool: ...  # C4：Tab 后是否追加空格


def parse_input(text: str, registry: CommandRegistry) -> Parsed: ...   # P1–P5
```

`CommandContext` 包含 `conversation`、`settings`、`registry` 和 `ui`。

### CommandUI 方法

| 方法 | 用途 | 使用者 |
| --- | --- | --- |
| `note(text, style=None)` | 在对话中显示本地结果 | 大多数命令 |
| `submit(prompt, *, command=None)` | 发起一次 Agent 请求；`command` 写入 `Message.command` | `/plan 任务`、`/do 任务`、所有 prompt 命令 |
| `refresh_status()` | 重算上下文比例并重绘状态栏 | `/plan`、`/do`、`/permissions`、`/model`、`/clear` |
| `clear_screen()` | 清空屏幕和终端滚动历史，重新显示启动区 | `/clear` |
| `choose(title, options, current, on_pick)` | 打开选择面板并立即返回；Enter 时以所选序号调用 `on_pick`，Esc 不调用 | `/model`、`/resume`（封装现有的 `_show_panel`） |
| `compact(instructions)` | 带进度行的手动压缩 | `/compact` |
| `resume(session_id)` | 带进度的恢复与重放（启动后台任务，立即返回） | `/resume` |
| `switch_model(name)` | 切换到指定提供商 | `/model` |
| `exit()` | 退出应用 | `/exit` |

读取权限档位、Token 用量、上下文估算、会话和 MCP 状态，以及切换模式、切换模型，都通过 `ctx.conversation` 完成，不放进界面接口。

### 数据流

```
Enter ─→ on_prompt_area_submitted
          ├─ 运行中 ─→ parse_input ─→ message：排队 │ command/unknown：提示并保留输入（P8）
          └─ 空闲   ─→ parse_input
                         ├─ empty    ─→ 忽略
                         ├─ message  ─→ _start_request(text)
                         ├─ unknown  ─→ 回显 + 未命中提示（P6）
                         └─ command  ─→ 回显 ─→ 参数校验（P7）
                                         ├─ local  ─→ handler(ctx, argument)
                                         └─ prompt ─→ ui.submit(展开后的模板, command=原文)
```

### 实现说明

- `choose` 改为回调：Textual 的消息处理函数中等待用户选择会阻塞消息循环，界面停止响应。
- 增加 `switch_model(name)`：创建新客户端用的 `provider_factory` 属于界面层。
- `dispatch(ctx, parsed)` 在 `builtin_commands.py` 中，界面和测试共用；处理函数抛出的 `ProviderError` 由界面显示为提示。
- P6 的提示加上了 `//` 的用法；`/help` 的用法列按最长的用法对齐（`/review [范围或侧重点]` 超过了原来的 22 列）。
- `MODE_BADGES` 从 `tui.py` 移到 `permissions.py`，`/status` 的权限档位一行与状态栏使用同一份徽标。

## 取舍

| 选择 | 采用 | 未采用 | 理由 |
| --- | --- | --- | --- |
| 命令分类 | `local` / `prompt` 两类 | 三类（再拆出“影响界面”） | 分派时只有两种路径；是否改界面由处理函数自己决定 |
| 冲突处理 | 导入时抛异常 + 单元测试 | 运行时检测、记日志后跳过 | 内置命令是开发者写的，冲突就是 bug，应该尽早失败。用户自定义命令（Skill 章节）出现冲突时应该跳过并提示，不能让程序崩溃，届时另行设计 |
| 提示词命令的存档 | 存展开后的全文，加 `command` 字段 | 只存命令原文，恢复时重新展开 | 模板将来可能修改，存全文才能让恢复的上下文与当时一致 |
| 运行中的命令 | 保持拒绝 | 允许 `/status`、`/help` 等只读命令 | 运行中插入本地输出会打乱内联模式下滚动历史的顺序，收益小 |
| `/session` | 当前会话信息 | 作为 `/resume` 的别名 | 与 `/resume` 互补，也让会话 ID 和存档路径有个查看的地方 |

## 不做

用户自定义命令、模板中的变量和动态生成提示词、命令级权限控制（以上留给 Skill 章节）；运行中执行命令；MCP prompts 作为命令；命令历史单独存储（继续使用现有的输入历史）；命令的国际化别名（如 `/帮助`）。

## 已确认的决定（2026-10-26）

以下各项均按推荐值确认。

| # | 问题 | 决定 | 未采用的备选 |
| --- | --- | --- | --- |
| Q1 | 像命令但未登记的输入（`/api 怎么设计`） | 提示未知命令，可以用 `//` 强制发送（P4、P6） | 未登记的一律当作普通消息发给模型：不会误拦，但打错命令名时会白白消耗一次请求 |
| Q2 | `/do` 不在计划模式时带了任务 | 只提示“当前不在计划模式”，不发送任务 | 照常发送任务 |
| Q3 | `/review` 的默认范围 | 未提交的改动（`git diff HEAD`） | 当前分支相对主分支的全部提交 |
| Q4 | `-p` 中的本地命令 | 退出码 2 报错（H2） | 支持 `/status`、`/session` 等只读命令，把输出打印到 stdout |

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `commands.py`：`Command`、`CommandRegistry`（注册、冲突检测、查找、建议、补全匹配）、`parse_input` | — | `tests/test_commands_stage10.py` 的注册与解析部分（AC1–AC3） |
| T2 | `builtin_commands.py`：把 10 个现有命令从 `tui._command` 迁过来，处理函数只用 `ctx`；`_mcp_report`、`_memory_report` 一并迁移 | T1 | `FakeUI` 测试；现有命令相关测试全部通过（AC4） |
| T3 | TUI 接入：入口分流、实现 `CommandUI`（`choose` 封装现有面板）、补全和 `/help` 改为读注册表，删除 `COMMAND_HINTS`、`command_matches` 和特判 | T2 | `tests/test_tui.py`、`test_interaction_stage4.py`、`test_plan_commands.py` 按新接口更新后通过（AC5、AC6） |
| T4 | `Message.command`：消息、序列化、`run` / `reply` 参数、标题、提取渲染、重放 | — | 序列化往返、旧存档兼容、标题与提取渲染测试（AC8） |
| T5 | 新命令：`/do`、`/status`、`/session`、`/review`，`/help 命令`，`/memory` 名称补全 | T2、T4 | AC7、AC8 |
| T6 | `-p` 支持提示词命令 | T1、T4 | `tests/test_cli.py`（AC9） |
| T7 | 文档：新增 `docs/reference/commands.md`；更新 README 命令表、`docs/reference/interface.md` 的命令部分、`docs/acceptance.md`；本规格状态改为已实现 | T1–T6 | `tests/test_docs.py`；全量测试 |

T1 和 T4 互不依赖，可以先做。T2 完成前不改 TUI，保证每一步都能跑全量测试。

## 验收

| # | 标准 | 方法 |
| --- | --- | --- |
| AC1 | 名称与名称、名称与别名、别名与别名（含大小写不同）重复时，`register` 抛出 `CommandConflict`，错误信息含两条命令的名称；格式不合格、`local` 缺处理函数、`prompt` 缺模板时同样抛出；`BUILTIN` 构建成功 | 单元测试 |
| AC2 | `parse_input`：<br>• 空白 → empty；<br>• `/Help` → help 命令；<br>• `/permission plan` → permissions，参数 `plan`；<br>• `/model DeepSeek V4.1 Flash` 的参数保持原样；<br>• `/etc/hosts 怎么改`、`/ 你好`、`/tmp/a.py` → message；<br>• `//api 怎么设计` → message `/api 怎么设计`；<br>• `/compcat` → unknown，建议 `/compact`；<br>• `/review\n重点看并发` 的参数为“重点看并发” | 单元测试 |
| AC3 | 补全：<br>• `/` 列出 14 个命令，不含隐藏命令；<br>• `/q` 显示 `/exit（别名 /quit）`；<br>• `/permissions ` 列出四个档位；<br>• `/memory ` 列出笔记名；<br>• 只有一个提供商时 `/model` 补全后不追加空格 | 单元测试（`matches`） |
| AC4 | 10 个现有命令的行为与迁移前一致：输出文字、状态变化、面板选择、错误提示 | 现有测试 + `FakeUI` 测试 |
| AC5 | `/clear extra` 显示“/clear 不接受参数。用法：/clear”，会话不被清空；运行中输入 `/status` 提示并保留输入；运行中输入 `//x` 进入排队 | TUI 测试 |
| AC6 | `/plan` → 状态栏显示“⏸ 计划模式”；`/do` → 回到进入前的档位（从 acceptEdits 进入则回到 acceptEdits），状态栏同步；不在计划模式时 `/do 任务` 只提示，不发请求 | TUI 测试 |
| AC7 | `/status` 各行与 `Conversation` 的数据一致，包括无请求、未启用工具、无 MCP 的情况；`/session` 显示 ID 与存档路径，`persist_sessions: false` 时说明未存档；`/help review` 显示用法、类型和模板首行 | `FakeUI` 测试 |
| AC8 | `/review 安全`：<br>• 对话显示 `/review 安全`；<br>• 模型收到模板 + “补充要求：安全”；<br>• 存档的消息含 `"command":"/review 安全"`；<br>• 恢复后重放显示 `/review 安全`，会话列表标题为 `/review 安全`；<br>• 提取器输入中是 `[用户]\n/review 安全`，没有模板文字；<br>• 没有 `command` 字段的旧存档照常加载 | 单元测试 + TUI 测试 |
| AC9 | `-p "/review"` 发出展开后的请求；`-p "/clear"` 以退出码 2 结束并输出提示；`-p "/etc/hosts 是什么"` 原样发送 | `tests/test_cli.py` |
| AC10 | 全量测试通过；README 二级标题不变，文档链接有效 | `pytest`、`tests/test_docs.py` |

人工验收（写进 `docs/acceptance.md`）：

- 真实模型下执行 `/review`，确认它只读、不改文件，并按格式输出；
- Windows Terminal 中检查补全菜单的别名显示和 Tab 行为；
- 在 `/do` 前后观察状态栏徽标。
