# 后台命令

日期：2026-11-10。状态：规格待评审，未实现；待定问题 Q1–Q8 需要决定。

来源：
- 沙箱规格的 F2 与 Q9：`execute_command` 只在前台运行，没有后台 shell；Q9 决定“将来加后台 shell 时作为独立的长期容器启动”。
- 2026-11-10 沙箱规格收尾后，下一项优化选定为后台命令。
- 参考实现：Claude Code 的 Bash `run_in_background`、Codex CLI 的 unified exec（“Background terminal”）、Gemini CLI 的 `is_background`，见 F12–F14。

## 问题

开发服务器、监视构建（`vite`、`tsc --watch`）、长时间的测试和构建，模型现在都没法用：

- 前台命令最长 600 秒，超时即结束整个进程树。
- 想“放到后台”时，模型通常写 `cmd &` 或 `Start-Process`，结果要么卡到超时，要么命令一结束后台进程就被杀掉（F8–F10）。所以模型启动不了开发服务器，然后在同一会话里对它发请求、看日志。
- 用户只能自己在另一个终端里启动这些进程，模型看不到它们的输出。

## 已确认的事实

### 现有代码

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `tools/process.py` | `execute_command` 的参数是 `command`、`cwd`、`timeout`（默认 120，最大 600），沙箱开启时另有 `outside_sandbox`、`justification`。`_run` 用管道读 stdout/stderr，各保留开头 1 MiB 和末尾 1 MiB（`COMMAND_HEAD`、`COMMAND_TAIL`），`finally` 中 `terminate_tree` 结束整个进程树 |
| F2 | `tools/process.py`、`tools/windows_job.py` | Windows 上每条命令放进自己的 Job Object（`KILL_ON_JOB_CLOSE`），先经一字节闸门确认已入 Job 再运行 shell；关闭 Job 即结束所有后代，MossCode 进程异常退出时系统关闭句柄，后代同样被结束。其他系统用 `start_new_session`，结束时 `killpg` 整个进程组 |
| F3 | `tools/process.py` | 沙箱中的命令：`run_process` 调用后端的 `prepare` 得到包装后的命令，命令结束时调用 `finish`。网络为 `proxy` 时 `ExecuteCommand` 为每条命令启动一个只属于它的代理，命令结束即关闭；新域名经 `PermissionPolicy.connect` 询问 |
| F4 | `agents/tasks.py` | 已有会话级的任务管理器 `TaskManager`：任务 id 为 `t1`、`t2`…，状态有运行中、已完成、失败、已取消，带有前台/后台标记、`stop`、`cancel_all`、`reset`、`detach_foreground`，以及 `finished` 事件和 `on_change`、`on_finish` 回调。`Task` 的字段是为子 Agent 设计的（`agent_type`、`usage`、`prompt`、`worktree`） |
| F5 | `agents/runtime.py`、`conversation.py` | 后台任务结束时生成 `<task-notification>`，追加到 `notifications`；主对话忙时在下一次请求前加入，空闲时立即开启一轮（`_tasks_ready`）。被 `/tasks stop`、`task_stop`、`/clear`、恢复、退出取消的任务不发通知 |
| F6 | `tools/agent.py`、`tui.py`、`cli.py` | `task_output`（`task_id`，`wait` 最多 300 秒）和 `task_stop` 已经存在，只认子 Agent。`/tasks` 可以列出、查看和停止任务；Ctrl+B 把前台子 Agent 转入后台；状态栏显示后台任务数。`/clear`、恢复、`/exit`、Ctrl+Q 遇到运行中的任务会先确认；`-p` 等全部任务结束才退出（`_turns`）。后台任务同时最多 4 个（`agent_max_background`） |
| F7 | `permissions.py`、`docs/reference/context.md` | 读取本会话目录（`~/.mosscode/projects/<项目键>/sessions/<会话 ID>/`）不需要审批，计划模式中也能读；大结果已经存在其中的 `tool-results/`。`PolicyView` 可以设为非交互：需要审批时直接返回 `approval_required`，后台子 Agent 就是这样处理的 |

### 实测：现在的命令留下后台进程时（2026-11-10）

探测脚本在 `.test-artifacts/bgprobe/`，直接调用 `ExecuteCommand.execute`，不开沙箱。

| # | 平台 | 命令 | 结果 |
| --- | --- | --- | --- |
| F8 | Windows（Git Bash）、Linux（WSL） | `长命令 & echo started`，不重定向 | 等到超时才返回：后台进程继承了 stdout 管道，`drain` 读不到 EOF；超时后进程树被结束 |
| F9 | Windows（Git Bash）、Linux（WSL） | `长命令 >/dev/null 2>&1 & echo started`，Linux 上也试了 `nohup` | 立即返回 `started`，但后台进程随命令一起被结束（Job 关闭 / `killpg`） |
| F10 | Windows（PowerShell） | `Start-Process -WindowStyle Hidden` 或 `-NoNewWindow` 启动的进程 | 6 次中 6 次随命令被结束（仍在 Job 中）。更早的一次运行中后台进程存活，之后没能复现，原因不明 |
| F11 | Linux（WSL） | `setsid 长命令 >/dev/null 2>&1 & sleep 0.5; echo started` | 后台进程存活：它已离开进程组，`killpg` 管不到；MossCode 认为命令已结束，这个进程不再受任何管理。没有 `sleep 0.5` 时，命令结束得太快，`setsid` 还没执行就被结束，所以结果取决于时机。bubblewrap 沙箱中有 `--unshare-pid` 和 `--die-with-parent`，同样的进程会随命令结束（沙箱规格 13D 的实测） |

结论：现在没有可靠的办法让进程比命令活得长，Windows 上完全不行；Linux 和 macOS 上 `setsid` 在不开沙箱时会“漏掉”进程。模型需要一个正式的后台运行方式，而不是靠 shell 技巧。

### 参考实现（2026-11-10 查阅官方文档与源码）

| # | 项目 | 做法 |
| --- | --- | --- |
| F12 | Claude Code | Bash 工具参数 `run_in_background: true` 立即返回任务 ID，命令继续运行；用户按 Ctrl+B 可以把运行中的 Bash 调用转入后台，`!` 命令也可以。前台命令到达超时后自动转入后台而不是结束（以 `sleep` 开头的除外），结果写明 “did not complete within its 120s timeout and was moved to the background”，并附任务 ID 和输出文件路径；转入后台的命令中的 `cd` 不影响之后的命令。输出写入文件，模型用 Read 读取；`TaskOutput` 已在 v2.1.277 移除。`/tasks` 列出并停止后台任务，`TaskStop` 按 ID 停止。退出时清理后台任务，macOS 和 Linux 上连 `setsid`、`timeout` 脱离的进程也一起结束；输出超过 5 GB 时结束命令。交互会话中后台命令没有时间限制；`-p`、SDK、云端等无人值守的会话中，后台命令默认 30 分钟（以文本提示运行的 `-p` 为 10 分钟），`timeout` 可设到 2 小时，到时结束并告诉模型，`-p` 在结果之后等后台命令结束。前台子 Agent 启动的后台命令随它结束；主对话或后台子 Agent 启动的在回复结束后继续运行。`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` 关闭全部后台功能。另有 `Monitor` 工具把后台命令的每行输出作为事件送给模型，默认 5 分钟、最多 30 分钟。来源：[interactive-mode](https://code.claude.com/docs/en/interactive-mode#background-bash-commands)、[tools-reference](https://code.claude.com/docs/en/tools-reference#background-commands) |
| F13 | Codex CLI | 功能名 `unified_exec`（“Background terminal”）。`exec_command` 在 PTY 中启动进程，等待 `yield_time_ms`（250 ms–30 s，Windows 首次至少 10 s）后返回已有输出；没结束的进程保留，模型用 `write_stdin` 写入或轮询；没有完成通知（[issue #32188](https://github.com/openai/codex/issues/32188) 请求事件唤醒）。输出缓冲上限 1 MiB，每次返回默认最多 10,000 Token；最多 64 个进程，超过时按最久未用淘汰，最近用过的 8 个受保护，优先淘汰已退出的。审批和沙箱与普通命令相同，被沙箱拒绝时按策略在沙箱外重试。`/ps` 显示后台终端的命令和最近三行非空输出，`/stop` 停止本会话的全部后台终端。来源：[`unified_exec/mod.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/unified_exec/mod.rs)、[`process_manager.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/unified_exec/process_manager.rs)、[命令文档](https://learn.chatgpt.com/docs/developer-commands?surface=cli) |
| F14 | Gemini CLI | `run_shell_command` 有 `is_background` 参数（启动后立即转入后台），也接受命令中的 `&`；结果中的 `Background PIDs` 列出后台进程的 PID，没有输出读取、通知和统一的停止方式。来源：[shell.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/shell.md) |

## 需求

| # | 要求 | 验收 |
| --- | --- | --- |
| R1 | `execute_command` 增加 `run_in_background`（默认 `false`）和可选的 `description`（任务标签，最多 60 字）。为真时命令照常审批、照常进入沙箱，启动后立即返回任务 id、输出文件路径和读取方法，命令继续运行 | 模型以 `run_in_background: true` 启动 `python -m http.server`，调用立即返回 `t1`；随后的 `execute_command` 用 `curl` 访问它成功 |
| R2 | 后台命令的 stdout 和 stderr 按到达顺序写入会话目录的 `commands/<任务 id>.log`；模型用 `read_file` 读取，不需要审批 | 运行中读取输出文件能看到已经输出的行 |
| R3 | 命令结束时（退出、超出时间限制、输出超限）主对话收到一条 `<task-notification>`：任务 id、标签、命令、状态、退出码、用时、输出文件路径和最后 20 行（最多 2,000 字符）。送达方式与子 Agent 的通知相同（F5）；被用户或模型停止的不发通知 | `sleep 2; exit 3` 在后台结束后，主对话空闲时自动开启一轮，通知中退出码为 3 |
| R4 | `task_output` 和 `task_stop` 同样接受命令任务。`task_output` 返回状态、退出码、输出文件路径和最后 50 行，`wait` 照旧可以等待结束；`task_stop` 结束整个进程树 | `task_stop` 之后进程树中没有进程留下（Windows 用 Job，其他系统看进程组） |
| R5 | 用户在界面中按 Ctrl+B，正在运行的前台命令转入后台：已收到的输出写进输出文件，之后的输出追加在后面；工具结果为“已转入后台：任务 tN”加文件路径。没有前台命令也没有前台子 Agent 时，提示照旧 | 运行 `sleep 30` 时按 Ctrl+B，模型立刻收到结果，命令在 `/tasks` 中显示为运行中 |
| R6 | `/tasks` 列表包含命令任务（类型“命令”，标签缺省为截短的命令文本）；`/tasks <id>` 显示命令、工作目录、是否在沙箱中（后端、网络方式）、输出文件、最后几行；`/tasks stop <id>` 结束它。状态栏的后台任务数包含命令；有前台命令运行时提示 `Ctrl+B 转入后台` | 两个后台命令和一个后台子 Agent 时状态栏显示“后台任务 3” |
| R7 | 生命周期：主对话的后台命令跨轮次运行，Esc / Ctrl+C 取消当前任务时不受影响；`/clear`、恢复会话、`/exit`、Ctrl+Q 沿用现有确认，确认后结束；MossCode 退出时全部结束。代码回退照旧在有后台任务时被拒绝 | 有后台命令时 `/exit` 先确认；确认后进程树不再存在 |
| R8 | 沙箱：后台命令在沙箱中运行时，每条命令是一个独立的沙箱实例和代理（满足沙箱规格 Q9），到命令结束才 `finish`、关闭代理。后台命令的联网审批不弹出：没有规则放行的域名被拒绝，并在通知和输出文件末尾列出被拒的域名（见 Q4） | 沙箱开启、网络 `proxy` 时，后台命令访问未放行的域名得到 403，通知中列出该域名 |
| R9 | 无界面模式 `-p`：一轮结束后照旧等待运行中的任务，包括后台命令；后台命令在 `-p` 中有时间限制（见 Q2），到时结束并发通知，模型可以据此继续 | `-p` 中后台启动 `sleep 10000`，到时间限制后命令被结束，模型收到通知，进程退出 |
| R10 | 同时运行的后台命令有上限（见 Q3）；达到上限时 `run_in_background` 返回 `too_many_tasks`，说明怎样查看或停止已有任务 | 达到上限后再启动返回 `too_many_tasks` |
| R11 | 输出文件超过 256 MiB 时结束命令，通知中说明原因 | 用一条持续输出的命令验证（测试中把上限调小） |
| R12 | 会话存档写入命令任务的开始和结束记录（`{"type": "task"}`，含命令和输出文件路径）；恢复会话不会重新运行旧命令，输出文件保留在会话目录 | 恢复一个有后台命令记录的会话，没有命令被启动 |

## 设计

### 任务模型

在 `agents/tasks.py` 中让 `Task` 同时表示子 Agent 和命令：增加 `kind: "agent" | "command"`，命令专用的字段（`command`、`cwd`、`log`、`exit_code`、`end_reason`、`sandbox` 摘要）放在一个小的 `CommandInfo` 中。这样任务 id 统一编号，`/tasks`、`task_output`、`task_stop`、`cancel_all`、`reset`、`finished`、`-p` 的等待和退出时的确认都不用分两套。`full()` 按种类分别计数（Q3）。`task_line`、`STATUS_LABELS` 按种类给出文字，例如“后台任务 t5（命令 · 开发服务器）已完成，退出码 0”。

### 进程运行

在 `tools/process.py` 中把 `_run` 拆成两部分：

- `CommandRun`：启动进程（闸门、Job、`prepare`）、读取输出、等待结束、`terminate`。输出交给一个“接收器”；前台用现有的开头 + 末尾内存缓冲，后台用文件接收器。
- 文件接收器由 MossCode 进程写入输出文件。命令自己不需要写会话目录的权限，所以沙箱策略不用改。stdout 和 stderr 交错写入同一个文件，stderr 的块不另加标记（与终端看到的相同）；接收器同时保留最后 64 KiB，供通知和 `task_output` 使用，不必重读文件。
- Ctrl+B 转入后台时，把内存缓冲中已有的内容写进文件（超过 2 MiB 时中间标注省略的字节数），再切换到文件接收器。
- `terminate_tree` 不变。Windows 上 Job 保证整棵进程树随任务结束；其他系统上 `setsid` 脱离的进程仍会漏掉（F11），见 Q7。

### 工具

- `CommandParams` 增加 `run_in_background: bool = False` 和 `description: str | None`（最多 60 字）。`timeout` 在后台时的含义见 Q2。
- `ExecuteCommand.execute`：后台时检查上限，建任务，启动 `CommandRun`，立即返回：“已在后台启动命令：任务 t5，输出写入 <路径>（用 read_file 读取）。命令结束时你会收到 <task-notification>；不要用 sleep 轮询。”
- 工具说明和 `prompt_guidelines` 增加一条：开发服务器、监视进程和预计超过几分钟的命令用 `run_in_background`，不要用 `&`、`nohup`、`Start-Process`，它们在命令结束时会被结束（F8–F10）。
- 子 Agent 中的处理见 Q5。
- `task_output` 和 `task_stop` 的描述改为“后台任务（子 Agent 或命令）”。

### 沙箱与审批

- 启动前的审批、沙箱判断、`outside_sandbox` 申请与前台命令完全相同：审批针对的是“运行这条命令”，前台还是后台不影响判断。
- 沙箱中的后台命令：`prepare` 和代理在启动时建立，在 `CommandRun` 结束时 `finish` 和关闭。MXC 下每条后台命令就是一次独立的 `wxc-exec`，命令运行期间容器一直存在，满足 Q9 的“独立的长期容器”；命令中再用 `&` 启动的子进程在命令结束时仍被结束，这是想要的结果。
- 代理的联网审批用非交互的 `PolicyView`（F7）：规则放行的照常通过，其余拒绝，并把被拒的域名记下来，写进通知（Q4）。
- 硬性检查、拒绝规则、计划模式（计划模式只允许只读命令，后台运行不放宽）、`pre_tool_use` 和 `post_tool_use` Hook 都照常作用于这次调用；`post_tool_use` 在工具返回任务 id 时触发。

### 界面

- 工具行：后台命令显示“后台 · 任务 t5”和输出文件路径；结束时与子 Agent 一样显示一行“后台任务 t5（命令 · …）已完成，退出码 0”。
- Ctrl+B（`action_background`）：先转前台命令，再转前台子 Agent；状态栏的提示在两者之一运行时出现。
- `/tasks` 的列表和详情增加命令的列；命令任务没有 Token，显示输出大小。

### 无界面模式

`_turns` 已经等 `tasks.running()`，命令任务自然被等待。后台命令在 `-p` 中按 Q2 的时间限制结束，结束的通知开启新的一轮。stderr 中的开始和结束提示沿用 `Announcer`。

## 任务

| # | 内容 | 位置 | 验证 |
| --- | --- | --- | --- |
| T1 | `Task` 增加种类和命令字段，按种类计数；列表、详情、通知文字 | `agents/tasks.py`、`agents/runtime.py`（通知） | `tests/test_subagents.py` 中任务管理器的用例照常通过，新增命令任务的计数和文字 |
| T2 | `CommandRun` 与接收器；前台路径改用它，行为不变 | `tools/process.py` | `tests/test_tools.py` 等命令相关测试照常通过 |
| T3 | `run_in_background`、`description`、上限、输出文件、时间限制、输出上限、通知 | `tools/process.py`、`conversation.py` | 新测试：启动、读文件、通知、`task_stop`、上限、输出超限 |
| T4 | `task_output`、`task_stop` 支持命令 | `tools/agent.py` | 新测试 |
| T5 | Ctrl+B 转入后台、工具行、状态栏、`/tasks` | `tui.py`、`builtin_commands.py` | 无界面 TUI 测试 |
| T6 | 沙箱：后台命令的 `finish` 和代理时机，非交互的联网审批 | `tools/process.py`、`sandbox/proxy.py` | `tests/test_sandbox_proxy.py` 增加用例；Windows 上真实 MXC 实测一次 |
| T7 | `-p` 的时间限制与等待 | `cli.py` | `tests/test_subagents.py`（`-p` 等待任务的现有用例） |
| T8 | 存档记录与恢复 | `conversation.py`、`session.py` | 恢复测试 |
| T9 | 文档：`docs/reference/tools.md`（命令一节）、`subagents.md`（任务、`task_output`、`task_stop` 改为通用的说明，或把任务一节移到新的位置）、`interface.md`（Ctrl+B）、`sandbox.md`（后台命令）、`cli.md`（`-p` 等待）、CHANGELOG | `docs/` | `tests/test_docs.py` |

T1 → T2 → T3 → T4/T5/T6/T7/T8 → T9。

## 验收

自动测试覆盖 R1–R12 的验收列（Windows 用例按 `sys.platform` 标记，Linux 用例在 WSL 中跑一次）。另在真实终端（Windows Terminal）中验证：

| # | 操作 | 预期 |
| --- | --- | --- |
| B1 | 让模型启动 `python -m http.server 8765`（后台），再用 `curl` 访问 | 访问成功；`/tasks` 中有这条命令；状态栏显示后台任务数 |
| B2 | 让模型运行 `ping -n 60 127.0.0.1`（前台），运行中按 Ctrl+B | 工具行变为“后台 · 任务 tN”；模型继续回复；结束后自动开启一轮，模型报告结果 |
| B3 | `/tasks stop <id>` 停止 B1 的服务器 | 端口不再监听；任务管理器中没有残留的 `python` 进程 |
| B4 | 有后台命令时 `/exit` | 先确认；确认后没有残留进程 |
| B5 | `moss --sandbox`（MXC）中重复 B1 | 服务器在容器中运行，`curl` 从容器中访问成功；停止后容器结束 |
| B6 | `-p` 中让模型后台运行一个不会结束的命令 | 到时间限制时命令被结束，进程正常退出 |

## 不做

- **交互式输入**（Codex 的 `write_stdin`、PTY）：后台命令的 stdin 为空。需要交互的程序目前用前台命令加参数解决；以后需要时另立规格。
- **`Monitor` 式的逐行事件**：模型在需要时读取输出文件，结束时收到通知。按行把输出推给模型容易刷满上下文，先不做。
- **`!` 命令转入后台**：`!` 命令的输出要进入上下文，转入后台后结果的去向需要另行设计；先保持 Esc 取消。
- **跨会话的后台命令**：MossCode 退出时全部结束，恢复会话不重新运行。
- **内存、CPU 限制**（Claude Code 的 cgroup 内存上限）：与后台命令无关，单独考虑。

## 待定问题

| # | 问题 | 推荐 | 替代 |
| --- | --- | --- | --- |
| Q1 | 前台命令到达 `timeout` 时，是结束（现状）还是像 Claude Code 一样自动转入后台 | **结束**，保持现状：超时的语义模型已经熟悉，`-p` 和审批中的命令也不会意外地继续运行；需要长时间运行时模型用 `run_in_background`，用户用 Ctrl+B | 自动转入后台（Claude Code）：长构建不会白跑，但每次超时都会留下一个任务，`-p` 的等待时间变长 |
| Q2 | 后台命令的时间限制 | **交互界面中不限时**（与 Claude Code 相同）；**`-p` 中默认 10 分钟**，模型可以用 `timeout` 设到最多 1 小时，从启动（或转入后台）时算起。`timeout` 在后台时表示这个限制，所以后台时的最大值是 3600，前台仍是 600 | 所有场景都限时（如 30 分钟），交互中也要求模型估计时长 |
| Q3 | 同时运行的后台命令上限 | **单独计数，默认 8 个**，配置项 `command_max_background`：命令比子 Agent 便宜，开发服务器加监视构建很常见，不应占用子 Agent 的 4 个名额 | 与子 Agent 共用 `agent_max_background`（4 个）；或不设上限 |
| Q4 | 沙箱中的后台命令遇到没有规则放行的域名 | **拒绝，不弹审批**，在通知和输出文件末尾列出被拒的域名，模型可以请用户添加规则或在前台重新运行：与后台子 Agent 一致，也避免用户空闲时突然弹出与当前对话无关的审批 | 照常弹出审批面板（主对话空闲时也弹） |
| Q5 | 子 Agent 能否启动后台命令 | **能，但随子 Agent 结束**：子 Agent 的运行结束（完成、失败、取消）时，它启动的后台命令一并结束；它用 `read_file` 读输出文件，不收通知（子 Agent 没有 `task_output`、`task_stop`）。这样子 Agent 可以启动服务器再测试，又不会留下没人管的进程 | 子 Agent 中不提供 `run_in_background`；或像 Claude Code 那样后台子 Agent 的命令继续运行 |
| Q6 | 团队成员 | **与主对话相同**：每个成员是独立的 MossCode 进程，有自己的任务列表；成员退出时它的后台命令结束。Lead 看不到成员的后台命令 | 在 Lead 的 `/tasks` 中汇总成员的后台命令 |
| Q7 | Linux / macOS 上不开沙箱时，`setsid` 脱离的进程会漏掉（F11）。是否在本规格中解决 | **先记为已知限制**，写进文档；前台命令现在也有同样的问题。后续可以在启动时记录进程树、结束时按会话 id 环境变量查找残留进程（Claude Code 的做法未公开），另立任务 | 本规格中一起解决（Linux 可用 cgroup 或 `PR_SET_CHILD_SUBREAPER`，macOS 只能扫描进程表） |
| Q8 | 后台命令中的 `cd` 和环境变量 | **不影响之后的命令**（现在的前台命令也不保留状态，每条命令都是新的 shell），结果中不需要特别说明 | — |
