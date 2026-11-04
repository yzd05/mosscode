# 检查点与 `/rewind`

日期：2026-11-03。状态：已实现，已用真实模型验收（A1–A5，见 docs/acceptance.md 的 M132–M136）。

来源：
- [产品差距评审](../../research/product-gap-review.md)的 C1，属于“完善十：上手与安全网”。模型目录完成后，用户选择先做这一项（2026-11-03）。
- Claude Code 的[检查点文档](https://code.claude.com/docs/en/checkpointing)（2026-11-03 查阅）。每条开始一轮的用户消息都会创建检查点，记录 Claude 的文件编辑工具改过的文件。`/rewind` 或输入框为空时按两次 `Esc` 会打开菜单，菜单列出发过的消息。选中一条后可以：
  - Restore code and conversation（恢复代码和对话）
  - Restore conversation（只恢复对话）
  - Restore code（只恢复代码）
  - Summarize from here / up to here（从这里起或到这里为止压缩成摘要）
  - Never mind（取消）

  恢复对话后，选中那条消息的原文会放回输入框。检查点随会话保存，恢复会话后仍能使用，约 30 天后清理。命令改动的文件、外部改动和大部分子 Agent 的改动不跟踪；符号链接和硬链接跳过。
- 评审（同日）：用户同意 Q1–Q4 都按推荐方案定稿。

## 问题

- **P1 改坏了只能靠 git。** 模型用 `write_file` / `edit_file` 改错了文件，用户要自己用 git 或手动改回去。没提交过的改动、新建的文件和项目外的仓库都很难恢复。
- **P2 对话走偏了不能退回。** 要从某条消息重新开始，只能 `/clear` 后重讲一遍，或者在错误的上下文上继续纠正，浪费 Token，模型也容易被前面的错误带偏。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `tools/files.py` | 项目文件只有 `write_file` 和 `edit_file` 两个工具会写，都经过 `atomic_write`：先写临时文件再 `os.replace`，并保留原文件的权限位。`write_file` 可以覆盖不是 UTF-8 的文件，所以快照必须按字节保存。写入前 `FileState.check` 要求文件读过，而且读后没有被改过 |
| F2 | 其他写入者 | `memory.py`、`teams/` 也调用 `atomic_write`，但写的是 `~/.mosscode` 下的记忆和团队文件，不是项目文件 |
| F3 | `tools/base.py` | `ToolRegistry.view()` 给子 Agent 复制一份工具，`FileState` 换成新的。工作树中的子 Agent 用另一个根目录。团队成员有自己的注册表（`teams/runtime.py`） |
| F4 | 会话存档 | `Transcript` 是只追加的 JSONL，记录类型有 `session`、`message`、`compact`（`keep` 表示保留末尾几条）、`provider`、`skill`。`load_session` 按顺序重放，遇到不认识的类型就跳过。会话目录 `sessions/<id>/` 存放大结果和图片，`_prune_project` 在 30 天（`KEEP_DAYS`）后连同存档一起删除 |
| F5 | `messages.py` | `Message` 没有 id。用户输入的消息 `synthetic` 为 None，`command` 保存输入的提示词命令。压缩后历史变成 `[摘要边界] + 末尾若干条`，所以按下标或序号都无法对应到压缩前的消息 |
| F6 | `agent.py` | 用户消息和排队消息都通过 `_append` 进入历史并写入存档。`restore()` 替换历史并调用 `forget_reads()`。`_gap` 是只随下一次请求发送的一次性提醒，不进入历史 |
| F7 | `conversation.py` | `_rewind()` 是记忆提取的游标复位，在 `/clear` 和恢复会话后调用。`active_skills` 由 `skill` 记录恢复 |
| F8 | 界面 | 空闲时 `Esc` 依次用于：拒绝授权、关闭成员视图、关闭面板、关闭命令提示、取回排队消息。输入框支持找回带图片的历史消息 |

## 目标

- 用户可以把文件恢复到某条消息发出之前的状态，可以把对话退回到那条消息之前，也可以两者一起做。
- 检查点自动创建，不需要用户操作，也不改变正常对话的速度和请求内容。
- 恢复会话后还能回退，和会话存档一起保留和清理。

## 需求

### 检查点（CP）

| # | 要求 | 验收 |
| --- | --- | --- |
| CP1 | 每条用户消息（包括运行中排队的消息）进入历史时分配一个 id，写在存档的消息记录里 | 存档中的用户消息带 `id`；摘要边界等程序生成的消息没有 id |
| CP2 | `write_file` / `edit_file` 在一轮中第一次写某个文件之前，按字节保存它当前的内容；文件不存在时记为“不存在”。同一轮里再写同一个文件不再保存 | 一轮中改同一文件三次，只有一条检查点记录 |
| CP3 | 只在写入真正发生时保存：权限拒绝、“需要先读取”、匹配失败等情况不留记录 | 匹配失败的 `edit_file` 不产生记录 |
| CP4 | 主对话和在主工作区运行的子 Agent（前台和后台）改的文件都会被跟踪，记在当时最新的那条用户消息下面。工作树中的子 Agent、团队成员和命令改的文件不跟踪 | 子 Agent 用 `write_file` 改的文件可以恢复；`execute_command` 改的文件保持不变 |
| CP5 | 快照按内容的 SHA-256 存在会话目录的 `checkpoints/` 下，内容相同的只存一份。超过 10 MB 的文件不保存，记为“过大” | 同一内容只占一个文件；过大的文件在恢复时列为跳过 |
| CP6 | 保存快照失败（磁盘满、权限）时不阻止写入，本会话第一次失败时提示一次 | 模拟快照写入失败后工具照常写入，界面出现一次提示 |

### 回退（RW）

| # | 要求 | 验收 |
| --- | --- | --- |
| RW1 | `/rewind`（别名 `/checkpoint`）列出当前上下文中的用户消息，最新的在最上面。每行显示消息的第一行，有文件改动时附上“之后改动 N 个文件”。压缩掉的消息不列出 | 三轮对话后列出三条，压缩后只列出保留下来的 |
| RW2 | 选中消息后选择操作：恢复代码和对话、只恢复对话、只恢复代码、取消。那条消息之后没有文件改动时，只有“只恢复对话”和“取消” | 没有改动的消息不显示代码相关的选项 |
| RW3 | 恢复代码：对那条消息之后记录过的每个文件，写回第一次记录时的内容；记录为“不存在”的文件会被删除。恢复后显示恢复了哪些文件、删除了哪些、跳过了哪些及原因 | 改动两个文件、新建一个文件后恢复：两个文件内容复原，新文件被删除 |
| RW4 | 恢复对话：历史截断到那条消息之前，并在存档中追加一条 `rewind` 记录，退出后再恢复会话时也保持截断。那条消息的原文（包括图片）放回输入框 | 回退后重新打开会话，消息数与回退后相同；输入框中是原消息 |
| RW5 | 恢复对话后，记忆提取的游标和上下文估算随之更新。Token 统计和费用不回退，因为已经花掉了 | 状态栏的上下文比例变小，`/status` 的用量不变 |
| RW6 | 只恢复代码时，对话保留，下一次请求附带一次性提醒，列出被恢复的文件，要求模型重新读取后再修改。恢复代码后，所有文件都需要重新读取才能写 | 提醒出现在下一次请求中，不进入历史；之后不读取直接 `edit_file` 会被拒绝 |
| RW7 | 运行中不能回退。有后台子 Agent 运行时不能恢复代码，提示先等待或用 `/tasks` 停止 | 运行中输入 `/rewind` 提示不可用 |
| RW8 | 空闲且输入框为空时，0.5 秒内连按两次 `Esc` 打开 `/rewind` 列表；`Esc` 原有的用法（关闭面板、取回排队消息等）优先 | 空输入框双击 `Esc` 打开列表；面板打开时 `Esc` 只关闭面板 |
| RW9 | 恢复代码的顺序：先完成那条消息之后每个文件的第一份快照，再写回。任何一个文件失败不影响其他文件，失败的列在结果中 | 一个文件只读时其他文件照常恢复，结果中列出失败原因 |

### 文档（DOC）

| # | 要求 |
| --- | --- |
| DOC1 | `docs/reference/commands.md` 增加 `/rewind`；`interface.md` 说明双击 `Esc`；新增或扩展一节说明检查点：跟踪范围、不跟踪的情况、保留期限 |
| DOC2 | `docs/reference/sessions`（或会话相关文档）说明新的记录类型 `checkpoint` 和 `rewind` |
| DOC3 | README 的使用一节提一句；`CHANGELOG.md` 的 `[Unreleased]` 记一条 |

## 设计

### 模块

- **`checkpoints.py`（新）**：`Checkpoints` 类，由主对话创建，挂在主注册表的 `write_file` / `edit_file` 上（`FileTool.checkpoints`），`view()` 复制工具时一起带过去。工作树子 Agent 的工具根目录不同，不会带上。
  - `turn: str | None`：当前用户消息的 id，`Conversation` 在用户消息进入历史时更新。
  - `before_write(path)`：在工具的写入线程里调用，位置在检查通过之后、`atomic_write` 之前。本轮已记录过该路径就直接返回。否则读字节、写入 blob、追加记录，并用线程锁保护内存中的记录列表。
  - `records`：按时间排序的 `(message_id, 相对路径, blob 或 None 或 "too_large")`。
  - `marks: dict[message_id, int]`：每条用户消息发出时 `records` 的长度。
  - `changed_after(message_id)`：这条消息之后记录过的文件，每个文件取第一份快照。恢复时就用它。
  - `restore(message_id)`：写回或删除，返回恢复、删除、跳过、失败四类清单。写回用 `atomic_write` 的字节版本。
- **`Message.id`**：`str | None`，`field(default=None, compare=False)`，所以现有测试里消息的相等比较不受影响。`agent._append` 给 `synthetic is None` 的用户消息分配 `secrets.token_hex(6)`，序列化时写入 `id`。
- **存档**：
  - 新增记录 `{"type":"checkpoint","message":id,"path":"src/a.py","blob":"<SHA-256 前 32 位>"|null,"too_large":true?}`。
  - 新增记录 `{"type":"rewind","keep":N,"message":id}`，`keep` 的含义与 `compact` 相同：保留历史的前 N 条。
  - `load_session` 重放这两种记录，在 `Restored` 中返回检查点记录和每条消息的位置。
  - `Transcript.append` 加线程锁，因为检查点记录在工具的写入线程里追加。
  - 旧版本读到这两种记录时会跳过，所以旧版本打开新存档时只是看不到回退，不会出错。
- **`Conversation.rewind(message_id, code: bool, conversation: bool)`**：检查 RW7 的条件，然后恢复代码或截断对话：
  - 截断对话时，调用 `agent.restore(history[:i])` 但保留用量统计，再调用 `_rewind()`，并追加 `rewind` 记录。
  - 只恢复代码时设置一次性提醒，并调用 `forget_reads()`。
  - 返回结果文字和要放回输入框的消息。
- **`builtin_commands.py`**：`/rewind` 用 `ctx.ui.choose` 分两步选择（先选消息，再选操作）。
- **`tui.py`**：在 `action_cancel` 的最后加入双击 `Esc` 的判断（输入框为空、空闲、没有面板）。回退后把消息放回输入框，复用取回排队消息的方法，以保留图片。

### 恢复代码的算法

假设记录序列为 r1…rn，消息 M 发出时的位置是 marks[M]。对 marks[M] 之后出现的每个路径，取它第一次出现时的快照，写回去。

这样做在只回退对话之后也正确：在被放弃的那几轮里改过的文件，代码仍然保留着，它们的快照也在 marks[M] 之后。所以以后再回退到更早的消息时，这些文件也会被恢复到当时的状态。

### 数据流

```
用户消息 → agent._append（分配 id）→ Conversation 更新 checkpoints.turn，记下 marks
write_file/edit_file → 检查 → checkpoints.before_write(path) → atomic_write
/rewind → 选消息 → 选操作 → Conversation.rewind → 恢复文件 / 截断历史 + rewind 记录 → 界面显示结果，输入框放回原文
恢复会话 → load_session 重放 message/checkpoint/rewind → Checkpoints 恢复 records 和 marks
```

## 不做

- “从这里压缩”和“压缩到这里”：已有 `/compact`。以后需要再加。
- 回退到 `/clear` 之前的会话：用 `/resume`。
- 跟踪命令改动的文件。实际效果是：同一个文件只要被编辑工具改过，恢复时就会写回快照，不管之后是谁改的，这和 Claude Code 一致。
- 恢复前比较文件是否在外部被改过。恢复是用户主动选择的，结果会列出所有被写回的文件。

## 待定问题

| # | 问题 | 推荐 | 其他选择 |
| --- | --- | --- | --- |
| Q1 | 子 Agent 改的文件要不要跟踪？ | 跟踪在主工作区运行的子 Agent（前台、后台）。快照记在当时最新的用户消息下面，恢复结果和用户看到的过程一致。Claude Code 大多不跟踪，用户只能靠 git | 只跟踪主对话（和 Claude Code 一样），实现更简单，但子 Agent 改坏的文件无法退回 |
| Q2 | 恢复代码时，回退点之后新建的文件要不要删除？ | 删除，并在结果中列出，和 Claude Code 一致。保留的话，“恢复到之前的状态”就不完整了 | 保留新文件，只列出提示 |
| Q3 | 加不加双击 `Esc`？ | 加（RW8）。Claude Code 用户习惯这个操作，只在空闲且输入框为空时生效，不影响 `Esc` 现有的用法 | 只用 `/rewind` 命令 |
| Q4 | 列表的顺序 | 最新的在最上面，最常用的是退回上一两条 | 按时间顺序，最新的在最下面（Claude Code 的做法） |

## 任务

| # | 内容 | 验证 |
| --- | --- | --- |
| T1 | `Message.id`、序列化、`agent._append` 分配 id | `test_session*`、`test_serialization` 中存取带 id 的消息 |
| T2 | `checkpoints.py`：记录、blob、`changed_after`、`restore` | 新增 `tests/test_checkpoints.py`：同轮去重、不存在的文件、过大、失败不阻塞、恢复和删除 |
| T3 | 接入工具：`FileTool.checkpoints`、`view()` 带过去、工作树不带 | 子 Agent 改文件被记录；工作树子 Agent 不被记录；失败的写入不留记录 |
| T4 | 存档：`checkpoint` / `rewind` 记录、`load_session` 重放、`Transcript` 线程锁 | 回退后恢复会话，历史和检查点都正确；旧版本存档正常读取 |
| T5 | `Conversation.rewind` 和三种操作、一次性提醒、RW7 | 对话层测试 |
| T6 | `/rewind` 命令、两步选择、双击 `Esc`、输入框放回原文 | 无界面 TUI 测试 |
| T7 | 文档、CHANGELOG | `tests/test_docs.py` |
| T8 | 手动验收：真实模型改两个文件、新建一个文件，然后回退 | 见下面的验收步骤 |

## 验收（手动）

| # | 步骤 | 预期 |
| --- | --- | --- |
| A1 | 在一个测试目录让模型修改 `a.txt`、新建 `b.txt`，再发一条消息让它改 `a.txt` | `/rewind` 列出两条消息，第一条显示“之后改动 2 个文件” |
| A2 | 选第一条 →“恢复代码和对话” | `a.txt` 复原，`b.txt` 被删除，对话只剩回退点之前的内容，输入框中是第一条消息 |
| A3 | 重新发送，然后选“只恢复对话” | 文件不变，对话退回 |
| A4 | 退出后 `moss --resume` 回到该会话，再执行 `/rewind` | 回退过的消息没有回来，检查点仍可用 |
| A5 | 空输入框双击 `Esc` | 打开回退列表 |
