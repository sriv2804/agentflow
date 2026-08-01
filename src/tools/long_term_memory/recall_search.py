from src.tools.common import tool, ToolContext

@tool(name="recall_search", description="Search past conversations semantically")
async def recall_search(query: str, _ctx: ToolContext = None) -> str:
    """
    Search conversation history stored in long-term recall memory.

    Args:
        query: natural language description of what to search for
    """
    results = await _ctx.recall_store.search(query, n_results=5)
    if not results:
        return "No relevant past conversations found."
    formatted = "\n".join([
        f"[{r['timestamp']}] {r['role'].upper()}: {r['content'][:200]}"
        for r in results
    ])
    return f"Found {len(results)} relevant past conversation(s):\n{formatted}"
