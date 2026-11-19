# Agent 团队

主 Agent 作为 Team Lead（固定名称 `lead`）组建团队：成员是长期存在的 Agent，各自在独立的 git 工作树中工作，通过单真源追加式黑板（`board.jsonl`）、共享任务和信号邮箱协作。Lead 在用户确认后把成员的分支合并到主目录当前的分支。团队保存在用户目录中，跨重启保留。

团队在终端 TUI 和 ACP 编辑器集成（`in-process` 模式）中可用，而且需要开启工具和子 Agent（有 Agent 目录）。`-p` 无界面模式没有团队工具：合并确认和成员审批都要用户回答。

## 何时使用

适合能拆成几块、文件集合互不重叠的多文件改动。一处小改动、一个问题或只需要调查时，直接做，或用[子 Agent](subagents.md)。每个成员都是一份完整的模型调用成本。

典型流程（Lead 的系统提示词中有同样的说明，配合 `/swarm <任务>` 可一键启用）：

1. `team_create` 建团队；
2. `team_task_create` 建好任务，用 `blocked_by` 写明依赖，确保各实施成员负责的文件集合严格不重叠；
3. `team_spawn` 并发创建成员（同时最多 `team_max_members` 个活跃成员，默认 6），包括各工作树 `worker`、合流 `integrator` 以及只读审查员 `verifier`（`role: "verifier"`）；
4. 成员在黑板上通过 `post_board` 配合 `@integrator`、`@verifier` 和 `#seq` 编号直接点对点接力（用 `read_board(seqs=[...])` 按需查阅原文），期间不打扰 Lead（零 Lead 中转 Token），全部通过后再点名 `@lead`；
5. Lead 收到 `<team-update>` 后，用 `team_merge` 按依赖顺序逐个合并，每次都要用户确认，最后发一条不带 `@` 的 `[done] <摘要>` 收尾；
6. 向用户汇报每个成员做了什么、合并了哪些分支、还剩什么问题。

## 存储

团队放在 `~/.mosscode/projects/<项目键>/teams/<团队名>/`，项目键与会话存档相同。会话的按天清理不触及这个目录。

| 文件 | 内容 |
| --- | --- |
| `config.json` | 版本、团队名、描述、项目路径、Lead 持有者（进程号、会话、接管时间）、黑板游标（`lead_board_cursor`）、乒乓跳数（`peer_hops`）、收尾序号（`done_seq`）和成员列表 |
| `board.jsonl` | 追加式单真源黑板：单调递增 `#seq` 编号、作者、正文、摘要、`@mentions`、`#refs` 引用与类型（`brief`、`post`、`direct`、`watchdog`、`done`） |
| `tasks.json` | 共享任务 |
| `mailbox/<名称>.json` | 控制信号邮箱（仅存放计划审批、停止请求与响应、窗格审批答复和空闲通知，不双写协作正文） |
| `transcripts/<成员>.jsonl` | 成员的对话记录，格式与会话记录相同，包括压缩记录 |

每个成员的记录有：定义名、角色（`worker`、`integrator` 或只读 `verifier`）、模型、后端及选择原因、状态（`running` 一轮进行中 / `idle` 等待消息 / `stopped` 不在运行）、黑板游标（`board_cursor`）、是否需要计划审批、权限模式、工作树、窗格成员的进程号与心跳、最近的工具调用、最初的任务说明和累计用量。

团队名和成员名：小写字母、数字和 `-`，1–32 个字符，以字母或数字开头；成员名不能是 `lead`、`all`、`watchdog` 或 `*`。不合法时报 `invalid_name`，不自动改名。

所有文件的修改都在锁内进行（`<文件>.lock` 上的操作系统锁，最多等待 5 秒），先写临时文件再替换。持有锁的进程退出或崩溃时由系统释放锁；留在目录中的 `.lock` 文件本身不代表被占用。读到损坏的邮箱或 `tasks.json` 时，原文件改名为 `<文件>.corrupt-<时间>` 保留，按空文件继续并提示；`config.json` 损坏时不替换（`team_corrupt`），以免丢失成员名单。

一个团队同一时刻只由一个 Lead 进程持有。`team_create` 遇到同名团队时打开它：持有者仍在运行时报 `team_held`，否则接管。每个团队同一时刻最多 `team_max_members` 个活跃成员（`running` 与 `idle` 合计，默认 6，可配置 1–16）；已停止（`stopped`）的成员不再占用活跃槽位，槽位已满时新建或唤醒恢复成员报 `too_many_members`。

## 工具

Lead 从启动起就有十一个团队工具：

| 工具 | 作用 |
| --- | --- |
| `team_create` | 新建或打开团队，设为当前团队 |
| `team_spawn` | 创建成员并把初始任务说明记入黑板 `#seq` 后启动第一轮：`name`、`agent_type`（默认 `general`）、`role`（`worker` 默认 / `integrator` 合流员 / `verifier` 只读审查员）、`prompt`、`require_plan_approval`、`isolation`（`worker` 与 `integrator` 默认 `worktree`，`verifier` 默认 `none`） |
| `team_merge` | 把成员分支合并到主目录当前分支，见“合并” |
| `team_delete` | 删除当前团队；不带 `force` 时遇到运行中的成员拒绝 |
| `team_task_create`、`team_task_get`、`team_task_list`、`team_task_update` | 共享任务 |
| `post_board` | 向 `board.jsonl` 追加黑板条目（支持 `@<成员>`、`@all`、`@lead` 点名唤醒、`#seq` 引用和 `[done]` 结束标记） |
| `read_board` | 读取黑板条目：支持 `since` 增量拉取或 `seqs=[...]` 按编号精确查阅原文 |
| `send_message` | 发送文本（单真源写入 `board.jsonl` 并点名目标）或控制消息（`plan_approval_response`、`shutdown_request` 等写入 `mailbox/`） |

没有当前团队时，除 `team_create` 外都报 `no_team`。团队工具不需要审批。

成员的工具是它的 Agent 定义允许的工具，加上 `post_board`、`read_board`、`send_message` 和四个任务工具；需要计划审批的成员另有成员版本的 `exit_plan_mode`。当 `role: "verifier"` 时，运行时在工具层硬性移除 `write_file` 和 `edit_file`（即便 `agent_type` 为 `general` 也不能写改文件），保留读取、搜索、命令、黑板和任务工具，并在团队附录中注入只读对抗审查指令（若 `isolation: "none"` 在主目录执行非只读命令，仍受会话权限档位与审批约束）。成员不能建团队、创建成员或子 Agent、合并，也不能跨团队发消息，没有 `ask_user`。普通子 Agent 看不到团队工具；Fork 照旧提供这些工具（保持缓存前缀），但调用时拒绝。

## 黑板与点对点接力

`board.jsonl` 是团队协作正文的唯一真源（避免黑板与邮箱双写分裂）：

- **单调编号与按需查阅**：`team_spawn` 的初始任务说明（`kind: "brief"`）、`post_board`（`kind: "post"` 或 `"done"`）和 `send_message(type="text")`（`kind: "direct"`）都追加到 `board.jsonl`，分配从 `1` 开始的 `#seq`。成员交接时只需引用 `#3` 等编号，下游成员用 `read_board(seqs=[3])` 直接读取原文，无需 Lead 居中转述。
- **代码块感知的 `@` 点名解析**：解析 `@<成员>`、`@all`、`@lead` 前会先剥离围栏代码块（` ```…``` `）与行内反引号（`` `…` ``），因此代码里的 `@dataclass`、`@pytest.mark` 或 `` `@alice` `` 绝不会误唤醒成员；未知的 `@param` 等名称自动忽略。
- **零 Lead Token 点对点接力**：成员一轮中用 `post_board` 点名了其他成员（如 `@integrator` 或 `@verifier`）且未点名 `@lead` 时，目标成员立即被唤醒（若原已 `stopped` 且有空余槽位会自动恢复），而当前成员本轮结束时**不向 Lead 发送空闲通知**，让 `author -> @integrator -> @verifier -> @lead` 流水线在后台安静推进，直到有人显式 `@lead` 或整条链路停下。
- **12 跳乒乓熔断（`MAX_PEER_HOPS = 12`）**：连续不经 Lead 的成员间点对点接力每跳计数加 1（Lead 发言或任一任务变为 `completed` 时清零）。达到 12 跳时自动抑制继续互唤，在黑板追加一条 `watchdog` 熔断记录并唤醒 `lead` 介入裁决，防止无限返工死循环。
- **零 Token 停滞看门狗（Stall Watchdog）**：纯 Python 确定性状态机，不消耗模型 Token。若存在空闲成员尚有未读黑板 `@` 点名则立即补唤醒；若尚未收到 `[done]`、所有成员与 Lead 均空闲超过 15 秒且任务板上仍有未完成任务（`pending` 或 `in_progress`），按 `(最新 #seq, 未完成任务集合)` 去重，对同一停滞状态仅在黑板记录一次 `[stall]` 并通知 `lead`，避免空闲会话反复刷屏。Lead 发送以 `[done]` 开头的黑板消息后，看门狗静默收尾。

## 共享任务

- 字段：`id`（从 1 起）、`subject`（不超过 120 字）、`description`、`status`（`pending → in_progress → completed`）、`owner`、`blocked_by` 和时间；`blocks` 由 `blocked_by` 反推显示。
- `blocked_by` 全部完成时任务才“就绪”；不就绪的任务不能开始（`task_blocked`）。依赖只能指向已有任务，形成环时报 `dependency_cycle`。
- 成员只能修改没有负责人或归自己的任务，开始一个没有负责人的任务时自动成为负责人。只有 Lead 能重新打开已完成的任务、移除依赖、清除负责人。
- 一个团队最多 200 个任务，不提供删除。
- 任务完成时，Lead 的下一次团队更新中有一行“任务 N 完成（成员）”，以及因此就绪的任务和它们的负责人。

## 消息

协作文本统一存储在 `board.jsonl` 中，每个参与者的 `mailbox/<名称>.json` 仅传输运行时控制帧：

| 类型 | 存储通道 | 谁发 | 说明 |
| --- | --- | --- | --- |
| `text` | `board.jsonl` | 任何人 | 单真源写入黑板并分配 `#seq`（`send_message` 需提供不超过 80 字的 `summary`，`to` 为成员名、`lead` 或 `*`） |
| `plan_approval_request` | `mailbox/` | 运行时代成员发 | `request_id`、`plan` |
| `plan_approval_response` | `mailbox/` | 只有 Lead | `request_id`、`approve`、`feedback` |
| `shutdown_request` | `mailbox/` | 只有 Lead | `reason` |
| `shutdown_response` | `mailbox/` | 成员 | `request_id`、`approve`、`reason` |
| `idle_notification` | `mailbox/` | 运行时代成员发 | `reason`（`completed` / `failed` / `interrupted`）、完成的任务、最后回复的前 300 字（纯成员间黑板接力时不打扰 Lead） |

每个邮箱保留全部未读控制消息和最近 100 条已读控制消息；未读超过 500 条时发送报 `mailbox_full`。

Lead 收到的未读黑板 `@lead` 条目与控制消息合成一条 `<team-update>` 通知：Lead 空闲时开始一个通知轮（ACP 中若当前在 `session/prompt` 外则排队到下一轮请求开始时处理），忙时在下一个迭代边界并入。`/clear` 和 `/resume` 不停止团队；之后 Lead 的第一条消息前注入一次 `<team-context>`（团队、成员状态和任务概况）。

## 成员的运行

成员的一轮：从 `board.jsonl`（按自己的 `board_cursor` 取出新 `@` 提及）和 `mailbox/` 取出未读控制消息，合并为一条 `<incoming-messages>` 用户消息；运行到这一轮结束；若本轮已通过黑板点名其他成员接力则直接进入空闲等待，否则向 Lead 发空闲通知。一轮进行中新到达的黑板提及和消息留到下一轮一起读取，游标原子推进不丢消息。第一轮的输入是 `team_spawn` 写入黑板 `#seq` 的初始任务说明。

成员的对话按子 Agent 定义构建：定义的系统提示词加上团队附录（“你是团队 X 的成员 Y”、文字回复没人看得到要用 `send_message`、工作流程、工作树和主分支、怎样解决合并冲突、怎样答复停止请求）。工作区工具以工作树为根重新创建，不带 MCP 和 Skill 脚本工具；开启自动压缩。一轮失败（提供商错误、达到最大轮数）时空闲通知的 `reason` 为 `failed`，成员仍可接收消息。

成员默认使用工作树 `team-<团队>/<成员>`，分支 `mosscode/team-<团队>+<成员>`，基于主目录当前的 HEAD。它按手动工作树对待，不会被过期清理删除；成员运行时 `/worktree remove` 拒绝删除它。`isolation: none` 让成员在主目录工作，适合只读的角色。不在 git 仓库中时默认的隔离方式报 `worktree_unavailable`。

成员的工具事件照常触发 Hook，`agent` 字段为 `teammate`。进程内成员使用 Lead 的 Hook；窗格成员是独立进程，未确认的项目 Hook 不加载，与 `-p` 相同。

### 计划审批

`require_plan_approval: true` 的成员以计划模式启动，只能用只读工具和只读命令。它调用 `exit_plan_mode` 时，计划作为 `plan_approval_request` 发给 Lead，这一轮结束。Lead 用 `send_message` 的 `plan_approval_response` 回复：批准时成员切出计划模式（按下面的权限规则），拒绝时仍在计划模式，修改后重交会生成新的 `request_id`，旧的失效。界面为提交和答复各显示一行。

### 权限

成员的工具调用按[工具权限](permissions.md)的规则检查，项目边界是它的工作树，不能请求工作树以外的写入。每一轮开始时按会话当前的权限模式重新计算成员的模式：会话处于计划模式时成员也是；项目级定义不能放宽会话的模式。

进程内成员的审批卡片显示在主界面，来源写为“成员 <名称>”，Lead 运行或空闲时都会出现，多个请求按到达顺序排队；状态栏显示“成员审批 K”。窗格成员的审批同时出现在它自己的标签页和 Lead 的界面中（Lead 持有该团队且它是当前团队时），来源写为“成员 <名称>（窗格）”，与进程内成员排在同一个队列中。先回答的一方生效：在 Lead 中回答时，答复用邮箱消息 `permission_response` 发给成员进程（模型不能发送这种消息），标签页中的卡片随之撤下；在标签页中回答时，Lead 的卡片在约 1 秒内撤下，并显示“成员 <名称> 的审批已在它的窗口中回答”。成员停止时 Lead 撤下它的卡片；Lead 切换团队或退出后，审批只在标签页中等待。选项和效果与在标签页中回答相同，例如“本会话允许”记在成员进程自己的权限中。

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
- **异常退出与崩溃回收**：窗格成员进程意外退出或进程内成员运行异常终止时，运行时自动将该成员名下所有 `in_progress` 状态的共享任务重置回 `pending`（清空 `owner`，保留 `completed` 任务不变），在 `board.jsonl` 追加一条 `watchdog` 记录，并向 `lead` 发送 `<team-update>` 以便重新指派。
- **切换团队**：当前团队有运行中的成员时拒绝（`team_busy`）；否则停止原团队的进程内成员，窗格成员继续存活，然后切换。

## 后端

配置 `teammate_mode` 选择成员在哪里运行：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 环境变量 `TMUX` 非空且找得到 `tmux` → tmux；否则 `WT_SESSION` 非空且找得到 `wt.exe`（即 MossCode 运行在 Windows Terminal 中）→ Windows Terminal；否则进程内 |
| `in-process` | 成员是 Lead 进程中的 asyncio 任务，在 Lead 界面的成员面板中查看（见“成员面板与成员视图”） |
| `windows-terminal` | 每个团队一个单独的 Windows Terminal 窗口（团队窗口），每个成员一个标签页 |
| `tmux` | 每个成员一个 tmux 窗口，在当前 tmux 会话中新建，不切换过去 |

`team_spawn` 的结果、成员记录和 `/team <团队>` 写明选中的后端和原因，例如“auto：检测到 Windows Terminal”。显式指定的窗格后端不可用时报 `backend_unavailable` 并说明缺什么，不改用其他后端。

### 窗格成员

窗格成员是另一个 MossCode 进程，由 Lead 用隐藏参数启动（不出现在 `--help` 中，不要手动使用）：

```text
<python> -m mosscode --team-dir <团队目录> --team-member <成员名> [--config <Lead 的配置路径>] [--provider <名称>]
```

- Windows Terminal：`wt -w <窗口名> new-tab --title "<成员> · <团队>" --suppressApplicationTitle -d <工作目录> -- <python> -m mosscode.teams.pane <参数>`。窗口名为 `mosscode-<团队>-<团队目录路径的 6 位十六进制摘要>`，不同项目中同名的团队不会进入同一个窗口。同一团队的成员都是这个窗口中的标签页，Lead 的窗口不分割。
- tmux：`tmux new-window -d -n <成员> -P -F "#{pane_id} #{pane_pid}" -c <工作目录> <命令>`。

成员记录、创建成员的说明和 `/team <团队>` 写明“在团队窗口 <窗口名> 中”（tmux 没有窗口名）。

工作目录是成员的工作树（`isolation: none` 时为主目录）。成员进程启动后把进程号写入 `config.json`，每 5 秒更新一次心跳，每 0.5 秒轮询自己的邮箱，每一轮开始时从记录读取 Lead 写入的权限模式。20 秒内没有写入进程号时，`team_spawn` 报 `backend_failed`（附上成员进程写入的错误），成员标为 `stopped`，工作树保留，不改用进程内。

标签页中是成员模式的界面：标题为“成员 <名称> · 团队 <团队>”，显示它收到的消息、工具调用和回复。审批与主界面一样出现在输入框的位置，上下两条琥珀色横线，输入框暂时隐藏；按选项的序号或在选项上回车回答，选中的选项用 `❯` 标出，说明为灰色；Esc 拒绝屏幕上的审批，没有审批时中断当前一轮（成员会告诉 Lead）。它不接受新任务：需要调整成员时告诉 Lead，避免成员的上下文中出现 Lead 不知道的要求。

成员进程按 Lead 的配置路径重新加载配置，MossCode 不通过命令行或文件转交密钥；用 `api_key_keyring` 时成员直接从系统凭据管理器读取。窗格会继承 Lead 进程的环境变量，所以在当前 shell 中设置的 `api_key_env` 变量通常可用；成员进程中缺少时，启动失败并报“窗格中缺少环境变量 X：设为用户环境变量，或改用 in-process”。

关闭窗格成员时先发 `shutdown_request`，10 秒内没有退出就结束它的进程树：Windows 上 `taskkill /PID <pid> /T /F`，POSIX 上结束进程组，tmux 再执行 `kill-pane`。

Windows Terminal 的做法来自 Windows Terminal 1.24 上的实验：

- 标签页中进程以 0 退出时标签页关闭，非 0 时保留并显示退出码（默认的 `closeOnExit`）。被 `taskkill` 结束的进程退出码为 1，所以标签页中运行的是包装模块 `mosscode.teams.pane`：它以子进程运行成员，无论成员如何结束自己都以 0 退出，窗格随之关闭。
- `wt` 把参数中的 `;` 当作子命令分隔符，命令行中的 `;` 都写成 `\;`。
- `wt -w <名称>` 在没有同名窗口时新建窗口，有同名窗口时把标签页加入其中，无论从哪个窗口调用。最后一个标签页关闭后窗口随之关闭，名称可以再用。
- 新建标签页时团队窗口会被提到最前并获得焦点，`wt` 没有在后台打开的参数：每创建一个窗格成员，都要切回 Lead 的窗口（Alt+Tab 或点击）。
- `--suppressApplicationTitle` 让成员界面设置的终端标题不覆盖 `--title` 指定的标签页标题。
- Windows Terminal 不能按 id 关闭标签页或向它发送按键，所以成员靠轮询邮箱发现消息，靠结束进程关闭标签页。
- 如果你在 `settings.json` 中把 `closeOnExit` 设为 `never`，或成员进程异常卡住，标签页可能留下：确认 `/team <团队>` 中该成员已停止后，手动关闭它（Ctrl+Shift+W）。

tmux 后端没有在本机人工验证（本机没有 tmux），只有命令行和替身测试。

## 协调者模式

协调者模式让 Lead 只负责调查、无重叠切分、派发编组、验收与合并，所有代码改动交给成员完成。有两种启用方式：

1. **会话内动态开关（推荐）**：在 TUI 或 ACP 中输入 `/swarm on` 开启，或输入 `/swarm <任务>` 自动开启并立即把任务提交给 Lead；随时可用 `/swarm off` 关闭并恢复完整工具。
2. **启动默认开启**：同时满足配置 `coordinator_mode: true` 与环境变量 `MOSSCODE_COORDINATOR_MODE` 为 `1`、`true` 或 `yes`（不区分大小写）；只满足一个时不开启并给出提示。

`-p` 无界面模式中不启用协调者模式（`/swarm` 只用于具备团队运行时的 TUI 或 ACP 交互会话）；没有工具或没有 Agent 目录时同样不启用。

开启后：

- Lead 动态隐藏 `write_file`、`edit_file`、MCP 工具和 Skill 脚本工具（MCP 服务器照常连接，工具留给成员和子 Agent）；保留读工具、`execute_command`、团队与黑板工具、`agent`、`task_output`、`task_stop`、`load_skill`、`memory` 和 `exit_plan_mode`。模型若仍调用被隐藏的工具，返回 `coordinator_withheld`。`/swarm off` 关闭时立即恢复上述工具并重锚上下文计量器（`ContextMeter`）。
- 成员（`role: "worker"`）和子 Agent 的工具不受影响，照常可以写文件；`role: "verifier"` 的审查成员始终硬性只读。
- 系统提示词注入黑板 Swarm 四步规程：调查与严格不重叠文件切分 → 一键建队与 `worker` + `integrator` + `verifier` 黄金编组 → 黑板 `#seq` 点对点接力 → `@lead` 验收后按序 `team_merge`、只读检查并以 `[done]` 收尾。
- 状态栏的权限档位后显示 `[协调]`；`/status` 的“角色”行标明协调者模式状态。

这是角色约束，不是安全边界：`execute_command` 仍然能写文件，权限规则照常适用。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/swarm [status]` | 查看当前 Swarm 协调模式开关、活跃成员上限、当前团队进度及最近黑板 `#seq` 条目 |
| `/swarm on` / `/swarm off` | 在当前会话动态开启或关闭协调者模式（即使最初由配置开启也可 `/swarm off` 关闭） |
| `/swarm <任务>` | 开启协调者模式（直到手动 `/swarm off` 前跨轮保持），并立即提交任务 |
| `/team` | 列出本项目的团队：成员数、各状态人数、是否为当前团队、持有者及当前团队黑板摘要 |
| `/team <名称>` | 团队详情：每个成员的状态、后端及原因、工作树和分支、最近工具调用、用量；共享任务列表；最近黑板 `#seq · author: summary` 记录 |
| `/team stop <成员>` | 请当前团队的成员在这一轮结束后停止，10 秒后强制停止 |
| `/team delete <名称>` | 确认后停止全部成员，删除已合并且干净的工作树（其他的保留并列出）和团队目录；被另一个存活的 Lead 持有时拒绝 |

`/swarm`（不带参数或 `status`）、`/team`、`/team <名称>`、`/team stop` 在 Lead 运行时也能使用；`/swarm on|off|<任务>` 与 `/team delete` 等这一轮结束。ACP 会话同样在 `available_commands` 中提供 `/swarm` 与 `/team`。

有当前团队时，状态栏显示“团队 <名称> 运行 N / 空闲 M”。成员的创建、空闲、停止、恢复和失败各显示一行说明，写明后端。

## 成员面板与成员视图

有当前团队且其中有未停止的成员时，Lead 输入框下方显示成员面板，每个成员一行：状态符号和名称；状态（`运行中`、`空闲`，有待回答的审批时为 `待审批`）；它负责的第一个进行中任务；运行中时的最近工具调用和本轮已运行时间；窗格成员行尾标“窗格”。一行放不下时截断，不换行。面板最多 4 行：已停止的成员汇总为“已停止 N”，仍放不下时空闲成员合并为“○ 空闲 N：a、b”，再放不下时最后一行写“另有 N 个成员 · Ctrl+T 展开”。面板可见时状态栏追加“Ctrl+T 成员”。面板随成员状态变化刷新，有运行中的成员时每秒更新已运行时间。

Ctrl+T 展开面板并让它获得焦点，输入框中的草稿保留。展开后列出全部成员（含已停止），最多 8 行：

- ↑ / ↓ 选择成员，Enter 打开成员视图；
- `x` 停止所选成员：第一次按下时帮助行显示“再按 x 停止成员 a”，2 秒内再按一次才执行，效果与 `/team stop` 相同；
- Esc 或再按 Ctrl+T 收起，焦点回到输入框。

审批卡片出现时面板收起为紧凑形式；模型面板打开时成员面板隐藏。

成员视图暂时替换 Lead 的进行中条目和输入框，自上而下是标题行（成员、团队、状态、最近的工具调用）、成员的对话和帮助行。对话读自 `transcripts/<成员>.jsonl`：收到的消息、工具调用和结果的第一行（并行的多个调用各自跟着自己的结果；还没有结果的调用显示在对话下方，结果返回后一起写入）、回复正文，最多显示最近 200 条消息。视图停在底部时跟随新内容；向上滚动后不再跟随，帮助行显示“有新内容 · End 回到底部”。进程内成员每写入一条消息就刷新，窗格成员每 0.5 秒检查一次。成员的一轮在第一个回复到达时才连同收到的消息一起写入记录，所以模型回复之前视图中暂时看不到这一轮；标题行的状态始终是实时的。视图中的按键：

- PgUp / PgDn 翻页，Home / End 到顶部和底部，↑ / ↓ 逐行滚动；
- ← / → 切换到面板中的上一个和下一个成员；
- Esc 返回面板，Lead 的进行中条目恢复显示。

视图只读，给成员的指令仍然通过 Lead。查看期间 Lead 照常运行，已完成的内容照常写入终端回滚区；审批卡片出现在视图下方，按键交给卡片，回答后回到视图；Ctrl+C 仍取消 Lead 的任务。所看的成员停止或被删除时，视图保留已有内容，标题行显示新状态；当前团队被切换或删除时，视图关闭并显示一行说明。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `teammate_mode` | `auto` | `auto` / `in-process` / `windows-terminal` / `tmux`，见“后端” |
| `team_max_members` | `6` | 每个团队同时活跃（`running` + `idle`）的成员上限，1–16 |
| `coordinator_mode` | `false` | 启动默认开启协调者模式的配置开关（还需环境变量 `MOSSCODE_COORDINATOR_MODE`）；未配置时仍可在会话内用 `/swarm` 动态开关 |

取值不合法时启动报错。

## 限制

- 团队不能在 `-p` 中使用；不支持跨机器的团队、成员之间的流式通信、跨团队消息。
- 依赖只有“全部完成才就绪”一种，没有优先级或“任一完成即可”。
- 窗格成员的进程号来自解释器本身：venv 的 `python.exe` 是启动器，强制结束时以进程树为单位结束，启动器随之退出。
- 每个窗格成员每秒约读两次邮箱文件；进程内成员收到投递时立即唤醒，来自其他进程的消息靠 0.5 秒的轮询发现。
- 成员视图以记录文件为数据源，看不到流式输出中途的文字。
- 成员视图使用终端的全部高度；关闭后终端不能收回这些行，输入框停在屏幕上方，下方留下空白，Lead 之前的输出要向上滚动查看。
- 不能控制团队窗口的位置和大小；创建窗格成员时团队窗口会抢走焦点。
