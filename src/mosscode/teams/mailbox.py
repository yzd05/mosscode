"""One JSON file of messages per participant, changed only under its lock."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import secrets
from typing import Callable

from .lock import TeamError, atomic_write, file_lock, load_json, now

LEAD = "lead"
BROADCAST = "*"
TYPES = ("text", "plan_approval_request", "plan_approval_response", "shutdown_request", "shutdown_response",
         "idle_notification", "permission_response")
# permission_response: the user's answer, given in the lead's window, to a pane member's approval.
LEAD_ONLY = frozenset({"plan_approval_response", "shutdown_request", "permission_response"})
MEMBER_ONLY = frozenset({"shutdown_response", "plan_approval_request", "idle_notification"})
# Sent for the model, never by it.
RUNTIME_ONLY = frozenset({"plan_approval_request", "idle_notification", "permission_response"})
EXTRA_FIELDS = {
    "plan_approval_request": ("request_id", "plan"),
    "plan_approval_response": ("request_id", "approve", "feedback"),
    "shutdown_request": ("reason",),
    "shutdown_response": ("request_id", "approve", "reason"),
    "idle_notification": ("reason", "completed_tasks"),  # Its summary is the message's own.
    "permission_response": ("request_id", "choice"),
}
BODY_LIMIT = 20000
SUMMARY_LIMIT = 80
KEEP_READ = 100
UNREAD_LIMIT = 500


@dataclass
class Message:
    sender: str
    type: str
    body: str = ""
    summary: str = ""
    id: str = field(default_factory=lambda: f"m-{secrets.token_hex(4)}")
    timestamp: str = field(default_factory=now)
    read: bool = False
    extra: dict = field(default_factory=dict)  # The type's own fields.

    def to_dict(self) -> dict:
        return {"id": self.id, "from": self.sender, "type": self.type, "body": self.body, "summary": self.summary,
                "timestamp": self.timestamp, "read": self.read, **self.extra}

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        known = {"id", "from", "type", "body", "summary", "timestamp", "read"}
        return cls(sender=str(data.get("from", "")), type=str(data.get("type", "text")), body=str(data.get("body", "")),
                   summary=str(data.get("summary", "")), id=str(data.get("id", "")),
                   timestamp=str(data.get("timestamp", "")), read=bool(data.get("read", False)),
                   extra={key: value for key, value in data.items() if key not in known})


def check(message: Message) -> None:
    """What any message must satisfy, whoever sends it; TeamError otherwise."""
    if message.type not in TYPES:
        raise TeamError("invalid_message", f"未知的消息类型 {message.type}；可用：{'、'.join(TYPES)}。")
    if message.type in LEAD_ONLY and message.sender != LEAD:
        raise TeamError("invalid_message", f"只有 Lead 能发送 {message.type}。")
    if message.type in MEMBER_ONLY and message.sender == LEAD:
        raise TeamError("invalid_message", f"Lead 不能发送 {message.type}。")
    if message.type == "text":
        if not message.summary.strip():
            raise TeamError("invalid_message", "text 消息必须有 summary（一句话摘要）。")
        if len(message.summary) > SUMMARY_LIMIT:
            raise TeamError("invalid_message", f"summary 最多 {SUMMARY_LIMIT} 字。")
    if len(message.body) > BODY_LIMIT:
        raise TeamError("invalid_message", f"正文最多 {BODY_LIMIT} 字，当前 {len(message.body)} 字。")
    missing = [name for name in EXTRA_FIELDS.get(message.type, ()) if name not in message.extra]
    if missing:
        raise TeamError("invalid_message", f"{message.type} 消息缺少字段：{'、'.join(missing)}。")


def _valid(data) -> bool:
    return isinstance(data, list) and all(isinstance(item, dict) for item in data)


def _trim(messages: list[dict]) -> list[dict]:
    """Every unread message and the latest KEEP_READ read ones, in order."""
    read = [index for index, item in enumerate(messages) if item.get("read")]
    dropped = set(read[:-KEEP_READ]) if len(read) > KEEP_READ else set()
    return [item for index, item in enumerate(messages) if index not in dropped]


class Mailbox:
    """The mailbox directory of one team: mailbox/<name>.json for each participant, lead included."""

    def __init__(self, directory: Path, on_notice: Callable[[str], None] | None = None):
        self.directory = directory
        self.on_notice = on_notice

    def path(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def _load(self, name: str) -> list[dict]:
        return load_json(self.path(name), list, _valid, self.on_notice)

    def send(self, to: str, message: Message) -> None:
        """Append under the recipient's lock; TeamError when busy or full, never a silent drop."""
        check(message)
        path = self.path(to)
        with file_lock(path, busy="mailbox_busy"):
            messages = self._load(to)
            if sum(1 for item in messages if not item.get("read")) >= UNREAD_LIMIT:
                raise TeamError("mailbox_full", f"{to} 的未读消息已达 {UNREAD_LIMIT} 条，暂不能再发送。")
            messages.append(message.to_dict())
            atomic_write(path, _trim(messages))

    def read_unread(self, name: str) -> list[Message]:
        """The unread messages, marked read in the same locked step."""
        path = self.path(name)
        with file_lock(path, busy="mailbox_busy"):
            messages = self._load(name)
            unread = [item for item in messages if not item.get("read")]
            if not unread:
                return []
            for item in unread:
                item["read"] = True
            atomic_write(path, _trim(messages))
        return [Message.from_dict(item) for item in unread]

    def take(self, name: str, kinds: frozenset[str] | set[str]) -> list[Message]:
        """The unread messages of these types only, marked read; the rest stay unread (answers mid-turn)."""
        path = self.path(name)
        if not path.exists():
            return []
        with file_lock(path, busy="mailbox_busy"):
            messages = self._load(name)
            taken = [item for item in messages if not item.get("read") and item.get("type") in kinds]
            if not taken:
                return []
            for item in taken:
                item["read"] = True
            atomic_write(path, _trim(messages))
        return [Message.from_dict(item) for item in taken]

    def discard_unread(self, name: str, kinds: frozenset[str] | set[str]) -> int:
        """Mark stale unread messages of these types read (an old shutdown request or answer)."""
        return len(self.take(name, kinds))

    def peek(self, name: str) -> list[Message]:
        """Every kept message, read or not; for /team. No lock: a reader sees the old file or the new one."""
        return [Message.from_dict(item) for item in self._load(name)]

    def unread_count(self, name: str) -> int:
        return sum(1 for message in self.peek(name) if not message.read)
