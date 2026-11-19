"""Memory notes: two scopes, one Markdown file per note, and a program-generated MEMORY.md.

The memory tool and the background extractor share every check here, so neither can write
outside the two note directories, store a credential, or leave an index out of step with its notes.
"""

import math
import os
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .context import estimate_text
from .paths import memory_project_dir, mosscode_home

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
BM25_K1 = 1.5
BM25_B = 0.75
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TOKEN_RUN = re.compile(r"[a-z0-9_]+(?:-[a-z0-9_]+)*|[\u4e00-\u9fff\u3400-\u4dbf]+")
_CJK_PARTICLES = frozenset("的了着吗呢吧啊呀")

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
    pinned: bool = False

    @property
    def filename(self) -> str:
        return self.path.name

    def index_line(self) -> str:
        if self.pinned:
            return f"- [{self.name}]({self.filename}) [常驻] — {self.description}"
        return f"- [{self.name}]({self.filename}) — {self.description}"

    def text(self) -> str:
        return render_note(self.name, self.type, self.description, self.updated, self.body, pinned=self.pinned)


def render_note(
    name: str,
    type_: str,
    description: str,
    updated: str,
    body: str,
    *,
    pinned: bool = False,
) -> str:
    pin_line = "pinned: true\n" if pinned else ""
    return (
        f"---\nname: {name}\ndescription: {description}\ntype: {type_}\n"
        f"{pin_line}updated: {updated}\n---\n\n{body.strip()}\n"
    )


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
    body = "\n".join(lines[end + 1 :]).strip()
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
    pinned = False
    if "pinned" in fields:
        raw_pinned = fields["pinned"].lower()
        if raw_pinned == "true":
            pinned = True
        elif raw_pinned == "false":
            pinned = False
        else:
            return broken("pinned 必须是 true 或 false")
    return Note(scope, name, type_, description, updated, body, path, pinned=pinned)


def tokenize_memory(text: str, *, cjk_unigrams: bool = False) -> list[str]:
    """Tokenize ASCII identifiers (full + snake/kebab parts) and CJK text (overlapping bigrams + 1-char runs)."""
    if not text:
        return []
    tokens: list[str] = []
    for match in _TOKEN_RUN.finditer(text.lower()):
        run = match.group(0)
        first = run[0]
        if "\u3400" <= first <= "\u9fff":
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
                if cjk_unigrams:
                    tokens.extend(ch for ch in run if ch not in _CJK_PARTICLES)
        else:
            parts = [part for part in re.split(r"[_-]+", run) if part]
            if not parts:
                continue
            tokens.append(run)
            if len(parts) > 1 or parts[0] != run:
                tokens.extend(parts)
    return tokens


def find_secret(*texts: str) -> bool:
    return any(pattern.search(text) for text in texts for pattern in SECRET_PATTERNS)


def _validate_name(name: object) -> str:
    if not isinstance(name, str) or not SLUG.fullmatch(name):
        if isinstance(name, str) and any(sep in name for sep in "/\\.:"):
            raise MemoryFailure("invalid_name", "name 不能包含路径分隔符或扩展名；只写笔记名，例如 prefer-pnpm。")
        raise MemoryFailure("invalid_name", "name 只能用小写字母、数字和连字符，以字母或数字开头，最长 60 字符。")
    return name


def validate(scope, name, type_, description, body, *, pinned: object = None) -> None:
    """Shared by the tool and the extractor; raises MemoryFailure with a code the model can act on."""
    if scope not in SCOPES:
        raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
    _validate_name(name)
    if type_ not in TYPES:
        raise MemoryFailure("invalid_type", "type 只能是 user、feedback、project 或 reference。")
    if type_ == "project" and scope != "project":
        raise MemoryFailure("scope_mismatch", "project 类型的笔记只能写到项目级（scope: project）。")
    if not isinstance(description, str) or not description.strip():
        raise MemoryFailure("invalid_description", "description 不能为空，用一行说明这条笔记什么时候有用。")
    if "\n" in description.strip() or "\r" in description:
        raise MemoryFailure("invalid_description", "description 只能写一行。")
    if len(description.strip()) > DESCRIPTION_LIMIT:
        raise MemoryFailure(
            "description_too_long", f"description 最多 {DESCRIPTION_LIMIT} 字符，当前 {len(description.strip())}。"
        )
    if not isinstance(body, str) or not body.strip():
        raise MemoryFailure("invalid_body", "body 不能为空。")
    size = len(body.strip().encode("utf-8"))
    if size > BODY_LIMIT:
        raise MemoryFailure("body_too_large", f"body 最多 {BODY_LIMIT} 字节（UTF-8），当前 {size}；请精简。")
    if pinned is not None and not isinstance(pinned, bool):
        raise MemoryFailure("invalid_pinned", "pinned 必须是布尔值 true 或 false。")
    if find_secret(name, description, body):
        raise MemoryFailure("secret_detected", "内容疑似包含密钥、令牌或密码，未保存；笔记中不要记录凭据。")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                # Windows indexers and IDE file watchers can briefly lock the target without FILE_SHARE_DELETE.
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


class MemoryStore:
    def __init__(self, user_dir: Path, project_dir: Path):
        self.dirs = {"user": Path(user_dir), "project": Path(project_dir)}
        self._lock = threading.Lock()

    @classmethod
    def for_project(cls, root: Path, home: Path | None = None) -> "MemoryStore":
        return cls(mosscode_home(home) / "memory", memory_project_dir(root, home))

    def directory(self, scope: str) -> Path:
        return self.dirs[scope]

    # Reading ----------------------------------------------------------------------------------

    def load(self, scope: str | None = None) -> list[Note]:
        """Valid and broken notes, pinned then newest first within each scope; user scope before project."""
        if scope is not None and scope not in SCOPES:
            raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
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
            # `files` is already sorted by filename ascending, so Python's stable sort keeps `name` asc on ties.
            loaded.sort(key=lambda n: (not n.error, n.pinned, n.updated), reverse=True)
            notes += loaded
        return notes

    def find(self, name: str) -> list[Note]:
        return [note for note in self.load() if note.name == name]

    def read(self, name: str, *, scope: str | None = None) -> list[Note]:
        """Validate `name` against path traversal and return matching valid note(s) in project/user order."""
        if scope is not None and scope not in SCOPES:
            raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
        _validate_name(name)
        found: list[Note] = []
        for sc in (scope,) if scope else ("project", "user"):
            folder = self.dirs[sc]
            path = folder / f"{name}.md"
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.resolve().parent != folder.resolve():
                    continue
                note = parse_note(sc, path, path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if not note.error:
                found.append(note)
        return found

    def search(self, query: str, *, scope: str | None = None, limit: int = 10) -> list[tuple[Note, float]]:
        """Rank valid notes with Okapi BM25 (k1=1.5, b=0.75) over field-boosted tokens + CJK bigrams."""
        if scope is not None and scope not in SCOPES:
            raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
        if limit <= 0 or not isinstance(query, str):
            return []
        cleaned = query.strip()
        if not cleaned:
            return []
        q_tokens = tokenize_memory(cleaned)
        if not q_tokens:
            return []
        need_cjk_unigrams = any(len(tok) == 1 and "\u3400" <= tok <= "\u9fff" for tok in q_tokens)
        scopes = (scope,) if scope else ("project", "user")
        notes = [note for sc in scopes for note in self.load(sc) if not note.error]
        if not notes:
            return []

        doc_tfs: list[Counter[str]] = []
        doc_lens: list[int] = []
        for note in notes:
            bag = (
                tokenize_memory(note.name, cjk_unigrams=need_cjk_unigrams) * 3
                + tokenize_memory(note.description, cjk_unigrams=need_cjk_unigrams) * 2
                + tokenize_memory(note.body, cjk_unigrams=need_cjk_unigrams)
            )
            doc_tfs.append(Counter(bag))
            doc_lens.append(len(bag))

        n_docs = len(notes)
        total_len = sum(doc_lens)
        avgdl = total_len / n_docs if total_len > 0 else 1.0
        q_counts = Counter(q_tokens)
        idf: dict[str, float] = {}
        for term in q_counts:
            df = sum(1 for tf_map in doc_tfs if term in tf_map)
            if df > 0:
                idf[term] = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

        norm_q = cleaned.lower()
        slug_q = re.sub(r"[_\s]+", "-", norm_q).strip("-")
        scored: list[tuple[Note, float]] = []
        for note, tf_map, doc_len in zip(notes, doc_tfs, doc_lens):
            bm25 = 0.0
            for term, q_tf in q_counts.items():
                tf = tf_map.get(term, 0)
                if tf > 0:
                    denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * (doc_len / avgdl))
                    bm25 += idf[term] * ((tf * (BM25_K1 + 1.0)) / denom) * q_tf
            bonus = 0.0
            name_lower = note.name.lower()
            desc_lower = note.description.lower()
            if norm_q in name_lower or (len(slug_q) >= 2 and slug_q in name_lower):
                bonus += 2.0
            if norm_q in desc_lower or (len(slug_q) >= 2 and slug_q in desc_lower):
                bonus += 1.0
            score = bm25 + bonus
            if score <= 0.0:
                continue
            if note.pinned:
                score += 0.01
            scored.append((note, score))

        scored.sort(key=lambda item: item[0].name)
        scored.sort(
            key=lambda item: (round(item[1], 8), item[0].pinned, item[0].updated, item[0].scope == "project"),
            reverse=True,
        )
        return scored[:limit]

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
        """Both indexes for the system prompt, unpinned/oldest entries dropped first beyond the limits."""
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
            # Drop unpinned before pinned, older before newer, and global user notes before current project notes.
            oldest = min(
                (scope for scope in SCOPES if shown[scope]),
                key=lambda s: (shown[s][-1].pinned, shown[s][-1].updated, s == "project"),
            )
            shown[oldest] = shown[oldest][:-1]
            text = render()
        return text

    # Writing ----------------------------------------------------------------------------------

    def save(
        self,
        scope,
        name,
        type_,
        description,
        body,
        *,
        today: str | None = None,
        pinned: bool | None = None,
    ) -> tuple[Note, bool]:
        validate(scope, name, type_, description, body, pinned=pinned)
        updated = today or date.today().isoformat()
        path = self.dirs[scope] / f"{name}.md"
        with self._lock:
            created = not path.exists()
            effective_pinned = bool(pinned) if pinned is not None else False
            if pinned is None and not created:
                try:
                    existing = parse_note(scope, path, path.read_text(encoding="utf-8"))
                    if not existing.error:
                        effective_pinned = existing.pinned
                except (OSError, UnicodeError):
                    pass
            note = Note(scope, name, type_, description.strip(), updated, body.strip(), path, pinned=effective_pinned)
            atomic_write(path, note.text())
            self._write_index(scope)
        return note, created

    def delete(self, scope, name) -> Path:
        if scope not in SCOPES:
            raise MemoryFailure("invalid_scope", "scope 只能是 user 或 project。")
        _validate_name(name)
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
                    note, _ = self.save(
                        operation.get("scope"),
                        operation.get("name"),
                        operation.get("type"),
                        operation.get("description"),
                        operation.get("body"),
                        pinned=operation.get("pinned"),
                    )
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
