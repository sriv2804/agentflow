from typing import Tuple
from pathlib import Path
from src.core.agent import Agent
from src.flows.registry import register_flow
from src.tools.load_tool_group import load_tool_group
from src.tools.web_search import web_search
from src.tools.calculator import calculator
from src.tools.skill_retriever import skill_retriever
from src.tools.save_skill import save_skill,view_skill_template
from src.tools.common import ToolGroup
from src.core.flow import AgentsFlow, FlowContext

def build_qa_flow() -> Tuple[AgentsFlow, FlowContext]:
    
    web_tools = ToolGroup(
        name="web_tools",
        description="Search and fetch current information from the web",
        tools=[web_search],
        instructions=""  # no special prompting needed for web tools
    )
    
    compute_tools = ToolGroup(
        name="compute_tools",
        description="Mathematical calculations and expression evaluation",
        tools=[calculator],
        instructions=""  # straightforward, no special rules needed
    )
    
    memory_tools = ToolGroup(
        name="memory_tools",
        description="Save and retrieve long-term skills and reusable plans",
        tools=[skill_retriever, save_skill, view_skill_template],
        instructions="""
### Using Memory Tools

#### Retrieving skills
For ANY multi-step task, ALWAYS check for a relevant skill BEFORE starting:
1. Call `skill_retriever` with `mode="view"` to see available skills
2. If a relevant skill exists, call `skill_retriever` with `mode="get"` and a task description
3. The retrieved skill appears as [ACTIVE SKILL] in your scratchpad — follow its steps
DO NOT skip this check even if you think you know the answer.

#### Saving skills
After completing a multi-step task, save it as a skill if:
- It required 2+ distinct tool calls
- The approach is generalizable beyond the specific inputs
- A similar request could plausibly come up again

Before saving:
1. Call `skill_retriever(mode="view")` to check for duplicates
2. If a similar skill exists, do NOT save a duplicate
3. Use descriptive snake_case names e.g. `population_ratio_comparison`
4. Call `view_skill_template` first to see the required format
"""
    )

    orchestrator = Agent(
        agent_name="orchestrator",
        model_name="gpt-4o-mini",
        tool_grps=[web_tools, compute_tools, memory_tools],
        always_on_tools=[load_tool_group],
        execution_prompt_path=Path("examples/qa_agent/prompts/orchestrator.md"),
        resolver="user"
    )

    flow = AgentsFlow(
        start_agent=orchestrator,
        list_of_agents=[orchestrator]
    )
    flow_ctx = FlowContext(flow=flow, flow_description="A simple Q&A agent")
    return flow, flow_ctx
register_flow("qa_agent", build_qa_flow)