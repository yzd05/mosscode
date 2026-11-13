"""Evaluation harness: fixed coding tasks run against a real service, each judged by tests the agent never saw.

A task is a folder in evals/tasks: task.toml (category, prompt), repo/ (the project the agent works in), check/
(tests copied in after the agent finishes) and solution/ (a reference fix; tests/test_evals.py uses it to prove
every check fails on the untouched project and passes once fixed). Each run gets a fresh copy of repo/ in a
temporary folder and a conversation without memory, hooks, skills or the user's global instructions, so runs are
comparable across models and settings. Output is outcomes, token counts and timings: no prompt, reply or key.
"""

import argparse
import asyncio
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from mosscode.cli import _turns  # -p's loop: the task's turn, then a turn per batch of background results.
from mosscode.config import ConfigError, find_config, load_settings
from mosscode.conversation import Conversation
from mosscode.messages import ProviderError, RunStopped, ToolProgress
from mosscode.permissions import MODES
from mosscode.providers import create_provider
from mosscode.tools import create_default_registry
from mosscode.tools.background import HEADLESS_TIME_LIMIT
from mosscode.usage import cache_hit_rate

TASKS = Path(__file__).resolve().parents[1] / "evals" / "tasks"
CATEGORIES = ("bugfix", "feature", "refactor")
CHECK_TIMEOUT = 120.0


@dataclass(frozen=True)
class Task:
    name: str
    category: str
    prompt: str
    path: Path


def load_tasks(root: Path = TASKS, patterns: tuple[str, ...] = ()) -> list[Task]:
    """Tasks in name order; patterns (shell-style, e.g. "bugfix-*") narrow the set."""
    tasks = []
    for folder in sorted(p for p in root.iterdir() if (p / "task.toml").is_file()):
        if patterns and not any(fnmatch.fnmatch(folder.name, p) for p in patterns):
            continue
        data = tomllib.loads((folder / "task.toml").read_text(encoding="utf-8"))
        tasks.append(Task(folder.name, data["category"], data["prompt"].strip(), folder))
    return tasks


def prepare(task: Task, into: Path, *, solved: bool = False) -> Path:
    """A fresh copy of the task's project; solved lays the reference fix over it."""
    shutil.copytree(task.path / "repo", into)
    if solved:
        shutil.copytree(task.path / "solution", into, dirs_exist_ok=True)
    return into


def check(task: Task, workdir: Path, timeout: float = CHECK_TIMEOUT) -> tuple[bool, str]:
    """Copy the hidden tests in and run them; passed, and the tail of pytest's output."""
    shutil.copytree(task.path / "check", workdir, dirs_exist_ok=True)
    # Pins pytest's root here, so no settings from a folder above (this repository, say) leak into the check.
    (workdir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    names = sorted(p.name for p in (task.path / "check").glob("test_*.py"))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *names],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"检查超过 {timeout:.0f} 秒"
    return done.returncode == 0, "\n".join((done.stdout + done.stderr).strip().splitlines()[-5:])


async def run_task(
    task: Task,
    settings,
    config,
    *,
    provider_factory=create_provider,
    permission_mode: str = "bypassPermissions",
    timeout: float = 900.0,
    run: int = 1,
    keep: bool = False,
) -> dict:
    """One attempt at one task; a dict of outcome and counts, never the conversation's text."""
    base = Path(tempfile.mkdtemp(prefix=f"mosscode-eval-{task.name}-"))
    workdir = prepare(task, base / "project")
    home = base / "home"  # Empty: the user's global instructions stay out of the run.
    home.mkdir()
    registry = create_default_registry(
        workdir,
        permission_mode=permission_mode,
        load_permissions=False,
        shell=settings.shell,
        todo=settings.todo_tool,
    )
    conversation = Conversation(
        provider_factory(config),
        settings.system_prompt,
        registry,
        max_iterations=settings.max_iterations,
        context_root=workdir,
        instructions_home=home,
        budget=config.budget(),
        auto_compact=settings.auto_compact,
        context_collapse=settings.context_collapse,
        provider_info=(config.name, config.protocol, config.model),
        command_time_limit=HEADLESS_TIME_LIMIT,
        command_auto_background=settings.command_auto_background,
    )
    stop, error, tool_calls, tool_errors = "completed", None, 0, 0
    started = monotonic()
    try:
        async with asyncio.timeout(timeout):
            # As in -p: background results that arrive after the task's turn get turns of their own.
            async with aclosing(_turns(conversation, task.prompt, command=None, fork=None)) as stream:
                async for event in stream:
                    # Nobody approves here: what the chosen mode would ask about is refused as in -p.
                    if isinstance(event, ToolProgress) and event.phase == "finished":
                        tool_calls += 1
                        tool_errors += bool(event.result and event.result.is_error)
                    elif isinstance(event, RunStopped):
                        stop = event.reason
    except TimeoutError:
        stop, error = "timeout", f"超过 {timeout:.0f} 秒"
    except ProviderError as exc:
        stop, error = "error", str(exc)[:200]
    finally:
        seconds = monotonic() - started
        await conversation.close()
    try:
        # Judged even after an error or a timeout: whatever the agent left is what it delivered.
        passed, detail = await asyncio.to_thread(check, task, workdir)
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
    usage = conversation.total_usage
    return {
        "task": task.name,
        "category": task.category,
        "run": run,
        "provider": config.name,
        "model": config.model,
        "passed": passed,
        "stop": stop,
        "error": error,
        "check": None if passed else detail,
        "seconds": round(seconds, 1),
        "requests": usage.requests - usage.side_requests,
        "side_requests": usage.side_requests,
        "input_tokens": usage.input_tokens - usage.side_input_tokens,
        "cached_input_tokens": usage.cached_input_tokens - usage.side_cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "side_input_tokens": usage.side_input_tokens,
        "cache_reported": cache_hit_rate(usage) is not None,
        "cost_usd": None if usage.missing_cost else round(usage.cost_usd, 6),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "workdir": str(workdir) if keep else None,
    }


def summarize(rows: list[dict]) -> list[dict]:
    """One line per provider: pass rate, means per attempt, the conversation's cache hit rate and total cost."""
    out = []
    for provider in dict.fromkeys(r["provider"] for r in rows):
        mine = [r for r in rows if r["provider"] == provider]
        n = len(mine)
        tokens = sum(r["input_tokens"] for r in mine)
        out.append(
            {
                "provider": provider,
                "model": mine[0]["model"],
                "attempts": n,
                "passed": sum(r["passed"] for r in mine),
                "pass_rate": round(sum(r["passed"] for r in mine) / n, 4),
                "mean_requests": round(sum(r["requests"] for r in mine) / n, 1),
                "mean_input_tokens": round(tokens / n),
                "mean_output_tokens": round(sum(r["output_tokens"] for r in mine) / n),
                "hit_rate": (
                    round(sum(r["cached_input_tokens"] for r in mine) / tokens, 4)
                    if tokens and all(r["cache_reported"] for r in mine)
                    else None
                ),
                "mean_seconds": round(sum(r["seconds"] for r in mine) / n, 1),
                "cost_usd": (
                    round(sum(r["cost_usd"] for r in mine), 4) if all(r["cost_usd"] is not None for r in mine) else None
                ),
                "errors": sum(r["error"] is not None for r in mine),
            }
        )
    return out


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"


def report(rows: list[dict]) -> str:
    """The results as Markdown: one summary line per provider, then each task's outcome per provider."""
    summary = summarize(rows)
    lines = [
        "| Provider | 模型 | 通过 | 通过率 | 平均请求 | 平均输入 | 平均输出 | 缓存命中率 | 平均耗时 | 费用 | 出错 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in summary:
        cost = "—" if s["cost_usd"] is None else f"${s['cost_usd']:.4f}"
        lines.append(
            f"| {s['provider']} | {s['model']} | {s['passed']}/{s['attempts']} | {_percent(s['pass_rate'])}"
            f" | {s['mean_requests']} | {s['mean_input_tokens']:,} | {s['mean_output_tokens']:,}"
            f" | {_percent(s['hit_rate'])} | {s['mean_seconds']} 秒 | {cost} | {s['errors']} |"
        )
    providers = [s["provider"] for s in summary]
    lines += ["", "| 任务 | 类别 | " + " | ".join(providers) + " |", "| --- | --- |" + " --- |" * len(providers)]
    for name in dict.fromkeys(r["task"] for r in rows):
        mine = [r for r in rows if r["task"] == name]
        cells = []
        for provider in providers:
            attempts = [r for r in mine if r["provider"] == provider]
            if not attempts:
                cells.append("—")
                continue
            passed = sum(r["passed"] for r in attempts)
            requests = round(sum(r["requests"] for r in attempts) / len(attempts), 1)
            cells.append(f"{'✓' if passed == len(attempts) else '✗'} {passed}/{len(attempts)} · {requests} 次请求")
        lines.append(f"| {name} | {mine[0]['category']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


async def evaluate(settings, providers, tasks, *, repeat=1, on_row=None, **options) -> list[dict]:
    """Every task repeat times for each provider, one at a time so timings and caches do not interfere."""
    rows = []
    for name in providers:
        config = settings.select(name)
        for task in tasks:
            for run in range(1, repeat + 1):
                row = await run_task(task, settings, config, run=run, **options)
                rows.append(row)
                if on_row is not None:
                    on_row(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="在固定任务上运行 MossCode，用隐藏测试判定，输出通过率、Token、缓存命中率和耗时"
    )
    parser.add_argument("--config", type=Path, help="配置路径；省略时与 moss 相同的查找顺序")
    parser.add_argument(
        "--provider", action="append", help="Provider 配置名称，可重复给出以对比多个；省略时遵循配置默认值"
    )
    parser.add_argument("--tasks", nargs="+", default=(), help="只运行名称匹配的任务，例如 'bugfix-*'")
    parser.add_argument("--repeat", type=int, default=1, help="每个任务运行几次（默认 1）")
    parser.add_argument("--timeout", type=float, default=900.0, help="每次运行的时限，秒（默认 900）")
    parser.add_argument(
        "--permission-mode", choices=MODES, default="bypassPermissions", help="权限模式（默认 bypassPermissions）"
    )
    parser.add_argument("--output", type=Path, help="保存逐次结果和汇总的 JSON")
    parser.add_argument("--report", type=Path, help="保存 Markdown 报告")
    parser.add_argument("--keep", action="store_true", help="保留每次运行的临时项目，便于查看 Agent 改了什么")
    parser.add_argument("--list", action="store_true", help="列出任务后退出")
    args = parser.parse_args(argv)
    tasks = load_tasks(patterns=tuple(args.tasks))
    if args.list:
        for task in tasks:
            print(f"{task.name}\t{task.category}")
        return
    if not tasks:
        parser.exit(2, "没有匹配的任务。\n")
    if args.repeat < 1:
        parser.error("--repeat 至少为 1")
    try:
        settings = load_settings(args.config or find_config())
        rows = asyncio.run(
            evaluate(
                settings,
                args.provider or [None],
                tasks,
                repeat=args.repeat,
                on_row=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True),
                permission_mode=args.permission_mode,
                timeout=args.timeout,
                keep=args.keep,
            )
        )
    except ConfigError as exc:
        parser.exit(1, f"MossCode: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "已取消评估。\n")
    text = report(rows)
    print(text, end="")
    if args.output:
        data = {"rows": rows, "summary": summarize(rows)}
        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
