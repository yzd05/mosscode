"""@ mentions: files and directories the user names in a message, read for the model before it is sent.

The typed message stays as it is; what the mentions hold goes into a program-written message right after it,
so the display, /rewind and memory extraction keep the user's own words.
"""

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .messages import MENTIONS, ContentBlock, Message
from .permissions import Decision
from .security import SecurityError, resolve_path

# Full-width punctuation ends an unquoted path: Chinese text often runs on without a space after it.
STOPS = "，。；：！？（）【】「」『』、《》"
MENTION = re.compile(rf'(?<!\S)@(?:"([^"\n]+)"|([^\s"{STOPS}]+))')
LINES = re.compile(r"#L(\d+)(?:-L?(\d+))?$")
TRAILING = ",.;:!?)]}>'"
MENTION_LIMIT = 10  # Per message.
CONTENT_BUDGET = 112 * 1024  # Bytes of text attached per message; later files are named only.
LISTING_LIMIT = 100  # Entries of a mentioned directory.
TAG = re.compile(r'^<(file|directory) path="([^"]+)"', re.MULTILINE)


@dataclass(frozen=True)
class Mention:
    path: Path
    name: str  # Relative to the workspace (with /), or absolute outside it.
    directory: bool
    offset: int = 1
    limit: int | None = None  # Lines from offset; None reads read_file's first page.


@dataclass(frozen=True)
class Attached:
    message: Message
    entries: tuple[str, ...]  # One line per mention for the interface, e.g. "a.py（120 行）".


def _name(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def _resolve(root: Path, token: str) -> Mention | None:
    lines = LINES.search(token)
    offset, limit = 1, None
    if lines:
        start = int(lines.group(1))
        end = int(lines.group(2)) if lines.group(2) else None
        if start < 1 or (end is not None and end < start):
            return None
        offset, limit = start, (end - start + 1 if end is not None else None)
        token = token[: lines.start()]
    if not token:
        return None
    try:
        path = resolve_path(root, token)
        directory = path.is_dir()
        if not directory and not path.is_file():
            return None
    except (SecurityError, OSError):
        return None
    if directory and lines:
        return None
    return Mention(path, _name(root, path) + ("/" if directory and path != root else ""), directory, offset, limit)


def find_mentions(text: str, root: Path) -> list[Mention]:
    """Mentions of existing paths in order, each once; trailing punctuation is not part of a path."""
    found: list[Mention] = []
    seen: set[tuple[str, int, int | None]] = set()
    for match in MENTION.finditer(text):
        candidates = [match.group(1) or match.group(2)]
        while not match.group(1) and candidates[-1][-1:] in TRAILING and len(candidates[-1]) > 1:
            candidates.append(candidates[-1][:-1])
        for candidate in candidates:
            mention = _resolve(root, candidate)
            if mention is not None:
                key = (os.path.normcase(str(mention.path)), mention.offset, mention.limit)
                if key not in seen:
                    seen.add(key)
                    found.append(mention)
                break
    return found


def _listing(path: Path) -> tuple[str, int]:
    entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
    lines = [item.name + ("/" if item.is_dir() else "") for item in entries[:LISTING_LIMIT]]
    if len(entries) > LISTING_LIMIT:
        lines.append(f"…（另有 {len(entries) - LISTING_LIMIT} 项）")
    return "\n".join(lines) or "（空目录）", len(entries)


def _range(mention: Mention) -> str:
    if mention.limit is not None:
        return f"第 {mention.offset}–{mention.offset + mention.limit - 1} 行"
    return f"第 {mention.offset} 行起" if mention.offset > 1 else ""


async def attach(text: str, registry) -> Attached | None:
    """Reads what text mentions through the registry's read_file; None when it mentions nothing."""
    mentions = await asyncio.to_thread(find_mentions, text, registry.permissions.root)
    if not mentions:
        return None
    blocks: list[ContentBlock] = [
        ContentBlock(
            "text",
            "用户在上面的消息中用 @ 引用了下列文件或目录。MossCode 刚刚替你用 read_file 读取过，下面就是它们当前的内容"
            "（格式与 read_file 的结果相同），不需要再读取或搜索；要修改时直接用 edit_file。",
        )
    ]
    entries: list[str] = []
    used = 0
    for index, mention in enumerate(mentions):
        span = _range(mention)
        lines = f' lines="{mention.offset}-{mention.offset + mention.limit - 1}"' if mention.limit else ""
        if index >= MENTION_LIMIT or used >= CONTENT_BUDGET:
            why = f"一条消息最多附上 {MENTION_LIMIT} 个引用" if index >= MENTION_LIMIT else "附上的内容已达上限"
            blocks.append(
                ContentBlock(
                    "text", f'\n<file path="{mention.name}" omitted="true">\n{why}，需要时用 read_file 读取。\n</file>'
                )
            )
            entries.append(f"{mention.name}（只给出路径：{why}）")
            continue
        if mention.directory:
            refused = _refused(registry, mention)
            if refused is not None:
                body, entry = f"未读取：{refused.reason}", f"{mention.name}（未读取：{refused.reason}）"
            else:
                try:
                    body, count = await asyncio.to_thread(_listing, mention.path)
                    entry = f"{mention.name}（目录，{count} 项）"
                except OSError:
                    body, entry = "无法列出目录。", f"{mention.name}（无法列出目录）"
            blocks.append(ContentBlock("text", f'\n<directory path="{mention.name}">\n{body}\n</directory>'))
            used += len(body.encode("utf-8"))
            entries.append(entry)
            continue
        result = await registry.read_for_user(str(mention.path), mention.offset, mention.limit)
        if result.is_error:
            body = result.model_text()
            entries.append(f"{mention.name}（未读取：{result.output}）")
        else:
            body = result.output
            if result.images:
                entries.append(f"{mention.name}（图片）")
            else:
                shown = result.details.get("lines")
                entries.append(
                    f"{mention.name}（{span + '，' if span else ''}{shown} 行）" if shown is not None else mention.name
                )
        blocks.append(ContentBlock("text", f'\n<file path="{mention.name}"{lines}>\n{body}\n</file>'))
        blocks.extend(ContentBlock("image", image.alt, image) for image in result.images)
        used += len(body.encode("utf-8"))
    return Attached(Message("user", tuple(blocks), synthetic=MENTIONS), tuple(entries))


def _refused(registry, mention: Mention):
    """A directory is listed only where read_file could read: the same hard checks and deny rules."""
    tool = registry.get("read_file")
    if tool is None:
        return Decision("deny", "unknown_tool", "当前没有启用 read_file。")
    return registry.refusal(tool, tool.validate_arguments({"path": str(mention.path)}))


def mentioned(message: Message) -> list[str]:
    """The names an attachment message carries, for showing it again after a resume."""
    return [name for _, name in TAG.findall(message.text)]


# Completion.

REFRESH = 10.0  # Seconds an index stays fresh.
INDEX_LIMIT = 50000
SHOWN = 12


def _listed(root: Path) -> list[str]:
    from .tools.search import rg_command, skip_globs

    if shutil.which("rg"):
        args = [*rg_command(), "--files", "--path-separator", "/", *skip_globs(root), "--", "."]
        try:
            done = subprocess.run(args, cwd=root, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        lines = done.stdout.decode("utf-8", "replace").splitlines()
        return [line.removeprefix("./") for line in lines[:INDEX_LIMIT] if line]
    from .tools.search_worker import files

    found = []
    for path in files(root, root):
        found.append(path.relative_to(root).as_posix())
        if len(found) >= INDEX_LIMIT:
            break
    return found


class FileIndex:
    """The workspace's files and their directories, listed in a thread and reused for a few seconds."""

    def __init__(self, root: Path):
        self.root = root
        self.paths: tuple[str, ...] = ()
        self.loaded_at: float | None = None

    @property
    def stale(self) -> bool:
        return self.loaded_at is None or monotonic() - self.loaded_at > REFRESH

    def load(self) -> None:
        try:
            files = _listed(self.root)
        except OSError:
            files = []
        directories = {
            "/".join(parts[:end]) + "/" for parts in (name.split("/") for name in files) for end in range(1, len(parts))
        }
        self.paths = tuple(sorted(directories)) + tuple(sorted(files))
        self.loaded_at = monotonic()

    def candidates(self, query: str, limit: int = SHOWN) -> list[str]:
        if not query or query.endswith("/"):
            # A directory's own entries, directories first; the workspace's top level for a bare @.
            depth = query.count("/")
            inside = [
                path
                for path in self.paths
                if path.startswith(query) and path != query and path.rstrip("/").count("/") == depth
            ]
            return inside[:limit]
        from textual.fuzzy import Matcher

        matcher = Matcher(query)
        scored = [(score, path) for path in self.paths if (score := matcher.match(path)) > 0]
        scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        return [path for _, path in scored[:limit]]


def quoted(path: str) -> str:
    return f'"{path}"' if any(char.isspace() for char in path) else path


PARTIAL = re.compile(rf'(?<!\S)@(?:"([^"\n]*)|([^\s"{STOPS}]*))$')


def partial_mention(before: str) -> tuple[int, str] | None:
    """(where the @ starts, what follows it) when the text before the cursor ends in a mention being typed."""
    match = PARTIAL.search(before)
    if match is None:
        return None
    return match.start(), match.group(1) if match.group(1) is not None else match.group(2)
