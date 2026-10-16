# 完善阶段 3：计划模式只读 + exit_plan_mode，危险命令分级

来源：[差距评审](../../research/claude-code-pi-gap-review.md) 的 R4、R5，以及已采用的取舍 3、4。本阶段只做这两项；只读命令免审批（R1）、前缀规则（R2）、编辑审批的“本会话全部允许”（R3）、diff 审批预览（R6）和删除 strict（R7）不在范围内。

## 现状

- 计划模式只从工具列表中去掉 MCP 工具，写入、编辑和命令仍可调用，由权限系统审批；退出靠用户输入 `/do`。提示词需要专门解释“计划模式里也能经审批写入”。
- `security.check_command` 对所有已知危险特征一律硬拒绝，包括 `git reset --hard`、`git push --force`、`rm -rf build`；即使用户明确要求也无法授权。

## 需求

| 编号 | 行为 |
| --- | --- |
| F1 | 计划模式下，模型只能拿到只读工具（read_file、glob、grep）和 `exit_plan_mode`。write_file、edit_file、execute_command 和所有 MCP 工具在计划模式下返回 `plan_blocked` 并且不执行；任何规则和档位都不能放行 |
| F2 | 新工具 `exit_plan_mode(plan)`：plan 是 Markdown 计划文本。只在计划模式中提供；非计划模式下调用返回 `not_in_plan` |
| F3 | 调用 `exit_plan_mode` 总是请用户审阅，三个选项：**批准并执行**（恢复进入计划前的权限档位）、**批准并自动接受编辑**（切换为 acceptEdits）、**继续规划**（拒绝，仍在计划模式）。没有本会话或永久选项，不能被规则放行 |
| F4 | 批准后立即退出计划模式，同一任务的下一轮请求就能使用全部工具，不需要用户再次输入；工具结果告诉模型已经可以执行。继续规划时返回 `permission_denied`，模型据此修改计划 |
| F5 | 计划显示：TUI 中提交计划时，在对话里以 Markdown 显示完整计划（随后进入原生滚动历史，长计划也能完整查看），审批面板只提示审阅。无界面模式打印计划，并以 `approval_required` 拒绝 |
| F6 | 命令分两级。**灾难级**：删除文件系统根目录或用户主目录、`--no-preserve-root`、格式化和分区、写块设备、fork 炸弹、关机重启，仍为 `dangerous_command` 硬拒绝 |
| F7 | **高风险级**：递归强制删除（rm -rf、Remove-Item -Recurse -Force、rmdir /s、del /s）、`git reset --hard`、`git clean -f`、`git push --force`、`Invoke-Expression`/`iex`、PowerShell 编码命令。在所有档位都要求每次确认：会话规则、永久规则和 bypassPermissions 都不能自动放行；显式 deny 规则仍然生效；审批只有“本次允许”和“拒绝”；无界面时为 `approval_required` |
| F8 | 审批面板说明命令为何属于高风险；提示词和 `/plan`、`/permissions` 的说明改为新语义。`/do` 保留，作为用户手动退出计划模式的方式 |

## 设计

| 位置 | 改动 |
| --- | --- |
| `security.py` | 拆成 `CATASTROPHIC_COMMANDS` 和 `RISKY_COMMANDS`。`check_command` 只对 NUL 和灾难级抛出 `SecurityError`；新增 `command_risk(command) -> str \| None`，返回高风险原因 |
| `permissions.py` | `Operation` 增加 `risk`。`operation()` 开头：在计划模式中，非只读、且不是 `exit_plan_mode` 的工具抛出 `plan_blocked`（取代原来只针对 MCP 的检查）。`decide()` 先看规则的 deny，再看 `risk`（强制 Ask），然后才是规则 allow 和档位默认值。`ApprovalRequest` 增加 `choices`：默认四项；高风险为 `("once", "deny")`；计划为 `PLAN_CHOICES = ("execute", "accept_edits", "deny")`。`respond` 按该请求的 choices 校验。`exit_plan_mode` 在 `authorize` 中单独处理：检查是否在计划模式，入队审批；批准时由策略切换档位 |
| `tools/plan.py`（新增） | `ExitPlanMode`：`plan_only = True`，非只读（串行执行）；execute 只返回“已批准，按计划执行”的结果，details 带 plan |
| `tools/base.py` | `Tool.plan_only = False`。`definitions(plan=True)` 返回只读工具和 plan_only 工具；`plan=False` 排除 plan_only 工具 |
| `agent.py` | 环境补充每轮重新生成，让模式变化后的权限档位及时可见；工具定义本来就是每轮生成 |
| `prompts.py` | 任务模式模块和计划模式提醒改为：只读调查，完成后用 `exit_plan_mode` 提交计划，不用文字询问是否执行 |
| `tui.py` | 审批选项按 `request.choices` 动态生成；计划提交时先挂载计划 Markdown 条目，再显示工具行；工具名显示为 Plan；`/plan`、`/permissions` 说明更新 |
| `cli.py` | 无界面模式遇到 `exit_plan_mode` 时打印计划 |

## 取舍

- 计划模式不开放 `execute_command`，所以调查时不能运行 `git log` 之类的命令。R1 实现只读命令识别后可以放开。现在开放会让“只读”名不副实。
- 强制确认不能被 bypass 放行，这一点与 Claude Code 的 bypass 不同。用户已明确选择“其余强制确认”。
- 退出计划模式后的同一任务内，系统提示中的工具清单会变化，会有一次缓存未命中；与 Claude Code 相同。

## 验收

| 编号 | 方法 |
| --- | --- |
| AC1（F1） | 计划模式的定义只含 read_file/glob/grep/exit_plan_mode；在 bypassPermissions 进入的计划模式中，write/edit/命令/MCP 调用均为 `plan_blocked`，文件不变；即使有 `Write(**)` allow 规则也一样 |
| AC2（F2–F4） | 非计划模式调用 `exit_plan_mode` → `not_in_plan`；在计划模式中“批准并执行”后档位回到进入前（例如 acceptEdits），“批准并自动接受编辑”→ acceptEdits，“继续规划”→ 仍在计划模式且为 `permission_denied`；无界面 → `approval_required`。端到端：模型在一次任务内先提交计划、批准后写文件，写入成功 |
| AC3（F5） | TUI 测试：计划条目在对话中显示计划正文；审批面板选项为三项；选择后工具行完成 |
| AC4（F6） | 灾难级特征在 strict/default/acceptEdits/bypassPermissions、带 allow 规则和会话授权时仍为 `dangerous_command`；计划模式中所有命令先以 `plan_blocked` 拒绝 |
| AC5（F7） | 高风险命令：default、acceptEdits、bypassPermissions 加 `Bash(*)` allow 规则时，无界面均为 `approval_required`；交互时审批选项只有两项，“本次允许”后执行；之后再次调用仍需确认；deny 规则命中时为 `permission_denied`；`respond` 拒绝 `session`/`permanent` |
| AC6（F8） | 提示词测试：计划模式提醒提到 exit_plan_mode，不再提到“审批后写入” |
| 回归 | 全量 pytest、compileall、Windows 控制台烟测 |
