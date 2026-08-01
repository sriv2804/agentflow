from src.tools.common import tool, ToolContext

@tool(name="fact_write", description="Save an important fact to long-term memory")
async def fact_write(fact: str, _ctx: ToolContext = None) -> str:
    """
    Persist an important fact to long-term fact store.
    Use for: user preferences, important events, decisions made, key information.

    Args:
        fact: the fact to store, written as a clear standalone statement
    """
    fact_id = await _ctx.fact_store.add(
        content=fact,
        source_session=_ctx.session_id or "unknown"
    )
    return f"Fact saved successfully (id: {fact_id})"
