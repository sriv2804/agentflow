from src.tools.common import tool

@tool(name="memory_retriever", description="Retrieve context from past conversations")
async def memory_retriever(query: str) -> str:
    """
    Search agent's long-term corpus for context relevant to the query.
    Use this when the user references something from a past conversation or session.

    Args:
        query: The topic or question to search past context for
    """
    # Week 4 — wire ChromaDB here
    return "No long-term memory available yet."