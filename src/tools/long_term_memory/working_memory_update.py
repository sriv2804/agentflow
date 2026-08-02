import json
from src.tools.common import tool, ToolContext
from src.core.storage import get_working_memory_path

@tool(name="working_memory_update", description="Update your working memory index")
async def working_memory_update(updated_memory: str, _ctx: ToolContext = None) -> str:
    """
    Replace your working memory with an updated compact summary.
    Working memory is always visible in your prompt — keep it concise (under 200 words).
    Include: key facts about user, important context, what's stored in long-term memory.

    Args:
        updated_memory: the new working memory content (replaces current content entirely)
    """
    _ctx.memory_manager.working_memory = updated_memory

    wm_path = get_working_memory_path(
        _ctx.working_dir,
        _ctx.storage_session_id,
        _ctx.agent_name
    )
    with open(wm_path, "w") as f:
        json.dump({"working_memory": updated_memory}, f, indent=2)

    return "Working memory updated and persisted successfully."
