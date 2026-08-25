"""Tiny MCP server for testing the backend's MCP integration."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Test Server", port=3001)

@mcp.tool()
def greet(name: str) -> str:
    """Greet someone by name. Use this whenever the user asks to greet someone."""
    return f"Hello, {name} from MCP!"

if __name__ == "__main__":
    mcp.run(transport="sse")
