"""W1/W16: what MossCode records about each worktree it made; the record is what makes it safe to clean."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path


@dataclass
class WorktreeMeta:
    name: str  # As given, may contain /
    flat: str  # Directory name and the branch name after mosscode/
    path: str  # Absolute
    branch: str
    base: str  # Full SHA the worktree started from
    created: str  # ISO time
    manual: bool  # /worktree create: never cleaned automatically
    links: list[str] = field(default_factory=list)  # Relative to the worktree's top level
    copied: list[str] = field(default_factory=list)  # W8/W10 copies; not work of the sub-agent (W15)
    setup_done: bool = False
    in_use: dict | None = None  # {"pid": 1234, "task": "t3"} while a sub-agent works in it
    removing: bool = False  # Deletion started and did not finish (W14)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> WorktreeMeta:
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def load_meta(path: Path) -> WorktreeMeta | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WorktreeMeta.from_dict(data) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def save_meta(meta: WorktreeMeta, path: Path) -> None:
    """Atomic: a reader sees the old record or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(meta.to_json(), encoding="utf-8")
    os.replace(temporary, path)


def process_alive(pid: int) -> bool:
    """W16. Never os.kill(pid, 0) on Windows: there it terminates the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: it exists.
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
