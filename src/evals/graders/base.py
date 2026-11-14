"""
Grader interface and registry.

    grader.grade(transcript, task, ctx) -> GradeResult

A grader reads only stored data: the transcript (trace events), and through ctx
the trial record and the observations taken before teardown. It never reads the
live session dir (it has been torn down by the time grading runs).

GradeResult: id (stable, e.g. "tool_not_called:initiate_refund"), type,
passed (bool or None), score (0..1 or None), reason, evidence (transcript event
indices), details, and status "ok" | "grader_error". A grader_error (the judge's
output stayed unusable, a check function raised, an observation is missing) is a
problem with grading, never an agent failure.

Common config on every grader: required (default false), weight (1..5, default
1), min_score (optional).

Custom graders: register a Grader subclass, or a function
fn(transcript, task, ctx, spec) -> GradeResult | dict, under a new type name with
@register_grader("name"). A suite loads the modules that register them with
`grader_modules: [module, ...]`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from src.evals.spec import GraderSpec


class GraderError(Exception):
    """Grading could not produce a result (not an agent failure)."""


class GraderConfigError(ValueError):
    """A grader's config is invalid; reported by suite validation."""


@dataclass
class GradeResult:
    id: str
    type: str
    passed: Optional[bool]
    score: Optional[float]
    reason: str = ""
    evidence: List[int] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"                 # ok | grader_error
    required: bool = False
    weight: int = 1
    min_score: Optional[float] = None
    llm: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JudgeStats:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    parse_retries: int = 0


@dataclass
class GradeContext:
    trial: Dict[str, Any]
    observations: Dict[str, Any]
    suite: Any                                     # spec.Suite
    load_check: Callable[[str], Callable]
    judge_factory: Optional[Callable[[], Any]] = None   # -> an LLM-like object (temperature 0)
    judge_stats: JudgeStats = field(default_factory=JudgeStats)
    _judge: Any = None

    def judge_llm(self):
        if self._judge is None:
            if self.judge_factory is None:
                raise GraderError("no judge model configured")
            self._judge = self.judge_factory()
        return self._judge


class Grader:
    """Base class. Subclasses set `type`, read their config in __init__ and implement grade()."""
    type: str = ""
    llm: bool = False

    def __init__(self, spec: GraderSpec):
        self.spec = spec

    def opt(self, key: str, default: Any = None) -> Any:
        return self.spec.opt(key, default)

    @property
    def id(self) -> str:
        return self.type

    def result(self, passed: Optional[bool], score: Optional[float], reason: str = "",
               evidence: Optional[List[int]] = None, details: Optional[Dict[str, Any]] = None,
               status: str = "ok") -> GradeResult:
        return GradeResult(id=self.id, type=self.type, passed=passed, score=score, reason=reason,
                           evidence=sorted(set(evidence or [])), details=details or {}, status=status,
                           required=self.spec.required, weight=self.spec.weight,
                           min_score=self.spec.min_score, llm=self.llm)

    def error(self, reason: str, details: Optional[Dict[str, Any]] = None) -> GradeResult:
        return self.result(None, None, reason, details=details, status="grader_error")

    async def grade(self, transcript: List[Dict[str, Any]], task, ctx: GradeContext) -> GradeResult:
        raise NotImplementedError


class _FunctionGrader(Grader):
    """Wraps a registered function fn(transcript, task, ctx, spec) -> GradeResult | dict."""
    fn: Callable = None

    async def grade(self, transcript, task, ctx):
        out = type(self).fn(transcript, task, ctx, self.spec)
        if hasattr(out, "__await__"):
            out = await out
        if isinstance(out, GradeResult):
            return out
        if not isinstance(out, dict):
            raise GraderError(f"custom grader {self.type!r} returned {type(out).__name__}, not a dict")
        passed, score = out.get("passed"), out.get("score")
        if score is None and isinstance(passed, bool):
            score = 1.0 if passed else 0.0
        result = self.result(passed, score, out.get("reason", ""), out.get("evidence"), out.get("details"))
        if out.get("id"):
            result.id = out["id"]
        return result


GRADER_REGISTRY: Dict[str, Type[Grader]] = {}


def register_grader(name: str, *, llm: bool = False):
    """Register a Grader subclass or a grading function under a type name."""
    def decorator(obj):
        if isinstance(obj, type) and issubclass(obj, Grader):
            cls = obj
            cls.type = name
        elif callable(obj):
            cls = type(f"Grader_{name}", (_FunctionGrader,), {"type": name, "llm": llm, "fn": staticmethod(obj)})
        else:
            raise TypeError("register_grader expects a Grader subclass or a function")
        GRADER_REGISTRY[name] = cls
        return obj
    return decorator


def build_grader(spec: GraderSpec) -> Grader:
    cls = GRADER_REGISTRY.get(spec.type)
    if cls is None:
        raise GraderConfigError(f"unknown grader type {spec.type!r}")
    return cls(spec)


def unique_ids(results: List[GradeResult]) -> None:
    """Graders with the same id within a task get "#2", "#3", ... so ids stay stable and unique."""
    seen: Dict[str, int] = {}
    for r in results:
        seen[r.id] = seen.get(r.id, 0) + 1
        if seen[r.id] > 1:
            r.id = f"{r.id}#{seen[r.id]}"
