import asyncio
from dataclasses import dataclass
from pathlib import Path
from src.runtime.channel import AsyncChannel
from src.core.session import SessionContext
from src.core.flow import Edge
from src.flows.registry import get_flow

@dataclass
class AgentRunContext:
    session_id: str
    channel: AsyncChannel
    flow_name: str = "qa_agent"

async def run_on_channel(run_ctx: AgentRunContext) -> None:
    """
    Generic entry point for all agent flows.
    Creates SessionContext, waits for first client message, then runs the flow.
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

    try:
        await flow.run(session_ctx, flow_ctx, initial_edge=initial_edge)
    finally:
        # graceful end (flow finished, errored or was cancelled): persist each
        # agent's conversation history so the session can be resumed later
        await save_session_history(session_ctx)


async def save_session_history(session_ctx: SessionContext) -> None:
    for agent_ctx in session_ctx.agents.values():
        memory_manager = agent_ctx.memory_manager
        if memory_manager and memory_manager.history_path:
            await asyncio.to_thread(memory_manager.save_history, memory_manager.history_path)
