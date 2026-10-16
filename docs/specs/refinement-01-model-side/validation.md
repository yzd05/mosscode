# 完善阶段 1（模型侧）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线 | 302 通过，1 跳过 |
| 全量回归 `pytest -q` | 318 通过，1 跳过 |
| `compileall -q src tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 全部项目 passed，控制台模式已恢复 |
| CLI 配置查找烟测 | 项目根 `config.yaml` 被找到；空目录报错并列出三个查找路径，退出码 1 |

跳过项与第七章相同：当前 Windows 用户无法创建文件符号链接的既有权限测试。

删除的测试：`test_reminder_interval_validation`（`reminder_interval` 参数已移除）。改写的测试：提醒节奏（改为只在模式变化时提醒）、缓存块布局、HTTP 错误（429/500 现在重试）、输出上限（现在停止并保留正文），以及三处 `serialize()` 断言。

## 验收对照

| 编号 | 证据 |
| --- | --- |
| AC1 | `test_model_side.py::test_command_failure_is_plain_text_with_stdout_once`、`test_invalid_arguments_list_fields_for_the_model`；`test_tool_providers.py` 中 Anthropic 回放直接以 `1: 你好` 开头；`test_tools.py` 中各失败码以 `错误 [code]` 开头 |
| AC2 | `test_prompt_cache.py::test_actual_sdk_cache_stats_and_separate_system_instructions`（Anthropic：system 只有稳定块和环境块；最后一个历史块带 `cache_control`；`<system-reminder>` 在其后且未标记；OpenAI 两种协议的提醒在末尾）；`test_anthropic_explicit_cache_capability_switch` 覆盖消息断点的开关 |
| AC3 | `test_prompts.py::test_reminder_only_on_mode_change_stable_prefix_and_clean_history`、`test_leaving_plan_mode_announces_once`、`test_mode_reminder_is_change_only` |
| AC4 | `test_providers.py::test_output_budget_exhaustion_stops_with_text_preserved`（三种协议）、`test_responses_incomplete_for_other_reasons_stays_an_error`；`test_model_side.py::test_truncated_tool_call_is_not_executed_and_loop_continues`；`test_agent_tui.py::test_retry_status_and_max_tokens_notice` |
| AC5 | `test_providers.py::test_http_error_retry_policy_no_secret_leak`（401/400 请求 1 次；429/500 请求 3 次，Retrying 1/2、2/2）；`test_model_side.py` 中的 529 后成功（真实 SDK）、断连重试、首个事件后不重试、`max_retries: 0`、等待期间取消；TUI 状态行显示重试 |
| AC6 | `test_config_lookup_order_and_missing_error`、`test_limits_defaults_and_validation`、`test_configured_iteration_limit_applies`；CLI 烟测 |
| AC7 | `test_instruction_order_priority_and_truncation`（顺序、AGENTS.md 优先、32 KiB 截断不残留半个字符、非 UTF-8 跳过）、`test_instructions_reach_cached_prefix_with_notices`（在稳定提示末尾、带路径、自定义提示同样附加、只提示一次）、`test_without_context_root_nothing_is_loaded` |
| AC8 | `test_git_snapshot_repo_and_non_repo`（临时仓库、分支、`??` 状态、行数截断；用 `GIT_CEILING_DIRECTORIES` 隔离）、`test_git_failure_does_not_interrupt`、`test_git_fact_reaches_environment` |

## 未验证

- 真实服务：没有调用真实 API。缓存断点的实际命中率、真实 529/429 的 `retry-after` 头和各兼容端点的 `length`/`incomplete_details` 字段，都只用 SDK 模拟流验证过。
- 用户本地 `config.yaml` 仍显式写着 `max_output_tokens: 4096`，没有修改；新的默认值 16384 只对未填写该字段的配置生效。

## 可复核命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-artifacts\s1-final -p no:cacheprovider
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe scripts\verify_inline_terminal.py
.\.venv\Scripts\python.exe -m mosscode --list-providers
```
