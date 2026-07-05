import asyncio
from src.tools.common import tool, ToolContext

@tool(name="skill_retriever", description="Retrieve relevants skill from past conversations/Lists available skills")
async def skill_retriever(mode :str, query_str: str = "",_ctx: ToolContext = None) -> str:
    """
    Search agent's long-term corpus for context relevant to the query.
    Use this when the user references something from a past conversation or session.

    Args:
        mode: "view" to list available skills, "get" to retrieve a specific skill by task description
        query: task description for semantic search (required when mode is "get")
    """
    # Week 4 — wire ChromaDB here
    skill_store = _ctx.skill_store
    if mode == 'view':
        return "\n".join(skill_store.list_all_skills())
    elif mode == 'get':
        if not query_str:
            raise Exception("query_str is mandatory when using mode get")
        result = await asyncio.to_thread(skill_store.query, query_str)
        return result
    raise Exception("invalid mode passed, valid modes are view and get")