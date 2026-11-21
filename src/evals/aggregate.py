"""
Aggregation: one run's trial.json + grades.json (+ transcripts for tool health)
into results.json, the source of truth for every report.

Counting rules
- A trial's verdict is pass / fail / undetermined (a required grader errored) /
  excluded (infra_error after retries) / ungraded (no grades.json yet).
- Pass rates, pass@k and pass^k use the trials with a pass or fail verdict
  ("decided"); k is that number. pass@k: any decided trial passed; pass^k: all did.
- Failure reasons are counted once per trial: a trial failing two graders for
  required_failed counts once for each (reason, grader) pair, never twice for one.
- Every aggregate carries `refs` ([{task, trial}]) so a report can link each
  number to the trials behind it.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

FACT_VERDICTS = ("present", "absent", "contradicted", "undetermined")
TOP_REASONS = 3
REASON_ORDER = {"not_completed": 0, "required_failed": 1, "contradiction": 2, "min_score": 3, "below_threshold": 4}


def _read(path: Path) -> Optional[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def read_transcript(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _mean(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _ref(t: dict) -> dict:
    return {"task": t["task"], "trial": t["trial"]}


def load_trials(run_dir: Path) -> List[dict]:
    """Every stored trial of a run, with its grades and the transcript-derived numbers."""
    trials = []
    for trial_path in sorted(run_dir.glob("*/t*/trial.json")):
        d = trial_path.parent
        trial = json.loads(trial_path.read_text(encoding="utf-8"))
        grades = _read(d / "grades.json")
        events = read_transcript(d / "transcript.jsonl")
        tools: Dict[str, Dict[str, Any]] = {}
        for e in events:
            if e["type"] == "tool_call":
                t = tools.setdefault(e.get("tool") or "?", {"calls": 0, "errors": 0, "error_events": []})
                t["calls"] += 1
                if e.get("error"):
                    t["errors"] += 1
                    t["error_events"].append(e["index"])
        if trial.get("excluded"):
            verdict = "excluded"
        elif grades is None:
            verdict = "ungraded"
        else:
            verdict = {True: "pass", False: "fail", None: "undetermined"}[grades["verdict"]["passed"]]
        trials.append({
            "task": trial["task"], "trial": trial["trial"], "dir": str(d.relative_to(run_dir)),
            "status": trial["status"], "reason": trial.get("reason"), "excluded": bool(trial.get("excluded")),
            "verdict": verdict, "trial_data": trial, "grades": grades, "events": events, "tools": tools,
        })
    trials.sort(key=lambda t: (t["task"], t["trial"]))
    return trials


def _failure_keys(t: dict) -> List[tuple]:
    if not t["grades"]:
        return []
    keys = {(r["reason"], r["grader_id"]) for r in t["grades"]["verdict"]["failure_reasons"]}
    return sorted(keys, key=lambda k: (k[0], k[1] or ""))


def _ranked_reasons(trials: List[dict], limit: Optional[int]) -> List[dict]:
    counts: Dict[tuple, List[dict]] = defaultdict(list)
    for t in trials:
        for key in _failure_keys(t):          # once per trial
            counts[key].append(_ref(t))
    # most trials first; on a tie the more direct cause first (below_threshold mostly follows from the others)
    ranked = sorted(counts.items(), key=lambda kv: (-len(kv[1]), REASON_ORDER.get(kv[0][0], 99), kv[0][0], kv[0][1] or ""))
    out = [{"reason": r, "grader_id": g, "trials": len(refs), "refs": refs} for (r, g), refs in ranked]
    return out[:limit] if limit else out


def _graders(t: dict) -> List[dict]:
    return (t["grades"] or {}).get("graders") or []


def _dimension_rows(t: dict) -> List[dict]:
    rows = []
    for g in _graders(t):
        if g["type"] != "llm_rubric" or g["status"] != "ok":
            continue
        for name, d in (g["details"].get("dimensions") or {}).items():
            rows.append({"dimension": name, "task": t["task"], "trial": t["trial"], "score": d.get("score"),
                         "normalised": d.get("normalised"), "reason": d.get("reason", ""),
                         "tool_errors": sum(x["errors"] for x in t["tools"].values()), "grader_id": g["id"]})
    return rows


def _fact_rows(t: dict) -> List[dict]:
    rows = []
    for g in _graders(t):
        if g["type"] == "key_facts" and g["status"] == "ok":
            for f in g["details"].get("facts") or []:
                rows.append({"task": t["task"], "trial": t["trial"], "fact": f["fact"], "verdict": f["verdict"],
                             "reason": f.get("reason", ""), "evidence": f.get("evidence", [])})
    return rows


def trial_summary(t: dict) -> dict:
    counts = t["trial_data"].get("counts") or {}
    user = t["trial_data"].get("user") or {}
    v = (t["grades"] or {}).get("verdict") or {}
    return {
        "task": t["task"], "trial": t["trial"], "dir": t["dir"], "status": t["status"], "reason": t["reason"],
        "verdict": t["verdict"], "score": v.get("score"), "excluded": t["excluded"],
        "session_id": t["trial_data"].get("session_id"), "duration_s": t["trial_data"].get("duration_s"),
        "llm_calls": counts.get("llm_calls"), "tokens_in": counts.get("tokens_in"),
        "tokens_out": counts.get("tokens_out"), "clarifications": counts.get("clarifications"),
        "tool_calls": counts.get("tool_calls"), "tool_errors": counts.get("tool_errors"),
        "default_used": bool(user.get("default_used")), "user_mode": user.get("mode"),
        "sim_calls": user.get("sim_calls", 0), "teardown_error": t["trial_data"].get("teardown_error"),
        "failure_reasons": v.get("failure_reasons", []), "grader_errors": v.get("grader_errors", []),
        "judge": (t["grades"] or {}).get("judge"),
        "graders": [{k: g.get(k) for k in ("id", "type", "passed", "score", "status", "reason", "required",
                                            "weight", "min_score", "evidence", "llm")} for g in _graders(t)],
    }


def task_definitions(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Each task's definition as it was run: from the manifest, or, for a run made
    before the manifest recorded it, from the suite file when its hash still
    matches the run's (a changed suite would describe something else).
    """
    if manifest.get("tasks"):
        return {tid: {**d, "recorded": "manifest"} for tid, d in manifest["tasks"].items()}
    suite_info = manifest.get("suite") or {}
    if not suite_info.get("path"):
        return {}
    try:
        from src.evals.runner import describe_task
        from src.evals.spec import load_suite
        suite = load_suite(suite_info["path"])
    except Exception:
        return {}
    if suite.hash != suite_info.get("hash"):
        return {}
    return {t.id: {**describe_task(t, suite), "recorded": "suite"} for t in suite.tasks}


def build_results(run_dir: Path) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    manifest = _read(run_dir / "manifest.json") or {}
    trials = load_trials(run_dir)
    decided = [t for t in trials if t["verdict"] in ("pass", "fail")]
    by_task: Dict[str, List[dict]] = defaultdict(list)
    for t in trials:
        by_task[t["task"]].append(t)
    task_order = list((manifest.get("trials") or {}).keys()) or sorted(by_task)
    task_order += [k for k in sorted(by_task) if k not in task_order]

    definitions = task_definitions(manifest)
    tasks = []
    for task_id in task_order:
        ts = by_task.get(task_id, [])
        dec = [t for t in ts if t["verdict"] in ("pass", "fail")]
        passed = [t for t in dec if t["verdict"] == "pass"]
        live = [t for t in ts if not t["excluded"]]
        facts = Counter(r["verdict"] for t in ts for r in _fact_rows(t))
        dims: Dict[str, List[float]] = defaultdict(list)
        for t in ts:
            for row in _dimension_rows(t):
                if row["normalised"] is not None:
                    dims[row["dimension"]].append(row["normalised"])
        counts = lambda key: [t["trial_data"].get("counts", {}).get(key) for t in live]
        tasks.append({
            "id": task_id,
            "trials": len(ts), "decided": len(dec), "passed": len(passed), "k": len(dec),
            "pass_rate": round(len(passed) / len(dec), 4) if dec else None,
            "pass_at_k": bool(passed) if dec else None,
            "pass_pow_k": (len(passed) == len(dec)) if dec else None,
            "mean_score": _mean([(t["grades"] or {}).get("verdict", {}).get("score") for t in ts if not t["excluded"]]),
            "failure_reasons": _ranked_reasons(ts, TOP_REASONS),
            "mean_llm_calls": _mean(counts("llm_calls")),
            "mean_tokens_in": _mean(counts("tokens_in")),
            "mean_tokens_out": _mean(counts("tokens_out")),
            "mean_latency_s": _mean([t["trial_data"].get("duration_s") for t in live]),
            "mean_clarifications": _mean(counts("clarifications")),
            "default_reply_trials": sum(1 for t in live if (t["trial_data"].get("user") or {}).get("default_used")),
            "user_mode": next(((t["trial_data"].get("user") or {}).get("mode") for t in ts
                               if (t["trial_data"].get("user") or {}).get("mode")), None),
            "statuses": dict(Counter(t["status"] for t in ts)),
            "verdicts": dict(Counter(t["verdict"] for t in ts)),
            "key_facts": {v: facts.get(v, 0) for v in FACT_VERDICTS},
            "dimensions": {name: _mean(vals) for name, vals in sorted(dims.items())},
            "refs": [_ref(t) for t in ts],
            "trial_rows": [trial_summary(t) for t in ts],
            "definition": definitions.get(task_id),
        })

    # dimensions across the suite
    dim_rows = [row for t in trials for row in _dimension_rows(t)]
    dimensions = {}
    for name in sorted({r["dimension"] for r in dim_rows}):
        rows = [r for r in dim_rows if r["dimension"] == name]
        scored = [r for r in rows if r["normalised"] is not None]
        dimensions[name] = {
            "mean": _mean([r["normalised"] for r in scored]),
            "trials_scored": len(scored), "undetermined": len(rows) - len(scored),
            "tasks_scored": len({r["task"] for r in scored}),
            "rows": sorted(rows, key=lambda r: (r["normalised"] if r["normalised"] is not None else -1,
                                                r["task"], r["trial"])),
        }

    fact_rows = [row for t in trials for row in _fact_rows(t)]
    key_facts = {v: {"count": sum(1 for r in fact_rows if r["verdict"] == v),
                     "refs": sorted({(r["task"], r["trial"]) for r in fact_rows if r["verdict"] == v})}
                 for v in FACT_VERDICTS}
    for v in key_facts.values():
        v["refs"] = [{"task": a, "trial": b} for a, b in v["refs"]]

    tool_health: Dict[str, Dict[str, Any]] = {}
    for t in trials:
        if t["excluded"]:
            continue
        for name, x in t["tools"].items():
            h = tool_health.setdefault(name, {"calls": 0, "errors": 0, "refs": [], "error_refs": []})
            h["calls"] += x["calls"]
            h["errors"] += x["errors"]
            h["refs"].append(_ref(t))
            if x["errors"]:
                h["error_refs"].append({**_ref(t), "events": x["error_events"]})
    for h in tool_health.values():
        h["error_rate"] = round(h["errors"] / h["calls"], 4) if h["calls"] else 0.0

    def refs_where(pred):
        return [_ref(t) for t in trials if pred(t)]

    statuses = Counter(t["status"] for t in trials)
    grading = manifest.get("grading") or {}
    suite = {
        "name": (manifest.get("suite") or {}).get("name"),
        "trials": len(trials), "decided": len(decided),
        "passed": sum(1 for t in decided if t["verdict"] == "pass"),
        "pass_rate": round(sum(1 for t in decided if t["verdict"] == "pass") / len(decided), 4) if decided else None,
        "tasks": len(tasks),
        "tasks_pass_any": sum(1 for x in tasks if x["pass_at_k"]),
        "tasks_pass_all": sum(1 for x in tasks if x["pass_pow_k"]),
        "key_facts": key_facts,
        "contradicted_facts": [r for r in fact_rows if r["verdict"] == "contradicted"],
        "dimensions": dimensions,
        "failure_reasons": _ranked_reasons(trials, TOP_REASONS),
        "all_failure_reasons": _ranked_reasons(trials, None),
        "tool_health": dict(sorted(tool_health.items())),
        "counts": {
            "statuses": dict(statuses),
            "timeouts": statuses.get("timeout", 0),
            "limit_exceeded": statuses.get("limit_exceeded", 0),
            "agent_errors": statuses.get("agent_error", 0),
            "infra_errors": statuses.get("infra_error", 0),
            "excluded": sum(1 for t in trials if t["excluded"]),
            "grader_errors": sum(len((t["grades"] or {}).get("verdict", {}).get("grader_errors", [])) for t in trials),
            "undetermined": sum(1 for t in trials if t["verdict"] == "undetermined"),
            "ungraded": sum(1 for t in trials if t["verdict"] == "ungraded"),
        },
        "count_refs": {
            "timeouts": refs_where(lambda t: t["status"] == "timeout"),
            "limit_exceeded": refs_where(lambda t: t["status"] == "limit_exceeded"),
            "agent_errors": refs_where(lambda t: t["status"] == "agent_error"),
            "infra_errors": refs_where(lambda t: t["status"] == "infra_error"),
            "grader_errors": refs_where(lambda t: (t["grades"] or {}).get("verdict", {}).get("grader_errors")),
            "undetermined": refs_where(lambda t: t["verdict"] == "undetermined"),
            "passed": refs_where(lambda t: t["verdict"] == "pass"),
            "failed": refs_where(lambda t: t["verdict"] == "fail"),
        },
        "efficiency": {
            "mean_llm_calls": _mean([t["trial_data"].get("counts", {}).get("llm_calls") for t in trials if not t["excluded"]]),
            "mean_tokens_in": _mean([t["trial_data"].get("counts", {}).get("tokens_in") for t in trials if not t["excluded"]]),
            "mean_tokens_out": _mean([t["trial_data"].get("counts", {}).get("tokens_out") for t in trials if not t["excluded"]]),
            "mean_latency_s": _mean([t["trial_data"].get("duration_s") for t in trials if not t["excluded"]]),
        },
        "calls": {
            "agent": (manifest.get("run_totals") or {}).get("agent_llm_calls"),
            "user_sim": (manifest.get("run_totals") or {}).get("user_sim_calls"),
            "judge": grading.get("judge_calls"),
        },
    }
    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": manifest.get("run_id") or run_dir.name,
        "manifest": manifest,
        "suite": suite,
        "tasks": tasks,
    }


def write_results(run_dir: Path) -> Dict[str, Any]:
    results = build_results(run_dir)
    (Path(run_dir) / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return results
