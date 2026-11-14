"""
agentflow eval harness.

    python -m src.evals run <suite.yaml> [--config run.yaml] [--tasks a,b] [--trials N]

A suite (YAML, see spec.py) lists tasks, how the simulated user replies,
reference outcomes and grader configs. The run phase drives each trial through
the same channel API as the SSE server and the TUI (a third transport), records
the runtime trace, and writes eval_runs/<run_id>/<task>/t<n>/transcript.jsonl
plus trial.json, and eval_runs/<run_id>/manifest.json.
"""
