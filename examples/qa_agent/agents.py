from typing import Tuple
from pathlib import Path
from src.core.agent import Agent
from src.flows.registry import register_flow
from src.tools.load_tool_group import load_tool_group
from src.tools.web_search import web_search
from src.tools.calculator import calculator
from src.tools.skill_retriever import skill_retriever
from src.tools.save_skill import save_skill,view_skill_template
from src.tools.long_term_memory.recall_search import recall_search
from src.tools.long_term_memory.fact_search import fact_search
from src.tools.long_term_memory.fact_write import fact_write
from src.tools.long_term_memory.working_memory_update import working_memory_update
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

    long_term_memory_tools = ToolGroup(
        name="long_term_memory",
        description="Search and update long-term episodic and factual memory across sessions",
        tools=[recall_search, fact_search, fact_write, working_memory_update],
        instructions="""
### Using Long-Term Memory Tools

Your long-term memory has two stores:
- **Fact store**: curated facts you have explicitly saved (user preferences, key decisions, important context)
- **Recall store**: searchable history of past conversations

Your **working memory** (always visible in your prompt) is a compact index of what you know — update it when you learn something important.

#### When to search long-term memory
- User references something from a past session
- Task involves user preferences or history
- You want to check if you already know something before searching the web

#### When to write facts
- User shares a preference, decision, or important personal detail
- You learn something that will be relevant in future sessions
- During memory pressure (system will alert you)

#### When to update working memory
- After writing new facts
- During memory pressure alerts
- When your understanding of the user/context changes significantly

#### Memory pressure
When you see a [SYSTEM] Memory pressure alert in your scratchpad:
1. Call `fact_write` for each important fact in current conversation
2. Call `working_memory_update` with a revised compact summary
3. Then continue with your task — eviction will happen automatically
"""
    )

    orchestrator = Agent(
        agent_name="orchestrator",
        model_name="gemma4:26b-a4b-it-q4_K_M",
        tool_grps=[web_tools, compute_tools, memory_tools, long_term_memory_tools],
        always_on_tools=[load_tool_group],
        execution_prompt_path=Path("examples/qa_agent/prompts/orchestrator.md"),
        resolver="user",
        model_backend="ollama"
    )

    flow = AgentsFlow(
        start_agent=orchestrator,
        list_of_agents=[orchestrator]
    )
    flow_ctx = FlowContext(flow=flow, flow_description="A simple Q&A agent")
    return flow, flow_ctx
register_flow("qa_agent", build_qa_flow)