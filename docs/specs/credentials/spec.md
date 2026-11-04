# 密钥不外泄

日期：2026-11-04。状态：已实现，已验收（2026-11-04，M153–M157）。

来源：
- [产品差距评审](../../research/product-gap-review.md)的 A2（系统凭据管理器）和 R5（`keyring`），属于“完善十一：日常顺手”。
- 讨论中发现：环境变量和配置文件里的密钥都能经由模型执行的命令或读取的文件进入对话。用户的要求是“只要可以不泄露即可”，方案由我决定（2026-11-04）。

## 问题

密钥只能放在环境变量（`api_key_env`）或明文写进配置（`api_key`）。两种都会被模型顺手带出去：

- 模型为了排查问题执行 `env`，输出里就有 `DEEPSEEK_API_KEY=sk-...`；这段输出发给模型服务、写进会话存档、显示在屏幕上。
- 配置文件在项目目录中时，`read_file config.yaml` 或免审批的 `cat config.yaml` 直接读出明文密钥。

此外，Windows 上设置环境变量要用 `setx` 或系统设置，只对新开的终端生效，密钥还以明文存在注册表里。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `tools/process.py`、`tools/shell.py` | `run_process` 是 `execute_command`、`!` 命令、Hook 命令、Skill 工具和 `rg` 的共同入口；`env=None` 时继承全部环境变量，Windows 的 Git Bash 传 `{**os.environ, ...}` |
| F2 | `mcp` SDK | stdio 服务器只继承 PATH、HOME 等少数变量，加上配置中的 `env`，不会拿到 MossCode 的密钥变量 |
| F3 | `teams/backends.py` | 团队成员用 `subprocess.run` 启动另一个 MossCode 进程，自己读取配置和密钥，需要继承环境变量 |
| F4 | `tools/base.py` | 所有工具结果在 `ToolRegistry._invoke` 中生成；`_bound` 把超长输出写到会话目录的文件里 |
| F5 | `agent.py` | `_append` 是消息进入历史和存档的唯一入口（`@` 附件、`!` 命令、Hook 上下文、子 Agent 结果都经过它） |
| F6 | `keyring` 25.7 | MIT；依赖 jaraco.*、more-itertools（MIT），Windows 上 pywin32-ctypes（BSD-3），Linux 上 SecretStorage（BSD-3）、jeepney（MIT）。Windows 后端是凭据管理器，macOS 是钥匙串，Linux 是 Secret Service；没有可用后端时抛出 `NoKeyringError` |
| F7 | `setup.py` | `moss setup` 只创建新配置，从不修改已有文件 |

## 需求

| # | 要求 | 验收 |
| --- | --- | --- |
| R1 | 配置中所有 `api_key_env`（各 Provider 和 `web_search`）指向的变量，不传给 `execute_command`、`!` 命令、Hook 命令和 Skill 工具启动的进程；MossCode 自己和团队成员进程照常读取 | 设置 `api_key_env: DEEPSEEK_API_KEY` 后，`!env` 的输出没有这个变量 |
| R2 | MossCode 知道的每个密钥（明文 `api_key`、环境变量的值、凭据管理器中的值、名称像凭据的 `headers` 值），只要完整出现在工具结果（输出和详情）或进入历史的用户侧消息中，就替换为 `[已隐藏的密钥]`；替换发生在超长输出写入文件之前。少于 8 个字符的值不替换 | `cat config.yaml` 的结果里看不到明文密钥 |
| R3 | `write_file` 的内容或 `edit_file` 的 `new_string` 含有 `[已隐藏的密钥]` 时拒绝执行，避免模型把读到的占位符写回文件、覆盖真正的密钥 | 读取后整体重写配置文件被拒绝 |
| R4 | Provider 和 `web_search` 可以写 `api_key_keyring: 名称`，从系统凭据管理器（服务名 `mosscode`）读取密钥；与 `api_key`、`api_key_env` 三选一。找不到时报错并提示 `moss key set 名称`；系统没有可用的凭据管理器时说明原因，建议改用环境变量 | 缺少条目时的报错 |
| R5 | `moss key set 名称` 在终端中不回显地读入密钥（stdin 不是终端时读第一行）并保存；`moss key delete 名称` 删除。两者都不显示密钥 | 保存后 `moss doctor` 显示“系统凭据管理器 名称 已设置” |
| R6 | `moss setup` 的凭据保存方式增加“系统凭据管理器”，有可用后端时作为推荐的第一项；选择后保存密钥，配置只写 `api_key_keyring` | 向导写出的配置能直接启动 |
| R7 | 配置文件位于项目目录中且含有明文 `api_key` 时，启动时提示一次：模型读取时会隐藏，但建议改用 `moss key set` | 用户现在的配置会看到提示 |
| R8 | `moss doctor` 显示凭据管理器条目是否存在 | |

## 设计

- **`credentials.py`（新）**：
  - 进程级的两组数据：要从子进程环境中去掉的变量名、要隐藏的密钥值。`protect(settings)` 在读取配置后登记（`cli.run`、团队成员进程），凭据管理器中的值也一并读出登记；`ProviderConfig.credential()` 每次返回前再登记一次。
  - `child_environment(env)`：返回去掉这些变量后的环境（Windows 上变量名不区分大小写）；没有登记时原样返回。
  - `redact(text)`、`redact_details(value)`：按长度从长到短替换。
  - `keyring_get/set/delete`、`keyring_problem()`：包住 `keyring`，测试中替换为内存实现。
- **接入点**：`run_process` 调用 `child_environment`；`ToolRegistry._invoke` 在 `_bound` 之前隐藏输出和详情；`agent._append` 隐藏用户和工具消息的文本；`user_command` 隐藏界面显示的输出；`write_file`/`edit_file` 检查占位符。
- **配置**：`ProviderConfig`、`WebSearchConfig` 增加 `api_key_keyring`；`credential()` 按 `api_key_env` → `api_key_keyring` → `api_key` 读取。
- **命令**：`moss key`（`key_command.py`），注册到 `launcher.SUBCOMMANDS`。
- **依赖**：`keyring>=25,<26` 作为默认依赖。理由：向导面向新手，推荐项不能要求先安装额外的包；纯 Python，许可证见 F6。

## 不做

- 拦截读取配置文件：读取的方式太多（`cat`、`grep`、脚本），用 R2 统一处理输出。
- 密钥被截断、编码（base64、URL 编码）或拆开后出现：只识别完整的原文。
- 防范以当前用户身份运行、专门窃取密钥的程序：它可以直接调用系统接口读取凭据管理器。
- MCP 配置中 `${VAR}` 展开的令牌、`config.yaml` 中其他字段里的秘密。
- 自动迁移已有的明文密钥：`moss setup` 不改已有配置，迁移由用户运行 `moss key set` 后改一行配置完成。

## 验收

自动测试覆盖 R1–R8，凭据管理器用内存实现。手动验收见 [docs/acceptance.md](../../acceptance.md)。
