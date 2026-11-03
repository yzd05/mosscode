"""Git worktrees, so a sub-agent can change files without touching the main directory."""

from .changes import Changes, count_changes
from .git import WorktreeError, find_repo, read_head
from .manager import CleanReport, Created, Entry, Finished, WorktreeChanged, WorktreeManager
from .meta import WorktreeMeta, load_meta, process_alive
from .names import EPHEMERAL, branch_of, ephemeral_name, flatten, validate_name

__all__ = [
    "Changes",
    "CleanReport",
    "Created",
    "EPHEMERAL",
    "Entry",
    "Finished",
    "WorktreeChanged",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeMeta",
    "branch_of",
    "count_changes",
    "ephemeral_name",
    "find_repo",
    "flatten",
    "load_meta",
    "process_alive",
    "read_head",
    "validate_name",
]
