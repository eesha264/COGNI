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
import asyncio
from typing import Any

logger = logging.getLogger("cogni.mcp")

# LangChain BaseTool objects loaded from MCP servers. Populated by
# connect_mcp_servers() at startup; read by query_rag() on each call.
# H9 fix: protect with a lock since connect/disconnect can overlap.
_mcp_tools: list[Any] = []
_mcp_lock = asyncio.Lock()

# Holds the raw transport + session context managers so we can clean
# them up on shutdown (the async context managers are entered manually
# so the connection stays open for the lifetime of the server).
_mcp_connections: list[dict] = []


# C6 fix: timeout for connecting to each MCP server. Without this, a single
# unreachable MCP server hangs the entire FastAPI startup forever.
MCP_CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "10"))

# H11 fix: timeout for individual MCP tool calls. Prevents a hung MCP server
# from blocking a chat request indefinitely.
MCP_TOOL_TIMEOUT = float(os.getenv("MCP_TOOL_TIMEOUT", "30"))


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
    # M27 fix: check if MCP is configured BEFORE importing the MCP packages.
    # This way the server starts fine even if mcp/langchain_mcp_adapters
    # aren't installed and no MCP_SERVER_URLS are configured.
    urls_raw = os.getenv("MCP_SERVER_URLS", "")
    urls = [u.strip() for u in urls_raw.split(",") if u.strip()]

    if not urls:
        logger.info("No MCP_SERVER_URLS configured — running with local tools only.")
        return

    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from langchain_mcp_adapters.tools import load_mcp_tools

    for url in urls:
        transport = None
        session = None
        try:
            # C6 fix: wrap the entire connect+initialize+load in a timeout so
            # an unreachable server doesn't hang startup forever.
            async def _connect():
                nonlocal transport, session
                # Enter the sse_client context manually so the transport stays
                # open beyond this function call (we clean it up in disconnect).
                transport = sse_client(url=url)
                read, write = await transport.__aenter__()

                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()  # MCP handshake

                tools = await load_mcp_tools(session)
                return tools

            tools = await asyncio.wait_for(_connect(), timeout=MCP_CONNECT_TIMEOUT)
            tool_names = [t.name for t in tools]

            # H10 fix: detect tool name collisions between MCP servers and
            # local tools. Log a warning — the last-loaded tool wins (dict
            # merge semantics in rag_pipeline), which may shadow built-ins.
            existing_names = {t.name for t in _mcp_tools}
            builtin_names = {"calculator", "get_current_datetime", "search_document", "web_search"}
            for t in tools:
                if t.name in builtin_names:
                    logger.warning(f"MCP tool '{t.name}' from {url} shadows a built-in tool!")
                elif t.name in existing_names:
                    logger.warning(f"MCP tool '{t.name}' from {url} collides with a tool from another MCP server!")

            _mcp_connections.append({
                "url": url,
                "transport": transport,
                "session": session,
            })
            _mcp_tools.extend(tools)

            logger.info(f"Connected to MCP server {url}: loaded tools {tool_names}")
        except asyncio.TimeoutError:
            # Clean up partially-opened connections
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
            logger.warning(f"Timeout connecting to MCP server {url} (limit: {MCP_CONNECT_TIMEOUT}s) — skipping.")
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
    """Close all MCP connections. Called from main.py's lifespan shutdown.
    H12 fix: use a short timeout per connection so a hung MCP server
    doesn't block shutdown indefinitely."""
    async with _mcp_lock:
        for conn in _mcp_connections:
            try:
                await asyncio.wait_for(conn["session"].__aexit__(None, None, None), timeout=5)
            except Exception:
                pass
            try:
                await asyncio.wait_for(conn["transport"].__aexit__(None, None, None), timeout=5)
            except Exception:
                pass
            logger.info(f"Disconnected from MCP server {conn['url']}")

        _mcp_tools.clear()
        _mcp_connections.clear()
