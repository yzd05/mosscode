# 完善阶段 5（MCP）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`，Textual 8.2.8，MCP Python SDK（`.venv` 中的版本），shell 为 Git Bash。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线（阶段 4 结束） | 499 通过，1 跳过 |
| 全量回归 `pytest -q`（连续两次） | 514 通过，1 跳过（两次一致） |
| `compileall -q src scripts tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 退出码 0，全部项目 passed，含 `mcp_discovery_approval_call_cleanup`。该场景现在先断言输入框在 MCP 连接期间可用，再等待发现完成并执行 `/mcp`。终端输出中出现 `● console  stdio  已连接 · 4 个工具`；退出后 stdio 子进程已关闭（`mcp.closed` 存在） |

跳过项与之前相同：当前 Windows 用户不能创建文件符号链接。新增 `tests/test_mcp_stage5.py`，共 15 项。全部使用仓库内的确定性 stdio Server（`tests/fixtures/mcp_stdio_server.py`）和本机 `127.0.0.1` 上的临时 HTTP Server，没有访问外部网络，没有读取用户的 MCP、权限或模型配置，也没有使用真实凭据（测试中的 `fake-secret` 只用于验证不会被回显）。

## 验收对应

| 验收 | 证据（`tests/test_mcp_stage5.py`，另注明的除外） |
| --- | --- |
| AC1 | `test_tui_background_startup_registers_between_requests`：配置一个正常 Server 和一个握手挂起的 Server（`startup_timeout=60`）。`_startup_task` 完成后输入框可用，状态栏显示“MCP 连接中 0/2”。第一个任务的模型调用在 notes 注册后再等 0.2 秒，此时还没有“MCP notes”提示；任务结束后才出现“MCP notes 已连接 · 注册 4 个工具”。第一次请求不含 `mcp__notes__echo`，状态栏变为“MCP 连接中 1/2”；第二次请求包含该工具。退出时启动任务、后台发现任务和两个连接任务都已结束。`test_conversation_start_still_waits_by_default`：`Conversation.start()` 默认等待发现，返回“MCP notes 已连接 · 注册 4 个工具。”，第二次调用返回空。`test_mcp.py::test_headless_startup_does_not_wait_for_mcp_approval`：`-p` 路径行为不变 |
| AC2 | `test_mcp_command_lists_status_reasons_and_tools`：`/mcp` 输出“MCP Server（4）”，并显示 notes “已连接 · 4 个工具”、“失败：找不到命令 mosscode-missing-command-s5”、“失败：HTTP 503”、“失败：启动超时（0.5 秒）”，以及配置问题“! MCP bad 不可用”。输出中没有 `fake-secret`（URL 查询参数和 Authorization 头），也没有 `127.0.0.1`。`/mcp notes` 列出 `mcp__notes__echo  [只读]` 和 `mcp__notes__mixed`；`/mcp nope` 提示“未找到 MCP Server nope”。输入 `/mc` 后 Tab 补全为 `/mcp `，提示列出四个 Server 名称。`test_mcp_command_without_servers_points_to_configuration`：没有 MCP 时提示“未配置 MCP Server”和 mcp.yaml 位置。`test_tui.py` 的命令提示数量随 `/mcp` 加入改为 7 |
| AC3 | `test_server_and_tool_rules_parse_and_match`：`mcp__notes` 和 `mcp__notes__*` 匹配 notes 的工具，不匹配 other 的工具，也不匹配本地工具。`mcp__my-server` 匹配名称被哈希缩短的工具（暴露名不是 `mcp__my-server__echo`）。`mcp__notes__echo` 匹配任意参数，不匹配同 Server 的其他工具。`mcp__`、`mcp__bad_`、`mcp__a__`、`mcp__bad name` 抛出 PermissionConfigError。bypassPermissions 下 `mcp__notes` 的 deny 拒绝 notes，other 照常放行。`test_server_rule_loads_from_permission_file`：项目 `permissions.yaml` 中的 `mcp__notes__*`（deny）和 `mcp__web`（allow）按写法生效 |
| AC4 | `test_mcp_session_grant_covers_any_arguments_only_in_session`：真实 stdio 调用的审批选项为 once/session/permanent/deny；session 和 permanent 的文字都含“任意参数”。选择 session 后，同一工具换参数不再询问，其他工具（mixed）仍需审批，不写本地文件；重新加载的注册表再次需要审批。`test_mcp.py::test_mcp_permissions_plan_default_and_tool_level_persistent_grant`：选择 permanent 后，`permissions.local.yaml` 的 allow 为 `["mcp__notes__echo"]`，换参数直接执行，重新加载后仍然有效；手写的 exact 规则仍然固定参数 |
| AC5 | `test_trusted_read_only_hint`：开启 `trust_read_only_hint` 后，echo 的 `read_only` 为真，提示摘要带“（只读）”，default 档非交互直接执行。计划模式下工具定义中包含它，调用返回结果而不是 plan_blocked。相邻的两个受信任只读工具调用被 `partition_calls` 分到同一批并发执行。加 deny 规则后返回 permission_denied。`test_mcp.py::test_mcp_permissions_plan_default_and_tool_level_persistent_grant`：未开启时保持原行为（`read_only` 为假，不在只读定义中，只读调用返回 plan_blocked） |
| AC6 | `test_mcp_json_import`：未设置 `import_mcp_json` 时忽略 `.mcp.json`（只有用户和项目 yaml 中的 Server）。开启后：省略 type 的项为 stdio，args 中 `${MCP_S5_UNSET:-fallback}` 展开为 fallback、`${MCP_S5_SET}` 展开为环境变量值；http 项可用；sse 项产生一条“不受支持”的提示并被跳过；`.mcp.json` 覆盖用户 yaml 的同名项，项目 yaml 又覆盖 `.mcp.json`。`test_model_cannot_write_mcp_json`：bypassPermissions 下写 `.mcp.json` 返回 policy_protected，文件不存在 |
| AC7 | `test_per_server_timeouts`：notes 配置 `timeout: 0.3` 后仍能正常握手，`echo` 正常返回，`slow`（Server 端休眠 5 秒）返回 timeout；hang 配置 `startup_timeout: 0.4`，提示“MCP hang 失败：启动超时（0.4 秒）；可用 /mcp 查看。”，整体在 5 秒内完成。`test_out_of_range_options_become_notices`：`timeout` 为 0 或 601、`startup_timeout` 为 121、`trust_read_only_hint` 为字符串时，只跳过该 Server 并产生一条“MCP bad 不可用”提示，同文件中的合法 Server（`timeout: 5`）照常加载 |

## 实施中发现并修复的问题

- **短的工具超时会让启动失败**：最初把 `timeout` 设为整个 `ClientSession` 的读取超时，它同样作用于 `initialize` 和 `tools/list`。`timeout: 0.3` 可能在子进程启动阶段就失败。现在会话级超时取 max(timeout, startup_timeout)，工具调用单独传入 `timeout`（AC7 的测试覆盖）。
- **命令提示被截断**：加入 `/mcp` 后共 7 个命令，提示列表最多 6 行，最后的 `/exit` 被滚出视野。上限改为 7 行，小窗口仍按可用高度收缩。
- **退出时更新状态栏的竞态**：0.2 秒的计时器可能在屏幕卸载期间调用 `_status`，偶发 `NoMatches: #status`（全量回归中出现一次，单独运行无法复现）。后台 MCP 回调也可能在这时触发，所以 `_status` 在 `#status` 不存在时直接返回。
- **烟测原本靠时序通过**：`verify_inline_terminal.py` 的 MCP 场景之前隐含依赖“启动等待 MCP”。改为后台连接后，它只因 `/permissions` 往返耗时足够长才通过。现在显式等待发现完成。
- **规格修正**：设计表中的 `MCP_CHOICES` 改为实际做法：沿用四个选项，由 `choice_label` 按 `operation.server` 换文字。

## 未验证

- **真实第三方 Server**：只用了仓库内的 fixture 和本机 HTTP Server，没有连接社区的 MCP Server，也没有用 npx/uvx 启动。Windows 上 `npx` 等 `.cmd` 包装程序能否直接作为 command 执行，取决于 SDK 的进程启动方式，未验证。
- **`.mcp.json` 与 Claude Code 的完全兼容**：只覆盖 stdio/http 两种类型与常见字段；`sse`、`ws`、OAuth 等明确不支持。
- **真实终端中的提示时机**：AC1 的“任务结束后才显示”由 pilot 测试验证；真实控制台烟测只覆盖空闲时的连接与 `/mcp`。
