import asyncio
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.evals.grading import decide_verdict, grade_run, grade_trial
from src.evals.graders import (GRADER_REGISTRY, GradeContext, GradeResult, GraderConfigError, build_grader,
                               register_grader)
from src.evals.spec import Dimension, GraderSpec, Limits, Reference, Task, UserSettings, load_suite
from src.flows.registry import FLOW_REGISTRY, register_flow
from src.utils.llm import LLMError


# ---------------- helpers ----------------

def T(*events):
    """A transcript: (type, fields) pairs -> indexed events."""
    return [{"index": i, "t": float(i), "type": typ, **fields} for i, (typ, fields) in enumerate(events)]


def call(tool, args=None, output="ok", error=None, agent="a"):
    return ("tool_call", {"agent": agent, "tool": tool, "args": args or {}, "output": output, "error": error})


def task(**kw):
    defaults = dict(id="t", prompt="p", setup=None, user=UserSettings("scripted", [], None, [], "x", 4),
                    reference=Reference(expected="refund issued", key_facts=["a refund was initiated",
                                                                             "arrives in 5 to 7 days"]),
                    context={"policy": "POLICY-TEXT", "other": "OTHER-TEXT"}, graders=[], trials=1,
                    pass_threshold=0.7, limits=Limits())
    defaults.update(kw)
    return Task(**defaults)


DIMS = {
    "policy_adherence": Dimension(description="follows policy", scale={0: "no", 1: "minor", 2: "yes"},
                                  needs=["policy"]),
    "politeness": Dimension(description="polite", scale={0: "rude", 1: "neutral", 2: "courteous"}),
}


def ctx(observations=None, judge=None, checks=None):
    suite = SimpleNamespace(dimensions=DIMS)
    return GradeContext(trial={}, observations=observations or {}, suite=suite,
                        load_check=lambda name: (checks or {})[name],
                        judge_factory=(lambda: judge) if judge else None)


def grade(spec: dict, transcript, c=None, t=None):
    g = build_grader(GraderSpec(**spec))
    return asyncio.run(g.grade(transcript, t or task(), c or ctx()))


class FakeJudge:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.last_usage = None

    async def invoke(self, prompt):
        self.prompts.append(prompt)
        self.last_usage = {"tokens_in": 100, "tokens_out": 10}
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


# ---------------- state_check ----------------

def test_state_check_pass_fail_and_errors():
    obs = {"state_checks": {"fx.status": {"ok": True, "value": {"status": "resolved", "n": 1}},
                            "fx.bad": {"ok": False, "error": "KeyError: 'x'"}}}
    ok = grade({"type": "state_check", "check": "fx.status", "expect": {"status": "resolved"}}, [], ctx(obs))
    assert (ok.passed, ok.score, ok.id) == (True, 1.0, "state_check:fx.status")
    bad = grade({"type": "state_check", "check": "fx.status", "expect": {"status": "open"}}, [], ctx(obs))
    assert bad.passed is False and bad.score == 0.0 and "status='resolved' (expected 'open')" in bad.reason
    assert grade({"type": "state_check", "check": "fx.bad", "expect": {}}, [], ctx(obs)).status == "grader_error"
    assert grade({"type": "state_check", "check": "fx.gone", "expect": {}}, [], ctx(obs)).status == "grader_error"


# ---------------- tool_called / tool_not_called ----------------

REFUNDS = T(("user_message", {"content": "hi", "initial": True}),
            call("initiate_refund", {"order_id": "1182", "amount": 300}, error="limit exceeded"),
            call("initiate_refund", {"order_id": "1182", "amount": 64.99, "method": "original_payment"}),
            call("lookup_order", {"order_id": "1182"}))


def test_tool_called_counts_successful_calls_by_default():
    r = grade({"type": "tool_called", "tool": "initiate_refund"}, REFUNDS)
    assert r.passed and r.evidence == [2] and r.id == "tool_called:initiate_refund"
    r = grade({"type": "tool_called", "tool": "initiate_refund", "include_failed": True, "max_calls": 1}, REFUNDS)
    assert r.passed is False and "at most 1" in r.reason
    assert grade({"type": "tool_called", "tool": "update_ticket"}, REFUNDS).passed is False


def test_tool_called_args_filters():
    assert grade({"type": "tool_called", "tool": "initiate_refund", "args_equal": {"order_id": 1182}}, REFUNDS).passed
    assert grade({"type": "tool_called", "tool": "initiate_refund",
                  "args_contain": {"method": "ORIGINAL"}}, REFUNDS).passed
    r = grade({"type": "tool_called", "tool": "initiate_refund", "args_equal": {"order_id": "9"}}, REFUNDS)
    assert r.passed is False and "at least 1" in r.reason


def test_tool_called_predicate_on_args_runs_at_grading_time():
    checks = {"fx.small": lambda c: c["args"]["amount"] <= 100, "fx.tiny": lambda c: c["args"]["amount"] <= 10}
    assert grade({"type": "tool_called", "tool": "initiate_refund", "check": "fx.small"}, REFUNDS,
                 ctx(checks=checks)).passed
    r = grade({"type": "tool_called", "tool": "initiate_refund", "check": "fx.tiny"}, REFUNDS, ctx(checks=checks))
    assert r.passed is False and r.details["check_failed"] == [2]


def test_tool_called_state_predicate_reads_observations_and_must_hold_for_every_call():
    def within_limit(call, session_dir):   # (call, session_dir): ran before teardown
        raise AssertionError("must not run at grading time")
    two = REFUNDS + T(call("initiate_refund", {"order_id": "1182", "amount": 999}))[0:1]
    two[-1]["index"] = 4
    obs = {"tool_checks": {"initiate_refund:fx.limit": {"1": {"ok": True, "value": False},
                                                        "2": {"ok": True, "value": True},
                                                        "4": {"ok": True, "value": False}}}}
    c = ctx(obs, checks={"fx.limit": within_limit})
    r = grade({"type": "tool_called", "tool": "initiate_refund", "check": "fx.limit"}, two, c)
    assert r.passed is False and r.details["check_failed"] == [4]          # one bad refund fails it
    only_good = {"tool_checks": {"initiate_refund:fx.limit": {"2": {"ok": True, "value": True}}}}
    assert grade({"type": "tool_called", "tool": "initiate_refund", "check": "fx.limit"}, REFUNDS,
                 ctx(only_good, checks={"fx.limit": within_limit})).passed
    missing = grade({"type": "tool_called", "tool": "initiate_refund", "check": "fx.limit"}, REFUNDS,
                    ctx({}, checks={"fx.limit": within_limit}))
    assert missing.status == "grader_error"


def test_tool_not_called_counts_failed_attempts():
    assert grade({"type": "tool_not_called", "tool": "update_ticket"}, REFUNDS).passed
    r = grade({"type": "tool_not_called", "tool": "initiate_refund"}, REFUNDS)
    assert r.passed is False and r.evidence == [1, 2] and r.id == "tool_not_called:initiate_refund"


# ---------------- handoff, clarification, text, steps ----------------

FLOW = T(("handoff", {"from_agent": "user", "action": None, "to_agent": "orch"}),
         ("llm_call", {"agent": "orch"}),
         ("handoff", {"from_agent": "orch", "action": "research", "to_agent": "researcher"}),
         ("llm_call", {"agent": "researcher"}),
         ("handoff", {"from_agent": "researcher", "action": "orchestrator", "to_agent": "orch"}),
         ("clarification", {"agent": "orch", "to": "user", "query": "Which order?"}),
         ("user_message", {"content": "1182"}),
         ("final", {"agent": "orch", "output": "Your refund of $64.99 arrives in 5 to 7 business days."}),
         ("handoff", {"from_agent": "orch", "action": "end", "to_agent": "user"}))


def test_handoff():
    assert grade({"type": "handoff", "sequence_contains": ["orch", "orch"]}, FLOW).passed
    assert grade({"type": "handoff", "sequence_contains": ["researcher", "orch"],
                  "agents_invoked": ["researcher"]}, FLOW).passed
    assert grade({"type": "handoff", "sequence_contains": ["researcher", "researcher"]}, FLOW).passed is False
    r = grade({"type": "handoff", "agents_invoked": ["writer"]}, FLOW)
    assert r.passed is False and "writer" in r.reason


def test_clarification_asked():
    assert grade({"type": "clarification_asked", "min": 1}, FLOW).passed
    assert grade({"type": "clarification_asked", "min": 2}, FLOW).passed is False
    assert grade({"type": "clarification_asked", "min": 0, "max": 0}, FLOW).passed is False


def test_text_match():
    assert grade({"type": "text_match", "contains": "5 to 7"}, FLOW).passed
    assert grade({"type": "text_match", "regex": r"\$\d+\.\d\d"}, FLOW).passed
    assert grade({"type": "text_match", "contains": "Which order", "target": "final"}, FLOW).passed is False
    r = grade({"type": "text_match", "contains": "which order", "target": "transcript"}, FLOW)
    assert r.passed and r.evidence == [5]
    assert grade({"type": "text_match", "contains": "sorry", "negate": True}, FLOW).passed
    assert grade({"type": "text_match", "contains": "refund", "negate": True}, FLOW).passed is False


def test_max_steps():
    assert grade({"type": "max_steps", "max_llm_calls": 2}, FLOW).passed
    assert grade({"type": "max_steps", "max_llm_calls": 1}, FLOW).passed is False


@pytest.mark.parametrize("spec", [
    {"type": "text_match"}, {"type": "text_match", "regex": "a", "contains": "b"},
    {"type": "text_match", "regex": "("}, {"type": "max_steps"}, {"type": "handoff"},
    {"type": "clarification_asked", "min": 2, "max": 1}, {"type": "tool_called"},
    {"type": "llm_rubric", "dimensions": []}, {"type": "nope"},
])
def test_invalid_grader_config(spec):
    with pytest.raises(GraderConfigError):
        build_grader(GraderSpec(**spec))


# ---------------- LLM graders ----------------

def facts_reply(*verdicts):
    return {"facts": [{"fact": f"f{i}", "reasoning": f"because {i}", "verdict": v, "evidence": [7, 99]}
                      for i, v in enumerate(verdicts)]}


def test_key_facts_parses_fenced_json_and_scores():
    judge = FakeJudge("Here you go:\n```json\n" + json.dumps(facts_reply("present", "absent")) + "\n```")
    c = ctx(judge=judge)
    r = grade({"type": "key_facts", "weight": 4}, FLOW, c)
    assert r.status == "ok" and r.score == 0.5 and r.passed is None and r.llm
    assert r.details["counts"] == {"present": 1, "absent": 1, "contradicted": 0, "undetermined": 0}
    assert r.evidence == [7]                              # 99 is not an event index: dropped
    prompt = judge.prompts[0]
    assert "1. a refund was initiated" in prompt and "refund issued" in prompt and "[5] agent asks: Which order?" in prompt
    assert (c.judge_stats.calls, c.judge_stats.tokens_in, c.judge_stats.parse_retries) == (1, 100, 0)


def test_key_facts_contradiction_and_min_score():
    r = grade({"type": "key_facts", "fail_on_contradiction": True, "min_score": 0.5}, FLOW,
              ctx(judge=FakeJudge(facts_reply("present", "contradicted"))))
    assert r.details["contradiction"] is True and r.details["contradicted"] == ["arrives in 5 to 7 days"]
    assert r.passed is True                               # min_score met; the verdict handles the contradiction
    r = grade({"type": "key_facts"}, FLOW, ctx(judge=FakeJudge(facts_reply("present", "contradicted"))))
    assert r.details["contradiction"] is False            # not flagged without fail_on_contradiction


def test_judge_retries_unusable_output_then_succeeds():
    judge = FakeJudge("not json at all", facts_reply("present", "maybe"), facts_reply("present", "present"))
    c = ctx(judge=judge)
    r = grade({"type": "key_facts"}, FLOW, c)
    assert r.status == "ok" and r.score == 1.0
    assert (c.judge_stats.calls, c.judge_stats.parse_retries) == (3, 2)
    assert "could not be used" in judge.prompts[1] and "'maybe' is not one of" in judge.prompts[2]


def test_judge_gives_up_after_two_retries_with_grader_error():
    c = ctx(judge=FakeJudge("{}", "{}", "{}", "never asked"))
    r = grade({"type": "key_facts"}, FLOW, c)
    assert r.status == "grader_error" and r.passed is None and r.score is None
    assert "unusable after 3 attempts" in r.reason and c.judge_stats.calls == 3


def test_judge_provider_failure_is_grader_error():
    r = grade({"type": "key_facts"}, FLOW, ctx(judge=FakeJudge(LLMError("quota"))))
    assert r.status == "grader_error" and "judge call failed" in r.reason
    assert grade({"type": "key_facts"}, FLOW, ctx()).status == "grader_error"     # no judge configured


def rubric_reply(**scores):
    return {"dimensions": {n: {"reasoning": f"{n} reasoning\nsecond line", "score": s, "evidence": [7]}
                           for n, s in scores.items()}}


def test_llm_rubric_normalises_and_sends_only_needed_context():
    judge = FakeJudge(rubric_reply(policy_adherence=2, politeness=1))
    r = grade({"type": "llm_rubric", "dimensions": ["policy_adherence", "politeness"], "min_score": 0.5}, REFUNDS,
              ctx(judge=judge))
    assert r.score == 0.75 and r.passed is True and r.id == "llm_rubric:policy_adherence+politeness"
    dims = r.details["dimensions"]
    assert dims["politeness"]["normalised"] == 0.5 and dims["politeness"]["reason"] == "politeness reasoning second line"
    prompt = judge.prompts[0]
    assert "POLICY-TEXT" in prompt and "OTHER-TEXT" not in prompt
    assert "calls tool initiate_refund" in prompt and "ERROR: limit exceeded" in prompt


def test_llm_rubric_undetermined_and_out_of_scale():
    r = grade({"type": "llm_rubric", "dimensions": ["policy_adherence", "politeness"]}, FLOW,
              ctx(judge=FakeJudge(rubric_reply(policy_adherence=None, politeness=2))))
    assert r.score == 1.0 and "undetermined: ['policy_adherence']" in r.reason
    c = ctx(judge=FakeJudge(rubric_reply(policy_adherence=5, politeness=2), rubric_reply(policy_adherence=0, politeness=0)))
    r = grade({"type": "llm_rubric", "dimensions": ["policy_adherence", "politeness"]}, FLOW, c)
    assert r.score == 0.0 and c.judge_stats.parse_retries == 1
    r = grade({"type": "llm_rubric", "dimensions": ["politeness"]}, FLOW,
              ctx(judge=FakeJudge(rubric_reply(politeness=None))))
    assert r.status == "grader_error"


# ---------------- verdict ----------------

def R(id, passed, score, required=False, weight=1, min_score=None, status="ok", **details):
    return GradeResult(id=id, type=id, passed=passed, score=score, required=required, weight=weight,
                       min_score=min_score, status=status, details=details)


def reasons(v):
    return [(r["reason"], r["grader_id"]) for r in v["failure_reasons"]]


def test_verdict_passes_when_everything_holds():
    v = decide_verdict("completed", [R("a", True, 1.0, required=True), R("kf", None, 0.75, weight=4)], 0.7)
    assert v["passed"] is True and v["score"] == 0.8 and v["failure_reasons"] == []


def test_verdict_failure_reasons_are_tagged():
    results = [R("state", False, 0.0, required=True),
               R("rubric", True, 0.4, min_score=0.5),
               R("kf", None, 1.0, weight=4, contradiction=True, contradicted=["x"])]
    v = decide_verdict("timeout", results, 0.9)
    assert v["passed"] is False
    assert reasons(v) == [("not_completed", None), ("required_failed", "state"), ("min_score", "rubric"),
                          ("contradiction", "kf"), ("below_threshold", None)]


def test_required_scored_grader_without_min_score_uses_the_threshold():
    assert reasons(decide_verdict("completed", [R("kf", None, 0.5, required=True)], 0.7)) == [
        ("required_failed", "kf"), ("below_threshold", None)]


def test_grader_errors_are_not_agent_failures():
    v = decide_verdict("completed", [R("a", True, 1.0), R("judge", None, None, status="grader_error")], 0.7)
    assert v["passed"] is True and v["grader_errors"] == ["judge"]        # excluded from the mean
    v = decide_verdict("completed", [R("a", True, 1.0), R("judge", None, None, required=True,
                                                          status="grader_error")], 0.7)
    assert v["passed"] is None and v["undetermined_by"] == ["judge"]
    v = decide_verdict("completed", [R("a", False, 0.0, required=True),
                                     R("judge", None, None, required=True, status="grader_error")], 0.7)
    assert v["passed"] is False                                            # already failed for another reason


# ---------------- custom graders ----------------

def test_custom_function_grader_from_grader_modules(tmp_path):
    (tmp_path / "my_graders.py").write_text(textwrap.dedent('''
        from src.evals.graders import register_grader

        @register_grader("final_is_short")
        def final_is_short(transcript, task, ctx, spec):
            final = [e for e in transcript if e["type"] == "final"]
            n = len(str(final[-1]["output"])) if final else 0
            return {"passed": n <= spec.opt("max_chars", 100), "reason": f"{n} chars",
                    "evidence": [e["index"] for e in final]}
    '''))
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(textwrap.dedent('''
        suite: s
        flow: f
        grader_modules: [my_graders]
        tasks:
          - {id: a, prompt: hi, user: {replies: []}, graders: [{type: final_is_short, max_chars: 20}]}
    '''))
    agent = SimpleNamespace(tool_manager=SimpleNamespace(tool_dict={}))
    suite = load_suite(suite_path, build_flow=lambda n: (SimpleNamespace(get_agents_list=lambda: [agent]), None))
    try:
        r = asyncio.run(build_grader(suite.tasks[0].graders[0]).grade(FLOW, suite.tasks[0], ctx()))
        assert r.passed is False and r.score == 0.0 and r.reason.endswith("chars") and r.evidence == [7]
    finally:
        GRADER_REGISTRY.pop("final_is_short", None)


# ---------------- grading a stored run (re-grading without re-running) ----------------

SUITE = """
suite: s
flow: test_grading_flow
dimensions:
  politeness: {description: polite, scale: {0: rude, 1: neutral, 2: courteous}}
tasks:
  - id: refund
    prompt: hi
    user: {replies: []}
    reference: {expected: refund, key_facts: ["a refund was initiated"]}
    graders:
      - {type: state_check, check: fx.status, expect: {status: resolved}, required: true}
      - {type: tool_called, tool: initiate_refund, check: fx.limit, required: true}
      - {type: key_facts, weight: 4}
      - {type: llm_rubric, dimensions: [politeness], weight: 2}
"""

FX = """
def status(session_dir):
    raise AssertionError("state checks never run at grading time")

def limit(call, session_dir):
    raise AssertionError("state predicates never run at grading time")
"""


@pytest.fixture
def stored_run(tmp_path):
    agent = SimpleNamespace(tool_manager=SimpleNamespace(tool_dict={"initiate_refund": None}))
    register_flow("test_grading_flow", lambda: (SimpleNamespace(get_agents_list=lambda: [agent]), None))
    (tmp_path / "fx.py").write_text(FX)
    (tmp_path / "suite.yaml").write_text(SUITE)
    run_dir = tmp_path / "run"
    transcript = T(("user_message", {"content": "hi", "initial": True}),
                   call("initiate_refund", {"order_id": "1", "amount": 5}),
                   ("final", {"agent": "a", "output": "Refunded."}))

    def write_trial(n, status="completed", excluded=False, ticket="resolved"):
        d = run_dir / "refund" / f"t{n}"
        d.mkdir(parents=True)
        (d / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in transcript) + "\n")
        (d / "trial.json").write_text(json.dumps({"task": "refund", "trial": n, "status": status,
                                                  "excluded": excluded}))
        (d / "observations.json").write_text(json.dumps({
            "state_checks": {"fx.status": {"ok": True, "value": {"status": ticket}}},
            "tool_checks": {"initiate_refund:fx.limit": {"1": {"ok": True, "value": True}}}}))
    write_trial(1)
    write_trial(2, ticket="open")
    write_trial(3, status="infra_error", excluded=True)
    (run_dir / "manifest.json").write_text(json.dumps({"suite": {"path": str(tmp_path / "suite.yaml"),
                                                                 "hash": "x"}}))
    yield tmp_path, run_dir
    FLOW_REGISTRY.pop("test_grading_flow", None)


def judge_for_run():
    return FakeJudge(*[facts_reply("present"), rubric_reply(politeness=2)] * 2)


def test_grade_run_writes_grades_for_each_trial(stored_run):
    tmp_path, run_dir = stored_run
    suite = load_suite(tmp_path / "suite.yaml")
    judge = judge_for_run()
    summary = asyncio.run(grade_run(run_dir, suite, lambda: judge, log=lambda m: None))
    assert (summary["passed"], summary["failed"], summary["excluded"], summary["judge_calls"]) == (1, 1, 1, 4)

    g1 = json.loads((run_dir / "refund/t1/grades.json").read_text())
    assert g1["verdict"]["passed"] is True and g1["verdict"]["score"] == 1.0
    assert [g["id"] for g in g1["graders"]] == ["state_check:fx.status", "tool_called:initiate_refund",
                                                "key_facts", "llm_rubric:politeness"]   # in suite order
    assert g1["judge"]["calls"] == 2
    g2 = json.loads((run_dir / "refund/t2/grades.json").read_text())
    assert reasons(g2["verdict"]) == [("required_failed", "state_check:fx.status")]
    g3 = json.loads((run_dir / "refund/t3/grades.json").read_text())
    assert g3["excluded"] is True and g3["graders"] == [] and g3["verdict"]["passed"] is None
    assert json.loads((run_dir / "manifest.json").read_text())["grading"]["passed"] == 1


def test_regrading_uses_the_current_suite_without_rerunning(stored_run):
    tmp_path, run_dir = stored_run
    asyncio.run(grade_run(run_dir, load_suite(tmp_path / "suite.yaml"), lambda: judge_for_run(), log=lambda m: None))
    # the author decides "open" is the right final state: re-grade the same stored run
    (tmp_path / "suite.yaml").write_text(SUITE.replace("expect: {status: resolved}", "expect: {status: open}"))
    asyncio.run(grade_run(run_dir, load_suite(tmp_path / "suite.yaml"), lambda: judge_for_run(), log=lambda m: None))
    g1 = json.loads((run_dir / "refund/t1/grades.json").read_text())
    g2 = json.loads((run_dir / "refund/t2/grades.json").read_text())
    assert g1["verdict"]["passed"] is False and g2["verdict"]["passed"] is True


def test_cli_grade_reads_the_suite_from_the_manifest(stored_run, capsys, monkeypatch):
    from src.evals import cli
    tmp_path, run_dir = stored_run
    monkeypatch.setattr("src.evals.config.ModelConfig.build", lambda self, temperature=0.0: judge_for_run())
    (run_dir / "manifest.json").write_text(json.dumps({
        "suite": {"path": str(tmp_path / "suite.yaml"), "hash": "old"},
        "judge": {"backend": "ollama", "model_name": "judge-m", "base_url": None, "api_key_env": None}}))
    assert cli.main(["grade", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert "changed since the run" in out and "judge ollama:judge-m" in out and "1 passed, 1 failed" in out
    assert cli.main(["grade", str(tmp_path / "nowhere")]) == 2
