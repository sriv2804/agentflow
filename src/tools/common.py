from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING
from collections.abc import Callable
import functools
import inspect
import docstring_parser
from src.core.skill_store import SkillStore
from src.core.memory import ScratchPad

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
    scratchpad: Optional[ScratchPad] = None
    tool_manager: Optional["ToolManager"] = None
    recall_store: Optional[Any] = None
    fact_store: Optional[Any] = None
    session_id: Optional[str] = None
    memory_manager: Optional[Any] = None
    
@dataclass
class ToolGroup:
    name : str 
    description : str
    tools : List[Tool]
    instructions : str
    schema : str = None
    
    def render_group_schema(self):
        if self.schema:
            return self.schema
        self.schema = f"AVAILABLE TOOLS:\n\n"
        available_tools_str = ""
        for tool in self.tools:
            available_tools_str += (
                f"NAME : {tool.name}\n"
                f"DESCRIPTION : {tool.description}\n"
                f"ARGS:\n{tool.args_schema}\n\n"
            )
        self.schema += available_tools_str
        self.schema += f"INSTRUCTIONS FOR THIS TOOL GROUP\nTO BE STRICTLY FOLLOWED\n"
        self.schema += self.instructions
        return self.schema
    
class ToolManager:
    """
    generic tool manager to provide common utilities for tools
    To be inited one per agent
    """
    
    def __init__(
        self,
        tool_grp_list: List[ToolGroup],
        always_on_tools: List[Tool],
        tool_context : ToolContext = None
    ):
        self.tool_dict : Dict[str, Callable] = {}
        self.tool_grp_list = tool_grp_list
        self.tool_grp_dict = {}
        self.always_on_tools = always_on_tools
        for tool_grp in tool_grp_list:
            self.tool_grp_dict[tool_grp.name] = tool_grp
            for tool in tool_grp.tools:
                self.tool_dict[tool.name] = tool
        for tool in always_on_tools:
            self.tool_dict[tool.name] = tool
        self.tool_call_list : List[ToolCall] = []
        self.tool_call_str :str = "" 
        self.available_tools : str = ""
        self.grp_summary : str = ""
        self.always_on_tools_desc : str = ""
        self.grp_names : str = ""
        self.tool_context = tool_context
    
    def set_toolctx(
        self,
        skill_store : SkillStore,
        scratchpad : ScratchPad,
        tool_manager : "ToolManager",
        recall_store : Optional[Any] = None,
        fact_store : Optional[Any] = None,
        session_id : Optional[str] = None,
        memory_manager : Optional[Any] = None,
    ):
        if self.tool_context:
            return
        self.tool_context = ToolContext()
        self.tool_context.skill_store = skill_store
        self.tool_context.scratchpad = scratchpad
        self.tool_context.tool_manager = tool_manager
        self.tool_context.recall_store = recall_store
        self.tool_context.fact_store = fact_store
        self.tool_context.session_id = session_id
        self.tool_context.memory_manager = memory_manager
    
    def get_tool_group(self, tool_grp_name : str):
        return self.tool_grp_dict.get(tool_grp_name, None)
    
    async def execute_tool(self, tool_call: ToolCall):
        #execute the tool , and add to list and str(right now we are adding to memory)
        #to do -> use is_cmd to decide whether to run in a container or not
        if self.tool_context.tool_manager is None:
            self.tool_context.tool_manager = self
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
    
    def group_names(self):
        if self.grp_names:
            return self.grp_names
        self.grp_names = f"{[gp.name for gp in self.tool_grp_list]}"
        return self.grp_names
    
    def get_group_summary(self):
        if self.grp_summary:
            return self.grp_summary
        grp_summary = ""
        for tool_grp in self.tool_grp_list:
            grp_summary += f"[{tool_grp.name}] -> {tool_grp.description}\n"
            tool_name_list = f"{[t.name for t in tool_grp.tools]}"
            grp_summary += f"\t{tool_name_list}\n\n"
        self.grp_summary = grp_summary
        return self.grp_summary
    
    def get_always_on_tools(self):
        if self.always_on_tools_desc:
            return self.always_on_tools_desc
        always_on_tools_desc = ""
        for tool in self.always_on_tools:
            always_on_tools_desc += (
                f"NAME : {tool.name}\n"
                f"DESCRIPTION : {tool.description}\n"
                f"ARGS:\n{tool.args_schema}\n\n"
            )
        self.always_on_tools_desc = always_on_tools_desc
        return self.always_on_tools_desc
        
        
