# 第十五章：Agent Teams

日期：2026-11-01。状态：规格已通过（2026-11-01）；T1–T7 已实现，收尾补上了 TM6 与 MG4 的两处缺口，Windows Terminal 后端做过真实冒烟测试（见“实现说明”）。tmux 后端未经人工验证（本机没有 tmux）；人工验收 M115–M123 已通过（2026-11-01，见“实现说明”）。

来源：
- 课程第 15 章：理论学习、Python 源码解析、实战演练中的参考规格（G1–G18、F1–F66、AC1–AC30）。
- 用户的初步设计：
  - 主 Agent 升级为 Team Lead，团队长期存在；成员通过共享任务列表和邮箱协作；
  - 团队对象记录名称、Lead、成员名单和持久化位置；每个成员记录角色、工作目录、后端、是否需要计划审批；
  - 两种后端：独立终端窗格（tmux 或终端自带的分屏）和进程内协程；自动按优先级选择，不静默降级；
  - 协作工具：共享任务的增删改查（含依赖）和点对点消息；主入口和普通子 Agent 看不到；
  - 计划审批：需要审批的成员把计划发给 Lead，Lead 用结构化的批准或拒绝回复；
  - 消息：名称注册表加邮箱文件；窗格后端还要唤醒目标窗格；支持广播和结构化的协议消息；
  - Lead 拆解目标为有依赖的任务、创建成员、用 git 合并、解决冲突或回滚并报告；
  - 成员空闲时通知 Lead，可以从磁盘恢复；
  - 协调者模式：配置能力开关加用户环境变量；去掉写工具，保留读工具和 shell；
  - 不做分布式团队、成员之间的流式通信、复杂的依赖约束；
  - 消息字段：发送者、正文、时间戳、已读、摘要；邮箱用锁文件，拿不到就重试，锁太旧视为过期。
- 对初步设计的 18 条评审建议，以及三个待定点的推荐：Lead 从启动起带团队工具、团队跨重启保留、合并前需用户确认。用户确认“按推荐写规格”。
- 当天的调查：
  - 第十三章子 Agent 运行时（`agents/runtime.py`、`agents/tasks.py`）、工具注册表（`tools/base.py`）、权限视图（`permissions.py`）、后台通知（`conversation.py`、`agent.py`）、会话存档（`session.py`、`paths.py`）；
  - 第十四章工作树管理（`worktree/manager.py`）；
  - 界面（`tui.py`）、命令（`builtin_commands.py`）、配置（`config.py`）、命令行（`cli.py`）、Hook 事件（`hooks/events.py`）；
  - 本机环境：Windows 11，Python 3.12，git 2.45.2；`wt.exe` 位于 `%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe`；没有 tmux。

本规格按 MossCode 的现状调整课程方案和用户的初步设计，调整逐条列在“对课程方案的调整”中。

## 目标

用户在交互界面中把一个较大的目标交给 MossCode。主 Agent 作为 Team Lead：
- 把目标拆成有依赖关系的任务，创建至多 4 个成员；每个成员在自己的 git 工作树中工作；
- 成员按依赖顺序领取任务，完成后提交到自己的分支，通过消息向 Lead 汇报；
- Lead 在用户确认后按依赖顺序把成员分支合并到主目录当前分支；冲突时放弃这次合并，交给成员解决后再合并。

团队保存在用户目录中，跨重启保留。重启后成员处于停止状态，向它发消息就会从记录中恢复。

## 实验

已确认的事实：

| # | 检查 | 结果 |
| --- | --- | --- |
| F1 | `where wt`、`where tmux` | 有 `wt.exe`，没有 tmux |
| F2 | 工具环境中的 `WT_SESSION` | 为空。Bash 工具不在 Windows Terminal 中运行；用户的 MossCode 在 Windows Terminal 中运行时它非空 |

T5 开始时在 Windows Terminal 1.24.12741.0 中做的实验（2026-11-01）。为了不打扰用户正在用的窗口，实验在单独打开的 Windows Terminal 窗口中进行，由该窗口里的进程执行 `wt`：

| # | 操作 | 要确认的问题 | 结果 |
| --- | --- | --- | --- |
| E1 | 设置临时环境变量 `X` 的进程执行 `wt -w 0 split-pane -d <dir> -- python <脚本>`，脚本把 `os.environ.get('X')` 写入文件；`new-tab` 也试一次 | 新窗格是否继承调用者的环境变量 | 继承，两种方式都读到了 `X`。所以只在当前 shell 中设置的 `api_key_env` 变量对成员窗格也可用；BE9 的检查仍然保留，以防用户从别处启动 |
| E2 | 在窗口 A 的窗格中执行 `wt -w 0 split-pane`，此前另开了窗口 B | `-w 0` 是否总是调用者所在的窗口 | 是。新窗格出现在窗口 A，即使窗口 B 更晚打开；每个窗格的 `WT_SESSION` 各不相同 |
| E3 | 窗格中的进程分别以退出码 0 和 1 结束（用户的 settings.json 没有覆盖 `closeOnExit`） | 默认的 `closeOnExit` 行为 | 退出码 0：窗格关闭。非 0：窗格保留，显示“[process exited with code 1 …]”，要用户手动关闭 |
| E4 | `taskkill /PID <pid> /T /F` 结束窗格中的 Python 进程 | 窗格是否随之关闭，是否留下孤儿进程 | 进程及其子进程都被结束，没有孤儿；但被结束的进程退出码为 1，窗格按 E3 保留 |

由此得到的做法：

- 窗格中运行的是一个很小的包装模块 `python -m mosscode.teams.pane <参数>`：它以子进程运行 `python -m mosscode <参数>`，无论子进程如何结束，自己都以退出码 0 结束。正常退出和强制结束后窗格都会关闭（已验证：`taskkill` 结束成员后窗格关闭）。用 `cmd /c "… & exit 0"` 包装不可行：引号经过 `wt` 后被拆开。
- `wt` 把参数中的 `;` 当作子命令分隔符，命令行中的每个 `;` 都要写成 `\;`。
- venv 中的 `python.exe` 是启动器，它再启动真正的解释器。成员写入的进程号（`os.getpid()`）是解释器的；强制结束这个进程树后启动器以 1 退出，包装模块仍以 0 结束。

## 现状

| 位置 | 现在的做法 | 与本章的关系 |
| --- | --- | --- |
| 子 Agent（`agents/runtime.py`） | `definition_loop` 为一次性的子 Agent 建一个空白对话；`drive` 运行到结束；结果作为工具结果或 `<task-notification>` 返回 | 成员需要长期存在的对话：一轮结束后等待新消息，而不是结束（RT1）。构建方式复用 `definition_loop` 的做法 |
| 一次性任务（`agents/tasks.py`） | `TaskManager` 记录 `t1`、`t2`…，`/tasks`、`task_output`、`task_stop` 操作它们 | 成员不放入 `TaskManager`；共享任务另用 `team_task_*` 命名，避免与 `/tasks` 混淆（D9） |
| 禁用工具（`GLOBAL_BAN`） | `agent`、`load_skill`、`memory`、`exit_plan_mode`、`task_output`、`task_stop` 不给子 Agent；Fork 仍提供，但调用时拒绝 | 团队工具加入这个集合，子 Agent 和 Fork 都不能用（TL3） |
| 工作树（`worktree/manager.py`） | `create(name, manual=True)` 允许 `/` 嵌套，展平为 `+`，分支 `mosscode/<flat>`；目录已存在时快速恢复；`in_use` 记录进程号，使用中拒绝删除；过期清理跳过 `manual` | 成员工作树用 `team-<team>/<member>` 创建，分支为 `mosscode/team-<team>+<member>`，按手动工作树对待，不会被过期清理删除（TM6） |
| 隔离的工具（`create_default_registry(root)`） | 第十四章中隔离的子 Agent 以工作树为根，重新创建工作区工具，不带 MCP 和 Skill 脚本工具 | 成员沿用这个做法（RT2） |
| 权限视图（`PolicyView`） | 共用父策略的规则、待审批请求和目录；模式、会话授权和请求队列各自独立；后台子 Agent 不能请求审批 | 进程内成员需要在 Lead 空闲时也能请求审批（PM1） |
| 审批显示（`tui.py`） | 审批只作为 Lead 这一轮的事件流中的 `ApprovalRequest` 显示；`_show_approval` 同一时刻只保存一个请求 | 需要一条不依赖 Lead 这一轮的审批通道，并能排队显示（PM2，待调查实现细节） |
| 后台通知（`AgentLoop.notifications`、`Conversation._tasks_ready`、TUI `_schedule_notify`） | 后台结果放入 `notifications`。Lead 忙时在下一个迭代边界并入；Lead 空闲时界面开始一个合成的通知轮 | Lead 收到的团队消息走同一条路（LD3） |
| 计划模式（`tools/plan.py`） | `exit_plan_mode` 提交计划，由权限策略询问用户 | 需要审批的成员用一个同名的成员版本，提交给 Lead（PA2） |
| 会话存档（`session.py`） | `~/.mosscode/projects/<key>/sessions/`；`load_session(path)` 从记录重建历史，包括压缩；过期会话按天清理 | 团队放在 `~/.mosscode/projects/<key>/teams/`，不参与会话清理；成员记录复用相同格式和 `load_session`（TM2、ID4） |
| 命令行（`cli.py`） | `-p` 无界面运行一次；`--config`、`--provider`、`--permission-mode` | 窗格成员通过隐藏参数启动另一个 MossCode 进程（BE5） |
| Hook（`hooks/events.py`） | 工具事件带 `agent` 字段（`main`、`fork`、`subagent`）；`subagent_start`、`subagent_stop` | 成员的工具事件照常触发，`agent` 为 `teammate`；不新增事件（HK1） |
| 配置（`config.py`） | 平铺的键 | 新增 `teammate_mode`、`team_max_members`、`coordinator_mode`（CF1） |

## 对课程方案的调整

| # | 课程方案或初步设计 | 调整 | 原因 |
| --- | --- | --- | --- |
| D1 | 窗格后端只有 tmux；用 `send-keys` 往窗格的标准输入写一行来唤醒成员 | 支持 tmux 和 Windows Terminal 分屏（`wt -w 0 split-pane`）。所有窗格成员每 0.5 秒轮询自己的邮箱，不用按键唤醒（BE4） | 本机没有 tmux，有 Windows Terminal。Windows Terminal 不能向窗格发送按键，也不能按 id 关闭窗格。成员进程本来就要读邮箱文件，轮询对两种后端都适用 |
| D2 | 优先级：在 tmux 中用 tmux；否则 iTerm2；否则进程内 | `auto`：在 tmux 会话中且有 tmux 时用 tmux；否则，MossCode 运行在 Windows Terminal 中（`WT_SESSION` 非空）且找得到 `wt.exe` 时用 Windows Terminal；否则用进程内。选择结果和原因写入工具结果和 `/team`。显式指定的窗格后端不可用时报错，不降级（BE1–BE3） | 用户要求“不静默降级”；`auto` 本身是用户选择的降级规则，告诉用户最终用了哪个就不算静默 |
| D3 | 关闭窗格：tmux `kill-pane` | 先发 `shutdown_request`；10 秒内没有退出，就结束记录的进程号及其子进程（BE7） | Windows Terminal 不能按 id 关闭窗格；窗格中运行总以 0 退出的包装模块，结束成员进程后窗格随之关闭（实验 E3、E4） |
| D4 | 团队工具由协调者模式或特性开关动态加入 | Lead 从启动起就有团队工具，工具列表在会话内不变；协调者模式在启动时决定，会话内不变（TL1、CO2） | 工具列表变化会让提示词缓存失效 |
| D5 | Lead 用 LLM 驱动 `git merge`，冲突时自行解决或 `--abort` | 新增 `team_merge` 工具：检查前提、请用户确认、执行 `git merge --no-ff`；冲突时 `git merge --abort`，返回冲突文件。Lead 不在主目录解决冲突，而是让成员把主分支合入自己的工作树，解决后提交，再合并一次。回滚只用 `merge --abort`，从不 `reset --hard`（MG1–MG9） | 协调者模式下 Lead 没有编辑工具，无法解决冲突；把合并做成工具才能保证“合并前需用户确认”和“主目录脏就停下”，而不依赖模型自觉 |
| D6 | 团队存放在 `~/.mewcode/teams/<team>/` | 存放在 `~/.mosscode/projects/<key>/teams/<team>/`，包括 `config.json`、`tasks.json`、`mailbox/`、`transcripts/`（TM2） | 团队属于一个项目；不同项目的同名团队互不影响，与会话存档的位置一致 |
| D7 | 消息字段：from、text、timestamp、read、summary；类型由正文中的 JSON 判断 | 增加 `id` 和 `type` 字段，协议消息的字段直接放在消息中（MB2） | 结构化字段比解析正文可靠，Lead 回复时可以引用 `request_id` |
| D8 | 计划审批的回复由成员轮询邮箱时识别 | 需要审批的成员以计划模式启动。它的 `exit_plan_mode` 把计划作为 `plan_approval_request` 发给 Lead，然后结束这一轮；Lead 用 `send_message` 的 `plan_approval_response` 类型回复；批准后运行时把成员切出计划模式（PA1–PA6） | 复用 MossCode 已有的计划模式和只读限制，不需要另一套“只读阶段” |
| D9 | 共享任务工具叫 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate` | 叫 `team_task_create`、`team_task_get`、`team_task_list`、`team_task_update`；消息工具叫 `send_message`（TL2） | MossCode 已有 `/tasks`、`task_output`、`task_stop`，指一次性子 Agent |
| D10 | 重启后进程内成员不恢复；只探测窗格成员是否存活 | 团队跨重启保留。重启后不在运行的成员标为“已停止”；向它发消息时从它的记录恢复对话，再投递消息。还在运行的窗格成员照常工作（ID3–ID6） | 用户确认“团队跨重启保留”；第八章的压缩保证长期成员的上下文不会无限增长 |
| D11 | 协调者模式只留 Agent、团队工具、读工具和 bash | 去掉 `write_file`、`edit_file`、MCP 工具和 Skill 脚本工具；保留读工具、`execute_command`、团队工具、`agent`、`load_skill`、`memory`。规格写明这是角色约束，不是安全边界（CO1–CO6） | MCP 和 Skill 脚本可以写文件；`load_skill` 只给规程文本，`memory` 只写笔记，不改项目代码。`execute_command` 本来就能写文件，去掉写工具只是让模型按协调者的方式工作 |
| D12 | 成员数量没有上限；成员可以再建团队 | 每个团队最多 4 个成员（`team_max_members` 可调到 8）；成员不能建团队、不能创建子 Agent、不能跨团队发消息（TL4、TM5） | 每个成员都是一份完整的模型调用成本 |
| D13 | 团队名重复时自动加 `-2` 后缀 | `team_create` 遇到已有的同名团队时打开它，而不是另建（TM4） | 团队跨重启保留，重启后 Lead 需要重新接管原来的团队 |

## 需求

术语：
- **团队**：一个项目中的一组长期协作的 Agent，由一个 Lead 和至多 4 个成员组成。
- **Lead**：交互界面中的主 Agent，固定名称为 `lead`。
- **成员**：按 Agent 定义创建的长期 Agent，有自己的名称、工作树和对话。
- **当前团队**：本会话的 Lead 正在操作的团队；除 `team_create` 外，团队工具都作用于它。

### 团队模型与存储

- **TM1** 团队名和成员名：小写字母、数字和 `-`，1–32 个字符，以字母或数字开头。成员名在团队内唯一，不能是 `lead` 或 `*`。不合法时报 `invalid_name`，不自动改名。
- **TM2** 位置：`~/.mosscode/projects/<key>/teams/<team>/`，`<key>` 与会话存档相同（`paths.project_key`）。目录中有：
  - `config.json`：团队与成员名单；
  - `tasks.json`：共享任务；
  - `mailbox/<name>.json`：每个参与者（包括 `lead`）一个邮箱；
  - `transcripts/<member>.jsonl`：成员对话记录，格式与会话记录相同。

  会话的按天清理不触及 `teams/`。
- **TM3** `config.json` 记录：版本、团队名、描述、创建时间、Lead 持有者（进程号、会话 id、接管时间）和成员列表。每个成员记录：
  - `name`；`agent_id`（`<member>@<team>`）；定义名 `agent_type`；`model`；
  - `backend`（`in-process` / `windows-terminal` / `tmux`）及选择原因；
  - `status`（`running` 一轮进行中 / `idle` 等待消息 / `stopped` 不在运行）；
  - `require_plan_approval`；当前权限模式；
  - `worktree`（名称、路径、分支），不使用工作树时为 null；
  - 窗格成员的 `pid`、心跳时间、tmux 窗格 id；
  - 记录文件的相对路径；最初的任务说明；累计用量；创建时间。
- **TM4** 一个团队同一时刻只由一个 Lead 进程持有。`team_create` 打开已有团队时：
  - 持有者的进程仍在运行时拒绝（`team_held`）；
  - 否则接管：更新持有者，并按存活情况刷新各成员状态（ID3）。
- **TM5** 每个团队最多 `team_max_members` 个成员（默认 4），超出时 `team_spawn` 报 `too_many_members`。已停止的成员也计入，名额只随团队删除释放（CM4）。一个 Lead 同一时刻只有一个当前团队（ID6），所以同时运行的成员也不超过这个数。
- **TM6** 成员默认使用工作树：
  - 用工作树管理器创建，名称 `team-<team>/<member>`，分支 `mosscode/team-<team>+<member>`，基于主目录当前的 HEAD；
  - 按手动工作树对待，不被过期清理删除；
  - 成员运行时工作树记录 `in_use`（运行它的进程号和“团队 <team> 成员 <member>”），`/worktree remove` 因此拒绝删除它。窗格成员的进程启动后用自己的进程号重写，Lead 退出后工作树仍算使用中。

  `team_spawn` 可以用 `isolation: none` 让成员在主目录工作，适合只读的角色。不在 git 仓库中时，默认的隔离方式报 `worktree_unavailable`。
- **TM7** `config.json` 和 `tasks.json` 的每次修改都在锁内完成：重新读取、修改、写临时文件、`os.replace`（MB4）。另一个进程写入的内容不会被覆盖。

### 邮箱与锁

- **MB1** 每个参与者一个邮箱文件，内容是消息数组。发送是在锁内追加到收件人的邮箱；读取是在锁内取出未读消息并标为已读。
- **MB2** 消息字段：

  | 字段 | 说明 |
  | --- | --- |
  | `id` | `m-` 加 8 位随机十六进制 |
  | `from` | 发送者名称 |
  | `type` | 消息类型，见下表 |
  | `body` | 正文 |
  | `summary` | 摘要 |
  | `timestamp` | 带时区的 ISO 时间 |
  | `read` | 是否已读 |

  各类型的额外字段：

  | 类型 | 发送者 | 额外字段 |
  | --- | --- | --- |
  | `text` | 任何人 | `summary` 必填，不超过 80 字 |
  | `plan_approval_request` | 成员（运行时代发） | `request_id`、`plan` |
  | `plan_approval_response` | 只有 Lead | `request_id`、`approve`、`feedback` |
  | `shutdown_request` | 只有 Lead | `reason` |
  | `shutdown_response` | 成员 | `request_id`、`approve`、`reason` |
  | `idle_notification` | 成员（运行时代发） | `reason`（`completed` / `failed` / `interrupted`）、`completed_tasks`、`summary`（最后回复的前 300 字） |

- **MB3** 锁：
  - 锁文件是 `<file>.lock`，以 `O_CREAT|O_EXCL` 创建，内容为进程号、时间和随机标记；
  - 拿不到锁时重试 10 次，每次随机等待 5–100 毫秒；重试期间锁换过持有者（标记变了，说明别人在正常推进）时重新计数，但总等待不超过 5 秒；
  - 释放时删除锁文件；Windows 上其他等待者正读取锁文件时删除会失败，短暂重试（至多 0.2 秒）；
  - 锁文件存在超过 10 秒视为过期：删除后重试；
  - 重试用尽时报错（`mailbox_busy` 或 `team_busy`），不丢弃消息；
  - 持锁期间只做文件读写，不等待模型或网络。
- **MB4** 写入：先写同目录的临时文件，再 `os.replace`。Windows 上 `os.replace` 遇到 `PermissionError` 时短暂重试至多 3 次。读到损坏的 JSON 时，把原文件改名为 `<file>.corrupt-<时间>` 保留，按空文件继续，并在界面中提示。
- **MB5** 截断：每个邮箱保留全部未读消息和最近 100 条已读消息。未读消息超过 500 条时，发送报 `mailbox_full`。
- **MB6** `body` 不超过 20000 字。广播 `to: "*"` 投递给除发送者外的全部成员，不包括 Lead。只有 `text` 类型可以广播。
- **MB7** 投递给已停止的成员时，消息照常写入它的邮箱；由 Lead 发送时，同时触发恢复（ID4）。

### 共享任务

- **TK1** 任务字段：
  - `id`：从 1 起的递增数字字符串；
  - `subject`（不超过 120 字）、`description`；
  - `status`：`pending` / `in_progress` / `completed`；
  - `owner`：成员名、`lead` 或空；
  - `blocked_by`：任务 id 列表；
  - 创建和更新时间。

  `blocks` 不存储，由 `blocked_by` 反推后显示。
- **TK2** 一个任务的 `blocked_by` 全部 `completed` 时，它是“就绪”的。不就绪的任务不能改为 `in_progress`（`task_blocked`）。
- **TK3** 依赖只能指向已有的任务。添加依赖时检查环，形成环时报 `dependency_cycle`。
- **TK4** 状态只能按 `pending → in_progress → completed` 前进。只有 Lead 能把 `completed` 改回 `pending`（重新打开）。
- **TK5** 成员只能领取没有负责人或已经归自己的任务；Lead 可以指定任何负责人。负责人必须是团队成员或 `lead`。
- **TK6** 一个团队最多 200 个任务。不提供删除；不再需要的依赖由 Lead 移除。
- **TK7** 任务变为 `completed` 时，Lead 的下一次团队更新中包含一行：“任务 N 完成（成员）”，以及因此就绪的任务和它们的负责人（LD3）。

### 工具与可见性

- **TL1** Lead 的团队工具从启动起就在工具列表中，会话内不变。条件：
  - 交互界面；
  - 工具开启；
  - 子 Agent 开启，即有 Agent 目录。

  `-p` 无界面模式不提供团队工具（见“取舍”）。
- **TL2** Lead 的工具：

  | 工具 | 参数 | 作用 |
  | --- | --- | --- |
  | `team_create` | `name`、`description` | 新建或打开团队，设为当前团队 |
  | `team_spawn` | `name`、`agent_type`（默认 `general`）、`prompt`、`require_plan_approval`（默认 false）、`isolation`（`worktree` / `none`，默认 `worktree`） | 创建成员并开始它的第一轮 |
  | `team_merge` | `member` | 见“合并” |
  | `team_delete` | `force`（默认 false） | 删除当前团队 |
  | `team_task_create` | `subject`、`description`、`blocked_by` | 新建共享任务 |
  | `team_task_get` | `id` | 查看一个任务 |
  | `team_task_list` | 无 | 列出全部任务 |
  | `team_task_update` | `id`、`status`、`owner`、`subject`、`description`、`add_blocked_by`、`remove_blocked_by` | 修改任务 |
  | `send_message` | `to`（成员名或 `*`）、`type`、`body`、`summary`、`request_id`、`approve`、`feedback` | 发消息 |

  没有当前团队时，除 `team_create` 外的工具报 `no_team`。
- **TL3** 普通子 Agent 和 Fork 不能用团队工具：团队工具名加入 `GLOBAL_BAN`。Fork 照旧仍提供这些工具，但调用时拒绝，以保持缓存前缀。
- **TL4** 成员的工具：
  - 定义允许的工具，去掉 `GLOBAL_BAN`；
  - 加上 `send_message`（`to` 可以是 `lead`、其他成员名或 `*`）、`team_task_create`、`team_task_get`、`team_task_list`、`team_task_update`；
  - 需要计划审批的成员，加上成员版本的 `exit_plan_mode`（PA2）。

  成员没有 `team_create`、`team_spawn`、`team_merge`、`team_delete`、`agent`。
- **TL5** `send_message` 校验：
  - 收件人必须存在；
  - `text` 必须有 `summary`；
  - 只有 Lead 能发 `plan_approval_response` 和 `shutdown_request`；`plan_approval_response` 的 `request_id` 必须是该成员尚未答复的请求；
  - 只有成员能发 `shutdown_response`；
  - `idle_notification` 和 `plan_approval_request` 只由运行时发送，模型调用时拒绝。

### 后端

- **BE1** 配置 `teammate_mode`：`auto`（默认）、`in-process`、`windows-terminal`、`tmux`。
- **BE2** `auto` 的选择顺序：
  1. 环境变量 `TMUX` 非空且找得到 `tmux` → `tmux`；
  2. 环境变量 `WT_SESSION` 非空且找得到 `wt.exe` → `windows-terminal`；
  3. 否则 → `in-process`。

  `team_spawn` 的结果、成员记录和 `/team` 写明选中的后端和原因，例如“auto：检测到 Windows Terminal”。
- **BE3** 显式指定 `windows-terminal` 或 `tmux` 但条件不满足时，`team_spawn` 报 `backend_unavailable` 并说明缺什么，不改用其他后端。
- **BE4** 窗格成员是另一个 MossCode 进程，每 0.5 秒轮询自己的邮箱。不使用按键唤醒。
- **BE5** 窗格成员的启动命令：

  ```text
  <python> -m mosscode --team-dir <团队目录> --team-member <成员名> [--config <Lead 使用的配置路径>] [--provider <名称>]
  ```

  工作目录为成员的工作树或主目录。`--team-dir` 和 `--team-member` 是隐藏参数，不出现在 `--help` 中。
  - Windows Terminal：`wt -w 0 split-pane -d <cwd> -- <python> -m mosscode.teams.pane <参数>`，参数中的 `;` 写成 `\;`（E3、E4：包装模块总以 0 退出，窗格随之关闭）；
  - tmux：`tmux split-window -h -P -F "#{pane_id} #{pane_pid}" -c <cwd> <命令>`。

  已由[完善七](../refinement-07-team-views/spec.md)修改（WN1、WN2）：Windows Terminal 改为团队窗口中的 `new-tab`，tmux 改为 `new-window -d`，不再分割 Lead 的窗口。
- **BE6** 子进程启动后，在 `config.json` 中写入自己的进程号，此后每 5 秒更新一次心跳。20 秒内没有写入时：
  - `team_spawn` 报 `backend_failed`，附上子进程写入的错误（如果有）；
  - 成员标为 `stopped`，工作树保留；
  - 不改用进程内。
- **BE7** 关闭窗格成员：
  1. 发 `shutdown_request`；
  2. 成员在当前一轮结束后回复 `shutdown_response`，然后退出；
  3. 10 秒内进程没有退出时，结束它的进程树：Windows 上用 `taskkill /PID <pid> /T /F`，POSIX 上结束进程组；tmux 再执行 `kill-pane`。
- **BE8** 窗格成员的界面：
  - 窗格中运行成员模式的 MossCode 界面，标题为“成员 <name> · 团队 <team>”，显示它的对话和工具调用；
  - 输入框只用于回答审批；Esc 中断当前一轮；
  - 不接受用户直接输入新任务，界面中给出提示，见“取舍”。

  已由[完善七](../refinement-07-team-views/spec.md)修改：界面在团队窗口的标签页中（WN1、WN4）；审批同时转发到 Lead，先回答的一方生效（FA1、FA2）。
- **BE9** 窗格成员按 Lead 的配置路径重新加载配置。提供商密钥来自环境变量但子进程中没有时，子进程把错误写入 `config.json` 后退出，Lead 收到 `backend_failed`：“窗格中缺少环境变量 X：设为用户环境变量，或改用 in-process”。MossCode 不通过命令行或文件转交密钥。
- **BE10** 进程内成员是 Lead 进程中的一个 asyncio 任务，等待时不轮询；收到投递时立即唤醒。来自其他进程的消息仍靠 0.5 秒的轮询发现。

### 成员运行时

- **RT1** 成员的一轮：
  1. 取出全部未读消息，合并为一条用户消息：`<incoming-messages>` 中每条一段，包括发送者、类型、摘要和正文；
  2. 运行到这一轮结束；
  3. 运行时向 Lead 发 `idle_notification`；
  4. 进入 `idle`，等待下一条消息。

  第一轮的消息是 `team_spawn` 的 `prompt`，来自 `lead`。
- **RT2** 成员的对话按 `definition_loop` 的方式构建：
  - 定义的系统提示词，加上团队附录（RT3）；
  - 以工作树为根，用 `create_default_registry` 重新创建工作区工具，不带 MCP 和 Skill 脚本工具；
  - 项目说明沿用主项目的，git 快照取工作树的；
  - 最大轮数取定义和主对话的较小值，按每一轮计算；
  - 开启自动压缩（第八章）。
- **RT3** 团队附录，内容固定，只因团队名和成员名不同：
  - 你是团队 `<team>` 的成员 `<name>`，Lead 是 `lead`；
  - 你的文字回复不会被任何人看到，汇报和提问都要用 `send_message`；
  - 工作流程：查看任务列表 → 领取就绪的任务（`owner` 设为自己，状态设为 `in_progress`）→ 完成 → 在工作树中 `git commit` → 把任务标为 `completed` → 用 `send_message` 向 Lead 汇报；
  - 你的工作树、分支和主分支名；Lead 要求你解决合并冲突时，在工作树中执行 `git merge <主分支>`，解决后提交；
  - 收到 `shutdown_request` 时，结束手头的工作后用 `shutdown_response` 回复。
- **RT4** 成员的每一轮写入 `transcripts/<member>.jsonl`，格式与会话记录相同，包括压缩记录。进程内成员的用量计入会话总用量；每一轮结束时，用量写入 `config.json` 中该成员的记录。
- **RT5** 一轮失败（提供商错误、达到最大轮数）时，`idle_notification` 的 `reason` 为 `failed`，`summary` 为原因；成员仍然转为 `idle`，可以继续接收消息。
- **RT6** 一轮进行中又收到消息时，消息留在邮箱，在下一轮开始时合并读取。成员不在一轮中途插入消息。

### 计划审批

- **PA1** `require_plan_approval: true` 的成员以计划模式启动，只能使用只读工具和只读命令。
- **PA2** 成员版本的 `exit_plan_mode` 与主 Agent 的参数相同。调用时：
  1. 运行时生成 `request_id`，把 `plan_approval_request` 发给 Lead；
  2. 工具结果为“计划已提交给 Lead，等待审批；收到回复前不要继续”；
  3. 这一轮结束。因为请求本身已经告诉 Lead，这一轮不发 `idle_notification`。
- **PA3** Lead 在团队更新中看到完整的计划，用 `send_message`（`type: plan_approval_response`）回复，附上 `request_id`、`approve`、`feedback`。
- **PA4** 批准时，运行时先把成员切换到 PM3 算出的模式，再把回复作为消息投递：“计划已批准”加上反馈。
- **PA5** 拒绝时，成员仍在计划模式，回复作为消息投递：“计划被拒绝”加上反馈。成员修改后再次提交，生成新的 `request_id`；旧的 `request_id` 失效。
- **PA6** 界面中为提交和答复各显示一行：“成员 alice 提交了计划，等待 Lead 审批”、“Lead 批准了 alice 的计划”。用户不需要操作。

### 成员的权限

- **PM1** 成员的工具调用按第六章的规则检查，使用自己的 `PolicyView`。标签为“成员 <name>”，根目录为它的工作树。
- **PM2** 进程内成员的审批显示在主界面中：
  - 来源写为“成员 <name>”；
  - Lead 正在运行一轮或空闲时都能显示；
  - 多个请求按到达顺序逐个显示；
  - 成员等待审批时状态仍为 `running`。

  需要调查 `_show_approval` 当前只保存一个请求的问题，见 T2。
  窗格成员的审批显示在自己的窗格中。（已由[完善七](../refinement-07-team-views/spec.md) FA1 修改：同时出现在 Lead 的界面中。）
- **PM3** 成员的权限模式在每一轮开始时，按第十三章的规则用当前会话模式重新计算：
  - 会话处于计划模式时，成员也是计划模式；
  - 项目定义不能放宽会话的模式；
  - 用户定义和内置定义可以使用自己的模式。

  需要计划审批而尚未批准的成员保持计划模式。窗格成员的模式由 Lead 写入它在 `config.json` 中的记录，子进程在每一轮开始时读取。
- **PM4** 成员不能请求超出自己工作树的写入例外。项目边界以工作树为准，与第十四章隔离的子 Agent 相同。

### Lead 流程

- **LD1** Lead 的系统提示词中有一节固定的团队说明：
  - 何时用团队：可以并行、互相独立的多文件改动；单个小改动不用；
  - 先建任务并写好依赖，再创建成员，至多 4 个；
  - 创建后不要轮询，团队更新会自动到达；
  - 成员都完成后，按依赖顺序用 `team_merge` 合并；
  - 冲突时让成员合入主分支、解决、提交后再合并；
  - 主目录有改动时停下来问用户；
  - 最后向用户报告结果。
- **LD2** Lead 的邮箱：
  - 进程内成员发来的消息立即被发现；其他进程的消息由 0.5 秒的轮询发现；
  - 取出后渲染为一条 `<team-update>` 通知，放入 `notifications`；
  - Lead 空闲时开始一个通知轮（第十三章 B6）；忙时在下一个迭代边界并入。
- **LD3** `<team-update>` 的内容：
  - `text`：发送者、摘要和正文，正文超过 4000 字时截断并注明；
  - `plan_approval_request`：完整的计划和 `request_id`；
  - `idle_notification`、`shutdown_response`：每条一行；
  - 任务完成和新就绪的任务（TK7）。
- **LD4** 界面为每条团队更新显示一行，例如“团队 auth：alice → lead：<摘要>”、“alice 空闲：完成任务 2”。通知轮本身照旧不显示合成的用户消息。
- **LD5** `/clear` 和 `/resume` 不停止团队，当前团队保持不变。之后 Lead 的第一条消息前注入一次 `<team-context>`，内容为团队名、成员及状态、任务概况。
- **LD6** Lead 进程正常退出时：
  1. 向全部运行中和空闲的成员发 `shutdown_request`；
  2. 总共最多等 10 秒；
  3. 结束剩余的窗格进程，取消进程内成员；
  4. 全部标为 `stopped`，清除持有者记录。

  工作树、记录和团队都保留。

### 合并

- **MG1** `team_merge(member)` 把成员分支合并到主目录当前分支，按下面的顺序检查，任一不满足就报错，不执行合并：

  | 检查 | 错误码 |
  | --- | --- |
  | 主目录是 git 仓库，HEAD 在分支上，没有进行中的合并或变基 | `main_not_ready` |
  | 主目录没有已跟踪文件的改动（已暂存或未暂存）。未跟踪的文件不阻止合并，在确认内容中列出 | `main_dirty`，附文件列表 |
  | 成员有工作树，且不在一轮中 | `no_worktree` / `member_busy` |
  | 成员工作树没有未提交的改动，包括未跟踪的文件 | `member_uncommitted` |
  | 成员分支有主分支没有的提交 | `nothing_to_merge` |
  | 依赖顺序（MG2） | `merge_order` |

- **MG2** 依赖顺序：成员已完成的任务所依赖的任务，如果归另一个成员，且那个成员的分支还有未合并到 HEAD 的提交，就报 `merge_order`：“先合并 <另一个成员>”。
- **MG3** 检查通过后请用户确认，不受权限模式和允许规则影响：
  - 内容为成员、分支、目标分支、提交列表（最多 20 条）、`git diff --stat` 和未跟踪的文件；
  - 选项为“合并”和“不合并”；
  - 无法请求审批时报 `merge_not_confirmed`。
- **MG4** 用户选择不合并时，返回 `merge_declined`，Lead 应停下来询问用户。卡片没有输入框，界面在对话中提示用户直接在输入框说明原因或改动要求；这条说明作为用户的下一条消息交给 Lead。
- **MG5** 合并：`git merge --no-ff --no-edit <branch>`。成功时返回新的提交号和变更摘要。
- **MG6** 冲突时：
  1. 用 `git diff --name-only --diff-filter=U` 收集冲突文件；
  2. 执行 `git merge --abort`；
  3. 返回 `merge_conflict`、冲突文件和下一步：让该成员在工作树中执行 `git merge <主分支>`，解决后提交，再调用 `team_merge`。
- **MG7** 其他失败时：存在 `MERGE_HEAD` 就执行 `git merge --abort`，然后返回 git 的输出。任何情况下都不执行 `reset --hard`、`checkout --` 或 `clean`。
- **MG8** `--abort` 也失败时，返回 `merge_abort_failed` 和 git 输出，并要求 Lead 立即告诉用户，不再执行其他 git 写操作。
- **MG9** 合并后成员的工作树和分支保留，成员可以继续工作，需要时先合入主分支。

### 空闲、停止与恢复

- **ID1** 成员状态：
  - `running`：一轮进行中，包括等待审批；
  - `idle`：存活，等待消息；
  - `stopped`：没有在运行的协程或进程。
- **ID2** 进程内的空闲成员在内存中等待，不重新加载记录。
- **ID3** 打开团队时（TM4），按存活情况刷新成员状态：
  - 窗格成员的进程号存活且心跳不超过 15 秒：保持原状态，继续工作；
  - 其他成员一律标为 `stopped`，包括上一个 Lead 进程中的进程内成员。
- **ID4** 恢复：Lead 向已停止的成员发消息时，
  1. 用当前的 `teammate_mode` 选择后端（可能与上次不同，结果中注明）；
  2. 从 `transcripts/<member>.jsonl` 用 `load_session` 重建对话；需要压缩时，在下一轮开始时按第八章压缩；
  3. 复用原工作树：目录存在时快速恢复；
  4. 投递消息，开始一轮。

  恢复失败时（记录损坏、工作树丢失、后端失败），`send_message` 报 `revive_failed`。这时消息已经写入邮箱，下次恢复时仍会读到。
- **ID5** `/team stop <member>` 和 Lead 的 `shutdown_request`：
  - 成员一轮结束后回复 `shutdown_response` 并停止；
  - 成员可以用 `approve: false` 加理由拒绝；
  - 10 秒内没有停止时，由 `/team stop` 强制停止（BE7）。
- **ID6** 切换当前团队：已有当前团队时，用 `team_create` 打开或新建另一个团队，
  - 原团队有 `running` 的成员时拒绝（`team_busy`）；
  - 否则先按 ID5 停止原团队的空闲进程内成员。原团队的窗格成员继续存活，下次打开时按 ID3 处理；
  - 然后切换，并释放原团队的持有者记录。

### 协调者模式

- **CO1** 两个条件同时满足时开启：配置 `coordinator_mode: true`，且环境变量 `MOSSCODE_COORDINATOR_MODE` 为 `1`、`true` 或 `yes`（不区分大小写）。只满足一个时不开启，启动时提示缺少的那一个。
- **CO2** 在启动时决定，会话内不变；没有命令可以切换。
- **CO3** Lead 的工具：
  - 去掉 `write_file`、`edit_file`、全部 MCP 工具和 Skill 脚本工具。MCP 服务器照常连接，但工具不注册到 Lead；
  - 保留读工具、`execute_command`、团队工具、`agent`、`task_output`、`task_stop`、`load_skill`、`memory`、`exit_plan_mode`。
- **CO4** 系统提示词增加协调者一节，分四个阶段：
  1. 调查：读代码，或派只读的子 Agent；
  2. 综合：拆任务、写依赖；
  3. 实施：创建成员，派发后不要自己动手；
  4. 验证：让成员或子 Agent 运行测试；合并后自己可以运行只读的检查。
- **CO5** 状态栏显示 `[协调]`；`/status` 写明“协调者模式（配置与环境变量均已开启）”。
- **CO6** 文档写明：这是角色约束，不是安全边界；`execute_command` 仍能写文件，权限规则照常适用。
- **CO7** `-p` 中忽略协调者模式，并提示“协调者模式只用于交互界面”。没有工具或没有 Agent 目录时同样忽略并提示。

### 命令

- **CM1** `/team`：列出本项目的团队，包括名称、成员数和各状态的数量、是否为当前团队、持有者。
- **CM2** `/team <name>`：显示团队详情：
  - 每个成员的状态、后端及原因、工作树和分支、最近的工具调用、用量、未读消息数；
  - 任务列表，包括状态、负责人和依赖。
- **CM3** `/team stop <member>`：停止当前团队的一个成员（ID5）。Lead 运行一轮时也可以使用。
- **CM4** `/team delete <name>`：
  1. 确认后停止全部成员；
  2. 对每个成员的工作树：分支已全部合入 HEAD 且没有改动时删除，否则保留并列出；
  3. 删除团队目录。

  团队被另一个存活的 Lead 进程持有时拒绝。`team_delete` 工具不带 `force` 时，遇到运行中的成员会拒绝；带 `force` 时与本命令相同，但不需要用户确认。
- **CM5** `/help team` 写明这些子命令。

### Hook

- **HK1** 不新增 Hook 事件。成员的工具事件照常触发：
  - `agent` 字段为 `teammate`，`agent_type` 为定义名，`worktree` 为工作树的相对路径；
  - 进程内成员使用 Lead 的 Hook 引擎；
  - 窗格成员是独立进程，按自己的启动规则加载 Hook，未确认的项目 Hook 不加载，与 `-p` 相同。

### 配置

- **CF1** 新增：

  | 键 | 默认值 | 取值 |
  | --- | --- | --- |
  | `teammate_mode` | `auto` | `auto` / `in-process` / `windows-terminal` / `tmux` |
  | `team_max_members` | 4 | 1–8 |
  | `coordinator_mode` | false | 布尔 |

  不合法时报 `ConfigError`。

### 界面

- **UI1** 有当前团队时，状态栏显示“团队 <name> 运行 N / 空闲 M”；有成员等待审批时显示“成员审批 K”。（已由[完善七](../refinement-07-team-views/spec.md)修改：输入框下方另有成员面板 PL1–PL6；“成员审批 K”计入转发到 Lead 的窗格成员审批，FA5。）
- **UI2** 成员的创建、空闲、停止、恢复、失败各显示一行说明，写明后端。
- **UI3** 审批卡片的来源写为“成员 <name>”（PM2）。合并确认使用同样的卡片，标题为“合并成员 <name> 的分支”。
- **UI4** 协调者徽标（CO5）。

## 设计

### 模块

| 模块 | 职责 |
| --- | --- |
| `teams/__init__.py` | `Team`、`Member` 数据结构；`TeamStore`：路径、名称校验、读写 `config.json`、持有与接管 |
| `teams/lock.py` | `file_lock`：锁文件、重试、过期判断；`atomic_write`、`load_json`；`TeamError`（各模块共用的错误，带错误码） |
| `teams/mailbox.py` | `Message`；`Mailbox.send/read_unread/peek`；截断 |
| `teams/board.py` | `TaskBoard`：共享任务的增改查、就绪判断、环检查。不叫 `tasks.py`，避免与 `agents/tasks.py` 混淆 |
| `teams/member.py` | `MemberRunner`：成员的一轮循环、团队附录、计划审批状态、空闲通知。进程内和窗格子进程共用 |
| `teams/backends.py` | `detect_backend`；`WindowsTerminalBackend`、`TmuxBackend`：启动、存活检查、强制结束（实现时没有单独的 `InProcessBackend`，见 T5 的实现说明） |
| `teams/runtime.py` | `TeamRuntime`：Lead 侧的入口，包括当前团队、成员表、Lead 邮箱监视、恢复、退出、审批队列 |
| `teams/merge.py` | `merge_member`：MG1–MG8 的检查与 git 操作 |
| `tools/team.py` | 九个团队工具和成员版本的 `exit_plan_mode` |
| `prompts.py` | 团队说明一节、协调者一节、团队附录 |
| `conversation.py` | 创建 `TeamRuntime`；注册团队工具；协调者模式下的工具裁剪；`close` 时关闭团队 |
| `cli.py` | 隐藏参数 `--team-dir`、`--team-member`；成员模式的启动；协调者模式的判断 |
| `tui.py` | 成员审批队列、团队状态栏、团队说明行、成员模式的界面、协调者徽标 |
| `builtin_commands.py` | `/team` |
| `config.py` | CF1 |
| `agents/runtime.py` | `GLOBAL_BAN` 加入团队工具名 |
| `worktree/manager.py` | 团队工作树：`create(..., manual=True, task=...)` 已支持 `in_use`；停止时清除 `in_use` 的入口（待调查是否要新增方法） |

### 数据结构

```python
@dataclass
class Member:
    name: str
    agent_type: str
    backend: Literal["in-process", "windows-terminal", "tmux"]
    backend_reason: str
    status: Literal["running", "idle", "stopped"] = "stopped"
    require_plan_approval: bool = False
    mode: str = "default"  # PM3: written by the lead, read by a pane member each turn.
    model: str = ""
    worktree: dict | None = None  # {"name", "path", "branch"}
    pid: int | None = None
    heartbeat: str | None = None
    pane: str | None = None  # tmux pane id.
    transcript: str = ""  # Relative to the team directory.
    prompt: str = ""
    usage: dict = field(default_factory=dict)
    created: str = ""
    error: str = ""  # Written by a pane member that could not start (BE6, BE9).

@dataclass
class Team:
    name: str
    description: str
    created: str
    lead: dict | None  # {"pid", "session", "since"}
    members: list[Member]
```

`Message` 和 `TeamTask` 的字段见 MB2 和 TK1。

### 数据流

```text
Lead team_spawn
  → TeamStore 加成员（锁内） → WorktreeManager.create("team-<t>/<m>")
  → backends.detect → InProcess: asyncio 任务 MemberRunner
                    → 窗格: wt/tmux 启动子进程 → 子进程 cli --team-member → MemberRunner
MemberRunner 一轮
  ← Mailbox.read_unread(<m>)  →  AgentLoop.run(<incoming-messages>)
  → 审批: 进程内 → TeamRuntime.approvals → TUI 卡片；窗格 → 自己的界面
  → 结束: Mailbox.send(lead, idle_notification)
TeamRuntime（Lead 进程）
  ← Mailbox.read_unread(lead)（投递事件 + 0.5 秒轮询）
  → main.notifications.append(<team-update>) → Conversation._tasks_ready → 通知轮
Lead team_merge
  → merge_member 检查 → 确认卡片 → git merge --no-ff → 冲突: merge --abort + 冲突文件
```

## 取舍

- **`-p` 不提供团队工具**：合并确认和成员审批都需要用户；`-p` 一次运行就结束，与“长期团队”的用法不符。评审建议中的“`-p` 只用进程内”因此不再需要。如果以后需要无界面的团队，再单独设计。
- **合并做成工具，并且总是确认**：`bypassPermissions` 和允许规则也不能跳过合并确认，因为这是用户在本章明确要求的决定。代价是每合并一个成员都要点一次确认。
- **窗格成员的界面不接受新任务输入**：成员只接受 Lead 的指令，避免它的上下文中出现 Lead 不知道的要求。用户需要调整成员时，告诉 Lead。
- **不转交密钥**：窗格不继承环境变量时（实验 E1 表明正常情况下会继承），成员启动失败并说明原因，而不是把密钥写进文件或命令行。代价是用户需要把密钥设为用户环境变量，或改用 `in-process`。
- **所有窗格后端都轮询**：每个成员每秒约 2 次文件读取，开销可以忽略；换来两种后端只有一条唤醒路径。
- **成员数上限按团队计**：已停止的成员也占名额，名单才稳定；要释放名额需要明确删除。

## 不做

- 分布式团队、跨机器的邮箱；成员之间的流式通信。
- 复杂的依赖约束，例如“任一完成即可”、优先级、截止时间。
- 跨团队的消息；成员创建团队或子 Agent。
- iTerm2 等其他终端的窗格后端。
- 在主目录中自动解决合并冲突；`reset --hard` 式的回滚。
- 新的 Hook 事件。
- 在 `-p` 中使用团队。

## 实现说明

- T1（2026-11-01）：`src/mosscode/teams/` 的 `lock.py`、`mailbox.py`、`board.py`、`__init__.py`；测试 `tests/test_teams_stage15.py` 44 个通过。
  - 压力测试 AC2 暴露了两个问题，已修复并写回 MB3：
    - 两个线程连续写时，一方刚释放又立刻拿到锁，另一方 10 次重试都落空。改为锁换过持有者就重新计数，总等待上限 5 秒；
    - Windows 上删除被其他线程打开读取的锁文件会失败，原先忽略这个错误，锁文件留到过期才能被别人拿到。改为短暂重试删除；
  - 修复后 AC2–AC4 连续运行 15 次全部通过。
  - `config.json` 损坏时不改名、不替换（`team_corrupt`），以免丢失成员名单；邮箱和 `tasks.json` 按 MB4 改名保留后继续。
  - TK5 的细化：成员只能修改没有负责人或归自己的任务；成员开始一个没有负责人的任务时自动成为负责人；Lead 推进没有负责人的任务时报 `no_owner`；只有 Lead 能移除依赖、清除负责人。
- T2（2026-11-01）：`teams/member.py`（成员的一轮、团队附录、`MemberPolicy`、`TeamAccess`）、`teams/runtime.py`（`TeamRuntime`：当前团队、进程内成员、Lead 邮箱的读取与 `<team-update>`）、`tools/team.py`；`tests/test_teams_stage15.py` 共 55 个通过（T2 新增 11 个），连续 5 次全部通过；相关的子 Agent、工作树、权限、配置、CLI、界面测试 343 个通过。
  - 接入：`Conversation(teams=, team_max_members=)`，CLI 只在交互界面传 `teams=True`（TL1）；配置新增 `team_max_members`（1–8）；`GLOBAL_BAN` 加入九个团队工具名；权限新增 `team` 类，团队工具不需要审批；`WorktreeManager.mark_in_use` 在成员运行时写入、停止时清除 `in_use`（也覆盖复用已有工作树的情况）。
  - 审批通道（PM2）的调查结论：`_show_approval` 只保存一个请求，且 Lead 的一轮结束时无条件隐藏卡片，成员在 Lead 一轮中途发起的请求会因此丢失、成员一直等待。改为：卡片排队，按到达顺序显示；隐藏时只丢弃已答复的请求，仍在等待的成员请求保留或接着显示；一轮结束时有卡片则不把焦点交回输入框。审批锁由各视图共用，所以同一时刻至多一个请求在等待，排队是兜底。
  - `idle_notification` 的 `summary` 就是消息本身的 `summary` 字段（MB2 的表中照旧列出），不再要求它出现在额外字段里：原先运行时发出的空闲通知因字段同名而校验失败、被静默丢弃。
  - AC11 现在是八个工具：`team_merge` 随 T3 加入，届时测试改为九个。`team_delete` 先实现工具的部分（不带 `force` 时遇到运行中的成员拒绝；删除已合并且干净的工作树，保留其他工作树）；`/team delete` 随 T4。
  - 向已停止的成员发消息时，消息只写入邮箱；恢复（ID4）随 T4。窗格后端随 T5，进程内成员的后端原因暂为“进程内运行，与 Lead 共用这个窗口”。
- T3（2026-11-01）：`teams/merge.py`（MG1–MG8 的检查、确认内容与合并）、`TeamRuntime.merge`、`team_merge` 工具（`timeout=None`，等待用户确认）、合并确认卡片、Lead 的团队说明；`tests/test_teams_stage15.py` 共 64 个通过（T3 新增 9 个），连续 3 次全部通过；相关测试 362 个通过（另加入 `test_agent.py`）。
  - 确认（MG3）：`PermissionPolicy.confirm` 不看模式和规则，界面无法询问时返回空，工具报 `merge_not_confirmed`；选项新增 `merge`（`MERGE_CHOICES = ("merge", "deny")`）。卡片标题“需要确认 · 合并成员 <name> 的分支”，选项“合并 · …”“不合并 · …”，详情是成员、分支 → 目标分支、提交列表、`git diff --stat` 和主目录的未跟踪文件。
  - MG4 的“用户的说明”无法在卡片中收集：审批卡片只有选项，没有输入框。拒绝时返回 `merge_declined` 和“停下来问用户”；收尾时补上界面提示（见下文“收尾”）。
  - 检查在确认前后各做一次，确认期间状态可能已变；同一时刻只做一个合并（`_merge_lock`）。MG1 的第一项还拒绝进行中的 cherry-pick、revert；成员不存在时报 `unknown_member`；其他失败报 `merge_failed`。
  - `member_busy` 以进程内成员的运行状态为准；已停止的成员视为空闲。
  - LD1：`AgentLoop.team_guide`，只在 Lead 上设置，排在 Agent 目录之后；子 Agent、Fork 和成员都没有这一节。
  - RT3：创建成员时记录主目录当前分支（`worktree.main`），附录中写出“主分支是 main”和 `git merge main`；T2 创建的旧记录没有这一项，附录仍写 `<主分支>`。
  - AC11 改为九个工具（`LEAD_TOOLS` 含 `team_merge`）。
- T4（2026-11-01）：`TeamRuntime` 的退出、单个成员的停止与恢复、`overview`/`detail`/`context`；`/team` 命令；状态栏团队段；LD5 的 `<team-context>`。`tests/test_teams_stage15.py` 共 72 个通过（T4 新增 8 个），连续 5 次全部通过；相关测试 564 个通过（另加入命令、会话、Skill、计划命令的测试），`test_commands_stage10.py` 的内置命令数改为 19。
  - LD6：退出时向每个进程内成员真的发送 `shutdown_request`（原因“Lead 正在退出”），成员跑完一轮后答复并停止；总共最多等 `stop_wait`（10 秒），剩下的取消。之后 Lead 邮箱里没人读的 `shutdown_response` 标记为已读，下次打开团队的 Lead 不会当成新消息。
  - ID5：成员因停止而结束时不再发空闲通知；它从未答复停止请求时，运行时代它答复同意（T2 已有），现在先于空闲通知判断。`/team stop` 发出请求后，超过 `stop_wait` 仍未停止（包括答复不同意）就强制停止，并提示“没有在 N 秒内停止，已强制停止”。成员自己停下后由运行时收尾（移出、释放工作树、提示“已停止”）。
  - ID4：恢复用 `load_session` 读成员的对话记录，有无法解析的行（`skipped > 0`）或读取失败即 `revive_failed`“对话记录已损坏”，消息留在邮箱；中断时没有得到回复的末尾输入不重放。恢复前丢弃成员邮箱里遗留的 `shutdown_request`，否则它一醒来就会停止。`approved`（计划已批准）写入成员记录，恢复后沿用；Token 用量从记录累加。向已停止的成员发 `shutdown_request` 报 `member_stopped`。
  - TM4/CM4：`/team delete` 可以删除非当前团队，先 `acquire` 再删，别的存活 Lead 持有时报 `team_held`；删除前用选择框确认，默认停在“取消”。
  - CM1/CM2：非当前团队若持有者已退出，成员一律显示为已停止（与重新打开时的 ID3 一致），不照搬记录里遗留的状态。`/help team` 通过 `Command.details` 显示子命令，命令表本身不变宽。`/team`、`/team <名称>`、`/team stop` 可在运行中使用，`/team delete` 等这一轮结束。
  - LD5：`/clear`、`/resume` 后，Lead 的下一条普通消息（不含 Fork 和系统通知）前面加上 `<team-context>`；消息的 `command` 设为用户原文，界面和存档显示用户输入的内容。
  - UI1：`TeamRuntime.on_change` 在成员状态变化、打开/删除团队时刷新状态栏；“成员审批 K”只数仍在等待的成员请求。
  - Windows 上不加锁的读取（`/team`、`peek`）可能正碰上另一方的 `os.replace` 而报 `PermissionError`：`lock.read_text` 短暂重试，`load_json` 与 `TeamStore` 都改用它。
  - 测试注意：新建的成员在第一轮开始前状态就是空闲，等待“空闲”时同时要求它已有对话历史。
- T5（2026-11-01）：先做实验 E1–E4（结果见“实验”，BE5、D3 和“不转交密钥”已据此修改），再实现 `teams/backends.py`（`detect_backend`、`WindowsTerminalBackend`、`TmuxBackend`、`kill_tree`、心跳判断）、`teams/pane.py`（总以 0 退出的包装模块）、`teams/process.py`（窗格成员进程 `MemberProcess`、`run_member`）、`teams/pane_app.py`（成员模式的界面 `MemberApp`）、`cli.py` 的隐藏参数，以及 `TeamRuntime` 中的 `PaneRunner`。新增 `tests/test_team_backends.py` 25 个（含 Esc 中断），前 23 个连续 3 次全部通过；团队测试合计 97 个通过。
  - 与“设计”的差异：没有 `InProcessBackend` 类，进程内成员仍由 `TeamRuntime` 直接创建；`teams/backends.py` 只放窗格后端。Lead 侧把两种成员统一放在 `runners` 中：进程内是 `MemberRunner`，窗格是 `PaneRunner`（监视进程号和记录中的状态，停止时按 BE7 结束进程树并关闭 tmux 窗格）。构建成员对话的 RT2 逻辑抽成模块函数 `build_runner`，Lead 和窗格成员进程共用；恢复用的 `load_history`、`restore_member` 同样共用。
  - 记录新增字段：`Team.project`（主目录，窗格成员进程据此重建工作区和项目说明）、`Member.last_tool`（最近的工具调用，`/team <名称>` 显示）。窗格成员的模式由 Lead 在监视循环中写入记录（`_sync_mode`），成员每一轮开始时读取（PM3）。
  - 窗格成员进程：注册表以项目为根（使用工作树时以工作树为根），Hook 不信任项目文件（与 `-p` 相同），不连接 MCP，不存档会话，不提取记忆；不触发 `session_start`。stdin 和 stdout 都是终端时运行 `MemberApp`，否则逐行打印事件（测试与日志）。
  - 启动失败（BE6/BE9）：成员进程把错误写入记录、标为停止后以 1 退出（包装模块仍以 0 退出，窗格关闭）；Lead 在 `START_TIMEOUT`（20 秒）内轮询错误或进程号，失败时关闭窗格、释放工作树的 `in_use`，报 `backend_failed`“…成员已标为停止，工作树保留；没有改用其他后端。”。
  - 恢复（ID4）按当前 `teammate_mode` 重新选择后端，`send_message` 的结果写明“已从记录恢复（后端：…，原因；上次是…）”。重新打开团队（ID3）时，进程号存活且心跳新鲜的窗格成员直接接上 `PaneRunner`，结果中列出“仍在窗格中运行的成员”。
  - Esc（BE8）：有审批时拒绝本次，否则中断成员的这一轮；成员的空闲通知 `reason` 为 `interrupted`，摘要为“用户在成员窗格中中断了这一轮”，Lead 的团队更新和界面说明行写作“被用户中断”。
  - Windows Terminal 的真实启动在实验中验证过（窗格出现、包装模块退出后关闭、`taskkill` 后关闭）；完整流程留给人工验收 M120。tmux 只有替身测试，记为“未验证（本机没有 tmux）”。
- T6（2026-11-01）：`coordinator.py`（开关判断、提示文字、提示词一节、`withheld`）、`ToolRegistry.withheld`、`Conversation(coordinator=)`、CLI 的判断、状态栏徽标和 `/status` 的“角色”一行。`tests/test_teams_stage15.py` 新增 13 个，共 85 个通过。
  - CO3 的做法：不从注册表删除工具，而是给 Lead 的注册表设 `withheld`。被扣下的工具（`write_file`、`edit_file`、`MCPTool`、`SkillTool`）不出现在 Lead 的工具定义和 `names()` 中，调用时返回 `coordinator_withheld`。成员和子 Agent 的注册表由 `for_agent` 复制，不带这个过滤，所以成员照常有写工具，MCP 工具也照常给子 Agent。直接删除会让成员一起失去写工具。
  - Fork 用 `for_agent` 构建，不带过滤，因此 Fork 的工具列表与 Lead 不同，协调者模式下 Fork 不再共享 Lead 的缓存前缀。这一点可以接受：协调者主要通过成员和定义式子 Agent 工作。
  - CO7：`-p` 中两个开关都开时提示 `HEADLESS`；只开一个时仍给出 CO1 的提示。交互界面中没有团队（没有工具或 Agent 目录）时，`Conversation` 提示 `NO_AGENTS` 并按普通模式运行。
  - CO4 的协调者一节接在 Lead 的团队说明之后（`team_guide`），不进入成员和子 Agent 的提示词。

- T7（2026-11-01）：新增 `docs/reference/teams.md`；`docs/acceptance.md` 加入第十五章 M115–M123 和已知限制；README 的配置表、命令表、模块表和文档索引，`docs/reference/commands.md` 的 `/team`，`config.example.yaml` 的三个配置键。全量 pytest 1079 个通过（4 分 35 秒）。之后把 Esc 中断的空闲通知改为 `reason: interrupted`（原先是 `failed`）并新增一个测试，团队的两个测试文件重跑 110 个通过。
- 收尾（2026-11-01）：对照规格逐条检查后补上两处缺口，并在真实的 Windows Terminal 中做了一次冒烟测试。
  - TM6：窗格成员的工作树原先记录的是 Lead 的进程号，Lead 退出后看起来没人使用。现在 `WorktreeManager.mark_in_use(..., pid=)` 可以指定进程号，`MemberProcess.prepare` 用成员自己的进程号写入。
  - MG4：用户在合并卡片上选“不合并”后，界面在对话中加一行 `DECLINED_HINT`（“没有合并。想说明原因或改动要求，直接在输入框告诉 Lead。”），说明作为用户的下一条消息交给 Lead。
  - 新增测试：`test_merge_card_in_the_ui` 检查选“不合并”后的回答和提示；`test_ac13_ac14_ac15_spawn_in_a_worktree` 检查指定的进程号。团队与工作树的三个测试文件 163 个通过。
  - Windows Terminal 冒烟测试，用脚本模型，不用真实密钥。驱动脚本在一个单独命名的新窗口中作为 Lead 运行，`teammate_mode: windows-terminal`，在临时 Git 仓库中：
    - alice 在工作树中：窗格出现，成员完成一轮，Lead 收到消息和“alice 空闲”的团队更新；心跳更新；工作树的 `in_use` 进程号等于成员进程号，而不是 Lead 的进程号。
    - bob 不用工作树，执行 `sleep 120` 时 Lead 退出（`stop_wait` 4 秒）：关闭用时 4.5 秒，即等待后强制结束；bob 的进程和其中的 `sleep.exe` 都已结束。之后记录中 `lead` 为空，两个成员都是 `stopped` 且没有进程号，工作树的 `in_use` 已清除。
    - 正常退出的路径（bob 的命令先结束）关闭用时 0.7 秒，两个成员进程都已结束。
    - 没有截到窗格画面（无法把该窗口切到前台）；`MemberApp` 在真实窗格中的显示和按键仍留给人工验收 M120。
- 人工验收（2026-11-01）：M115–M123 全部通过，用真实模型（deepseek-flash）和用户的 `config.yaml`（只通过 `--config` 传入，未改动）。界面由驱动脚本经 Textual 的 pilot 无头运行，输入框、命令、审批卡片和状态栏都是真实的；`teammate_mode`、`coordinator_mode` 在内存中覆盖，代替临时修改配置文件。审批按规则回答：git 等安全命令允许、其他命令拒绝，合并按用例选择。
  - M120 在真实的 Windows Terminal 窗口中运行，用 `teammate_mode: auto`：分出的窗格标题为“成员 a · 团队 wt”，显示收到的消息和工具调用；窗格中的审批用鼠标点选；Lead 退出后窗格关闭；重启后发消息恢复成员时出现新窗格；`/team stop a` 时成员正等待审批，10 秒后“已强制停止（Windows Terminal 窗格）”，窗格关闭。在窗格输入框中打字的提示无法验证（不能向终端发按键），由自动测试 BE8 覆盖。
  - 验收中发现并修复：
    - 模型照着 `team_create` 的提示先建任务，并把负责人填成还没创建的成员，得到 `unknown_member`。提示、`team_task_create` 的说明和错误信息改为：负责人只能是已创建的成员，可以先留空，创建成员后再指派。
    - Lead 退出时成员仍在请求审批：界面已拆除，`_show_approval` 找不到卡片而让成员任务异常结束。新增 `_member_approval`：窗口关闭中时直接拒绝这次请求。
    - 协调者把写文件交给了子 Agent，子 Agent 直接写主目录，绕过了合并确认。协调者一节改为：子 Agent 只做调查和运行测试；所有文件改动由成员在工作树中完成，合并时由用户确认。重跑 M121 后由成员完成并经合并确认。
    - `backend_unavailable` 的文字“但MossCode”补上空格。
    - M120 的预期改为与 CM2 一致：最近的工具调用只在成员运行时显示。

## 已确认的决定（2026-11-01）

- **Q1** Lead 从启动起就有团队工具，工具列表在会话内不变（TL1）。
- **Q2** 团队跨重启保留；重启后成员停止，收到消息时恢复（ID3、ID4）。
- **Q3** 合并前需要用户确认（MG3）。
- 评审的 18 条建议全部采用。规格阶段新增的决定见“取舍”，随本规格一起评审。

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `teams/lock.py`、`mailbox.py`、`board.py`、`__init__.py`：存储、锁、消息、共享任务、名称校验 | — | AC1–AC10 |
| T2 | `teams/member.py`、`runtime.py` 的进程内部分、`tools/team.py`，除 `team_merge` 外：创建成员、消息、Lead 通知、团队附录、`GLOBAL_BAN`、成员审批通道（先调查 `_show_approval`）、计划审批 | T1 | AC11–AC22 |
| T3 | `teams/merge.py` 与 `team_merge`、合并确认卡片、Lead 的团队说明 | T2 | AC23–AC29 |
| T4 | 空闲与恢复：Lead 退出、打开团队、接管、恢复、`/team`、状态栏 | T2 | AC30–AC35 |
| T5 | 窗格后端：先做实验 E1–E4 并把结果写回本规格；再实现 `--team-dir/--team-member`、成员模式的界面、Windows Terminal、tmux、心跳与强制结束 | T4 | AC36–AC40，M120 |
| T6 | 协调者模式 | T2 | AC41–AC44 |
| T7 | 文档：`docs/reference/teams.md`、`docs/acceptance.md`、配置说明；全量测试 | T1–T6 | 全量 pytest |

测试文件：`tests/test_teams_stage15.py`（T1–T4、T6）和 `tests/test_team_backends.py`（T5，命令调用用替身，Windows Terminal 的真实启动只在人工验收中验证）。

## 验收

自动测试（脚本化的提供商、临时 HOME、临时 git 仓库）：

| # | 场景 | 期望 |
| --- | --- | --- |
| AC1 | 团队名、成员名的合法与非法例子 | TM1 |
| AC2 | 两个线程各向同一邮箱发 200 条 | 400 条都在，没有损坏 |
| AC3 | 预先放一个 11 秒前的锁文件 | 视为过期，发送成功 |
| AC4 | 一个新锁始终被占用 | 10 次重试后报 `mailbox_busy`，邮箱内容不变 |
| AC5 | 读取未读 | 只返回未读并标为已读；保留 100 条已读的截断 |
| AC6 | `text` 不带 `summary`；Lead 以外的发送者发 `plan_approval_response` | 都被拒绝 |
| AC7 | 任务依赖：`2 blocked_by 1` | 1 完成前 2 不能改为 `in_progress`；完成后可以 |
| AC8 | 添加形成环的依赖 | `dependency_cycle` |
| AC9 | 成员领取别人的任务；状态倒退 | 拒绝；只有 Lead 能重新打开 |
| AC10 | 损坏的 `tasks.json` | 改名保留，按空文件继续，并给出提示 |
| AC11 | Lead 的工具列表 | 启动时就包含九个团队工具；`-p` 中没有；创建团队前后不变 |
| AC12 | 普通子 Agent 和 Fork 调用团队工具 | 子 Agent 看不到；Fork 被拒绝 |
| AC13 | `team_spawn`（进程内） | 工作树 `team-<t>/<m>`、分支 `mosscode/team-<t>+<m>`；`config.json` 记录完整；结果写明后端及原因 |
| AC14 | 成员工具列表 | 有消息和任务工具，没有 `agent`、`team_spawn`、`team_merge` |
| AC15 | 成员完成一轮 | Lead 的 `notifications` 中出现 `<team-update>`，含 `idle_notification`；Lead 空闲时开始一个通知轮 |
| AC16 | 成员一轮中途收到两条消息 | 下一轮一次读入两条 |
| AC17 | 广播 `*` | 除发送者外的成员都收到，Lead 不收到 |
| AC18 | 第五个成员 | `too_many_members` |
| AC19 | 进程内成员请求审批，Lead 空闲 | 审批卡片出现，来源为“成员 <name>”；批准后成员继续 |
| AC20 | 需要审批的成员调用 `write_file` | `plan_blocked`；`exit_plan_mode` 后 Lead 收到计划 |
| AC21 | Lead 拒绝，成员修改后重交，Lead 批准 | 旧的 `request_id` 失效；批准后成员的模式按 PM3 切换，能写文件 |
| AC22 | 会话处于计划模式时批准成员的计划 | 成员仍为计划模式，消息中说明 |
| AC23 | 主目录有已跟踪文件的改动时合并 | `main_dirty`，git 状态不变 |
| AC24 | 成员有未提交的文件；成员正在运行 | `member_uncommitted`；`member_busy` |
| AC25 | 依赖成员 a 的任务的成员 b 先合并 | `merge_order`；先合并 a 后成功 |
| AC26 | 用户选择不合并 | `merge_declined`，HEAD 不变 |
| AC27 | 正常合并 | 产生合并提交；工作树和分支保留 |
| AC28 | 两个成员改同一行，第二个合并 | `merge_conflict` 列出文件；`git status` 干净，HEAD 等于合并前 |
| AC29 | 成员在工作树中合入主分支并解决冲突后再合并 | 成功 |
| AC30 | Lead 关闭 | 成员收到 `shutdown_request`，全部 `stopped`；工作树和团队目录保留 |
| AC31 | 新进程 `team_create` 同名团队 | 打开原团队，成员为 `stopped`；持有者为新进程 |
| AC32 | 另一个存活的进程持有团队；当前团队有运行中的成员时切换到另一个团队 | `team_held`；`team_busy`（ID6） |
| AC33 | 向已停止的成员发消息 | 从记录恢复，新的一轮能看到之前的对话 |
| AC34 | 恢复时记录损坏 | `revive_failed`，消息仍在邮箱中 |
| AC35 | `/team`、`/team <name>`、`/team stop`、`/team delete` | CM1–CM4；delete 删除已合并且干净的工作树，保留其他工作树 |
| AC36 | `auto`：设 `TMUX` 并有 tmux 替身；设 `WT_SESSION` 并有 wt 替身；都没有 | 依次选 tmux、windows-terminal、in-process，原因正确 |
| AC37 | 显式 `tmux`，但没有 tmux | `backend_unavailable`，不创建进程内成员 |
| AC38 | 窗格命令行 | Windows Terminal 和 tmux 的参数符合 BE5，含 `--team-dir`、`--team-member`、工作目录 |
| AC39 | 子进程 20 秒内没有写入进程号 | `backend_failed`，成员为 `stopped` |
| AC40 | 子进程模式：`--team-dir/--team-member` 运行一轮（脚本化的提供商） | 读邮箱、运行一轮、写空闲通知、更新心跳；收到 `shutdown_request` 后回复并退出 |
| AC41 | 只开配置，或只设环境变量 | 不开启，提示缺少的一项 |
| AC42 | 两项都开 | Lead 没有 `write_file`、`edit_file`、MCP 工具；有 `execute_command`、`agent` 和团队工具 |
| AC43 | 协调者模式的状态 | 状态栏显示 `[协调]`；`/status` 写明 |
| AC44 | `-p` 中开启协调者模式 | 忽略，并给出提示 |

人工验收（在 `.test-artifacts/acceptance/project` 中，用户在 Windows Terminal 中运行）：

| # | 场景 |
| --- | --- |
| M115 | 并行改不同文件：Lead 建团队和两个成员（进程内），各改一个文件；确认后依次合并，主分支有两个合并提交 |
| M116 | 依赖门控：任务 2 依赖任务 1；成员 b 在 1 完成前不开工，完成后接手 |
| M117 | 冲突：两个成员改同一处；第二次合并冲突并被放弃，主目录干净；成员解决后再合并成功。再做一次“不合并”，HEAD 不变 |
| M118 | 空闲 → 消息 → 恢复：退出 MossCode，重新启动，让 Lead 给某个成员发消息；成员恢复，记得之前的工作 |
| M119 | 计划被拒后重交：需要审批的成员提交计划，Lead 拒绝；成员修改后重交并被批准，之后才写文件 |
| M120 | Windows Terminal 分屏：`teammate_mode: auto`，在 Windows Terminal 中运行；成员出现在新窗格，工作并汇报；Lead 退出时窗格关闭；强制结束路径也验证一次 |
| M121 | 协调者模式：配置加环境变量；状态栏显示 `[协调]`；Lead 没有写工具，把实施交给成员 |
| M122 | 成员审批：进程内成员执行需要确认的命令，卡片来源为“成员 <name>”，Lead 空闲时也出现 |
| M123 | 显式 `teammate_mode: tmux`（本机没有 tmux）：报错，不降级 |

tmux 后端在本机无法人工验收，记为“未验证（本机没有 tmux）”，只有 AC36–AC38 的替身测试。
