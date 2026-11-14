"""
Deterministic graders. Each scores 1.0 (passed) or 0.0 (failed).

    state_check          {check, expect}               observation of check(session_dir) vs expect
    tool_called          {tool, args_equal, args_contain, check, min_calls, max_calls, include_failed}
    tool_not_called      {tool}
    handoff              {sequence_contains: [...], agents_invoked: [...]}
    clarification_asked  {min, max, to}
    text_match           {target: final | transcript, regex | contains, negate, case_sensitive}
    max_steps            {max_llm_calls}
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.evals.graders.base import GraderConfigError, Grader, register_grader
from src.evals.spec import predicate_reads_state


def _int_opt(grader: Grader, key: str, default: Optional[int] = None, minimum: int = 0) -> Optional[int]:
    value = grader.opt(key, default)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise GraderConfigError(f"{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _dict_opt(grader: Grader, key: str) -> Dict[str, Any]:
    value = grader.opt(key) or {}
    if not isinstance(value, dict):
        raise GraderConfigError(f"{key} must be a mapping")
    return value


def _list_opt(grader: Grader, key: str) -> List[Any]:
    value = grader.opt(key) or []
    if not isinstance(value, list):
        raise GraderConfigError(f"{key} must be a list")
    return value


def _passfail(grader: Grader, ok: bool, reason: str, evidence=None, details=None):
    return grader.result(ok, 1.0 if ok else 0.0, reason, evidence, details)


def tool_calls(transcript, tool: Optional[str] = None):
    return [e for e in transcript if e["type"] == "tool_call" and (tool is None or e.get("tool") == tool)]


@register_grader("state_check")
class StateCheck(Grader):
    def __init__(self, spec):
        super().__init__(spec)
        self.check = self.opt("check")
        if not self.check:
            raise GraderConfigError("'check' is required")
        self.expect = _dict_opt(self, "expect")

    @property
    def id(self):
        return f"state_check:{self.check}"

    async def grade(self, transcript, task, ctx):
        obs = (ctx.observations.get("state_checks") or {}).get(self.check)
        if obs is None:
            return self.error(f"no observation for {self.check}: the run did not record it before teardown")
        if not obs.get("ok"):
            return self.error(f"{self.check} raised: {obs.get('error')}")
        value = obs.get("value")
        if not isinstance(value, dict):
            return self.error(f"{self.check} returned {type(value).__name__}, expected a dict")
        mismatches = {k: {"expected": v, "actual": value.get(k)} for k, v in self.expect.items()
                      if value.get(k) != v}
        details = {"observed": value, "expect": self.expect, "mismatches": mismatches}
        if mismatches:
            parts = ", ".join(f"{k}={m['actual']!r} (expected {m['expected']!r})" for k, m in mismatches.items())
            return _passfail(self, False, f"state differs: {parts}", details=details)
        return _passfail(self, True, f"state matches {self.expect}", details=details)


def _contains(actual: Any, wanted: Any) -> bool:
    if isinstance(actual, str):
        return str(wanted).lower() in actual.lower()
    if isinstance(actual, list):
        return wanted in actual
    if isinstance(actual, dict) and isinstance(wanted, dict):
        return all(actual.get(k) == v for k, v in wanted.items())
    return actual == wanted


def _equal(actual: Any, wanted: Any) -> bool:
    if actual == wanted:
        return True
    # the model may pass "1182" where the suite wrote 1182 (or the reverse)
    return actual is not None and not isinstance(actual, (dict, list)) and str(actual) == str(wanted)


@register_grader("tool_called")
class ToolCalled(Grader):
    """
    Selects the calls of `tool` (successful ones unless include_failed), narrowed by
    args_equal / args_contain. Passes when the number selected is within
    min_calls (default 1) .. max_calls and `check` holds for every selected call.
    A check(call) runs here; a check(call, session_dir) ran before teardown and its
    per-call results are read from the observations.
    """

    def __init__(self, spec):
        super().__init__(spec)
        self.tool = self.opt("tool")
        if not self.tool:
            raise GraderConfigError("'tool' is required")
        self.args_equal = _dict_opt(self, "args_equal")
        self.args_contain = _dict_opt(self, "args_contain")
        self.check = self.opt("check")
        self.min_calls = _int_opt(self, "min_calls", 1)
        self.max_calls = _int_opt(self, "max_calls")
        self.include_failed = bool(self.opt("include_failed", False))
        if self.max_calls is not None and self.max_calls < self.min_calls:
            raise GraderConfigError("max_calls is below min_calls")

    @property
    def id(self):
        return f"tool_called:{self.tool}"

    def select(self, transcript):
        calls = tool_calls(transcript, self.tool)
        if not self.include_failed:
            calls = [c for c in calls if not c.get("error")]
        args = lambda c: c.get("args") or {}
        calls = [c for c in calls if all(_equal(args(c).get(k), v) for k, v in self.args_equal.items())]
        return [c for c in calls if all(_contains(args(c).get(k), v) for k, v in self.args_contain.items())]

    async def grade(self, transcript, task, ctx):
        selected = self.select(transcript)
        indices = [c["index"] for c in selected]
        n = len(selected)
        details: Dict[str, Any] = {"selected": indices, "all_calls": [c["index"] for c in tool_calls(transcript, self.tool)]}

        failing: List[int] = []
        if self.check and selected:
            try:
                fn = ctx.load_check(self.check)
            except Exception as e:
                return self.error(f"check {self.check} cannot be loaded: {e}")
            if predicate_reads_state(fn):
                per_call = (ctx.observations.get("tool_checks") or {}).get(f"{self.tool}:{self.check}")
                if per_call is None:
                    return self.error(f"no observations for {self.check}: the run did not record them before teardown")
                for c in selected:
                    obs = per_call.get(str(c["index"]))
                    if obs is None:
                        return self.error(f"no observation of {self.check} for call {c['index']}")
                    if not obs.get("ok"):
                        return self.error(f"{self.check} raised on call {c['index']}: {obs.get('error')}")
                    if not obs.get("value"):
                        failing.append(c["index"])
            else:
                for c in selected:
                    try:
                        ok = fn(c)
                    except Exception as e:
                        return self.error(f"{self.check} raised on call {c['index']}: {type(e).__name__}: {e}")
                    if not ok:
                        failing.append(c["index"])
            details["check_failed"] = failing

        too_few = n < self.min_calls
        too_many = self.max_calls is not None and n > self.max_calls
        if too_few:
            return _passfail(self, False, f"{self.tool} called {n} time(s) matching the filters, "
                                          f"at least {self.min_calls} required", indices, details)
        if too_many:
            return _passfail(self, False, f"{self.tool} called {n} times, at most {self.max_calls} allowed",
                             indices, details)
        if failing:
            return _passfail(self, False, f"{self.check} failed for call(s) {failing}", indices, details)
        return _passfail(self, True, f"{self.tool} called {n} time(s)"
                                     + (f", {self.check} holds for each" if self.check else ""), indices, details)


@register_grader("tool_not_called")
class ToolNotCalled(Grader):
    """Any attempt counts, including a call that returned an error."""

    def __init__(self, spec):
        super().__init__(spec)
        self.tool = self.opt("tool")
        if not self.tool:
            raise GraderConfigError("'tool' is required")

    @property
    def id(self):
        return f"tool_not_called:{self.tool}"

    async def grade(self, transcript, task, ctx):
        calls = [c["index"] for c in tool_calls(transcript, self.tool)]
        if calls:
            return _passfail(self, False, f"{self.tool} was called {len(calls)} time(s)", calls)
        return _passfail(self, True, f"{self.tool} was not called")


@register_grader("handoff")
class Handoff(Grader):
    """sequence_contains: agents reached by handoffs, in this order (other agents may come between).
    agents_invoked: every listed agent ran at least once."""

    def __init__(self, spec):
        super().__init__(spec)
        self.sequence = _list_opt(self, "sequence_contains")
        self.invoked = _list_opt(self, "agents_invoked")
        if not self.sequence and not self.invoked:
            raise GraderConfigError("needs sequence_contains and/or agents_invoked")

    async def grade(self, transcript, task, ctx):
        hops = [e for e in transcript if e["type"] == "handoff" and e.get("to_agent") not in (None, "user")]
        sequence = [e["to_agent"] for e in hops]
        ran = {e.get("agent") for e in transcript if e["type"] == "llm_call"} | set(sequence)
        details = {"sequence": sequence, "agents_invoked": sorted(a for a in ran if a)}
        problems = []
        if self.sequence:
            it = iter(sequence)
            if not all(any(agent == step for step in it) for agent in self.sequence):
                problems.append(f"handoff sequence {sequence} does not contain {self.sequence} in order")
        missing = [a for a in self.invoked if a not in ran]
        if missing:
            problems.append(f"agents never invoked: {missing}")
        evidence = [e["index"] for e in hops]
        if problems:
            return _passfail(self, False, "; ".join(problems), evidence, details)
        return _passfail(self, True, f"handoffs {sequence}", evidence, details)


@register_grader("clarification_asked")
class ClarificationAsked(Grader):
    """Counts clarifications addressed to `to` (default "user")."""

    def __init__(self, spec):
        super().__init__(spec)
        self.min = _int_opt(self, "min", 1)
        self.max = _int_opt(self, "max")
        self.to = self.opt("to", "user")
        if self.max is not None and self.max < self.min:
            raise GraderConfigError("max is below min")

    async def grade(self, transcript, task, ctx):
        asks = [e for e in transcript if e["type"] == "clarification" and e.get("to") == self.to]
        n = len(asks)
        evidence = [e["index"] for e in asks]
        details = {"count": n, "queries": [e.get("query") for e in asks]}
        if n < self.min:
            return _passfail(self, False, f"{n} clarification(s) asked, at least {self.min} expected", evidence, details)
        if self.max is not None and n > self.max:
            return _passfail(self, False, f"{n} clarifications asked, at most {self.max} allowed", evidence, details)
        return _passfail(self, True, f"{n} clarification(s) asked", evidence, details)


def _event_text(e) -> Optional[str]:
    t = e["type"]
    if t == "user_message":
        return str(e.get("content", ""))
    if t == "clarification":
        return str(e.get("query", ""))
    if t == "final":
        return str(e.get("output", ""))
    if t == "tool_call":
        return f"{e.get('tool')} {e.get('args')} {e.get('output')} {e.get('error') or ''}"
    return None


@register_grader("text_match")
class TextMatch(Grader):
    """target final: the final answer; transcript: every user message, clarification, tool call and the final answer."""

    def __init__(self, spec):
        super().__init__(spec)
        self.target = self.opt("target", "final")
        if self.target not in ("final", "transcript"):
            raise GraderConfigError("target must be 'final' or 'transcript'")
        self.regex = self.opt("regex")
        self.contains = self.opt("contains")
        if bool(self.regex) == bool(self.contains):
            raise GraderConfigError("give exactly one of regex or contains")
        self.negate = bool(self.opt("negate", False))
        flags = 0 if self.opt("case_sensitive", False) else re.IGNORECASE
        try:
            self.pattern = re.compile(self.regex if self.regex else re.escape(str(self.contains)), flags)
        except re.error as e:
            raise GraderConfigError(f"invalid regex: {e}")

    @property
    def id(self):
        what = self.regex or self.contains
        return f"text_match:{'!' if self.negate else ''}{str(what)[:40]}"

    async def grade(self, transcript, task, ctx):
        events = [e for e in transcript if e["type"] == "final"][-1:] if self.target == "final" else transcript
        hits = [e["index"] for e in events if (text := _event_text(e)) is not None and self.pattern.search(text)]
        found = bool(hits)
        ok = not found if self.negate else found
        what = f"/{self.regex}/" if self.regex else repr(self.contains)
        if self.target == "final" and not events:
            reason = "no final answer"
        elif found:
            reason = f"{what} found in {self.target}"
        else:
            reason = f"{what} not found in {self.target}"
        return _passfail(self, ok, reason, hits, {"negate": self.negate, "matches": hits})


@register_grader("max_steps")
class MaxSteps(Grader):
    """A post-hoc cap on agent LLM calls (separate from the live limit)."""

    def __init__(self, spec):
        super().__init__(spec)
        self.max_llm_calls = _int_opt(self, "max_llm_calls", minimum=1)
        if self.max_llm_calls is None:
            raise GraderConfigError("'max_llm_calls' is required")

    async def grade(self, transcript, task, ctx):
        n = sum(1 for e in transcript if e["type"] == "llm_call")
        ok = n <= self.max_llm_calls
        return _passfail(self, ok, f"{n} LLM calls ({'within' if ok else 'over'} {self.max_llm_calls})",
                         details={"llm_calls": n})
