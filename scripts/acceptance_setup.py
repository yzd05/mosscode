"""Create a throwaway project for manual acceptance (docs/acceptance.md). No API calls, no credentials.

Layout:
    <target>/AGENTS.md            parent-level instructions
    <target>/outside.txt          a file outside the project, for the out-of-project read case
    <target>/project/             the project to start MossCode in (its own git repository)
    <target>/mcp-demo.log         calls received by the fixture MCP servers
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKER = ".acceptance-sandbox"
PYTHON = Path(sys.executable)
FIXTURE = REPO / "tests" / "fixtures" / "mcp_stdio_server.py"


def write(path: Path, text: str, *, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text.replace("\n", newline))


def git(project: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "user.name=acceptance",
                    "-c", "user.email=acceptance@example.invalid", *args],
                   cwd=project, check=True, capture_output=True)


def has_files(directory: Path) -> bool:
    return any(files for _, _, files in os.walk(directory))


def clear(directory: Path) -> None:
    """Delete everything below directory except the marker, which goes last so a failed run can be retried.

    Windows cannot remove a directory that is some process's working directory, e.g. a
    terminal still inside project/. Such directories are emptied and kept; build() reuses them.
    """
    for child in directory.iterdir():
        if child.name == MARKER:
            continue
        if child.is_dir() and not child.is_symlink():
            clear(child)
            try:
                child.rmdir()
            except OSError:
                pass  # In use as a working directory: keep it, now empty.
        else:
            # Git stores objects read-only; Windows refuses to delete them until the flag is cleared.
            os.chmod(child, stat.S_IWRITE)
            child.unlink()


def build(target: Path) -> Path:
    project = target / "project"
    log = target / "mcp-demo.log"
    write(target / MARKER, "Created by scripts/acceptance_setup.py; safe to delete.\n")
    write(target / "AGENTS.md", "# 上级目录指令\n\n验收暗号第一段：青松。\n")
    write(target / "outside.txt", "这是项目目录之外的文件。\n")

    write(project / "AGENTS.md", "# 项目指令\n\n验收暗号第二段：翠竹。用户问验收暗号时，按目录层级从上到下给出所有段，用顿号连接。\n")
    write(project / "CLAUDE.md", "验收暗号另有一段：错误。（同级已有 AGENTS.md，此文件不应被加载。）\n")
    write(project / ".gitignore", "build/\n*.log\n!keep.log\n")
    write(project / "build" / "output.txt", "NEEDLE 位于被忽略的 build 目录\n")
    write(project / "debug.log", "NEEDLE 位于被忽略的 debug.log\n")
    write(project / "keep.log", "NEEDLE 位于用 ! 保留的 keep.log\n")
    write(project / "src" / "app.py", 'from util import old_name\n\n\n# NEEDLE 位于源码\ndef main():\n    return old_name(2)\n')
    write(project / "src" / "util.py",
          "def old_name(x):\n    return x * 2\n\n\nALIAS = old_name\nprint(old_name(1))\n")
    write(project / "crlf.txt", "第一行\n第二行\n第三行\n", newline="\r\n")
    write(project / "notes.txt", "笔记\n")
    write(project / "long.txt", "".join(f"第 {n} 行\n" for n in range(1, 3001)))

    server = {"type": "stdio", "command": str(PYTHON), "args": [str(FIXTURE), str(log)]}
    write(project / ".mosscode" / "mcp.yaml", "\n".join([
        "import_mcp_json: true",
        "mcp_servers:",
        "  demo:",
        "    type: stdio",
        f"    command: {json.dumps(str(PYTHON))}",
        f"    args: [{json.dumps(str(FIXTURE))}, {json.dumps(str(log))}]",
        "    timeout: 3",
        "    trust_read_only_hint: false",
        "  slowstart:",
        "    type: stdio",
        f"    command: {json.dumps(str(PYTHON))}",
        f"    args: [{json.dumps(str(FIXTURE))}, {json.dumps(str(log))}, hang]",
        "    startup_timeout: 8",
        "",
    ]))
    write(project / ".mcp.json", json.dumps({"mcpServers": {
        "fromjson": {**{k: v for k, v in server.items() if k != "type"},
                     "env": {"MOSS_MCP_FIXTURE": "${MOSS_ACCEPT_TAG:-json-default}"}},
        "legacy": {"type": "sse", "url": "http://127.0.0.1:9/sse"},
    }}, ensure_ascii=False, indent=2) + "\n")

    write(project / ".mosscode" / "permissions.yaml", "deny:\n  - mcp__demo__fail\n  - Bash(git push:*)\n")

    # Nothing listens on port 9: every request fails with a connection error, so retries run offline.
    write(target / "offline-config.yaml", "\n".join([
        "default_provider: unreachable",
        "providers:",
        "  - name: unreachable",
        "    protocol: anthropic-messages",
        "    base_url: http://127.0.0.1:9",
        "    model: offline-test",
        "    api_key: not-a-real-key",
        "    max_retries: 2",
        "",
    ]))

    if shutil.which("git"):
        git(project, "init", "-q")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "acceptance fixture")
        write(project / "notes.txt", "笔记\n第二条笔记\n")
        write(project / "untracked.txt", "未跟踪的文件\n")
    else:
        print("未找到 git：跳过仓库初始化，Git 快照用例会显示“不是仓库”。")
    return project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", nargs="?", default=str(REPO / ".test-artifacts" / "acceptance"))
    parser.add_argument("--force", action="store_true", help="重建已有的验收目录（只删除本脚本创建的目录）")
    target = Path(parser.parse_args().target).resolve()
    if target.exists():
        if not parser.parse_args().force:
            print(f"{target} 已存在；加 --force 重建。")
            return 1
        # Only empty directories left (an older run stopped after deleting the marker): nothing to lose.
        if not (target / MARKER).exists() and has_files(target):
            print(f"{target} 不是本脚本创建的目录，拒绝删除。")
            return 1
        try:
            clear(target)
        except OSError as exc:
            print(f"无法删除 {exc.filename}：文件正被其他程序使用。请先退出在验收项目中运行的 MossCode，再重试。")
            return 1
    project = build(target)
    print(f"验收项目：{project}")
    print(f"在该目录启动：{PYTHON} -m mosscode --config {REPO / 'config.yaml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
