# 完善阶段 3b：权限体验（R1、R2、R3、R6、R7、L2）

来源：[差距评审](../../research/claude-code-pi-gap-review.md) 阶段 3 中尚未实施的部分。R4（计划模式只读 + exit_plan_mode）和 R5（危险命令分级）已在 [阶段 3](../refinement-03-plan-commands/spec.md) 完成。

## 现状

- default 档下 `git status`、`ls` 也要审批；计划模式不能运行任何命令。
- 命令规则只有 glob 和精确匹配。永久授权按精确命令保存，`pytest tests/a.py` 和 `pytest tests/b.py` 各存一条。
- 编辑审批只能逐个文件授权，没有“之后的编辑都自动通过”；切换档位只能输入 `/permissions`。
- 编辑审批显示原文和替换文本；写入审批显示新内容开头，看不出改了什么。
- 旧 `strict` 模式和 `permissive` 别名仍在，`decide` 中有专门分支。
- `AgentLoop` 在没有工具注册表时另存一份 `_mode`。

## 需求

| 编号 | 行为 |
| --- | --- |
| F1（R1） | **只读命令**无需审批：default、acceptEdits 中直接执行；计划模式中提供 execute_command，但只允许只读命令，其他命令为 `plan_blocked`。显式 deny 规则仍然生效 |
| F2（R1） | 只读命令的判定：<br>- 通过 `simple_command`：没有管道、串联、重定向、变量和命令替换；<br>- 命令在白名单中，且不含白名单禁止的选项；<br>- 所有非选项参数都是项目内的相对路径：不是绝对路径，不以 `~` 开头，不含 `..` 路径段。项目外读取仍按阶段 2 的约定审批 |
| F3（R2） | **前缀规则** `Bash(pytest:*)`：匹配命令的前几个词恰为 `pytest` 的简单命令。allow 前缀规则不匹配复合命令。deny 前缀规则还会检查复合命令中的每一段，例如 `Bash(git push:*)` 拒绝 `git status && git push` |
| F4（R2） | 普通命令审批增加一项：**永久允许以 `<前缀>` 开头的命令**，保存为 `Bash(<前缀>:*)`，写入 `.mosscode/permissions.local.yaml`。<br>- 前缀由命令推导：取开头不像参数的词，最多 3 个。<br>- 以解释器或包装命令开头且推导不出子命令时，不提供此项（例如 `python x.py`、`bash -c`、`env`、`xargs`）。<br>- 高风险命令仍然只有“本次允许 / 拒绝” |
| F5（R3） | 写入、编辑审批增加一项：**本会话自动接受编辑**。选择后当前调用放行，档位切换为 acceptEdits |
| F6（R3） | TUI 中 **Shift+Tab** 循环切换档位：default → acceptEdits → plan → default；bypassPermissions 时切到 default。任务运行中或审批中不切换，状态栏显示提示 |
| F7（R6） | 写入、编辑审批的预览改为统一 diff，按行着色（新增绿、删除红）。<br>- 新建文件显示新文件内容；<br>- 编辑匹配失败等无法生成 diff 时，退回显示原文和替换文本；<br>- 预览最多显示 16 行，超出部分标注总行数 |
| F8（R7） | 删除 `strict` 模式和 `permissive` 别名。配置或参数中使用它们时启动失败，并提示改用 default 或 bypassPermissions |
| F9（L2） | 计划状态只保存在权限策略中。没有工具注册表时始终是自动模式；此时进入计划模式会报错 |

## 设计

| 位置 | 改动 |
| --- | --- |
| `security.py` | 新增 `read_only_command(command) -> bool` 和 `command_prefix(command) -> str \| None`，二者都基于 `simple_command` 和 `shlex` 分词 |
| `permissions.py` | - **Operation**：增加 `read_only: bool`，表示只读命令。<br>- **operation()**：计划模式中，只读命令放行；其他非只读工具仍为 `plan_blocked`。<br>- **decide()**：参数 `read_only` 与 `operation.read_only` 取或。<br>- **Rule**：增加前缀匹配（`pattern` 以 `:*` 结尾）。<br>- **审批选项**：<br>  - 普通命令：`("once","session","prefix","permanent","deny")`，推导不出前缀时去掉 prefix；<br>  - 写入、编辑：`("once","session","accept_edits","permanent","deny")`；<br>  - 其他工具：维持四项。<br>- **选择处理**：`prefix` 用 `_persist` 保存前缀规则；`accept_edits` 调用 `set_mode("acceptEdits")`。<br>- **删除 strict**：删去 `strict` 分支和 `MODE_ALIASES` |
| `tools/base.py` | `Tool.plan_commands = False`；`definitions(plan=True)` 也提供 `plan_commands` 为真的工具 |
| `tools/process.py` | `ExecuteCommand.plan_commands = True` |
| `tools/files.py` | - **EditFile**：抽出纯函数 `_replace(text, params)`，供执行和预览共用；<br>- **预览方法**：WriteFile 和 EditFile 增加 `approval_preview(params) -> str \| None`，只读文件，不写入；<br>- **调用方式**：策略在构造 Operation 时调用，异常时退回原预览 |
| `agent.py` | 删除 `_mode`；`mode` 只从策略读取 |
| `prompts.py` | 计划模式的说明和提醒改为“只读工具和只读命令（git status/diff/log、ls 等）” |
| `tui.py` | - **审批面板**：新选项的标签；diff 按行着色；详情区最大高度由 7 行放宽到 20 行；<br>- **Shift+Tab**：绑定档位循环；<br>- **`/permissions`**：只接受四个档位 |

### 只读命令白名单

| 命令 | 条件 |
| --- | --- |
| `git status/diff/log/show/blame/rev-parse/ls-files/describe/shortlog` | 第二个词必须是子命令（不允许 `git -c …`）；禁止 `--output`、`--ext-diff` |
| `git branch`、`git remote` | 只能带 `-a -r -v -vv --list --show-current --all --remotes --verbose` 这些选项，不带位置参数 |
| `ls pwd cat head wc stat tree which grep egrep fgrep` | 无额外限制 |
| `tail` | 禁止 `-f`、`-F`、`--follow` |
| `rg` | 禁止 `--pre` |
| `find` | 禁止 `-delete -exec -execdir -ok -okdir -fprint -fprint0 -fprintf -fls` |
| PowerShell：`Get-ChildItem Get-Content Get-Location Select-String Test-Path Get-Item`，以及 `gci gc sls dir` | 不区分大小写 |

## 取舍

- **白名单保守**：只覆盖调查最常用的命令，宁可多问。`pytest --collect-only` 会导入项目代码，所以不纳入。Git 若有恶意的仓库配置（如 fsmonitor），`git status` 也可能执行代码；与 Claude Code 相同，接受这一风险。
- **前缀授权是永久的**：这样可以解决永久规则越积越多的问题。只提供推导结果，不让用户手动编辑前缀；需要更宽或更窄的规则时，直接编辑 YAML。
- **本会话自动接受编辑**：实现为切换档位，所以命令仍按 acceptEdits 处理（需要审批）。这与 Claude Code 一致。
- **删除 strict 不兼容旧配置**：项目还没有外部用户。本机检查过，没有使用它的权限文件。

## 验收

| 编号 | 方法 |
| --- | --- |
| AC1（F1、F2） | 白名单命令为 True，越界参数、禁用选项、复合命令、全局选项为 False。default 中只读命令直接执行，非只读命令为 `approval_required`；有 deny 规则时为 `permission_denied`；计划模式中只读命令执行，其他命令为 `plan_blocked`，计划工具定义含 execute_command |
| AC2（F3） | `Bash(pytest:*)` 匹配 `pytest`、`pytest tests/a.py`，不匹配 `pytestx`、`pytest && rm a`；deny `Bash(git push:*)` 拒绝 `git status && git push origin` |
| AC3（F4） | 前缀推导表测试。交互：选择前缀授权后，本地文件出现 `Bash(pytest:*)`，重载后 `pytest tests/b.py` 免审批；`python x.py` 的选项中没有 prefix；高风险命令仍为两项 |
| AC4（F5、F6） | 编辑审批选择“本会话自动接受编辑”后，档位为 acceptEdits，后续写入免审批，命令仍需审批。TUI 中 Shift+Tab 按顺序循环，运行中不切换 |
| AC5（F7） | 编辑审批预览含 `-旧`/`+新` 的统一 diff；覆盖写入显示 diff，新建文件显示内容；匹配失败时退回原文和替换文本；超过 16 行时有截断说明；TUI 中 diff 行着色 |
| AC6（F8、F9） | `strict`/`permissive` 在配置和参数中报错并给出替代；没有注册表时进入计划模式报错 |
| 回归 | 全量 pytest、compileall、Windows 控制台烟测 |
