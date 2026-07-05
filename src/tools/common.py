from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
from collections.abc import Callable
import functools
import inspect
import docstring_parser
from src.core.skill_store import SkillStore

class Tool:
    """Base class for agent tools. Use the @tool decorator to create one."""
    def __init__(self, fn, name: str, description: str, is_cmd: bool):
        self.name = name
        self.description = description
        self._fn = fn
        self.args_schema = self._build_args_schema()
        self.is_cmd = is_cmd
        functools.update_wrapper(self, fn)

    async def __call__(self, *args, **kwargs):
        return await self._fn(*args, **kwargs)

    def __repr__(self):
        return f"Tool(name={self.name!r})"
    
    def _build_args_schema(self):
        hints = self._fn.__annotations__
        sig = inspect.signature(self._fn)
        parsed_doc = docstring_parser.parse(self._fn.__doc__ or "")
        param_docs = {p.arg_name: p.description for p in parsed_doc.params}
        lines = []
        for param_name, param in sig.parameters.items():
            if param_name == "_ctx":   # ← skip, framework-internal
               continue
            type_hint = hints.get(param_name,"Any")
            type_str = type_hint.__name__ if hasattr(type_hint, "__name__") else str(type_hint)
            description = param_docs.get(param_name, "")
            if description:
                lines.append(
                    f" {param_name} ({type_str}) - {description}"
                )
            else:
                lines.append(
                    f" {param_name} ({type_str})"
                )
        return "\n".join(lines) if lines else "(no arguments)"

def tool(name: str = "", description: str = "", is_cmd = False):
    """Decorator that wraps an async function as a Tool."""
    def decorator(fn):
        tool_name = name or fn.__name__
        return Tool(fn, name=tool_name, description=description, is_cmd = is_cmd)
    return decorator


@dataclass
class ToolCall:
    tool_name: str
    args: Dict[str, Any]
    output: Any = None
    exception: Optional[Exception] = None

@dataclass
class ToolContext:
    skill_store: Optional[SkillStore] = None
    
class ToolManager:
    """
    generic tool manager to provide common utilities for tools
    To be inited one per agent
    """
    
    def __init__(self, tool_list: List[Tool], tool_context : ToolContext):
        self.tool_dict : Dict[str, Callable] = {}
        for tool in tool_list:
            self.tool_dict[tool.name] = tool
        self.tool_call_list : List[ToolCall] = []
        self.tool_call_str :str = "" 
        self.available_tools : str = ""
        self.tool_context = ToolContext
        
    async def execute_tool(self, tool_call: ToolCall):
        #execute the tool , and add to list and str(right now we are adding to memory)
        #to do -> use is_cmd to decide whether to run in a container or not
        tool_name = tool_call.tool_name
        tool_args = tool_call.args
        tool = self.tool_dict[tool_name]
        res = ""
        try:
            res = await tool(**tool_args, _ctx = self.tool_context)
            tool_call.output=res
        except Exception as e:
            tool_call.exception = str(e)
        finally:
            self.tool_call_list.append(tool_call)
            self.tool_call_str += (
                f"TOOL: {tool_call.tool_name} | "
                f"ARGS: {tool_call.args} | "
                f"RESULT: {tool_call.output} | "
                f"ERROR: {tool_call.exception}\n"
                )
            return res
    
    def get_tool_call_history(self) -> str:
        #tool_call_str to be inserted into the prompt of LLM in markdown format
        return self.tool_call_str
    
    def get_available_tools(self) -> str:
        if self.available_tools:
            return self.available_tools
        available_tools_str = ""
        for tool in self.tool_dict.values():
            available_tools_str += (
                f"NAME : {tool.name}\n"
                f"DESCRIPTION : {tool.description}\n"
                f"ARGS:\n{tool.args_schema}\n\n"
            )
        self.available_tools = available_tools_str
        return self.available_tools
        
