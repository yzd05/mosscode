# 9b：会话存档与恢复

日期：2026-10-24。状态：已实现（2026-10-24）。总览见 [spec.md](spec.md)。

## 目标

每个会话的消息实时追加到 JSONL 文件。程序退出、崩溃或断电后，用户可以选择一个会话接着对话，体验与没有中断时一致；异常的存档也能尽量恢复，不会让会话无法继续。

## 现状

- 历史只在内存中（`AgentLoop.history`）。消息进入历史的位置有以下几处：
  - 排队消息在迭代边界立即追加（`agent.py` 约 220 行）；
  - 第一次完整回复后追加用户消息和回复（约 258、260 行，“晚提交”）；
  - 输出截断时追加 `output_truncated` 结果（约 277 行）；
  - `execute_batch` 先追加空的 tool 消息占位，每完成一个调用就原地替换，`finally` 中补齐 `not_executed` 并经过 `bound_message` 处理后才是最终结果。
- 历史被整体替换的位置有三处：
  - 压缩（`self.history = done.history`，结构为 `[边界消息, 近期原文…]`）；
  - `set_provider`（清除 `native` 和 `protocol`）；
  - `clear`（清空）。
- 第八章的会话目录为 `~/.mosscode/sessions/<时间>-<4 位随机串>/`，首次写入时创建，含标记文件，启动时清理 30 天前的目录。`/clear` 不新建会话目录。边界消息和存盘预览中写有该目录下文件的绝对路径。
- `ToolResult.details` 只在本地使用，可能包含 diff 文本、路径、耗时等；`Message.native` 保存 thinking 签名、加密推理等只对原模型有效的数据。
- 权限的“本会话允许”规则存放在 `PermissionPolicy.session` 中；`FileState` 记录读过的文件。
- 无界面模式的 json 结果包含 `compactions`，没有会话 ID。
- TUI 的 `/model` 在输入框下方用选项列表（`KeyboardOptions`）选择。

## 需求

### 写入

| # | 行为 |
| --- | --- |
| S1 | **位置与命名**：存档路径为 `~/.mosscode/projects/<项目键>/sessions/<会话 ID>.jsonl`（项目键和 ID 格式见总览）。第一条记录写入时才创建文件，没有任何消息的运行不留下文件 |
| S2 | **第一行是会话记录**：`{"type":"session","v":1,"id":…,"cwd":…,"created":…,"mosscode":版本}`，键按这个顺序写。它是 JSONL 中的一行，不是单独的 meta 文件 |
| S3 | **随历史提交而写**：消息每次进入历史时追加一条 `{"type":"message","time":…,"message":{…}}`。<br>• 排队消息并入时立即写；<br>• 用户消息与第一次完整回复一起写；<br>• 一批工具结果在 `execute_batch` 结束、经过第一层处理后写一次，不写中间状态。<br>生成到一半的回复不写，与“晚提交”一致 |
| S4 | **历史替换写成记录**：<br>• 压缩成功写 `{"type":"compact","boundary":{边界消息},"keep":N,"record":记录文件路径}`，表示“历史变为边界消息加上当前历史的最后 N 条”；<br>• `/model` 切换写 `{"type":"provider","name":…,"protocol":…,"model":…}`，表示此前的消息清除 `native` 和 `protocol`；<br>• `/clear` 不写记录，而是结束当前会话，下一条消息开始新的会话 ID 和新的会话目录 |
| S5 | **消息完整序列化**：<br>• role、content（kind、text）、protocol、native、tool_calls（id、name、arguments）、tool_results（call_id、name、output、is_error、code、details）、synthetic 都写入；<br>• `details` 中不能转换为 JSON 的值转成字符串，转换失败的键丢弃；<br>• 序列化和反序列化往返后，消息与原消息相等（details 中被转换的值除外） |
| S6 | **原子追加**：每条记录先序列化为一整行（`ensure_ascii=False`，行尾 `\n`），用一次 `write()` 写入，然后 `flush()`。打开已有文件准备追加前，如果最后一个字节不是 `\n`，先把文件截断到最后一个 `\n` 之后，避免新记录接在半行后面 |
| S7 | **写入失败不影响对话**：磁盘满、权限错误等情况，在界面提示一次“会话存档写入失败：原因，本会话之后的消息不会被保存”，对话继续，之后不再尝试写入 |
| S8 | **会话目录随会话 ID**：第八章的会话目录改为 `sessions/<会话 ID>/`，与 JSONL 同名，存盘结果和压缩记录仍写在其中。恢复会话时继续使用同一目录，历史中引用的路径保持有效 |
| S9 | **使用中的锁**：会话在用时持有 `<会话 ID>.lock` 上的操作系统独占锁（Windows 用 `msvcrt.locking`，其他系统用 `fcntl.flock`），进程退出或崩溃时由系统释放。锁被占用的会话不能恢复，提示“该会话正在另一个 MossCode 中使用” |
| S10 | `persist_sessions: false` 时不写存档、不加锁，`/resume`、`--continue`、`--resume` 不可用并说明原因 |

### 列出与选择

| # | 行为 |
| --- | --- |
| R1 | **入口**：<br>• `--continue`（`-c`）：恢复本项目最近活动的会话；<br>• `--resume ID`：恢复指定会话，可与 `-p` 一起使用；<br>• `--resume` 不带 ID：交互界面启动后打开会话列表；<br>• TUI 空闲时输入 `/resume` 打开列表，`/resume ID` 直接恢复。运行中不能恢复 |
| R2 | **列表**：显示本项目最近 20 个会话，按最后活动时间（文件修改时间）倒序。每项显示：最后活动时间、标题、消息数、会话 ID 的后 4 位；当前会话和被锁定的会话标注出来。↑ / ↓ 选择，Enter 恢复，Esc 收起，交互方式与 `/model` 列表相同 |
| R3 | **标题与消息数从 JSONL 计算**：<br>• 标题是第一条非合成用户消息的第一行，最多 40 个字符，只在文件开头最多 64 KiB 内查找，找不到时显示“（无标题）”；<br>• 消息数是以 `{"type":"message"` 开头的行数，按字节扫描计数，不解析每行 JSON。<br>列表不需要读取或维护任何额外文件 |
| R4 | **没有可恢复的会话**时提示“本项目没有可恢复的会话”；指定的 ID 不存在时提示并列出最近的几个 ID |

### 恢复

| # | 行为 |
| --- | --- |
| R5 | **重放**：逐行解析。<br>• `message` 记录追加到历史；<br>• `compact` 记录把历史替换为“边界消息 + 最后 keep 条”；<br>• `provider` 记录清除此前消息的 `native` 和 `protocol`；<br>• 未知 `type` 跳过（兼容以后的版本）。<br>第一行不是 `session` 记录的文件仍然尝试恢复 |
| R6 | **坏行跳过**：JSON 解析失败、缺少必需字段或字段类型不对的行跳过并计数，恢复完成后提示“跳过了 N 行无法读取的记录”。`compact` 记录的 keep 大于当前历史长度时按当前长度处理 |
| R7 | **补齐未完成的工具调用**：重放后，任何带 tool_calls 的 assistant 消息，如果紧随其后的 tool 消息缺少某些调用的结果（或根本没有 tool 消息），就为缺失的调用补上结果：错误码 `interrupted`，内容为“会话中断，这个调用可能未执行，也可能执行了但结果丢失；需要时请检查当前状态后重新执行”。保留助手消息的正文，不截断 |
| R8 | **清理孤立和悬空的消息**：<br>• tool 消息中找不到对应调用的结果删除；<br>• 历史末尾没有得到回复的用户消息（例如排队并入后进程中断）从历史中移除，其文本放回输入框（TUI），或在 `-p` 中输出到 stderr，让用户决定是否重新发送 |
| R9 | **模型适配**：用当前 Provider 恢复。如果存档中最后一次 `provider` 记录（没有时以消息的 `protocol` 为准）与当前 Provider 的名称或协议不同，清除全部消息的 `native` 和 `protocol`，与 `/model` 切换相同 |
| R10 | **超限先压缩**：恢复后锚点失效，按全量估算。估算达到当前 Provider 的自动阈值时，立即压缩一次再等待输入，与手动压缩一样不计入熔断，并显示第八章的压缩进度行。`auto_compact: false` 时也执行，否则会话无法继续。压缩失败时会话仍然恢复，并提示“恢复的会话超出上下文窗口，压缩失败：原因；可以 /compact 重试或 /clear” |
| R11 | **时间跨度提醒**：最后一条记录的时间距现在 ≥ 6 小时（总览 Q2）时，恢复后的第一次请求附加一次 `<system-reminder>`：“距上次对话已过去约 N 小时（上次：日期时间）。期间文件、分支和依赖可能已经变化；涉及文件内容时先重新读取，不要依据历史中的旧输出下结论。”机制与模式提醒相同，不写入存档 |
| R12 | **重置的状态**：<br>• `FileState` 清空，修改文件前必须重新读取；<br>• 权限档位保持本进程的当前档位，不恢复存档时的档位；<br>• “本会话允许”的规则清空；<br>• 计划模式不恢复；<br>• 项目指令和 git 快照重新加载（含 9a 的展开）；<br>• Token 累计从 0 开始；<br>• 模式提醒在第一次请求时重新发送 |
| R13 | **继续写入同一个文件**：恢复后取得锁，新消息继续追加到原来的 JSONL，会话目录不变。TUI 中恢复其他会话前，先释放当前会话的锁 |
| R14 | **界面呈现**：<br>• 恢复后，对话区先显示一行“已恢复会话 · 标题 · N 条消息 · 上次活动 时间”；<br>• 再按顺序显示最近 20 条消息的简要形式：用户消息原文，回复的 Markdown，工具调用每个一行（工具名和主要参数，不展开 diff），边界消息显示为“（较早的对话已压缩）”；<br>• 然后是 R6、R7、R8 的提示。<br>这些内容像普通记录一样进入终端滚动历史 |
| R15 | **无界面输出**：json 与 stream-json 的结果增加 `session_id`。stream-json 的开头增加 `{"type":"session","id":…,"resumed":true/false}` |

### 清理

| # | 行为 |
| --- | --- |
| G1 | 启动时清理所有项目的 `sessions/` 目录：最后修改时间在 30 天前的 `<ID>.jsonl` 与同名会话目录、锁文件一起删除。只处理文件名符合 ID 格式的文件；锁被占用的会话跳过。会话目录仍要求带第八章的标记文件 |
| G2 | 第八章的旧位置 `~/.mosscode/sessions/` 不再写入，继续按原规则清理，直到其中的目录全部过期 |
| G3 | 清理失败（文件被占用等）静默跳过，不影响启动 |

## 设计

| 模块 | 改动 |
| --- | --- |
| `paths.py`（新） | `project_key(root)`、`project_dir(root)`、`SESSION_ID` 正则，供 9b、9c 共用 |
| `session.py` | `Session` 增加 `id` 和 `transcript_path`，`directory` 改为 `sessions/<id>/`。新增 `Transcript`：<br>• `append(record)`（S6、S7）；<br>• `lock()` / `unlock()`（S9）。<br>新增：<br>• `list_sessions(root, limit)`（R2–R3）；<br>• `load_session(path) -> Restored(history, skipped, interrupted, unanswered, last_time, provider)`（R5–R9）；<br>• 清理逻辑扩展到 `projects/*/sessions`（G1–G3） |
| `serialization.py`（新） | `message_to_json` / `message_from_json`（S5），对字段做严格校验，不合格时抛出异常，由 `load_session` 计为坏行 |
| `agent.py` | 新增 `on_commit` 回调（由 `Conversation` 设置）。在 S3 的提交点、压缩成功和 `set_provider` 时调用，传入要写的记录。新增：<br>• `restore(history)`：设置历史，清空 FileState，使锚点失效，并设置下次发送模式提醒；<br>• 一次性的 `gap_reminder`（R11）。<br>Agent 不直接操作文件 |
| `tools/execution.py` | 不改动；agent 在 `execute_batch` 结束后提交最终的 tool 消息 |
| `prompts.py` | `gap_reminder(hours, last_time) -> Supplement` |
| `conversation.py` | 持有当前 `Session` 和 `Transcript`；`clear()` 时结束当前会话并新建 `Session`；新增 `async resume(session_id)`（R7–R13，含 R10 的立即压缩，以事件形式产出供界面显示）；暴露 `session_id` |
| `cli.py` | `--continue` / `-c`、`--resume [ID]`；`persist_sessions` 检查；清理；json 和 stream-json 输出 `session_id`（R15） |
| `config.py` | `Settings.persist_sessions: bool = True` |
| `tui.py`、`appearance.py` | `/resume` 命令、列表、补全和帮助；R14 的恢复呈现；R8 放回输入框 |
| `permissions.py` | `session_dir` 跟随 `Session` 变化（`/clear`、恢复后更新），读取豁免仍然有效 |

### 一条 JSONL 的生命周期

```
启动 → Session(id) 惰性创建
第 1 次提交 → 创建 <id>.jsonl，写 session 记录，加锁 → 写 message
……         → 写 message / compact / provider
/clear     → 解锁；新 Session(id2)；下一次提交写入 <id2>.jsonl
退出或崩溃 → 锁由系统释放；最后一行可能不完整
/resume id → 加锁；截掉不完整的半行；重放、修复；可能压缩（压缩时写 compact）→ 继续追加
```

### 重放与修复（伪代码）

```
history, skipped = [], 0
for line in file:
    try: record = parse(line)
    except: skipped += 1; continue
    match record.type:
        "message":  history.append(record.message)
        "compact":  history = [record.boundary] + history[len(history) - min(keep, len(history)):]
        "provider": history = strip_native(history); last_provider = record
history = drop_orphan_results(history)
history, interrupted = fill_missing_results(history)      # R7
history, unanswered = pop_trailing_user(history)          # R8
```

## 取舍

| 决定 | 理由 |
| --- | --- |
| 存档放在用户目录，不放在项目的 `sessions/`（用户初稿） | 存档包含完整的工具输出，可能有凭据；放在项目中容易被提交，也会被 glob / grep 搜到。同时与第八章的会话目录合并为一处 |
| 压缩和切换模型写成记录，不重写文件 | 保持只追加，崩溃最多丢最后一行；重写整个文件在中途崩溃时可能丢掉全部内容 |
| 工具结果整批写一次，不写中间状态 | 中间状态会在重放时产生重复的 tool 消息。代价是崩溃时这一批的结果全部丢失，由 R7 补为 `interrupted`，模型会检查状态后重新执行 |
| 缺失结果补为 `interrupted`，不截断助手消息（用户初稿为截断） | 截断会丢掉模型已经说出的分析和计划。补结果与第四章取消时补 `not_executed` 一致，协议也合法 |
| 末尾未回复的用户消息放回输入框 | 直接保留会与下一条用户输入形成两条相邻的用户消息；直接删除会丢掉用户的输入 |
| 第一行写会话记录，列表只读文件头和行数 | 满足“不维护 meta 文件”，又避免为了标题完整解析可能有几 MB 的文件 |
| 用操作系统锁，不用写入 PID 的锁文件 | 进程崩溃时系统自动释放锁，不需要判断 PID 是否还活着（Windows 上判断不可靠） |
| 不恢复权限档位和会话授权 | 用户今天批准的“本会话允许”规则，不应在一周后恢复会话时自动生效 |
| `/clear` 开始新会话，不写清空记录 | 旧会话仍然可以恢复；一个文件对应一段连续的对话，列表中的标题和消息数更有意义 |
| 超限时在恢复时立即压缩（即使 `auto_compact: false`） | 用户初稿要求“token 超限先压一次”。不压缩的话，第一次请求必然失败 |

## 不做

会话重命名、删除命令（用户可直接删除文件）、从中间某条消息分叉、跨项目列出会话、存档加密、恢复时重放工具的 diff 显示。

## 任务

| # | 内容 | 验证 |
| --- | --- | --- |
| T1 | `paths.py`；`serialization.py` 消息往返 | AC1 |
| T2 | `Transcript`：追加、半行截断、写入失败、锁 | AC2、AC3 |
| T3 | `load_session`：重放、坏行、compact 和 provider 记录、补齐结果、孤立结果、末尾用户消息 | AC4、AC5 |
| T4 | `list_sessions` 与清理（含旧位置） | AC6、AC10 |
| T5 | `agent.py` 提交回调、`restore`、时间跨度提醒；`Conversation` 的会话管理、`clear` 新会话、`resume`（含立即压缩） | AC7、AC8、AC9 |
| T6 | CLI 参数与输出；TUI `/resume` 列表、恢复呈现、放回输入框 | AC11 |
| T7 | 文档：新增 `docs/reference/sessions.md`；README 的命令、参数和配置；`config.example.yaml`；`interface.md` 中“不提供退出后的恢复功能”和 README 末尾“不支持会话持久化与恢复”的说明；验收指南“阶段 9b” | `tests/test_docs.py` |

T1 先做；T2、T3、T4 依赖 T1，三者互不依赖；T5 依赖 T2、T3；T6 依赖 T4、T5。

## 验收

| 验收 | 方法 |
| --- | --- |
| AC1（S5） | 含文本、thinking、native、工具调用、各种工具结果（含 details 中的 Path、diff）和边界消息的历史，序列化往返后相等；details 中无法转换的值不会导致写入失败 |
| AC2（S1–S3、S6） | 用 SequenceProvider 跑多轮工具任务：第一行是会话记录；记录顺序与历史提交顺序一致；工具结果每批一条且是最终结果。没有消息的运行不创建文件。手工写入半行后再追加：半行被截掉，新记录完整 |
| AC3（S7、S9、S10） | 写入时抛出 OSError：提示一次，对话继续。同一会话在两个 Conversation 中恢复，第二个被拒绝；第一个关闭后可以恢复。`persist_sessions: false` 时不创建文件，`/resume` 说明原因 |
| AC4（R5、R6、R9） | 构造含坏行、未知类型、compact、provider 记录的文件：重放结果与运行时的历史一致，坏行计数正确。当前 Provider 与存档不同时 native 被清除；相同时保留 |
| AC5（R7、R8） | 末尾是带 3 个调用的助手消息但没有结果：补 3 个 `interrupted` 结果，助手正文保留，三种协议都能生成合法请求。一批中只缺部分结果时只补缺失的。孤立结果被删除。末尾两条未回复的用户消息被移除并返回其文本 |
| AC6（R2–R4） | 20 多个会话按修改时间排序，只返回 20 个；标题取第一条非合成用户消息并截断；消息数正确；锁定的会话被标注。列表过程中不打开会话目录，不解析每一行 |
| AC7（R10） | 恢复一个估算超过自动阈值的会话：在等待输入前完成压缩，写入 compact 记录，不计入熔断；`auto_compact: false` 时同样压缩；摘要失败时仍然恢复并提示 |
| AC8（R11、R12） | 最后记录在 7 小时前：第一次请求包含时间跨度提醒，第二次不包含，存档中没有这条提醒。5 小时前时没有提醒。恢复后未读取就 `edit_file` 返回 `not_read`；之前“本会话允许”的命令重新要求审批；权限档位为启动参数指定的档位 |
| AC9（R13、S4、S8） | 恢复后的新消息追加到原文件；存盘结果路径和压缩记录路径仍可读取。`/clear` 后的消息写入新的会话 ID，旧会话仍可恢复 |
| AC10（G1–G3） | 31 天前的 jsonl、会话目录、锁文件一起删除；29 天前的保留；锁被占用的跳过；文件名不符合格式的文件不删除；旧位置的带标记目录按原规则清理 |
| AC11（R1、R14、R15） | `--continue` 恢复最近的会话；`--resume ID -p "继续"` 在恢复的上下文中回答，json 包含 `session_id`。TUI 中 `/resume` 打开列表，Enter 恢复后显示恢复行、最近消息和异常提示，未回复的消息出现在输入框中。运行中输入 `/resume` 被拒绝 |
| 人工验收 | 真实模型：<br>① 让模型修改 notes.txt 后退出，`--continue` 询问“刚才改了什么”，模型能回答；<br>② 在长时间运行的命令中关闭终端窗口，重新 `--continue`，看到中断提示，模型检查状态后继续；<br>③ 修改存档的修改时间为 7 小时前（验收脚本提供），恢复后模型提到时间间隔，并在回答文件内容前重新读取；<br>④ 用 50K 窗口的配置恢复一个大会话，看到压缩进度行 |
| 回归 | 全量 pytest、Windows 控制台烟测 |
