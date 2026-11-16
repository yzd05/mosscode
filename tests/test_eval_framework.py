import copy
import json
from pathlib import Path

import pytest
from evals.harbor.results import compatible, normalize_trial, paired_successes, parse_stream, summary
from scripts.run_harbor_evals import BatchBudget, main, reject_credentials


def manifest():
    from evals.harbor.results import digest

    agent = {"name": "reference"}
    return {
        "series": "test",
        "run_id": "one",
        "conditions": {"model": {"backend_version": None}},
        "prices": None,
        "agents": [{"label": "reference", "config_hash": digest(agent)}],
        "plan": [
            {"task": task, "agent": "reference", "repeat": repeat} for task in ("a", "b") for repeat in range(1, 4)
        ],
    }


@pytest.mark.parametrize("missing, cost", [(0, 0.25), (1, None)])
def test_final_totals_include_auxiliary_calls_once_and_require_complete_cost(missing, cost):
    events = [
        {"type": "assistant", "usage": {"input_tokens": 4, "output_tokens": 2}},
        {
            "type": "result",
            "subtype": "completed",
            "usage": {
                "requests": 2,
                "input_tokens": 9,
                "output_tokens": 5,
                "cached_input_tokens": 3,
                "cache_write_tokens": 0,
            },
            "accounting": {
                "missing_input": 0,
                "missing_output": 0,
                "missing_cached": 0,
                "missing_cache_write": 0,
                "missing_cost": missing,
                "side_requests": 1,
                "side_input_tokens": 5,
            },
            "total_cost_usd": 0.25,
        },
    ]
    got = parse_stream("\n".join(map(json.dumps, events)))
    assert (got["requests"], got["input_tokens"], got["output_tokens"], got["side_requests"]) == (2, 9, 5, 1)
    assert got["cost_usd"] == cost


def test_old_stream_cannot_prove_auxiliary_usage_or_accept_a_cost_subtotal():
    got = parse_stream(
        json.dumps(
            {
                "type": "result",
                "subtype": "completed",
                "usage": {"requests": 2, "input_tokens": 100},
                "total_cost_usd": 0.1,
            }
        )
    )
    assert got["input_tokens"] is None and got["cost_usd"] is None and got["side_requests"] is None
    assert got["diagnostics"]["collapses"] is None
    assert parse_stream('{"type":"assistant"}\n{"type":')["stop"] == "missing_result"


def trial():
    return {
        "id": "1",
        "task_name": "a",
        "trial_name": "a-1",
        "task_checksum": "abc",
        "config": {"agent": {"name": "reference"}},
        "agent_result": {"cost_usd": 0.2},
        "agent_execution": {"started_at": "2026-11-14T00:00:00"},
        "started_at": "2026-11-14T00:00:00",
        "finished_at": "2026-11-14T00:00:10",
        "reward_valid": True,
        "verifier_result": {"rewards": {"reward": 0}},
    }


def test_native_task_namespace_maps_to_the_frozen_plan():
    raw = trial()
    raw["task_name"] = "terminal-bench/a"
    raw["verifier_result"]["rewards"]["reward"] = 1
    plan = manifest()
    plan["trial_plan"] = {"a-1": {"task": "a", "repeat": 1}}
    got = normalize_trial(raw, Path("result.json"), plan)
    assert got["task"] == "a" and got["native_task_name"] == "terminal-bench/a" and got["repeat"] == 1
    assert summary({"manifest": plan, "attempts": [got]}, "reference")["pass_rate"] == pytest.approx(1 / 6)


def test_judgement_and_stop_are_separate_and_service_errors_keep_their_cost():
    raw = trial()
    raw["agent_result"]["metadata"] = {"mosscode": {"stop": "completed"}}
    got = normalize_trial(raw, Path("result.json"), manifest())
    assert not got["passed"] and got["stop"] == "completed" and got["cost_usd"] == 0.2
    raw["exception_info"] = {"exception_type": "ApiRateLimitError"}
    raw["reward_valid"] = False
    raw["verifier_result"]["rewards"]["reward"] = 1
    got = normalize_trial(raw, Path("result.json"), manifest())
    assert not got["passed"] and got["error_category"] == "service" and got["cost_usd"] == 0.2
    setup_failed = trial()
    setup_failed["agent_result"] = None
    setup_failed["agent_execution"] = None
    setup_failed["reward_valid"] = False
    setup_failed["exception_info"] = {"exception_type": "NonZeroAgentExitCodeError"}
    got = normalize_trial(setup_failed, Path("result.json"), manifest())
    assert got["error_category"] == "environment" and got["cost_usd"] == 0.0


@pytest.mark.parametrize("change", ["dataset", "model", "environment"])
def test_incompatible_experiments_are_rejected(change):
    left = {"manifest": manifest(), "attempts": []}
    right = copy.deepcopy(left)
    right["manifest"]["conditions"][change] = "different"
    with pytest.raises(ValueError, match="不相容"):
        compatible(left, right)


def test_repeats_are_task_weighted_and_missing_attempts_stay_visible():
    rows = [
        {
            "id": str(i),
            "task": "a" if i < 3 else "b",
            "agent": "reference",
            "finished": True,
            "passed": i < 3,
            "cost_usd": 0.1,
            "seconds": 10,
            "stop": "completed",
        }
        for i in range(4)
    ]
    got = summary({"manifest": manifest(), "attempts": rows}, "reference")
    assert got["pass_rate"] == 0.5 and got["coverage"] == pytest.approx(4 / 6)
    assert got["cost_usd"] == pytest.approx(0.4) and got["completed_but_failed"] == 1


def test_budget_reserves_inflight_estimates_and_stops_on_unknown_spend():
    guard = BatchBudget(1, 0.4, [{"id": "done", "cost_usd": 0.3}])
    assert guard.may_start()
    guard.active.add("running")
    assert not guard.may_start()
    assert not BatchBudget(1, 0.1, [{"id": "unknown", "cost_usd": None}]).may_start()


def test_credentials_are_not_accepted_in_archived_job_configuration(tmp_path):
    with pytest.raises(ValueError, match="凭据"):
        reject_credentials({"agents": [{"kwargs": {"api_key": "not-a-real-key"}}]})
    with pytest.raises(ValueError, match="占位符"):
        reject_credentials({"env": {"DEEPSEEK_API_KEY": "not-a-real-key"}})
    reject_credentials({"env": {"DEEPSEEK_API_KEY": "${DEEPSEEK_API_KEY}"}})
    with pytest.raises(ValueError, match="端点"):
        reject_credentials({"base_url": "https://api.example.com?api_key=not-a-real-key"})
    from scripts.run_harbor_evals import redact_logs

    log = tmp_path / "stderr.log"
    log.write_text("server echoed offline-test-secret", encoding="utf-8")
    redact_logs(tmp_path, ["offline-test-secret"])
    assert log.read_text(encoding="utf-8") == "server echoed [REDACTED]"


def test_default_entry_only_prints_the_complete_plan(capsys):
    main(["run", "--series", "offline", "--run-id", "preview", "--budget-usd", "1", "--estimate-usd", ".01"])
    assert json.loads(capsys.readouterr().out)["planned_attempts"] == 6


def test_install_preflight_has_no_model_budget(capsys):
    main(["run", "--series", "offline", "--run-id", "install", "--install-only"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "install-only" and plan["estimated_usd"] == 0 and plan["budget_usd"] is None


def test_frozen_baseline_matches_the_checked_in_evaluation_inputs():
    from evals.harbor.results import digest, text_sha256

    root = Path(__file__).parents[1]
    lock = json.loads((root / "evals/harbor/baseline.lock.json").read_text(encoding="utf-8"))
    dataset_path = root / "evals/harbor/dataset.lock.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    assert lock["tasks"] == dataset["subsets"]["dev"]
    assert lock["artifacts"] == {
        "dataset_lock_sha256": text_sha256(dataset_path),
        "job_config_sha256": text_sha256(root / "evals/harbor/job.yaml"),
        "runner": digest((root / "scripts/run_harbor_evals.py").read_text(encoding="utf-8")),
        "mosscode_adapter": digest((root / "evals/harbor/mosscode_agent.py").read_text(encoding="utf-8")),
        "terminus_setup_adapter": digest((root / "evals/harbor/terminus_agent.py").read_text(encoding="utf-8")),
    }


def test_text_sha256_matches_across_line_endings(tmp_path):
    from evals.harbor.results import text_sha256

    lf = tmp_path / "lf.yaml"
    crlf = tmp_path / "crlf.yaml"
    lf.write_bytes(b"a: 1\nb: 2\n")
    crlf.write_bytes(b"a: 1\r\nb: 2\r\n")
    assert text_sha256(lf) == text_sha256(crlf)


def test_paired_costs_use_only_joint_successes_and_require_common_prices():
    left = {
        "manifest": manifest(),
        "attempts": [
            {
                "task": "a",
                "agent": "reference",
                "repeat": 1,
                "passed": True,
                "finished": True,
                "seconds": 10,
                "cost_usd": 0.2,
            }
        ],
    }
    right = copy.deepcopy(left)
    right["manifest"]["run_id"] = "two"
    right["attempts"][0].update(seconds=5, cost_usd=0.1)
    (pair,) = paired_successes([left, right])
    assert pair["matched"] == 1 and pair["time_ratio"] == 0.5 and pair["cost_ratio"] is None
    left["manifest"]["prices"] = right["manifest"]["prices"] = {"source": "snapshot"}
    assert paired_successes([left, right])[0]["cost_ratio"] == 0.5
    combined = {
        "manifest": {
            **manifest(),
            "prices": {"source": "snapshot"},
            "agents": [{"label": "candidate", "config_hash": "1"}, {"label": "terminus-2", "config_hash": "2"}],
            "plan": [{"task": "a", "agent": label, "repeat": 1} for label in ("candidate", "terminus-2")],
        },
        "attempts": [
            {
                "task": "a",
                "agent": "candidate",
                "repeat": 1,
                "passed": True,
                "finished": True,
                "seconds": 6,
                "cost_usd": 0.1,
                "stop": "completed",
                "artifacts": "result.json",
            },
            {
                "task": "a",
                "agent": "terminus-2",
                "repeat": 1,
                "passed": True,
                "finished": True,
                "seconds": 10,
                "cost_usd": 0.2,
                "stop": "completed",
                "artifacts": "result.json",
            },
        ],
    }
    from evals.harbor.results import markdown

    text, summaries = markdown([combined])
    assert "| one/terminus-2 → one/candidate | 1 | 0.5 | 0.6 |" in text
    assert summaries[0]["paired_successes"]["time_ratio"] == pytest.approx(0.6)


def test_zero_request_result_reports_zero_cost_when_accounting_is_complete():
    got = parse_stream(
        json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "usage": {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                },
                "accounting": {
                    "missing_input": 0,
                    "missing_output": 0,
                    "missing_cached": 0,
                    "missing_cache_write": 0,
                    "missing_cost": 0,
                },
                "total_cost_usd": None,
            }
        )
    )
    assert got["requests"] == 0 and got["cost_usd"] == 0.0


def test_mosscode_timeout_and_cancellation_override_missing_result_and_empty_context_has_zero_spend():
    timed_out = trial()
    timed_out["reward_valid"] = False
    timed_out["exception_info"] = {"exception_type": "AgentTimeoutError"}
    timed_out["agent_result"] = {"cost_usd": None, "metadata": {"mosscode": {"stop": "missing_result"}}}
    got_timeout = normalize_trial(timed_out, Path("result.json"), manifest())
    assert got_timeout["stop"] == "timeout" and got_timeout["finished"] is True

    cancelled = copy.deepcopy(timed_out)
    cancelled["exception_info"] = {"exception_type": "CancelledError"}
    got_cancel = normalize_trial(cancelled, Path("result.json"), manifest())
    assert got_cancel["stop"] == "cancelled" and got_cancel["finished"] is False

    nonzero = copy.deepcopy(timed_out)
    nonzero["exception_info"] = {"exception_type": "NonZeroAgentExitCodeError"}
    assert normalize_trial(nonzero, Path("result.json"), manifest())["stop"] == "missing_result"

    setup_failed = trial()
    setup_failed["agent_execution"] = None
    setup_failed["reward_valid"] = False
    setup_failed["exception_info"] = {"exception_type": "EnvironmentStartError"}
    setup_failed["agent_result"] = {
        "n_input_tokens": None,
        "n_output_tokens": None,
        "n_cache_tokens": None,
        "cost_usd": None,
        "metadata": None,
    }
    got_setup = normalize_trial(setup_failed, Path("result.json"), manifest())
    assert got_setup["error_category"] == "environment" and got_setup["cost_usd"] == 0.0


def test_pane_redaction_and_credential_name_validation(tmp_path):
    from scripts.run_harbor_evals import redact_logs

    with pytest.raises(ValueError, match="model_api_key_name"):
        reject_credentials({"agents": [{"model_api_key_name": "sk-live-not-an-env-name"}]})
    reject_credentials({"agents": [{"model_api_key_name": "DEEPSEEK_API_KEY"}]})

    pane = tmp_path / "terminus_2.pane"
    pane.write_text("export DEEPSEEK_API_KEY=offline-pane-secret\n", encoding="utf-8")
    redact_logs(tmp_path, ["offline-pane-secret"])
    assert pane.read_text(encoding="utf-8") == "export DEEPSEEK_API_KEY=[REDACTED]\n"


def test_controlled_finish_command_ablation_and_duplicate_run_ids_compare_cleanly(tmp_path):
    from evals.harbor.results import controlled_document, markdown
    from scripts import compare_evals

    def make_checkpoint(finish_commands, seconds):
        return {
            "metadata": {
                "providers": [{"name": "fake", "model": "fake-model"}],
                "tasks": {"bugfix-a": "hash-a"},
                "repeat": 1,
                "settings": {"max_iterations": 20},
                "system_prompt": "prompt-hash",
                "options": {
                    "permission_mode": "bypassPermissions",
                    "timeout": 900.0,
                    "finish_commands": finish_commands,
                },
                "python_source_sha256": "src-hash",
            },
            "rows": [
                {
                    "task": "bugfix-a",
                    "provider": "fake",
                    "run": 1,
                    "passed": True,
                    "stop": "completed",
                    "error": None,
                    "seconds": seconds,
                    "cost_usd": 0.01,
                    "total_input_tokens": 100,
                    "cached_input_tokens": 50,
                    "side_cached_input_tokens": 0,
                    "output_tokens": 20,
                    "missing_input": 0,
                    "missing_cached": 0,
                    "missing_output": 0,
                    "tool_errors": 0,
                }
            ],
        }

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    file_a = dir_a / "local.json"
    file_b = dir_b / "local.json"
    file_a.write_text(json.dumps(make_checkpoint([], 10.0)), encoding="utf-8")
    file_b.write_text(json.dumps(make_checkpoint(["pytest -q"], 5.0)), encoding="utf-8")

    doc_a = controlled_document(json.loads(file_a.read_text(encoding="utf-8")), file_a)
    doc_b = controlled_document(json.loads(file_b.read_text(encoding="utf-8")), file_b)
    assert "finish_commands" not in doc_a["manifest"]["conditions"]["execution"]
    assert doc_b["manifest"]["agents"][0]["settings"]["finish_commands"] == ["pytest -q"]

    text, summaries = markdown([doc_a, doc_b])
    assert "local#1 / fake" in text and "local#2 / fake" in text
    assert "| local#1/fake → local#2/fake | 1 | — | 0.5 |" in text
    assert len(summaries) == 2

    out_json = tmp_path / "report.json"
    compare_evals.main([str(file_a), str(file_b), "--output", str(out_json)])
    assert out_json.is_file() and (tmp_path / "report.md").is_file()
    assert json.loads(out_json.read_text(encoding="utf-8"))["summary"][1]["run_id"] == "local#2"
    assert "# 评估对照" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_paired_successes_matches_mosscode_baseline_instead_of_terminus_across_runs():
    baseline_doc = {
        "manifest": {
            **manifest(),
            "run_id": "baseline-01",
            "prices": {"source": "snapshot"},
            "agents": [{"label": "candidate", "config_hash": "1"}, {"label": "terminus-2", "config_hash": "2"}],
            "plan": [{"task": "a", "agent": label, "repeat": 1} for label in ("candidate", "terminus-2")],
        },
        "attempts": [
            {
                "task": "a",
                "agent": "candidate",
                "repeat": 1,
                "passed": True,
                "finished": True,
                "seconds": 20,
                "cost_usd": 0.2,
            },
            {
                "task": "a",
                "agent": "terminus-2",
                "repeat": 1,
                "passed": True,
                "finished": True,
                "seconds": 40,
                "cost_usd": 0.4,
            },
        ],
    }
    candidate_doc = {
        "manifest": {
            **manifest(),
            "run_id": "candidate-02",
            "prices": {"source": "snapshot"},
            "agents": [{"label": "candidate", "config_hash": "3"}],
            "plan": [{"task": "a", "agent": "candidate", "repeat": 1}],
        },
        "attempts": [
            {
                "task": "a",
                "agent": "candidate",
                "repeat": 1,
                "passed": True,
                "finished": True,
                "seconds": 10,
                "cost_usd": 0.1,
            }
        ],
    }
    (pair,) = paired_successes([baseline_doc, candidate_doc])
    assert pair["baseline"] == "baseline-01/candidate"
    assert pair["candidate"] == "candidate-02/candidate"
    assert pair["cost_ratio"] == pytest.approx(0.5) and pair["time_ratio"] == pytest.approx(0.5)


def test_mode_mismatch_and_cli_validation_guards(tmp_path):
    left = {"manifest": {**manifest(), "mode": "install-only"}, "attempts": []}
    right = {"manifest": {**manifest(), "mode": "evaluation"}, "attempts": []}
    with pytest.raises(ValueError, match="mode"):
        compatible(left, right)

    bad_prices = tmp_path / "prices.json"
    bad_prices.write_text(
        json.dumps({"source": "s", "date": "2026-11-16", "input": True, "cached_input": 0, "output": 0}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        main(["run", "--series", "s", "--run-id", "r", "--install-only", "--wheel", "missing-equals"])
    with pytest.raises(SystemExit):
        main(["run", "--series", "s", "--run-id", "r", "--install-only", "--agent", "nonexistent"])
    with pytest.raises(SystemExit):
        main(["run", "--series", "s", "--run-id", "r", "--install-only", "--prices", str(bad_prices)])

    root = Path(__file__).parents[1]
    lock_path = root / "evals/harbor/baseline.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    wheel = tmp_path / "mosscode-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"placeholder")
    wheel.with_suffix(".json").write_text(
        json.dumps(
            {
                "revision": lock["agents"][0]["revision"],
                "wheel_sha256": lock["agents"][0]["wheel_sha256"],
            }
        ),
        encoding="utf-8",
    )
    base_cmd = [
        "run",
        "--series",
        "s",
        "--run-id",
        "r",
        "--scope",
        "dev",
        "--wheel",
        f"candidate={wheel}",
        "--backend-version",
        lock["model"]["backend_version"],
        "--baseline-lock",
        str(lock_path),
        "--install-only",
    ]
    main([*base_cmd, "--prices", str(root / lock["pricing"]["snapshot"])])
    with pytest.raises(SystemExit):
        main([*base_cmd, "--timeout", "60"])
    other_prices = tmp_path / "other-prices.json"
    other_prices.write_text(
        json.dumps({"source": "other", "date": "2026-11-16", "input": 1.0, "cached_input": 0.5, "output": 2.0}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        main([*base_cmd, "--prices", str(other_prices)])


def test_leading_slash_instructions_escape_cleanly_for_headless_mode():
    from mosscode.cli import _headless_input

    for raw in ("/review server.py fails on startup", "/unknown_cmd fix bug", "// C++ header\nint main() {}", "/"):
        escaped = "/" + raw.lstrip() if raw.lstrip().startswith("/") else raw
        prompt, command, skill = _headless_input(escaped)
        assert prompt == raw.lstrip() and command is None and skill is None
