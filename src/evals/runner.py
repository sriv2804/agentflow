"""
Run phase: every task x trial through the eval client, then the files.

    eval_runs/<run_id>/manifest.json               written at the start, completed at the end
    eval_runs/<run_id>/<task>/t<n>/transcript.jsonl one trace event per line (index, t, type, ...)
    eval_runs/<run_id>/<task>/t<n>/trial.json       status, session id, timings, counts, user summary
    eval_runs/<run_id>/<task>/t<n>/observations.json results of the state-reading checks, taken
                                                    before teardown (grading reads these, not the state)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.core.storage import configured_working_dir, get_session_dir
from src.evals.client import AttemptOutcome, run_trial_once
from src.evals.config import RunConfig
from src.evals.spec import Suite, Task


def utc_stamp(ms: bool = False) -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + (f"{now.microsecond // 1000:03d}" if ms else "") + "Z"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def agentflow_version() -> Dict[str, Any]:
    """There is no package version: the git commit, and whether the tree was dirty."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=5).stdout.strip())
        return {"git_sha": sha or None, "dirty": dirty}
    except Exception:
        return {"git_sha": None, "dirty": None}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def describe_agents(build_flow: Callable) -> Dict[str, Dict[str, Any]]:
    """Model and prompt hash per agent, from a freshly built flow (no LLM call)."""
    flow, _ = build_flow()
    out = {}
    for agent in flow.get_agents_list():
        client = getattr(agent.llm, "client", agent.llm)
        out[agent.agent_name] = {
            "model_name": getattr(client, "model_name", None),
            "backend": type(client).__name__,
            "prompt_hash": sha256(f"{agent.prompt_template}\n---\n{agent.execution_prompt}"),
            "execution_prompt_hash": sha256(agent.execution_prompt),
            "tools": sorted(agent.tool_manager.tool_dict),
        }
    return out


def to_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_transcript(path: Path, events: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, default=str) + "\n")


def trial_counts(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    llm = [e for e in events if e["type"] == "llm_call"]
    tools = [e for e in events if e["type"] == "tool_call"]
    return {
        "llm_calls": len(llm),
        "tokens_in": sum(e.get("tokens_in") or 0 for e in llm),
        "tokens_out": sum(e.get("tokens_out") or 0 for e in llm),
        "tool_calls": len(tools),
        "tool_errors": sum(1 for e in tools if e.get("error")),
        "clarifications": sum(1 for e in events if e["type"] == "clarification" and e.get("to") == "user"),
        "llm_latency_s": round(sum(e.get("latency_s") or 0 for e in llm), 3),
    }


@dataclass
class TrialRecord:
    task: str
    trial: int
    status: str
    excluded: bool
    dir: Path
    data: Dict[str, Any]


class Runner:
    def __init__(self, suite: Suite, config: RunConfig, *, task_ids: Optional[List[str]] = None,
                 trials: Optional[int] = None, build_flow: Optional[Callable[[str], Any]] = None,
                 simulator_factory: Optional[Callable[[], Any]] = None, manager=None,
                 log: Callable[[str], None] = print):
        self.suite = suite
        self.config = config
        self.trials_override = trials
        self.log = log
        if task_ids:
            unknown = [t for t in task_ids if t not in {x.id for x in suite.tasks}]
            if unknown:
                raise ValueError(f"unknown task ids: {unknown} (suite has {[t.id for t in suite.tasks]})")
        self.tasks = [t for t in suite.tasks if not task_ids or t.id in task_ids]
        if build_flow is None:
            from src.flows.registry import get_flow
            build_flow = get_flow
        self.build_flow = build_flow
        self.simulator_factory = simulator_factory or (lambda: config.user_sim_model().build(temperature=0.0))
        self._manager = manager
        self.working_dir = configured_working_dir()

    def trials_for(self, task: Task) -> int:
        return self.trials_override or task.trials

    def manifest(self, run_id: str, started_at: str) -> Dict[str, Any]:
        needs_sim = any(t.user.mode == "llm" for t in self.tasks)
        return to_jsonable({
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": None,
            "suite": {"name": self.suite.name, "path": str(self.suite.path), "hash": self.suite.hash},
            "flow": self.suite.flow,
            "agentflow_version": agentflow_version(),
            "agents": describe_agents(lambda: self.build_flow(self.suite.flow)),
            "judge": self.config.judge_model().describe(),
            "user_sim": self.config.user_sim_model().describe() if needs_sim else None,
            "trials": {t.id: self.trials_for(t) for t in self.tasks},
            "concurrency": self.config.concurrency,
            "infra_retry": self.config.infra_retry,
            "metadata": self.config.metadata,
        })

    async def run(self, run_dir: Optional[Path] = None) -> Path:
        run_id = f"{self.suite.name}_{utc_stamp()}"
        run_dir = run_dir or Path(self.config.output_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        started_wall = time.time()
        manifest = self.manifest(run_dir.name, iso_now())
        manifest["run_id"] = run_dir.name
        write_json(run_dir / "manifest.json", manifest)
        # the agents write one prompt log per LLM call; keep a run's logs with the run
        os.environ.setdefault("AGENTFLOW_LOG_DIR", str(run_dir / "prompt_logs"))

        manager = self._manager
        if manager is None:
            from src.runtime.scheduler import AsyncAgentManager
            manager = AsyncAgentManager()
            manager.run()

        # one slot per trial for its whole lifecycle (setup -> interact -> checks -> teardown,
        # infra retries included); isolating app state between trials is the suite's job
        sem = asyncio.Semaphore(self.config.concurrency)
        jobs = [(task, n) for task in self.tasks for n in range(1, self.trials_for(task) + 1)]

        async def job(task: Task, n: int) -> TrialRecord:
            async with sem:
                return await self.run_trial(manager, run_dir, task, n)

        records = await asyncio.gather(*(job(t, n) for t, n in jobs))

        statuses: Dict[str, int] = {}
        for r in records:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        totals = {
            "trials": len(records),
            "statuses": statuses,
            "excluded": sum(1 for r in records if r.excluded),
            "agent_llm_calls": sum(r.data["counts"]["llm_calls"] for r in records),
            "agent_tokens_in": sum(r.data["counts"]["tokens_in"] for r in records),
            "agent_tokens_out": sum(r.data["counts"]["tokens_out"] for r in records),
            "user_sim_calls": sum(r.data["user"]["sim_calls"] for r in records),
            "user_sim_tokens_in": sum(r.data["user"]["sim_tokens_in"] for r in records),
            "user_sim_tokens_out": sum(r.data["user"]["sim_tokens_out"] for r in records),
            "attempts": sum(len(r.data["attempts"]) for r in records),
            "duration_s": round(time.time() - started_wall, 2),
        }
        manifest["finished_at"] = iso_now()
        manifest["run_totals"] = totals
        write_json(run_dir / "manifest.json", manifest)
        return run_dir

    async def run_trial(self, manager, run_dir: Path, task: Task, n: int) -> TrialRecord:
        trial_dir = run_dir / task.id / f"t{n}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        setup = self.suite.load_callable(task.setup) if task.setup else None
        teardown = self.suite.load_callable(task.teardown) if task.teardown else None
        attempts: List[Dict[str, Any]] = []
        result: Optional[AttemptOutcome] = None
        for attempt in range(1, self.config.infra_retry + 2):
            session_id = f"eval_{self.suite.name}_{task.id}_t{n}_{utc_stamp(ms=True)}"
            session_dir = get_session_dir(self.working_dir, session_id)
            simulator = self.simulator_factory() if task.user.mode == "llm" else None
            try:
                result = await run_trial_once(manager=manager, task=task, flow_name=self.suite.flow,
                                              session_id=session_id, session_dir=session_dir,
                                              setup=setup, teardown=teardown,
                                              load_check=self.suite.load_callable, simulator=simulator)
            except Exception as e:  # a harness bug must not take the whole run down
                now = time.time()
                result = AttemptOutcome(status="infra_error", reason=f"harness failure: {type(e).__name__}: {e}",
                                        session_id=session_id, events=[], user={}, started_at=now,
                                        finished_at=now)
            attempts.append({"attempt": attempt, "session_id": session_id, "status": result.status,
                             "reason": result.reason, "teardown_error": result.teardown_error,
                             "duration_s": round(result.finished_at - result.started_at, 3)})
            if result.status != "infra_error":
                break
            if attempt <= self.config.infra_retry:
                self.log(f"  {task.id} t{n}: infra_error ({result.reason}), retrying")

        excluded = result.status == "infra_error"
        user = result.user or {"mode": task.user.mode, "replies": 0, "from_source": 0, "default_used": False,
                               "default_reply_at": [], "sim_calls": 0, "sim_tokens_in": 0, "sim_tokens_out": 0}
        data = {
            "task": task.id,
            "trial": n,
            "status": result.status,
            "reason": result.reason,
            "excluded": excluded,
            "session_id": result.session_id,
            "session_dir": str(Path(self.working_dir) / result.session_id),
            "started_at": datetime.fromtimestamp(result.started_at, timezone.utc).isoformat(),
            "finished_at": datetime.fromtimestamp(result.finished_at, timezone.utc).isoformat(),
            "duration_s": round(result.finished_at - result.started_at, 3),
            "close_reason": result.close_reason,
            "teardown_error": result.teardown_error,
            "observation_errors": [
                k for k, v in result.observations.get("state_checks", {}).items() if not v["ok"]
            ] + [
                f"{k}@{i}" for k, calls in result.observations.get("tool_checks", {}).items()
                for i, v in calls.items() if not v["ok"]
            ],
            "attempts": attempts,
            "counts": trial_counts(result.events),
            "user": user,
            "limits": task.limits.model_dump(),
        }
        write_transcript(trial_dir / "transcript.jsonl", result.events)
        write_json(trial_dir / "observations.json",
                   result.observations or {"state_checks": {}, "tool_checks": {}})
        write_json(trial_dir / "trial.json", data)
        c = data["counts"]
        self.log(f"  {task.id} t{n}: {result.status}"
                 f"{' (excluded)' if excluded else ''}  {data['duration_s']:.1f}s  "
                 f"llm_calls={c['llm_calls']} tools={c['tool_calls']} sim_calls={user['sim_calls']}"
                 + (f"  - {result.reason}" if result.reason else "")
                 + (f"  [teardown failed: {result.teardown_error}]" if result.teardown_error else ""))
        return TrialRecord(task=task.id, trial=n, status=result.status, excluded=excluded,
                           dir=trial_dir, data=data)
