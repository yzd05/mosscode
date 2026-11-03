# 产品差距评审：对照 Claude Code 与 pi

日期：2026-11-02。范围：作为给别人使用的开源产品，MossCode 与 Claude Code、pi 还差什么；哪些地方自己造了轮子，可以换成成熟的库或做法。本文是评审和规划，没有改动代码。上一次评审见 [claude-code-pi-gap-review.md](claude-code-pi-gap-review.md)（2026-10-15，其中大部分建议已在后续阶段完成）。

依据：
- MossCode：`src/mosscode`（约 20,100 行）、`docs/reference/*.md`、`pyproject.toml`。
- pi：`earendil-works/pi` 的 `packages/coding-agent` 文档（README、usage、slash-commands、skills、configuration、packages、providers、sessions、settings、keybindings，2026-11-02 下载）。
- Claude Code：公开文档与工具契约（[Tools reference](https://code.claude.com/docs/en/tools-reference) 等），以及本会话中对其行为的观察。
- 依赖信息：PyPI JSON 接口（2026-11-02 查询）；LiteLLM 模型目录 `model_prices_and_context_window.json`（同日下载）。

## 结论

1. **核心能力已经是产品级的，差距在“第一次用”和“日常顺手”。** Agent 循环、工具契约、权限、计划模式、MCP、Skill、Hook、子 Agent、工作树、团队、会话、压缩、记忆、无界面输出都已具备，并且大多数按 Claude Code 的契约实现。差的是：装上就能用（模型信息、凭据、更新）、输入时的便利（`@` 文件、`!` 命令、外部编辑器、复制）、出错后能退回（检查点）、与其他工具共享配置（Skill 目录、插件包）。
2. **自己造的轮子集中在基础设施，不在核心。** 重的部分已经在用成熟的库：Textual（界面）、官方 `anthropic`/`openai` SDK、官方 `mcp` SDK、pydantic、jsonschema、httpx。自己写的是几处基础设施：文件锁、`.gitignore` 匹配、命令危险性识别（正则）、手动填写上下文窗口、明文保存密钥。这些都有成熟方案，见“三、可以换成成熟方案的部分”。
3. **不建议照搬的：** 用 LiteLLM 取代自己的 Provider 层、订阅账号登录（OAuth）、IDE 插件、插件市场。理由见“四、不建议照搬”。

## 一、已经对齐的能力

| 能力 | Claude Code | pi | MossCode |
| --- | --- | --- | --- |
| 项目指令 | `CLAUDE.md` | `AGENTS.md`、`CLAUDE.md` | 两者都读，支持 `@路径` 引用 |
| 工具契约 | Read/Write/Edit/Bash/Glob/Grep | read/write/edit/bash（另有 grep/find/ls） | 同 Claude Code：先读后改、`replace_all`、头尾截断、`.gitignore` |
| 权限 | 档位 + `Tool(arg)` 规则 | 默认不限制，靠扩展或容器 | 四档 + 规则 + 硬限制，Shift+Tab 切换 |
| 计划模式 | 有 | 不内置 | 有，`exit_plan_mode` |
| MCP | stdio/HTTP，`.mcp.json` | stdio/HTTP，`.pi/mcp.json` | stdio/HTTP，可导入 `.mcp.json` |
| Skill | `.claude/skills` | Agent Skills 规范 | `SKILL.md`，用户级/项目级/内置 |
| Hook | settings.json 中的 hooks | 扩展事件 | `hooks.yaml`，四种动作，项目级需信任 |
| 子 Agent、团队、工作树 | 有 | 不内置（靠包） | 有 |
| 会话 | `-c`、`--resume` | 同，另有会话树 | `-c`、`--resume`、`/resume` |
| 压缩、记忆 | 有 | 有压缩 | 有 |
| 排队消息 | 有 | steering / follow-up | 有，在迭代边界合并 |
| 无界面 | `-p`、json、stream-json | print、json、RPC、SDK | `-p`、json、stream-json |
| 安装与诊断 | 安装脚本、`claude doctor` | 安装脚本、`pi update` | `uv tool install`、`moss setup`、`moss doctor` |

## 二、差距

优先级：**高** = 别人第一次使用就会碰到，或影响能否放心使用；**中** = 日常使用明显不便；**低** = 有更好，没有也能用。

### A. 安装与上手

| # | 差距 | Claude Code / pi 的做法 | 建议 | 优先级 |
| --- | --- | --- | --- | --- |
| A1 | 上下文窗口、输出上限要用户自己填，填错会过早或过晚压缩；没有价格信息，只能显示 Token | pi 内置模型目录（上下文、输出上限、价格），`/model` 中直接选择 | 内置一份模型目录快照（见 R1），按 Provider 的模型名自动取上下文窗口和输出上限；配置中的值优先；`/status` 显示估算费用 | 高 |
| A2 | `api_key` 只能写成明文或环境变量；Windows 上设置环境变量对新手不友好 | pi 的 `/login` 把密钥存入 `auth.json`；Claude Code 在 macOS 上用钥匙串 | `moss setup` 增加第三种保存方式“系统凭据管理器”（Windows 凭据管理器、macOS 钥匙串、Linux Secret Service，见 R5），配置中写 `api_key_keyring: deepseek`；默认仍推荐环境变量 | 中 |
| A3 | 没有生成项目指令的命令，用户要自己写 `AGENTS.md` | Claude Code `/init` 分析项目后生成 `CLAUDE.md` | 内置 Skill `/init`：读取项目结构、构建和测试命令，生成或补充 `AGENTS.md`，写入前显示 diff 审批 | 中 |
| A4 | 不知道有新版本 | Claude Code 自动更新；pi `pi update` | 启动时（每天最多一次，后台）查询 PyPI 最新版本，有新版时在启动区提示 `uv tool upgrade mosscode`；可在配置中关闭；不自动安装 | 低 |

### B. 输入与显示

| # | 差距 | Claude Code / pi 的做法 | 建议 | 优先级 |
| --- | --- | --- | --- | --- |
| B1 | 输入中不能引用文件 | 两者都支持输入 `@` 后模糊搜索文件，选中后插入路径 | 输入 `@` 时弹出文件候选（`rg --files` 或内置遍历，遵守 `.gitignore`），用 Textual 自带的模糊匹配排序（R7）；发送时附上文件内容（受大小限制，超过时只给路径让模型自己读） | 高 |
| B2 | 不能在输入框中直接执行命令 | 两者都支持 `!命令`，输出进入对话；pi 的 `!!` 只执行不发送 | `!命令` 执行（走 `execute_command` 的同一套 shell 和硬限制，用户自己输入的命令不需要审批），输出作为一条用户消息进入上下文 | 中 |
| B3 | 长提示只能在输入框里写 | pi `Ctrl+G`、Claude Code `Ctrl+G` 打开外部编辑器 | `Ctrl+G` 用 `$VISUAL`/`$EDITOR`（Windows 默认 `notepad`）编辑当前输入，用 Textual 的 `App.suspend()` 交出终端 | 低 |
| B4 | 不能复制模型的回答 | pi `/copy`、`Ctrl+X`；Claude Code `/copy` | `/copy` 复制最后一条回答，用 Textual 的 `copy_to_clipboard`（OSC 52，Windows Terminal 支持） | 低 |
| B5 | 不能调整思考强度 | pi `/thinking` 和 `Shift+Tab` 循环；Claude Code 按模型提供 effort | `/thinking [off\|low\|medium\|high]`，按协议映射到 Anthropic 的 thinking budget、OpenAI 的 `reasoning_effort`；不支持的模型提示并忽略 | 中 |
| B6 | 历史只能按 ↑/↓ 逐条翻 | Claude Code `Ctrl+R` 搜索历史 | `Ctrl+R` 在输入历史中搜索 | 低 |

### C. 会话

| # | 差距 | Claude Code / pi 的做法 | 建议 | 优先级 |
| --- | --- | --- | --- | --- |
| C1 | 模型改错了文件只能靠 git 或手动恢复 | Claude Code 每次编辑前自动保存检查点，`Esc Esc` / `/rewind` 可以把代码和对话一起退回到某条消息之前 | 在 `write_file`/`edit_file` 写入前保存原内容（按会话、按用户消息分组，存在会话目录）；`/rewind` 列出用户消息，选择后恢复文件、截断对话，可只恢复其一。命令修改的文件不在范围内，界面上说明 | 高 |
| C2 | 不能从中间某条消息另起分支 | pi 的会话树：`/tree`、`/fork`、`/clone` | 先做 `/fork`：从选中的用户消息复制出一个新会话（新 ID、原会话不变）。会话树暂不做 | 低 |
| C3 | 不能导出会话 | pi `/export` 输出 HTML 或 JSONL；Claude Code `/export` 输出文本 | `/export [路径]` 输出 Markdown（对话、工具调用摘要）；HTML 可用 Rich 的 `Console.export_html`（Textual 已依赖 Rich） | 低 |

### D. 与其他工具共享配置（生态）

| # | 差距 | Claude Code / pi 的做法 | 建议 | 优先级 |
| --- | --- | --- | --- | --- |
| D1 | Skill 只从 `.mosscode/skills` 读取，别人为其他 agent 写的 Skill 要复制一份 | pi 实现 [Agent Skills 规范](https://agentskills.io/specification)，并读取 `~/.agents/skills/`、`.agents/skills/`；Claude Code 读取 `.claude/skills/` | 按 Agent Skills 规范校验 `SKILL.md`（不认识的字段只警告）；另外读取 `.agents/skills/`、`~/.agents/skills/`，可选读取 `.claude/skills/`（与 `import_mcp_json` 同样由用户开启） | 高 |
| D2 | 子 Agent 已兼容 `.claude/agents` 的写法，但需要用户自己复制 | Claude Code 读取 `.claude/agents/` | 与 D1 一起：可选读取 `.claude/agents/` | 中 |
| D3 | Skill、子 Agent、Hook、MCP 只能逐个复制到目录中，不能作为一个整体分享和更新 | pi 的包：`pi install npm:…`、`git:…`、本地路径；Claude Code 的插件 | `moss plugin install <git 地址或本地路径>`：一个目录中按约定放 `skills/`、`agents/`、`hooks.yaml`、`mcp.yaml`；安装到 `~/.mosscode/plugins/`，`moss plugin list/remove/update`；项目级插件同样需要信任。不做市场 | 中 |
| D4 | Hook 使用自己的 YAML 格式，Claude Code 的 Hook 配置不能直接用 | Claude Code 的 hooks 写在 settings.json | 暂不做。两种格式的事件和字段差别较大，强行兼容会让两边都不准确；在文档中给出对照表 | 低 |

### E. 安全

| # | 差距 | Claude Code / pi 的做法 | 建议 | 优先级 |
| --- | --- | --- | --- | --- |
| E1 | 命令的危险性靠正则识别，引号、变量、`&&` 组合容易绕过或误判 | Claude Code 解析 shell 语法树后逐条判断子命令 | 用 tree-sitter 的 bash 语法（R4）把命令拆成子命令，再按现有规则判断；PowerShell 仍用现在的规则 | 中 |
| E2 | 没有系统级隔离 | Claude Code 在 Linux/macOS 上用 bubblewrap/Seatbelt 沙箱；pi 推荐在容器中运行 | 不自己实现沙箱。README 和 `CONTRIBUTING.md` 给出在 Docker / Dev Container 中运行的方法；`docs/reference` 写明当前的边界 | 中 |

### F. 工程质量（作为开源项目）

| # | 差距 | 成熟项目的做法 | 建议 | 优先级 |
| --- | --- | --- | --- | --- |
| F1 | 没有代码风格检查和类型检查 | 绝大多数 Python 开源项目用 ruff 检查和格式化，用 mypy 或 pyright 检查类型 | 引入 ruff（先只开默认规则，一次性修正）；类型检查从核心模块开始，CI 中运行 | 高 |
| F2 | 没有 CHANGELOG 和发布说明 | Claude Code、pi 都有 changelog，pi 还有 `/changelog` | 新增 `CHANGELOG.md`（Keep a Changelog 格式），发布时 GitHub Release 引用它 | 中 |
| F3 | 没有问题模板和安全报告方式 | 开源项目的 issue 模板、`SECURITY.md` | 新增 bug / 功能 issue 模板和 `SECURITY.md`（私下报告漏洞的方式） | 中 |
| F4 | 没有可重复的任务评测，改提示词和工具后不知道变好还是变坏 | pi 公开会话数据用于评测；业界用 Terminal-Bench、SWE-bench 等 | 后续：在 `tests/` 之外建一个小的评测集（几个真实修复任务 + 断言），用 `-p --output-format json` 跑，记录完成率、轮数、Token | 低 |

## 三、可以换成成熟方案的部分

| # | 现在的实现 | 换成 | 许可证 | 说明 |
| --- | --- | --- | --- | --- |
| R1 | 上下文窗口、输出上限由用户填写，缺省按 128K 估算（`config.py`、`setup.py`） | LiteLLM 的模型目录 `model_prices_and_context_window.json`（只用这份数据，不引入 LiteLLM 包） | MIT | 2026-11-02 下载的版本有 4,553 个模型，包括 `deepseek/deepseek-v4-flash`（输入 1,000,000、输出 393,216）。发布时把精简后的快照打进包里（只保留上下文、输出上限、价格），可用 `moss models update` 手动刷新。models.dev 也是常用的数据源，本机未能访问，未核实 |
| R2 | 团队文件锁（`teams/lock.py`，163 行：锁文件、过期判断、重试） | `filelock` | MIT | 跨平台、处理进程退出后的过期锁；保留现有的“写临时文件再替换”逻辑 |
| R3 | 无 ripgrep 时的搜索实现自己解析 `.gitignore`（`tools/search.py`、`search_worker.py`） | `pathspec` 的 gitignore 匹配 | MPL-2.0（只作为依赖使用，与 MIT 兼容） | `.gitignore` 的否定规则、目录规则、`**` 等边界很多，交给专门的库更可靠。PyPI 的 `ripgrep` 包（15.1.0）只有 macOS arm64 和 Linux x86_64 的 wheel，没有 Windows，不能用它替代内置实现 |
| R4 | 危险命令用正则识别（`security.py`） | `tree-sitter` + `tree-sitter-bash` | MIT | Claude Code 也是先解析语法再判断。`bashlex` 是 GPL-3.0，不适合 MIT 项目 |
| R5 | 密钥只能明文或环境变量 | `keyring` | MIT | 统一访问 Windows 凭据管理器、macOS 钥匙串、Linux Secret Service；作为可选依赖 |
| R6 | 网页转 Markdown（完善九计划中） | `beautifulsoup4` + `markdownify` | MIT | 已写入完善九规格 |
| R7 | 文件候选的匹配排序（B1，尚未实现） | Textual 自带的 `textual.fuzzy.Matcher` | MIT（已依赖） | 不需要新依赖；需要更快时再换 `rapidfuzz`（MIT） |
| R8 | 剪贴板、外部编辑器、导出 HTML（B3、B4、C3，尚未实现） | Textual 的 `copy_to_clipboard`、`App.suspend()`，Rich 的 `export_html` | MIT（已依赖） | 不需要新依赖 |
| R9 | 打包、发布、CI（完善九计划中） | `uv build`、`pypa/gh-action-pypi-publish`（Trusted Publishing）、`astral-sh/setup-uv` | MIT / Apache-2.0 | 已写入完善九规格 |
| R10 | 没有风格检查（F1） | `ruff` | MIT | 一个工具同时做检查和格式化 |

保留自己实现、不换的部分：

- **Provider 层（`providers.py`，约 500 行）**：三种协议覆盖了 Anthropic、OpenAI 和绝大多数兼容服务（DeepSeek、Qwen、Kimi、本地 Ollama/llama.cpp）；重试、缓存断点、流式事件都按 MossCode 的需要处理。换成 LiteLLM 会引入大量依赖，而且缓存断点、思考块等细节反而更难控制。只借用它的模型数据（R1）。
- **请求重试**：SDK 自带的重试只覆盖建立连接阶段，MossCode 需要“收到第一个事件之前可以重试、之后不重试”的语义，现有实现更合适。
- **Windows 进程树（`tools/windows_job.py`）**：Job Object 是 Windows 上可靠结束整棵进程树的标准做法，`psutil` 的逐个结束有竞争条件。

## 四、不建议照搬

| 能力 | 理由 |
| --- | --- |
| 订阅账号登录（pi 的 `/login` OAuth） | 用订阅账号驱动第三方客户端要遵守各服务商的条款，风险由项目承担；API 密钥已经够用 |
| 会话树（pi `/tree`） | 实现和界面都复杂；`/fork`（C2）和检查点（C1）覆盖了主要需求 |
| IDE 插件、浏览器扩展、桌面应用 | 超出一个终端 agent 的范围 |
| 插件市场 | 先支持从 git 地址安装（D3），有了用户再考虑 |
| 自己实现系统沙箱 | 不同系统差别很大，做不好反而给人错误的安全感；推荐容器（E2） |
| 图片输入 | 用户已决定先不做 |

## 五、建议的顺序

完善九（公开发布）之后，按“第一次用就会碰到”优先：

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| 完善九 | 公开内容、许可证、打包发布、CI、网络工具，加上 F1（ruff）、F2、F3 | F1–F3 是开源项目的基本配套，和 CI 一起做最省事 |
| 完善十：上手与安全网 | A1 + R1（模型目录）、C1（检查点 `/rewind`）、B1（`@` 文件）、D1（Agent Skills 目录） | 别人第一次用最容易碰到：不知道填什么上下文窗口、改坏了不能退回、不能引用文件、已有的 Skill 用不上 |
| 完善十一：日常顺手 | B2（`!命令`）、B5（`/thinking`）、A3（`/init`）、A2 + R5（凭据管理器）、R2、R3 | 日常使用的便利和基础设施替换 |
| 完善十二：生态与安全 | D3（插件安装）、D2、E1 + R4、E2 | 分享配置、更可靠的命令识别 |
| 以后 | A4、B3、B4、B6、C2、C3、F4 | 有更好 |
