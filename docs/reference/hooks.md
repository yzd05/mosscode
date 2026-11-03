# Hook

Hook 是挂在 Agent 生命周期上的自动化：在会话开始、每轮开始结束、每次请求模型前后、工具调用前后等时机，执行命令、注入一段提示、发送 HTTP 请求，或拦截一次工具调用。Hook 写在 YAML 文件里，改动后 `/hooks reload` 即可生效，不需要改代码。子 Agent 相关的事件和 agent 动作见 [子 Agent](subagents.md)。

> Hook 以 MossCode 进程的权限运行，**不经过工具审批**。它是你自己的自动化：项目里带来的 Hook 文件需要先读一遍再信任（见 [信任](#信任)）。

## 文件

| 位置 | 来源 | 需要确认 |
| --- | --- | --- |
| `~/.mosscode/hooks.yaml` | 用户级 | 否 |
| `<启动目录>/.mosscode/hooks.yaml` | 项目级 | 是 |
| `<启动目录>/.mosscode/hooks.local.yaml` | 本地 | 是 |

三个文件追加合并，不互相覆盖。同一事件的 Hook 按用户级 → 项目级 → 本地的顺序执行，同一文件内按书写顺序。`id` 在全部 Hook 中必须唯一，重复的后一条被跳过。

文件按 UTF-8 读取（容忍 BOM），超过 256 KB 不加载。顶层只有 `hooks:`。

- 整个文件有问题（YAML 语法错误、顶层不是对象、`hooks` 不是列表、编码错误、超大）时跳过该文件，提示路径和原因，不回显文件内容；
- 某一条 Hook 有问题时只跳过这一条，提示 `文件路径 · id · 原因`，其他 Hook 照常加载；
- 不认识的字段（如把 `async` 拼成 `asnyc`）同样报错，不会被静默忽略。

启动时加载一次。运行中修改文件不影响已加载的 Hook，`/hooks reload` 后才生效。`--no-hooks` 启动参数让本次运行不加载任何 Hook，用于排查一条坏 Hook。

三个 Hook 文件是受保护文件：模型不能用 `write_file` / `edit_file` 写入或编辑（返回 `policy_protected`）。

## Hook 字段

```yaml
hooks:
  - id: format-python               # 必填
    event: post_tool_use            # 必填
    if: tool == Write || tool == Edit   # 可选
    action:                         # 必填（静态拒绝可省略）
      type: command
      command: ["${PYTHON}", -m, ruff, format, --quiet, "${FILE_PATH}"]
      timeout: 30
    once: false
    async: false
```

| 字段 | 说明 |
| --- | --- |
| `id` | 必填。字母、数字、`_`、`.`、`-`，以字母或数字开头，最长 64 个字符 |
| `event` | 必填，见 [事件](#事件) |
| `if` | 可选，条件表达式，见 [条件](#条件)；不写表示总是执行 |
| `action` | 对象，`type` 为 `command`、`prompt`、`http`、`agent`，见 [动作](#动作)。写了 `reject` 的 Hook 可以省略 |
| `reject` | 只用于 `pre_tool_use`：命中即拒绝，值是给模型看的原因（非空字符串，最多 2000 个字符）。不能与 `prompt` 动作同用 |
| `on_error` | 只用于 `pre_tool_use`：`ignore`（缺省，失败时放行）或 `block`（失败时拒绝） |
| `once` | 每个会话最多执行一次。条件满足、开始执行就算一次，失败也算；`/clear` 和恢复会话时重置 |
| `async` | 后台执行，不阻塞主流程，结果只进入日志和失败提示。不能用于 `pre_tool_use`、`prompt` 动作、`feedback: true` |

## 事件

除下表的条件字段外，所有事件都有三个字段：

- `event`：事件名；
- `agent`：主对话为 `main`；Fork（Skill fork 与 `agent` 工具省略类型时）中为 `fork`；按定义运行的子 Agent 中为 `subagent`；
- `agent_type`：主对话为空串，定义式子 Agent 为定义名（如 `explore`），Agent 的 Fork 为 `fork`，Skill fork 为 `skill:<名称>`。

在 git 工作树中隔离运行的子 Agent，其工具事件以及 `subagent_start` / `subagent_stop` 还有字段 `worktree`：相对仓库根的工作树路径（如 `.mosscode/worktrees/agent-1a2b3c4d`），不隔离时为空串；它也可以用在条件中。这时 `path` 相对工作树根，`MOSSCODE_FILE_PATH` 是工作树中的绝对路径，Hook 命令仍在主项目目录运行。见 [Git 工作树](worktree.md#hook-字段)。

| 事件 | 触发时机 | 条件字段 | 可用动作 | 执行方式 |
| --- | --- | --- | --- | --- |
| `session_start` | 启动加载上下文之后（界面中在项目 Hook 确认之后）、`/clear` 之后、恢复会话之后 | `source`：`new`、`clear`、`resume` | command、prompt、http、agent | 同步或 `async` |
| `session_end` | `/clear` 之前、恢复另一个会话之前、退出时 | `reason`：`clear`、`resume`、`exit` | command、http、agent | 同步或 `async` |
| `turn_start` | 用户提交消息或命令后，第一次请求模型之前；运行中排队的消息不算新一轮 | `prompt`：输入原文（命令为命令原文） | command、prompt、http、agent | 同步或 `async` |
| `turn_end` | 一轮结束 | `reason`：`completed`、`max_tokens`、`unknown_tools`、`max_iterations`、`cancelled`、`error` | command、http、agent | 同步或 `async` |
| `pre_send` | 主对话每次请求模型之前 | `iteration`：本轮第几次请求（从 1 开始） | prompt；其他动作总在后台 | 见下 |
| `post_receive` | 主对话每次收到完整回复之后 | `iteration`；`tool_calls`：本次回复调用的工具数 | command、http、agent，总在后台 | 见下 |
| `pre_tool_use` | 参数校验、硬检查和拒绝规则都通过之后，审批之前 | `tool`、`path`、`command`、`args.<参数名>` | command、prompt、http、agent | 只能同步 |
| `post_tool_use` | 工具执行结束（成功或失败）之后，结果交给模型之前 | 同上，另有 `error`（`true` / `false`）、`code`（错误码，成功时为空） | command、prompt、http、agent | 同步或 `async` |
| `permission_request` | 交互界面中即将弹出审批时 | `tool`、`path`、`command`、`args.<参数名>`、`risk`（高风险说明） | command、http、agent，总在后台 | 见下 |
| `compact` | 压缩成功之后 | `trigger`：`manual`、`auto`、`restore` | command、http、agent | 同步或 `async` |
| `error` | 一轮因模型错误结束时，在 `turn_end` 之前 | `error`：错误提示 | command、http、agent | 同步或 `async` |
| `subagent_start` | 子 Agent 发出第一个请求之前，在启动它的一方触发 | `task_id`、`description`、`background`（`true` / `false`）；`agent_type` 为子 Agent 的类型 | command、prompt、http | 同步或 `async` |
| `subagent_stop` | 子 Agent 结束之后、结果返回或通知送达之前 | `task_id`、`status`（`completed`、`failed`、`cancelled`）、`background`；`agent_type` 同上；`worktree_kept`（隔离时 `true` 为有改动已保留、`false` 为已删除，不隔离时为空串） | command、http、agent，总在后台 | 见下 |

- `pre_send`、`post_receive`、`permission_request` 每轮可能触发很多次，上面的命令、HTTP、agent 动作总在后台执行；写 `async: false` 会报错。需要同步处理时改用 `turn_start` / `turn_end`。
- 子 Agent（Skill fork、`agent` 工具启动的 Fork 和定义式子 Agent）中只触发 `pre_tool_use`、`post_tool_use`、`permission_request`。fork 命令本身是主对话的一轮，照常触发 `turn_start` / `turn_end`，但 `turn_start` 注入的内容不发给子 Agent。
- `subagent_start` 的 prompt 和 feedback 放在该子 Agent 第一条用户消息之前，只发给这个子 Agent。
- 压缩、记忆提取和子 Agent 的请求不触发 `pre_send` / `post_receive`。
- `agent` 动作总在后台执行，见 [agent](#agent)。

## 条件

`字段 运算符 值`，多个子条件用 `&&`（全部满足）或 `||`（任一满足）连接。同一条件中不能混用，也不支持括号；更复杂的逻辑拆成多条 Hook。字段必须是该事件有的字段，例如 `turn_start` 上写 `tool` 会报错。

| 运算符 | 含义 |
| --- | --- |
| `==` | 相等 |
| `!=` | 不相等（`==` 的取反） |
| `~=` | 通配 |
| `=~` | 正则，必须写成 `/…/`，部分匹配，区分大小写 |

值可以裸写（到下一个 `&&` / `||` 或结尾为止），也可以写在双引号中，此时可以包含空格、`&&`、`||`，用 `\"`、`\\` 转义。

各字段的匹配与 [权限规则](permissions.md) 相同：

- **`tool`**：工具全名。`==` / `!=` 先换算权限规则的别名（`Bash`、`Shell` → `execute_command`，`Read`、`Write`、`Edit` → `read_file`、`write_file`、`edit_file` 等）；`~=` 对全名通配（如 `mcp__github__*`）。
- **`path`**：文件类工具（`read_file`、`write_file`、`edit_file`、`glob`、`grep`）的目标；项目内是 `/` 分隔的相对路径，项目外是绝对路径，其他工具为空。`~=` 中 `*` 匹配一段、`**` 跨目录；相对模式只匹配项目内，绝对模式只匹配项目外。Windows 下不区分大小写。
- **`command`**：`execute_command` 的命令原文。`~=` 以 `:*` 结尾时按词前缀匹配（与 `Bash(git push:*)` 相同，`git pushx` 不算），否则是通配；整条命令或复合命令中任一段命中即为命中，所以 `cd x && git push -f` 也会被 `git push:*` 命中。
- **`args.<名称>`**：参数的顶层字段。字符串原样，其他类型为紧凑 JSON；不存在时为空串。不支持 `args.a.b`。
- 其他字段取字符串值，`~=` 为通配。

条件不满足时 Hook 不执行，也不消耗 `once`。

```yaml
if: tool == Bash && command ~= "git push:*"
if: path ~= "config/prod/**" || path ~= "*.env"
if: tool =~ /^mcp__github__/ && args.repo != my-org/sandbox
if: reason == completed && agent == main
```

## 动作

### command

```yaml
action:
  type: command
  command: ["${PYTHON}", scripts/check.py, "${FILE_PATH}"]   # 或字符串
  timeout: 30          # 秒，1–600，缺省 30
  feedback: false      # 成功时把 stdout 注入给模型
```

- **数组（推荐）**：直接执行，不经过 shell。元素中的 `${FILE_PATH}`、`${PROJECT_ROOT}`、`${PYTHON}`（MossCode 所用的 Python）、`${TOOL_NAME}`、`${EVENT}` 替换为对应值，替换结果总是一个完整参数，文件名里的空格和 `;` 不会被解释。其他 `${…}` 加载时报错。第一个元素按 `PATH` 查找。YAML 会把 `-3`、`8080` 这类不加引号的元素读成数字，加载时转成文本参数（`1.50` 会变成 `1.5`，需要原样时加引号）。
- **字符串**：用 `execute_command` 同一个 shell（配置项 `shell`）执行，不做任何文本替换。上下文从环境变量读：bash 写 `"$MOSSCODE_FILE_PATH"`，PowerShell 写 `$env:MOSSCODE_FILE_PATH`。不向 shell 字符串替换文件名，是因为文件名来自模型，可能是 `a.py; rm -rf ~`。

工作目录为项目根目录。环境变量在当前环境之上加：

| 变量 | 内容 |
| --- | --- |
| `MOSSCODE_HOOK_ID`、`MOSSCODE_HOOK_EVENT` | Hook 的 `id` 和事件 |
| `MOSSCODE_SESSION_ID`、`MOSSCODE_PROJECT_ROOT`、`MOSSCODE_AGENT` | 会话 ID、项目根目录、`main`、`fork` 或 `subagent` |
| `MOSSCODE_AGENT_TYPE` | `agent_type` 字段的值（主对话为空） |
| `MOSSCODE_TOOL_NAME`、`MOSSCODE_FILE_PATH` | 工具事件：工具名；文件类工具目标的绝对路径 |
| `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` | 让 Python 脚本按 UTF-8 输出 |

stdin 是一个 UTF-8 JSON 对象：`event`、`hook_id`、`session_id`、`project_root`、`agent`、`agent_type` 和该事件的条件字段；工具事件另有 `tool`、`args`（完整参数）、`path`、`command`，`post_tool_use` 另有 `result`（`content` 最多 64 KB、`is_error`、`code`）。

退出码 0 为成功；`pre_tool_use` 上退出码 2 表示拒绝（原因取 stderr，为空时取 stdout）；其他为失败。超时后终止整个进程树，算失败。`feedback: true` 只能用于 `session_start`、`turn_start`、`pre_tool_use`、`post_tool_use`，stdout 为空时不注入。

### prompt

```yaml
action:
  type: prompt
  message: "刚写入了 {{path}}，提交前记得运行测试。"
```

把一段文字注入给模型，位置见 [注入位置](#注入位置)。`{{字段}}` 替换为该事件条件字段的值，只出现在给模型的文字里，不进入任何命令。最长 8000 个字符，不能 `async`。

### http

```yaml
action:
  type: http
  url: "https://hooks.example.com/notify?token=${NOTIFY_TOKEN}"
  method: POST                     # POST（缺省）、GET、PUT、PATCH、DELETE
  headers: {Authorization: "Bearer ${NOTIFY_TOKEN}"}
  json: {text: "MossCode 完成一轮：{{reason}}"}
  timeout: 10
```

- `url`、`headers` 中的 `${VAR}`、`${VAR:-默认值}` 在加载时展开（与 MCP 配置相同）；变量未设置且没有默认值时该 Hook 加载失败。`url` 中不支持 `{{字段}}`。请求头的名称和值只能是 ASCII。
- `json` 中字符串值里的 `{{字段}}` 替换后仍是 JSON 字符串，值中的引号不会破坏结构。不写 `json` 时，`POST` / `PUT` / `PATCH` 发送与 command 的 stdin 相同的上下文。
- 不跟随重定向；2xx 为成功，其他状态码、连接错误、超时为失败。日志和提示中的 URL 去掉查询串。

### agent

```yaml
action:
  type: agent
  agent_type: general   # 子 Agent 定义名，缺省 general
  prompt: "检查 {{path}} 的改动是否需要更新文档。"
  timeout: 300          # 秒，1–600，缺省 300
```

以 `prompt` 的渲染结果（`{{字段}}` 与 prompt 动作相同）启动一个后台的定义式子 Agent，类型见 [子 Agent](subagents.md)：

- 总在后台执行，不阻塞主流程，所以不能写 `async: false`、`reject`、`on_error: block`；
- 子 Agent 的权限视图为非交互，需要审批的调用直接返回 `approval_required`；
- 结果不交给模型：写入 `hooks.log`（`detail` 为结果的前 300 个字符），在 `/tasks` 中可见，界面提示一行 `Hook <id> 的子 Agent（任务 t3）已完成。`；
- `timeout` 到期时停止该任务，记为 `timeout`；后台任务已达上限（`agent_max_background`）时记为 `failed`；
- 由 `agent` 动作启动的子 Agent 中，工具事件上的 `agent` 动作记为 `skipped`，不会再启动子 Agent；当前会话未启用工具时同样记为 `skipped`，类型不存在时记为 `failed`。

## 拦截

一次工具调用依次经过：参数校验 → 硬检查（计划模式、受保护文件、灾难级命令）→ 权限规则中的 deny → `pre_tool_use` → 审批（需要时）→ 执行 → `post_tool_use`。任一步拒绝后，后面的步骤都不发生。

- **静态拒绝**：命中且写了 `reject` 的 Hook。有 `action` 时先执行它（例如记录），然后拒绝。
- **动态拒绝**：`pre_tool_use` 的命令以退出码 2 结束。
- **失败**：退出码非 0 非 2、超时、命令找不到、HTTP 失败时，缺省放行并提示一行；`on_error: block` 时拒绝，原因为 `Hook <id> 执行失败，按配置阻止此调用：<失败原因>`。

被拒绝的调用不执行、不弹审批、不触发 `post_tool_use`。模型收到错误码为 `hook_rejected` 的工具结果 `Hook <id> 拒绝了此调用：<原因>`，据此调整做法。第一条拒绝之后，后面的 `pre_tool_use` Hook 不再执行。

Hook 只能收紧：`bypassPermissions` 档位和只读命令的自动放行同样受 `pre_tool_use` 约束，而 Hook 通过并不会让需要审批的调用跳过审批。

## 注入位置

| 来源 | 位置 | 持续时间 |
| --- | --- | --- |
| `session_start` 的 prompt 和 feedback | 系统提示的可缓存前缀中（`<active-skills>` 之后），`<hook-context>` | 整个会话的每次请求；不进历史，压缩后仍在；`/clear` 和恢复会话时由新的 `session_start` 重新生成 |
| `turn_start` 的 prompt 和 feedback | 请求末尾的 `<hook-notification>`（与模式提醒同一位置） | 本轮的每次请求 |
| `pre_send` 的 prompt | 请求末尾的 `<hook-notification>` | 只在当次请求 |
| `pre_tool_use`（未拒绝）、`post_tool_use` 的 prompt 和 feedback | 工具结果末尾的 `<hook-notification hook="id">…</hook-notification>` | 随结果进入历史和会话存档 |

每段注入都标明来源 Hook 的 `id`。`hook-context`、每次请求的 `hook-notification`、每个工具结果附加的内容各自最多 8000 个字符，超出部分截断并注明。

`write_file` / `edit_file` 成功后，如果同步的 `post_tool_use` Hook 改动了这个文件（例如格式化），结果末尾会附加“文件已被 Hook <id> 修改（如格式化）；再次编辑前请重新读取。”，模型下次编辑前必须重新读取；界面的工具行下方也会显示一行同样含义的灰色提示。后台 Hook 的改动检测不到，所以格式化类 Hook 请用同步执行。

## 执行与失败

- 同步 Hook 逐条等待；后台 Hook 启动后不等待。同步 Hook 的耗时不计入工具的 `timeout`。
- 按 Esc 取消一轮时，正在运行的同步 Hook 终止进程树，记为 `cancelled`；`pre_tool_use` 被取消时工具不执行，`post_tool_use` 被取消时工具结果照常保留。
- 退出时先执行 `session_end`（同步部分最多等 10 秒），再等仍在运行的后台 Hook 最多 3 秒，超时的终止并记为 `cancelled`。
- Hook 失败不中断主流程，界面中提示一行 `Hook <id>（<事件>）失败：<原因>；已忽略。`，`-p` 中打印到 stderr（stream-json 中为 `notice` 事件）。同一条 Hook 一轮内只提示一次。拒绝不另外提示，工具行中已经显示。
- 每次执行在当前会话目录（`~/.mosscode/projects/<项目键>/sessions/<会话 ID>/`）的 `hooks.log` 中追加一行 JSON：`time`、`hook`、`source`、`event`、`agent`、`action`、`outcome`（`ok`、`failed`、`rejected`、`timeout`、`cancelled`、`skipped`）、`duration_ms`、`exit_code` 或 `status`、`detail`（最多 500 个字符）。不记录环境变量、请求头和 stdin。

## 信任

项目级的两个文件（`.mosscode/hooks.yaml`、`.mosscode/hooks.local.yaml`）随仓库而来，会以你的权限运行命令，所以每个文件都要单独确认后才启用：

- 界面中有未确认的文件时，启动时（`session_start` 之前）弹出面板，列出文件路径（项目内显示相对路径）和每条 Hook：第一行是 `id · 事件`，条件和动作（命令原文或拒绝原因）各占一行缩进显示。选“信任并启用”会记住文件内容的 SHA-256（`~/.mosscode/projects/<项目键>/hooks-trust.json`）；选“本次不启用”或按 Esc，本次运行不加载，`/hooks reload` 时再次询问。
- 文件内容变化后需要重新确认，包括被命令改写的情况。
- `-p` 中未确认的文件不加载，stderr 提示 `项目 Hook 文件 <路径> 未经确认，未启用；请在交互界面中启动一次并确认，或使用 --trust-project-hooks。` `--trust-project-hooks` 只在本次运行信任，不写入记录。
- 用户级文件 `~/.mosscode/hooks.yaml` 直接生效。

## /hooks

| 命令 | 行为 |
| --- | --- |
| `/hooks`（别名 `/hook`） | 按来源列出 Hook，格式同信任面板（`id · 事件`，后跟标记 reject / once / async / on_error 和状态：已启用、未确认、once 已执行；条件、动作摘要分行缩进），以及加载时跳过的 Hook 和原因、未确认的文件、最近 10 条执行记录 |
| `/hooks <id>` | 来源文件、完整配置（`headers` 的值显示为 `***`）、本次运行的执行记录、`hooks.log` 路径 |
| `/hooks reload` | 重新加载三个文件，报告新增、移除和出错的 Hook；未确认的项目级文件再次询问。`once` 状态按 `id` 保留 |

`/status` 中有一行“Hook：已启用 N 条（项目级未确认 M 条）”，`--no-hooks` 时为“已关闭”。无界面模式中 `/hooks` 以退出码 2 结束。

## 示例

```yaml
hooks:
  # 禁止编辑生产配置（只拦写入，读取照常）
  - id: protect-prod
    event: pre_tool_use
    if: tool =~ /^(write_file|edit_file)$/ && path ~= "config/prod/**"
    reject: 生产配置只能由发布流程修改，请改 config/dev/ 下的文件。

  # 写入 Python 文件后格式化（同步，模型会被提示重新读取）
  - id: format-python
    event: post_tool_use
    if: tool == Write || tool == Edit
    action:
      type: command
      command: ["${PYTHON}", -m, ruff, format, --quiet, "${FILE_PATH}"]

  # 会话开始时把最近的提交告诉模型
  - id: recent-commits
    event: session_start
    action: {type: command, command: [git, log, -3, --oneline], feedback: true}

  # 推送前跑测试，失败（退出码 2）时拒绝推送
  - id: test-before-push
    event: pre_tool_use
    if: tool == Bash && command ~= "git push:*"
    action:
      type: command
      command: ["${PYTHON}", -c, "import subprocess, sys; sys.exit(0 if subprocess.run([sys.executable, '-m', 'pytest', '-q']).returncode == 0 else 2)"]
      timeout: 600

  # 审查子 Agent 不许运行命令
  - id: review-read-only
    event: pre_tool_use
    if: agent == fork && tool == Bash
    reject: 审查时只读代码，不运行命令。

  # 每轮结束通知（后台）
  - id: notify
    event: turn_end
    if: reason == completed
    async: true
    action:
      type: http
      url: "${NOTIFY_URL}"
      json: {text: "MossCode 完成了一轮"}
```
