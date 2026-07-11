from src.tools.common import tool, ToolContext

@tool(name = "load_tool_grp", description = "Load a tool group into your working context to access its tools")
async def load_tool_grp(
    group_name : str,
    _ctx : ToolContext
):
    tool_grp = _ctx.tool_manager.get_group(group_name)
    if tool_grp is None:
        return f"Unknown tool group '{group_name}'. Available: {_ctx.tool_manager.get_group_names()}"
    expanded = tool_grp.render_group_schema()
    _ctx.scratchpad.load_group(group_name, expanded)
    return f"Tool group '{group_name}' loaded. You can now use: {[t.name for t in tool_grp.tools]}"