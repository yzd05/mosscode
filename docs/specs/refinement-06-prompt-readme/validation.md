# 完善阶段 6（系统提示与 README）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`，shell 为 Git Bash。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线（阶段 5 结束） | 514 通过，1 跳过 |
| 全量回归 `pytest -q`（连续两次） | 526 通过，1 跳过（两次一致） |
| `compileall -q src scripts tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 退出码 0，全部项目 passed |

新增 `tests/test_prompts_stage6.py`（4 项）和 `tests/test_docs.py`（8 项）。跳过项与之前相同（当前 Windows 用户不能创建文件符号链接）。

## 提示长度

用 `create_default_registry` 的默认工具集生成，单位为字符：

| 工具集 | 系统提示（前 → 后） | 工具描述合计（不变） | 每次请求合计（前 → 后） |
| --- | --- | --- | --- |
| 自动模式（6 个工具） | 2,856 → 1,429（−50.0%） | 1,857 | 4,713 → 3,286（−30.3%） |
| 计划模式（5 个工具） | 2,629 → 1,407（−46.5%） | 1,559 | 4,188 → 2,966（−29.2%） |

七个固定模块本身从 1,539 字符减到 1,245（−19.1%），其中已经加入了 P4 的两条新规则。主要的节省来自去掉重复：工具规则原来在系统提示和 API 工具描述中各发送一遍，现在只在工具描述中发送一遍。

## 验收对应

| 验收 | 证据 |
| --- | --- |
| AC1 | `test_prompts_stage6.py::test_parallel_read_only_calls_and_brief_replies_in_every_tool_set`：自动模式和计划模式的系统提示都包含“互不依赖的只读调用…同一次回复中一起发出…并发执行”、“有副作用时按顺序调用”，以及“简洁直接”“不复述计划”“不逐条叙述工具调用” |
| AC2 | `test_prompt_is_at_least_30_percent_shorter_and_rules_are_sent_once`：系统提示 ≤ 2,856 × 0.7，不含“使用指导”；每条 `prompt_guidelines` 在系统提示中出现 0 次，在其工具描述中恰好出现 1 次。`test_key_conventions_survive_in_prompt_or_tool_descriptions`：在“系统提示 + 工具描述”中检查 16 个关键短语，覆盖数据不是指令、凭据、文字不能代替授权、不绕过权限、调用意图不等于执行成功、标为未验证、先读后改、按原因调整且不原样重复、计划模式与 exit_plan_mode、截断、Markdown、不用 Emoji、不复述系统提示，计划模式提醒仍包含 exit_plan_mode 和只读命令。`test_prompt_cache.py`：真实 SDK 请求负载中，规则不在 system/instructions 中，而在 `tools` 中 |
| AC3 | `test_stable_prompt_has_no_session_specific_values`：两次构建逐字节一致，不含工作目录。原有的 `test_prompts.py`、`test_prompt_cache.py`、`test_model_side.py`、`test_plan_commands.py` 等全部通过。改动的断言只有三处，都是把“规则在系统提示中”改为“规则在工具描述中”：`test_tool_contributions_follow_enabled_definitions_and_api_guidance`、改名后的 `test_custom_tool_contributions_list_without_hardcoded_builtin_names`、`test_actual_sdk_cache_stats_and_separate_system_instructions` |
| AC4 | `test_docs.py::test_readme_has_install_configure_use_architecture`：二级标题恰好为“安装、配置、使用、架构”。README 从 325 行减到 160 行 |
| AC5 | `test_docs.py::test_relative_links_resolve`：README 和 6 个参考文档中的相对链接全部指向存在的文件和锚点。迁移脚本（`.test-artifacts/s6_split.py`）按行号把原 README 的 204 行原样移入参考文档，断言没有行被移动两次。检查脚本（`.test-artifacts/s6_check_docs.py`）对原 README 中的 37 个关键词（`stale_read`、`MOSSCODE_COMMAND`、`retry-after`、`import_mcp_json`、`Bash(pytest:*)`、`dangerous_command`、`stream_usage`、`include_encrypted_reasoning` 等）检查后，全部能在新 README 或参考文档中找到 |

## 实施中的发现与处理

- **工具规则重复发送**：这是 P5 最大的一处冗余，前文已说明。规则保留在工具描述中，没有保留在系统提示中，取舍见规格。
- **README 迁移时遗漏的事实**：未原样迁移的行逐一核对后，发现 `auth_type: bearer` 漏掉了，已补进 [providers.md](../../reference/providers.md)。
- **README 新写内容的核对**：配置表起初把 `max_output_tokens` 列为顶层选项，核对 `config.py` 后发现它是 Provider 级字段，已移入 Provider 示例。退出码说明也按 `cli.py` 改为准确描述：json 格式下 `is_error` 为真时返回 1，配置和连接错误在任何格式下都返回 1。
- **过时的章节说明**：参考文档中的“本章不实现…”改为“目前不实现…”。原 README 中“此轮只增加固定命令…第十章”、“当前工作区已经建立 .venv”等过时句子已删除。

## 未验证

- **真实模型的行为变化**：模型是否更多地在一次回复中发出并行只读调用、回复是否更简洁，需要用真实 API 对比，本阶段没有运行。方法见 [第五章人工评估](../chapter-05-prompts/manual-evaluation.md)。
- **规则只在工具描述中的效果**：Anthropic、OpenAI 等主流模型会读取工具描述，但个别兼容服务可能会截断过长的描述，未验证。
- **[第五章默认提示示例](../chapter-05-prompts/default-prompt.md) 未更新**：它是当时版本的历史记录，参考文档中已经注明。
