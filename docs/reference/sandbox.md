# 命令沙箱

审批决定一条命令能不能运行，沙箱决定它运行时能碰到什么。打开沙箱后，模型运行的命令（`execute_command` 和 Skill 的专属工具）由操作系统限制：只能写项目目录和临时目录，联网只经 MossCode 的代理、新域名由你确认，凭据目录不可读。边界由系统强制，所以沙箱中的命令不再逐条审批。

沙箱默认关闭。

## 后端

| 系统 | 后端 | 级别 | 需要 |
| --- | --- | --- | --- |
| Linux | bubblewrap | 完整：写入、读取、网络 | 安装 `bwrap`，系统允许非特权用户命名空间 |
| macOS | Seatbelt（`sandbox-exec`） | 完整 | 系统自带 |
| Windows | MXC | 完整 | 安装 `wxc-exec.exe`，Windows 11 的较新更新 |
| Windows | 受限令牌 | 部分：只限制写入 | 无 |

`sandbox.backend: auto`（默认）按上表选用本系统的第一个后端。Windows 上 MXC 不可用时改用受限令牌，启动区、`/sandbox` 和 `moss doctor` 都说明 MXC 为什么不可用；指定 `backend: mxc` 时不退回，MXC 不可用即失败关闭。状态栏在完整级别显示“沙箱”（绿色），部分级别显示“沙箱·部分”（琥珀色）。

### Linux：bubblewrap

用系统的包管理器安装：`sudo apt install bubblewrap`、`sudo dnf install bubblewrap`、`sudo pacman -S bubblewrap`。MossCode 启动时做一次自检；Ubuntu 23.10 起 AppArmor 默认限制非特权用户命名空间，在不允许嵌套命名空间的容器中也无法运行，这时自检失败并给出原因。

整个文件系统以只读方式挂载，可写目录挂回可写，凭据目录用空目录（文件用 `/dev/null`）遮住；命令有自己的 PID、IPC 和网络命名空间，网络命名空间中只有回环接口。受保护路径存在时挂成只读，它们和它们上级的目录（如 `.git`）成为挂载点，不能删除、重命名或移走。命令启动前 MossCode 用 seccomp 禁止它新建 Unix 套接字（否则可以经 Docker、ssh-agent、D-Bus 的套接字越界）。`/tmp` 是这个 MossCode 进程的私有临时目录。

### macOS：Seatbelt

不需要安装。MossCode 为每条命令生成一份 Seatbelt 配置，经 `/usr/bin/sandbox-exec -p` 运行它：默认允许，再拒绝可写目录之外的写入、受保护路径的写入（包括还不存在的）、凭据目录的读取，以及除 MossCode 代理端口外的所有网络连接（包括 Unix 套接字）。Apple 已不推荐直接使用 `sandbox-exec`，但它仍随系统提供，Claude Code、Codex CLI 和 Gemini CLI 的 macOS 沙箱也基于它。

### Windows：MXC

MXC（Microsoft eXecution Containers）是微软的进程隔离组件，MossCode 通过它的 `wxc-exec.exe` 启动命令。它不随 MossCode 安装：

1. 从 [MXC 的发布页](https://github.com/microsoft/mxc/releases) 下载 `mxc-release-binaries.zip`；
2. 把其中的 `x64/wxc-exec.exe`（ARM 设备用 `arm64/`）放到 PATH 中的目录、`~/.mosscode/bin/`，或在 `config.yaml` 的 `sandbox.wxc_exec` 中填写它的路径。

MXC 需要 Windows 11 24H2/25H2 的 26x00.9278 或 26H2 的 26300.9550 及以上更新。MossCode 第一次使用时运行 `wxc-exec --probe` 并做一次自检（在容器中写一个允许的目录和一个不允许的目录），确认隔离级别是 base-container、越界写入被拦截；通过的结果按 `wxc-exec.exe` 和 Windows 版本缓存在 `~/.mosscode/sandbox-probe.json`，之后启动不再重测。`moss doctor` 显示检查结果。

MossCode 发给 MXC 的请求关闭了遥测（`telemetry.enabled: false`）。MXC 本身的隐私说明见它的仓库。

### Windows：受限令牌

不需要安装，MXC 不可用时自动使用（或设 `sandbox.backend: token`）。命令以你的令牌的受限版本运行：Windows 只对写入检查受限令牌，所以命令能像你一样读取和联网，但只能写入 MossCode 授权的位置。MossCode 为每个项目生成一个能力 SID，在项目目录、私有临时目录和 `allow_write` 上加“允许这个 SID 修改”的访问控制项（ACE），在已存在的受保护路径上加“拒绝写入”。删除不受令牌限制，所以命令运行期间 MossCode 保持打开受保护路径，使它们和上级目录不能被删除或重命名；命令结束时，它启动的进程一起结束。

这是部分级别：读取和网络不受沙箱限制，免审批等于允许命令悄悄读取文件再发出去，所以命令仍按权限档位审批，审批面板标题注明“沙箱只限制写入”。它防的是批准过的命令写错地方：改到项目外的文件、改写 `.git/hooks` 让之后的 git 执行别的代码。

ACE 写在磁盘上，跨会话保留（项目目录的 ACE 会被其中所有文件继承，大项目第一次设置要几秒到几十秒，之后不再重复），全部记录在 `~/.mosscode/sandbox-acl.json`。不再使用时运行：

```
moss sandbox cleanup            # 移除全部
moss sandbox cleanup D:/proj    # 只移除这个项目的
```

先关闭正在使用沙箱的 MossCode 会话。`moss doctor` 显示记录了几个项目。

## 打开和关闭

- 在用户的 `config.yaml` 中设 `sandbox.mode: on`；
- 或者用 `--sandbox` 启动，本次运行打开；`--no-sandbox` 本次关闭。

打开后状态栏在权限档位后显示“沙箱”或“沙箱·部分”；后端不可用时显示“沙箱不可用”（红色），启动区说明原因。

`/sandbox` 显示当前状态、可写目录、只读保护的路径、禁止读取的路径、网络和越界设置，以及最近 20 条记录（被删除的受保护路径、疑似被拦截的命令、开关）。`/sandbox off` 确认后在本会话关闭沙箱：命令以你的完整权限运行，按权限档位审批；`/sandbox on` 重新打开。启动时没有打开沙箱的会话不能用 `/sandbox on` 打开，因为命令工具是按启动时的状态构建的（见下文的 shell），需要用 `--sandbox` 重新启动。

团队成员沿用 Lead 的沙箱状态，包括 `--sandbox`、`--no-sandbox` 和 `/sandbox` 的改动（改动后新启动的成员）。子 Agent 与主对话共用沙箱；在 git 工作树中运行的子 Agent 以工作树为可写的项目目录，并且可以写主仓库的 `.git`（提交写在那里），`.git/hooks` 和 `.git/config` 除外。

## 边界

下表是完整级别的边界；各后端的差别写在表后。

| 方面 | 沙箱中的命令 |
| --- | --- |
| 写入 | 项目目录、本进程的私有临时目录（`~/.mosscode/tmp/` 下，见 [密钥保护](providers.md#密钥保护)）、`sandbox.allow_write` 中的路径；工作树中还有主仓库的 `.git` |
| 只读保护 | 项目中的 `.mosscode`、`.claude`、`.mcp.json`，以及 `.git/hooks`、`.git/config`：它们决定之后 MossCode 和 git 会执行什么。存在时只读；不存在时命令可以创建，但命令结束后立即删除，结果中注明“命令创建了受保护的路径 …，已删除” |
| 读取 | 项目目录和可写目录；系统目录；PATH 中的目录（目录名是 `bin`、`cmd`、`Scripts` 时连同上一级）、Scoop 垫片指向的安装目录、项目 `.venv` 的基础 Python；`~/.gitconfig`、`~/.config/git`、`~/.mosscode/skills`、`~/.mosscode/plugins`；`sandbox.allow_read` 中的路径。主目录、驱动器根目录和 `AppData` 下的一级目录不会整体放行 |
| 禁止读取 | 上面放行的范围内，`~/.ssh`、`~/.aws`、`~/.azure`、`~/.kube`、`~/.docker`、`~/.config/gh`、`~/.git-credentials`、`~/.netrc`、`~/.pypirc`、`~/.cargo/credentials`、`~/.mosscode`（`skills/`、`plugins/` 除外）、当前使用的配置文件和 `sandbox.deny_read` 中的路径 |
| 网络 | 只经 MossCode 为这条命令开的代理（`HTTP_PROXY`、`HTTPS_PROXY` 指向它），新域名需要你确认，见下文的 [联网](#联网)；不能直连 IP、本机回环地址（包括本机的代理软件）和内网。`sandbox.network: none` 时完全断开，域名解析也不可用 |
| 界面 | 不能读写剪贴板，不能向其他窗口注入输入 |
| 进程 | 命令结束（或超时、被取消）时，容器中的其他进程一起结束 |

各后端的差别：

- **读取**：上表是 MXC 的允许清单。bubblewrap 和 Seatbelt 允许读取整个文件系统，只遮住禁止读取的路径（`~/.mosscode` 中命令要用的私有临时目录、`skills/`、`plugins/` 仍可读）。受限令牌不限制读取。
- **受保护路径**：MXC 和 bubblewrap 只保护已存在的，命令创建的在结束后删除；Seatbelt 连创建也拒绝；受限令牌同 MXC，另外命令运行期间不能删除或重命名它们。
- **网络**：受限令牌不限制网络，命令沿用你环境中的代理设置。
- **界面**：受限令牌与 MXC 相同，不能读写剪贴板、不能向其他窗口注入输入；bubblewrap 用新会话阻止命令向终端注入输入。

被拦截的命令通常以“拒绝访问”“无法解析主机”或代理返回的 `403` 失败。结果末尾会附上推测的原因和边界，并提示模型：确需越界时申请在沙箱外运行。

### shell

MXC 中和受限令牌下都无法启动 MSYS2 程序：Git Bash 和它带的 `sed`、`awk` 等都不能用（Git for Windows 的 `git.exe` 可以）。所以打开沙箱后，Windows 上模型的命令改用 PowerShell，工具说明中写明。Linux 和 macOS 不受影响。你在输入框中用 `!` 运行的命令和 Hook 不进沙箱，仍使用配置的 `shell`。

## 审批

| | 沙箱中的命令 | 申请在沙箱外运行 |
| --- | --- | --- |
| `default`、`acceptEdits`、`bypassPermissions` | 不询问（部分级别：与沙箱关闭时相同） | 每次询问 |
| `plan` | 只能运行只读命令，与沙箱关闭时相同 | 只读命令可以申请 |
| 显式 deny 规则、灾难级命令 | 照样拒绝 | 照样拒绝 |
| 高风险命令（如 `rm -rf`、`git push --force`） | 每次询问 | 每次询问 |

模型需要越界时（写项目外的文件、用 SSH 拉取代码、`sandbox.network: none` 时联网安装依赖等），以 `outside_sandbox: true` 重新调用 `execute_command`，并在 `justification` 中写明理由。审批面板标题注明“在沙箱外运行”，显示理由和“将以你的完整权限执行，不受沙箱的读、写、网络限制”。选项只有三个：本次在沙箱外运行；本会话允许这条命令（能推导出前缀时为这个前缀，见 [工具权限](permissions.md#审批)）在沙箱外运行，退出后失效；拒绝。越界不能永久保存：经常需要的路径写进 `sandbox.allow_write` 或 `allow_read`。

已有的 allow 规则和 `bypassPermissions` 只对沙箱中的命令生效，不会让越界申请自动通过。`sandbox.allow_unsandboxed: never` 时越界申请一律拒绝（`sandbox_required`）。无界面模式（`-p`）不等待审批，越界申请返回 `approval_required`。

## 联网

`sandbox.network: proxy`（默认）时，每条沙箱中的命令有一个只属于它的代理，命令结束即关闭。MXC 和 Seatbelt 下它监听 `127.0.0.1` 的随机端口，命令只能连接这个端口；bubblewrap 下它监听私有临时目录中的 Unix 套接字，命令的网络命名空间中的 `127.0.0.1:3128` 转发到它，`HTTP_PROXY`、`HTTPS_PROXY` 指向这个端口。受限令牌不使用代理。命令经代理连接一个域名时：

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

沙箱打开但不能用时（找不到 `bwrap` 或 `wxc-exec.exe`、系统不允许用户命名空间、自检失败、指定的后端不属于本系统），MossCode 不会悄悄改为不带沙箱运行：模型的命令和 Skill 工具返回 `sandbox_unavailable`，说明原因和解决方法。可以修好后重新启动，或用 `/sandbox off`（无界面模式用 `--no-sandbox`）在本次不用沙箱。后端没能启动某条命令时（MXC 报错，受限令牌无法设置访问控制等），这条命令不运行，结果中给出原因。

## 配置

写在用户的 `config.yaml` 中：

```yaml
sandbox:
  mode: on                  # off（默认）/ on
  backend: auto             # auto（默认）/ bubblewrap / seatbelt / mxc / token
  allow_write: [D:/cache]   # 另外可写的路径
  allow_read: [D:/sdk]      # 项目外另外可读的路径
  deny_read: [~/secrets]    # 另外禁止读取的路径（在可读范围内时生效）
  allow_unsandboxed: ask    # ask（默认）：越界申请询问；never：一律拒绝
  network: proxy            # proxy（默认）：经 MossCode 的代理联网，新域名需确认；none：断开
  wxc_exec: D:/tools/wxc-exec.exe  # 不在 PATH 和 ~/.mosscode/bin 时填写
```

## 限制

- MXC 下每条命令多出约 0.2–0.4 秒启动容器的时间；其他后端只多出启动一个 Python 启动器的时间（受限令牌第一次为项目设置访问控制除外）。
- 命令启动的后台进程（如开发服务器）随命令结束。
- MXC 的读取是允许清单，项目的上级目录不可读。会向上查找配置的工具在项目嵌套于另一个项目中时可能失败：例如 pytest 找到上级目录的 `pyproject.toml` 后读取被拒，以 `PermissionError` 退出。把需要的文件加进 `sandbox.allow_read`，或在项目中放一份自己的配置（pytest 找到项目内的 `pytest.ini`、`pyproject.toml` 等就不再向上查找）。
- Windows PowerShell 的部分命令被拒绝后仍以退出码 0 结束，这时 MossCode 依据错误输出附上同样的沙箱说明。
- 受保护路径只在命令结束后检查：命令运行期间创建的 `.git/hooks/pre-commit` 等会在结束时删除，但同一条命令中紧接着运行的 git 可能已经执行了它。`.git/hooks` 不存在时 MossCode 会在运行命令前创建它，使它处于只读保护之下。
- 代理模式下 MXC 容器中的域名解析可用（MXC 的这种网络模式不拦截 DNS，查询会到达外部的 DNS 服务器）：命令不能直连任何地址，但可以通过解析特意构造的域名带出少量数据。在意这一点时设 `sandbox.network: none`。
- 代理模式下 MXC 要求放开容器的入站连接：命令运行期间启动的监听可以被本机和局域网连接到。
- 等待你回答时命令仍在运行，客户端自己的超时（如 `pip` 的 15 秒）可能先到，回答后让模型重新运行即可；选“本会话”可以避免同一个域名再问。
- bubblewrap 和 Seatbelt 下命令中没有域名解析，只能经代理联网；自己先解析域名、不读代理变量的程序连不上。`git` 的 SSH 地址和需要 Unix 套接字的程序（Docker、ssh-agent）都不可用。
- 受限令牌下命令能删除项目外你有权删除的文件，或把它们移进项目（删除和移出不受令牌限制）；只有受保护路径在命令运行期间被保持打开而不能删除。它也不限制读取和网络，所以命令仍需审批。
- 受限令牌把 Everyone 列为受限 SID，Everyone 可写的目录命令也能写：MossCode 检查项目的上级目录、PATH 中的目录和主目录、`C:\Users\Public`、`C:\ProgramData` 下的一级目录，发现时在启动区、`/sandbox` 和 `moss doctor` 中提示。
- macOS 后端还没有在真实的 Mac 上验证过；遇到问题请用 `--no-sandbox` 并报告。
- 只限制模型运行的命令。专用的文件工具（`write_file`、`edit_file`）仍由应用层的 [路径限制](permissions.md#路径限制) 和审批约束；MCP Server 和 Hook 不在沙箱中。
- 项目目录的 `.mosscode/config.yaml` 存在时会整份代替用户配置（见 [配置](../../README.md#配置)），其中的 `sandbox` 同样生效；不要在不信任的仓库中使用项目级配置。
