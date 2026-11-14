"""
Eval client: a third transport next to the SSE server and the TUI. Like them it
creates a channel, submits the session through AsyncAgentManager and talks to it
only through channel.send_to_agent / receive_from_agent.

One call of run_trial_once() is one attempt of one trial:
  1. run the task's setup hook with the session dir
  2. submit the session with a recording tracer and send the prompt
  3. answer each "response" (clarification) from the simulated user
  4. enforce timeout_s, max_llm_calls and max_clarifications live; on a breach,
     cancel the session with agent_manager.cancel
  5. read until ChannelClosed, so the session has saved its history
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.core.trace import RecordingTracer
from src.runtime.channel import AsyncChannel, ChannelClosed
from src.runtime.run_context import AgentRunContext
from src.utils.llm import LLMError

logger = logging.getLogger("agentflow.evals")

STATUSES = ("completed", "timeout", "limit_exceeded", "agent_error", "infra_error")
DRAIN_TIMEOUT = 30.0   # after a cancel, how long to wait for the session to save and close


class LimitTracer(RecordingTracer):
    """Records the trace and calls on_breach once when the LLM-call budget is exceeded."""

    def __init__(self, max_llm_calls: int, on_breach: Callable[[str], None]):
        super().__init__()
        self.max_llm_calls = max_llm_calls
        self.llm_calls = 0
        self._on_breach = on_breach
        self._breached = False

    def emit(self, event_type: str, **fields: Any) -> None:
        super().emit(event_type, **fields)
        if event_type == "llm_call":
            self.llm_calls += 1
            if self.llm_calls > self.max_llm_calls and not self._breached:
                self._breached = True
                self._on_breach(f"max_llm_calls: {self.llm_calls} LLM calls, limit {self.max_llm_calls}")


SIMULATOR_PROMPT = """You are role-playing a user who is talking to an AI assistant. Stay in character.

Persona: {persona}

The ONLY things you know (your facts):
{facts}

Your opening message was:
{prompt}

Conversation so far:
{conversation}

The assistant now asks:
{question}

Rules:
- Answer only from your facts. If the question asks for something your facts do not cover, say you do not know.
- Never invent details (names, numbers, dates) that are not in your facts.
- Reply in one or two short sentences, in character. Output only your reply, nothing else."""


@dataclass
class UserDriver:
    """
    Decides each reply to a clarification.

    Replies come from the task's source (the scripted list in order, or the
    simulator) up to max_clarifications. When the source is used up or the cap
    is reached, the on_exhausted reply is sent once ("default"); a further
    clarification ends the trial as limit_exceeded (reply() returns None).
    """
    mode: str
    replies: List[str]
    on_exhausted: str
    max_clarifications: int
    prompt: str = ""
    persona: Optional[str] = None
    facts: List[str] = field(default_factory=list)
    simulator: Any = None                        # an LLM-like object with invoke() and last_usage
    sent: int = 0
    from_source: int = 0
    default_sent: bool = False
    default_reply_at: List[int] = field(default_factory=list)
    sim_calls: int = 0
    sim_tokens_in: int = 0
    sim_tokens_out: int = 0
    conversation: List[tuple] = field(default_factory=list)

    def _source_available(self) -> bool:
        if self.from_source >= self.max_clarifications:
            return False
        return self.mode == "llm" or self.from_source < len(self.replies)

    async def reply(self, question: str) -> Optional[str]:
        self.conversation.append(("assistant", question))
        if self.default_sent:
            return None
        if self._source_available():
            text = self.replies[self.from_source] if self.mode == "scripted" else await self._simulate(question)
            self.from_source += 1
        else:
            text = self.on_exhausted
            self.default_sent = True
            self.default_reply_at.append(self.sent)
        self.sent += 1
        self.conversation.append(("user", text))
        return text

    async def _simulate(self, question: str) -> str:
        prompt = SIMULATOR_PROMPT.format(
            persona=self.persona,
            facts="\n".join(f"- {f}" for f in self.facts),
            prompt=self.prompt,
            conversation="\n".join(f"{role}: {text}" for role, text in self.conversation[:-1]) or "(none)",
            question=question,
        )
        self.sim_calls += 1
        text = await self.simulator.invoke(prompt)
        usage = getattr(self.simulator, "last_usage", None) or {}
        self.sim_tokens_in += usage.get("tokens_in") or 0
        self.sim_tokens_out += usage.get("tokens_out") or 0
        return (text or "").strip() or "I don't know."

    def summary(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "replies": self.sent, "from_source": self.from_source,
            "default_used": self.default_sent, "default_reply_at": self.default_reply_at,
            "sim_calls": self.sim_calls, "sim_tokens_in": self.sim_tokens_in, "sim_tokens_out": self.sim_tokens_out,
        }


@dataclass
class AttemptOutcome:
    status: str
    reason: Optional[str]
    session_id: str
    events: List[Dict[str, Any]]
    user: Dict[str, Any]
    started_at: float
    finished_at: float
    close_reason: Optional[str] = None


def status_from_close(closed: ChannelClosed, events: List[Dict[str, Any]]) -> tuple:
    """Status of a session that ended on its own (no breach, no cancel by the harness)."""
    if closed.reason == "done":
        errors = [e for e in events if e["type"] == "error"]
        if errors:
            return "agent_error", f"agent error: {errors[-1].get('error')}"
        if any(e["type"] == "final" for e in events):
            return "completed", None
        return "agent_error", "flow ended without a final answer"
    if closed.reason == "error":
        message = closed.message or ""
        if message.startswith(LLMError.__name__):
            return "infra_error", f"LLM provider failure ({message})"
        return "agent_error", f"flow raised {message or 'an exception'}"
    return "infra_error", f"session shut down unexpectedly ({closed.reason})"


async def run_trial_once(*, manager, task, flow_name: str, session_id: str, session_dir: Path,
                         setup: Optional[Callable], simulator=None) -> AttemptOutcome:
    loop = asyncio.get_running_loop()
    started = time.time()
    breach = asyncio.Event()
    breach_reason: List[str] = []

    def on_breach(reason: str) -> None:
        # called on the agent loop from inside the session: hand it to this loop
        breach_reason.append(reason)
        loop.call_soon_threadsafe(breach.set)

    tracer = LimitTracer(task.limits.max_llm_calls, on_breach)
    driver = UserDriver(mode=task.user.mode, replies=task.user.replies, on_exhausted=task.user.on_exhausted,
                        max_clarifications=task.user.max_clarifications, prompt=task.prompt,
                        persona=task.user.persona, facts=task.user.facts, simulator=simulator)

    def outcome(status, reason, close_reason=None):
        return AttemptOutcome(status=status, reason=reason, session_id=session_id, events=tracer.events,
                              user=driver.summary(), started_at=started, finished_at=time.time(),
                              close_reason=close_reason)

    if setup is not None:
        try:
            await asyncio.to_thread(setup, session_dir)
        except Exception as e:
            return outcome("infra_error", f"setup hook failed: {type(e).__name__}: {e}")

    channel = AsyncChannel()
    manager.set_client_event_loop(channel, loop)
    run_ctx = AgentRunContext(session_id=session_id, channel=channel, flow_name=flow_name, tracer=tracer)

    async def converse() -> tuple:
        """Returns ("closed", ChannelClosed) or ("limit", reason)."""
        await channel.send_to_agent(task.prompt)
        while True:
            try:
                msg = await channel.receive_from_agent()
            except ChannelClosed as closed:
                return "closed", closed
            if msg.get("message_type") == "response":
                text = await driver.reply(str(msg.get("content", "")))
                if text is None:
                    return "limit", (f"max_clarifications: clarification after the default reply "
                                     f"({driver.sent} replies sent)")
                await channel.send_to_agent(text)

    async def cancel_and_drain() -> Optional[str]:
        try:
            await asyncio.wait_for(asyncio.wrap_future(manager.cancel(session_id)), DRAIN_TIMEOUT)
        except Exception:
            logger.exception("session %s: cancel failed", session_id)
        try:
            async with asyncio.timeout(DRAIN_TIMEOUT):
                while True:
                    await channel.receive_from_agent()
        except ChannelClosed as closed:
            return closed.reason
        except TimeoutError:
            return None

    try:
        await asyncio.wrap_future(manager.submit(run_ctx))
    except Exception as e:
        return outcome("infra_error", f"could not start the session: {type(e).__name__}: {e}")

    conv = asyncio.create_task(converse())
    breach_wait = asyncio.create_task(breach.wait())
    done, _ = await asyncio.wait({conv, breach_wait}, timeout=task.limits.timeout_s,
                                 return_when=asyncio.FIRST_COMPLETED)
    breach_wait.cancel()

    if conv in done and not conv.cancelled() and conv.exception() is None and not breach.is_set():
        kind, value = conv.result()
        if kind == "closed":
            status, reason = status_from_close(value, tracer.events)
            return outcome(status, reason, value.reason)
        status, reason = "limit_exceeded", value
    elif conv in done and conv.exception() is not None:
        exc = conv.exception()
        status = "infra_error"
        what = "user simulator" if isinstance(exc, LLMError) else "eval client"
        reason = f"{what} failed: {type(exc).__name__}: {exc}"
    elif breach.is_set():
        status, reason = "limit_exceeded", breach_reason[0]
    else:
        status, reason = "timeout", f"timeout_s: no result after {task.limits.timeout_s}s"

    if not conv.done():
        conv.cancel()
        try:
            await conv
        except BaseException:
            pass
    close_reason = await cancel_and_drain()
    return outcome(status, reason, close_reason)
