import json
import re
from pathlib import Path

import pytest

from src.evals.aggregate import build_results, write_results
from src.evals.cli import ci_exit_code, main
from src.evals.compare import compare, render_compare_md
from src.evals.report import FIELD_LIMIT, embed_json, render_html, render_md, report_data

XSS = '<script>alert("pwned")</script><img src=x onerror=alert(1)>'


def ev(i, typ, **f):
    return {"index": i, "t": float(i), "type": typ, **f}


def grader(gid, typ, passed, score, required=False, weight=1, status="ok", details=None):
    return {"id": gid, "type": typ, "passed": passed, "score": score, "reason": f"{gid} reason", "evidence": [1],
            "details": details or {}, "status": status, "required": required, "weight": weight,
            "min_score": None, "llm": typ in ("key_facts", "llm_rubric")}


def make_run(root: Path, name="run_a", outcomes=None, manifest_extra=None, xss=False):
    """outcomes: {task: [(verdict, status, tool_error, politeness_score, facts...)]}"""
    outcomes = outcomes or {
        "refund": [("pass", "completed"), ("fail", "completed"), ("pass", "completed")],
        "decline": [("pass", "completed"), ("pass", "completed"), ("excluded", "infra_error")],
        "ask": [("fail", "timeout"), ("undetermined", "completed"), ("fail", "completed")],
    }
    run = root / name
    run.mkdir(parents=True)
    manifest = {"run_id": name, "started_at": "2026-11-14T10:00:00", "finished_at": "2026-11-14T10:05:00",
                "suite": {"name": "s", "path": "/x/suite.yaml", "hash": "abc"}, "flow": "f",
                "agents": {"a": {"model_name": "m1", "backend": "OllamaClient", "prompt_hash": "p1"}},
                "judge": {"backend": "ollama", "model_name": "j1"}, "user_sim": None,
                "agentflow_version": {"git_sha": "deadbeef", "dirty": False},
                "trials": {t: len(v) for t, v in outcomes.items()}, "metadata": {"label": "base"},
                "run_totals": {"agent_llm_calls": 20, "user_sim_calls": 1}, "grading": {"judge_calls": 7}}
    manifest.update(manifest_extra or {})
    (run / "manifest.json").write_text(json.dumps(manifest))
    for task, trials in outcomes.items():
        for n, (verdict, status, *rest) in enumerate(trials, 1):
            tool_error = rest[0] if rest else False
            polite = rest[1] if len(rest) > 1 else 2
            d = run / task / f"t{n}"
            d.mkdir(parents=True)
            content = XSS if xss else "please refund order 1182"
            events = [ev(0, "user_message", content=content, initial=True),
                      ev(1, "tool_call", agent="a", tool="lookup_order", args={"id": 1}, output="x" * 5000,
                         error="boom" if tool_error else None, latency_s=0.1),
                      ev(2, "tool_call", agent="a", tool="initiate_refund", args={}, output="ok", error=None, latency_s=0.1),
                      ev(3, "llm_call", agent="a", latency_s=1.0, tokens_in=100, tokens_out=10, retries=0,
                         parse_ok=True, action="yield", summary=content, error=None),
                      ev(4, "final", agent="a", output=content)]
            (d / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
            excluded = verdict == "excluded"
            (d / "trial.json").write_text(json.dumps({
                "task": task, "trial": n, "status": status, "reason": None, "excluded": excluded,
                "session_id": f"eval_s_{task}_t{n}", "duration_s": 10.0 + n,
                "counts": {"llm_calls": 1, "tokens_in": 100, "tokens_out": 10, "tool_calls": 2,
                           "tool_errors": int(tool_error), "clarifications": 1},
                "user": {"mode": "llm" if task == "ask" else "scripted", "default_used": n == 1 and task == "ask",
                         "sim_calls": 1 if task == "ask" else 0}}))
            if excluded:
                continue
            passed = {"pass": True, "fail": False, "undetermined": None}[verdict]
            reasons = []
            if verdict == "fail":
                reasons = [{"reason": "required_failed", "grader_id": "state_check:fx.status", "detail": "open"},
                           {"reason": "below_threshold", "grader_id": None, "detail": "0.3 < 0.7"}]
                if status != "completed":
                    reasons.insert(0, {"reason": "not_completed", "grader_id": None, "detail": status})
            facts = [{"fact": "refund initiated", "verdict": "present" if verdict == "pass" else "contradicted",
                      "reason": "said so", "comment": "said so", "evidence": [4]},
                     {"fact": "5 to 7 days", "verdict": "absent", "reason": "not said", "comment": "", "evidence": []}]
            graders = [
                grader("state_check:fx.status", "state_check", verdict == "pass", 1.0 if verdict == "pass" else 0.0, True,
                       details={"observed": {"status": "resolved"}, "expect": {"status": "resolved"}}),
                grader("key_facts", "key_facts", None, 0.5, weight=4, details={"facts": facts, "contradiction": False}),
                grader("llm_rubric:politeness", "llm_rubric", None, polite / 2, weight=2, details={"dimensions": {
                    "politeness": {"score": polite, "normalised": polite / 2, "reason": content, "comment": content,
                                   "scale": {"0": "rude", "1": "ok", "2": "nice"}, "evidence": [4]}}}),
            ]
            if verdict == "undetermined":
                graders[1] = grader("key_facts", "key_facts", None, None, required=True, status="grader_error")
            (d / "grades.json").write_text(json.dumps({
                "task": task, "trial": n, "status": status, "excluded": False,
                "verdict": {"passed": passed, "score": 0.9 if passed else 0.3, "failure_reasons": reasons,
                            "grader_errors": ["key_facts"] if verdict == "undetermined" else [],
                            "undetermined_by": [], "pass_threshold": 0.7},
                "graders": graders, "judge": {"calls": 2, "tokens_in": 1, "tokens_out": 1, "parse_retries": 0}}))
    return run


# ---------------- aggregation ----------------

def test_pass_at_k_pass_pow_k_and_rates(tmp_path):
    r = build_results(make_run(tmp_path))
    t = {x["id"]: x for x in r["tasks"]}
    assert (t["refund"]["passed"], t["refund"]["k"], t["refund"]["pass_at_k"], t["refund"]["pass_pow_k"]) == (2, 3, True, False)
    # the excluded infra_error trial does not count against "decline"
    assert (t["decline"]["passed"], t["decline"]["k"], t["decline"]["pass_pow_k"]) == (2, 2, True)
    # undetermined trials are left out of k
    assert (t["ask"]["passed"], t["ask"]["k"], t["ask"]["pass_at_k"], t["ask"]["pass_pow_k"]) == (0, 2, False, False)
    s = r["suite"]
    assert (s["passed"], s["decided"], s["pass_rate"]) == (4, 7, round(4 / 7, 4))
    assert (s["tasks_pass_any"], s["tasks_pass_all"]) == (2, 1)
    assert s["counts"]["excluded"] == 1 and s["counts"]["undetermined"] == 1 and s["counts"]["timeouts"] == 1
    assert s["counts"]["grader_errors"] == 1 and s["counts"]["infra_errors"] == 1
    assert t["ask"]["user_mode"] == "llm" and t["ask"]["default_reply_trials"] == 1 and t["refund"]["user_mode"] == "scripted"


def test_failure_reasons_are_counted_once_per_trial(tmp_path):
    run = make_run(tmp_path)
    # the same (reason, grader) listed twice in one trial still counts once for that trial
    g = json.loads((run / "refund/t2/grades.json").read_text())
    g["verdict"]["failure_reasons"].append(dict(g["verdict"]["failure_reasons"][0]))
    (run / "refund/t2/grades.json").write_text(json.dumps(g))
    s = build_results(run)["suite"]
    top = {(x["reason"], x["grader_id"]): x for x in s["all_failure_reasons"]}
    assert top[("required_failed", "state_check:fx.status")]["trials"] == 3      # refund t2, ask t1, ask t3
    assert top[("not_completed", None)]["trials"] == 1
    assert len(s["failure_reasons"]) == 3 and s["failure_reasons"][0]["trials"] == 3
    # ties: the direct cause ranks before below_threshold
    assert [(x["reason"], x["trials"]) for x in s["failure_reasons"]] == [
        ("required_failed", 3), ("below_threshold", 3), ("not_completed", 1)]
    assert {"task": "refund", "trial": 2} in top[("required_failed", "state_check:fx.status")]["refs"]


def test_key_facts_dimensions_and_tool_health(tmp_path):
    outcomes = {"refund": [("pass", "completed", False, 2), ("fail", "completed", True, 0)],
                "decline": [("pass", "completed", True, 1)]}
    s = build_results(make_run(tmp_path, outcomes=outcomes))["suite"]
    assert {k: v["count"] for k, v in s["key_facts"].items()} == {"present": 2, "absent": 3, "contradicted": 1,
                                                                  "undetermined": 0}
    assert s["contradicted_facts"][0]["task"] == "refund" and s["contradicted_facts"][0]["trial"] == 2
    d = s["dimensions"]["politeness"]
    assert d["mean"] == 0.5 and d["trials_scored"] == 3 and d["tasks_scored"] == 2
    assert [r["normalised"] for r in d["rows"]] == [0.0, 0.5, 1.0]                 # lowest first
    h = s["tool_health"]["lookup_order"]
    assert (h["calls"], h["errors"], h["error_rate"]) == (3, 2, round(2 / 3, 4))
    assert h["error_refs"][0]["events"] == [1]
    assert s["tool_health"]["initiate_refund"]["error_rate"] == 0.0


def test_write_results(tmp_path):
    run = make_run(tmp_path)
    write_results(run)
    assert json.loads((run / "results.json").read_text())["run_id"] == "run_a"


# ---------------- compare ----------------

def two_runs(tmp_path, a, b, **extra_b):
    return (build_results(make_run(tmp_path, "a", outcomes=a)),
            build_results(make_run(tmp_path, "b", outcomes=b, **extra_b)))


def test_one_trial_flip_is_not_flagged_but_more_is(tmp_path):
    a, b = two_runs(tmp_path,
                    {"t1": [("pass", "completed")] * 2, "t2": [("pass", "completed")] * 4},
                    {"t1": [("pass", "completed"), ("fail", "completed")],             # one flip of k=2
                     "t2": [("pass", "completed")] * 2 + [("fail", "completed")] * 2})  # two flips of k=4
    c = compare(a, b)
    t = {x["id"]: x for x in c["tasks"]}
    assert t["t1"]["pass_rate"]["delta"] == -0.5 and t["t1"]["pass_rate"]["flag"] is None
    assert t["t2"]["pass_rate"]["delta"] == -0.5 and t["t2"]["pass_rate"]["flag"] == "regression"
    assert {(f["id"], f["metric"]) for f in c["regressions"]} >= {("t2", "pass_rate")}
    assert ("t1", "pass_rate") not in {(f["id"], f["metric"]) for f in c["flagged"]}


def test_compare_dimensions_efficiency_and_manifest(tmp_path):
    a, b = two_runs(tmp_path,
                    {"t": [("pass", "completed", False, 0)] * 3},
                    {"t": [("pass", "completed", False, 2)] * 3},
                    manifest_extra={"judge": {"backend": "ollama", "model_name": "j2"}, "run_id": "b"})
    c = compare(a, b)
    d = c["dimensions"][0]
    assert (d["name"], d["delta"], d["flag"]) == ("politeness", 1.0, "improvement")
    keys = {x["key"] for x in c["manifest_diff"]}
    assert "judge.model_name" in keys and "run_id" not in keys and "started_at" not in keys
    assert c["suite"]["efficiency"]["mean_llm_calls"]["delta"] == 0
    md = render_compare_md(c)
    assert "▲ improvement" in md and "`judge.model_name`: 'j1' → 'j2'" in md


def test_compare_tasks_only_in_one_run(tmp_path):
    a, b = two_runs(tmp_path, {"old": [("pass", "completed")]}, {"new": [("pass", "completed")]})
    assert {(t["id"], t.get("only_in")) for t in compare(a, b)["tasks"]} == {("old", "a"), ("new", "b")}


# ---------------- reports ----------------

def data_block(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_report_is_self_contained_and_escapes_transcript_content(tmp_path):
    run = make_run(tmp_path, xss=True)
    results = build_results(run)
    html = render_html(run, results)
    # only the two script elements of the report itself: the JSON data block and the inline JS
    assert html.count("<script") == 2 and "alert(" not in html.split('id="report-data">')[0]
    block = html.split('id="report-data">')[1].split("</script>")[0]
    assert "<" not in block and ">" not in block                     # nothing in the data can close the tag
    assert "\\u003cscript\\u003e" in block
    data = data_block(html)
    assert data["trials"]["refund/1"]["events"][0]["content"] == XSS   # intact as data, rendered as text by the JS
    # no network: no external URLs, a CSP that blocks every fetch
    assert not re.search(r'(src|href)="(https?:)?//', html)
    assert "default-src 'none'" in html
    js = (Path("src/evals/report/assets/report.js")).read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js


def test_embedded_fields_are_truncated(tmp_path):
    run = make_run(tmp_path)
    data = report_data(run, build_results(run))
    output = data["trials"]["refund/1"]["events"][1]["output"]
    assert output.startswith("x" * FIELD_LIMIT) and "3000 more characters; full text in transcript.jsonl" in output
    assert len(json.loads((run / "refund/t1/transcript.jsonl").read_text().splitlines()[1])["output"]) == 5000


def test_every_link_target_in_the_data_resolves(tmp_path):
    """The JS builds links from these refs; each must name a trial/task/dimension present in the data
    (the browser-side check, reportSelfTest(), visits every rendered link)."""
    run = make_run(tmp_path)
    data = report_data(run, build_results(run))
    r, trials = data["results"], data["trials"]
    tasks = {t["id"] for t in r["tasks"]}
    refs = [ref for t in r["tasks"] for ref in t["refs"]]
    s = r["suite"]
    refs += [ref for x in s["all_failure_reasons"] for ref in x["refs"]]
    refs += [ref for v in s["key_facts"].values() for ref in v["refs"]]
    refs += [ref for v in s["count_refs"].values() for ref in v]
    refs += [ref for h in s["tool_health"].values() for ref in h["error_refs"]]
    refs += [{"task": row["task"], "trial": row["trial"]} for d in s["dimensions"].values() for row in d["rows"]]
    assert refs and all(f"{x['task']}/{x['trial']}" in trials and x["task"] in tasks for x in refs)
    for h in s["tool_health"].values():
        for ref in h["error_refs"]:
            indices = {e["index"] for e in trials[f"{ref['task']}/{ref['trial']}"]["events"]}
            assert set(ref["events"]) <= indices
    for t in r["tasks"]:
        for row in t["trial_rows"]:
            for g in row["graders"]:
                indices = {e["index"] for e in trials[f"{t['id']}/{row['trial']}"]["events"]}
                assert set(g["evidence"] or []) <= indices


def test_markdown_report(tmp_path):
    run = make_run(tmp_path)
    md = render_md(build_results(run))
    assert "| trial pass rate |" in md and "57% (4/7)" in md
    assert "| refund | scripted | 2/3 | ✓ yes | ✗ no |" in md
    assert "| ask | LLM-simulated (default reply in 1/3) | 0/2 |" in md
    assert "| politeness |" in md and "required_failed" in md


def test_embed_json_cannot_break_out():
    assert embed_json({"x": "</script><!--"}) == '{"x": "\\u003c/script\\u003e\\u003c!--"}'


# ---------------- CLI ----------------

def test_ci_exit_code():
    res = lambda rate: {"suite": {"pass_rate": rate}}
    assert ci_exit_code(res(0.5), None) == 0
    assert ci_exit_code(res(0.8), 0.8) == 0
    assert ci_exit_code(res(0.79), 0.8) == 1
    assert ci_exit_code(res(None), 0.5) == 1


def test_cli_report_and_compare(tmp_path, capsys):
    a = make_run(tmp_path, "a")
    b = make_run(tmp_path, "b", outcomes={"refund": [("fail", "completed")] * 3})
    assert main(["report", str(b), "--baseline", str(a)]) == 0
    assert (b / "report.html").exists() and (b / "report.md").exists() and (b / "results.json").exists()
    assert json.loads((b / "compare.json").read_text())["run_a"] == "a"
    assert "Compare with baseline" in (b / "report.md").read_text()
    assert main(["compare", str(a), str(b)]) == 0
    assert "| refund | 67% → 0% |" in capsys.readouterr().out
    assert main(["compare", str(a), str(b), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_b"] == "b"
    assert main(["report", str(tmp_path / "nope")]) == 2


TASK_DEF = {"prompt": "refund please", "setup": "fixtures.seed", "setup_doc": "Order 1182, ticket open.",
            "teardown": "fixtures.clear", "expected": "a refund is made",
            "user": {"mode": "scripted", "replies": ["Order 1182"], "on_exhausted": "proceed", "max_clarifications": 4},
            "key_facts": ["a refund was initiated"], "context": {"policy": {"source": "policy.md", "text": "30 days"}},
            "graders": [{"type": "state_check", "required": True, "weight": 1, "check": "fx.status",
                         "expect": {"status": "resolved"}}],
            "pass_threshold": 0.7, "limits": {"timeout_s": 300, "max_llm_calls": 40}}


def test_task_definition_comes_from_the_manifest(tmp_path):
    run = make_run(tmp_path, manifest_extra={"tasks": {"refund": TASK_DEF}})
    tasks = {t["id"]: t for t in build_results(run)["tasks"]}
    assert tasks["refund"]["definition"] == {**TASK_DEF, "recorded": "manifest"}
    assert tasks["decline"]["definition"] is None


def test_task_definition_falls_back_to_an_unchanged_suite(tmp_path, monkeypatch):
    import src.evals.runner as runner
    import src.evals.spec as spec
    run = make_run(tmp_path)                       # manifest without "tasks", suite hash "abc"
    suite = type("S", (), {"hash": "abc", "tasks": [type("T", (), {"id": "refund"})()]})()
    monkeypatch.setattr(spec, "load_suite", lambda path: suite)
    monkeypatch.setattr(runner, "describe_task", lambda t, s: {"prompt": "from the suite"})
    tasks = {t["id"]: t for t in build_results(run)["tasks"]}
    assert tasks["refund"]["definition"] == {"prompt": "from the suite", "recorded": "suite"}

    suite.hash = "changed"                         # the suite changed since the run: show nothing
    assert all(t["definition"] is None for t in build_results(run)["tasks"])


def test_reports_show_the_task_definition(tmp_path):
    run = make_run(tmp_path, manifest_extra={"tasks": {"refund": {**TASK_DEF, "prompt": XSS}}})
    results = build_results(run)
    md = render_md(results)
    assert "## Tasks" in md and "### refund" in md and "`fixtures.seed`: Order 1182, ticket open." in md
    assert '- **User:** scripted user; replies: "Order 1182"' in md
    assert "`state_check` (required, weight 1): check=fx.status, expect={\"status\": \"resolved\"}" in md
    html = render_html(run, results)
    assert "<script>alert" not in html             # the prompt is embedded escaped, like every field
    data = report_data(run, results)
    assert data["results"]["tasks"][0]["definition"]["prompt"] == XSS
