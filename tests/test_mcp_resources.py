import json
import sys
from pathlib import Path

import pytest

from mosscode.commands import NAME as COMMAND_NAME_RE
from mosscode.mcp_client import (
    MCPManager,
    MCPPromptArg,
    MCPPromptInfo,
    MCPResourceInfo,
    MCPResourceTemplateInfo,
    ServerStatus,
    prompt_command_name,
    tool_name,
)
from mosscode.mcp_config import MCPSettings, ServerConfig
from mosscode.messages import ToolCall
from mosscode.permissions import Operation, parse_rule
from mosscode.session import Session
from mosscode.tools import create_default_registry
from mosscode.tools.base import ToolFailure
from mosscode.tools.mcp_resources import ListMCPResourcesTool, ReadMCPResourceTool

# A 3x2 red PNG (same deterministic fixture bytes used in test_mcp.py).
PICTURE = "iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAEElEQVR4nGP8zwAFTDAGAwATKQED8NgHhAAAAABJRU5ErkJggg=="

SERVER_SCRIPT = """
import base64
import json
import os
import sys
import time

mode = sys.argv[1]
PICTURE = sys.argv[2]

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def send(msg_id, *, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    print(json.dumps(payload, ensure_ascii=False), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    mid = req["id"]
    method = req["method"]
    params = req.get("params") or {}

    if method == "initialize":
        if mode == "tools_only":
            caps = {"tools": {}}
        elif mode == "resources_only":
            caps = {"resources": {}}
        elif mode == "prompts_only":
            caps = {"prompts": {}}
        elif mode in {"res_no_templates", "cursor_cycle"}:
            caps = {"resources": {}}
        elif mode == "dup_prompts":
            caps = {"prompts": {}}
        else:
            caps = {"tools": {}, "resources": {}, "prompts": {}}
        send(
            mid,
            result={
                "protocolVersion": params["protocolVersion"],
                "capabilities": caps,
                "serverInfo": {"name": f"fixture-{mode}", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        if mode in {"resources_only", "prompts_only", "res_no_templates", "cursor_cycle", "dup_prompts"}:
            send(mid, error={"code": -32601, "message": "Method not found"})
        else:
            schema = {"type": "object", "properties": {"x": {"type": "string"}}}
            send(mid, result={"tools": [{"name": "ping", "inputSchema": schema}]})
    elif method == "resources/list":
        cursor = params.get("cursor")
        if mode == "cursor_cycle":
            send(
                mid,
                result={
                    "resources": [{"uri": "memo://a", "name": "a"}],
                    "nextCursor": "loop",
                },
            )
        elif not cursor:
            send(
                mid,
                result={
                    "resources": [
                        {
                            "uri": "memo://today",
                            "name": "today",
                            "title": "Today Memo",
                            "description": "今日纪要",
                            "mimeType": "text/plain",
                            "size": 12,
                        },
                        {
                            "uri": "memo://private/secret",
                            "name": "private-note",
                            "description": "内部私密记录",
                            "mimeType": "text/plain",
                        },
                    ],
                    "nextCursor": "page2",
                },
            )
        else:
            send(
                mid,
                result={
                    "resources": [
                        {
                            "uri": "memo://chart.png",
                            "name": "chart",
                            "description": "统计图",
                            "mimeType": "image/png",
                        }
                    ]
                },
            )
    elif method == "resources/templates/list":
        if mode in {"resources_only", "res_no_templates"}:
            if mode == "res_no_templates":
                send(mid, error={"code": -32601, "message": "Method not found"})
            else:
                send(
                    mid,
                    result={
                        "resourceTemplates": [
                            {
                                "uriTemplate": "memo://notes/{id}",
                                "name": "note-by-id",
                                "description": "按编号读取笔记",
                                "mimeType": "text/markdown",
                            }
                        ]
                    },
                )
        elif not params.get("cursor"):
            send(
                mid,
                result={
                    "resourceTemplates": [
                        {
                            "uriTemplate": "memo://notes/{id}",
                            "name": "note-by-id",
                            "description": "按编号读取笔记",
                            "mimeType": "text/markdown",
                        }
                    ],
                    "nextCursor": "tpl2",
                },
            )
        else:
            send(
                mid,
                result={
                    "resourceTemplates": [
                        {
                            "uriTemplate": "memo://archive/{year}",
                            "name": "archive-by-year",
                            "title": "Yearly Archive",
                        }
                    ]
                },
            )
    elif method == "prompts/list":
        if mode == "dup_prompts":
            send(
                mid,
                result={
                    "prompts": [
                        {"name": "dup", "description": "first"},
                        {"name": "dup", "description": "second"},
                    ]
                },
            )
        elif not params.get("cursor"):
            send(
                mid,
                result={
                    "prompts": [
                        {
                            "name": "summarize",
                            "description": "总结一段内容",
                            "arguments": [
                                {"name": "topic", "description": "主题", "required": True},
                            ],
                        }
                    ],
                    "nextCursor": "p2",
                },
            )
        else:
            send(
                mid,
                result={
                    "prompts": [
                        {
                            "name": "review-code",
                            "title": "Code Review",
                            "arguments": [
                                {"name": "lang", "description": "语言", "required": False},
                            ],
                        }
                    ]
                },
            )
    elif method == "resources/read":
        uri = params["uri"]
        if uri == "memo://today":
            send(
                mid,
                result={
                    "contents": [
                        {"uri": uri, "mimeType": "text/plain", "text": "你好，今日待办已完成。"}
                    ]
                },
            )
        elif uri == "memo://private/secret":
            send(
                mid,
                result={
                    "contents": [
                        {"uri": uri, "mimeType": "text/plain", "text": "TOP_SECRET_VALUE"}
                    ]
                },
            )
        elif uri == "memo://huge":
            # 30,000 Chinese chars = 90,000 UTF-8 bytes (> 64 KiB).
            send(
                mid,
                result={
                    "contents": [
                        {"uri": uri, "mimeType": "text/plain", "text": "中" * 30000}
                    ]
                },
            )
        elif uri == "memo://chart.png":
            send(
                mid,
                result={
                    "contents": [
                        {"uri": uri, "mimeType": "image/png", "blob": PICTURE}
                    ]
                },
            )
        elif uri == "memo://bad-image":
            send(
                mid,
                result={
                    "contents": [
                        {"uri": uri, "mimeType": "image/png", "blob": base64.b64encode(b"not-a-png").decode()}
                    ]
                },
            )
        elif uri == "memo://doc.pdf":
            blob = base64.b64encode(b"%PDF-1.7-demo").decode("ascii")
            send(
                mid,
                result={
                    "contents": [
                        {"uri": uri, "mimeType": "application/pdf", "blob": blob}
                    ]
                },
            )
        elif uri == "memo://raw.bin":
            blob = base64.b64encode(b"\\x00\\x01\\x02\\x03").decode("ascii")
            send(mid, result={"contents": [{"uri": uri, "blob": blob}]})
        elif uri == "memo://missing":
            send(mid, error={"code": -32002, "message": "private-server-path-leak"})
        elif uri == "memo://invalid":
            send(mid, error={"code": -32602, "message": "invalid-uri-secret"})
        elif uri == "memo://slow":
            time.sleep(5)
            send(mid, result={"contents": [{"uri": uri, "text": "late"}]})
        elif uri == "memo://crash":
            os._exit(0)
        else:
            send(mid, result={"contents": []})
    elif method == "prompts/get":
        name = params["name"]
        args = params.get("arguments") or {}
        if name == "summarize":
            topic = args.get("topic", "默认主题")
            send(
                mid,
                result={
                    "description": "",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": f"请用中文总结：{topic}"},
                        }
                    ],
                },
            )
        elif name == "dialogue":
            send(
                mid,
                result={
                    "description": "多轮对话模板",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": f"检查语言：{args.get('lang', 'python')}"},
                        },
                        {
                            "role": "assistant",
                            "content": {"type": "text", "text": "请粘贴需要检查的片段。"},
                        },
                    ],
                },
            )
        elif name == "rich":
            pdf_blob = base64.b64encode(b"PDFDATA").decode("ascii")
            send(
                mid,
                result={
                    "description": "富媒体提示词",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "image", "data": PICTURE, "mimeType": "image/png"},
                        },
                        {
                            "role": "user",
                            "content": {
                                "type": "resource",
                                "resource": {
                                    "uri": "memo://snippet.py",
                                    "mimeType": "text/x-python",
                                    "text": "print('ok')",
                                },
                            },
                        },
                        {
                            "role": "user",
                            "content": {
                                "type": "resource",
                                "resource": {
                                    "uri": "memo://spec.pdf",
                                    "mimeType": "application/pdf",
                                    "blob": pdf_blob,
                                },
                            },
                        },
                    ],
                },
            )
        elif name == "huge":
            send(
                mid,
                result={
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": "长" * 30000}}
                    ]
                },
            )
        elif name == "bad":
            send(mid, error={"code": -32602, "message": "missing required argument"})
        else:
            send(mid, error={"code": -32601, "message": "unknown prompt"})
"""


def write_fixture(tmp_path: Path) -> Path:
    script = tmp_path / "mcp_eco_server.py"
    script.write_text(SERVER_SCRIPT, encoding="utf-8")
    return script


def make_config(script: Path, name: str, mode: str = "full", *, timeout: float = 30.0) -> ServerConfig:
    return ServerConfig(name, "stdio", sys.executable, (str(script), mode, PICTURE), timeout=timeout)


def call(name: str, **args) -> ToolCall:
    return ToolCall("call_1", name, json.dumps(args, ensure_ascii=False))


def register_resource_tools(reg, manager: MCPManager) -> None:
    reg.register(ListMCPResourcesTool(reg.permissions.root, manager, permissions=reg.permissions))
    reg.register(ReadMCPResourceTool(reg.permissions.root, manager, permissions=reg.permissions))


def test_prompt_command_name_normalization_and_collision_resistance():
    assert prompt_command_name("notes", "summarize") == "mcp__notes__summarize"
    assert prompt_command_name("my-srv_1", "code-review_2") == "mcp__my-srv_1__code-review_2"

    candidates = [
        ("notes", "search"),
        ("Notes", "search"),
        ("notes", "Search"),
        ("Notes", "Search"),
        ("a", "b__c"),
        ("a__b", "c"),
        ("srv/1", "prompt.v1"),
        ("srv 1", "prompt v1"),
        ("中文服务", "总结提示"),
        ("s" * 50, "p" * 50),
        ("-leading", "9prompt"),
    ]
    generated = [prompt_command_name(s, p) for s, p in candidates]
    assert len(set(generated)) == len(generated)
    for cmd in generated:
        assert len(cmd) <= 64
        assert COMMAND_NAME_RE.fullmatch(cmd), cmd


def test_server_status_backwards_compatibility_and_counts():
    tools_only = ServerStatus("alpha", "stdio", "ready", tools=("t1", "t2"))
    assert tools_only.describe() == "已连接 · 2 个工具"
    assert tools_only.notice() == "MCP alpha 已连接 · 注册 2 个工具。"

    rich = ServerStatus(
        "beta",
        "stdio",
        "ready",
        tools=("t1",),
        resources=(MCPResourceInfo("beta", "memo://1", "one"),),
        resource_templates=(MCPResourceTemplateInfo("beta", "memo://{id}", "tpl"),),
        prompts=(MCPPromptInfo("beta", "sum", "mcp__beta__sum", arguments=(MCPPromptArg("q", required=True),)),),
    )
    assert rich.describe() == "已连接 · 1 个工具 · 2 个资源 · 1 个提示词"
    assert rich.notice() == "MCP beta 已连接 · 注册 1 个工具 · 2 个资源 · 1 个提示词。"


async def test_paginated_discovery_capability_subsets_and_error_isolation(tmp_path):
    script = write_fixture(tmp_path)
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    manager = MCPManager(
        tmp_path,
        MCPSettings(
            (
                make_config(script, "full", "full"),
                make_config(script, "tools_only", "tools_only"),
                make_config(script, "res_only", "resources_only"),
                make_config(script, "prm_only", "prompts_only"),
                make_config(script, "no_tpl", "res_no_templates"),
                make_config(script, "cycle", "cursor_cycle"),
                make_config(script, "dup", "dup_prompts"),
            )
        ),
        startup_timeout=5,
    )
    try:
        notices = await manager.start(reg)
        by_name = {s.name: s for s in manager.statuses()}

        assert by_name["full"].state == "ready"
        assert [r.uri for r in by_name["full"].resources] == [
            "memo://today",
            "memo://private/secret",
            "memo://chart.png",
        ]
        assert [t.uri_template for t in by_name["full"].resource_templates] == [
            "memo://notes/{id}",
            "memo://archive/{year}",
        ]
        assert [p.command_name for p in by_name["full"].prompts] == [
            "mcp__full__summarize",
            "mcp__full__review-code",
        ]
        assert by_name["full"].prompts[0].arguments == (MCPPromptArg("topic", "主题", True),)
        assert any("MCP full 已连接 · 注册 1 个工具 · 5 个资源 · 2 个提示词。" in n for n in notices)

        # Subset servers succeed cleanly even when unadvertised list methods return -32601.
        assert by_name["tools_only"].state == "ready"
        assert len(by_name["tools_only"].tools) == 1
        assert by_name["tools_only"].resources == () and by_name["tools_only"].prompts == ()
        assert tool_name("tools_only", "ping") in {d.name for d in reg.definitions()}

        assert by_name["res_only"].state == "ready"
        assert by_name["res_only"].tools == () and len(by_name["res_only"].resources) == 3
        assert len(by_name["res_only"].resource_templates) == 1 and by_name["res_only"].prompts == ()

        assert by_name["prm_only"].state == "ready"
        assert by_name["prm_only"].tools == () and by_name["prm_only"].resources == ()
        assert len(by_name["prm_only"].prompts) == 2

        # Advertising resources while returning -32601 for resources/templates/list falls back to empty templates.
        assert by_name["no_tpl"].state == "ready"
        assert len(by_name["no_tpl"].resources) == 3 and by_name["no_tpl"].resource_templates == ()

        # Cursor cycle and duplicate prompt names fail only those servers without breaking the others.
        assert by_name["cycle"].state == "failed" and "重复分页游标" in by_name["cycle"].detail
        assert by_name["dup"].state == "failed" and "重复提示词名" in by_name["dup"].detail

        assert manager.has_server("full") and not manager.has_server("absent")
        assert manager.connection_for("full") is not None and manager.connection_for("absent") is None
        assert all(r.server != "cycle" for r in manager.list_resources())
        assert [r.uri for r in manager.list_resources("full")] == [
            "memo://today",
            "memo://private/secret",
            "memo://chart.png",
        ]
        assert len(manager.list_resource_templates("full")) == 2
        assert len(manager.list_prompts("prm_only")) == 2
    finally:
        await manager.close()


async def test_read_mcp_resource_text_truncation_images_blobs_and_errors(tmp_path):
    script = write_fixture(tmp_path)
    reg = create_default_registry(tmp_path, permission_mode="default", load_permissions=False)
    reg.use_session(Session(tmp_path / "session"))
    manager = MCPManager(
        tmp_path,
        MCPSettings(
            (
                make_config(script, "notes", "full"),
                make_config(script, "timed", "full", timeout=0.15),
                make_config(script, "crasher", "full"),
            )
        ),
        startup_timeout=5,
    )
    register_resource_tools(reg, manager)
    try:
        await manager.start(reg)

        listed = await reg.execute(call("list_mcp_resources", server="notes"))
        assert not listed.is_error
        assert "memo://today" in listed.output and "今日纪要" in listed.output
        assert "memo://notes/{id}" in listed.output and "按编号读取笔记" in listed.output

        no_tpl = await reg.execute(call("list_mcp_resources", server="notes", include_templates=False))
        assert "memo://today" in no_tpl.output and "memo://notes/{id}" not in no_tpl.output

        # Plain UTF-8 text resource works in default mode without interactive approval.
        today = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://today"))
        assert not today.is_error and today.output == "你好，今日待办已完成。"

        # >64 KiB text truncation preserves UTF-8 validity and emits the truncation notice.
        huge_text, huge_refs, huge_details = await manager.read_resource("notes", "memo://huge")
        assert huge_refs == () and huge_details["truncated"] is True
        assert huge_text.endswith("…（MCP 资源内容超过 64 KB，已截断）")
        body_before_note = huge_text.split("\n…（MCP 资源内容超过 64 KB，已截断）")[0]
        assert len(body_before_note.encode("utf-8")) <= 65536

        huge_tool = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://huge"))
        assert not huge_tool.is_error and "MCP 资源内容超过 64 KB，已截断" in huge_tool.output

        # Image blob attaches ImageRef and stores under session/images when accepts_images=True.
        chart = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://chart.png"))
        assert not chart.is_error
        assert [(img.width, img.height, img.media_type) for img in chart.images] == [(3, 2, "image/png")]
        assert chart.images[0].path.is_file()
        assert "notes/memo://chart.png 的图片 1 · 图片 · 3×2" in chart.output
        assert PICTURE not in chart.output

        # Text-only models omit ImageRef cleanly with explanation.
        reg.images = False
        chart_text_only = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://chart.png"))
        assert not chart_text_only.images and "当前模型不支持图片，已省略" in chart_text_only.output
        reg.images = True

        # Corrupt image data degrades safely without crashing or attaching broken images.
        bad_img = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://bad-image"))
        assert not bad_img.is_error and not bad_img.images and "不是可识别的图片" in bad_img.output

        # Non-image binary blobs emit safe byte-length summaries without dumping base64.
        pdf = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://doc.pdf"))
        assert not pdf.is_error and not pdf.images
        assert pdf.output == "[二进制资源 memo://doc.pdf · application/pdf · 13 字节]"

        raw = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://raw.bin"))
        assert not raw.is_error and raw.output == "[二进制资源 memo://raw.bin · application/octet-stream · 4 字节]"

        # Invalid URI and protocol errors (-32002 / -32602) do NOT kill the connection or leak server internals.
        bad_uri = await reg.execute(call("read_mcp_resource", server="notes", uri="not a uri without scheme"))
        assert bad_uri.is_error and bad_uri.code == "invalid_arguments"

        missing = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://missing"))
        assert missing.is_error and missing.code == "mcp_protocol_error"
        assert missing.details["rpc_code"] == -32002 and "private-server-path-leak" not in missing.output

        invalid = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://invalid"))
        assert invalid.is_error and invalid.code == "mcp_protocol_error"
        assert invalid.details["rpc_code"] == -32602 and "invalid-uri-secret" not in invalid.output

        # Session remains alive after protocol errors!
        still_ok = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://today"))
        assert not still_ok.is_error and still_ok.output == "你好，今日待办已完成。"

        # Unknown server error.
        unknown = await reg.execute(call("read_mcp_resource", server="ghost", uri="memo://today"))
        assert unknown.is_error and unknown.code == "mcp_server_not_found"

        # Timeout closes that connection and subsequent reads report mcp_unavailable.
        timed = await reg.execute(call("read_mcp_resource", server="timed", uri="memo://slow"))
        assert timed.is_error and timed.code == "timeout"
        after_timeout = await reg.execute(call("read_mcp_resource", server="timed", uri="memo://today"))
        assert after_timeout.is_error and after_timeout.code == "mcp_unavailable"

        # Process crash / disconnect closes that connection and removes it from list_resources.
        disconnected = await reg.execute(call("read_mcp_resource", server="crasher", uri="memo://crash"))
        assert disconnected.is_error and disconnected.code == "mcp_disconnected"
        assert manager.list_resources("crasher") == ()
        after_crash = await reg.execute(call("read_mcp_resource", server="crasher", uri="memo://today"))
        assert after_crash.is_error and after_crash.code == "mcp_unavailable"
    finally:
        await manager.close()


async def test_get_prompt_single_multi_images_embedded_resources_and_truncation(tmp_path):
    script = write_fixture(tmp_path)
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    session = Session(tmp_path / "session")
    reg.use_session(session)
    manager = MCPManager(tmp_path, MCPSettings((make_config(script, "notes", "full"),)), startup_timeout=5)
    try:
        await manager.start(reg)

        # Single user message omits role header for natural prompt feel.
        single_text, single_refs = await manager.get_prompt("notes", "summarize", {"topic": "架构升级"})
        assert single_text == "请用中文总结：架构升级"
        assert single_refs == ()
        assert "[用户]" not in single_text

        # Multi-message prompt includes description and [用户] / [助手] blocks.
        multi_text, multi_refs = await manager.get_prompt("notes", "dialogue", {"lang": "rust"})
        assert multi_refs == ()
        assert multi_text.startswith("多轮对话模板\n\n[用户]\n检查语言：rust\n\n[助手]\n请粘贴需要检查的片段。")

        # ImageContent + EmbeddedResource (text & binary blob).
        rich_text, rich_refs = await manager.get_prompt("notes", "rich", accepts_images=True, image_store=reg)
        assert len(rich_refs) == 1 and rich_refs[0].media_type == "image/png"
        assert rich_refs[0].path.is_file()
        assert "[嵌入资源 memo://snippet.py]\nprint('ok')" in rich_text
        assert "[二进制资源 memo://spec.pdf · application/pdf · 7 字节]" in rich_text

        # When accepts_images=False, image refs are omitted cleanly.
        no_img_text, no_img_refs = await manager.get_prompt("notes", "rich", accepts_images=False)
        assert no_img_refs == () and "当前模型不支持图片，已省略" in no_img_text

        # >64 KiB prompt cap.
        huge_prompt, _ = await manager.get_prompt("notes", "huge")
        assert huge_prompt.endswith("…（MCP 提示词内容超过 64 KB，已截断）")
        head = huge_prompt.split("\n…（MCP 提示词内容超过 64 KB，已截断）")[0]
        assert len(head.encode("utf-8")) <= 65536

        # Protocol error on get_prompt raises ToolFailure without closing session.
        with pytest.raises(ToolFailure) as exc_info:
            await manager.get_prompt("notes", "bad")
        assert exc_info.value.code == "mcp_protocol_error"
        ok_again, _ = await manager.get_prompt("notes", "summarize", {"topic": "恢复验证"})
        assert ok_again == "请用中文总结：恢复验证"
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "deny_rule",
    [
        "mcp__secret",
        "mcp__secret__*",
        "read_mcp_resource(secret:*)",
    ],
)
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypassPermissions"])
async def test_deny_rules_block_read_mcp_resource_and_hide_server_in_all_modes(tmp_path, deny_rule, mode):
    script = write_fixture(tmp_path)
    reg = create_default_registry(tmp_path, permission_mode=mode, load_permissions=False)
    reg.permissions.layers[1] = (parse_rule(deny_rule, "deny"),)
    manager = MCPManager(
        tmp_path,
        MCPSettings((make_config(script, "secret", "full"), make_config(script, "public", "full"))),
        startup_timeout=5,
    )
    register_resource_tools(reg, manager)
    try:
        await manager.start(reg)

        # Both tools are offered in plan mode as read-only discovery/read tools.
        if mode == "plan":
            plan_names = {d.name for d in reg.definitions(plan=True)}
            assert {"list_mcp_resources", "read_mcp_resource"} <= plan_names

        # Reading from secret server is strictly blocked even in bypassPermissions and plan modes!
        denied = await reg.execute(call("read_mcp_resource", server="secret", uri="memo://today"))
        assert denied.is_error and denied.code == "permission_denied"
        assert "TOP_SECRET_VALUE" not in denied.output

        # Reading from public server continues to work in every mode (including plan mode).
        allowed = await reg.execute(call("read_mcp_resource", server="public", uri="memo://today"))
        assert not allowed.is_error and allowed.output == "你好，今日待办已完成。"

        # list_mcp_resources completely hides secret's resources & templates while keeping public's!
        listed_all = await reg.execute(call("list_mcp_resources"))
        assert not listed_all.is_error
        assert "[public]" in listed_all.output
        assert "secret]" not in listed_all.output
        assert all(r["server"] == "public" for r in listed_all.details["resources"])
        assert all(t["server"] == "public" for t in listed_all.details["resource_templates"])

        # Explicitly asking list_mcp_resources(server="secret") also leaks nothing.
        listed_secret = await reg.execute(call("list_mcp_resources", server="secret"))
        if listed_secret.is_error:
            assert listed_secret.code == "permission_denied"
        else:
            assert listed_secret.output == "未发现可用的 MCP 资源。"
            assert listed_secret.details["count"] == 0
    finally:
        await manager.close()


async def test_fine_grained_uri_and_bare_tool_deny_rules(tmp_path):
    script = write_fixture(tmp_path)
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    reg.permissions.layers[1] = (parse_rule("read_mcp_resource(notes:memo://private/*)", "deny"),)
    manager = MCPManager(tmp_path, MCPSettings((make_config(script, "notes", "full"),)), startup_timeout=5)
    register_resource_tools(reg, manager)
    try:
        await manager.start(reg)

        # Public URI on the same server works and is listed; private/* URI is blocked and hidden!
        listed = await reg.execute(call("list_mcp_resources", server="notes"))
        assert "memo://today" in listed.output
        assert "memo://private/secret" not in listed.output

        blocked = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://private/secret"))
        assert blocked.is_error and blocked.code == "permission_denied"

        ok = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://today"))
        assert not ok.is_error and ok.output == "你好，今日待办已完成。"

        # Bare deny: [read_mcp_resource] blocks all MCP resource reads and hides all resources.
        reg.permissions.layers[2] = (parse_rule("read_mcp_resource", "deny"),)
        assert (
            reg.permissions.decide(Operation("read_mcp_resource", "notes", server="notes"), read_only=True).action
            == "deny"
        )
        blocked_all = await reg.execute(call("read_mcp_resource", server="notes", uri="memo://today"))
        assert blocked_all.is_error and blocked_all.code == "permission_denied"
        empty_list = await reg.execute(call("list_mcp_resources"))
        assert empty_list.output == "未发现可用的 MCP 资源。"
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "adversarial_server,adversarial_uri",
    [
        ("notes", "MEMO://PRIVATE/secret"),
        ("notes", "memo://private/public/../secret"),
        ("notes", "memo://private/a/%2e%2e/secret"),
        ("notes", "memo://private/a/%252e%252e/%73ecret"),
        ("notes", "memo://private/%73ecret"),
        ("notes", "  memo://private/secret  "),
        ("  notes  ", "memo://private/secret"),
        ("  notes  ", "MEMO://PRIVATE/a/%2e%2e/%73ecret"),
    ],
)
async def test_per_uri_deny_blocks_canonicalization_traversal_encoding_case_and_whitespace(
    tmp_path, adversarial_server, adversarial_uri
):
    script = write_fixture(tmp_path)
    reg = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    reg.permissions.layers[1] = (
        parse_rule("read_mcp_resource(notes:memo://private/*)", "deny"),
        parse_rule("read_mcp_resource(notes:file:///etc/shadow)", "deny"),
    )
    manager = MCPManager(tmp_path, MCPSettings((make_config(script, "notes", "full"),)), startup_timeout=5)
    register_resource_tools(reg, manager)
    try:
        await manager.start(reg)
        res = await reg.execute(call("read_mcp_resource", server=adversarial_server, uri=adversarial_uri))
        assert res.is_error and res.code == "permission_denied"
        assert "TOP_SECRET_VALUE" not in res.output

        for file_bypass in ("file:///etc/public/../shadow", "FILE:///etc/a/%2e%2e/shadow"):
            file_res = await reg.execute(call("read_mcp_resource", server="notes", uri=file_bypass))
            assert file_res.is_error and file_res.code == "permission_denied"

        # Exact deny rule also matches canonicalized URI forms.
        exact_rule = parse_rule({"rule": "read_mcp_resource(notes:memo://private/secret)", "exact": True}, "deny")
        assert exact_rule.matches(
            Operation("read_mcp_resource", "notes:MEMO://PRIVATE/a/%2e%2e/%73ecret", server="notes")
        )

        # Allow rule does NOT let dot-segment traversal escape an allowed prefix.
        allow_public = parse_rule("read_mcp_resource(notes:memo://today/*)", "allow")
        assert not allow_public.matches(
            Operation("read_mcp_resource", "notes:memo://today/../../secret", server="notes")
        )
    finally:
        await manager.close()
