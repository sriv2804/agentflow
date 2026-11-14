from __future__ import annotations
from typing import Any, Dict, Optional, List, TYPE_CHECKING
from dataclasses import dataclass
from src.core.trace import tracer_of, safe_emit

if TYPE_CHECKING:
    from src.core.agent import Agent
    from src.core.session import SessionContext, AgentContext
    
@dataclass
class Edge:
    callee : str = ""
    call_to : str = ""
    data : Any = None        
    
class FlowContext:
    def __init__(self, flow: AgentsFlow, flow_description: str = ""):
        self.flow = flow
        self.flow_description: str = flow_description
        self.agents_description: Dict[str, Any] = {}
        for agent in flow.get_agents_list():
            self.agents_description[agent.agent_name] = agent.get_description
    
    def get_agent_description(self, agent_name : str):
        if agent_name in self.agents_description:
            return self.agents_description.get(agent_name, "")
         
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
        tracer = tracer_of(session_ctx)
        safe_emit(tracer, "handoff", from_agent=edge.callee or "user", action=None,
                  to_agent=curr_agent.agent_name)
        while True:
            agent_ctx = session_ctx.get_agent(curr_agent.agent_name)
            edge = await curr_agent.execute(
                session_context = session_ctx,
                agent_context = agent_ctx,
                flow_context = flow_ctx,
                input_edge = edge
            )
            if edge.call_to is None or edge.call_to == "end":
                #we can also use a terminal edge contract to end the flow
                safe_emit(tracer, "handoff", from_agent=curr_agent.agent_name, action=edge.call_to,
                          to_agent="user" if edge.call_to == "end" else None)
                return
            next_agent = curr_agent.successors[edge.call_to]
            safe_emit(tracer, "handoff", from_agent=curr_agent.agent_name, action=edge.call_to,
                      to_agent=next_agent.agent_name)
            curr_agent = next_agent
