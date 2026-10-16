# 完善阶段 3（计划模式与命令分级）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`，shell 为 Git Bash。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线（阶段 2 结束） | 335 通过，1 跳过 |
| 全量回归 `pytest -q`（连续两次） | 402 通过，1 跳过 |
| `compileall -q src scripts tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 全部项目 passed（含 `plan_do_multi_iteration_and_usage`、`inline_keyboard_permission_approval`），控制台模式已恢复 |

跳过项与之前相同：当前 Windows 用户不能创建文件符号链接的既有权限测试。新增 `tests/test_plan_commands.py`（57 项）。

改写的既有测试：

- **计划模式工具集**：从 6 个工具改为 read_file/glob/grep/exit_plan_mode。涉及：
  - `test_agent.py`
  - `test_agent_tui.py`
  - `test_prompts.py`
  - `test_prompt_cache.py`
  - `test_tool_providers.py`
  - `scripts/preview_agent.py`
- **计划模式中的写入和命令**：由 `approval_required` 改为 `plan_blocked`。涉及 `test_agent.py`，以及 `test_permission_modes.py` 的矩阵测试。
- **计划写入审批测试**：`test_permission_modes.py` 原来测试“计划模式中审批写入”，改为 `test_plan_is_approved_in_tui_then_writes_in_same_task`，验证新流程。
- **危险命令测试**：`test_permissions.py` 的 `test_known_danger_signatures` 拆成两项：
  - `test_catastrophic_signatures`；
  - `test_risky_signatures_are_not_hard_denied`。
- **硬限制测试**：`test_hard_guards_cannot_be_granted` 把高风险命令换成灾难级命令；在计划模式下，期望的错误码统一为 `plan_blocked`。
- **提示词断言**：计划模式提醒改为提到 `exit_plan_mode`，不再提到 `/do`。

## 实现中发现并修复的问题

1. **计划正文在审批面板中显示不全**。审批详情区最多 7 行，长计划会被截断。修复后，提交计划时先在对话中挂载完整的 Markdown 计划条目（`PlanEntry`），条目随即进入原生滚动历史；审批面板只提示“完整计划显示在上方对话中”。
2. **批准计划后环境信息过期**。环境补充原来每个任务只生成一次，批准后同一任务的下一轮请求仍显示 `permission_mode: plan`。现在每轮重新生成。回归测试：`test_plan_approval_switches_tools_within_one_task` 断言第三次请求的环境为 acceptEdits。
3. **审批期间模式被切换**。如果用户在审批计划时用 `/permissions` 切换了档位，批准结果不应再改动档位。现在这种情况返回 `not_in_plan`，并保留用户新选的档位。测试：`test_plan_approval_is_void_if_mode_changed_meanwhile`。

## 验收对照

| 编号 | 证据 |
| --- | --- |
| AC1 | `test_plan_definitions_are_read_only_plus_exit_plan_mode`：<br>- 计划模式定义恰为 4 个工具；<br>- 非计划定义不含 exit_plan_mode，MCP 工具仍在。<br>`test_plan_blocks_non_read_tools_from_bypass_and_with_allow_rules`：<br>- 从 bypassPermissions 进入计划模式；<br>- 带有 `Write(**)`、`Edit(**)`、`Bash(*)` allow 规则；<br>- write、edit、命令、MCP 均为 `plan_blocked`，未执行、未入审批队列，文件不变；<br>- read_file 正常。<br>`test_permission_modes.py` 矩阵：plan 行全部为 `plan_blocked`。 |
| AC2 | - `test_exit_plan_mode_outside_plan_is_not_in_plan`；<br>- `test_exit_plan_mode_headless_requires_approval_and_stays_in_plan`；<br>- `test_plan_choices_set_the_next_mode`：从 acceptEdits/default/bypass 批准执行后，各自恢复原档位；选“自动接受编辑”→ acceptEdits；选“继续规划”→ 仍为 plan，返回 `permission_denied`；有 `Bash(*)` 规则时仍然询问。<br>端到端：<br>- `test_plan_approval_switches_tools_within_one_task`：一次任务内，先写入被 `plan_blocked`，再提交计划，批准后写入成功；前两次请求是计划工具集，之后是全部工具；<br>- `test_headless_cli_prints_plan`：无界面输出包含“计划：”和正文，仍为计划模式。 |
| AC3 | `test_plan_is_approved_in_tui_then_writes_in_same_task[True/False]`：<br>- `PlanEntry` 渲染出标题和计划各行；<br>- 审批选项为 3 项，choices 为 execute/accept_edits/deny；<br>- Enter 批准后，同一任务写入成功，档位恢复 acceptEdits；<br>- Esc 后仍在计划模式，`/do` 手动退出。 |
| AC4 | `test_catastrophic_commands_are_hard_denied_in_every_mode`：<br>- 5 条灾难级命令 × strict/default/acceptEdits/bypassPermissions；<br>- 同时有 `Bash(*)` 和会话精确授权；<br>- 均为 `dangerous_command`，未执行、未询问。<br>`test_catastrophic_signatures`：13 条特征。 |
| AC5 | - `test_risky_commands_always_ask_headless`：5 条命令 × default/acceptEdits/bypass，带 `Bash(*)` 和会话精确授权，均为 `approval_required`，未执行。<br>- `test_risky_command_offers_once_or_deny_and_asks_every_time`：choices 为 once/deny，预览说明风险；选择本次允许后执行；再次调用仍询问；不写入会话规则。<br>- `test_respond_rejects_choices_the_request_does_not_offer`：session、permanent、execute 均抛出 ValueError，不生成 `.mosscode`。<br>- `test_deny_rule_still_wins_over_risky_confirmation`。<br>- `test_ordinary_commands_keep_four_choices`。<br>- `test_risky_signatures_are_not_hard_denied`：12 条特征，各自对应风险原因。 |
| AC6 | - `test_plan_reminder_points_to_exit_plan_mode`；<br>- `test_prompts.py::test_mode_reminder_is_change_only`；<br>- 计划模式的稳定提示列出 `` `exit_plan_mode` ``、不列出 `` `write_file` ``（`test_mode_switch_changes_only_enabled_contributions_and_custom_override_survives`、`test_prompt_cache.py`）。 |

## 未验证

- **真实模型**：没有调用真实 API，观察模型在计划模式中是否主动调用 `exit_plan_mode`、被 `plan_blocked` 后能否调整。只验证了工具和策略一侧的行为。
- **命令特征的覆盖面**：正则只覆盖已知写法，没有覆盖别名、变量拼接、脚本文件间接执行等。例如 `$cmd = "rm"; & $cmd -rf /` 不会被识别为灾难级。这是既有限制，README 已说明。
- **计划正文在真实终端中的长计划滚动**：TUI 测试和烟测确认条目已挂载并进入记录；没有在真实终端中人工翻看数百行的计划。

## 可复核命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-artifacts\s3-final -p no:cacheprovider
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py
```
