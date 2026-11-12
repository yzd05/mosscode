"""`moss doctor` reads the installation, the configuration and the tools it relies on.

It starts no session and sends no request. Credentials are reported as set or not, never shown.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

OK, WARN, ERROR = "✓", "!", "✗"


@dataclass(frozen=True)
class Item:
    level: str
    text: str
    fix: str = ""


def _distribution():
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        return distribution("mosscode")
    except PackageNotFoundError:
        return None


def install_method(prefix: str | None = None, dist=None) -> str:
    prefix = sys.prefix if prefix is None else prefix
    parts = [part.lower() for part in Path(prefix).parts]
    if "uv" in parts and "tools" in parts:
        kind = "uv tool"
    elif "pipx" in parts:
        kind = "pipx"
    elif sys.prefix != sys.base_prefix:
        kind = f"虚拟环境 {prefix}"
    else:
        kind = f"Python 环境 {prefix}"
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}") if dist is not None else {}
    except (ValueError, OSError):
        direct = {}
    if (direct.get("dir_info") or {}).get("editable"):
        source = direct.get("url", "").removeprefix("file:///").removeprefix("file://")
        kind += f"，可编辑安装（源码 {Path(source)}）" if source else "，可编辑安装"
    return kind


def check_install() -> list[Item]:
    from .launcher import version

    dist = _distribution()
    items = [
        Item(OK, f"mosscode {version()}（命令 moss 与 mosscode 相同）"),
        Item(OK, f"Python {sys.version.split()[0]}：{sys.executable}"),
    ]
    if dist is None:
        items.append(
            Item(WARN, "mosscode 没有安装，正直接从源码运行", "在仓库中运行 uv tool install --editable . 安装为命令")
        )
    else:
        items.append(Item(OK, "安装方式：" + install_method(dist=dist)))
    return items


def _all_on_path(name: str, path: str | None = None) -> list[Path]:
    found, seen = [], set()
    extensions = os.environ.get("PATHEXT", ".EXE").split(os.pathsep) if os.name == "nt" else [""]
    for folder in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep):
        if not folder:
            continue
        for extension in extensions:
            candidate = Path(folder) / (name + extension.lower())
            key = os.path.normcase(str(candidate))
            if key not in seen and candidate.is_file():
                seen.add(key)
                found.append(candidate)
    return found


def check_path(path: str | None = None, scripts: Path | None = None) -> list[Item]:
    """Which moss / mosscode a new terminal runs, and whether it belongs to this installation."""
    scripts = Path(sys.executable).parent if scripts is None else scripts
    tool = install_method().startswith(("uv tool", "pipx"))
    items = []
    for name in ("moss", "mosscode"):
        found = _all_on_path(name, path)
        if not found:
            items.append(
                Item(WARN, f"PATH 中找不到 {name}", "用 uv tool 安装后运行 uv tool update-shell，再打开新的终端")
            )
            continue
        first = found[0]
        same = os.path.normcase(str(first.parent)) == os.path.normcase(str(scripts)) or tool
        text = f"{name} → {first}" + ("" if same else "（不是当前运行的这一个）")
        others = "；".join(str(item) for item in found[1:])
        if len(found) > 1:
            items.append(Item(WARN, text + f"；PATH 中还有 {others}", "排在前面的生效；卸载不用的那一个"))
        else:
            items.append(Item(OK if same else WARN, text, "" if same else "检查 PATH 的顺序，或卸载旧的安装"))
    return items


def _credential(provider) -> tuple[str, str, str]:
    """Level, what is configured, and how to fix it."""
    if getattr(provider, "follow", None):
        from .follow import SOURCES

        return OK, f"跟随 {SOURCES[provider.follow]} 的设置（已读取）", ""
    if provider.api_key_env:
        if os.environ.get(provider.api_key_env):
            return OK, f"环境变量 {provider.api_key_env} 已设置", ""
        return WARN, f"环境变量 {provider.api_key_env} 未设置", f"设置环境变量 {provider.api_key_env} 后打开新的终端"
    if provider.api_key_keyring:
        from .credentials import KeyringError, keyring_get

        name = provider.api_key_keyring
        try:
            found = keyring_get(name) is not None
        except KeyringError as exc:
            return WARN, f"系统凭据管理器 {name} 无法读取", str(exc)
        if found:
            return OK, f"系统凭据管理器 {name} 已设置", ""
        return WARN, f"系统凭据管理器 {name} 未找到", f"运行 moss key set {name}"
    if provider.api_key:
        return OK, "已设置（配置文件中的 api_key）", ""
    return WARN, "未配置", "在配置中填写 api_key_env 或 api_key_keyring"


def _web_search(config) -> Item:
    if config is None:
        return Item(OK, "网络搜索：未配置（web_search 工具不可用；读取网页不受影响）")
    level, credential, fix = _credential(config)
    return Item(level, f"网络搜索：{config.provider} · 凭据：{credential}", fix)


def _catalog_match(provider) -> str:
    from .catalog import tokens_label

    if provider.catalog is None:
        return "目录中没有该模型"
    window, output = tokens_label(provider.context_window), tokens_label(provider.max_output_tokens)
    return f"目录：{provider.catalog.name}（{window} / {output}）"


def check_config(config: str | None = None, cwd: Path | None = None, home: Path | None = None) -> list[Item]:
    from .catalog import load_catalog
    from .config import ConfigError, find_config, load_settings, project_confirmed, project_notice
    from .setup import OFFICIAL

    cwd = Path.cwd() if cwd is None else cwd
    items = []
    try:
        path = Path(config) if config is not None else find_config(cwd, home)
    except ConfigError as exc:
        items.append(Item(ERROR, str(exc).split("。")[0], "运行 moss setup 创建配置，或用 --config 指定"))
        path = None
    if path is not None:
        try:
            settings = load_settings(path, home)
        except ConfigError as exc:
            items.append(Item(ERROR, f"配置 {path}：{exc}", "对照 config.example.yaml 修改"))
        else:
            items.append(Item(OK, f"配置 {path}"))
            if note := project_notice(settings):
                items.append(Item(WARN, note, "需要时把它们写进用户配置"))
            if not project_confirmed(settings, home):
                fix = "启动 moss 并在确认面板中选“信任并使用”，或加 --trust-project-config"
                items.append(Item(WARN, "这份项目配置尚未确认（第一次使用，或内容有变化），-p 不会使用它", fix))
            catalog = load_catalog()
            where = catalog.path or "随程序发布"
            items.append(
                Item(WARN, catalog.problem, "")
                if catalog.problem
                else Item(OK, f"模型目录 {len(catalog.models)} 个模型，数据日期 {catalog.fetched}（{where}）")
            )
            for provider in settings.providers:
                level, credential, fix = _credential(provider)
                host = (
                    urlsplit(provider.base_url).hostname
                    if provider.base_url
                    else f"{urlsplit(OFFICIAL[provider.protocol]).hostname}（官方地址）"
                )
                default = "（默认）" if provider.name == settings.default_provider else ""
                items.append(
                    Item(
                        level,
                        f"Provider {provider.name}{default}：{provider.protocol} · {provider.model}"
                        f" · {host} · 凭据：{credential} · {_catalog_match(provider)}",
                        fix,
                    )
                )
                if note := provider.window_note():
                    items.append(Item(WARN, note, ""))
            items.append(_web_search(settings.web_search))
    legacy = cwd / "config.yaml"
    if config is None and legacy.is_file():
        items.append(
            Item(
                WARN,
                f"{legacy} 不会被读取（只查找 .mosscode/config.yaml 和 ~/.mosscode/config.yaml）",
                f"需要用它时加 --config {legacy}",
            )
        )
    return items


def check_tools(which=shutil.which, windows: bool | None = None) -> list[Item]:
    windows = os.name == "nt" if windows is None else windows
    items = []
    rg = which("rg")
    items.append(
        Item(OK, f"ripgrep：{rg}")
        if rg
        else Item(
            WARN,
            "找不到 ripgrep（rg）：搜索改用较慢的内置实现",
            "安装 ripgrep，例如 winget install BurntSushi.ripgrep.MSVC",
        )
    )
    git = which("git")
    items.append(
        Item(OK, f"git：{git}")
        if git
        else Item(WARN, "找不到 git：工作树、团队合并和差异不可用", "安装 Git for Windows")
    )
    if windows:
        from .tools.shell import git_bash

        bash = git_bash()
        items.append(
            Item(OK, f"Git Bash：{bash}")
            if bash
            else Item(
                WARN,
                "找不到 Git Bash：命令改用 PowerShell 执行",
                "安装 Git for Windows，或在配置中设 shell: powershell",
            )
        )
        wt = which("wt")
        items.append(
            Item(OK, f"Windows Terminal：{wt}")
            if wt
            else Item(WARN, "找不到 wt.exe：团队成员无法在分屏窗格中运行，改为进程内运行", "安装 Windows Terminal")
        )
    return items


def check_home(home: Path | None = None) -> list[Item]:
    from .paths import mosscode_home

    folder = mosscode_home(home)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return [Item(ERROR, f"{folder} 不可写：{exc.strerror or exc}", "检查这个目录的权限")]
    return [Item(OK, f"{folder} 可写")]


def check_project(cwd: Path | None = None, config: str | None = None, home: Path | None = None) -> list[Item]:
    """The project's own files under .mosscode/; nothing when it has none."""
    cwd = Path.cwd() if cwd is None else cwd
    from .paths import mosscode_home

    folder = cwd / ".mosscode"
    if not folder.is_dir() or os.path.normcase(os.path.abspath(folder)) == os.path.normcase(
        os.path.abspath(mosscode_home(home))
    ):  # In the home folder it is the user folder, not a project's.
        return []
    items = []
    project_config = folder / "config.yaml"
    if config is not None and project_config.is_file():  # Without --config it is the one checked above.
        from .config import ConfigError, load_settings

        try:
            load_settings(project_config, home)
        except ConfigError as exc:
            items.append(Item(ERROR, f"{project_config}：{exc}", "对照 config.example.yaml 修改"))
    from .permissions import PermissionConfigError, read_policy

    for path in (folder / "permissions.yaml", folder / "permissions.local.yaml"):
        try:
            read_policy(path)
        except PermissionConfigError as exc:
            items.append(Item(ERROR, str(exc), "修改后重新启动"))
    from .mcp_config import MCPConfigError, load_mcp_settings

    try:
        mcp = load_mcp_settings(cwd, user_path=None if home is None else home / ".mosscode" / "mcp.yaml", plugins=())
    except MCPConfigError as exc:
        items.append(Item(ERROR, f"MCP 配置：{exc}", "修改 .mosscode/mcp.yaml"))
    else:
        items.extend(Item(WARN, notice) for notice in mcp.notices)
    from .hooks.config import hook_paths, read_file

    for source, path, _ in hook_paths(cwd, home):
        if source != "user":
            items.extend(Item(WARN, issue) for issue in read_file(source, path)[1])
    if not items:
        items.append(Item(OK, f"项目配置 {folder} 可以解析"))
    return items


def check_plugins(home: Path | None = None, cwd: Path | None = None, which=shutil.which) -> list[Item]:
    """Installed plugins and the project's: git when one needs it, each one on disk, and the problems of its
    components."""
    from .plugins import components, installed, load_records, project_plugins, read_plugin

    cwd = Path.cwd() if cwd is None else cwd
    records, problems = load_records(home)
    project = project_plugins(cwd, home)
    if not records and not problems and not project.records and not project.problems:
        return []
    items = [Item(WARN, problem, "修改 ~/.mosscode/plugins.yaml") for problem in problems]
    if any(record.kind == "git" for record in (*records, *project.records)) and which("git") is None:
        items.append(Item(ERROR, "有 git 来源的插件，但找不到 git：无法安装和更新", "安装 git 并加入 PATH"))
    enabled = {plugin.name: plugin for plugin in installed(home).plugins}
    for record in records:
        if not record.enabled:
            items.append(Item(OK, f"插件 {record.name}：已停用"))
            continue
        plugin = enabled.get(record.name)
        if plugin is None:
            if not record.root(home).is_dir():
                items.append(
                    Item(
                        ERROR,
                        f"插件 {record.name} 不在磁盘上（{record.root(home)}）",
                        f"moss plugin update {record.name}",
                    )
                )
            else:
                try:
                    read_plugin(record.root(home), record.name, home, record)
                except ValueError as exc:
                    items.append(Item(ERROR, f"插件 {record.name}：{exc}", f"moss plugin update {record.name}"))
            continue
        parts = components(replace(plugin, create_data=False), cwd)
        if parts.problems:
            items.extend(Item(WARN, f"插件 {record.name}：{problem}") for problem in parts.problems)
        else:
            items.append(Item(OK, f"插件 {record.name}：{parts.count()}"))
    return items + _project_plugin_items(project, home, cwd)


def _project_plugin_items(project, home: Path | None, cwd: Path) -> list[Item]:
    from .plugins import components

    items = [Item(WARN, problem, "修改项目的 .mosscode/plugins.yaml") for problem in project.problems]
    items += [Item(WARN, f"项目插件 {record.name} 尚未安装", "moss plugin install") for record in project.missing]
    if project.plugins and not project.trusted(home):
        items.append(Item(WARN, "项目插件尚未确认，不会加载", "启动 moss 并在确认面板中选“信任并启用”"))
    for plugin in project.plugins:
        parts = components(replace(plugin, create_data=False), cwd)
        if parts.problems:
            items.extend(Item(WARN, f"项目插件 {plugin.name}：{problem}") for problem in parts.problems)
        else:
            items.append(Item(OK, f"项目插件 {plugin.name}：{parts.count()}"))
    return items


def check_sandbox(config: str | None = None, *, probe=None) -> list[Item]:
    """probe: the Sandbox to detect with; tests pass one whose detection is replaced."""
    from .config import ConfigError, SandboxConfig, find_config, load_settings
    from .sandbox import BACKEND_NAMES, PLATFORM_BACKENDS, Sandbox

    try:
        settings = load_settings(Path(config) if config is not None else find_config())
        sandbox = settings.sandbox
    except ConfigError:
        sandbox = SandboxConfig()  # check_config reports the problem.
    on = sandbox.mode == "on"
    items = [
        Item(
            OK, "沙箱：" + ("开启（sandbox.mode: on）" if on else "关闭（sandbox.mode: off，可用 --sandbox 临时打开）")
        )
    ]
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    choices = "、".join(BACKEND_NAMES[name] for name in PLATFORM_BACKENDS.get(system, ())) or "无"
    # Detection runs each backend's self-test whether or not the sandbox is on: doctor says what turning it on gives.
    probe = probe or Sandbox(sandbox, enabled=True)
    status = probe.status()
    name = BACKEND_NAMES.get(status.backend, status.backend) or "沙箱后端"
    if status.state == "unavailable":
        if on:
            items.append(
                Item(WARN, f"{name}不可用，模型的命令都会被拒绝：{status.reason}", "修复后重试，或用 --no-sandbox 运行")
            )
        else:
            items.append(Item(OK, f"这个系统可用的后端：{choices}；现在不可用：{status.reason}"))
        return items
    level = "写入、读取和网络都由系统强制" if status.state == "full" else "只限制写入，命令仍需审批"
    where = f"（{probe.executable}）" if probe.executable else ""
    items.append(Item(OK, f"沙箱后端：{name}{where}，{level}，自检通过（这个系统可用：{choices}）"))
    for note in (probe.fallback_note, probe.network_note, probe.open_note(Path.cwd())):
        if note:
            items.append(Item(WARN if on else OK, note))
    if status.backend == "token":
        from .sandbox import windows_token

        if count := len(windows_token.load()["roots"]):
            items.append(
                Item(
                    OK,
                    f"受限令牌在 {count} 个项目上设置了访问控制，见 {windows_token.record_path()}；moss sandbox cleanup 可移除",
                )
            )
    return items


def run(config: str | None = None) -> list[Item]:
    return [
        *check_install(),
        *check_path(),
        *check_config(config),
        *check_home(),
        *check_tools(),
        *check_sandbox(config),
        *check_project(config=config),
        *check_plugins(),
    ]


def report(items: list[Item]) -> str:
    lines = []
    for item in items:
        lines.append(f"{item.level} {item.text}")
        if item.fix and item.level != OK:
            lines.append(f"    → {item.fix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(
        prog="moss doctor", description="检查安装、配置和依赖工具；不启动会话，不发送请求。"
    )
    command.add_argument("--config", help="检查这个配置文件，而不是按默认顺序查找")
    args = command.parse_args(argv)
    items = run(args.config)
    text = report(items)
    try:
        print(text)
    except UnicodeEncodeError:  # A console that cannot show ✓ and ✗.
        print(text.encode(sys.stdout.encoding or "ascii", "replace").decode(sys.stdout.encoding or "ascii"))
    return 1 if any(item.level == ERROR for item in items) else 0
