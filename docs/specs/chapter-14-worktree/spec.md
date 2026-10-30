# 第十四章：Worktree 隔离

日期：2026-10-30。状态：已实现，人工验收待进行。

来源：
- 课程第 14 章：理论学习、Python 源码解析、实战演练中的参考规格。
- 用户的初步设计：
  - 用 git worktree 隔离：目录放在仓库内不被追踪的位置，每个 worktree 一个分支；
  - 名称严格校验：限字符集和长度，拒绝 `.` 和 `..` 段，允许 `/` 嵌套；
  - 完整生命周期：创建（目录已存在时只读文件系统、不调 git 的快速恢复）、进入、退出、删除；
  - 创建后初始化：复制本地配置、配置 git hooks、软链大型依赖目录、按规则补上被忽略但需要的文件；
  - 不 chdir，工作目录显式传给每个工具调用；与路径相关的缓存都以绝对路径为 key；
  - 定义 frontmatter 中的 `isolation: worktree`：进入时自动创建、注入路径说明，结束时按改动决定保留或删除；
  - 退出时的改动保护，以及三层过滤的后台过期清理；
  - 不做 worktree 之间的合并策略、跨目录同步、多 Agent 并行编排。
- 对用户初步设计的评审建议，用户确认“按推荐写规格”。
- 当天的调查：
  - 对照第十三章的子 Agent 运行时（`agents/runtime.py`）、工具与权限的根目录（`tools/base.py`、`permissions.py`）、Hook 的工具事件（`hooks/engine.py`）、搜索工具（`tools/search.py`）；
  - 在 Windows 11、git 2.45.2、Python 3.12 上做了实验（见“实验”）。

本规格按 MossCode 的现状和实验结果调整课程方案和用户的初步设计，调整逐条列在“对课程方案的调整”中。

## 目标

子 Agent 可以在自己的 git worktree 中修改文件：
- 它的读写、搜索、命令都发生在 worktree 中，不改动主工作目录，也不受主 Agent 同时进行的修改影响；
- 结束时没有改动就自动删除 worktree 和分支；有改动就保留，并把位置、分支和合并方法告诉主 Agent。

用户也可以手动创建、列出和删除 MossCode 管理的 worktree。

## 实验

在 Windows 11、git 2.45.2、Python 3.12.1 上：

| # | 操作 | 结果 |
| --- | --- | --- |
| E1 | 在 worktree 中建一个 junction 指向主项目的 `node_modules`（`node_modules/` 已被忽略），然后执行不带 `--force` 的 `git worktree remove` | 命令成功，**主项目 `node_modules` 中的文件被删除**；重复一次结果相同 |
| E2 | 对含同样 junction 的目录执行 `shutil.rmtree`，再执行 `git worktree prune`、`git branch -d` | 只删除了链接本身，主项目文件完好；worktree 记录与分支都被清除 |
| E3 | 普通用户执行 `os.symlink(..., target_is_directory=True)` | `WinError 1314`（没有所需的特权）；`_winapi.CreateJunction` 成功 |
| E4 | 在仓库内 `.mosscode/worktrees/<name>` 创建 worktree 后，在主目录执行 `git status` | 出现 `?? .mosscode/`；不排除时，`git add -A` 会把它当作嵌入的仓库加入 |
| E5 | 删除含 git 对象的目录 | git 对象文件是只读的，`shutil.rmtree` 报 `WinError 5`，需要先去掉只读属性 |

E1 决定了删除流程（W14）：MossCode 不调用 `git worktree remove`。

## 现状

| 位置 | 现在的做法 | 与本章的关系 |
| --- | --- | --- |
| 工具（`tools/base.py`、`tools/__init__.py`） | 每个工具在构造时持有 `root`，相对路径按它解析；`execute_command` 的 `cwd` 参数默认为 `root`；`create_default_registry(root)` 创建 6 个工作区工具和 `exit_plan_mode`，共用一个 `FileState` | “显式工作目录”在 MossCode 中已经是工具的 `root`，不需要另传 cwd（D1） |
| `ToolRegistry.for_agent` | 复制持有 `FileState` 的工具实例并换上新的状态，**根目录不变**；`extra` 工具的根目录必须等于权限策略的根目录 | 隔离的子 Agent 需要以 worktree 为根重新创建工具（I1） |
| `PermissionPolicy` / `PolicyView` | 策略有 `root`；路径规则、项目内外判断、受保护文件都相对 `root`；`PolicyView` 共用父策略的 `root` | 视图需要能换根目录（I2） |
| 系统提示词（`prompts.environment`） | 工作目录取第一个工具的 `root`；`git` 快照由调用方给出 | 隔离的子 Agent 的环境信息与 git 快照取 worktree（I3） |
| 子 Agent（`agents/runtime.py`） | `definition_loop` 用主对话的 `project_instructions` 与 `main.git`；`describe_call` 相对 `Path.cwd()` 显示路径 | 项目说明沿用主项目的，git 快照与路径显示改为 worktree（I3、U2） |
| Agent 定义（`agents/__init__.py`） | `isolation` 在“未支持”的 Claude Code 字段中，被忽略并提示 | 改为支持的字段（A1） |
| Hook 的工具事件（`hooks/engine.py`） | `path` 相对工具的根目录，`MOSSCODE_FILE_PATH` 为 `root / path`；Hook 命令在主项目目录运行 | 根目录换成 worktree 后，格式化等 Hook 自然作用于 worktree 中的文件；增加 `worktree` 字段（H1） |
| 搜索（`tools/search.py`、`search_worker.py`） | 跳过 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`；遵守 `.gitignore`；不进入链接目录 | 主 Agent 的搜索需要跳过 `.mosscode/worktrees`，否则结果重复（S1） |
| 项目说明（`prompts.load_instructions`） | 从文件系统根到项目目录，每层取一个说明文件 | 在 worktree 中启动 MossCode 时会同时读到主项目和 worktree 中的同一份说明（S3） |
| 配置（`config.py`） | 平铺的键，例如 `agent_max_background` | 新增 `worktree_links`、`worktree_stale_hours`（C1） |

## 对课程方案的调整

| # | 课程方案或初步设计 | 调整 | 原因 |
| --- | --- | --- | --- |
| D1 | 用 ctx 把 cwd 显式传给每个工具调用，工具从 ctx 取路径 | 隔离的子 Agent 有一套以 worktree 为根的工具实例和权限视图；不新增 cwd 传递通道，进程工作目录始终不变（I1、I2） | MossCode 的工具本来就按 `root` 解析路径；工具实例本来就按子 Agent 复制。换根目录比给每个调用加通道改动小，权限的项目内外判断也随之正确 |
| D2 | 所有路径相关缓存（文件内容、系统提示词、项目指令、记忆）以绝对路径为 key，按目录自然隔离 | 只有文件状态和环境信息随 worktree 变化：<br>• `FileState` 每个子 Agent 一份，键本来就是绝对路径；<br>• 项目说明、记忆、会话目录、Hook、Skill、Agent 定义仍属于主项目（I3、I4） | worktree 是同一个项目的另一份副本。记忆按 worktree 路径分开会让每个临时 worktree 产生一个空的记忆目录；会话记录也应留在主会话中 |
| D3 | 主会话可以 `/worktree enter` 进入、`exit` 退出，会话持久化到 `worktree_session.json` 并随 `--resume` 恢复 | 本章的“进入、退出”只发生在隔离的子 Agent 内部；主会话不切换。用户可以手动创建 worktree，在另一个终端中进入该目录启动 MossCode（M2、S3） | 主会话切换需要重建工具、权限根目录、环境信息（缓存失效）、状态栏和恢复逻辑，又与下一章“团队成员各自一个 worktree”的模型不同；先把子 Agent 的隔离做稳 |
| D4 | 删除时 `git worktree remove --force`，再 `git branch -D` | 先做改动检查，再用不跟随链接的 `rmtree` 删除目录，然后 `git worktree prune`，最后删除分支；从不调用 `git worktree remove`（W14） | 实验 E1：`git worktree remove` 会穿过 junction 删除主项目的依赖 |
| D5 | 默认软链 `node_modules`、`.venv`、`vendor` | 默认不链接；由 `worktree_links` 配置开启。先尝试符号链接，Windows 上没有特权时对目录改用 junction，文件跳过并提示（W9） | Windows 普通用户不能创建符号链接（E3）；链接 `.venv` 时，以可编辑方式安装的包仍指向主项目源码，worktree 中的测试会测到主项目的代码；在 worktree 中安装依赖会改动主项目 |
| D6 | 复制 `.mewcode/config.yaml`、`settings.local.yaml` 和 `.env` | 只复制 `.mosscode/permissions.local.yaml`、`.mosscode/hooks.local.yaml`；其他被忽略的文件（包括 `.env`、`config.yaml`）由项目根的 `.worktreeinclude` 列出才复制（W8、W10） | `config.yaml` 可能含明文 API Key，复制带密钥的文件应由用户明确选择 |
| D7 | 创建后用 `git config core.hooksPath` 为 worktree 配置 git hooks | 不做任何设置（W11） | worktree 共用主仓库的 `.git/hooks` 和配置；相对的 `core.hooksPath`（如 `.husky`）按 worktree 根目录解析，而 `.husky` 是被追踪的文件，本来就在 worktree 中。执行 `git config` 写的是共用的配置，会改动主仓库 |
| D8 | “未推送的 commit” 用 `--not --remotes` 判断 | 改为“只在这个分支上的提交”：`git rev-list --count HEAD --not --exclude=refs/heads/<分支> --branches --remotes`（W12） | 本地基准提交本身尚未推送时，`--not --remotes` 会把所有 worktree 都判为有改动，永远不会被清理；分支已合并到其他分支后，其提交不再算作改动 |
| D9 | 在项目 `.gitignore` 中加入 worktree 目录，或只警告 | 首次创建时把 `/.mosscode/worktrees/` 写入 `$GIT_COMMON_DIR/info/exclude`（W6） | 不改动被追踪的文件；`info/exclude` 是本地文件，对所有 worktree 生效，可以避免 E4 中的嵌入仓库 |
| D10 | 用 `agent-a<7 位十六进制>` 等五种名称模式识别临时 worktree | 用元数据识别：只有 MossCode 写了元数据且 `manual: false` 的 worktree 才可能被清理；名称模式只作为第二道检查（W16） | 名称可能被用户手动使用；元数据还记录了基准提交、链接和使用中的进程 |
| D11 | Fork 式子 Agent 也可以隔离 | Fork 式不支持隔离；Skill fork 不支持隔离（A4） | Fork 继承父对话的历史，其中的路径指向主目录；同时它要与父对话的请求逐字节一致才能命中缓存，加入 worktree 说明会破坏这一点 |
| D12 | MCP 工具在隔离的子 Agent 中照常可用 | 隔离的子 Agent 只提供以 worktree 为根的内置工作区工具；MCP 工具、Skill 的脚本工具不提供（I1） | MCP 服务器在主项目目录启动，文件类服务器会直接改动主目录，破坏隔离 |
| D13 | 只能在定义的 frontmatter 中声明隔离 | frontmatter 的 `isolation: worktree`，以及 `agent` 工具的可选参数 `isolation`（A2） | 内置 `general` 没有隔离；模型需要“用 general 在 worktree 中改”时不必让用户先写定义 |
| D14 | 计划模式下也创建 worktree | 子 Agent 的有效模式为 `plan` 时不创建 worktree，在主目录只读运行，结果中说明（A5） | 计划模式不写文件，隔离没有意义；创建 worktree 本身会写入 `.git` 和新目录 |
| D15 | 名称校验只限字符集、长度和 `.`、`..` | 另外拒绝以 `.` 或 `-` 开头的段、以 `.lock` 结尾或以 `.` 结尾的段，以及 Windows 保留名（`CON`、`NUL`、`COM1` 等）；名称不区分大小写地判重（W2） | 以 `-` 开头会被 git 当作选项；`.lock` 结尾的分支名非法；Windows 文件名不区分大小写 |

## 需求

### 名称与位置

- **W1** 位置：
  - worktree 目录为 `<仓库根>/.mosscode/worktrees/<扁平名>`，仓库根即 `git rev-parse --show-toplevel`；
  - 扁平名把名称中的 `/` 换成 `+`；
  - 分支为 `mosscode/<扁平名>`；
  - 元数据为 `<仓库根>/.mosscode/worktrees/.meta/<扁平名>.json`。
- **W2** 名称校验，失败时返回带原因的错误：
  - 非空，总长不超过 64 个字符；
  - 按 `/` 分段，不允许空段（即不允许 `//`、开头或结尾的 `/`）；
  - 每段只含 `A-Z a-z 0-9 . _ -`；
  - 每段不能是 `.` 或 `..`，不能以 `.` 或 `-` 开头，不能以 `.` 或 `.lock` 结尾；
  - 每段不能是 Windows 保留名（不区分大小写，带扩展名也算，例如 `nul.txt`）；
  - 与已有 worktree 的名称不区分大小写地相同 → 报错。
- **W3** 子 Agent 使用的名称由 MossCode 生成：`agent-<8 位十六进制>`。模型不能指定名称。
- **W4** MossCode 在仓库的子目录中启动时，子 Agent 的根目录是 worktree 中对应的子目录。例如在 `<仓库>/pkg` 启动，根目录为 `<worktree>/pkg`。

### 创建

- **W5** 前提，任何一条不满足 → 错误 `worktree_unavailable`，不回退到在主目录中运行：
  - 找得到 `git`；
  - 项目目录在一个 git 工作树中，且不是裸仓库；
  - 仓库至少有一个提交。
- **W6** 首次创建时，若 `$GIT_COMMON_DIR/info/exclude` 中没有 `/.mosscode/worktrees/`，追加这一行。
- **W7** 创建：
  - 基准为主工作目录当前的 `HEAD` 提交（分离 HEAD 也可以），记录其完整 SHA；
  - 执行 `git worktree add -b mosscode/<扁平名> -- <路径> <SHA>`；
    - 环境变量 `GIT_TERMINAL_PROMPT=0`，标准输入关闭；
    - 在线程中运行，不阻塞界面；
  - 分支已存在 → 报错，不使用 `-B` 覆盖；
  - 创建失败 → 删除可能残留的目录、执行 `git worktree prune`、删除已创建的分支，返回 git 的错误信息；
  - 同一进程中，创建与删除串行执行（一把 `asyncio.Lock`）；运行中的子 Agent 之间不互相阻塞。
- **W8** 创建后初始化，按以下顺序执行：
  1. 复制本地配置（W8 的复制范围见 D6）；
  2. 创建链接（W9）；
  3. 复制 `.worktreeinclude` 列出的文件（W10）。

  规则：
  - 每一步都尽力而为，失败只记录警告，不中断创建；
  - 目标已存在的文件不覆盖；
  - 警告出现在子 Agent 的结果中，或 `/worktree create` 的输出中；
  - 全部完成后在元数据中记 `setup_done: true`。
- **W9** 链接：
  - 配置项 `worktree_links` 是相对仓库根的路径列表，默认为空；
  - 对每一项：主仓库中存在、worktree 中不存在时才创建：
    1. 先尝试符号链接；
    2. Windows 上因特权失败（`WinError 1314`）且是目录时，改用 junction；
    3. 否则跳过，并提示原因；
  - 创建的链接记入元数据的 `links`。
- **W10** `.worktreeinclude`：
  - 位置为主仓库根，每行一个 gitignore 风格的模式，`#` 开头为注释；
  - 候选为 `git ls-files --others --ignored --exclude-standard --directory` 列出的被忽略路径，与模式匹配的文件或目录复制到 worktree 的同一位置；
  - 已由 W9 链接的路径不复制；
  - 文件不存在时没有任何操作。
- **W11** git hooks：不做任何设置（D7）。worktree 中的 `git commit` 照常触发仓库的 git hooks。
- **W12** 主目录有未提交的改动时，照常创建，但在路径说明（A3）和结果（A6）中注明：“主目录有未提交的改动，它们不在 worktree 中”。

### 快速恢复

- **W13** `/worktree create <名称>` 发现目录已存在：
  - 只读文件系统，不启动 git 进程；
  - 依次验证 worktree 的 `.git` 文件 → `gitdir:` 指向的目录存在 → 该目录中的 `HEAD` 可解析为提交（读 loose ref，或读 `packed-refs`）→ 元数据存在且分支一致；
  - 全部通过时返回已有 worktree 的信息，标明“已存在”；
  - 元数据中 `setup_done` 不为真时，重新执行 W8 中尚未完成的步骤（已存在的文件、链接跳过）；
  - 任何一步失败 → 报错“目录存在但不是有效的 MossCode worktree”，并提示用 `/worktree remove <名称> --discard` 清理或手动处理；不自动删除。

### 改动检查与删除

- **W14** 删除流程：
  1. 改动检查（W15）。有改动且没有显式丢弃 → 拒绝，列出改动；
  2. 解除元数据中记录的链接，只删除链接本身；
  3. 用不跟随链接的 `rmtree` 删除目录，遇到只读文件时去掉只读属性后重试（E5）；
  4. `git worktree prune`；
  5. `git branch -D mosscode/<扁平名>`：只在第 1 步确认没有独有提交，或用户显式丢弃时执行；
  6. 删除元数据。

  约束：
  - 任何时候都不调用 `git worktree remove`（E1）；
  - 第 3 步失败时（例如 Windows 上文件被进程占用）：
    - 保留元数据，并标记 `removing: true`；
    - 报告失败原因；
    - 后台清理以后重试。
- **W15** 改动检查（任何 git 命令失败都按“有改动”处理）：
  - 未提交：`git status --porcelain --untracked-files=all` 非空，链接路径除外；
  - 独有提交：`git rev-list --count HEAD --not --exclude=refs/heads/mosscode/<扁平名> --branches --remotes` 大于 0；
  - 结果包含两类改动的数量和最多 10 个改动的文件名。

### 后台过期清理

- **W16** 三层过滤，全部通过才删除。只考虑 `.mosscode/worktrees` 下的目录：
  1. **MossCode 创建的临时 worktree**：
     - 元数据存在；
     - 元数据中 `manual: false`；
     - 名称符合 `agent-[0-9a-f]{8}`；
  2. **没有在使用、且已过期**：
     - 元数据中的 `in_use` 为空，或记录的进程已不存在（Windows 上用 `OpenProcess` 查询，不能用 `os.kill(pid, 0)`：它在 Windows 上会结束目标进程）；
     - 元数据的修改时间早于 `worktree_stale_hours`；
  3. **没有改动**：W15 判为没有改动。

  标记 `removing: true` 的 worktree 只检查第 1、2 层，然后重试 W14 的第 2–6 步。
- **W17** 时机：
  - 启动后在后台运行一次，不阻塞启动；
  - 运行期间每 24 小时运行一次；
  - `worktree_stale_hours: 0` 关闭自动清理（`/worktree clean` 仍可用）；
  - 不在 git 仓库中时不运行；
  - 清理掉的数量大于 0 时提示一行。
- **W18** `git worktree prune` 只在 W14 和 W16 中执行。

### 子 Agent 隔离

- **A1** 定义字段：
  - `isolation`：`worktree` 或 `none`（默认）；
  - 其他取值 → 定义无效，按第十三章 F5 列出；
  - `isolation` 从“未支持的 Claude Code 字段”中移除；
  - `/agents <name>` 显示隔离方式。
- **A2** `agent` 工具新增可选参数 `isolation`（`worktree` 或 `none`）：
  - 定义或参数任一为 `worktree` 时隔离；
  - 参数不能关闭定义中的隔离。
- **A3** 隔离运行：
  1. 在子 Agent 的任务中创建 worktree（W3、W5–W10），任务的工具行显示“创建工作树…”；
  2. 后台任务也一样：主 Agent 立即拿到任务 id，创建在任务中进行；
  3. 元数据写入 `in_use`（进程 id、任务 id）；
  4. 第一条用户消息之前插入路径说明 `<worktree-context>`，内容包括：
     - 工作目录（worktree 中的对应目录）；
     - 主项目目录，以及“不要修改主目录”；
     - 分支名与基准提交；
     - 父 Agent 给出的绝对路径要换成 worktree 中的路径；
     - 主目录有未提交改动时的说明（W12）；
     - “完成时有改动会保留，无需删除 worktree”；
  5. 运行到完成（第十三章的流程不变）。
- **A4** 不支持隔离时返回 `isolation_unsupported`：
  - Fork 式（省略 `agent_type` 或写 `fork`）同时要求隔离；
  - Hook 的 `agent` 动作使用的定义要求隔离时，正常隔离运行；
  - Skill fork 没有隔离选项。
- **A5** 子 Agent 的有效模式为 `plan` 时：
  - 不创建 worktree，在主目录只读运行；
  - 结果首行说明“计划模式下未创建工作树”。
- **A6** 结束（完成、失败、取消、主会话关闭都执行，取消时也不跳过）：
  - 清除元数据中的 `in_use`；
  - 做 W15 改动检查：
    - **没有改动** → 按 W14 删除 worktree 和分支，结果末尾附“[工作树] 无改动，已删除。”；
    - **有改动** → 保留，结果末尾附：
      ```
      [工作树] 已保留：.mosscode/worktrees/agent-1a2b3c4d
      分支 mosscode/agent-1a2b3c4d，基于 abc1234；未提交 3 个文件，独有提交 2 个。
      合并：git merge mosscode/agent-1a2b3c4d；查看：git -C .mosscode/worktrees/agent-1a2b3c4d status；删除：/worktree remove agent-1a2b3c4d
      ```
  - 合并由主 Agent 或用户决定，MossCode 不自动提交、不自动合并；
  - 创建时的警告（W8）附在这一段之前。
- **A7** 进程被强制结束时，worktree 留在磁盘上，由 W16 清理（没有改动时）。

### 隔离的运行时

- **I1** 工具：
  - 隔离的子 Agent 的工具由 `create_default_registry(root=<worktree 根>)` 的工作区工具组成，再按定义的 `tools` / `disallowed_tools` 和第十三章 L1 过滤；
  - `FileState` 为新的空状态；
  - MCP 工具、Skill 脚本工具不提供；定义的 `tools` 中列出它们时，在 `/agents <name>` 中提示“隔离时不可用”；
  - 第十三章中由主对话提供给子 Agent 的工具（如 `task_output`）照常提供。
- **I2** 权限：
  - `PolicyView` 增加可选的 `root`：隔离的子 Agent 的视图以 worktree 根为根；
  - 规则文件、审批队列和模式规则沿用第十三章 P1–P6；
  - 路径规则相对 worktree 根匹配，例如 `Edit(src/**)` 匹配 worktree 中的 `src`；
  - 主目录中的路径对它而言在项目之外：读取前台需要审批、后台返回 `approval_required`；写入和编辑以 `path_blocked` 拒绝（文件工具对项目外的写入一律如此，不能授权）；
  - 受保护的配置文件同时包括主项目和 worktree 中的副本（`permissions*.yaml`、`hooks*.yaml`、`mcp.yaml`、`.mcp.json`）。
- **I3** 系统提示词：
  - 环境信息的工作目录与 git 快照取 worktree；
  - 项目说明沿用主项目已加载的内容，不从 worktree 重新加载。
- **I4** 不变的部分：
  - 记忆、会话目录、子 Agent 对话记录、Hook 配置、Skill 与 Agent 定义、用量统计都属于主会话；
  - 进程工作目录不变。

### 主 Agent 与 worktree 目录

- **S1** 主 Agent 的 `glob`、`grep` 不进入 `.mosscode/worktrees`；显式把 `path` 指向其中时照常搜索。
- **S2** 主 Agent 读写 worktree 中的文件不受额外限制（它们在主项目目录之内）。
- **S3** 在 MossCode 管理的 worktree 中启动 MossCode 时：
  - 项目说明从 worktree 根向下读取，不再读取主项目根到 worktree 之间的目录中的说明文件，避免同一份说明出现两次；
  - 记忆与会话按 worktree 路径分开（与在其他目录启动相同）。

### 命令

- **M1** `/worktree`（同 `/worktree list`），列出 MossCode 管理的 worktree。每项显示：
  - 名称、分支、基准提交；
  - 创建时间；
  - 手动或临时；
  - 状态：使用中、有改动（数量）、无改动、删除未完成。
- **M2** `/worktree create <名称>`：
  - 名称按 W2 校验；创建或快速恢复（W13）；
  - 元数据 `manual: true`，永不被自动清理；
  - 输出路径、分支、警告，以及在另一个终端中进入该目录的提示。
- **M3** `/worktree remove <名称> [--discard]`：
  - 按 W14 删除；
  - 有改动时拒绝并列出改动，提示 `--discard`；
  - 正在被子 Agent 使用时拒绝。
- **M4** `/worktree clean`：立即执行 W16，报告删除、跳过的数量和跳过的原因。
- **M5** 可以在运行中使用：`/worktree` 和 `/worktree list`；其余子命令要等运行结束。补全：子命令和已有的名称。
- **M6** `/tasks <id>` 的详情显示 worktree 路径、分支与最终的保留或删除结果。
- **M7** `-p "/worktree ..."` 与其他本地命令一样退出码为 2。

### Hook

- **H1** 隔离的子 Agent：
  - 工具事件和 `subagent_start` / `subagent_stop` 增加字段 `worktree`（相对仓库根的路径，如 `.mosscode/worktrees/agent-1a2b3c4d`）；未隔离时为空字符串；
  - 工具事件的 `path` 相对 worktree 根，`MOSSCODE_FILE_PATH` 为 worktree 中的绝对路径；
  - Hook 命令仍在主项目目录运行。
- **H2** `subagent_stop` 增加字段 `worktree_kept`（`true` / `false`；未隔离时为空字符串）。

### 配置

- **C1** 新的配置项：

  | 键 | 默认 | 说明 |
  | --- | --- | --- |
  | `worktree_links` | `[]` | W9，相对仓库根的路径列表；不能是绝对路径，不能含 `..` |
  | `worktree_stale_hours` | `24` | W16；`0` 关闭自动清理；取值 0–8760 |

### 界面

- **U1** 隔离的子 Agent 的工具行在类型后显示“· 工作树”（按实际是否在工作树中运行：计划模式跳过创建时不显示，定义中声明 `isolation: worktree` 时显示）；结束后显示“已保留”或“已删除”。
- **U2** 工具行中的路径相对该子 Agent 的根目录显示。
- **U3** 后台清理、创建警告、删除失败的提示经现有的 `Notice` 路径送到 TUI、stderr 与 stream-json。

## 设计

### 模块

| 模块 | 职责 |
| --- | --- |
| `worktree/__init__.py` | 导出 |
| `worktree/names.py` | `validate_name`、`flatten`、`ephemeral_name()`、Windows 保留名 |
| `worktree/git.py` | `run_git`（环境变量、超时、线程中运行）、`repo_root`、`common_dir`、`read_head_sha`（纯文件系统，W13）、`ensure_excluded`（W6） |
| `worktree/meta.py` | `WorktreeMeta` 的读写（原子写：临时文件后替换）、`in_use` 的设置与清除、进程是否存在 |
| `worktree/setup.py` | W8–W10：复制本地配置、链接（符号链接 → junction）、`.worktreeinclude` |
| `worktree/changes.py` | `Changes`、`count_changes`（W15，失败时按有改动处理） |
| `worktree/manager.py` | `WorktreeManager`：`create`、`resume`、`remove`、`list`、`clean`、`finish`（A6）；进程内的锁 |
| `agents/__init__.py` | `AgentDef.isolation`；`UNSUPPORTED` 去掉 `isolation`；详情中的隔离方式与不可用的工具 |
| `agents/runtime.py` | `isolated_loop`（I1–I3）、路径说明、`drive` 结束时的 `finish`、结果后缀；`describe_call` 相对任务根目录 |
| `agents/tasks.py` | `Task.worktree`（名称、路径、分支、最终结果） |
| `tools/agent.py` | `isolation` 参数；`isolation_unsupported`、`worktree_unavailable` |
| `tools/base.py` | 以新根目录组装子 Agent 的工具视图 |
| `permissions.py` | `PolicyView(root=...)`；受保护文件包括主项目的副本 |
| `tools/search.py`、`search_worker.py` | S1 |
| `prompts.py` | S3：worktree 中的说明文件边界 |
| `hooks/engine.py` | H1、H2 的字段 |
| `config.py` | C1 |
| `builtin_commands.py` | `/worktree`；`/tasks <id>` 的 worktree 信息 |
| `conversation.py`、`cli.py`、`tui.py` | 创建 `WorktreeManager`（仅在 git 仓库中）、启动后台清理、关闭时等待 `finish` 完成、工具行 |

### 数据结构

```python
@dataclass
class WorktreeMeta:
    name: str                      # 原始名称，可含 /
    flat: str                      # 目录名与分支名的后半部分
    path: str                      # 绝对路径
    branch: str                    # mosscode/<flat>
    base: str                      # 创建时的完整 SHA
    created: str                   # ISO 时间
    manual: bool                   # /worktree create 为 True
    links: list[str]               # 创建的链接，相对 worktree 根
    setup_done: bool = False
    in_use: dict | None = None     # {"pid": 1234, "task": "t3"}
    removing: bool = False

@dataclass(frozen=True)
class Changes:
    uncommitted: int
    commits: int
    files: tuple[str, ...]         # 最多 10 个
    error: str = ""                # 检查失败的原因（此时 uncommitted 记为 1）

    @property
    def any(self) -> bool: ...
```

### 数据流

隔离的定义式子 Agent：

1. `AgentTool.execute`：
   - 确定是否隔离（A2）；
   - Fork 式要求隔离 → `isolation_unsupported`；
   - 有效模式为 `plan` → 按 A5 不隔离。
2. `TaskManager.new` 创建任务；任务中：
   1. `WorktreeManager.create(ephemeral_name(), manual=False)`；失败时任务以 `worktree_unavailable` 的说明结束；
   2. `SubAgents.isolated_loop`：
      - 工具 = 以 worktree 中对应目录为根新建的工作区工具，按定义与 L1 过滤；
      - 权限 = `parent.view(mode, interactive=..., root=<该目录>)`；
      - 环境信息与 git 快照取该目录；项目说明取主项目的；
   3. `drive` 在第一条用户消息之前插入 `<worktree-context>`，之后与第十三章相同；
   4. `finally`（不受取消影响）：`WorktreeManager.finish(meta)` → 检查改动 → 删除或保留 → 生成结果后缀。
3. 结果（前台）或通知（后台）带上 A6 的后缀；`subagent_stop` 带上 `worktree`、`worktree_kept`。

## 取舍

- **换根目录而不是传 cwd。** 课程方案改造每个工具从 ctx 读 cwd。MossCode 的工具实例本来就绑定根目录，而子 Agent 本来就有自己的工具实例，换根目录后路径解析、项目内外判断、Hook 的路径都自然正确，进程工作目录也无需变化。代价是隔离的子 Agent 用不了 MCP 工具（D12）。
- **项目说明不从 worktree 重新加载。** worktree 基于 `HEAD`，与主项目的说明文件只在“主目录有未提交的改动”时不同；重新加载还会从父目录读到主项目的说明（S3 的问题）。沿用已加载的内容既简单，又与主对话一致。
- **链接默认关闭。** 链接省磁盘、省安装时间，但会让 worktree 与主项目共享可变的状态，Windows 上还有特权与删除穿透的问题（E1、E3）。需要的用户可以明确列出。
- **不自动提交。** 自动提交能让“有改动”只剩一种形态，但提交信息、提交哪些文件应由子 Agent 或用户决定；保留未提交的改动同样能被检查和合并。
- **改动检查失败时保留。** 宁可多留一个目录，也不删除可能有价值的改动；`/worktree remove --discard` 可以手动处理。
- **主会话不进入 worktree。** 见 D3。需要在 worktree 中长时间工作时，用 `/worktree create` 创建后，在另一个终端中进入该目录运行 MossCode。

## 不做

- worktree 之间的合并策略、跨 worktree 的代码同步、文件监视。
- 主会话进入、退出 worktree（`/worktree enter` / `exit`），以及相应的会话持久化与恢复。
- 多 Agent 并行编排与团队协作（下一章）。
- Fork 式子 Agent、Skill fork 的隔离。
- 隔离的子 Agent 使用 MCP 工具、Skill 脚本工具。
- 子模块的初始化：仓库有 `.gitmodules` 时，在警告中提示 worktree 中的子模块未初始化。
- 跨进程共享同一个 worktree：同一 worktree 同时只被一个子 Agent 使用。
- 自动提交、自动合并、自动推送。

## 已确认的决定（2026-10-30）

| # | 问题 | 决定 |
| --- | --- | --- |
| Q1 | 主会话是否支持 `/worktree enter` / `exit`？ | 不支持，只做子 Agent 隔离和手动创建、删除（D3） |
| Q2 | 默认是否链接 `node_modules`、`.venv` 等依赖目录？ | 默认不链接，由 `worktree_links` 开启（D5） |
| Q3 | 是否复制 `config.yaml`、`.env`？ | 不自动复制，由 `.worktreeinclude` 列出（D6） |
| Q4 | `agent` 工具是否增加 `isolation` 参数？ | 增加（D13），工具定义变化一次 |

## 实现说明

与上文不同或上文未写明的地方：

- **写主目录**（I2、AC12）：隔离的子 Agent 写入主目录返回 `path_blocked`，比规格原先的“前台审批”更严格。文件工具对项目根之外的写入一直如此，不为隔离开例外。
- **`execute_command`**：命令在工作树中运行，但命令本身仍可访问主目录，路径检查只约束文件工具（应用层限制，见 [权限](../../reference/permissions.md#路径限制)）。
- **`.worktreeinclude`**：用 `git ls-files --others --ignored --exclude-from=.worktreeinclude` 求出要复制的文件，只复制未跟踪的文件，已链接的路径跳过。
- **改动检查**：MossCode 链接和复制进工作树的路径记在元数据的 `links`、`copied` 中，不算未提交的改动；独有提交用 `rev-list HEAD --not --exclude=<分支> --branches --remotes` 计算（与 `--branches` 连用时 `--exclude` 的模式相对 `refs/heads/`）。
- **名称**：段中含 `..`（如 `a..b`）也拒绝，git 不接受这样的分支名。
- **删除失败的重试**：手动工作树由再次 `/worktree remove` 重试（不再检查改动）；临时工作树另由过期清理重试。
- **`-p`**：不运行启动时的过期清理；隔离的子 Agent 照常创建和收尾。
- **运行中的命令**：`Command.while_running` 可以是按参数判断的函数，`/worktree` 只有 `list`（或不带参数）在运行中可用。

## 任务

| # | 内容 | 依赖 | 验证 |
| --- | --- | --- | --- |
| T1 | `worktree/names.py`、`git.py`、`meta.py`：名称校验、仓库探测、纯文件系统读 `HEAD`、`info/exclude`、元数据 | — | 单元测试（AC1、AC4） |
| T2 | `worktree/changes.py`、`setup.py`、`manager.py`：创建、快速恢复、初始化、改动检查、删除、清理 | T1 | 临时 git 仓库测试（AC2、AC3、AC5–AC10） |
| T3 | 运行时：`PolicyView(root)`、新根目录的工具视图、`isolated_loop`、路径说明、`finish`、结果后缀；`AgentDef.isolation`、`agent` 工具参数 | T2 | 假提供商测试（AC11–AC15） |
| T4 | 主 Agent 侧：搜索跳过 worktree 目录、S3 的说明文件边界、Hook 字段 | T3 | 搜索、提示词、Hook 测试（AC16–AC18） |
| T5 | `/worktree` 命令、`/tasks` 详情、配置项、启动时的后台清理、TUI 工具行、`-p` | T2、T3 | `FakeUI`、`run_test`、`tests/test_cli.py`（AC19–AC21） |
| T6 | 文档：新增 `docs/reference/worktree.md`；更新 README、`docs/reference/subagents.md`、`commands.md`、`hooks.md`、`permissions.md`、`docs/acceptance.md`；第十三章规格中 `isolation` 的说明；本规格状态 | T1–T5 | `tests/test_docs.py`；全量测试（AC22） |

T4 与 T5 互不依赖。

## 验收

| # | 标准 | 方法 |
| --- | --- | --- |
| AC1 | 名称：<br>• `feature/a`、`agent-1a2b3c4d` 通过；<br>• 以下被拒绝：`""`、65 个字符、`..`、`a/../b`、`a//b`、`/a`、`a/`、`.hidden`、`-x`、`a.lock`、`a.`、`nul`、`COM1.txt`、`a b`、`中文`；<br>• 已有 `Demo` 时 `demo` 被拒绝；<br>• `team/alice` 的目录为 `team+alice`，分支为 `mosscode/team+alice` | 单元测试 |
| AC2 | 创建：<br>• 在临时仓库中创建后，目录、分支、元数据都存在，`base` 为主目录 `HEAD`；<br>• `info/exclude` 含 `/.mosscode/worktrees/`，只写一次；<br>• 主目录 `git status --porcelain` 为空；<br>• 非 git 目录、空仓库 → `worktree_unavailable`；<br>• 分支已存在 → 报错且没有残留目录 | 临时 git 仓库测试 |
| AC3 | 在子目录 `pkg` 中启动时，子 Agent 的根目录为 `<worktree>/pkg` | 临时 git 仓库测试 |
| AC4 | 快速恢复：<br>• 目录有效时不启动 git 进程（替换 `run_git` 断言未调用）；<br>• 用 `git pack-refs --all` 打包后仍能读出 `HEAD`；<br>• `.git` 文件损坏或元数据缺失 → 报错且不删除目录；<br>• `setup_done: false` 时补做初始化 | 单元测试 |
| AC5 | 初始化：<br>• `.mosscode/permissions.local.yaml` 被复制；<br>• `.worktreeinclude` 中的 `.env` 被复制，未列出的 `config.yaml` 不被复制；<br>• `worktree_links: [node_modules]` 时，worktree 中为链接（Windows 上无特权时为 junction）；<br>• 某一步失败时创建仍成功并带有警告 | 临时 git 仓库测试 |
| AC6 | git hooks：主仓库的 `pre-commit` hook（写一个标记文件）在 worktree 中 `git commit` 时被执行；创建前后主仓库的 `.git/config` 不变 | 临时 git 仓库测试 |
| AC7 | 改动检查：<br>• 修改、新增未跟踪文件 → `uncommitted > 0`；<br>• 在分支上提交 → `commits > 0`；<br>• 把该分支合并进 `main` 后 → `commits == 0`；<br>• 本地 `main` 有未推送提交、分支无新提交 → `commits == 0`；<br>• git 命令失败 → 判为有改动 | 临时 git 仓库测试 |
| AC8 | 删除安全：<br>• worktree 中有指向主仓库 `node_modules` 的 junction（或符号链接），删除后主仓库的 `node_modules` 内容完好；<br>• 不调用 `git worktree remove`（替换 `run_git` 记录参数）；<br>• 删除后分支与 `git worktree list` 中的记录都已消失；<br>• 有改动时不带 `--discard` 被拒绝 | 临时 git 仓库测试 |
| AC9 | 删除失败：模拟 `rmtree` 失败 → 元数据标记 `removing`，下次 `clean` 时重试成功 | 临时 git 仓库测试 |
| AC10 | 过期清理：<br>• 过期、无改动的临时 worktree 被删除；<br>• 以下保留：未过期、有改动、`manual: true`、没有元数据、名称不符、`in_use` 的进程仍存在；<br>• `in_use` 的进程不存在时视为未使用；<br>• `worktree_stale_hours: 0` 时启动不清理 | 临时 git 仓库测试 |
| AC11 | 隔离运行（假提供商，临时 git 仓库）：<br>• 定义 `isolation: worktree` 的子 Agent `write_file` 写入 `a.txt`，主目录的 `a.txt` 不变，worktree 中已改；<br>• 结果末尾为“已保留”，含分支与合并命令；<br>• 只读的子 Agent 结束后 worktree 与分支都被删除，结果末尾为“已删除” | 假提供商测试 |
| AC12 | 隔离的边界：<br>• 系统提示词的工作目录为 worktree；第一条用户消息前有 `<worktree-context>`；<br>• 工具中没有 MCP 工具；<br>• 写主目录中的文件返回 `path_blocked`，不出现审批请求；<br>• 写 worktree 中的 `.mosscode/permissions.yaml` 被拒绝 | 假提供商测试 |
| AC13 | 参数与限制：<br>• `agent` 工具 `isolation: worktree` 让 `general` 隔离；<br>• Fork 式加 `isolation` → `isolation_unsupported`；<br>• 主对话为计划模式时不创建 worktree，结果首行说明原因；<br>• 未知的 `isolation` 取值使定义无效 | 假提供商测试 + 定义测试 |
| AC14 | 生命周期：<br>• 后台隔离任务立即返回任务 id；<br>• 运行中取消 → `finish` 仍执行（有改动时保留）；<br>• 两个隔离的子 Agent 并行运行，各自的 worktree 不同，互不影响 | 任务测试 |
| AC15 | 主目录有未提交改动时，`<worktree-context>` 与结果中都有说明 | 假提供商测试 |
| AC16 | 主 Agent 的 `glob "**/*.txt"`、`grep` 不返回 `.mosscode/worktrees` 中的文件；`path` 指向其中时返回 | 搜索测试 |
| AC17 | 在 worktree 目录中加载项目说明，主项目根的 `AGENTS.md` 只出现一次（来自 worktree） | 提示词测试 |
| AC18 | Hook：隔离的子 Agent 写文件时，`post_tool_use` 的 `worktree` 字段非空，`MOSSCODE_FILE_PATH` 在 worktree 中；`subagent_stop` 的 `worktree_kept` 正确 | Hook 测试 |
| AC19 | 命令：<br>• `/worktree create demo` 创建；再次执行显示“已存在”且不调用 git；<br>• `/worktree` 列出状态；<br>• `/worktree remove demo` 在有改动时拒绝，`--discard` 后删除；<br>• `/worktree clean` 报告数量；<br>• 非法名称给出原因；<br>• 运行中只有列表可用 | `FakeUI` 测试 |
| AC20 | 配置：`worktree_links` 含绝对路径或 `..`、`worktree_stale_hours: -1` 时报配置错误；默认值生效 | 配置测试 |
| AC21 | TUI：隔离的子 Agent 工具行显示“· 工作树”和最终的保留或删除；`/tasks <id>` 显示 worktree 信息；`-p "/worktree"` 退出码 2 | `run_test`、`tests/test_cli.py` |
| AC22 | 全量测试通过；README 二级标题不变，文档链接有效 | `pytest`、`tests/test_docs.py` |

人工验收（写进 `docs/acceptance.md`，在验收项目中进行；验收项目是 git 仓库）：

- 写一个 `isolation: worktree` 的定义，让模型用它修改 `notes.txt`：
  - 确认主目录的 `notes.txt` 未变，`.mosscode/worktrees/agent-…` 中已改；
  - 确认结果中有分支名与合并命令；
  - 执行 `git merge` 后 `/worktree remove` 能够删除；
- 让模型用 `general` 加 `isolation: worktree` 只做阅读，确认结束后 worktree 与分支都不存在；
- 两个隔离的子 Agent 同时修改同一个文件的不同位置，确认各自的 worktree 中只有自己的改动；
- 在 `worktree_links` 中加入 `node_modules`（验收项目中放一个假的 `node_modules`），创建并删除 worktree 后，确认主项目的 `node_modules` 完好；
- `/worktree create demo`、`/worktree`、`/worktree remove demo` 的显示；
- 在 `.mosscode/worktrees/demo` 中另开终端启动 MossCode，确认项目说明不重复；
- 主 Agent 搜索时不出现 worktree 中的文件；
- Windows Terminal 中检查工具行与 `/worktree` 列表的显示。
