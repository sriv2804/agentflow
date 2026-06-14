import uuid
from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
from dataclasses import dataclass, field
from src.runtime.channel import AsyncChannel
from src.core.memory import MemoryManager
from src.tools.common import ToolCall, ToolManager
from src.core.flow import AgentsFlow
from pathlib import Path

@dataclass
class AgentContext:
    agent_id : str
    data : Dict[str, Any] = field(default_factory=dict)
    memory_manager : Optional[MemoryManager] = None
    tool_manager : Optional[ToolManager] = None
    
    def set_memory_manager(self, memory_manager: MemoryManager) -> None:
        self.memory_manager = memory_manager
    
    def set_tool_manager(self, tool_manager: ToolManager) -> None:
        self.tool_manager = tool_manager
    

class FlowContext:
    def __init__(self, flow: AgentsFlow):
        self.flow_description: str  = ""
        self.agents_description : List[Dict] = []
        for agent in flow.get_agents_list():
            self.agents_description[agent.agent_name] = agent.get_description
    
    def get_agent_description(self, agent_name : str):
        if agent_name in self.agents_description:
            return self.agents_description.get(agent_name, "")
        
class SessionContext:
    """
    session scoped container for agent
    Holds runtime information of the user session
    """
    
    def __init__(
        self,
        session_id : str,
        channel : AsyncChannel,
        *,
        logger,
        workspace_dir : Path
    ) -> None:
        self.session_id = session_id
        self.channel = channel
        self.agents : Dict[str, AgentContext] = {}
        self.user_chat = MemoryManager()
        self.data : Dict[str, Any] = {}
        self.workspace_dir = workspace_dir
        
    def get_agent(self, agent_id: str):
        agent_ctx = self.agents.get(agent_id)
        if agent_ctx is None:
            agent_ctx = AgentContext(agent_id = agent_id)
            self.agents[agent_id] = agent_ctx
        return agent_ctx
    
    def set_user_channel(self, channel: AsyncChannel):
        self.channel = channel
    
    