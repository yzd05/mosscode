# MCP 外部工具

MCP Server 的工具、资源、提示词配置、启动、权限与安全边界。

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

**启动不阻塞输入。** 界面挂载后输入框立即可用，各 Server 在后台并行连接、握手、分页发现，状态行显示“MCP 连接中 已完成/总数”。每个 Server 就绪时一次性注册全部工具与提示词命令，并根据声明的 capabilities 发现资源（`resources/list`）、资源模板（`resources/templates/list`）与提示词（`prompts/list`），下一次模型请求即可使用；之前发出的请求不包含它的工具。与 Claude Code 一样，连接成功不单独提示（用 `/mcp` 查看工具、资源、资源模板与提示词命令）；失败的提示在空闲时立即显示，任务运行期间则等任务结束后显示，不打断回复。全部 Server 完成后“连接中”标记消失。失败不影响其他 Server 和本地工具，也不自动重连；修改配置后重新启动生效。`--prompt` 无界面模式仍等待全部 Server 完成发现后再发送。

stdio 传输默认开启自适应分帧识别：同时兼容标准换行分隔 JSON（JSONL）与旧式 LSP 风格的 `Content-Length` 字节分帧（按 UTF-8 字节长度切分，单帧上限 16 MiB），对握手阶段要求严格 `Content-Length` 请求头的旧式 Server 也会自动切换重试，无需手工配置分帧模式。

调用复用会话，工具名称通常为 `mcp__notes__search`；特殊或过长名称生成带摘要的稳定名称。保留原始 JSON Schema，在调用前校验；不自动加载外部 Schema 引用。

## 资源与提示词

### 资源（Resources 与 Resource Templates）

配置了 MCP Server 时，自动向模型提供两个只读内置工具（计划模式中也可用），并支持用户在输入中通过 `@server:uri` 直接引用：

- **`list_mcp_resources`**：列出当前已就绪 Server 发现的资源与 URI 模板（可选按 `server` 筛选，默认包含 `include_templates: true`）。被权限规则显式 `deny` 的 Server 不会列出。
- **`read_mcp_resource`**：按 `server` 和 `uri` 调用 `resources/read` 读取指定资源。文本资源单次最多保留 64 KiB（超出截断提示）；`image/*` 二进制资源按 [图片](tools.md#图片) 规则解码并随消息附带；其他二进制资源仅返回 MIME 类型与字节长度摘要。
- **`@server:uri` 内联引用**：在消息中写 `@notes:memo://today` 或 `@"db:postgres://localhost/users"` 时，发送前自动读取对应资源并包入 `<mcp-resource server="..." uri="...">` 附加消息（正文中出现的同名闭合标签自动转义，与本地 `@文件` 共享单条消息最多 10 个引用、总计 112 KiB 的预算）。本地文件路径（包括 Windows 盘符路径 `@C:\path\file.py`、`@d:/a.py` 与行段 `@src/app.py#L10-L20`）始终优先按本地文件解析，仅当本地不存在该路径且前缀匹配已就绪的 MCP Server 时才按 MCP 资源读取。交互界面输入 `@` 时也会补全已就绪的 `server:` 与 `server:uri` 候选。

### 提示词（Prompts 斜杠命令）

声明了 `prompts` 能力的 Server 就绪后，每个提示词自动注册为 `/mcp__<server>__<prompt>` 斜杠命令（转小写并规范化为合法命令名，特殊字符或超长名称附带短摘要；若与内置命令或 Skill 重名则自动跳过并给出提示），在终端交互界面、`-p` 无界面模式与 [ACP 编辑器集成](acp.md)中均可直接调用：

- **参数解析**：同时支持 `key=value`（含引号 `key="two words"`）与按声明顺序映射的位置参数；当提示词只有一个必填参数且输入不含 `key=` 时，整段输入直接作为该参数传入（例如 `/mcp__notes__summarize 这段文字`）。缺少必填参数时直接提示用法，不发起请求。
- **展开与多模态**：调用 `prompts/get` 获取模板消息，合并文本与内嵌资源（上限 64 KiB），返回的图片自动附在提交的用户消息上；历史记录与标题保留用户输入的斜杠命令原文。
- **ACP 动态同步**：后台 MCP 发现注册新的提示词命令后，主动向客户端推送 `available_commands_update` 通知，编辑器斜杠菜单无需重开会话即可显示新命令。

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

Server 级规则按配置中的 Server 名称匹配，名称被摘要缩短的工具同样适用，并且同时约束该 Server 的资源读取（`read_mcp_resource`、`list_mcp_resources`、`@server:uri`）与提示词命令（`/mcp__<server>__<prompt>`）；也可单独写 `read_mcp_resource` 或 `read_mcp_resource(notes:*)` 拒绝资源读取。带参数的工具规则沿用 JSON 匹配：大小写敏感，普通 glob 匹配整个规范化 JSON 字符串。`--prompt` 不等待交互确认，需确认调用返回 approval_required 供模型调整。

## 读取 .mcp.json

**读取 `.mcp.json`（可选）。** 在任一 `mcp.yaml` 顶层设置 `import_mcp_json: true` 后，读取项目根目录的 `.mcp.json`（Claude Code 的 `mcpServers` 格式）。省略 `type` 且有 `command` 时视为 stdio；支持 `http`，`sse` 等其他类型提示“不受支持”并跳过；command、args、url、env、headers 都展开 `${VAR}` / `${VAR:-默认值}`。优先级：[插件](plugins.md)中的服务器 < 用户 `mcp.yaml` < `.mcp.json` < 项目 `mcp.yaml`，同名整项覆盖。插件自带的 `.mcp.json` 不受这个开关限制（安装时已确认），其中可以用 `${CLAUDE_PLUGIN_ROOT}` 等[插件变量](plugins.md#变量)。`.mcp.json` 随仓库分发、会启动其中的程序，因此默认不读取；开启后，其中的服务器和项目 `mcp.yaml` 一样，要先经过[确认](#项目的-mcp-服务器需要确认)才会启动。模型不能写入或编辑 `.mcp.json`（返回 policy_protected）。

## 项目的 MCP 服务器需要确认

项目的 `.mosscode/mcp.yaml` 和（开启 `import_mcp_json` 时）`.mcp.json` 随仓库而来，stdio 服务器在会话开始时就以你的权限运行其中的命令。所以克隆一个仓库后第一次在其中启动时，这些服务器在你确认之前不会启动；用户级 `~/.mosscode/mcp.yaml` 和已安装插件中的服务器不受影响。

- **交互界面**：启动时先列出每个项目服务器要运行的命令行（`${VAR}` 不展开）、它引用的环境变量名，或远端服务器的地址（不显示其中的用户名和密码），然后询问。选“信任并启动”后记住这两个文件内容的 SHA-256，记录在 `~/.mosscode/projects/<项目键>/mcp-trust.json`；审批展示、摘要与本次加载使用同一次读取的配置快照，等待审批时替换文件不会改变本次启动的内容；任一文件有任何变化（包括 `.mcp.json` 被换掉）都会重新询问。
- **选“本次不启动”或按 Esc**：只启动用户级和插件中的服务器，项目文件中的服务器一个都不启动，也不能覆盖同名的用户级服务器；启动区说明，下次启动再次询问。
- **`-p`**：不询问。未确认的项目服务器不启动，stderr 说明原因；先在交互界面中确认一次，或加 `--trust-project-mcp`（只在本次信任，不写入记录）。
- **`moss doctor`**：指出尚未确认的项目服务器。

即使用户级 `mcp.yaml` 中设了 `import_mcp_json: true`，项目的 `.mcp.json` 也要按项目确认：这个开关表示愿意读取这种格式，不代表信任每个仓库放进去的内容。

## 安全边界与结果

**本地路径限制不隔离 MCP Server。** 外部参数的 path 不按本地文件路径检查；Server 可能在远端，也可能以当前用户权限运行，Shell 黑名单不检查其内部行为。专用本地写入/编辑不能修改 MCP 配置；用户手动修改后重启生效。

文本和 structuredContent 进入工具结果，isError 保持失败。图片内容按 [图片](tools.md#图片) 的规则处理后附在结果中，结果文本中每张图片有一行说明，例如 `server/tool 的图片 1 · 图片 · 800×600 · 120 KB`；当前模型不接受图片或数据无效时，这一行注明原因，不附带图片。其他类型明确标明未展示，不自动获取资源。参数最多 64 KiB，成功与失败正文超过 64 KiB 均截断。调用超时按 Server 的 `timeout`（默认 30 秒），审批等待不计入超时。断连、超时、取消不自动重连或重发，可能产生的外部效果不能回滚。退出关闭会话、HTTP 客户端和本程序启动的 stdio 子进程。

HTTP 支持 POST 的 JSON/SSE 响应，不打开可选 GET 通知流，不恢复中断 SSE。目前不实现采样（sampling）、roots、OAuth、健康检查或动态变更订阅。
