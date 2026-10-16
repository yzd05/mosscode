# 完善阶段 5：MCP（不阻塞启动、/mcp、Server 级规则、只读提示、.mcp.json、按 Server 超时）

来源：[差距评审](../../research/claude-code-pi-gap-review.md) 的 M1–M5。依赖阶段 3/3b 的规则格式。

## 现状

- TUI 启动时禁用输入框，等所有 Server 连接和工具发现完成（每个最多 10 秒）。一个慢 Server 会拖住整个启动。
- 看不到各 Server 的状态。失败只有一句“连接或发现失败，已跳过”，没有原因。
- 规则只能写 `mcp__server__tool(参数 JSON glob)`，不能一次覆盖整个 Server。审批的“本会话/永久允许”保存的是**完全相同的参数 JSON**，参数稍有不同就要重新审批，几乎不会再命中。
- 所有 MCP 工具都视为有副作用：计划模式不可用，每次调用都需要审批（readOnlyHint 被忽略）。
- 只读取 `.mosscode/mcp.yaml`，不能复用 Claude Code 的 `.mcp.json`。
- 工具调用超时固定 30 秒，启动超时固定 10 秒。
- MCP 结果已经是纯文本，失败以 `错误 [code]：` 开头（阶段 1 的 A1）。M5 的这一部分不需要再改动。

## 需求

| # | 行为 |
| --- | --- |
| F1 | **不阻塞启动**：TUI 启动时只等项目上下文（AGENTS.md、git 快照）加载完，输入框始终可用。MCP 连接在后台进行，每个 Server 就绪后立即注册工具，并显示一条本地提示；如果任务正在运行，提示推迟到任务结束后显示。就绪前发送的请求不包含这些工具，就绪后的下一次模型请求（包括同一任务的下一轮）会包含。状态栏在连接期间显示“MCP 连接中 n/m”。无界面 `-p` 仍然等所有 Server 就绪后再发送请求，保证脚本结果确定 |
| F2 | **`/mcp`**：<br>• 列出所有 Server：名称、传输方式、状态（连接中 / 已连接·N 个工具 / 失败：原因 / 已跳过：原因 / 已关闭），以及配置问题。<br>• `/mcp 名称` 列出该 Server 的工具：暴露名、是否按只读处理、描述首行。<br>• 失败原因只用安全的分类说明（找不到命令、启动超时、无法连接、HTTP 状态码、协议错误、工具 Schema 无效等），不包含 URL、headers、环境变量或 Server 的原始输出。<br>• 没有配置 MCP 时给出配置文件位置的提示。命令补全中加入 `/mcp` 和 Server 名称 |
| F3 | **Server 级和工具级规则**（Claude Code 写法，不带括号）：`mcp__server` 或 `mcp__server__*` 匹配该 Server 的全部工具，`mcp__server__tool` 匹配该工具的任意参数。allow 和 deny 都支持。原有的 `mcp__server__tool(参数 glob)` 和 exact 规则不变。Server 级规则按配置中的 Server 名称匹配，与工具名是否被哈希缩短无关 |
| F4 | **MCP 审批选项**：本次允许 / 本会话允许该工具（任意参数）/ 永久允许该工具（任意参数，写入本地权限文件，保存为 `mcp__server__tool`）/ 拒绝。选项文字明确写出“任意参数” |
| F5 | **信任只读提示**：每个 Server 可以配置 `trust_read_only_hint: true`（默认 false）。开启后，带 `readOnlyHint: true` 的工具按只读工具处理：计划模式中可用，各档位都自动放行（显式 deny 规则仍然生效），也参与只读工具的并发（相邻只读调用最多 4 个同时执行） |
| F6 | **`.mcp.json`**：在 `~/.mosscode/mcp.yaml` 或项目 `.mosscode/mcp.yaml` 中设置顶层 `import_mcp_json: true` 后，读取项目根目录的 `.mcp.json`（`mcpServers`）。<br>• 优先级：用户 mcp.yaml < `.mcp.json` < 项目 mcp.yaml，同名 Server 整体替换。<br>• stdio 项可以省略 `type`；支持 `http`；`sse` 等其他传输方式给出“不支持”的提示，并跳过该 Server。<br>• `command`、`args`、`env`、`url`、`headers` 中的 `${VAR}` 和 `${VAR:-默认值}` 会被展开；其余字段忽略。<br>• 模型不能写入或编辑 `.mcp.json` |
| F7 | **按 Server 配置超时**：`timeout`（工具调用，默认 30 秒，范围 (0, 600]）和 `startup_timeout`（连接和发现，默认 10 秒，范围 (0, 120]），两种格式都支持。`${VAR:-默认值}` 在 mcp.yaml 的 env/headers 中同样可用 |

## 设计

| 模块 | 改动 |
| --- | --- |
| `mcp_config.py` | `ServerConfig` 新增 `timeout`、`startup_timeout`、`trust_read_only_hint`。`_read` 允许顶层 `import_mcp_json`。新增 `_read_mcp_json` 把 `.mcp.json` 转换为内部格式。`_expand` 支持 `:-默认值`。`config_paths` 不变，另外提供 `mcp_json_path(root)` |
| `mcp_client.py` | `ServerConnection` 记录 `error`（安全的原因说明）和 `tools`。`describe_failure(exc)` 负责分类，会展开 ExceptionGroup。`MCPTool.read_only` 只在信任时取 readOnlyHint，`timeout` 取配置值；工具调用按 `timeout` 单独设置读取超时，握手与发现请求使用 max(timeout, startup_timeout)，避免很短的工具超时让启动失败。`MCPManager.begin(registry, on_update)` 为每个连接建立后台任务，就绪即注册；`start()` = `begin()` + 等待全部完成，并返回提示（保持原有接口）；`statuses()` 供 `/mcp` 使用；`pending` 表示仍在连接的数量 |
| `conversation.py` | `start(*, wait_mcp=True, on_mcp=None)`：项目上下文仍在锁内加载。需要等待时调用 `manager.start`，否则调用 `manager.begin`。`reply()` 不再等待已在后台运行的 MCP |
| `permissions.py` | `Operation.server`；`Rule.server`；`parse_rule` 支持不带括号的 MCP 写法；MCP 沿用四个审批选项，`session`/`permanent` 对 MCP 保存为工具级规则；`_persist` 写成 `mcp__server__tool`；`.mcp.json` 加入受保护文件 |
| `tui.py` | `_initialize` 不再禁用输入框，用 `wait_mcp=False` 启动。`_on_mcp_update` 生成提示，运行中先缓存。状态栏显示 MCP 连接进度。新增 `/mcp` 命令。MCP 审批选项使用新文字（`choice_label` 按 `operation.server` 判断）。`_status` 在 `#status` 已卸载时直接返回 |
| `appearance.py` | 命令提示和帮助加入 `/mcp`；补全 Server 名称 |

### 后台注册的时序

```
on_mount → start(wait_mcp=False)
  ├─ 加载上下文（锁内，很快）
  └─ manager.begin：每个 Server 一个任务
        connection.start() → tools
        已关闭 → 丢弃；与已注册的工具名冲突 → 关闭，状态记为“已跳过”
        否则 registry.register(...)（同步完成，不会与模型请求交错）→ on_update(status)
AgentLoop 每轮 _model() 都重新读取 registry.definitions() → 新工具出现在下一次请求
```

## 取舍

- **工具在任务中途出现**：同一任务内工具列表发生变化，会让缓存前缀失效一次。好处是不用等慢 Server。也可以选择“任务开始时冻结工具列表”，但那样任务开始后才就绪的工具要等到下一个任务才能用。
- **审批改为工具级**：与 Claude Code 一致，便利性大幅提高。风险是选了“本会话允许”后，同一工具的其他参数（例如发给别的收件人）也会自动放行，所以选项文字要明确写出“任意参数”。需要逐个参数确认时选“本次允许”，精确参数规则仍可以手写。
- **`.mcp.json` 需要显式开启**：克隆来的仓库中的 `.mcp.json` 可以让程序自动启动任意命令。Claude Code 对项目级 Server 会先征求同意；这里用一个配置开关代替交互式批准，保持轻量。项目 `.mosscode/mcp.yaml` 一直是直接生效的，这个已有的信任模型不变，在 README 中说明。
- **不提供手动重连**：`/mcp` 只显示状态。修改配置后需要重启。重连涉及注销和重新注册工具，以及正在进行中的调用，留到以后。

## 验收

| 验收 | 方法 |
| --- | --- |
| AC1（F1） | 使用一个启动会挂起的 Server 和一个正常 Server：<br>• TUI 挂载后输入框立即可用，状态栏显示“MCP 连接中”。<br>• 正常 Server 就绪前发送的请求不包含其工具，就绪后的请求包含。<br>• 任务运行期间就绪的提示，在任务结束后才显示。<br>• 退出时后台任务被清理。<br>• `Conversation.start()` 默认仍然等待，`-p` 的行为不变 |
| AC2（F2） | `/mcp` 显示已连接（工具数）、失败（找不到命令、HTTP 503、启动超时，原因中不含 URL 或密钥）、配置问题；`/mcp notes` 列出工具和只读标记；未知名称、未配置 MCP 时给出提示；补全 `/mcp ` 后出现 Server 名称 |
| AC3（F3） | 解析和匹配：`mcp__notes`、`mcp__notes__*` 匹配该 Server 的全部工具，不匹配其他 Server；`mcp__notes__echo` 匹配任意参数；deny 写法优先于 bypass；名称被哈希缩短的工具也能被 Server 级规则匹配；非法写法报错 |
| AC4（F4） | 真实 stdio Server 审批：选项为 4 项，并写明“任意参数”；选择 permanent 后，本地文件出现 `mcp__notes__echo`，换参数后不再询问，重新加载后仍然有效；session 只在本会话内有效 |
| AC5（F5） | 开启 `trust_read_only_hint` 时，带提示的工具在 default 档免审批，在计划模式中可用（工具定义中包含，调用不返回 plan_blocked），deny 规则仍然生效；不开启时保持原有行为 |
| AC6（F6） | 未开启时忽略 `.mcp.json`。开启后：stdio 项可省略 type；http 项可用；sse 项产生“不支持”的提示；`${VAR:-默认值}` 被展开；优先级正确；模型写入 `.mcp.json` 返回 policy_protected |
| AC7（F7） | `timeout: 0.2` 使慢工具返回 timeout；`startup_timeout` 让挂起的 Server 按配置时间失败，失败原因写明“启动超时”；超出范围的值产生配置提示 |
| 回归 | 全量 pytest 连续两次，compileall，Windows 控制台烟测（其中 MCP 场景按新的不阻塞启动更新） |
