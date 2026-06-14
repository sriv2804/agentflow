from typing import Tuple
from src.core.flow import AgentsFlow
from src.core.session import FlowContext
from src.flows.registry import register_flow


def build_qa_flow() -> Tuple[AgentsFlow, FlowContext]:
    # stub — will be implemented in Week 3 with real agents
    raise NotImplementedError("qa_agent flow not yet implemented")


register_flow("qa_agent", build_qa_flow)
