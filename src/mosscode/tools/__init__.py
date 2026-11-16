from pathlib import Path

from ..permissions import PermissionPolicy
from .ask import AskUser
from .base import ToolRegistry
from .files import EditFile, FileState, ReadFile, WriteFile
from .plan import ExitPlanMode
from .process import ExecuteCommand
from .search import Glob, Grep
from .shell import Shell
from .todo import TodoWrite
from .web import WebFetch, WebSearch

WEB_TOOLS = ("web_fetch", "web_search")


def create_default_registry(
    root: Path | None = None,
    *,
    permission_mode: str | None = None,
    load_permissions: bool = True,
    permissions: PermissionPolicy | None = None,
    shell: Shell | str | None = "auto",
    web_search=None,
    todo: bool = True,
    ask: bool = False,
) -> ToolRegistry:
    """web_search (a WebSearchConfig) adds the search tool when its key is set; todo offers todo_write.

    ask offers ask_user: only the interactive main conversation has someone to answer.
    """
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
        WebFetch(root),
    )
    if todo:
        tools += (TodoWrite(root),)
    if ask:
        tools += (AskUser(root),)
    if web_search is not None:
        from ..web_search import create_backend

        backend = create_backend(web_search)
        if backend is not None:
            tools += (WebSearch(root, backend),)
    return ToolRegistry(tools, permissions=policy)


def web_tools(registry: ToolRegistry, root: Path) -> tuple:
    """The registry's web tools re-rooted for a worktree; copies share the page cache and search count."""
    from copy import copy

    tools = []
    for name in WEB_TOOLS:
        tool = registry.get(name)
        if tool is not None:
            tool = copy(tool)
            tool.root = root.resolve()
            tools.append(tool)
    return tuple(tools)


def shell_label(registry: ToolRegistry | None) -> str | None:
    """The execute_command shell's name, e.g. "Git Bash", for prompts and the UI."""
    tool = registry.get("execute_command") if registry is not None else None
    # With the sandbox on, the model's commands may run in another shell than the user's ! commands.
    shell = getattr(tool, "command_shell", None) or getattr(tool, "shell", None)
    return shell.label if isinstance(shell, Shell) else None
