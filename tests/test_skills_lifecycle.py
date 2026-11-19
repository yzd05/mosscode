"""Lifecycle tests for ActiveSkills 64 KiB budget, LRU eviction, unload, /rewind alignment, and /status source."""

import json
from pathlib import Path

import pytest

from mosscode.builtin_commands import BUILTIN, _context_line, dispatch, status_report
from mosscode.checkpoints import Checkpoints, Snapshot, parse_active_skills
from mosscode.commands import CommandContext, parse_input
from mosscode.context import ContextBudget
from mosscode.messages import ToolCall, Usage
from mosscode.skills import ACTIVE_LIMIT, SkillCatalog
from mosscode.tools import create_default_registry
from mosscode.tools.base import ToolFailure
from test_agent import SequenceProvider, call, response
from test_commands import FakeUI
from test_sessions import drain, make


def _write_skill(
    base: Path, name: str, body: str = "按规程执行。", *, front: str = "", with_tool: bool = False
) -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    header = f"---\nname: {name}\ndescription: {name} 说明\n{front}---\n"
    (directory / "SKILL.md").write_text(header + body, encoding="utf-8")
    if with_tool:
        spec = [
            {
                "name": "run",
                "description": f"{name} 专属工具",
                "input_schema": {"type": "object", "properties": {}},
                "command": ["${PYTHON}", "-c", "print('ok')"],
            }
        ]
        (directory / "tool.json").write_text(json.dumps(spec), encoding="utf-8")
    return directory


def _load_call(call_id: str, name: str, arguments: str = "", *, unload: bool = False) -> ToolCall:
    payload: dict[str, object] = {"name": name, "arguments": arguments}
    if unload:
        payload["unload"] = True
    return ToolCall(call_id, "load_skill", json.dumps(payload))


def _make_conv(tmp_path: Path, skills_dir: Path, *responses, budget: ContextBudget | None = None):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    catalog = SkillCatalog.load(project, home=tmp_path / "home", reserved=BUILTIN.names(), builtin=builtin)
    provider = SequenceProvider(*responses)
    registry = create_default_registry(project, permission_mode="bypassPermissions", load_permissions=False)
    conv = make(tmp_path, provider, registry=registry, skills=catalog, budget=budget)
    return provider, conv, project


async def _run_slash(conv, text: str, ui: FakeUI | None = None) -> FakeUI:
    if ui is None:
        ui = FakeUI()
    commands, _ = BUILTIN.with_skills(conv.skills)
    parsed = parse_input(text, commands)
    assert parsed.kind == "command", parsed
    await dispatch(CommandContext(conv, ui, commands), parsed)
    return ui


async def test_sixty_kb_valid_skill_activates_under_64_kib_active_limit(tmp_path: Path):
    assert ACTIVE_LIMIT == 65536
    skills_dir = tmp_path / "project" / ".mosscode" / "skills"
    body_60k = "规程内容 ABCDEF1234567890\n" + ("x" * 60_000)  # ~60 KB UTF-8 bytes (< 64 KiB FILE_LIMIT)
    skill_path = _write_skill(skills_dir, "heavy-sop", body_60k, with_tool=True) / "SKILL.md"
    assert 60_000 <= len(skill_path.read_bytes()) <= 65_536

    # Also create a skill whose $ARGUMENTS expansion exceeds 65,536 chars when invoked with long args.
    _write_skill(skills_dir, "over-limit", " ".join(["$ARGUMENTS"] * 20))

    provider, conv, _ = _make_conv(
        tmp_path,
        skills_dir,
        response(calls=[_load_call("c1", "heavy-sop")]),
        response("已按 60KB 规程处理"),
    )
    await drain(conv.reply("请加载重型规程"))
    assert conv.active_skills == ("heavy-sop",)
    assert conv.registry.get("skill__heavy_sop__run") is not None
    supplements = provider.requests[1][1].supplements
    assert [s.kind for s in supplements[:2]] == ["environment", "active-skills"]
    assert "规程内容 ABCDEF1234567890" in supplements[1].text

    # Single skill whose rendered SOP exceeds ACTIVE_LIMIT is refused without evicting heavy-sop.
    with pytest.raises(ToolFailure) as exc:
        conv._load_skill("over-limit", "z" * 3500)
    assert exc.value.code == "skill_too_large"
    assert conv.active_skills == ("heavy-sop",)
    assert conv.registry.get("skill__heavy_sop__run") is not None
    await conv.close()


async def test_multiple_skills_exceeding_limit_lru_evict_oldest_and_unregister_tools(tmp_path: Path):
    skills_dir = tmp_path / "project" / ".mosscode" / "skills"
    for name in ("alpha", "beta", "gamma", "delta"):
        _write_skill(skills_dir, name, f"{name}: $ARGUMENTS\n" + ("x" * 24_000), with_tool=True)

    _, conv, _ = _make_conv(tmp_path, skills_dir)

    # Load alpha (~24 KB) then beta (~24 KB) -> ~48 KB <= 65,536, both fit.
    conv._load_skill("alpha", "v1")
    conv._load_skill("beta", "v1")
    assert conv.active_skills == ("alpha", "beta")
    assert {"skill__alpha__run", "skill__beta__run"} <= set(conv.registry.names())

    # Refresh alpha so alpha becomes MRU and beta becomes LRU!
    refreshed_msg, refreshed_details = conv._load_skill("alpha", "v2")
    assert refreshed_msg.startswith("已刷新 Skill alpha")
    assert refreshed_details == {"skill": "alpha", "arguments": "v2", "refreshed": True}
    # Prompt order stays ("alpha", "beta") for cache stability, while LRU order is ("beta", "alpha").
    assert conv.active_skills == ("alpha", "beta")
    assert conv._agent.active_skills.lru_names == ("beta", "alpha")

    # Loading gamma (~24 KB -> total ~72 KB > 65,536) must evict LRU "beta" (NOT recently refreshed "alpha"!).
    out, details = conv._load_skill("gamma", "v1")
    assert details["evicted"] == ["beta"]
    assert "自动卸载：beta" in out
    assert conv.active_skills == ("alpha", "gamma")
    assert "skill__beta__run" not in conv.registry.names()
    assert {"skill__alpha__run", "skill__gamma__run"} <= set(conv.registry.names())

    # Loading delta (~24 KB) now evicts "alpha" (older than "gamma").
    out2, details2 = conv._load_skill("delta", "v1")
    assert details2["evicted"] == ["alpha"]
    assert "自动卸载：alpha" in out2
    assert conv.active_skills == ("gamma", "delta")
    assert "skill__alpha__run" not in conv.registry.names()
    assert {"skill__gamma__run", "skill__delta__run"} <= set(conv.registry.names())
    await conv.close()


async def test_load_skill_unload_param_and_slash_skills_unload(tmp_path: Path):
    skills_dir = tmp_path / "project" / ".mosscode" / "skills"
    _write_skill(skills_dir, "alpha", "规程 A", with_tool=True)
    _write_skill(skills_dir, "beta", "规程 B", with_tool=True)

    provider, conv, _ = _make_conv(
        tmp_path,
        skills_dir,
        response(calls=[_load_call("c1", "alpha"), _load_call("c2", "beta")]),
        response("已加载两个"),
        response(calls=[_load_call("c3", "alpha", unload=True)]),
        response("已卸载 alpha"),
        response("全部卸载后"),
    )

    await drain(conv.reply("加载两个"))
    assert conv.active_skills == ("alpha", "beta")
    assert {"skill__alpha__run", "skill__beta__run"} <= set(conv.registry.names())

    # Model unloads alpha via load_skill(name="alpha", unload=True) during a turn.
    await drain(conv.reply("卸载 alpha"))
    assert conv.active_skills == ("beta",)
    assert "skill__alpha__run" not in conv.registry.names()
    assert "skill__beta__run" in conv.registry.names()
    unload_result = conv.history[-2].tool_results[0]
    assert not unload_result.is_error and "已卸载 Skill alpha" in unload_result.output

    # Autocomplete offers "unload beta" while beta is active.
    commands, _ = BUILTIN.with_skills(conv.skills)
    hints = [h.value for h in commands.matches(CommandContext(conv, FakeUI(), commands), "/skills unload ")]
    assert hints == ["/skills unload beta"]

    # User unloads beta manually via `/skills unload beta`.
    ui = await _run_slash(conv, "/skills unload beta")
    assert ui.notes == ["└ 已卸载 Skill beta。"]
    assert ("refresh",) in ui.calls
    assert conv.active_skills == ()
    assert "skill__beta__run" not in conv.registry.names()

    # Subsequent request has no <active-skills> supplement at all.
    await drain(conv.reply("继续"))
    last_supplements = [s.kind for s in provider.requests[-1][1].supplements]
    assert "active-skills" not in last_supplements

    # Unloading an inactive known skill or unknown skill via /skills unload reports clearly.
    assert (await _run_slash(conv, "/skills unload beta")).notes == ["Skill beta 当前未激活。"]
    assert (await _run_slash(conv, "/skills unload missing")).notes == ["未找到已激活的 Skill missing。"]
    assert (await _run_slash(conv, "/skills unload")).notes == ["用法：/skills unload <名称>"]
    await conv.close()


async def test_rewind_restores_active_skills_tools_meter_and_persisted_checkpoints(tmp_path: Path):
    skills_dir = tmp_path / "project" / ".mosscode" / "skills"
    _write_skill(skills_dir, "alpha", "规程 A $ARGUMENTS\n" + ("a" * 24_000), with_tool=True)
    _write_skill(skills_dir, "beta", "规程 B $ARGUMENTS\n" + ("b" * 24_000), with_tool=True)
    _write_skill(skills_dir, "gamma", "规程 G $ARGUMENTS\n" + ("g" * 24_000), with_tool=True)

    _, conv, project = _make_conv(
        tmp_path,
        skills_dir,
        # Turn 1: load alpha("v1")
        response(calls=[_load_call("l1", "alpha", "v1")], usage=Usage(2_000, 100)),
        response("第一轮完", usage=Usage(2_500, 100)),
        # Turn 2: load beta("v2") + write file note.txt
        response(
            calls=[_load_call("l2", "beta", "v2"), call("write_file", "w1", path="note.txt", content="t2")],
            usage=Usage(3_500, 100),
        ),
        response("第二轮完", usage=Usage(4_000, 100)),
        # Turn 3: load gamma("v3") -> LRU-evicts alpha!
        response(calls=[_load_call("l3", "gamma", "v3")], usage=Usage(5_000, 100)),
        response("第三轮完", usage=Usage(5_500, 100)),
    )

    await conv.ask("第一轮")
    assert conv.active_skills == ("alpha",)
    await conv.ask("第二轮")
    assert conv.active_skills == ("alpha", "beta")
    await conv.ask("第三轮")
    assert conv.active_skills == ("beta", "gamma")
    assert "skill__alpha__run" not in conv.registry.names()
    assert {"skill__beta__run", "skill__gamma__run"} <= set(conv.registry.names())
    assert conv._agent.meter.anchor is not None

    # Check persisted Snapshot carries active_skills and old records default to ().
    assert parse_active_skills(None) == ()
    assert parse_active_skills([["alpha", "v1"]]) == (("alpha", "v1"),)
    assert Snapshot("m", "note.txt", None).active_skills == ()
    assert conv.checkpoints.snapshots[0].active_skills == (("alpha", "v1"),)

    points = conv.rewind_points()  # [Turn 3, Turn 2, Turn 1]
    assert [p.message.text for p in points] == ["第三轮", "第二轮", "第一轮"]

    # Rewind before Turn 3: restores ("alpha", "beta") (re-registering evicted alpha's tool & dropping gamma's!).
    await conv.rewind(points[0].message.id, code=False, conversation=True)
    assert conv.active_skills == ("alpha", "beta")
    assert [item.arguments for item in conv._agent.active_skills.items()] == ["v1", "v2"]
    assert "skill__gamma__run" not in conv.registry.names()
    assert {"skill__alpha__run", "skill__beta__run"} <= set(conv.registry.names())
    assert conv._agent.meter.anchor is None

    # Rewind before Turn 2 (code + conversation): restores ("alpha",), removes beta's tool & deletes note.txt.
    await conv.rewind(points[1].message.id, code=True, conversation=True)
    assert conv.active_skills == ("alpha",)
    assert not (project / "note.txt").exists()
    assert "skill__alpha__run" in conv.registry.names()
    assert "skill__beta__run" not in conv.registry.names()

    # Resume from transcript preserves post-rewind skill state (only alpha, NOT beta/gamma).
    session_id = conv.session_id
    await conv.close()
    _, resumed_conv, _ = _make_conv(tmp_path, skills_dir)
    await drain(resumed_conv.resume(session_id))
    assert resumed_conv.active_skills == ("alpha",)
    assert "skill__alpha__run" in resumed_conv.registry.names()
    assert "skill__beta__run" not in resumed_conv.registry.names()

    # Rewind before Turn 1 on resumed conversation: clears alpha and its tool too.
    first_point = resumed_conv.rewind_points()[0]
    await resumed_conv.rewind(first_point.message.id, code=False, conversation=True)
    assert resumed_conv.active_skills == ()
    assert "skill__alpha__run" not in resumed_conv.registry.names()
    await resumed_conv.close()


async def test_lru_eviction_and_refresh_recency_persist_accurately_across_resume(tmp_path: Path):
    skills_dir = tmp_path / "project" / ".mosscode" / "skills"
    for name in ("alpha", "beta", "gamma", "delta"):
        _write_skill(skills_dir, name, f"{name}: $ARGUMENTS\n" + ("x" * 24_000), with_tool=True)

    # Case 1: load alpha -> load beta -> refresh alpha (MRU) -> load gamma (evicts beta) -> /resume
    _, conv1, _ = _make_conv(
        tmp_path,
        skills_dir,
        response(calls=[_load_call("a1", "alpha", "v1"), _load_call("b1", "beta", "v1")]),
        response("已加载两个"),
        response(calls=[_load_call("a2", "alpha", "v2"), _load_call("g1", "gamma", "v1")]),
        response("已刷新 alpha 并淘汰 beta"),
    )
    await conv1.ask("第一轮")
    await conv1.ask("第二轮")
    assert conv1.active_skills == ("alpha", "gamma")
    assert "skill__beta__run" not in conv1.registry.names()
    sid1 = conv1.session_id
    await conv1.close()

    _, resumed1, _ = _make_conv(tmp_path, skills_dir)
    await drain(resumed1.resume(sid1))
    assert resumed1.active_skills == ("alpha", "gamma")
    assert [item.arguments for item in resumed1._agent.active_skills.items()] == ["v2", "v1"]
    assert "skill__beta__run" not in resumed1.registry.names()
    assert {"skill__alpha__run", "skill__gamma__run"} <= set(resumed1.registry.names())

    # Post-resume rewind before Turn 2 restores ("alpha", "beta") with alpha="v1"!
    turn2_pt = resumed1.rewind_points()[0]
    await resumed1.rewind(turn2_pt.message.id, code=False, conversation=True)
    assert resumed1.active_skills == ("alpha", "beta")
    assert [item.arguments for item in resumed1._agent.active_skills.items()] == ["v1", "v1"]
    assert "skill__gamma__run" not in resumed1.registry.names()
    assert {"skill__alpha__run", "skill__beta__run"} <= set(resumed1.registry.names())
    await resumed1.close()

    # Case 2: load alpha -> load beta -> load gamma (evicts alpha) -> unload gamma -> /resume (only beta stays!)
    tmp_case2 = tmp_path / "case2"
    skills_dir2 = tmp_case2 / "project" / ".mosscode" / "skills"
    for name in ("alpha", "beta", "gamma"):
        _write_skill(skills_dir2, name, f"{name}: $ARGUMENTS\n" + ("y" * 24_000), with_tool=True)
    _, conv2, _ = _make_conv(
        tmp_case2,
        skills_dir2,
        response(
            calls=[
                _load_call("c1", "alpha", "1"),
                _load_call("c2", "beta", "1"),
                _load_call("c3", "gamma", "1"),
            ]
        ),
        response("已淘汰 alpha"),
    )
    await conv2.ask("加载三个")
    assert conv2.unload_skill("gamma") is True
    assert conv2.active_skills == ("beta",)
    assert set(conv2._skill_tools) == {"beta"}
    sid2 = conv2.session_id
    await conv2.close()

    _, resumed2, _ = _make_conv(tmp_case2, skills_dir2)
    await drain(resumed2.resume(sid2))
    assert resumed2.active_skills == ("beta",)
    assert "skill__alpha__run" not in resumed2.registry.names()
    assert "skill__gamma__run" not in resumed2.registry.names()
    assert "skill__beta__run" in resumed2.registry.names()
    await resumed2.close()

    # Case 3: load alpha -> load beta -> refresh alpha (no eviction yet) -> /resume -> load delta evicts beta (LRU)!
    tmp_case3 = tmp_path / "case3"
    skills_dir3 = tmp_case3 / "project" / ".mosscode" / "skills"
    for name in ("alpha", "beta", "delta"):
        _write_skill(skills_dir3, name, f"{name}: $ARGUMENTS\n" + ("z" * 24_000), with_tool=True)
    _, conv3, _ = _make_conv(
        tmp_case3,
        skills_dir3,
        response(
            calls=[
                _load_call("r1", "alpha", "v1"),
                _load_call("r2", "beta", "v1"),
                _load_call("r3", "alpha", "v2"),
            ]
        ),
        response("已刷新 alpha"),
    )
    await conv3.ask("加载并刷新")
    assert conv3.active_skills == ("alpha", "beta")
    assert conv3._agent.active_skills.lru_names == ("beta", "alpha")
    sid3 = conv3.session_id
    await conv3.close()

    _, resumed3, _ = _make_conv(tmp_case3, skills_dir3)
    await drain(resumed3.resume(sid3))
    assert resumed3.active_skills == ("alpha", "beta")
    assert resumed3._agent.active_skills.lru_names == ("beta", "alpha")
    _, details = resumed3._load_skill("delta", "v1")
    assert details["evicted"] == ["beta"]
    assert resumed3.active_skills == ("alpha", "delta")
    await resumed3.close()


def test_checkpoints_load_defaults_missing_active_skills_on_legacy_snapshots(tmp_path: Path):
    from mosscode.session import Session

    cp = Checkpoints(tmp_path, Session(tmp_path / "s"))
    legacy = Snapshot("m1", "a.py", None)
    cp.load([legacy], {"m1": 0})
    assert cp.skills_before("m1") == ()
    assert cp.skills_before("unknown") is None


async def test_status_context_line_formats_reported_mixed_and_estimated_sources(tmp_path: Path):
    skills_dir = tmp_path / "project" / ".mosscode" / "skills"
    _write_skill(skills_dir, "note-sop", "简短规程" * 100)  # No exclusive tools -> anchor kept on load/unload!
    budget = ContextBudget.for_provider(100_000, 20_000)  # effective = 80,000, auto_threshold = 67,000

    _, conv, _ = _make_conv(
        tmp_path,
        skills_dir,
        response("收到", usage=Usage(input_tokens=15_800, output_tokens=200)),
    )
    conv._agent.budget = budget

    # 1. Before any request: "尚未发送请求"
    assert _context_line(conv) == "尚未发送请求"

    # 2. Right after server-reported usage with no subsequent changes: "服务端返回" (no "约 ")
    await conv.ask("你好")
    snap_reported = conv.context_snapshot()
    assert snap_reported.source == "reported" and snap_reported.tokens == 16_000
    assert _context_line(conv) == "16,000 / 80,000 Token（20%，服务端返回），自动压缩阈值 67,000；已压缩 0 次"

    # 3. Loading a tool-less skill adds <active-skills> supplement tokens -> "mixed" (+delta)
    conv._load_skill("note-sop", "")
    snap_mixed = conv.context_snapshot()
    assert snap_mixed.source == "mixed"
    assert snap_mixed.anchored_tokens == 16_000 and snap_mixed.delta_tokens > 0
    expected_mixed = (
        f"约 {snap_mixed.tokens:,} / 80,000 Token"
        f"（{round(100 * snap_mixed.tokens / 80_000)}%，服务端 16,000 + 估算 {snap_mixed.delta_tokens:+,}），"
        "自动压缩阈值 67,000；已压缩 0 次"
    )
    assert _context_line(conv) == expected_mixed
    assert expected_mixed in status_report(CommandContext(conv, FakeUI(), BUILTIN))

    # 4. Entering plan mode changes tool definitions, invalidating anchor -> "estimated"
    conv.set_mode("plan")
    snap_est = conv.context_snapshot()
    assert snap_est.source == "estimated"
    expected_est = (
        f"约 {snap_est.tokens:,} / 80,000 Token"
        f"（{round(100 * snap_est.tokens / 80_000)}%，估算），自动压缩阈值 67,000；已压缩 0 次"
    )
    assert _context_line(conv) == expected_est
    await conv.close()
