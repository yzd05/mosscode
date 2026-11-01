"""Ch15: long-lived agent teams of one project, stored under ~/.mosscode/projects/<key>/teams/<team>/."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
from pathlib import Path
import re
from typing import Callable, Literal

from ..paths import project_dir
from ..worktree.meta import process_alive
from ..worktree.names import RESERVED
from .board import TaskBoard
from .lock import TeamError, atomic_write, file_lock, now, read_text
from .mailbox import BROADCAST, LEAD, Mailbox

__all__ = ["BROADCAST", "LEAD", "Member", "Team", "TeamError", "TeamStore", "list_teams", "teams_root",
           "validate_member_name", "validate_team_name"]

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
VERSION = 1


def _validate(name: str, what: str) -> str | None:
    """TM1: None when usable, otherwise the reason."""
    if not NAME.fullmatch(name or ""):
        return f"{what}只能含小写字母、数字和 -，1–32 个字符，以字母或数字开头"
    if name in RESERVED:
        return f"{name} 是 Windows 保留的设备名"
    return None


def validate_team_name(name: str) -> str | None:
    return _validate(name, "团队名")


def validate_member_name(name: str) -> str | None:
    if name in {LEAD, BROADCAST}:
        return f"成员名不能是 {name}"
    return _validate(name, "成员名")


def teams_root(project: Path, home: Path | None = None) -> Path:
    """TM2: next to the project's sessions, so the same key finds both."""
    return project_dir(project, home) / "teams"


@dataclass
class Member:
    """TM3."""
    name: str
    agent_type: str
    backend: Literal["in-process", "windows-terminal", "tmux"] = "in-process"
    backend_reason: str = ""
    status: Literal["running", "idle", "stopped"] = "stopped"
    require_plan_approval: bool = False
    approved: bool = False  # PA4: its plan was approved; a revived member keeps it (ID4).
    mode: str = "default"  # PM3: written by the lead, read by a pane member each turn.
    model: str = ""
    worktree: dict | None = None  # {"name", "path", "branch"}; None works in the project directory.
    pid: int | None = None
    heartbeat: str | None = None
    pane: str | None = None  # tmux pane id.
    transcript: str = ""  # Relative to the team directory.
    prompt: str = ""
    usage: dict = field(default_factory=dict)
    created: str = field(default_factory=now)
    error: str = ""  # Written by a pane member that could not start (BE6, BE9).
    last_tool: str = ""  # CM2: written by a pane member while it runs.

    def agent_id(self, team: str) -> str:
        return f"{self.name}@{team}"

    @classmethod
    def from_dict(cls, data: dict) -> Member:
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Team:
    name: str
    description: str = ""
    created: str = field(default_factory=now)
    lead: dict | None = None  # {"pid", "session", "since"} of the process holding it (TM4).
    members: list[Member] = field(default_factory=list)
    project: str = ""  # The main project directory; a pane member loads its rules and agents there.

    def member(self, name: str) -> Member | None:
        return next((member for member in self.members if member.name == name), None)

    def names(self) -> tuple[str, ...]:
        return tuple(member.name for member in self.members)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["v"] = VERSION
        for item, member in zip(data["members"], self.members):
            item["agent_id"] = member.agent_id(self.name)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Team:
        members = [Member.from_dict(item) for item in data.get("members", []) if isinstance(item, dict)]
        return cls(name=str(data["name"]), description=str(data.get("description", "")),
                   created=str(data.get("created", "")), lead=data.get("lead") or None, members=members,
                   project=str(data.get("project", "")))


def _valid(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("name"), str) and isinstance(data.get("members", []), list)


class TeamStore:
    """One team directory: config.json, tasks.json, mailbox/, transcripts/ (TM2, TM7)."""

    def __init__(self, directory: Path, on_notice: Callable[[str], None] | None = None):
        self.directory = directory
        self.name = directory.name
        self.on_notice = on_notice
        self.config = directory / "config.json"
        self.mailbox = Mailbox(directory / "mailbox", on_notice)
        self.board = TaskBoard(directory / "tasks.json", on_notice)
        self.transcripts = directory / "transcripts"

    @classmethod
    def at(cls, root: Path, name: str, on_notice: Callable[[str], None] | None = None) -> TeamStore:
        problem = validate_team_name(name)
        if problem:
            raise TeamError("invalid_name", f"团队名 {name} 不可用：{problem}。")
        return cls(root / name, on_notice)

    def exists(self) -> bool:
        return self.config.exists()

    def create(self, description: str = "") -> Team:
        """A new team; team_exists when the directory already has one (D13: the caller opens it instead)."""
        with file_lock(self.config):
            if self.config.exists():
                raise TeamError("team_exists", f"团队 {self.name} 已存在。")
            team = Team(self.name, description.strip())
            self.transcripts.mkdir(parents=True, exist_ok=True)
            self.mailbox.directory.mkdir(parents=True, exist_ok=True)
            atomic_write(self.config, team.to_dict())
        return team

    def _read(self) -> Team:
        """Unlike a mailbox, a broken config is left where it is, not replaced: the roster would be lost."""
        try:
            data = json.loads(read_text(self.config))
        except FileNotFoundError:
            raise TeamError("no_team", f"团队 {self.name} 不存在。") from None
        except (OSError, ValueError) as exc:
            raise TeamError("team_corrupt", f"团队 {self.name} 的记录无法读取（{self.config}：{exc}）。") from None
        if not _valid(data):
            raise TeamError("team_corrupt", f"团队 {self.name} 的记录格式不对（{self.config}）。")
        return Team.from_dict(data)

    def load(self) -> Team:
        return self._read()

    def modify(self, change: Callable[[Team], object]) -> Team:
        """TM7: re-read, change, write, all under the lock; another process's writes are never lost."""
        with file_lock(self.config):
            team = self._read()
            change(team)
            atomic_write(self.config, team.to_dict())
        return team

    def acquire(self, pid: int, session: str = "", project: str = "") -> Team:
        """TM4: the lead process takes the team; team_held while another live process has it."""
        def take(team: Team) -> None:
            holder = team.lead or {}
            other = int(holder.get("pid") or 0)
            if other and other != pid and process_alive(other):
                raise TeamError("team_held", f"团队 {team.name} 正由另一个 MossCode 进程（PID {other}）作为 Lead 使用。")
            team.lead = {"pid": pid, "session": session, "since": now()}
            if project:
                team.project = project
        return self.modify(take)

    def release(self, pid: int) -> Team:
        def drop(team: Team) -> None:
            if team.lead and int(team.lead.get("pid") or 0) == pid:
                team.lead = None
        return self.modify(drop)


def list_teams(root: Path) -> list[TeamStore]:
    """CM1: every directory with a config.json, by name."""
    if not root.is_dir():
        return []
    return [TeamStore(path) for path in sorted(root.iterdir()) if (path / "config.json").is_file()]
