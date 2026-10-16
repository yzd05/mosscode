# 完善阶段 3b（权限补完）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`，shell 为 Git Bash。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线（阶段 3 结束） | 402 通过，1 跳过 |
| 全量回归 `pytest -q`（连续两次） | 480 通过，1 跳过 |
| `compileall -q src scripts tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 全部项目 passed（含 `inline_keyboard_permission_approval`、`plan_do_multi_iteration_and_usage`），控制台模式已恢复 |

跳过项与之前相同：当前 Windows 用户不能创建文件符号链接，相关的既有权限测试被跳过。新增 `tests/test_permissions_stage3b.py`，共 84 项。

## 验收对应

| 验收 | 证据（`tests/test_permissions_stage3b.py`） |
| --- | --- |
| AC1 | `test_read_only_commands`：23 条应放行的命令。`test_not_read_only_commands`：28 条应拒绝的命令，覆盖越界路径、`git -c`、`--output`、`tail -f`、`rg --pre`/`-z`、`find -delete/-exec`、管道、串联、重定向和变量。`test_read_only_command_runs_without_approval_but_deny_rules_win`：在 default 和 acceptEdits 下，只读命令直接执行，`git fetch` 返回 approval_required，deny 规则下返回 permission_denied。`test_plan_mode_runs_read_only_commands_only`：计划模式工具定义含 execute_command；`git log -3` 可执行，`python -m pytest` 和 `cat ../outside` 返回 plan_blocked |
| AC2 | `test_prefix_rule_matching`：allow 前缀按单词匹配，不放行 `pytestx` 和复合命令。deny `Bash(git push:*)` 拦截 `git status && git push origin main`，也拦截 `$(git push)` |
| AC3 | `test_command_prefix`：18 条推导用例。`test_prefix_choice_is_saved_and_reused_after_reload`：本地文件写入 `allow: [Bash(pytest:*)]`；重载后 `pytest tests/b.py -q` 免审批，`pytest; rm a` 仍需审批。`test_risky_and_underivable_commands_offer_no_prefix`：高风险命令只有两项；`python build.py` 没有 prefix 选项，`respond(..., "prefix")` 报错 |
| AC4 | `test_accept_edits_choice_switches_mode_for_later_edits_only`：选择后档位变为 acceptEdits，不新增会话规则；后续写入免审批，命令仍需审批。`test_shift_tab_cycles_modes_and_waits_while_running`：档位按 default → acceptEdits → plan → default → acceptEdits 循环，bypassPermissions 回到 default；运行中按键不切换，并提示“运行中不能切换权限档位” |
| AC5 | `test_edit_and_overwrite_previews_are_diffs`：编辑预览和覆盖写入预览都是 unified diff；新建文件显示“新建文件 new.py（1 行）”和 `+` 内容。`test_preview_falls_back_and_truncates`：原文无法匹配时退回“原文：…”；超过 16 行时有截断说明。`test_diff_lines_are_colored`：增、删、头部三种样式，非 diff 文本不着色。`test_tui_approval_shows_colored_diff_and_five_choices`：真实 TUI 面板显示 `-old`/`+new` 和 5 个选项，选择“本会话自动接受编辑”后完成写入并切换档位 |
| AC6 | `test_removed_modes_fail_with_replacement`：`normalize_mode` 和 `PermissionPolicy` 遇到 strict/permissive 报错，并给出替代档位。另有 `test_permission_modes.py::test_cli_rejects_removed_modes`（参数）和配置文件用例。`test_without_tools_plan_mode_is_unavailable`：没有注册表时进入计划模式抛出 ProviderError，`AgentLoop` 不再保存 `_mode` |

## 实施中发现并修复的问题

- **窄窗口审批面板溢出**：审批面板选项增至 5 项后，`_fit_chat` 仍按固定 4 项和旧的面板高度预留空间，导致溢出。现改为按 `request.choices` 计算固定高度，并预留完整上限。
- **运行中的切换提示被覆盖**：Shift+Tab 的“运行中不能切换权限档位”会在 0.2 秒内被状态计时器覆盖，用户可能看不到。新测试间歇失败时发现了这个问题。现在提示保留 2 秒，期间计时器仍刷新消息耗时，但不覆盖状态栏。
- **既有测试改写**：`git status` 现在属于只读命令，原先依赖它“需要审批”的测试改用 `git fetch`。计划模式工具集新增 execute_command（共 5 个）。strict/permissive 相关用例改为验证报错。并行审批测试改用项目外读取来触发两次审批。

## 未验证

- 只读白名单基于已知程序和选项，不保证覆盖所有平台变体，例如 PowerShell 别名的全部参数形式。白名单之外的命令按档位审批，属于保守方向。
- Shift+Tab 在不同终端中能否送达（部分终端会拦截 Shift+Tab）未逐一验证；Windows Terminal 下的 Textual pilot 测试已覆盖按键处理逻辑。
