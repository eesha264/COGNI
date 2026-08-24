"""
Minimal MCP server proof-of-concept.

Exposes one tool ("add") over the Model Context Protocol via stdio
transport. Run this indirectly by running mcp_client.py, which launches
this file as a subprocess automatically -- you don't need to run this
file by hand.
"""
from mcp.server import MCPServer

mcp = MCPServer("Demo Server")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


if __name__ == "__main__":
    mcp.run(transport="stdio")
