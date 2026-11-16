# ACP 编辑器集成

MossCode 支持 [Agent Client Protocol (ACP)](https://agentclientprotocol.com/)，可通过标准输入/输出 JSON-RPC 2.0 接入 Zed、JetBrains IDE、Neovim 等支持 ACP 的客户端，并配套提供面向 ACP Registry（`uvx` 方式）的分发清单 [agent.json](../../registry/mosscode/agent.json)。

命令行选项与基础配置查找规则见 [命令行](cli.md)。

## 启动与编辑器配置

启动子命令为 `moss acp`（兼容 `mosscode acp`）：

```bash
moss acp [选项]
```

如果通过 `uvx` 独立运行，可直接配置：

```json
{
  "command": "uvx",
  "args": ["mosscode", "acp"]
}
```

可用命令行选项与 [命令行选项](cli.md#选项) 中的无界面信任/配置参数对应：

- `--config <路径>`：指定配置文件路径
- `--provider <名称>`：默认优先使用的 Provider 名称
- `--permission-mode <档位>`：初始权限档位（`default`、`acceptEdits`、`plan`、`bypassPermissions`）
- `--no-hooks`：本次运行不加载 Hook
- `--sandbox` / `--no-sandbox`：覆盖配置中的命令沙箱开关
- `--trust-project-config` / `--trust-project-mcp` / `--trust-project-hooks` / `--trust-project-plugins`：信任当前工作区对应的项目级配置（语义与 `-p` 相同）

ACP 模式下，`stdout` 严格保留为 UTF-8 换行分隔的 JSON-RPC 2.0 报文，内部诊断信息统一输出至 `stderr`。

## 认证方式

`initialize` 响应按客户端能力声明 `authMethods`：

1. `moss-setup-terminal`（客户端声明 `auth.terminal` 时包含）：在支持 Terminal Auth 的编辑器内拉起 `moss setup`，通过终端交互写入 `~/.mosscode/config.yaml` 与系统凭据管理器；
2. `local-config`（始终包含，并附带 `_meta.terminal-auth` 扩展信息）：由服务端按用户目录及已确认的会话工作目录重新扫描配置文件与凭据（支持环境变量、系统凭据管理器及 `follow: claude-code` / `codex`）。

当本地已有可用配置和凭据时，客户端可直接发起 `session/new` / `session/load`，无需重复弹窗；缺少配置或凭据时，`session/new`、`session/load`、`session/resume` 与 `session/list` 会返回 JSON-RPC 错误码 `-32000`（Authentication required）。

## 会话能力与安全边界

- **多会话工作区隔离**：同一 ACP 进程下可打开不同目录的多个会话，每个会话显式绑定 `cwd` 解析路径，不修改进程工作目录。
- **模式与配置联动**：同时暴露 `modes` 与 `configOptions`（`mode`、`model`、`thought_level`），并可在 `exit_plan_mode` 批准后主动同步模式变更通知。
- **工具与 Diff 展示**：所有文件写入与命令执行继续走 MossCode 内置 `ToolRegistry` 与 [命令沙箱](sandbox.md)（不委派给客户端 `fs/write_text_file` 或 `terminal/create`），通过标准 `tool_call` / `tool_call_update` 的 `locations` 与结构化 `Diff` 在编辑器内展示差异、跳转行号和接收审批。
- **任务清单与历史重放**：`todo_write` 实时触发 `plan` 通知；`session/load` / `session/resume` / `session/list` / `session/close` 复用原有 [会话存档](sessions.md) 机制。
- **客户端与项目信任**：编辑器传入的 `mcpServers` 支持 `stdio` 与 `http` 传输；未经确认的项目级 `config.yaml`、`mcp.yaml`/`.mcp.json`、`hooks.yaml` 与项目插件默认跳过执行。对 `file://` URI 内嵌上下文执行受保护凭据目录与显式 `deny` 规则检查。
- **交互询问 (`ask_user`)**：单选题通过 `session/request_permission` 逐题确认；含 `multi_select` 的多选题会在对话中给出可读选项提示，交由用户在下一条消息中自由回答。
