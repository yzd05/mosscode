# 第七章：MCP 工具客户端

> 权限模式已在 2026-10-15 更新，见 [四档权限规格](../permission-modes/spec.md)。本章保守 MCP 分类和 plan 禁用 MCP 的约定保持不变。

日期：2026-10-14。用户已确认官方 Python SDK、保守权限分类、独立两层配置与同名 Server 整项覆盖。

## 范围和契约

- 本项目名称为 MossCode。增加 stdio 与 Streamable HTTP 客户端，初始化、工具列表分页、调用工具；JSON-RPC 2.0 ID 配对和协议协商由官方 SDK 完成。
- 用户级 `~/.mosscode/mcp.yaml`，项目级 `<root>/.mosscode/mcp.yaml`，顶层 `mcp_servers` map。同名项目项整体覆盖用户项，不深合并字段。没有文件或空 map 时不连接任何 Server。
- stdio 配置 `type: stdio`、`command`、`args`、`env`；HTTP 配置 `type: http`、`url`、`headers`。仅 env/headers 值展开 `${VAR}`；缺失变量使对应 Server 不可用并给出安全说明，不回显凭据。字段、类型或 YAML 错误明确报告。
- 每个 Server 在当前事件循环中拥有独立连接与生命周期任务；所有调用复用其会话。启动发现结束后注册工具，失败隔离。启动时有界等待，取消/退出关闭会话、HTTP 客户端及本程序启动的 stdio 进程。无健康探测和自动重连，不重试工具调用。
- 稳定、唯一、符合模型工具名称限制的命名空间名称关联 Server 与原始工具名；保留原始 JSON Schema，校验参数后经现有工具执行入口调用。支持工具列表分页，异常分页不能无限等待。
- 外部工具默认 `read_only=False`，不信任 readOnlyHint；计划模式不展示或执行它们。默认档位需确认，严格/放行档位和分层规则继续适用。
- MCP 权限目标使用规范化 JSON 参数，不把外部参数的 path 误判为本地文件路径；保存授权精确限定命名空间工具与参数。规则 `mcp__server__tool(*)` 可匹配该工具的任意参数，精确保存不扩大范围。
- 审批显示 Server/工具与参数；MCP 配置文件由用户管理，专用本地写入/编辑不可更改它们。外部 Server 的实际文件与网络访问不受本地路径隔离或 Shell 黑名单约束，不宣称有系统沙箱。
- 文本结果与 structuredContent 进入现有 ToolResult；isError 保持失败语义。其他内容类型明确标记不支持，不自动获取资源，不把图片/base64 大块原样放入历史。工具结果和参数均有现有/新增体积边界。
- TUI 与 --prompt 共用管理器。启动发现期间不能发送任务，连接结果显示中文摘要；无界面默认需确认操作仍返回 approval_required，不挂起。
- 不实现资源、提示词、sampling、elicitation、roots 提供、OAuth、工具变更订阅或其他非工具产品能力；本章远程认证使用配置 headers。

## 设计与任务

1. mcp_config.py：严格配置解析、两层合并、环境值展开、Server 问题隔离。
2. mcp_client.py：生命周期所有者任务、两种传输、初始化/发现/缓存/调用/关闭；Tool 适配器保留原始 Schema。使用 SDK 1.x 明确依赖范围，按本地实际安装接口验收。
3. tools/base.py：可覆盖的参数验证接口，复用本地 Pydantic；外部工具使用 JSON Schema 验证。
4. permissions.py：MCP 参数目标、命名空间规则、精确授权和配置保护。禁止将 MCP 参数视为本地路径。
5. Conversation/CLI/TUI：在实际使用的事件循环内初始化与关闭，异步启动不冻结界面；状态和工具展示沿用现有布局。
6. 测试和文档：临时 stdio Server、本地 HTTP Server、失败与取消场景，不读取真实 API 配置或连接用户实际 Server。

HTTP 适配固定在 SDK 1.30.x：复用官方 StreamableHTTPTransport 的 POST/JSON/SSE 处理及 ClientSession，仅关闭可选 GET 通知流，并覆盖一个 SSE 恢复钩子，让中断按原请求 ID 返回连接关闭错误。官方默认会恢复带 event ID 的中断 SSE，因此只关闭通知 GET 不足以满足本章不自动重连要求。升级 SDK 时必须重新验证此钩子。stdio 的 AnyIO 上下文始终由同一个生命周期任务进入和退出，避免跨任务关闭取消作用域。

## 验收

- 合并、整项覆盖、空配置、错误配置、变量缺失和错误信息不泄露 headers/env。
- 两种传输真实握手/list/call，工具分页、会话复用、同时存在同名远端工具、请求关联。
- JSON Schema 参数不合法不发送调用，内部引用支持，外部引用不引入隐式网络获取。
- MCP isError、协议错误、连接中断/不可用、超时和取消均产生合理结构化结果；无自动副作用重试。
- 默认审批/拒绝、会话/永久精确授权、规则冲突、计划模式拦截、无界面不等待；旧本地工具权限回归。
- 单个 Server 失败不影响其余；初始化取消、调用取消、重复 close 和启动复用；无未关闭客户端或子进程。
- 使用模拟模型测试 Agent Loop 与 TUI 集成；全量回归、编译、依赖检查和 Windows 控制台烟测。

验证记录在 validation.md。真实远程服务、OAuth、Server 自身隔离不属于本章本地验收结论。
