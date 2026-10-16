# 第六章本地验收

日期：2026-10-12。结论：已按确认的应用层方案实现并通过本地验收；未调用真实模型 API、未读取真实 Provider 凭据、未引入系统级沙箱。

## 实现与需求对应

| 需求 | 实现及证据 |
| --- | --- |
| 不可配置解除的危险命令拦截 | security.py 的固定正则；三种档位和显式 allow 下均返回 dangerous_command；安全相似命令样例通过 |
| 项目文件路径隔离 | project_path 按启动时捕获的根目录与解析后的路径组成部分判断；文件工具、搜索入口、搜索遍历、命令 cwd 共用边界 |
| 精确、glob 与分层规则 | permissions.py 支持六工具别名、真实工具名、单段 *、跨目录 **、精确对象和 cwd；会话 > 本地 > 项目 > 用户，同层 deny 优先 |
| strict/default/permissive | 各档位行为矩阵、显式 deny、模式配置层覆盖、计划模式检查、无界面 approval_required 均测试 |
| 本次/会话/永久授权 | 本次不留规则；会话限定工具和目标；永久原子保存本地文件，重建策略后有效；命令同时绑定精确内容与 cwd |
| 拒绝后继续 Agent Loop | 假模型首轮写入被拒，下一轮读取允许文件，第三轮正常完成；工具拒绝结果确实进入后续请求历史 |
| 异步审批与取消 | ApprovalRequest 经 execute_batch 事件流到 TUI；审批排队、等待不占执行超时、Ctrl+C/退出清理、历史结果配对与取消后复用测试 |
| 内联键盘界面 | Enter 授权、Esc 单次拒绝、Ctrl+C 取消、点击不执行；100×32 和 44×20 布局测试，蓝色选项、终端背景继承 |
| 规则文件保护 | 专用 write_file/edit_file 不可修改用户/项目/本地权限文件；永久授权通过独立持久化入口保存 |

额外覆盖：同前缀相邻目录不能越界，Windows junction 指向项目外时读写与搜索被拦，新文件不能在链接外目标创建；将启动后的项目根目录替换为外部 junction 也不能移动边界。审批期间目标变化返回 target_changed，新增本地 deny 在保存授权时仍生效，持久化失败不执行且保留原文件。

## 执行结果

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

最终结果：**242 passed, 1 skipped in 35.36s**。跳过的是文件符号链接测试：当前 Windows 用户没有创建文件符号链接的权限。Windows 目录 junction 的真实创建、解析、搜索与根目录替换测试已执行并通过；不把 junction 的通过视为文件符号链接测试也已执行。

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py
```

编译通过。烟测在实际 Windows PTY/控制台直接运行，未将 stdout 重定向到文件，exit 0。固定假模型与临时项目自动确认一次写入，确认前目标不存在、确认后正文与工具结果完成、审批请求清空；脚本同时验证已有命令补全、主题、流式思考/正文/Markdown、Agent 多轮、计划/执行切换、连续两次启动的原生滚动记录和控制台模式恢复。

脚本检查应用驱动自身输出不进入 alternate screen、不发整屏清空、不启用鼠标报告、不绘制独立背景、不使用 dim；最外层 PowerShell/PTY 的初始化控制码不计作应用驱动输出。最终摘要包含 `inline_keyboard_permission_approval: passed`、`native_scrollback_across_restarts: passed`、`console_modes_restored: true`。

实际界面预览由 `scripts/preview_permissions.py` 的 Textual compositor 导出到 `docs/ui/previews/permission-approval.svg` 和 `.json`。项目虚拟环境没有 Pillow，PNG 用 Codex 内置依赖 Python 运行现有 render_terminal_preview.py 绘制，未为此修改项目运行环境。已查看 PNG，随后将授权正文改为正常前景色，仅标题、目标与选项保留语义色；预览已重新生成。

文档查询使用 Context7 的 Textual 官方文档 [OptionList](https://textual.textualize.io/widgets/option_list/) 与 [键盘绑定](https://textual.textualize.io/guide/input/) 核对 OptionSelected 和优先级绑定，运行时以项目实际安装的 Textual 为验收依据。

## 范围与限制

- 命令进程仍使用当前用户系统权限；仅限制 cwd、检查已知危险特征并执行授权策略，不保证其所有读写都在项目内。正则不保证识别全部混淆与间接执行，也可能误报。
- 文件工具解析链接并在审批后/实际操作前再次检查；不保证防住其他进程在检查与 IO 之间并发更换链接的全部竞争情况。
- 权限规则通常在启动时加载；永久保存前重读本地文件。没有自动监听或热重载所有规则文件。
- 配置示例未自动安装为实际权限文件；默认档位无需额外配置即可使用。已有非权限回归测试和开发预览明确选择 permissive 并关闭外部规则加载，以隔离测试行为；硬限制仍生效，生产启动没有此绕过。
- 不包含网络限制、资源配额、审计日志、历史压缩或会话恢复。真实服务的模型如何根据权限拒绝调整策略尚未进行真实 API 评估。
