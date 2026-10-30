# Git 工作树

子 Agent 可以在独立的 git 工作树（worktree）中运行：它改的是工作树中的文件，主目录不变；结束时没有改动就自动删除，有改动就保留下来，由你或主 Agent 决定是否合并。也可以用 `/worktree create` 手动建一个工作树，在另一个终端里使用。

工作树只在 git 仓库中可用。项目不在仓库中、仓库还没有提交、是裸仓库或找不到 git 时，要求隔离的调用返回 `worktree_unavailable`，`/worktree` 给出原因。

## 子 Agent 隔离

两种方式让子 Agent 隔离运行，任一种成立即隔离：

- 定义中写 `isolation: worktree`（见 [子 Agent](subagents.md#字段)）；
- 模型调用 `agent` 工具时传 `isolation: "worktree"`。参数不能关闭定义中的隔离。

不隔离的情况：

- Fork（省略 `agent_type`）继承当前对话，不能隔离，同时要求隔离时返回 `isolation_unsupported`；
- 子 Agent 的有效档位为 `plan` 时不创建工作树，它在主目录只读运行，结果首行说明“计划模式下未创建工作树”；
- Skill fork 没有隔离选项；Hook 的 `agent` 动作使用的定义要求隔离时照常隔离。

### 运行

1. 在子 Agent 的任务中创建工作树，工具行显示“创建工作树…”。后台任务也一样：主 Agent 立即拿到任务 id，创建在任务中进行。名称为 `agent-` 加 8 位十六进制，分支为 `mosscode/agent-…`，基于主目录当前的 `HEAD`。
2. 第一条用户消息之前插入 `<worktree-context>`：工作目录、主项目目录（不要修改）、分支与基准提交、父 Agent 给出的绝对路径要换成工作树中的路径、主目录有未提交改动时的提醒，以及“有改动会保留，无需删除工作树”。
3. 子 Agent 照常运行（[第十三章](subagents.md) 的流程不变）。

隔离的子 Agent：

- **根目录**是工作树中与项目对应的目录。MossCode 在仓库的子目录 `pkg` 中启动时，根目录为 `<工作树>/pkg`。相对路径、工具行和 Hook 的 `path` 都相对这个根目录；
- **工具**只有以工作树为根的内置工作区工具：`read_file`、`write_file`、`edit_file`、`execute_command`、`glob`、`grep`（再按定义的 `tools` / `disallowed_tools` 过滤）。MCP 工具和 Skill 的脚本工具作用于主目录，所以不提供；定义中列出了它们时，`/agents <名称>` 显示“隔离时不可用”；
- **文件读取记录**从零开始；
- **系统提示**中环境信息的工作目录与 git 状态取工作树；项目指令沿用主项目已加载的内容；
- 记忆、会话目录、子 Agent 对话记录、Hook 配置、用量统计都属于主会话，进程的工作目录不变。

### 权限

隔离的子 Agent 的权限视图以工作树根为项目根，其余与 [子 Agent 的权限](subagents.md#权限) 相同：

- 路径规则相对工作树根匹配，例如 `Edit(src/**)` 匹配工作树中的 `src`；
- 主目录中的文件对它而言在项目之外：读取需要审批（后台返回 `approval_required`），**写入和编辑直接拒绝**（`path_blocked`，不能授权）；
- 受保护的配置文件同时包括主项目和工作树中的副本（`permissions*.yaml`、`hooks*.yaml`、`mcp.yaml`、`.mcp.json`），写入返回 `policy_protected`；
- `execute_command` 在工作树中运行，但命令本身能访问的路径不受限制（路径检查是应用层限制，不是系统沙箱），仍按档位和规则审批。

### 结束

子 Agent 结束时（完成、失败、取消或会话关闭，取消时也不跳过）检查工作树中的改动：

- **没有改动**：删除工作树和分支，结果末尾附“[工作树] 无改动，已删除。”
- **有改动**：保留，结果末尾附：

  ```
  [工作树] 已保留：.mosscode/worktrees/agent-1a2b3c4d
  分支 mosscode/agent-1a2b3c4d，基于 abc1234；未提交 3 个文件，独有提交 2 个。
  合并：git merge mosscode/agent-1a2b3c4d；查看：git -C .mosscode/worktrees/agent-1a2b3c4d status；删除：/worktree remove agent-1a2b3c4d
  ```

MossCode 不自动提交，也不自动合并。创建时的警告（如未能链接某个目录）附在这一段之前。

“改动”指：

- 未提交的文件：`git status` 中的修改、新增和删除，包括未跟踪的文件（被 `.gitignore` 忽略的不算）；
- 独有提交：分支上不属于任何其他本地分支或远程分支的提交。分支已合并进 `main` 后不再算改动。

MossCode 自己放进去的链接和复制的文件不算改动。检查失败（例如 git 出错）时按有改动处理，工作树保留。

进程被强制结束时，工作树留在磁盘上，由 [自动清理](#自动清理) 处理（没有改动时）。

## 目录与分支

| 项 | 位置 |
| --- | --- |
| 工作树 | `<仓库根>/.mosscode/worktrees/<名称>` |
| 分支 | `mosscode/<名称>` |
| 元数据 | `<仓库根>/.mosscode/worktrees/.meta/<名称>.json`：名称、路径、分支、基准提交、创建时间、手动或临时、链接与复制的文件、使用中的进程与任务 |

- 名称中的 `/` 在目录名和分支名中换成 `+`：`team/alice` 的目录为 `team+alice`，分支为 `mosscode/team+alice`。
- 第一次创建时在 `.git/info/exclude` 中加一行 `/.mosscode/worktrees/`，主目录的 `git status` 不会显示工作树。不修改 `.gitignore` 和 `.git/config`。
- 主仓库的 git hooks（如 `pre-commit`）在工作树中同样生效。

名称规则：

- 1–64 个字符，可以用 `/` 分段；每段只含字母、数字、`.`、`_`、`-`；
- 段不能为空，不能是 `.` 或 `..`，不能含 `..`，不能以 `.` 或 `-` 开头，不能以 `.` 或 `.lock` 结尾，不能是 Windows 保留的设备名（`con`、`nul`、`com1` 等，含扩展名时也不行）；
- 与已有的工作树只差大小写的名称会被拒绝（Windows 上文件名不分大小写）。

## 初始化

新建的工作树只有仓库中跟踪的文件。创建后依次做三件事，每一步失败都只给出警告，工作树仍然可用：

1. **复制本地配置**：`.mosscode/permissions.local.yaml`、`.mosscode/hooks.local.yaml`（相对项目目录）。
2. **链接目录**：配置项 `worktree_links` 中列出的路径（相对仓库根），在工作树中建成指向主目录的链接，例如 `node_modules`、`.venv`，免得每个工作树重新安装。先尝试符号链接；Windows 上没有创建符号链接的权限时，目录改用 junction；文件无法链接时给出警告。
3. **复制被忽略的文件**：仓库根的 `.worktreeinclude` 用 `.gitignore` 的语法列出要复制的文件，例如：

   ```
   .env
   config/local/*.json
   ```

   只复制未被跟踪的文件；已链接的路径跳过。

`config.yaml`、`.env` 这类含凭据的文件不会自动复制，需要时写进 `.worktreeinclude`。仓库有子模块时，工作树中的子模块不初始化（给出警告）。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/worktree`（同 `/worktree list`，别名 `/worktrees`） | 列出 MossCode 管理的工作树：名称、手动或临时、状态（使用中、有改动及数量、无改动、删除未完成、目录不存在），分支、基准提交、创建时间和路径；没有元数据的目录标为“未登记” |
| `/worktree create <名称>` | 创建工作树（`manual: true`，永不被自动清理），输出路径、分支、警告和进入目录的命令。已存在时直接显示，不运行 git |
| `/worktree remove <名称> [--discard]` | 删除工作树和分支。有改动时拒绝并列出改动，加 `--discard` 才删除；正被子 Agent 使用时拒绝 |
| `/worktree clean` | 立即执行一次 [自动清理](#自动清理)，报告删除和跳过的数量及原因 |

- 主对话运行期间只能用 `/worktree` 和 `/worktree list`，其他子命令要等运行结束。
- `/tasks <id>` 显示隔离任务的工作树路径、分支和最终的保留或删除结果。
- `-p "/worktree ..."` 与其他本地命令一样以退出码 2 结束。

手动创建的工作树可以在另一个终端中进入，在那里启动 MossCode：项目指令从工作树根向下读取，不会把主项目的 `AGENTS.md` 读两次；记忆与会话按工作树的路径分开。

## 删除

删除不使用 `git worktree remove`：工作树中有指向主目录的 junction 时，它会跟着 junction 删掉主目录中的内容。MossCode 的做法是：

1. 确认路径就在 `.mosscode/worktrees/` 之下，在元数据中标记“删除中”；
2. 先移除链接本身，再删除目录（不跟随链接，只读的 git 对象先改为可写）；
3. `git worktree prune`，删除分支，删除元数据。

中途失败时（例如 Windows 上有程序占用其中的文件），`/worktree` 中显示“删除未完成”。关闭占用的程序后再次 `/worktree remove <名称>` 会直接删除，不再检查改动；临时工作树也会由自动清理重试。

## 自动清理

启动后在后台运行一次（不阻塞启动），之后每 24 小时运行一次。只删除同时满足以下条件的工作树：

- 有 MossCode 的元数据，名称是子 Agent 的临时格式（`agent-` 加 8 位十六进制），不是 `/worktree create` 手动创建的；
- 没有正在使用它的 MossCode 进程（记录的进程已不存在也算未使用）；
- 元数据超过 `worktree_stale_hours` 小时未更新；
- 没有改动。

删除了工作树时提示一行。`-p` 不运行自动清理。

## 配置

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `worktree_links` | `[]` | 链接进每个工作树的路径列表，相对仓库根；不能是绝对路径，不能含 `..`，不能在 `.git` 或 `.mosscode/worktrees` 中 |
| `worktree_stale_hours` | `24` | 自动清理的过期时间，0–8760 小时；`0` 关闭自动清理（`/worktree clean` 仍可用，此时清理所有未使用、无改动的临时工作树） |

## Hook 字段

- 隔离的子 Agent 的工具事件和 `subagent_start` / `subagent_stop` 带字段 `worktree`：相对仓库根的工作树路径，如 `.mosscode/worktrees/agent-1a2b3c4d`；不隔离时为空。
- `subagent_stop` 另有 `worktree_kept`：`true` / `false`，不隔离时为空。
- 工具事件的 `path` 相对工作树根，`MOSSCODE_FILE_PATH` 是工作树中的绝对路径。Hook 命令仍在主项目目录运行。

详见 [Hook](hooks.md#事件)。

## 搜索

主 Agent 的 `glob`、`grep` 不进入 `.mosscode/worktrees`（工作树中是整个仓库的副本，会让每个结果出现多次）；`path` 指向其中时照常搜索。主 Agent 读写工作树中的文件不受额外限制。
