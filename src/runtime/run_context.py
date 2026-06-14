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

    await flow.run(session_ctx, flow_ctx, initial_edge=initial_edge)
