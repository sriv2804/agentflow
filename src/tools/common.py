from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
from collections.abc import Callable
import functools


class Tool:
    """Base class for agent tools. Use the @tool decorator to create one."""
    def __init__(self, fn, name: str, description: str, is_cmd: bool):
        self.name = name
        self.description = description
        self._fn = fn
        self.is_cmd = is_cmd
        functools.update_wrapper(self, fn)

    async def __call__(self, *args, **kwargs):
        return await self._fn(*args, **kwargs)

    def __repr__(self):
        return f"Tool(name={self.name!r})"


def tool(name: str = "", description: str = "", is_cmd = False):
    """Decorator that wraps an async function as a Tool."""
    def decorator(fn):
        tool_name = name or fn.__name__
        return Tool(fn, name=tool_name, description=description, is_cmd = is_cmd)
    return decorator


@dataclass
class ToolCall:
    tool_name: str
    args: str
    output: Any = None
    exception: Optional[Exception] = None
    
class ToolManager:
    """
    generic tool manager to provide common utilities for tools
    To be inited one per agent
    """
    
    def __init__(self, tool_list: List[Tool]):
        self.tool_dict : Dict[str, Callable] = {}
        for tool in tool_list:
            self.tool_dict[tool.name] = tool
        self.tool_call_list : List[ToolCall] = []
        self.tool_call_str :str = "" 
        self.available_tools : str = ""
    async def execute_tool(self, tool_call: ToolCall):
        #execute the tool , and add to list and str(right now we are adding to memory)
        #to do -> use is_cmd to decide whether to run in a container or not
        tool_name = tool_call.tool_name
        tool_args = self._convert_to_dict(tool_call.args)
        tool = self.tool_dict[tool_name]
        try:
            res=tool(**tool_args)
            tool_call.output=res
            return res
        except Exception as e:
            tool_call.exception = str(e)
            return ""
    
    def get_tool_call_history(self) -> str:
        #tool_call_str to be inserted into the prompt of LLM in markdown format
        return self.tool_call_str
    
    def get_available_tools(self) -> str:
        if self.available_tools:
            return self.available_tools
        available_tools_str = ""
        for tool in self.tool_list:
            available_tools_str.append(
                f"NAME : {tool.name}\n"
                f"DESCRIPTION : {tool.description}\n"#also add IO contract
            )
        self.available_tools = available_tools_str
        return self.available_tools
        
