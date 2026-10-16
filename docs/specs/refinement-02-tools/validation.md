# 完善阶段 2（工具）验证记录

日期：2026-10-15。环境：Windows 11、Python 3.12、项目 `.venv`；Git Bash 为 Scoop 安装的 Git 2.45.2；本机未安装 ripgrep。规格见 [spec.md](spec.md)。

## 结果

| 检查 | 结果 |
| --- | --- |
| 改动前基线（阶段 1 结束） | 318 通过，1 跳过 |
| 全量回归 `pytest -q`（连续两次） | 335 通过，1 跳过 |
| `compileall -q src tests` | 通过 |
| Windows 真实控制台烟测 `scripts/verify_inline_terminal.py` | 全部项目 passed，控制台模式已恢复 |

跳过项与之前相同：当前 Windows 用户不能创建文件符号链接的既有权限测试。新增 `tests/test_tools_stage2.py`（17 项，本机无跳过）。

改写的既有测试：

- 读取输出断言从 `2: 二` 改为 `     2\t二`。
- 写入结果从“文件已写入”改为“已创建 …”。
- 命令超时上限从 121 改为 601。
- `grep` 内容断言加上 `output_mode="content"`。
- `python_command` 按实际 shell 生成 bash 或 PowerShell 语法。
- 权限测试中，项目外读取由 `path_blocked` 改为 `approval_required`；junction 和锚点测试改用 default 档位，写入仍断言 `path_blocked`。
- 永久授权测试在覆盖已有文件前先读取。

## 实现中发现并修复的问题

1. **Git Bash 损坏命令中的 `\\`**。MSYS 运行时按 Cygwin 规则重新解析 bash.exe 的 Windows 命令行，引号内的 `\\` 被合并为 `\`；Python 生成的命令行不按这一规则转义。结果是含 `\\` 的命令（Windows 路径的 Python 字符串、正则等）被悄悄改写。已用探针确认：bash 内置 `printf` 收到的参数已经被改写。修复方法是在 Windows 上把命令文本放进环境变量 `MOSSCODE_COMMAND`（环境变量原样传递），以 `-c 'eval "$MOSSCODE_COMMAND"'` 执行。回归测试：`test_bash_syntax_exit_code_utf8_and_backslashes`。
2. **重试提示被状态刷新覆盖**（阶段 1 遗留）。0.2 秒一次的状态定时刷新会把“服务过载 · 2 秒后重试”改回“连接中”，原测试只在断言先于刷新时通过，全量运行中偶发失败。现在重试提示保持到下一个事件；测试改为等待 0.5 秒后断言。

## 验收对照

| 编号 | 证据 |
| --- | --- |
| AC1 | `test_read_line_numbers_defaults_and_notes`（格式、空文件、offset 越界、超长行、默认 2000 行与下一页提示）；`test_tools.py::test_read_byte_budget_next_page_does_not_skip_unshown_lines` |
| AC2 | `test_outside_read_asks_and_writes_stay_blocked`：<br>- 无界面为 `approval_required`；同意后读到内容；<br>- 预览标注“项目目录之外”；<br>- 项目外写入和编辑为 `path_blocked`；<br>- bypass 直接读取；<br>- `Read(**)` 不放行项目外。<br>`test_outside_read_permanent_rule_reloads`：<br>- 永久授权保存绝对路径；重载后可读取；<br>- 读取类规则允许绝对路径；写入规则拒绝绝对路径；拒绝 `..`。 |
| AC3 | `test_replace_all_hint_and_crlf_multiline`：多处匹配报错并提示 replace_all；replace_all 替换 3 处并报告第 1–3 行；CRLF 文件以 LF 多行原文修改后仍为 CRLF |
| AC4 | `test_read_before_modify_and_stale_detection`：edit 和 write 未读时报 `not_read`；外部修改后报 `stale_read`，文件未变；重读后可编辑；连续编辑无需重读；新建文件无需读取 |
| AC5 | `test_edit_summary_keeps_diff_local`：输出为摘要；`details["diff"]` 含 `-二`/`+新`；`model_text()` 不含 diff；覆盖写入报告行数变化 |
| AC6 | `test_command_keeps_head_and_tail`（同时保留开头和结尾，标注省略字节数，小于 40 KB）；`test_command_timeout_bounds`（默认 120，600 合法，601 不合法） |
| AC7 | `test_windows_auto_prefers_git_bash`（auto 选中 Scoop 的 Git Bash）；`test_bash_syntax_exit_code_utf8_and_backslashes`（`$((1+2))`、退出码 4、中文、`\\` 原样）；`test_configured_powershell_runs_commands`；`test_explicit_shell_choices`（路径不存在报错；模拟 SystemRoot 下的 bash 被判定为 WSL 并拒绝）；`test_shell_label_reaches_prompt_and_tool_description`；`test_config_shell_key` |
| AC8 | `test_fallback_respects_gitignore`（`build/`、`*.log`、`!keep.log`、子目录 `.gitignore`；显式搜索被忽略的目录仍能找到）；`test_grep_output_modes`（content、ignore_case + context、count、head_limit 截断提示）；`test_glob_and_grep_list_newest_first` |

## 未验证

- **ripgrep 路径**：本机没有 rg，所有搜索测试都走 Python 回退实现。以下 rg 参数按 ripgrep 文档实现，没有实际运行，安装 rg 后应补跑 `tests/test_tools.py` 和 `tests/test_tools_stage2.py`：
  - `--hidden --no-require-git`（不用 `--no-ignore`）；
  - `--count --with-filename --null`；
  - `--json -C n` 的 context 事件；
  - 对显式路径的忽略规则处理。
- **`.gitignore` 子集**：回退实现只支持常见语法（注释、`!`、锚定、目录模式、`**`），不处理 `.git/info/exclude`、全局 excludes 和转义空格等边角规则。
- **非 Windows 平台**：bash/sh 选择逻辑没有在 Linux/macOS 实际运行。
- **真实模型**：没有调用真实 API 观察模型是否遵守“先读后改”和新的 grep 参数，只验证了工具侧行为。

## 可复核命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-artifacts\s2-final -p no:cacheprovider
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py
```
