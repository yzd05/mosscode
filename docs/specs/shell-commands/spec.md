# `!` 命令

日期：2026-11-04。状态：已实现，已验收（2026-11-04，M142–M147）。

来源：
- [产品差距评审](../../research/product-gap-review.md)的 B2，属于“完善十一：日常顺手”。B1 `@` 文件引用完成后接着做（2026-11-04）。
- Claude Code 的 bash 模式：输入以 `!` 开头时输入框前缀变成 `!`，命令直接执行，输入和输出作为消息进入上下文，但不会让模型回复。pi 的 `!命令` 同样把输出交给模型，`!!命令` 只执行不进入上下文。

## 问题

想看一眼 `git status` 或跑一次测试，只能请模型调用 `execute_command`（多一次请求，还要审批），或者切到另一个终端（模型看不到结果）。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `tools/process.py` | `ExecuteCommand` 用注册时选定的 `Shell`（Windows 上优先 Git Bash）；`run_process` 限制输出、保留开头和结尾，取消时结束整个进程树 |
| F2 | `permissions.py` | `operation()` 对命令做硬性检查 `check_command`（空字符、内置黑名单），计划模式拒绝非只读命令；`decide()` 处理显式拒绝规则，没有按档位的拒绝 |
| F3 | `conversation.py` | `_fork_result` 用 `agent._append` 在一轮之外向历史写入消息，写入存档 |
| F4 | `session.py` | 恢复时只把末尾“用户输入的”消息放回输入框，`synthetic` 消息留在历史中 |
| F5 | `providers.py` | 相邻的同角色消息会合并，用户消息后面再跟一条用户消息没有问题（B1 已经这样用） |
| F6 | `docs/reference/sessions.md` | `/rewind` 不跟踪 `execute_command` 改的文件 |

## 需求

| # | 要求 | 验收 |
| --- | --- | --- |
| R1 | 输入以 `!` 开头（允许前导空白）时是命令；输入框前缀从 `❯` 变成 `!`，`@` 文件补全不弹出 | 输入 `!git status` 时前缀是 `!` |
| R2 | 命令在 `execute_command` 使用的同一个 shell 中、在项目根目录执行，stdin 为空，没有超时；Esc 取消并结束进程树。每条命令单独执行，`cd` 不会保留到下一条 | `!pwd` 输出项目根目录 |
| R3 | 不需要审批，计划模式中也能执行，因为命令是用户自己输入的；内置黑名单和显式拒绝规则照样生效，被拒绝时只显示原因、不执行 | `deny: execute_command(rm *)` 时 `!rm a` 不执行 |
| R4 | `!命令` 的命令、退出码和输出（开头和结尾各最多 16 KiB）作为一条 `synthetic` 为 `shell-command` 的用户消息写入历史和存档；不会让模型回复，下一条消息发送时模型能看到 | 执行后再问“刚才的输出是什么”，模型不调用工具就能回答 |
| R5 | `!!命令` 只执行、显示，不进入上下文 | `!!ls` 后历史没有变化 |
| R6 | 界面显示 `! 命令`，下面是输出（最多显示开头 10 行和结尾 20 行）和退出码、耗时；取消或被拒绝时显示原因。恢复会话时命令消息显示为一行 | 退出码非 0 时以错误颜色显示 |
| R7 | 回复进行中不能执行命令：提示稍后再发，文字留在输入框中。命令执行期间输入的消息排队，结束后放回输入框 | 运行中输入 `!ls` 有提示 |
| R8 | 只输入 `!` 或 `!!` 时提示用法，不执行 | |

## 设计

- **`commands.parse_input`**：以 `!` 开头时返回 `Parsed("shell", text=命令, word="!" 或 "!!")`。
- **`user_command.py`（新）**：`run_user_command(command, registry)` 取注册表中的 `execute_command`，用 `registry.refusal` 做硬性检查和拒绝规则（`operation()` 对用户输入的命令跳过计划模式的拦截），再用 `run_process` 执行；`command_message(...)` 生成写入历史的消息。
- **`Conversation.run_command(command, keep=)`**：在没有请求运行时执行；`keep` 时用 `agent._append` 写入消息。
- **界面**：`_run_shell` 作为 `_reply_task` 运行，Esc 走现有的取消；`ShellEntry` 显示命令和输出；`resumed_rows` 显示 `shell-command` 消息；输入框前缀随内容切换。

## 不做

- `-p` 中的 `!`：无界面模式的提示就是给模型的，不解析 `!`。
- 命令不触发 Hook：Hook 针对模型的工具调用。
- 交互式命令（需要输入的程序、分页器）：stdin 为空，需要交互时请在终端中运行。
- `/rewind` 不恢复 `!` 命令改的文件，与 `execute_command` 相同。

## 验收

自动测试覆盖 R1–R8。手动验收见 [docs/acceptance.md](../../acceptance.md)。
