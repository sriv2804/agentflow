"""
Trace sink for the agent runtime.

The runtime calls tracer.emit(event_type, **fields) at fixed points (LLM calls,
tool calls, handoffs, clarifications, user messages, the final answer and the
irrecoverable-error path). Serving paths (SSE, TUI) get a NoopTracer, so they
behave exactly as before; the eval harness passes a recording tracer.

Event types emitted by the runtime:
    user_message   content, initial (True for the first message of the session)
    handoff        from_agent, action, to_agent
    llm_call       agent, latency_s, tokens_in, tokens_out, retries, parse_ok,
                   action, summary, error
    tool_call      agent, tool, args, output, error, latency_s
    clarification  agent, to, query
    final          agent, output
    error          agent, error
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agentflow.trace")


class Tracer:
    """Base trace sink. Subclasses override emit(); the base drops every event."""

    def emit(self, event_type: str, **fields: Any) -> None:
        pass


class NoopTracer(Tracer):
    """Default tracer: does nothing."""


NOOP_TRACER = NoopTracer()


class RecordingTracer(Tracer):
    """
    Keeps every event in memory, in order, with the time since the tracer was
    created. Events are emitted on the agent loop and may be read from another
    thread, hence the lock.
    """

    def __init__(self):
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []

    def emit(self, event_type: str, **fields: Any) -> None:
        with self._lock:
            event = {"index": len(self._events), "t": round(time.monotonic() - self._t0, 4),
                     "type": event_type, **fields}
            self._events.append(event)

    @property
    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)


def tracer_of(ctx: Any) -> Tracer:
    """The tracer on a SessionContext (or anything with a .tracer), NoopTracer when absent."""
    tracer = getattr(ctx, "tracer", None)
    return tracer if tracer is not None else NOOP_TRACER


def safe_emit(tracer: Optional[Tracer], event_type: str, **fields: Any) -> None:
    """Emit without ever letting a faulty tracer break the run."""
    if tracer is None:
        return
    try:
        tracer.emit(event_type, **fields)
    except Exception:
        logger.exception("tracer failed on %s event", event_type)
