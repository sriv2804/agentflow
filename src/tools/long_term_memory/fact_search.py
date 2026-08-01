from src.tools.common import tool, ToolContext

@tool(name="fact_search", description="Search stored facts about the user or context")
async def fact_search(query: str, _ctx: ToolContext = None) -> str:
    """
    Search facts stored in long-term fact memory.

    Args:
        query: natural language description of what fact to look for
    """
    results = await _ctx.fact_store.search(query, n_results=5)
    if not results:
        return "No relevant facts found."
    formatted = "\n".join([f"- {r['content']}" for r in results])
    return f"Found {len(results)} relevant fact(s):\n{formatted}"
