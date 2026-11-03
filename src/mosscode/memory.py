"""Memory notes: two scopes, one Markdown file per note, and a program-generated MEMORY.md.

The memory tool and the background extractor share every check here, so neither can write
outside the two note directories, store a credential, or leave an index out of step with its notes.
"""

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import re
import secrets
import threading

from .context import estimate_text
from .paths import mosscode_home, project_dir

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
TYPES = ("user", "feedback", "project", "reference")
SCOPES = ("user", "project")
SCOPE_LABELS = {"user": "用户级", "project": "项目级"}
INDEX_FILE = "MEMORY.md"
DESCRIPTION_LIMIT = 150
BODY_LIMIT = 4096  # UTF-8 bytes.
INDEX_TOKENS = 3000  # Both indexes together, as injected into the system prompt.
INDEX_LINES = 200  # Per index.
MAX_OPERATIONS = 5
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# An aid, not a guarantee; the tool description and extraction prompt carry the rule as well.
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|token|password|passwd|secret)[\"']?\s*[=:：]\s*[\"']?[^\s\"']{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)


class MemoryFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Note:
    scope: str
    name: str
    type: str
    description: str
    updated: str
    body: str
    path: Path
    error: str = ""  # Set when the file could not be parsed; such notes stay out of the index.

    @property
    def filename(self) -> str:
        return self.path.name

    def index_line(self) -> str:
        return f"- [{self.name}]({self.filename}) — {self.description}"

    def text(self) -> str:
        return render_note(self.name, self.type, self.description, self.updated, self.body)


def render_note(name: str, type_: str, description: str, updated: str, body: str) -> str:
    return (f"---\nname: {name}\ndescription: {description}\ntype: {type_}\nupdated: {updated}\n---\n\n"
            f"{body.strip()}\n")


def parse_note(scope: str, path: Path, text: str) -> Note:
    """The frontmatter this module writes; anything else is marked as a format error."""
    def broken(reason: str) -> Note:
        return Note(scope, path.stem, "", "", "", text, path, reason)
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return broken("缺少 frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return broken("frontmatter 没有结束标记")
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1:]).strip()
    name, type_ = fields.get("name", ""), fields.get("type", "")
    description, updated = fields.get("description", ""), fields.get("updated", "")
    if name != path.stem or not SLUG.fullmatch(name):
        return broken("name 与文件名不一致")
    if type_ not in TYPES:
        return broken("type 不合法")
    if not description:
        return broken("缺少 description")
    if not _DATE.fullmatch(updated):
        return broken("updated 不是 YYYY-MM-DD")
    return Note(scope, name, type_, description, updated, body, path)


def find_secret(*texts: str) -> bool:
    return any(pattern.search(text) for text in texts for pattern in SECRET_PATTERNS)


def validate(scope, name, type_, description, body) -> None:
    """Shared by the tool and the extractor; raises MemoryFailure with a code the model can act on."""
    if scope not in SCOPES:
        raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
    if not isinstance(name, str) or not SLUG.fullmatch(name):
        if isinstance(name, str) and any(sep in name for sep in "/\\.:"):
            raise MemoryFailure("invalid_name", "name 不能包含路径分隔符或扩展名；只写笔记名，例如 prefer-pnpm。")
        raise MemoryFailure("invalid_name", "name 只能用小写字母、数字和连字符，以字母或数字开头，最长 60 字符。")
    if type_ not in TYPES:
        raise MemoryFailure("invalid_type", "type 只能是 user、feedback、project 或 reference。")
    if type_ == "project" and scope != "project":
        raise MemoryFailure("scope_mismatch", "project 类型的笔记只能写到项目级（scope: project）。")
    if not isinstance(description, str) or not description.strip():
        raise MemoryFailure("invalid_description", "description 不能为空，用一行说明这条笔记什么时候有用。")
    if "\n" in description.strip() or "\r" in description:
        raise MemoryFailure("invalid_description", "description 只能写一行。")
    if len(description.strip()) > DESCRIPTION_LIMIT:
        raise MemoryFailure("description_too_long", f"description 最多 {DESCRIPTION_LIMIT} 字符，当前 {len(description.strip())}。")
    if not isinstance(body, str) or not body.strip():
        raise MemoryFailure("invalid_body", "body 不能为空。")
    size = len(body.strip().encode("utf-8"))
    if size > BODY_LIMIT:
        raise MemoryFailure("body_too_large", f"body 最多 {BODY_LIMIT} 字节（UTF-8），当前 {size}；请精简。")
    if find_secret(name, description, body):
        raise MemoryFailure("secret_detected", "内容疑似包含密钥、令牌或密码，未保存；笔记中不要记录凭据。")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


class MemoryStore:
    def __init__(self, user_dir: Path, project_dir: Path):
        self.dirs = {"user": Path(user_dir), "project": Path(project_dir)}
        self._lock = threading.Lock()

    @classmethod
    def for_project(cls, root: Path, home: Path | None = None) -> "MemoryStore":
        return cls(mosscode_home(home) / "memory", project_dir(root, home) / "memory")

    def directory(self, scope: str) -> Path:
        return self.dirs[scope]

    # Reading ----------------------------------------------------------------------------------

    def load(self, scope: str | None = None) -> list[Note]:
        """Valid and broken notes, newest first within each scope; user scope before project."""
        notes = []
        for name in (scope,) if scope else SCOPES:
            folder = self.dirs[name]
            try:
                files = sorted(p for p in folder.glob("*.md") if p.name != INDEX_FILE and p.is_file())
            except OSError:
                files = []
            loaded = []
            for path in files:
                try:
                    loaded.append(parse_note(name, path, path.read_text(encoding="utf-8")))
                except (OSError, UnicodeError):
                    loaded.append(Note(name, path.stem, "", "", "", "", path, "无法读取"))
            loaded.sort(key=lambda n: (n.updated, n.name), reverse=True)
            notes += loaded
        return notes

    def find(self, name: str) -> list[Note]:
        return [note for note in self.load() if note.name == name]

    def render_index(self, scope: str) -> str:
        notes = [note for note in self.load(scope) if not note.error]
        lines = [note.index_line() for note in notes]
        return "\n".join(lines) + ("\n" if lines else "")

    def refresh(self) -> None:
        """Regenerate both indexes from the notes, e.g. after the user edited one by hand."""
        with self._lock:
            for scope in SCOPES:
                self._write_index(scope)

    def _write_index(self, scope: str) -> None:
        path = self.dirs[scope] / INDEX_FILE
        text = self.render_index(scope)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            current = None
        if current == text or (current is None and not text):
            return
        atomic_write(path, text)

    def index_section(self, limit: int = INDEX_TOKENS, lines: int = INDEX_LINES) -> str:
        """Both indexes for the system prompt, oldest entries dropped first beyond the limits."""
        entries = {scope: [n for n in self.load(scope) if not n.error] for scope in SCOPES}
        shown = {scope: notes[:lines] for scope, notes in entries.items()}

        def render():
            parts = []
            for scope in SCOPES:
                body = [note.index_line() for note in shown[scope]] or ["（暂无）"]
                omitted = len(entries[scope]) - len(shown[scope])
                if omitted:
                    body.append(f"另有 {omitted} 条较旧的笔记未列出，可用 glob 查看目录")
                parts.append(f"### {SCOPE_LABELS[scope]}笔记（目录：{self.dirs[scope]}）\n" + "\n".join(body))
            return "\n\n".join(parts)

        text = render()
        while estimate_text(text) > limit and any(shown.values()):
            # Drop the oldest entry across both scopes, then re-measure.
            oldest = min((scope for scope in SCOPES if shown[scope]), key=lambda s: (shown[s][-1].updated, s == "user"))
            shown[oldest] = shown[oldest][:-1]
            text = render()
        return text

    # Writing ----------------------------------------------------------------------------------

    def save(self, scope, name, type_, description, body, *, today: str | None = None) -> tuple[Note, bool]:
        validate(scope, name, type_, description, body)
        updated = today or date.today().isoformat()
        path = self.dirs[scope] / f"{name}.md"
        note = Note(scope, name, type_, description.strip(), updated, body.strip(), path)
        with self._lock:
            created = not path.exists()
            atomic_write(path, note.text())
            self._write_index(scope)
        return note, created

    def delete(self, scope, name) -> Path:
        if scope not in SCOPES:
            raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
        if not isinstance(name, str) or not SLUG.fullmatch(name):
            raise MemoryFailure("invalid_name", "name 只能用小写字母、数字和连字符，以字母或数字开头，最长 60 字符。")
        path = self.dirs[scope] / f"{name}.md"
        with self._lock:
            if not path.is_file():
                other = "project" if scope == "user" else "user"
                hint = f"；{SCOPE_LABELS[other]}有同名笔记" if (self.dirs[other] / f"{name}.md").is_file() else ""
                raise MemoryFailure("not_found", f"{SCOPE_LABELS[scope]}没有名为 {name} 的笔记{hint}。")
            path.unlink()
            self._write_index(scope)
        return path

    def apply(self, operations) -> tuple[list[tuple[str, Note | Path, str]], list[tuple[object, str]]]:
        """At most five operations, each checked on its own; returns (applied, rejected).

        applied items are (op, note or deleted path, name); rejected items are (operation, reason)."""
        applied, rejected = [], []
        if not isinstance(operations, list):
            return applied, [(operations, "操作列表必须是 JSON 数组。")]
        for operation in operations[:MAX_OPERATIONS]:
            if not isinstance(operation, dict):
                rejected.append((operation, "操作必须是 JSON 对象。"))
                continue
            op = operation.get("op")
            try:
                if op == "save":
                    note, _ = self.save(operation.get("scope"), operation.get("name"), operation.get("type"),
                                        operation.get("description"), operation.get("body"))
                    applied.append(("save", note, note.name))
                elif op == "delete":
                    path = self.delete(operation.get("scope"), operation.get("name"))
                    applied.append(("delete", path, path.stem))
                else:
                    rejected.append((operation, "op 只能是 save 或 delete。"))
            except MemoryFailure as exc:
                rejected.append((operation, f"{exc.code}: {exc}"))
            except OSError as exc:
                rejected.append((operation, f"写入失败：{exc.strerror or exc}"))
        rejected += [(operation, "超过一次最多 5 个操作的限制。") for operation in operations[MAX_OPERATIONS:]]
        return applied, rejected
