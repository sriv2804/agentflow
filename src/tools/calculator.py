import math
from src.tools.common import tool

@tool(name="calculator", description="Evaluate a mathematical expression")
async def calculator(expression: str, _ctx = None) -> str:
    """
    Evaluate a mathematical expression safely.

    Args:
        expression: A valid Python math expression e.g. '2 + 2' or 'math.sqrt(16)'
    """
    try:
        allowed_names = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return str(result)
    except Exception as e:
        return f"Error: {str(e)}"