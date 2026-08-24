"""
Minimal MCP client proof-of-concept.

Launches mcp_server.py as a subprocess over stdio, performs the MCP
handshake, lists the server's available tools, calls the "add" tool,
and prints the result -- proving end-to-end connectivity.

Run with: python mcp_client.py
"""
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command=sys.executable,  # same interpreter/venv running this client
    args=["mcp_server.py"],
)


async def main():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()  # MCP handshake

            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])

            result = await session.call_tool("add", {"a": 3, "b": 4})
            print("Result of add(3, 4):", result.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
