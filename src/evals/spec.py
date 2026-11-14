"""
Suite spec: YAML, validated at load time, before any LLM call.

    suite: <name>
    flow: <registered flow name>
    base_dir: <dir>          # optional: where setup/check modules and context paths
                             # resolve; relative to the suite file, default its directory
    defaults: {trials, pass_threshold, limits: {timeout_s, max_llm_calls},
               user: {on_exhausted, max_clarifications}}
    dimensions: {<name>: {description, scale: {0: str, 1: str, 2: str}, needs: [<context key>]}}
    tasks: [{id, setup, prompt, user, reference, graders, trials?, pass_threshold?,
             limits?, ...}]

load_suite() returns a Suite with every task's effective settings resolved
(task overrides on top of defaults) and raises SpecError listing every problem.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9_\-]*$"   # ids end up in paths and session ids
CONTEXT_FILE_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".html")

# grader types known to the spec; PR 3 adds the implementations and a registry for custom ones
BUILTIN_GRADER_TYPES = {
    "state_check", "tool_called", "tool_not_called", "handoff", "clarification_asked",
    "text_match", "max_steps", "key_facts", "llm_rubric",
}
GRADERS_NAMING_A_TOOL = {"tool_called", "tool_not_called"}
GRADERS_WITH_CHECK = {"state_check", "tool_called"}


class SpecError(Exception):
    """Every problem found in a suite, one per line."""

    def __init__(self, path: str, errors: List[str]):
        self.path = path
        self.errors = errors
        super().__init__(f"invalid suite {path}:\n" + "\n".join(f"  - {e}" for e in errors))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(_Strict):
    timeout_s: float = Field(300, gt=0)
    max_llm_calls: int = Field(40, ge=1)


class LimitsOverride(_Strict):
    timeout_s: Optional[float] = Field(None, gt=0)
    max_llm_calls: Optional[int] = Field(None, ge=1)


class UserDefaults(_Strict):
    on_exhausted: str = "No more clarification can be provided, proceed on your own."
    max_clarifications: int = Field(4, ge=0)


class Defaults(_Strict):
    trials: int = Field(3, ge=1)
    pass_threshold: float = Field(0.7, ge=0, le=1)
    limits: Limits = Field(default_factory=Limits)
    user: UserDefaults = Field(default_factory=UserDefaults)


class Dimension(_Strict):
    description: str
    scale: Dict[int, str]
    needs: List[str] = Field(default_factory=list)

    @field_validator("scale")
    @classmethod
    def _scale_not_empty(cls, v):
        if not v:
            raise ValueError("scale needs at least one level")
        return v


class TaskUser(_Strict):
    mode: Literal["scripted", "llm"] = "scripted"
    replies: Optional[List[str]] = None   # scripted: required (may be empty)
    persona: Optional[str] = None         # llm
    facts: Optional[List[str]] = None     # llm
    on_exhausted: Optional[str] = None
    max_clarifications: Optional[int] = Field(None, ge=0)


class Reference(_Strict):
    expected: str = ""
    key_facts: List[str] = Field(default_factory=list)
    context: Dict[str, str] = Field(default_factory=dict)


class GraderSpec(BaseModel):
    """Common grader config; type-specific keys are kept as extra fields."""
    model_config = ConfigDict(extra="allow")
    type: str
    required: bool = False
    weight: int = 1
    min_score: Optional[float] = Field(None, ge=0, le=1)

    @field_validator("weight")
    @classmethod
    def _weight_range(cls, v):
        if not 1 <= v <= 5:
            raise ValueError(f"weight must be between 1 and 5, got {v}")
        return v

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(key, default)


class TaskSpec(_Strict):
    id: str = Field(pattern=NAME_RE)
    setup: Optional[str] = None
    prompt: str
    user: TaskUser = Field(default_factory=TaskUser)
    reference: Reference = Field(default_factory=Reference)
    graders: List[GraderSpec] = Field(default_factory=list)
    trials: Optional[int] = Field(None, ge=1)
    pass_threshold: Optional[float] = Field(None, ge=0, le=1)
    limits: Optional[LimitsOverride] = None


class SuiteSpec(_Strict):
    suite: str = Field(pattern=NAME_RE)
    flow: str
    base_dir: Optional[str] = None
    defaults: Defaults = Field(default_factory=Defaults)
    dimensions: Dict[str, Dimension] = Field(default_factory=dict)
    tasks: List[TaskSpec] = Field(min_length=1)


# ---------- resolved form used by the run and grade phases ----------

@dataclass
class UserSettings:
    mode: str
    replies: List[str]
    persona: Optional[str]
    facts: List[str]
    on_exhausted: str
    max_clarifications: int


@dataclass
class Task:
    id: str
    prompt: str
    setup: Optional[str]
    user: UserSettings
    reference: Reference
    context: Dict[str, str]          # context key -> resolved text
    graders: List[GraderSpec]
    trials: int
    pass_threshold: float
    limits: Limits


@dataclass
class Suite:
    name: str
    flow: str
    path: Path
    base_dir: Path
    hash: str
    dimensions: Dict[str, Dimension]
    tasks: List[Task]
    raw: Dict[str, Any] = field(default_factory=dict)

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def load_callable(self, dotted: str) -> Callable:
        return load_callable(dotted, self.base_dir)


# ---------- helpers ----------

def load_callable(dotted: str, base_dir: Path) -> Callable:
    """
    "pkg.mod.func": first looked up as a file under base_dir (pkg/mod.py or
    pkg/mod/__init__.py), loaded under a private module name so suite fixtures
    never clash with installed packages; otherwise imported normally.
    """
    if "." not in dotted:
        raise ImportError(f"{dotted!r} is not of the form module.function")
    module_path, func_name = dotted.rsplit(".", 1)
    module = _load_module_from_dir(module_path, base_dir)
    if module is None:
        module = importlib.import_module(module_path)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise ImportError(f"{module_path} has no callable {func_name!r}")
    return func


def _load_module_from_dir(module_path: str, base_dir: Path):
    rel = Path(*module_path.split("."))
    for candidate in (base_dir / rel.with_suffix(".py"), base_dir / rel / "__init__.py"):
        if candidate.is_file():
            key = "agentflow_eval_fixture_" + hashlib.sha1(str(candidate.resolve()).encode()).hexdigest()[:12]
            if key in sys.modules:
                return sys.modules[key]
            spec = importlib.util.spec_from_file_location(key, candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(key, None)
                raise
            return module
    return None


def resolve_context(value: str, base_dir: Path) -> str:
    """A context value is a path (relative to base_dir) when it names a file, else inline text."""
    looks_like_path = "\n" not in value and value.strip().lower().endswith(CONTEXT_FILE_SUFFIXES)
    path = (base_dir / value) if not Path(value).is_absolute() else Path(value)
    if looks_like_path:
        if not path.is_file():
            raise FileNotFoundError(f"context file not found: {path}")
        return path.read_text(encoding="utf-8")
    return value


def flow_tool_names(flow) -> set:
    names = set()
    for agent in flow.get_agents_list():
        names.update(agent.tool_manager.tool_dict.keys())
    return names


def _format_pydantic(err: ValidationError) -> List[str]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        out.append(f"{loc}: {e['msg']}")
    return out


def _task_label(i: int, raw_task: Any) -> str:
    tid = raw_task.get("id") if isinstance(raw_task, dict) else None
    return f"tasks[{i}]" + (f" ({tid})" if tid else "")


# ---------- loading ----------

def load_suite(path: str | Path, build_flow: Optional[Callable[[str], Any]] = None) -> Suite:
    """
    Parse and validate a suite. build_flow(name) -> (flow, flow_ctx) defaults to
    the flow registry; it is used to check that the flow exists and that graders
    only name tools it has (building a flow makes no LLM call).
    """
    path = Path(path)
    raw_bytes = path.read_bytes()
    try:
        raw = yaml.safe_load(raw_bytes) or {}
    except yaml.YAMLError as e:
        raise SpecError(str(path), [f"not valid YAML: {e}"])
    if not isinstance(raw, dict):
        raise SpecError(str(path), ["the suite must be a YAML mapping"])

    try:
        spec = SuiteSpec.model_validate(raw)
    except ValidationError as e:
        errors = []
        for line in _format_pydantic(e):
            m = re.match(r"tasks\.(\d+)\.(.*)", line)
            if m and isinstance(raw.get("tasks"), list) and int(m.group(1)) < len(raw["tasks"]):
                i = int(m.group(1))
                line = f"{_task_label(i, raw['tasks'][i])}.{m.group(2)}"
            errors.append(line)
        raise SpecError(str(path), errors)

    base_dir = (path.parent / spec.base_dir).resolve() if spec.base_dir else path.parent.resolve()
    errors: List[str] = []

    # flow and its tools
    tools: Optional[set] = None
    if build_flow is None:
        from src.flows.registry import FLOW_REGISTRY, get_flow
        if spec.flow not in FLOW_REGISTRY:
            errors.append(f"flow: unknown flow {spec.flow!r} (registered: {sorted(FLOW_REGISTRY)})")
            build_flow = None
        else:
            build_flow = get_flow
    if build_flow is not None:
        try:
            flow, _ = build_flow(spec.flow)
            tools = flow_tool_names(flow)
        except ValueError as e:
            errors.append(f"flow: {e}")
        except Exception as e:
            errors.append(f"flow: building {spec.flow!r} failed: {type(e).__name__}: {e}")

    seen_ids = set()
    tasks: List[Task] = []
    d = spec.defaults
    for t in spec.tasks:
        label = f"task {t.id}"
        if t.id in seen_ids:
            errors.append(f"{label}: duplicate task id")
        seen_ids.add(t.id)

        # user
        u = t.user
        if u.mode == "scripted" and u.replies is None:
            errors.append(f"{label}: user.mode is scripted but no replies are given (use replies: [] for none)")
        if u.mode == "llm" and (not u.persona or not u.facts):
            errors.append(f"{label}: user.mode is llm but persona and facts are both required")

        # setup hook
        if t.setup:
            try:
                load_callable(t.setup, base_dir)
            except Exception as e:
                errors.append(f"{label}: setup {t.setup!r} cannot be imported: {type(e).__name__}: {e}")

        # context
        context: Dict[str, str] = {}
        for key, value in t.reference.context.items():
            try:
                context[key] = resolve_context(value, base_dir)
            except Exception as e:
                errors.append(f"{label}: reference.context.{key}: {e}")

        # graders
        for gi, g in enumerate(t.graders):
            glabel = f"{label}: graders[{gi}] ({g.type})"
            if g.type not in BUILTIN_GRADER_TYPES and not _is_custom_grader(g.type):
                errors.append(f"{glabel}: unknown grader type")
            if g.type in GRADERS_NAMING_A_TOOL:
                tool = g.opt("tool")
                if not tool:
                    errors.append(f"{glabel}: 'tool' is required")
                elif tools is not None and tool not in tools:
                    errors.append(f"{glabel}: tool {tool!r} is not in flow {spec.flow!r} (tools: {sorted(tools)})")
            if g.type in GRADERS_WITH_CHECK and g.opt("check"):
                try:
                    load_callable(g.opt("check"), base_dir)
                except Exception as e:
                    errors.append(f"{glabel}: check {g.opt('check')!r} cannot be imported: {type(e).__name__}: {e}")
            elif g.type == "state_check":
                errors.append(f"{glabel}: 'check' is required")
            if g.type == "llm_rubric":
                dims = g.opt("dimensions") or []
                if not dims:
                    errors.append(f"{glabel}: 'dimensions' is required")
                for dim in dims:
                    if dim not in spec.dimensions:
                        errors.append(f"{glabel}: dimension {dim!r} is not declared in the suite")
                        continue
                    missing = [k for k in spec.dimensions[dim].needs if k not in t.reference.context]
                    if missing:
                        errors.append(f"{glabel}: dimension {dim!r} needs context {missing} "
                                      f"missing from reference.context")
            if g.type == "key_facts" and not t.reference.key_facts:
                errors.append(f"{glabel}: reference.key_facts is empty")

        limits = d.limits.model_copy(update={k: v for k, v in (t.limits.model_dump() if t.limits else {}).items()
                                             if v is not None})
        tasks.append(Task(
            id=t.id, prompt=t.prompt, setup=t.setup,
            user=UserSettings(
                mode=u.mode, replies=list(u.replies or []), persona=u.persona, facts=list(u.facts or []),
                on_exhausted=u.on_exhausted if u.on_exhausted is not None else d.user.on_exhausted,
                max_clarifications=(u.max_clarifications if u.max_clarifications is not None
                                    else d.user.max_clarifications),
            ),
            reference=t.reference, context=context, graders=t.graders,
            trials=t.trials or d.trials,
            pass_threshold=t.pass_threshold if t.pass_threshold is not None else d.pass_threshold,
            limits=limits,
        ))

    if errors:
        raise SpecError(str(path), errors)
    return Suite(name=spec.suite, flow=spec.flow, path=path.resolve(), base_dir=base_dir,
                 hash=hashlib.sha256(raw_bytes).hexdigest(), dimensions=spec.dimensions,
                 tasks=tasks, raw=raw)


def _is_custom_grader(type_name: str) -> bool:
    """Custom graders arrive with the grader registry (PR 3)."""
    return False
