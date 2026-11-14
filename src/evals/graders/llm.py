"""
LLM graders, judged by the run config's judge model at temperature 0.

The judge replies with JSON only, writes a short comment (at most 2-3 sentences)
before each verdict or score, and may
answer "undetermined" (key_facts) or score null (llm_rubric) when it cannot tell.
Unusable output is retried twice with the parse error fed back; after that the
grader's status is grader_error, which is never an agent failure.

    key_facts   {fail_on_contradiction}   one call: per fact present | absent |
                                          contradicted | undetermined; score = present / total
    llm_rubric  {dimensions: [...]}       one call: per dimension a score on its declared
                                          scale; score = mean of the scores normalised to 0..1
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from src.evals.graders.base import GraderConfigError, GraderError, Grader, register_grader
from src.utils.llm import LLMError

PARSE_RETRIES = 2
FIELD_LIMIT = 2000     # characters per rendered transcript field
FACT_VERDICTS = ("present", "absent", "contradicted", "undetermined")


# ---------- rendering the transcript for the judge ----------

def _clip(value: Any, limit: int = FIELD_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f" …[{len(text) - limit} more characters]"


def render_conversation(transcript: List[Dict[str, Any]]) -> str:
    """What the user saw: their messages, the agent's questions and its final answer."""
    lines = []
    for e in transcript:
        i, t = e["index"], e["type"]
        if t == "user_message":
            lines.append(f"[{i}] user: {_clip(e.get('content'))}")
        elif t == "clarification" and e.get("to") == "user":
            lines.append(f"[{i}] agent asks: {_clip(e.get('query'))}")
        elif t == "final":
            lines.append(f"[{i}] agent final answer: {_clip(e.get('output'))}")
        elif t == "error":
            lines.append(f"[{i}] agent stopped with an internal error: {_clip(e.get('error'))}")
    return "\n".join(lines) or "(empty)"


def render_full(transcript: List[Dict[str, Any]]) -> str:
    """Conversation plus tool calls with their outputs and handoffs between agents."""
    lines = []
    for e in transcript:
        i, t = e["index"], e["type"]
        if t == "tool_call":
            result = f"ERROR: {_clip(e.get('error'))}" if e.get("error") else _clip(e.get("output"))
            lines.append(f"[{i}] {e.get('agent')} calls tool {e.get('tool')}({_clip(e.get('args'), 500)}) -> {result}")
        elif t == "handoff" and e.get("to_agent") not in (None, "user") and e.get("from_agent") != "user":
            lines.append(f"[{i}] handoff {e.get('from_agent')} -> {e.get('to_agent')}")
        else:
            line = render_conversation([e])
            if line != "(empty)":
                lines.append(line)
    return "\n".join(lines) or "(empty)"


def final_answer(transcript) -> Optional[str]:
    finals = [e for e in transcript if e["type"] == "final"]
    return str(finals[-1].get("output")) if finals else None


# ---------- calling the judge ----------

def extract_json(text: str) -> Any:
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence:
        cleaned = fence.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}")


async def ask_judge(ctx, prompt: str, parse: Callable[[Any], Any]) -> Any:
    """One judgement: up to 1 + PARSE_RETRIES calls; raises GraderError when nothing usable came back."""
    llm = ctx.judge_llm()
    stats = ctx.judge_stats
    feedback = ""
    last_error = None
    for attempt in range(PARSE_RETRIES + 1):
        if attempt:
            stats.parse_retries += 1
        try:
            reply = await llm.invoke(prompt + feedback)
        except LLMError as e:
            raise GraderError(f"judge call failed: {e}")
        stats.calls += 1
        usage = getattr(llm, "last_usage", None) or {}
        stats.tokens_in += usage.get("tokens_in") or 0
        stats.tokens_out += usage.get("tokens_out") or 0
        try:
            return parse(extract_json(reply))
        except (ValueError, TypeError, KeyError) as e:
            last_error = str(e)
            feedback = (f"\n\nYour previous reply could not be used: {last_error}\n"
                        f"Reply again with only the JSON object in the required format.")
    raise GraderError(f"judge output unusable after {PARSE_RETRIES + 1} attempts: {last_error}")


def _evidence(value: Any, valid: set) -> List[int]:
    if not isinstance(value, list):
        return []
    return [int(v) for v in value if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) in valid]


def _comment(item: Dict[str, Any]) -> str:
    """The judge's comment; "reasoning" is accepted too, since models sometimes rename the field."""
    return str(item.get("comment") or item.get("reasoning") or "")


def one_line(text: Any, limit: int = 200) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


# ---------- key_facts ----------

KEY_FACTS_PROMPT = """You are grading a conversation between a user and an AI agent against a list of key facts.

Expected outcome (from the test author):
{expected}

Conversation (each line starts with its event index):
{conversation}

Key facts:
{facts}

For each key fact, decide whether the agent's messages state or establish it:
- "present": the conversation clearly states or establishes the fact
- "absent": the fact is not stated
- "contradicted": the conversation states something incompatible with the fact
- "undetermined": you cannot tell from the conversation

For each fact, write a short comment (at most 2-3 sentences) before its verdict.
Reply with only this JSON object, facts in the given order, and no text before or after it:
{{"facts": [{{"fact": "<the fact, copied>", "comment": "<at most 2-3 sentences>", "verdict": "present" | "absent" | "contradicted" | "undetermined", "evidence": [<event indices that support the verdict>]}}]}}"""


@register_grader("key_facts", llm=True)
class KeyFacts(Grader):
    llm = True

    def __init__(self, spec):
        super().__init__(spec)
        self.fail_on_contradiction = bool(self.opt("fail_on_contradiction", False))

    async def grade(self, transcript, task, ctx):
        facts = list(task.reference.key_facts)
        if not facts:
            return self.error("reference.key_facts is empty")
        valid = {e["index"] for e in transcript}
        prompt = KEY_FACTS_PROMPT.format(
            expected=task.reference.expected or "(not given)",
            conversation=render_conversation(transcript),
            facts="\n".join(f"{n}. {f}" for n, f in enumerate(facts, 1)),
        )

        def parse(data):
            items = data["facts"]
            if not isinstance(items, list) or len(items) != len(facts):
                raise ValueError(f"expected {len(facts)} facts, got {len(items) if isinstance(items, list) else items!r}")
            out = []
            for fact, item in zip(facts, items):
                verdict = str(item.get("verdict", "")).strip().lower()
                if verdict not in FACT_VERDICTS:
                    raise ValueError(f"verdict {item.get('verdict')!r} is not one of {list(FACT_VERDICTS)}")
                out.append({"fact": fact, "verdict": verdict, "comment": _comment(item),
                            "reason": one_line(_comment(item)),
                            "evidence": _evidence(item.get("evidence"), valid)})
            return out

        try:
            verdicts = await ask_judge(ctx, prompt, parse)
        except GraderError as e:
            return self.error(str(e))
        counts = {v: sum(1 for f in verdicts if f["verdict"] == v) for v in FACT_VERDICTS}
        score = counts["present"] / len(facts)
        contradicted = [f["fact"] for f in verdicts if f["verdict"] == "contradicted"]
        passed = None if self.spec.min_score is None else score >= self.spec.min_score
        details = {"facts": verdicts, "counts": counts, "contradicted": contradicted,
                   "fail_on_contradiction": self.fail_on_contradiction,
                   "contradiction": bool(contradicted) and self.fail_on_contradiction}
        reason = f"{counts['present']}/{len(facts)} facts present" + (
            f"; contradicted: {contradicted}" if contradicted else "")
        evidence = [i for f in verdicts for i in f["evidence"]]
        return self.result(passed, score, reason, evidence, details)


# ---------- llm_rubric ----------

RUBRIC_PROMPT = """You are grading an AI agent's behaviour in one conversation, on the dimensions below.

Expected outcome (from the test author):
{expected}
{context}
Transcript (each line starts with its event index; tool calls show their outputs):
{transcript}

Dimensions:
{dimensions}

For each dimension, write a short comment (at most 2-3 sentences) first, then give a score from that
dimension's scale. If the transcript does not let you judge a dimension, give "score": null (unable to determine).
Reply with only this JSON object, and no text before or after it:
{{"dimensions": {{{example}}}}}"""


@register_grader("llm_rubric", llm=True)
class LLMRubric(Grader):
    llm = True

    def __init__(self, spec):
        super().__init__(spec)
        dims = self.opt("dimensions")
        if not isinstance(dims, list) or not dims or not all(isinstance(d, str) for d in dims):
            raise GraderConfigError("'dimensions' must be a non-empty list of dimension names")
        self.dimensions = dims

    @property
    def id(self):
        return f"llm_rubric:{'+'.join(self.dimensions)}"

    async def grade(self, transcript, task, ctx):
        declared = ctx.suite.dimensions
        missing = [d for d in self.dimensions if d not in declared]
        if missing:
            return self.error(f"dimensions not declared in the suite: {missing}")
        dims = {name: declared[name] for name in self.dimensions}
        needed = sorted({key for d in dims.values() for key in d.needs})
        context = "".join(f"\nReference material, {key}:\n{_clip(task.context.get(key, '(missing)'), 8000)}\n"
                          for key in needed)
        valid = {e["index"] for e in transcript}
        prompt = RUBRIC_PROMPT.format(
            expected=task.reference.expected or "(not given)",
            context=context,
            transcript=render_full(transcript),
            dimensions="\n".join(
                f"- {name}: {d.description}\n  scale: " + "; ".join(f"{k} = {v}" for k, v in sorted(d.scale.items()))
                for name, d in dims.items()),
            example=", ".join(f'"{name}": {{"comment": "<at most 2-3 sentences>", "score": <{"|".join(str(k) for k in sorted(d.scale))}'
                              f' or null>, "evidence": [<event indices>]}}' for name, d in dims.items()),
        )

        def parse(data):
            got = data["dimensions"]
            if not isinstance(got, dict):
                raise ValueError("'dimensions' must be an object keyed by dimension name")
            out = {}
            for name, d in dims.items():
                if name not in got or not isinstance(got[name], dict):
                    raise ValueError(f"dimension {name!r} is missing")
                item = got[name]
                score = item.get("score")
                if isinstance(score, str) and score.strip().lstrip("-").isdigit():
                    score = int(score)
                if score is not None and (isinstance(score, bool) or score not in d.scale):
                    raise ValueError(f"{name}: score {item.get('score')!r} is not on the scale {sorted(d.scale)}")
                low, high = min(d.scale), max(d.scale)
                normalised = None if score is None else (1.0 if high == low else (score - low) / (high - low))
                out[name] = {"score": score, "normalised": normalised, "comment": _comment(item),
                             "reason": one_line(_comment(item)), "scale": {str(k): v for k, v in d.scale.items()},
                             "evidence": _evidence(item.get("evidence"), valid)}
            return out

        try:
            results = await ask_judge(ctx, prompt, parse)
        except GraderError as e:
            return self.error(str(e))
        scored = [r["normalised"] for r in results.values() if r["normalised"] is not None]
        details = {"dimensions": results, "context_keys": needed}
        if not scored:
            return self.error("the judge could not determine any dimension", details)
        score = sum(scored) / len(scored)
        passed = None if self.spec.min_score is None else score >= self.spec.min_score
        undetermined = [n for n, r in results.items() if r["score"] is None]
        reason = ", ".join(f"{n}={r['score']}" for n, r in results.items()) + (
            f" (undetermined: {undetermined})" if undetermined else "")
        evidence = [i for r in results.values() for i in r["evidence"]]
        return self.result(passed, score, reason, evidence, details)
