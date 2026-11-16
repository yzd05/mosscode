"""Small, dependency-free result readers shared by the adapter and comparison CLI."""

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import median

HARBOR_VERSION = "0.26.0"
SCHEMA = 1
COUNTERS = ("requests", "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def text_sha256(path):
    """Hash normalized UTF-8 text so CRLF and LF checkouts agree."""
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def estimate_cost(usage, prices):
    """Prices are explicit USD per million tokens; an unknown cache split is not a free cache."""
    if not prices or any(usage.get(k) is None for k in ("input_tokens", "output_tokens")):
        return None
    cached = usage.get("cached_input_tokens")
    if cached is None and prices["input"] != prices["cached_input"]:
        return None
    cached = cached or 0
    if cached < 0 or cached > usage["input_tokens"]:
        return None
    return (
        (usage["input_tokens"] - cached) * prices["input"]
        + cached * prices["cached_input"]
        + usage["output_tokens"] * prices["output"]
    ) / 1_000_000


def parse_stream(text, prices=None):
    """Read each request once; the final totals include auxiliary calls and replace, rather than add to, them."""
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    accounting = result.get("accounting") or {}
    requests = [e.get("usage") for e in events if e.get("type") == "assistant"]
    usage = {key: None for key in COUNTERS}
    totals = result.get("usage") or {}
    if accounting:
        usage.update({key: totals.get(key) for key in COUNTERS})
        for key, missing in (
            ("input_tokens", "missing_input"),
            ("output_tokens", "missing_output"),
            ("cached_input_tokens", "missing_cached"),
            ("cache_write_tokens", "missing_cache_write"),
        ):
            if accounting.get(missing, 1):
                usage[key] = None
    elif result and totals.get("requests") == len(requests):
        # Old CLIs have no completeness flags. Their assistant events can prove the main-only case.
        usage["requests"] = len(requests)
        for key in COUNTERS[1:]:
            values = [item.get(key) if item else None for item in requests]
            usage[key] = sum(values) if all(v is not None for v in values) else None
    cost = estimate_cost(usage, prices)
    if prices is None and accounting.get("missing_cost") == 0:
        cost = 0.0 if usage["requests"] == 0 and result.get("total_cost_usd") is None else result.get("total_cost_usd")
    return {
        **usage,
        "side_requests": accounting.get("side_requests"),
        "side_input_tokens": accounting.get("side_input_tokens") if not accounting.get("missing_input", 1) else None,
        "stop": result.get("subtype", "missing_result"),
        "cost_usd": cost,
        "catalog_cost_subtotal_usd": result.get("total_cost_usd"),
        "cost_source": "price_snapshot" if prices else "model_catalog_estimate",
        "reported_model": None,  # The CLI's model field is the requested ID, not a backend response ID.
        "seconds": result["duration_ms"] / 1000 if result.get("duration_ms") is not None else None,
        "diagnostics": {
            "tool_errors": sum(bool(e.get("is_error")) for e in events if e.get("type") == "tool_result"),
            "compactions": result.get("compactions"),
            "collapses": sum(e.get("subtype") == "collapsed" for e in events if e.get("type") == "compact")
            if accounting or any(e.get("subtype") == "collapsed" for e in events)
            else None,
            "repeated_calls": int(result.get("subtype") == "repeated_calls")
            if accounting or result.get("subtype") == "repeated_calls"
            else None,
            "background_commands": None,
            "finish_check_failures": None,
        },
    }


def seconds_between(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() if start and end else None


def normalize_trial(data, path, manifest):
    config = data["config"]["agent"]
    agent = next(a for a in manifest["agents"] if a["config_hash"] == digest(config))
    planned = manifest.get("trial_plan", {}).get(data["trial_name"], {})
    context = data.get("agent_result") or {}
    info = (context.get("metadata") or {}).get("mosscode", {})
    exception = data.get("exception_info") or {}
    rewards = (data.get("verifier_result") or {}).get("rewards")
    valid = data.get("reward_valid", not exception and bool(rewards))
    error = exception.get("exception_type")
    before_execution = not data.get("step_results") and (
        not data.get("agent_execution") or not data["agent_execution"].get("started_at")
    )
    category = None
    if error:
        category = (
            "verifier"
            if "Verifier" in error
            else "service"
            if any(k in error for k in ("Api", "Model", "Authentication", "Connection"))
            else "environment"
            if before_execution or any(k in error for k in ("Environment", "Sandbox", "Build"))
            else "agent"
        )
    if before_execution and all(context.get(k) is None for k in ("cost_usd", "n_input_tokens", "n_output_tokens")):
        # Setup-only failures and cancellations before model execution have no model spend.
        context = {"cost_usd": 0.0, "n_input_tokens": 0, "n_output_tokens": 0, "n_cache_tokens": 0}
    stop = info.get("stop")
    if not stop and not error and (data.get("agent_execution") or {}).get("finished_at"):
        stop = "completed"
    if error and stop in (None, "missing_result"):
        if "Timeout" in error:
            stop = "timeout"
        elif "Cancel" in error:
            stop = "cancelled"
        elif stop == "missing_result" and "NonZeroAgentExitCode" in error:
            stop = "missing_result"
        else:
            stop = "error"
    usage = {
        "input_tokens": context.get("n_input_tokens"),
        "output_tokens": context.get("n_output_tokens"),
        "cached_input_tokens": context.get("n_cache_tokens"),
        "requests": info.get("requests"),
        "side_requests": info.get("side_requests"),
        "side_input_tokens": info.get("side_input_tokens"),
        "cache_write_tokens": info.get("cache_write_tokens"),
    }
    cost = estimate_cost(usage, manifest.get("prices")) if manifest.get("prices") else context.get("cost_usd")
    return {
        "id": str(data["id"]),
        "task": planned.get("task", data["task_name"]),
        "native_task_name": data["task_name"],
        "agent": agent["label"],
        "repeat": planned.get("repeat"),
        "task_checksum": data["task_checksum"],
        "reward": rewards,
        "reward_valid": valid,
        "passed": bool(valid and rewards and rewards.get("reward") == 1),
        "finished": bool(data.get("finished_at")) and stop != "cancelled",
        "stop": stop,
        "error": error,
        "error_category": category,
        **usage,
        "cost_usd": cost,
        "cost_source": "price_snapshot"
        if manifest.get("prices")
        else info.get("cost_source", "harbor_native_estimate"),
        "diagnostics": info.get("diagnostics"),
        "seconds": seconds_between(data.get("started_at"), data.get("finished_at")),
        "artifacts": str(Path(path).resolve()),
    }


def collect(job_dir, manifest):
    rows = []
    for path in sorted(Path(job_dir).glob("*/result.json")):
        data = read_json(path)
        if "task_name" in data:
            rows.append(normalize_trial(data, path, manifest))
    return {"schema_version": SCHEMA, "manifest": manifest, "attempts": rows}


def summary(document, label):
    plan = [p for p in document["manifest"]["plan"] if p["agent"] == label]
    rows = [r for r in document["attempts"] if r["agent"] == label]
    tasks = sorted({p["task"] for p in plan})
    finished = [r for r in rows if r["finished"]]
    successes = sum(r["passed"] for r in finished)
    # Missing planned attempts remain in the denominator; retries never silently expand it.
    rates = [sum(r["passed"] for r in finished if r["task"] == t) / sum(p["task"] == t for p in plan) for t in tasks]
    costs = [r["cost_usd"] for r in rows]
    durations = sorted(r["seconds"] for r in finished if r["seconds"] is not None)
    return {
        "agent": label,
        "errors": {
            kind: sum(r.get("error_category") == kind for r in rows)
            for kind in ("service", "environment", "agent", "verifier")
        },
        "planned": len(plan),
        "finished": len(finished),
        "passed": successes,
        "coverage": len(finished) / len(plan) if plan else 0,
        "pass_rate": sum(rates) / len(rates) if rates else None,
        "cost_usd": sum(costs) if costs and all(c is not None for c in costs) else None,
        "unknown_cost_attempts": sum(c is None for c in costs),
        "median_seconds": median(durations) if durations and len(durations) == len(finished) else None,
        "p90_seconds": durations[math.ceil(len(durations) * 0.9) - 1]
        if durations and len(durations) == len(finished)
        else None,
        "completed_but_failed": sum(r["stop"] == "completed" and not r["passed"] for r in rows),
        "cost_per_success": sum(costs) / successes if successes and all(c is not None for c in costs) else None,
    }


def compatible(left, right):
    for key in ("series", "mode", "conditions", "prices"):
        if left["manifest"].get(key) != right["manifest"].get(key):
            raise ValueError(f"实验条件不相容：{key}")

    def plan(document):
        groups = [
            sorted((p["task"], p["repeat"]) for p in document["manifest"]["plan"] if p["agent"] == agent["label"])
            for agent in document["manifest"]["agents"]
        ]
        if not groups or any(group != groups[0] for group in groups):
            raise ValueError("各 Agent 的任务与重复计划不一致")
        return groups[0]

    if plan(left) != plan(right):
        raise ValueError("实验任务或重复计划不相容")
    checksums = {}
    for row in left["attempts"] + right["attempts"]:
        previous = checksums.setdefault(row["task"], row["task_checksum"])
        if previous != row["task_checksum"]:
            raise ValueError(f"任务内容不相容：{row['task']}")


def _unique_documents(documents):
    counts = {}
    for doc in documents:
        rid = doc["manifest"]["run_id"]
        counts[rid] = counts.get(rid, 0) + 1
    if all(count == 1 for count in counts.values()):
        return documents
    used, seen, out = {rid for rid, count in counts.items() if count == 1}, {}, []
    for doc in documents:
        rid = doc["manifest"]["run_id"]
        if counts[rid] > 1:
            index = seen.get(rid, 0) + 1
            while f"{rid}#{index}" in used:
                index += 1
            seen[rid] = index
            rid = f"{rid}#{index}"
            doc = {**doc, "manifest": {**doc["manifest"], "run_id": rid}}
        used.add(rid)
        out.append(doc)
    return out


def markdown(documents):
    documents = _unique_documents(documents)
    for doc in documents:
        for agent in doc["manifest"]["agents"]:
            for task in {p["task"] for p in doc["manifest"]["plan"]}:
                actual = sum(r["task"] == task and r["agent"] == agent["label"] for r in doc["attempts"])
                planned = sum(p["task"] == task and p["agent"] == agent["label"] for p in doc["manifest"]["plan"])
                if actual > planned:
                    raise ValueError("额外尝试不能并入原计划分数；请分别报告重试开销")
    for document in documents[1:]:
        compatible(documents[0], document)
    lines = [
        "# 评估对照",
        "",
        "费用均为估计；缺失值为 —。部分覆盖不是全量成绩。",
        "运行范围："
        + ", ".join(f"{d['manifest']['run_id']}={d['manifest'].get('scope', 'controlled')}" for d in documents),
        "",
        "| 运行 / Agent | 覆盖 | 通过 / 计划 | 端到端单次成功率 | 已发生费用 | 耗时中位数 / P90 | 正常结束但未通过 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def show(value):
        return "—" if value is None else str(round(value, 3))

    summaries, by_key = [], {}
    for doc in documents:
        for agent in doc["manifest"]["agents"]:
            s = {"run_id": doc["manifest"]["run_id"], **summary(doc, agent["label"])}
            summaries.append(s)
            by_key[f"{doc['manifest']['run_id']}/{s['agent']}"] = s
            cost = f"${s['cost_usd']:.5f}" if s["cost_usd"] is not None else "—"
            rate = f"{s['pass_rate']:.1%}" if s["pass_rate"] is not None else "—"
            lines.append(
                f"| {doc['manifest']['run_id']} / {s['agent']} | {s['finished']}/{s['planned']}"
                f" | {s['passed']}/{s['planned']} | {rate} | {cost}"
                f" | {show(s['median_seconds'])} / {show(s['p90_seconds'])} | {s['completed_but_failed']} |"
            )
    if not documents[0]["manifest"]["conditions"]["model"].get("backend_version"):
        lines += ["", "后端实际版本未锁定：滚动模型别名的历史结果可能发生漂移。"]
    lines += [
        "",
        "| 运行 / Agent | 任务 | 通过次数 / 计划次数 | 停止原因 | 原始结果 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for doc in documents:
        for agent in doc["manifest"]["agents"]:
            for task in sorted({p["task"] for p in doc["manifest"]["plan"]}):
                rows = [r for r in doc["attempts"] if r["agent"] == agent["label"] and r["task"] == task]
                n = sum(p["task"] == task and p["agent"] == agent["label"] for p in doc["manifest"]["plan"])
                lines.append(
                    f"| {doc['manifest']['run_id']} / {agent['label']} | {task}"
                    f" | {sum(r['passed'] for r in rows)}/{n} | {', '.join(str(r['stop'] or '—') + (' / ' + r['error_category'] if r.get('error_category') else '') for r in rows) or '未执行'}"
                    f" | {', '.join(r['artifacts'] for r in rows)} |"
                )
    paired = paired_successes(documents)
    if paired:
        lines += ["", "| 双成功尝试对照 | 数量 | 候选/基线费用 | 候选/基线耗时 |", "| --- | --- | --- | --- |"]
        for item in paired:
            lines.append(
                f"| {item['baseline']} → {item['candidate']} | {item['matched']} | {show(item['cost_ratio'])} | {show(item['time_ratio'])} |"
            )
            if item["candidate"] in by_key:
                by_key[item["candidate"]]["paired_successes"] = item
    return "\n".join(lines) + "\n", summaries


def controlled_document(data, source):
    """Import new controlled-task checkpoints; old unlabelled cost reports are historical context only."""
    metadata = data["metadata"]
    labels = [p["name"] for p in metadata["providers"]]
    plan = [
        {"task": task, "agent": label, "repeat": repeat}
        for label in labels
        for task in metadata["tasks"]
        for repeat in range(1, metadata["repeat"] + 1)
    ]
    manifest = {
        "series": "controlled",
        "mode": "evaluation",
        "run_id": Path(source).stem,
        "conditions": {
            "dataset": metadata["tasks"],
            "model": {"providers": metadata["providers"], "backend_version": None},
            "execution": {k: v for k, v in metadata["options"].items() if k not in {"keep", "finish_commands"}},
        },
        "prices": None,
        "plan": plan,
        "agents": [
            {
                "label": label,
                "source": metadata["python_source_sha256"],
                "settings": {
                    **metadata["settings"],
                    "finish_commands": list(metadata["options"].get("finish_commands", ())),
                },
                "system_prompt": metadata["system_prompt"],
            }
            for label in labels
        ],
    }
    attempts = []
    for i, row in enumerate(data["rows"]):
        attempts.append(
            {
                **row,
                "id": f"{manifest['run_id']}-{i}",
                "agent": row["provider"],
                "repeat": row["run"],
                "task_checksum": metadata["tasks"][row["task"]],
                "reward": {"reward": int(row["passed"])},
                "reward_valid": True,
                "finished": row["stop"] != "cancelled",
                "error_category": ("service" if row["stop"] == "error" else "agent") if row.get("error") else None,
                "input_tokens": row.get("total_input_tokens") if not row.get("missing_input", 1) else None,
                "cached_input_tokens": row["cached_input_tokens"] + row.get("side_cached_input_tokens", 0)
                if not row.get("missing_cached", 1)
                else None,
                "output_tokens": row["output_tokens"] if not row.get("missing_output", 1) else None,
                "artifacts": str(Path(source).resolve()),
                "diagnostics": {"tool_errors": row["tool_errors"]},
            }
        )
    return {"schema_version": SCHEMA, "manifest": manifest, "attempts": attempts}


def paired_successes(documents):
    documents = _unique_documents(documents)

    def indexed(rows):
        return {
            (r["task"], r.get("repeat")): r
            for r in rows
            if r["passed"] and r["finished"] and r.get("repeat") is not None
        }

    def ratio(pairs, field):
        if not pairs or any(a.get(field) is None or b.get(field) is None for a, b in pairs):
            return None
        denominator = sum(a[field] for a, b in pairs)
        return sum(b[field] for a, b in pairs) / denominator if denominator else None

    def entry(base_doc, base_label, left, doc, label, rows):
        pairs = [
            (left[(r["task"], r.get("repeat"))], r)
            for r in rows
            if r["passed"] and r["finished"] and (r["task"], r.get("repeat")) in left
        ]
        return {
            "baseline": base_doc["manifest"]["run_id"] + "/" + base_label,
            "candidate": doc["manifest"]["run_id"] + "/" + label,
            "matched": len(pairs),
            "cost_ratio": ratio(pairs, "cost_usd") if doc["manifest"].get("prices") else None,
            "time_ratio": ratio(pairs, "seconds"),
        }

    if len(documents) == 1:
        doc = documents[0]
        groups = [
            (doc, agent["label"], [r for r in doc["attempts"] if r["agent"] == agent["label"]])
            for agent in doc["manifest"]["agents"]
        ]
        if len(groups) < 2:
            return []
        if any(g[1] == "candidate" for g in groups) and any(g[1] != "candidate" for g in groups):
            groups.sort(key=lambda g: g[1] == "candidate")
        base_doc, base_label, baseline = groups[0]
        left = indexed(baseline)
        return [entry(base_doc, base_label, left, d, label, rows) for d, label, rows in groups[1:]]

    if len(documents) < 2:
        return []
    base_doc = documents[0]
    base_groups = [
        (agent["label"], [r for r in base_doc["attempts"] if r["agent"] == agent["label"]])
        for agent in base_doc["manifest"]["agents"]
    ]
    if not base_groups:
        return []
    comparisons = []
    for doc in documents[1:]:
        for agent in doc["manifest"]["agents"]:
            label = agent["label"]
            rows = [r for r in doc["attempts"] if r["agent"] == label]
            match = next((g for g in base_groups if g[0] == label), None)
            if match is None and label != "terminus-2":
                match = next((g for g in base_groups if g[0] != "terminus-2"), None)
            base_label, baseline = match or base_groups[0]
            comparisons.append(entry(base_doc, base_label, indexed(baseline), doc, label, rows))
    return comparisons
