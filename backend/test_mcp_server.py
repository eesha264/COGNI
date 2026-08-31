"""Tiny MCP server for testing the backend's MCP integration."""
# L18 fix: use the correct import for mcp v1 (the backend pins mcp<2.0.0).
# In mcp v1 the class is FastMCP from mcp.server.fastmcp; in v2 it was
# renamed to MCPServer from mcp.server. The backend venv has v1.
from mcp.server.fastmcp import FastMCP

# L18 fix: port is passed to run(), not the constructor (v2 moved it).
# In v1, FastMCP accepts port in the constructor, so this works for the
# backend's pinned mcp version. If upgrading to mcp v2, change to:
#   mcp = MCPServer("Test Server")
#   mcp.run(transport="sse", port=3001)
mcp = FastMCP("Test Server", port=3001)

@mcp.tool()
def greet(name: str) -> str:
    """Greet someone by name. Use this whenever the user asks to greet someone."""
    return f"Hello, {name} from MCP!"

if __name__ == "__main__":
    mcp.run(transport="sse")
