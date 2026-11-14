"""
python -m src.evals run <suite.yaml> [--config run.yaml] [--tasks a,b] [--trials N]
                        [--concurrency N] [--output-dir DIR]
python -m src.evals grade <run_dir> [--config run.yaml] [--suite suite.yaml] [--concurrency N]

`run` runs the trials, then grades them. `grade` re-grades a stored run without
re-running the agent (e.g. after changing a grader's config in the suite).
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.evals.config import ConfigError, ModelConfig, load_run_config
from src.evals.spec import SpecError, load_suite


def cmd_run(args) -> int:
    if args.trials is not None and args.trials < 1:
        print("--trials must be at least 1", file=sys.stderr)
        return 2
    try:
        config = load_run_config(args.config, concurrency=args.concurrency, output_dir=args.output_dir)
    except (ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    try:
        suite = load_suite(args.suite)
    except SpecError as e:
        print(e, file=sys.stderr)
        return 2

    from src.evals.runner import Runner
    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()] if args.tasks else None
    try:
        runner = Runner(suite, config, task_ids=task_ids, trials=args.trials)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    n_trials = sum(runner.trials_for(t) for t in runner.tasks)
    print(f"suite {suite.name}: {len(runner.tasks)} task(s), {n_trials} trial(s), "
          f"concurrency {config.concurrency}")
    if config.concurrency > 1:
        print("note: trials run concurrently. Each trial's app state must be isolated: kept under its "
              "session dir, or namespaced per trial by the setup/teardown hooks. Shared state leaks "
              "between trials.")
    started = time.time()
    run_dir = asyncio.run(runner.run())
    totals = json.loads((run_dir / "manifest.json").read_text())["run_totals"]
    print(f"\nrun dir: {run_dir}")
    print(f"time: {time.time() - started:.1f}s  statuses: {totals['statuses']}  excluded: {totals['excluded']}")
    print(f"agent LLM calls: {totals['agent_llm_calls']}  "
          f"(tokens in/out {totals['agent_tokens_in']}/{totals['agent_tokens_out']})  "
          f"user-simulator calls: {totals['user_sim_calls']}  "
          f"(tokens in/out {totals['user_sim_tokens_in']}/{totals['user_sim_tokens_out']})")

    judge = config.judge_model()
    print(f"\ngrading with judge {judge.backend}:{judge.model_name}")
    summary = do_grade(run_dir, suite, judge, config.concurrency)
    print(f"total time: {time.time() - started:.1f}s")
    return 0


def do_grade(run_dir: Path, suite, judge: ModelConfig, concurrency: int) -> dict:
    from src.evals.grading import grade_run
    started = time.time()
    summary = asyncio.run(grade_run(run_dir, suite, lambda: judge.build(temperature=0.0), concurrency=concurrency))
    print(f"graded {summary['trials']} trial(s) in {time.time() - started:.1f}s: "
          f"{summary['passed']} passed, {summary['failed']} failed, {summary['undetermined']} undetermined, "
          f"{summary['excluded']} excluded; grader errors: {summary['grader_errors']}")
    print(f"judge calls: {summary['judge_calls']}  (tokens in/out {summary['judge_tokens_in']}/"
          f"{summary['judge_tokens_out']}, parse retries {summary['judge_parse_retries']})")
    return summary


def cmd_grade(args) -> int:
    run_dir = Path(args.run_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"{run_dir} is not a run directory (no manifest.json)", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    try:
        config = load_run_config(args.config, concurrency=args.concurrency)
    except (ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    suite_path = args.suite or manifest["suite"]["path"]
    try:
        suite = load_suite(suite_path)
    except SpecError as e:
        print(e, file=sys.stderr)
        return 2
    except OSError as e:
        print(f"cannot read the suite {suite_path}: {e}", file=sys.stderr)
        return 2
    if suite.hash != manifest["suite"]["hash"]:
        print(f"note: {suite_path} changed since the run; grading with its current version")
    # the judge: the run config's when given, else the one recorded in the manifest
    if config.judge is not None:
        judge = config.judge
    elif manifest.get("judge") and manifest["judge"].get("model_name"):
        judge = ModelConfig(**{k: v for k, v in manifest["judge"].items()
                               if k in ("backend", "model_name", "base_url", "api_key_env")})
    else:
        judge = config.judge_model()
    print(f"grading {run_dir} with judge {judge.backend}:{judge.model_name}")
    do_grade(run_dir, suite, judge, config.concurrency)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.evals", description="agentflow eval harness")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a suite and grade it")
    run.add_argument("suite", help="suite YAML")
    run.add_argument("--config", help="run config YAML (judge, user_sim, concurrency, ...)")
    run.add_argument("--tasks", help="comma-separated task ids to run (default: all)")
    run.add_argument("--trials", type=int, help="trials per task (overrides the suite)")
    run.add_argument("--concurrency", type=int,
                     help="trials run at once, each from setup to teardown (overrides the config); "
                          "only correct when each trial's app state is isolated by the suite's "
                          "session dir or setup/teardown hooks")
    run.add_argument("--output-dir", help="where run directories go (default: eval_runs)")
    run.set_defaults(func=cmd_run)

    grade = sub.add_parser("grade", help="grade (or re-grade) a stored run without re-running it")
    grade.add_argument("run_dir", help="eval_runs/<run_id>")
    grade.add_argument("--config", help="run config YAML (its judge overrides the one in the manifest)")
    grade.add_argument("--suite", help="suite YAML (default: the path recorded in the manifest)")
    grade.add_argument("--concurrency", type=int, help="trials graded at once (bounds judge calls)")
    grade.set_defaults(func=cmd_grade)

    args = parser.parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    return args.func(args)
