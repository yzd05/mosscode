# 命令行

`moss` 和 `mosscode` 是同一个命令。安装见 [README](../../README.md#安装)。

```text
moss [选项] [PROMPT]
moss setup | doctor
```

## 启动方式

| 写法 | 行为 |
| --- | --- |
| `moss` | 在当前目录启动交互界面 |
| `moss "PROMPT"` | 启动交互界面，就绪后像输入一样发送这条消息；与 `-c`、`--resume ID` 一起用时，在恢复的会话中发送 |
| `moss -p "任务"` | 无界面执行一次任务后退出，见 [模型提供商](providers.md#无界面模式与输出格式) |
| `moss "任务" -p` | 同上：`-p` 不带值时，位置参数就是任务 |
| `命令 \| moss -p "任务"` | 管道输入空一行后附在任务之后；只有管道输入（`命令 \| moss -p`）时它就是任务 |
| `moss -- doctor` | 把 `doctor`、`setup` 这样的词当作消息发送，而不是运行子命令 |

交互界面需要终端：标准输入被重定向时（例如管道）只能用 `-p`，否则以退出码 2 结束。

## 选项

| 选项 | 说明 |
| --- | --- |
| `-v`、`--version` | 显示 `mosscode <版本>` 后退出；版本来自安装的包元数据 |
| `-h`、`--help` | 显示帮助、子命令和例子 |
| `-p`、`--prompt [任务]` | 无界面模式 |
| `-c`、`--continue` | 恢复本项目最近活动的会话 |
| `--resume [ID]` | 恢复指定会话；不带 ID 时在交互界面中打开会话列表（不能同时给 PROMPT，`-p` 中必须带 ID） |
| `--config 路径` | 使用这个配置文件，不按默认顺序查找 |
| `--provider 名称` | 指定 Provider，跳过启动选择；无界面模式默认用 `default_provider` |
| `--permission-mode 档位` | `default`、`acceptEdits`、`plan`、`bypassPermissions`，见 [权限](permissions.md) |
| `--output-format 格式` | 只用于 `-p`：`text`（默认）、`json`、`stream-json` |
| `--show-usage` | `text` 格式在 stderr 显示每次请求的 Token 与缓存用量 |
| `--list-providers` | 列出配置中的 Provider，不发请求 |
| `--no-hooks` | 本次运行不加载 Hook |
| `--trust-project-hooks` | 本次运行信任项目级 Hook 文件，不写入确认记录 |

`--team-dir`、`--team-member` 是团队成员在分屏窗格中启动时用的内部参数，不在帮助中显示。

## 子命令

### setup

`moss setup` 在终端中逐项询问：协议（Anthropic Messages、OpenAI Responses、OpenAI Chat Completions）、服务地址 `base_url`（回车使用协议的官方地址，提问中写明是哪个；选 OpenAI Chat Completions 时提示填写兼容服务的地址，例如 `https://api.deepseek.com`）、模型名、上下文窗口（如 `200k`，回车按 128K 估算）、凭据。凭据有两种保存方式：

- 环境变量（默认）：配置中只写变量名，按协议默认 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY`，给了服务地址时按域名取名，如 `DEEPSEEK_API_KEY`。变量还没设置时，写完后显示 PowerShell 的设置方法。
- 直接输入密钥：输入时不显示，以明文写入配置文件。

写入前显示汇总（密钥只显示“已设置”），确认后写入 `~/.mosscode/config.yaml`，并用与启动相同的规则校验。Provider 名称取自服务地址的域名（如 `deepseek`），没有地址时按协议取 `claude`、`openai` 或 `compat`，并设为 `default_provider`。

- 文件已存在时不修改它，只显示位置；追加 Provider 请手动编辑。
- 按 Ctrl+C 或在确认时回答 n，不写入任何文件。
- 交互启动找不到任何配置时（没有 `--config`，且在终端中），`moss` 自动进入同样的引导，写入且凭据可用后直接进入界面。`-p`、`--list-providers` 和非终端仍然报错。

### doctor

`moss doctor [--config 路径]` 只读检查，不启动会话，不发送请求。每项一行，以 ✓、!（警告）、✗（错误）开头，! 和 ✗ 的下一行是修复建议：

- 版本、解释器、安装方式（uv tool、pipx、虚拟环境，是否可编辑安装）；
- PATH 中的 `moss`、`mosscode`：找不到、不是当前运行的这一个、或有多个时提示；
- 生效的配置文件和每个 Provider 的协议、模型、服务地址的主机名、凭据状态（“已设置”“环境变量 X 未设置”），从不显示密钥；工作目录下有不会被读取的 `config.yaml` 时提示；
- 网络搜索服务（`web_search`）及其凭据状态；未配置时说明搜索不可用，读取网页不受影响；
- `~/.mosscode` 是否可写；
- ripgrep、git，Windows 上的 Git Bash 和 Windows Terminal（`wt.exe`，团队分屏窗格需要）；
- 当前目录 `.mosscode/` 下的权限、MCP 和 Hook 文件能否解析。

## 配置查找

未指定 `--config` 时依次查找，第一个存在的生效：

1. `./.mosscode/config.yaml`（项目级）
2. `~/.mosscode/config.yaml`（用户级）

工作目录下的 `config.yaml` 不会被读取；需要时用 `--config` 指定。

## 退出码

| 命令 | 退出码 |
| --- | --- |
| `moss`、`moss -p` | 0 完成；1 配置、权限或连接错误，或 `json` 输出中 `is_error` 为真；2 参数错误、`-p` 没有任务、`-p` 中的本地命令或未知命令、交互界面没有终端 |
| `moss setup` | 0 写入完成或配置已存在；1 写入或校验失败；2 不在终端中；130 中途退出 |
| `moss doctor` | 0 没有 ✗；1 有 ✗ |

## 启动速度

`--help`、`--version`、参数错误和子命令在加载界面与模型 SDK 之前处理；会话只加载所选协议的 SDK（Anthropic Messages 只加载 `anthropic`，两种 OpenAI 协议只加载 `openai`）。
