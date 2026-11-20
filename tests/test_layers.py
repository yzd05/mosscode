"""Static AST guard for the four-layer dependency DAG and protocol contracts.

Layers (dependencies may only point downward or within the same layer):
- L0 (AI / Protocol): message/event DTOs, media primitives, wire adapters, usage/reasoning/context/limits.
- L1 (Agent Core): agent loop, tool contract/execution, compaction, tool output budgeting.
- L2 (Coding Agent): sessions, permissions, config, prompts, tools, hooks, MCP, skills, sandbox, teams, wiring.
- L3 (Surfaces): CLI, TUI, subcommands, team pane UI/process runners, ACP surface.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
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
from mosscode.providers import AnthropicProvider, ProviderSettings
from mosscode.reasoning import ReasoningConfig, thinking_options
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
# Enforced shrink-only against the target branch in pull-request CI: new or swapped edges fail CI,
# and resolved edges must be removed immediately.
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
        # Tool registry & execution couplings (including enclosing tools/__init__.py cold-import edge).
        ("tools/base.py", "coordinator.py"),
        ("tools/base.py", "credentials.py"),
        ("tools/base.py", "hooks/__init__.py"),
        ("tools/base.py", "permissions.py"),
        ("tools/base.py", "security.py"),
        ("tools/base.py", "todo.py"),
        ("tools/base.py", "tools/__init__.py"),
        ("tools/base.py", "tools/files.py"),
        ("tools/execution.py", "tools/__init__.py"),
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


def _module_index(
    src_root: Path = SRC_ROOT,
    root_package: str = "mosscode",
) -> tuple[dict[str, str], dict[str, tuple[str, bool]], set[str]]:
    mod_to_rel: dict[str, str] = {}
    rel_to_mod: dict[str, tuple[str, bool]] = {}
    namespace_pkgs: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        mod = root_package + ("." + ".".join(parts) if parts else "")
        mod_to_rel[mod] = rel
        rel_to_mod[rel] = (mod, is_pkg)
    for mod in list(mod_to_rel):
        parts = mod.split(".")
        for idx in range(1, len(parts)):
            prefix = ".".join(parts[:idx])
            if prefix not in mod_to_rel:
                namespace_pkgs.add(prefix)
    return mod_to_rel, rel_to_mod, namespace_pkgs


def _ancestor_package_files(target_mod: str, mod_to_rel: dict[str, str]) -> list[str]:
    """Enclosing nested package initializers executed on a cold import of target_mod.

    Skips the top-level package initializer (tested separately to stay free of top-level imports).
    """
    parts = target_mod.split(".")
    ancestors: list[str] = []
    for idx in range(2, len(parts)):
        pkg = ".".join(parts[:idx])
        if pkg in mod_to_rel:
            ancestors.append(mod_to_rel[pkg])
    return ancestors


def _record_module_and_ancestors(
    target_mod: str,
    lineno: int,
    rel: str,
    mod_to_rel: dict[str, str],
    namespace_pkgs: set[str],
    found: set[tuple[int, str]],
) -> None:
    if target_mod in mod_to_rel:
        found.add((lineno, mod_to_rel[target_mod]))
    elif target_mod not in namespace_pkgs:
        raise ValueError(f"Unresolved internal import in {rel}:{lineno}: {target_mod}")
    for anc_rel in _ancestor_package_files(target_mod, mod_to_rel):
        found.add((lineno, anc_rel))


def _resolve_relative_base(cur_mod: str, is_pkg: bool, level: int, rel: str, lineno: int) -> list[str]:
    pkg_parts = cur_mod.split(".") if is_pkg else cur_mod.split(".")[:-1]
    up = level - 1
    if up >= len(pkg_parts):
        raise ValueError(f"Relative import escapes package in {rel}:{lineno}")
    return pkg_parts[: len(pkg_parts) - up]


def _resolve_dynamic_import_call(
    node: ast.Call,
    cur_mod: str,
    is_pkg: bool,
    rel: str,
    src_layer: int | None,
    root_package: str,
    importlib_aliases: set[str],
    import_module_aliases: set[str],
    builtins_aliases: set[str],
    builtin_import_aliases: set[str],
    mod_to_rel: dict[str, str],
    namespace_pkgs: set[str],
    found: set[tuple[int, str]],
) -> None:
    func = node.func
    kind: str | None = None
    if isinstance(func, ast.Name):
        if func.id in import_module_aliases:
            kind = "import_module"
        elif func.id in builtin_import_aliases:
            kind = "__import__"
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in importlib_aliases and func.attr == "import_module":
            kind = "import_module"
        elif func.value.id in builtins_aliases and func.attr == "__import__":
            kind = "__import__"
    if kind is None:
        return

    name_arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
    if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
        if src_layer is not None and src_layer < 3:
            raise ValueError(f"Dynamic non-literal import is forbidden in guarded layer {rel}:{node.lineno}")
        return
    raw_target = name_arg.value

    if raw_target.startswith("."):
        if kind != "import_module":
            raise ValueError(f"Relative dynamic __import__ is unsupported in {rel}:{node.lineno}")
        pkg_arg = (
            node.args[1]
            if len(node.args) > 1
            else next((kw.value for kw in node.keywords if kw.arg == "package"), None)
        )
        if isinstance(pkg_arg, ast.Constant) and isinstance(pkg_arg.value, str):
            pkg_mod = pkg_arg.value
        elif isinstance(pkg_arg, ast.Name) and pkg_arg.id == "__package__":
            pkg_mod = cur_mod if is_pkg else cur_mod.rsplit(".", 1)[0]
        else:
            if src_layer is not None and src_layer < 3:
                raise ValueError(f"Relative import_module requires a literal package in {rel}:{node.lineno}")
            return
        if pkg_mod != root_package and not pkg_mod.startswith(f"{root_package}."):
            return
        dots = len(raw_target) - len(raw_target.lstrip("."))
        tail = raw_target[dots:]
        base = _resolve_relative_base(pkg_mod, True, dots, rel, node.lineno)
        if tail:
            base.extend(tail.split("."))
        target_mod = ".".join(base)
        _record_module_and_ancestors(target_mod, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
        return

    if raw_target == root_package or raw_target.startswith(f"{root_package}."):
        _record_module_and_ancestors(raw_target, node.lineno, rel, mod_to_rel, namespace_pkgs, found)


def _internal_imports(
    rel: str,
    mod_to_rel: dict[str, str],
    rel_to_mod: dict[str, tuple[str, bool]],
    namespace_pkgs: set[str],
    *,
    src_root: Path = SRC_ROOT,
    root_package: str = "mosscode",
    src_layer: int | None = None,
) -> set[tuple[int, str]]:
    tree = ast.parse((src_root / rel).read_text(encoding="utf-8"), filename=rel)
    cur_mod, is_pkg = rel_to_mod[rel]
    found: set[tuple[int, str]] = set()

    # Cold-importing this module executes its enclosing nested package initializers first.
    for anc_rel in _ancestor_package_files(cur_mod, mod_to_rel):
        found.add((1, anc_rel))

    importlib_aliases: set[str] = set()
    import_module_aliases: set[str] = set()
    builtins_aliases: set[str] = set()
    builtin_import_aliases: set[str] = {"__import__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "importlib":
                    importlib_aliases.add(local)
                elif alias.name == "builtins":
                    builtins_aliases.add(local)
                if alias.name == root_package or alias.name.startswith(f"{root_package}."):
                    _record_module_and_ancestors(alias.name, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_aliases.add(alias.asname or alias.name)
                continue
            if node.level == 0 and node.module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        builtin_import_aliases.add(alias.asname or alias.name)
                continue
            if node.level > 0:
                base = _resolve_relative_base(cur_mod, is_pkg, node.level, rel, node.lineno)
                if node.module:
                    base.extend(node.module.split("."))
                target = ".".join(base)
            elif node.module and (node.module == root_package or node.module.startswith(f"{root_package}.")):
                target = node.module
            else:
                continue

            if target not in mod_to_rel and target not in namespace_pkgs:
                raise ValueError(f"Unresolved internal import in {rel}:{node.lineno}: {target}")
            for anc_rel in _ancestor_package_files(target, mod_to_rel):
                found.add((node.lineno, anc_rel))
            for alias in node.names:
                candidate = f"{target}.{alias.name}" if alias.name != "*" else target
                if alias.name != "*" and (candidate in mod_to_rel or candidate in namespace_pkgs):
                    _record_module_and_ancestors(candidate, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
                elif target in mod_to_rel:
                    found.add((node.lineno, mod_to_rel[target]))
                else:
                    raise ValueError(f"Unresolved internal import in {rel}:{node.lineno}: {candidate}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _resolve_dynamic_import_call(
                node,
                cur_mod,
                is_pkg,
                rel,
                src_layer,
                root_package,
                importlib_aliases,
                import_module_aliases,
                builtins_aliases,
                builtin_import_aliases,
                mod_to_rel,
                namespace_pkgs,
                found,
            )

    return found


def _scan_upward_edges(
    src_root: Path,
    layer_of: Callable[[str], int | None],
    *,
    root_package: str = "mosscode",
) -> dict[tuple[str, str], list[int]]:
    mod_to_rel, rel_to_mod, namespace_pkgs = _module_index(src_root, root_package)
    upward_lines: dict[tuple[str, str], list[int]] = {}
    for rel in sorted(rel_to_mod):
        src_layer = layer_of(rel)
        if src_layer is None:
            raise ValueError(f"Module missing layer assignment: {rel}")
        for lineno, dst in _internal_imports(
            rel,
            mod_to_rel,
            rel_to_mod,
            namespace_pkgs,
            src_root=src_root,
            root_package=root_package,
            src_layer=src_layer,
        ):
            dst_layer = layer_of(dst)
            if dst_layer is None:
                raise ValueError(f"Imported module missing layer assignment: {dst}")
            if src_layer < dst_layer:
                upward_lines.setdefault((rel, dst), []).append(lineno)
    return upward_lines


def _verify_upward_edges(
    upward_lines: dict[tuple[str, str], list[int]],
    allowlist: frozenset[tuple[str, str]],
    layer_of: Callable[[str], int | None],
) -> None:
    actual_edges = set(upward_lines)
    unexpected = sorted(actual_edges - allowlist)
    if unexpected:
        details = "\n".join(
            f"  L{layer_of(src)} -> L{layer_of(dst)}: {src}:{','.join(map(str, sorted(upward_lines[(src, dst)])))} -> {dst}"
            for src, dst in unexpected
        )
        raise AssertionError(f"Forbidden upward layer dependencies found:\n{details}")

    stale = sorted(allowlist - actual_edges)
    if stale:
        raise AssertionError(f"Remove resolved entries from ALLOWLIST (it is shrink-only): {stale}")


def _extract_allowlist(source: str) -> frozenset[tuple[str, str]]:
    """Parse ALLOWLIST statically from a revision of this test file without executing it."""
    tree = ast.parse(source)
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "ALLOWLIST" and value is not None:
            inner = value.args[0] if isinstance(value, ast.Call) and len(value.args) == 1 else value
            entries = ast.literal_eval(inner)
            return frozenset((str(src), str(dst)) for src, dst in entries)
    raise ValueError("ALLOWLIST definition not found")


def _verify_allowlist_shrink_only(
    current: frozenset[tuple[str, str]],
    baseline: frozenset[tuple[str, str]],
) -> None:
    added = sorted(current - baseline)
    if added:
        raise AssertionError(f"ALLOWLIST is shrink-only; new or swapped entries are forbidden: {added}")


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


def test_root_package_init_has_no_top_level_imports() -> None:
    """mosscode/__init__.py is executed before every submodule import and must stay lazily initialized."""
    tree = ast.parse((SRC_ROOT / "__init__.py").read_text(encoding="utf-8"), filename="__init__.py")
    top_level_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not top_level_imports, "mosscode/__init__.py must not execute top-level imports"


def test_no_unapproved_upward_layer_dependencies() -> None:
    upward_lines = _scan_upward_edges(SRC_ROOT, _layer_of)
    _verify_upward_edges(upward_lines, ALLOWLIST, _layer_of)


def test_l0_and_decoupled_core_modules_have_zero_upward_debt() -> None:
    l0_debt = sorted((src, dst) for src, dst in ALLOWLIST if _layer_of(src) == 0)
    assert not l0_debt, f"L0 must have zero upward dependencies: {l0_debt}"

    decoupled = {"messages.py", "providers.py", "reasoning.py", "compaction.py", "tool_budget.py"}
    remaining = sorted((src, dst) for src, dst in ALLOWLIST if src in decoupled)
    assert not remaining, f"Decoupled foundation modules must not carry upward debt: {remaining}"


def test_layer_guard_scanner_accepts_valid_edges_and_rejects_violations(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "tools").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "messages.py").write_text("class Message:\n    pass\n", encoding="utf-8")
    (pkg / "reasoning.py").write_text(
        "import importlib\nfrom .messages import Message\nimportlib.import_module('json')\n",
        encoding="utf-8",
    )
    (pkg / "compaction.py").write_text("from .messages import Message\n", encoding="utf-8")
    (pkg / "config.py").write_text("from .messages import Message\n", encoding="utf-8")
    (pkg / "tools" / "__init__.py").write_text("from ..config import Message\n", encoding="utf-8")
    (pkg / "tools" / "base.py").write_text("class BaseTool:\n    pass\n", encoding="utf-8")

    layers = {
        "__init__.py": 3,
        "messages.py": 0,
        "reasoning.py": 0,
        "compaction.py": 1,
        "config.py": 2,
        "tools/__init__.py": 2,
        "tools/base.py": 1,
    }
    base_allowlist = frozenset({("tools/base.py", "tools/__init__.py")})

    # Clean graph passes, including valid relative import and external importlib.import_module.
    upward = _scan_upward_edges(pkg, layers.get, root_package="pkg")
    _verify_upward_edges(upward, base_allowlist, layers.get)

    # Stale allowlist entry fails.
    with pytest.raises(AssertionError, match="Remove resolved entries"):
        _verify_upward_edges(upward, base_allowlist | {("compaction.py", "config.py")}, layers.get)

    # Unapproved direct upward import fails.
    (pkg / "reasoning.py").write_text("from .config import Message\n", encoding="utf-8")
    upward_bad = _scan_upward_edges(pkg, layers.get, root_package="pkg")
    with pytest.raises(AssertionError, match=r"reasoning\.py.*config\.py"):
        _verify_upward_edges(upward_bad, base_allowlist, layers.get)

    # Nested package initializer is caught when L1 imports an L1 submodule inside an L2 package.
    (pkg / "reasoning.py").write_text("from .messages import Message\n", encoding="utf-8")
    (pkg / "compaction.py").write_text("from pkg.tools import base\n", encoding="utf-8")
    upward_nested = _scan_upward_edges(pkg, layers.get, root_package="pkg")
    assert ("compaction.py", "tools/__init__.py") in upward_nested
    with pytest.raises(AssertionError, match=r"compaction\.py.*tools/__init__\.py"):
        _verify_upward_edges(upward_nested, base_allowlist, layers.get)
    (pkg / "compaction.py").write_text("from .messages import Message\n", encoding="utf-8")

    # Literal lazy imports via importlib.import_module and __import__ are caught.
    for snippet in (
        "import importlib\nimportlib.import_module('pkg.config')\n",
        "from importlib import import_module as load\nload('.config', __package__)\n",
        "__import__('pkg.config')\n",
    ):
        (pkg / "reasoning.py").write_text(snippet, encoding="utf-8")
        upward_lazy = _scan_upward_edges(pkg, layers.get, root_package="pkg")
        assert ("reasoning.py", "config.py") in upward_lazy

    # Invalid module path is rejected instead of matching a valid prefix.
    (pkg / "reasoning.py").write_text("import pkg.messages.missing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unresolved internal import"):
        _scan_upward_edges(pkg, layers.get, root_package="pkg")

    # Relative import escaping package root is rejected.
    (pkg / "reasoning.py").write_text("from ..pkg.messages import Message\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Relative import escapes package"):
        _scan_upward_edges(pkg, layers.get, root_package="pkg")


def test_allowlist_shrink_only_baseline_check() -> None:
    baseline = _extract_allowlist(Path(__file__).read_text(encoding="utf-8"))
    assert baseline == ALLOWLIST

    # Removing an entry is allowed.
    shrunk = frozenset(sorted(baseline)[1:])
    _verify_allowlist_shrink_only(shrunk, baseline)

    # Swapping one entry for a different upward edge is rejected even if size stays equal.
    swapped = (baseline - {("agent.py", "prompts.py")}) | {("agent.py", "config.py")}
    with pytest.raises(AssertionError, match="shrink-only"):
        _verify_allowlist_shrink_only(swapped, baseline)


def test_concrete_types_satisfy_layer_protocols_and_signatures(tmp_path: Path) -> None:
    cfg = ProviderConfig(name="demo", protocol="anthropic-messages", model="claude-sonnet-4-6", api_key="test")
    assert isinstance(cfg, ReasoningConfig)
    assert isinstance(cfg, ProviderSettings)
    assert inspect.signature(ProviderConfig.credential).bind(cfg)
    assert cfg.credential() == "test"
    assert thinking_options(cfg, "low").options
    provider = AnthropicProvider(cfg, client=object())
    assert provider.config is cfg

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
    for req in (approval, question):
        assert isinstance(req.id, str)
        assert isinstance(req.call_id, str)
        assert isinstance(req.choices, tuple)
        assert isinstance(req.source, str)

    session = Session(tmp_path / "session")
    assert isinstance(session, ResultStore)
    assert isinstance(session, RecordStore)
    assert inspect.signature(Session.persist_result).bind(session, "call-1", "payload")
    assert inspect.signature(Session.write_record).bind(session, "summary")
    assert inspect.signature(Session.write_record).bind(session, "summary", "collapse")
    persisted = session.persist_result("call-1", "payload")
    assert persisted.read_text(encoding="utf-8") == "payload"
    record = session.write_record("summary")
    assert record.read_text(encoding="utf-8") == "summary"


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
