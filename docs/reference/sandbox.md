# 命令沙箱

审批决定一条命令能不能运行，沙箱决定它运行时能碰到什么。打开沙箱后，模型运行的命令（`execute_command` 和 Skill 的专属工具）由操作系统限制：只能写项目目录和临时目录，联网只经 MossCode 的代理、新域名由你确认，项目外只能读取系统目录和工具目录。边界由系统强制，所以沙箱中的命令不再逐条审批。

沙箱默认关闭。目前只有 Windows 有后端（MXC）；Linux 和 macOS 上打开沙箱时，模型的命令都会被拒绝，并说明原因。

## 安装 MXC

MXC（Microsoft eXecution Containers）是微软的进程隔离组件，MossCode 通过它的 `wxc-exec.exe` 启动命令。它不随 MossCode 安装：

1. 从 [MXC 的发布页](https://github.com/microsoft/mxc/releases) 下载 `mxc-release-binaries.zip`；
2. 把其中的 `x64/wxc-exec.exe`（ARM 设备用 `arm64/`）放到 PATH 中的目录、`~/.mosscode/bin/`，或在 `config.yaml` 的 `sandbox.wxc_exec` 中填写它的路径。

MXC 需要 Windows 11 24H2/25H2 的 26x00.9278 或 26H2 的 26300.9550 及以上更新。MossCode 第一次使用时运行 `wxc-exec --probe` 并做一次自检（在容器中写一个允许的目录和一个不允许的目录），确认隔离级别是 base-container、越界写入被拦截；通过的结果按 `wxc-exec.exe` 和 Windows 版本缓存在 `~/.mosscode/sandbox-probe.json`，之后启动不再重测。`moss doctor` 显示检查结果。

MossCode 发给 MXC 的请求关闭了遥测（`telemetry.enabled: false`）。MXC 本身的隐私说明见它的仓库。

## 打开和关闭

- 在用户的 `config.yaml` 中设 `sandbox.mode: on`；
- 或者用 `--sandbox` 启动，本次运行打开；`--no-sandbox` 本次关闭。

打开后状态栏在权限档位后显示“沙箱”（绿色）；后端不可用时显示“沙箱不可用”（红色），启动区说明原因。

`/sandbox` 显示当前状态、可写目录、只读保护的路径、禁止读取的路径、网络和越界设置，以及最近 20 条记录（被删除的受保护路径、疑似被拦截的命令、开关）。`/sandbox off` 确认后在本会话关闭沙箱：命令以你的完整权限运行，按权限档位审批；`/sandbox on` 重新打开。启动时没有打开沙箱的会话不能用 `/sandbox on` 打开，因为命令工具是按启动时的状态构建的（见下文的 shell），需要用 `--sandbox` 重新启动。

团队成员沿用 Lead 的沙箱状态，包括 `--sandbox`、`--no-sandbox` 和 `/sandbox` 的改动（改动后新启动的成员）。子 Agent 与主对话共用沙箱；在 git 工作树中运行的子 Agent 以工作树为可写的项目目录，并且可以写主仓库的 `.git`（提交写在那里），`.git/hooks` 和 `.git/config` 除外。

## 边界

| 方面 | 沙箱中的命令 |
| --- | --- |
| 写入 | 项目目录、本进程的私有临时目录（`~/.mosscode/tmp/` 下，见 [密钥保护](providers.md#密钥保护)）、`sandbox.allow_write` 中的路径；工作树中还有主仓库的 `.git` |
| 只读保护 | 项目中的 `.mosscode`、`.claude`、`.mcp.json`，以及 `.git/hooks`、`.git/config`：它们决定之后 MossCode 和 git 会执行什么。存在时只读；不存在时命令可以创建，但命令结束后立即删除，结果中注明“命令创建了受保护的路径 …，已删除” |
| 读取 | 项目目录和可写目录；系统目录；PATH 中的目录（目录名是 `bin`、`cmd`、`Scripts` 时连同上一级）、Scoop 垫片指向的安装目录、项目 `.venv` 的基础 Python；`~/.gitconfig`、`~/.config/git`、`~/.mosscode/skills`、`~/.mosscode/plugins`；`sandbox.allow_read` 中的路径。主目录、驱动器根目录和 `AppData` 下的一级目录不会整体放行 |
| 禁止读取 | 上面放行的范围内，`~/.ssh`、`~/.aws`、`~/.azure`、`~/.kube`、`~/.docker`、`~/.config/gh`、`~/.git-credentials`、`~/.netrc`、`~/.pypirc`、`~/.cargo/credentials`、`~/.mosscode`（`skills/`、`plugins/` 除外）、当前使用的配置文件和 `sandbox.deny_read` 中的路径 |
| 网络 | 只经 MossCode 为这条命令开的代理（`HTTP_PROXY`、`HTTPS_PROXY` 指向它），新域名需要你确认，见下文的 [联网](#联网)；不能直连 IP、本机回环地址（包括本机的代理软件）和内网。`sandbox.network: none` 时完全断开，域名解析也不可用 |
| 界面 | 不能读写剪贴板，不能向其他窗口注入输入 |
| 进程 | 命令结束（或超时、被取消）时，容器中的其他进程一起结束 |

被拦截的命令通常以“拒绝访问”“无法解析主机”或代理返回的 `403` 失败。结果末尾会附上推测的原因和边界，并提示模型：确需越界时申请在沙箱外运行。

### shell

MXC 中无法启动 MSYS2 程序：Git Bash 和它带的 `sed`、`awk` 等都不能用（Git for Windows 的 `git.exe` 可以）。所以打开沙箱后，Windows 上模型的命令改用 PowerShell，工具说明中写明。你在输入框中用 `!` 运行的命令和 Hook 不进沙箱，仍使用配置的 `shell`。

## 审批

| | 沙箱中的命令 | 申请在沙箱外运行 |
| --- | --- | --- |
| `default`、`acceptEdits`、`bypassPermissions` | 不询问 | 每次询问 |
| `plan` | 只能运行只读命令，与沙箱关闭时相同 | 只读命令可以申请 |
| 显式 deny 规则、灾难级命令 | 照样拒绝 | 照样拒绝 |
| 高风险命令（如 `rm -rf`、`git push --force`） | 每次询问 | 每次询问 |

模型需要越界时（写项目外的文件、用 SSH 拉取代码、`sandbox.network: none` 时联网安装依赖等），以 `outside_sandbox: true` 重新调用 `execute_command`，并在 `justification` 中写明理由。审批面板标题注明“在沙箱外运行”，显示理由和“将以你的完整权限执行，不受沙箱的读、写、网络限制”。选项只有三个：本次在沙箱外运行；本会话允许这条命令（能推导出前缀时为这个前缀，见 [工具权限](permissions.md#审批)）在沙箱外运行，退出后失效；拒绝。越界不能永久保存：经常需要的路径写进 `sandbox.allow_write` 或 `allow_read`。

已有的 allow 规则和 `bypassPermissions` 只对沙箱中的命令生效，不会让越界申请自动通过。`sandbox.allow_unsandboxed: never` 时越界申请一律拒绝（`sandbox_required`）。无界面模式（`-p`）不等待审批，越界申请返回 `approval_required`。

## 联网

`sandbox.network: proxy`（默认）时，每条沙箱中的命令有一个只属于它的代理，监听 `127.0.0.1` 的随机端口，命令结束即关闭；MXC 只允许容器连接这个端口。命令经它连接一个域名时：

1. 先按 `WebFetch(domain:…)` 规则和权限档位判断，与 `web_fetch` 相同：deny 规则拒绝，allow 规则或 `bypassPermissions` 放行；
2. 否则弹出审批面板，标题为“沙箱中的命令联网”，显示命令和要连接的主机、端口。选项：本次允许（只在这条命令中）；本会话允许这个域名（命令和 `web_fetch` 都可以访问）；永久允许，在 `.mosscode/permissions.local.yaml` 中保存为 `WebFetch(domain:…)`；拒绝；
3. 无界面模式（`-p`）和后台子 Agent 不询问，没有规则的域名一律拒绝。

一条命令对同一个域名只问一次，同时发起的连接等同一个回答。等待回答时命令已经结束（例如超时），审批面板自动撤下。被拒绝的连接得到 `403`；命令结果末尾列出“联网：已连接 …”和“联网：… 被拒绝（原因）”，被拒绝的也记入 `/sandbox`。

代理另外检查：

- IP 字面量和 `localhost` 等本机名直接拒绝，不询问；域名解析到本机或内网地址时拒绝（fake-ip 段视为公网，与 `web_fetch` 相同），并连接检查过的那个地址；
- HTTPS 隧道建立后，TLS 握手中的主机名（SNI）与申请的域名不同则断开；
- 只接受 `CONNECT` 和 `http://` 绝对地址的请求。

MossCode 自己的环境中有 `HTTPS_PROXY`（或 `ALL_PROXY`，`NO_PROXY` 覆盖的主机除外）时，代理经它连接，域名解析也交给它；只支持 `http://` 的上游，可以带用户名和密码。命令看不到这个上游地址。

`pip`、`npm`、`git`（HTTPS 地址）、`curl.exe`、Python 的 `urllib` 和 `requests` 读取 `HTTPS_PROXY`；Windows PowerShell 5.1 的 `Invoke-WebRequest` 不读，MossCode 在每条命令前设置 .NET 的默认代理，让它也经过代理。git 的 SSH 地址（`git@github.com:…`）不走 HTTP 代理，在沙箱中不通。Skill 的专属工具仍然断网。

本机的 MXC 不支持这种模式（`wxc-exec --probe` 的 `baseContainerSupportsIdentitylessLoopbackProxy`）时，网络退回断开，`/sandbox` 和启动区说明原因。

## 失败关闭

沙箱打开但不能用时（找不到 `wxc-exec.exe`、系统版本不支持、自检失败、其他系统），MossCode 不会悄悄改为不带沙箱运行：模型的命令和 Skill 工具返回 `sandbox_unavailable`，说明原因和解决方法。可以修好后重新启动，或用 `/sandbox off`（无界面模式用 `--no-sandbox`）在本次不用沙箱。MXC 没能启动某条命令时，结果中给出 MXC 报告的错误。

## 配置

写在用户的 `config.yaml` 中：

```yaml
sandbox:
  mode: on                  # off（默认）/ on
  allow_write: [D:/cache]   # 另外可写的路径
  allow_read: [D:/sdk]      # 项目外另外可读的路径
  deny_read: [~/secrets]    # 另外禁止读取的路径（在可读范围内时生效）
  allow_unsandboxed: ask    # ask（默认）：越界申请询问；never：一律拒绝
  network: proxy            # proxy（默认）：经 MossCode 的代理联网，新域名需确认；none：断开
  wxc_exec: D:/tools/wxc-exec.exe  # 不在 PATH 和 ~/.mosscode/bin 时填写
```

## 限制

- 每条命令多出约 0.2–0.4 秒启动容器的时间。
- 命令启动的后台进程（如开发服务器）随命令结束。
- 读取是允许清单，项目的上级目录不可读。会向上查找配置的工具在项目嵌套于另一个项目中时可能失败：例如 pytest 找到上级目录的 `pyproject.toml` 后读取被拒，以 `PermissionError` 退出。把需要的文件加进 `sandbox.allow_read`，或在项目中放一份自己的配置（pytest 找到项目内的 `pytest.ini`、`pyproject.toml` 等就不再向上查找）。
- Windows PowerShell 的部分命令被拒绝后仍以退出码 0 结束，这时 MossCode 依据错误输出附上同样的沙箱说明。
- 受保护路径只在命令结束后检查：命令运行期间创建的 `.git/hooks/pre-commit` 等会在结束时删除，但同一条命令中紧接着运行的 git 可能已经执行了它。`.git/hooks` 不存在时 MossCode 会在运行命令前创建它，使它处于只读保护之下。
- 代理模式下容器中的域名解析可用（MXC 的这种网络模式不拦截 DNS，查询会到达外部的 DNS 服务器）：命令不能直连任何地址，但可以通过解析特意构造的域名带出少量数据。在意这一点时设 `sandbox.network: none`。
- 代理模式下 MXC 要求放开容器的入站连接：命令运行期间启动的监听可以被本机和局域网连接到。
- 等待你回答时命令仍在运行，客户端自己的超时（如 `pip` 的 15 秒）可能先到，回答后让模型重新运行即可；选“本会话”可以避免同一个域名再问。
- 只限制模型运行的命令。专用的文件工具（`write_file`、`edit_file`）仍由应用层的 [路径限制](permissions.md#路径限制) 和审批约束；MCP Server 和 Hook 不在沙箱中。
- 项目目录的 `.mosscode/config.yaml` 存在时会整份代替用户配置（见 [配置](../../README.md#配置)），其中的 `sandbox` 同样生效；不要在不信任的仓库中使用项目级配置。
