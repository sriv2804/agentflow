from typing import Tuple
from pathlib import Path
from src.core.agent import Agent
from src.flows.registry import register_flow
from src.tools.load_tool_group import load_tool_group
from src.tools.web_search import web_search
from src.tools.write_report import write_report
from src.tools.skill_retriever import skill_retriever
from src.tools.save_skill import save_skill, view_skill_template
from src.tools.common import ToolGroup
from src.core.flow import AgentsFlow, FlowContext

MODEL_NAME = "gemma4:26b-a4b-it-q4_K_M"
MODEL_BACKEND = "ollama"
PROMPTS_DIR = Path("examples/competitive_analysis/prompts")


def build_competitive_analysis_flow() -> Tuple[AgentsFlow, FlowContext]:

    web_tools = ToolGroup(
        name="web_tools",
        description="Search and fetch current information from the web",
        tools=[web_search],
        instructions=""
    )

    reporting_tools = ToolGroup(
        name="reporting_tools",
        description="Tools for generating and persisting structured reports",
        tools=[write_report],
        instructions="Use write_report to persist the final structured report to disk. Pass the full markdown content as sections. Always call this before yielding."
    )

    memory_tools = ToolGroup(
        name="memory_tools",
        description="Save and retrieve long-term skills and reusable plans",
        tools=[skill_retriever, save_skill, view_skill_template],
        instructions="""
### Using Memory Tools

#### Retrieving skills
Before planning, check for a prior research plan:
1. Call `skill_retriever` with `mode="view"` to see available skills
2. If a relevant skill exists, call `skill_retriever` with `mode="get"` and a task description
3. The retrieved skill appears as [ACTIVE SKILL] in your scratchpad — follow its steps

#### Saving skills
After a successful run, save the research + delegation plan as a skill:
1. Call `skill_retriever(mode="view")` to check for duplicates — do NOT save a duplicate
2. Call `view_skill_template` to see the required format
3. Call `save_skill` with a descriptive snake_case name and generic (not tool-specific) steps
"""
    )

    researcher = Agent(
        agent_name="researcher",
        model_name=MODEL_NAME,
        model_backend=MODEL_BACKEND,
        tool_grps=[web_tools],
        always_on_tools=[load_tool_group],
        execution_prompt_path=PROMPTS_DIR / "researcher.md",
        resolver="orchestrator",
    )

    report_generator = Agent(
        agent_name="report_generator",
        model_name=MODEL_NAME,
        model_backend=MODEL_BACKEND,
        tool_grps=[reporting_tools],
        always_on_tools=[load_tool_group],
        execution_prompt_path=PROMPTS_DIR / "report_generator.md",
        resolver="orchestrator",
    )

    orchestrator = Agent(
        agent_name="orchestrator",
        model_name=MODEL_NAME,
        model_backend=MODEL_BACKEND,
        tool_grps=[memory_tools],
        always_on_tools=[load_tool_group],
        execution_prompt_path=PROMPTS_DIR / "orchestrator.md",
        resolver="user",
    )

    # Routing: orchestrator delegates downstream
    orchestrator - "research" >> researcher
    orchestrator - "generate_report" >> report_generator

    # Return edges: workers hand results back via yield_action="orchestrator".
    # Clarifications also travel this edge, since a non-user resolver returns
    # Edge(call_to=<resolver>) and AgentsFlow.run looks it up in successors.
    # ("end" would terminate the whole flow, so workers never use it.)
    researcher - "orchestrator" >> orchestrator
    report_generator - "orchestrator" >> orchestrator

    flow = AgentsFlow(
        start_agent=orchestrator,
        list_of_agents=[orchestrator, researcher, report_generator]
    )
    flow_ctx = FlowContext(
        flow=flow,
        flow_description="Competitive analysis flow: orchestrator delegates research to researcher and report writing to report_generator, then synthesizes findings for the user."
    )
    return flow, flow_ctx

register_flow("competitive_analysis", build_competitive_analysis_flow)
