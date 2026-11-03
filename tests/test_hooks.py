"""Hooks."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest
import yaml

from mosscode import cli
from mosscode.hooks import HookEngine, HookEvent, parse_condition, tool_event
from mosscode.hooks.config import REJECT_TYPE, TrustStore, hook_paths, parse_hook
from mosscode.hooks.conditions import HookConfigError
from mosscode.messages import ProviderError, ToolCall
from mosscode.permissions import Operation, parse_rule
from mosscode.prompts import PREFIX_KINDS, hook_context, hook_notification
from mosscode.tools import create_default_registry
from mosscode.tools.base import ExecutionCancelled
from test_agent import SequenceProvider, call, response
from test_cli import setup_cli
from test_commands import FakeUI, context, run
from test_context import NO_SUMMARY_REPLY, summary_reply
from test_sessions import drain, make, write_session, user, assistant
from test_skills import places  # noqa: F401  (fixture)

PY = "${PYTHON}"
MARK = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('x', encoding='utf-8')"


def py(code, *args):
    return [PY, "-c", code, *map(str, args)]


def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return root


def write_hooks(path, *hooks):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"hooks": list(hooks)}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def engine(tmp_path, *hooks, trust=True, **options):
    root = project(tmp_path)
    write_hooks(root / ".mosscode" / "hooks.yaml", *hooks)
    hooks_engine = HookEngine(root, trust_project=trust, **options)
    assert not hooks_engine.problems, hooks_engine.problems
    return hooks_engine


def registry(hooks, mode="bypassPermissions"):
    tools = create_default_registry(hooks.root, permission_mode=mode, load_permissions=False)
    tools.hooks = hooks
    return tools


async def execute(tools, name, id="c1", **args):
    return await tools.execute(ToolCall(id, name, json.dumps(args)))


def turn_start(**fields):
    return HookEvent("turn_start", fields={"prompt": "", **fields})


def record(hooks):
    """Every event the application fires, in order (wants() is forced so each one is fired)."""
    seen, original = [], hooks.fire

    async def fire(event):
        seen.append((event.name, event.agent, dict(event.fields)))
        return await original(event)

    hooks.fire = fire
    hooks.wants = lambda name: True
    return seen


def names(seen):
    return [item[0] for item in seen]


# Configuration -------------------------------------------------------------------------------

def test_three_files_append_in_order(tmp_path):
    root = project(tmp_path)
    for (source, path), hook_id in zip(hook_paths(root), ("u1", "p1", "l1")):
        write_hooks(path, {"id": hook_id, "event": "turn_end", "action": {"type": "command", "command": "x"}})
    hooks = HookEngine(root, trust_project=True)
    assert [h.id for h in hooks.hooks] == ["u1", "p1", "l1"]
    assert [h.source for h in hooks.hooks] == ["user", "project", "local"]


def test_a_broken_file_is_skipped_without_echoing_it(tmp_path):
    root = project(tmp_path)
    path = root / ".mosscode" / "hooks.yaml"
    path.parent.mkdir()
    path.write_text("hooks: [ {id: a, headers: secret-token-123\n", encoding="utf-8")
    write_hooks(root / ".mosscode" / "hooks.local.yaml",
                {"id": "ok", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    hooks = HookEngine(root, trust_project=True)
    assert [h.id for h in hooks.hooks] == ["ok"]
    assert len(hooks.problems) == 1 and str(path) in hooks.problems[0]
    assert "secret-token-123" not in hooks.problems[0]


@pytest.mark.parametrize("entry,message", [
    ({"id": "dup", "event": "turn_end", "action": {"type": "command", "command": "x"}}, "重复"),
    ({"id": "e", "event": "on_save", "action": {"type": "command", "command": "x"}}, "event 需为"),
    ({"id": "a", "event": "turn_end", "asnyc": True, "action": {"type": "command", "command": "x"}}, "asnyc"),
    ({"id": "m", "event": "turn_end"}, "缺少 action"),
    ({"id": "r", "event": "pre_tool_use", "reject": True}, REJECT_TYPE),
    ({"id": "p", "event": "post_tool_use", "reject": "不行"}, "reject 只能用于 pre_tool_use"),
    ({"id": "o", "event": "turn_end", "on_error": "block", "action": {"type": "command", "command": "x"}},
     "on_error 只能用于 pre_tool_use"),
    ({"id": "s", "event": "pre_send", "async": False, "action": {"type": "command", "command": "x"}}, "总在后台"),
    ({"id": "y", "event": "pre_tool_use", "async": True, "action": {"type": "command", "command": "x"}}, "不能 async"),
    ({"id": "f", "event": "turn_end", "action": {"type": "command", "command": "x", "feedback": True}}, "feedback"),
    ({"id": "v", "event": "turn_end", "action": {"type": "command", "command": ["x", "${HOME}"]}}, "${HOME}"),
])
def test_a_bad_hook_is_skipped_with_path_and_id(tmp_path, entry, message):
    root = project(tmp_path)
    good = {"id": "dup", "event": "turn_end", "action": {"type": "command", "command": "x"}}
    write_hooks(hook_paths(root)[0][1], good)
    path = write_hooks(root / ".mosscode" / "hooks.yaml", entry,
                       {"id": "fine", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    hooks = HookEngine(root, trust_project=True)
    assert [h.id for h in hooks.hooks] == ["dup", "fine"]
    assert len(hooks.problems) == 1
    assert hooks.problems[0].startswith(f"{path} · {entry['id']} · ") and message in hooks.problems[0]


def test_static_reject_needs_no_action_and_async_follows_the_event(tmp_path):
    path = tmp_path / "hooks.yaml"
    hook = parse_hook({"id": "prod", "event": "pre_tool_use", "if": "path ~= config/prod/**", "reject": " 生产配置 "},
                      "project", path)
    assert hook.action is None and hook.reject == "生产配置" and not hook.background
    hook = parse_hook({"id": "s", "event": "pre_send", "action": {"type": "command", "command": "x"}}, "user", path)
    assert hook.background and hook.flags() == ("async",)
    hook = parse_hook({"id": "s", "event": "pre_send", "action": {"type": "prompt", "message": "m"}}, "user", path)
    assert not hook.background


# Condition parsing ---------------------------------------------------------------------------

@pytest.mark.parametrize("expression,event,message", [
    ("tool == a && path == b || tool == c", "pre_tool_use", "混用"),
    ("tool Bash", "pre_tool_use", "运算符"),
    ("tool =~ /[/", "pre_tool_use", "正则不合法"),
    ("tool =~ Bash", "pre_tool_use", "/…/"),
    ("args.a.b == x", "pre_tool_use", "顶层参数名"),
    ("tool == Bash", "turn_start", "没有条件字段 tool"),
    ("args.x == 1", "turn_end", "args.*"),
    ("colour == red", "turn_end", "未知条件字段"),
    ("tool == Bash && ", "pre_tool_use", "空的子条件"),
    ('args.command == "a', "pre_tool_use", "没有闭合"),
])
def test_bad_conditions(expression, event, message):
    with pytest.raises(HookConfigError, match=message.replace("*", r"\*").replace(".", r"\.")):
        parse_condition(expression, event)


def test_quotes_keep_operators_and_templates_are_checked(tmp_path):
    group = parse_condition('args.command == "a && b \\"c\\"" || tool == Read', "pre_tool_use")
    assert group.any and [item.value for item in group.items] == ['a && b "c"', "read_file"]
    with pytest.raises(HookConfigError, match="没有条件字段 path"):
        parse_hook({"id": "t", "event": "session_start", "action": {"type": "prompt", "message": "{{path}}"}},
                   "user", tmp_path)


# Condition evaluation ------------------------------------------------------------------------

def matches(expression, event="pre_tool_use", **fields):
    return parse_condition(expression, event).matches({key.replace("__", "."): value for key, value in fields.items()})


def test_tool_aliases_and_globs():
    assert matches("tool == Bash", tool="execute_command")
    assert matches("tool ~= mcp__x__*", tool="mcp__x__search")
    assert not matches("tool ~= mcp__x__*", tool="mcp__y__search")
    assert matches("tool != Bash", tool="read_file") and not matches("tool != Bash", tool="execute_command")


def test_paths_follow_the_permission_rules(tmp_path):
    assert not matches('path ~= "src/*.py"', path="src/a/b.py")
    assert matches("path ~= src/**/*.py", path="src/a/b.py")
    assert matches("path == src/a.py", path="src\\a.py")
    assert matches("path ~= src/*.py", path="SRC/A.py") == (os.name == "nt")
    assert matches("path == src/a.py", path="SRC/A.PY") == (os.name == "nt")
    outside = tmp_path.as_posix() + "/**"
    assert not matches(f'path ~= "{outside}"', path="src/a.py")
    assert matches(f'path ~= "{outside}"', path=tmp_path.as_posix() + "/x/y.txt")
    assert not matches("path ~= src/**", path="")


def test_commands_and_args():
    assert matches('command ~= "git push:*"', command="cd x && git push -f")
    assert matches('command ~= "git push:*"', command="git push")
    assert not matches('command ~= "git push:*"', command="git pushx")
    assert matches("command ~= *rm -rf*", command="echo a; rm -rf /tmp/x")
    assert matches('args.x == ""') and not matches("args.x == a")
    assert matches("args.command =~ /^npm (test|run)/", args__command="npm test")
    assert matches("tool == Bash && command ~= ls*", tool="execute_command", command="ls -la")
    assert not matches("tool == Bash && command ~= ls*", tool="execute_command", command="pwd")
    assert matches("tool == Bash || tool == Read", tool="read_file")


# Trust ---------------------------------------------------------------------------------------

def test_project_files_need_trust_by_content(tmp_path):
    root = project(tmp_path)
    write_hooks(hook_paths(root)[0][1], {"id": "u", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    path = write_hooks(root / ".mosscode" / "hooks.yaml",
                       {"id": "p", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    hooks = HookEngine(root)
    assert [h.id for h in hooks.hooks] == ["u"] and [f.path for f in hooks.untrusted] == [path]
    assert hooks.status(hooks.find("p")) == "未确认"
    hooks.decline(hooks.untrusted[0])
    assert hooks.pending_trust() == () and hooks.untrusted
    hooks.trust(hooks.untrusted[0])
    assert [h.id for h in hooks.hooks] == ["u", "p"]
    assert [h.id for h in HookEngine(root).hooks] == ["u", "p"]  # Remembered by hash.
    write_hooks(path, {"id": "p", "event": "turn_end", "action": {"type": "command", "command": "y"}})
    assert [h.id for h in HookEngine(root).hooks] == ["u"]  # Changed content: asked again.


def test_trust_flag_writes_no_record(tmp_path):
    root = project(tmp_path)
    write_hooks(root / ".mosscode" / "hooks.local.yaml",
                {"id": "l", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    assert [h.id for h in HookEngine(root, trust_project=True).hooks] == ["l"]
    assert not TrustStore(root).path.exists()
    assert HookEngine(root).hooks == ()


@pytest.mark.parametrize("name", ["hooks.yaml", "hooks.local.yaml"])
async def test_the_model_cannot_write_hook_files(tmp_path, name):
    tools = registry(engine(tmp_path))
    result = await execute(tools, "write_file", path=f".mosscode/{name}", content="hooks: []")
    assert result.code == "policy_protected" and "Hook" in result.output


# Command actions -----------------------------------------------------------------------------

PROBE = """import json, os, sys
data = json.load(sys.stdin)
env = {k: v for k, v in os.environ.items() if k.startswith('MOSSCODE_')}
print(json.dumps({'stdin': data, 'env': env, 'cwd': os.getcwd(), 'argv': sys.argv[1:], 'zh': '中文输出'}))
"""


async def test_stdin_environment_cwd_and_argv(tmp_path):
    hooks = engine(tmp_path, {"id": "probe", "event": "pre_tool_use",
                              "action": {"type": "command", "command": py(PROBE, "${FILE_PATH}"), "feedback": True}})
    target = "dir x;y/a b.txt"
    outcome = await hooks.fire(tool_event("pre_tool_use", "main", "write_file", {"path": target, "content": "c"},
                                          Operation("write_file", target), hooks.root))
    assert outcome.rejection is None and outcome.notes[0][0] == "probe"
    seen = json.loads(outcome.notes[0][1])
    assert seen["argv"] == [str(hooks.root / target)] and seen["zh"] == "中文输出"
    assert Path(seen["cwd"]) == hooks.root
    assert seen["stdin"]["tool"] == "write_file" and seen["stdin"]["args"] == {"path": target, "content": "c"}
    assert seen["stdin"]["event"] == "pre_tool_use" and seen["stdin"]["hook_id"] == "probe"
    assert seen["stdin"]["path"] == target and seen["stdin"]["agent"] == "main"
    assert seen["env"] == {"MOSSCODE_HOOK_ID": "probe", "MOSSCODE_HOOK_EVENT": "pre_tool_use", "MOSSCODE_SESSION_ID": "",
                           "MOSSCODE_PROJECT_ROOT": str(hooks.root), "MOSSCODE_AGENT": "main",
                           "MOSSCODE_AGENT_TYPE": "",
                           "MOSSCODE_TOOL_NAME": "write_file", "MOSSCODE_FILE_PATH": str(hooks.root / target)}


async def test_numeric_argv_parts_become_text(tmp_path):
    # YAML reads `[git, log, -3]` as a number in the last slot.
    hooks = engine(tmp_path, {"id": "nums", "event": "turn_start",
                              "action": {"type": "command", "command": [PY, "-c", "import sys; print(sys.argv[1:])", -3, 2.5],
                                         "feedback": True}})
    outcome = await hooks.fire(turn_start())
    assert outcome.notes == (("nums", "['-3', '2.5']"),)


async def test_string_commands_use_the_configured_shell(tmp_path):
    root = project(tmp_path)
    shell = create_default_registry(root, load_permissions=False).get("execute_command").shell
    hooks = engine(tmp_path, {"id": "say", "event": "turn_start",
                              "action": {"type": "command", "command": "echo hello-shell", "feedback": True}},
                   shell=shell)
    outcome = await hooks.fire(turn_start())
    assert outcome.notes == (("say", "hello-shell"),)


async def test_timeout_ends_the_process_tree(tmp_path):
    late = tmp_path / "late.txt"
    child = f"import pathlib, time; time.sleep(2); pathlib.Path({str(late)!r}).write_text('late')"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    hooks = engine(tmp_path, {"id": "slow", "event": "turn_start",
                              "action": {"type": "command", "command": py(parent), "timeout": 1}})
    started = time.monotonic()
    await hooks.fire(turn_start())
    assert time.monotonic() - started < 10
    assert hooks.recent[-1].outcome == "timeout"
    assert hooks.take_notices() == ["Hook slow（turn_start）失败：超过 1 秒未结束，已终止；已忽略。"]
    await asyncio.sleep(2.5)
    assert not late.exists()


async def test_missing_command_and_nonzero_exit_fail(tmp_path):
    hooks = engine(tmp_path, {"id": "gone", "event": "turn_start",
                              "action": {"type": "command", "command": ["no-such-command-mosscode-xyz"]}},
                   {"id": "bad", "event": "turn_start",
                    "action": {"type": "command", "command": py("import sys; print('炸了', file=sys.stderr); sys.exit(1)")}})
    outcome = await hooks.fire(turn_start())
    assert outcome.rejection is None
    assert [(r.hook, r.outcome) for r in hooks.recent] == [("gone", "failed"), ("bad", "failed")]
    assert "找不到命令" in hooks.recent[0].detail and hooks.recent[1].exit_code == 1
    assert "退出码为 1：炸了" in hooks.recent[1].detail
    assert len(hooks.take_notices()) == 2


# Http actions --------------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            hits.append((self.command, self.path, dict(self.headers), body))
            route = self.path.split("?")[0]
            if route == "/slow":
                time.sleep(2)
            status = {"/fail": 500, "/redirect": 302}.get(route, 200)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/ok")
            self.send_header("Content-Length", "2")
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except OSError:
                pass

        do_GET = do_POST = do_PUT = handle_request

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", hits
    httpd.shutdown()
    httpd.server_close()


def post_tool(tool="execute_command", **args):
    operation = Operation(tool, args.get("command", args.get("path", "")))
    return tool_event("post_tool_use", "main", tool, args, operation, Path.cwd())


async def test_json_templates_context_and_env(tmp_path, server, monkeypatch):
    base, hits = server
    monkeypatch.setenv("HOOK_TEST_TOKEN", "t-123")
    hooks = engine(tmp_path, {"id": "j", "event": "post_tool_use", "action": {
        "type": "http", "url": base + "/ok?token=${HOOK_TEST_TOKEN}", "headers": {"X-Token": "${HOOK_TEST_TOKEN}",
                                                                                 "X-Default": "${HOOK_UNSET_VAR:-fallback}"},
        "json": {"tool": "{{tool}}", "command": "{{args.command}}", "n": [1, "{{event}}"]}}},
                   {"id": "ctx", "event": "post_tool_use", "action": {"type": "http", "url": base + "/ok"}})
    await hooks.fire(post_tool(command='echo "hi" \\ end'))
    assert [hit[0] for hit in hits] == ["POST", "POST"]
    assert hits[0][1] == "/ok?token=t-123" and hits[0][2]["X-Token"] == "t-123" and hits[0][2]["X-Default"] == "fallback"
    assert json.loads(hits[0][3]) == {"tool": "execute_command", "command": 'echo "hi" \\ end', "n": [1, "post_tool_use"]}
    context = json.loads(hits[1][3])
    assert context["event"] == "post_tool_use" and context["hook_id"] == "ctx" and context["args"]["command"] == 'echo "hi" \\ end'
    assert [r.outcome for r in hooks.recent] == ["ok", "ok"] and hooks.recent[0].status == 200


async def test_unset_variable_fails_the_hook_at_load(tmp_path, monkeypatch):
    monkeypatch.delenv("HOOK_UNSET_VAR", raising=False)
    root = project(tmp_path)
    write_hooks(root / ".mosscode" / "hooks.yaml",
                {"id": "u", "event": "turn_end", "action": {"type": "http", "url": "http://x/${HOOK_UNSET_VAR}"}})
    hooks = HookEngine(root, trust_project=True)
    assert hooks.hooks == () and "· u ·" in hooks.problems[0] and "环境变量未设置" in hooks.problems[0]
    write_hooks(root / ".mosscode" / "hooks.yaml",
                {"id": "h", "event": "turn_end", "action": {"type": "http", "url": "http://x/", "headers": {"X": "中文"}}})
    assert "ASCII" in HookEngine(root, trust_project=True).problems[0]


async def test_failures_and_no_redirects(tmp_path, server):
    base, hits = server
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    hooks = engine(tmp_path, *({"id": name, "event": "post_tool_use",
                                "action": {"type": "http", "url": url, "timeout": 1}}
                               for name, url in (("fail", base + "/fail?secret=s3"), ("refused", f"http://127.0.0.1:{closed}/"),
                                                 ("slow", base + "/slow"), ("redirect", base + "/redirect"))))
    await hooks.fire(post_tool(command="x"))
    # Windows retries a refused connection for about two seconds, so that one may also end as a timeout.
    outcomes = [(r.hook, r.outcome if r.hook != "refused" else r.outcome in {"failed", "timeout"}, r.status)
                for r in hooks.recent]
    assert outcomes == [("fail", "failed", 500), ("refused", True, None), ("slow", "timeout", None), ("redirect", "failed", 302)]
    assert "/ok" not in [hit[1] for hit in hits]
    assert all("s3" not in r.detail for r in hooks.recent)


# Prompt and agent actions --------------------------------------------------------------------

async def test_prompt_templates_and_agent_placeholder(tmp_path):
    hooks = engine(tmp_path, {"id": "note", "event": "post_tool_use", "action": {"type": "prompt",
                                                                                 "message": "改了 {{path}}（{{tool}}）"}},
                   {"id": "helper", "event": "post_tool_use", "action": {"type": "agent", "prompt": "检查"}})
    event = tool_event("post_tool_use", "main", "write_file", {"path": "a.py"}, Operation("write_file", "a.py"), hooks.root)
    assert (await hooks.fire(event)).notes == (("note", "改了 a.py（write_file）"),)
    await hooks.drain(1)  # Agent actions always run in the background.
    await hooks.fire(event)
    await hooks.drain(1)
    assert [r.outcome for r in hooks.recent] == ["ok", "skipped", "ok", "skipped"]
    assert hooks.take_notices() == ["Hook helper（post_tool_use）的子 Agent 动作已跳过：当前运行方式不支持子 Agent。"]
    hooks.new_session()
    await hooks.fire(event)
    await hooks.drain(1)
    assert len(hooks.take_notices()) == 1


# Engine --------------------------------------------------------------------------------------

async def test_order_and_the_first_rejection_stops(tmp_path):
    marker = tmp_path / "third.txt"
    hooks = engine(tmp_path, {"id": "first", "event": "pre_tool_use", "action": {"type": "prompt", "message": "一"}},
                   {"id": "second", "event": "pre_tool_use", "if": "tool == Bash", "reject": "不许执行命令"},
                   {"id": "third", "event": "pre_tool_use", "action": {"type": "command", "command": py(MARK, marker)}})
    outcome = await hooks.fire(tool_event("pre_tool_use", "main", "execute_command", {"command": "ls"},
                                          Operation("execute_command", "ls"), hooks.root))
    assert outcome.rejection == ("second", "Hook second 拒绝了此调用：不许执行命令")
    assert outcome.notes == (("first", "一"),) and not marker.exists()
    assert hooks.take_notices() == []  # A rejection is the hook's job, not a failure.
    outcome = await hooks.fire(tool_event("pre_tool_use", "main", "read_file", {"path": "a"},
                                          Operation("read_file", "a"), hooks.root))
    assert outcome.rejection is None and marker.exists() and outcome.ran == ("first", "third")


async def test_exit_code_2_rejects_with_stderr(tmp_path):
    hooks = engine(tmp_path, {"id": "guard", "event": "pre_tool_use", "action": {
        "type": "command", "command": py("import sys; print('分支受保护', file=sys.stderr); sys.exit(2)")}})
    outcome = await hooks.fire(tool_event("pre_tool_use", "main", "read_file", {}, None, hooks.root))
    assert outcome.rejection == ("guard", "Hook guard 拒绝了此调用：分支受保护")
    assert hooks.recent[-1].outcome == "rejected"


async def test_once(tmp_path):
    hooks = engine(tmp_path, {"id": "first-time", "event": "turn_start", "once": True, "if": "prompt == go",
                              "action": {"type": "command", "command": py("import sys; sys.exit(1)")}})
    await hooks.fire(turn_start(prompt="wait"))
    assert hooks.recent == type(hooks.recent)() and hooks.status(hooks.find("first-time")) == "已启用"
    await hooks.fire(turn_start(prompt="go"))
    await hooks.fire(turn_start(prompt="go"))
    assert [r.outcome for r in hooks.recent] == ["failed"]  # A failure still counts as its one run.
    assert hooks.status(hooks.find("first-time")) == "once 已执行"
    hooks.new_session()
    await hooks.fire(turn_start(prompt="go"))
    assert len(hooks.recent) == 2


async def test_background_hooks_do_not_block_and_drain_cancels(tmp_path):
    hooks = engine(tmp_path, {"id": "bg", "event": "turn_end", "async": True,
                              "action": {"type": "command", "command": py("import time; time.sleep(30)")}})
    started = time.monotonic()
    outcome = await hooks.fire(HookEvent("turn_end", fields={"reason": "completed"}))
    assert time.monotonic() - started < 2 and outcome.ran == ()
    await asyncio.sleep(0.3)
    await hooks.drain(0.5)
    assert [r.outcome for r in hooks.recent] == ["cancelled"]
    assert hooks.take_notices() == []


# Log and notices -----------------------------------------------------------------------------

async def test_log_lines_and_one_notice_per_turn(tmp_path, server):
    from mosscode.session import Session
    base, _ = server
    hooks = engine(tmp_path, {"id": "web", "event": "post_tool_use",
                              "action": {"type": "http", "url": base + "/fail?k=secret-q", "headers": {"Authorization": "secret-h"}}},
                   {"id": "deny", "event": "pre_tool_use", "reject": "不行"})
    hooks.use_session(Session.new(hooks.root))
    for _ in range(2):
        await hooks.fire(post_tool(command="x"))
    await hooks.fire(tool_event("pre_tool_use", "main", "read_file", {}, None, hooks.root))
    assert len(hooks.take_notices()) == 1
    hooks.begin_turn()
    await hooks.fire(post_tool(command="x"))
    assert len(hooks.take_notices()) == 1
    text = hooks.log_path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    assert [line["hook"] for line in lines] == ["web", "web", "deny", "web"]
    assert set(lines[0]) == {"time", "hook", "source", "event", "agent", "action", "outcome", "duration_ms", "status", "detail"}
    assert lines[0]["outcome"] == "failed" and lines[0]["status"] == 500 and lines[2]["outcome"] == "rejected"
    assert "secret" not in text and "MOSSCODE_" not in text


# Tools and permissions -----------------------------------------------------------------

def guard(**extra):
    return {"id": "guard", "event": "pre_tool_use", "if": "path ~= config/prod/**", "reject": "生产配置只读", **extra}


async def test_static_rejection_even_when_bypassing(tmp_path):
    marker = tmp_path / "post.txt"
    hooks = engine(tmp_path, guard(), {"id": "post", "event": "post_tool_use",
                                       "action": {"type": "command", "command": py(MARK, marker)}})
    tools = registry(hooks)
    result = await execute(tools, "write_file", path="config/prod/app.yaml", content="a: 1")
    assert (result.is_error, result.code) == (True, "hook_rejected")
    assert result.output == "Hook guard 拒绝了此调用：生产配置只读"
    assert result.details == {"executed": False, "hook": "guard"}
    assert not (hooks.root / "config/prod/app.yaml").exists() and not marker.exists()
    assert tools.permissions.requests.empty()
    result = await execute(tools, "write_file", path="config/dev/app.yaml", content="a: 1")
    assert not result.is_error and marker.exists()


async def test_deny_rules_and_plan_mode_come_before_hooks(tmp_path):
    marker = tmp_path / "pre.txt"
    hooks = engine(tmp_path, {"id": "pre", "event": "pre_tool_use", "action": {"type": "command", "command": py(MARK, marker)}})
    tools = registry(hooks)
    tools.permissions.layers[-1] = (parse_rule("Write(secret/**)", "deny"),)
    result = await execute(tools, "write_file", path="secret/a.txt", content="x")
    assert result.code == "permission_denied" and not marker.exists()
    tools = registry(hooks, mode="plan")
    result = await execute(tools, "write_file", path="a.txt", content="x")
    assert result.code == "plan_blocked" and not marker.exists()


async def test_a_passing_hook_never_skips_approval(tmp_path):
    marker = tmp_path / "pre.txt"
    hooks = engine(tmp_path, {"id": "pre", "event": "pre_tool_use", "action": {"type": "command", "command": py(MARK, marker)}},
                   {"id": "asked", "event": "permission_request", "action": {"type": "command", "command": py(MARK, marker.with_suffix(".ask"))}})
    tools = registry(hooks, mode="default")
    tools.permissions.interactive = True
    task = asyncio.create_task(execute(tools, "write_file", path="a.txt", content="x"))
    request = await asyncio.wait_for(tools.permissions.requests.get(), 10)
    assert marker.exists() and request.operation.target == "a.txt"
    tools.permissions.respond(request.id, "deny")
    result = await task
    assert result.code != "hook_rejected" and not (hooks.root / "a.txt").exists()
    await hooks.drain(10)
    assert marker.with_suffix(".ask").exists()


async def test_failures_pass_unless_blocking(tmp_path):
    failing = {"type": "command", "command": py("import sys; print('lint 崩了', file=sys.stderr); sys.exit(1)")}
    hooks = engine(tmp_path, {"id": "lint", "event": "pre_tool_use", "action": failing})
    result = await execute(registry(hooks), "write_file", path="a.txt", content="x")
    assert not result.is_error and hooks.take_notices() == ["Hook lint（pre_tool_use）失败：退出码为 1：lint 崩了；已忽略。"]
    hooks = engine(tmp_path, {"id": "lint", "event": "pre_tool_use", "on_error": "block", "action": failing})
    result = await execute(registry(hooks), "write_file", path="b.txt", content="x")
    assert result.code == "hook_rejected" and result.output == "Hook lint 执行失败，按配置阻止此调用：退出码为 1：lint 崩了"
    assert not (hooks.root / "b.txt").exists() and hooks.take_notices() == []


FORMAT = "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.write_text(p.read_text() + 'formatted\\n')"


async def test_post_hooks_annotate_results_and_force_a_reread(tmp_path):
    hooks = engine(tmp_path, {"id": "fmt", "event": "post_tool_use", "if": "tool == Write || tool == Edit",
                              "action": {"type": "command", "command": py(FORMAT, "${FILE_PATH}")}},
                   {"id": "missing", "event": "post_tool_use", "if": "error == true && code == not_found",
                    "action": {"type": "prompt", "message": "{{path}} 不存在，先 glob 一下"}},
                   {"id": "fb", "event": "post_tool_use", "if": "tool == Read && error == false",
                    "action": {"type": "command", "command": py("print('读到了')"), "feedback": True}})
    tools = registry(hooks)
    result = await execute(tools, "write_file", path="a.py", content="x = 1\n")
    assert result.output.endswith("文件已被 Hook fmt 修改（如格式化）；再次编辑前请重新读取。")
    assert result.details["hook_modified"] == "fmt"
    assert (hooks.root / "a.py").read_text() == "x = 1\nformatted\n"
    result = await execute(tools, "edit_file", path="a.py", old_text="x = 1", new_text="x = 2")
    assert result.code == "stale_read"
    result = await execute(tools, "read_file", path="a.py")
    assert result.output.endswith('<hook-notification hook="fb">\n读到了\n</hook-notification>')
    result = await execute(tools, "edit_file", path="a.py", old_text="x = 1", new_text="x = 2")
    assert not result.is_error and "已被 Hook fmt 修改" in result.output
    result = await execute(tools, "read_file", path="nope.py")
    assert result.code == "not_found"
    assert result.output.endswith('<hook-notification hook="missing">\nnope.py 不存在，先 glob 一下\n</hook-notification>')


async def test_tool_row_shows_the_hook_change(tmp_path):
    # The row shows only the first output line and the diff, so the notice gets its own line.
    from mosscode.conversation import Conversation
    from mosscode.messages import ToolProgress, ToolResult
    from mosscode.tui import MossCodeApp, ToolEntry
    app = MossCodeApp(Conversation(SequenceProvider()), "Demo")
    async with app.run_test(size=(120, 36)) as pilot:
        write = ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))
        row = ToolEntry(ToolProgress(write, "running"), None)
        await app.query_one("#chat").mount(row)
        output = "已创建 a.py（1 行）。\n\n文件已被 Hook fmt 修改（如格式化）；再次编辑前请重新读取。"
        row.complete(ToolResult("w", "write_file", output, details={"created": True, "hook_modified": "fmt"}), 0.1)
        await pilot.pause()
        assert row.transcript.plain.endswith("1 + x = 1\n    文件已被 Hook fmt 修改（如格式化），模型再次编辑前会重新读取。")


async def test_no_notice_when_the_file_is_untouched(tmp_path):
    hooks = engine(tmp_path, {"id": "look", "event": "post_tool_use", "action": {"type": "command", "command": py("pass")}})
    tools = registry(hooks)
    result = await execute(tools, "write_file", path="a.py", content="x = 1\n")
    assert "已被 Hook" not in result.output and "hook_modified" not in result.details
    result = await execute(tools, "edit_file", path="a.py", old_text="x = 1", new_text="x = 2")
    assert not result.is_error


# Fork sub-agents ----------------------------------------------------------------------------

async def test_fork_views_report_their_agent(tmp_path):
    hooks = engine(tmp_path, {"id": "fork-no-bash", "event": "pre_tool_use", "if": "agent == fork && tool == Bash",
                              "reject": "审查子 Agent 不能执行命令"})
    tools = registry(hooks)
    fork = tools.for_agent(agent="fork", agent_type="fork", permissions=tools.permissions)
    result = await execute(fork, "execute_command", command="echo hi")
    assert result.code == "hook_rejected" and "审查子 Agent 不能执行命令" in result.output
    result = await execute(tools, "execute_command", command="echo hi")
    assert result.code != "hook_rejected"


async def test_fork_skill_runs_fire_tool_events_only(tmp_path, places):
    from mosscode.builtin_commands import BUILTIN
    from test_skills import fork_run, fork_skill, load
    _, _, project_skills, _ = places
    fork_skill(project_skills)
    hooks = engine(tmp_path, {"id": "fork-no-bash", "event": "pre_tool_use", "if": "agent == fork && tool == Bash",
                              "reject": "审查子 Agent 不能执行命令"},
                   {"id": "turn", "event": "turn_start", "action": {"type": "prompt", "message": "TURN-NOTE"}})
    seen = record(hooks)
    provider = SequenceProvider(response(calls=[call("execute_command", command="echo hi")]), response("审查结果"))
    tools = create_default_registry(hooks.root, permission_mode="bypassPermissions", load_permissions=False)
    conversation = make(tmp_path, provider, registry=tools, skills=load(places, reserved=BUILTIN.names()), hooks=hooks)
    await fork_run(conversation, "/rev a.py")
    # subagent_start on the main side; subagent_stop runs in the background.
    assert [n for n in names(seen) if n != "subagent_stop"] == ["session_start", "turn_start", "subagent_start",
                                                                "pre_tool_use", "turn_end"]
    assert seen[2][1] == "main" and seen[2][2]["agent_type"] == "skill:rev" and seen[3][1] == "fork"
    sent = provider.requests[1][0][-1].tool_results[0]
    assert sent.code == "hook_rejected" and "审查子 Agent 不能执行命令" in sent.output
    for request in provider.requests:
        assert "TURN-NOTE" not in " ".join(item.text for item in request[1].supplements)
    await conversation.close()


# Cancellation -------------------------------------------------------------------------------

WAIT = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(30)"


async def wait_for_file(path):
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


async def test_cancelling_pre_hooks_skips_the_tool(tmp_path):
    started = tmp_path / "started.txt"
    hooks = engine(tmp_path, {"id": "slow", "event": "pre_tool_use", "action": {"type": "command", "command": py(WAIT, started)}})
    task = asyncio.create_task(execute(registry(hooks), "write_file", path="a.txt", content="x"))
    await wait_for_file(started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (hooks.root / "a.txt").exists() and hooks.recent[-1].outcome == "cancelled"


async def test_cancelling_post_hooks_keeps_the_result(tmp_path):
    started = tmp_path / "started.txt"
    hooks = engine(tmp_path, {"id": "note", "event": "pre_tool_use", "action": {"type": "prompt", "message": "先说一句"}},
                   {"id": "slow", "event": "post_tool_use", "action": {"type": "command", "command": py(WAIT, started)}})
    task = asyncio.create_task(execute(registry(hooks), "write_file", path="a.txt", content="x"))
    await wait_for_file(started)
    task.cancel()
    with pytest.raises(ExecutionCancelled) as caught:
        await task
    result = caught.value.result
    assert not result.is_error and (hooks.root / "a.txt").read_text() == "x"
    assert '<hook-notification hook="note">' in result.output


# Injection ----------------------------------------------------------------------------------

def supplements(request):
    return {item.kind: item.text for item in request[1].supplements}


async def test_where_hook_text_goes(tmp_path):
    hooks = engine(tmp_path, {"id": "ss", "event": "session_start", "action": {"type": "prompt", "message": "SS {{source}}"}},
                   {"id": "ts", "event": "turn_start", "once": True, "action": {"type": "prompt", "message": "TS {{prompt}}"}},
                   {"id": "ps", "event": "pre_send", "action": {"type": "prompt", "message": "PS {{iteration}}"}},
                   {"id": "pt", "event": "post_tool_use", "action": {"type": "prompt", "message": "PT {{path}}"}})
    (hooks.root / "a.txt").write_text("内容", encoding="utf-8")
    provider = SequenceProvider(response(calls=[call("read_file", path="a.txt")]), response("好"), response("好2"),
                                summary_reply(), response("好3"), response("好4"))
    conversation = make(tmp_path, provider, hooks=hooks)
    await drain(conversation.reply("甲"))
    await drain(conversation.reply("乙"))
    first, second, third = map(supplements, provider.requests[:3])
    assert first["hook-context"] == second["hook-context"] == third["hook-context"]
    assert "=== Hook ss" in first["hook-context"] and "SS new" in first["hook-context"]
    assert "TS 甲" in first["hook-notification"] and "PS 1" in first["hook-notification"]
    assert "TS 甲" in second["hook-notification"] and "PS 2" in second["hook-notification"]
    assert "PS 1" not in second["hook-notification"]
    assert "TS" not in third["hook-notification"] and "PS 1" in third["hook-notification"]
    assert "PT a.txt" in provider.requests[1][0][-1].tool_results[0].output
    saved = conversation.session.transcript_path.read_text(encoding="utf-8")
    assert "PT a.txt" in saved and "TS 甲" not in saved and "SS new" not in saved
    await drain(conversation.compact())
    await drain(conversation.reply("丙"))
    assert "SS new" in supplements(provider.requests[-1])["hook-context"]
    await conversation.clear()
    await drain(conversation.reply("丁"))
    assert "SS clear" in supplements(provider.requests[-1])["hook-context"]
    await conversation.close()


def test_hook_text_is_cut_at_8000_characters():
    text = hook_notification([("big", "长" * 9000)]).text
    assert "=== Hook big" in text and len(text) < 8200 and "截断" in text
    assert hook_context([]) is None and hook_notification(()) is None
    assert "hook-context" in PREFIX_KINDS and "hook-notification" not in PREFIX_KINDS


@pytest.mark.parametrize("index", range(3))
async def test_prefix_and_request_positions_for_every_protocol(index):
    from mosscode.messages import ContentBlock, Message
    from mosscode.prompts import PromptRequest, Supplement
    from test_providers import CASES, SSEBody, adapter
    protocol, cls, frames, _ = CASES[index]
    provider, requests, _ = adapter(protocol, cls, lambda: SSEBody(frames()))
    prompt = PromptRequest("stable", (Supplement("environment", "env"), hook_context([("ss", "会话")]),
                                      hook_notification([("ts", "本轮")])))
    try:
        _ = [e async for e in provider.stream([Message("user", (ContentBlock("text", "问题"),))], prompt)]
    finally:
        await provider.close()
    payload = requests[0][1]
    if protocol == "anthropic-messages":
        texts = [block["text"] for block in payload["system"]]
        assert texts[1].startswith("<environment>") and texts[2].startswith("<hook-context>")
        assert payload["messages"][-1]["content"][-1]["text"].startswith("<hook-notification>")
    else:
        content = payload["input"] if protocol == "openai-responses" else payload["messages"]
        texts = [item["content"] for item in content]
        start = 0 if protocol == "openai-responses" else 1
        assert texts[start + 1].startswith("<hook-context>") and texts[start + 2] == "问题"
        assert texts[-1].startswith("<hook-notification>")


# Event timing -------------------------------------------------------------------------------

async def test_one_turn_in_order(tmp_path):
    hooks = engine(tmp_path)
    seen = record(hooks)
    (hooks.root / "a.txt").write_text("a", encoding="utf-8")
    provider = SequenceProvider(response(calls=[call("read_file", path="a.txt")]), response("好"))
    conversation = make(tmp_path, provider, hooks=hooks)
    await drain(conversation.reply("甲"))
    assert names(seen) == ["session_start", "turn_start", "pre_send", "post_receive", "pre_tool_use", "post_tool_use",
                           "pre_send", "post_receive", "turn_end"]
    fields = [item[2] for item in seen]
    assert fields[1]["prompt"] == "甲" and fields[2]["iteration"] == "1" and fields[3]["tool_calls"] == "1"
    assert fields[6]["iteration"] == "2" and fields[7]["tool_calls"] == "0" and fields[8]["reason"] == "completed"
    assert fields[4]["tool"] == "read_file" and fields[4]["path"] == "a.txt"
    await conversation.close()


async def test_errors_and_compaction(tmp_path):
    hooks = engine(tmp_path)
    seen = record(hooks)
    provider = SequenceProvider(ProviderError("服务暂时不可用"), response("好"), summary_reply(), NO_SUMMARY_REPLY)
    conversation = make(tmp_path, provider, hooks=hooks)
    with pytest.raises(ProviderError):
        await drain(conversation.reply("甲"))
    assert names(seen)[-2:] == ["error", "turn_end"] and seen[-1][2]["reason"] == "error"
    assert "服务暂时不可用" in seen[-2][2]["error"]
    await drain(conversation.reply("乙"))
    seen.clear()
    await drain(conversation.compact())
    assert [(name, fields) for name, _, fields in seen] == [("compact", {"trigger": "manual"})]
    seen.clear()
    await drain(conversation.compact())
    assert seen == []
    await conversation.close()


# Session events -----------------------------------------------------------------------------

async def test_session_events(tmp_path):
    saved = write_session(tmp_path, [user("一"), assistant("二")])
    hooks = engine(tmp_path)
    seen = record(hooks)
    conversation = make(tmp_path, SequenceProvider(), hooks=hooks)
    await conversation.start()
    await conversation.clear()
    await drain(conversation.resume(saved.id))
    await conversation.close()
    assert [(name, fields) for name, _, fields in seen] == [
        ("session_start", {"source": "new"}), ("session_end", {"reason": "clear"}), ("session_start", {"source": "clear"}),
        ("session_end", {"reason": "resume"}), ("session_start", {"source": "resume"}), ("session_end", {"reason": "exit"})]


async def test_continue_starts_with_resume_and_close_waits(tmp_path):
    saved = write_session(tmp_path, [user("一"), assistant("二")])
    marker = tmp_path / "bye.txt"
    hooks = engine(tmp_path, {"id": "bye", "event": "session_end", "async": True,
                              "action": {"type": "command", "command": py("import time; time.sleep(0.5)\n" + MARK, marker)}})
    seen = record(hooks)
    conversation = make(tmp_path, SequenceProvider(response("好")), hooks=hooks)
    await drain(conversation.resume(saved.id))
    await drain(conversation.reply("继续"))
    await conversation.close()
    assert names(seen) == ["session_start", "turn_start", "pre_send", "post_receive", "turn_end", "session_end"]
    assert seen[0][2] == {"source": "resume"} and marker.exists()


# /hooks -------------------------------------------------------------------------------------

async def test_hooks_command(tmp_path, server):
    base, _ = server
    root = project(tmp_path)
    write_hooks(hook_paths(root)[0][1],
                {"id": "web", "event": "turn_end", "action": {"type": "http", "url": base + "/ok?key=q-secret",
                                                              "headers": {"Authorization": "Bearer h-secret"}}},
                {"id": "bad", "event": "nope", "action": {"type": "prompt", "message": "x"}})
    write_hooks(root / ".mosscode" / "hooks.yaml", guard(once=True))
    hooks = HookEngine(root)
    conversation = make(tmp_path, SequenceProvider(), hooks=hooks)
    await hooks.fire(HookEvent("turn_end", fields={"reason": "completed"}))
    await hooks.drain(10)
    ui = await run(context(conversation), "/hooks")
    report = ui.notes[-1]
    assert report.startswith("Hook（已启用 1 条）") and "用户级" in report and "项目级" in report
    # Paths as the user knows them; condition and action on their own indented lines.
    shown = str(Path(".mosscode", "hooks.yaml"))
    assert f"用户级  {Path('~', shown)}\n  web · turn_end · 已启用\n      请求  POST {base}/ok\n" in report
    assert (f"项目级  {shown}（未确认，未启用）\n  guard · pre_tool_use [reject] [once] · 未确认\n"
            "      条件  path ~= config/prod/**\n      拒绝  生产配置只读\n") in report
    assert "· bad · event 需为" in report and f"项目 Hook 文件 {shown} 未确认" in report
    assert "最近执行" in report and "web  turn_end  成功" in report and "secret" not in report
    ui = await run(context(conversation), "/hooks web")
    assert "Authorization: '***'" in ui.notes[-1] and "secret" not in ui.notes[-1].replace("q-secret", "")
    assert "本次运行的执行记录" in ui.notes[-1] and "hooks.log" in ui.notes[-1]
    write_hooks(hook_paths(root)[0][1], {"id": "new", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    ui = await run(context(conversation), "/hooks reload")
    assert ui.notes[0] == "└ 已重新加载 Hook：已启用 1 条，新增 new，移除 web。"
    assert ui.notes[1].startswith(f"项目 Hook 文件 {shown} 尚未确认。\nHook 以 MossCode 的权限")
    assert ("choose", "信任项目 Hook · hooks.yaml", 2, 0) in ui.calls
    await ui.pick(0)
    assert [h.id for h in hooks.hooks] == ["new", "guard"] and ui.notes[-1] == f"└ 已信任并启用 {shown}。"
    await conversation.close()


async def test_trust_picker_cancel_declines_and_status_line(tmp_path):
    from mosscode.builtin_commands import hooks_line, offer_trust
    root = project(tmp_path)
    write_hooks(root / ".mosscode" / "hooks.yaml", guard())
    write_hooks(root / ".mosscode" / "hooks.local.yaml", {"id": "l", "event": "turn_end", "action": {"type": "command", "command": "x"}})
    hooks = HookEngine(root)
    assert hooks_line(hooks) == "已启用 0 条（项目级未确认 2 条）（/hooks 查看）"
    ui, done = FakeUI(), []

    async def finished():
        done.append(True)

    await offer_trust(ui, hooks, finished)
    await ui.cancel()
    assert done == [] and ui.notes[-1].startswith("项目 Hook 文件") and ui.notes[-1].endswith("x")
    await ui.pick(0)
    assert done == [True] and [h.id for h in hooks.hooks] == ["l"]
    assert hooks_line(HookEngine(root, enabled=False)) == "已关闭（--no-hooks）"


@pytest.mark.parametrize("key,trusted", [("enter", True), ("escape", False)])
async def test_tui_asks_before_session_start(tmp_path, key, trusted):
    from mosscode.tui import MossCodeApp
    hooks = engine(tmp_path, {"id": "ss", "event": "session_start", "action": {"type": "prompt", "message": "x"}},
                   trust=False)
    seen = record(hooks)
    app = MossCodeApp(make(tmp_path, SequenceProvider(), hooks=hooks), "test")
    async with app.run_test() as pilot:
        for _ in range(100):
            await pilot.pause()
            if app._pick is not None:
                break
        assert app._pick is not None and seen == []  # session_start waits for the answer.
        await pilot.press(key)
        for _ in range(100):
            await pilot.pause()
            if seen:
                break
        assert names(seen) == ["session_start"] and app._pick is None
        assert [h.id for h in hooks.hooks] == (["ss"] if trusted else [])
        assert TrustStore(hooks.root).path.exists() is trusted


def test_headless_hooks_command_exits_with_code_2(monkeypatch, capsys):
    setup_cli(monkeypatch, ["-p", "/hooks"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and capsys.readouterr().err.startswith("/hooks 只能在交互界面中使用。")


# -p -----------------------------------------------------------------------------------------

def cli_project(tmp_path, monkeypatch, *hooks, user_hooks=()):
    root = project(tmp_path)
    write_hooks(root / ".mosscode" / "hooks.yaml", *hooks)
    if user_hooks:
        write_hooks(hook_paths(root)[0][1], *user_hooks)
    monkeypatch.chdir(root)
    return root


def test_untrusted_project_hooks_are_skipped_with_a_notice(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "ran.txt"
    root = cli_project(tmp_path, monkeypatch, {"id": "p", "event": "turn_end", "action": {"type": "command", "command": py(MARK, marker)}})
    setup_cli(monkeypatch, ["-p", "你好"])
    cli.main()
    err = capsys.readouterr().err
    assert f"项目 Hook 文件 {root / '.mosscode' / 'hooks.yaml'} 未经确认，未启用" in err and not marker.exists()
    setup_cli(monkeypatch, ["-p", "你好", "--trust-project-hooks"])
    cli.main()
    assert marker.exists() and "未经确认" not in capsys.readouterr().err
    assert not TrustStore(root).path.exists()


def test_failures_go_to_stderr_and_stream_json(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "end.txt"
    cli_project(tmp_path, monkeypatch, user_hooks=(
        {"id": "boom", "event": "turn_start", "action": {"type": "command", "command": py("import sys; sys.exit(3)")}},
        {"id": "end", "event": "session_end", "action": {"type": "command", "command": py(MARK, marker)}}))
    setup_cli(monkeypatch, ["-p", "你好"])
    cli.main()
    out, err = capsys.readouterr()
    assert "Hook boom（turn_start）失败：退出码为 3；已忽略。" in err and marker.exists()
    marker.unlink()
    setup_cli(monkeypatch, ["-p", "你好", "--output-format", "stream-json"])
    cli.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(e.get("type") == "notice" and "Hook boom" in e.get("text", "") for e in events), events
    marker.unlink()
    setup_cli(monkeypatch, ["-p", "你好", "--no-hooks"])
    cli.main()
    assert "Hook boom" not in capsys.readouterr().err and marker.exists() is False
