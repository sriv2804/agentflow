from typing import Tuple
from pathlib import Path
from src.core.flow import AgentsFlow, FlowContext
from src.flows.registry import register_flow
from src.core.agent import Agent
from src.tools.web_search import web_search
from src.tools.calculator import calculator
from src.tools.memory_retriever import memory_retriever


def build_qa_flow() -> Tuple[AgentsFlow, FlowContext]:
    # stub — will be implemented in Week 3 with real agents
    orchestrator = Agent(
        agent_name = "orchestrator",
        model_name = "gpt-4o-mini",
        tools = [web_search, calculator, memory_retriever],
        execution_prompt_path = Path("/Users/vibhosri/Documents/CodeCrafter/agentflow/examples/qa_agent/prompts/orchestrator.md"),
        resolver = "user"
    )
    flow = AgentsFlow(
        start_agent=orchestrator,
        list_of_agents=[orchestrator]
    )
    flow_ctx = FlowContext(flow= flow, flow_description="A simple Q&A agent")
    return flow, flow_ctx


register_flow("qa_agent", build_qa_flow)
