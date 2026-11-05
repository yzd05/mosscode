# DeepSeek Harness 及其沙箱 / 执行隔离设计

> 调研日期：2026-11-05。WebFetch 工具当时不可用，一手资料用 curl 直接取自 GitHub raw、GitHub API、arXiv 和 The Hacker News。标注“仅搜索摘要”的条目没有打开原文核实。

## 问题 1："DeepSeek harness" 指什么？DeepSeek 有没有官方的 harness 项目？

### Takeaway
有官方项目，名字完全对得上：**DeepSeek Harness（`dsh`）**，仓库 `github.com/deepseek-ai/deepseek-harness`，DeepSeek-AI 官方发布，MIT 许可，TypeScript/Node 实现，2026-08-13 公开，到 2026-11 星数约 27 万。它是一个本地运行的 coding agent 运行时，主打“一切皆插件”。用户说的 “deepseek harness” 基本可以确定指它（推断，但把握很高）。另有一个同名的个人 Python 项目，以及一个不相干的训练侧沙箱平台 DSec，要分开看。

### Cited Findings
- README 原文定位：DeepSeek Harness (`dsh`) is "an open-source agent harness developed by DeepSeek AI"，架构是 everything-is-a-plugin，基于 Cordis 插件框架（对应论文 arXiv 2608.25512）— [README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/README.md)
- 状态是 developer preview，README 原话为 "THERE WILL BE COMPATIBILITY-BREAKING CHANGES"。运行方式是 `npx @deepseek-ai/dsh web`，Web UI 默认地址为 `http://127.0.0.1:3080`；另有 headless profile 和 Desktop 应用 — [README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/README.md)
- GitHub API（2026-11-05 查询）：description 为 "DeepSeek Harness: Everything is a Plugin."，created_at 2026-08-13，pushed_at 2026-11-04，stargazers_count 273098，forks 33283，license MIT — [GitHub API](https://api.github.com/repos/deepseek-ai/deepseek-harness)
- SAFETY.md 说明项目"has not undergone a security audit"，不能当成安全或生产就绪的软件；沙箱、审批和权限控制"do not guarantee isolation"；建议在一次性虚拟机或容器里运行 — [SAFETY.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/SAFETY.md)
- 官方文档站：deepseek-harness.github.io/deepseek-harness — [README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/README.md)
- 同名项目：`HenryZ838978/deepseek-harness` 是独立开发者的 Python 包，与 DeepSeek AI 无关（仅搜索摘要）— [搜索结果中的说明](https://springbrand.ai/deepseek-harness)
- 社区里其他 “DeepSeek + 终端 agent” 项目，均为仅搜索摘要：
  - `Hmbown/DeepSeek-TUI`：Rust 实现，有评测称 25,000+ 星 — [DEV Community](https://dev.to/arshtechpro/deepseek-tui-run-a-deepseek-coding-agent-directly-in-your-terminal-59ij)
  - `lessweb/deepcode-cli`（Deep Code）：被收录在官方的 `deepseek-ai/awesome-deepseek-agent` 列表中 — [awesome-deepseek-agent](https://github.com/deepseek-ai/awesome-deepseek-agent/blob/main/docs/deepcode.md)
  - `holasoymalva/deepseek-cli`、`fenwii/deepseek-cli`：较旧、较小的项目 — [holasoymalva](https://github.com/holasoymalva/deepseek-cli)、[fenwii](https://github.com/fenwii/deepseek-cli)
- 一篇 2026 年 5 月的评测称 Deep Code 与 DeepSeek 内部的 Harness 团队及"即将推出的官方 DeepSeek Code 产品"无关（仅搜索摘要）— [Verdent](https://www.verdent.ai/guides/deep-code-cli-review)
- 训练侧另有一套沙箱平台 DSec（DeepSeek Elastic Compute），见问题 4。它不是 dsh 的组成部分 — [arXiv 2609.22978](https://arxiv.org/abs/2609.22978)

### Inferences
- 名字、发布方和热度（27 万星，远超其他候选）都指向 `deepseek-ai/deepseek-harness`。如果用户想的是“DeepSeek 训练 agent 用的沙箱”，那对应的是 DSec，不是 harness。
- 它的设计明显借鉴了 Codex CLI：模式名 `read-only` / `workspace-write` / `danger-full-access` 和审批策略的思路都与 Codex 一致。所以它和 Codex 的调研结论会有大量重叠，差异主要在 Windows 后端和插件化的接缝设计。

### Gaps
- 没有核实 “DeepSeek Code” 官方产品是否存在，只在一篇第三方评测里看到。
- 社区项目的星数没有逐一用 GitHub API 核对。

## 问题 2：dsh 怎样隔离命令执行（机制、文件系统和网络策略、审批、密钥、平台）

### Takeaway
dsh 的沙箱是**宿主机上的 OS 级进程沙箱，只管文件写入**，不用容器或虚拟机：
- Linux：bubblewrap，不可用时用 Landlock；
- macOS：Seatbelt（`sandbox-exec`）；
- Windows：自研的 `WRITE_RESTRICTED` 受限令牌，加 Low 完整性级别，加每个 workspace 一个能力 SID 的 ACL 授权。

模式只有三种：`read-only`、`workspace-write`、`danger-full-access`。读取、网络和进程可见性都**明确不在**模式的承诺范围内。沙箱与审批策略（`ask` / `never`）是两个独立开关，由 permission preset 打包成一个选择器。设计上有几个亮点：沙箱不可用时失败关闭，执行结果报告 `full` / `partial` 两级 enforcement，按后端区分“拒绝”和“runner 故障”的 stderr 方言，被拒后模型可以申请一次性的提权重试。

### Cited Findings

**接缝与模式**
- `SandboxMode` 只管文件副作用：
  - `read-only` 拒绝写入，POSIX 后端额外放行 `/dev/null`；
  - `workspace-write` 允许写 workspace 根目录、显式追加的目录和后端提供的临时区；
  - `danger-full-access` 不做隔离。
  
  文档原话："Network and process visibility are outside this vocabulary." — [docs/subsystems/sandbox.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/sandbox.md)
- 策略按每次调用传入，不固定在 provider 上，因此并发的会话、子 agent 和提权重试可以同时使用不同的边界。`workspaceRoot` 取自会话不可变的 cwd。策略优先级是：批准过的显式模式，高于会话日志中最后一个 `sandbox/mode` 事件，再高于部署默认值 — [sandbox.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/sandbox.md)
- `ctx.sandbox.confine(argv, policy)` 返回包装后的 argv，以及 `enforcement`、`denialSignatures`、`runnerFailureRules`。没有可用后端时以 `SANDBOX_UNAVAILABLE` 拒绝，文档原话："Silent unconfined passthrough is never legal for a confined policy." — [sandbox.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/sandbox.md)
- enforcement 是“报告出来的事实”：`partial` 表示后端只能管住承诺效果的一部分。目前的 partial 情形有两种：较旧的 Landlock ABI，以及 Windows ACL 后端（硬链接、读取不受限、AppContainer ACL 三处缺口）— [sandbox.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/sandbox.md)
- 失败分类：先匹配 runner 故障规则（命令根本没运行，报告为沙箱基础设施故障），再匹配拒绝签名（沙箱正常工作，拦下了操作）。拒绝签名按后端区分：bwrap 是 EROFS，Landlock 是 EACCES，Seatbelt 是 EPERM。之所以不用跨后端的并集，是因为并集会把某个后端根本不会产生的错误也当成拒绝 — [sandbox.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/sandbox.md)

**各平台后端（`dsh-sandbox-local`）**
- 先按平台定 runner 链：linux 依次是 bwrap、Landlock；darwin 是 Seatbelt；win32 是 ACL 受限令牌。链上只有一个候选时直接选用；多个候选时做一次功能探测，结果在 provider 生命周期内缓存。全部不可用时失败关闭 — [sandbox-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-local/README.md)
- bwrap profile：宿主根目录只读挂载，`/dev` 重新创建，`/proc` 来自私有 PID 命名空间（看不到宿主进程，procfs magic link 无法绕过挂载）。`workspace-write` 额外挂载临时的 `/tmp`，并把 workspace 和追加目录以可写方式绑定 — [sandbox-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-local/README.md)
- Seatbelt profile 默认允许，加上 `(deny file-write*)` 和写入白名单，所有路径都先规范化（`/tmp` 实际是 `/private/tmp`）。它依赖已被弃用的 `sandbox-exec` — [sandbox-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-local/README.md)
- 可以用 `runnerCommand` 配置自定义 runner，必须兼容 bwrap 的参数格式。这被视为运维方的断言：跳过探测，直接认定为 full — [sandbox-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-local/README.md)

**Windows 后端（与 MossCode 最相关）**
- 令牌构造：用 `CreateRestrictedToken`（`WRITE_RESTRICTED` + `DISABLE_MAX_PRIVILEGE`）复制调用者令牌，restricting SIDs 中放入 workspace 和私有临时目录两个能力 SID，再把令牌降到 Low 完整性。Windows 会做两次访问检查，写入必须两次都通过；此外，Low 标签使所有未标为 Low 的对象都拒绝写入 — [sandbox-windows-acl README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-windows-acl/README.md)
- 每次授权在同一个 `SetNamedSecurityInfoW` 调用里写入三项：能力 SID 的允许 ACE、对 Everyone 拒绝父目录的 `FILE_DELETE_CHILD`、Low 完整性标签。要拒绝 `FILE_DELETE_CHILD`，是因为父目录的这项权限不需要 restricting SID 共同签署，不加拒绝的话 `cmd /c del` 能删除 workspace 外的文件 — [sandbox-windows-acl README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-windows-acl/README.md)；[决策记录](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/.agents/notes/implemented/feature/2026-08-08-windows-acl-restricted-token-sandbox.md)
- SID 与临时目录的生命周期：
  - workspace SID 由规范化路径确定性派生，ACE 常驻（每台机器每个 workspace 只授权一次，之后都命中跳过）；
  - 每个“会话 × workspace”组合分配一个随机私有临时目录和独立的可撤销 SID，runner 会把 TMP/TEMP 改写到这个目录；
  - 子进程放进 `KILL_ON_JOB_CLOSE` Job；
  - runner 失败时打印 `windows-acl-run: <detail>` 并以 127 退出。
  
  — [sandbox-windows-acl README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-windows-acl/README.md)
- 已知限制（官方列出）：
  - 读取、网络和进程可见性都不受限；
  - NTFS 硬链接可以别名到 workspace 外的文件；
  - Low 标签会长期留在 workspace 上，其他 Low 完整性进程也能写入；
  - 大目录树首次授权要全树传播 ACL，可能耗时数十秒；
  - 不能做控制台隔离（`CREATE_NO_WINDOW` 会导致 0xC0000142）；
  - 受限进程的孙进程无法使用命名管道 stdio；
  - restricting 列表里必须保留 Everyone 和 logon SID，否则 DLL 初始化和 CNG 会崩溃；
  - Windows 上带 `additionalWritableRoots` 的请求一律返回 `SANDBOX_UNSUPPORTED_POLICY`。
  
  — [sandbox-windows-acl README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-windows-acl/README.md)；[sandbox-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-local/README.md)
- 为什么没选 AppContainer 或 mxc：
  - AppContainer 和独立身份方案默认连读取都拒绝，要支持“读任意文件”就得大面积改写 DACL；
  - Microsoft mxc 要求 Windows 11 24H2（build 26100）以上，BaseContainer 层需要 25H2+（26600+），更低版本会退回 AppContainer 加 DACL 增补；
  - 受限令牌只截断写访问，不需要任何读授权，也不抬高 OS 版本下限。
  
  — [决策记录](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/.agents/notes/implemented/feature/2026-08-08-windows-acl-restricted-token-sandbox.md)
- 在这个后端出现之前，Windows 的 runner 链是空的，Windows profile 实际退化为 `danger-full-access` — [决策记录](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/.agents/notes/implemented/feature/2026-08-08-windows-acl-restricted-token-sandbox.md)
- 附带的 `diagnose-windows-sandbox-acl` skill 用来诊断并修复 ACL 冲突，比如其他 AppContainer 工具留下的 package SID ACE。它需要以非受限方式运行，也就是要走一次提权审批 — [sandbox-windows-acl README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/sandbox-windows-acl/README.md)

**审批、提权与权限预设**
- `ApprovalPolicy` 有两个值：`ask` 交给 UI 回答者链处理，没人回答时视为 `unavailable`；`never` 直接返回 `rejected`。审批结果是封闭集合，只有 `allowed-once` 放行（一次性），其余一律拒绝。`approval/asked` 和 `approval/decided` 只记日志，不进模型上下文 — [docs/subsystems/approval.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/approval.md)
- 被拒的命令不会静默重试。结果里带 `sandbox: { mode, denied: true }`，并追加标记 `[sandbox: file access denied under <mode> mode]`。之后模型可以带上 `sandbox_permissions`（够用的最窄的更宽模式）和 `justification`，把同一条命令重试一次；用户批准之前不会执行 — [bash-sandbox README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/shell/bash-sandbox/README.md)
- 默认预设有两个：`workspace-write`（对应 workspace-write + ask）和 `danger-full-access`（对应 danger-full-access + never）。另有实验性的 `auto`（Auto review，由模型代为审查，捆绑 danger-full-access + ask）。委派出去的子 agent 固定使用 `never` 审批 — [permission-presets.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/permission-presets.md)；[auto-review README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/experimental/auto-review/README.md)
- 普通命令本来就不需要审批；只有申请比当前会话更宽的权限时才会弹出审批 — [The Hacker News](https://thehackernews.com/2026/09/deepseek-harness-flaw-let-ai-agents.html)

**环境变量、密钥与网络**
- 子进程从清理过的环境启动。不过清理只按变量名做启发式匹配（`*KEY*`、`*PASSWORD*`、`*SECRET*`、`*TOKEN*`），例如 `*PASSPHRASE*` 就会漏过 — [subprocess-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/subprocess/subprocess-local/README.md)；[subprocess.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/subprocess.md)
- 进程树与输出的管理：
  - 进程树归属：Linux 用 user-systemd 的 transient scope，Windows 用 helper 持有的 kill-on-close Job，macOS 退回进程组；
  - 溢出文件以 `0600` + `O_EXCL` 权限创建在 `0700` 目录下，防止符号链接预置攻击。
  
  — [subprocess-local README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/subprocess/subprocess-local/README.md)
- 网络与代理变量：
  - 网络本身不做限制；
  - 项目自己的 `.env` 不允许设置代理变量，否则拒绝启动，以免仓库决定流量去向；
  - 模型编写的脚本（workflow worker、ptc-runtime）拿不到代理变量，以免读出 URL 中的代理密码；
  - bash 工具里的命令会继承代理变量，文档也承认这些命令能读到其中的密码。
  
  — [network-proxy.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/user/guide/network-proxy.md)
- 平台：Linux、macOS、Windows 都有本地后端；Windows 上默认 shell 是 pwsh（按 notes 文件名推断）。另有 SSH 远程后端 `dsh-sandbox-ssh` — [sandbox.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/sandbox.md)
- 有搜索摘要称 `ctx.subprocess` 有一个 E2B 沙箱 executor，**未核实**，仓库树中没有找到 e2b 路径 — [eesel 摘要](https://www.eesel.ai/blog/deepseek-harness)

**CVE-2026-82533（沙箱逃逸）**
- 漏洞经过：
  - 沙箱内的 agent 用一条 shell 命令调用 dsh 自己的本地 Web 接口，把会话改成 `danger-full-access` + 审批 `never`，之后的命令都在沙箱外运行，且不经审批；
  - 利用需要 agent 先读到攻击者写的文本（提示注入）；
  - VulnCheck 评分 9.4，2026-09-08 公开；
  - 受影响版本：≤0.1.1-rc.2；npm 上第一个修复版本是 0.1.2-alpha.2（2026-08-30）。
  
  — [The Hacker News](https://thehackernews.com/2026/09/deepseek-harness-flaw-let-ai-agents.html)
- 根因有四点：
  - 沙箱不限制网络，所以 loopback 上的管理接口可达；
  - agent 的 shell 环境里直接给出了接口地址和当前会话 ID；
  - 接口没有认证，“本地”判断只看客户端提供的 Host 头；
  - 这个接口还能下载整个会话日志。
  
  修复方式是启动时打印一次性 token，换取签名 cookie，之后每次调用都要带 cookie；但沙箱本身没有改，读取和网络仍然不受限 — [The Hacker News](https://thehackernews.com/2026/09/deepseek-harness-flaw-let-ai-agents.html)
- 修复版本号说法不一：OX 和 DevOps.com 写的是 0.1.2-alpha.1；THN 指出这个版本只在 GitHub 上有，没有发布到 npm — [The Hacker News](https://thehackernews.com/2026/09/deepseek-harness-flaw-let-ai-agents.html)；[OX Security](https://www.ox.security/blog/cve-2026-82533-deepseek-harness-ai-agent-sandbox-escape/)（OX 原文未能取回正文）

### Inferences
- 对 MossCode 最有参考价值的几点：
  1. **沙箱模式与审批策略分成两个开关**，再用预设打包给用户选；
  2. **策略按调用传入**，provider 无状态，方便子 agent 和提权重试；
  3. **失败关闭，并报告 full/partial**，绝不静默回退为不受限执行；
  4. **区分 runner 故障与拒绝的 stderr 方言**；
  5. **拒绝后让模型带 justification 申请一次性提权**；
  6. Windows 上用 `CreateRestrictedToken(WRITE_RESTRICTED)` + Low IL + 能力 SID ACE + 拒绝 `FILE_DELETE_CHILD`，**不需要管理员权限，不抬高 OS 版本下限**，可以从 Python 通过 ctypes/pywin32 调用（推断，未验证）。
- CVE 给的教训：只限制写入的沙箱，必须假设 agent 能访问 loopback 上的一切。宿主自身的控制面（MossCode 若有本地 HTTP/IPC、MCP 管理端口、配置文件写入路径）必须认证，或者放在沙箱写入范围之外；也不应把会话 ID、控制端点暴露在子进程环境里。凡是能改变权限模式的路径，都必须经过用户审批。
- Windows 方案的代价不小：对 workspace 的 DACL 和标签修改是常驻的，大目录树首次授权有全树传播开销，硬链接有缺口，命名管道 stdio 也有问题。MossCode 采用时要权衡这些对用户 workspace 的副作用，至少要提供清理命令。

### Gaps
- `packages/sandbox/sandbox-local/src/profiles.ts` 的具体 bwrap/Seatbelt 参数没有逐行阅读。
- Auto review 的审查模型如何判断风险，没有深入了解。
- 没有核实修复后，沙箱内的 agent 是否还能拿到有效的 cookie 或 token。THN 也说没有来源回答这一点。
- OX Security 原文是 JS 渲染页面，没有取到正文，只引用了 THN 的转述。

## 问题 3：DeepSeek 训练侧的大规模沙箱（DSec）有哪些设计点与本地沙箱相关？

### Takeaway
DSec 是 DeepSeek 用于 agentic RL 训练和评测的集群级沙箱平台，在 V4 技术报告中首次披露，2026-09 发表了独立论文。它用一套 SDK 提供 FnCall、容器、Firecracker microVM 和 QEMU 全虚拟机四种隔离级别。它属于云端多租户基础设施，与本地 coding agent 沙箱的问题不同，可以借鉴的主要是理念层面。

### Cited Findings
- 论文摘要中的事实：
  - 四种后端通过统一 SDK 提供：FnCall、container、microVM、full-VM；
  - 环境由独立版本化的分层组合而成，镜像从 3FS 按需加载；
  - 与 RL 框架协同设计，把有状态的 rollout 执行与可抢占的 GPU 训练解耦，并缓解 reward hacking 等不当行为；
  - 规模：单个生产单元约 160 个节点，每天约 300 万个沙箱，并发超过 380,000，每秒创建超过 5,000 个。
  
  作者约 131 人，由梁文锋提交，2026-09-19 — [arXiv 2609.22978](https://arxiv.org/abs/2609.22978)
- 搜索摘要中的细节（未逐条在原文核实）：
  - Rust 编写；
  - 只读 EROFS 分层；
  - 用 virtio-pmem DAX 和 DAMON 做高密度部署；
  - 支持增量快照和恢复；
  - V4.1 起 rollout 在 DSec 内的 worker 容器加 agent 沙箱中执行；
  - 有序记录命令和结果；
  - 覆盖 V3.2 到 V4.1 的全部 agent 训练和评测工作负载。
  
  — [Pandaily](https://pandaily.com/deepseek-dsec-elastic-compute-agentic-training-sandbox-3m-day)；[DeepSeek-V4 报告 arXiv 2606.19348](https://arxiv.org/pdf/2606.19348)；[Zhihu Frontier](https://x.com/ZhihuFrontier/status/2104889429345354180)

### Inferences
- 对本地沙箱可借鉴的理念：
  - **按需选择隔离级别**：轻量的函数调用和进程沙箱用于常规任务，容器或 VM 用于不可信任务；
  - **命令和结果的有序审计日志**；
  - **快照与回滚**：本地可以对应 git stash/worktree 或文件快照。
  
  集群调度、3FS、内存超分等内容与本地 agent 无关。
- 这也解释了 DeepSeek 自己的分工：训练用 DSec 做强隔离；本地 dsh 只做写入限制，并在 SAFETY.md 里建议用户自行放进 VM 或容器。

### Gaps
- 论文正文中关于网络隔离、seccomp/gVisor 和 reward hacking 防护机制的具体细节没有读到（HTML 版解析失败），只引用了摘要。
