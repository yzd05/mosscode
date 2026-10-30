# 第十二章：Hook 系统

日期：2026-10-27。状态：已实现（2026-10-27）。测试见 `tests/test_hooks_stage12.py`，使用说明见 `docs/reference/hooks.md`。

来源：
- 课程第 12 章：理论、Python 源码解析、实战演练中的事件接入点。
- 用户的初步设计：三要素格式，四层生命周期事件加系统级事件，`pre_tool_use` 拦截，复用权限规则的匹配，四种动作，只跑一次、后台、超时，失败不中断主流程，YAML 声明式加载并集中校验。
- 2026-10-27 对用户初步设计提出的 15 条优化建议。
- 当天对照现有权限、工具执行、Agent 循环、Skill 实现的调查。

本规格按 MossCode 的现状调整课程方案，调整逐条列在“对课程方案的调整”中。

## 目标

让用户在 Agent 生命周期的关键时刻自动执行自己的规则，不改代码、不依赖模型记得去做。每条 Hook 由三要素组成：

- **事件**（必填）：什么时候触发；
- **条件**（可选）：满足什么才执行，语法为 `字段 运算符 值`；
- **动作**（必填）：执行什么，四种之一：shell 命令、提示词注入、HTTP 请求、子 Agent（本章只占位）。

典型用途：
- 拦截危险或违规的工具调用，并把原因告诉模型；
- 写入文件后自动格式化；
- 会话开始时把项目状态注入上下文；
- 任务结束时发通知。

Hook 是辅助机制：失败只记录、提示一行，不中断主流程；Hook 只能收紧权限，不能放行。

## 现状

| 位置 | 现在的做法 | 与本章的关系 |
| --- | --- | --- |
| `PermissionPolicy.authorize`（`permissions.py`） | 一个方法内完成硬检查（`operation()` 抛出 `SecurityError`：计划模式、受保护文件、危险命令）、规则判定 `decide()`、审批 `_ask()` | `pre_tool_use` 要插在“拒绝规则”和“审批”之间，需要把这段流程开一个口 |
| 权限规则匹配 | `Rule.matches`：路径为项目相对、`/` 分隔，`*` 匹配一段、`**` 跨目录，Windows 不区分大小写，相对模式只管项目内、绝对模式只管项目外；命令规则匹配整条命令，`前缀:*` 为按词前缀，拒绝规则逐段检查复合命令；工具别名 `Read`/`Write`/`Edit`/`Bash`/`Shell`/`Glob`/`Grep`；没有正则和取反 | 条件复用这套取值和匹配语义，另加正则与 `!=` |
| `PermissionPolicy.operation()` | 把参数换算成 `Operation`：`target` 为规范化路径或命令原文 | 条件的 `path`、`command` 字段直接取自 `Operation`，保证与权限规则看到的一样 |
| 受保护文件 | 权限文件和 MCP 配置不能由模型写入或编辑（`policy_protected`） | Hook 配置同样能执行任意命令，加入受保护列表 |
| `ToolRegistry.execute`（`tools/base.py`） | 唯一的执行边界：未知工具 → JSON 解析 → 参数校验 → `authorize` → `_invoke`（受超时约束）→ 结果截断或落盘 | `pre_tool_use` 经 `authorize` 接入，`post_tool_use` 在结果交给模型前接入 |
| `ToolRegistry.subset` | fork 子 Agent 的工具视图，共用工具实例、权限策略、会话 | 子 Agent 的工具调用同样触发工具级 Hook，标记为 `agent: fork` |
| `execute_batch`（`tools/execution.py`） | 相邻只读调用最多 4 个并行，有副作用的调用串行 | 并行的只读调用，其 Hook 也并行执行 |
| `edit_file` / `write_file` 的 `FileState` | 记录读取或自身写入时的 `(mtime_ns, size)`，不一致时返回 `stale_read` | 格式化类 Hook 改写文件后，模型的下一次编辑会失败；需要在结果中提示重新读取 |
| `AgentLoop.run`（`agent.py`） | 一次 `run` 是一轮：循环中每次迭代调用 `_model()` 发请求，`RunStopped` 的原因为 completed / max_tokens / unknown_tools / max_iterations / cancelled / error；`_compact()` 在成功时产出 `CompactFinished` | `pre_send`、`post_receive`、`compact` 的接入点 |
| `Conversation`（`conversation.py`） | `start()`（首次加载上下文）、`clear()`、`resume()`、`close()`、`reply()`（含 fork Skill 的 `_fork`）；`_raised` 收集运行中的提示，转成 `Notice` 事件；后台提示经 `on_memory` 回调送到界面 | 会话级、轮次级事件和提示的接入点 |
| `prompts.Supplement` 与各提供商 | `PREFIX_KINDS`（`environment`、`active-skills`）放在可缓存前缀中；其他类型（`system-reminder`）放在最后一个缓存断点之后，只随当次请求发送 | 会话级注入用新的前缀类型，轮次级和请求级注入用请求级类型，不破坏缓存 |
| `run_process`（`tools/process.py`）与 `Shell` | 子进程：stdin 写入、输出头尾截断、Windows Job Object / POSIX 进程组终止整个进程树；`Shell.args()` / `env()` 按配置的 shell（Git Bash 或 PowerShell）包装命令 | 命令动作直接复用 |
| `SkillTool`（`tools/skill.py`） | argv 不经过 shell，`${SKILL_DIR}`、`${PYTHON}` 替换，参数 JSON 写入 stdin，环境变量 `PYTHONUTF8=1` 等 | 命令动作的 argv 形式照此设计 |
| `mcp_config._substitute` | `${VAR}`、`${VAR:-默认值}` 环境变量展开 | HTTP 的 `url`、`headers` 复用 |
| `mcp_client.py` | 直接使用 `httpx`（经 anthropic、openai、mcp 间接安装，未在依赖中声明） | HTTP 动作用 `httpx`，并在依赖中显式声明 |
| 会话目录（`session.py`） | `tool-results/`、`compact-<n>.md`，首次写入时才创建 | Hook 执行记录写入同一目录的 `hooks.log` |
| `paths.project_dir` | `~/.mosscode/projects/<项目键>/`，存放会话与记忆 | 项目级 Hook 的信任记录放在这里 |
| `/skills`、`/mcp`（`builtin_commands.py`） | 列表、详情、`reload`；`-p` 中输入时退出码 2 | `/hooks` 沿用 |
| `-p` 的提示（`cli.py`） | 启动提示和运行中的 `Notice` 打印到 stderr；stream-json 输出 `{"type":"notice"}` | Hook 的失败与跳过提示走同一路径 |

## 对课程方案的调整

| # | 课程方案 | 调整 | 原因 |
| --- | --- | --- | --- |
| D1 | Hook 写在 `config.yaml` 的 `hooks:` 中 | 独立文件 `~/.mosscode/hooks.yaml`、`.mosscode/hooks.yaml`、`.mosscode/hooks.local.yaml`，追加合并 | 与权限、MCP 的三层文件一致；`config.yaml` 含密钥、不入库，项目级 Hook 需要能入库共享 |
| D2 | 配置文件中的 Hook 直接执行 | 项目级的两个文件首次加载或内容变化时需要用户确认，确认按文件内容的哈希记录在用户目录；未确认的不执行，`-p` 中跳过并提示（S1–S5） | 克隆来的仓库可以借 Hook 在任意时刻执行任意命令，且不经过审批 |
| D3 | 条件值按字符串做 `==`、`fnmatch`、正则比较 | 语法沿用 `字段 运算符 值`；`tool`、`path`、`command` 的取值和通配语义与权限规则相同：别名、项目相对路径、分段通配、按词前缀、复合命令逐段匹配（K4–K7） | 同一个 `config/**` 在权限规则和 Hook 中含义一致；字符串 `fnmatch` 的 `*` 会跨目录，`git push --force` 藏在 `cd x && …` 中会漏判 |
| D4 | `$TOOL_NAME`、`$FILE_PATH` 等变量以文本替换进 shell 命令 | 不向 shell 命令做文本替换。上下文经环境变量（`MOSSCODE_*`）和 stdin JSON 传入；命令可写成 argv 数组（不经过 shell），其中 `${FILE_PATH}` 等整段替换；HTTP 用结构化的 `json:` 请求体（A1–A4、A9） | 文件名和命令来自模型，文本替换进 shell 就是注入点 |
| D5 | `reject: true` 时，动作的输出作为拒绝原因 | 静态拒绝：`reject: "原因"`；动态拒绝：命令以退出码 2 结束，stderr 作为原因；其他非 0 退出码是 Hook 失败（I2–I4） | 原方案中“命令失败”和“决定拒绝”无法区分；退出码 2 的约定与 Claude Code 一致，已有的检查脚本可以直接用 |
| D6 | 拦截类 Hook 出错只记日志 | 默认放行（fail-open），并提示一行；`pre_tool_use` 可写 `on_error: block`，出错时拒绝此调用（I5） | 安全检查脚本自身出错时，有的用户宁可停下 |
| D7 | 拦截在审批之前还是之后未说明 | 顺序：参数校验 → 硬检查与拒绝规则 → `pre_tool_use` → 审批 → 执行 → `post_tool_use`。Hook 只能收紧；被拒绝或未执行的调用不触发 `post_tool_use`（I1、I6） | 先问用户、再被 Hook 拒绝是浪费；已被规则拒绝的调用不必再跑 Hook |
| D8 | `prompt` 输出统一在下一轮注入 | 按事件决定注入位置和生存期：`session_start` 的注入整个会话有效（前缀区，可缓存）；`turn_start` 的在本轮每次请求附带；`pre_send` 的只附带当次请求；`pre_tool_use` / `post_tool_use` 的附在该工具结果后面。命令的输出用 `feedback: true` 注入，规则相同（P1–P6） | 不同事件的注入语义不同；附在工具结果上的说明随结果一起进入历史，模型最容易对上 |
| D9 | `async` 只禁止用于 `pre_tool_use` | 另外禁止用于 `prompt` 动作、`feedback: true`、`on_error`；`pre_send`、`post_receive`、`permission_request` 上的命令、HTTP、agent 动作一律后台执行；退出时等待后台 Hook 至多 3 秒，然后终止（R4–R7） | 后台结果来不及注入；高频事件同步执行会拖慢每次请求；退出不能被一条卡住的 Hook 挂住 |
| D10 | 子 Agent 未讨论 | fork 子 Agent 中触发工具级事件（`pre_tool_use`、`post_tool_use`、`permission_request`），条件字段 `agent` 为 `fork`；会话级、轮次级、消息级事件不在子 Agent 中触发（E3） | 子 Agent 的工具调用同样需要拦截；子 Agent 不是新会话，也不是新一轮 |
| D11 | 15 种事件 | 11 种：去掉 `startup`、`shutdown`（与 `session_start` / `session_end` 重复）、`file_change`（`post_tool_use` 加 `path` 条件即可）、`command_execute`（`tool == Bash`）；`session_start` 带 `source`，`turn_end` 带 `reason`，`post_tool_use` 带 `error`（E1） | 少几个重叠的事件，每个事件含义清楚 |
| D12 | 只按 YAML 顺序执行 | 顺序为用户级 → 项目级 → 本地，同一文件内按书写顺序；第一个拒绝之后的 `pre_tool_use` Hook 不再执行（R1） | 与配置的合并顺序一致；不做显式优先级 |
| D13 | 用 `log.warning` 记录 | 每个会话目录写 `hooks.log`；`/hooks` 列出已加载的 Hook、问题和最近的执行；失败在界面提示一行，`-p` 中打印到 stderr（O1–O5、U1–U4） | Hook 在后台默默失败时，用户需要知道“为什么没生效” |
| D14 | `once` 是进程内标记 | 按会话计：`/clear` 和恢复会话后重置（R3） | `/clear` 开始的是新会话，`session_start` 类的一次性 Hook 应再跑一次 |
| D15 | 未知字段返回空串，条件解析错误时整条配置报错 | 校验在加载时完成：未知字段、该事件没有的条件字段、非法正则、不允许的组合都让该 Hook 被跳过并提示文件与 `id`，其他 Hook 照常加载（C4、C5） | 与 Skill 扫描一致：一条坏规则不拖垮其他规则；`args.comand` 这种拼写错误之外，都能在加载时发现 |

## 需求

### 配置文件

| # | 行为 |
| --- | --- |
| C1 | 三个位置：`~/.mosscode/hooks.yaml`（用户级）、`<项目根目录>/.mosscode/hooks.yaml`（项目级）、`<项目根目录>/.mosscode/hooks.local.yaml`（本地）。文件不存在即为空 |
| C2 | 顶层只有 `hooks:`，值为 Hook 列表。文件按 UTF-8 读取（容忍 BOM），超过 256 KB 不加载 |
| C3 | 三层追加合并，不覆盖。`id` 在所有已加载的 Hook 中必须唯一；重复时跳过后出现的那条并提示 |
| C4 | 文件级错误（YAML 语法错误、顶层不是对象、`hooks` 不是列表、编码错误、超大）：跳过整个文件，提示文件路径和原因，不回显文件内容 |
| C5 | Hook 级错误（见“Hook 字段”和各节的校验规则）：跳过该 Hook，提示 `文件路径 · id · 原因`（没有 `id` 时写第几项），其他 Hook 照常加载 |
| C6 | 启动时加载一次；`/hooks reload` 重新加载（变化的项目级文件需要重新确认，见 S2）。文件在运行中被修改，不影响当前已加载的 Hook |
| C7 | `--no-hooks` 启动参数：本次运行不加载任何 Hook（用于排查一条坏 Hook） |

### Hook 字段

```yaml
hooks:
  - id: format-python               # 必填
    event: post_tool_use            # 必填
    if: tool == Edit && path ~= "**/*.py"   # 可选
    action:                         # 必填（静态拒绝时可省略，见 H4）
      type: command
      command: [ruff, format, --quiet, "${FILE_PATH}"]
      timeout: 30
    once: false
    async: false
```

| # | 行为 |
| --- | --- |
| H1 | `id`：必填，`[A-Za-z0-9][A-Za-z0-9_.-]*`，最长 64 个字符 |
| H2 | `event`：必填，取值见“事件” |
| H3 | `if`：可选，条件表达式字符串，见“条件”；缺省表示总是执行 |
| H4 | `action`：必填，对象，`type` 为 `command`、`prompt`、`http`、`agent` 之一，其余字段见“动作”。例外：有 `reject`（字符串）的 Hook 可以省略 `action`，表示命中即拒绝（已确认 Q1） |
| H5 | `reject`：非空字符串，最长 2000 个字符，只能用于 `pre_tool_use`。写成 `true` 等非字符串时报错，提示“reject 需为拒绝原因；动态拒绝请让命令以退出码 2 结束” |
| H6 | `on_error`：`ignore`（缺省）或 `block`，只能用于 `pre_tool_use` |
| H7 | `once`：布尔，缺省 `false` |
| H8 | `async`：布尔，缺省 `false`；限制见 R5 |
| H9 | 不认识的字段报错（不忽略），防止 `asnyc` 这类拼写错误让 Hook 静默改变行为 |

### 事件

**E1** 事件一览。下表“条件字段”一列之外，所有事件都有 `event` 和 `agent` 两个字段：

| 事件 | 触发时机 | 条件字段 | 可用动作 | 执行方式 |
| --- | --- | --- | --- | --- |
| `session_start` | 首次启动加载上下文之后（TUI 中在项目级 Hook 确认之后）、`/clear` 之后、恢复会话之后；`-p` 同样触发 | `source`：`new`、`clear`、`resume` | command、prompt、http、agent | 同步或 `async` |
| `session_end` | `/clear` 清空之前、恢复另一个会话之前、退出时（`/exit`、Ctrl+C 退出、`-p` 结束） | `reason`：`clear`、`resume`、`exit` | command、http、agent | 同步或 `async`；退出时同步部分合计最多等待 10 秒 |
| `turn_start` | 一轮开始：用户提交消息、提示词命令、Skill 命令（含 fork）后，第一次模型请求之前。运行中追加的引导消息不算新一轮 | `prompt`：用户输入原文（命令为命令原文） | command、prompt、http、agent | 同步或 `async` |
| `turn_end` | 一轮结束：`RunStopped` 之后 | `reason`：`completed`、`max_tokens`、`unknown_tools`、`max_iterations`、`cancelled`、`error` | command、http、agent | 同步或 `async` |
| `pre_send` | 主对话每次向模型发请求之前；压缩、记忆提取、fork 子 Agent 的请求不触发 | `iteration`：本轮第几次请求（从 1 开始） | prompt；command、http、agent 只在后台执行 | 见 R6 |
| `post_receive` | 主对话每次收到完整回复之后 | `iteration`；`tool_calls`：本次回复请求的工具数 | command、http、agent，只在后台执行 | 见 R6 |
| `pre_tool_use` | 参数校验、硬检查和拒绝规则都通过之后，审批之前 | `tool`、`args.<名称>`、`path`、`command` | command、prompt、http、agent；可配 `reject`、`on_error` | 只能同步 |
| `post_tool_use` | 工具执行结束（成功或失败）之后、结果交给模型之前 | 同上，另有 `error`：`true` / `false`，`code`：错误码（成功时为空） | command、prompt、http、agent | 同步或 `async` |
| `permission_request` | 交互模式中即将弹出审批时 | `tool`、`args.<名称>`、`path`、`command`、`risk`（高风险说明，普通审批为空） | command、http、agent，只在后台执行 | 见 R6 |
| `compact` | 压缩成功之后 | `trigger`：`manual`、`auto`、`restore` | command、http、agent | 同步或 `async` |
| `error` | 一轮因模型错误或内部错误结束时，在 `turn_end` 之前 | `error`：提示文字 | command、http、agent | 同步或 `async` |

| # | 行为 |
| --- | --- |
| E2 | `agent` 字段：主对话为 `main`，fork 子 Agent 中为 `fork` |
| E3 | fork 子 Agent 中只触发 `pre_tool_use`、`post_tool_use`、`permission_request`。fork Skill 命令本身是主对话的一轮，照常触发 `turn_start` / `turn_end`；`turn_start` 的注入不发给子 Agent（子 Agent 只有自己的上下文） |
| E4 | `exit_plan_mode` 也是工具调用：不在计划模式时直接拒绝（现有行为），否则先触发 `pre_tool_use`，再弹出计划审批 |
| E5 | Hook 自身的失败不触发 `error` 事件，避免循环 |
| E6 | 未启用工具时，工具级事件不会出现；配置了也不报错 |

### 条件

**K1 语法**：`字段 运算符 值`，多个子条件用 `&&`（全部满足）或 `||`（任一满足）连接，同一条件中不能混用，也不支持括号；需要更复杂的逻辑时拆成多条 Hook。

**K2 运算符**：`==` 相等，`!=` 不相等（`==` 的取反），`~=` 通配，`=~` 正则。

**K3 值的写法**：
- 裸写：到下一个 `&&` / `||` 或结尾为止，去掉首尾空白；
- 双引号：可包含空格、`&&`、`||`，支持 `\"` 和 `\\` 转义；
- 正则必须写成 `/…/`，加载时编译，非法时报错（C5）。匹配用 `re.search`（部分匹配），区分大小写。

**K4 `tool`** 取工具全名：
- `==` / `!=`：值先按权限规则的别名换算（`Bash`、`Shell` → `execute_command`，`Edit` → `edit_file` 等），MCP 和 Skill 工具写全名；
- `~=`：对全名通配（如 `mcp__github__*`），值恰好是别名时先换算；
- `=~`：对全名做正则，不换算别名。

**K5 `path`** 取文件类工具（`read_file`、`write_file`、`edit_file`、`glob`、`grep`）的目标，与权限规则看到的 `Operation.target` 相同。其他工具为空串。
- 取值：项目内为项目相对路径，`/` 分隔；项目外为绝对路径；
- `==` / `!=`：值同样规范化（去掉开头的 `./`，`\` 换成 `/`）后比较，Windows 下不区分大小写；
- `~=`：与权限规则相同的路径通配：`*` 匹配一段，`**` 跨目录；相对模式只匹配项目内，绝对模式只匹配项目外；
- `=~`：对取值做正则。

**K6 `command`** 取 `execute_command` 的命令原文，其他工具为空串。
- `==` / `!=`：整条命令比较；
- `~=`：以 `:*` 结尾时是按词前缀（与 `Bash(git push:*)` 相同），否则是通配。整条命令命中，或复合命令中任一段命中，即为命中。分段用拒绝规则的 `command_segments`：按 `;`、`&`、`|`、换行、反引号、括号、`$(` 粗略拆分，宁可多拆，不会漏掉藏在复合命令中的一段；
- `=~`：对整条命令做正则。

**K7 `args.<名称>`** 取参数顶层字段：
- 字符串原样，其他类型为规范化 JSON（键排序、紧凑）；
- 不存在时为空串；
- 不支持多级路径（`args.a.b` 报错）。

**K8 其他字段**（`event`、`agent`、`source`、`reason`、`prompt`、`iteration`、`tool_calls`、`error`、`code`、`risk`、`trigger`）：
- 取字符串值；
- `~=` 用 `fnmatchcase`，`=~` 用正则。

**K9 加载时校验**：
- 字段名必须是 K4–K8 中的一个；
- 必须是该事件有的字段（E1 表）。例如 `turn_start` 上写 `tool` 时报错；
- `args.` 后的名称不做检查，因为不同工具的参数不同。

**K10** 条件不满足时 Hook 不执行，也不消耗 `once`。

示例：

```yaml
if: tool == Bash && command ~= "git push:*"
if: path ~= "config/prod/**" || path ~= "*.env"
if: tool =~ /^mcp__github__/ && args.repo != my-org/sandbox
if: reason == completed && agent == main
```

### 动作

**command**

| # | 行为 |
| --- | --- |
| A1 | `command`：字符串或非空字符串数组。<br>• 字符串：用 `execute_command` 相同的 shell（配置项 `shell`，Windows 上默认 Git Bash，没有时 PowerShell）执行，不做任何文本替换；上下文用环境变量读取（bash 写 `"$MOSSCODE_FILE_PATH"`，PowerShell 写 `$env:MOSSCODE_FILE_PATH`）；<br>• 数组：argv，不经过 shell。每个元素中的 `${FILE_PATH}`、`${PROJECT_ROOT}`、`${PYTHON}`、`${TOOL_NAME}`、`${EVENT}` 替换为对应值（替换结果是一个完整参数，不会被拆分或解释）；其他 `${…}` 加载时报错。第一个元素按 `PATH` 查找，找不到时为 Hook 失败 |
| A2 | 工作目录为项目根目录。环境变量在当前环境之上加：<br>• `MOSSCODE_HOOK_ID`、`MOSSCODE_HOOK_EVENT`、`MOSSCODE_SESSION_ID`、`MOSSCODE_PROJECT_ROOT`、`MOSSCODE_AGENT`；<br>• 工具事件另加 `MOSSCODE_TOOL_NAME`，以及 `MOSSCODE_FILE_PATH`（文件类工具目标的绝对路径，使用系统路径分隔符）；<br>• `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` |
| A3 | stdin 写入 UTF-8 JSON 上下文：`event`、`hook_id`、`session_id`、`project_root`、`agent`，以及该事件的条件字段。工具事件另有：<br>• `tool`、`args`（完整参数对象）、`path`、`command`；<br>• `post_tool_use` 另有 `result`：`content`（模型将看到的内容，最多 64 KB）、`is_error`、`code` |
| A4 | `timeout`：秒，缺省 30，范围 1–600。超时后终止整个进程树（`run_process` 的机制），算 Hook 失败 |
| A5 | 退出码 0 为成功；`pre_tool_use` 上退出码 2 为拒绝（I3）；其他非 0 为失败。stdout、stderr 各保留开头和结尾共 32 KB，供日志和 `feedback` 使用 |
| A6 | `feedback: true`：成功时把 stdout（去掉首尾空白，最多 8000 个字符）注入给模型，位置见 P1–P4。只能用于 `session_start`、`turn_start`、`pre_tool_use`、`post_tool_use`，不能与 `async` 同用。stdout 为空时不注入 |

**prompt**

| # | 行为 |
| --- | --- |
| A7 | `message`：非空字符串，最长 8000 个字符。其中 `{{字段}}` 替换为该事件条件字段的值（字段名的校验同 K9），用于“写入了 {{path}}”这类说明；替换只发生在给模型的文本中，不进入任何命令 |
| A8 | 注入位置见“提示词注入与反馈”。`prompt` 不能 `async`，也不能用于 E1 中未列出 prompt 的事件 |

**http**

| # | 行为 |
| --- | --- |
| A9 | 请求字段：<br>• `url`：必填，只允许 `http` / `https`；<br>• `method`：`POST`（缺省）、`GET`、`PUT`、`PATCH`、`DELETE`；<br>• `headers`：字符串键值，展开后只能是 ASCII（实现时补充：非 ASCII 值会让 httpx 在发送时出错，改为加载时报错）；<br>• `json`：任意 YAML 值，作为 JSON 请求体，其中的字符串值支持 `{{字段}}`（替换后仍是 JSON 字符串，不会破坏结构）；<br>• 不写 `json` 时，`POST` / `PUT` / `PATCH` 发送 A3 的上下文 JSON |
| A10 | `url` 和 `headers` 的值支持 `${VAR}`、`${VAR:-默认值}`（与 MCP 配置相同），加载时展开；变量未设置时该 Hook 报错跳过。`url` 中不支持 `{{字段}}` |
| A11 | `timeout` 同 A4。用 `httpx` 发送，不跟随重定向；2xx 为成功，其他状态码、连接错误、超时为失败；响应体最多读取 64 KB，日志中保留前 500 个字符 |

**agent**

| # | 行为 |
| --- | --- |
| A12 | `prompt`：必填，非空字符串。本章只做加载和校验：执行时记录 `skipped`（原因：子 Agent 动作尚未实现），每个会话对每条这样的 Hook 提示一次。不拒绝、不注入。接口按 SubAgent 一章的需要预留（见“设计”） |

### 执行控制

| # | 行为 |
| --- | --- |
| R1 | 同一事件的 Hook 按顺序执行：用户级 → 项目级 → 本地，同一文件内按书写顺序。同步 Hook 逐条等待；后台 Hook 启动后不等待，继续下一条 |
| R2 | `pre_tool_use`：第一条拒绝（静态、动态或 `on_error: block`）之后，后面的 `pre_tool_use` Hook 不再执行 |
| R3 | `once: true`：每个会话最多执行一次。条件满足、开始执行即算一次，执行失败也算。`/clear` 和恢复会话时重置，然后才触发新会话的 `session_start` |
| R4 | `async: true`：后台执行，不阻塞主流程，结果只进入日志和失败提示 |
| R5 | `async` 不能用于：`pre_tool_use`、`prompt` 动作、`feedback: true`、`on_error`。违反时加载报错 |
| R6 | `pre_send`、`post_receive`、`permission_request` 上的 command、http、agent 动作总是后台执行；显式写 `async: false` 时加载报错（避免误以为会等待） |
| R7 | 后台 Hook 在主流程继续之后完成：失败提示经后台提示通道送到界面（与记忆提取相同）。退出时先执行 `session_end`，然后等待仍在运行的后台 Hook 最多 3 秒，超时的终止进程树并记为 `cancelled` |
| R8 | 按 Esc 取消一轮时：正在运行的同步 Hook 终止进程树，记为 `cancelled`；`pre_tool_use` 被取消时工具不执行；`post_tool_use` 被取消时工具结果照常保留。后台 Hook 不受影响 |
| R9 | 同步 Hook 的耗时不计入工具的 `timeout` |

### 拦截

| # | 行为 |
| --- | --- |
| I1 | 一次工具调用的顺序：未知工具 / JSON / 参数校验 → 硬检查（计划模式、受保护文件、危险命令）→ 显式拒绝规则 → `pre_tool_use` → 审批（需要时）→ 执行 → `post_tool_use`。前面任一步拒绝，后面的步骤都不发生 |
| I2 | 静态拒绝：命中且有 `reject` 的 Hook，先执行其 `action`（如果有，例如记录或通知；结果不影响拒绝），然后拒绝。`reject` 与 `prompt` 动作同用时加载报错（拒绝后注入没有意义） |
| I3 | 动态拒绝：`pre_tool_use` 上的 command 以退出码 2 结束。原因取 stderr，stderr 为空时取 stdout，都为空时为“未说明原因”；去掉首尾空白，最多 2000 个字符 |
| I4 | 拒绝时，工具结果为错误：<br>• 错误码 `hook_rejected`，内容为 `Hook <id> 拒绝了此调用：<原因>`；<br>• `details` 含 `executed: false` 和 `hook`；<br>• 模型据此调整，与权限拒绝的处理相同；<br>• 不弹出审批，不触发 `post_tool_use` |
| I5 | `pre_tool_use` Hook 失败（非 0 且非 2 的退出码、超时、命令找不到、HTTP 失败）：<br>• 缺省放行，继续后面的 Hook 和审批，并提示一行（O3）；<br>• `on_error: block` 时拒绝，原因为 `Hook <id> 执行失败，按配置阻止此调用：<失败原因>` |
| I6 | Hook 只能收紧：任何 Hook 的结果都不会把“需要审批”变成“放行”，也不会越过拒绝规则和硬检查。`bypassPermissions` 档位和只读命令的自动放行同样受 `pre_tool_use` 约束 |
| I7 | 同一批并行的只读调用，各自的 `pre_tool_use` / `post_tool_use` 并行执行；Hook 脚本需要自己处理并发 |

### 提示词注入与反馈

| # | 行为 |
| --- | --- |
| P1 | `session_start` 的 prompt 和 feedback：<br>• 合成一个新的补充类型 `hook-context`，放在可缓存前缀中（`active-skills` 之后），整个会话的每次请求都带；<br>• 不进入历史，所以压缩不会摘要掉；<br>• `/clear` 和恢复会话时清空，由新的 `session_start` 重新生成 |
| P2 | `turn_start` 的 prompt 和 feedback：以 `hook-notification` 补充（请求级，与 `system-reminder` 的位置相同）附在本轮的每次请求上；本轮结束后不再发送 |
| P3 | `pre_send` 的 prompt：以 `hook-notification` 附在当次请求上 |
| P4 | `pre_tool_use`（未拒绝）和 `post_tool_use` 的 prompt 和 feedback 附在该工具结果的末尾，格式为 `<hook-notification hook="id">…</hook-notification>`。它们随结果进入历史和存档 |
| P5 | 每个注入块写明来源 Hook 的 `id`。限额：<br>• `hook-context` 合计 8000 个字符，超出的部分截断并提示；<br>• 每次请求的 `hook-notification` 合计 8000 个字符；<br>• 每个工具结果附加的 Hook 内容合计 8000 个字符 |
| P6 | `write_file` / `edit_file` 成功后，同步 `post_tool_use` Hook 运行前后比较目标文件的 `(mtime_ns, size)`。有变化时在结果末尾附加：`文件已被 Hook <id> 修改（如格式化）；再次编辑前请重新读取。` 有多条 Hook 时写最后一条运行的 `id`。`FileState` 不更新，模型下次编辑前必须重新读取。后台 Hook 的修改检测不到，文档建议格式化类 Hook 用同步 |
| P7 | 上下文估算（`estimate`）包含 `hook-context`；请求级的 `hook-notification` 不计入阈值判断（与 `system-reminder` 相同） |

### 失败处理与可观测

| # | 行为 |
| --- | --- |
| O1 | 每次执行（包括跳过和拒绝）追加一行 JSON 到当前会话目录的 `hooks.log`，字段如下。不记录环境变量、请求头、stdin 内容。会话目录不存在时按需创建（与 `tool-results` 相同）<br>• `time`、`hook`、`source`（user / project / local）、`event`、`agent`、`action`；<br>• `outcome`：`ok`、`failed`、`rejected`、`timeout`、`cancelled`、`skipped`；<br>• `duration_ms`、`exit_code` 或 `status`；<br>• `detail`：失败原因或输出末尾，最多 500 个字符；URL 去掉查询串 |
| O2 | 内存中保留本次运行最近 50 条执行记录，供 `/hooks` 显示 |
| O3 | 失败提示：`Hook <id>（<事件>）失败：<原因>；已忽略。` 交互界面中显示一行，`-p` 中打印到 stderr（stream-json 为 `{"type":"notice"}`）。同一条 Hook 在一轮内只提示一次，其余只写日志 |
| O4 | 拒绝不另外提示：工具行中已显示 `hook_rejected` 的结果 |
| O5 | 启动提示列出：配置错误（C4、C5）、未确认的项目级文件（S3、S4） |
| O6 | `/status` 增加一行：`Hook：已启用 N 条（项目级未确认 M 条）`；`--no-hooks` 时为“已关闭” |

### 信任与安全

| # | 行为 |
| --- | --- |
| S1 | 用户级文件直接生效。项目级的 `.mosscode/hooks.yaml` 和 `.mosscode/hooks.local.yaml` 都需要确认（已确认 Q2），每个文件单独确认 |
| S2 | 确认记录在 `~/.mosscode/projects/<项目键>/hooks-trust.json`，按文件名记录确认时内容的 SHA-256。文件内容变化后需要重新确认；文件删除后记录保留，不影响其他文件 |
| S3 | TUI 中有未确认的项目级文件时，在 `session_start` 之前弹出选择面板：<br>• 列出文件路径和每条 Hook 的 `id`、事件、条件、动作摘要（命令原文或 URL）；<br>• 选项：“信任并启用”（写入记录）、“本次不启用”；<br>• 选择“本次不启用”后，`/hooks reload` 时再次询问 |
| S4 | `-p` 中未确认的项目级文件不加载，stderr 提示：`项目 Hook 文件 <路径> 未经确认，未启用；请在交互界面中启动一次并确认，或使用 --trust-project-hooks。` `--trust-project-hooks` 只在本次运行信任，不写入记录 |
| S5 | 三个 Hook 文件加入受保护列表：模型不能用 `write_file` / `edit_file` 写入或编辑。即使通过命令改写，内容哈希变化后也需要重新确认 |
| S6 | Hook 以 MossCode 进程的权限运行，不经过工具审批。这一点写进文档：Hook 是用户自己的自动化，项目级 Hook 需要先读一遍再信任 |

### /hooks 命令

| # | 行为 |
| --- | --- |
| U1 | `/hooks`（别名 `/hook`）：按来源分组列出 Hook，还列出加载时跳过的 Hook 及原因、未确认的文件、本次运行最近 10 条执行记录。<br>每条 Hook 显示：`id`、事件、条件、动作摘要、标记（reject / once / async / on_error）、状态（已启用、未确认、`once` 已执行） |
| U2 | `/hooks <id>`：<br>• 来源文件、完整配置（`headers` 的值显示为 `***`）；<br>• 该 Hook 本次运行的执行记录；<br>• `hooks.log` 的路径 |
| U3 | `/hooks reload`：重新加载三个文件，报告新增、移除、出错的 Hook；变化的项目级文件按 S3 询问。`once` 状态按 `id` 保留 |
| U4 | `-p "/hooks"`：与 `/skills` 相同，提示只能在交互界面中使用，退出码 2 |

## 设计

### 模块

新增包 `mosscode/hooks/`：

| 文件 | 职责 |
| --- | --- |
| `events.py` | 事件名、每个事件的条件字段、可用动作、执行方式（E1 表的数据形式） |
| `conditions.py` | 条件解析（K1–K3、K9）与求值（K4–K8）；路径和命令的匹配调用 `permissions.path_matches`、`security.command_segments`、`security.starts_with`，别名用 `permissions.ALIASES` |
| `config.py` | 读取三个文件、校验（C、H、R5、R6、A 节的校验）、`${VAR}` 展开（复用 `mcp_config._substitute`，改名为公共函数 `substitute_env`）、信任记录（S1–S4） |
| `actions.py` | 四种动作：command 复用 `run_process` 与 `Shell`；http 用 `httpx.AsyncClient`；prompt 做 `{{字段}}` 替换；agent 为占位 |
| `engine.py` | `HookEngine`：匹配、顺序、`once`、同步与后台执行、失败处理、日志、提示、结果汇总 |

改动的现有模块：

| 模块 | 改动 |
| --- | --- |
| `permissions.py` | `authorize(..., gate=None, notify=None)`：硬检查和 `decide()` 之后、若不是拒绝，先 `await gate(operation)`；`gate` 返回拒绝的 `Decision("deny", "hook_rejected", …)` 或 `None`。`exit_plan_mode` 在模式检查之后、审批之前调用 `gate(None)`。`notify(request)` 在 `_ask()` 之前调用（`permission_request`）。受保护列表加入三个 Hook 文件 |
| `tools/base.py` | `ToolRegistry.hooks`（`HookEngine` 或 `None`）和 `agent`（`main` / `fork`）；`execute()` 把 `gate`、`notify` 传给 `authorize`，执行后运行 `post_tool_use` 并附加注入内容（P4、P6）；`subset()` 带上 `hooks`，并把 `agent` 设为 `fork` |
| `prompts.py` | `PREFIX_KINDS` 加入 `hook-context`；新增 `hook_context()`、`hook_notification()` 构造函数 |
| `agent.py` | `AgentLoop.hooks`（只有主对话设置）；`_model()` 前触发 `pre_send` 并合并其注入，合并 `turn_notes`（本轮注入）和 `hook_context`；收到回复后触发 `post_receive`；`_compact()` 成功后触发 `compact`；`estimate()` 计入 `hook_context` |
| `conversation.py` | 持有引擎；`start()` 首次加载后、`clear()`、`resume()` 触发 `session_start` / `session_end`，并重置 `once` 和 `hook_context`；`reply()` 触发 `turn_start`（注入交给 `AgentLoop.turn_notes`）、`error`、`turn_end`；`close()` 触发 `session_end` 并等待后台 Hook（R7）；后台失败经 `_emit(Notice)` 送出 |
| `cli.py` | `--no-hooks`、`--trust-project-hooks`；`-p` 的提示与 `/hooks` 退出码 2 |
| `tui.py` | 信任选择面板（S3）：沿用 `/model` 的选择面板，`_initialize` / `_run_resume` 中在 `session_start` 之前等待；回调链式进行，不在命令处理中等待选择结果（避免 Textual 消息循环死锁）；退出时等待后台 Hook |
| `commands.py` | `CommandUI.choose(..., on_cancel=None)`：Esc 收起面板时回调（信任面板据此视为“本次不启用”） |
| `builtin_commands.py` | `/hooks`；`/status` 的 Hook 一行 |
| `pyproject.toml` | 依赖中显式加入 `httpx` |

### 数据结构

```python
@dataclass(frozen=True)
class Condition:
    field: str                  # "tool"、"path"、"args.command" ……
    op: str                     # "==" "!=" "~=" "=~"
    value: str                  # tool 已换算别名，path 已规范化
    regex: re.Pattern | None = None

@dataclass(frozen=True)
class ConditionGroup:
    items: tuple[Condition, ...]
    any: bool = False           # True: ||；False: &&
    text: str = ""              # 原文，供 /hooks 显示

@dataclass(frozen=True)
class Action:
    type: Literal["command", "prompt", "http", "agent"]
    command: str | tuple[str, ...] | None = None
    message: str = ""
    url: str = ""
    method: str = "POST"
    headers: tuple[tuple[str, str], ...] = ()
    json_body: Any = None       # 配置中的 json 字段
    has_json: bool = False      # 区分没写 json 与 json: null
    prompt: str = ""            # agent
    timeout: float = 30.0
    feedback: bool = False

@dataclass(frozen=True)
class Hook:
    id: str
    event: str
    source: Literal["user", "project", "local"]
    path: Path
    condition: ConditionGroup | None
    action: Action | None       # None 只在静态拒绝时出现（H4）
    reject: str | None = None
    on_error: Literal["ignore", "block"] = "ignore"
    once: bool = False
    background: bool = False    # async，或 R6 的事件
    raw: dict = {}              # 原始配置，供 /hooks <id>

@dataclass(frozen=True)
class HookEvent:
    name: str
    agent: Literal["main", "fork"] = "main"
    fields: Mapping[str, str] = field(default_factory=dict)    # 条件字段的字符串值
    payload: Mapping[str, Any] = field(default_factory=dict)   # stdin JSON 的其余部分（args、result ……）
    file_path: Path | None = None           # MOSSCODE_FILE_PATH / ${FILE_PATH}
    tool: str = ""                          # MOSSCODE_TOOL_NAME / ${TOOL_NAME}

@dataclass(frozen=True)
class HookOutcome:
    rejection: tuple[str, str] | None = None   # (hook id, 原因)
    notes: tuple[tuple[str, str], ...] = ()    # (hook id, 注入文本)
    ran: tuple[str, ...] = ()                  # 执行过的同步 Hook，P6 取最后一条

class HookEngine:
    hooks: tuple[Hook, ...]
    problems: tuple[str, ...]          # 加载问题，启动提示与 /hooks
    untrusted: tuple[HookFile, ...]    # 需要确认的项目级文件；pending_trust() 去掉本次已拒绝的
    recent: deque[HookRun]             # O2
    async def fire(self, event: HookEvent) -> HookOutcome
    def trust(self, file) / decline(self, file) / reload(self) -> (新增, 移除)
    def use_session(self, session) -> None     # 切换日志位置
    def new_session(self, session=None) -> None    # 重置 once 与 agent 提示
    def begin_turn(self) -> None               # 失败提示按轮去重（O3）
    async def drain(self, timeout: float) -> None   # R7
    on_notice: Callable[[str], None] | None
    agent_runner: Callable[[Hook, HookEvent], Awaitable[None]] | None  # SubAgent 一章接入
```

### 数据流

**一次工具调用**（`ToolRegistry.execute`）：

1. 未知工具、JSON、参数校验失败 → 直接返回错误（不触发 Hook）。
2. `authorize(call_id, tool, params, gate=…)`：
   - `operation()` 硬检查失败，或 `decide()` 为拒绝 → 返回拒绝；
   - 调用 `gate`：构造 `HookEvent("pre_tool_use", fields={tool, path, command, args.*})`，执行 `engine.fire()`。有拒绝 → `Decision("deny", "hook_rejected", …)`；`gate` 返回的注入内容暂存；
   - `decide()` 为 `ask` 时：`notify` 触发 `permission_request`（后台），再弹出审批。
3. 允许 → `_invoke`（受工具超时约束）。
4. 记录目标文件的 `(mtime_ns, size)`（仅 `write_file` / `edit_file` 成功时）；执行 `post_tool_use`。
5. 合并 `pre_tool_use` 和 `post_tool_use` 的注入，以及 P6 的提示，附在结果末尾（P4、P5）。
6. 返回结果。被取消时按 R8 处理，保留 `ExecutionCancelled` 的结果语义。

**一轮**（`Conversation.reply`）：

```
turn_start  → AgentLoop.turn_notes
AgentLoop.run:
  每次迭代：pre_send（prompt 注入当次请求；其他动作在后台）→ _model() → post_receive（后台）
  工具执行（见上）
  压缩成功 → compact
RunStopped
  reason == error → error
turn_end
```

**会话**：

```
启动：加载 Hook 文件 → （TUI）确认项目级文件 → start() 加载上下文 → session_start(source=new 或 resume)
/clear：session_end(reason=clear) → 清空 → new_session() → session_start(source=clear)
恢复：session_end(reason=resume) → 切换存档 → new_session() → session_start(source=resume)
退出：session_end(reason=exit) → drain(3 秒) → 关闭
```

`-c` / `--resume` 启动时，第一次 `session_start` 的 `source` 为 `resume`，不另外触发 `new`。

### 事件与界面

- 不新增运行事件类型：拒绝体现为工具结果，失败和跳过体现为 `Notice`，后台失败经 `on_memory` 回调。
- TUI 的信任面板沿用审批面板的样式和按键（上下选择、回车确认、Esc 等同“本次不启用”）。
- 工具行中被 Hook 拒绝的调用与权限拒绝显示方式相同，原因文字中有 Hook 的 `id`。

## 取舍

- **复用匹配语义，不复用规则语法。** 权限规则是 `Tool(pattern)`，只表达一个工具加一个模式；Hook 需要组合多个字段和取反，所以用课程的 `字段 运算符 值`。字段取值和通配算法直接调用权限模块，同一个模式在两处含义相同。
- **`.local` 文件也需要确认。** 原建议中 `.local` 直接执行。但 `.gitignore` 不能保证它不被提交，克隆来的仓库可以带上它。用户自己写的文件只需确认一次，改动后再确认一次，成本很低。
- **默认放行。** 用户要求 Hook 失败不中断主流程。`on_error: block` 是额外的选项，只对拦截有意义，缺省行为不变。
- **不向 shell 命令做文本替换。** 课程的 `$FILE_PATH` 写法最直观，但文件名来自模型，可以是 `a.py; rm -rf ~`。代价是 shell 字符串里要写 `"$MOSSCODE_FILE_PATH"`；argv 形式可以直接写 `${FILE_PATH}`，是推荐写法。
- **高频事件只允许后台的外部动作。** `pre_send` / `post_receive` 每轮可能几十次，同步命令会直接拖慢交互；需要同步处理的场景改用 `turn_start` / `turn_end`。
- **注入位置跟随事件。** 统一放到“下一轮”最简单，但 `post_tool_use` 的格式化说明要紧跟结果，`session_start` 的项目状态要贯穿会话。按事件区分后，缓存前缀只在会话边界变化。
- **会话级注入不进历史。** 进历史会被压缩摘要掉，也会在恢复后重复；放在前缀区与 `active-skills` 同样处理。代价是恢复会话时要重新跑 `session_start` 才能得到最新内容，这正是期望的行为。

## 不做

- `agent` 动作的真实执行（等 SubAgent 一章，通过 `agent_runner` 接入）。
- `once` 的持久化（跨进程、跨会话记住已执行）。
- Hook 的显式优先级（`priority` 字段）。
- `turn_start` 拦截用户消息、`turn_end` 阻止结束并让模型继续（Claude Code 的 UserPromptSubmit 阻止、Stop 阻止）。
- Hook 修改工具参数（改写命令或路径后再执行）。
- HTTP 响应参与决策（如返回 `{"decision": "reject"}`）；需要远程决策时用命令动作调用。
- 从界面编辑 Hook、按 `id` 在本地文件中禁用某条项目级 Hook。
- 移除的事件 `startup`、`shutdown`、`file_change`、`command_execute`（D11）。

## 已确认的决定（2026-10-27）

| # | 问题 | 决定 |
| --- | --- | --- |
| Q1 | 用户的设计中“动作必填”。纯拦截规则（命中即拒绝，如“禁止编辑 `config/prod/**`”）写 `reject: "原因"` 时，是否允许省略 `action`？ | 允许省略（H4）。否则用户只能写一个无意义的动作来满足格式 |
| Q2 | 项目级 `.mosscode/hooks.local.yaml` 是否也需要确认？（之前的建议是直接执行） | 需要确认（S1，见“取舍”） |
| Q3 | `pre_send` / `post_receive` / `permission_request` 上的命令、HTTP 一律后台执行，写 `async: false` 报错，是否可以接受？ | 接受（R6）。确需同步的场景用 `turn_start` / `turn_end` |

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `hooks/events.py`、`conditions.py`：事件表、条件解析与求值；`config.py`：三层读取、校验、`${VAR}` 展开、信任记录 | — | 单元测试（AC1–AC4） |
| T2 | `hooks/actions.py`：command（字符串与 argv、环境变量、stdin、超时终止进程树、退出码）、http（本地测试服务器）、prompt 替换、agent 占位；`pyproject.toml` 加 `httpx` | T1 | 动作测试（AC5–AC7） |
| T3 | `hooks/engine.py`：匹配、顺序、拒绝短路、`once`、同步与后台、失败处理、`hooks.log`、最近记录、提示去重、`drain` | T1、T2 | 引擎测试（AC8、AC9、AC14） |
| T4 | 工具接入：`authorize` 的 `gate` 与 `notify`、`ToolRegistry` 的 `pre_tool_use` / `post_tool_use`、注入附加、P6、`subset` 的 `agent`、受保护文件、取消处理 | T3 | 权限与工具测试（AC10–AC13） |
| T5 | 循环与会话接入：`hook-context` 与 `hook-notification`、`pre_send` / `post_receive` / `compact` / `error`、轮次与会话事件、`/clear`、恢复、关闭、估算 | T3 | 假提供商测试（AC15–AC17） |
| T6 | 界面与命令行：TUI 信任面板、`/hooks`、`/status`、提示；`-p` 的 `--no-hooks`、`--trust-project-hooks`、stderr 与 stream-json 提示、`/hooks` 退出码 2；退出等待 | T4、T5 | `FakeUI` 测试、`tests/test_cli.py`（AC18–AC20） |
| T7 | 文档：新增 `docs/reference/hooks.md`（文件、字段、事件表、条件、动作、注入、信任、示例）；更新 README、`docs/reference/commands.md`、`docs/acceptance.md`（本章人工验收）；本规格状态改为已实现 | T1–T6 | `tests/test_docs.py`；全量测试 |

T3 完成后，T4 和 T5 互不依赖。

## 验收

| # | 标准 | 方法 |
| --- | --- | --- |
| AC1 | 配置：<br>• 三层文件追加合并，顺序为用户级 → 项目级 → 本地；<br>• YAML 错误跳过整个文件，提示中没有文件内容；<br>• 重复 `id`、未知事件、未知字段（如 `asnyc`）、缺 `action`、`reject: true`、`reject` 用在 `post_tool_use`、`on_error` 用在其他事件 → 只跳过该 Hook 并提示路径与 `id` | 单元测试（临时目录作为 home 和项目） |
| AC2 | 条件解析：<br>• `&&` 与 `\|\|` 混用、缺运算符、非法正则、`args.a.b` → 报错；<br>• 引号中的 `&&` 不拆分；<br>• `turn_start` 上写 `tool`、`{{path}}` 用于 `turn_end` → 报错 | 单元测试 |
| AC3 | 条件求值：<br>• `tool == Bash` 命中 `execute_command`，`tool ~= mcp__x__*`；<br>• `path ~= "src/*.py"` 不命中 `src/a/b.py`，`src/**/*.py` 命中；Windows 下大小写不敏感；绝对模式不命中项目内路径；<br>• `command ~= "git push:*"` 命中 `cd x && git push -f`，不命中 `git pushx`；<br>• `!=` 是 `==` 的取反；<br>• 不存在的 `args.x` 为空串 | 单元测试，与权限规则同一组样例对照 |
| AC4 | 信任：<br>• 未确认的项目级文件不加载，确认后写入哈希，内容变化后再次未确认，用户级文件不需要确认；<br>• `--trust-project-hooks` 生效但不写入记录；<br>• 模型 `write_file` 写 `.mosscode/hooks.yaml` 被 `policy_protected` 拒绝 | 单元测试 + 权限测试 |
| AC5 | 命令动作：<br>• stdin 收到上下文 JSON，环境变量齐全，工作目录为项目根目录；<br>• 中文输出不乱码；<br>• argv 中的 `${FILE_PATH}` 含空格和 `;` 时作为一个参数传入，没有被 shell 解释；<br>• 字符串命令用配置的 shell 执行；<br>• 超时后进程树已结束；<br>• 命令找不到为失败 | 动作测试（测试脚本用 `sys.executable`） |
| AC6 | HTTP 动作（本地测试服务器）：<br>• `json` 中的 `{{tool}}` 替换正确，包含引号的值不破坏 JSON；<br>• 不写 `json` 时发送上下文；<br>• `${VAR:-默认}` 展开，未设置的变量让该 Hook 加载失败；<br>• 非 2xx、连接拒绝、超时为失败；<br>• 不跟随重定向 | 动作测试 |
| AC7 | prompt 与 agent：`{{path}}` 替换；agent 动作记为 `skipped`，每会话提示一次，不影响工具执行 | 单元测试 |
| AC8 | 引擎：<br>• 顺序；第一个拒绝之后的 `pre_tool_use` 不执行；<br>• `once` 在条件不满足时不消耗，失败也算一次，`new_session()` 后重置；<br>• 后台 Hook 不阻塞，`drain` 超时后终止并记为 `cancelled` | 引擎测试 |
| AC9 | 日志与提示：<br>• `hooks.log` 每次执行一行，字段齐全，不含请求头与环境变量；<br>• 同一 Hook 一轮内多次失败只提示一次；<br>• 拒绝不提示 | 引擎测试 |
| AC10 | 拦截顺序：<br>• 拒绝规则命中的调用不执行 Hook；<br>• 计划模式下写入被 `plan_blocked` 拒绝，不执行 Hook；<br>• 静态拒绝、退出码 2 拒绝 → 结果为 `hook_rejected`，原因正确，不弹审批，工具未执行，不触发 `post_tool_use`；<br>• `bypassPermissions` 下仍被拒绝；<br>• Hook 退出码 0 不会让需要审批的调用跳过审批 | 权限 + 工具测试 |
| AC11 | 失败处理：`pre_tool_use` 命令退出码 1 → 放行并提示；同样配置加 `on_error: block` → 拒绝，原因含失败信息 | 工具测试 |
| AC12 | `post_tool_use`：<br>• 工具失败时 `error == true` 的条件命中；<br>• feedback 和 prompt 附在结果末尾并进入存档；<br>• 同步 Hook 修改了刚写入的文件 → 结果含“已被 Hook 修改”，随后 `edit_file` 返回 `stale_read`，重新读取后可以编辑；<br>• 未修改时没有该提示 | 工具测试 |
| AC13 | fork 子 Agent：工具调用触发 `pre_tool_use`，`agent == fork` 的条件命中，拒绝生效；子 Agent 的请求不触发 `pre_send`；`turn_start` 的注入不在子 Agent 的请求中 | 假提供商测试 |
| AC14 | 取消：`pre_tool_use` 运行中取消 → 进程树结束，工具未执行；`post_tool_use` 运行中取消 → 工具结果保留 | 工具测试 |
| AC15 | 注入位置（三种协议分别检查）：<br>• `session_start` 的 prompt 和 feedback 在前缀区，同一会话的每次请求都有，压缩后仍在，`/clear` 后由新的 `session_start` 重新生成；<br>• `turn_start` 的在本轮每次请求的请求级位置，下一轮没有；<br>• `pre_send` 的只在当次请求；<br>• 超出 8000 字符时截断 | 提供商请求结构测试 |
| AC16 | 事件时机：<br>• 一轮中依次触发 `turn_start`、`pre_send`、`post_receive`（每次请求各一次）、工具事件、`turn_end`；`turn_end` 的 `reason` 与 `RunStopped` 一致；<br>• 模型错误时 `error` 在 `turn_end` 之前；<br>• 压缩成功触发 `compact` 且 `trigger` 正确，失败不触发 | 假提供商测试 |
| AC17 | 会话事件：<br>• 启动 `session_start(new)`；<br>• `/clear` 依次为 `session_end(clear)`、`session_start(clear)`；<br>• 恢复依次为 `session_end(resume)`、`session_start(resume)`；<br>• `-c` 启动只有 `session_start(resume)`；<br>• 关闭时 `session_end(exit)` 后等待后台 Hook | `Conversation` 测试 |
| AC18 | `/hooks`：<br>• 列表含来源、标记、跳过原因、未确认文件、最近记录；<br>• `/hooks <id>` 中 `headers` 的值被隐藏；<br>• `/hooks reload` 报告变化；<br>• `-p "/hooks"` 退出码 2 | `FakeUI` 测试、`tests/test_cli.py` |
| AC19 | `-p`：<br>• 未确认的项目级 Hook 被跳过，stderr 有提示；<br>• Hook 失败在 stderr，stream-json 为 `notice`；<br>• `--no-hooks` 时不执行任何 Hook；<br>• 退出前 `session_end` 已执行 | `tests/test_cli.py` |
| AC20 | 全量测试通过；README 二级标题不变，文档链接有效 | `pytest`、`tests/test_docs.py` |

人工验收（写进 `docs/acceptance.md`）：

- 在验收项目中放一个 `.mosscode/hooks.yaml`：一条拒绝编辑 `config/prod/**` 的静态拒绝，一条 `post_tool_use` 格式化 Python 文件的命令，一条 `session_start` 的 `git log -3 --oneline` 反馈。启动时确认弹出信任面板并列出三条；选择“本次不启用”后 `/hooks` 显示未确认；
- 信任后让模型编辑 `config/prod/app.yaml`，确认工具行显示 Hook 拒绝及原因，没有弹出审批，模型改变做法；
- 让模型写一个格式不规范的 Python 文件，确认文件被格式化，模型随后的修改先重新读取；
- 问“最近的提交是什么”，确认模型不调用工具即可回答（来自 `session_start` 的注入）；`/clear` 后再问，同样能回答；
- 修改 `hooks.yaml` 后 `/hooks reload`，确认再次询问；
- 写一条超时 1 秒、执行 `sleep 5` 的 `post_tool_use` Hook，确认提示一行失败、工具结果正常、`hooks.log` 记录 `timeout`；
- `/review` 运行时，用一条 `agent == fork` 的 `pre_tool_use` 拒绝 `execute_command`，确认审查子 Agent 收到拒绝并继续；
- `-p` 运行同一项目（未带 `--trust-project-hooks`），确认 stderr 提示项目 Hook 未启用；
- Windows Terminal 中检查信任面板与 `/hooks` 输出的显示。
