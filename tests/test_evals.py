import json
import tomllib

import pytest
from scripts import run_evals

from mosscode.config import ProviderConfig, Settings
from mosscode.messages import Usage
from test_agent import SequenceProvider, call, response

TASKS = run_evals.load_tasks()


def test_the_task_set_covers_each_category_with_complete_folders():
    assert len(TASKS) >= 10
    assert {t.category for t in TASKS} == set(run_evals.CATEGORIES)
    for task in TASKS:
        assert task.prompt, task.name
        assert set(tomllib.loads((task.path / "task.toml").read_text(encoding="utf-8"))) == {"category", "prompt"}
        checks = list((task.path / "check").glob("test_*.py"))
        assert checks, task.name
        # A hidden test must not be visible, or overwritten by, anything in the project the agent sees.
        assert not any((task.path / "repo" / c.name).exists() for c in checks), task.name
        assert any((task.path / "solution").rglob("*.py")), task.name


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_each_check_fails_on_the_project_and_passes_with_the_reference_fix(task, tmp_path):
    untouched = run_evals.prepare(task, tmp_path / "untouched")
    assert run_evals.check(task, untouched)[0] is False
    solved = run_evals.prepare(task, tmp_path / "solved", solved=True)
    passed, detail = run_evals.check(task, solved)
    assert passed, detail


def settings():
    return Settings((ProviderConfig("fake", "openai-chat-completions", "fake-model"),))


def pagination():
    return next(t for t in TASKS if t.name == "bugfix-pagination")


async def test_a_run_that_fixes_the_project_passes_and_reports_counts_but_no_text(monkeypatch, tmp_path):
    monkeypatch.setattr(run_evals.tempfile, "tempdir", str(tmp_path))
    task = pagination()
    fixed = (task.path / "solution" / "pagination.py").read_text(encoding="utf-8")
    provider = SequenceProvider(
        response("private-plan", [call("read_file", path="pagination.py")], Usage(1_000, 50, 0, 0)),
        response("", [call("write_file", path="pagination.py", content=fixed)], Usage(1_200, 400, 1_000, 0)),
        response("private-answer", usage=Usage(1_700, 20, 1_100, 0)),
    )
    config = settings().select(None)
    row = await run_evals.run_task(task, settings(), config, provider_factory=lambda c: provider)
    assert row["passed"] is True and row["stop"] == "completed" and row["error"] is None
    assert (row["requests"], row["tool_calls"], row["tool_errors"]) == (3, 2, 0)
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (3_900, 2_100, 470)
    assert row["cache_reported"] is True and row["provider"] == "fake" and row["model"] == "fake-model"
    text = json.dumps(row, ensure_ascii=False)
    assert "private" not in text and fixed not in text
    assert not list(tmp_path.iterdir())  # The temporary project is removed.
    # Only the run's copy changed.
    assert "start = page * per_page" in (task.path / "repo" / "pagination.py").read_text(encoding="utf-8")


async def test_a_run_that_changes_nothing_fails_and_keeps_the_project_on_request(monkeypatch, tmp_path):
    monkeypatch.setattr(run_evals.tempfile, "tempdir", str(tmp_path))
    provider = SequenceProvider(response("done", usage=Usage(800, 5, 0, 0)))
    config = settings().select(None)
    row = await run_evals.run_task(pagination(), settings(), config, provider_factory=lambda c: provider, keep=True)
    assert row["passed"] is False and row["check"]
    assert (tmp_path / row["workdir"]).joinpath("test_hidden.py").is_file()


async def test_what_the_mode_would_ask_about_is_refused_and_the_run_goes_on(monkeypatch, tmp_path):
    monkeypatch.setattr(run_evals.tempfile, "tempdir", str(tmp_path))
    provider = SequenceProvider(
        response("", [call("write_file", path="new.txt", content="x")]),
        response("done"),
    )
    config = settings().select(None)
    row = await run_evals.run_task(
        pagination(), settings(), config, provider_factory=lambda c: provider, permission_mode="default"
    )
    assert row["tool_calls"] == 1 and row["tool_errors"] == 1 and row["stop"] == "completed"
    assert provider.requests[1][0][-1].tool_results[0].code == "approval_required"


async def test_a_provider_error_is_recorded_and_the_project_is_still_checked(monkeypatch, tmp_path):
    from mosscode.messages import ProviderError

    monkeypatch.setattr(run_evals.tempfile, "tempdir", str(tmp_path))
    provider = SequenceProvider(ProviderError("服务不可用"))
    config = settings().select(None)
    row = await run_evals.run_task(pagination(), settings(), config, provider_factory=lambda c: provider)
    assert row["stop"] == "error" and "服务不可用" in row["error"] and row["passed"] is False


def row(task, provider, passed, requests=4, input_tokens=10_000, cached=8_000, cost=0.01, reported=True):
    return {
        "task": task,
        "category": "bugfix",
        "provider": provider,
        "model": provider + "-model",
        "passed": passed,
        "requests": requests,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": 300,
        "cache_reported": reported,
        "seconds": 12.0,
        "cost_usd": cost,
        "error": None,
    }


def test_report_compares_providers_by_pass_rate_tokens_and_hit_rate():
    rows = [
        row("a", "deepseek", True),
        row("b", "deepseek", False, requests=6),
        row("a", "kimi", True, cached=5_000, cost=None),
        row("b", "kimi", True, reported=False),
    ]
    first, second = run_evals.summarize(rows)
    assert (first["provider"], first["passed"], first["pass_rate"], first["mean_requests"]) == ("deepseek", 1, 0.5, 5)
    assert first["hit_rate"] == 0.8 and first["cost_usd"] == 0.02
    assert second["pass_rate"] == 1.0 and second["hit_rate"] is None and second["cost_usd"] is None
    text = run_evals.report(rows)
    assert "| deepseek | deepseek-model | 1/2 | 50.0% | 5.0 | 10,000 | 300 | 80.0% | 12.0 秒 | $0.0200 | 0 |" in text
    assert "| a | bugfix | ✓ 1/1 · 4.0 次请求 | ✓ 1/1 · 4.0 次请求 |" in text
    assert "| b | bugfix | ✗ 0/1 · 6.0 次请求 | ✓ 1/1 · 4.0 次请求 |" in text


def test_tasks_can_be_narrowed_by_pattern():
    names = [t.name for t in run_evals.load_tasks(patterns=("refactor-*",))]
    assert names and all(n.startswith("refactor-") for n in names)
