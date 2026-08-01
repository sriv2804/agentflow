from src.tools.common import tool, ToolContext

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
    return "Working memory updated successfully."
