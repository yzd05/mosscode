# MossCode 开发说明

这份文件写给在本仓库中工作的 coding agent（MossCode 自己、Claude Code、Codex 等），也适合人阅读。贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 项目

MossCode 是终端里的 coding agent，Python 3.12+，界面用 Textual。源码在 `src/mosscode/`，测试在 `tests/`，面向用户的完整行为说明在 `docs/reference/`。模块职责见 [README 的架构一节](README.md#架构)。

主要入口：

- `launcher.py`：命令入口，`--version`、`--help` 和子命令不导入界面与 SDK；
- `cli.py`：解析参数，启动 TUI 或 `-p`；
- `conversation.py`：上下文、MCP、会话，TUI 与 `-p` 共用；
- `agent.py`：Agent Loop；
- `tools/`：工具的注册、校验、授权和执行；
- `providers.py`：三种模型协议的适配。

## 环境

```
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Windows 上虚拟环境的解释器是 `.venv\Scripts\python.exe`，其他系统是 `.venv/bin/python`。

## 测试

测试使用模拟模型、模拟 SSE 和无界面 TUI，不读取 API 配置，不发送网络请求。

- 改动后只运行相关的测试文件，例如改了 `permissions.py` 就运行 `tests/test_permissions.py`、`tests/test_permission_rules.py`、`tests/test_permission_modes.py`。全量测试需要几分钟，留给提交 Pull Request 前或 CI。
- 测试的临时目录放在仓库内并在结束后删除：`python -m pytest -q --basetemp .test-artifacts/<名称> -p no:cacheprovider tests/test_x.py`。
- 新行为配新测试，修复的缺陷配能复现它的测试。测试名描述行为，不带需求编号。
- 不要为了让测试通过而放宽断言或跳过测试；先找出失败原因。

## 代码风格

- `ruff check` 和 `ruff format --check` 必须通过，配置在 `pyproject.toml`（行宽 120）。
- 代码、注释和 docstring 用英文；界面文字、命令输出和 `docs/` 用中文。
- 注释说明“为什么”，不复述代码；不写需求编号、阶段名或开发过程。
- 优先复用现有的函数和模式。新增依赖要说明理由，并确认许可证与 MIT 兼容（不引入 GPL）。
- 保持文件原有的换行符和编码（UTF-8）。

## 平台

MossCode 同时运行在 Windows、Linux 和 macOS 上：

- 路径用 `pathlib`，不要假设分隔符；命令工具在 Windows 上优先使用 Git Bash，找不到时用 PowerShell。
- Windows 专用的代码（`windows_inline.py`、`tools/windows_job.py`、Windows Terminal 分屏）必须在其他系统上安全地跳过，测试用 `sys.platform` 条件标记。
- 搜索优先使用 PATH 中的 `rg`，没有时用内置实现，两条路径都要测到。

## 文档

- 改变用户可见的行为时，同步更新 `docs/reference/` 中对应的文档，必要时更新 README。
- README 只有四个二级标题：安装、配置、使用、架构（`tests/test_docs.py` 会检查），文档中的相对链接必须能解析。
- 用户可见的变化在 `CHANGELOG.md` 的 `[Unreleased]` 下记一条。

## 安全

- 不要读取、打印或提交任何凭据：`config.yaml`、`.env`、`~/.mosscode/` 下的配置都可能包含 API 密钥。
- 权限检查、命令风险分级和路径检查是安全边界，改动它们时补充绕过方向的测试。

## 代码审查（CodeRabbit）

CodeRabbit 按本文件与 `.coderabbit.yaml` 审查 PR，审查意见用中文输出。

### 核心不变量

- 未知状态绝不静默当成 0 或当成允许；失败必须 fail-closed。
- 写操作需幂等或加锁；跨进程共享状态文件必须用原子写（临时文件 + rename）与文件锁保护。
- 权限检查、命令风险分级和路径检查是安全边界，绝不能被绕过或放宽。

### 安全与隐私

- 禁止在命令行参数（`argv`）、日志或报错详情中输出密钥、Token 或凭证。
- 禁止默认降级到明文存储敏感凭据，除非显式通过环境变量开启。
- 外部 URL、文件路径与命令参数必须做白名单或规范化校验。

### 错误处理与并发

- 不要用裸 `except: pass` 或无条件 `.ok()` 吞掉取消（Cancel）与 I/O 错误。
- 外部接口 / JSON 返回 null、缺字段或超时 / 取消信号须正确透传，不静默吞错。
- 涉及子进程、文件句柄或网络连接时，异常退出不得泄漏或留下孤儿进程；并发读写须检查竞态（TOCTOU）与未回滚状态。

### 提交与验证

- 代码注释只解释“为什么（外部约束、陷阱、不变量）”，不重复代码本身做了什么。
- 提交信息遵循 Conventional Commits（`feat:` / `fix:` / `refactor:` / `chore:`），说明用户可见变化。
- 每次修复 Bug 必须附带可复现该问题的最小回归测试；不要为让测试通过而放宽断言或跳过测试。
