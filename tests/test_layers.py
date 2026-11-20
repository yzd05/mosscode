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
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from mosscode import (
    appearance,
    commands,
    compaction,
    config,
    credentials,
    launcher,
    messages,
    paths,
    permissions,
    plugin_command,
    plugins,
    prompts,
    providers,
    reasoning,
    redaction,
    security,
    setup,
    tool_budget,
    tool_core,
    tool_execution,
    version,
)
from mosscode.agent import ActiveSkillStore, AgentHookSink, AgentLoop
from mosscode.compaction import RecordStore, clear_results, compact_history
from mosscode.config import ProviderConfig
from mosscode.context import ContextBudget
from mosscode.hooks import HookEngine
from mosscode.messages import (
    Completed,
    ContentBlock,
    InteractionRequest,
    Message,
    RunStopped,
    ToolCall,
    ToolResult,
)
from mosscode.permissions import ApprovalRequest, Operation, PermissionPolicy, Question, QuestionRequest
from mosscode.providers import AnthropicProvider, ProviderSettings
from mosscode.reasoning import ReasoningConfig, thinking_options
from mosscode.session import Session
from mosscode.skills import ActiveSkills
from mosscode.todo import TodoList
from mosscode.tool_budget import RESULT_TOKENS, ResultStore, bound_result
from mosscode.tool_core import Params, TodoStore, Tool, ToolHookSink, ToolPermissions, ToolRegistry
from mosscode.tools import base as tools_base
from mosscode.tools import execution as tools_execution

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "mosscode"

L0_FILES = frozenset(
    {
        "catalog.py",
        "cli_providers.py",
        "context.py",
        "images.py",
        "limits.py",
        "messages.py",
        "paths.py",
        "providers.py",
        "reasoning.py",
        "redaction.py",
        "serialization.py",
        "usage.py",
        "version.py",
    }
)

L1_FILES = frozenset(
    {
        "agent.py",
        "compaction.py",
        "tool_budget.py",
        "tool_core.py",
        "tool_execution.py",
    }
)

L2_FILES = frozenset(
    {
        "agent_policies.py",
        "auth.py",
        "builtin_commands.py",
        "checkpoints.py",
        "command_env.py",
        "commands.py",
        "config.py",
        "conversation.py",
        "conversation_skills.py",
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
        "model_discovery.py",
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
    "builtin_cmds/",
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
        "config_command.py",
        "config_web.py",
        "doctor.py",
        "key_command.py",
        "launcher.py",
        "login_command.py",
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
        "tui_connect.py",
        "tui_hints.py",
        "tui_model_picker.py",
        "tui_panels.py",
        "tui_teams.py",
        "tui_views.py",
        "windows_inline.py",
    }
)

L3_PACKAGES = ("acp/",)

# Upward edges across layers must stay empty (enforced shrink-only in pull-request CI).
ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


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


_ScopeStack = list[tuple[dict[str, str | None], bool]]


def _target_names(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in node.elts:
            names.extend(_target_names(elt))
        return names
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return []


def _arg_names(args: ast.arguments) -> list[str]:
    names = [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg is not None:
        names.append(args.vararg.arg)
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return names


def _lookup_binding(scopes: _ScopeStack, name: str) -> str | None:
    for idx in range(len(scopes) - 1, -1, -1):
        bindings, is_class = scopes[idx]
        if is_class and idx != len(scopes) - 1:
            continue
        if name in bindings:
            return bindings[name]
    return None


def _resolve_loader_expr(expr: ast.expr, scopes: _ScopeStack) -> str | None:
    if isinstance(expr, ast.Name):
        return _lookup_binding(scopes, expr.id)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        base = _lookup_binding(scopes, expr.value.id)
        if base == "importlib" and expr.attr == "import_module":
            return "import_module"
        if base == "builtins" and expr.attr == "__import__":
            return "__import__"
    return None


def _collect_scope_bindings(
    stmts: list[ast.stmt],
    *,
    outer_scopes: _ScopeStack,
    param_names: Iterable[str] = (),
    is_class: bool = False,
) -> dict[str, str | None]:
    non_local: set[str] = set()
    scope_nodes: list[ast.AST] = []

    def _walk_scope(node: ast.AST) -> None:
        scope_nodes.append(node)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            non_local.update(node.names)
            return
        if isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            return
        for child in ast.iter_child_nodes(node):
            _walk_scope(child)

    for stmt in stmts:
        _walk_scope(stmt)

    bindings: dict[str, str | None] = {name: None for name in param_names}
    assigns: list[ast.Assign] = []

    for node in scope_nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    top = alias.name.split(".", 1)[0]
                    if top in non_local:
                        continue
                    if top in {"importlib", "builtins"}:
                        bindings[top] = top
                    else:
                        bindings.setdefault(top, None)
                else:
                    if alias.asname in non_local:
                        continue
                    if alias.name in {"importlib", "builtins"}:
                        bindings[alias.asname] = alias.name
                    else:
                        bindings.setdefault(alias.asname, None)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    if local in non_local:
                        continue
                    if alias.name == "import_module":
                        bindings[local] = "import_module"
                    else:
                        bindings.setdefault(local, None)
            elif node.level == 0 and node.module == "builtins":
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    if local in non_local:
                        continue
                    if alias.name == "__import__":
                        bindings[local] = "__import__"
                    else:
                        bindings.setdefault(local, None)
            else:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    if local not in non_local:
                        bindings.setdefault(local, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in non_local:
                bindings.setdefault(node.name, None)
        elif isinstance(node, ast.Assign):
            assigns.append(node)
            for target in node.targets:
                for name in _target_names(target):
                    if name not in non_local:
                        bindings.setdefault(name, None)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for name in _target_names(node.target):
                if name not in non_local:
                    bindings.setdefault(name, None)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _target_names(node.target):
                if name not in non_local:
                    bindings.setdefault(name, None)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                for name in _target_names(item.optional_vars):
                    if name not in non_local:
                        bindings.setdefault(name, None)
        elif isinstance(node, ast.ExceptHandler):
            if node.name and node.name not in non_local:
                bindings.setdefault(node.name, None)
        elif isinstance(node, ast.NamedExpr):
            if node.target.id not in non_local:
                bindings.setdefault(node.target.id, None)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name and node.name not in non_local:
                bindings.setdefault(node.name, None)
        elif isinstance(node, ast.MatchMapping):
            if node.rest and node.rest not in non_local:
                bindings.setdefault(node.rest, None)

    probe_scopes = [*outer_scopes, (bindings, is_class)]
    changed = True
    while changed:
        changed = False
        for assign in assigns:
            loader_kind = _resolve_loader_expr(assign.value, probe_scopes)
            if loader_kind is None:
                continue
            for target in assign.targets:
                if isinstance(target, ast.Name) and target.id not in non_local and bindings.get(target.id) is None:
                    bindings[target.id] = loader_kind
                    changed = True

    return bindings


def _extract_call_arg(node: ast.Call, pos: int, kw_name: str) -> ast.expr | None:
    if len(node.args) > pos:
        return node.args[pos]
    return next((kw.value for kw in node.keywords if kw.arg == kw_name), None)


def _is_local_globals_call(expr: ast.expr | None) -> bool:
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "globals"
        and not expr.args
        and not expr.keywords
    )


def _resolve_dynamic_import_call(
    node: ast.Call,
    cur_mod: str,
    is_pkg: bool,
    rel: str,
    src_layer: int | None,
    root_package: str,
    scopes: _ScopeStack,
    mod_to_rel: dict[str, str],
    namespace_pkgs: set[str],
    found: set[tuple[int, str]],
) -> None:
    kind = _resolve_loader_expr(node.func, scopes)
    if kind not in {"import_module", "__import__"}:
        return

    if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
        if src_layer is not None and src_layer < 3:
            raise ValueError(
                f"Dynamic import with unpacked arguments is forbidden in guarded layer {rel}:{node.lineno}"
            )
        return

    name_arg = _extract_call_arg(node, 0, "name")
    if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
        if src_layer is not None and src_layer < 3:
            raise ValueError(f"Dynamic non-literal import is forbidden in guarded layer {rel}:{node.lineno}")
        return
    raw_target = name_arg.value

    if kind == "import_module":
        if raw_target.startswith("."):
            pkg_arg = _extract_call_arg(node, 1, "package")
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
        return

    # kind == "__import__"
    if raw_target.startswith("."):
        raise ValueError(f"Relative dynamic __import__ with leading dots is unsupported in {rel}:{node.lineno}")

    level_arg = _extract_call_arg(node, 4, "level")
    if level_arg is None:
        level = 0
    elif (
        isinstance(level_arg, ast.Constant)
        and isinstance(level_arg.value, int)
        and not isinstance(level_arg.value, bool)
    ):
        level = level_arg.value
    else:
        if src_layer is not None and src_layer < 3:
            raise ValueError(f"Dynamic __import__ level must be a literal integer in guarded layer {rel}:{node.lineno}")
        return
    if level < 0:
        raise ValueError(f"Invalid negative __import__ level in {rel}:{node.lineno}")

    if level > 0:
        globals_arg = _extract_call_arg(node, 1, "globals")
        if not _is_local_globals_call(globals_arg):
            if src_layer is not None and src_layer < 3:
                raise ValueError(f"Relative __import__ requires globals() in guarded layer {rel}:{node.lineno}")
            return
        base = _resolve_relative_base(cur_mod, is_pkg, level, rel, node.lineno)
        if raw_target:
            base.extend(raw_target.split("."))
        target_mod = ".".join(base)
    else:
        if raw_target != root_package and not raw_target.startswith(f"{root_package}."):
            return
        target_mod = raw_target

    fromlist_arg = _extract_call_arg(node, 3, "fromlist")
    if fromlist_arg is None or (isinstance(fromlist_arg, ast.Constant) and fromlist_arg.value is None):
        _record_module_and_ancestors(target_mod, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
        return

    if isinstance(fromlist_arg, (ast.Tuple, ast.List, ast.Set)) and all(
        isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in fromlist_arg.elts
    ):
        items = [elt.value for elt in fromlist_arg.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
        if not items:
            _record_module_and_ancestors(target_mod, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
            return
        if target_mod not in mod_to_rel and target_mod not in namespace_pkgs:
            raise ValueError(f"Unresolved internal import in {rel}:{node.lineno}: {target_mod}")
        if target_mod != root_package:
            _record_module_and_ancestors(target_mod, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
        for item in items:
            candidate = f"{target_mod}.{item}" if item != "*" else target_mod
            if item != "*" and (candidate in mod_to_rel or candidate in namespace_pkgs):
                _record_module_and_ancestors(candidate, node.lineno, rel, mod_to_rel, namespace_pkgs, found)
            elif target_mod in mod_to_rel:
                found.add((node.lineno, mod_to_rel[target_mod]))
            else:
                raise ValueError(f"Unresolved internal import in {rel}:{node.lineno}: {candidate}")
        return

    if src_layer is not None and src_layer < 3:
        raise ValueError(
            f"Dynamic __import__ fromlist must be a literal sequence of strings in guarded layer {rel}:{node.lineno}"
        )
    if target_mod in mod_to_rel or target_mod in namespace_pkgs:
        _record_module_and_ancestors(target_mod, node.lineno, rel, mod_to_rel, namespace_pkgs, found)


class _InternalImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        cur_mod: str,
        is_pkg: bool,
        rel: str,
        src_layer: int | None,
        root_package: str,
        mod_to_rel: dict[str, str],
        namespace_pkgs: set[str],
        found: set[tuple[int, str]],
    ) -> None:
        self._cur_mod = cur_mod
        self._is_pkg = is_pkg
        self._rel = rel
        self._src_layer = src_layer
        self._root_package = root_package
        self._mod_to_rel = mod_to_rel
        self._namespace_pkgs = namespace_pkgs
        self._found = found
        self._scopes: _ScopeStack = [({"__import__": "__import__"}, False)]

    def visit_Module(self, node: ast.Module) -> None:
        mod_bindings = _collect_scope_bindings(node.body, outer_scopes=self._scopes, is_class=False)
        self._scopes.append((mod_bindings, False))
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for expr in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
            self.visit(expr)
        for arg in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *(a for a in (node.args.vararg, node.args.kwarg) if a is not None),
        ):
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        fn_bindings = _collect_scope_bindings(
            node.body,
            outer_scopes=self._scopes,
            param_names=_arg_names(node.args),
            is_class=False,
        )
        self._scopes.append((fn_bindings, False))
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expr in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
            self.visit(expr)
        self._scopes.append(({name: None for name in _arg_names(node.args)}, False))
        try:
            self.visit(node.body)
        finally:
            self._scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw)
        cls_bindings = _collect_scope_bindings(node.body, outer_scopes=self._scopes, is_class=True)
        self._scopes.append((cls_bindings, True))
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._scopes.pop()

    def _visit_comprehension(self, generators: list[ast.comprehension], exprs: list[ast.expr]) -> None:
        if generators:
            self.visit(generators[0].iter)
        comp_bindings: dict[str, str | None] = {}
        for gen in generators:
            for name in _target_names(gen.target):
                comp_bindings[name] = None
        self._scopes.append((comp_bindings, False))
        try:
            for idx, gen in enumerate(generators):
                if idx > 0:
                    self.visit(gen.iter)
                self.visit(gen.target)
                for cond in gen.ifs:
                    self.visit(cond)
            for expr in exprs:
                self.visit(expr)
        finally:
            self._scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == self._root_package or alias.name.startswith(f"{self._root_package}."):
                _record_module_and_ancestors(
                    alias.name, node.lineno, self._rel, self._mod_to_rel, self._namespace_pkgs, self._found
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level > 0:
            base = _resolve_relative_base(self._cur_mod, self._is_pkg, node.level, self._rel, node.lineno)
            if node.module:
                base.extend(node.module.split("."))
            target = ".".join(base)
        elif node.module and (node.module == self._root_package or node.module.startswith(f"{self._root_package}.")):
            target = node.module
        else:
            return

        if target not in self._mod_to_rel and target not in self._namespace_pkgs:
            raise ValueError(f"Unresolved internal import in {self._rel}:{node.lineno}: {target}")
        for anc_rel in _ancestor_package_files(target, self._mod_to_rel):
            self._found.add((node.lineno, anc_rel))
        for alias in node.names:
            candidate = f"{target}.{alias.name}" if alias.name != "*" else target
            if alias.name != "*" and (candidate in self._mod_to_rel or candidate in self._namespace_pkgs):
                _record_module_and_ancestors(
                    candidate, node.lineno, self._rel, self._mod_to_rel, self._namespace_pkgs, self._found
                )
            elif target in self._mod_to_rel:
                self._found.add((node.lineno, self._mod_to_rel[target]))
            else:
                raise ValueError(f"Unresolved internal import in {self._rel}:{node.lineno}: {candidate}")

    def visit_Call(self, node: ast.Call) -> None:
        _resolve_dynamic_import_call(
            node,
            self._cur_mod,
            self._is_pkg,
            self._rel,
            self._src_layer,
            self._root_package,
            self._scopes,
            self._mod_to_rel,
            self._namespace_pkgs,
            self._found,
        )
        self.generic_visit(node)


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

    visitor = _InternalImportVisitor(
        cur_mod=cur_mod,
        is_pkg=is_pkg,
        rel=rel,
        src_layer=src_layer,
        root_package=root_package,
        mod_to_rel=mod_to_rel,
        namespace_pkgs=namespace_pkgs,
        found=found,
    )
    visitor.visit(tree)
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
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
                if not value.args and not value.keywords:
                    return frozenset()
                if len(value.args) == 1 and not value.keywords:
                    entries = ast.literal_eval(value.args[0])
                    return frozenset((str(src), str(dst)) for src, dst in entries)
                raise ValueError("Unsupported ALLOWLIST frozenset call")
            entries = ast.literal_eval(value)
            return frozenset((str(src), str(dst)) for src, dst in entries)
    raise ValueError("ALLOWLIST definition not found")


def _verify_allowlist_shrink_only(
    current: frozenset[tuple[str, str]],
    baseline: frozenset[tuple[str, str]],
) -> None:
    added = sorted(current - baseline)
    if added:
        raise AssertionError(f"ALLOWLIST is shrink-only; new or swapped entries are forbidden: {added}")


def _import_time_imports(source: str, filename: str = "__init__.py") -> list[ast.AST]:
    """Collect import statements and dynamic import calls executed eagerly at module import time."""
    tree = ast.parse(source, filename=filename)
    found: list[ast.AST] = []

    def _walk_eager(node: ast.AST) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                _walk_eager(dec)
            for expr in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
                _walk_eager(expr)
            for arg in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                *(a for a in (node.args.vararg, node.args.kwarg) if a is not None),
            ):
                if arg.annotation is not None:
                    _walk_eager(arg.annotation)
            if node.returns is not None:
                _walk_eager(node.returns)
            return
        if isinstance(node, ast.Lambda):
            for expr in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
                _walk_eager(expr)
            return
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                found.append(node)
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"importlib", "builtins"}
                and node.func.attr in {"import_module", "__import__"}
            ):
                found.append(node)
        for child in ast.iter_child_nodes(node):
            _walk_eager(child)

    _walk_eager(tree)
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


def test_root_package_init_has_no_top_level_imports() -> None:
    """mosscode/__init__.py is executed before every submodule import and must stay lazily initialized."""
    init_source = (SRC_ROOT / "__init__.py").read_text(encoding="utf-8")
    eager_imports = _import_time_imports(init_source, filename="__init__.py")
    assert not eager_imports, "mosscode/__init__.py must not execute imports at module import time"

    # Guard must catch imports hidden in module-level control flow, class bodies, or eager dynamic calls,
    # while allowing deferred imports inside function bodies.
    assert not _import_time_imports("def __getattr__(name):\n    from .launcher import version\n    return version\n")
    for bad_snippet in (
        "if True:\n    from . import config\n",
        "try:\n    import mosscode.config\nexcept ImportError:\n    pass\n",
        "class Holder:\n    from . import config\n",
        "__import__('mosscode.config')\n",
        "def f(x=__import__('mosscode.config')):\n    return x\n",
    ):
        assert _import_time_imports(bad_snippet), f"Expected import-time violation for: {bad_snippet!r}"


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
    (pkg / "tools" / "ui.py").write_text("class ToolView:\n    pass\n", encoding="utf-8")

    layers = {
        "__init__.py": 3,
        "messages.py": 0,
        "reasoning.py": 0,
        "compaction.py": 1,
        "config.py": 2,
        "tools/__init__.py": 2,
        "tools/base.py": 1,
        "tools/ui.py": 3,
    }
    base_allowlist = frozenset({("tools/base.py", "tools/__init__.py")})

    # Clean graph passes, including valid relative import, scoped aliases, and external importlib.import_module.
    upward = _scan_upward_edges(pkg, layers.get, root_package="pkg")
    _verify_upward_edges(upward, base_allowlist, layers.get)

    # Scoped loader alias in one function does not falsely flag a same-named parameter/local in another function,
    # and valid level=0 / level=1 __import__ targeting same-layer modules is accepted.
    (pkg / "reasoning.py").write_text(
        "def helper():\n"
        "    from importlib import import_module as load\n"
        "    return load('json')\n"
        "\n"
        "def callback(load, arg):\n"
        "    return load(arg)\n"
        "\n"
        "__import__('pkg.messages', globals(), locals(), ('Message',), 0)\n"
        "__import__('messages', globals(), locals(), ('Message',), 1)\n"
        "__import__('', globals(), locals(), ('messages',), 1)\n",
        encoding="utf-8",
    )
    upward_scoped = _scan_upward_edges(pkg, layers.get, root_package="pkg")
    _verify_upward_edges(upward_scoped, base_allowlist, layers.get)

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

    # Literal lazy imports via importlib.import_module and __import__ (including relative level & fromlist) are caught.
    for snippet in (
        "import importlib\nimportlib.import_module('pkg.config')\n",
        "from importlib import import_module as load\nload('.config', __package__)\n",
        "def outer():\n    from importlib import import_module as load\n    def inner():\n        return load('pkg.config')\n",
        "__import__('pkg.config')\n",
        "__import__('config', globals(), level=1)\n",
        "__import__('', globals(), None, ('config',), 1)\n",
    ):
        (pkg / "reasoning.py").write_text(snippet, encoding="utf-8")
        upward_lazy = _scan_upward_edges(pkg, layers.get, root_package="pkg")
        assert ("reasoning.py", "config.py") in upward_lazy, f"Missed upward edge in: {snippet!r}"

    # __import__ loading an upward submodule via fromlist (keyword or positional) is caught,
    # and dynamic non-literal fromlist or non-globals() relative __import__ is rejected in guarded layers.
    for snippet in (
        "__import__('pkg.tools', fromlist=('ui',))\n",
        "__import__('pkg.tools', None, None, ('ui',))\n",
    ):
        (pkg / "config.py").write_text(snippet, encoding="utf-8")
        (pkg / "reasoning.py").write_text("from .messages import Message\n", encoding="utf-8")
        upward_fromlist = _scan_upward_edges(pkg, layers.get, root_package="pkg")
        assert ("config.py", "tools/ui.py") in upward_fromlist, f"Missed fromlist edge in: {snippet!r}"

    (pkg / "config.py").write_text("names = ('ui',)\n__import__('pkg.tools', fromlist=names)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fromlist must be a literal sequence"):
        _scan_upward_edges(pkg, layers.get, root_package="pkg")
    (pkg / "config.py").write_text("from .messages import Message\n", encoding="utf-8")

    (pkg / "reasoning.py").write_text("__import__('config', level=1)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Relative __import__ requires globals"):
        _scan_upward_edges(pkg, layers.get, root_package="pkg")

    # Invalid module path is rejected instead of matching a valid prefix.
    (pkg / "reasoning.py").write_text("import pkg.messages.missing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unresolved internal import"):
        _scan_upward_edges(pkg, layers.get, root_package="pkg")

    # Relative import escaping package root is rejected.
    (pkg / "reasoning.py").write_text("from ..pkg.messages import Message\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Relative import escapes package"):
        _scan_upward_edges(pkg, layers.get, root_package="pkg")


def test_allowlist_shrink_only_baseline_check() -> None:
    assert _extract_allowlist(Path(__file__).read_text(encoding="utf-8")) == ALLOWLIST
    assert _extract_allowlist("ALLOWLIST: frozenset[tuple[str, str]] = frozenset()\n") == frozenset()

    baseline = frozenset({("a.py", "b.py"), ("c.py", "d.py")})

    # Keeping the same set, removing an entry, or emptying ALLOWLIST are all allowed.
    _verify_allowlist_shrink_only(baseline, baseline)
    _verify_allowlist_shrink_only(frozenset({("c.py", "d.py")}), baseline)
    _verify_allowlist_shrink_only(frozenset(), baseline)

    # Adding a new entry or swapping an existing entry for a different upward edge is rejected.
    with pytest.raises(AssertionError, match="shrink-only"):
        _verify_allowlist_shrink_only(baseline | {("e.py", "f.py")}, baseline)

    swapped = (baseline - {("a.py", "b.py")}) | {("a.py", "x.py")}
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


def test_concrete_types_satisfy_l1_core_protocols_and_reexport_identities(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path, load=False)
    assert isinstance(policy, ToolPermissions)
    assert inspect.signature(PermissionPolicy.set_mode).bind(policy, "plan")
    assert inspect.signature(PermissionPolicy.authorize).bind(policy, "c1", object(), object(), gate=None, notify=None)
    assert inspect.signature(PermissionPolicy.operation).bind(policy, object(), object(), typed=True)
    assert inspect.signature(PermissionPolicy.decide).bind(policy, object(), read_only=True)

    todos = TodoList()
    assert isinstance(todos, TodoStore)
    assert inspect.signature(TodoList.rewind).bind(todos, 0)
    assert inspect.signature(TodoList.rebase).bind(todos)
    assert inspect.signature(TodoList.clear).bind(todos)

    hooks = HookEngine(tmp_path, enabled=False)
    assert isinstance(hooks, ToolHookSink)
    assert isinstance(hooks, AgentHookSink)
    assert inspect.signature(HookEngine.make_event).bind(hooks, "pre_send", fields={"iteration": "1"})
    assert inspect.signature(HookEngine.make_tool_event).bind(
        hooks, "pre_tool_use", "main", "read_file", {}, None, tmp_path, None, agent_type="", worktree="", nested=False
    )

    skills = ActiveSkills()
    assert isinstance(skills, ActiveSkillStore)
    assert inspect.signature(ActiveSkills.texts).bind(skills)
    assert inspect.signature(ActiveSkills.clear).bind(skills)

    # Compatibility re-exports must preserve exact object and ContextVar identities.
    assert tools_base.ToolRegistry is tool_core.ToolRegistry
    assert tools_base.Tool is tool_core.Tool
    assert tools_base.Params is tool_core.Params
    assert tools_base.CURRENT_CALL is tool_core.CURRENT_CALL
    assert tools_base.CURRENT_REGISTRY is tool_core.CURRENT_REGISTRY
    assert tools_execution.execute_batch is tool_execution.execute_batch
    assert tools_execution.partition_calls is tool_execution.partition_calls
    assert permissions.Decision is tool_core.Decision
    assert credentials.redact is redaction.redact
    assert credentials.redact_value is redaction.redact_value
    assert security.SecurityError is paths.SecurityError
    assert security.project_path is paths.project_path
    assert security.resolve_path is paths.resolve_path
    assert setup.OFFICIAL is config.OFFICIAL
    assert appearance.KEY_HELP is commands.KEY_HELP
    assert plugin_command.report is plugins.report
    assert launcher.version is version.version


@pytest.mark.asyncio
async def test_standalone_l1_agent_loop_executes_tool_without_l2_policies(tmp_path: Path) -> None:
    class EchoParams(Params):
        text: str

    class EchoTool(Tool):
        name = "echo"
        description = "Echo text back."
        params_model = EchoParams
        read_only = True

        async def execute(self, params: EchoParams):
            return f"echo:{params.text}", {}

    class TwoStepProvider:
        def __init__(self) -> None:
            self.seen: list[list[Message]] = []

        async def stream(self, req_messages, system, tools=(), *, allow_tools=True):
            self.seen.append(list(req_messages))
            if len(self.seen) == 1:
                call = ToolCall("call-1", "echo", json.dumps({"text": "hello"}))
                yield Completed(Message("assistant", (), tool_calls=(call,)), "tool_use")
            else:
                yield Completed(Message("assistant", (ContentBlock("text", "done: hello"),)), "end_turn")

        async def close(self) -> None:
            pass

    registry = ToolRegistry([EchoTool(tmp_path)])
    provider = TwoStepProvider()
    loop = AgentLoop(provider, "Minimal L1 system prompt.", registry)

    events = [event async for event in loop.run("say hello")]

    assert any(isinstance(e, Completed) and e.message.text == "done: hello" for e in events)
    assert any(isinstance(e, RunStopped) and e.reason == "completed" for e in events)
    assert len(provider.seen) == 2
    tool_msgs = [m for m in provider.seen[1] if m.role == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0].tool_results[0].output == "echo:hello"


def test_l1_cold_import_loads_no_l2_or_l3_modules() -> None:
    script = (
        "import json, sys; "
        "import mosscode.agent, mosscode.tool_core, mosscode.tool_execution; "
        "mods = sorted(m for m in sys.modules if m == 'mosscode' or m.startswith('mosscode.')); "
        "print(json.dumps(mods))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(proc.stdout)
    mod_to_rel, _, _ = _module_index()
    offenders: list[tuple[str, str, int | None]] = []
    for mod in loaded:
        if mod == "mosscode":
            continue
        rel = mod_to_rel.get(mod)
        layer = _layer_of(rel) if rel is not None else None
        if layer not in (0, 1):
            offenders.append((mod, rel or "<unknown>", layer))
    assert not offenders, f"Cold import of L1 core pulled in non-L0/L1 modules: {offenders}"
