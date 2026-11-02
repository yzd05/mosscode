# 完善八：作为命令行产品安装和启动

日期：2026-11-02。状态：已实现，相关自动测试通过；人工验收 M132–M137 通过（2026-11-02）。

来源：
- 用户的反馈：其他 coding agent 直接输入一个命令就能用（`claude`、`pi`），MossCode 要用 Python 启动，还不像一个真正的产品；要求参考 Claude Code 的产品形态整改。
- Claude Code 文档（2026-11-02 查阅）：
  - [CLI reference](https://code.claude.com/docs/en/cli-reference)：`claude`、`claude "query"`（带初始提示进入交互）、`claude -p "query"`、`cat file | claude -p "query"`（管道输入）、`claude -c`、`claude -r`、`claude update`、`claude --version`；
  - [Advanced setup](https://code.claude.com/docs/en/setup)：
    - Windows 上用 `irm https://claude.ai/install.ps1 | iex` 安装到 `~\.local\bin\claude.exe`，也可以用 WinGet、npm；不需要管理员权限；
    - 装好后“打开新的终端窗口，运行 `claude --version`”，找不到命令时按文档修 PATH；
    - `claude doctor` 不启动会话，只读地诊断安装和设置文件，并给出修复建议；
    - 首次运行 `claude` 时在界面中引导登录；设置了 `ANTHROPIC_API_KEY` 并确认后跳过登录；
    - 原生安装在后台自动更新，也可以手动 `claude update`；包管理器安装不自动更新；
    - 卸载：删除可执行文件；用户设置在 `~/.claude/`，项目设置在 `.claude/`。

## 问题

- **P1 没有全局命令。** `pyproject.toml` 已有入口点 `mosscode = "mosscode.cli:main"`，但只装进了仓库的 `.venv`（`pip install -e`）。README 的所有示例都是 `.\.venv\Scripts\python.exe -m mosscode`。在其他项目目录中使用，要写出解释器的完整路径。
- **P2 启动慢。** `mosscode --help` 要 4.5 秒，其中导入约 3.4 秒：`cli.py` 在顶层导入全部模块，`providers.py` 在顶层同时导入 `anthropic`（约 1.7 秒）和 `openai`。只是查看帮助或版本也要付出这些成本。
- **P3 没有版本信息。** 没有 `--version`，出问题时无法确认运行的是哪个版本、装在哪里。
- **P4 首次运行没有引导。** 没有配置文件时直接报错退出，提示“复制 `config.example.yaml`”。但这个文件只在仓库里，通过包安装的用户手里没有。
- **P5 配置查找会误读别的项目的文件。** 默认依次查找 `./.mosscode/config.yaml`、`./config.yaml`（旧位置）、`~/.mosscode/config.yaml`。作为全局命令在任意项目中启动时，项目自己的 `config.yaml`（很多项目都有）会被当成 MossCode 的配置，解析失败或得到错误的设置。
- **P6 和常见用法不一致。** 不能用 `mosscode "任务"` 带着初始提示进入交互；`-p` 不读管道输入，不能用 `Get-Content error.log | mosscode -p "解释"`。
- **P7 出问题时难以自查。** 没有 `doctor` 这样的命令检查安装位置、配置、凭据、依赖工具（rg、git、Git Bash、wt）。

## 已确认的事实

| # | 来源 | 内容 |
| --- | --- | --- |
| F1 | `pyproject.toml` | 包名 `mosscode`，版本 `0.1.0`，`requires-python >= 3.12`；入口点已存在；Skill 和 Agent 的内置文件以 package-data 打包，不依赖仓库 |
| F2 | `cli.py` `main()` | `argparse` 定义所有参数；顶层导入 `tui`、`providers`、`conversation` 等全部模块；`--team-dir`、`--team-member` 是 Lead 启动窗格成员用的隐藏参数 |
| F3 | `config.py` | `config_candidates()` 给出上面的三个位置；`find_config()` 找不到时抛出 `ConfigError` |
| F4 | `paths.py` 等 | 用户级数据都在 `~/.mosscode/`（会话、记忆、Agent、Hook、MCP、权限），项目级在 `.mosscode/`；分层已与 Claude Code 的 `~/.claude/`、`.claude/` 一致，本次不需要迁移 |
| F5 | `teams/backends.py`、`hooks`、`tools` | 窗格成员、Hook 的 `PYTHON`、搜索子进程都用 `sys.executable -m mosscode…` 启动。用 `uv tool` 或 `pipx` 安装时，`sys.executable` 是工具环境的解释器，这些路径照常工作 |
| F6 | 本机 | 有 `uv 0.11.18`，没有 `pipx`；`.venv` 是 Python 3.12.1；`%USERPROFILE%\.local\bin` 目前不在 PATH 中 |
| F7 | 仓库 | 没有 git 远程，也没有发布到 PyPI；安装只能从本地路径（或以后的 git 地址）进行 |
| F8 | 计时 | `.venv/Scripts/mosscode.exe --help`：4.5 秒；`python -X importtime -c "import mosscode.cli"`：3.36 秒，其中 `mosscode.providers` 2.13 秒、`anthropic` 1.67 秒 |

## 目标

- 安装一次后，在任意目录的新终端中输入 `mosscode` 就能启动，和 `claude` 的用法一致。
- 第一次运行时有引导，不需要先读文档或复制示例文件。
- `--help`、`--version` 立即返回；交互启动不再加载用不到的 SDK。
- 出问题时用 `mosscode doctor` 自查。
- 开发者仍能在仓库中修改代码后立即生效。

## 需求

### 安装（IN）

- **IN1** 支持用 `uv tool install` 安装为全局命令（推荐），也支持 `pipx install`。从本地路径安装：
  ```powershell
  uv tool install F:\path\to\MossCode
  ```
  开发者用 `--editable` 安装，修改源码后无需重装：
  ```powershell
  uv tool install --editable F:\path\to\MossCode
  ```
  安装后，`mosscode.exe` 位于 `%USERPROFILE%\.local\bin`。这个目录不在 PATH 中时，由用户自己运行 `uv tool update-shell` 或手动添加，MossCode 不修改 PATH。
- **IN2** 升级和卸载使用安装工具自己的命令：`uv tool upgrade mosscode`（可编辑安装不需要升级）、`uv tool uninstall mosscode`。README 写明这些命令，以及卸载后要删除的 `~/.mosscode`。
- **IN3** 版本号只在 `pyproject.toml` 中维护，运行时从包的元数据读取（`importlib.metadata.version("mosscode")`）。
- **IN4** 原来的开发方式（仓库 `.venv` 中 `pip install -e '.[dev]'`、`python -m mosscode`）保持可用，测试照常运行。

### 命令行（CL）

- **CL1** `mosscode --version`（以及 `-v`）输出 `mosscode 0.1.0`，退出码 0。
- **CL2** `mosscode --help` 列出用法、常用选项和子命令，末尾附几个例子（交互、带提示启动、`-p`、管道、`-c`）。
- **CL3** `mosscode "任务"`：进入交互界面，并把“任务”作为第一条消息发送，等同于启动后输入它再回车。可以与 `-c`、`--resume ID` 一起用，在恢复的会话中发送。
- **CL4** `-p` 读取管道输入：标准输入不是终端时，读取全部内容，附在提示之后，中间空一行。只有管道输入、没有 `-p` 的值时，管道内容就是提示（`Get-Content task.md | mosscode -p`）。交互模式（没有 `-p`）的标准输入不是终端时报错：“交互界面需要终端；无界面执行请用 -p”。
- **CL5** 子命令：`mosscode setup`（SU）、`mosscode doctor`（DR）。第一个参数正好是子命令名时按子命令处理。真要发送“setup”或“doctor”这样的单词作为提示时，写成 `mosscode -- doctor`。
- **CL6** 错误输出和退出码保持现状：配置、凭据等错误输出“MossCode: …”到 stderr，退出码 1；Ctrl+C 退出码 130；参数错误退出码 2（argparse）。

### 启动速度（ST）

- **ST1** `--help`、`--version`、参数错误都在导入 Textual 和模型 SDK 之前处理。目标：`mosscode --version` 在本机少于 0.5 秒（含解释器启动）。
- **ST2** 只导入所选 Provider 协议需要的 SDK：`anthropic-messages` 只导入 `anthropic`，两种 OpenAI 协议只导入 `openai`。
- **ST3** 实现前后各测一次 `--help`、`--version`，以及从回车到交互界面出现第一帧的时间，结果写入实现说明。

### 首次配置（SU）

- **SU1** 交互启动时找不到配置文件（且没有 `--config`），并且标准输入是终端，就进入配置引导，不再直接报错。无界面模式（`-p`）和非终端仍然报错，提示改为“运行 `mosscode setup` 创建配置，或用 --config 指定”。
- **SU2** 配置引导（也可以用 `mosscode setup` 随时运行）在终端中逐项询问：
  1. 协议：Anthropic Messages / OpenAI Responses / OpenAI Chat Completions；
  2. 服务地址 `base_url`（可空，回车使用官方地址）；
  3. 模型名；
  4. 上下文窗口（可空，回车按 128K 估算）；
  5. 凭据的提供方式，见 Q3。

  最后显示将要写入的内容（凭据只显示“已设置”或环境变量名），确认后写入 `~/.mosscode/config.yaml`，然后直接进入交互界面。
- **SU3** 配置引导不覆盖已有文件：`~/.mosscode/config.yaml` 已存在时，`mosscode setup` 提示文件位置并退出，不修改它。追加 Provider 仍然手动编辑文件（与现在一致）。
- **SU4** 写入后用 `load_settings()` 校验一次；校验失败时说明原因并保留文件，不进入交互界面。
- **SU5** 在引导中按 Ctrl+C 退出，不写入任何文件。

### 配置查找（CF）

- **CF1** 默认查找顺序按 Q4 的结论修改。推荐：只查找 `./.mosscode/config.yaml` 和 `~/.mosscode/config.yaml`，不再读取工作目录下的 `config.yaml`；`--config` 照常可用。
- **CF2** `--help` 中 `--config` 的说明、找不到配置时的错误信息、README 的配置一节同步修改。

### 诊断（DR）

- **DR1** `mosscode doctor` 不启动会话，不发送模型请求，只读检查下列各项，每项一行，以 ✓、!（警告）、✗（错误）开头，并对 ! 和 ✗ 给出修复建议：
  - 版本、解释器路径、安装方式（uv tool、pipx、可编辑安装或虚拟环境）；
  - PATH 中的 `mosscode` 是否就是当前运行的这一个（`shutil.which`），有多个时列出；
  - 生效的配置文件路径；配置能否解析；每个 Provider 的名称、协议、模型；凭据只显示“已设置”“环境变量 X 未设置”或“未配置”，绝不显示值；
  - ripgrep、git、Git Bash（Windows）、Windows Terminal（`wt.exe`，团队窗口需要）是否可用；
  - `~/.mosscode` 是否可写；
  - 当前目录的项目级配置（`.mosscode/` 下的 config、permissions、mcp、hooks）能否解析。
- **DR2** 有 ✗ 时退出码 1，否则 0。

### 文档（DOC）

- **DOC1** README“安装”一节改为：用 uv 安装（含可编辑安装）、确认 `mosscode --version`、PATH 不对时怎么办、升级与卸载；保留“开发环境”小节（`.venv` 和测试）。
- **DOC2** README 中所有 `.\.venv\Scripts\python.exe -m mosscode` 改为 `mosscode`；“配置”一节先介绍 `mosscode setup`，再介绍手动编辑。
- **DOC3** 新增 `docs/reference/cli.md`：命令行的完整参考（选项、子命令、管道输入、退出码、配置查找顺序）；README 只保留常用用法并链接到它。

## 设计

### 模块

| 模块 | 改动 |
| --- | --- |
| `cli.py` | 拆为两层：`main()` 只导入 `argparse`、`sys`、`os` 和轻量模块，构建解析器、处理 `--version`、子命令分派和参数检查；之后再导入 `_run(args)` 所需的重模块（`tui`、`conversation`、`providers` 等）。`OUTPUT_FORMATS`、`_permission_mode` 等解析器用到的常量移到不带重依赖的位置。新增位置参数 `prompt`（`nargs="?"`），以及管道输入的读取 |
| `providers.py` | `anthropic`、`openai` 改为在创建对应 Provider 时导入；类型注解用字符串或 `TYPE_CHECKING` |
| `config.py` | `config_candidates()` 按 CF1 修改；`find_config()` 的错误信息改为指向 `mosscode setup`；新增 `render_config(answers) -> str`，生成带注释的 YAML 文本，供配置引导使用 |
| `setup.py`（新） | 配置引导：用 `input()` 和 `getpass` 逐项询问（不使用 Textual，在任何终端中都能用，也便于测试），写文件、校验 |
| `doctor.py`（新） | 诊断项，每项是一个返回 `(级别, 文本, 建议)` 的函数，便于单独测试 |
| `tui.py` | `MossCodeApp` 接受 `initial_prompt`：界面挂载、会话恢复完成后，像用户输入一样提交它 |
| `__init__.py` | `__version__` 从元数据读取，读取失败（直接从源码运行）时为 `"0+unknown"` |

### 数据流

```
mosscode [args]
 └ main()：解析参数（只用轻量模块）
    ├ --version / --help / 参数错误 → 输出并退出
    ├ setup  → setup.run() → 写 ~/.mosscode/config.yaml → 进入交互界面
    ├ doctor → doctor.run() → 退出码 0 / 1
    └ 其他 → 导入重模块 → 查找配置
               ├ 找到 → 与现在相同；有 prompt 时交给 MossCodeApp(initial_prompt=…)
               └ 找不到 → 终端：setup.run()；否则报错退出
```

### 取舍

- **不做独立的可执行文件**（PyInstaller、Nuitka）。单文件模式每次启动都要先解压（更慢），体积超过 100 MB，常被杀毒软件误报；多文件模式与 `uv tool` 的效果相同。Claude Code 的原生二进制来自它自己的构建链，Python 项目最接近的等价物是 `uv tool` 或 `pipx` 管理的独立环境加命令垫片。
- **配置引导用纯文本问答，不用 Textual 界面。** 只运行一次，问答更简单可靠，而且在 `mosscode setup` 中不必启动整个界面。
- **不做自动更新**，也暂不做 `mosscode update`。没有发布渠道（F7），可编辑安装也不需要更新；`doctor` 显示安装方式和对应的升级命令。

## 待定

| # | 问题 | 推荐 | 备选及差异 |
| --- | --- | --- | --- |
| Q1 | 命令名 | 已定（2026-11-02）：保留 `mosscode`，另加短命令 `moss`，两者是同一个入口；文档以 `moss` 为主，`--version` 和 `doctor` 中说明两个名字都可用 | — |
| Q2 | 推荐的安装方式 | 已定（2026-11-02，按推荐）：`uv tool install`（本机已有 uv），文档同时给出 `pipx` | 只写 `pipx`：要先装 pipx；只写 `pip install --user`：会和用户的其他包共用环境，依赖容易冲突 |
| Q3 | 配置引导如何保存凭据 | 已定（2026-11-02，按推荐）：默认填写环境变量名（按协议给出 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY` 作为默认值），配置文件中只写 `api_key_env`；也允许选择直接输入密钥（`getpass` 不回显）写入 `api_key`，选择时提示明文存储的风险 | 只允许环境变量：最安全，但第一次使用还要自己设置环境变量；接入系统凭据管理器（Windows 凭据管理器）：要新增依赖（`keyring`），本次不做 |
| Q4 | 是否停止读取工作目录下的 `config.yaml` | 已定（2026-11-02，按推荐）：停止（CF1）。你现在的 `config.yaml` 在仓库根目录：在仓库中启动时，改为用 `--config` 指定，或由你自己把它复制到 `~/.mosscode/config.yaml`（之后在任意目录都能用） | 保留，但只在文件包含 `providers` 时采用：兼容旧习惯，但误读项目文件的风险仍在，查找规则也更难解释 |

## 不做

- 发布到 PyPI 或其他公开渠道（对外发布，等有远程仓库后另行决定）。
- 独立可执行文件、安装脚本（`install.ps1`）、WinGet 包。
- 自动更新、`mosscode update`。
- 账号登录（OAuth）；“是否信任此文件夹”的首次确认；命令行补全。
- `mosscode mcp add` 一类管理子命令（界面中已有 `/mcp` 等命令）。
- 修改 PATH 或其他系统设置。

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | 计时基线（ST3），记录到实现说明 | — | 实测数字 |
| T2 | `cli.py` 两层结构、`--version`、`__version__`；`providers.py` 延迟导入 | T1 | 单元测试：`--version`、`--help` 的输出；导入 `mosscode.cli` 后 `sys.modules` 中没有 `textual`、`anthropic`、`openai`；选择 OpenAI 协议时不导入 `anthropic`。计时 |
| T3 | 位置参数提示（CL3）和管道输入（CL4） | T2 | 单元测试：参数组合与报错；管道内容的拼接；`MossCodeApp` 收到 `initial_prompt` 后提交第一条消息 |
| T4 | 配置查找（CF） | Q4 | 单元测试：工作目录下的 `config.yaml` 不再生效；错误信息 |
| T5 | `setup.py` 和首次运行引导（SU） | T2、T4、Q3 | 单元测试：用替身输入跑完问答，生成的文件能被 `load_settings()` 读取；不覆盖已有文件；Ctrl+C 不写文件；非终端时报错 |
| T6 | `doctor.py`（DR） | T2 | 单元测试：各项在正常和异常时的输出；凭据值不出现在输出中；退出码 |
| T7 | 文档（DOC）：README、`docs/reference/cli.md`、相关参考文档中的启动命令 | T2–T6 | `tests/test_docs.py`；链接检查 |
| T8 | 相关测试文件；人工验收 M132–M137 | T1–T7 | 见“验收” |

T3、T4、T6 互不依赖。

## 验收

自动测试：新文件 `tests/test_cli_product.py` 覆盖 T2–T6；`tests/test_docs.py` 和现有的 CLI 测试继续通过。只运行相关的测试文件。

人工验收在 Windows Terminal 的 PowerShell 中进行。安装步骤由用户执行：

| # | 操作 | 预期 |
| --- | --- | --- |
| M132 安装 | 在仓库外的目录执行 `uv tool install --editable <仓库路径>`；按提示确认 `%USERPROFILE%\.local\bin` 在 PATH 中；打开新的终端，运行 `mosscode --version` | 输出 `mosscode 0.1.0`，不到 0.5 秒 |
| M133 帮助与诊断 | `mosscode --help`；`mosscode doctor` | 帮助立即出现，列出子命令和例子；诊断逐项显示，凭据只显示是否设置 |
| M134 首次运行 | 临时把 `~/.mosscode/config.yaml` 移开（如有），在任意项目目录运行 `mosscode`，按引导填写，凭据选环境变量 | 依次询问五项，确认后写入文件并直接进入交互界面；之后恢复原文件 |
| M135 任意目录启动 | 在另一个项目目录（含有它自己的 `config.yaml`）运行 `mosscode` | 使用 `~/.mosscode/config.yaml`，不读取该项目的 `config.yaml` |
| M136 带提示启动与管道 | `mosscode "列出这个目录的文件"`；`Get-Content README.md \| mosscode -p "用一句话概括"` | 前者进入界面并立即开始执行这条任务；后者输出概括后退出 |
| M137 开发环境不受影响 | 在仓库中修改一处可见的文字（例如帮助中的一句），不重装，再运行 `mosscode --help`；然后恢复修改 | 修改立即生效（可编辑安装）；`.venv` 中的 `python -m mosscode` 照常可用 |

### 人工验收结果

2026-11-02，Windows Terminal + PowerShell 5.1，`uv tool install --editable` 安装，命令用 `moss`：

| # | 结果 |
| --- | --- |
| M132 | 通过。新终端中 `moss --version` 输出 `mosscode 0.1.0`；`Measure-Command` 测得 310 毫秒 |
| M133 | 通过。帮助列出子命令和例子；doctor 显示 uv tool 可编辑安装、`moss` 与 `mosscode` 在 `%USERPROFILE%\.local\bin`，无配置时为 ✗ 并提示 `moss setup`，退出码 1。验收中修正两处（见下） |
| M134 | 通过。五项提问、汇总不显示密钥，确认后写入并直接进入界面，启动区显示 `deepseek`。第一次填写时留空了服务地址，修正提示后重做（见下） |
| M135 | 通过。在含有自己 `config.yaml` 的仓库中运行，使用 `~/.mosscode/config.yaml`；doctor 对仓库的 `config.yaml` 给出 ! 警告 |
| M136 | 通过。`moss "列出这个目录的文件"` 启动后立即执行；`Get-Content README.md \| moss -p "用一句话概括"` 输出一句概括后退出 |
| M137 | 通过。临时修改帮助描述后，不重装运行 `%USERPROFILE%\.local\bin\moss.exe --help` 立即显示修改；还原后恢复。`.venv` 中 `python -m mosscode --version`、`--help` 和 `mosscode.exe` 照常可用 |

验收中的修正：

- **在家目录中运行时**，项目级与用户级配置是同一个文件：`config_candidates()` 去重，错误信息不再把同一路径列两次；doctor 不把 `~/.mosscode` 当作项目的 `.mosscode/` 检查。
- **配置引导的服务地址**：原提问“回车使用官方地址”没有说明是哪个地址，选“OpenAI Chat Completions（DeepSeek、Qwen 等兼容服务）”后回车，实际指向 OpenAI。现在提问写明官方地址（`OFFICIAL`：Anthropic 为 `https://api.anthropic.com`，两种 OpenAI 协议为 `https://api.openai.com/v1`），选 Chat Completions 时先提示兼容服务需填写自己的地址；汇总和 doctor 也显示官方地址的主机名。

## 实现说明

### 模块

| 模块 | 实现 |
| --- | --- |
| `launcher.py`（新） | 轻量入口，`moss`、`mosscode` 和 `python -m mosscode` 都从 `launcher.main` 进入：子命令分派、解析器、参数组合检查（CL3–CL5）、管道输入（`with_stdin`）、非终端的检查；之后才 `from .cli import run`。只导入 `argparse`、`pathlib`、`sys`；`--version` 用 `importlib.metadata` |
| `cli.py` | `main(argv)` 只是 `run(parse_arguments(argv))`，供测试调用（测试替换 `cli` 模块上的名字，所以重模块仍在 `cli.py` 顶层导入）；`run(args)` 是原来的主体；`_config_path` 在交互终端中找不到配置时进入引导（SU1） |
| `providers.py` | 顶层不再导入 `anthropic`、`openai`；在 `SDKProvider._client` 中按协议导入。错误分类用 `_errors(name)`，只取已经加载的 SDK 的异常类 |
| `config.py` | `config_candidates()` 去掉工作目录下的 `config.yaml`；错误信息指向 `moss setup`；`render_config(provider)` 用 `yaml.safe_dump` 生成，前面两行注释指向 `config.example.yaml` |
| `setup.py`（新） | `Wizard`（`input`、`getpass`，可注入替身）、`setup()` 返回 ready / written / exists / cancelled / failed，`run_setup()` 供首次运行，`main()` 供 `moss setup`。写文件用独占创建（`open(..., "x")`），不会覆盖同时出现的文件 |
| `doctor.py`（新） | 每类检查一个函数，返回 `Item(级别, 文本, 建议)`；`report()` 排版；`main()` 按 DR2 返回退出码 |
| `tui.py` | `MossCodeApp(initial_prompt=...)`：没有启动任务时挂载后发送；有项目上下文、MCP 或会话恢复时在 `_initialize` 结束后发送（Hook 确认之后）；发送走 `on_prompt_area_submitted`，与输入相同 |
| `__init__.py` | `__version__` 用模块 `__getattr__` 在访问时读取元数据，导入包本身不读 |
| `pyproject.toml` | `moss`、`mosscode` 两个入口都指向 `mosscode.launcher:main` |

### 与规格的差异

- **轻量入口放在新模块 `launcher.py`，而不是拆分 `cli.py`。** 现有测试大量替换 `cli` 模块上的名字（`MossCodeApp`、`create_provider`、`load_settings` 等），这些名字留在 `cli.py` 顶层，测试不用改。T2 的“导入 `mosscode.cli` 后没有 textual”相应改为“导入并运行 `launcher --version` 后没有 textual、anthropic、openai、`mosscode.cli`”。
- **SU2“确认后直接进入界面”加了一个条件：凭据可用。** 选择环境变量而这个变量当前没有设置时，写入文件后显示 PowerShell 的设置方法，提示在新终端中运行 `moss`，不进入界面——否则界面一启动就会报凭据错误。
- **Provider 名称和默认变量名不询问。** 有服务地址时取域名的主体（`api.deepseek.com` → `deepseek`、`DEEPSEEK_API_KEY`），否则按协议取 `claude`、`openai`、`compat`。问题保持 SU2 的五项。
- **上下文窗口接受 `200k`、`1m` 这样的写法。**
- **`moss setup` 的退出码**：0 写入完成或配置已存在，1 写入或校验失败，2 不在终端中，130 中途退出（规格未规定）。
- **`moss doctor` 增加 `--config`**，与主命令一致；工作目录下有不再读取的 `config.yaml` 时给出警告（针对 CF1 的迁移）。PATH 中的命令是否“就是当前运行的这一个”，按它是否在当前解释器的 Scripts 目录判断；uv tool 和 pipx 安装的命令在它们自己的 bin 目录，视为同一个。
- **服务地址在 doctor 中只显示主机名**，不显示路径和查询参数（其中可能带令牌）。

### 计时（ST3）

`.venv` 中，每项取 5 次中最快的一次：

| 项目 | 实现前 | 实现后 |
| --- | --- | --- |
| `python -c pass`（解释器本身） | 0.06 秒 | 0.06 秒 |
| `python -m mosscode --version` | 3.50 秒（当时没有这个选项，参数错误退出） | 0.23 秒 |
| `python -m mosscode --help` | 3.50 秒 | 0.23 秒 |
| `moss.exe --version`、`moss.exe --help` | — | 0.25 秒 |
| `import mosscode.launcher` | — | 0.11 秒 |
| `import mosscode.cli`（会话启动前的导入） | 3.54 秒 | 1.59 秒 |
| `import anthropic` / `import openai`（参考） | 1.97 / 0.82 秒 | 2.15 / 0.94 秒 |

`--version` 达到 ST1 的 0.5 秒目标。会话启动时只再加载所选协议的 SDK：OpenAI 协议约省去 `anthropic` 的 2 秒。“从回车到第一帧”没有自动测量，在 M132、M136 中人工观察。

### 测试

新文件 `tests/test_cli_product.py`（32 个）：版本与帮助；`launcher` 不加载界面和 SDK、每种协议只加载自己的 SDK（子进程检查 `sys.modules`）；参数组合与错误；管道拼接；`-p` 无任务、非终端；子命令分派；`initial_prompt` 的传递与发送；配置引导的环境变量、域名命名、明文密钥不回显、不覆盖、Ctrl+C 与拒绝确认不写文件、非终端；`render_config`；首次运行只在终端中进入引导；doctor 不显示凭据值和查询参数、缺配置为 ✗、旧位置为 !、PATH 多份、工具缺失、`~/.mosscode` 可写、退出码；在家目录中运行时配置路径只列一次、不当作项目检查；引导和 doctor 写明官方地址。

修改的测试：`tests/test_model_side.py` 的查找顺序用例改为“工作目录下的 `config.yaml` 不生效，错误信息提到 `moss setup`”。

运行的相关测试文件（未运行全量）：`test_cli_product`、`test_cli`、`test_model_side`、`test_config`、`test_providers`、`test_tool_providers`、`test_interaction_stage4`、`test_tui`、`test_docs`，以及其他调用 `cli.main` 的 10 个文件（`test_commands_stage10`、`test_context_stage8`、`test_hooks_stage12`、`test_permission_modes`、`test_plan_commands`、`test_sessions_stage9b`、`test_skills_stage11`、`test_teams_stage15`、`test_tool_tui`、`test_worktree_stage14`，502 个），全部通过。

### 文档

README 的安装（uv、pipx、PATH、重装与卸载）、配置（先 `moss setup`，再手动编辑；新的查找顺序）、使用与无界面模式（`moss`，管道输入）、架构表和开发与测试（`.venv` 流程移到这里）；新增 [命令行参考](../../reference/cli.md)；`docs/reference/providers.md` 的例子；`config.example.yaml` 开头的查找顺序。

### 附加：启动区吉祥物

按用户要求，在完善八的同一轮中替换了启动区图标：原来的机器人改为小幽灵（`appearance.py` 的 `MASCOT_PIXELS`、`MASCOT_COLORS`，`pixel_art()` 把每两行像素画成一行半块字符），右侧三行信息垂直居中。▀ 单元格的下半像素用背景色表示，所以 `tests/test_tui.py` 的终端背景检查对吉祥物的颜色单独放行。用户在 Windows Terminal 中确认显示正常（2026-11-02）。
