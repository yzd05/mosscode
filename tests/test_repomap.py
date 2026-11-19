"""Tests for Tree-sitter AST indexing, Personalized PageRank repo_map, and read_file outline/symbol modes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mosscode import repomap
from mosscode.context import estimate_text
from mosscode.messages import ToolCall
from mosscode.permissions import PermissionPolicy, Rule, parse_rule
from mosscode.skills import BUILTIN_TOOLS, CLAUDE_TOOLS
from mosscode.tools import create_default_registry


def call(name: str, **arguments) -> ToolCall:
    return ToolCall("call-repomap", name, json.dumps(arguments, ensure_ascii=False))


# 1. Multi-language AST symbol & reference extraction --------------------------------------


def test_parse_symbols_and_references_across_all_nine_languages(tmp_path: Path):
    repomap.clear_symbol_cache()

    # 1) Python (.py) with multi-decorator class, decorated async method, type alias, and constant.
    py_file = tmp_path / "sample.py"
    py_file.write_text(
        "DEFAULT_RETRIES = 3\n"
        "type TokenId = str\n"
        "\n"
        "@register_service\n"
        "@configurable(timeout=10)\n"
        "class AuthService(BaseService):\n"
        "    @classmethod\n"
        "    async def verify_user(\n"
        "        cls,\n"
        "        token: TokenId,\n"
        "    ) -> bool:\n"
        "        return check_signature(token)\n"
        "\n"
        "def top_helper(value: int) -> int:\n"
        "    return compute_hash(value)\n",
        encoding="utf-8",
    )
    py_defs, py_refs = repomap.parse_file_symbols(py_file)
    by_qname = {d.qualified_name: d for d in py_defs}
    assert {"DEFAULT_RETRIES", "TokenId", "AuthService", "AuthService.verify_user", "top_helper"} <= set(by_qname)

    cls_def = by_qname["AuthService"]
    assert cls_def.start_line == 4 and cls_def.end_line == 12
    assert cls_def.signature == "class AuthService(BaseService):"
    assert cls_def.depth == 0

    method_def = by_qname["AuthService.verify_user"]
    assert method_def.start_line == 7 and method_def.end_line == 12
    assert method_def.signature == "async def verify_user( cls, token: TokenId, ) -> bool:"
    assert method_def.depth == 1

    assert {"BaseService", "check_signature", "compute_hash"} <= py_refs
    assert not ({"def", "async", "class", "return", "cls", "self"} & py_refs)

    # 2) JavaScript (.js)
    js_file = tmp_path / "client.js"
    js_file.write_text(
        "export const API_LIMIT = 50;\n"
        "export async function fetchProfile(userId) {\n"
        "  return requestJson(userId);\n"
        "}\n"
        "export const formatLabel = (name) => trimText(name);\n"
        "export class ApiClient extends BaseClient {\n"
        "  async load(id) {\n"
        "    return fetchProfile(id);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    js_defs, js_refs = repomap.parse_file_symbols(js_file)
    js_qnames = {d.qualified_name: d for d in js_defs}
    assert {"API_LIMIT", "fetchProfile", "formatLabel", "ApiClient", "ApiClient.load"} <= set(js_qnames)
    assert js_qnames["ApiClient.load"].depth == 1
    assert {"BaseClient", "requestJson", "trimText", "fetchProfile"} <= js_refs
    assert "function" not in js_refs and "export" not in js_refs

    # 3) TypeScript (.ts) & TSX (.tsx)
    ts_file = tmp_path / "types.ts"
    ts_file.write_text(
        "export interface StoreConfig {\n"
        "  openSession(id: string): Promise<boolean>;\n"
        "}\n"
        "export type SessionKey = string;\n"
        "export enum StoreMode { Memory, Disk }\n"
        "export abstract class TokenVault implements StoreConfig {\n"
        "  public async openSession(id: string): Promise<boolean> {\n"
        "    return validateKey(id);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    ts_defs, ts_refs = repomap.parse_file_symbols(ts_file)
    ts_qnames = {d.qualified_name for d in ts_defs}
    assert {
        "StoreConfig",
        "StoreConfig.openSession",
        "SessionKey",
        "StoreMode",
        "TokenVault",
        "TokenVault.openSession",
    } <= ts_qnames
    assert "validateKey" in ts_refs and "StoreConfig" in ts_refs

    tsx_file = tmp_path / "Badge.tsx"
    tsx_file.write_text(
        "export interface BadgeProps { title: string; }\n"
        "export function StatusBadge({ title }: BadgeProps) {\n"
        "  return <span>{renderTitle(title)}</span>;\n"
        "}\n"
        "export const Pill = ({ title }: BadgeProps) => <div>{title}</div>;\n",
        encoding="utf-8",
    )
    tsx_defs, tsx_refs = repomap.parse_file_symbols(tsx_file)
    assert {"BadgeProps", "StatusBadge", "Pill"} <= {d.qualified_name for d in tsx_defs}
    assert "renderTitle" in tsx_refs and "BadgeProps" in tsx_refs

    # 4) Go (.go)
    go_file = tmp_path / "server.go"
    go_file.write_text(
        "package server\n"
        "const DefaultPort = 8080\n"
        "type Router interface {\n"
        "    Dispatch(path string) error\n"
        "}\n"
        "type HttpServer struct {\n"
        "    port int\n"
        "}\n"
        "func NewHttpServer(port int) *HttpServer {\n"
        "    return &HttpServer{port: port}\n"
        "}\n"
        "func (s *HttpServer) Serve(r Router) error {\n"
        "    return dialBackend(s.port)\n"
        "}\n",
        encoding="utf-8",
    )
    go_defs, go_refs = repomap.parse_file_symbols(go_file)
    go_qnames = {d.qualified_name for d in go_defs}
    assert {"DefaultPort", "Router", "Router.Dispatch", "HttpServer", "NewHttpServer", "HttpServer.Serve"} <= go_qnames
    assert "dialBackend" in go_refs and "func" not in go_refs

    # 5) Rust (.rs)
    rs_file = tmp_path / "engine.rs"
    rs_file.write_text(
        "pub const MAX_WORKERS: usize = 8;\n"
        "pub type JobId = u64;\n"
        "pub enum State { Idle, Busy }\n"
        "pub struct Scheduler { pub limit: usize }\n"
        "pub trait Executor {\n"
        "    fn execute(&self, job: JobId) -> bool;\n"
        "}\n"
        "impl Scheduler {\n"
        "    pub fn new(limit: usize) -> Self { Self { limit } }\n"
        "}\n"
        "impl Executor for Scheduler {\n"
        "    fn execute(&self, job: JobId) -> bool { run_kernel(job) }\n"
        "}\n"
        "pub fn spawn_pool() -> Scheduler { Scheduler::new(MAX_WORKERS) }\n",
        encoding="utf-8",
    )
    rs_defs, rs_refs = repomap.parse_file_symbols(rs_file)
    rs_qnames = {d.qualified_name for d in rs_defs}
    assert {
        "MAX_WORKERS",
        "JobId",
        "State",
        "Scheduler",
        "Executor",
        "Executor.execute",
        "impl Scheduler",
        "Scheduler.new",
        "impl Executor for Scheduler",
        "Scheduler.execute",
        "spawn_pool",
    } <= rs_qnames
    assert "run_kernel" in rs_refs and "impl" not in rs_refs

    # 6) Java (.java)
    java_file = tmp_path / "WorkerManager.java"
    java_file.write_text(
        "package app;\n"
        "public interface TaskRunner { void runTask(String id); }\n"
        "public enum Priority { LOW, HIGH }\n"
        "public class WorkerManager implements TaskRunner {\n"
        "    public static final int MAX_QUEUE = 32;\n"
        "    public WorkerManager() {}\n"
        "    @Override\n"
        "    public void runTask(String id) {\n"
        "        AuditLogger.record(id);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    java_defs, java_refs = repomap.parse_file_symbols(java_file)
    java_by_qname = {d.qualified_name: d for d in java_defs}
    assert {
        "TaskRunner",
        "TaskRunner.runTask",
        "Priority",
        "WorkerManager",
        "WorkerManager.MAX_QUEUE",
        "WorkerManager.WorkerManager",
        "WorkerManager.runTask",
    } <= set(java_by_qname)
    assert java_by_qname["WorkerManager.runTask"].start_line == 7
    assert java_by_qname["WorkerManager.runTask"].signature == "public void runTask(String id)"
    assert "AuditLogger" in java_refs

    # 7) C (.c / .h)
    c_file = tmp_path / "buffer.h"
    c_file.write_text(
        "#ifndef BUFFER_H\n"
        "#define BUFFER_H\n"
        "#define RING_CAPACITY 256\n"
        "typedef struct RingBuffer {\n"
        "    int head;\n"
        "} RingBuffer;\n"
        "enum RingState { RING_EMPTY, RING_FULL };\n"
        "static int *ring_push(RingBuffer *buf, int item) {\n"
        "    return alloc_slot(buf, item);\n"
        "}\n"
        "void ring_reset(RingBuffer *buf);\n"
        "#endif\n",
        encoding="utf-8",
    )
    c_defs, c_refs = repomap.parse_file_symbols(c_file)
    c_qnames = {d.qualified_name for d in c_defs}
    assert "BUFFER_H" not in c_qnames  # Empty include-guard skipped.
    assert {"RING_CAPACITY", "RingBuffer", "RingState", "ring_push", "ring_reset"} <= c_qnames
    assert "alloc_slot" in c_refs

    # 8) C++ (.cpp)
    cpp_file = tmp_path / "pipeline.cpp"
    cpp_file.write_text(
        "namespace engine {\n"
        "using FrameId = unsigned long;\n"
        "class Pipeline : public BaseStage {\n"
        "public:\n"
        "    explicit Pipeline(int lanes);\n"
        "    bool step(FrameId id) const {\n"
        "        return dispatch_frame(id);\n"
        "    }\n"
        "};\n"
        "template <typename T>\n"
        "T clamp_metric(T val, T hi) { return val; }\n"
        "int Pipeline::flush_all() { return clear_queues(); }\n"
        "}\n",
        encoding="utf-8",
    )
    cpp_defs, cpp_refs = repomap.parse_file_symbols(cpp_file)
    cpp_qnames = {d.qualified_name for d in cpp_defs}
    assert {
        "FrameId",
        "Pipeline",
        "Pipeline.Pipeline",
        "Pipeline.step",
        "clamp_metric",
        "Pipeline.flush_all",
    } <= cpp_qnames
    assert {"BaseStage", "dispatch_frame", "clear_queues"} <= cpp_refs

    # 9) Bash (.sh)
    sh_file = tmp_path / "deploy.sh"
    sh_file.write_text(
        "#!/usr/bin/env bash\n"
        "verify_env() {\n"
        '    check_binary "$1"\n'
        "}\n"
        "function rollout_service {\n"
        "    verify_env docker\n"
        "}\n",
        encoding="utf-8",
    )
    sh_defs, sh_refs = repomap.parse_file_symbols(sh_file)
    assert {d.qualified_name for d in sh_defs} == {"verify_env", "rollout_service"}
    assert {"check_binary", "verify_env"} <= sh_refs
    assert "function" not in sh_refs


def test_python_ast_fallback_and_lru_cache_invalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repomap.clear_symbol_cache()
    target = tmp_path / "fallback_mod.py"
    target.write_text(
        "@dec_one\n"
        "class Sample:\n"
        "    @dec_two\n"
        "    async def run(self, x: int) -> int:\n"
        "        return external_call(x)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repomap, "_load_ts_language", lambda _lang: None)
    defs, refs = repomap.parse_file_symbols(target)
    by_qname = {d.qualified_name: d for d in defs}
    assert by_qname["Sample"].start_line == 1
    assert by_qname["Sample"].signature == "class Sample:"
    assert by_qname["Sample.run"].start_line == 3
    assert by_qname["Sample.run"].signature == "async def run(self, x: int) -> int:"
    assert "external_call" in refs

    # Modify file size/mtime and verify cache invalidates immediately.
    target.write_text("def brand_new_symbol() -> None:\n    return other_dep()\n", encoding="utf-8")
    defs2, refs2 = repomap.parse_file_symbols(target)
    assert [d.qualified_name for d in defs2] == ["brand_new_symbol"]
    assert "other_dep" in refs2


# 2. Personalized PageRank ranking, edge cases & token budget ------------------------------


async def test_repo_map_pagerank_ranks_dependency_chain_above_disconnected_files_and_packs_budget(tmp_path: Path):
    (tmp_path / "auth.py").write_text(
        "from token_store import verify_token\n\n"
        "class AuthGateway:\n"
        "    def authenticate_request(self, header: str, remote_ip: str) -> bool:\n"
        "        return verify_token(header)\n\n"
        "    def parse_authorization_bearer(self, raw_header: str) -> str:\n"
        "        return raw_header.strip()\n\n"
        "    def enforce_session_policy(self, subject_id: str, scope: str) -> bool:\n"
        "        return bool(subject_id and scope)\n\n"
        "    def audit_authentication_attempt(self, subject_id: str, ok: bool) -> None:\n"
        "        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "token_store.py").write_text(
        "from crypto import verify_hmac_signature\n\n"
        "class TokenStore:\n"
        "    def lookup_active_session(self, token_id: str, tenant_id: str) -> str:\n"
        "        return token_id\n\n"
        "    def revoke_expired_tokens(self, cutoff_epoch: int) -> int:\n"
        "        return cutoff_epoch\n\n"
        "    def rotate_refresh_handle(self, current_handle: str, ttl_seconds: int) -> str:\n"
        "        return current_handle\n\n"
        "def verify_token(raw_token: str) -> bool:\n"
        "    return verify_hmac_signature(raw_token)\n",
        encoding="utf-8",
    )
    (tmp_path / "crypto.py").write_text(
        "class SignatureVerifier:\n"
        "    def derive_signing_subkey(self, master_seed: bytes, epoch: int) -> bytes:\n"
        "        return master_seed\n\n"
        "    def verify_key_rotation_window(self, issued_at: int, now_epoch: int) -> bool:\n"
        "        return now_epoch >= issued_at\n\n"
        "def verify_hmac_signature(payload: str) -> bool:\n"
        "    return len(payload) > 0\n\n"
        "def constant_time_digest_compare(expected: str, actual: str) -> bool:\n"
        "    return expected == actual\n",
        encoding="utf-8",
    )
    # Disconnected large module with many symbols that would dominate naive line/symbol-count maps.
    unrelated_lines = [
        f"def unrelated_report_helper_{idx:02d}(value: int) -> int:\n    return value + {idx}\n" for idx in range(60)
    ]
    (tmp_path / "unrelated_big.py").write_text("\n".join(unrelated_lines), encoding="utf-8")

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)

    # Focus on auth.py with tight budget: connected chain (auth -> token_store -> crypto) beats unrelated_big.py.
    focused = await registry.execute(call("repo_map", focus_files=["auth.py"], max_tokens=256))
    assert not focused.is_error, focused.output
    assert estimate_text(focused.output) <= 256
    assert "auth.py:" in focused.output
    assert "token_store.py:" in focused.output
    assert "crypto.py:" in focused.output
    assert "unrelated_big.py:" not in focused.output

    # Query-driven ranking without focus_files: query="verify_token" boosts token_store.py -> crypto.py & auth.py.
    queried = await registry.execute(call("repo_map", query="verify_token", max_tokens=256))
    assert not queried.is_error, queried.output
    assert estimate_text(queried.output) <= 256
    assert "token_store.py:" in queried.output
    assert "crypto.py:" in queried.output
    assert "unrelated_big.py:" not in queried.output

    # Even with a large budget that includes everything, connected files rank ahead of unrelated_big.py,
    # and symbols within each file preserve ascending source line numbers.
    generous = await registry.execute(call("repo_map", focus_files=["auth.py"], max_tokens=4000))
    assert not generous.is_error
    assert estimate_text(generous.output) <= 4000
    assert generous.output.index("token_store.py:") < generous.output.index("unrelated_big.py:")
    assert generous.output.index("crypto.py:") < generous.output.index("unrelated_big.py:")
    assert generous.output.index("[L3-L11] class TokenStore:") < generous.output.index("[L13-L14] def verify_token")


def test_pagerank_edge_cases_conserve_probability_mass(tmp_path: Path):
    # 0 files
    assert repomap.personalized_pagerank([], {}, {}) == {}
    empty_res = repomap.build_repo_map(tmp_path)
    assert empty_res.files_indexed == 0 and "未找到" in empty_res.text

    # 1 file
    r1 = repomap.personalized_pagerank(["only.py"], {"only.py": {"only.py": 5.0}}, {"only.py": 1.0})
    assert r1 == {"only.py": pytest.approx(1.0)}

    # 100% dangling nodes
    dangling = repomap.personalized_pagerank(["a.py", "b.py", "c.py"], {}, {"a.py": 3.0, "b.py": 1.0, "c.py": 0.0})
    assert sum(dangling.values()) == pytest.approx(1.0)
    assert dangling["a.py"] > dangling["b.py"] > dangling["c.py"]

    # Cyclic imports + self-loops
    cyclic_edges = {
        "a.py": {"b.py": 2.0, "a.py": 99.0},
        "b.py": {"c.py": 4.0},
        "c.py": {"a.py": 1.0},
    }
    cyclic = repomap.personalized_pagerank(
        ["a.py", "b.py", "c.py"], cyclic_edges, {"a.py": 1.0, "b.py": 1.0, "c.py": 1.0}
    )
    assert sum(cyclic.values()) == pytest.approx(1.0)
    assert all(0.0 < val < 1.0 for val in cyclic.values())


# 3. .gitignore, worktrees, binary files & PermissionPolicy deny boundaries ----------------


async def test_repo_map_excludes_gitignore_worktrees_binaries_and_denied_secret_files(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("ignored_dir/\n*.gen.py\n!keep.gen.py\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def run_public_app() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "keep.gen.py").write_text("def kept_generated_fn() -> int:\n    return 2\n", encoding="utf-8")
    (tmp_path / "drop.gen.py").write_text("def dropped_generated_fn() -> int:\n    return 3\n", encoding="utf-8")

    for skipped in (
        "ignored_dir/hidden.py",
        ".venv/lib_mod.py",
        "node_modules/pkg/index.js",
        "__pycache__/cached.py",
        ".mosscode/worktrees/wt1/isolated_wt.py",
        ".claude/worktrees/acp/claude_wt.py",
    ):
        p = tmp_path / skipped
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"def leaked_{Path(skipped).stem}() -> None:\n    pass\n", encoding="utf-8")

    # Binary file masquerading with a .py extension.
    (tmp_path / "corrupt_bin.py").write_bytes(b"\x00\x01def binary_ghost(): pass\n")

    # Secret files guarded by Read(secrets/**) and repo_map(private/**) deny rules.
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "keys.py").write_text(
        "def load_master_signing_key() -> str:\n    return 'top-secret'\n",
        encoding="utf-8",
    )
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "internal.py").write_text(
        "def internal_vault_token() -> str:\n    return 'vault'\n",
        encoding="utf-8",
    )

    policy = PermissionPolicy(tmp_path, mode="default", load=False)
    policy.layers[1] = (
        parse_rule("Read(secrets/**)", "deny"),
        Rule("repo_map", "private/**", "deny"),
    )
    registry = create_default_registry(tmp_path, permissions=policy)

    res = await registry.execute(call("repo_map"))
    assert not res.is_error, res.output
    assert "app.py:" in res.output and "run_public_app" in res.output
    assert "keep.gen.py:" in res.output and "kept_generated_fn" in res.output

    for forbidden in (
        "dropped_generated_fn",
        "leaked_hidden",
        "leaked_lib_mod",
        "leaked_index",
        "leaked_cached",
        "leaked_isolated_wt",
        "leaked_claude_wt",
        "binary_ghost",
        "secrets/keys.py",
        "load_master_signing_key",
        "private/internal.py",
        "internal_vault_token",
    ):
        assert forbidden not in res.output

    # Direct repo_map call targeting a Read-denied path is rejected with permission_denied.
    denied_direct = await registry.execute(call("repo_map", path="secrets/keys.py"))
    assert denied_direct.is_error and denied_direct.code == "permission_denied"

    # Targeting inside a worktree explicitly indexes that worktree cleanly.
    wt_res = await registry.execute(call("repo_map", path=".mosscode/worktrees/wt1"))
    assert not wt_res.is_error and "leaked_isolated_wt" in wt_res.output


def test_repo_map_registered_in_plan_mode_and_skills_and_launcher_stays_lazy(tmp_path: Path):
    assert "repo_map" in BUILTIN_TOOLS
    assert CLAUDE_TOOLS["RepoMap"] == "repo_map"

    registry = create_default_registry(tmp_path, permission_mode="plan", load_permissions=False)
    plan_names = {d.name for d in registry.definitions(plan=True)}
    assert "repo_map" in plan_names

    # Verify launcher.py never eagerly imports tree_sitter or mosscode.repomap.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, mosscode.launcher as l; l.parse_arguments(['--list-providers']); "
            "assert 'tree_sitter' not in sys.modules and 'mosscode.repomap' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# 4. read_file(outline=True) & read_file(symbol="...") + Read-Before-Edit invariant --------


async def test_read_file_outline_preserves_not_read_while_symbol_unlocks_edit_file(tmp_path: Path):
    target = tmp_path / "service.py"
    target.write_text(
        "class Alpha:\n"
        "    def __init__(self, x: int) -> None:\n"
        "        self.x = x\n"
        "\n"
        "class Beta:\n"
        "    def __init__(self, y: int) -> None:\n"
        "        self.y = y\n"
        "\n"
        "    @staticmethod\n"
        "    def compute(val: int) -> int:\n"
        "        return val * 2\n",
        encoding="utf-8",
    )
    (tmp_path / "readme.md").write_text("# Title\n\nParagraph\n", encoding="utf-8")

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)

    # Reject specifying both outline=True and symbol, or whitespace-only symbol.
    both_err = await registry.execute(call("read_file", path="service.py", outline=True, symbol="Beta"))
    assert both_err.is_error and both_err.code == "invalid_arguments"
    blank_sym = await registry.execute(call("read_file", path="service.py", symbol="   "))
    assert blank_sym.is_error and blank_sym.code == "invalid_arguments"

    # Unsupported extension outline returns line count hint and does NOT mark FileState read.
    md_outline = await registry.execute(call("read_file", path="readme.md", outline=True))
    assert not md_outline.is_error and "共 3 行" in md_outline.output and "普通 read_file" in md_outline.output
    assert (
        await registry.execute(call("edit_file", path="readme.md", old_text="# Title", new_text="# Updated"))
    ).code == "not_read"

    # Python outline shows [Lstart-Lend] hierarchy + footer and does NOT unlock edit_file!
    outline = await registry.execute(call("read_file", path="service.py", outline=True))
    assert not outline.is_error, outline.output
    assert "[L1-L3] class Alpha:" in outline.output
    assert "  [L2-L3] def __init__(self, x: int) -> None:" in outline.output
    assert "[L5-L11] class Beta:" in outline.output
    assert "  [L9-L11] def compute(val: int) -> int:" in outline.output
    assert "（共 11 行，共 5 个符号；修改前请用 read_file 的 symbol 或 offset/limit 读取目标代码段）" in outline.output

    blocked_edit = await registry.execute(
        call("edit_file", path="service.py", old_text="return val * 2", new_text="return val * 3")
    )
    assert blocked_edit.is_error and blocked_edit.code == "not_read"

    # Ambiguous short symbol name (__init__) lists qualified candidates with [Lstart-Lend] and keeps not_read.
    ambig = await registry.execute(call("read_file", path="service.py", symbol="__init__"))
    assert ambig.is_error and ambig.code == "ambiguous_symbol"
    assert "Alpha.__init__ [L2-L3]" in ambig.output and "Beta.__init__ [L6-L7]" in ambig.output
    assert (
        await registry.execute(call("edit_file", path="service.py", old_text="self.x = x", new_text="self.x = x + 1"))
    ).code == "not_read"

    # Missing symbol suggests close matches via difflib and keeps not_read.
    missing = await registry.execute(call("read_file", path="service.py", symbol="Beta.comput"))
    assert missing.is_error and missing.code == "symbol_not_found"
    assert "Beta.compute" in missing.output
    assert (
        await registry.execute(
            call("edit_file", path="service.py", old_text="return val * 2", new_text="return val * 3")
        )
    ).code == "not_read"

    # Reading exact qualified symbol returns formatted line\tcode (including decorator!) AND unlocks edit_file!
    sym_read = await registry.execute(call("read_file", path="service.py", symbol="Beta.compute"))
    assert not sym_read.is_error, sym_read.output
    assert sym_read.output.splitlines() == [
        "     9\t    @staticmethod",
        "    10\t    def compute(val: int) -> int:",
        "    11\t        return val * 2",
    ]
    edited = await registry.execute(
        call("edit_file", path="service.py", old_text="return val * 2", new_text="return val * 10")
    )
    assert not edited.is_error, edited.output
    assert "return val * 10" in target.read_text(encoding="utf-8")


async def test_read_file_symbol_paging_and_cpp_scope_resolution(tmp_path: Path):
    giant = tmp_path / "giant.py"
    methods = [f"    def step_{i:03d}(self) -> str:\n        return '{'x' * 500}'\n" for i in range(140)]
    giant.write_text("class HugeEngine:\n" + "\n".join(methods), encoding="utf-8")

    cpp = tmp_path / "mod.cpp"
    cpp.write_text(
        "class Worker {\npublic:\n    int execute(int v) {\n        return v + 1;\n    }\n};\n",
        encoding="utf-8",
    )

    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)

    page1 = await registry.execute(call("read_file", path="giant.py", symbol="HugeEngine"))
    assert not page1.is_error and page1.details["truncated"] is True
    next_off = page1.details["next_offset"]
    assert next_off is not None and next_off > 1

    page2 = await registry.execute(call("read_file", path="giant.py", symbol="HugeEngine", offset=next_off, limit=5))
    assert not page2.is_error and f"{next_off:>6}\t" in page2.output

    # C++ '::' scope separator resolves to 'Worker.execute' seamlessly.
    cpp_sym = await registry.execute(call("read_file", path="mod.cpp", symbol="Worker::execute"))
    assert not cpp_sym.is_error and "int execute(int v)" in cpp_sym.output


async def test_repomap_symlink_escape_excluded_and_read_symbol_credential_redaction(tmp_path: Path):
    from mosscode import credentials

    inside_dir = tmp_path / "workspace"
    outside_dir = tmp_path / "outside_escape"
    inside_dir.mkdir()
    outside_dir.mkdir()

    (inside_dir / "safe_mod.py").write_text(
        "def safe_public_entry() -> str:\n    return 'ok'\n",
        encoding="utf-8",
    )
    outside_file = outside_dir / "escaped.py"
    outside_file.write_text(
        "class EscapedSymlinkSecret:\n    def leaked_outside_symbol(self) -> str:\n        return 'leak'\n",
        encoding="utf-8",
    )

    created_link = False
    try:
        (inside_dir / "link_out_dir").symlink_to(outside_dir, target_is_directory=True)
        created_link = True
    except OSError:
        pass
    try:
        (inside_dir / "link_out_file.py").symlink_to(outside_file)
        created_link = True
    except OSError:
        pass

    registry = create_default_registry(inside_dir, permission_mode="bypassPermissions", load_permissions=False)
    rm_res = await registry.execute(call("repo_map", path="."))
    assert not rm_res.is_error, rm_res.output
    assert "safe_public_entry" in rm_res.output
    if created_link:
        assert "EscapedSymlinkSecret" not in rm_res.output
        assert "leaked_outside_symbol" not in rm_res.output

    # Verify read_file(..., symbol="...") applies credentials.redact before returning output!
    secret_value = "sk-test-do-not-show-123"
    credentials.remember(secret_value)
    (inside_dir / "client.py").write_text(
        "class ApiClient:\n"
        "    def headers(self) -> dict[str, str]:\n"
        f'        return {{"Authorization": "Bearer {secret_value}"}}\n',
        encoding="utf-8",
    )
    sym_res = await registry.execute(call("read_file", path="client.py", symbol="ApiClient.headers"))
    assert not sym_res.is_error, sym_res.output
    assert secret_value not in sym_res.output
    assert "[已隐藏的密钥]" in sym_res.output
