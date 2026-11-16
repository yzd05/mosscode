"""Plan and guard native Harbor jobs; Harbor owns task dispatch, containers, judging and resume."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.harbor.results import (  # noqa: E402
    HARBOR_VERSION,
    collect,
    digest,
    normalize_trial,
    read_json,
    text_sha256,
    write_json,
)

DEFAULT_OUTPUT = ROOT / ".test-artifacts/evals"


def agent_label(agent):
    label = agent.get("kwargs", {}).get("label") or agent.get("name")
    if label:
        return label
    if agent.get("import_path") == "evals.harbor.terminus_agent:Terminus2Agent":
        return "terminus-2"
    raise ValueError("评估 Agent 缺少可记录的标签")


def is_terminus(agent):
    return agent_label(agent) == "terminus-2"


def verify_baseline(path, data, manifest, args):
    lock = read_json(path)
    expected_agents = [item["label"] for item in lock["agents"]]
    actual_agents = [item["label"] for item in manifest["agents"]]
    actual_tasks = [item["task"] for item in manifest["plan"] if item["agent"] == actual_agents[0]]
    if (
        args.scope != lock["scope"]
        or args.task
        or args.timeout is not None
        or args.repeat != lock["repeat"]
        or args.concurrency != lock["concurrency"]
        or actual_tasks != lock["tasks"]
        or actual_agents != expected_agents
    ):
        raise ValueError("运行计划与冻结基线不一致")
    snapshot = lock.get("pricing", {}).get("snapshot")
    if args.prices and snapshot and manifest["prices"] != read_json(ROOT / snapshot):
        raise ValueError("价格快照与冻结基线不一致")
    expected_model = lock["model"]
    actual_model = manifest["conditions"]["model"]
    if any(actual_model.get(key) != value for key, value in expected_model.items()):
        raise ValueError("模型条件与冻结基线不一致")
    if data.get("agent_setup_timeout_multiplier") != lock["agent_setup_timeout_multiplier"]:
        raise ValueError("Agent 安装时限与冻结基线不一致")
    if data.get("retry", {}).get("max_retries") != lock["automatic_retries"]:
        raise ValueError("重试策略与冻结基线不一致")
    for expected, agent in zip(lock["agents"], data["agents"]):
        if "revision" not in expected:
            continue
        wheel = Path(agent["kwargs"]["wheel"])
        build = read_json(wheel.with_suffix(".json"))
        if build.get("revision") != expected["revision"] or build.get("wheel_sha256") != expected["wheel_sha256"]:
            raise ValueError("MossCode wheel 与冻结基线不一致")
    artifacts = lock["artifacts"]
    current = {
        "dataset_lock_sha256": text_sha256(ROOT / "evals/harbor/dataset.lock.json"),
        "job_config_sha256": text_sha256(args.config),
        "runner": digest((ROOT / "scripts/run_harbor_evals.py").read_text(encoding="utf-8")),
        "mosscode_adapter": manifest["conditions"]["adapters"]["mosscode"],
        "terminus_setup_adapter": manifest["conditions"]["adapters"]["terminus_setup"],
    }
    if current != artifacts:
        raise ValueError("评估适配器或冻结配置已经变化")
    manifest["baseline_lock"] = {
        "id": lock["id"],
        "sha256": text_sha256(path),
    }


def reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"api_key", "access_token", "token", "password", "secret", "authorization", "headers"}:
                raise ValueError(f"评估配置禁止保存凭据：{key}；请使用环境变量名称")
            if key == "model_api_key_name" and item is not None and item != "":
                if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
                    raise ValueError("model_api_key_name 仅允许环境变量名称")
            if key == "env" and isinstance(item, dict):
                if any(not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", str(v)) for v in item.values()):
                    raise ValueError("Agent env 仅允许环境变量占位符")
            if key in {"base_url", "api_base"} and item:
                from urllib.parse import urlsplit

                url = urlsplit(item)
                if url.username or url.password or url.query or url.fragment:
                    raise ValueError("评估端点不能包含认证信息、查询或片段；请用环境变量提供密钥")
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)


def build_wheel(ref, output):
    git = ["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT)]
    revision = subprocess.check_output(
        [*git, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], text=True
    ).strip()
    destination = output.resolve() / revision
    destination.mkdir(parents=True, exist_ok=True)
    existing = list(destination.glob("mosscode-*.whl"))
    if existing:
        (wheel,) = existing
        record = read_json(wheel.with_suffix(".json"))
        if record["revision"] != revision or record["wheel_sha256"] != hashlib.sha256(wheel.read_bytes()).hexdigest():
            raise ValueError("已有构建产物不完整或摘要不符；请使用新输出目录，不覆盖旧产物")
        print(wheel)
        return
    epoch = subprocess.check_output([*git, "show", "-s", "--format=%ct", revision], text=True).strip()
    archive = subprocess.check_output([*git, "archive", "--format=tar", revision])
    with tempfile.TemporaryDirectory(dir=destination, prefix="source-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(temporary, filter="data")
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(destination), temporary],
            env={**os.environ, "SOURCE_DATE_EPOCH": epoch},
            check=True,
        )
    (wheel,) = destination.glob("mosscode-*.whl")
    write_json(
        wheel.with_suffix(".json"),
        {"revision": revision, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "wheel": wheel.name},
    )
    print(wheel)


def prepare(args):
    data = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    reject_credentials(data)
    lock = read_json(ROOT / "evals/harbor/dataset.lock.json")
    names = lock["tasks"] if args.scope == "full" else lock["subsets"][args.scope]
    selected_tasks = args.task or names
    if len(set(selected_tasks)) != len(selected_tasks) or set(selected_tasks) - set(names):
        raise ValueError("--task 必须来自当前 scope 的冻结清单，且不能重复")
    names = selected_tasks
    agents = data["agents"]
    for item in args.wheel:
        if "=" not in item:
            raise ValueError("--wheel 格式必须为 LABEL=PATH")
        label, path = item.split("=", 1)
        if not label or not path:
            raise ValueError("--wheel 格式必须为 LABEL=PATH")
        agent = next((a for a in agents if a.get("kwargs", {}).get("label") == label), None)
        if agent is None:
            import copy

            agent = copy.deepcopy(next(a for a in agents if a.get("import_path")))
            agent["kwargs"]["label"] = label
            agents.append(agent)
        agent["kwargs"]["wheel"] = str(Path(path).resolve())
    if args.agent:
        available = {agent_label(a) for a in agents}
        unknown = set(args.agent) - available
        if unknown:
            raise ValueError(f"未知的 Agent：{', '.join(sorted(unknown))}")
        agents = [a for a in agents if agent_label(a) in args.agent]
    if not agents:
        raise ValueError("没有选中的 Agent")
    data.update(
        {
            "agents": agents,
            "job_name": args.run_id,
            "jobs_dir": str((args.output / args.series).resolve()),
            "install_only": args.install_only,
            "n_attempts": args.repeat,
            "n_concurrent_trials": args.concurrency,
            "datasets": [{"repo": lock["repo"], "ref": lock["ref"], "path": lock["path"], "task_names": names}],
        }
    )
    if args.timeout is not None:
        for agent in agents:
            agent["override_timeout_sec"] = args.timeout
    if data.get("retry", {}).get("max_retries", 0):
        raise ValueError("自动重试会覆盖失败轨迹；请设 max_retries: 0，额外尝试使用新 run-id")
    model_settings = []
    for agent in agents:
        options = agent.get("kwargs", {})
        external = is_terminus(agent)
        if external:
            if (
                options.get("temperature") is not None
                or options.get("reasoning_effort") is not None
                or options.get("max_thinking_tokens") is not None
                or options.get("llm_kwargs")
                or set(options.get("llm_call_kwargs", {})) - {"max_tokens"}
                or options.get("api_base") not in (None, agent.get("base_url"))
            ):
                raise ValueError("此共同配置使用服务默认采样/思考设置，不能只给 Terminus 2 单侧覆盖")
            output = options.get("llm_call_kwargs", {}).get("max_tokens")
            window = options.get("model_info", {}).get("max_input_tokens")
        else:
            output, window = options.get("max_output_tokens"), options.get("context_window")
            if any(is_terminus(a) for a in agents) and any(options.get("features", {}).get("run_budget", {}).values()):
                raise ValueError("外部对照不能仅限制 MossCode 的累计请求/Token 预算")
        model_settings.append(
            {
                "requested": agent.get("model_name"),
                "credential_env": agent.get("model_api_key_name"),
                "credential_overlays": agent.get("env", {}),
                "account_tier": args.account_tier,
                "base_url": agent.get("base_url"),
                "protocol": agent.get("model_api_format"),
                "max_output_tokens": output,
                "context_window": window,
                "sampling": "service_default",
                "thinking": "service_default",
                "backend_version": args.backend_version,
            }
        )
    if any(m != model_settings[0] for m in model_settings[1:]):
        raise ValueError("Agent 的模型端点、协议、输出或上下文设置不一致")
    if not model_settings[0]["max_output_tokens"] or not model_settings[0]["context_window"]:
        raise ValueError("需要为双方显式指定输出和上下文上限")
    prices = read_json(args.prices) if args.prices else None
    if prices:
        import math

        if (
            any(
                isinstance(prices.get(k), bool)
                or not isinstance(prices.get(k), (int, float))
                or not math.isfinite(prices[k])
                or prices[k] < 0
                for k in ("input", "cached_input", "output")
            )
            or not prices.get("source")
            or not prices.get("date")
        ):
            raise ValueError("价格快照需记录来源、日期及每百万 Token 的非负 USD 价格")
        for agent in agents:
            if "wheel" in agent.get("kwargs", {}):
                agent.setdefault("kwargs", {})["prices"] = prices
    conditions = {
        "dataset": {
            "repo": lock["repo"],
            "ref": lock["ref"],
            "version": lock["version"],
            "tasks": {name: lock["metadata"][name] for name in names},
        },
        "model": model_settings[0],
        "environment": data.get("environment", {}),
        "timeout_multiplier": data.get("timeout_multiplier", 1),
        "timeout": args.timeout,
        "concurrency": args.concurrency,
        "harbor": HARBOR_VERSION,
        "adapters": {
            "mosscode": digest((ROOT / "evals/harbor/mosscode_agent.py").read_text(encoding="utf-8")),
            "terminus_setup": digest((ROOT / "evals/harbor/terminus_agent.py").read_text(encoding="utf-8")),
        },
        "host": {"os": platform.system(), "machine": platform.machine(), "cpus": os.cpu_count()},
    }
    manifest = {
        "schema_version": 1,
        "series": args.series,
        "run_id": args.run_id,
        "scope": args.scope + ("/selection" if args.task else ""),
        "conditions": conditions,
        "prices": prices,
        "selection": lock["selection"],
        "plan": [],
        "agents": [],
        "mode": "install-only" if args.install_only else "evaluation",
        "budget": None
        if args.install_only
        else {"limit_usd": args.budget_usd, "estimated_attempt_usd": args.estimate_usd},
    }
    for agent in agents:
        label = agent_label(agent)
        manifest["agents"].append({"label": label, "config_hash": digest(agent)})
        manifest["plan"].extend(
            {"task": name, "agent": label, "repeat": repeat} for name in names for repeat in range(1, args.repeat + 1)
        )
    if args.baseline_lock:
        verify_baseline(args.baseline_lock, data, manifest, args)
    return data, manifest


class BatchBudget:
    """Reserve one declared estimate per in-flight trial; unknown actual usage stops new dispatch."""

    def __init__(self, limit, estimate, rows=()):
        self.limit, self.estimate = limit, estimate
        self.rows = {r["id"]: r for r in rows}
        self.active = set()
        self.reason = None

    def may_start(self):
        costs = [r["cost_usd"] for r in self.rows.values()]
        import math

        if any(c is None or not math.isfinite(c) or c < 0 for c in costs):
            self.reason = "存在未知费用，需先核对原始用量"
        elif sum(costs) + (len(self.active) + 1) * self.estimate > self.limit:
            self.reason = "剩余预算不足下一次预计开销"
        return self.reason is None


def redact_logs(directory, secrets):
    """Remove selected runtime credentials if a tool or server echoed them into local artifacts."""
    directory = Path(directory).resolve()
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            continue
        if not path.is_file() or path.suffix not in {
            ".cast",
            ".json",
            ".jsonl",
            ".log",
            ".md",
            ".pane",
            ".txt",
            ".yaml",
            ".yml",
        }:
            continue
        data = path.read_bytes()
        redacted = data
        for secret in secrets:
            redacted = redacted.replace(secret.encode(), b"[REDACTED]")
        if redacted != data:
            path.write_bytes(redacted)


async def execute(data, manifest, args):
    if importlib.metadata.version("harbor") != HARBOR_VERSION:
        raise ValueError(f"请在独立环境安装 harbor=={HARBOR_VERSION}")
    from harbor.job import Job
    from harbor.models.job.config import JobConfig

    native = JobConfig.model_validate(data)
    job_dir = Path(native.jobs_dir) / native.job_name
    manifest_path = job_dir / "manifest.json"
    resolved = native.model_dump(mode="json")
    config_hash = digest(resolved)
    manifest["config_hash"] = config_hash
    manifest["host_packages"] = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    manifest["conditions"]["harness_packages"] = digest(manifest["host_packages"])
    for spec, agent in zip(manifest["agents"], native.agents):
        spec["config_hash"] = digest(agent.model_dump(mode="json"))
        if agent.import_path and "wheel" in agent.kwargs:
            wheel = Path(agent.kwargs["wheel"])
            build = read_json(wheel.with_suffix(".json"))
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != build["wheel_sha256"]:
                raise ValueError("wheel 摘要与构建记录不符")
            spec.update(build)
    if manifest_path.exists():
        old = read_json(manifest_path)
        manifest["trial_plan"] = old.get("trial_plan", {})
        if not args.resume or old["config_hash"] != config_hash or old["conditions"] != manifest["conditions"]:
            raise ValueError("运行已存在或恢复配置变化；请明确 --resume 或换一个 run-id")
        if old.get("prices") != manifest["prices"]:
            raise ValueError("恢复运行不能更换价格口径")
    elif args.resume:
        raise ValueError("恢复目标不存在")
    write_json(manifest_path, manifest)
    previous = collect(job_dir, manifest)
    rows = {row["id"]: row for row in previous["attempts"]}
    guard = None if native.install_only else BatchBudget(args.budget_usd, args.estimate_usd, previous["attempts"])
    if len([r for r in previous["attempts"] if r["finished"]]) == len(manifest["plan"]):
        write_json(job_dir / "normalized.json", previous)
        print(job_dir / "normalized.json")
        return
    if guard is not None and not guard.may_start():
        previous["dispatch_stop"] = guard.reason
        write_json(job_dir / "normalized.json", previous)
        raise ValueError(guard.reason)
    job = await Job.create(native)
    # Read the pinned Harbor trial identities, but leave dispatch and resume entirely to Harbor.
    counts, index = {}, {}
    for trial in job._trial_configs:
        spec = next(a for a in manifest["agents"] if a["config_hash"] == digest(trial.agent.model_dump(mode="json")))
        name = Path(trial.task.path).name
        key = name, spec["label"]
        counts[key] = counts.get(key, 0) + 1
        index[trial.trial_name] = {"task": name, "agent": spec["label"], "repeat": counts[key]}
    manifest["trial_plan"] = {**manifest.get("trial_plan", {}), **index}
    write_json(manifest_path, manifest)
    job_task = None
    credential_names = set()
    for agent in native.agents:
        name = agent.model_api_key_name or "DEEPSEEK_API_KEY"
        credential_names.add(name)
        for value in (agent.env or {}).values():
            if match := re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(value)):
                credential_names.add(match.group(1))
    secrets = [os.environ[name] for name in credential_names if os.environ.get(name)]

    async def start(event):
        if guard is None:
            return
        if not guard.may_start():
            job_task.cancel()
            raise asyncio.CancelledError
        guard.active.add(str(event.trial_id))

    async def end(event):
        redact_logs(job_dir / event.trial_name, secrets)
        row = normalize_trial(
            event.result.model_dump(mode="json"), job_dir / event.trial_name / "result.json", manifest
        )
        rows[row["id"]] = row
        if guard is not None:
            guard.rows[row["id"]] = row
            guard.active.discard(row["id"])
        write_json(
            job_dir / "normalized.json",
            {"schema_version": 1, "manifest": manifest, "attempts": list(rows.values())},
        )

    job.on_trial_started(start).on_trial_ended(end)
    try:
        job_task = asyncio.create_task(job.run())
        await job_task
    except asyncio.CancelledError:
        if guard is None or guard.reason is None:
            raise
    finally:
        redact_logs(job_dir, secrets)
        document = collect(job_dir, manifest)
        document["dispatch_stop"] = guard.reason if guard is not None else None
        write_json(job_dir / "normalized.json", document)
    if guard is not None and guard.reason:
        print(guard.reason + "；结果已保存，不能声称全量完成。")
    print(job_dir / "normalized.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description="准备和运行 Harbor 评估；默认只展示计划，不请求模型")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="从指定 Git 提交构建 wheel，不包含工作区草稿")
    build.add_argument("ref")
    build.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "wheels")
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, default=ROOT / "evals/harbor/job.yaml")
    run.add_argument("--series", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--scope", choices=("smoke", "dev", "full"), default="smoke")
    run.add_argument("--task", action="append", help="从当前 scope 的冻结清单显式选题；报告标为选题抽检")
    run.add_argument("--agent", action="append")
    run.add_argument("--wheel", action="append", default=[], metavar="LABEL=PATH")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--timeout", type=float)
    run.add_argument("--budget-usd", type=float)
    run.add_argument("--estimate-usd", type=float, help="按试跑估算的单次开销；首次试跑可保守预估")
    run.add_argument("--prices", type=Path)
    run.add_argument("--account-tier", help="核实的账号/计费层级；不保存账号凭据")
    run.add_argument("--backend-version", help="已核实的服务后端版本；省略时报告滚动别名限制")
    run.add_argument("--baseline-lock", type=Path, help="核对任务、模型、Agent wheel 与适配器的冻结基线")
    run.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    run.add_argument("--execute", action="store_true", help="显式授权本次付费运行")
    run.add_argument("--install-only", action="store_true", help="只验证容器与 Agent 安装，不请求模型或运行判定")
    run.add_argument("--resume", action="store_true")
    normal = commands.add_parser("normalize", help="从原生结果重建归一化 JSON，不运行模型")
    normal.add_argument("job_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build_wheel(args.ref, args.output)
        elif args.command == "normalize":
            write_json(
                args.job_dir / "normalized.json", collect(args.job_dir, read_json(args.job_dir / "manifest.json"))
            )
        else:
            import math

            if (
                args.repeat < 1
                or args.concurrency < 1
                or (
                    not args.install_only
                    and (
                        args.budget_usd is None
                        or args.estimate_usd is None
                        or not math.isfinite(args.budget_usd)
                        or not math.isfinite(args.estimate_usd)
                        or args.budget_usd <= 0
                        or args.estimate_usd <= 0
                    )
                )
            ):
                raise ValueError("次数和并发必须大于零；正式评估还需要正数预算与单次估算")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.series
            ):
                raise ValueError("series 和 run-id 仅支持字母、数字、下划线、点和连字符")
            data, manifest = prepare(args)
            print(
                json.dumps(
                    {
                        "agents": len(manifest["agents"]),
                        "tasks": len({p["task"] for p in manifest["plan"]}),
                        "repeat": args.repeat,
                        "planned_attempts": len(manifest["plan"]),
                        "mode": manifest["mode"],
                        "estimated_usd": 0 if args.install_only else len(manifest["plan"]) * args.estimate_usd,
                        "budget_usd": args.budget_usd,
                        "retries": 0,
                    },
                    ensure_ascii=False,
                )
            )
            if args.execute:
                asyncio.run(execute(data, manifest, args))
    except (ValueError, KeyError, FileNotFoundError, importlib.metadata.PackageNotFoundError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        parser.exit(130, "运行已中断；已结束的尝试保留在原目录。\n")


if __name__ == "__main__":
    main()
