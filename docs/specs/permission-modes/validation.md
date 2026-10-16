# 四档权限验收记录

日期：2026-10-15，Windows、项目 Python 3.12 虚拟环境。

## 结果

- 四档权限专项 `tests/test_permission_modes.py`：33 项（包含在下面的回归批次中），全部通过。
- 首批权限、Agent、TUI、提示回归：126 passed, 1 skipped。
- MCP、三种 API 的工具序列化/缓存、新模式复核：86 passed。
- 最终全量回归：**302 passed, 1 skipped**，45.89 秒。
- `compileall -q src scripts tests`、`pip check` 和 CLI `--help`：通过。
- Windows 真实控制台 `scripts/verify_inline_terminal.py`：通过，exit 0；实际显示“权限：接受编辑”，MCP 调用仍经过键盘审批，控制台模式恢复。

唯一跳过是现有文件符号链接创建测试，当前 Windows 用户不能创建文件符号链接；目录 junction 及其余路径边界测试通过。

## 关键行为证据

- 矩阵测试经过真实 ToolRegistry 和 PermissionPolicy 执行入口，只有最终工具函数被模拟；覆盖四个模式下只读、写入、编辑、Bash 写文件、MCP 的不同决定。
- acceptEdits 界面集成：真实文件工具自动创建临时文件，后续 `bash -c "echo 3 > hello.txt"` 停在审批面板；确认前命令函数没有调用，确认后仅调用一次。此处命令函数为模拟，不写实际 hello.txt。
- plan 界面集成：确认前不写入；允许后真实临时文件写入，拒绝后无文件；两条分支都会把正确结果回写模型，并保持 plan。后续 /permissions、/plan、/do 恢复 acceptEdits。
- CLI/YAML 四种模式、旧 permissive 别名、旧 strict 行为、进入计划前的权限恢复和 plan 启动默认状态均覆盖。
- 分层 allow/deny 顺序及硬检查回归；bypassPermissions 仍不能执行已知黑名单命令或突破文件工具路径保护。
- 计划模式向三种 API 发送六个本地工具，并同步更新模式提醒；MCP 仍被计划模式过滤并在执行入口禁止。
- 无界面 Ask 返回 approval_required，不挂起、不自动执行。用户 MCP 配置在 CLI 测试中已隔离，未连接实际服务。

## 复核命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_permission_modes.py tests/test_permissions.py tests/test_permission_tui.py tests/test_agent.py tests/test_agent_tui.py tests/test_prompts.py --basetemp .test-artifacts\modes-20261015-03
.\.venv\Scripts\python.exe -m pytest -q tests/test_mcp.py tests/test_prompt_cache.py tests/test_tool_providers.py tests/test_permission_modes.py --basetemp .test-artifacts\modes-20261015-04
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-artifacts\modes-full-20261015-02
.\.venv\Scripts\python.exe scripts\verify_inline_terminal.py
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe -m pip check
```

本次工具的受限执行环境把 Python asyncio 初始化阻塞在 socket.socketpair 的回环 accept；最小 `asyncio.run(asyncio.sleep(0))` 同样无法进入循环。通过执行工具审批后，在支持本机回环的环境中重跑最小检查成功，上述异步回归和真实控制台烟测在该环境执行。未修改系统网络设置，也没有为通过测试修改应用的事件循环。已按确切 PID 和唯一命令标记清理卡住的自有诊断进程。

第一次全量回归发现旧测试仍断言计划模式只有三个只读工具，现已按新需求更新为六个本地工具；另修复了通过旧 permissive 名称直接设置策略状态时未规范化的问题。最终回归通过。

测试使用本地模拟模型/Server；本次没有连接真实模型或远端 MCP 服务，也没有修改用户配置。已知 -Force 正则误判和 Shell 无法被应用层路径保护全面限制的边界仍存在，本次没有扩展为系统级沙箱或宣称解决这些独立问题。
