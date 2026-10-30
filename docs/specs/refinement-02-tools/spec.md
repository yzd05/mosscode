# 完善阶段 2：工具契约（T1–T7）

来源：[差距评审](../../research/claude-code-pi-gap-review.md) 第三节。依赖阶段 1 的纯文本工具结果（[refinement-01](../refinement-01-model-side/spec.md)）。已确认的取舍：Windows 检测到 Git Bash 时优先用 bash，否则用 PowerShell；项目外的路径读取改为询问，写入保持硬拒绝。

## 现状

- `read_file` 输出 `N: 原文`，默认 200 行；项目外路径一律 `path_blocked`。
- `edit_file` 只能替换唯一匹配，没有 `replace_all`；`edit_file`/`write_file` 不检查模型是否读过文件、读后文件是否被改过；CRLF 文件里多行 `old_text`（模型看到的是 LF）无法匹配。
- `execute_command` 默认 30 秒、最大 120 秒；每个流只保留前 32 KiB，测试和编译器的失败摘要在末尾，正好被截掉；Windows 固定用 PowerShell。
- `grep`/`glob` 用 `--no-ignore`，会搜到 `.test-artifacts` 等被忽略的目录；`grep` 只有一种输出（匹配行），没有大小写、上下文、条数控制。本机没有安装 ripgrep，实际走 Python 回退实现，回退实现也必须遵守 `.gitignore`。

## 需求

| # | 行为 |
|---|------|
| F1 | `read_file` 每行输出 `行号（右对齐 6 位）+ Tab + 原文`；默认读取 2000 行；单行超过 2000 字符时截断并标注；字节预算不变，截断时提示下一页 offset；空文件和 offset 超出末尾给出明确说明 |
| F2 | 读取项目外的路径（`read_file`、`glob`、`grep`）不再硬拒绝，改为需要确认：default/acceptEdits/plan 档位询问，`bypassPermissions` 放行，无界面模式拒绝为 `approval_required`；可用会话或永久授权（规则可写绝对路径，仅限读取类工具）。写入和编辑项目外路径仍硬拒绝 |
| F3 | `edit_file` 增加 `replace_all`（默认 false）：false 时要求恰好一处（重叠匹配也计入）；true 时替换全部非重叠匹配，至少一处。`old_text` 与 `new_text` 相同时报错 |
| F4 | 先读再改：会话内记录每个文件最后一次被 `read_file` 读取（或被本工具写入）时的修改时间和大小。`edit_file` 和覆盖已有文件的 `write_file` 在文件未读过时返回 `not_read`，读后被外部改动时返回 `stale_read`，均不修改文件。新建文件不受限制。成功写入后更新记录，连续编辑无需重读 |
| F5 | `edit_file`/`write_file` 成功后返回简短摘要（替换处数、所在行范围，或“已创建/已覆盖”及行数变化）；统一 diff 放在 `details["diff"]`，供阶段 3 的审批和阶段 4 的界面使用，不发送给模型 |
| F6 | 整体为 CRLF 换行的文件，`edit_file` 按 LF 匹配 `old_text`，写回时保持 CRLF。混合换行的文件仍按原文精确匹配 |
| F7 | `execute_command` 默认超时 120 秒，最大 600 秒；每个流超过上限时保留头部 8 KiB 和尾部 22 KiB，中间标注省略的字节数 |
| F8 | Shell 选择：配置顶层 `shell: auto`（默认）、`bash`、`powershell` 或 bash 可执行文件路径。Windows 的 auto 先找 Git Bash（由 `git.exe` 位置推导，再查常见安装目录；排除 `System32` 下的 WSL bash），找不到时用 PowerShell；其他系统 auto 优先 `bash`，否则 `sh`。显式指定但找不到时启动报错。bash 使用 `--noprofile --norc -c` 运行。实现中发现：MSYS 会按 Cygwin 规则重新解析 bash.exe 的 Windows 命令行，把引号内的 `\\` 合并为 `\`，因此 Windows 上命令文本经环境变量 `MOSSCODE_COMMAND` 传入，以 `-c 'eval "$MOSSCODE_COMMAND"'` 执行 |
| F9 | 实际 shell 体现在工具描述、使用指导、环境补充（`shell` 字段）和界面工具名（`Bash`/`PowerShell`）中 |
| F10 | `grep`/`glob` 遵守 `.gitignore`（rg 去掉 `--no-ignore` 并加 `--no-require-git`；回退实现解析项目根到搜索目录及其下各级 `.gitignore`，支持 `!`、`/` 锚定、目录模式和 `**`）。仍跳过 `.git`、`.venv`、`node_modules` 等目录；显式指定的搜索目录本身不因被忽略而跳过 |
| F11 | `grep` 增加 `output_mode`：`files_with_matches`（默认，按修改时间倒序）、`content`（`路径:行号: 匹配行`，上下文行为 `路径-行号- 原文`）、`count`（`路径:次数`）；增加 `ignore_case`、`context`（0–10 行）、`head_limit`（默认 200） |
| F12 | `glob` 结果按修改时间倒序（最近修改的在前），最多 200 项 |

## 设计

| 模块 | 改动 |
|------|------|
| `tools/shell.py`（新增） | `Shell(kind, path, label)`；`resolve_shell(preference)` 实现 F8 的检测；`ShellNotFound(ValueError)` |
| `tools/process.py` | `run_process(..., tail=0)` 头尾保留；`ExecuteCommand(root, shell)` 按 shell 构造参数、描述和指导；超时参数改为 120/600 |
| `tools/files.py` | `FileState`（路径 → `(mtime_ns, size)`，`record`/`check`）；读取格式与限制；`replace_all`；CRLF 处理；摘要与 diff |
| `tools/diffing.py`（新增） | `unified_diff(before, after, path)`（限制 200 行）与 `changed_range` |
| `tools/base.py` | `Tool.read_path()`：只做解析，不做项目边界检查，供读取类工具使用 |
| `tools/search.py`、`search_worker.py` | rg 参数；回退实现的 gitignore 解析、上下文、计数模式；输出模式、排序、条数限制 |
| `tools/__init__.py` | `create_default_registry(..., shell="auto")`，文件工具共享一个 `FileState` |
| `permissions.py` | 读取类工具的项目外目标：`Operation.outside=True`，目标为绝对路径；`decide` 对其询问（bypass 放行）；规则解析允许读取类工具使用绝对路径 |
| `config.py`、`cli.py` | 顶层 `shell` 配置并传入注册表；`ShellNotFound` 作为启动错误显示 |
| `prompts.py`、`agent.py` | `environment(..., shell=...)` 使用实际 shell 名称；约束模块中项目边界说明改为“写入限于项目目录，读取项目外需确认” |
| `tui.py` | 工具行与审批标题使用实际 shell 名称；审批中显示“项目目录之外” |

关键取舍：
- 读取记录用修改时间和大小，而非内容哈希：部分读取不必读全文件，开销小；时间戳精度不足的极端情况（同一时间片、大小不变的外部修改）不检测。
- 记录在读取前取 stat：读取过程中文件被改，记录就与当前状态不一致，编辑时会要求重读，偏向安全。
- 模型只收到摘要，不收 diff：模型已知道自己替换了什么，diff 主要服务于人。需要核对时模型可以重新读取。
- `bypassPermissions` 下项目外读取直接放行：该档位的含义是跳过审批，只保留硬限制，而项目外读取已不属于硬限制。

## 验收

| 编号 | 场景 → 结果 |
|------|-------------|
| AC1（F1） | 输出为 `     2\t二`；默认 2000 行；超长行带截断标注；空文件、offset 越界有说明；字节预算翻页不跳行 |
| AC2（F2） | 项目外文件：default 档位产生审批请求，同意后读到内容；无界面拒绝为 `approval_required`；bypass 直接读取；永久授权写入绝对路径规则并能重新加载；项目外写入仍为 `path_blocked` |
| AC3（F3、F6） | `replace_all` 替换多处；默认多处匹配报错且提示 `replace_all`；CRLF 文件用 LF 的多行 `old_text` 修改成功并保持 CRLF |
| AC4（F4） | 未读直接编辑 → `not_read`；读后外部修改 → `stale_read`；文件均未变；写入后连续编辑成功；新建文件无需读取 |
| AC5（F5） | 编辑结果是摘要，`details["diff"]` 含 `-旧`/`+新` 行；模型收到的文本不含 diff |
| AC6（F7） | 输出超限时同时含开头标记和结尾标记，并标注省略字节数；超时参数 600 合法、601 不合法 |
| AC7（F8、F9） | Windows 本机 auto 选中 Git Bash，`echo $((1+2))`、退出码、UTF-8 中文正确；`shell: powershell` 走 PowerShell；显式路径不存在报错；WSL 路径被排除；环境补充和工具描述含实际 shell |
| AC8（F10–F12） | `.gitignore` 中的目录和 `*.log` 被跳过，`!keep.log` 被保留，显式搜索被忽略目录仍可搜到；`grep` 三种输出模式、`ignore_case`、`context`、`head_limit`；`glob` 按修改时间倒序 |
| 回归 | 全量 pytest、compileall、Windows 控制台烟测 |

未验证风险：本机未安装 ripgrep，rg 路径（`--no-require-git`、`--count --null`、`--json` 上下文事件）只能按文档实现，不能在本机运行。（2026-10-30 已安装 ripgrep 补跑通过，见 [validation.md](validation.md)。）
