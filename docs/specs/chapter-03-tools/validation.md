# 第三章工具系统：验收记录

日期：2026-10-09。Windows、Python 3.12.1；实际 SDK 为 openai 3.26.0、anthropic 1.12.1，Textual 8.2.8，Pydantic 2.13.5。

## 结果

最终完整回归：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

**124 passed in 33.62s**，无跳过。编译检查 `python -m compileall -q src scripts` 通过。

| 规格行为 | 验证及结果 |
| --- | --- |
| 统一 Tool、六个工具与注册中心 | test_tools.py：六个 Schema；重复注册拒绝；未知工具、坏 JSON、缺失/多余参数及类型错误分别返回结构化错误 |
| 文件读写与修改 | 临时目录真实写入/覆盖、嵌套目录、绝对路径与相对越出启动目录；分页带行号；超出字节预算的下一页不跳行；中文与 CRLF 保持；0/多次/重叠匹配不改文件；二进制、解码、目录错误可区分 |
| 内容和文件搜索 | 实际 rg 与强制不提供 rg 的 Python 回退均通过；模式过滤、中文、行号、忽略内部目录、空结果及非法正则。Python 回退也在独立进程中运行 |
| 命令执行边界 | 实际 PowerShell 调用本测试的 Python 进程，验证中文 stdout/stderr、非零退出码、双管道大输出排空与截断、超时 |
| 进程清理 | 测试创建会休眠的命令及子进程，先确认子进程存活，分别取消/等待超时，随后验证子进程已退出。Windows Job Object 使用启动门控，避免分配前的派生进程竞态；未操作其他进程 |
| 三种 API 协议 | test_tool_providers.py 使用实际安装 SDK + MockTransport/SSE：六个定义注入、中文/转义 JSON 分片、多调用与 Chat 交错片段、签名/必要 reasoning 保留、工具 ID 配对、下一轮历史回放、流资源关闭 |
| 不完整请求没有副作用 | 三种协议分别测试断流与取消：未提交工具历史、未写文件，HTTP 流已关闭 |
| 一次工具阶段后停止 | test_tool_conversation.py：顺序写入再读取、最多两次模型请求、续答 allow_tools=False；兼容服务忽略禁用设置时不再执行工具或请求模型，并补齐未执行结果 |
| 取消与失败保留真实状态 | 最终模型响应失败、续答取消、写入线程进行中取消、消费者关闭事件流：已完成写入与结果保留，未启动调用补齐结果，busy 状态释放 |
| TUI 与无界面入口 | test_tool_tui.py：工具行/摘要、无空白助手标题、最终 Markdown、原生背景、输入焦点、取消/失败提示可见、--prompt 共用流程且输出工具结果 |
| 第二章功能回归 | 原有配置、官方/兼容端点、多轮、thinking、取消、命令补全、正常文字亮度、主题面板、鼠标滚轮与历史快捷键检查随完整测试通过 |

## 实际终端与视觉检查

```powershell
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py
.\.venv\Scripts\python.exe scripts/preview_tools.py
```

真实 Windows 控制台脚本 exit 0，使用固定模拟模型和临时演示文件。检查选择界面、流式 thinking/正文/Markdown、历史滚轮、主题、本地命令、实际 Read/Grep 工具与最终 Markdown、退出后恢复 console mode；驱动没有发送备用屏幕或清空全屏指令，也没有独立背景色和 dim 样式。鼠标输入通过真实驱动注入 SGR 事件，未进行物理鼠标操作验证。

实际 Textual compositor 导出的预览见 `docs/ui/previews/tool-dialogue` 与 `tool-dialogue-narrow` 的 JSON/SVG；PNG 根据单元格数据绘制，模拟原生暗色终端背景，不代表应用会涂黑背景。已查看宽屏、窄屏效果，并据此去掉空白助手行、将项目内搜索结果改为相对路径。

## 验证范围与限制

- 未读取用户密钥，未对用户配置的 DeepSeek 或其他真实 API 发请求；测试证明本地执行与标准协议处理，不能证明某个中转服务或具体模型支持所有工具参数组合。真实端点的端到端调用仍待用户运行。
- Windows 的真实命令与进程清理已验证；POSIX 的 sh/进程组代码在本 Windows 主机未运行。
- 文件 I/O 取消需要等待进行中的线程完成，以保留准确结果；没有文件事务或副作用撤销，也未验证挂起的网络文件系统。
- 第四章自动循环、第六章权限、会话持久化、上下文压缩均未实现。只执行一批工具后续答一次是本章明确边界。
- 搜索优先采用 rg；Python 回退的高级正则语法遵循 Python re，基础搜索的两条路径均已验证。
