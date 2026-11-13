# 参与贡献

欢迎提交问题报告、功能建议和 Pull Request。较大的改动请先开 Issue 讨论做法，避免白费工夫。安全漏洞请按 [安全策略](SECURITY.md) 私下报告。

## 开发环境

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Git。

```
git clone https://github.com/yzd11/mosscode.git
cd mosscode
uv venv --python 3.12
uv pip install -e ".[dev]"
```

虚拟环境中有 `moss` 命令（Windows 上是 `.venv\Scripts\moss.exe`），`python -m mosscode` 同样可用。想在任意目录用开发中的版本，可以把仓库安装成可编辑的全局命令，修改代码后不用重装：

```
uv tool install --editable .
```

依赖或命令入口（`pyproject.toml`）变化后加 `--reinstall` 重新安装。需要复现锁定的依赖版本时，先安装 `requirements-lock.txt`，再执行 `uv pip install --no-deps -e .`。

## 测试

```
python -m pytest -q
```

测试使用模拟模型、模拟 SSE 和无界面 TUI，不读取 API 配置，也不发送请求。开发时只运行和改动相关的测试文件；提交 Pull Request 前运行一次全量测试。与真实模型服务的联调需要自己配置 Provider。

没有安装 ripgrep 时，搜索测试走内置实现；CI 中两种情况都会测试。

改动提示、工具或 Agent Loop 后，想知道对真实模型完成任务的影响，可以运行评估：`python scripts/run_evals.py` 在一组固定的编码任务上运行 MossCode，用隐藏测试判定，报告通过率、Token、缓存命中率和耗时，见 [评估任务](evals/README.md)。评估会请求真实服务并产生费用。

## 在容器中开发

在 Windows 或 macOS 上想在 Linux 中跑测试，或者不想让开发中的 MossCode 直接接触主机，可以用容器。虚拟环境要建在挂载目录之外，否则会覆盖主机上的 `.venv`：

```
docker run --rm -it -v "${PWD}:/src" -w /src -e UV_LINK_MODE=copy ghcr.io/astral-sh/uv:python3.12-bookworm-slim bash
```

进入容器后：

```
apt-get update && apt-get install -y --no-install-recommends git ripgrep
uv venv /opt/venv && . /opt/venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest -q -p no:cacheprovider --basetemp /tmp/pytest tests/test_permissions.py
```

`-p no:cacheprovider` 和 `--basetemp /tmp/pytest` 让测试不在挂载的仓库中留下文件。用 Dev Container 时同理：在 `devcontainer.json` 中把虚拟环境放在容器的家目录，例如 `"postCreateCommand": "uv venv ~/.venv && . ~/.venv/bin/activate && uv pip install -e '.[dev]'"`。

把 MossCode 本身放在容器中使用（隔离模型执行的命令）见 [在容器中运行](docs/reference/container.md)。

## 代码检查

```
ruff check .
ruff format --check .
```

`ruff check --fix` 和 `ruff format` 可以自动修复大部分问题。代码风格、注释、平台兼容和文档的约定见 [AGENTS.md](AGENTS.md)。

## Pull Request

- 一个 Pull Request 做一件事；提交信息的第一行说明改了什么。
- 新行为和修复的缺陷都要有测试。
- 用户可见的变化同步更新 `docs/reference/` 中的文档，并在 [CHANGELOG.md](CHANGELOG.md) 的 `[Unreleased]` 下记一条。
- CI 在 Windows、Linux 和 macOS 上运行测试、代码检查和打包检查，全部通过后合并。安装 wheel 后还在源码目录之外运行 `scripts/smoke_wheel.py`，用假模型完成配置加载、文件读写，并检查内置 Skill 和模型目录资源，不读取个人配置、不联网。

## 发布

维护者发布新版本的步骤：

1. 更新 `pyproject.toml` 中的 `version`；把 `CHANGELOG.md` 中 `[Unreleased]` 的内容移到新版本标题下，写上日期，并更新底部的比较链接。
2. 提交后打标签并推送：`git tag v0.1.1`、`git push origin v0.1.1`。
3. 发布流程先在打标签的提交上跑一遍完整的 CI（ruff、三个系统上的测试、打包检查），全部通过后才构建；任何一项失败都不会上传，修复后删除标签、重新打标签推送即可（PyPI 上的版本一旦上传就不能替换）。之后先上传到 TestPyPI 并检查能否安装，再上传到 PyPI，最后创建 GitHub Release。上传使用 PyPI 的 Trusted Publishing，不需要 API 令牌。
