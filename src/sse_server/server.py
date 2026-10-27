"""
Graceful shutdown for the SSE server.

On SIGTERM/SIGINT, AgentflowServer stops accepting chats (503), cancels every
active session on the agent loop and waits for them to finish. Cancelled flows
save their history as "interrupted" (run_on_channel's finally) and the task's
done-callback closes the channel with "shutdown", which ends each SSE stream so
uvicorn's own wait for open connections completes. The agent loop runs on a
daemon thread that dies with the process, so this wait is what makes the saves
happen.
"""
import asyncio
import logging
import os
from typing import Optional

import uvicorn

from src.sse_server.app import app, agent_manager

logger = logging.getLogger("agentflow.server")

DEFAULT_SHUTDOWN_TIMEOUT = 20.0


def shutdown_timeout() -> float:
    return float(os.environ.get("AGENTFLOW_SHUTDOWN_TIMEOUT") or DEFAULT_SHUTDOWN_TIMEOUT)


async def drain_sessions(timeout: Optional[float] = None) -> None:
    """Runs on the client loop: cancel every active session and wait for it to finish."""
    timeout = shutdown_timeout() if timeout is None else timeout
    session_ids = list(agent_manager.registry.copy())  # the agent thread mutates the registry
    if not session_ids:
        return
    logger.info("shutdown: cancelling %d active session(s)", len(session_ids))
    waits = []
    for session_id in session_ids:
        try:
            waits.append(asyncio.wrap_future(agent_manager.cancel(session_id)))
        except RuntimeError:
            logger.warning("shutdown: agent loop already stopped, cannot cancel %s", session_id)
    if not waits:
        return
    done, pending = await asyncio.wait(waits, timeout=timeout)
    if pending:
        logger.warning("shutdown: %d session(s) did not finish within %.0fs", len(pending), timeout)
    else:
        logger.info("shutdown: all sessions saved")


class AgentflowServer(uvicorn.Server):
    """uvicorn.Server that drains agent sessions before its own shutdown."""

    def __init__(self, config: uvicorn.Config):
        super().__init__(config)
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None
        self._drain_started = False

    async def serve(self, sockets=None) -> None:
        self._client_loop = asyncio.get_running_loop()
        await super().serve(sockets=sockets)

    def handle_exit(self, sig, frame) -> None:
        if not self._drain_started and self._client_loop is not None:
            self._drain_started = True
            app.state.shutting_down = True
            self._client_loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(drain_sessions())
            )
        super().handle_exit(sig, frame)
