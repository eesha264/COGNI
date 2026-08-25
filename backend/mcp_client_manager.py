"""
MCP client manager — connects to external MCP servers over SSE transport
and loads their tools as LangChain BaseTool objects so query_rag can bind
them to the LLM alongside the built-in local tools.

Why SSE and not stdio:
  stdio requires launching a subprocess per connection and only supports
  one client. A web server has concurrent users — SSE (HTTP-based) lets
  one MCP server serve many clients, and is the transport real external
  MCP servers (GitHub, filesystem, etc.) expose.

Configure via the MCP_SERVER_URLS env var (comma-separated SSE endpoints):
  MCP_SERVER_URLS=http://localhost:3001/sse,http://localhost:3002/sse

If no servers are configured or a server is unreachable, query_rag simply
falls back to the local tools only — MCP is an extension layer, not a
hard dependency.
"""
from __future__ import annotations

import os
import logging
from typing import Any

logger = logging.getLogger("cogni.mcp")

# LangChain BaseTool objects loaded from MCP servers. Populated by
# connect_mcp_servers() at startup; read by query_rag() on each call.
_mcp_tools: list[Any] = []

# Holds the raw transport + session context managers so we can clean
# them up on shutdown (the async context managers are entered manually
# so the connection stays open for the lifetime of the server).
_mcp_connections: list[dict] = []


def get_mcp_tools() -> list[Any]:
    """Return the list of LangChain tools loaded from external MCP servers."""
    return list(_mcp_tools)


async def connect_mcp_servers() -> None:
    """Connect to all MCP servers listed in MCP_SERVER_URLS and load their tools.

    Called once from main.py's lifespan startup. Each server that connects
    successfully contributes its tools to the global pool. A server that
    fails to connect is logged and skipped — the app continues with
    whatever tools are available.
    """
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from langchain_mcp_adapters.tools import load_mcp_tools

    urls_raw = os.getenv("MCP_SERVER_URLS", "")
    urls = [u.strip() for u in urls_raw.split(",") if u.strip()]

    if not urls:
        logger.info("No MCP_SERVER_URLS configured — running with local tools only.")
        return

    for url in urls:
        transport = None
        session = None
        try:
            # Enter the sse_client context manually so the transport stays
            # open beyond this function call (we clean it up in disconnect).
            transport = sse_client(url=url)
            read, write = await transport.__aenter__()

            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()  # MCP handshake

            tools = await load_mcp_tools(session)
            tool_names = [t.name for t in tools]

            _mcp_connections.append({
                "url": url,
                "transport": transport,
                "session": session,
            })
            _mcp_tools.extend(tools)

            logger.info(f"Connected to MCP server {url}: loaded tools {tool_names}")
        except Exception as e:
            # Clean up partially-opened connections so transports don't leak.
            # If session.__aenter__ or load_mcp_tools failed after the
            # transport opened, we need to close the transport explicitly.
            if session is not None:
                try:
                    await session.__aexit__(None, None, None)
                except Exception:
                    pass
            if transport is not None:
                try:
                    await transport.__aexit__(None, None, None)
                except Exception:
                    pass
            logger.warning(f"Failed to connect to MCP server {url}: {e}")


async def disconnect_mcp_servers() -> None:
    """Close all MCP connections. Called from main.py's lifespan shutdown."""
    for conn in _mcp_connections:
        try:
            await conn["session"].__aexit__(None, None, None)
        except Exception:
            pass
        try:
            await conn["transport"].__aexit__(None, None, None)
        except Exception:
            pass
        logger.info(f"Disconnected from MCP server {conn['url']}")

    _mcp_tools.clear()
    _mcp_connections.clear()
