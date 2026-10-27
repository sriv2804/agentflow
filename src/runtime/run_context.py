import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from src.runtime.channel import AsyncChannel
from src.core.session import SessionContext
from src.core.flow import Edge
from src.core.storage import configured_working_dir, get_session_status_path
from src.flows.registry import get_flow

logger = logging.getLogger("agentflow.runtime")

# bound on each cleanup step in run_on_channel's finally, so shutdown can't hang on disk I/O
CLEANUP_TIMEOUT = 5.0


@dataclass
class AgentRunContext:
    session_id: str
    channel: AsyncChannel
    flow_name: str = "qa_agent"

async def run_on_channel(run_ctx: AgentRunContext) -> None:
    """
    Generic entry point for all agent flows.
    Creates SessionContext, waits for first client message, then runs the flow.

    Ending the client's stream is not done here: the task's done-callback
    closes the channel (see scheduler.close_channel_when_done), which also
    covers a task cancelled while still waiting for the first message.
    """
    channel = run_ctx.channel

    session_ctx = SessionContext(
        session_id=run_ctx.session_id,
        channel=channel,
        logger=None,
        workspace_dir=Path(".")
    )

    first_input = await channel.receive_from_client()

    flow, flow_ctx = get_flow(run_ctx.flow_name)

    initial_edge = Edge(
        callee="user",
        call_to=flow.start_agent.agent_name,
        data=first_input
    )

    status = "completed"
    try:
        await flow.run(session_ctx, flow_ctx, initial_edge=initial_edge)
    except asyncio.CancelledError:
        status = "interrupted"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        # persist each agent's conversation history and the outcome so the
        # session can be resumed; each step is bounded and failures only logged
        try:
            await asyncio.wait_for(save_session_history(session_ctx), timeout=CLEANUP_TIMEOUT)
        except Exception:
            logger.exception("session %s: saving conversation history failed", run_ctx.session_id)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(save_session_status, run_ctx.session_id, status),
                timeout=CLEANUP_TIMEOUT,
            )
        except Exception:
            logger.exception("session %s: saving session status failed", run_ctx.session_id)


async def save_session_history(session_ctx: SessionContext) -> None:
    for agent_ctx in session_ctx.agents.values():
        memory_manager = agent_ctx.memory_manager
        if memory_manager and memory_manager.history_path:
            await asyncio.to_thread(memory_manager.save_history, memory_manager.history_path)


def save_session_status(session_id: str, status: str) -> None:
    """Blocking: writes {working_dir}/{session_id}/session_status.json atomically."""
    path = get_session_status_path(configured_working_dir(), session_id)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump({"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}, f)
    os.replace(tmp_path, path)
