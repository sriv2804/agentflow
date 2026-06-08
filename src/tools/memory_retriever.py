from src.core.memory import MemoryManager

@tool
async def memory_retriever(query: str, memory_manager: MemoryManager) -> str:
    """
    search agent's long term corpus for context relevant to the query
    Use this when you need context from past conversation or sessions
    """
    return ""