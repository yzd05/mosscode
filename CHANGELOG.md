# 更新日志

这里记录每个版本的用户可见变化。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)；1.0 之前，次版本号的变化可能包含不兼容的改动，会在条目中注明。

## [Unreleased]

### 新增

- 重复调用检测：同一次任务连续 3 轮使用相同参数调用相同工具、得到相同结果时，提醒模型换一种做法；连续 5 轮仍然相同时停止，保留已执行的调用和结果。参数或结果变化、用户补充信息或收到后台通知后重新计数。界面和 `-p` 说明原因，json / stream-json 返回 `repeated_calls`，`/goal` 暂停续跑，子 Agent 返回未完成。见[重复调用检测](docs/reference/tools.md#重复调用检测)。
- 命令：主对话的前台命令运行超过 5 秒自动转入后台（`command_auto_background`，0 关闭），模型可以先做别的事，需要结果时用 `task_output` 等它；模型给的 `timeout` 仍然有效。`task_output` 已经交出结果的任务不再重复发送通知。沙箱经代理联网的命令留在前台，以便询问新域名。
- 配置：`follow: claude-code` 或 `follow: codex` 的 Provider 读取 Claude Code、Codex 的设置（CC Switch、Magpie 写入的服务地址、密钥和模型），不再重复填写；在那边切换服务后，`/model` 选中它即重新读取，对话历史保留。`preset: deepseek` 等厂商预设（DeepSeek、Kimi、GLM、通义千问、OpenRouter、Anthropic、OpenAI）代替协议、地址、默认模型和密钥变量名。`moss setup` 先列出可跟随的设置和预设，只有“其他服务”才逐项询问。见 [跟随 Claude Code 或 Codex](docs/reference/providers.md#跟随-claude-code-或-codex)。
- 评估：`scripts/run_evals.py` 在 13 个固定的编码任务（修 bug、加功能、重构）上运行 MossCode，每次用任务项目的新副本，结束后用 Agent 没见过的测试判定；报告每个 Provider 的通过率、平均请求次数和 Token、对话缓存命中率、耗时和费用，以及每个任务的结果。`--provider` 可重复给出以对比多个模型，`--repeat` 多跑几次看稳定性。任务和用法见 [评估任务](evals/README.md)。
- 最近工具的成败：任务运行时，输入框上方运行状态行的右端显示最近 5 个工具调用的结果（✓ 成功、× 失败、! 被取消或未执行），颜色与工具行的标记相同，长任务中不用往回翻也能看出最近有没有失败。只计主对话的工具调用，`/clear`、恢复会话和用 `/rewind` 回退对话后清空。
- 文档：README 改为更简短的首页（logo、徽章、核心能力、快速开始、常用入口），完整的配置项、按键、命令和各项功能移到 [使用指南](docs/usage.md)；新增英文 README（[README.en.md](README.en.md)）和英文使用指南、与 Claude Code 设计逐项对照的 [对照 Claude Code](docs/claude-code-patterns.md) 和 [路线图](ROADMAP.md)。
- 压缩前先清理工具结果：自动压缩到期时，如果只清理较早的工具结果（文件读取、搜索、命令输出、网页和 MCP 工具的结果）就能把上下文降到自动阈值的一半以下，就不再做摘要，对话文字全部保留原文，也不多发一次请求；被清理的读取结果可以重新读取，其他结果的原文保存在会话目录中。腾不出足够空间时照旧摘要。
- 折叠最早的一段对话：自动压缩到期、只清理工具结果又不够时，先把最早的几轮对话交给模型写成摘要，只在发给模型的内容中替换，够用（降到自动阈值的 60%）就停；中间较早的对话保留原文，会话记录中的历史不变，`/rewind` 仍能退回到被折叠的消息，恢复会话时按记录重建。第二次折叠不改动第一段，已有 2 段时、要折到近期消息才够时或折叠失败时照旧摘要。对话中显示“✓ 已折叠较早的对话 · N 条消息 · 约 X → Y Token”，`stream-json` 的 `compact` 事件新增 `collapsed`；`config.yaml` 中 `context_collapse: false` 关闭。
- 空回复恢复：模型返回既没有正文也没有工具调用的回复，或在写出任何内容前就因思考过长碰到输出上限时，回复不进入历史，带一条说明自动重发同一请求，最多 2 次；仍然失败时本轮报错，并写出停止原因、收到的内容块类型和建议。服务拒绝（`refusal`、`content_filter`）时不重发。此前空回复会写入历史，使用 Anthropic 协议时之后的请求都会被拒绝。界面状态行与 `-p` 显示重发，`stream-json` 新增 `reply_retry` 事件。
- 截断后自动续写：回复写到一半碰到输出上限时，自动请模型从中断处接着写，每轮最多 3 次，两段之间显示“↻ 回复达到输出上限，自动续写（1/3）”；`-p` 的结果、子 Agent 和 Skill 的返回取合起来的全文，`stream-json` 新增 `auto_continue` 事件。此前本轮直接停止，需要手动输入“继续”，`/goal` 也会因此暂停。
- 目标：新增 `/goal <条件>`，设置一个完成条件并立即开始，之后每轮结束由另一次不带工具的模型请求根据对话中的证据判断是否达成，未达成时把理由告诉模型并自动开始下一轮，达成或无法达成时清除目标并说明理由。本轮取消或出错、达到迭代上限、回复被截断、连续 3 轮没有调用工具、自动运行达到 `goal_max_turns`（默认 30）轮时暂停，发送消息或 `/goal` 继续；有后台任务在运行时等它们的通知那一轮结束再判断。`/goal` 查看条件、时间、轮数、Token 和最近的理由，`/goal clear` 或 `/clear` 清除；状态栏显示目标状态；目标写入会话记录，恢复会话时以暂停状态恢复。`goal_provider` 可以指定判断用的模型。不改变权限档位，`-p` 中不可用。
- 向用户提问：新增 `ask_user` 工具，模型遇到有歧义或需要你决定的地方时提出 1–4 个选择题，在输入框位置出现的面板中单选、多选或选“其他”自己输入，Esc 不回答；回答作为工具结果返回，本轮直接继续。提问与审批排队显示，计划模式中可用；只在交互界面的主对话中提供，`-p`、子 Agent 和团队成员中没有。恢复会话后回放中仍能看到回答。
- 任务清单：新增 `todo_write` 工具，模型处理多步任务时列出清单并边做边更新，输入框上方的面板显示进度和未完成的项，`/todo` 查看完整清单。清单变化后，快照作为提醒追加到对话末尾；随会话记录保存，恢复、`/rewind`、压缩和 `/clear` 时保持一致。子 Agent 和团队成员各有自己的清单。不需要审批，计划模式中可用；`config.yaml` 中 `todo_tool: false` 关闭。
- 后台命令：`execute_command` 新增 `run_in_background` 和 `description`，开发服务器、监视构建等长时间运行的命令在后台运行，调用立即返回任务 id；输出写入会话目录的 `commands/<任务 id>.log`，模型用 `read_file` 读取，命令结束时主对话收到通知（退出码、最后 20 行）。`task_output`、`task_stop`、`/tasks` 支持命令任务；界面中按 Ctrl+B 把前台命令转入后台。交互界面中不限时，`-p` 中默认 600 秒（`timeout` 最多 3600）；同时最多 8 个（`command_max_background`，0 关闭）；输出超过 256 MiB 时结束。沙箱中的后台命令联网不询问，没有规则的域名被拒绝并列在通知中。子 Agent 和团队成员的后台命令随它们结束。
- 插件：`moss plugin install|list|update|remove|enable|disable` 安装和管理用户级插件，一个插件可以包含 Skill、命令、子 Agent、MCP 服务器和 Hook，兼容 Claude Code 的插件布局（`.claude-plugin/plugin.json`、`commands/`、`.mcp.json`、`${CLAUDE_PLUGIN_ROOT}` 等）。来源可以是 git 仓库、GitHub 的 `owner/repo` 或本机目录，支持 marketplace 仓库（`<来源>#<插件名>`）；安装和更新前列出其中的命令并询问，标签和提交固定的插件不随更新移动。记录在 `~/.mosscode/plugins.yaml`，启动时不联网。对话中 `/plugins` 查看，`moss doctor` 检查插件。
- 项目级插件：`moss plugin install 来源 --project` 把插件声明到 `.mosscode/plugins.yaml`（只含来源、`ref`、路径，只接受 git 来源），随仓库共享；团队成员在项目中运行不带来源的 `moss plugin install` 逐个预览并安装，启动时提示尚未安装的声明。项目插件首次加载前在启动时确认，声明或插件的提交变化后再次询问；`-p` 不加载未确认的项目插件（stderr 说明原因），`--trust-project-plugins` 只在本次信任。项目插件覆盖同名的用户级插件；`moss plugin remove 名称 --project` 删除声明；`moss plugin list` 和 `moss doctor` 显示项目插件和确认状态。
- 命令沙箱（Windows）：`config.yaml` 中设 `sandbox.mode: on` 或用 `--sandbox` 启动后，模型运行的命令和 Skill 工具通过微软的 MXC（`wxc-exec.exe`，需自行安装）在容器中运行：只能写项目目录和私有临时目录，`.mosscode`、`.git/hooks`、`.git/config` 等只读，项目外只能读系统目录和工具目录，`~/.ssh` 等凭据目录不可读。沙箱中的命令不再逐条审批（deny 规则和高风险命令照旧）；模型需要越界时带理由申请 `outside_sandbox`，每次询问，最多记住本会话。联网只经 MossCode 为每条命令开的回环代理：域名按 `WebFetch(domain:…)` 规则判断，没有规则时询问（本次、本会话、永久、拒绝），`-p` 中拒绝；不能直连 IP、本机和内网，TLS 主机名与申请的不同时断开，支持经用户的 `HTTPS_PROXY` 出网；`sandbox.network: none` 完全断网。沙箱不可用时命令被拒绝而不是不带沙箱运行。Windows 上沙箱中的命令改用 PowerShell。新增 `/sandbox`、`--sandbox`/`--no-sandbox`，状态栏显示沙箱状态，`moss doctor` 检查 MXC。
- 命令沙箱的其他后端：Linux 用 bubblewrap（需安装 `bwrap`；只读挂载整个文件系统、遮住凭据目录、独立网络命名空间，经 Unix 套接字桥接到 MossCode 的代理，seccomp 禁止新建 Unix 套接字），macOS 用系统自带的 Seatbelt（`sandbox-exec`，尚未在真机上验证）。Windows 上 MXC 不可用时改用受限令牌：只限制写入，读取和网络不受限，命令仍需审批，状态栏显示“沙箱·部分”，审批面板注明“沙箱只限制写入”，启动区说明为何没有用 MXC；它在项目目录上加的访问控制记录在 `~/.mosscode/sandbox-acl.json`，用新的 `moss sandbox cleanup` 移除，Everyone 可写的目录会提示。`sandbox.backend` 可指定后端；`moss doctor` 按系统检查可用的后端。
- 命令沙箱默认关闭，第一次启动界面时在启动区提示一次怎样打开（`--sandbox` 或 `sandbox.mode: on`）。
- `config.yaml` 新增 `claude_directories`：为 `true` 时另外读取 Claude Code 的 `.claude/skills/`、`.claude/agents/`（用户级和项目级）；未开启而项目中有这些定义时启动区提示一次。

### 变更

- 缓存：Anthropic Messages 协议对任何服务地址都默认加缓存标记，经 CC Switch 等中转使用 Claude 时也能命中缓存（以前只有官方地址才加，中转的命中率接近 0）。服务因标记拒绝请求时自动去掉标记重发，之后不再加；`prompt_cache: false` 关闭。
- 界面外观：启动区换成两行高的彗星 logo，目录中的用户主目录缩写为 `~`，在 Git 仓库中显示当前分支，并多一行提示；耗时、结果摘要、说明和按键提示改用固定的灰色，正文不变；任务运行时输入框上方的一行显示转圈符号、当前阶段、轮次和耗时，状态栏不再显示运行阶段，上下文用量和缓存命中率靠右对齐；输入框的 `❯` 为紫色，上下的横线随权限档位变色；命令提示、审批、提问、模型和成员列表的选中项用 `❯` 标出；回复中的标题左对齐，标了语言的代码块按语言着色；“思考用时 N s”改为“✻ 思考 N s”，计划模式的标记由 `⏸` 改为 `‖`。
- 回复边收边按 Markdown 显示：标题、列表、表格和代码块在输出过程中就排好版，写完的部分不再变化，完整收到时不再整段跳变；排版与写入滚动历史的相同。取消或出错时，已收到的部分保持 Markdown 显示，空一行后是说明（此前变回纯文本）。有序列表的序号带点（“1.”，此前是“1”）。输出过程中回复上方不再多出一个空行。
- 界面间距统一为一行：启动区与输入框之间、“✻ 完成”与输入框之间不再空三行；输入框上方的运行状态行只在任务运行时出现。命令候选的按键提示与状态栏之间空一行。`/model`、`/resume`、`/rewind`、`/thinking` 列表中的说明未选中时为灰色；成员窗格的审批选项与主界面一样用 `❯` 标出选中项、说明为灰色，鼠标点击不再直接回答；候选列表的滚动条改为一列灰色细条（此前是两列宽的黑块）。
- 审批和提问面板出现在输入框的位置：输入框暂时隐藏，面板上下各一条琥珀色横线，视线不用在输入框和下方的面板之间移动；输入框里没发送的文字在面板撤下后原样恢复，提问选“其他”时输入框重新出现。成员窗格中也一样，审批出现在输入框的位置，按选项的序号直接回答。审批面板只占它需要的行数，等待确认的那一行工具调用留在面板上方（此前会被挤出可见区域）。
- 浅色终端配色：启动交互界面时向终端查询背景色（OSC 11，不回答时看 `COLORFGBG`），浅色背景换用一套更深的强调色和灰色，文字对比度都不低于 4.5:1，logo 改为从浅蓝到深蓝，团队成员窗口也换用浅色背景；`MOSSCODE_THEME=light` 或 `dark` 可直接指定。此前强调色和灰色在白底上只有约 1.7:1 到 3:1，偏淡难读。
- 模型运行的命令（`execute_command`、Skill 工具）看不到形似密钥的环境变量：名称含 TOKEN、SECRET、PASSWORD 等，或值形似私钥、JWT、服务令牌、带密码的 URL；`config.yaml` 新增 `command_env.keep` / `drop` 调整。这些命令的 `TEMP`、`TMP`、`TMPDIR` 改为每个 MossCode 进程私有的 `~/.mosscode/tmp/` 子目录，退出时删除；Windows 上不能读取剪贴板。Hook 和 `!` 命令不变。
- 项目的 `.mosscode/config.yaml` 不能再放宽命令沙箱和命令的环境变量：`sandbox` 和 `command_env` 以用户配置 `~/.mosscode/config.yaml` 为准，项目配置只能收紧（`mode: on`、`network: none`、`allow_unsandboxed: never`、追加 `deny_read` 和 `command_env.drop`）；`allow_write`、`allow_read`、`backend`、`wxc_exec`、`command_env.keep` 和关闭沙箱的写法被忽略，并在启动区和 `moss doctor` 中列出。**不兼容**：原来写在项目配置里的这些设置需要移到用户配置。
- 项目的 `.mosscode/config.yaml` 在你确认之后才会被使用：第一次使用和内容变化后，交互界面先列出每个 Provider 的服务地址主机名和它引用的凭据名称再询问，拒绝时改用用户配置；`-p` 中未确认的项目配置报错退出，加 `--trust-project-config` 本次信任；`moss doctor` 指出尚未确认的项目配置。**不兼容**：已有项目配置的项目，下一次启动会询问一次，脚本中的 `-p` 需要先确认或加这个选项。
- 模型不能用 `write_file`、`edit_file` 写 `.git/hooks/`、`.git/config` 和项目的 `.mosscode/plugins.yaml`。
- 命令文件名中的 `_` 视为 `-`：`clean_gone.md` 注册为 `/clean-gone`，Claude Code 插件中这类命令不再被跳过。
- 子 Agent 的 `model` 写 Claude Code 的模型名（`sonnet`、`opus`、`haiku`、`fable` 或 `claude-…`）且没有同名 Provider 时，使用当前模型，不再报错。
- PowerShell 命令出错时，报错的位置行显示命令本身，不再显示 MossCode 加在前面的编码设置。

### 修复

- 克隆的仓库在启动时就能运行任意命令：项目的 `.mosscode/mcp.yaml`（以及开启 `import_mcp_json` 时的 `.mcp.json`）中的 stdio 服务器此前在会话开始时直接启动。现在和项目配置、项目插件一样先确认：界面列出每个服务器要运行的命令后询问，内容有变化再次询问；`-p` 不启动未确认的项目服务器，`--trust-project-mcp` 只在本次信任；`moss doctor` 指出未确认的项目服务器。见 [项目的 MCP 服务器需要确认](docs/reference/mcp.md#项目的-mcp-服务器需要确认)。
- 缓存命中率被一次性请求拉低：底栏的“缓存命中”此前把后台记忆整理和 `/goal` 判断也算进去，这些请求各用自己的提示，几乎不会命中，对话本身缓存得再好，显示的比例也偏低。现在命中率只算对话请求（主对话、子 Agent、团队成员）；`/status` 的用量一行另写“对话缓存命中率”，并单独列出摘要、目标判断和记忆整理的次数、输入和缓存命中。压缩和折叠的摘要请求此前没有计入用量和费用，现在也计入。
- DeepSeek 等自动缓存的服务命中率偏低：任务清单快照、`/goal` 的目标、Hook 的 `<hook-notification>`、模式提醒和恢复会话后的一次性提醒此前只附在每次请求的末尾、不进入历史，下一次请求就不以上一次请求的全部内容开头，而 DeepSeek 只复用某次请求完整的输入作为前缀。现在这些提醒在需要时作为一条消息追加到对话末尾并写入历史和会话存档，之后原样带上，清单、目标和 Hook 输出只在内容变化时再追加；界面、`/rewind` 列表和目标判断不显示它们。恢复会话时，末尾没有得到回复的提醒与未回复的消息一起移除。
- `/agents <名称>` 把 `todo_write` 列在隔离的子 Agent 的“隔离时不可用”中，实际上它在工作树中有自己的任务清单；现在列在“可用工具”里。
- 同一批并行读取的文件没有合计上限：一次并行读 10 个大文件会有约 140,000 Token 进入历史，窗口较小的模型直接超出窗口。现在一批结果合计超过 50,000 Token 时，`read_file` 的结果按平均份额缩短成更短的一页，结尾注明从哪个 offset 继续读取；小文件保持完整，每个读取至少保留 2,000 Token。
- 沙箱中启动的服务器（如后台运行的开发服务器）从别处连不上：MXC 和 Seatbelt 下连同一条命令也连不上，bubblewrap 下只有同一条命令能连接。MXC 的这个限制无法由 MossCode 放开，现在工具说明按后端告诉模型，要访问的服务器和访问它的命令都申请在沙箱外运行；MXC 拒绝套接字的报错（`WinError 10013`）也会附上沙箱说明。文档中“MXC 下命令的监听可以被本机和局域网连接”的说法不正确，已改正。
- Windows 上按系统代码页输出的程序（如中文 Windows 上的 `ping`、`ipconfig`，代码页 936/GBK）在 `execute_command` 的结果、后台命令的输出文件、通知和 `task_output` 中不再显示为乱码：不是合法 UTF-8 的行改按 OEM 代码页解码，后台命令的输出文件转成 UTF-8 写入。

## [0.2.0] - 2026-11-04

### 新增

- `web_fetch` 读取网页：HTML 转为 Markdown，长页面按 `offset` 分段，同一会话缓存 15 分钟；拒绝本机和内网地址，连接固定到检查过的 IP；跨主机重定向交给模型决定；支持 `HTTPS_PROXY`。
- `web_search` 网络搜索，首先支持 Tavily，在 `config.yaml` 的 `web_search` 中配置；每个会话最多 100 次。
- 权限规则 `WebFetch(domain:…)` 和 `WebSearch`，审批时可按域名授权本会话或永久允许；`moss doctor` 和 `/status` 显示网络搜索的配置状态。
- 图片输入：`read_file` 可以查看图片（PNG、JPEG、GIF、WebP、BMP），MCP 工具返回的图片也会发给模型；图片自动转正、缩放和压缩，保存在会话目录的 `images/` 中，会话记录只保存引用。Provider 新增 `images` 配置，不接受图片的模型收到文字说明。新增依赖 Pillow。
- 粘贴和拖入图片：Alt+V（可用时也包括 Ctrl+V）附加剪贴板中的截图或复制的图片文件，拖入或粘贴图片路径也会附加；输入框中显示为 `[图片 #N]`，删掉占位符即取消；排队消息和调出的历史消息保留图片；`/status` 显示当前模型能否接收图片。
- 模型目录：随程序附带 LiteLLM 的模型数据，`context_window`、`max_output_tokens`（最多 32,000）和 `images` 未配置时按模型自动取值，不再一律按 128K 估算；模型名是别名时用新配置项 `catalog_model` 指定条目。新增 `moss models [关键词]` 查找条目、`moss models update` 下载最新数据；`/status` 和 `-p` 的 json 结果（`total_cost_usd`）显示按目录价格估算的费用；`moss setup` 和 `moss doctor` 显示目录的匹配结果。
- 内置 Skill `/init`：分析项目后生成或补充 `AGENTS.md`，写入前显示改动并按档位审批。
- 检查点与 `/rewind`（别名 `/checkpoint`）：`write_file`、`edit_file` 改文件前自动保存快照（包括主工作区中的子 Agent），可以把文件、对话或两者退回到之前某条消息发出前的状态，回退后的消息放回输入框；空闲且输入框为空时连按两次 Esc 打开。检查点随会话存档保存，恢复会话后仍可回退。
- `@` 文件引用：输入 `@` 列出项目中的文件和目录（遵守 `.gitignore`，模糊匹配），Tab 或 Enter 补全；发送时附上引用的文件内容（与 `read_file` 的结果相同，记为已读，可以直接编辑）和目录列表，`@文件#L10-20` 只附这几行；一条消息最多 10 个引用、约 112 KiB，显式拒绝规则仍然生效；`-p` 同样展开。
- `!命令`：在输入框中不经过模型直接执行命令（与 `execute_command` 同一个 shell，项目根目录，不需要审批，黑名单和显式拒绝规则仍然生效，Esc 取消）；输出加入对话但不触发回复，下一条消息时模型能看到；`!!命令` 只执行不加入对话。
- `/thinking off|low|medium|high|default`：在会话中调整思考强度，按协议换算为 Anthropic 的 adaptive 加 effort（较早的模型用思考预算）、OpenAI 的 `reasoning.effort` / `reasoning_effort`、DeepSeek 的思考开关；模型目录表明不支持时提示并忽略。`/status` 和状态栏显示当前档位。模型目录新增思考相关的字段，Provider 新增 `reasoning` 配置。
- 系统凭据管理器保存 API 密钥：Provider 和 `web_search` 新增 `api_key_keyring`，`moss key set|delete 名称` 保存或删除（Windows 凭据管理器、macOS 钥匙串、Linux Secret Service），`moss setup` 有可用的凭据管理器时默认用它，`moss doctor` 显示条目状态。新增依赖 keyring。
- 密钥保护：`execute_command`、`!` 命令、Hook 和 Skill 工具的进程不再继承 `api_key_env` 指向的变量；工具结果、`!` 输出、Hook 输出和进入对话的消息中完整出现的密钥替换为 `[已隐藏的密钥]`；`write_file`、`edit_file` 拒绝写入这个占位符；配置文件在项目目录中且含明文密钥时启动提示。
- Skill 另外从 [Agent Skills](https://agentskills.io/specification) 的共享目录 `~/.agents/skills/`、`.agents/skills/` 读取；按规范检查 `SKILL.md`，不认识的字段只提示；`/skills <名称>` 显示 `license`、`compatibility`；白名单中的 `WebFetch`、`WebSearch` 换算为 `web_fetch`、`web_search`。

### 变更

- 团队文件改用操作系统锁（`filelock`）：持有锁的进程崩溃后锁立即释放，不再要等 10 秒过期。
- 命令的灾难级和高风险特征也按 bash 语法（tree-sitter）拆出的子命令检查，引号、转义、`$(…)` 和子 shell 不再能绕过；显式 deny 规则同样按子命令匹配。
- Skill 名改按 Agent Skills 规范：可以以数字开头；以 `-` 结尾或含 `--` 的名称不再接受（不兼容）。
- 状态栏不再显示 Token 数、缓存读写数和模型名（`/model` 切换后仍显示新模型），改为“上下文 约 9K / 1M（1%）”和会话累计的“缓存命中 N%”；Token 数和费用见 `/status`。
- Skill 不符合 Agent Skills 规范的写法（如不认识的字段）启动时不再提示，改在 `/skills` 中列出。
- 没有 ripgrep 时，内置搜索用 `pathspec` 匹配 `.gitignore`，转义（如 `\[draft\].md`）和行尾空格等写法与 git 一致。

## [0.1.0] - 2026-11-03

首个公开版本。

### 新增

- 终端交互界面（Textual 内联模式，完成的内容进入终端原生滚动历史）和无界面模式 `-p`，输出格式 `text`、`json`、`stream-json`。
- 三种模型协议：Anthropic Messages、OpenAI Responses、Chat Completions；多 Provider 切换，首个事件前自动重试，缓存断点。
- 首次运行的配置引导 `moss setup` 和诊断命令 `moss doctor`。
- 本地工具：读、写、编辑、命令、glob、grep，只读的计划模式；相邻的只读调用并发执行，大结果存盘。
- 四个权限档位（default、acceptEdits、plan、bypassPermissions），用户、项目和本机三层规则，命令风险分级和项目路径检查。
- MCP 外部工具：stdio 与 HTTP 传输，后台连接、工具发现和变更订阅。
- 上下文估算与自动压缩、会话实时存档与恢复（`-c`、`--resume`、`/resume`）。
- 记忆笔记：`memory` 工具、后台提取、用户级与项目级索引。
- 项目指令 `AGENTS.md`（兼容 `CLAUDE.md`），支持 `@路径` 引用。
- Skill：`SKILL.md` 规程与脚本工具，内置 `/commit`、`/review`、`/test`。
- Hook：会话、轮次、请求和工具调用前后的命令、提示注入、HTTP 和 Agent 动作，项目级 Hook 需确认后启用。
- 子 Agent：内置 explore、plan、general，自定义定义，Fork 方式，后台任务与通知。
- Git 工作树隔离：创建、初始化（链接与复制）、改动检查、安全删除和过期清理。
- Agent 团队：共享任务、成员邮箱、各自的工作树、Windows Terminal 分屏或进程内运行、确认后逐个合并。

[Unreleased]: https://github.com/yzd11/mosscode/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/yzd11/mosscode/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/yzd11/mosscode/releases/tag/v0.1.0
