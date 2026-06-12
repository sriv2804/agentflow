import asyncio
import threading
from typing import Any, Dict, Optional
from enum import Enum


class Turn(Enum):
    """
    Defines which side currently has the authority to send data.
    
    AGENT: Agent may send to client (streaming or response).
    CLIENT: Client may send to agent (user input).
    
    Agent-side streaming (info messages) does not transfer the turn.
    Turn transfers to CLIENT only when agent sends a "response" typed message,
    and transfers back to AGENT when client sends a message via send_to_agent.
    """
    AGENT = "agent"
    CLIENT = "client"


class ChannelClosed(Exception):
    """
    Raised when attempting to use a channel whose target event loop
    is not running or has not been registered.
    """


class AsyncChannel:
    """
    Thread-safe bidirectional channel for communication between the
    FastAPI server thread (Thread 1) and the agent runtime thread (Thread 2).
    
    Transport agnostic — works with SSE, WebSocket, terminal, or A2A.
    The transport is responsible for bridging its I/O to send_to_agent()
    and receive_from_agent(). The agent loop never touches transport code.

    Queue design:
        client_in_q (maxsize=1000): agent → client. Large buffer allows the
            agent to stream many tokens/events without blocking.
        agent_in_q (maxsize=1): client → agent. Size 1 enforces turn-taking —
            the agent processes one client input at a time.

    Turn protocol:
        - Turn starts as CLIENT (agent loop not yet running).
        - CLIENT turn: only send_to_agent may proceed.
        - AGENT turn: only send_to_client may proceed.
        - "info" messages do not transfer the turn (agent keeps streaming).
        - "response" messages transfer turn to CLIENT. Caller MUST follow
          with receive_from_client() — this is an atomic pair by convention.

    HITL is implicit: when the agent calls receive_from_client(), the agent
    coroutine suspends at the await. The server thread remains free to handle
    other sessions. No special framework handling required.
    """

    def __init__(self, max_q_size: int = 1000):
        self.turn: Turn = Turn.CLIENT
        self.in_progress: bool = False
        self.turn_lock: threading.Lock = threading.Lock()

        self.client_in_q: asyncio.Queue = asyncio.Queue(maxsize=max_q_size)
        self.agent_in_q: asyncio.Queue = asyncio.Queue(maxsize=1)

        self.client_loop: Optional[asyncio.AbstractEventLoop] = None
        self.agent_loop: Optional[asyncio.AbstractEventLoop] = None

    async def _put_with_timeout(
        self,
        queue: asyncio.Queue,
        msg: Any,
        timeout: int = 5
    ) -> None:
        """
        Puts a message onto a queue with a timeout.
        Raises TimeoutError if the queue does not accept the message in time.
        """
        try:
            await asyncio.wait_for(queue.put(msg), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timed out after {timeout}s waiting to put message on queue")

    # =======================
    # Client -> Agent (Thread 1 -> Thread 2)
    # =======================

    async def send_to_agent(self, msg: Any, timeout: int = 5) -> bool:
        """
        Sends a message from the client (server thread) to the agent (agent thread).

        Thread-safe. Checks that it is currently the CLIENT's turn and that no
        other send is in progress before submitting. Uses in_progress to guard
        against duplicate sends during the async put operation.

        Sets turn to AGENT on success — agent resumes from receive_from_client()
        and holds the turn until it sends a "response" message back.

        Returns:
            True if the message was delivered successfully.
            False if it is not the client's turn or a send is already in progress.

        Raises:
            ChannelClosed: if the agent event loop is not running.
            TimeoutError: if the queue put times out.
        """
        if not self.agent_loop or not self.agent_loop.is_running():
            raise ChannelClosed("Agent loop is not running")

        with self.turn_lock:
            if self.turn != Turn.CLIENT or self.in_progress:
                return False
            self.in_progress = True

        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._put_with_timeout(self.agent_in_q, msg, timeout),
                self.agent_loop,
            )
            await asyncio.wrap_future(fut)
        except Exception:
            with self.turn_lock:
                self.in_progress = False
            raise

        with self.turn_lock:
            self.turn = Turn.AGENT
            self.in_progress = False

        return True

    async def receive_from_agent(self) -> Any:
        """
        Receives a message sent by the agent to the client.
        Called by the server thread to drain client_in_q for SSE streaming.
        Blocks until a message is available.
        """
        return await self.client_in_q.get()

    # =======================
    # Agent -> Client (Thread 2 -> Thread 1)
    # =======================

    async def send_to_client(self, msg: Dict, timeout: int = 5) -> bool:
        """
        Sends a message from the agent (agent thread) to the client (server thread).

        Two message types:
            "info"     — streaming/thinking output. Turn is unchanged; agent
                         continues executing after this call.
            "response" — final output or clarification request. Turn flips to
                         CLIENT after a successful send. Caller MUST follow this
                         with receive_from_client() to suspend the agent coroutine
                         until the client responds. These two calls are an
                         atomic pair by convention.

        Returns:
            True if the message was delivered successfully.
            False if it is not the agent's turn.

        Raises:
            ChannelClosed: if the client event loop is not running.
            TimeoutError: if the queue put times out.
        """
        if not self.client_loop or not self.client_loop.is_running():
            raise ChannelClosed("Client loop is not running")

        with self.turn_lock:
            if self.turn != Turn.AGENT:
                return False

        message_type = msg.get("message_type", "info")

        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._put_with_timeout(self.client_in_q, msg, timeout),
                self.client_loop,
            )
            await asyncio.wrap_future(fut)
        except Exception:
            return False

        if message_type == "response":
            with self.turn_lock:
                self.turn = Turn.CLIENT

        return True

    async def receive_from_client(self) -> Any:
        """
        Receives a message sent by the client to the agent.
        Called from within the agent coroutine (agent thread).

        Suspends the agent coroutine until the client sends a message via
        send_to_agent(). This is the mechanism through which HITL pause/resume
        is implemented — no special framework handling required.
        """
        return await self.agent_in_q.get()
    