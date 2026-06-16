from src.tools.common import tool
from ddgs import DDGS

@tool(name="web_search", description="Search the web for current information on a topic")
async def web_search(query: str) -> str:
    """
    Search the web for current information on a topic.

    Args:
        query: The search query to look up
    """
    try:
        results = DDGS().text(query, max_results=3)
        if not results:
            return "No results found."
        output = ""
        for i, r in enumerate(results, 1):
            output += f"RESULT {i}:\nTITLE: {r.get('title', '')}\nURL: {r.get('href', '')}\nSNIPPET: {r.get('body', '')}\n\n"
        return output.strip()
    except Exception as e:
        return f"Search failed: {str(e)}"