from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
import functools


class Tool:
    """Base class for agent tools. Use the @tool decorator to create one."""
    def __init__(self, fn, name: str, description: str):
        self.name = name
        self.description = description
        self._fn = fn
        functools.update_wrapper(self, fn)

    async def __call__(self, *args, **kwargs):
        return await self._fn(*args, **kwargs)

    def __repr__(self):
        return f"Tool(name={self.name!r})"


def tool(name: str = "", description: str = ""):
    """Decorator that wraps an async function as a Tool."""
    def decorator(fn):
        tool_name = name or fn.__name__
        return Tool(fn, name=tool_name, description=description)
    return decorator


@dataclass
class ToolCall:
    input : str = None
    output : Any = None
    exceptions : Optional[Exception] = None
    
class ToolManager:
    """
    generic tool manager to provide common utilities for tools
    To be inited one per agent
    """
    
    def __init__(self, tool_list: List[Tool]):
        self.tool_list = tool_list
        self.tool_call_list : List[ToolCall] = []
        self.tool_call_str :str = "" 
        
    async def execute_tool(self, tool_input: str):
        #execute the tool , and add to list and str
        pass
    
    def get_tool_call_history(self) -> str:
        #tool_call_str to be inserted into the prompt of LLM in markdown format
        return self.tool_call_str
    
    def get_available_tools(self) -> str:
        return ""
        
