# 第五章验收记录

日期：2026-10-11。Windows，Python 3.12.1；沿用项目虚拟环境的 SDK、Textual 等依赖，没有新增运行依赖。状态：实现和本地验收完成，真实服务命中与人工效果对比未验证。

## 验证结果

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/test_prompt_cache.py tests/test_cache_probe.py tests/test_providers.py tests/test_tool_providers.py tests/test_conversation.py -q
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe scripts/verify_inline_terminal.py
.\.venv\Scripts\python.exe scripts/preview_agent.py
```

最新完整回归 **184 passed in 34.90s**，包含 Pi 风格工具贡献、模式切换与自定义提示兼容；此前定向提示/缓存测试 **27 passed in 4.43s**。最新编译 exit 0。第五章此前的真实 Windows 控制台和实际宽窄 TUI 预览均 exit 0；PNG 从真实 compositor 单元格生成并查看。本次只调整提示和工具元信息，未重复视觉预览或真实控制台烟测。

完整回归曾发现无工具 Provider 被多传一个位置参数的问题，已恢复无工具调用契约，思考与取消测试通过。挂起烟测进程已结束，烟测等待改为有限等待；本轮启动的测试进程没有继续运行。

## 行为证据

| 行为 | 证据 |
| --- | --- |
| 七模块和扩展位置 | test_prompts 验证模块顺序、空行组装、可选显式模块、旧默认升级；已移除任意全文长度限制，稳定部分不包含工作目录或动态标签 |
| 工具贡献与模式切换 | 默认六工具自动模式、三只读工具计划模式；摘要按注册顺序显示，规则去重，同一指导进入 API 描述。旧自定义工具无元信息时使用描述，自定义完整系统提示保持原样 |
| 请求与探测一致性 | 三个真实 SDK 的模拟请求含启用工具的指导，不泄露内部元信息字段，也不出现未启用的写入工具；探测脚本使用同一提示构建器 |
| 环境作为数据 | 环境仅收集日期、系统、shell、工作目录、工具状态；路径中的标签被 JSON 转义，不读取任意环境变量。该转义不是程序级提示注入防护承诺 |
| 按任务请求调度 | 真正连续执行十次临时文件读取，第 1/6/11 次完整提醒；计划模式其他请求精简，自动模式省略。新任务从完整提醒重启，规范历史只含 user/assistant/tool |
| 角色和缓存边界 | 三个实际 SDK + MockTransport/SSE 检查稳定 instructions/system、环境和模式的独立 system/developer 位置、唯一 Anthropic 稳定块 cache_control；用户正文不混入补充 |
| 官方/兼容端点 | SDK 实际 URL 为官方地址才默认标记；配置 URL 缺失但实际连接模拟中转时不标记。显式开/关有效，其他协议不附加 Anthropic 参数 |
| 缓存解析 | Responses cached_tokens/cache_write_tokens；DeepSeek hit/miss；Anthropic message_start 与累计 message_delta 更新。缓存统计 80/20 更新为 100/30，普通输入 12，总输入正确为 142，不叠加快照 |
| 缺失与零 | 已知零、缺失、负数、错误类型独立验证；Anthropic 不完整分解不虚构总输入，旧服务只报普通输入时保留原输入并将分解标未知 |
| 会话累计与显示 | 两次已知一次缺失：缓存读取累计 ≥8、写入 ≥2；全缺失显示未提供。TUI 历史不显示补充标签，模式与 Token 显示保留 |
| CLI 分流 | --show-usage 的逐次用量只写 stderr，不污染 stdout 正文，Provider 正常关闭 |
| 探测工具不执行 | 显式模拟配置运行三次；模拟模型故意返回写入调用，目标文件仍不存在。前两次请求相同，第三次只增加补充；日志没有回复正文、密钥或提示 |
| 原有行为 | 多轮工具配对、签名/reasoning 回放、计划模式拦截、取消/断流清理、CLI、历史、补全、Markdown、真实 Windows 子进程回归通过 |
| Windows 和视觉 | 实际内联驱动无应用备用屏幕、全屏清空、独立背景或 dim，console mode 与鼠标模式恢复；宽屏/窄屏模式和缓存底栏不覆盖输入与历史 |

## 尚未验证

- 没有读取用户真实配置或密钥，没有请求真实 DeepSeek、OpenAI、Anthropic 或中转服务。上述缓存数来自模拟响应，不能证明服务真实命中、降低成本或改善模型行为。
- 真实缓存探测脚本与八组人工比较步骤见 [manual-evaluation.md](manual-evaluation.md)，尚未执行人工模型比较或填写评价结果。
- 文本约定不能保证模型编辑前总会读取；本章按用户选择不实现读后编辑程序门禁。计划模式仍由执行层约束。
- 本章仅保护稳定前缀组织，不保证所有历史缓存复用；Anthropic 动态 system 块不标缓存，OpenAI/DeepSeek 的具体缓存范围由服务决定。未进行费用估算或缓存门槛填充。
- 未引入项目指令文件加载、Skill/记忆加载、MCP、权限、自动评估、压缩或持久化；POSIX 主机未联调。
