# MCP 外部工具

MCP Server 的配置、启动、权限与限制。

## 配置

官方 Python MCP SDK 已安装到当前 `.venv`。所选 stdio Server 的程序和运行环境由用户安装；远程地址需支持 Streamable HTTP。配置独立于模型配置：用户级 `~/.mosscode/mcp.yaml`，项目级 `<启动目录>/.mosscode/mcp.yaml`；顶层 `mcp_servers` map，同名 Server 由项目级**整项覆盖**。没有文件或空 map 时不连接，可从 [mcp.example.yaml](../../mcp.example.yaml) 填写：

```yaml
mcp_servers:
  notes:
    type: stdio
    command: 'C:\path\to\python.exe'
    args: ['C:\path\to\your_mcp_server.py']
    env:
      SERVICE_TOKEN: '${NOTES_TOKEN}'
  remote:
    type: http
    url: https://your-server.example/mcp
    headers:
      Authorization: 'Bearer ${NOTES_TOKEN}'
```

`${VAR}` 与 `${VAR:-默认值}` 仅在 env/headers 的值中展开（变量未设置或为空时取默认值），先在启动进程环境中设置变量。缺少变量或单项配置错误只跳过对应 Server，并在启动提示和 `/mcp` 中列出；顶层/YAML 错误阻止启动。stdio 直接执行程序，以项目为工作目录，SDK 继承基础系统环境，其他所需变量显式填 env。HTTP 不做 OAuth 或自动读取代理环境。启动诊断和 `/mcp` 的失败原因只给类别（找不到命令、HTTP 503、启动超时、协议错误等），不回显 env、headers、URL 或 Server 输出。

每个 Server 可选三项：

| 选项 | 默认 | 含义 |
|------|------|------|
| `timeout` | 30 | 单次工具调用的超时秒数（0–600）；审批等待不计入 |
| `startup_timeout` | 10 | 连接、握手和工具发现的超时秒数（0–120） |
| `trust_read_only_hint` | false | 信任 Server 的 `readOnlyHint`：带该提示的工具按只读工具处理，default 档免审批、计划模式可用；deny 规则仍然生效 |

## 启动与发现

**启动不阻塞输入。** 界面挂载后输入框立即可用，各 Server 在后台并行连接、握手、分页发现，状态行显示“MCP 连接中 已完成/总数”。每个 Server 就绪时一次性注册全部工具，下一次模型请求即可使用；之前发出的请求不包含它的工具。与 Claude Code 一样，连接成功不单独提示（用 `/mcp` 查看）；失败的提示在空闲时立即显示，任务运行期间则等任务结束后显示，不打断回复。全部 Server 完成后“连接中”标记消失。失败不影响其他 Server 和本地工具，也不自动重连；修改配置后重新启动生效。`--prompt` 无界面模式仍等待全部 Server 完成发现后再发送。

调用复用会话，名称通常为 `mcp__notes__search`；特殊或过长名称生成带摘要的稳定名称。保留原始 JSON Schema，在调用前校验；不自动加载外部 Schema 引用。

## 权限

外部工具默认按可能有副作用处理：default 档需确认，各档位及分层规则继续适用；未开启 `trust_read_only_hint` 时计划模式不展示或执行 MCP 工具。审批在输入区下方显示工具和 JSON 参数，选项为本次 / 本会话 / 永久 / 拒绝。与 Claude Code 一致，**本会话和永久授权针对整个工具（任意参数）**；永久授权以工具名 `mcp__notes__search` 保存到 `<项目>/.mosscode/permissions.local.yaml`。规则写法：

```yaml
allow:
  - mcp__notes                # notes 的全部工具（等价写法 mcp__notes__*）
  - mcp__notes__search        # 该工具，任意参数
  - rule: 'mcp__remote__query({"q":"MossCode"})'
    exact: true               # 精确绑定规范化 JSON 参数
deny:
  - mcp__remote__delete       # deny 优先，bypassPermissions 也不放行
```

Server 级规则按配置中的 Server 名称匹配，名称被摘要缩短的工具同样适用。带参数的写法沿用 JSON 匹配：大小写敏感，普通 glob 匹配整个规范化 JSON 字符串。`--prompt` 不等待交互确认，需确认调用返回 approval_required 供模型调整。

## 读取 .mcp.json

**读取 `.mcp.json`（可选）。** 在任一 `mcp.yaml` 顶层设置 `import_mcp_json: true` 后，读取项目根目录的 `.mcp.json`（Claude Code 的 `mcpServers` 格式）。省略 `type` 且有 `command` 时视为 stdio；支持 `http`，`sse` 等其他类型提示“不受支持”并跳过；command、args、url、env、headers 都展开 `${VAR}` / `${VAR:-默认值}`。优先级：用户 `mcp.yaml` < `.mcp.json` < 项目 `mcp.yaml`，同名整项覆盖。`.mcp.json` 随仓库分发、会启动其中的程序，因此默认不读取；开启前请确认其内容可信。模型不能写入或编辑 `.mcp.json`（返回 policy_protected）。

## 安全边界与结果

**本地路径限制不隔离 MCP Server。** 外部参数的 path 不按本地文件路径检查；Server 可能在远端，也可能以当前用户权限运行，Shell 黑名单不检查其内部行为。专用本地写入/编辑不能修改 MCP 配置；用户手动修改后重启生效。

文本和 structuredContent 进入工具结果，isError 保持失败。图片内容按 [图片](tools.md#图片) 的规则处理后附在结果中，结果文本中每张图片有一行说明，例如 `server/tool 的图片 1 · 图片 · 800×600 · 120 KB`；当前模型不接受图片或数据无效时，这一行注明原因，不附带图片。其他类型明确标明未展示，不自动获取资源。参数最多 64 KiB，成功与失败正文超过 64 KiB 均截断。调用超时按 Server 的 `timeout`（默认 30 秒），审批等待不计入超时。断连、超时、取消不自动重连或重发，可能产生的外部效果不能回滚。退出关闭会话、HTTP 客户端和本程序启动的 stdio 子进程。

HTTP 支持 POST 的 JSON/SSE 响应，不打开可选 GET 通知流，不恢复中断 SSE。目前不实现资源、提示词、采样、roots、OAuth、健康检查或工具变更订阅。
