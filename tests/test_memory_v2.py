"""Memory Engine 2.0: Okapi BM25 + CJK hybrid search, read/list actions, pinned notes, and worktree sharing."""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from mosscode import memory as memory_module
from mosscode import paths as paths_module
from mosscode.context import estimate_text
from mosscode.conversation import Conversation
from mosscode.memory import INDEX_FILE, MemoryFailure, MemoryStore, tokenize_memory
from mosscode.memory_extract import MemoryExtractor, _notes_snapshot
from mosscode.messages import Completed, ContentBlock, Message, ToolCall, Usage
from mosscode.paths import git_common_root, memory_project_dir, project_dir, project_key
from mosscode.tools import create_default_registry


def memory_call(call_id: str, **args) -> ToolCall:
    return ToolCall(call_id, "memory", json.dumps(args, ensure_ascii=False))


def ops_reply(operations, usage: Usage = Usage()) -> Completed:
    text = f"<analysis>整理</analysis>\n<operations>\n{json.dumps(operations, ensure_ascii=False)}\n</operations>"
    return Completed(Message("assistant", (ContentBlock("text", text),)), "stop", usage)


class FakeProvider:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.requests = []

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests.append((tuple(messages), system, tuple(tools), allow_tools))
        yield self.replies.pop(0)

    async def close(self):
        pass


def make_conv(tmp_path, *, mode: str = "default", auto: bool = False):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    home = tmp_path / "home"
    store = MemoryStore.for_project(project, home=home)
    reg = create_default_registry(project, permission_mode=mode, load_permissions=False)
    conv = Conversation(
        FakeProvider(),
        registry=reg,
        context_root=project,
        instructions_home=home,
        memory=store,
        auto_memory=auto,
    )
    return conv, store


# 1. Tokenization & Okapi BM25 + CJK Hybrid Search -------------------------------------------


def test_tokenize_memory_handles_english_identifiers_and_cjk_phrases():
    snake = tokenize_memory("Check run_budget_limit in AgentLoop")
    assert "run_budget_limit" in snake
    assert {"run", "budget", "limit", "agentloop"}.issubset(snake)

    kebab = tokenize_memory("prefer-pnpm-v2")
    assert "prefer-pnpm-v2" in kebab
    assert {"prefer", "pnpm", "v2"}.issubset(kebab)

    single_cjk = tokenize_memory("门")
    assert single_cjk == ["门"]

    two_cjk = tokenize_memory("门禁")
    assert two_cjk == ["门禁"]
    two_cjk_with_uni = tokenize_memory("门禁", cjk_unigrams=True)
    assert {"门禁", "门", "禁"}.issubset(two_cjk_uni := two_cjk_with_uni) and len(two_cjk_uni) == 3

    phrase_cjk = tokenize_memory("本地全量门禁")
    assert phrase_cjk == ["本地", "地全", "全量", "量门", "门禁"]

    mixed = tokenize_memory("BM25本地检索check_local")
    assert "bm25" in mixed and "检索" in mixed and "check_local" in mixed and "check" in mixed

    assert tokenize_memory("") == []
    assert tokenize_memory("   --- !!!   ") == []


def test_bm25_ranking_across_identifiers_cjk_and_field_weights(tmp_path):
    store = MemoryStore(tmp_path / "u", tmp_path / "p")

    # Field weighting: name match (x3 + substring) > description match (x2 + substring) > body-only match (x1).
    store.save("project", "body-only", "reference", "通用配置说明", "详见 run_budget 参数定义。", today="2026-10-03")
    store.save("project", "desc-hit", "reference", "控制 run_budget 请求上限", "详见运行时说明。", today="2026-10-03")
    store.save("project", "run-budget", "reference", "预算总体架构", "限制单次任务开销。", today="2026-10-01")
    store.save("user", "unrelated-db", "user", "数据库迁移规范", "每次修改表结构都要写回滚脚本。", today="2026-10-05")

    hits = store.search("run_budget")
    ranked_names = [note.name for note, _ in hits]
    assert ranked_names[:3] == ["run-budget", "desc-hit", "body-only"]
    assert "unrelated-db" not in ranked_names
    assert all(score > 0 for _, score in hits)

    # Kebab-case query also ranks exact slug first.
    kebab_hits = store.search("run-budget")
    assert [note.name for note, _ in kebab_hits][:2] == ["run-budget", "desc-hit"]

    # Chinese phrase ranking & exclusion of unrelated notes (even if pinned!).
    store.save(
        "project",
        "pinned-unrelated",
        "project",
        "前端配色约定",
        "深色主题使用终端默认背景色。\n原因：视觉一致\n适用方式：TUI",
        today="2026-10-10",
        pinned=True,
    )
    store.save(
        "project",
        "local-gate",
        "feedback",
        "推送公开库前必须执行本地全量门禁",
        "先跑 check_local 再导出公开仓库。\n原因：防 CI 风控\n适用方式：每次推送前",
        today="2026-10-02",
    )
    store.save(
        "user",
        "partial-local",
        "user",
        "本地开发环境目录约定",
        "虚拟环境放在项目根目录。",
        today="2026-10-08",
    )

    cjk_hits = store.search("本地全量门禁")
    cjk_names = [note.name for note, _ in cjk_hits]
    assert cjk_names[0] == "local-gate"
    assert "unrelated-db" not in cjk_names
    assert "pinned-unrelated" not in cjk_names  # Pinned note with 0 lexical overlap must NOT appear.

    # Single-char CJK, 2-char CJK, and mixed English+Chinese queries.
    assert store.search("禁")[0][0].name == "local-gate"
    assert store.search("门禁")[0][0].name == "local-gate"
    assert store.search("check_local门禁")[0][0].name == "local-gate"

    # Scope filter & limit.
    only_user = store.search("本地", scope="user")
    assert [n.name for n, _ in only_user] == ["partial-local"]
    assert len(store.search("run_budget", limit=1)) == 1


def test_bm25_edge_cases_never_divide_by_zero(tmp_path):
    empty_store = MemoryStore(tmp_path / "u", tmp_path / "p")
    assert empty_store.search("anything") == []
    assert empty_store.search("") == []
    assert empty_store.search("   ") == []
    assert empty_store.search("...---!!!") == []

    # Corpus whose only note has punctuation-only description/body except slug.
    empty_store.save("user", "a", "user", "---", "!!!")
    assert empty_store.search("missing") == []
    assert empty_store.search("a")[0][0].name == "a"
    assert empty_store.search("a", limit=0) == []

    with pytest.raises(MemoryFailure) as exc:
        empty_store.search("a", scope="invalid")
    assert exc.value.code == "invalid_scope"


async def test_memory_tool_read_search_and_list_beyond_prompt_index_budget(tmp_path):
    conv, store = make_conv(tmp_path, mode="default")
    reg = conv.registry

    # Save 75 notes with long descriptions so prompt index_section() drops older ones (> INDEX_TOKENS).
    for idx in range(75):
        marker = " exact_oldest_marker" if idx == 0 else ""
        store.save(
            "project" if idx % 2 == 0 else "user",
            f"archive-note-{idx:03d}",
            "reference",
            f"历史归档条目 {idx:03d}：" + ("详细背景说明文字" * 12),
            f"归档正文内容编号 {idx:03d}，包含检索词 needle_{idx:03d}{marker}。",
            today=f"2026-{(idx // 28) + 1:02d}-{(idx % 28) + 1:02d}",
        )

    prompt_index = store.index_section()
    assert "archive-note-000" not in prompt_index
    assert "较旧的笔记未列出" in prompt_index

    # `list` returns ALL 75 notes, including archive-note-000 omitted from prompt_index!
    listed = await reg.execute(memory_call("l1", action="list"))
    assert not listed.is_error
    assert listed.details["count"] == 75
    assert "archive-note-000" in listed.output and "archive-note-074" in listed.output

    listed_proj = await reg.execute(memory_call("l2", action="list", scope="project"))
    assert not listed_proj.is_error and listed_proj.details["count"] == 38
    assert "archive-note-001" not in listed_proj.output

    # `search` finds archive-note-000 #1 even though it was evicted from the 3,000-token prompt index.
    searched = await reg.execute(memory_call("s1", action="search", query="needle_000", limit=5))
    assert not searched.is_error
    assert searched.details["results"][0]["name"] == "archive-note-000"
    assert searched.details["results"][0]["score"] > searched.details["results"][1]["score"]
    assert "archive-note-000" in searched.output and "needle_000" in searched.output

    unique_hit = await reg.execute(memory_call("s1b", action="search", query="exact_oldest_marker", limit=5))
    assert not unique_hit.is_error and unique_hit.details["count"] == 1
    assert unique_hit.details["results"][0]["name"] == "archive-note-000"

    no_hit = await reg.execute(memory_call("s2", action="search", query="nonexistent_xyz_999"))
    assert not no_hit.is_error and no_hit.output == "未找到匹配的记忆笔记。" and no_hit.details["count"] == 0

    empty_q = await reg.execute(memory_call("s3", action="search", query="   "))
    assert empty_q.is_error and empty_q.code == "invalid_query"

    # `read` returns full frontmatter + body, and gives cross-scope hint when looking in the wrong scope.
    read_res = await reg.execute(memory_call("r1", action="read", name="archive-note-000"))
    assert not read_res.is_error
    assert "name: archive-note-000" in read_res.output
    assert "needle_000" in read_res.output

    wrong_scope = await reg.execute(memory_call("r2", action="read", scope="user", name="archive-note-000"))
    assert wrong_scope.is_error and wrong_scope.code == "not_found"
    assert "项目级有同名笔记" in wrong_scope.output

    await conv.close()


# 2. `pinned: true` Frontmatter, Preservation & Eviction Fix ---------------------------------


async def test_pinned_frontmatter_preservation_and_extractor_safety(tmp_path):
    store = MemoryStore(tmp_path / "u", tmp_path / "p")

    # Unpinned note omits `pinned` for 100% backwards-compatible YAML format.
    unpinned, _ = store.save("user", "plain-pref", "user", "普通偏好", "使用 ruff", today="2026-10-01")
    raw_unpinned = unpinned.path.read_text(encoding="utf-8")
    assert "pinned:" not in raw_unpinned
    assert unpinned.index_line() == "- [plain-pref](plain-pref.md) — 普通偏好"

    # Pinned note emits `pinned: true` and `[常驻]` badge in index_line / MEMORY.md.
    pinned, _ = store.save(
        "project",
        "core-invariant",
        "project",
        "核心架构铁律",
        "绝不在提示前缀拼入动态时间戳。\n原因：保护 KV 缓存\n适用方式：全局",
        today="2020-01-01",
        pinned=True,
    )
    raw_pinned = pinned.path.read_text(encoding="utf-8")
    assert "\npinned: true\n" in raw_pinned
    assert "[常驻]" in pinned.index_line()
    assert "[常驻]" in (store.directory("project") / INDEX_FILE).read_text(encoding="utf-8")

    # Overwriting with `pinned=None` (default) preserves `pinned=True`!
    updated, created = store.save(
        "project",
        "core-invariant",
        "project",
        "核心架构铁律（更新）",
        "绝不在提示前缀拼入动态日期。\n原因：保护 KV 缓存\n适用方式：全局",
        today="2026-10-05",
    )
    assert not created and updated.pinned is True
    assert "\npinned: true\n" in updated.path.read_text(encoding="utf-8")

    # Background MemoryExtractor updating the note (both omitting `pinned` and emitting `pinned: false`) keeps it pinned!
    provider = FakeProvider(
        replies=[
            ops_reply(
                [
                    {
                        "op": "save",
                        "scope": "project",
                        "name": "core-invariant",
                        "type": "project",
                        "description": "核心架构铁律（自动合并）",
                        "body": "合并后的正文。\n原因：KV 缓存\n适用方式：全局",
                    }
                ]
            ),
            ops_reply(
                [
                    {
                        "op": "save",
                        "scope": "project",
                        "name": "core-invariant",
                        "type": "project",
                        "description": "核心架构铁律（二次合并）",
                        "body": "二次合并正文。\n原因：KV 缓存\n适用方式：全局",
                        "pinned": False,
                    }
                ]
            ),
        ]
    )
    extractor = MemoryExtractor(store, lambda: provider)
    extractor.schedule([Message("user", (ContentBlock("text", "补充说明架构铁律"),))])
    await extractor.close()
    reloaded = store.read("core-invariant", scope="project")[0]
    assert reloaded.pinned is True and "合并后的正文" in reloaded.body

    extractor.schedule([Message("user", (ContentBlock("text", "再补充一点"),))])
    await extractor.close()
    reloaded2 = store.read("core-invariant", scope="project")[0]
    assert reloaded2.pinned is True and "二次合并正文" in reloaded2.body

    # Explicit `pinned=False` via direct save unpins cleanly.
    unpinned_now, _ = store.save(
        "project",
        "core-invariant",
        "project",
        "核心架构铁律",
        "取消常驻。\n原因：测试\n适用方式：测试",
        pinned=False,
    )
    assert unpinned_now.pinned is False and "pinned:" not in unpinned_now.path.read_text(encoding="utf-8")

    # Invalid `pinned` in hand-edited file marks note.error cleanly; invalid type in save() raises invalid_pinned.
    bad_file = store.directory("user") / "bad-pin.md"
    bad_file.write_text(
        "---\nname: bad-pin\ndescription: d\ntype: user\npinned: maybe\nupdated: 2026-10-01\n---\nbody\n",
        encoding="utf-8",
    )
    loaded_map = {n.name: n for n in store.load("user")}
    assert "pinned" in loaded_map["bad-pin"].error
    with pytest.raises(MemoryFailure) as exc:
        store.save("user", "bad-arg", "user", "d", "b", pinned="yes")  # type: ignore[arg-type]
    assert exc.value.code == "invalid_pinned"


def test_pinned_survives_eviction_and_same_date_tie_keeps_project_over_user(tmp_path):
    store = MemoryStore(tmp_path / "u", tmp_path / "p")

    # Oldest pinned notes (2020-01-01) must survive both line-limit slicing and token-budget eviction!
    store.save(
        "project",
        "ancient-pinned",
        "project",
        "最早期常驻架构约束",
        "必须保留在索引中。\n原因：核心\n适用方式：始终",
        today="2020-01-01",
        pinned=True,
    )
    for idx in range(220):
        store.save(
            "project",
            f"ephemeral-{idx:03d}",
            "reference",
            f"近期普通笔记 {idx:03d} " + ("填充说明" * 10),
            "普通正文 " * 120,
            today=f"2026-{(idx // 28) + 1:02d}-{(idx % 28) + 1:02d}",
        )

    section = store.index_section(limit=memory_module.INDEX_TOKENS, lines=memory_module.INDEX_LINES)
    assert "ancient-pinned" in section
    assert "[常驻]" in section
    assert "ephemeral-000" not in section

    # Extraction _notes_snapshot also prioritizes ancient-pinned in full-text budget ahead of newer unpinned notes.
    _, bodies, omitted = _notes_snapshot(store)
    assert 'name="ancient-pinned"' in bodies
    assert ("project", "ancient-pinned") not in omitted

    # Same-date tie-break: unpinned `user` note is evicted BEFORE same-date `project` note!
    tie_store = MemoryStore(tmp_path / "tie_u", tmp_path / "tie_p")
    tie_store.save("user", "same-day-user", "user", "同日用户笔记 " + ("甲" * 60), "正文", today="2026-06-15")
    tie_store.save("project", "same-day-proj", "reference", "同日项目笔记 " + ("乙" * 60), "正文", today="2026-06-15")

    full_text = tie_store.index_section(limit=100_000)
    one_note_budget = estimate_text(full_text) - 20
    tight_section = tie_store.index_section(limit=one_note_budget)
    assert "same-day-proj" in tight_section
    assert "same-day-user" not in tight_section


# 3. Git Worktree Common-Root Project Memory & Legacy Migration ------------------------------


def _git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def test_real_git_worktree_shares_project_memory_while_isolating_project_dir(tmp_path):
    home = tmp_path / "home"
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    _git(main_repo, "init")
    _git(main_repo, "config", "user.email", "dev@example.com")
    _git(main_repo, "config", "user.name", "Dev")
    (main_repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(main_repo, "add", "README.md")
    _git(main_repo, "commit", "-m", "init")

    worktree = tmp_path / "linked-wt"
    _git(main_repo, "worktree", "add", "-b", "feat/wt", str(worktree))
    nested = worktree / "src" / "pkg"
    nested.mkdir(parents=True)

    # Pure filesystem resolution matches main_repo from both worktree root and nested subdirectory.
    assert git_common_root(main_repo) == main_repo.resolve()
    assert git_common_root(worktree) == main_repo.resolve()
    assert git_common_root(nested) == main_repo.resolve()

    # Session/checkpoint project_key and project_dir stay strictly isolated per worktree!
    assert project_key(main_repo) != project_key(worktree)
    assert project_dir(main_repo, home) != project_dir(worktree, home)

    # MemoryStore.for_project shares the canonical main_repo project memory across both trees!
    main_store = MemoryStore.for_project(main_repo, home=home)
    wt_store = MemoryStore.for_project(worktree, home=home)
    assert main_store.directory("project") == wt_store.directory("project")

    main_store.save(
        "project",
        "shared-arch",
        "project",
        "主工作区保存的架构笔记",
        "所有工作树立即可检索。\n原因：统一项目记忆\n适用方式：跨分支",
        pinned=True,
    )
    wt_hits = wt_store.search("工作树立即可检索", scope="project")
    assert len(wt_hits) == 1 and wt_hits[0][0].name == "shared-arch" and wt_hits[0][0].pinned is True


def test_synthetic_worktree_and_legacy_note_migration(tmp_path):
    home = tmp_path / "home"
    main_repo = tmp_path / "synthetic-main"
    wt_dir = tmp_path / "synthetic-wt"
    git_common = main_repo / ".git"
    wt_git_meta = git_common / "worktrees" / "synthetic-wt"
    wt_git_meta.mkdir(parents=True)
    wt_dir.mkdir(parents=True)

    (git_common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (wt_dir / ".git").write_text(f"gitdir: {wt_git_meta.as_posix()}\n", encoding="utf-8")
    (wt_git_meta / "commondir").write_text("../..\n", encoding="utf-8")
    (wt_git_meta / "gitdir").write_text(f"{(wt_dir / '.git').as_posix()}\n", encoding="utf-8")

    assert git_common_root(wt_dir) == main_repo.resolve()

    # Populate legacy per-worktree notes BEFORE first MemoryStore.for_project(wt_dir) call.
    legacy_dir = project_dir(wt_dir, home) / "memory"
    legacy_store = MemoryStore(home / ".mosscode" / "memory", legacy_dir)
    legacy_store.save("project", "legacy-wt-note", "reference", "旧版工作树笔记", "升级前保存在工作树下。")
    legacy_store.save("project", "conflict-note", "reference", "旧版冲突版本", "不应覆盖主仓库版本。")

    main_store = MemoryStore.for_project(main_repo, home=home)
    main_store.save("project", "conflict-note", "reference", "主仓库权威版本", "主仓库保留。")

    migrated_dir = memory_project_dir(wt_dir, home=home)
    assert migrated_dir == main_store.directory("project")

    wt_store = MemoryStore.for_project(wt_dir, home=home)
    names = {n.name: n for n in wt_store.load("project")}
    assert "legacy-wt-note" in names
    assert names["conflict-note"].description == "主仓库权威版本"  # Non-conflicting copy never overwrites main!

    # Deleting the migrated note must NOT resurrect it on subsequent calls.
    wt_store.delete("project", "legacy-wt-note")
    wt_store_again = MemoryStore.for_project(wt_dir, home=home)
    assert all(n.name != "legacy-wt-note" for n in wt_store_again.load("project"))


# 4. Security Tests: Spoofed .git, Path Traversal & Plan-Mode Permissions --------------------


def test_spoofed_dot_git_file_cannot_hijack_victim_repository_memory(tmp_path):
    home = tmp_path / "home"
    victim = tmp_path / "victim-repo"
    real_wt = tmp_path / "victim-wt"
    (victim / ".git" / "worktrees" / "wt").mkdir(parents=True)
    real_wt.mkdir()
    (victim / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (victim / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")
    (victim / ".git" / "worktrees" / "wt" / "gitdir").write_text(f"{(real_wt / '.git').as_posix()}\n", encoding="utf-8")
    (real_wt / ".git").write_text(f"gitdir: {(victim / '.git' / 'worktrees' / 'wt').as_posix()}\n", encoding="utf-8")

    victim_store = MemoryStore.for_project(victim, home=home)
    victim_store.save("project", "victim-secret-arch", "reference", "敏感项目架构", "仅属于 victim 仓库。")

    # Attack 1: Untrusted repo points `gitdir:` at victim's worktree metadata dir (backlink mismatch!).
    attacker1 = tmp_path / "untrusted-clone-1"
    attacker1.mkdir()
    (attacker1 / ".git").write_text(
        f"gitdir: {(victim / '.git' / 'worktrees' / 'wt').as_posix()}\n",
        encoding="utf-8",
    )
    assert git_common_root(attacker1) == attacker1.resolve()
    assert MemoryStore.for_project(attacker1, home=home).load("project") == []

    # Attack 2: Untrusted repo crafts local gitdir with `commondir: ../../../victim-repo/.git`.
    attacker2 = tmp_path / "untrusted-clone-2"
    fake_meta = attacker2 / ".fakegit" / "worktrees" / "x"
    fake_meta.mkdir(parents=True)
    (attacker2 / ".git").write_text(f"gitdir: {fake_meta.as_posix()}\n", encoding="utf-8")
    (fake_meta / "commondir").write_text(f"{(victim / '.git').as_posix()}\n", encoding="utf-8")
    (fake_meta / "gitdir").write_text(f"{(attacker2 / '.git').as_posix()}\n", encoding="utf-8")
    assert git_common_root(attacker2) == attacker2.resolve()
    assert MemoryStore.for_project(attacker2, home=home).load("project") == []

    # Malformed / oversized .git files and non-git directories fall back cleanly to resolved root.
    oversized = tmp_path / "oversized-git"
    oversized.mkdir()
    (oversized / ".git").write_text("gitdir: " + ("a" * 5000) + "\n", encoding="utf-8")
    assert git_common_root(oversized) == oversized.resolve()

    with tempfile.TemporaryDirectory(prefix="mosscode-non-git-") as raw_tmp:
        plain = Path(raw_tmp) / "plain-dir"
        plain.mkdir()
        if not any((parent / ".git").exists() for parent in (plain, *plain.parents)):
            assert git_common_root(plain) == plain.resolve()


def test_legacy_migration_falls_back_safely_when_primary_is_unwritable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    main_repo = tmp_path / "ro-main"
    wt_dir = tmp_path / "ro-wt"
    wt_meta = main_repo / ".git" / "worktrees" / "ro-wt"
    wt_meta.mkdir(parents=True)
    wt_dir.mkdir(parents=True)
    (main_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (wt_dir / ".git").write_text(f"gitdir: {wt_meta.as_posix()}\n", encoding="utf-8")
    (wt_meta / "commondir").write_text("../..\n", encoding="utf-8")
    (wt_meta / "gitdir").write_text(f"{(wt_dir / '.git').as_posix()}\n", encoding="utf-8")

    legacy_dir = project_dir(wt_dir, home) / "memory"
    legacy_store = MemoryStore(home / ".mosscode" / "memory", legacy_dir)
    legacy_store.save("project", "keep-safe", "reference", "只读回退测试", "主目录不可写时保留旧目录可读。")

    def fail_copy(*_args, **_kwargs):
        raise PermissionError("read-only primary")

    monkeypatch.setattr(paths_module.shutil, "copy2", fail_copy)
    resolved_dir = memory_project_dir(wt_dir, home=home)
    assert resolved_dir == legacy_dir
    assert [n.name for n in MemoryStore.for_project(wt_dir, home=home).load("project")] == ["keep-safe"]


@pytest.mark.parametrize("bad_name", ["../secret", "..\\secret", "sub/note", "note.md", "..", "C:note", "UpperCase"])
async def test_read_rejects_path_traversal_and_invalid_names(tmp_path, bad_name):
    conv, store = make_conv(tmp_path, mode="bypassPermissions")
    (tmp_path / "secret.md").write_text(
        "---\nname: secret\ndescription: d\ntype: user\nupdated: 2026-10-01\n---\noutside\n",
        encoding="utf-8",
    )
    with pytest.raises(MemoryFailure) as exc:
        store.read(bad_name)
    assert exc.value.code == "invalid_name"

    res = await conv.registry.execute(memory_call("bad", action="read", name=bad_name))
    assert res.is_error and res.code == "invalid_name"
    await conv.close()


async def test_plan_mode_allows_read_search_list_on_user_scope_while_gating_save_and_delete(tmp_path):
    conv, store = make_conv(tmp_path, mode="plan")
    store.save("user", "user-habit", "user", "用户长期偏好", "使用 pytest 运行测试", pinned=True)
    store.save("project", "proj-arch", "project", "项目结构说明", "核心模块说明\n原因：x\n适用方式：y")
    reg = conv.registry

    # Read-only actions (`read`, `search`, `list`) succeed in `plan` mode on `scope="user"` and `scope=None`!
    for idx, kwargs in enumerate(
        [
            {"action": "read", "scope": "user", "name": "user-habit"},
            {"action": "read", "name": "user-habit"},
            {"action": "search", "scope": "user", "query": "pytest"},
            {"action": "search", "query": "核心模块"},
            {"action": "list", "scope": "user"},
            {"action": "list"},
        ]
    ):
        result = await reg.execute(memory_call(f"ro-{idx}", **kwargs))
        assert not result.is_error, (kwargs, result)

    # Mutating `save` and `delete` on `scope="user"` remain gated by approval in `plan` mode!
    save_user = await reg.execute(
        memory_call(
            "su",
            action="save",
            scope="user",
            name="evil-global",
            type="user",
            description="企图全局写入",
            body="内容",
            pinned=True,
        )
    )
    assert save_user.is_error and save_user.code == "approval_required"
    assert not (store.directory("user") / "evil-global.md").exists()

    del_user = await reg.execute(memory_call("du", action="delete", scope="user", name="user-habit"))
    assert del_user.is_error and del_user.code == "approval_required"
    assert (store.directory("user") / "user-habit.md").exists()

    # Missing scope on save/delete returns clean validation error (never KeyError in PermissionPolicy.operation).
    no_scope = await reg.execute(memory_call("ns", action="save", name="x", type="user", description="d", body="b"))
    assert no_scope.is_error and no_scope.code == "invalid_scope"

    # Project-scope save/delete in `plan` mode remain allowed without prompting.
    save_proj = await reg.execute(
        memory_call(
            "sp",
            action="save",
            scope="project",
            name="plan-finding",
            type="project",
            description="规划阶段记录的项目发现",
            body="发现内容\n原因：调查结论\n适用方式：后续实现",
            pinned=True,
        )
    )
    assert not save_proj.is_error and store.read("plan-finding", scope="project")[0].pinned is True

    await conv.close()
