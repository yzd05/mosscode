"""The hook events, their condition fields, allowed actions and how they run."""

from dataclasses import dataclass

ACTIONS = ("command", "prompt", "http", "agent")
EXTERNAL = ("command", "http", "agent")
COMMON_FIELDS = ("event", "agent", "agent_type")
TOOL_FIELDS = ("tool", "path", "command", "worktree")  # Worktree is "" outside one.


@dataclass(frozen=True)
class EventSpec:
    fields: tuple[str, ...]
    actions: tuple[str, ...]
    background: bool = False  # Command, http and agent actions always run in the background.
    sync_only: bool = False  # pre_tool_use: async is not allowed.
    feedback: bool = False  # Command output may be injected.
    tool: bool = False  # args.<name> fields; MOSSCODE_TOOL_NAME and MOSSCODE_FILE_PATH.


EVENTS: dict[str, EventSpec] = {
    "session_start": EventSpec(("source",), ACTIONS, feedback=True),
    "session_end": EventSpec(("reason",), EXTERNAL),
    "turn_start": EventSpec(("prompt",), ACTIONS, feedback=True),
    "turn_end": EventSpec(("reason",), EXTERNAL),
    "pre_send": EventSpec(("iteration",), ACTIONS, background=True),
    "post_receive": EventSpec(("iteration", "tool_calls"), EXTERNAL, background=True),
    "pre_tool_use": EventSpec(TOOL_FIELDS, ACTIONS, sync_only=True, feedback=True, tool=True),
    "post_tool_use": EventSpec((*TOOL_FIELDS, "error", "code"), ACTIONS, feedback=True, tool=True),
    "permission_request": EventSpec((*TOOL_FIELDS, "risk"), EXTERNAL, background=True, tool=True),
    "compact": EventSpec(("trigger",), EXTERNAL),
    "error": EventSpec(("error",), EXTERNAL),
    # Fired on the side that started the sub-agent; agent_type here is the sub-agent's.
    "subagent_start": EventSpec(("task_id", "description", "background", "worktree"), ("command", "http", "prompt"),
                                feedback=True),
    "subagent_stop": EventSpec(("task_id", "status", "background", "worktree", "worktree_kept"), EXTERNAL,
                               background=True),
}

# The only events a fork sub-agent fires.
TOOL_EVENTS = frozenset(name for name, spec in EVENTS.items() if spec.tool)
ALL_FIELDS = frozenset((*COMMON_FIELDS, *(field for spec in EVENTS.values() for field in spec.fields)))


def fields_of(event: str) -> tuple[str, ...]:
    return (*COMMON_FIELDS, *EVENTS[event].fields)
