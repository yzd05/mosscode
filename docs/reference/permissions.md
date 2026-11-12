# 工具权限

四个权限档位、规则文件与命令风险分级。

## 档位

默认档位为 `default`。四档在没有匹配规则时按下表判断；除下文的只读命令外，命令统一作为有副作用操作，即使用 Bash/PowerShell 写文件，也不能获得 acceptEdits 的文件工具自动授权。

| 模式 | 只读工具与只读命令 | 专用文件写入/编辑 | 其他命令 |
| --- | --- | --- | --- |
| default | Allow | Ask | Ask |
| acceptEdits | Allow | Allow | Ask |
| plan | Allow | 不可用（plan_blocked） | 不可用（plan_blocked） |
| bypassPermissions | Allow | Allow | Allow |

**只读命令**无需审批，计划模式中也可执行：`git status/diff/log/show/blame/rev-parse/ls-files/describe/shortlog`、只带列表参数的 `git branch`/`git remote`，以及 `ls`、`pwd`、`cat`、`head`、`tail`、`wc`、`stat`、`tree`、`which`、`grep`、`rg`、`find` 和 PowerShell 的 `Get-ChildItem`、`Get-Content`、`Select-String` 等。必须是单条简单命令（不含管道、串联、重定向、变量和命令替换），路径参数限于项目内（拒绝绝对路径、`~` 和 `..`），并排除有副作用或执行外部程序的选项（`git -c`、`--output`、`--ext-diff`、`tail -f`、`rg --pre`/`-z`、`find -delete/-exec` 等）。生效的 deny 规则仍优先。白名单之外的命令即使只是读取也按档位审批。

## 审批

Ask 在输入框的位置申请授权：审批面板出现时输入框暂时隐藏，其中没发送的文字在回答后原样恢复。↑ / ↓ 选择、Enter 确认；Esc 拒绝当前调用并让模型调整，Ctrl+C 取消整个任务。点击选项不会执行授权。审批面板保留已有对话，背景沿用终端，选项使用蓝色高亮。写入和编辑的审批面板显示 unified diff 预览（新增行绿色、删除行红色，超过 16 行截断）；新建文件显示前 16 行内容，原文无法匹配时回退为原文/替换文本。空闲时按 Shift+Tab 依次切换 default → acceptEdits → plan → default（bypassPermissions 时回到 default），运行中不切换。旧档位 strict、permissive 已移除：配置文件或 `--permission-mode` 使用它们会报错并提示改用 default 或 bypassPermissions。

普通授权有三个允许选项：本次允许，只执行当前调用；本会话允许，当前工具对同一目标可继续执行，退出失效；永久允许，保存到当前项目的 `.mosscode/permissions.local.yaml`，重启有效。文件授权只匹配具体文件与工具；命令授权精确匹配命令，并绑定项目相对工作目录。不会自动保存为 `Write(**)` 或 `Bash(*)`。另有两个按操作出现的选项：写入/编辑审批提供“本会话自动接受编辑”，切换为 acceptEdits 档位，之后的编辑不再询问，命令仍需确认；命令能推导出前缀时提供“永久允许以 X 开头的命令”，保存为 `Bash(X:*)` 前缀规则。前缀取开头最多三个普通单词（如 `pytest`、`npm run test`、`python -m pytest`），遇到选项、路径或特殊字符即停止；脚本解释器（`python x.py`、`node app.js`）、包装命令（`sudo`、`env`、`bash -c`、`xargs`）、单独的 `git`/`npm` 和高风险命令不提供前缀选项。

## 沙箱

打开 [命令沙箱](sandbox.md) 后，沙箱中的命令在 default、acceptEdits 和 bypassPermissions 下都不再询问：它能写、能读和能连接什么由系统限制。Windows 的受限令牌后端只限制写入（状态栏显示“沙箱·部分”），命令照常审批，审批面板注明“沙箱只限制写入”。显式 deny 规则、灾难级命令的拒绝和高风险命令的确认照旧，plan 仍只开放只读命令。模型申请在沙箱外运行（`outside_sandbox`）时每次询问，bypassPermissions 也一样；选项只有本次、本会话（这条命令或它的前缀）和拒绝，不能永久保存，已有的 allow 规则也不会让它自动通过。沙箱打开但不可用时命令以 `sandbox_unavailable` 拒绝。

## 子 Agent

子 Agent 与主对话共用规则文件、硬限制和审批界面，有自己的档位和本会话授权：它的授权从空开始，审批中授予的也不带回主对话。主对话处于 plan 时子 Agent 一律为 plan；项目级 Agent 定义只能收紧档位，用户级和内置定义可以指定任意档位。后台子 Agent 不能请求审批，需要审批的调用返回 `approval_required`；前台子 Agent 的审批面板标题注明来源。详见 [子 Agent](subagents.md#权限)。

在 git 工作树中隔离运行的子 Agent 以工作树根为项目根：路径规则相对工作树匹配；主目录对它是项目之外，读取需要审批，写入和编辑以 `path_blocked` 拒绝；主项目和工作树中的权限、MCP、Hook 配置文件都受保护。见 [Git 工作树](worktree.md#权限)。

## 规则文件

权限配置独立于 `config.yaml`，可参考 [permissions.example.yaml](../../permissions.example.yaml)。从低到高加载：

| 层级 | 文件或位置 |
| --- | --- |
| 用户级 | `~/.mosscode/permissions.yaml`，所有项目的默认规则 |
| 项目级 | `<项目>/.mosscode/permissions.yaml`，可共享的项目规则 |
| 本地级 | `<项目>/.mosscode/permissions.local.yaml`，当前项目本机规则 |
| 会话级 | 内存中的临时授权，退出失效 |

规则按最高匹配层判断，同层同时命中时 deny 优先。更高层规则可覆盖更低层，任何模式都尊重最终生效的 deny；明确 allow 可以免去表格中的默认 Ask。配置 mode 按本地 > 项目 > 用户覆盖；启动参数 --permission-mode default/acceptEdits/plan/bypassPermissions 优先于文件，/permissions 切换仅影响本会话。配置格式错误阻止启动。

```yaml
mode: default
allow:
  - Read(src/**)
  - Bash(pytest:*)
deny:
  - Write(.git/**)
  - Edit(.git/**)
  - Bash(git push:*)
```

可用别名为 `Read`、`Write`、`Edit`、`Bash`/`Shell`、`Glob`、`Grep`、`WebFetch`、`WebSearch`（见下文 [网络工具](#网络工具)），也接受实际工具名。文件规则匹配解析后的项目相对路径；`Glob`/`Grep` 规则匹配搜索范围的路径。`*` 匹配单个路径段，`**` 可跨目录；Windows 文件匹配忽略大小写。读取类规则（`Read`/`Glob`/`Grep`）可以写绝对路径以放行项目外的读取，例如 `Read(C:/Users/me/notes/**)`；相对路径规则只匹配项目内，`Read(**)` 不会放行项目外。写入和编辑规则必须是项目相对路径。`Bash` 是规则别名，实际运行配置的 shell。命令规则默认匹配完整原始命令；含串联、管道、重定向、变量或命令替换等复杂语法的命令不能凭 `Bash(git *)` 这类 glob 自动授权。以 `:*` 结尾的规则是按单词的前缀规则：allow 形式 `Bash(pytest:*)` 匹配 `pytest` 和 `pytest tests -q`，不匹配 `pytestx`，也不放行任何复杂语法命令；deny 形式 `Bash(git push:*)` 会检查复合命令的每一段，`git status && git push` 同样被拒绝。

自动保存的规则使用对象形式保留精确匹配和命令目录，例如：

```yaml
allow:
  - rule: execute_command(git status)
    exact: true
    cwd: src
```

## 网络工具

`web_fetch` 和 `web_search` 把网址或查询词发到外部，不算只读工具：没有匹配规则时在 default、acceptEdits 和 plan 中询问（计划模式也提供这两个工具），bypassPermissions 中允许；deny 在任何档位都生效。无界面模式中没有被规则放行的调用返回 `approval_required`。本机和内网地址在审批之前就被拒绝，任何规则和档位都不能放行，见 [工具与 Agent Loop](tools.md#web_fetch)。

| 规则 | 匹配 |
| --- | --- |
| `WebFetch(domain:docs.python.org)` | 这个主机的任意页面 |
| `WebFetch(domain:*.python.org)` | 子域名，如 `docs.python.org`；不含 `python.org` 本身 |
| `WebFetch(domain:*)` 或 `WebFetch` | 任意域名 |
| `WebSearch` | 任意搜索，不带参数 |

域名不区分大小写，中文等国际化域名按 IDNA 编码后比较，`*` 和 `?` 是通配符；规则只匹配主机，不匹配路径和端口。也可以写实际工具名 `web_fetch(domain:…)`、`web_search`。网络规则不接受 `exact` 和 `cwd`，`WebSearch(…)` 带参数时报错。没有内置预先放行的域名。

审批选项：`web_fetch` 为本次允许、本会话允许这个域名（同一主机的其他页面不再询问）、永久允许这个域名（保存为 `WebFetch(domain:主机)`）；`web_search` 为本次允许、本会话允许搜索、永久允许搜索（保存为 `WebSearch`）。审批面板显示完整网址或查询词以及使用的搜索服务。沙箱中的命令联网时也按 `WebFetch(domain:…)` 规则判断，见 [命令沙箱](sandbox.md#联网)。

```yaml
allow:
  - WebFetch(domain:docs.python.org)
  - WebFetch(domain:*.github.io)
  - WebSearch
deny:
  - WebFetch(domain:*.example.com)
```

## 危险命令

程序内置命令特征分两级。**灾难级**——删除文件系统根目录或用户主目录、`--no-preserve-root`、格式化和分区、写块设备、fork 炸弹、关机重启——一律以 `dangerous_command` 拒绝，不可被配置、档位或用户确认解除。**高风险级**——递归强制删除（`rm -rf`、`Remove-Item -Recurse -Force`、`rmdir /s`、`del /s`）、`git reset --hard`、`git clean -f`、`git push --force`、`Invoke-Expression`/`iex`、PowerShell 编码命令——在任何档位都要求每次确认：会话规则、永久规则和 bypassPermissions 都不会自动放行，显式 deny 规则仍然生效；审批面板说明风险原因，只提供“本次允许”和“拒绝”；无界面模式返回 approval_required。两级特征既检查命令原文，也检查用 bash 语法（tree-sitter）拆出的每条子命令：引号和转义去掉后再比对，所以 `r''m -rf /`、`git "push" --force`、`$(…)` 和子 shell 中的命令同样能识别；显式 deny 规则也按这些子命令匹配。拆分只增加检查，不会放行原文已命中的命令；PowerShell 命令按同样方式多检查一遍，可能多出确认，不会少。依赖变量或命令替换的部分（如 `$cmd -rf /`）无法静态判断，特征仅覆盖已知写法，不保证识别全部混淆或间接执行，也可能误报。

## 路径限制

专用文件工具限制在启动时捕获的项目根目录内；解析符号链接和 Windows 目录 junction，再按路径组成部分检查包含关系。新文件父目录和搜索遍历同样检查，不能通过 `..`、相邻同前缀目录或链接访问项目外。模型不能通过专用写入/编辑工具改自己的权限文件，以及 MCP 配置、[Hook](hooks.md) 文件和项目的插件声明 `.mosscode/plugins.yaml`（返回 policy_protected）；同样不能写 `.git/hooks/` 下的文件和 `.git/config`，因为其中的程序（钩子、`core.fsmonitor` 等）会在你之后运行 git 时执行。`core.hooksPath` 指向的其他目录不在保护范围内。

读取项目外的路径需要审批，以下目录除外（计划模式中也一样）：`read_file` 读取当前会话的会话目录（存盘的大结果和压缩记录，见 [上下文管理](context.md#会话目录)）；`read_file` 和 `glob` 读取两个记忆笔记目录。`memory` 工具只写笔记目录、不接受路径参数，不需要审批，显式的 deny 规则仍然生效（见 [记忆笔记](memory.md)）。

用户自己输入的读取和命令不经过审批，任何档位（包括 plan）都一样：消息中 `@` 引用的文件和目录（见 [引用文件](interface.md#引用文件)），以及输入框中 `!` 开头的命令（见 [执行命令](interface.md#执行命令)，高风险命令也不再确认）。硬性检查（凭据文件、灾难级命令）和显式 deny 规则仍然生效。

**这是应用层路径限制，不是系统级沙箱。** 命令工作目录受检查，子进程仍拥有当前用户的系统权限，用户确认及正则无法约束其所有实际文件访问；确认命令前应根据其具体内容判断。其他进程并发更换链接的所有竞争情况也不在保证范围内。尚未加入网络限制、资源配额或审计日志。

模型运行的命令（`execute_command`、Skill 工具）另有三项限制，减少命令顺手带出的东西，但不构成边界：

- 环境中去掉形似密钥的变量，见 [密钥保护](providers.md#密钥保护)；
- `TEMP`、`TMP`、`TMPDIR` 指向这个 MossCode 进程私有的 `~/.mosscode/tmp/<进程号>-<随机>/`，进程退出时删除，残留超过 7 天的目录在下次启动时清理。Git Bash 中的 `/tmp` 仍是系统临时目录，`mktemp` 和 `$TMPDIR` 使用私有目录；
- Windows 上命令所在的 Job Object 带界面限制：不能读取剪贴板，不能切换桌面、修改系统参数和显示设置、注销或关机。通过 WMI 或计划任务启动的进程不在 Job 中，不受这些限制。

Hook 和 `!` 命令不受这三项影响。MossCode 不自带沙箱；需要把命令的影响限制在项目目录内时，在容器中运行，做法和容器仍不隔离的部分见 [在容器中运行](container.md)。

权限拒绝、需确认、保存失败等通过结构化工具结果回写，Agent Loop 可继续调整，仍受轮数上限约束。--prompt 无界面模式不会挂起等待审批；Ask 返回 approval_required。计划行为与权限中的 plan 使用同一状态，避免相互矛盾；硬检查、高风险命令确认和计划模式的只读限制不能通过已保存授权解除。
