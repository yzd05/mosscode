# Agent 团队

主 Agent 作为 Team Lead（固定名称 `lead`）组建团队：成员是长期存在的 Agent，各自在独立的 git 工作树中工作，通过共享任务和邮箱协作。Lead 在用户确认后把成员的分支合并到主目录当前的分支。团队保存在用户目录中，跨重启保留。

团队只在交互界面中可用，而且需要开启工具和子 Agent（有 Agent 目录）。`-p` 无界面模式没有团队工具：合并确认和成员审批都要用户回答。

## 何时使用

适合能拆成几块、可以并行、彼此独立的多文件改动。一处小改动、一个问题或只需要调查时，直接做，或用[子 Agent](subagents.md)。每个成员都是一份完整的模型调用成本。

典型流程（Lead 的系统提示词中有同样的说明）：

1. `team_create` 建团队；
2. `team_task_create` 建好任务，用 `blocked_by` 写明依赖；
3. `team_spawn` 创建成员，至多 4 个，在 prompt 中写清负责的任务、背景、范围和“完成后提交改动”；
4. 等待：成员的消息、空闲通知和新就绪的任务以 `<team-update>` 自动送达 Lead，不需要轮询；
5. 全部完成后，用 `team_merge` 按依赖顺序逐个合并，每次都要用户确认；
6. 向用户汇报每个成员做了什么、合并了哪些分支、还剩什么问题。

## 存储

团队放在 `~/.mosscode/projects/<项目键>/teams/<团队名>/`，项目键与会话存档相同。会话的按天清理不触及这个目录。

| 文件 | 内容 |
| --- | --- |
| `config.json` | 版本、团队名、描述、项目路径、Lead 持有者（进程号、会话、接管时间）和成员列表 |
| `tasks.json` | 共享任务 |
| `mailbox/<名称>.json` | 每个参与者（包括 `lead`）一个邮箱 |
| `transcripts/<成员>.jsonl` | 成员的对话记录，格式与会话记录相同，包括压缩记录 |

每个成员的记录有：定义名、模型、后端及选择原因、状态（`running` 一轮进行中 / `idle` 等待消息 / `stopped` 不在运行）、是否需要计划审批、权限模式、工作树、窗格成员的进程号与心跳、最近的工具调用、最初的任务说明和累计用量。

团队名和成员名：小写字母、数字和 `-`，1–32 个字符，以字母或数字开头；成员名不能是 `lead` 或 `*`。不合法时报 `invalid_name`，不自动改名。

所有文件的修改都在锁内进行（`<文件>.lock`，重试 10 次，总等待不超过 5 秒，超过 10 秒的锁视为过期），先写临时文件再替换。读到损坏的邮箱或 `tasks.json` 时，原文件改名为 `<文件>.corrupt-<时间>` 保留，按空文件继续并提示；`config.json` 损坏时不替换（`team_corrupt`），以免丢失成员名单。

一个团队同一时刻只由一个 Lead 进程持有。`team_create` 遇到同名团队时打开它：持有者仍在运行时报 `team_held`，否则接管。每个团队最多 `team_max_members` 个成员（默认 4），已停止的成员也占名额，只有删除团队才释放。

## 工具

Lead 从启动起就有九个团队工具，工具列表在会话内不变：

| 工具 | 作用 |
| --- | --- |
| `team_create` | 新建或打开团队，设为当前团队 |
| `team_spawn` | 创建成员并开始它的第一轮：`name`、`agent_type`（默认 `general`）、`prompt`、`require_plan_approval`、`isolation`（`worktree` 默认 / `none`） |
| `team_merge` | 把成员分支合并到主目录当前分支，见“合并” |
| `team_delete` | 删除当前团队；不带 `force` 时遇到运行中的成员拒绝 |
| `team_task_create`、`team_task_get`、`team_task_list`、`team_task_update` | 共享任务 |
| `send_message` | 发消息：`to` 为成员名或 `*`（广播给全部成员，不含 Lead） |

没有当前团队时，除 `team_create` 外都报 `no_team`。团队工具不需要审批。

成员的工具是它的 Agent 定义允许的工具，加上 `send_message`（可以发给 `lead`、其他成员或 `*`）和四个任务工具；需要计划审批的成员另有成员版本的 `exit_plan_mode`。成员不能建团队、创建成员或子 Agent、合并，也不能跨团队发消息。普通子 Agent 看不到团队工具；Fork 照旧提供这些工具（保持缓存前缀），但调用时拒绝。

## 共享任务

- 字段：`id`（从 1 起）、`subject`（不超过 120 字）、`description`、`status`（`pending → in_progress → completed`）、`owner`、`blocked_by` 和时间；`blocks` 由 `blocked_by` 反推显示。
- `blocked_by` 全部完成时任务才“就绪”；不就绪的任务不能开始（`task_blocked`）。依赖只能指向已有任务，形成环时报 `dependency_cycle`。
- 成员只能修改没有负责人或归自己的任务，开始一个没有负责人的任务时自动成为负责人。只有 Lead 能重新打开已完成的任务、移除依赖、清除负责人。
- 一个团队最多 200 个任务，不提供删除。
- 任务完成时，Lead 的下一次团队更新中有一行“任务 N 完成（成员）”，以及因此就绪的任务和它们的负责人。

## 消息

消息字段：`id`、`from`、`type`、`body`（不超过 20000 字）、`summary`、`timestamp`、`read`，协议消息的额外字段直接放在消息中。

| 类型 | 谁发 | 说明 |
| --- | --- | --- |
| `text` | 任何人 | 必须有不超过 80 字的 `summary`；只有它可以广播 |
| `plan_approval_request` | 运行时代成员发 | `request_id`、`plan` |
| `plan_approval_response` | 只有 Lead | `request_id`、`approve`、`feedback` |
| `shutdown_request` | 只有 Lead | `reason` |
| `shutdown_response` | 成员 | `request_id`、`approve`、`reason` |
| `idle_notification` | 运行时代成员发 | `reason`（`completed` / `failed` / `interrupted`）、完成的任务、最后回复的前 300 字 |

每个邮箱保留全部未读消息和最近 100 条已读消息；未读超过 500 条时发送报 `mailbox_full`。

Lead 收到的消息合成一条 `<team-update>` 通知：Lead 空闲时开始一个通知轮，忙时在下一个迭代边界并入。界面为每条更新显示一行，例如“团队 auth：alice → lead：<摘要>”。`/clear` 和 `/resume` 不停止团队；之后 Lead 的第一条消息前注入一次 `<team-context>`（团队、成员状态和任务概况）。

## 成员的运行

成员的一轮：取出全部未读消息，合并为一条 `<incoming-messages>` 用户消息；运行到这一轮结束；向 Lead 发空闲通知，然后等待下一条消息。一轮进行中收到的消息留到下一轮一起读。第一轮的消息是 `team_spawn` 的 prompt。

成员的对话按子 Agent 定义构建：定义的系统提示词加上团队附录（“你是团队 X 的成员 Y”、文字回复没人看得到要用 `send_message`、工作流程、工作树和主分支、怎样解决合并冲突、怎样答复停止请求）。工作区工具以工作树为根重新创建，不带 MCP 和 Skill 脚本工具；开启自动压缩。一轮失败（提供商错误、达到最大轮数）时空闲通知的 `reason` 为 `failed`，成员仍可接收消息。

成员默认使用工作树 `team-<团队>/<成员>`，分支 `mosscode/team-<团队>+<成员>`，基于主目录当前的 HEAD。它按手动工作树对待，不会被过期清理删除；成员运行时 `/worktree remove` 拒绝删除它。`isolation: none` 让成员在主目录工作，适合只读的角色。不在 git 仓库中时默认的隔离方式报 `worktree_unavailable`。

成员的工具事件照常触发 Hook，`agent` 字段为 `teammate`。进程内成员使用 Lead 的 Hook；窗格成员是独立进程，未确认的项目 Hook 不加载，与 `-p` 相同。

### 计划审批

`require_plan_approval: true` 的成员以计划模式启动，只能用只读工具和只读命令。它调用 `exit_plan_mode` 时，计划作为 `plan_approval_request` 发给 Lead，这一轮结束。Lead 用 `send_message` 的 `plan_approval_response` 回复：批准时成员切出计划模式（按下面的权限规则），拒绝时仍在计划模式，修改后重交会生成新的 `request_id`，旧的失效。界面为提交和答复各显示一行。

### 权限

成员的工具调用按[工具权限](permissions.md)的规则检查，项目边界是它的工作树，不能请求工作树以外的写入。每一轮开始时按会话当前的权限模式重新计算成员的模式：会话处于计划模式时成员也是；项目级定义不能放宽会话的模式。

进程内成员的审批卡片显示在主界面，来源写为“成员 <名称>”，Lead 运行或空闲时都会出现，多个请求按到达顺序排队；状态栏显示“成员审批 K”。窗格成员的审批显示在它自己的窗格中。

## 合并

`team_merge(member)` 按顺序检查，任一不满足就报错，不执行合并：

| 检查 | 错误码 |
| --- | --- |
| 主目录是 git 仓库，HEAD 在分支上，没有进行中的合并、变基、cherry-pick 或 revert | `main_not_ready` |
| 主目录没有已跟踪文件的改动（未跟踪的文件不阻止合并，在确认中列出） | `main_dirty` |
| 成员有工作树，且不在一轮中 | `no_worktree` / `member_busy` |
| 成员工作树没有未提交的改动 | `member_uncommitted` |
| 成员分支有主分支没有的提交 | `nothing_to_merge` |
| 它完成的任务所依赖的、归其他成员的分支已经合并 | `merge_order` |

检查通过后总是请用户确认，不受权限模式和允许规则影响。卡片标题为“合并成员 <名称> 的分支”，内容是分支、目标分支、提交列表、`git diff --stat` 和未跟踪的文件。用户选“不合并”时返回 `merge_declined`，Lead 应停下来问用户；对话中会提示用户直接在输入框说明原因或改动要求，这条说明就是交给 Lead 的下一条消息；界面无法询问时报 `merge_not_confirmed`。

合并执行 `git merge --no-ff --no-edit <分支>`。冲突时收集冲突文件、执行 `git merge --abort`，返回 `merge_conflict`：让该成员在自己的工作树中合入主分支、解决冲突并提交，再调用 `team_merge`。Lead 不在主目录解决冲突。任何情况下都不执行 `reset --hard`、`checkout --` 或 `clean`；`--abort` 也失败时返回 `merge_abort_failed`，Lead 要立即告诉用户。合并后成员的工作树和分支保留。

## 停止与恢复

- **Lead 退出**：向运行中和空闲的成员发 `shutdown_request`，总共最多等 10 秒，然后结束剩下的窗格进程、取消进程内成员，全部标为 `stopped`，清除持有者。工作树、记录和团队都保留。
- **停止一个成员**：`/team stop <成员>` 或 Lead 的 `shutdown_request`。成员在这一轮结束后答复并停止，也可以答复不同意；10 秒内没有停止时 `/team stop` 强制停止。
- **重新打开**：`team_create` 打开已有团队时，进程号存活且心跳不超过 15 秒的窗格成员继续工作，其他成员一律标为 `stopped`。
- **恢复**：Lead 向已停止的成员发消息时，按当前的 `teammate_mode` 重新选择后端（结果中注明，可能与上次不同），从对话记录重建对话，复用原工作树，再投递消息开始一轮。记录损坏、工作树丢失或后端失败时报 `revive_failed`，消息仍在邮箱中，下次恢复时会读到。
- **切换团队**：当前团队有运行中的成员时拒绝（`team_busy`）；否则停止原团队的进程内成员，窗格成员继续存活，然后切换。

## 后端

配置 `teammate_mode` 选择成员在哪里运行：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 环境变量 `TMUX` 非空且找得到 `tmux` → tmux；否则 `WT_SESSION` 非空且找得到 `wt.exe`（即 MossCode 运行在 Windows Terminal 中）→ Windows Terminal 分屏；否则进程内 |
| `in-process` | 成员是 Lead 进程中的 asyncio 任务，与 Lead 共用这个窗口 |
| `windows-terminal` | 每个成员一个 Windows Terminal 分屏窗格 |
| `tmux` | 每个成员一个 tmux 窗格 |

`team_spawn` 的结果、成员记录和 `/team <团队>` 写明选中的后端和原因，例如“auto：检测到 Windows Terminal”。显式指定的窗格后端不可用时报 `backend_unavailable` 并说明缺什么，不改用其他后端。

### 窗格成员

窗格成员是另一个 MossCode 进程，由 Lead 用隐藏参数启动（不出现在 `--help` 中，不要手动使用）：

```text
<python> -m mosscode --team-dir <团队目录> --team-member <成员名> [--config <Lead 的配置路径>] [--provider <名称>]
```

- Windows Terminal：`wt -w 0 split-pane -d <工作目录> -- <python> -m mosscode.teams.pane <参数>`，在 Lead 所在的窗口中分出窗格。
- tmux：`tmux split-window -h -P -F "#{pane_id} #{pane_pid}" -c <工作目录> <命令>`。

工作目录是成员的工作树（`isolation: none` 时为主目录）。成员进程启动后把进程号写入 `config.json`，每 5 秒更新一次心跳，每 0.5 秒轮询自己的邮箱，每一轮开始时从记录读取 Lead 写入的权限模式。20 秒内没有写入进程号时，`team_spawn` 报 `backend_failed`（附上成员进程写入的错误），成员标为 `stopped`，工作树保留，不改用进程内。

窗格中是成员模式的界面：标题为“成员 <名称> · 团队 <团队>”，显示它收到的消息、工具调用和回复。输入框只用于回答审批（输入序号回车，或在选项上回车）；Esc 拒绝屏幕上的审批，没有审批时中断当前一轮（成员会告诉 Lead）。它不接受新任务：需要调整成员时告诉 Lead，避免成员的上下文中出现 Lead 不知道的要求。

成员进程按 Lead 的配置路径重新加载配置，MossCode 不通过命令行或文件转交密钥。窗格会继承 Lead 进程的环境变量，所以在当前 shell 中设置的 `api_key_env` 变量通常可用；成员进程中缺少时，启动失败并报“窗格中缺少环境变量 X：设为用户环境变量，或改用 in-process”。

关闭窗格成员时先发 `shutdown_request`，10 秒内没有退出就结束它的进程树：Windows 上 `taskkill /PID <pid> /T /F`，POSIX 上结束进程组，tmux 再执行 `kill-pane`。

Windows Terminal 的做法来自 Windows Terminal 1.24 上的实验：

- 窗格中进程以 0 退出时窗格关闭，非 0 时窗格保留并显示退出码（默认的 `closeOnExit`）。被 `taskkill` 结束的进程退出码为 1，所以窗格中运行的是包装模块 `mosscode.teams.pane`：它以子进程运行成员，无论成员如何结束自己都以 0 退出，窗格随之关闭。
- `wt` 把参数中的 `;` 当作子命令分隔符，命令行中的 `;` 都写成 `\;`。
- `wt -w 0` 总是调用者所在的窗口，即使之后打开了别的窗口。
- Windows Terminal 不能按 id 关闭窗格或向窗格发送按键，所以成员靠轮询邮箱发现消息，靠结束进程关闭窗格。
- 如果你在 `settings.json` 中把 `closeOnExit` 设为 `never`，或成员进程异常卡住，窗格可能留在屏幕上：确认 `/team <团队>` 中该成员已停止后，手动关闭它（Ctrl+Shift+W）。

tmux 后端没有在本机人工验证（本机没有 tmux），只有命令行和替身测试。

## 协调者模式

协调者模式让 Lead 只负责调查、拆分、派发和验证，实施交给成员。同时满足两个条件时开启：

- 配置 `coordinator_mode: true`；
- 环境变量 `MOSSCODE_COORDINATOR_MODE` 为 `1`、`true` 或 `yes`（不区分大小写）。

只满足一个时不开启，启动时提示缺少的那一个。它在启动时决定，会话内不变，没有命令可以切换。`-p` 中忽略并提示“协调者模式只用于交互界面”；没有工具或没有 Agent 目录时同样忽略并提示。

开启后：

- Lead 不再有 `write_file`、`edit_file`、MCP 工具和 Skill 脚本工具（MCP 服务器照常连接，工具留给成员和子 Agent）；保留读工具、`execute_command`、团队工具、`agent`、`task_output`、`task_stop`、`load_skill`、`memory` 和 `exit_plan_mode`。模型若仍调用被去掉的工具，返回 `coordinator_withheld`。
- 成员和子 Agent 的工具不受影响，照常可以写文件。
- 系统提示词增加“协调者模式”一节，分四个阶段：调查（读代码或派只读子 Agent）、综合（拆任务、写依赖）、实施（创建成员并派发，之后不要自己动手）、验证（让成员或子 Agent 运行测试，合并后自己可以运行只读的检查）。
- 状态栏的权限档位后显示 `[协调]`；`/status` 有一行“角色  协调者模式（配置与环境变量均已开启）”。

这是角色约束，不是安全边界：`execute_command` 仍然能写文件，权限规则照常适用。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/team` | 列出本项目的团队：成员数、各状态人数、是否为当前团队、持有者 |
| `/team <名称>` | 团队详情：每个成员的状态、后端及原因、工作树和分支、最近的工具调用、用量、未读消息数；任务列表 |
| `/team stop <成员>` | 请当前团队的成员在这一轮结束后停止，10 秒后强制停止 |
| `/team delete <名称>` | 确认后停止全部成员，删除已合并且干净的工作树（其他的保留并列出）和团队目录；被另一个存活的 Lead 持有时拒绝 |

`/team`、`/team <名称>`、`/team stop` 在 Lead 运行时也能使用；`/team delete` 等这一轮结束。

有当前团队时，状态栏显示“团队 <名称> 运行 N / 空闲 M”。成员的创建、空闲、停止、恢复和失败各显示一行说明，写明后端。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `teammate_mode` | `auto` | `auto` / `in-process` / `windows-terminal` / `tmux`，见“后端” |
| `team_max_members` | `4` | 每个团队的成员上限，1–8 |
| `coordinator_mode` | `false` | 协调者模式的配置开关，还需要环境变量 `MOSSCODE_COORDINATOR_MODE` |

取值不合法时启动报错。

## 限制

- 团队不能在 `-p` 中使用；不支持跨机器的团队、成员之间的流式通信、跨团队消息。
- 依赖只有“全部完成才就绪”一种，没有优先级或“任一完成即可”。
- 窗格成员的进程号来自解释器本身：venv 的 `python.exe` 是启动器，强制结束时以进程树为单位结束，启动器随之退出。
- 每个窗格成员每秒约读两次邮箱文件；进程内成员收到投递时立即唤醒，来自其他进程的消息靠 0.5 秒的轮询发现。
