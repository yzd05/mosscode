# 命令

以 `/` 开头的输入不经过模型，直接执行本地操作，或者把一段规程作为一条消息发给模型。内置命令登记在 `src/mosscode/builtin_commands.py` 的 `BUILTIN` 注册表中；启动时再为每个 Skill 加一条命令（见 [Skill](skills.md)）。解析、补全菜单、`/help` 和无界面模式都读这一份登记。规格见 `docs/specs/chapter-10-commands/spec.md` 和 `docs/specs/chapter-11-skills/spec.md`。

## 内置命令

| 命令 | 别名 | 类型 | 行为 |
| --- | --- | --- | --- |
| `/help [命令]` | — | 本地 | 不带参数时列出所有命令和按键；带命令名时显示该命令的用法、描述、类型和别名，Skill 命令附上方式、来源和规程第一行 |
| `/clear` | — | 本地 | 开始新会话：清空屏幕、终端滚动历史、模型上下文和 Token 统计；权限档位、会话授权和 MCP 连接保留 |
| `/compact [说明]` | — | 本地 | 压缩上下文，说明用于指定摘要侧重点（见 [上下文管理](context.md#何时压缩)） |
| `/resume [ID]` | — | 本地 | 不带 ID 时打开会话列表，带 ID 时直接恢复（见 [会话存档与恢复](sessions.md)） |
| `/session` | — | 本地 | 显示当前会话的 ID、存档路径、标题、消息数和开始时间；`persist_sessions: false` 时说明未存档 |
| `/memory [名称]` | — | 本地 | 列出项目指令和记忆笔记；带名称时显示笔记全文（见 [记忆笔记](memory.md)） |
| `/model [名称]` | — | 本地 | 切换提供商，对话历史保留；配置了多个提供商时，不带名称会打开选择面板 |
| `/plan [任务]` | — | 本地 | 进入计划模式；带任务时随后把任务发给模型（见 [计划模式](tools.md#计划模式)） |
| `/do [任务]` | — | 本地 | 退出计划模式，回到进入前的档位（从 acceptEdits 进入就回到 acceptEdits）；带任务时随后把任务发给模型。不在计划模式时只提示“当前不在计划模式。”，不发送任务 |
| `/permissions [档位]` | `/permission` | 本地 | 查看档位说明，或切换到 default、acceptEdits、plan、bypassPermissions（见 [工具权限](permissions.md)） |
| `/status` | — | 本地 | 显示模型、权限档位、上下文估算、本会话用量、会话 ID、工作目录、项目指令、MCP、记忆和已激活 Skill 的状态 |
| `/mcp [名称]` | — | 本地 | 列出 MCP Server 的状态；带名称时列出该 Server 的工具（见 [MCP](mcp.md)） |
| `/skills [名称\|reload]` | `/skill` | 本地 | 列出 Skill；带名称时显示详情，`reload` 重新扫描（见 [Skill](skills.md#skills)） |
| `/exit` | `/quit` | 本地 | 退出并清理资源 |

除内置命令外，每个 Skill 是一条 `/<名称> [参数]` 命令，例如内置 Skill `/commit`、`/review`、`/test`（见 [Skill](skills.md#内置-skill)）。补全菜单和 `/help` 中，Skill 命令的描述后面带 `[skill]`；`user-invocable: false` 的 Skill 不列出，但输入全名仍可执行。

本地命令不请求模型，结果只显示在界面中，不进入模型上下文；`/compact` 例外，它用一次不带工具的请求生成摘要。任务运行期间不接受命令：输入命令会提示“运行中只能排队消息，命令请在完成后发送”，输入保留在框中。

## 输入如何解析

去掉前导空白后，第一个空白字符（空格、制表符或换行）之前的部分是命令词，之后的部分去掉首尾空白是参数，参数可以跨行。

| 输入 | 处理 |
| --- | --- |
| 只含空白 | 忽略 |
| 命令词匹配 `/[A-Za-z][A-Za-z0-9_-]*` 且已登记 | 执行命令。命令名不区分大小写（`/Help`、`/PERMISSION plan` 都可以），参数原样保留 |
| 命令词匹配但未登记 | 显示“未知命令 /xxx。输入 /help 查看命令；要把以 / 开头的文字发给模型，在开头再加一个 /。”有相近的名称（`difflib`，相似度 0.6 以上）时，前面加“是不是想输入 /yyy？” |
| 命令词不匹配，如 `/etc/hosts 怎么改`、`/tmp/a.py`、`/ 你好` | 整段原样作为普通消息发给模型 |
| 单独的 `/` | 打开补全菜单；按 Enter 提示“输入 /help 查看命令。” |
| 以 `//` 开头 | 去掉一个 `/` 后作为普通消息发送，例如 `//api 这个接口怎么设计` |

参数不符合命令要求时，例如 `/clear extra`，显示“/clear 不接受参数。用法：/clear”，不执行命令。

## 发给模型的命令

Skill 命令和提示词命令（`kind` 为 `prompt`，目前没有内置的此类命令）把一段文字（加上参数）发给模型：

- inline 方式的 Skill 和提示词命令把规程或模板作为一条普通的用户消息发出。工具、权限审批、计划模式、压缩和存档都和手动输入的消息相同。带参数时，提示词命令把参数以“补充要求：<参数>”附在模板后面；Skill 先替换正文中的 `$ARGUMENTS`，没有时同样追加。
- fork 方式的 Skill（如 `/review`）先显示“在独立上下文中运行 Skill <名称>”，由子 Agent 在新的历史中执行，主对话只增加命令原文和最终回复这一对消息（见 [两种执行方式](skills.md#两种执行方式)）。

规程只是提示：内置 `/review` 要求只读，但子 Agent 的写入和命令仍按当前档位审批。需要硬性保证时，先 `/plan` 进入计划模式再执行。

生成的用户消息记录了命令原文（消息的 `command` 字段，例如 `/review 安全`），并写入会话存档。界面回显、恢复会话后的重放、会话列表的标题，以及记忆提取看到的用户输入，都使用命令原文，不显示展开后的规程；模型收到的仍是完整规程。没有 `command` 字段的旧存档照常读取。

## 补全

- 输入以 `/` 开头的单行文字时，菜单按前缀列出命令，每行是用法和描述。前缀匹配到别名时，菜单显示主命令，描述后注明“（别名 /quit）”，选中后补全为主命令。
- Tab 补全高亮项；只有一项匹配时它默认高亮。按 Esc 收起菜单后，如果输入是某个命令唯一的前缀，Tab 仍然补全。
- 有参数候选的命令，Tab 补全命令名后追加空格，菜单换成参数候选：`/permissions` 的档位、`/model` 的提供商名（多于一个时）、`/mcp` 的 Server 名（有连接时）、`/memory` 的笔记名、`/skills` 的 Skill 名和 `reload`。

## 无界面模式

`-p` 的文字按同样的规则解析：

- 普通消息（包括 `/etc/hosts 是什么`，以及去掉一个 `/` 的 `//` 开头文字）照常发送。
- Skill 命令和提示词命令展开后执行，输出格式与普通 `-p` 相同。fork 方式的 Skill 先在 stderr 提示“在独立上下文中运行 Skill <名称>”，再由子 Agent 执行；Skill 指定的 `model` 不存在或所需工具不可用时不发请求，stderr 说明原因，以退出码 1 结束。
- 本地命令以退出码 2 结束，stderr 显示“/clear 只能在交互界面中使用。”
- 未知命令同样以退出码 2 结束，stderr 显示未知命令的提示。

## 添加命令

在 `BUILTIN` 中加一个 `Command`：

```python
Command("name", "一行描述", "/name [参数]", "local", aliases=("n",), argument="optional",
        argument_hint="[参数]", handler=_name, complete=_candidates)
```

- `kind` 为 `local` 时必须有 `handler`，签名为 `async (ctx: CommandContext, argument: str) -> None`。为 `prompt` 时必须有 `template`。`skill` 只由 Skill 扫描生成（`skill` 字段为 Skill 名），不要手写；新增可复用的提示词时写一个 Skill 更方便。
- `argument` 为 `none`、`optional` 或 `required`，分派前校验。
- `complete` 返回参数候选 `(值, 描述)` 的列表；返回空列表时 Tab 不追加空格。
- `hidden=True` 的命令不出现在菜单和 `/help` 中，但输入全名仍可执行。
- 名称和别名必须匹配 `[a-z][a-z0-9_-]*`，并且（转小写后）互不重复。违反时导入模块就抛出 `CommandConflict`，程序无法启动。

处理函数只通过 `ctx` 工作。读写状态（权限档位、会话、用量、MCP）用 `ctx.conversation`；改界面用 `ctx.ui`。`ctx.ui` 是 `CommandUI` 协议，由 `MossCodeApp` 实现：

| 方法 | 用途 |
| --- | --- |
| `note(text, style=None)` | 在对话中显示本地结果 |
| `submit(prompt, *, command=None, fork=None)` | 发起一次 Agent 请求；`command` 写入消息的 `command` 字段；`fork` 为 `ForkRequest` 时在子 Agent 中运行该 Skill |
| `refresh_status()` | 重算上下文比例并重绘状态栏，在改变档位、模型或会话后调用 |
| `clear_screen()` | 清空屏幕和终端滚动历史，重新显示启动区 |
| `choose(title, options, current, on_pick)` | 打开选择面板并立即返回；用户按 Enter 时以所选序号调用 `on_pick`，按 Esc 时什么都不调用 |
| `compact(instructions)` | 带进度行的手动压缩 |
| `resume(session_id)` | 带进度的恢复与重放 |
| `switch_model(name)` | 切换到指定提供商 |
| `exit()` | 退出应用 |

`choose` 用回调而不是等待返回值：Textual 的消息处理函数中等待用户输入会让界面停止响应。测试用一个记录调用的假实现（见 `tests/test_commands_stage10.py` 的 `FakeUI`），不启动界面就能测试命令逻辑。
