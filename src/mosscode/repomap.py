"""Tree-sitter multi-language AST symbol indexer and Personalized PageRank repository map."""

from __future__ import annotations

import ast
import functools
import keyword
import math
import os
import re
import threading
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from .context import estimate_text
from .security import inside

MAX_CACHE_FILES = 4096
MAX_FILE_BYTES = 512 * 1024
MAX_REPO_FILES = 5000
MAX_SIGNATURE_CHARS = 160
PAGERANK_ALPHA = 0.85
PAGERANK_MAX_ITER = 40
PAGERANK_TOL = 1e-6

SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})
WORKTREE_DIRS = ((".mosscode", "worktrees"), (".claude", "worktrees"))
FOLD = os.name == "nt"

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".sh": "bash",
    ".bash": "bash",
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")

_JS_TS_KEYWORDS = frozenset(
    (
        "abstract any as async await bigint boolean break case catch class const constructor continue "
        "debugger declare default delete do else enum export extends false finally for from function "
        "get if implements import in infer instanceof interface is keyof let namespace never new null "
        "number object of override package private protected public readonly return satisfies set "
        "static string super switch symbol this throw true try type typeof undefined unknown var void "
        "while with yield"
    ).split()
)

_C_CPP_KEYWORDS = frozenset(
    (
        "NULL alignas alignof auto bool break case catch char char8_t char16_t char32_t class co_await "
        "co_return co_yield compl concept const const_cast consteval constexpr constinit continue "
        "decltype default delete do double dynamic_cast else enum explicit export extern false final "
        "float for friend goto if inline int int8_t int16_t int32_t int64_t long mutable namespace new "
        "noexcept nullptr operator override private protected public register reinterpret_cast requires "
        "return short signed size_t sizeof ssize_t static static_assert static_cast std string struct "
        "switch template this thread_local throw true try typedef typeid typename uint8_t uint16_t "
        "uint32_t uint64_t union unsigned using vector virtual void volatile wchar_t while"
    ).split()
)

KEYWORDS: dict[str, frozenset[str]] = {
    "python": frozenset({*keyword.kwlist, *keyword.softkwlist, "self", "cls", "None", "True", "False"}),
    "javascript": _JS_TS_KEYWORDS,
    "typescript": _JS_TS_KEYWORDS,
    "tsx": _JS_TS_KEYWORDS,
    "go": frozenset(
        (
            "append bool break byte cap case chan close complex64 complex128 const continue copy "
            "default defer delete else error fallthrough false float32 float64 for func go goto if "
            "import int int8 int16 int32 int64 interface iota len make map new nil package panic "
            "print println range recover return rune select string struct switch true type uint "
            "uint8 uint16 uint32 uint64 uintptr var"
        ).split()
    ),
    "rust": frozenset(
        (
            "Box Err None Ok Option Result Self Some String Vec as async await bool break char const "
            "continue crate dyn else enum extern f32 f64 false fn for i8 i16 i32 i64 i128 if impl in "
            "isize let loop match mod move mut pub ref return self static str struct super trait true "
            "type u8 u16 u32 u64 u128 unsafe use usize where while"
        ).split()
    ),
    "java": frozenset(
        (
            "Object Override String abstract assert boolean break byte case catch char class const "
            "continue default do double else enum exports extends false final finally float for goto "
            "if implements import instanceof int interface long module native new null open opens "
            "package permits private protected provides public record requires return sealed short "
            "static strictfp super switch synchronized this throw throws to transient transitive true "
            "try uses var void volatile while with yield"
        ).split()
    ),
    "c": _C_CPP_KEYWORDS,
    "cpp": _C_CPP_KEYWORDS,
    "bash": frozenset(
        (
            "break case cd continue coproc declare do done echo elif else esac eval exec exit export "
            "false fi for function if in local printf pwd read readonly return select set shift source "
            "test then time trap true typeset unset until while"
        ).split()
    ),
}

_QUERY_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "into", "this", "that", "what", "where", "when", "how"}
)

_IDENTIFIER_NODE_TYPES = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "field_identifier",
        "namespace_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
    }
)


@dataclass(frozen=True)
class SymbolDef:
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    depth: int = 0


@dataclass(frozen=True)
class RepoMapResult:
    text: str
    files_indexed: int
    files_shown: int
    symbols_total: int
    symbols_shown: int
    tokens: int
    truncated: bool


_CacheKey = tuple[str, int, int]
_CacheValue = tuple[tuple[SymbolDef, ...], frozenset[str], dict[str, int]]
_SYMBOL_CACHE: OrderedDict[_CacheKey, _CacheValue] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def clear_symbol_cache() -> None:
    with _CACHE_LOCK:
        _SYMBOL_CACHE.clear()


def language_for_path(path: Path) -> str | None:
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower())


def is_supported_file(path: Path) -> bool:
    return language_for_path(path) is not None


@functools.cache
def _load_ts_language(lang_name: str):
    """Return a cached tree_sitter.Language for lang_name, or None if unavailable."""
    try:
        import tree_sitter

        if lang_name == "python":
            import tree_sitter_python as mod

            ptr = mod.language()
        elif lang_name == "javascript":
            import tree_sitter_javascript as mod

            ptr = mod.language()
        elif lang_name == "typescript":
            import tree_sitter_typescript as mod

            ptr = mod.language_typescript()
        elif lang_name == "tsx":
            import tree_sitter_typescript as mod

            ptr = mod.language_tsx()
        elif lang_name == "go":
            import tree_sitter_go as mod

            ptr = mod.language()
        elif lang_name == "rust":
            import tree_sitter_rust as mod

            ptr = mod.language()
        elif lang_name == "java":
            import tree_sitter_java as mod

            ptr = mod.language()
        elif lang_name == "c":
            import tree_sitter_c as mod

            ptr = mod.language()
        elif lang_name == "cpp":
            import tree_sitter_cpp as mod

            ptr = mod.language()
        elif lang_name == "bash":
            import tree_sitter_bash as mod

            ptr = mod.language()
        else:
            return None
        return tree_sitter.Language(ptr)
    except ImportError:
        return None


def _cap_signature(raw: str) -> str:
    collapsed = " ".join(raw.split()).strip()
    if len(collapsed) > MAX_SIGNATURE_CHARS:
        return collapsed[: MAX_SIGNATURE_CHARS - 1].rstrip() + "…"
    return collapsed


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _header_before_body(
    source: bytes,
    node,
    body_node=None,
    *,
    start_byte: int | None = None,
    keep_colon: bool = False,
) -> str:
    start = node.start_byte if start_byte is None else start_byte
    if body_node is not None:
        end = body_node.start_byte
        if keep_colon:
            for ch in node.children:
                if ch.type == ":" and ch.end_byte <= body_node.start_byte:
                    end = ch.end_byte
        raw = source[start:end].decode("utf-8", "replace")
    else:
        full = source[start : node.end_byte].decode("utf-8", "replace")
        raw = full.splitlines()[0] if full else ""
    raw = raw.rstrip()
    if not keep_colon and raw.endswith("{"):
        raw = raw[:-1].rstrip()
    return _cap_signature(raw)


def _skip_leading_decorators_byte(node) -> int:
    for child in node.children:
        if child.type in {"decorator", "comment", "attribute_item", "outer_attribute_item"}:
            continue
        return child.start_byte
    return node.start_byte


def _java_sig_start_byte(node) -> int:
    """Skip leading @Annotation lines inside Java modifiers while keeping public/static/etc."""
    for child in node.children:
        if child.type == "comment":
            continue
        if child.type == "modifiers":
            for mod_child in child.children:
                if mod_child.type in {"marker_annotation", "annotation", "comment"}:
                    continue
                return mod_child.start_byte
            continue
        return child.start_byte
    return node.start_byte


# Language-specific Tree-sitter symbol extractors ------------------------------------------


def _extract_python_ts(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()

    def visit_block(block_node, parent_cls: str | None, depth: int) -> None:
        for child in block_node.named_children:
            outer = child
            inner = child
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition")
                if inner is None:
                    continue
            if inner.type == "class_definition":
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_cls}.{name}" if parent_cls else name
                body = inner.child_by_field_name("body")
                sig = _header_before_body(source, inner, body, keep_colon=True)
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="class",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=sig,
                        depth=depth,
                    )
                )
                if body is not None and depth == 0:
                    visit_block(body, qname, depth + 1)
            elif inner.type == "function_definition":
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_cls}.{name}" if parent_cls else name
                body = inner.child_by_field_name("body")
                sig = _header_before_body(source, inner, body, keep_colon=True)
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="method" if parent_cls else "function",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=sig,
                        depth=depth,
                    )
                )
            elif depth == 0 and inner.type == "type_alias_statement":
                left = inner.child_by_field_name("left")
                ident = _first_ident_node(left) if left is not None else None
                if ident is not None:
                    name = _node_text(source, ident)
                    def_spans.add((ident.start_byte, ident.end_byte))
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="type",
                            start_line=outer.start_point[0] + 1,
                            end_line=outer.end_point[0] + 1,
                            signature=_header_before_body(source, inner),
                            depth=0,
                        )
                    )
            elif depth == 0 and inner.type == "expression_statement" and inner.named_child_count == 1:
                assign = inner.named_children[0]
                if assign.type == "assignment":
                    left = assign.child_by_field_name("left")
                    if left is not None and left.type == "identifier":
                        name = _node_text(source, left)
                        if name and not (name.startswith("__") and name.endswith("__")):
                            def_spans.add((left.start_byte, left.end_byte))
                            defs.append(
                                SymbolDef(
                                    name=name,
                                    qualified_name=name,
                                    kind="constant",
                                    start_line=inner.start_point[0] + 1,
                                    end_line=inner.end_point[0] + 1,
                                    signature=_header_before_body(source, assign),
                                    depth=0,
                                )
                            )

    visit_block(root_node, None, 0)
    return defs, def_spans


def _parse_python_ast_fallback(text: str, filename: str = "<memory>") -> _CacheValue:
    """Stdlib ast fallback when tree_sitter_python is unavailable."""
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError:
        return (), frozenset(), {}
    lines = text.splitlines()
    defs: list[SymbolDef] = []
    defined_names: set[str] = set()

    def header_from_ast(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        start_idx = node.lineno - 1
        end_idx = (node.body[0].lineno - 1) if node.body else node.lineno
        if end_idx <= start_idx:
            raw = lines[start_idx] if start_idx < len(lines) else ""
            colon = raw.find(":")
            if colon >= 0:
                raw = raw[: colon + 1]
            return _cap_signature(raw)
        chunk = []
        for idx in range(start_idx, min(end_idx + 1, len(lines))):
            line = lines[idx]
            comment_pos = line.find("#")
            if comment_pos >= 0:
                line = line[:comment_pos]
            chunk.append(line.strip())
            if line.rstrip().endswith(":"):
                break
        joined = " ".join(part for part in chunk if part)
        return _cap_signature(joined)

    def visit_stmts(stmts: Sequence[ast.stmt], parent_cls: str | None, depth: int) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                start_line = min([stmt.lineno, *(d.lineno for d in stmt.decorator_list)])
                end_line = stmt.end_lineno or stmt.lineno
                qname = f"{parent_cls}.{stmt.name}" if parent_cls else stmt.name
                defined_names.add(stmt.name)
                defs.append(
                    SymbolDef(
                        name=stmt.name,
                        qualified_name=qname,
                        kind="class",
                        start_line=start_line,
                        end_line=end_line,
                        signature=header_from_ast(stmt),
                        depth=depth,
                    )
                )
                if depth == 0:
                    visit_stmts(stmt.body, qname, depth + 1)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start_line = min([stmt.lineno, *(d.lineno for d in stmt.decorator_list)])
                end_line = stmt.end_lineno or stmt.lineno
                qname = f"{parent_cls}.{stmt.name}" if parent_cls else stmt.name
                defined_names.add(stmt.name)
                defs.append(
                    SymbolDef(
                        name=stmt.name,
                        qualified_name=qname,
                        kind="method" if parent_cls else "function",
                        start_line=start_line,
                        end_line=end_line,
                        signature=header_from_ast(stmt),
                        depth=depth,
                    )
                )
            elif depth == 0 and isinstance(stmt, ast.TypeAlias):
                name = getattr(stmt.name, "id", "")
                if name:
                    defined_names.add(name)
                    raw = lines[stmt.lineno - 1] if stmt.lineno - 1 < len(lines) else f"type {name}"
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="type",
                            start_line=stmt.lineno,
                            end_line=stmt.end_lineno or stmt.lineno,
                            signature=_cap_signature(raw),
                            depth=0,
                        )
                    )
            elif depth == 0 and isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                if isinstance(target, ast.Name) and not (target.id.startswith("__") and target.id.endswith("__")):
                    defined_names.add(target.id)
                    raw = lines[stmt.lineno - 1] if stmt.lineno - 1 < len(lines) else target.id
                    defs.append(
                        SymbolDef(
                            name=target.id,
                            qualified_name=target.id,
                            kind="constant",
                            start_line=stmt.lineno,
                            end_line=stmt.end_lineno or stmt.lineno,
                            signature=_cap_signature(raw),
                            depth=0,
                        )
                    )
            elif depth == 0 and isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                if not (name.startswith("__") and name.endswith("__")):
                    defined_names.add(name)
                    raw = lines[stmt.lineno - 1] if stmt.lineno - 1 < len(lines) else name
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="constant",
                            start_line=stmt.lineno,
                            end_line=stmt.end_lineno or stmt.lineno,
                            signature=_cap_signature(raw),
                            depth=0,
                        )
                    )

    visit_stmts(tree.body, None, 0)
    kw = KEYWORDS["python"]
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            tok = node.id
            if len(tok) >= 2 and tok not in kw and _IDENT_RE.match(tok):
                counts[tok] += 1
        elif isinstance(node, ast.Attribute):
            tok = node.attr
            if len(tok) >= 2 and tok not in kw and _IDENT_RE.match(tok):
                counts[tok] += 1
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                for tok in (alias.name, alias.asname):
                    if tok and len(tok) >= 2 and tok not in kw and _IDENT_RE.match(tok):
                        counts[tok] += 1
    return tuple(defs), frozenset(counts), dict(counts)


def _first_ident_node(node):
    if node is None:
        return None
    if node.type in _IDENTIFIER_NODE_TYPES:
        return node
    for child in node.named_children:
        found = _first_ident_node(child)
        if found is not None:
            return found
    return None


def _extract_js_ts(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()

    def visit_class_members(body_node, parent_name: str) -> None:
        for member in body_node.named_children:
            if member.type in {"method_definition", "abstract_method_signature", "method_signature"}:
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                if not name:
                    continue
                def_spans.add((name_node.start_byte, name_node.end_byte))
                body = member.child_by_field_name("body")
                sig = _header_before_body(source, member, body, start_byte=_skip_leading_decorators_byte(member))
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=f"{parent_name}.{name}",
                        kind="method",
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        signature=sig,
                        depth=1,
                    )
                )
            elif member.type in {"public_field_definition", "field_definition"}:
                val = member.child_by_field_name("value")
                name_node = member.child_by_field_name("name")
                if name_node is not None and val is not None and val.type in {"arrow_function", "function_expression"}:
                    name = _node_text(source, name_node)
                    def_spans.add((name_node.start_byte, name_node.end_byte))
                    fn_body = val.child_by_field_name("body")
                    sig = _header_before_body(source, member, fn_body, start_byte=_skip_leading_decorators_byte(member))
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=f"{parent_name}.{name}",
                            kind="method",
                            start_line=member.start_point[0] + 1,
                            end_line=member.end_point[0] + 1,
                            signature=sig,
                            depth=1,
                        )
                    )

    def visit_top(container) -> None:
        for child in container.named_children:
            outer = child
            inner = child
            if child.type == "export_statement":
                decl = child.child_by_field_name("declaration") or child.child_by_field_name("value")
                if decl is None:
                    continue
                inner = decl
            sig_start = _skip_leading_decorators_byte(outer)
            if inner.type in {"class_declaration", "abstract_class_declaration"}:
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                body = inner.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="class",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body, start_byte=sig_start),
                        depth=0,
                    )
                )
                if body is not None:
                    visit_class_members(body, name)
            elif inner.type == "interface_declaration":
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                body = inner.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="interface",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body, start_byte=sig_start),
                        depth=0,
                    )
                )
                if body is not None:
                    visit_class_members(body, name)
            elif inner.type == "enum_declaration":
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                body = inner.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="enum",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body, start_byte=sig_start),
                        depth=0,
                    )
                )
            elif inner.type == "type_alias_declaration":
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="type",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, start_byte=sig_start),
                        depth=0,
                    )
                )
            elif inner.type in {"function_declaration", "generator_function_declaration"}:
                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                body = inner.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="function",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body, start_byte=sig_start),
                        depth=0,
                    )
                )
            elif inner.type in {"lexical_declaration", "variable_declaration"}:
                is_const = any(ch.type == "const" for ch in inner.children)
                for decl in inner.named_children:
                    if decl.type != "variable_declarator":
                        continue
                    name_node = decl.child_by_field_name("name")
                    val = decl.child_by_field_name("value")
                    if name_node is None or name_node.type != "identifier":
                        continue
                    name = _node_text(source, name_node)
                    if val is not None and val.type in {"arrow_function", "function_expression"}:
                        def_spans.add((name_node.start_byte, name_node.end_byte))
                        fn_body = val.child_by_field_name("body")
                        defs.append(
                            SymbolDef(
                                name=name,
                                qualified_name=name,
                                kind="function",
                                start_line=outer.start_point[0] + 1,
                                end_line=outer.end_point[0] + 1,
                                signature=_header_before_body(source, outer, fn_body, start_byte=sig_start),
                                depth=0,
                            )
                        )
                    elif is_const or outer.type == "export_statement":
                        def_spans.add((name_node.start_byte, name_node.end_byte))
                        defs.append(
                            SymbolDef(
                                name=name,
                                qualified_name=name,
                                kind="constant",
                                start_line=outer.start_point[0] + 1,
                                end_line=outer.end_point[0] + 1,
                                signature=_header_before_body(source, outer, start_byte=sig_start),
                                depth=0,
                            )
                        )
            elif inner.type in {"internal_module", "module"}:
                body = inner.child_by_field_name("body")
                if body is not None:
                    visit_top(body)

    visit_top(root_node)
    return defs, def_spans


def _go_receiver_type(source: bytes, receiver_node) -> str:
    if receiver_node is None:
        return ""
    found = _first_ident_by_type(receiver_node, "type_identifier")
    return _node_text(source, found) if found is not None else ""


def _first_ident_by_type(node, target_type: str):
    if node is None:
        return None
    if node.type == target_type:
        return node
    for child in node.named_children:
        res = _first_ident_by_type(child, target_type)
        if res is not None:
            return res
    return None


def _extract_go(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()
    for child in root_node.named_children:
        if child.type == "type_declaration":
            for spec in child.named_children:
                if spec.type not in {"type_spec", "type_alias"}:
                    continue
                name_node = spec.child_by_field_name("name")
                type_node = spec.child_by_field_name("type")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                if type_node is not None and type_node.type == "struct_type":
                    body = next((c for c in type_node.named_children if c.type == "field_declaration_list"), None)
                    sig = _cap_signature(f"type {name} struct")
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="struct",
                            start_line=spec.start_point[0] + 1,
                            end_line=spec.end_point[0] + 1,
                            signature=sig if body is not None else _header_before_body(source, spec),
                            depth=0,
                        )
                    )
                elif type_node is not None and type_node.type == "interface_type":
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="interface",
                            start_line=spec.start_point[0] + 1,
                            end_line=spec.end_point[0] + 1,
                            signature=_cap_signature(f"type {name} interface"),
                            depth=0,
                        )
                    )
                    for elem in type_node.named_children:
                        if elem.type in {"method_elem", "method_spec"}:
                            m_name_node = elem.child_by_field_name("name")
                            if m_name_node is not None:
                                m_name = _node_text(source, m_name_node)
                                def_spans.add((m_name_node.start_byte, m_name_node.end_byte))
                                defs.append(
                                    SymbolDef(
                                        name=m_name,
                                        qualified_name=f"{name}.{m_name}",
                                        kind="method",
                                        start_line=elem.start_point[0] + 1,
                                        end_line=elem.end_point[0] + 1,
                                        signature=_header_before_body(source, elem),
                                        depth=1,
                                    )
                                )
                else:
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="type",
                            start_line=spec.start_point[0] + 1,
                            end_line=spec.end_point[0] + 1,
                            signature=_cap_signature(f"type {_header_before_body(source, spec)}"),
                            depth=0,
                        )
                    )
        elif child.type == "function_declaration":
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _node_text(source, name_node)
            def_spans.add((name_node.start_byte, name_node.end_byte))
            body = child.child_by_field_name("body")
            defs.append(
                SymbolDef(
                    name=name,
                    qualified_name=name,
                    kind="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    signature=_header_before_body(source, child, body),
                    depth=0,
                )
            )
        elif child.type == "method_declaration":
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _node_text(source, name_node)
            def_spans.add((name_node.start_byte, name_node.end_byte))
            recv = _go_receiver_type(source, child.child_by_field_name("receiver"))
            qname = f"{recv}.{name}" if recv else name
            body = child.child_by_field_name("body")
            defs.append(
                SymbolDef(
                    name=name,
                    qualified_name=qname,
                    kind="method",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    signature=_header_before_body(source, child, body),
                    depth=0,
                )
            )
        elif child.type in {"const_declaration", "var_declaration"}:
            kw_prefix = "const" if child.type == "const_declaration" else "var"
            for spec in child.named_children:
                if spec.type not in {"const_spec", "var_spec"}:
                    continue
                name_node = spec.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="constant",
                        start_line=spec.start_point[0] + 1,
                        end_line=spec.end_point[0] + 1,
                        signature=_cap_signature(f"{kw_prefix} {_header_before_body(source, spec)}"),
                        depth=0,
                    )
                )
    return defs, def_spans


def _extract_rust(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()

    def visit_items(container, parent_name: str | None, depth: int) -> None:
        for child in container.named_children:
            sig_start = _skip_leading_decorators_byte(child)
            if child.type in {"struct_item", "enum_item"}:
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_name}.{name}" if parent_name else name
                body = child.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="struct" if child.type == "struct_item" else "enum",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        signature=_header_before_body(source, child, body, start_byte=sig_start),
                        depth=depth,
                    )
                )
            elif child.type == "trait_item":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_name}.{name}" if parent_name else name
                body = child.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="trait",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        signature=_header_before_body(source, child, body, start_byte=sig_start),
                        depth=depth,
                    )
                )
                if body is not None and depth == 0:
                    visit_items(body, name, depth + 1)
            elif child.type == "impl_item" and depth == 0:
                type_node = child.child_by_field_name("type")
                trait_node = child.child_by_field_name("trait")
                type_ident = _first_ident_by_type(type_node, "type_identifier")
                if type_ident is None:
                    continue
                type_name = _node_text(source, type_ident)
                trait_ident = _first_ident_by_type(trait_node, "type_identifier") if trait_node is not None else None
                impl_qname = (
                    f"impl {_node_text(source, trait_ident)} for {type_name}" if trait_ident else f"impl {type_name}"
                )
                body = child.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=type_name,
                        qualified_name=impl_qname,
                        kind="impl",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        signature=_header_before_body(source, child, body, start_byte=sig_start),
                        depth=0,
                    )
                )
                if body is not None:
                    visit_items(body, type_name, 1)
            elif child.type in {"function_item", "function_signature_item"}:
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_name}.{name}" if parent_name else name
                body = child.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="method" if parent_name else "function",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        signature=_header_before_body(source, child, body, start_byte=sig_start),
                        depth=depth,
                    )
                )
            elif depth == 0 and child.type == "type_item":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(source, name_node)
                    def_spans.add((name_node.start_byte, name_node.end_byte))
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="type",
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            signature=_header_before_body(source, child, start_byte=sig_start),
                            depth=0,
                        )
                    )
            elif depth == 0 and child.type in {"const_item", "static_item"}:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(source, name_node)
                    def_spans.add((name_node.start_byte, name_node.end_byte))
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="constant",
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            signature=_header_before_body(source, child, start_byte=sig_start),
                            depth=0,
                        )
                    )
            elif depth == 0 and child.type == "mod_item":
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name_node is not None:
                    name = _node_text(source, name_node)
                    def_spans.add((name_node.start_byte, name_node.end_byte))
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="module",
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            signature=_header_before_body(source, child, body, start_byte=sig_start),
                            depth=0,
                        )
                    )
                    if body is not None:
                        visit_items(body, name, 1)

    visit_items(root_node, None, 0)
    return defs, def_spans


def _extract_java(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()

    def visit_type_body(body_node, parent_name: str, depth: int) -> None:
        for member in body_node.named_children:
            if member.type in {"method_declaration", "constructor_declaration"}:
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                body = member.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=f"{parent_name}.{name}",
                        kind="method",
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        signature=_header_before_body(source, member, body, start_byte=_java_sig_start_byte(member)),
                        depth=depth,
                    )
                )
            elif member.type == "field_declaration":
                mods = next((c for c in member.named_children if c.type == "modifiers"), None)
                mod_text = _node_text(source, mods) if mods is not None else ""
                if "static" in mod_text and "final" in mod_text:
                    for decl in member.named_children:
                        if decl.type == "variable_declarator":
                            name_node = decl.child_by_field_name("name")
                            if name_node is not None:
                                name = _node_text(source, name_node)
                                def_spans.add((name_node.start_byte, name_node.end_byte))
                                defs.append(
                                    SymbolDef(
                                        name=name,
                                        qualified_name=f"{parent_name}.{name}",
                                        kind="constant",
                                        start_line=member.start_point[0] + 1,
                                        end_line=member.end_point[0] + 1,
                                        signature=_header_before_body(
                                            source, member, start_byte=_java_sig_start_byte(member)
                                        ),
                                        depth=depth,
                                    )
                                )
            elif depth == 1 and member.type in {
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
            }:
                visit_type_decl(member, parent_name, depth)

    def visit_type_decl(node, parent_name: str | None, depth: int) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(source, name_node)
        def_spans.add((name_node.start_byte, name_node.end_byte))
        qname = f"{parent_name}.{name}" if parent_name else name
        body = node.child_by_field_name("body")
        kind_map = {
            "class_declaration": "class",
            "record_declaration": "class",
            "interface_declaration": "interface",
            "annotation_type_declaration": "interface",
            "enum_declaration": "enum",
        }
        defs.append(
            SymbolDef(
                name=name,
                qualified_name=qname,
                kind=kind_map.get(node.type, "class"),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=_header_before_body(source, node, body, start_byte=_java_sig_start_byte(node)),
                depth=depth,
            )
        )
        if body is not None and depth == 0:
            visit_type_body(body, qname, depth + 1)

    for child in root_node.named_children:
        if child.type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }:
            visit_type_decl(child, None, 0)
    return defs, def_spans


def _unwrap_c_fn_declarator(node):
    """Find (function_declarator, leaf_name_node) inside nested C/C++ declarators."""
    cur = node
    fn_decl = None
    while cur is not None:
        if cur.type == "function_declarator":
            fn_decl = cur
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            break
        cur = nxt
    if fn_decl is None:
        return None, cur
    leaf = fn_decl.child_by_field_name("declarator")
    while leaf is not None and leaf.child_by_field_name("declarator") is not None:
        leaf = leaf.child_by_field_name("declarator")
    return fn_decl, leaf


def _c_cpp_leaf_names(source: bytes, leaf_node, parent_cls: str | None) -> tuple[str, str, tuple[int, int] | None]:
    if leaf_node is None:
        return "", "", None
    if leaf_node.type == "qualified_identifier":
        name_part = leaf_node.child_by_field_name("name")
        scope_part = leaf_node.child_by_field_name("scope")
        short = _node_text(source, name_part) if name_part is not None else _node_text(source, leaf_node)
        scope = _node_text(source, scope_part).replace("::", ".") if scope_part is not None else ""
        span = (name_part.start_byte, name_part.end_byte) if name_part is not None else None
        qname = f"{scope}.{short}" if scope else short
        return short, qname, span
    short = _node_text(source, leaf_node).strip()
    qname = f"{parent_cls}.{short}" if parent_cls else short
    return short, qname, (leaf_node.start_byte, leaf_node.end_byte)


def _extract_c_cpp(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()

    def visit_container(container, parent_cls: str | None, depth: int) -> None:
        for child in container.named_children:
            outer = child
            inner = child
            if child.type == "template_declaration":
                decl = next(
                    (
                        c
                        for c in child.named_children
                        if c.type
                        in {
                            "class_specifier",
                            "struct_specifier",
                            "function_definition",
                            "declaration",
                            "field_declaration",
                            "alias_declaration",
                        }
                    ),
                    None,
                )
                if decl is None:
                    continue
                inner = decl
            if inner.type in {"preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else"}:
                visit_container(inner, parent_cls, depth)
                continue
            if inner.type in {"namespace_definition", "linkage_specification"} and depth == 0:
                body = inner.child_by_field_name("body")
                if body is not None:
                    visit_container(body, None, 0)
                continue
            if inner.type in {"class_specifier", "struct_specifier", "union_specifier"}:
                name_node = inner.child_by_field_name("name")
                body = inner.child_by_field_name("body")
                if name_node is None or body is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_cls}.{name}" if parent_cls else name
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="class" if inner.type == "class_specifier" else "struct",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body),
                        depth=depth,
                    )
                )
                if depth == 0:
                    visit_container(body, qname, 1)
            elif inner.type == "enum_specifier":
                name_node = inner.child_by_field_name("name")
                body = inner.child_by_field_name("body")
                if name_node is None or body is None:
                    continue
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                qname = f"{parent_cls}.{name}" if parent_cls else name
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=qname,
                        kind="enum",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body),
                        depth=depth,
                    )
                )
            elif inner.type == "function_definition":
                fn_decl, leaf = _unwrap_c_fn_declarator(inner.child_by_field_name("declarator"))
                if fn_decl is None or leaf is None:
                    continue
                short, qname, span = _c_cpp_leaf_names(source, leaf, parent_cls)
                if not short:
                    continue
                if span is not None:
                    def_spans.add(span)
                body = inner.child_by_field_name("body")
                defs.append(
                    SymbolDef(
                        name=short,
                        qualified_name=qname,
                        kind="method" if (parent_cls or "." in qname) else "function",
                        start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1,
                        signature=_header_before_body(source, outer, body),
                        depth=depth,
                    )
                )
            elif inner.type in {"declaration", "field_declaration"}:
                decl_field = inner.child_by_field_name("declarator")
                fn_decl, leaf = _unwrap_c_fn_declarator(decl_field)
                if fn_decl is not None and leaf is not None:
                    short, qname, span = _c_cpp_leaf_names(source, leaf, parent_cls)
                    if short:
                        if span is not None:
                            def_spans.add(span)
                        defs.append(
                            SymbolDef(
                                name=short,
                                qualified_name=qname,
                                kind="method" if (parent_cls or "." in qname) else "function",
                                start_line=outer.start_point[0] + 1,
                                end_line=outer.end_point[0] + 1,
                                signature=_header_before_body(source, outer),
                                depth=depth,
                            )
                        )
            elif depth == 0 and inner.type == "type_definition":
                decl_field = inner.child_by_field_name("declarator")
                type_field = inner.child_by_field_name("type")
                ident = _first_ident_by_type(decl_field, "type_identifier") or _first_ident_node(decl_field)
                if ident is not None:
                    name = _node_text(source, ident)
                    def_spans.add((ident.start_byte, ident.end_byte))
                    body = type_field.child_by_field_name("body") if type_field is not None else None
                    sig = (
                        _cap_signature(f"typedef {type_field.type.removesuffix('_specifier')} {name}")
                        if body is not None
                        else _header_before_body(source, inner)
                    )
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="type",
                            start_line=inner.start_point[0] + 1,
                            end_line=inner.end_point[0] + 1,
                            signature=sig,
                            depth=0,
                        )
                    )
                    if body is not None and type_field.type in {"struct_specifier", "class_specifier"}:
                        visit_container(body, name, 1)
            elif depth == 0 and inner.type == "alias_declaration":
                name_node = inner.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(source, name_node)
                    def_spans.add((name_node.start_byte, name_node.end_byte))
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualified_name=name,
                            kind="type",
                            start_line=outer.start_point[0] + 1,
                            end_line=outer.end_point[0] + 1,
                            signature=_header_before_body(source, outer),
                            depth=0,
                        )
                    )
            elif depth == 0 and inner.type in {"preproc_def", "preproc_function_def"}:
                name_node = inner.child_by_field_name("name")
                val_node = inner.child_by_field_name("value")
                if name_node is None:
                    continue
                if inner.type == "preproc_def" and (val_node is None or not _node_text(source, val_node).strip()):
                    continue  # Skip empty include-guard #define FOO_H.
                name = _node_text(source, name_node)
                def_spans.add((name_node.start_byte, name_node.end_byte))
                end_line = max(inner.start_point[0] + 1, inner.end_point[0] + (1 if inner.end_point[1] > 0 else 0))
                defs.append(
                    SymbolDef(
                        name=name,
                        qualified_name=name,
                        kind="function" if inner.type == "preproc_function_def" else "constant",
                        start_line=inner.start_point[0] + 1,
                        end_line=end_line,
                        signature=_header_before_body(source, inner),
                        depth=0,
                    )
                )

    visit_container(root_node, None, 0)
    return defs, def_spans


def _extract_bash(source: bytes, root_node) -> tuple[list[SymbolDef], set[tuple[int, int]]]:
    defs: list[SymbolDef] = []
    def_spans: set[tuple[int, int]] = set()
    for child in root_node.named_children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _node_text(source, name_node)
            def_spans.add((name_node.start_byte, name_node.end_byte))
            body = child.child_by_field_name("body")
            defs.append(
                SymbolDef(
                    name=name,
                    qualified_name=name,
                    kind="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    signature=_header_before_body(source, child, body),
                    depth=0,
                )
            )
    return defs, def_spans


def _extract_references(
    source: bytes, root_node, lang_name: str, def_spans: set[tuple[int, int]]
) -> tuple[frozenset[str], dict[str, int]]:
    kw = KEYWORDS.get(lang_name, frozenset())
    counts: Counter[str] = Counter()
    is_bash = lang_name == "bash"
    stack = [root_node]
    while stack:
        node = stack.pop()
        if is_bash:
            if node.type == "command_name" and node.named_child_count >= 1:
                w = node.named_children[0]
                if w.type == "word" and (w.start_byte, w.end_byte) not in def_spans:
                    tok = _node_text(source, w)
                    if len(tok) >= 2 and tok not in kw and _IDENT_RE.match(tok):
                        counts[tok] += 1
                continue
            if node.type == "variable_name" and (node.start_byte, node.end_byte) not in def_spans:
                tok = _node_text(source, node)
                if len(tok) >= 2 and tok not in kw and _IDENT_RE.match(tok):
                    counts[tok] += 1
                continue
        elif node.type in _IDENTIFIER_NODE_TYPES and (node.start_byte, node.end_byte) not in def_spans:
            tok = _node_text(source, node)
            if len(tok) >= 2 and tok not in kw and _IDENT_RE.match(tok):
                counts[tok] += 1
                continue
        if node.named_child_count:
            stack.extend(reversed(node.named_children))
    return frozenset(counts), dict(counts)


def parse_source_symbols(source: str | bytes, lang_name: str, *, filename: str = "<memory>") -> _CacheValue:
    """Parse AST symbol definitions and referenced identifier counts from source."""
    data = source.encode("utf-8") if isinstance(source, str) else source
    if b"\x00" in data:
        return (), frozenset(), {}
    ts_lang = _load_ts_language(lang_name)
    if ts_lang is None:
        if lang_name == "python":
            text = source if isinstance(source, str) else data.decode("utf-8", "replace")
            return _parse_python_ast_fallback(text, filename)
        return (), frozenset(), {}

    from tree_sitter import Parser

    root = Parser(ts_lang).parse(data).root_node
    if lang_name == "python":
        defs, def_spans = _extract_python_ts(data, root)
    elif lang_name in {"javascript", "typescript", "tsx"}:
        defs, def_spans = _extract_js_ts(data, root)
    elif lang_name == "go":
        defs, def_spans = _extract_go(data, root)
    elif lang_name == "rust":
        defs, def_spans = _extract_rust(data, root)
    elif lang_name == "java":
        defs, def_spans = _extract_java(data, root)
    elif lang_name in {"c", "cpp"}:
        defs, def_spans = _extract_c_cpp(data, root)
    elif lang_name == "bash":
        defs, def_spans = _extract_bash(data, root)
    else:
        return (), frozenset(), {}

    refs, ref_counts = _extract_references(data, root, lang_name, def_spans)
    return tuple(defs), refs, ref_counts


def _parse_file_cached(path: Path) -> _CacheValue:
    lang = language_for_path(path)
    if lang is None:
        return (), frozenset(), {}
    resolved = path.resolve()
    try:
        st = resolved.stat()
    except OSError:
        return (), frozenset(), {}
    key: _CacheKey = (os.path.normcase(str(resolved)), st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        cached = _SYMBOL_CACHE.get(key)
        if cached is not None:
            _SYMBOL_CACHE.move_to_end(key)
            return cached
    try:
        data = resolved.read_bytes()
    except OSError:
        return (), frozenset(), {}
    if b"\x00" in data:
        return (), frozenset(), {}
    parsed = parse_source_symbols(data, lang, filename=str(resolved))
    with _CACHE_LOCK:
        _SYMBOL_CACHE[key] = parsed
        _SYMBOL_CACHE.move_to_end(key)
        while len(_SYMBOL_CACHE) > MAX_CACHE_FILES:
            _SYMBOL_CACHE.popitem(last=False)
    return parsed


def parse_file_symbols(path: Path) -> tuple[tuple[SymbolDef, ...], frozenset[str]]:
    """Extract (defs, refs) for `path`, backed by the process-wide `(path, mtime_ns, size)` LRU cache."""
    defs, refs, _ = _parse_file_cached(path)
    return defs, refs


# Workspace discovery (.gitignore + boundary + permission filter) --------------------------


class _Ignore:
    """Layered .gitignore evaluation matching tools/search_worker.py."""

    def __init__(self, layers=()):
        self.layers = tuple(layers)

    def extend(self, directory: Path) -> _Ignore:
        try:
            text = (directory / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self
        spec = GitIgnoreSpec.from_lines((text.casefold() if FOLD else text).splitlines())
        patterns = tuple(pattern for pattern in spec.patterns if pattern.include is not None)
        return _Ignore((*self.layers, (directory, patterns))) if patterns else self

    def ignored(self, path: Path, is_dir: bool) -> bool:
        result = False
        for base, patterns in self.layers:
            try:
                relative = path.relative_to(base).as_posix()
            except ValueError:
                continue
            if FOLD:
                relative = relative.casefold()
            if is_dir:
                relative += "/"
            for pattern in patterns:
                if pattern.regex.fullmatch(relative):
                    result = pattern.include
        return result


def _inside_worktrees(path: Path) -> bool:
    parts = path.parts
    return any(parts[i : i + 2] in WORKTREE_DIRS for i in range(len(parts) - 1))


def _initial_ignore(target_dir: Path, base: Path | None) -> _Ignore:
    ignore = _Ignore()
    if base is None or not target_dir.is_relative_to(base) or _inside_worktrees(target_dir):
        return ignore.extend(target_dir)
    parts = target_dir.relative_to(base).parts
    for depth in range(len(parts) + 1):
        ignore = ignore.extend(base.joinpath(*parts[:depth]))
    return ignore


def _is_non_binary_source(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            chunk = stream.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


def collect_repo_files(
    target: Path,
    root: Path | None = None,
    *,
    allow_file: Callable[[Path], bool] | None = None,
    max_files: int = MAX_REPO_FILES,
) -> list[Path]:
    """Discover supported code files under `target`, honoring `.gitignore`, size/binary caps, and `allow_file`."""
    boundary = target.resolve()
    base = root.resolve() if root is not None else None
    if boundary.is_file():
        if not is_supported_file(boundary):
            return []
        if allow_file is not None and not allow_file(boundary):
            return []
        try:
            if boundary.stat().st_size > MAX_FILE_BYTES:
                return []
        except OSError:
            return []
        return [boundary] if _is_non_binary_source(boundary) else []
    if not boundary.is_dir():
        raise FileNotFoundError(str(target))

    target_in_worktree = _inside_worktrees(boundary)
    ignores = {boundary: _initial_ignore(boundary, base)}
    candidates: list[tuple[int, str, Path]] = []

    for directory, folders, names in os.walk(boundary):
        current = Path(directory)
        ignore = ignores.pop(current, None) or _Ignore()
        kept: list[str] = []
        for name in sorted(folders):
            sub = current / name
            if name in SKIP_DIRS:
                continue
            if not target_in_worktree and name == "worktrees" and current.name in {".mosscode", ".claude"}:
                continue
            try:
                if sub.is_symlink() or sub.is_junction():
                    continue
                resolved_sub = sub.resolve()
            except (OSError, RuntimeError):
                continue
            if not inside(boundary, resolved_sub) or ignore.ignored(sub, True):
                continue
            kept.append(name)
            ignores[sub] = ignore.extend(sub)
        folders[:] = kept

        for name in sorted(names):
            file_path = current / name
            if not is_supported_file(file_path):
                continue
            if ignore.ignored(file_path, False):
                continue
            try:
                resolved_file = file_path.resolve()
                if not inside(boundary, resolved_file) or not resolved_file.is_file():
                    continue
                if allow_file is not None and not allow_file(resolved_file):
                    continue
                st = resolved_file.stat()
            except (OSError, RuntimeError):
                continue
            if st.st_size > MAX_FILE_BYTES:
                continue
            rel_key = resolved_file.relative_to(boundary).as_posix()
            candidates.append((st.st_mtime_ns, rel_key, resolved_file))

    if len(candidates) > max_files:
        candidates.sort(key=lambda item: (-item[0], item[1]))
    else:
        candidates.sort(key=lambda item: item[1])

    selected: list[Path] = []
    for _, _, resolved_file in candidates:
        if len(selected) >= max_files:
            break
        if _is_non_binary_source(resolved_file):
            selected.append(resolved_file)
    return selected


# Personalized PageRank & Token-Budget Map Packing -----------------------------------------


def _subtokens(identifier: str) -> set[str]:
    expanded = _CAMEL_BOUNDARY_RE.sub(lambda m: f"{m.group(1) or m.group(3)}_{m.group(2) or m.group(4)}", identifier)
    return {part.lower() for part in expanded.split("_") if len(part) >= 2}


def tokenize_query(query: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return `(exact_lower_tokens, subtokens)` extracted from `query`."""
    exact: set[str] = set()
    subs: set[str] = set()
    for match in _QUERY_TOKEN_RE.finditer(query):
        tok = match.group(0)
        low = tok.lower()
        if len(low) < 2 or low in _QUERY_STOPWORDS:
            continue
        exact.add(low)
        for sub in _subtokens(tok):
            if sub not in _QUERY_STOPWORDS:
                subs.add(sub)
    return frozenset(exact), frozenset(subs | exact)


def _is_specific_symbol(sym: str) -> bool:
    return ("_" in sym or any(c.isupper() for c in sym[1:]) or len(sym) >= 8) and not sym.startswith("_")


def personalized_pagerank(
    nodes: Sequence[str],
    edges: Mapping[str, Mapping[str, float]],
    personalization: Mapping[str, float],
    *,
    alpha: float = PAGERANK_ALPHA,
    max_iter: int = PAGERANK_MAX_ITER,
    tol: float = PAGERANK_TOL,
) -> dict[str, float]:
    """Sparse power-iteration Personalized PageRank with dangling-node mass redistribution."""
    node_list = list(dict.fromkeys(nodes))
    n = len(node_list)
    if n == 0:
        return {}
    node_set = set(node_list)

    pers_raw = {u: max(0.0, float(personalization.get(u, 0.0))) for u in node_list}
    pers_sum = sum(pers_raw.values())
    if pers_sum <= 0.0 or not math.isfinite(pers_sum):
        pers = {u: 1.0 / n for u in node_list}
    else:
        pers = {u: pers_raw[u] / pers_sum for u in node_list}

    if n == 1:
        return {node_list[0]: 1.0}

    norm_out: dict[str, list[tuple[str, float]]] = {}
    dangling: list[str] = []
    for u in node_list:
        raw_targets = edges.get(u)
        if not raw_targets:
            dangling.append(u)
            continue
        valid = [
            (v, float(w)) for v, w in raw_targets.items() if v != u and v in node_set and w > 0.0 and math.isfinite(w)
        ]
        total_w = sum(w for _, w in valid)
        if total_w <= 0.0:
            dangling.append(u)
        else:
            norm_out[u] = [(v, w / total_w) for v, w in valid]

    rank = dict(pers)
    teleport_scale = 1.0 - alpha
    for _ in range(max_iter):
        dangling_mass = sum(rank[u] for u in dangling)
        base_factor = teleport_scale + alpha * dangling_mass
        nxt = {v: base_factor * pers[v] for v in node_list}
        for u, targets in norm_out.items():
            ru = alpha * rank[u]
            if ru <= 0.0:
                continue
            for v, prob in targets:
                nxt[v] += ru * prob
        diff = sum(abs(nxt[v] - rank[v]) for v in node_list)
        rank = nxt
        if diff < tol:
            break
    return rank


def _resolve_focus_keys(
    focus_files: Sequence[str],
    root: Path,
    target: Path,
    indexed_by_norm: Mapping[str, str],
    indexed_keys: Sequence[str],
) -> set[str]:
    matched: set[str] = set()
    for item in focus_files:
        cleaned = item.strip()
        if not cleaned:
            continue
        for anchor in (root, target):
            try:
                cand = (Path(cleaned) if Path(cleaned).is_absolute() else anchor / cleaned).resolve()
            except (OSError, RuntimeError):
                continue
            hit = indexed_by_norm.get(os.path.normcase(str(cand)))
            if hit is not None:
                matched.add(hit)
                break
        else:
            norm_suffix = cleaned.replace("\\", "/").lstrip("./")
            if FOLD:
                norm_suffix = norm_suffix.casefold()
            suffix_hits = [
                k
                for k in indexed_keys
                if (k.casefold() if FOLD else k) == norm_suffix
                or (k.casefold() if FOLD else k).endswith("/" + norm_suffix)
            ]
            if len(suffix_hits) == 1:
                matched.add(suffix_hits[0])
    return matched


def format_file_outline(symbols: Sequence[SymbolDef], *, max_bytes: int | None = None) -> tuple[list[str], int]:
    """Format `[Lstart-Lend] signature` lines for a single file's symbols."""
    lines: list[str] = []
    used = 0
    for sym in symbols:
        indent = "  " * sym.depth
        line = f"{indent}[L{sym.start_line}-L{sym.end_line}] {sym.signature}"
        cost = len(line.encode("utf-8")) + 1
        if max_bytes is not None and lines and used + cost > max_bytes:
            break
        lines.append(line)
        used += cost
    return lines, len(lines)


def build_repo_map(
    root: Path,
    target: Path | None = None,
    *,
    focus_files: Sequence[str] = (),
    query: str = "",
    max_tokens: int = 2000,
    allow_file: Callable[[Path], bool] | None = None,
) -> RepoMapResult:
    """Build a token-budgeted repository structural map ranked via Personalized PageRank."""
    root_resolved = root.resolve()
    target_resolved = (target or root).resolve()
    discovered = collect_repo_files(target_resolved, root_resolved, allow_file=allow_file)
    if not discovered:
        msg = "未找到可索引的代码文件。"
        return RepoMapResult(msg, 0, 0, 0, 0, estimate_text(msg), False)

    file_defs: dict[str, tuple[SymbolDef, ...]] = {}
    file_ref_counts: dict[str, dict[str, int]] = {}
    indexed_by_norm: dict[str, str] = {}
    defines: dict[str, list[str]] = defaultdict(list)

    for path in discovered:
        if path.is_relative_to(root_resolved):
            rel = path.relative_to(root_resolved).as_posix()
        elif path.is_relative_to(target_resolved):
            rel = path.relative_to(target_resolved).as_posix()
        else:
            rel = path.as_posix()
        indexed_by_norm[os.path.normcase(str(path))] = rel
        defs, _, ref_counts = _parse_file_cached(path)
        file_defs[rel] = defs
        file_ref_counts[rel] = ref_counts
        seen_names: set[str] = set()
        for sym in defs:
            if sym.name and sym.name not in seen_names:
                seen_names.add(sym.name)
                defines[sym.name].append(rel)

    all_files = list(file_defs.keys())
    total_symbols = sum(len(v) for v in file_defs.values())
    if total_symbols == 0:
        msg = f"已扫描 {len(all_files)} 个代码文件，未提取到顶层符号定义。"
        return RepoMapResult(msg, len(all_files), 0, 0, 0, estimate_text(msg), False)

    exact_q, all_q = tokenize_query(query)
    focus_set = _resolve_focus_keys(focus_files, root_resolved, target_resolved, indexed_by_norm, all_files)

    # Directed dependency graph edges[src][dst] and per-symbol edge weights for symbol-level attribution.
    edges: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    sym_edge_weights: dict[str, list[tuple[str, str, float]]] = defaultdict(list)

    for src, ref_counts in file_ref_counts.items():
        for sym, count in ref_counts.items():
            definers = defines.get(sym)
            if not definers:
                continue
            w = 1.0
            sym_low = sym.lower()
            if len(sym) >= 2 and (sym_low in all_q or bool(_subtokens(sym) & all_q)):
                w *= 10.0
            if _is_specific_symbol(sym):
                w *= 10.0
            if sym.startswith("_"):
                w *= 0.1
            if len(definers) > 5:
                w *= 1.0 / math.sqrt(len(definers))
            w *= math.sqrt(max(count, 1))
            for dst in definers:
                if dst != src:
                    edges[src][dst] += w
                    sym_edge_weights[src].append((dst, sym, w))

    # Personalization vector.
    pers: dict[str, float] = {}
    for rel in all_files:
        score = 1.0
        if rel in focus_set:
            score += 50.0
        if all_q:
            stem = Path(rel).stem
            file_tokens = {stem.lower(), *_subtokens(stem)}
            for s in file_defs[rel]:
                file_tokens.add(s.name.lower())
                file_tokens.update(_subtokens(s.name))
            matched_q = all_q & file_tokens
            if matched_q:
                score += 10.0 * len(matched_q)
        pers[rel] = score

    file_rank = personalized_pagerank(all_files, edges, pers)

    # Attribute inbound PageRank flow to individual (dst, sym_name) targets.
    sym_inbound_flow: dict[tuple[str, str], float] = defaultdict(float)
    for src, triples in sym_edge_weights.items():
        out_total = sum(edges[src].values())
        if out_total <= 0.0:
            continue
        src_flow = PAGERANK_ALPHA * file_rank.get(src, 0.0) / out_total
        for dst, sym_name, w in triples:
            sym_inbound_flow[(dst, sym_name)] += src_flow * w

    # Score every symbol and find enclosing depth-0 parent index per file so nested methods keep their header.
    parent_index: dict[tuple[str, int], int] = {}
    scored_symbols: list[tuple[float, float, int, str, int]] = []

    for rel in all_files:
        defs = file_defs[rel]
        if not defs:
            continue
        f_rank = file_rank.get(rel, 0.0)
        base_sym_rank = f_rank / math.sqrt(len(defs))
        last_top_idx: int | None = None
        for idx, sym in enumerate(defs):
            if sym.depth == 0:
                last_top_idx = idx
            elif last_top_idx is not None:
                parent_index[(rel, idx)] = last_top_idx

            s_score = base_sym_rank + sym_inbound_flow.get((rel, sym.name), 0.0)
            if all_q:
                sym_low = sym.name.lower()
                qname_low = sym.qualified_name.lower()
                if sym_low in exact_q or qname_low in exact_q:
                    s_score *= 8.0
                elif sym_low in all_q or bool(_subtokens(sym.name) & all_q):
                    s_score *= 3.0
            if sym.name.startswith("_") and sym.name != "__init__":
                s_score *= 0.5
            scored_symbols.append((s_score, f_rank, -sym.start_line, rel, idx))

    scored_symbols.sort(reverse=True)
    file_order = sorted(all_files, key=lambda f: (-file_rank.get(f, 0.0), f))

    def render_prefix(count: int, *, with_footer: bool) -> tuple[str, int, int]:
        chosen_by_file: dict[str, set[int]] = defaultdict(set)
        for _, _, _, rel, idx in scored_symbols[:count]:
            chosen_by_file[rel].add(idx)
            p_idx = parent_index.get((rel, idx))
            if p_idx is not None:
                chosen_by_file[rel].add(p_idx)

        blocks: list[str] = []
        shown_syms = 0
        shown_files = 0
        for rel in file_order:
            indices = chosen_by_file.get(rel)
            if not indices:
                continue
            shown_files += 1
            defs = file_defs[rel]
            lines = [f"{rel}:"]
            for idx in sorted(indices):
                sym = defs[idx]
                shown_syms += 1
                indent = "  " * (sym.depth + 1)
                lines.append(f"{indent}[L{sym.start_line}-L{sym.end_line}] {sym.signature}")
            blocks.append("\n".join(lines))
        body = "\n\n".join(blocks)
        if with_footer:
            footer = f"\n\n（共索引 {len(all_files)} 个文件、{total_symbols} 个符号；当前预算展示 {shown_files} 个文件、{shown_syms} 个高相关符号）"
            body = body + footer if body else footer.lstrip()
        return body, shown_files, shown_syms

    full_text, full_files, full_syms = render_prefix(len(scored_symbols), with_footer=False)
    full_tokens = estimate_text(full_text)
    if full_tokens <= max_tokens:
        return RepoMapResult(full_text, len(all_files), full_files, total_symbols, full_syms, full_tokens, False)

    lo, hi = 1, len(scored_symbols) - 1
    best: tuple[str, int, int, int] | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate_text, f_cnt, s_cnt = render_prefix(mid, with_footer=True)
        tok_cnt = estimate_text(candidate_text)
        if tok_cnt <= max_tokens:
            best = (candidate_text, f_cnt, s_cnt, tok_cnt)
            lo = mid + 1
        else:
            hi = mid - 1

    if best is not None:
        text, f_cnt, s_cnt, tok_cnt = best
        return RepoMapResult(text, len(all_files), f_cnt, total_symbols, s_cnt, tok_cnt, True)

    # Ultra-tight fallback if even a single symbol + footer exceeded max_tokens.
    minimal_text, f_cnt, s_cnt = render_prefix(1, with_footer=False)
    while len(minimal_text) > 32 and estimate_text(minimal_text) > max_tokens:
        minimal_text = minimal_text[: len(minimal_text) - 16].rstrip() + "…"
    return RepoMapResult(minimal_text, len(all_files), f_cnt, total_symbols, s_cnt, estimate_text(minimal_text), True)
