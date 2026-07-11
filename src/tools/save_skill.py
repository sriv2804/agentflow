import json
from pathlib import Path
import asyncio
from src.tools.common import tool, ToolContext

SKILL_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "skill_template.md"

def _load_template():
    global SKILL_TEMPLATE
    with open(SKILL_TEMPLATE_PATH) as f:
        SKILL_TEMPLATE = f.read()

_load_template()

@tool(name="save_skill", description="Save a skill to long term memory")
async def save_skill(
    skill_name: str,
    skill_description: str,
    skill_content: str,
    _ctx: ToolContext = None
) -> str:
    """
    Save a reusable skill/plan to the agent's long term memory.
    
    Args:
        skill_name: short unique name for the skill e.g. "population_ratio_comparison"
        skill_description: one sentence describing when to use this skill
        skill_content: full markdown content of the skill following the skill template
    """
    skill_store = _ctx.skill_store
    await asyncio.to_thread(skill_store.add, {
        "name": skill_name,
        "description": skill_description,
        "content": skill_content
    })
    return f"Skill '{skill_name}' saved successfully."

@tool(name="view_skill_template", description="View the template format for saving a new skill, USE before saving a skill")
async def view_skill_template(_ctx: ToolContext = None) -> str:
    return SKILL_TEMPLATE