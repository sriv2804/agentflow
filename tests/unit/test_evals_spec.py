import textwrap
from types import SimpleNamespace

import pytest

from src.evals.config import ConfigError, load_run_config
from src.evals.spec import SpecError, load_suite


def fake_flow(*tools):
    agent = SimpleNamespace(tool_manager=SimpleNamespace(tool_dict={t: None for t in tools}))
    return lambda name: (SimpleNamespace(get_agents_list=lambda: [agent]), None)


BUILD = fake_flow("initiate_refund", "lookup_order")

FIXTURES = '''
def seed(session_dir):
    pass

def status(session_dir):
    return {"status": "resolved"}
'''

BASE = """
suite: s
flow: f
dimensions:
  politeness: {description: d, scale: {0: rude, 1: ok, 2: nice}}
  policy_adherence: {description: d, scale: {0: violates, 2: consistent}, needs: [policy]}
tasks:
"""


def write_suite(tmp_path, tasks: str, base: str = BASE):
    (tmp_path / "fx.py").write_text(FIXTURES)
    (tmp_path / "policy.md").write_text("30 days")
    path = tmp_path / "suite.yaml"
    path.write_text(base + textwrap.indent(textwrap.dedent(tasks), "  "))
    return path


def errors_of(path, build=BUILD):
    with pytest.raises(SpecError) as info:
        load_suite(path, build_flow=build)
    return info.value.errors


GOOD = """
- id: a
  setup: fx.seed
  prompt: hi
  user: {mode: scripted, replies: ["x"]}
  reference: {expected: e, key_facts: [k], context: {policy: policy.md}}
  graders:
    - {type: state_check, check: fx.status, expect: {status: resolved}, required: true}
    - {type: tool_not_called, tool: initiate_refund}
    - {type: llm_rubric, dimensions: [politeness, policy_adherence], weight: 2}
"""


def test_valid_suite_resolves_defaults_overrides_and_context(tmp_path):
    path = write_suite(tmp_path, GOOD + """
- id: b
  prompt: hi
  trials: 5
  limits: {timeout_s: 10}
  user: {mode: llm, persona: p, facts: [f], max_clarifications: 1}
""")
    suite = load_suite(path, build_flow=BUILD)
    a, b = suite.tasks
    assert (a.trials, a.pass_threshold, a.limits.timeout_s, a.limits.max_llm_calls) == (3, 0.7, 300, 40)
    assert a.context == {"policy": "30 days"}
    assert a.user.on_exhausted.startswith("No more clarification")
    assert (b.trials, b.limits.timeout_s, b.limits.max_llm_calls, b.user.max_clarifications) == (5, 10, 40, 1)
    assert suite.load_callable("fx.status")(None) == {"status": "resolved"}
    assert len(suite.hash) == 64


def test_unknown_flow(tmp_path):
    path = write_suite(tmp_path, GOOD, BASE.replace("flow: f", "flow: no_such_flow"))
    errs = errors_of(path, build=None)  # the real flow registry
    assert any("unknown flow 'no_such_flow'" in e for e in errs)


def test_undeclared_dimension_and_missing_needs(tmp_path):
    path = write_suite(tmp_path, """
- id: a
  prompt: hi
  user: {replies: []}
  graders:
    - {type: llm_rubric, dimensions: [politeness, tone, policy_adherence]}
""")
    errs = errors_of(path)
    assert any("dimension 'tone' is not declared" in e for e in errs)
    assert any("dimension 'policy_adherence' needs context ['policy']" in e for e in errs)


def test_grader_naming_a_tool_the_flow_lacks(tmp_path):
    path = write_suite(tmp_path, """
- id: a
  prompt: hi
  user: {replies: []}
  graders: [{type: tool_called, tool: send_email}]
""")
    assert any("tool 'send_email' is not in flow 'f'" in e for e in errors_of(path))


def test_unimportable_setup_and_check(tmp_path):
    path = write_suite(tmp_path, """
- id: a
  setup: fx.nope
  prompt: hi
  user: {replies: []}
  graders: [{type: state_check, check: missing_module.fn, expect: {}}]
""")
    errs = errors_of(path)
    assert any("setup 'fx.nope' cannot be imported" in e for e in errs)
    assert any("check 'missing_module.fn' cannot be imported" in e for e in errs)


def test_duplicate_task_ids(tmp_path):
    path = write_suite(tmp_path, """
- {id: a, prompt: hi, user: {replies: []}}
- {id: a, prompt: again, user: {replies: []}}
""")
    assert any("task a: duplicate task id" in e for e in errors_of(path))


@pytest.mark.parametrize("weight", [0, 6])
def test_weight_outside_range(tmp_path, weight):
    path = write_suite(tmp_path, f"""
- id: a
  prompt: hi
  user: {{replies: []}}
  graders: [{{type: tool_not_called, tool: initiate_refund, weight: {weight}}}]
""")
    assert any("weight must be between 1 and 5" in e for e in errors_of(path))


def test_scripted_without_replies_but_empty_list_is_fine(tmp_path):
    assert any("scripted but no replies" in e
               for e in errors_of(write_suite(tmp_path, "- {id: a, prompt: hi}\n")))
    load_suite(write_suite(tmp_path, "- {id: a, prompt: hi, user: {replies: []}}\n"), build_flow=BUILD)


def test_llm_mode_needs_persona_and_facts(tmp_path):
    path = write_suite(tmp_path, """
- {id: a, prompt: hi, user: {mode: llm, persona: p}}
- {id: b, prompt: hi, user: {mode: llm, facts: [x]}}
""")
    errs = errors_of(path)
    assert sum("persona and facts are both required" in e for e in errs) == 2


def test_every_problem_is_reported_at_once(tmp_path):
    path = write_suite(tmp_path, """
- {id: a, prompt: hi, setup: fx.nope}
- {id: a, prompt: hi, user: {mode: llm}}
""")
    assert len(errors_of(path)) >= 3


def test_missing_context_file_and_unknown_grader_type(tmp_path):
    path = write_suite(tmp_path, """
- id: a
  prompt: hi
  user: {replies: []}
  reference: {context: {policy: nowhere.md, note: "inline text is fine"}}
  graders: [{type: vibes}]
""")
    errs = errors_of(path)
    assert any("context file not found" in e for e in errs)
    assert any("unknown grader type" in e for e in errs)


def test_structural_errors_name_the_task(tmp_path):
    path = write_suite(tmp_path, "- {id: a, prompt: hi, user: {mode: chat, replies: []}}\n")
    assert any(e.startswith("tasks[0] (a).user.mode") for e in errors_of(path))


def test_reference_suite_validates_against_the_real_flow(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_MODEL_BACKEND", "ollama")  # build the flow without needing a key
    suite = load_suite("evals/suites/support_refunds.yaml")
    assert [t.id for t in suite.tasks] == ["late_delivery_refund", "out_of_policy_refund", "missing_order_id"]
    assert "30 days" in suite.tasks[0].context["policy"]


def test_run_config(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text("judge: {backend: openai, model_name: j, base_url: http://x, api_key_env: K}\n"
                    "metadata: {label: base}\nci_min_pass_rate: 0.8\n")
    cfg = load_run_config(str(path), concurrency=2)
    assert cfg.concurrency == 2 and cfg.infra_retry == 1 and cfg.metadata == {"label": "base"}
    assert cfg.user_sim_model() == cfg.judge_model()            # simulator defaults to the judge
    assert cfg.judge_model().describe() == {"backend": "openai", "model_name": "j", "base_url": "http://x"}
    path.write_text("concurrency: 0\n")
    with pytest.raises(ConfigError):
        load_run_config(str(path))


def test_cli_reports_spec_errors_before_running(tmp_path, capsys):
    from src.evals.cli import main
    path = write_suite(tmp_path, "- {id: a, prompt: hi}\n", BASE.replace("flow: f", "flow: no_such_flow"))
    assert main(["run", str(path)]) == 2
    err = capsys.readouterr().err
    assert "unknown flow" in err and "scripted but no replies" in err
    assert main(["run", str(path), "--trials", "0"]) == 2
