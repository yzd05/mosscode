"""Static AST guard for the four-layer dependency DAG and protocol contracts.

Layers (dependencies may only point downward or within the same layer):
- L0 (AI / Protocol): message/event DTOs, media primitives, wire adapters, usage/reasoning/context/limits.
- L1 (Agent Core): agent loop, tool contract/execution, compaction, tool output budgeting.
- L2 (Coding Agent): sessions, permissions, config, prompts, tools, hooks, MCP, skills, sandbox, teams, wiring.
- L3 (Surfaces): CLI, TUI, subcommands, team pane UI/process runners, ACP surface.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mosscode import compaction, messages, prompts, providers, reasoning, tool_budget
from mosscode.compaction import RecordStore, clear_results, compact_history
from mosscode.config import ProviderConfig
from mosscode.context import ContextBudget
from mosscode.messages import (
    Completed,
    ContentBlock,
    InteractionRequest,
    Message,
    ToolResult,
)
from mosscode.permissions import ApprovalRequest, Operation, Question, QuestionRequest
from mosscode.providers import ProviderSettings
from mosscode.reasoning import ReasoningConfig
from mosscode.session import Session
from mosscode.tool_budget import RESULT_TOKENS, ResultStore, bound_result

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "mosscode"

L0_FILES = frozenset(
    {
        "catalog.py",
        "context.py",
        "images.py",
        "limits.py",
        "messages.py",
        "paths.py",
        "providers.py",
        "reasoning.py",
        "serialization.py",
        "usage.py",
    }
)

L1_FILES = frozenset(
    {
        "agent.py",
        "compaction.py",
        "tool_budget.py",
        "tools/base.py",
        "tools/execution.py",
    }
)

L2_FILES = frozenset(
    {
        "builtin_commands.py",
        "checkpoints.py",
        "command_env.py",
        "commands.py",
        "config.py",
        "conversation.py",
        "coordinator.py",
        "credentials.py",
        "follow.py",
        "goal.py",
        "loop_schedule.py",
        "mcp_client.py",
        "mcp_config.py",
        "mcp_stdio.py",
        "mcp_surface.py",
        "memory.py",
        "memory_extract.py",
        "mentions.py",
        "permissions.py",
        "plugins.py",
        "presets.py",
        "prompts.py",
        "repomap.py",
        "runtime_factory.py",
        "security.py",
        "session.py",
        "todo.py",
        "user_command.py",
        "web_search.py",
    }
)

L2_PACKAGES = (
    "agents/",
    "hooks/",
    "sandbox/",
    "skills/",
    "teams/",
    "tools/",
    "worktree/",
)

L3_FILES = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "appearance.py",
        "choice_list.py",
        "cli.py",
        "clipboard.py",
        "doctor.py",
        "key_command.py",
        "launcher.py",
        "member_ui.py",
        "models_command.py",
        "plugin_command.py",
        "sandbox_command.py",
        "scrollback.py",
        "setup.py",
        "teams/pane_app.py",
        "teams/process.py",
        "teams/view.py",
        "terminal_theme.py",
        "tui.py",
        "tui_views.py",
        "windows_inline.py",
    }
)

L3_PACKAGES = ("acp/",)

# Existing upward edges tracked for incremental dependency inversion.
# This set is strictly shrink-only: new upward edges fail CI, and resolved edges must be removed immediately.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Agent loop product-policy couplings (to be inverted into injected hooks/policies).
        ("agent.py", "credentials.py"),
        ("agent.py", "hooks/__init__.py"),
        ("agent.py", "prompts.py"),
        ("agent.py", "security.py"),
        ("agent.py", "session.py"),
        ("agent.py", "skills/__init__.py"),
        ("agent.py", "todo.py"),
        ("agent.py", "tools/__init__.py"),
        # Tool registry product-policy couplings (to be inverted into execution middleware).
        ("tools/base.py", "coordinator.py"),
        ("tools/base.py", "credentials.py"),
        ("tools/base.py", "hooks/__init__.py"),
        ("tools/base.py", "permissions.py"),
        ("tools/base.py", "security.py"),
        ("tools/base.py", "todo.py"),
        ("tools/base.py", "tools/files.py"),
        # Product-to-surface couplings pending extraction of shared constants/metadata.
        ("builtin_commands.py", "appearance.py"),
        ("builtin_commands.py", "plugin_command.py"),
        ("config.py", "setup.py"),
        ("tools/web.py", "launcher.py"),
    }
)


def _layer_of(rel: str) -> int | None:
    if rel in L0_FILES:
        return 0
    if rel in L1_FILES:
        return 1
    if rel in L3_FILES or rel.startswith(L3_PACKAGES):
        return 3
    if rel in L2_FILES or rel.startswith(L2_PACKAGES):
        return 2
    return None


def _module_index() -> tuple[dict[str, str], dict[str, tuple[str, bool]]]:
    mod_to_rel: dict[str, str] = {}
    rel_to_mod: dict[str, tuple[str, bool]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        mod = "mosscode" + ("." + ".".join(parts) if parts else "")
        mod_to_rel[mod] = rel
        rel_to_mod[rel] = (mod, is_pkg)
    return mod_to_rel, rel_to_mod


def _resolve_target(target: str, mod_to_rel: dict[str, str]) -> str | None:
    current = target
    while current:
        if current in mod_to_rel:
            return mod_to_rel[current]
        if "." not in current:
            return None
        current = current.rsplit(".", 1)[0]
    return None


def _internal_imports(
    rel: str,
    mod_to_rel: dict[str, str],
    rel_to_mod: dict[str, tuple[str, bool]],
) -> set[tuple[int, str]]:
    tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    cur_mod, is_pkg = rel_to_mod[rel]
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mosscode" or alias.name.startswith("mosscode."):
                    dst = _resolve_target(alias.name, mod_to_rel)
                    assert dst is not None, f"Unresolved internal import in {rel}:{node.lineno}: {alias.name}"
                    found.add((node.lineno, dst))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                pkg_parts = cur_mod.split(".") if is_pkg else cur_mod.split(".")[:-1]
                up = node.level - 1
                assert up <= len(pkg_parts), f"Relative import escapes package in {rel}:{node.lineno}"
                base = pkg_parts[: len(pkg_parts) - up]
                if node.module:
                    base.extend(node.module.split("."))
                target = ".".join(base)
            elif node.module and (node.module == "mosscode" or node.module.startswith("mosscode.")):
                target = node.module
            else:
                continue
            for alias in node.names:
                candidate = f"{target}.{alias.name}" if alias.name != "*" else target
                dst = _resolve_target(candidate if candidate in mod_to_rel else target, mod_to_rel)
                assert dst is not None, f"Unresolved internal import in {rel}:{node.lineno}: {candidate}"
                found.add((node.lineno, dst))
    return found


def test_every_module_has_an_explicit_layer() -> None:
    explicit_sets = [L0_FILES, L1_FILES, L2_FILES, L3_FILES]
    for idx, left in enumerate(explicit_sets):
        for right in explicit_sets[idx + 1 :]:
            overlap = left & right
            assert not overlap, f"Modules assigned to multiple layers: {sorted(overlap)}"

    all_files = {p.relative_to(SRC_ROOT).as_posix() for p in SRC_ROOT.rglob("*.py")}
    explicit_all = L0_FILES | L1_FILES | L2_FILES | L3_FILES
    stale_explicit = sorted(explicit_all - all_files)
    assert not stale_explicit, f"Layer map references missing files: {stale_explicit}"

    unmapped = sorted(rel for rel in all_files if _layer_of(rel) is None)
    assert not unmapped, f"Every src/mosscode module must be explicitly assigned to L0/L1/L2/L3: {unmapped}"


def test_no_unapproved_upward_layer_dependencies() -> None:
    mod_to_rel, rel_to_mod = _module_index()
    upward_lines: dict[tuple[str, str], list[int]] = {}
    for rel in sorted(rel_to_mod):
        src_layer = _layer_of(rel)
        assert src_layer is not None
        for lineno, dst in _internal_imports(rel, mod_to_rel, rel_to_mod):
            dst_layer = _layer_of(dst)
            assert dst_layer is not None
            if src_layer < dst_layer:
                upward_lines.setdefault((rel, dst), []).append(lineno)

    actual_edges = set(upward_lines)
    unexpected = sorted(actual_edges - ALLOWLIST)
    if unexpected:
        details = "\n".join(
            f"  L{_layer_of(src)} -> L{_layer_of(dst)}: {src}:{','.join(map(str, sorted(upward_lines[(src, dst)])))} -> {dst}"
            for src, dst in unexpected
        )
        pytest.fail(f"Forbidden upward layer dependencies found:\n{details}")

    stale = sorted(ALLOWLIST - actual_edges)
    assert not stale, f"Remove resolved entries from ALLOWLIST (it is shrink-only): {stale}"


def test_l0_and_decoupled_core_modules_have_zero_upward_debt() -> None:
    l0_debt = sorted((src, dst) for src, dst in ALLOWLIST if _layer_of(src) == 0)
    assert not l0_debt, f"L0 must have zero upward dependencies: {l0_debt}"

    decoupled = {"messages.py", "providers.py", "reasoning.py", "compaction.py", "tool_budget.py"}
    remaining = sorted((src, dst) for src, dst in ALLOWLIST if src in decoupled)
    assert not remaining, f"Decoupled foundation modules must not carry upward debt: {remaining}"


def test_concrete_types_satisfy_layer_protocols(tmp_path: Path) -> None:
    cfg = ProviderConfig(name="demo", protocol="anthropic-messages", model="claude-sonnet-4-6", api_key="test")
    assert isinstance(cfg, ReasoningConfig)
    assert isinstance(cfg, ProviderSettings)

    approval = ApprovalRequest(
        id="req-1",
        call_id="call-1",
        operation=Operation(tool="execute_command", target="echo hi"),
        reason="needs confirmation",
    )
    question = QuestionRequest(
        id="req-2",
        call_id="call-2",
        questions=(Question(question="Pick one?", header="Mode", options=(("A", "Option A"),)),),
    )
    assert isinstance(approval, InteractionRequest)
    assert isinstance(question, InteractionRequest)

    session = Session(tmp_path / "session")
    assert isinstance(session, ResultStore)
    assert isinstance(session, RecordStore)


def test_prompts_reexports_l0_prompt_request_dtos() -> None:
    assert prompts.Supplement is messages.Supplement
    assert prompts.PromptRequest is messages.PromptRequest
    assert prompts.as_prompt is messages.as_prompt
    assert prompts.PREFIX_KINDS is messages.PREFIX_KINDS
    assert providers.as_prompt is messages.as_prompt
    assert reasoning.ReasoningConfig is not None
    assert tool_budget.ResultStore is compaction.ResultStore


@pytest.mark.asyncio
async def test_tool_budget_and_compaction_run_with_protocol_stub(tmp_path: Path) -> None:
    class MemoryStore:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.persisted: list[tuple[str, str]] = []
            self.records: list[tuple[str, str]] = []

        def persist_result(self, call_id: str, text: str) -> Path:
            self.persisted.append((call_id, text))
            path = self.root / f"{call_id}.txt"
            path.write_text(text, encoding="utf-8")
            return path

        def write_record(self, text: str, name: str = "compact") -> Path:
            self.records.append((name, text))
            path = self.root / f"{name}.md"
            path.write_text(text, encoding="utf-8")
            return path

    class StubProvider:
        async def stream(self, req_messages, system, tools=(), *, allow_tools=True):
            yield Completed(
                Message("assistant", (ContentBlock("text", "<summary>Kept summary.</summary>"),)),
                "end_turn",
            )

    store = MemoryStore(tmp_path)
    assert isinstance(store, ResultStore)
    assert isinstance(store, RecordStore)

    oversized = "x" * (RESULT_TOKENS * 5)
    preview_text, details = bound_result("execute_command", "call-big", oversized, {}, store)
    assert details["budgeted"] is True
    assert details["persisted"] == str(tmp_path / "call-big.txt")
    assert "call-big.txt" in preview_text

    large_output = "y" * 4000
    history = [
        Message("user", (ContentBlock("text", "first question"),)),
        Message("assistant", (ContentBlock("text", "running grep"),)),
        Message("tool", (), tool_results=(ToolResult("call-clear", "grep", large_output),)),
        Message("user", (ContentBlock("text", "latest follow-up"),)),
    ]
    cleared = clear_results(history, cut=3, session=store)
    assert "call-clear" in cleared.results
    assert cleared.results["call-clear"].details["cleared"] is True
    assert cleared.results["call-clear"].details["persisted"] == str(tmp_path / "call-clear.txt")

    compacted = await compact_history(
        StubProvider(),
        history,
        ContextBudget.for_provider(128_000, 16_384),
        session=store,
        manual=True,
    )
    assert compacted.record == tmp_path / "compact.md"
    assert store.records and store.records[0][0] == "compact"
