from __future__ import annotations
from typing import Any, Dict, Optional, List, TYPE_CHECKING
from dataclasses import dataclass
from pathlib import Path
import json

from src.core.memory import MemoryManager
from src.tools.common import Tool, ToolCall, ToolManager
from src.utils.llm import LLM
from src.utils.prompt import PromptReader
from src.core.flow import Edge, FlowContext

if TYPE_CHECKING:
    from src.core.session import SessionContext, AgentContext

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
        self.prompt_template = PromptReader.read_prompt()
        self.execution_prompt = PromptReader.read_execution_prompt(execution_prompt_path)
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
        parse_errors = []
        scratchpad : List[str] = agent_context.scratchpad
        while not runtime_state.should_yield and not runtime_state.irrecoverable_error:
            if runtime_state.pending_tool_call:
                tool_call = runtime_state.tool_call
                #need to put this under an try/except and feedback to agent
                await channel.send_to_client({
                    "message_type": "info",
                    "content": f"invoking tool : {tool_call.tool_name}"
                })
                result =  await tool_manager.execute_tool(tool_call)
                runtime_state.pending_tool_call = False
                if tool_call.exception:
                    agent_memory_manager.append_msg(
                        role="tool_with_exception",
                        content=tool_call.exception
                    )
                    scratchpad.append(
                    f"[FAILED TOOL CALL] {tool_call.tool_name}({tool_call.args}) -> EXCEPTION : {tool_call.exception}"
                    )
                    #setting this so that the LLM can decide whether this as a 
                    #recoverable error or not
                    continue
                agent_memory_manager.append_msg(role="tool", content=str(result))
                scratchpad.append(
                    f"[TOOL CALL] {tool_call.tool_name}({tool_call.args}) -> {result}"
                )
            elif runtime_state.needs_clarification:
                query = runtime_state.clarification
                agent_memory_manager.append_msg(role=self.agent_name, content=query)
                if self.resolver == "user":
                        await channel.send_to_client(
                            {
                                "message_type": 'response',
                                'content': query
                            }
                        )
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
                print(f"--- SCRATCHPAD AT THIS STEP ---\n{scratchpad}\n---")
                prompt_for_llm = self.prompt_template.format(
                    agent_name=self.agent_name,
                    flow_description=flow_context.flow_description,
                    connected_agents_context=str(self.connected_agents_ctx),
                    execution_prompt=self.execution_prompt,
                    available_tools=tool_manager.get_available_tools(),
                    summary=agent_memory_manager.summary,
                    conversation_history=agent_memory_manager.get_messages(),
                    scratchpad='\n'.join(scratchpad) if scratchpad else "empty: this is your first step",
                    tool_call_history=tool_manager.get_tool_call_history(),
                    recent_errors="\n".join(parse_errors) if parse_errors else "None"
                )
                response_from_llm = await self.llm.invoke(prompt_for_llm)
                #need to put this under an try/except and feedback to agent in case of wrong format
                try:
                    parsed_llm_output = self.parse_llm_output(response_from_llm, tool_manager)
                except Exception as e:
                    #feed the error to the llm and prompt it again
                    parse_errors.append(str(e))
                    if len(parse_errors) > 3:
                        runtime_state.irrecoverable_error = True
                        runtime_state.error_ctx = f"Failed to parse LLM output after 3 attempts. Last error: {parse_errors[-1]}"
                    else:
                        agent_memory_manager.append_msg(
                            role="system",
                            content=(
                                f"Your previous response could not be parsed. Error: {parse_errors[-1]}\n"
                                f"Your response was: {response_from_llm}\n"
                                f"Please respond with a valid JSON object as specified in the output format."
                            )
                        )
                    continue
                #we also need to properly format the LLM o/p and display the relevant parts to user
                runtime_state = RuntimeState(
                    **parsed_llm_output['runtime_state']
                )
                summary = parsed_llm_output.get("summary", "")
                scratchpad.append(f"[THOUGHT] {summary}")
                await channel.send_to_client({
                    "message_type": "info",
                    "content": summary
                })
        if runtime_state.irrecoverable_error:
            await channel.send_to_client({
                "message_type":  "done",
                "content": f"Hit an internal error : {runtime_state.error_ctx}"
                }
            )
            return Edge(
                callee=self.agent_name,
                call_to = None,
                data = runtime_state.error_ctx
            )
        agent_context.scratchpad = []
        agent_memory_manager.update_summary()
        if runtime_state.yield_action == "end":
            await channel.send_to_client({
                "message_type": "done",
                "content": runtime_state.yield_output
            })
        return Edge(
            callee = self.agent_name,
            call_to = runtime_state.yield_action,
            data = runtime_state.yield_output
        )
    
    def parse_llm_output(self, llm_response: str, tool_manager: ToolManager) -> dict:
        """
        Parses the LLM's JSON response into a RuntimeState-compatible dict.
        Raises ValueError on invalid format — caller wraps in try/except.
        """
        try:
            parsed = json.loads(llm_response.strip())
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM response is not valid JSON: {e}\nResponse was: {llm_response}")
        
        action = parsed.get("action")
        if not action:
            raise ValueError(f"Missing 'action' field in LLM response: {parsed}")
        
        summary = parsed.get("summary", "")
        
        # base state — all flags off
        base = {
            "should_yield": False,
            "yield_action": None,
            "yield_output": None,
            "pending_tool_call": False,
            "tool_call": None,
            "needs_clarification": False,
            "clarification": None,
            "irrecoverable_error": False,
            "error_ctx": None
        }
        
        if action == "tool_call":
            tool_name = parsed.get("tool_name")
            args = parsed.get("args", {})
            if not tool_name:
                raise ValueError("action is 'tool_call' but 'tool_name' is missing")
            if tool_name not in tool_manager.tool_dict:
                raise ValueError(f"Unknown tool '{tool_name}'. Available: {list(tool_manager.tool_dict.keys())}")
            return {"summary": summary, "runtime_state": {
                **base,
                "pending_tool_call": True,
                "tool_call": ToolCall(tool_name=tool_name, args=args)
            }}
        
        elif action == "clarification":
            query = parsed.get("query")
            if not query:
                raise ValueError("action is 'clarification' but 'query' is missing")
            return {"summary": summary, "runtime_state": {
                **base,
                "needs_clarification": True,
                "clarification": query
            }}
        
        elif action == "yield":
            yield_action = parsed.get("yield_action")
            yield_output = parsed.get("yield_output")
            if not yield_action:
                raise ValueError("action is 'yield' but 'yield_action' is missing")
            if yield_action != "end" and yield_action not in self.successors:
                raise ValueError(f"Unknown yield_action '{yield_action}'. Valid: {list(self.successors.keys())}")
            return {"summary": summary, "runtime_state": {
                **base,
                "should_yield": True,
                "yield_action": yield_action,
                "yield_output": yield_output
            }}
        
        elif action == "error":
            error_ctx = parsed.get("error_ctx", "Unknown error")
            return {"summary": summary, "runtime_state": {
                **base,
                "irrecoverable_error": True,
                "error_ctx": error_ctx
            }}
        else:
            raise ValueError(f"Unknown action '{action}'. Must be one of: tool_call, clarification, yield, error")
        
    def get_description(self) -> str:
        return ""

        