"""Single-truth append-only team blackboard (board.jsonl), modeled after OpenHands EventStream."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .lock import TeamError, file_lock, now, read_text
from .mailbox import BROADCAST, LEAD

BODY_LIMIT = 20_000
SUMMARY_LIMIT = 120
POST_KINDS = ("brief", "post", "direct", "watchdog", "done")
PostKind = Literal["brief", "post", "direct", "watchdog", "done"]

# ASCII boundaries allow Chinese text directly adjacent to @name or #seq (e.g. "请@verifier审查#1")
# while rejecting emails (foo@bar), paths (/a/@b), snake_case (@a_b), and decorators (@pytest.mark).
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_/.-])@([a-z0-9][a-z0-9-]{0,31})(?![A-Za-z0-9_-])(?!(?:\.[A-Za-z0-9_]))")
REF_RE = re.compile(r"(?<![A-Za-z0-9_])#(\d+)(?![A-Za-z0-9_])")
FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def _strip_code(text: str, *, inline: bool = True) -> str:
    """Remove fenced code blocks and (optionally) inline backtick spans before scanning mentions."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in lines:
        if fence_char:
            stripped = line.lstrip(" \t")
            run = len(stripped) - len(stripped.lstrip(fence_char))
            if run >= fence_len and stripped[run:].strip() == "":
                fence_char = ""
                fence_len = 0
            kept.append("\n")
            continue
        match = FENCE_OPEN_RE.match(line)
        if match:
            marker, rest = match.group(1), match.group(2)
            # Single-line triple-backtick spans like ```@alice``` have closing backticks on the same line.
            if marker[0] == "~" or marker[0] not in rest:
                fence_char = marker[0]
                fence_len = len(marker)
                kept.append("\n")
                continue
        kept.append(line)

    without_fences = "".join(kept)
    if not inline or "`" not in without_fences:
        return without_fences

    out: list[str] = []
    idx = 0
    total = len(without_fences)
    while idx < total:
        if without_fences[idx] != "`":
            nxt = without_fences.find("`", idx)
            if nxt == -1:
                out.append(without_fences[idx:])
                break
            out.append(without_fences[idx:nxt])
            idx = nxt
            continue
        end = idx
        while end < total and without_fences[end] == "`":
            end += 1
        tick_len = end - idx
        # Find a matching closing run of exact length tick_len.
        search = end
        closed = -1
        while search < total:
            pos = without_fences.find("`" * tick_len, search)
            if pos == -1:
                break
            before_ok = pos == 0 or without_fences[pos - 1] != "`"
            after_pos = pos + tick_len
            after_ok = after_pos == total or without_fences[after_pos] != "`"
            if before_ok and after_ok:
                closed = after_pos
                break
            while pos < total and without_fences[pos] == "`":
                pos += 1
            search = pos
        if closed != -1:
            out.append(" ")
            idx = closed
        else:
            out.append(without_fences[idx:end])
            idx = end
    return "".join(out)


def partition_mentions(
    text: str,
    valid_members: Iterable[str] | None = None,
    *,
    author: str = "",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (resolved_mentions, unknown_mentions) after stripping code blocks and inline code."""
    cleaned = _strip_code(text, inline=True)
    raw: list[str] = []
    for match in MENTION_RE.finditer(cleaned):
        token = match.group(1)
        if token not in raw:
            raw.append(token)
    if valid_members is None:
        resolved: list[str] = []
        for token in raw:
            mapped = BROADCAST if token == "all" else token
            if mapped != author and mapped not in resolved:
                resolved.append(mapped)
        return tuple(resolved), ()

    allowed = set(valid_members) | {LEAD, "all"}
    resolved = []
    unknown: list[str] = []
    for token in raw:
        if token == "all":
            if BROADCAST not in resolved:
                resolved.append(BROADCAST)
        elif token in allowed:
            if token != author and token not in resolved:
                resolved.append(token)
        else:
            if token not in unknown:
                unknown.append(token)
    return tuple(resolved), tuple(unknown)


def extract_mentions(
    text: str,
    valid_members: Iterable[str] | None = None,
    *,
    author: str = "",
) -> tuple[str, ...]:
    """Extract validated @mentions from non-code text ('@all' -> '*')."""
    return partition_mentions(text, valid_members, author=author)[0]


def extract_refs(text: str) -> tuple[int, ...]:
    """Extract positive #seq references from text (ignoring fenced code blocks)."""
    cleaned = _strip_code(text, inline=False)
    seen: list[int] = []
    for match in REF_RE.finditer(cleaned):
        number = int(match.group(1))
        if number > 0 and number not in seen:
            seen.append(number)
    return tuple(seen)


def derive_summary(body: str, limit: int = SUMMARY_LIMIT) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def mentions_actor(post: BoardPost, actor: str) -> bool:
    """Whether a post addresses `actor` ('*' wakes team members except author, never lead unless named)."""
    if post.author == actor:
        return False
    if actor in post.mentions:
        return True
    return BROADCAST in post.mentions and actor != LEAD


@dataclass
class BoardPost:
    seq: int
    author: str
    body: str
    summary: str = ""
    mentions: tuple[str, ...] = ()
    refs: tuple[int, ...] = ()
    kind: PostKind = "post"
    timestamp: str = field(default_factory=now)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "author": self.author,
            "body": self.body,
            "summary": self.summary,
            "mentions": list(self.mentions),
            "refs": list(self.refs),
            "kind": self.kind,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BoardPost:
        raw_kind = str(data.get("kind", "post"))
        kind: PostKind = raw_kind if raw_kind in POST_KINDS else "post"  # type: ignore[assignment]
        return cls(
            seq=int(data["seq"]),
            author=str(data["author"]),
            body=str(data.get("body", "")),
            summary=str(data.get("summary", "")),
            mentions=tuple(str(item) for item in data.get("mentions", ()) if item),
            refs=tuple(int(item) for item in data.get("refs", ()) if int(item) > 0),
            kind=kind,
            timestamp=str(data.get("timestamp", "")),
        )


class Blackboard:
    """Append-only JSONL blackboard stored at <team>/board.jsonl."""

    def __init__(self, path: Path, on_notice: Callable[[str], None] | None = None):
        self.path = path
        self.on_notice = on_notice
        self._warned_corrupt = False

    def _load_unlocked(self) -> list[BoardPost]:
        try:
            raw = read_text(self.path)
        except FileNotFoundError:
            return []
        posts: list[BoardPost] = []
        corrupt = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict) or "seq" not in item or "author" not in item:
                    corrupt += 1
                    continue
                posts.append(BoardPost.from_dict(item))
            except (ValueError, TypeError, KeyError):
                corrupt += 1
        if corrupt and not self._warned_corrupt and self.on_notice is not None:
            self._warned_corrupt = True
            self.on_notice(f"{self.path} 中有 {corrupt} 行损坏记录，已跳过。")
        posts.sort(key=lambda post: post.seq)
        return posts

    def append(
        self,
        author: str,
        body: str,
        summary: str = "",
        *,
        kind: PostKind = "post",
        explicit_mentions: Iterable[str] = (),
        valid_members: Iterable[str] | None = None,
    ) -> BoardPost:
        if kind not in POST_KINDS:
            raise TeamError("invalid_message", f"未知的黑板条目类型 {kind}。")
        if len(body) > BODY_LIMIT:
            raise TeamError("invalid_message", f"正文最多 {BODY_LIMIT} 字，当前 {len(body)} 字。")
        summary = summary.strip()
        if len(summary) > SUMMARY_LIMIT:
            raise TeamError("invalid_message", f"summary 最多 {SUMMARY_LIMIT} 字。")
        if not body.strip() and not summary:
            raise TeamError("invalid_message", "黑板内容不能为空。")
        if not summary:
            summary = derive_summary(body)
        if kind == "post" and body.lstrip().lower().startswith("[done]"):
            kind = "done"

        allowed = None if valid_members is None else (set(valid_members) | {LEAD, BROADCAST})
        merged_mentions: list[str] = []
        for item in explicit_mentions:
            mapped = BROADCAST if item in {"all", BROADCAST} else str(item).strip()
            if not mapped or mapped == author:
                continue
            if allowed is not None and mapped not in allowed:
                continue
            if mapped not in merged_mentions:
                merged_mentions.append(mapped)
        if kind in {"post", "done"} or (kind == "watchdog" and not merged_mentions):
            for item in extract_mentions(body, valid_members, author=author):
                if item not in merged_mentions:
                    merged_mentions.append(item)

        refs = extract_refs(body)
        with file_lock(self.path, busy="board_busy"):
            posts = self._load_unlocked()
            seq = (posts[-1].seq if posts else 0) + 1
            post = BoardPost(
                seq=seq,
                author=author,
                body=body,
                summary=summary,
                mentions=tuple(merged_mentions),
                refs=refs,
                kind=kind,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prefix = ""
            if self.path.exists() and self.path.stat().st_size > 0:
                with self.path.open("rb") as existing:
                    existing.seek(-1, 2)
                    if existing.read(1) != b"\n":
                        prefix = "\n"
            encoded = json.dumps(post.to_dict(), ensure_ascii=False)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{prefix}{encoded}\n")
                stream.flush()
        return post

    def read_since(self, since: int = 0, *, limit: int = 20) -> tuple[list[BoardPost], int]:
        with file_lock(self.path, busy="board_busy"):
            posts = self._load_unlocked()
            head_seq = posts[-1].seq if posts else 0
            after = [post for post in posts if post.seq > max(0, since)]
            return after[: max(0, limit)], head_seq

    def read_recent(self, limit: int = 20) -> tuple[list[BoardPost], int]:
        with file_lock(self.path, busy="board_busy"):
            posts = self._load_unlocked()
            head_seq = posts[-1].seq if posts else 0
            cap = max(0, limit)
            return (posts[-cap:] if cap else []), head_seq

    def read_seqs(self, seqs: Iterable[int]) -> tuple[list[BoardPost], int]:
        wanted = {int(item) for item in seqs}
        with file_lock(self.path, busy="board_busy"):
            posts = self._load_unlocked()
            head_seq = posts[-1].seq if posts else 0
            return [post for post in posts if post.seq in wanted], head_seq

    def unread_for(self, actor: str, after_seq: int) -> tuple[list[BoardPost], int]:
        with file_lock(self.path, busy="board_busy"):
            posts = self._load_unlocked()
            head_seq = posts[-1].seq if posts else 0
            unread = [post for post in posts if post.seq > after_seq and mentions_actor(post, actor)]
            return unread, head_seq

    def peek(self) -> list[BoardPost]:
        """Lockless read for UI inspection (/team, /swarm)."""
        return self._load_unlocked()
