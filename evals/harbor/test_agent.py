"""Run explicitly in the pinned Harbor venv; no Docker or model service is used."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from evals.harbor.mosscode_agent import ROOT, MossCodeAgent
from evals.harbor.terminus_agent import ReliableTmuxSession
from harbor.agents.installed.base import ApiError, NonZeroAgentExitCodeError
from harbor.models.agent.context import AgentContext
from harbor.models.job.config import JobConfig

REPO = Path(__file__).resolve().parents[2]


class Environment:
    def __init__(self, output):
        self.output = output
        self.uploads = {}
        self.downloads = []
        self.commands = []
        self.environments = []

    async def exec(self, command, **kwargs):
        self.commands.append(command)
        self.environments.append(kwargs.get("env", {}))
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    async def upload_file(self, source_path, target_path):
        self.uploads[target_path] = Path(source_path).read_bytes()

    async def download_file(self, source_path, target_path):
        self.downloads.append(source_path)
        Path(target_path).write_text(self.output if source_path.endswith("moss.jsonl") else "0", encoding="utf-8")


def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_OFFLINE_KEY", "offline-test-key")
    wheel = tmp_path / "mosscode-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"fixture-wheel")
    wheel.with_suffix(".json").write_text(
        json.dumps({"revision": "a" * 40, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    return MossCodeAgent(
        tmp_path / "logs",
        wheel=wheel,
        model_name="deepseek/deepseek-flash",
        model_api_key_name="EVAL_OFFLINE_KEY",
        base_url="https://api.deepseek.com",
        model_api_format="openai_chat_completions",
    )


def test_native_job_schema_accepts_the_common_configuration():
    cfg = JobConfig.model_validate(yaml.safe_load((REPO / "evals/harbor/job.yaml").read_text(encoding="utf-8")))
    assert len(cfg.agents) == 2 and cfg.retry.max_retries == 0 and cfg.agent_setup_timeout_multiplier == 2
    from harbor.agents.terminus_2.terminus_2 import Terminus2

    options = Terminus2.parse_options(cfg.agents[1].kwargs, {})
    assert options.llm_call_kwargs["max_tokens"] == 16384 and options.temperature is None
    assert options.record_terminal_session is False


def test_terminus_setup_adapter_retries_package_downloads():
    session = object.__new__(ReliableTmuxSession)
    command = session._get_combined_install_command({"package_manager": "apt-get"}, ["tmux"])
    assert "Acquire::Retries=3" in command and "attempt in 1 2 3" in command


async def test_installed_agent_keeps_task_dependencies_separate_and_counts_cli_totals(tmp_path, monkeypatch):
    item = agent(tmp_path, monkeypatch)
    environment = Environment(
        json.dumps(
            {
                "type": "result",
                "subtype": "completed",
                "usage": {"requests": 2, "input_tokens": 30, "output_tokens": 5, "cached_input_tokens": 10},
                "accounting": {
                    "missing_input": 0,
                    "missing_output": 0,
                    "missing_cached": 0,
                    "missing_cache_write": 0,
                    "missing_cost": 0,
                    "side_requests": 1,
                },
                "total_cost_usd": 0.2,
            }
        )
    )
    await item.install(environment)
    await item.configure(environment)
    cfg = json.loads(environment.uploads[ROOT + "/config.json"])
    assert cfg["memory"] is False and cfg["providers"][0]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert "offline-test-key" not in json.dumps(cfg)
    assert any(f"--python {ROOT}/venv/bin/python" in cmd for cmd in environment.commands)
    context = AgentContext()
    await item.run("/review the visible project", environment, context)
    assert (context.n_input_tokens, context.n_output_tokens, context.cost_usd) == (30, 5, 0.2)
    assert context.metadata["mosscode"]["side_requests"] == 1
    assert environment.environments[-1]["DEEPSEEK_API_KEY"] == "offline-test-key"
    assert f"-p < {ROOT}/instruction.txt" in environment.commands[-1]
    assert environment.uploads[ROOT + "/instruction.txt"] == b"//review the visible project"
    assert {Path(p).name for p in environment.downloads} == {"moss.jsonl", "stderr.log", "exit-code"}
    assert all("visible project" not in cmd for cmd in environment.commands)


async def test_native_job_builds_the_declared_trial_plan_without_starting_environments(tmp_path, monkeypatch):
    from harbor.job import Job
    from harbor.models.trial.config import TaskConfig
    from scripts import run_harbor_evals

    prepared = []
    original_prepare = run_harbor_evals.prepare

    def prepare(args):
        data, manifest = original_prepare(args)
        prepared.append(data)
        return data, manifest

    monkeypatch.setattr(run_harbor_evals, "prepare", prepare)

    # Use native Job.create and trial identities while replacing only external task downloads.
    configs = []

    async def tasks(config):
        configs.append(config)
        return [TaskConfig(path=tmp_path / "cancel-async-tasks")]

    async def metrics(config, tasks):
        return {"adhoc": []}, []

    async def cache(tasks):
        return {}

    monkeypatch.setattr(Job, "_preflight_agents", staticmethod(lambda *args: None))
    monkeypatch.setattr(Job, "_resolve_task_configs", staticmethod(tasks))
    monkeypatch.setattr(Job, "_resolve_metrics", staticmethod(metrics))
    monkeypatch.setattr(Job, "_cache_tasks", staticmethod(cache))
    run_harbor_evals.main(
        ["run", "--series", "offline", "--run-id", "preview", "--budget-usd", "1", "--estimate-usd", ".01"]
    )
    config = JobConfig.model_validate(prepared[0])
    config.job_name, config.jobs_dir, config.n_attempts = "native-plan", tmp_path, 2
    job = await Job.create(config)
    try:
        assert len(job) == 4 and len(configs) == 1
        assert [c.agent for c in job._trial_configs] == list(config.agents) * 2
        assert len({c.trial_name for c in job._trial_configs}) == 4
    finally:
        job._close_logger_handlers()


@pytest.mark.parametrize(
    "output,error",
    [('{"type":"result","subtype":"error"}', ApiError), ("incomplete stream", NonZeroAgentExitCodeError)],
)
async def test_model_errors_and_incomplete_cli_output_are_not_success(output, error, tmp_path, monkeypatch):
    item = agent(tmp_path, monkeypatch)
    item.logs_dir.mkdir()
    context = AgentContext()
    with pytest.raises(error):
        await item.run("Fix it", Environment(output), context)
    assert context.cost_usd is None


async def test_install_retries_transient_package_exit_and_timeout_still_downloads_logs(tmp_path, monkeypatch):
    import asyncio as aio

    async def no_sleep(_):
        pass

    monkeypatch.setattr(aio, "sleep", no_sleep)
    item = agent(tmp_path, monkeypatch)
    env = Environment('{"type":"tool_result","is_error":true}\n')
    attempts = 0

    async def flaky_deps(environment, deps):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise NonZeroAgentExitCodeError("apt 502")

    monkeypatch.setattr(item, "ensure_system_dependencies", flaky_deps)
    await item.install(env)
    assert attempts == 2

    async def timed_out_exec(environment, command, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(item, "exec_as_agent", timed_out_exec)
    item.logs_dir.mkdir(exist_ok=True)
    context = AgentContext()
    with pytest.raises(TimeoutError):
        await item.run("Fix it", env, context)
    assert context.metadata["mosscode"]["diagnostics"]["tool_errors"] == 1
