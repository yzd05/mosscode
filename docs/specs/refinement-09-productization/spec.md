# 完善九：作为可分发的产品

日期：2026-11-02。状态：规格待评审，未实现。

来源：
- 用户的问题：MossCode 现在是否已经和 Claude Code 一样，能加入自己的 MCP、Skill，有项目级的 md 文件，像真正的 coding agent 一样给别人使用，而不是一个 demo。
- 我的回答（同日）：核心能力（AGENTS.md / CLAUDE.md、MCP、Skill、Hook、子 Agent、工作树、团队、权限、会话、无界面模式、安装与诊断）已经具备；给别人用还缺：许可证、公开的发布渠道、网络工具、多平台验证与 CI。用户同意按这个方向改进。
- 第一版评审（同日）中用户的意见：
  - 图片输入太复杂，先不做；
  - 公开的内容中不要出现学习用的参考资料；
  - 上传的版本要像 pi 一样都是给别人用的，自己开发时用的东西（例如 `code-spec` Skill）不应该在里面。
- 第二版评审（同日）中用户的决定：
  - 采用两个仓库（Q3）；
  - 学习时购买的参考资料不上传；用户确认自己的项目可以开源；
  - 项目要尽可能像真正的产品，能借鉴成熟产品和库的地方不要自己造轮子。为此做了 [产品差距评审](../../research/product-gap-review.md)，其中开源项目的基本配套（EQ）并入本完善，其余按评审中的顺序放到后续完善。
- Claude Code 文档（2026-11-02 查阅），[Tools reference](https://code.claude.com/docs/en/tools-reference)：
  - `WebFetch` 接受 URL 和提取要求，HTML 转为 Markdown 后用另一次模型调用按要求提取，Claude 收到的是提取结果（“lossy by design”）；
  - 拒绝 `localhost` 和不带点的主机名；HTTP 自动升级为 HTTPS；每次最多读取 100,000 个字符，并说明还有多少未读；每个 URL 缓存 15 分钟；跨主机的重定向不跟随，而是把目标告诉模型；
  - 默认档位中按域名询问，可选“本次允许”或“以后不再询问这个域名”（保存为 `WebFetch(domain:...)` 规则）；有一组预先放行的文档域名；
  - `WebSearch` 使用 Anthropic 的服务端搜索，只返回标题和 URL，不读取页面；可以限定或排除域名；规则不带参数；后端不能更换，要用别的搜索服务需接入 MCP；每个交互会话最多 200 次。

## 问题

- **P1 没有许可证。** 仓库中没有 LICENSE，包的元数据也没有许可证。别人拿到代码不能确定是否可以使用、修改和再分发。
- **P2 别人装不上。** 只能从本机路径安装（`uv tool install --editable F:\...`）。没有远程仓库，也没有发布到 PyPI。
- **P3 仓库里混着自己开发用的东西。** 除了产品代码、测试和使用文档，仓库还跟踪了开发用的 Skill、各阶段的规格与验收记录、学习和调研笔记、只在本机有意义的验证脚本；代码注释和测试文件名中有大量阶段编号（P3 的细节见 F5、F6）。直接公开时，别人看到的不是一个产品仓库，而且这些内容也留在 git 历史中。
- **P4 只在一个平台上验证过。** 开发和全部人工验收都在 Windows + Windows Terminal 上进行，测试只在本机运行，没有 CI。Linux、macOS 能否运行未知。
- **P5 模型不能上网。** 没有读取网页或搜索的工具，查文档只能靠模型自己的知识，或由用户自己接 MCP。

## 已确认的事实

| # | 来源 | 内容 |
| --- | --- | --- |
| F1 | `uv build`（2026-11-02 试验） | wheel 和 sdist 都能构建。wheel 只包含 `mosscode/` 包（90 个文件，含内置 Skill `commit`、`review`、`test` 和内置 Agent `explore`、`general`、`plan`）；sdist 另含 `tests/`、README、`pyproject.toml`。两者都不含 `docs/`、`scripts/`、`skills/`。元数据只有名称、版本、简介、依赖，没有许可证、README、项目链接和分类 |
| F2 | 运行时 | 用户在自己的项目中运行 `moss` 时，只读取该项目的 `AGENTS.md` / `CLAUDE.md`、`.mosscode/` 和用户自己的 `~/.mosscode/`，以及包内的内置 Skill 和 Agent，从不读取 MossCode 仓库中的任何文件 |
| F3 | PyPI（2026-11-02） | `mosscode` 在 PyPI 和 TestPyPI 上都未被占用（404）；`moss` 已被占用。命令名不受影响，包名用 `mosscode` |
| F4 | 仓库 | 没有 git 远程；本机 `gh` 已登录 `yzd11`；共 22 个提交；`config.yaml`、`mcp.yaml`、`.mosscode/mcp.yaml`、`permissions.local.yaml` 从未进入历史。`_local/`、`rtk/`、`reference/`、`.claude/` 不在仓库中 |
| F5 | `git ls-files` | 跟踪的文件中，属于产品的：`src/`（85）、`tests/`（44）、`docs/reference/`（16）、README、`pyproject.toml`、`requirements-lock.txt`、三个 `*.example.yaml`、`.gitignore`。属于开发过程的：`skills/code-spec/`（3）、`docs/specs/`（43）、`docs/acceptance.md`、`docs/research/`（4）、`docs/learning/`（2）、`docs/interview/`（2）、`docs/ui/`（64，界面方案的预览图）、`docs/MossCode 初步想法-修订版.md`、`code-spec-review.md`、`scripts/`（12，本机验证和验收准备脚本）。`docs/research/mewcode-pi-comparison.md`、`code-spec-review.md` 中有本机路径 |
| F6 | 代码与测试 | `src/`、`tests/` 中约 194 处注释或文档字符串引用阶段编号（如 `Ch15 BE5`、`Refinement 08 CL3`、`chapter 10 K3`），指向 `docs/specs/` 中的需求编号；15 个测试文件按阶段命名（`test_teams_stage15.py` 等）。README 的“开发”一节链接 `docs/specs/`、`docs/acceptance.md` 和 `scripts/` |
| F7 | `tests/test_docs.py` | 只检查 README 和 `docs/reference/*.md` 的结构与相对链接 |
| F8 | 依赖的元数据 | anthropic、textual、PyYAML、pydantic、mcp、jsonschema 为 MIT；openai 为 Apache-2.0；httpx 为 BSD-3-Clause。都允许以 MIT 或 Apache-2.0 发布本项目 |
| F9 | 测试与代码 | 测试中有 17 处平台条件或跳过（Windows junction、PowerShell、taskkill、Windows 内联输入等）；`src/mosscode` 中 13 个模块有 `os.name` / `sys.platform` 分支。本机有 WSL Ubuntu 和 Docker，可以在本地先跑 Linux |
| F10 | `tools/__init__.py` | 默认工具：`read_file`、`write_file`、`edit_file`、`execute_command`、`glob`、`grep`、`exit_plan_mode`，另有 memory、Skill、子 Agent、团队工具。没有网络工具；`httpx` 已是依赖 |
| F11 | `tool_budget.py` | 单个工具结果超过约 12,500 Token 时存盘，只给模型开头和结尾 |
| F12 | `permissions.py`、`docs/reference/permissions.md` | 规则写法 `工具(参数)`，别名 `Read`、`Write`、`Edit`、`Bash`、`Glob`、`Grep`；“永久允许”保存到 `.mosscode/permissions.local.yaml`；deny 在任何档位都生效 |
| F13 | 本机 | 仓库 `.venv` 是 Python 3.12.1；`uv tool` 安装的环境是 Python 3.14.5 |

## 目标

- 别人用一条命令（`uv tool install mosscode`）就能装上，许可证清楚。
- 公开的仓库像一个普通的开源产品：使用文档、源码、测试和贡献说明；开发过程的资料只留在本机。
- Windows、Linux、macOS 上的测试在每次推送后自动运行。
- 模型可以读取网页、搜索资料，网络访问和文件访问一样受权限控制。

## 需求

### 公开内容（PUB）

- **PUB1** 区分三类内容：
  1. **包**（PyPI，`uv tool install mosscode` 装上的内容）：只有 `mosscode` 包和 LICENSE（F1 已满足，PK3 自动检查）；
  2. **公开仓库**：使用者和贡献者需要的内容（PUB2）；
  3. **开发资料**：只留在本机（PUB3）。
- **PUB2** 公开仓库只包含：
  - `src/`、`tests/`、`docs/reference/`、`docs/pypi.md`；
  - `README.md`、`LICENSE`、`CONTRIBUTING.md`、`AGENTS.md`、`CHANGELOG.md`、`SECURITY.md`；
  - `pyproject.toml`、`MANIFEST.in`（sdist 带上完整的测试）、`requirements-lock.txt`、`config.example.yaml`、`mcp.example.yaml`、`permissions.example.yaml`；
  - `.gitignore`（由导出脚本生成，不含开发仓库中关于私有资料的条目）、`.github/`。
  - 导出脚本：`scripts/export_public.py`（留在开发仓库），默认导出 HEAD，`--worktree` 预览未提交的改动。
- **PUB3** 开发资料不进入公开仓库，也不出现在它的 git 历史中：
  - `skills/code-spec/`、`docs/specs/`、`docs/acceptance.md`、`docs/research/`、`docs/learning/`、`docs/interview/`、`docs/ui/`、`docs/MossCode 初步想法-修订版.md`、`code-spec-review.md`、`scripts/`；
  - 以及已经不在仓库中的 `_local/`、`rtk/`、`reference/`、`.claude/`。

  它们留在本机的开发仓库中（Q3 已定：两个仓库）。`reference/` 中购买的参考资料从未进入任何仓库，也不进入公开仓库。
- **PUB4** 公开的代码不引用公开仓库中没有的文档：
  - 注释和文档字符串中的阶段编号前缀（F6）去掉，只保留说明本身；
  - 必要的约束直接写清楚，不写“见某规格”；
  - 按阶段命名的测试文件改为按功能命名（如 `test_teams_stage15.py` → `test_teams.py`；同一功能有多个文件时合并或加后缀）。
- **PUB5** 新增 `AGENTS.md`，给在这个仓库中工作的 coding agent（包括 MossCode 自己）：项目结构、开发环境、只运行相关测试、换行符约定、提交前检查。它只在别人开发 MossCode 时生效，不影响用 MossCode 的人（F2）。内容来自现在的开发约定，不含个人信息和本机路径。
- **PUB6** 公开前检查：
  - 公开仓库的全部文件中没有本机路径、用户名、邮箱以外的个人信息，也没有密钥；
  - 公开仓库从一个新的历史开始（Q3），不带入现有的 22 个提交。

### 许可证（LC）

- **LC1** 仓库根目录加入 `LICENSE`（许可证按 Q1），`pyproject.toml` 用 SPDX 写法声明（`license = "MIT"`、`license-files = ["LICENSE"]`），wheel 和 sdist 都包含它。

### 打包与发布（PK）

- **PK1** 包的元数据补齐：作者、项目链接（主页、源码、问题反馈）、分类（Python 版本、操作系统、许可证、`Environment :: Console`）、关键词、PyPI 页面说明。
- **PK2** PyPI 页面使用单独的简短说明 `docs/pypi.md`：一句话介绍、安装、第一次运行、主要能力，链接都写完整的 GitHub 地址。仓库 README 保持相对链接。
- **PK3** 每次发布前自动检查（CI 中的打包任务）：
  - 构建 wheel 和 sdist，检查元数据（`twine check`）；
  - 在全新的环境中安装 wheel，运行 `moss --version`、`moss --help`、`moss doctor`；
  - 确认 wheel 中只有 `mosscode/` 和 dist-info，有 LICENSE 和内置 Skill、Agent；sdist 中没有 PUB3 的任何文件。
- **PK4** 发布方式按 Q4。推荐：推送 `v*` 标签时由 GitHub Actions 构建，先发布到 TestPyPI，确认后再发布到 PyPI，使用 Trusted Publishing（不保存令牌）。每次发布都由用户推送标签、在 PyPI 上确认，不由 MossCode 或 CI 自行发起。
- **PK5** 发布后 README 的安装一节以 `uv tool install mosscode` 为主，`pipx install mosscode` 为备选；从源码可编辑安装移到 `CONTRIBUTING.md`。升级：`uv tool upgrade mosscode`。README 的“开发与测试”一节改为链接 `CONTRIBUTING.md`，不再链接开发资料（F6）。
- **PK6** `moss doctor` 的安装方式区分“从 PyPI 安装”“从本地路径安装”“可编辑安装”，并给出对应的升级命令。

### 多平台与 CI（CI）

- **CI1** GitHub Actions 在每次推送和 PR 时运行测试：Windows、Ubuntu、macOS，各在 Python 3.12 和 3.14 上运行全部测试。不使用任何模型密钥，不发送真实的模型请求。
- **CI2** Linux、macOS 上的测试全部通过或有明确的跳过原因。依赖 Windows 的测试保持跳过；产品代码中只在 Windows 上成立的假设（路径、Shell、进程、终端）在 POSIX 上给出正确行为，而不是只让测试跳过。
- **CI3** 至少一个任务在没有 ripgrep 的环境中运行，覆盖搜索的内置实现；其他任务安装 ripgrep。
- **CI4** README 写明各平台的支持程度：Windows（主要平台，经过人工验收）、Linux、macOS（CI 测试；团队分屏窗格需要 tmux）。

### 开源项目的基本配套（EQ）

- **EQ1** 引入 ruff 做代码检查和格式化：先启用默认规则，一次性修正现有代码（单独一个提交，不夹带功能改动）；CI 中运行 `ruff check` 和 `ruff format --check`。类型检查暂不纳入 CI，后续从核心模块开始。
- **EQ2** 新增 `CHANGELOG.md`（Keep a Changelog 格式），第一条为 0.1.0；发布时 GitHub Release 的说明引用对应条目。
- **EQ3** 新增 `.github/ISSUE_TEMPLATE/`（问题报告：版本、系统、终端、`moss doctor` 输出、复现步骤；功能建议）和 `SECURITY.md`（通过 GitHub 的私密漏洞报告提交，不在公开 issue 中描述漏洞）。

### 读取网页（WF）

- **WF1** 新工具 `web_fetch`：参数 `url`（必填）、`offset`（字符位置，默认 0）。只接受 `http`、`https`，`http` 自动升级为 `https`。
- **WF2** 拒绝访问本机和内网：不带点的主机名、`localhost`、IP 字面量和 DNS 解析结果中的环回、私有、链路本地、保留地址。错误提示让模型改用 `execute_command` 访问本地服务（受命令权限约束）。
- **WF3** 重定向：同一主机内最多跟随 5 次；跨主机时不跟随，返回原 URL 和目标 URL，由模型决定是否再次调用。
- **WF4** 内容处理：
  - HTML 去掉 script、style、nav、header、footer、aside 等，转为 Markdown，有 `<main>` 或 `<article>` 时优先取它；
  - `text/plain`、`text/markdown`、JSON 等文本原样返回；
  - 其他类型（图片、PDF、压缩包等）只说明类型和大小，不下载正文。
- **WF5** 下载最多 10 MB、总时长最多 60 秒；超出时返回错误类别，不返回部分内容。请求头带 `User-Agent: MossCode/<版本>`，`Accept` 优先 Markdown。
- **WF6** 返回结果（Q6 推荐方案）：每次返回约 10,000 Token 以内的正文（按 `context.py` 的估算），开头写明 URL、标题、内容类型、总字符数和本段范围；有剩余时写明“从 offset=N 继续读取”。同一会话内每个 URL 缓存 15 分钟。
- **WF7** 网页内容是不可信的数据：结果包在 `<web_content url="…">` 标记中，系统提示中 `web_fetch` 的说明要求把其中的指令当作数据，不执行，需要时告诉用户。
- **WF8** 权限：
  - `web_fetch` 不是只读工具（会把 URL 发到外部）。`default`、`acceptEdits` 档位中询问；`bypassPermissions` 中允许；`plan` 中可用，同样询问；
  - 审批选项：本次允许、本会话允许这个域名、永久允许这个域名（保存为 `WebFetch(domain:example.com)`）；
  - 规则中可写 `WebFetch(domain:example.com)`、`WebFetch(domain:*.python.org)`、`WebFetch(domain:*)`，deny 在任何档位都生效；
  - 不内置预先放行的域名；
  - 无界面模式中未被规则放行的调用返回 `approval_required`；
  - Hook 的工具匹配器、子 Agent 定义的工具列表可以使用 `web_fetch` 这个名称。
- **WF9** 内置的 `explore`、`plan` 子 Agent 可以使用 `web_fetch`（审批照常）。

### 搜索（WS）

- **WS1** 新工具 `web_search`：参数 `query`（必填）、`allowed_domains` 或 `blocked_domains`（二选一，可选）、`max_results`（默认 8，最多 20）。返回编号的结果列表：标题、URL、摘要。不读取结果页面，需要时由模型再调用 `web_fetch`。
- **WS2** 搜索服务按 Q5。推荐：在 `config.yaml` 中配置第三方搜索 API，首先支持 Tavily，接口留出添加其他服务的位置：
  ```yaml
  web_search:
    provider: tavily
    api_key_env: TAVILY_API_KEY
  ```
  没有配置时不向模型提供 `web_search`，`/status` 和 `moss doctor` 显示“未配置”；凭据规则与模型 Provider 相同（只显示是否设置）。
- **WS3** 权限：在 `default`、`acceptEdits`、`plan` 中询问（查询内容会发给搜索服务），选项为本次允许、本会话允许、永久允许（保存为 `WebSearch`）；`bypassPermissions` 中允许；deny 照常生效。
- **WS4** 每个会话（包括它的子 Agent 和团队成员）最多 100 次搜索；达到上限后返回说明，要求模型用已有信息回答并告诉用户。
- **WS5** 搜索结果同样按 WF7 视为不可信数据。

### 文档（DOC）

- **DOC1** README：
  - 安装改为从 PyPI 安装（PK5）；
  - 新增“平台支持”（CI4）；
  - 使用一节加入网络工具；
  - 新增“安全说明”：路径检查是应用层限制，不是系统沙箱；网页和搜索结果是不可信内容；项目级 Hook、`.mcp.json` 需要信任。
- **DOC2** `docs/reference/tools.md` 加入 `web_fetch`、`web_search`；`permissions.md` 加入 `WebFetch`、`WebSearch` 规则；`cli.md` 的 doctor 一节加入搜索服务和安装来源；`config.example.yaml` 加入 `web_search`。
- **DOC3** `docs/pypi.md`（PK2）和 `CONTRIBUTING.md`：开发环境、运行相关测试、CI、发布步骤。

## 设计

### 模块

| 模块 | 改动 |
| --- | --- |
| `LICENSE`、`pyproject.toml`、`docs/pypi.md` | LC1、PK1、PK2 |
| `AGENTS.md`、`CONTRIBUTING.md`（新） | PUB5、DOC3 |
| `src/`、`tests/` | PUB4：去掉阶段编号前缀，测试文件按功能改名 |
| 导出脚本（只在本机，Q3） | 按 PUB2 的清单把文件复制到公开仓库的工作目录，检查 PUB6，不包含任何 PUB3 的文件 |
| `.github/workflows/ci.yml`（新） | 测试矩阵（CI1、CI3）、ruff（EQ1）和打包检查（PK3） |
| `CHANGELOG.md`、`SECURITY.md`、`.github/ISSUE_TEMPLATE/`（新），`pyproject.toml` 的 `[tool.ruff]` | EQ1–EQ3 |
| `.github/workflows/release.yml`（新） | 标签触发的构建与发布（PK4），使用 `pypa/gh-action-pypi-publish` 和 GitHub 环境 `testpypi`、`pypi` |
| `tools/web.py`（新） | `WebFetch`、`WebSearch` 两个工具；地址检查、重定向、内容转换、分页、缓存 |
| `web_search.py`（新） | 搜索服务接口 `SearchBackend.search(query, …) -> list[SearchResult]` 和 Tavily 实现；按配置创建 |
| `permissions.py` | `WebFetch(domain:…)`、`WebSearch` 规则的解析与匹配，审批选项中的“这个域名”，永久规则的保存 |
| `config.py` | `web_search` 字段与校验 |
| `doctor.py` | 安装来源（PK6），搜索服务配置 |
| `prompts.py` | 两个网络工具的说明，网页内容不可信的要求 |

### 依赖

| 依赖 | 用途 | 许可证 |
| --- | --- | --- |
| `httpx`（已有） | 读取网页、调用搜索服务 | BSD-3-Clause |
| `beautifulsoup4`、`markdownify` | HTML 清理与转换为 Markdown | MIT |

### 数据流

```
本机开发仓库（完整历史与开发资料）
 └ 导出脚本：PUB2 清单 → 检查（PUB6）→ 公开仓库的工作目录 → 提交、推送（用户执行或授权）
     └ GitHub Actions：测试矩阵、打包检查；推送标签 → TestPyPI → PyPI

网页：模型 → web_fetch(url) → 地址检查 → 权限（域名规则 / 审批）→ httpx 获取（同主机重定向）
        → 按类型处理 → 缓存 → 分页 → <web_content> 结果
```

### 取舍

- **公开仓库从新的历史开始（Q3）。** 现有历史中有全部开发资料，用 `git filter-repo` 改写历史可以删掉它们，但改写容易遗漏，也会改变所有提交。新历史最简单可靠，本机仓库保留完整历史。
- **`web_fetch` 不再调用模型提取内容（Q6）。** Claude Code 用另一次模型调用按提取要求处理页面，节省主对话的上下文，但每次读取多一次请求，结果有损失，还要再配置一个小模型。MossCode 直接返回清理后的正文并分页，结果确定，配合 F11 的大结果存盘已能控制上下文。
- **搜索用第三方 API 而不是模型服务商的搜索（Q5）。** Anthropic 和 OpenAI 的服务端搜索只在它们的官方服务上可用，用户正在用的 DeepSeek 没有；第三方 API 对所有模型都一样。代价是要再申请一个密钥。
- **HTML 转换用 `beautifulsoup4` + `markdownify`，不用 `trafilatura`。** 后者提取正文更准，但依赖 `lxml` 等较重的包；先用简单规则（去掉导航、页眉页脚，优先 `<main>`），效果不够再换。
- **不内置预先放行的文档域名。** 规则更容易解释；常用的域名由用户在第一次审批时选择“永久允许这个域名”。

## 待定

| # | 问题 | 推荐 | 备选及差异 |
| --- | --- | --- | --- |
| Q1 | 许可证 | MIT：最短，使用和再分发几乎没有限制，与全部依赖兼容 | Apache-2.0：多了明确的专利授权和修改声明要求，文本更长；暂不公开（保留所有权利）：则不发布到 PyPI，本完善只做网络工具和 CI |
| Q2 | 代码托管 | 由你在 GitHub 上新建公开仓库（例如 `yzd11/mosscode`），我添加远程地址；推送由你执行或明确授权后由我执行。公开仓库的 Actions 在三个系统上都免费，PyPI 的 Trusted Publishing 也依赖它 | 私有仓库：Actions 每月有免费分钟数限制，macOS 按 10 倍计费，三系统矩阵可能不够用；PyPI 页面的源码链接别人打不开 |
| Q3 | 开发资料保存在哪里，公开仓库如何产生 | **已定（2026-11-02）**：两个仓库：现在的仓库保持为本机私有的开发仓库（完整历史、规格、验收、开发用 Skill 照旧），开发流程不变；公开仓库是单独的目录和新历史，每次发布前用导出脚本按 PUB2 的清单同步，再提交推送 | 一个仓库：开发资料移到被忽略的目录（自己再建一个私有 git 仓库保存），当前仓库用新历史重新开始并公开。开发和公开在同一个目录里，不需要导出；但规格和验收记录要换地方，日常开发要管两个 git 仓库 |
| Q4 | 发布到 PyPI 的方式 | 推送 `v*` 标签后由 GitHub Actions 用 Trusted Publishing 发布：先 TestPyPI，确认后再 PyPI；你需要在两个站点上各注册账号并添加“受信任的发布者” | 你在本机用 `uv publish` 和 API 令牌手动发布：不依赖 GitHub，但令牌要妥善保管，发布步骤不留记录 |
| Q5 | 搜索服务 | 第三方搜索 API，先支持 Tavily（为 AI 应用设计，有免费额度，以官网为准），没有配置时不提供 `web_search` | 模型服务商的服务端搜索：只在官方 Anthropic / OpenAI 上可用，DeepSeek 不行；无密钥抓取 DuckDuckGo 等网页：结构随时变化，可能违反服务条款；不内置，只用 MCP：最省事，但每个用户都要自己找搜索 MCP |
| Q6 | `web_fetch` 返回什么 | 清理后的正文，分页返回（WF6） | 像 Claude Code 一样再调用一次模型按要求提取：节省主对话上下文，但每次多一次请求、结果有损失，要配置提取用的模型 |
| Q7 | 实施顺序 | 分两批，每批单独人工验收：9A 公开内容整理、许可证、打包、CI、多平台；9B 网络工具。两批都通过后发布 0.1.0 到 PyPI | 9A 完成后先发布，网络工具随 0.2.0 发布：别人更早能装上，但第一个公开版本不能上网 |

## 不做

- 图片输入（粘贴截图、`read_file` 读取图片、MCP 结果中的图片）：用户决定先不做。
- 独立可执行文件、WinGet / Homebrew 包、自动更新、`moss update`。
- 网页中 JavaScript 渲染后的内容（无头浏览器）；登录后才能访问的页面；PDF 正文提取。
- 模型服务商的服务端搜索（Q5 的备选）；多个搜索服务同时使用。
- 系统沙箱（路径检查仍是应用层限制，见 DOC1 的安全说明）。
- 英文界面和英文文档。
- [产品差距评审](../../research/product-gap-review.md) 中的其他改进（模型目录、检查点 `/rewind`、`@` 文件、Agent Skills 目录、插件安装等）：按评审中的顺序放到完善十及以后。

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | PUB4：去掉代码和测试中的阶段编号前缀，测试文件按功能改名 | — | 相关测试照常通过；`git grep` 不再找到阶段编号和 `docs/specs` 的引用 |
| T2 | LICENSE、`pyproject.toml` 元数据、`docs/pypi.md`、`AGENTS.md`、`CONTRIBUTING.md`，README 的开发一节（LC、PK1、PK2、PK5、PUB5） | Q1 | 构建后检查 wheel / sdist 的内容和元数据；`twine check`；`tests/test_docs.py` |
| T3 | 在 WSL Ubuntu 中运行全部测试，记录失败；按 CI2 修正产品代码或补充跳过原因 | T1 | WSL 中全部测试通过或跳过有原因 |
| T4 | `ci.yml`、`release.yml`（CI1、CI3、PK3、PK4） | T2、T3 | 推送后三个系统的任务全部通过 |
| T5 | 导出脚本和公开仓库（PUB1–PUB3、PUB6） | T1–T4、Q2、Q3 | 导出结果只含 PUB2 的文件；在导出目录中运行全部测试通过；扫描无本机路径和密钥 |
| T6 | `tools/web.py` 的 `web_fetch`（WF）、权限中的域名规则 | — | 单元测试：地址检查（含解析到私有地址）、重定向、类型处理、分页、缓存、不可信内容标记；权限规则与审批选项；无界面的 `approval_required`。用本地 HTTP 测试服务，不访问外网 |
| T7 | `web_search.py` 和 `web_search` 工具（WS） | T6、Q5 | 单元测试：替身后端的结果格式、域名过滤、未配置时不提供、次数上限、凭据不出现在输出中 |
| T8 | doctor、`/status`、配置示例与文档（PK6、DOC） | T2、T6、T7 | `tests/test_docs.py`；相关测试文件 |
| T9 | 人工验收 M138–M145 | 各批完成后 | 见“验收” |
| T10 | EQ1–EQ3：ruff 配置与一次性修正、`CHANGELOG.md`、issue 模板、`SECURITY.md` | T1 | `ruff check`、`ruff format --check` 通过；格式修正后运行受影响模块的测试（格式修正涉及全部文件时，这一次经你同意后运行全量测试） |

T1、T6 互不依赖。按 Q7 分批时：9A = T1–T5、T10（T4 的 CI 包含 ruff，因此在 T10 之后），9B = T6、T7；T8 随各批进行。

## 验收

自动测试：新增测试文件覆盖 T6、T7；每批只运行相关的测试文件，全量测试由 CI 在三个系统上运行。

人工验收（Windows Terminal + PowerShell，Linux 部分在 WSL 中）：

| # | 操作 | 预期 |
| --- | --- | --- |
| M138 公开内容 | 在 GitHub 上浏览公开仓库的文件和提交历史 | 只有 PUB2 的文件；历史从新的第一个提交开始；看不到开发用的 Skill、规格、验收记录和学习笔记 |
| M139 CI | 打开 Actions 页面 | 三个系统、两个 Python 版本的测试和打包检查全部通过 |
| M140 从 TestPyPI 安装 | 在 WSL 中：`uv tool install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ mosscode`，然后 `moss --version`、`moss setup`、`moss` | 安装成功；首次配置和交互界面在 Linux 终端中正常；`/skills` 只列出内置的 `commit`、`review`、`test` |
| M141 读取网页 | 让模型读取一个文档页面并概括 | 出现审批，显示域名；选择“本会话允许这个域名”后，同一域名的第二次读取不再询问；结果有标题和内容 |
| M142 拒绝本地地址 | 让模型读取 `http://localhost:8000` | 立即拒绝，提示改用命令访问本地服务，没有发出请求 |
| M143 搜索 | 配置 Tavily 后，让模型搜索一个最新的版本号并给出来源 | 先搜索再读取页面，回答带 URL；`moss doctor` 显示搜索服务已配置，不显示密钥 |
| M144 无界面中的网络工具 | `moss -p "读取 <某个页面> 并概括"`，不加规则 | 返回需要审批的说明，没有发出请求；加上 `WebFetch(domain:…)` 规则后成功 |
| M145 正式发布 | 推送标签发布到 PyPI 后，在另一台电脑或 WSL 中 `uv tool install mosscode` | 安装成功，`moss` 正常使用 |
