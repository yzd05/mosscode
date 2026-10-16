"""Stage-1 model-side refinements: docs/specs/refinement-01-model-side/spec.md (AC1, AC4-AC8)."""

import asyncio
import json
import shutil
import subprocess

import anthropic
import httpx
import httpx2
import pytest
import yaml

from mosscode.config import ConfigError, ProviderConfig, find_config, load_settings
from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Delta, Message, ProviderError, Retrying, RunStopped, ToolCall
from mosscode.prompts import INSTRUCTION_LIMIT, git_snapshot, load_instructions
from mosscode.providers import AnthropicProvider, SDKProvider
from mosscode.tools import create_default_registry
from test_agent import SequenceProvider, call, response
from test_providers import SSEBody, anthropic_frames
from test_tools import python_command


# AC1: plain-text tool results.

async def test_command_failure_is_plain_text_with_stdout_once(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    failure = await registry.execute(call("execute_command", command=python_command(
        "import sys; print('marker-out'); print('marker-err', file=sys.stderr); sys.exit(3)"), timeout=10))
    text = failure.model_text()
    assert text.startswith("错误 [nonzero_exit]：命令退出码为 3。")
    assert text.count("marker-out") == 1 and text.count("marker-err") == 1
    assert not text.lstrip().startswith("{") and '"ok"' not in text
    ok = await registry.execute(call("execute_command", command=python_command("print('fine')"), timeout=10))
    assert ok.model_text() == ok.output and "fine" in ok.output


async def test_invalid_arguments_list_fields_for_the_model(tmp_path):
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    result = await registry.execute(call("read_file", path="a", offset="x", invented=True))
    assert result.code == "invalid_arguments"
    assert "- offset：" in result.output and "- invented：" in result.output


# AC4: truncated tool calls are never executed.

async def test_truncated_tool_call_is_not_executed_and_loop_continues(tmp_path):
    cut = Completed(Message("assistant", (), tool_calls=(ToolCall("w", "write_file", '{"path": "big.txt", "con'),)),
                    "max_tokens")
    provider = SequenceProvider(cut, response("改为分段写入"))
    conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False))
    events = [e async for e in conv.reply("写一个大文件")]
    assert not (tmp_path / "big.txt").exists()
    result = conv.history[2].tool_results[0]
    assert result.code == "output_truncated" and result.details == {"executed": False}
    assert len(provider.requests) == 2 and events[-1].reason == "completed"
    assert provider.requests[1][0][-1].tool_results[0].model_text().startswith("错误 [output_truncated]")


# AC5: retry only before the first event of a request.

class FlakyProvider(SDKProvider):
    def __init__(self, plan, retries=4):
        super().__init__(ProviderConfig("flaky", "anthropic-messages", "m", max_retries=retries), client=object())
        self.plan = list(plan)
        self.calls = 0
        self.retry_base_delay = 0

    async def _stream(self, messages, system, tools=(), *, allow_tools=True):
        self.calls += 1
        step = self.plan.pop(0)
        if step == "drop":
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://gateway.example"))
        if step == "partial":
            yield Delta("text", "部分")
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://gateway.example"))
        yield Delta("text", "好")
        yield response("好")


async def collect(provider):
    return [e async for e in provider.stream([], "system")]


async def test_retries_connection_drops_before_any_event():
    provider = FlakyProvider(["drop", "drop", "ok"])
    events = await collect(provider)
    assert [(e.attempt, e.max_attempts, e.reason) for e in events if isinstance(e, Retrying)] == [
        (1, 4, "连接中断"), (2, 4, "连接中断")]
    assert isinstance(events[-1], Completed) and provider.calls == 3


async def test_no_retry_after_first_event_or_when_disabled():
    partial = FlakyProvider(["partial", "ok"])
    with pytest.raises(ProviderError, match="连接"):
        await collect(partial)
    assert partial.calls == 1
    disabled = FlakyProvider(["drop", "ok"], retries=0)
    with pytest.raises(ProviderError):
        await collect(disabled)
    assert disabled.calls == 1


async def test_retry_wait_is_cancellable():
    provider = FlakyProvider(["drop", "ok"])
    provider.retry_base_delay = 30
    seen = asyncio.Event()

    async def consume():
        async for event in provider.stream([], "system"):
            if isinstance(event, Retrying):
                seen.set()
    task = asyncio.create_task(consume())
    await asyncio.wait_for(seen.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert provider.calls == 1


async def test_real_sdk_overloaded_529_then_success():
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=SSEBody(anthropic_frames()))

    sdk = anthropic.AsyncAnthropic(api_key="private-test-key", base_url="https://gateway.example/proxy",
                                   http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), max_retries=0)
    provider = AnthropicProvider(ProviderConfig("gateway", "anthropic-messages", "custom-model"), sdk)
    provider.retry_base_delay = 0
    try:
        events = [e async for e in Conversation(provider).reply("hello")]
        retries = [e for e in events if isinstance(e, Retrying)]
        assert len(retries) == 1 and "529" in retries[0].reason
        assert next(e for e in events if isinstance(e, Completed)).message.text == "你好"
        assert len(requests) == 2
    finally:
        await provider.close()


# AC6: config lookup and limits.

def write_yaml(path, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"providers": [{"name": path.parent.name or "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "K"}]}
    path.write_text(yaml.safe_dump({**data, **extra}), encoding="utf-8")
    return path


def test_config_lookup_order_and_missing_error(tmp_path):
    cwd, home = tmp_path / "work", tmp_path / "home"
    cwd.mkdir()
    with pytest.raises(ConfigError) as error:
        find_config(cwd, home)
    for expected in (cwd / ".mosscode" / "config.yaml", cwd / "config.yaml", home / ".mosscode" / "config.yaml"):
        assert str(expected) in str(error.value)
    user = write_yaml(home / ".mosscode" / "config.yaml")
    assert find_config(cwd, home) == user
    legacy = write_yaml(cwd / "config.yaml")
    assert find_config(cwd, home) == legacy
    project = write_yaml(cwd / ".mosscode" / "config.yaml")
    assert find_config(cwd, home) == project
    assert load_settings(project).source == project


def test_limits_defaults_and_validation(tmp_path):
    settings = load_settings(write_yaml(tmp_path / "a" / "config.yaml"))
    provider = settings.select()
    assert (settings.max_iterations, provider.max_output_tokens, provider.max_retries) == (200, 16384, 4)
    assert load_settings(write_yaml(tmp_path / "b" / "config.yaml", max_iterations=7)).max_iterations == 7
    for bad in (0, True, "9"):
        with pytest.raises(ConfigError, match="max_iterations"):
            load_settings(write_yaml(tmp_path / "c" / "config.yaml", max_iterations=bad))


async def test_configured_iteration_limit_applies(tmp_path):
    (tmp_path / "a").write_text("x")
    steps = [response(calls=[call("read_file", str(i), path="a")]) for i in range(5)]
    provider = SequenceProvider(*steps)
    conv = Conversation(provider, registry=create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False),
                        max_iterations=3)
    events = [e async for e in conv.reply("循环")]
    assert isinstance(events[-1], RunStopped) and events[-1].reason == "max_iterations"
    assert len(provider.requests) == 3


# AC7: AGENTS.md / CLAUDE.md project instructions.

def instruction_tree(tmp_path):
    home, parent = tmp_path / "home", tmp_path / "repo"
    project = parent / "pkg"
    (home / ".mosscode").mkdir(parents=True)
    project.mkdir(parents=True)
    (home / ".mosscode" / "AGENTS.md").write_text("用户偏好", encoding="utf-8")
    (parent / "CLAUDE.md").write_text("仓库约定", encoding="utf-8")
    (project / "AGENTS.md").write_text("子项目约定", encoding="utf-8")
    (project / "CLAUDE.md").write_text("被 AGENTS.md 取代", encoding="utf-8")
    return home, parent, project


def test_instruction_order_priority_and_truncation(tmp_path):
    home, parent, project = instruction_tree(tmp_path)
    files, problems = load_instructions(project, home)
    ours = [f for f in files if tmp_path in f.path.resolve().parents]
    assert [f.path.resolve() for f in ours] == [(home / ".mosscode" / "AGENTS.md").resolve(),
                                              (parent / "CLAUDE.md").resolve(), (project / "AGENTS.md").resolve()]
    assert files[-3:] == ours and not problems
    assert all("取代" not in f.text for f in files)
    # A multibyte character cut at the size limit is dropped, not reported.
    (project / "AGENTS.md").write_bytes(("中" * (INSTRUCTION_LIMIT // 3 + 10)).encode())
    (parent / "CLAUDE.md").write_bytes(b"\xff\xfe broken")
    files, problems = load_instructions(project, home)
    last = files[-1]
    assert last.truncated and set(last.text) == {"中"} and len(last.text.encode()) <= INSTRUCTION_LIMIT
    assert len(problems) == 1 and "UTF-8" in problems[0]


async def test_instructions_reach_cached_prefix_with_notices(tmp_path):
    home, _, project = instruction_tree(tmp_path)
    provider = SequenceProvider(response("好"), response("好"))
    conv = Conversation(provider, context_root=project, instructions_home=home)
    notices = await conv.start()
    assert any(n.startswith("已加载项目指令：") and "AGENTS.md" in n for n in notices)
    assert await conv.start() == ()
    await conv.ask("任务")
    stable = provider.requests[0][1].stable
    assert stable.index("## 文本输出") < stable.index("## 项目指令")
    assert stable.index("用户偏好") < stable.index("仓库约定") < stable.index("子项目约定")
    assert f'path="{project / "AGENTS.md"}"' in stable
    custom = SequenceProvider(response("好"))
    conv = Conversation(custom, "自定义系统提示", context_root=project, instructions_home=home)
    await conv.ask("任务")
    assert custom.requests[0][1].stable.startswith("自定义系统提示") and "子项目约定" in custom.requests[0][1].stable


async def test_without_context_root_nothing_is_loaded(tmp_path):
    provider = SequenceProvider(response("好"))
    conv = Conversation(provider)
    assert await conv.start() == ()
    await conv.ask("任务")
    assert "## 项目指令" not in provider.requests[0][1].stable
    assert "git" not in json.loads(provider.requests[0][1].supplements[0].text)


# AC8: git snapshot.

@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_snapshot_repo_and_non_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # Git still searches the ceiling itself.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_snapshot(plain) == {"is_repo": False}
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    for i in range(3):
        (repo / f"f{i}.txt").write_text("x")
    snap = git_snapshot(repo, max_lines=2)
    assert snap["is_repo"] and snap["branch"] in {"trunk", "HEAD"}
    assert "?? f0.txt" in snap["status"] and "另有 1 项" in snap["status"]


def test_git_failure_does_not_interrupt(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 3)
    monkeypatch.setattr(subprocess, "run", broken)
    assert git_snapshot(tmp_path) == {"available": False}


async def test_git_fact_reaches_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # Git still searches the ceiling itself.
    provider = SequenceProvider(response("好"))
    conv = Conversation(provider, context_root=tmp_path, instructions_home=tmp_path / "home")
    await conv.ask("任务")
    facts = json.loads(provider.requests[0][1].supplements[0].text)
    assert facts["git"] in ({"is_repo": False}, {"available": False})
