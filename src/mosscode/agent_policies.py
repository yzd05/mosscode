"""Coding-agent policies wired from L2 modules into the L1 AgentLoop."""

from __future__ import annotations

from .agent import AgentPolicies, TranscriptRecords, needs_finish_check
from .messages import ToolCall, ToolResult
from .prompts import (
    active_skills,
    build_system_prompt,
    current_date,
    environment,
    hook_notification,
    mode_reminder,
    stable_prompt,
)
from .security import read_only_command
from .session import clear_record, collapse_record, compact_record, message_record, rewind_record, todo_record
from .skills import ActiveSkills
from .todo import todo_reminder
from .tool_core import ToolRegistry
from .tools import shell_label


def coding_needs_finish_check(call: ToolCall, result: ToolResult, registry: ToolRegistry) -> bool:
    return needs_finish_check(call, result, registry, read_only_command=read_only_command)


CODING_AGENT_POLICIES = AgentPolicies(
    stable_prompt=stable_prompt,
    build_system_prompt=build_system_prompt,
    environment=environment,
    mode_reminder=mode_reminder,
    active_skills_supplement=active_skills,
    hook_notification=hook_notification,
    todo_reminder=todo_reminder,
    shell_label=shell_label,
    today=current_date,
    needs_finish_check=coding_needs_finish_check,
    active_skills_factory=ActiveSkills,
    records=TranscriptRecords(
        message=message_record,
        clear=clear_record,
        collapse=collapse_record,
        compact=compact_record,
        rewind=rewind_record,
        todo=todo_record,
    ),
)


def coding_agent_policies() -> AgentPolicies:
    """Return the L2 coding-agent policy bundle for AgentLoop."""
    return CODING_AGENT_POLICIES
