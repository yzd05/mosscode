"""`moss plugin`: install, list, update, remove, enable and disable user-level plugins.

Git sources are cloned with the git command; nothing else reaches the network, and a session never does.
Like `moss key`, this module imports neither the interface nor a provider SDK.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from .plugins import (
    MARKETPLACE,
    Plugin,
    PluginError,
    PluginRecord,
    check_name,
    components,
    data_dir,
    is_plugin,
    load_records,
    manifest_name,
    plugins_home,
    read_plugin,
    repo_dir,
    save_records,
    source_kind,
)

GITHUB = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
GIT_TIMEOUT = 600


class Listed(Exception):
    """A marketplace was given without #name: its plugins were listed instead."""


# Git --------------------------------------------------------------------------------------


def git(*args: str, cwd: Path | None = None) -> str:
    if shutil.which("git") is None:
        raise PluginError("需要 git：安装 git 并确认它在 PATH 中")
    # Never wait for a password prompt that nobody sees.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise PluginError(f"git {args[0]} 超过 {GIT_TIMEOUT} 秒未完成") from None
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
        # The first fatal/error line says what went wrong; git's closing lines are generic advice.
        reason = next((line for line in lines if line.startswith(("fatal:", "error:"))), lines[-1] if lines else "")
        raise PluginError(f"git {args[0]} 失败：{reason or f'退出码 {result.returncode}'}")
    return result.stdout.strip()


def clone(url: str, ref: str | None, home: Path | None) -> Path:
    """Into a fresh directory under plugins/.tmp; a commit needs the full history, anything else is shallow."""
    target = plugins_home(home) / ".tmp" / secrets.token_hex(4)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if ref and COMMIT.fullmatch(ref):
            git("clone", "--quiet", url, str(target))
            git("checkout", "--quiet", "--detach", ref, cwd=target)
        elif ref:
            git("clone", "--quiet", "--depth", "1", "--branch", ref, url, str(target))
        else:
            git("clone", "--quiet", "--depth", "1", url, str(target))
    except PluginError:
        remove_tree(target)
        raise
    return target


def head(directory: Path) -> str:
    try:
        return git("rev-parse", "HEAD", cwd=directory)
    except PluginError:
        return ""


def is_branch(url: str, ref: str) -> bool:
    return bool(git("ls-remote", "--heads", url, ref))


def remove_tree(path: Path) -> None:
    """Git marks its objects read-only, which stops a plain rmtree on Windows."""

    def retry(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, onexc=retry)


# Sources ----------------------------------------------------------------------------------


def normalize(text: str) -> str:
    """An existing directory, a git address, or owner/repo on GitHub."""
    text = text.strip()
    if source_kind(text) == "git":
        return text
    directory = Path(text).expanduser()
    if directory.is_dir():
        return directory.resolve().as_posix()
    if GITHUB.fullmatch(text):
        return f"https://github.com/{text.removesuffix('.git')}.git"
    raise PluginError(f"{text} 不是本机目录，也不是 git 地址（https://、ssh://、git@、file://）或 owner/repo")


def default_name(source: str) -> str:
    tail = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git").lower()


def _inside(base: Path, relative: str) -> str:
    path = relative[2:] if relative.startswith("./") else relative
    if Path(path).is_absolute() or not (base / path).resolve().is_relative_to(base.resolve()):
        raise PluginError(f"marketplace 中的路径 {relative} 不在仓库内")
    return Path(path).as_posix().strip("/")


@dataclass(frozen=True)
class Entry:
    """Where a marketplace entry's plugin is: a directory of the same repository, or another git source."""

    path: str | None = None
    source: str | None = None
    ref: str | None = None


def marketplace(base: Path, wanted: str | None) -> Entry:
    """Raises Listed after printing the plugins when wanted is None."""
    path = base / MARKETPLACE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        plugins = data["plugins"]
        if not isinstance(plugins, list):
            raise ValueError
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise PluginError(f"{MARKETPLACE.as_posix()} 不是有效的 marketplace（需要 plugins 列表）") from None
    entries = [item for item in plugins if isinstance(item, dict) and isinstance(item.get("name"), str)]
    if wanted is None:
        print(f"这是一个 marketplace，其中有 {len(entries)} 个插件：")
        width = max((len(item["name"]) for item in entries), default=0)
        for item in entries:
            description = item.get("description") if isinstance(item.get("description"), str) else ""
            print(f"  {item['name']:<{width}}  {' '.join(description.split())}")
        print("用 moss plugin install <来源>#<名称> 安装其中一个。")
        raise Listed
    entry = next((item for item in entries if item["name"] == wanted), None)
    if entry is None:
        raise PluginError(f"marketplace 中没有名为 {wanted} 的插件")
    source = entry.get("source")
    if isinstance(source, str):
        root = data.get("metadata", {}).get("pluginRoot") if isinstance(data.get("metadata"), dict) else None
        if isinstance(root, str) and not source.startswith("./"):
            source = f"{root.rstrip('/')}/{source}"
        return Entry(path=_inside(base, source) or None)
    if isinstance(source, dict):
        kind = source.get("source")
        ref = source.get("sha") or source.get("ref")
        ref = ref if isinstance(ref, str) and ref else None
        if kind == "github" and isinstance(source.get("repo"), str) and GITHUB.fullmatch(source["repo"]):
            return Entry(source=f"https://github.com/{source['repo'].removesuffix('.git')}.git", ref=ref)
        if kind in ("url", "git-subdir") and isinstance(source.get("url"), str):
            if source_kind(source["url"]) != "git":
                raise PluginError(f"插件 {wanted} 的地址 {source['url']} 不是 git 地址")
            sub = source.get("path") if kind == "git-subdir" else None
            return Entry(path=sub if isinstance(sub, str) else None, source=source["url"], ref=ref)
        raise PluginError(f"插件 {wanted} 的来源类型 {kind!s} 不受支持（支持仓库内的相对路径、github、url）")
    raise PluginError(f"插件 {wanted} 没有可用的 source")


@dataclass
class Fetched:
    source: str  # Recorded as it is.
    ref: str | None
    path: str | None
    base: Path  # The clone (temporary) or the local directory.
    temporary: bool
    name_hint: str

    @property
    def root(self) -> Path:
        return self.base / self.path if self.path else self.base


def fetch(text: str, ref: str | None, home: Path | None) -> Fetched:
    """Resolve a source given on the command line to a plugin directory on disk."""
    raw, _, wanted = text.partition("#")
    source = normalize(raw)
    wanted = wanted or None
    if source_kind(source) == "local" and ref:
        raise PluginError("本机目录原地使用，不能指定 --ref；要按 git 仓库克隆，写成 file:// 地址")
    fetched = _get(source, ref, home, wanted or default_name(source))
    try:
        if is_plugin(fetched.base) and not (fetched.base / MARKETPLACE).is_file():
            if wanted:
                raise PluginError(f"{raw} 是一个插件，不是 marketplace，去掉 #{wanted}")
            return fetched
        if not (fetched.base / MARKETPLACE).is_file():
            raise PluginError(f"{raw} 不是插件（没有 .claude-plugin/plugin.json 和约定的组件目录），也不是 marketplace")
        if wanted is None and is_plugin(fetched.base):
            return fetched  # A plugin that also lists itself.
        entry = marketplace(fetched.base, wanted)
        if entry.source is None:
            fetched.path, fetched.name_hint = entry.path, wanted
            return fetched
    except BaseException:
        if fetched.temporary:
            remove_tree(fetched.base)
        raise
    if fetched.temporary:
        remove_tree(fetched.base)
    inner = _get(entry.source, ref or entry.ref, home, wanted)
    inner.path = entry.path
    return inner


def _get(source: str, ref: str | None, home: Path | None, name_hint: str) -> Fetched:
    if source_kind(source) == "git":
        return Fetched(source, ref, None, clone(source, ref, home), True, name_hint)
    return Fetched(source, None, None, Path(source), False, name_hint)


# Showing and asking -----------------------------------------------------------------------


def describe(plugin: Plugin, record: PluginRecord, commit: str = "") -> list[str]:
    parts = components(plugin, Path.cwd())
    where = (
        record.source
        + (f"（ref {record.ref}）" if record.ref else "")
        + (f"，路径 {record.path}" if record.path else "")
    )
    rows = [
        ("来源", where),
        *([("提交", commit[:7])] if commit else []),
        *([("描述", plugin.description)] if plugin.description else []),
        ("Skill", "、".join(parts.skills) or "无"),
        ("命令", "、".join(parts.commands) or "无"),
        ("子 Agent", "、".join(parts.agents) or "无"),
    ]
    lines = [f"插件 {plugin.name}" + (f" {plugin.version}" if plugin.version else "")]
    lines += [f"  {label}{' ' * (10 - _width(label))}{value}" for label, value in rows]
    lines += _block("MCP 服务器（以你的权限运行）", parts.mcp)
    lines += _block("Hook（以你的权限运行）", parts.hooks)
    lines += _block("未支持的组件（不加载）", plugin.unsupported)
    problems = parts.problems
    if plugin.ref_root is not None and plugin.ref_root != plugin.root:  # A preview: name files where they will be.
        problems = tuple(problem.replace(str(plugin.root), str(plugin.ref_root)) for problem in problems)
    lines += _block("问题", problems)
    return lines


def _width(text: str) -> int:
    """Terminal columns: CJK characters take two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _block(title: str, items) -> list[str]:
    return [f"  {title}：", *(f"    {item}" for item in items)] if items else []


def confirm(question: str, yes: bool, stdin=None) -> bool:
    from .launcher import is_terminal

    if yes:
        return True
    stdin = sys.stdin if stdin is None else stdin
    if not is_terminal(stdin):
        print("stdin 不是终端，无法确认；看过上面的内容后加 -y 重新运行。", file=sys.stderr)
        return False
    try:
        print(f"{question}[y/N] ", end="", flush=True)
        return stdin.readline().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _preview(fetched: Fetched, record: PluginRecord, home: Path | None) -> Plugin:
    """Read from where the files are now, shown with the paths they will have; writes nothing."""
    plugin = read_plugin(fetched.root, record.name, home, record)
    return replace(plugin, ref_root=record.root(home), create_data=False)


def _place(fetched: Fetched, name: str, home: Path | None) -> None:
    """Move a clone into repos/<name>, replacing what was there."""
    target = repo_dir(name, home)
    if target.exists():
        remove_tree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(fetched.base), str(target))


# Subcommands ------------------------------------------------------------------------------


def install(args, home: Path | None, stdin=None) -> int:
    records, _ = load_records(home)
    fetched = fetch(args.source, args.ref, home)
    try:
        if not fetched.root.is_dir() or not is_plugin(fetched.root):
            raise PluginError(f"{fetched.root} 不是插件目录")
        name = args.name or manifest_name(fetched.root) or fetched.name_hint
        if problem := check_name(name):
            raise PluginError(f"{problem}；当前为 {name!r}，用 --name 指定一个")
        if any(record.name == name for record in records):
            raise PluginError(f"已安装名为 {name} 的插件；先 moss plugin remove {name}，或用 --name 换一个名称")
        record = PluginRecord(name, fetched.source, fetched.ref, fetched.path)
        commit = head(fetched.base) if fetched.temporary else ""
        print("\n".join(describe(_preview(fetched, record, home), record, commit)))
        if not confirm("安装？", args.yes, stdin):
            print("已取消，没有安装。")
            return 1
        if fetched.temporary:
            _place(fetched, name, home)
            fetched.temporary = False
        save_records([*records, record], home)
    finally:
        if fetched.temporary:
            remove_tree(fetched.base)
    print(f"已安装插件 {name}，下次启动 moss 时生效。")
    return 0


def _find(name: str, home: Path | None) -> tuple[list[PluginRecord], int]:
    records, _ = load_records(home)
    index = next((i for i, record in enumerate(records) if record.name == name), None)
    if index is None:
        raise PluginError(f"没有安装名为 {name} 的插件（moss plugin list 查看）")
    return records, index


def _diff(old: Plugin | None, new: Plugin) -> list[str]:
    before = components(old, Path.cwd()) if old is not None else None
    after = components(new, Path.cwd())
    lines = []
    for label, field in (
        ("Skill", "skills"),
        ("命令", "commands"),
        ("子 Agent", "agents"),
        ("MCP 服务器", "mcp"),
        ("Hook", "hooks"),
    ):
        was = set(getattr(before, field)) if before else set()
        now = getattr(after, field)
        lines += [f"  + {label} {item}" for item in now if item not in was]
        lines += [f"  - {label} {item}" for item in sorted(was - set(now))]
    return lines or ["  组件没有变化"]


def update_one(record: PluginRecord, yes: bool, home: Path | None, stdin=None) -> int:
    if record.kind == "local":
        print(f"插件 {record.name} 是本机目录，原地使用，无需更新。")
        return 0
    repo = repo_dir(record.name, home)
    present = repo.is_dir()
    if present and record.ref and not is_branch(record.source, record.ref):
        print(f"插件 {record.name} 固定在 {record.ref}，不更新（要跟随分支，把 plugins.yaml 中的 ref 改为分支名）。")
        return 0
    fetched = _get(record.source, record.ref, home, record.name)
    fetched.path = record.path
    try:
        old_commit, new_commit = head(repo) if present else "", head(fetched.base)
        if present and old_commit == new_commit:
            print(f"插件 {record.name} 已是最新（{new_commit[:7]}）。")
            return 0
        if not fetched.root.is_dir() or not is_plugin(fetched.root):
            raise PluginError(f"新版本中 {record.path or '仓库根目录'} 不是插件目录")
        old = read_plugin(record.root(home), record.name, home, record) if record.root(home).is_dir() else None
        new = _preview(fetched, record, home)
        print(f"插件 {record.name}：{old_commit[:7] or '（不在磁盘上）'} → {new_commit[:7]}")
        print("\n".join(_diff(old and replace(old, create_data=False), new)))
        if not confirm("更新？", yes, stdin):
            print("已取消，没有更新。")
            return 1
        _place(fetched, record.name, home)
        fetched.temporary = False
    finally:
        if fetched.temporary:
            remove_tree(fetched.base)
    print(f"已更新插件 {record.name}，下次启动 moss 时生效。")
    return 0


def update(args, home: Path | None, stdin=None) -> int:
    records, _ = load_records(home)
    if args.name:
        records = [records[_find(args.name, home)[1]]]
    if not records:
        print("没有安装插件。")
        return 0
    status = 0
    for record in records:
        try:
            status = max(status, update_one(record, args.yes, home, stdin))
        except PluginError as exc:
            print(f"moss plugin：插件 {record.name}：{exc}。", file=sys.stderr)
            status = 1
    return status


def remove(args, home: Path | None) -> int:
    records, index = _find(args.name, home)
    record = records.pop(index)
    save_records(records, home)
    if record.kind == "git":
        remove_tree(repo_dir(record.name, home))
        remove_tree(data_dir(record.name, home))
        print(f"已删除插件 {record.name}（记录、克隆目录和数据目录）。")
    else:
        print(f"已删除插件 {record.name} 的记录；本机目录 {record.source} 保留。")
    return 0


def toggle(args, home: Path | None, enabled: bool) -> int:
    records, index = _find(args.name, home)
    records[index] = replace(records[index], enabled=enabled)
    save_records(records, home)
    print(f"已{'启用' if enabled else '停用'}插件 {args.name}，下次启动 moss 时生效。")
    return 0


def report(home: Path | None = None, name: str = "") -> str:
    """moss plugin list, and /plugins in a session."""
    records, problems = load_records(home)
    if name:
        record = next((item for item in records if item.name == name), None)
        if record is None:
            return f"没有安装名为 {name} 的插件。"
        if not record.root(home).is_dir():
            return f"插件 {name} 不在磁盘上（{record.root(home)}），运行 moss plugin update {name}。"
        try:
            plugin = replace(read_plugin(record.root(home), name, home, record), create_data=False)
        except PluginError as exc:
            return f"插件 {name}：{exc}。"
        lines = describe(plugin, record, head(record.base(home)) if record.kind == "git" else "")
        return "\n".join([*lines, f"  状态{' ' * 6}{'启用' if record.enabled else '已停用'}"])
    if not records:
        return "\n".join([*problems, "没有安装插件。用 moss plugin install <来源> 安装。"])
    lines = [f"插件（{len(records)}）"]
    for record in records:
        root = record.root(home)
        state = "启用" if record.enabled else "已停用"
        commit = head(record.base(home))[:7] if record.kind == "git" and root.is_dir() else ""
        if not root.is_dir():
            lines.append(
                f"{record.name}  {state}  {record.source}  ! 不在磁盘上，运行 moss plugin update {record.name}"
            )
            continue
        try:
            plugin = replace(read_plugin(root, record.name, home, record), create_data=False)
        except PluginError as exc:
            lines.append(f"{record.name}  {state}  {record.source}  ! {exc}")
            continue
        parts = components(plugin, Path.cwd())
        head_text = " ".join(filter(None, (record.name, plugin.version, state, record.source, commit)))
        lines.append(f"{head_text}  {parts.count()}")
        lines += [f"  ! {problem}" for problem in parts.problems]
    lines += [f"! {problem}" for problem in problems]
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="moss plugin",
        description="安装和管理用户级插件（~/.mosscode/plugins.yaml）。插件可以包含 Skill、命令、子 Agent、"
        "MCP 服务器和 Hook；兼容 Claude Code 的插件目录。变化在下一次启动 moss 时生效。",
    )
    actions = command.add_subparsers(dest="action", required=True, metavar="install | list | update | remove | …")
    add = actions.add_parser("install", help="从 git 仓库、owner/repo 或本机目录安装", description="安装一个插件")
    add.add_argument("source", help="git 地址、owner/repo（GitHub）或本机目录；marketplace 写成 <来源>#<插件名>")
    add.add_argument("--ref", help="分支、标签或提交；标签和提交不随 update 移动")
    add.add_argument("--name", help="插件名（默认取 plugin.json 的 name 或仓库名）")
    add.add_argument("-y", "--yes", action="store_true", help="不询问，直接安装")
    actions.add_parser("list", help="列出已安装的插件", description="列出已安装的插件")
    up = actions.add_parser("update", help="更新 git 来源的插件", description="更新插件；标签和提交固定的不移动")
    up.add_argument("name", nargs="?", help="插件名；省略时更新全部")
    up.add_argument("-y", "--yes", action="store_true", help="不询问，直接更新")
    for action, text in (
        ("remove", "删除插件（记录、克隆目录和数据目录；本机目录来源只删除记录）"),
        ("enable", "启用插件"),
        ("disable", "停用插件（保留文件，不加载）"),
    ):
        actions.add_parser(action, help=text, description=text).add_argument("name", help="插件名")
    return command


def main(argv: list[str] | None = None, *, home: Path | None = None, stdin=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "install":
            return install(args, home, stdin)
        if args.action == "list":
            print(report(home))
            return 0
        if args.action == "update":
            return update(args, home, stdin)
        if args.action == "remove":
            return remove(args, home)
        return toggle(args, home, args.action == "enable")
    except Listed:
        return 1
    except PluginError as exc:
        print(f"moss plugin：{exc}。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
