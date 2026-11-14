"""
python -m src.evals run <suite.yaml> [--config run.yaml] [--tasks a,b] [--trials N]
                        [--concurrency N] [--output-dir DIR]

This PR: the run phase only (transcripts, trial.json files and the manifest).
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.evals.config import ConfigError, load_run_config
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
    started = time.time()
    run_dir = asyncio.run(runner.run())
    totals = json.loads((run_dir / "manifest.json").read_text())["run_totals"]
    print(f"\nrun dir: {run_dir}")
    print(f"time: {time.time() - started:.1f}s  statuses: {totals['statuses']}  excluded: {totals['excluded']}")
    print(f"agent LLM calls: {totals['agent_llm_calls']}  "
          f"(tokens in/out {totals['agent_tokens_in']}/{totals['agent_tokens_out']})  "
          f"user-simulator calls: {totals['user_sim_calls']}  "
          f"(tokens in/out {totals['user_sim_tokens_in']}/{totals['user_sim_tokens_out']})  judge calls: 0")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.evals", description="agentflow eval harness")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a suite: transcripts, trial files and the manifest")
    run.add_argument("suite", help="suite YAML")
    run.add_argument("--config", help="run config YAML (judge, user_sim, concurrency, ...)")
    run.add_argument("--tasks", help="comma-separated task ids to run (default: all)")
    run.add_argument("--trials", type=int, help="trials per task (overrides the suite)")
    run.add_argument("--concurrency", type=int, help="trials run at once (overrides the config)")
    run.add_argument("--output-dir", help="where run directories go (default: eval_runs)")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    return args.func(args)
