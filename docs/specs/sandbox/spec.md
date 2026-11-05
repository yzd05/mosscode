# 命令沙箱

日期：2026-11-05。状态：13A（控制面加固 H1–H5）已实现；13B（Windows 探测）已完成，结果见 F14–F21，Q1、Q3、Q11 已据此决定；其余待定问题在实现时按“推荐”一栏处理（2026-11-05）。

来源：
- 用户的要求（2026-11-05）：调研系统级沙箱的设计，参考 Claude Code、Codex、Gemini CLI、DeepSeek Harness 等成熟项目，然后写规格。
- 调研报告：[Coding Agent 沙箱调研与 MossCode 的 Windows 路线](../../research/sandbox-designs.md)，各项目的笔记在 [sandbox-notes/](../../research/sandbox-notes/)。下文的“报告”指这份调研。
- [产品差距评审](../../research/product-gap-review.md)的 E2 当时决定“不自己实现系统沙箱，推荐容器”，容器文档已在 [在容器中运行](../../reference/container.md) 完成。本规格推翻其中“不实现”的部分：调研表明 macOS、Linux 已有成熟做法，Windows 在 2026 年第四季度有了 MXC，“做不好反而给人错误的安全感”的风险用强制级别（S8）显式处理。容器文档继续保留，作为最强隔离的选项。

## 问题

1. **确认过的命令能做任何事。** 命令以用户的完整权限运行：能读 `~/.ssh`、浏览器数据，能把项目文件发到网上，能改项目外的文件。权限文档自己写着“这是应用层路径限制，不是系统级沙箱”。
2. **命令能改写 MossCode 自己的控制面。** 文件工具不能写 `.mosscode/permissions.yaml` 等文件（`policy_protected`），但一条 `echo … >> ~/.mosscode/permissions.yaml` 能给自己加 allow 规则；团队成员的审批答复 `permission_response` 放在 `~/.mosscode/projects/<项目键>/teams/` 的邮箱文件里，命令能伪造它；`.git/hooks` 中写入的脚本会在用户下次 `git commit` 时以用户身份运行。审批面板挡住的只是“模型直接这么做”，挡不住“模型让命令这么做”。
3. **审批太频繁。** 跑测试、装依赖、构建都要逐条确认，用户要么一直点“允许”，要么切到 `bypassPermissions` 放弃检查。成熟的 Agent 已经改成“边界内的命令不问，越界才问”，前提是边界由操作系统强制。
4. **容器方案对多数人太重。** 需要 Docker、镜像和挂载，不隔离网络，用户的 Windows Home 上 Docker Desktop 也起不来（F11）。

## 已确认的事实

| # | 位置 | 内容 |
| --- | --- | --- |
| F1 | `tools/process.py` | `run_process(args, cwd, *, check, limit, tail, env, input)` 是 `execute_command`、Hook 命令（`hooks/actions.py`）、Skill 专属工具（`tools/skill.py`）、内置搜索调用 rg（`tools/search.py`）和用户的 `!` 命令（`user_command.py`）共用的启动路径。Windows 上先用一个 Python 闸门进程（读 1 字节再 exec）把进程放进 Job Object（只设 `KILL_ON_JOB_CLOSE`），`CREATE_NO_WINDOW`；其他系统 `start_new_session`。环境经 `credentials.child_environment(env)`。命令非交互：没有 PTY，stdin 为空或管道 |
| F2 | `tools/process.py` | `execute_command` 的参数只有 `command`、`cwd`、`timeout`（默认 120 秒，最大 600），只在前台运行，没有后台 shell；结束时 `terminate_tree` 结束整个进程树 |
| F3 | 其他启动点 | 不经过 `run_process` 的子进程：`clipboard.py`、`mentions.py`、`plugin_command.py`（git）、`prompts.py`、`teams/backends.py`、`teams/pane.py`、`teams/runtime.py`、`worktree/git.py`，以及 `mcp_client.py` 启动的 MCP stdio 服务器。这些由 MossCode 自己或用户的配置发起，不是模型写的命令 |
| F4 | `tools/shell.py` | Windows 上优先 Git Bash，找不到时 PowerShell；其他系统用 bash/sh |
| F5 | `permissions.py` | `MODES = default / acceptEdits / plan / bypassPermissions`。`PROTECTED_FILES`（项目的 `permissions*.yaml`、`mcp.yaml`、`hooks*.yaml`、`.mcp.json`）加上用户级 `permissions.yaml`、`mcp.yaml`、`hooks.yaml` 只约束文件工具，写入时以 `policy_protected` 拒绝。文件工具写项目之外以 `path_blocked` 拒绝，读项目之外要审批 |
| F6 | `security.py` | `CATASTROPHIC_COMMANDS` 直接拒绝，`RISKY_COMMANDS` 总要确认，`read_only_command` 识别的只读命令免审批且计划模式可用；`public_address`、`web_target` 判断地址是否为本机、内网或保留段（含代理软件的 fake-ip 段 198.18.0.0/15），供 `web_fetch` 使用 |
| F7 | `permissions.py`、`tui.py` | 已有域名规则 `WebFetch(domain:域名)`（可用 `*` 通配），审批面板有“永久允许这个域名”选项 |
| F8 | `credentials.py` | `child_environment` 只去掉 `api_key_env` 指向的变量名；`redact` 把已知密钥值替换为占位符。其他 `*_TOKEN`、`*_SECRET` 变量照样传给命令 |
| F9 | 控制面文件 | `~/.mosscode/` 下：`config.yaml`、`permissions.yaml`、`mcp.yaml`、`hooks.yaml`、`plugins.yaml`、`plugins-trust.json`、`plugins/`、`agents/`、`skills/`、`projects/<项目键>/`（会话、记忆、`hooks-trust.json`、团队邮箱）。项目中：`.mosscode/` 下的权限、MCP、Hook、插件声明、`agents/`、`skills/`、`worktrees/`，`.mcp.json`，开启 `claude_directories` 后的 `.claude/agents|skills/`。用户级子 Agent 的 `permissionMode` 可以放宽会话档位（`agents/runtime.py` 的 `effective_mode`），所以能写 `~/.mosscode/agents/` 就能提权 |
| F10 | `src/mosscode` | 没有本地 HTTP 监听（搜索 `listen`、`create_server`、`HTTPServer`），不存在 DeepSeek Harness CVE 那种回环管理接口 |
| F11 | 用户机器 | Windows 11 家庭中文版（EditionID `CoreCountrySpecific`），build 26300.9818，版本 26H2；PATH 中没有 `wxc-exec`；Docker Desktop 引擎启动失败；终端是 Windows Terminal + PowerShell，MossCode 的命令走 Git Bash |
| F12 | 报告 | 主流做法：每条命令一层 OS 沙箱（macOS Seatbelt、Linux bubblewrap），写入限于工作区和临时目录，受保护路径在工作区内也不可写，网络断开或只经认证的回环代理按域名放行；边界内命令免审批，越界由模型带理由申请、用户批准；放宽沙箱的配置只接受用户级。Windows：MXC 能管写、读、网络但要求 24H2/25H2 的特定补丁且 Home 版未核实；WRITE_RESTRICTED 受限令牌只能管写入；独立账户 + WFP 能全管但要一次 UAC；Job Object 不是边界 |
| F13 | 报告 | 已知逃逸几乎都绕到沙箱旁边：回环管理接口（dsh CVE-2026-82533）、改写自己的配置（Amp）、`.git/hooks` 与 `core.fsmonitor`、`docker.sock`、可写 PATH 目录 |
| F14 | 13B 探测 | MXC v1.0.1（microsoft/mxc 发布页的 `mxc-release-binaries.zip`，517 MB；其中 `x64/wxc-exec.exe` 11.6 MB，Microsoft Corporation 签名有效，单独复制出来也能运行）。在用户的 Windows 11 家庭版 26300.9818 上 `wxc-exec --probe` 返回 `tier: base-container`、`needsDaclAugmentation: false`：不需要管理员准备（`wxc-host-prep`），支持 `deniedPaths`、回环开关和全部 UI 限制。官方最低版本是 26H2 的 26300.9550 |
| F15 | 13B 探测 | 请求用稳定的 `1.0.0` 契约，`containment: processcontainer`，`telemetry.enabled: false`（`1.0.0` 请求的遥测默认关闭，只有开发中的 `1.1.0-alpha` 契约有必需诊断数据）。`ui.disable` 必须为 `false`，否则 PowerShell 和 Git Bash 都以 `STATUS_DLL_INIT_FAILED (0xC0000142)` 退出；`ui.clipboard: none` 照常生效（实测容器内连 `clipboard: all` 都读不到剪贴板） |
| F16 | 13B 探测 | 写入：`readwritePaths` 之外全部拒绝。cmd 和 PowerShell 写项目外、写用户目录都得到“拒绝访问”，项目外没有出现文件 |
| F17 | 13B 探测 | 读取是**允许清单**：没有授予的位置一律不可读，包括列出用户目录、读取 `~/.gitconfig`（git 警告 `unable to access 'C:/Users/LEGION/.gitconfig': Permission denied`）。系统目录可读。不在 Program Files 下的工具链要显式授予 `readonlyPaths`：本机 Git 在 `D:\Scoop\apps\git`，Python 基础解释器在 `~/miniconda3`，项目 `.venv` 指向它；不授予时 Python 报 `No pyvenv.cfg file`，授予后项目 `.venv` 中的 `pytest` 通过 |
| F18 | 13B 探测 | **MSYS2 程序不能在 ProcessContainer 中运行**：Git Bash 的 `bash.exe` 乃至 `usr/bin/echo.exe` 都以 `0xC0000142` 退出；授予 Git 目录读写、授予用户目录读写、改 UI 隔离级别、关闭 `leastPrivilege` 都无效。没有授予 Git 目录时是 `STATUS_DLL_NOT_FOUND (0xC0000135)`。Git 自带的原生 `mingw64/bin/git.exe` 能运行 |
| F19 | 13B 探测 | 网络：`egress.default: deny` 时连 DNS 也失败（`Could not resolve host`），`allow` 时 `https://example.com` 返回 200。`ingress.hostLoopback` 默认拒绝回环，`allow` 时能连宿主的回环服务。**容器继承宿主环境变量**，用户设置了 `HTTP(S)_PROXY=http://127.0.0.1:7897`（本机代理软件），所以 `hostLoopback: allow` 加上 `egress: deny` 时命令仍能经用户的代理上网 |
| F20 | 13B 探测 | `processContainer.captureDenials` 在本机返回 `StartLearningModeTraceWithOptions failed (HRESULT = 0x80004001)`，尽管 `--probe` 报告 `nativeCaptureAvailable: true`；拦截说明（V2）不能依赖它 |
| F21 | 13B 探测 | 每条命令的启动开销：`cmd /c echo` 经 `wxc-exec` 约 370–540 ms，直接运行约 160 ms |

## 需求

### 控制面加固（H）：不依赖沙箱，所有平台

| # | 需求 | 验收 |
| --- | --- | --- |
| H1 | 模型运行的命令（`execute_command`、Skill 工具）的环境在“去掉 `api_key_env`”之外，再按名称和值去掉形似密钥的变量：名称含 `TOKEN`（不含 `TOKENIZER`）、`SECRET`、`PASSWORD`、`PASSWD`、`PASSPHRASE`、`CREDENTIAL`、`API_KEY`、`ACCESS_KEY`、`PRIVATE_KEY`、`COOKIE` 或以 `_KEY`、`_PAT` 结尾；值形如私钥、JWT、常见服务令牌或带口令的 URL。代理变量和 `MOSSCODE_*` 保留。`config.yaml` 新增 `command_env.keep`（保留，支持 `*`，优先于一切去除规则）和 `command_env.drop`（额外去掉）。Hook、`!` 命令不变。采用黑名单而非白名单的理由见“命令环境（H1）” | `GITHUB_TOKEN=x moss -p "执行 env"` 的输出中没有它；`command_env.keep: [GITHUB_TOKEN]` 后有 |
| H2 | 每个 MossCode 进程一个私有临时目录（`~/.mosscode/tmp/<进程号>-<随机>/`，进程退出时删除，超过 7 天的残留在下次启动时清理），H1 范围内命令的 `TEMP`、`TMP`、`TMPDIR` 指向它。Git Bash 把 `/tmp` 映射到系统临时目录，不随环境变量改变，所以 `/tmp` 仍是共享的；真正隔离要等后端（S2） | 两个 MossCode 进程的命令写入 `$TMPDIR/x` 互不可见；退出后目录删除 |
| H3 | H1 范围内命令的 Windows Job Object 增加 UI 限制（Hook 和 `!` 命令不加）：禁止读剪贴板、全局原子、修改系统参数、切换桌面、注销/关机（`JOB_OBJECT_UILIMIT_*`） | 命令中读取剪贴板（`Get-Clipboard`）得到空或错误；Git Bash、PowerShell、git、python、npm 照常运行 |
| H4 | 文件工具的受保护清单补上 `.git/hooks/` 下的文件、`.git/config`、项目的 `.mosscode/plugins.yaml`，以 `policy_protected` 拒绝 | `write_file .git/hooks/pre-commit` 被拒绝 |
| H5 | 会话 id、团队目录、任何控制端点都不放进命令环境；文档写明沙箱内命令写出的文件（Makefile、`package.json` 脚本等）是不可信产物 | 测试检查命令环境中没有 MossCode 自己的路径和 id 变量 |

### 策略与配置（S）

| # | 需求 | 验收 |
| --- | --- | --- |
| S1 | 新增与平台无关的沙箱策略：可写根目录、受保护路径（可写根目录内也不可写）、禁止读取的路径、网络模式（`none` / `proxy`）和放行的域名。策略随每次调用传入，子 Agent、团队成员和越界重试可以用不同的策略 | 单元测试：同一后端同时编译两份不同的策略 |
| S2 | 默认可写：项目根目录（工作树中的子 Agent 为工作树根）、H2 的私有临时目录。默认受保护：F9 中项目内的全部控制面文件和目录、`.git/hooks/`、`.git/config`；不存在的受保护路径也要拦截创建（后端能做到时预先建占位） | 沙箱内 `echo x >> .mosscode/permissions.yaml`、`mkdir -p .git/hooks && echo x > .git/hooks/pre-commit` 都失败，`git commit` 成功 |
| S3 | 默认禁止读取（后端能强制时）：`~/.mosscode`（`skills/`、`plugins/` 除外，Skill 专属工具要读取自己的目录；其中的 `config.yaml` 可能有明文密钥）、当前使用的配置文件（包括 `--config` 指定的）、`~/.ssh`（保留 `config`、`known_hosts`）、`~/.aws`、`~/.azure`、`~/.kube`、`~/.docker`、`~/.config/gh`、`~/.git-credentials`、`~/.netrc`、`~/.pypirc`、浏览器配置目录、系统凭据目录。`~/.mosscode` 整体不可写 | full 后端下 `cat ~/.ssh/id_ed25519` 失败，`git fetch`（SSH）在越界申请后可用 |
| S4 | 配置写在用户级 `config.yaml` 的 `sandbox:` 下：`mode`（`off` / `on`，默认见 Q2）、`backend`（`auto` 或指定）、`network`（`none` / `proxy`）、`allow_write`（追加可写路径）、`deny_read`（追加禁止读取）、`allow_unsandboxed`（`ask` / `never`）。项目级 `.mosscode/sandbox.yaml` 只能收紧：追加 `deny_read`、受保护路径、把 `network` 改成 `none`；写了放宽的键时忽略并提示 | 项目文件写 `allow_write: [/]` 时启动区提示“项目配置不能放宽沙箱，已忽略” |
| S5 | `--sandbox` / `--no-sandbox` 覆盖本次运行；`/sandbox` 查看当前后端、强制级别、可写目录、网络和最近的拦截，可在本会话中关闭（需确认）或打开 | |
| S6 | 状态栏显示沙箱状态：`沙箱`（full）、`沙箱·部分`（partial）、`无沙箱`（关闭或失败后用户选择不用） | 三种状态各有界面测试 |

### 平台后端（B）

| # | 需求 | 验收 |
| --- | --- | --- |
| B1 | 后端接口：把（argv、cwd、env、策略）编译成包装后的 argv、env 和清理回调，并报告强制级别 `full`（写、读、网络都由系统强制）或 `partial`（只强制写入）。`run_process` 接受可选的策略，有策略时经后端包装后启动，Job Object、超时、输出截断、`terminate_tree` 不变 | 无策略时行为与现在完全相同（现有测试不改） |
| B2 | Linux：bubblewrap。`--ro-bind / /`，可写根目录 `--bind`，受保护路径 `--ro-bind` 覆盖，禁止读取的目录用 `--tmpfs`、文件用 `/dev/null` 覆盖，`--unshare-pid`、新的 `/proc`、`--unshare-net`（网络为 `proxy` 时用 Unix socket 桥接到宿主代理）、`--die-with-parent`。没有 `bwrap` 或用户命名空间不可用时后端不可用。级别 full | Linux CI 上运行后端测试：写项目外失败、写受保护路径失败、`curl` 直连失败 |
| B3 | macOS：Seatbelt（`sandbox-exec -p <生成的 profile>`），规则与 B2 相同，网络只允许连接回环代理端口。级别 full | macOS CI 上同 B2 的测试 |
| B4 | Windows MXC：调用 `wxc-exec.exe`，`1.0.0` 请求（F15）：`readwritePaths` 为可写根目录与私有临时目录，受保护路径与 S3 的路径进 `deniedPaths`，`readonlyPaths` 为可读根目录（F17：PATH 中各目录、命令解释器所在目录、`~/.gitconfig`、`sandbox.allow_read`），`egress: deny`、`hostLoopback: deny`（F19：允许回环等于允许经用户代理上网，13E 改用 MXC 的 `networkProxy`），`ui.disable: false`、`clipboard: none`、`injection: false`，`telemetry.enabled: false`，`fallback.allowDaclMutation: false`。只有 `--probe` 报告 `base-container` 且不需要 DACL 改写时可用。shell 用 PowerShell（F18、Q3）。级别 full | 在用户机器上通过 M168–M171 |
| B5 | Windows 受限令牌：ctypes 实现 `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE、LUA_TOKEN、WRITE_RESTRICTED)`，restricting SID 为 [工作区能力 SID、登录 SID、Everyone]；可写根目录和私有临时目录加允许该能力 SID 写入的 ACE，受保护路径加拒绝写入 ACE，父目录拒绝 `FILE_DELETE_CHILD`；`CreateProcessAsUserW` 后放进 Job。不使用 Low IL。级别 partial：读取和网络不受限，网络只通过代理环境变量引导 | 写项目外、写 `.mosscode/permissions.yaml`、重命名受保护文件的父目录都失败；`git commit`、`pip install -e .` 成功 |
| B6 | B5 留下的 ACE 记录在 `~/.mosscode/sandbox-acl.json`；`moss sandbox cleanup` 移除全部或指定项目的 ACE；可写根目录中有 Everyone 可写的目录时警告 | 清理后 `icacls` 中不再有能力 SID |
| B7 | 受保护路径的绕过方向全部有测试：符号链接、目录联接（junction）、硬链接、重命名父目录、先删后建、大小写变体、`..`、8.3 短文件名 | 每种方式各一个测试，在对应平台上运行 |

### 与审批的关系（A）

审批决定“是否运行”，沙箱决定“能碰到什么”，两者互不放宽。

| # | 需求 | 验收 |
| --- | --- | --- |
| A1 | 沙箱为 full 时，`default` 和 `acceptEdits` 下沙箱内的命令免审批；deny 规则、`CATASTROPHIC_COMMANDS` 的拒绝、`RISKY_COMMANDS` 的确认照旧 | full 下 `pytest` 不弹审批；`rm -rf ~` 照样被拒绝 |
| A2 | 沙箱为 partial 时审批与现在相同（读取和网络不受限，免审批等于允许命令悄悄读取并外发文件）；面板上注明“沙箱只限制写入” | partial 下 `pytest` 仍弹审批 |
| A3 | `plan` 档位：只读策略（没有可写根目录，只有私有临时目录）、网络 `none`。在 full 下，计划模式允许的命令可以放宽为“任何命令”（见 Q5） | |
| A4 | `bypassPermissions`：沙箱照常开启；沙箱外执行按 `allow_unsandboxed` 处理（Q4） | |
| A5 | 子 Agent 和团队成员使用主会话的策略（工作树中的以工作树为可写根目录）；成员的越界申请走现有的 `permission_response` 流程，邮箱文件只由宿主进程写入，且在 S2/S3 的保护范围内 | 成员命令写 `~/.mosscode/projects/…/teams/` 失败 |

### 拦截与越界（V）

| # | 需求 | 验收 |
| --- | --- | --- |
| V1 | `execute_command` 的工具说明写明当前边界：可写目录、网络状态、强制级别；沙箱关闭时不提 | 提示词测试 |
| V2 | 命令被拦截时，工具结果末尾追加中文的拦截说明，`details["sandbox"]` 记录结构化内容：被拒的主机（来自代理，确定）、疑似被拒的路径（来自后端的拒绝特征，标为“推测”）、强制级别；并提示“确需越界时以 outside_sandbox 和 justification 重新调用” | Linux 后端测试：写项目外失败时结果中列出该路径 |
| V3 | `execute_command` 增加参数 `outside_sandbox: bool` 和 `justification: str`（`outside_sandbox` 为真时必填）。审批面板标题为“在沙箱外运行”，显示理由；选项：本次、本会话中这个前缀、拒绝（是否可永久保存见 Q6）。`-p` 中按现有的非交互审批处理（未允许的操作拒绝） | 模型带理由申请后面板显示理由；拒绝后结果说明“用户拒绝在沙箱外运行” |
| V4 | 沙箱外执行就是不带沙箱、以用户的完整权限运行，S3 的禁止读取也不再生效；面板写明“将以你的完整权限运行，不受沙箱的读、写、网络限制” | |
| V5 | 每次拦截和越界写入会话记录（`/sandbox` 显示最近 20 条） | |

### 网络（N）

| # | 需求 | 验收 |
| --- | --- | --- |
| N1 | 网络模式 `proxy`：宿主在回环上启动 HTTP CONNECT 代理（asyncio，无新依赖），每条命令一个随机令牌，命令的 `HTTP(S)_PROXY` 带令牌；没有令牌的连接拒绝 | 不带令牌连接代理返回 407 |
| N2 | 代理按 `WebFetch(domain:…)` 规则放行（与 `web_fetch` 共用，F7）；不在名单上的域名暂停连接并在界面中询问（本次、本会话、永久保存为 WebFetch 规则、拒绝），`-p` 中拒绝 | 首次 `pip install` 询问 pypi.org 和 files.pythonhosted.org |
| N3 | 解析结果复用 `public_address` 检查，拒绝回环、内网、链路本地和云元数据地址；CONNECT 后检查 TLS 的 SNI 与目标主机一致 | 指向 `127.0.0.1` 的域名被拒绝 |
| N4 | 用户本机原本有上游代理（系统代理或 `HTTP(S)_PROXY`）时，沙箱代理通过上游转发；fake-ip 段按现有 `web_fetch` 的处理方式对待 | 在用户的代理环境下 `pip install` 能用（Q8） |
| N5 | 模式 `none` 时不启动代理，命令完全断网 | |

### 失败处理与自检（D）

| # | 需求 | 验收 |
| --- | --- | --- |
| D1 | 沙箱开启但后端不可用或自检失败时，默认失败关闭：`execute_command` 返回错误，说明原因和解决方法；界面询问一次“本会话不使用沙箱”（需确认），`-p` 中需要 `--no-sandbox` | 卸载 bwrap 后启动，命令被拒绝并给出说明 |
| D2 | 自检：用生成的策略实际执行一次越界写入、一次受保护路径写入和一次直连（full 后端），确认都被拦截；结果按后端和版本缓存在 `~/.mosscode/sandbox-probe.json`，后端或系统版本变化后重测 | 缓存命中时启动不增加可感知的延迟 |
| D3 | `moss doctor` 增加沙箱一节：可用后端、选中的后端、强制级别、自检结果、Windows 上 MXC 是否可用及原因 | |
| D4 | 后端不能用于某个 shell 时（例如 Git Bash 在某后端下不工作，Q3），说明原因并按配置回退到 PowerShell 或失败关闭，不静默降级 | |

## 设计

### 模块

| 位置 | 内容 |
| --- | --- |
| `src/mosscode/sandbox/__init__.py`（新） | `SandboxPolicy`（不可变数据类：`writable`、`protected`、`deny_read`、`network`、`domains`、`temp_dir`）；`policy_for(settings, root, mode, *, worktree)` 生成默认策略；`select_backend(settings)`；`Prepared(argv, env, cleanup, enforcement)` |
| `sandbox/bwrap.py`、`sandbox/seatbelt.py`（新） | B2、B3：纯文本生成参数和 profile，单元测试不需要真实系统 |
| `sandbox/windows_mxc.py`、`sandbox/windows_token.py`（新） | B4、B5；只在 Windows 导入，其他系统的测试按 `sys.platform` 跳过 |
| `sandbox/proxy.py`（新） | N1–N4；令牌、域名判断（调用 `permissions.py` 的域名规则）、地址检查（调用 `security.public_address`）、拦截记录 |
| `sandbox/violations.py`（新） | 按后端解析拒绝特征，合并代理的拦截记录，生成 V2 的文字和结构 |
| `sandbox/probe.py`（新） | D2 的自检和缓存，`doctor.py` 调用 |
| `tools/process.py` | `run_process(..., sandbox=None)`；有策略时 `prepare` 后启动，`finally` 中调用清理；Windows 的 Job 闸门放在包装之后（MXC 自己管理进程树时见 Q9） |
| `command_env.py`（已实现） | H1 的去除规则与 `command_env` 配置、H2 的私有临时目录；`credentials.protect` 读入配置 |
| `tools/windows_job.py` | H3：UI 限制（已实现） |
| `tools/process.py` 的 `ExecuteCommand` | V1 的说明、V3 的参数、V2 的结果；`plan_commands` 与 A3 的关系 |
| `permissions.py` | H4；A1/A2 的免审批判断（需要知道当前强制级别）；V3 的面板 |
| `config.py` | `Settings.sandbox`、`Settings.command_env`；S4 的项目文件只收紧 |
| `cli.py`、`tui.py`、`commands.py` | S5、S6、D1 的询问 |
| `sandbox_command.py`（新）、`launcher.py` | B6 的 `moss sandbox cleanup`（子命令，不导入界面和 SDK） |

### 哪些子进程进沙箱

| 子进程 | 是否进沙箱 | 理由 |
| --- | --- | --- |
| `execute_command` | 是 | 模型写的命令，沙箱的主要对象 |
| Skill 专属工具（`tool.json`） | 是 | 由模型触发，内容来自 Skill 作者 |
| 子 Agent、团队成员的命令 | 是，用主会话的策略（A5） | |
| 用户的 `!` 命令 | 否（Q7） | 用户自己输入的命令 |
| Hook、MCP stdio 服务器、git（插件、工作树）、剪贴板、`@` 提及 | 否 | 由用户配置或 MossCode 自己发起（F3）；它们不应自动执行沙箱内命令写出的产物（H5） |
| 内置搜索调用的 rg | 否 | 只读，参数由工具生成 |

### 命令环境（H1）

最初设计是白名单（只保留 `PATH`、`HOME`、语言与工具链变量等）再清理。实现时改为只按名称和值去除：Windows 上 MSVC（`INCLUDE`、`LIB`、`VCToolsInstallDir`…）、MSYS2、各语言版本管理器需要的变量太多，白名单在用户机器上漏掉一个就会让构建莫名失败，而用户很难看出原因。黑名单的风险是漏掉名称和值都不像密钥的变量，这类变量由 `command_env.drop` 补上；真正的边界由后端的读取限制（S3）提供。

判断顺序：`MOSSCODE_*` 与 `keep` 保留 → `drop` 去掉 → 代理变量保留 → 名称或值命中则去掉。`command_env` 写在当前使用的 `config.yaml` 中。项目目录的 `.mosscode/config.yaml` 存在时整份代替用户配置（现有行为，也决定 `base_url` 和密钥），所以 `keep` 并不比该文件的其他字段更危险；S4 实现时，放宽类字段（`keep`、沙箱的可写路径）应只接受用户级配置，见 Q13。

### Windows 路线

| 步骤 | 内容 | 决定什么 |
| --- | --- | --- |
| 探测（已完成） | 结果见 F14–F21：MXC 在用户机器上可用，写、读、网络都由系统强制；Git Bash 不可用 | 主后端是 B4（full） |
| 主后端 | B4，命令改走 PowerShell | |
| 另一后端 | B4 可用时 B5 仍作为其他机器的保底（Q10） | |
| 严格模式 | 独立账户 + ACL + WFP（一次 UAC），本规格不做 | |

### 失败与降级的显示

后端选择的结果只有三种，界面、`-p` 的 stderr、工具说明和 `doctor` 使用同一个来源：

- full：后端可用且自检通过；
- partial：只有 B5 可用；
- 无沙箱：配置关闭，或失败后用户选择本会话不用。

“失败后静默以无沙箱运行”不存在。

## 分批

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| 13A | H1–H5 | 不需要任何后端，所有平台立即生效：环境清理、私有临时目录、Windows UI 限制、文件工具的保护清单（已实现） |
| 13B | Windows 探测 | 一次实验，结果决定 13D 的顺序；不写产品代码（已完成） |
| 13C | S1–S6、B1、B4、A1–A5、V1–V5、D1–D4，网络只有 `none` | 公共层和 Windows MXC 后端；在用户机器上能完整验证 |
| 13D | B2、B3、B5、B6，B7 | Linux、macOS 后端和 Windows 的保底后端；需要 CI |
| 13E | N1–N5 | 代理依赖 13C 的拦截记录和审批面板 |

13A 可以独立提交。13C 原计划是 Linux/macOS 加公共层，13B 之后改为公共层加 MXC（Q11）：GitHub 账户暂停期间没有 CI，Linux 和 macOS 后端在本机无法验证，而 MXC 已在用户机器上实测可用。

## 不做

- Windows 严格模式（独立账户 + WFP）、Low IL、Windows Sandbox、直接使用 AppContainer。
- Linux 上拦截 Unix socket 的 seccomp（`docker.sock` 是已知逃逸，文档写明，见 Q12）。
- 密钥的哨兵值替换（Claude Code 的 `mask`，需要 TLS 终止）。
- MCP stdio 服务器和 Hook 进沙箱。
- 文件工具改走 OS 沙箱：保持应用层检查，但受保护路径和可写目录从同一份策略取值，避免两边不一致。
- 资源限制（内存、CPU）和审计日志。
- 云端沙箱（E2B 等）和会话移交。
- 依赖 srt（Node ≥ 22.12，仍是 Beta）；可参考其设计，复制 Apache-2.0 代码时保留许可证和 NOTICE。

## 待定问题

| # | 问题 | 推荐 | 替代 |
| --- | --- | --- | --- |
| Q1 | MXC 能否在用户的 Windows 11 Home 26300 上运行；`wxc-exec.exe` 怎么获得 | **已决定**：能（F14）。获得方式按推荐。`wxc-exec.exe` 不随 MossCode 打包，由用户自行安装（MXC 是 MIT，但有“必需的诊断数据”，需要用户自己知情）；MossCode 在 PATH 和固定位置查找 | 随 pip 包附带二进制：开箱即用，但包体积变大、平台轮子变复杂，遥测由 MossCode 代为接受 |
| Q2 | 沙箱默认是否开启 | 13C/13D 发布时默认关闭，`sandbox.mode: on` 开启，`doctor` 和首次启动提示一次；稳定一个版本后改为“full 可用时默认开启” | 一开始就默认开启：保护最好，但 Windows 上 partial 后端改变不了审批体验，失败关闭可能频繁打断用户 |
| Q3 | Git Bash（MSYS2）在 MXC 和受限令牌下能否工作 | **已决定**：MXC 下不能（F18），沙箱开启时命令改用 PowerShell 并在工具说明中写明；受限令牌下待 13D 实测 | 不能工作时整个后端不可用（失败关闭） |
| Q4 | `bypassPermissions` 下申请沙箱外执行是否自动批准 | `allow_unsandboxed: ask`（默认）：仍然询问；`never` 时直接拒绝 | 自动批准（Claude Code 的做法）：bypass 更彻底，但模型一句理由就能离开沙箱 |
| Q5 | full 沙箱下计划模式是否允许任意命令（只读、断网） | 允许：计划阶段能跑测试、查依赖；写入只会落在私有临时目录 | 维持现有只读命令白名单：行为不变，但沙箱的好处在计划模式中用不上 |
| Q6 | “在沙箱外运行”能否永久保存 | 不能，只有本次和本会话的前缀：永久规则等于给某类命令长期开后门，需要时用户可以在 `allow_write` 或域名规则里精确放宽 | 可以保存为用户级规则（如 `Unsandboxed(npm publish)`）：常用命令不必每次申请 |
| Q7 | 用户的 `!` 命令是否进沙箱 | 不进：用户自己输入的命令 | 进：行为一致，但用户在沙箱里调试时会被自己的边界挡住 |
| Q8 | 用户的上游代理（`security.py` 专门处理了 fake-ip 段，推断用户使用代理软件） | N4 支持上游 HTTP 代理；13E 在用户机器上实测 | 沙箱代理只直连：实现简单，但在需要代理的网络中 `pip`、`npm` 不可用 |
| Q9 | MXC 在前台命令退出后结束其余子进程，后台进程（如开发服务器）怎么办 | MossCode 目前没有后台 shell（F2），先不处理；将来加后台 shell 时作为独立的长期容器启动 | 现在就设计长期容器 |
| Q10 | MXC 可用时是否还做受限令牌后端 | 做，作为没有 MXC 的 Windows（企业版本控制、旧补丁）的保底，并明确标为 partial | 不做：工作量减半，没有 MXC 的 Windows 上不提供沙箱 |
| Q11 | 13C（Linux/macOS + 公共层）和 13D（Windows）谁先 | **已决定**：公共层加 MXC 先（见“分批”）。原推荐是 13C 先：公共层在 CI 上能完整验证，成熟做法可以照搬；13D 复用公共层 | 13D 先：用户日常在 Windows，先拿到对自己有用的部分；公共层要在没有成熟参照的平台上同时成形 |
| Q12 | `.git` 整体只读（Codex）还是只保护 `.git/hooks`、`.git/config` | 只保护这两项：`git commit`、`git checkout` 在沙箱内照常可用 | 整体只读：杜绝所有 `.git` 投毒，但每次提交都要申请沙箱外执行 |
| Q13 | 项目目录的 `.mosscode/config.yaml` 整份代替用户配置，仓库可借此放宽 `command_env.keep`、改 `base_url` | 放宽类字段（`command_env.keep`、沙箱可写路径、`base_url`）只认用户级配置，项目配置中出现时报错或忽略并提示 | 维持现状，在文档中说明不要在不信任的仓库中运行 |

## 验收

自动测试：策略生成（默认、工作树、计划模式、项目配置只收紧）；bwrap 参数与 Seatbelt profile 的文本；Linux 和 macOS CI 上的真实后端测试（写项目外、写受保护路径、读禁止读取的路径、直连都失败，`git commit` 成功）；B7 的每种绕过方式；Windows CI 上 B5 的 ACE 与令牌；环境清理与 `command_env`；免审批只在 full 时生效；拦截结果的文字与结构；越界申请的面板；代理的令牌、域名、地址检查；失败关闭；`moss sandbox cleanup` 不导入界面。CI 中拿不到用户命名空间或 `sandbox-exec` 时测试标为跳过并说明原因，不放宽断言。

手动验收（Windows Terminal，真实模型），编号接在 M167 之后，具体操作在实现对应批次时补进 `docs/acceptance.md`：

| # | 操作 | 期望 |
| --- | --- | --- |
| M165–M167（13A） | 见 `docs/acceptance.md` 的“命令环境与控制面” | |
| M168 写入边界（13C） | 开启沙箱，让模型运行测试、提交一次 git，再让它“在 `.mosscode/permissions.yaml` 加一条 allow 规则”“在 `.git/hooks` 加一个 pre-commit” | 测试和提交不弹审批（full）或照常审批（partial）；两次改写都被拦截，结果列出路径，状态栏显示沙箱级别 |
| M169 越界申请（13C） | 让模型往项目外的目录写文件 | 第一次被拦截；模型带理由以 `outside_sandbox` 重试，面板标题“在沙箱外运行”并显示理由；拒绝后模型不再尝试 |
| M170 读取与网络（13C） | 让模型 `cat ~/.ssh/config` 以外的私钥文件、用 `curl` 访问一个网站 | 都被拦截，结果说明原因 |
| M171 失败关闭（13C） | 让后端不可用（Windows 上把 `wxc-exec.exe` 移出 PATH 并删除探测缓存）后启动 | 启动时说明原因并询问是否本会话不用沙箱；拒绝时命令被拒绝；`moss doctor` 显示原因 |
| M172 代理（13E） | `sandbox.network: proxy`，让模型 `pip install` 一个小包 | 询问 pypi.org 和 files.pythonhosted.org，允许后安装成功；拒绝时结果列出被拒的主机 |
