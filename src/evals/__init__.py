"""
agentflow eval harness.

    python -m src.evals run <suite.yaml> [--config run.yaml] [--tasks a,b] [--trials N]

A suite (YAML, see spec.py) lists tasks, how the simulated user replies,
reference outcomes and grader configs. The run phase drives each trial through
the same channel API as the SSE server and the TUI (a third transport), records
the runtime trace, and writes eval_runs/<run_id>/<task>/t<n>/transcript.jsonl
plus trial.json, and eval_runs/<run_id>/manifest.json.

Trial lifecycle (one trial = one attempt of one task):
    1. setup(session_dir)        seeds the starting state
    2. client <-> agent          produces transcript.jsonl
    3. state-reading checks      state_check functions and (call, session_dir)
                                 predicates run on the live state -> observations.json
    4. teardown(session_dir)     always runs
Grading reads only the stored files, never the live state.

Concurrency and app state:
    `concurrency: N` (run config or --concurrency) runs up to N trials at once, each
    holding its slot for the whole lifecycle above, retries included. Every trial gets
    its own session id and session dir ({working_dir}/eval_<suite>_<task>_t<n>_<time>),
    channel, freshly built agents, tracer and user simulator.

    The harness cannot isolate state your tools keep elsewhere. Concurrent trials are
    only correct when each trial's app state is isolated:
      - tools keep their state under the session dir (ToolContext.working_dir /
        storage_session_id), which setup seeds, the checks read and teardown removes;
      - state in external systems (a database, a sandbox account, a shared file) is
        namespaced per trial in setup and removed in teardown; the session dir's name
        is the session id.
    State shared between trials (a fixed path, a module-level dict, one DB table)
    leaks across trials: concurrent trials race and see each other's writes, and even
    with concurrency 1 a trial inherits what an earlier one left unless teardown
    cleans it. Keep such suites at concurrency 1 and reset the state in setup.
"""
