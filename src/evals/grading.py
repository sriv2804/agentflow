"""
Grading phase: reads a run's stored files and writes grades.json per trial. It
never re-runs the agent and never reads the live session dir, so it can be
re-run alone (`python -m src.evals grade <run_dir>`), e.g. after changing a
grader's config in the suite.

Per trial it reads transcript.jsonl, trial.json and observations.json, runs the
code graders first and then the LLM graders, and decides the verdict:

    a trial passes if its status is completed
      AND every required grader passed
      AND no min_score is breached
      AND no contradiction where fail_on_contradiction is set
      AND the weighted mean of the scored graders >= pass_threshold

Every failure reason is recorded, tagged with the grader id:
required_failed | min_score | contradiction | below_threshold | not_completed.
A required grader whose status is grader_error leaves the verdict undetermined
(passed: null) unless another reason already fails the trial. Excluded trials
(infra_error after retries) are not graded.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.evals.graders import GradeContext, GradeResult, build_grader, unique_ids
from src.evals.spec import Suite, Task


def read_transcript(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _passes_required(r: GradeResult, pass_threshold: float) -> bool:
    if r.passed is not None:
        return r.passed
    # a scored grader without a pass/fail of its own (no min_score): judged against the task threshold
    return r.score is not None and r.score >= pass_threshold


def decide_verdict(status: str, results: List[GradeResult], pass_threshold: float) -> Dict[str, Any]:
    reasons: List[Dict[str, Any]] = []
    if status != "completed":
        reasons.append({"reason": "not_completed", "grader_id": None, "detail": f"trial status is {status}"})
    ok = [r for r in results if r.status == "ok"]
    for r in ok:
        if r.required and not _passes_required(r, pass_threshold):
            reasons.append({"reason": "required_failed", "grader_id": r.id, "detail": r.reason})
        if r.min_score is not None and r.score is not None and r.score < r.min_score:
            reasons.append({"reason": "min_score", "grader_id": r.id,
                            "detail": f"score {r.score:.2f} < min_score {r.min_score}"})
        if r.details.get("contradiction"):
            reasons.append({"reason": "contradiction", "grader_id": r.id,
                            "detail": f"contradicted: {r.details.get('contradicted')}"})
    scored = [r for r in ok if r.score is not None]
    weighted = (sum(r.weight * r.score for r in scored) / sum(r.weight for r in scored)) if scored else None
    if weighted is not None and weighted < pass_threshold:
        reasons.append({"reason": "below_threshold", "grader_id": None,
                        "detail": f"weighted score {weighted:.2f} < pass_threshold {pass_threshold}"})
    errors = [r.id for r in results if r.status == "grader_error"]
    required_errors = [r.id for r in results if r.status == "grader_error" and r.required]
    passed: Optional[bool] = not reasons
    if passed and required_errors:
        passed = None
    return {"passed": passed, "score": None if weighted is None else round(weighted, 4),
            "pass_threshold": pass_threshold, "failure_reasons": reasons,
            "grader_errors": errors, "undetermined_by": required_errors if passed is None else []}


async def grade_trial(trial_dir: Path, suite: Suite, task: Task,
                      judge_factory: Optional[Callable[[], Any]]) -> Dict[str, Any]:
    trial = json.loads((trial_dir / "trial.json").read_text())
    obs_path = trial_dir / "observations.json"
    observations = json.loads(obs_path.read_text()) if obs_path.exists() else {}
    base = {"task": task.id, "trial": trial["trial"], "status": trial["status"],
            "suite_hash": suite.hash, "graded_at": datetime.now(timezone.utc).isoformat()}
    if trial.get("excluded"):
        return {**base, "excluded": True, "verdict": {"passed": None, "score": None, "failure_reasons": [],
                                                      "grader_errors": [], "undetermined_by": [],
                                                      "pass_threshold": task.pass_threshold},
                "graders": [], "judge": {"calls": 0, "tokens_in": 0, "tokens_out": 0, "parse_retries": 0}}

    transcript = read_transcript(trial_dir / "transcript.jsonl")
    ctx = GradeContext(trial=trial, observations=observations, suite=suite,
                       load_check=suite.load_callable, judge_factory=judge_factory)
    graders = [build_grader(spec) for spec in task.graders]
    order = sorted(range(len(graders)), key=lambda i: graders[i].llm)    # code graders first
    results: List[Optional[GradeResult]] = [None] * len(graders)
    for i in order:
        g = graders[i]
        try:
            results[i] = await g.grade(transcript, task, ctx)
        except Exception as e:   # a grader bug is a grading problem, not an agent failure
            results[i] = g.error(f"grader raised {type(e).__name__}: {e}")
    unique_ids(results)
    verdict = decide_verdict(trial["status"], results, task.pass_threshold)
    stats = ctx.judge_stats
    return {**base, "excluded": False, "verdict": verdict, "graders": [r.to_dict() for r in results],
            "judge": {"calls": stats.calls, "tokens_in": stats.tokens_in, "tokens_out": stats.tokens_out,
                      "parse_retries": stats.parse_retries}}


def trial_dirs(run_dir: Path, suite: Suite) -> List[tuple]:
    out = []
    for task in suite.tasks:
        task_dir = run_dir / task.id
        if not task_dir.is_dir():
            continue
        for d in sorted(task_dir.glob("t*"), key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0):
            if (d / "trial.json").exists():
                out.append((task, d))
    return out


async def grade_run(run_dir: Path, suite: Suite, judge_factory: Optional[Callable[[], Any]],
                    concurrency: int = 1, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Grade every stored trial of a run (up to `concurrency` trials at once, which also bounds judge calls)."""
    sem = asyncio.Semaphore(concurrency)

    async def one(task, d):
        async with sem:
            grades = await grade_trial(d, suite, task, judge_factory)
        (d / "grades.json").write_text(json.dumps(grades, indent=2, default=str), encoding="utf-8")
        v = grades["verdict"]
        label = "excluded" if grades["excluded"] else {True: "PASS", False: "FAIL", None: "UNDETERMINED"}[v["passed"]]
        why = "; ".join(f"{r['reason']}" + (f" [{r['grader_id']}]" if r["grader_id"] else "")
                        for r in v["failure_reasons"])
        log(f"  {task.id} {d.name}: {label}" + (f"  score={v['score']:.2f}" if v.get("score") is not None else "")
            + (f"  - {why}" if why else "") + (f"  grader errors: {v['grader_errors']}" if v["grader_errors"] else ""))
        return grades

    all_grades = await asyncio.gather(*(one(t, d) for t, d in trial_dirs(run_dir, suite)))
    graded = [g for g in all_grades if not g["excluded"]]
    summary = {
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "suite_hash": suite.hash,
        "trials": len(all_grades),
        "excluded": len(all_grades) - len(graded),
        "passed": sum(1 for g in graded if g["verdict"]["passed"] is True),
        "failed": sum(1 for g in graded if g["verdict"]["passed"] is False),
        "undetermined": sum(1 for g in graded if g["verdict"]["passed"] is None),
        "grader_errors": sum(len(g["verdict"]["grader_errors"]) for g in graded),
        "judge_calls": sum(g["judge"]["calls"] for g in all_grades),
        "judge_tokens_in": sum(g["judge"]["tokens_in"] for g in all_grades),
        "judge_tokens_out": sum(g["judge"]["tokens_out"] for g in all_grades),
        "judge_parse_retries": sum(g["judge"]["parse_retries"] for g in all_grades),
    }
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        manifest["grading"] = summary
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return summary
