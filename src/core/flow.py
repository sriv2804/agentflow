from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
from dataclasses import dataclass, field
from src.core.agent import Agent
from src.core.session import SessionContext, AgentContext, FlowContext

@dataclass
class Edge:
    callee : str = ""
    call_to : str = ""
    data : Any = None        
        
class AgentsFlow:
    def __init__(
        self,
        start_agent : Agent,
        list_of_agents : List["Agent"]
    ):
       self.start_agent = start_agent
       self.list_of_agents = list_of_agents
       
    def get_agents_list(self):
        return self.list_of_agents
    
    async def run(
        self,
        session_ctx : SessionContext,
        flow_ctx : FlowContext,
        initial_edge : Edge
    ):
        curr_agent = self.start_agent
        edge = initial_edge if initial_edge else Edge()
        while True:
            agent_ctx = session_ctx.get_agent(curr_agent.agent_name)
            edge = await curr_agent.execute(
                session_context = session_ctx,
                agent_context = agent_ctx,
                flow_context = flow_ctx,
                input_edge = edge
            )
            if edge.call_to is None:
                return
            curr_agent = curr_agent.successors[edge.call_to]
