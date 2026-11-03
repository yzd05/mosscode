from pathlib import Path

from ..permissions import PermissionPolicy
from .base import ToolRegistry
from .files import EditFile, FileState, ReadFile, WriteFile
from .plan import ExitPlanMode
from .process import ExecuteCommand
from .search import Glob, Grep
from .shell import Shell


def create_default_registry(
    root: Path | None = None,
    *,
    permission_mode: str | None = None,
    load_permissions: bool = True,
    permissions: PermissionPolicy | None = None,
    shell: Shell | str | None = "auto",
) -> ToolRegistry:
    root = Path.cwd() if root is None else root
    policy = permissions or PermissionPolicy(root, mode=permission_mode, load=load_permissions)
    state = FileState()  # Shared so edits can see what read_file returned.
    tools = (
        ReadFile(root, state),
        WriteFile(root, state),
        EditFile(root, state),
        ExecuteCommand(root, shell),
        Glob(root),
        Grep(root),
        ExitPlanMode(root),
    )
    return ToolRegistry(tools, permissions=policy)


def shell_label(registry: ToolRegistry | None) -> str | None:
    """The execute_command shell's name, e.g. "Git Bash", for prompts and the UI."""
    shell = getattr(registry.get("execute_command"), "shell", None) if registry is not None else None
    return shell.label if isinstance(shell, Shell) else None
