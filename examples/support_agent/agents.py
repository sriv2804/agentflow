from pathlib import Path
from typing import Tuple

from examples.support_agent.tools import get_policy, initiate_refund, lookup_order, update_ticket
from src.core.agent import Agent
from src.core.flow import AgentsFlow, FlowContext
from src.flows.registry import register_flow

PROMPTS_DIR = Path(__file__).parent / "prompts"


def build_support_flow() -> Tuple[AgentsFlow, FlowContext]:
    support_agent = Agent(
        agent_name="support_agent",
        tool_grps=[],
        always_on_tools=[lookup_order, get_policy, initiate_refund, update_ticket],
        execution_prompt_path=PROMPTS_DIR / "support_agent.md",
        resolver="user",
    )
    flow = AgentsFlow(start_agent=support_agent, list_of_agents=[support_agent])
    flow_ctx = FlowContext(
        flow=flow,
        flow_description="Customer support flow: a single support agent answers order and refund requests "
                         "using local order, policy, refund and ticket tools.",
    )
    return flow, flow_ctx


register_flow("support_agent", build_support_flow)
