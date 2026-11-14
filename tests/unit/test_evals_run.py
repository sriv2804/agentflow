"""Run phase: the eval client drives real sessions (AsyncAgentManager, AsyncChannel,
run_on_channel) whose agents use a fake LLM."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.evals.client import UserDriver
from src.evals.config import RunConfig
from src.evals.runner import Runner
from src.evals.spec import GraderSpec, Limits, Reference, Suite, Task, UserSettings
from src.flows.registry import FLOW_REGISTRY, register_flow
from src.runtime.scheduler import AsyncAgentManager
from src.tools.common import tool
from src.utils.llm import LLMError

ON_EXHAUSTED = "No more clarification can be provided, proceed on your own."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.agent.SkillStore", MagicMock())
    monkeypatch.setattr("src.core.agent.FactStore", MagicMock())
    monkeypatch.setattr("src.core.agent.RecallStore", lambda *a, **k: MagicMock(append=AsyncMock()))
    (tmp_path / "config.yaml").write_text(
        "storage:\n  working_dir: .agentflow\nllm:\n  backend: ollama\n  model_name: judge-model\n")
    monkeypatch.setenv("AGENTFLOW_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)
    yield
    for name in [n for n in FLOW_REGISTRY if n.startswith("test_")]:
        FLOW_REGISTRY.pop(name)


class SpyManager(AsyncAgentManager):
    def __init__(self):
        super().__init__()
        self.cancelled = []

    def cancel(self, chat_id):
        self.cancelled.append(chat_id)
        return super().cancel(chat_id)


@pytest.fixture(scope="module")
def manager():
    m = SpyManager()
    m.run()
    return m


class FakeLLM:
    model_name = "fake-agent-model"

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.last_usage = None
        self.last_retries = 0

    async def invoke(self, prompt):
        self.calls += 1
        self.last_usage = {"tokens_in": 10, "tokens_out": 2}
        out = self.script(self.calls)
        if asyncio.iscoroutine(out):
            out = await out
        if isinstance(out, Exception):
            raise out
        return out


@tool(name="ping", description="ping")
async def ping(_ctx=None):
    return "pong"


def j(**fields):
    return json.dumps(fields)


def end(text="all done"):
    return j(summary="finish", action="yield", yield_action="end", yield_output=text)


def clarify(q="which order?"):
    return j(summary="ask", action="clarification", query=q)


def register(name, script_factory):
    """script_factory(build_index) -> script(call_index) -> response; a fresh agent per trial."""
    builds = {"n": 0}

    def build():
        builds["n"] += 1
        with patch("src.core.agent.LLM"), patch("src.core.agent.PromptReader") as reader:
            reader.read_prompt.return_value = "{conversation_history}\n{scratchpad}"
            reader.read_execution_prompt.return_value = "be helpful"
            from src.core.agent import Agent
            from src.core.flow import AgentsFlow, FlowContext
            agent = Agent(agent_name="helper", model_name="m", tool_grps=[], always_on_tools=[ping],
                          execution_prompt_path=Path("/fake.md"), resolver="user")
        agent.llm = FakeLLM(script_factory(builds["n"]))
        flow = AgentsFlow(start_agent=agent, list_of_agents=[agent])
        return flow, FlowContext(flow)

    register_flow(name, build)
    return builds


def make_suite(flow, *, mode="scripted", replies=(), persona=None, facts=(), max_clar=4,
               timeout_s=30.0, max_llm_calls=40, trials=1, setup=None, teardown=None, graders=(),
               tmp_base=Path(".")):
    task = Task(id="t", prompt="help me", setup=setup, teardown=teardown,
                user=UserSettings(mode=mode, replies=list(replies), persona=persona, facts=list(facts),
                                  on_exhausted=ON_EXHAUSTED, max_clarifications=max_clar),
                reference=Reference(), context={}, graders=list(graders), trials=trials, pass_threshold=0.7,
                limits=Limits(timeout_s=timeout_s, max_llm_calls=max_llm_calls))
    return Suite(name="s", flow=flow, path=Path("suite.yaml").resolve(), base_dir=tmp_base.resolve(),
                 hash="h" * 64, dimensions={}, tasks=[task])


def run(manager, suite, config=None, simulator_factory=None, **kw):
    runner = Runner(suite, config or RunConfig(), build_flow=lambda name: FLOW_REGISTRY[name](),
                    simulator_factory=simulator_factory, manager=manager, log=lambda m: None, **kw)
    run_dir = asyncio.run(runner.run(Path("eval_runs/run1")))
    return run_dir


def trial(run_dir, task="t", n=1):
    d = run_dir / task / f"t{n}"
    events = [json.loads(line) for line in (d / "transcript.jsonl").read_text().splitlines()]
    return json.loads((d / "trial.json").read_text()), events


def user_messages(events):
    return [e["content"] for e in events if e["type"] == "user_message" and not e.get("initial")]


# ---------------- replies ----------------

def test_scripted_replies_in_order_then_default(manager):
    register("test_scripted", lambda b: lambda n: clarify(f"q{n}") if n <= 3 else end())
    data, events = trial(run(manager, make_suite("test_scripted", replies=["first", "second"])))
    assert data["status"] == "completed"
    assert user_messages(events) == ["first", "second", ON_EXHAUSTED]
    assert data["user"] == {"mode": "scripted", "replies": 3, "from_source": 2, "default_used": True,
                            "default_reply_at": [2], "sim_calls": 0, "sim_tokens_in": 0, "sim_tokens_out": 0}
    assert data["counts"]["clarifications"] == 3


def test_no_default_when_replies_suffice(manager):
    register("test_enough", lambda b: lambda n: clarify() if n == 1 else end())
    data, events = trial(run(manager, make_suite("test_enough", replies=["only"])))
    assert user_messages(events) == ["only"] and data["user"]["default_used"] is False


def test_simulated_user_answers_from_facts_and_tokens_are_separate(manager):
    register("test_sim", lambda b: lambda n: clarify("What is your order number?") if n == 1 else end())
    prompts = []

    class Simulator:
        last_usage = None

        async def invoke(self, prompt):
            prompts.append(prompt)
            self.last_usage = {"tokens_in": 500, "tokens_out": 9}
            return "It is 1182." if "order number is 1182" in prompt else "I don't know."

    suite = make_suite("test_sim", mode="llm", persona="A hurried customer.", facts=["order number is 1182"])
    data, events = trial(run(manager, suite, simulator_factory=Simulator))
    assert user_messages(events) == ["It is 1182."]
    prompt = prompts[0]
    assert "A hurried customer." in prompt and "- order number is 1182" in prompt
    assert "help me" in prompt and "What is your order number?" in prompt and "do not know" in prompt
    assert data["user"]["sim_calls"] == 1
    assert (data["user"]["sim_tokens_in"], data["user"]["sim_tokens_out"]) == (500, 9)
    assert (data["counts"]["tokens_in"], data["counts"]["tokens_out"]) == (20, 4)   # agent's 2 calls only


def test_simulated_user_driver_caps_then_defaults():
    sim = SimpleNamespace(last_usage=None, invoke=AsyncMock(return_value="sure"))
    driver = UserDriver(mode="llm", replies=[], on_exhausted="DEFAULT", max_clarifications=2,
                        persona="p", facts=["f"], simulator=sim)
    replies = [asyncio.run(driver.reply("q")) for _ in range(4)]
    assert replies == ["sure", "sure", "DEFAULT", None] and sim.invoke.await_count == 2


# ---------------- limits ----------------

def test_timeout_cancels_the_session(manager):
    async def slow(n):
        await asyncio.sleep(30)
    register("test_slow", lambda b: lambda n: slow(n))
    data, _ = trial(run(manager, make_suite("test_slow", timeout_s=0.5)))
    assert data["status"] == "timeout" and "timeout_s" in data["reason"]
    assert data["session_id"] in manager.cancelled and data["close_reason"] == "shutdown"


def test_max_llm_calls_cancels_the_session(manager):
    register("test_loop", lambda b: lambda n: j(summary="again", action="tool_call", tool_name="ping", args={}))
    data, events = trial(run(manager, make_suite("test_loop", max_llm_calls=3)))
    assert data["status"] == "limit_exceeded" and "max_llm_calls" in data["reason"]
    assert data["session_id"] in manager.cancelled
    assert data["counts"]["llm_calls"] == 4   # the call that crossed the limit is recorded


def test_max_clarifications_cancels_the_session(manager):
    register("test_ask", lambda b: lambda n: clarify(f"q{n}"))
    data, events = trial(run(manager, make_suite("test_ask", replies=["a", "b", "c"], max_clar=1)))
    assert data["status"] == "limit_exceeded" and "max_clarifications" in data["reason"]
    assert user_messages(events) == ["a", ON_EXHAUSTED]
    assert data["session_id"] in manager.cancelled


def test_irrecoverable_error_is_agent_error(manager):
    register("test_garbage", lambda b: lambda n: "not json")
    data, events = trial(run(manager, make_suite("test_garbage")))
    assert data["status"] == "agent_error" and "Failed to parse" in data["reason"]
    assert data["excluded"] is False and events[-2]["type"] == "error"


# ---------------- infra errors ----------------

def test_infra_error_is_retried(manager):
    # the first attempt's provider fails; the retry gets a fresh flow that works
    failed = []

    def script(n):
        if not failed:
            failed.append(1)
            return LLMError("503 after retries")
        return end()
    register("test_flaky", lambda b: script)
    data, _ = trial(run(manager, make_suite("test_flaky"), RunConfig(infra_retry=1)))
    assert data["status"] == "completed" and data["excluded"] is False
    assert [a["status"] for a in data["attempts"]] == ["infra_error", "completed"]
    assert data["attempts"][0]["session_id"] != data["attempts"][1]["session_id"]


def test_infra_error_that_persists_is_excluded(manager):
    register("test_down", lambda b: lambda n: LLMError("quota exhausted"))
    run_dir = run(manager, make_suite("test_down"), RunConfig(infra_retry=2))
    data, _ = trial(run_dir)
    assert data["status"] == "infra_error" and data["excluded"] is True
    assert len(data["attempts"]) == 3 and "LLMError" in data["reason"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["run_totals"]["excluded"] == 1


def test_failing_setup_hook_is_infra_error(manager, tmp_path):
    (tmp_path / "fx.py").write_text("def boom(session_dir):\n    raise RuntimeError('no db')\n")
    register("test_setup", lambda b: lambda n: end())
    data, _ = trial(run(manager, make_suite("test_setup", setup="fx.boom", tmp_base=tmp_path),
                        RunConfig(infra_retry=0)))
    assert data["status"] == "infra_error" and "setup hook failed" in data["reason"]


def test_setup_hook_gets_the_session_dir(manager, tmp_path):
    (tmp_path / "fx.py").write_text("def seed(session_dir):\n    (session_dir / 'seeded').write_text('1')\n")
    register("test_seed", lambda b: lambda n: end())
    data, _ = trial(run(manager, make_suite("test_seed", setup="fx.seed", tmp_base=tmp_path)))
    assert (Path(data["session_dir"]) / "seeded").exists()
    assert data["session_dir"] == f".agentflow/{data['session_id']}"
    assert data["session_id"].startswith("eval_s_t_t1_")


# ---------------- files ----------------

def test_transcript_manifest_and_trial_files(manager):
    register("test_files", lambda b: lambda n: end("final answer"))
    config = RunConfig(metadata={"label": "baseline", "git_sha": "abc"}, concurrency=2)
    run_dir = run(manager, make_suite("test_files", trials=2), config)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["run_id"] == "run1" and manifest["started_at"] and manifest["finished_at"]
    assert manifest["suite"] == {"name": "s", "path": str(Path("suite.yaml").resolve()), "hash": "h" * 64}
    assert manifest["flow"] == "test_files" and manifest["metadata"] == {"label": "baseline", "git_sha": "abc"}
    agent = manifest["agents"]["helper"]
    assert agent["model_name"] == "fake-agent-model" and len(agent["prompt_hash"]) == 64
    assert manifest["judge"]["model_name"] == "judge-model"   # defaults to the agents' llm settings
    assert manifest["user_sim"] is None                       # no llm-mode task
    assert manifest["trials"] == {"t": 2} and "git_sha" in manifest["agentflow_version"]
    assert manifest["run_totals"]["statuses"] == {"completed": 2}

    data, events = trial(run_dir, n=2)
    assert [e["index"] for e in events] == list(range(len(events)))
    assert all(isinstance(e["t"], float) for e in events)
    assert events[0] == {**events[0], "type": "user_message", "content": "help me", "initial": True}
    assert any(e["type"] == "final" and e["output"] == "final answer" for e in events)
    assert data["trial"] == 2 and data["duration_s"] >= 0 and data["counts"]["llm_calls"] == 1
    # the session saved its history before the client stopped reading
    assert (Path(data["session_dir"]) / "conversation_history_helper.json").exists()


def test_task_and_trial_filters(manager):
    register("test_filter", lambda b: lambda n: end())
    suite = make_suite("test_filter", trials=3)
    run_dir = run(manager, suite, trials=1, task_ids=["t"])
    assert sorted(p.name for p in (run_dir / "t").iterdir()) == ["t1"]
    with pytest.raises(ValueError):
        Runner(suite, RunConfig(), task_ids=["nope"], build_flow=lambda n: None)


# ---------------- state checks before teardown ----------------

FX = """
from pathlib import Path

def seed(session_dir):
    (session_dir / "state.txt").write_text("open")

def resolve(session_dir):
    (session_dir / "state.txt").write_text("resolved")

def status(session_dir):
    return {"status": (session_dir / "state.txt").read_text()}

def broken(session_dir):
    raise KeyError("no ticket")

def call_ok(call, session_dir):          # reads state: runs before teardown
    return (session_dir / "state.txt").exists() and call["output"] == "pong"

def args_only(call):                     # reads no state: left for grading
    raise AssertionError("must not run in the run phase")

def clear(session_dir):
    (session_dir / "state.txt").unlink(missing_ok=True)
    (session_dir / "torn_down").write_text("1")

def bad_teardown(session_dir):
    raise OSError("disk full")
"""


def two_pings_then_end(b):
    return lambda n: j(summary="p", action="tool_call", tool_name="ping", args={}) if n <= 2 else end()


GRADERS = [
    GraderSpec(type="state_check", check="fx.status", expect={"status": "open"}, required=True),
    GraderSpec(type="state_check", check="fx.status", expect={"status": "resolved"}),   # same fn: run once
    GraderSpec(type="state_check", check="fx.broken", expect={}),
    GraderSpec(type="tool_called", tool="ping", check="fx.call_ok"),
    GraderSpec(type="tool_called", tool="ping", check="fx.args_only"),
]


def observations(run_dir):
    return json.loads((run_dir / "t" / "t1" / "observations.json").read_text())


def test_state_checks_run_on_live_state_then_teardown(manager, tmp_path):
    (tmp_path / "fx.py").write_text(FX)
    register("test_obs", two_pings_then_end)
    run_dir = run(manager, make_suite("test_obs", setup="fx.seed", teardown="fx.clear", graders=GRADERS,
                                      tmp_base=tmp_path))
    data, events = trial(run_dir)
    obs = observations(run_dir)
    assert obs["state_checks"]["fx.status"] == {"ok": True, "value": {"status": "open"}}
    assert obs["state_checks"]["fx.broken"]["ok"] is False and "KeyError" in obs["state_checks"]["fx.broken"]["error"]
    ping_indices = [str(e["index"]) for e in events if e["type"] == "tool_call"]
    assert list(obs["tool_checks"]["ping:fx.call_ok"]) == ping_indices and len(ping_indices) == 2
    assert all(v == {"ok": True, "value": True} for v in obs["tool_checks"]["ping:fx.call_ok"].values())
    assert "ping:fx.args_only" not in obs["tool_checks"]       # (call)-only predicates wait for grading
    session_dir = Path(data["session_dir"])
    assert (session_dir / "torn_down").exists() and not (session_dir / "state.txt").exists()
    assert data["teardown_error"] is None and data["observation_errors"] == ["fx.broken"]


def test_teardown_runs_after_timeout_and_checks_see_partial_state(manager, tmp_path):
    (tmp_path / "fx.py").write_text(FX)

    async def slow(n):
        await asyncio.sleep(30)
    register("test_obs_slow", lambda b: lambda n: slow(n))
    run_dir = run(manager, make_suite("test_obs_slow", setup="fx.seed", teardown="fx.clear", timeout_s=0.5,
                                      graders=GRADERS[:1], tmp_base=tmp_path))
    data, _ = trial(run_dir)
    assert data["status"] == "timeout"
    assert observations(run_dir)["state_checks"]["fx.status"]["value"] == {"status": "open"}
    assert (Path(data["session_dir"]) / "torn_down").exists()


def test_teardown_runs_after_failed_setup(manager, tmp_path):
    (tmp_path / "fx.py").write_text(FX + "\ndef half_seed(session_dir):\n"
                                    "    seed(session_dir)\n    raise RuntimeError('db down')\n")
    register("test_obs_setup", lambda b: lambda n: end())
    data, _ = trial(run(manager, make_suite("test_obs_setup", setup="fx.half_seed", teardown="fx.clear",
                                            tmp_base=tmp_path), RunConfig(infra_retry=0)))
    assert data["status"] == "infra_error"
    assert (Path(data["session_dir"]) / "torn_down").exists()


def test_failed_teardown_is_recorded_without_changing_the_status(manager, tmp_path):
    (tmp_path / "fx.py").write_text(FX)
    register("test_obs_td", lambda b: lambda n: end())
    data, _ = trial(run(manager, make_suite("test_obs_td", teardown="fx.bad_teardown", tmp_base=tmp_path)))
    assert data["status"] == "completed"
    assert data["teardown_error"] == "OSError: disk full" and data["attempts"][0]["teardown_error"]
