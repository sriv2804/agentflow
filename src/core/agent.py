from src.core.session import SessionContext, AgentContext
from src.core.memory import MemoryManager
from src.tools.common import Tool, ToolCall, ToolManager
from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
from src.utils.llm import LLM
from src.utils.prompt import PromptReader
from dataclasses import dataclass, field
from pathlib import Path
from src.core.flow import FlowContext, Edge

@dataclass
class RuntimeState:
    should_yield: bool = False
    yield_action: str | None = None        # which edge to take on yield
    yield_output: Any = None               # data to pass to next agent

    pending_tool_call: bool = False
    tool_call: ToolCall | None = None      # populated by LLM response

    needs_clarification: bool = False
    clarification: str | None = None  # query
    
    irrecoverable_error: bool = False
    error_ctx: str | None = None


class _ConditionalTransition:
    def __init__(
        self,
        src: "Agent",
        action: str
    ):
        self.src = src
        self.action = action
    def __rshift__(self, other) -> "Agent":
       return self.src.next(other, self.action)
        
class Agent:
    def __init__(
        self,
        agent_name: str,
        model_name: str,
        tools : List[Tool],
        execution_prompt_path : Path,
        resolver : str
    ):
        self.agent_name = agent_name
        self.llm = LLM(model_name = model_name)
        self.tools = tools
        self.execution_prompt = PromptReader.read_prompt(execution_prompt_path)
        #connected_agents_ctx will be used by this agent to refer to the
        #description of the agents it is connected to, will be helpful in
        #conjunction with the messages in memory manager
        self.connected_agents_ctx = {}
        self.resolver = resolver
        self.successors: Dict[str | "Agent"] = {}
        
    def next(self, dest_agent: "Agent", action: str = "default") -> "Agent":
        self.successors[action] = dest_agent
        return dest_agent
        
    def __rshift__(self, other) -> "Agent":
        return self.next(other)
    
    def __sub__(self, action: str) -> _ConditionalTransition:
        if not isinstance(action, str):
            raise TypeError("Action must be a string")
        return _ConditionalTransition(src=self, action=action)
        
    async def execute(
        self,
        session_context : SessionContext,
        agent_context : AgentContext,
        flow_context : FlowContext,
        input_edge : Edge
    )-> Edge:
        callee_agent, call_to, input_data = input_edge.callee, input_edge.call_to, input_edge.data
        if callee_agent not in self.connected_agents_ctx:
            self.connected_agents_ctx[callee_agent] = flow_context.get_agent_description(callee_agent)
        agent_memory_manager = agent_context.memory_manager
        if agent_memory_manager is None:
            agent_memory_manager = MemoryManager()
            agent_context.memory_manager = agent_memory_manager
        agent_memory_manager.append_msg(role=callee_agent, content=input_data)
        tool_manager = agent_context.tool_manager
        if tool_manager is None:
            tool_manager = ToolManager(self.tools)
            agent_context.tool_manager = tool_manager
        runtime_state = RuntimeState()
        channel = session_context.channel
        while not runtime_state.should_yield and not runtime_state.irrecoverable_error:
            if runtime_state.pending_tool_call:
                tool_call = runtime_state.tool_call
                #need to put this under an try/except and feedback to agent
                result =  await tool_manager.execute_tool(tool_call.input)
                if tool_call.exception:
                    agent_memory_manager.append_msg(
                        role="tool_with_exception",
                        content=tool_call.exception
                    )
                    runtime_state.pending_tool_call = False
                    #setting this so that the LLM can decide whether this as a 
                    #recoverable error or not
                    continue
                agent_memory_manager.append_msg(role="tool", content=str(result))
                runtime_state.pending_tool_call = False
            elif runtime_state.needs_clarification:
                query = runtime_state.clarification
                agent_memory_manager.append_msg(role=self.agent_name, content=query)
                if self.resolver == "user":
                        await channel.send_to_client(query)
                        response_from_client = await channel.receive_from_client()
                        agent_memory_manager.append_msg(role='user', content=response_from_client)
                        runtime_state.needs_clarification = False
                else:
                    return Edge(
                        callee=self.agent_name,
                        call_to = self.resolver,
                        data = query
                    )
            else:
                #need to prompt the LLM and update the state
                prompt_for_llm = self.execution_prompt.format(
                    connected_agents_context= str(self.connected_agents_ctx),
                    available_tools = tool_manager.get_available_tools(),
                    conversation_history= agent_memory_manager.get_messages(),
                    tool_call_history= tool_manager.get_tool_call_history(),
                )
                response_from_llm = await self.llm.invoke(prompt_for_llm)
                #need to put this under an try/except and feedback to agent in case of wrong format
                parsed_llm_output = self.parse_llm_output(response_from_llm)
                #we also need to properly format the LLM o/p and display the relevant parts to user
                runtime_state = RuntimeState(
                    **parsed_llm_output['runtime_state']
                )
        if runtime_state.irrecoverable_error:
            await channel.send_to_client(
                f"Hit an internal error : {runtime_state.error_ctx}"
            )
            return Edge(
                callee=self.agent_name,
                call_to = None,
                data = runtime_state.error_ctx
            )
        agent_memory_manager.update_summary()
        return Edge(
            callee = self.agent_name,
            call_to = runtime_state.yield_action,
            data = runtime_state.yield_output
        )
    
    def parse_llm_output(self, llm_response: str) -> dict:
        return {
            "runtime_state": {
                "should_yield": False,
                "yield_action": None,
                "yield_output": None,
                "pending_tool_call": False,
                "tool_call": None,
                "needs_clarification": False,
                "clarification": None,
                "irrecoverable_error": False,
                "error_ctx": None,
            }
        }
        