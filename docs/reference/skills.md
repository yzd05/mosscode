# Skill

Skill 是写在 Markdown 文件里的可复用操作规程（SOP），可以带上自己的脚本工具。增删改 Skill 不需要改代码或重装。

## 存放位置

每个 Skill 是一个目录，目录名就是 Skill 名，也是命令名：

| 位置 | 来源 |
| --- | --- |
| 包内 `mosscode/skills/builtin/<名称>/` | 内置 |
| `~/.mosscode/skills/<名称>/` | 用户级 |
| `<启动目录>/.mosscode/skills/<名称>/` | 项目级 |

同名时项目级覆盖用户级，用户级覆盖内置，以整个目录为单位（专属工具一起替换）。被覆盖的版本在 `/skills <名称>` 中列出。

启动时扫描一次，`/skills reload` 重新扫描。扫描出错的 Skill 会被跳过，并在启动区（`-p` 中为 stderr）提示文件路径和原因，不影响其他 Skill；被跳过的项目级 Skill 不会覆盖同名的用户级或内置版本。Skill 名与内置命令或其别名相同时（如 `help`、`quit`）同样跳过，并提示“与内置命令 /x 冲突”。

每次使用时会重新读取 `SKILL.md` 和 `tool.json`，所以改了正文直接调用就能生效。读取失败时用启动时的缓存版本，并提示“已使用缓存版本”。名称和描述的变化要 `/skills reload` 后才进入命令表和模型可见的目录。

## SKILL.md

```markdown
---
name: api-check
description: 按团队接口规范检查 HTTP 接口的改动
allowed_tools: [read_file, grep, glob]
argument-hint: "[接口路径]"
---

请按 reference/api-style.md 检查 $ARGUMENTS 的接口定义……
```

文件以 YAML frontmatter 开头，按 UTF-8 读取（容忍 BOM），超过 64 KB 不加载。

| 字段 | 说明 |
| --- | --- |
| `name` | 必填。`[a-z][a-z0-9-]*`，最长 64 个字符，必须与目录名相同 |
| `description` | 必填。一句话，模型据此判断何时加载；超过 250 个字符的部分在模型可见的目录中截断 |
| `allowed_tools`（或 `allowed-tools`） | 工具名列表，也可以写成逗号或空格分隔的字符串，见[工具白名单](#工具白名单) |
| `mode` | `inline`（缺省）或 `fork`；`context: fork` 等同 `mode: fork`，见[两种执行方式](#两种执行方式) |
| `fork_context` | `none`（缺省）、`recent`、`full`，只对 fork 有效 |
| `model` | 配置中的提供商名称（与 `/model` 的参数相同），只对 fork 有效；缺省使用当前提供商 |
| `argument-hint` | 补全菜单中命令后显示的参数提示 |
| `disable-model-invocation: true` | 不列入模型可见的目录，模型不能加载，只能由用户输入命令调用 |
| `user-invocable: false` | 不出现在补全菜单和 `/help` 中，仍可输入全名调用，也仍可由模型加载 |

字段值不合法（如 `mode: async`）时跳过该 Skill 并提示；不认识的字段忽略。为 Claude Code 写的 Skill 可以直接放进来：`Read`、`Write`、`Edit`、`Bash`、`Glob`、`Grep` 会换算成 MossCode 的工具名，`Bash(git:*)` 这类写法只取工具名。

**参数**：正文中的 `$ARGUMENTS` 替换为调用参数；正文没有 `$ARGUMENTS` 而调用时带了参数，就在末尾追加“补充要求：<参数>”。

**来源说明**：发给模型的规程前面加一行 `Skill <名称>（目录：<绝对路径>）`，有专属工具时再加一行列出它们，所以规程可以用相对路径引用目录中的其他文件（参考资料、模板、脚本）。模型读取正在使用的 Skill 目录中的文件不需要审批。

## 调用方式

**用户调用**：输入 `/<名称> [参数]`。补全菜单和 `/help` 中，Skill 命令的描述后面带 `[skill]`。对话中显示、存档和恢复后重放的都是命令原文，模型收到的是展开后的规程。`-p "/<名称> 参数"` 同样可用。

**模型加载**：启用工具时，系统提示中有一节“Skill”，列出每个可由模型加载的 Skill 的名称和描述（整节最多 8000 个字符，放不下的不列入，仍可用命令调用）。模型判断用户的请求与某个描述相符时，调用 `load_skill` 工具，完整规程随后出现在每次请求的 `<active-skills>` 补充中，直到 `/clear`：

- 规程放在可缓存前缀中、`<environment>` 之后，不进入对话历史，所以压缩不会把它摘要掉；
- 可以同时加载多个 Skill，已加载的同名 Skill 再次加载时原位刷新（例如换参数）；
- 已加载的规程合计不超过 40000 个字符，超过时 `load_skill` 返回错误；
- `load_skill` 只读、不需要审批，计划模式下也可用；
- 加载记录写入会话存档，`-c` / `/resume` 恢复会话后规程和专属工具照样生效；Skill 已不存在时在恢复提示中写明。

## 两种执行方式

**inline**（缺省）：显式调用时，规程作为一条用户消息发给当前对话，模型在同一上下文中执行，工具和审批与普通消息相同。

**fork**：只能由用户用命令触发（`load_skill` 加载 fork Skill 时只是把规程放入 `<active-skills>`，并提示“需要隔离时请用户输入 /<名称>”）。界面先显示一行“在独立上下文中运行 Skill <名称>”，然后由一个子 Agent 在新的历史中执行：

| `fork_context` | 子 Agent 的历史 |
| --- | --- |
| `none` | 只有展开后的规程 |
| `recent` | 主对话最近 3 轮（从一条用户消息开始，工具调用和结果不拆开），再加规程 |
| `full` | 主对话的全部历史（压缩后的即为当前历史），再加规程 |

- 系统提示与主对话相同（项目指令、记忆索引），但没有 Skill 目录；子 Agent 没有 `load_skill`。
- 子 Agent 的流式回复、工具调用和审批照常显示；结束时只有一次完成。
- 完成后主对话末尾只增加一对消息：命令原文（附一句说明）和子 Agent 的最终回复。之后可以接着追问。
- 模型错误、达到轮数上限或按 Esc 取消时，同样加一对消息，回复为 `[Skill <名称> 未完成：<原因>]`，主对话可以继续。
- 子 Agent 的 Token 计入本会话用量（状态栏、`/status`）；主对话的上下文只增加那一对消息。子 Agent 需要时按主对话的配置自动压缩，压缩不写入存档。
- 运行时与 `agent` 工具的子 Agent 相同（见 [子 Agent](subagents.md)）：在 `/tasks` 中是一个前台任务（类型 `skill:<名称>`），消息写入会话目录的 `subagents/<任务 id>.jsonl`，触发 `subagent_start` / `subagent_stop` Hook。它总在前台同步运行，`Ctrl+B` 和自动转入后台对它无效。
- `fork_context` 为 `recent`、`full` 时，子 Agent 继承主对话已读文件的记录（可以直接编辑这些文件）；`none` 时从零开始，编辑前要先读取。

## 工具白名单

`allowed_tools` 分两次检查：

1. 启动时（和 `/skills reload` 时）：内置工具（`read_file`、`write_file`、`edit_file`、`execute_command`、`glob`、`grep`、`memory`、`exit_plan_mode`）和本 Skill 的专属工具通过；`mcp__` 开头的名称这时不检查；其他名称（拼写错误、别的 Skill 的专属工具、MossCode 没有的工具如 `WebFetch`）让该 Skill 被跳过并提示。
2. 调用时（命令、`load_skill`、fork 启动）：列出的工具必须当前可用。MCP Server 未连接、关闭记忆后的 `memory` 等缺失时不执行，并提示缺少哪些工具。

白名单的作用因方式而异：

- inline：只是提示，规程前加一行“本 Skill 建议使用的工具：……”。模型仍可使用全部工具。白名单不放行任何审批。
- fork：子 Agent 只拿到列出的工具；不写白名单时拿到主对话的全部工具和本 Skill 的专属工具。

## 专属工具

Skill 目录中可以放一个 `tool.json`，把目录里的脚本声明为工具：

```json
[
  {
    "name": "detect",
    "description": "检测项目类型，返回建议的测试命令",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": false},
    "command": ["${PYTHON}", "${SKILL_DIR}/scripts/detect.py"],
    "timeout": 30
  }
]
```

| 字段 | 说明 |
| --- | --- |
| `name` | 必填。`[a-z][a-z0-9_]*`，最长 32 个字符，在本 Skill 内唯一 |
| `description` | 必填，非空 |
| `input_schema` | 必填。`type` 为 `object` 的 JSON Schema |
| `command` | 必填。argv 数组，不经过 shell。`${SKILL_DIR}` 替换为 Skill 目录，`${PYTHON}` 替换为运行 MossCode 的 Python |
| `timeout` | 可选，秒，默认 120，最大 600 |

`tool.json` 不合法时（JSON 错误、字段缺失、Schema 不合法、重名、全名超长）跳过整个 Skill 并提示。

- **名称**：模型看到的工具名是 `skill__<Skill 名，- 换成 _>__<name>`，如 `skill__test__detect`，全名最长 64 个字符。
- **何时可用**：Skill 被使用后才注册（显式 inline 调用、`load_skill`、恢复会话），下一次请求起模型可以调用；fork Skill 的专属工具只给它的子 Agent。`/clear` 移除全部专属工具；`/skills reload` 后按新的 `tool.json` 重新注册，已删除的 Skill 的工具随之移除。
- **执行**：先按 `input_schema` 校验参数，不合法时不审批也不执行。工作目录为项目根目录，参数对象以 UTF-8 JSON 写入 stdin，环境变量另加 `MOSSCODE_SKILL_DIR`、`MOSSCODE_PROJECT_ROOT`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。stdout 作为结果；退出码非 0 时返回错误，附退出码和 stdout、stderr 的末尾；超时后终止整个进程树。找不到命令时不执行。

## 权限

- 专属工具运行的是任意代码，一律按有副作用处理：default 和 acceptEdits 下每次需要审批，bypassPermissions 下放行，计划模式下拒绝，`-p` 中没有规则放行时返回 approval_required。审批时显示 Skill 名、工具名、替换后的命令和参数 JSON。
- 规则写法与 MCP 工具相同。本会话和永久授权针对整个工具（任意参数），永久授权以工具名保存到 `.mosscode/permissions.local.yaml`：

```yaml
allow:
  - skill__test__detect                      # 任意参数
deny:
  - 'skill__deploy__run({"env":"prod*"})'    # 参数按规范化 JSON 通配匹配
```

- fork 的子 Agent 与主对话共用权限档位、规则和审批界面（审批面板标题注明是哪个子 Agent 在请求）；它在审批中选“本会话允许”得到的授权只在这个子 Agent 内有效，不带回主对话。
- 项目级 Skill 与项目指令一样属于项目内容：只在用户调用或模型加载时进入上下文，专属工具在使用后才注册，并且每次执行都要审批。克隆来的仓库中的 Skill 请先读一遍再用。
- `.mosscode/skills/` 不是受保护文件，模型可以按你的要求编写或修改 Skill，写入照常审批。

## /skills

| 输入 | 行为 |
| --- | --- |
| `/skills`（别名 `/skill`） | 列出全部 Skill：命令、来源、模式、描述，以及扫描时跳过的 Skill |
| `/skills 名称` | 路径、来源、被覆盖的版本、模式、`fork_context`、`model`、白名单、专属工具（全名、命令、超时）、是否对模型可见、是否已加载、正文首行 |
| `/skills reload` | 重新扫描，更新命令表和模型可见的目录（下一次请求生效），报告新增、移除和出错的 Skill；已加载的 Skill 保持加载，已删除的移出 |

`/status` 中的“已激活 Skill”一行列出当前已加载的 Skill。

## 内置 Skill

| 命令 | 方式 | 作用 |
| --- | --- | --- |
| `/commit [对提交说明的要求]` | inline | 把与当前任务相关的改动提交为一次 git commit：逐个暂存相关文件，不暂存疑似凭据，不 `--amend`、不 `--no-verify`、不推送。`git add` 和 `git commit` 按档位审批 |
| `/review [范围或侧重点]` | fork（`none`） | 在独立上下文中审查尚未提交的改动，只用读取、搜索和命令执行工具，按严重程度列出问题；审查者看不到之前的对话 |
| `/test [测试范围]` | inline | 先调用专属工具 `skill__test__detect` 识别项目类型（pytest、npm / pnpm / yarn、go、cargo、maven / gradle），再运行建议的测试命令，失败时分析原因；没有要求时不修改文件 |

在用户级或项目级目录放一个同名的 Skill（如 `.mosscode/skills/review/SKILL.md`）即可替换内置版本。
