# 完善阶段 4（交互）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`，Textual 8.2.8，shell 为 Git Bash。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线（阶段 3b 结束） | 480 通过，1 跳过 |
| 全量回归 `pytest -q`（连续两次） | 499 通过，1 跳过（两次一致） |
| `compileall -q src scripts tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 退出码 0，全部项目 passed（含 `command_completion`、`inline_model_panel`、`plan_then_execute_multi_iteration_and_usage`、`picker_dialogue_model_exit`）；没有切换备用屏幕或清全屏，控制台模式已恢复 |
| 预览脚本 `scripts/preview_tui.py`、`scripts/preview_agent.py` | 退出码 0；新增 `docs/ui/previews/model-menu.*`、`permissions-completion.*` |

跳过项与之前相同：当前 Windows 用户不能创建文件符号链接，相关的既有权限测试被跳过。新增 `tests/test_interaction_stage4.py`，共 19 项。所有测试都使用模拟模型或捕获请求参数的假客户端，没有读取 API 配置或发送凭据。

## 验收对应

| 验收 | 证据（`tests/test_interaction_stage4.py`，另注明的除外） |
| --- | --- |
| AC1 | `test_ac1_multiline_keys_backslash_paste_and_history`：Shift+Enter、Ctrl+J、行尾 `\` 都插入换行，Enter 发送后模型收到 `a\nb\nc\nd`。经 App 送达的多行粘贴原样保留，不会触发发送。↑ 在首行时依次调出更早的记录；不在末行时 ↓ 只在文本内移动；在末行时 ↓ 返回较新的记录，越过最新一条后恢复草稿“草稿”。命令提示打开时 ↓ 选择提示，不改变输入 |
| AC2 | `test_ac2_queued_messages_join_at_the_next_iteration_boundary`：第 1 轮生成期间排入两条消息，输入框可用、清空，队列区显示“↳ 排队：改用 b”，状态栏显示“已排队 2 条”。第 2 轮请求末尾是一条 user 消息 `改用 b\n\n再补充\n一行`，对话中有三条 You，结束后队列区和计数消失。`test_ac2_queue_during_final_reply_becomes_the_next_task`：在最终回复期间排队，任务结束后自动作为下一个任务发送，历史为 user/assistant/user。`test_ac2_cancel_returns_queue_to_input_and_commands_are_not_queued`：运行中提交 `/help`、`/clear` 不入队，提示“运行中只能排队消息”，历史未清空；Esc 取消后，排队内容与草稿合并放回输入框，不再请求模型 |
| AC3 | `test_ac3_diff_rows_number_color_separate_hunks_and_truncate`：40 行文件中两处修改显示为 `    3 - line 3` / `    3 + line three` 等带行号的行，两个修改块之间有 `⋯`，不显示 `---`/`@@` 头部；删除行为 ERROR_COLOR，新增行为 TOOL_SUCCESS_COLOR；60 行变更只显示 20 行，并注明“… 还有 40 行 diff 未显示”。`test_ac3_tool_rows_show_diff_for_edit_and_content_for_new_file`：真实工具行中，edit_file 显示 `    1   a = 1`、`    2 - b = 2`、`    2 + b = 3`；新建 10 行文件显示前 8 行，并注明“… 还有 2 行未显示” |
| AC4 | `test_ac4_status_shows_mode_badge_model_hint_and_tokens`：四个档位的状态栏都以“标记 (Shift+Tab 切换) · model-a”开头；标记的样式区间正好覆盖标记文字，颜色依次为默认前景、绿色、琥珀色、红色；宽度 60 时省略 Shift+Tab 提示但保留模型名；请求后显示 Token 统计 |
| AC5 | `test_ac5_clear_starts_fresh_context_and_keeps_mode`：计划模式下第 1 次请求带 system-reminder，第 2 次不带；`/clear` 后历史为空、累计 Token 归零、状态栏不再显示 Token，档位仍为 plan；下一次请求只含“问题三”，并重新带上计划模式提醒。`test_ac5_clear_is_refused_while_the_loop_is_busy`：运行中 `clear()` 和 `set_provider()` 抛出 ProviderError。`test_ac5_model_switch_by_argument_and_panel_keeps_history`：`/model missing` 列出“可用：test、other”，不创建客户端；`/model other` 关闭旧 Provider，状态栏显示 model-b，新 Provider 收到完整历史 hello/partial/again；`/model` 面板获得焦点，↑ + Enter 切回 test，第二个 Provider 被关闭，历史保留 4 条。`test_tui.py::test_removed_theme_and_do_commands_are_unknown_and_preserve_conversation`：`/theme`、`/do` 提示未知命令，历史不变 |
| AC6 | `test_ac6_set_provider_strips_native_blocks_but_keeps_calls_and_results`：切换后，带 Responses 原生数据的助手消息变为通用形式，工具调用和结果保留。`test_ac6_anthropic_rebuilds_tool_use_and_merges_same_role_turns`：角色序列为 user/assistant/user/assistant/user，没有相邻同角色；tool_use 从通用字段重建（`input` 为解析后的对象）；工具结果与随后的排队文本合并为同一条 user；只有工具调用的助手消息不产生空文本块。`test_ac6_responses_and_chat_pair_every_call_with_its_result`：Responses 的 function_call 与 function_call_output、Chat 的 tool_calls 与 tool 消息，调用 ID 一一对应（c1、c2） |
| AC7 | `test_ac7_short_flag_text_output_is_unchanged`：`-p` 的文本输出仍为 `partial\n`。`test_ac7_json_outputs_one_result_object`：stdout 是单个 JSON 对象，subtype=completed、num_turns=1、model、permission_mode、usage.requests、整数 duration_ms 都存在。`test_ac7_stream_json_lines_include_tools_in_order`：每行都是合法 JSON，事件顺序为 system → assistant（tool_calls 参数已解析，stop_reason=tool_use）→ tool_result（读取 pyproject.toml 的真实结果）→ assistant → result（num_turns=2，累计输入 Token 12）。`test_ac7_errors_report_is_error_and_exit_one`：json 与 stream-json 失败时最后一行 is_error=true、subtype=error，退出码 1，Provider 已关闭。`test_ac7_output_format_requires_prompt`：不带 `-p` 使用 `--output-format` 时参数错误，退出码 2 |

## 实施中发现并修复的问题

- **不带原生数据的工具调用丢失**：Anthropic 和 Responses 在助手消息没有原生数据时只发送文本，工具调用丢失，导致下一条工具结果无法配对。切换模型后一定会触发。现在从通用字段重建 tool_use / function_call（AC6）。
- **Anthropic 相邻同角色消息**：排队消息紧跟工具结果，会形成两条连续的 user。现在组装请求时合并。
- **Textual 属性名冲突**：`TextArea.history` 用于撤销，`App._running` 是内部状态。输入历史和“任务运行中”分别改名为 `input_history` 和 `_task_running`。
- **窄窗口模型面板溢出**：44 列下选项文字折行，面板比预留高度多一行，状态栏被挤出窗口。现在选项不折行（超长省略），列表高度固定为提供商数量（最多 8）。
- **既有测试与脚本改写**：输入控件从 `Input` 改为 `PromptArea`。“生成期间输入框禁用”的断言改为“保持可用”。状态栏断言改用档位标记。`/theme`、`/do` 场景改为 `/model`、`/permissions` 加普通消息。`verify_inline_terminal.py` 的报告项同步改名。

## 未验证

- **Shift+Enter**：Shift+Enter 能否送达取决于终端，部分终端发送的序列与 Enter 相同。测试通过 pilot 发送按键，未在各终端中逐一验证；不能区分时可用 Ctrl+J 或行尾 `\`。
- **粘贴**：真实终端的 bracketed paste 未手动验证。测试按驱动的路径把 Paste 事件发给 App。
- **真实服务**：跨协议切换只验证了生成的请求参数（假客户端捕获），没有用真实服务往返。不同服务对重建后历史的接受程度可能不同，例如某些兼容端点要求 thinking 块。
