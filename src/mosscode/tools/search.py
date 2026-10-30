import json
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field

from .base import Params, Tool, ToolFailure
from .process import run_process


SKIP_DIRS = (".git", ".venv", "venv", "node_modules", "__pycache__")
WORKTREES = (".mosscode", "worktrees")  # Ch14 S1: skipped unless the search starts inside one.
GLOB_LIMIT = 200


class GlobParams(Params):
    pattern: str = Field(min_length=1)
    path: str = "."


class GrepParams(Params):
    pattern: str
    path: str = "."
    glob: str | None = None
    output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches"
    ignore_case: bool = False
    context: int = Field(default=0, ge=0, le=10)
    head_limit: int = Field(default=200, ge=1, le=2000)


def rg_command():
    executable = shutil.which("rg")
    if executable is None:
        raise ToolFailure("search_unavailable", "搜索工具需要 ripgrep（rg），请安装并加入 PATH。")
    # .gitignore applies even outside a git repository, matching the fallback.
    return [executable, "--hidden", "--no-require-git"]


def fallback_command(mode, arguments):
    return [sys.executable, "-m", "mosscode.tools.search_worker", mode, json.dumps(arguments, ensure_ascii=False)]


def stderr_hint(stderr: str) -> str:
    text = stderr.strip()[:500]
    return f"\n{text}" if text else ""


def inside_worktrees(path: Path) -> bool:
    parts = path.parts
    return any(parts[i:i + 2] == WORKTREES for i in range(len(parts) - 1))


def skip_globs(path: Path | None = None):
    # Later rg globs override earlier ones, so exclusions follow user filters.
    globs = [arg for name in SKIP_DIRS for arg in ("-g", f"!**/{name}/**")]
    if path is None or not inside_worktrees(path):
        globs.extend(("-g", "!**/.mosscode/worktrees/**"))
    return globs


def newest_first(paths):
    def mtime(path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0
    return sorted(paths, key=lambda path: (-mtime(path), str(path)))


class SearchTool(Tool):
    read_only = True

    def base(self, path: Path) -> str | None:
        # The fallback reads .gitignore files from the project root down to `path`.
        return str(self.root) if path.is_relative_to(self.root) else None


class Glob(SearchTool):
    name = "glob"
    description = (f"按 glob 模式查找文件，例如 **/*.py，支持花括号多选如 *.{{py,md}}、{{src,docs}}/**/*，可指定搜索目录 path。遵守 .gitignore，并跳过 .git、.venv、node_modules "
                   f"和缓存目录（以及 .mosscode/worktrees，除非 path 指向其中）；结果按修改时间从新到旧排列，最多 {GLOB_LIMIT} 项。项目目录之外的路径需要用户确认。")
    prompt_summary = "按文件名或路径模式定位文件"
    prompt_guidelines = (
        "按文件名或路径找文件时优先 glob；pattern 是路径模式，不是内容正则。根据已知目录缩小范围，结果截断时细化模式。",
    )
    params_model = GlobParams

    async def execute(self, params: GlobParams):
        path = self.read_path(params.path)
        if not path.is_dir():
            raise ToolFailure("invalid_path", "glob 的 path 必须是存在的目录。")
        if shutil.which("rg"):
            args = [*rg_command(), "--files", "-g", params.pattern, *skip_globs(path), "--", "."]
        else:
            args = fallback_command("glob", {"pattern": params.pattern, "path": str(path), "base": self.base(path)})
        details = await run_process(args, path, check=False, limit=262144)
        if details["exit_code"] not in (0, 1):
            raise ToolFailure("search_failed", "文件搜索失败。" + stderr_hint(details["stderr"]), stderr=details["stderr"])
        lines = details["stdout"].splitlines()
        if details["truncated"] and lines:
            lines.pop()  # The last path may be only a captured prefix.
        files = newest_first(found for line in lines if (found := self.read_path(str(path / line))).is_file())
        truncated = details["truncated"] or len(files) > GLOB_LIMIT
        shown = [self.display(item) for item in files[:GLOB_LIMIT]]
        if not shown:
            return "没有匹配文件。", {"count": 0, "truncated": False}
        note = f"\n（共 {len(files)}{'+' if details['truncated'] else ''} 项，仅显示最新的 {GLOB_LIMIT} 项；请细化模式）" if truncated else ""
        return "\n".join(shown) + note, {"count": len(shown), "truncated": truncated}


class Grep(SearchTool):
    name = "grep"
    description = ("用正则搜索文本内容。output_mode：files_with_matches（默认，只列文件，按修改时间从新到旧）、"
                   "content（路径:行号: 匹配行，context 指定前后各几行）、count（每个文件的匹配行数）。"
                   "path 指定范围，glob 限定文件，ignore_case 忽略大小写，head_limit 限制条数（默认 200）。"
                   "遵守 .gitignore；无匹配是成功。项目目录之外的路径需要用户确认。")
    prompt_summary = "搜索代码内容，返回文件、匹配行或计数"
    prompt_guidelines = (
        "按内容定位代码时优先 grep；先用默认的 files_with_matches 找到相关文件，需要看匹配行时再用 output_mode=content，必要时加 context。使用 path 和 glob 限定范围，结果截断时细化搜索。",
        "grep 的匹配行用于定位；修改前仍需读取相关原文和周边上下文。无匹配只说明当前范围和模式没有命中，必要时调整搜索。",
    )
    params_model = GrepParams

    async def execute(self, params: GrepParams):
        path = self.read_path(params.path)
        if not path.exists():
            raise FileNotFoundError()
        counting = params.output_mode != "content"
        if shutil.which("rg"):
            args = rg_command()
            if params.ignore_case:
                args.append("-i")
            args.extend(["--count", "--with-filename", "--null"] if counting else ["--json", "-C", str(params.context)])
            if params.glob is not None:
                args.extend(["-g", params.glob])
            args.extend([*skip_globs(path), "-e", params.pattern, "--", str(path)])
        else:
            args = fallback_command("count" if counting else "grep", {
                "pattern": params.pattern, "path": str(path), "base": self.base(path), "glob": params.glob,
                "ignore_case": params.ignore_case, "context": params.context})
        details = await run_process(args, self.root, check=False, limit=262144)
        if details["exit_code"] not in (0, 1):
            raise ToolFailure("search_failed", "搜索模式无效或目标无法访问。" + stderr_hint(details["stderr"]), stderr=details["stderr"])
        lines = details["stdout"].splitlines()
        if details["truncated"] and lines:
            lines.pop()
        if counting:
            counts = {}
            for line in lines:
                name, _, number = line.rpartition("\0")
                if name and number.isdigit():
                    counts[self.read_path(name)] = int(number)
            files = newest_first(counts)
            entries = ([self.display(item) for item in files] if params.output_mode == "files_with_matches"
                       else [f"{self.display(item)}:{counts[item]}" for item in files])
            total_matches = sum(counts.values())
            empty = "没有匹配内容。"
            unit = "个文件"
        else:
            events = {}
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") not in {"match", "context"}:
                    continue
                data = event["data"]
                if "text" not in data["path"] or "text" not in data["lines"]:
                    continue
                name = self.display(self.read_path(data["path"]["text"]))
                separator = ":" if event["type"] == "match" else "-"
                events.setdefault(name, {})[data["line_number"]] = (
                    f"{name}{separator}{data['line_number']}{separator} {data['lines']['text'].rstrip()}", event["type"] == "match")
            entries, total_matches = [], 0
            for name in sorted(events):
                previous = None
                for number in sorted(events[name]):
                    text, matched = events[name][number]
                    if params.context and previous is not None and number > previous + 1:
                        entries.append("--")
                    entries.append(text)
                    total_matches += matched
                    previous = number
            empty = "没有匹配内容。"
            unit = "行"
        if not entries:
            return empty, {"count": 0, "matches": 0, "truncated": False}
        truncated = details["truncated"] or len(entries) > params.head_limit
        shown = entries[:params.head_limit]
        if truncated:
            shown.append(f"（共 {len(entries)}{'+' if details['truncated'] else ''} {unit}，仅显示前 {params.head_limit} {unit}；请缩小范围或调整 head_limit）")
        return "\n".join(shown), {"count": len(entries), "matches": total_matches, "truncated": truncated,
                                  "output_mode": params.output_mode}
