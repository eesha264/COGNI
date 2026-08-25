# Model Context Protocol (MCP) — Documentation

This document covers the full MCP implementation in the Cogni project, from
the initial proof-of-concept to the production backend integration.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Proof-of-Concept (mcp-poc/)](#3-proof-of-concept-mcp-poc)
4. [Backend Integration](#4-backend-integration)
5. [Design Decisions](#5-design-decisions)
6. [Configuration](#6-configuration)
7. [Setup & Installation](#7-setup--installation)
8. [Testing](#8-testing)
9. [Files Reference](#9-files-reference)
10. [Alternatives Considered](#10-alternatives-considered)
11. [Known Limitations & Future Work](#11-known-limitations--future-work)

---

## 1. Overview

The Model Context Protocol (MCP) is an open standard that lets AI
applications communicate with external tool servers over a standardized
protocol, instead of calling tools as in-process functions. This
decouples tool providers from tool consumers — a single MCP server can
be reused by any MCP-compatible client, and a client can consume tools
from servers it didn't write.

Cogni's MCP implementation has two phases:

| Phase | Location | Status | Purpose |
|-------|----------|--------|---------|
| Phase 1 — Proof of Concept | `mcp-poc/` | Complete | Standalone learning exercise: a minimal MCP server + client over stdio transport |
| Phase 2 — Backend Integration | `backend/` | Complete | Production integration: the Cogni backend connects to external MCP servers over SSE and merges their tools into the LLM's tool list |

**Phase 1** is a self-contained demo that proves end-to-end MCP
connectivity. It is intentionally isolated from the backend — it has
its own virtual environment and is not imported by any backend code.

**Phase 2** is the production feature. The backend's `query_rag()`
function can now use tools from external MCP servers alongside its
existing built-in tools (`calculator`, `get_current_datetime`,
`web_search`, `search_document`). MCP is an additive extension layer —
if no MCP servers are configured, the app behaves exactly as before.

---

## 2. Architecture

### Phase 1 — Proof of Concept (stdio)

```
┌─────────────────┐         stdio (stdin/stdout)         ┌─────────────────┐
│  mcp_client.py   │  ───────────────────────────────►   │  mcp_server.py   │
│                  │                                       │                  │
│ 1. launches the  │  ◄───────────────────────────────   │  exposes one     │
│    server as a   │      tool list / tool call result     │  tool: add(a,b)  │
│    subprocess    │                                       │                  │
│ 2. does the MCP  │                                       │                  │
│    handshake     │                                       │                  │
│ 3. calls add()   │                                       │                  │
│ 4. prints result │                                       │                  │
└─────────────────┘                                       └─────────────────┘
```

The client never imports the server's Python code directly — the two
communicate only through MCP protocol messages over stdio. The server
could be rewritten in a different language and the client wouldn't
change.

### Phase 2 — Backend Integration (SSE)

```
┌──────────────────────────────────────────────────────────────┐
│  Cogni Backend (FastAPI)                                     │
│                                                              │
│  App Startup (lifespan):                                     │
│    database.connect_db()                                     │
│    mcp_client_manager.connect_mcp_servers()  ──── SSE ────┐  │
│                                                              │  │
│  /chat endpoint:                                            │  │
│    result = await query_rag(...)                            │  │
│           │                                                 │  │
│           ▼                                                 │  │
│  ┌─────────────────────────────────────────┐               │  │
│  │  query_rag() [async]                    │               │  │
│  │                                         │               │  │
│  │  Local tools (in-process):              │               │  │
│  │    calculator                           │               │  │
│  │    get_current_datetime                 │               │  │
│  │    search_document                      │               │  │
│  │    web_search                           │               │  │
│  │                                         │               │  │
│  │  + MCP tools (from external servers):   │               │  │
│  │    <BaseTool> ◄── get_mcp_tools() ──────┼───────────────┼──┘
│  │    <BaseTool>                           │               │
│  │    ...                                  │               │
│  │                                         │               │
│  │  llm.bind_tools(local + mcp)            │               │
│  │  await llm.ainvoke(messages)            │               │
│  └─────────────────────────────────────────┘               │
│                                                              │
│  App Shutdown (lifespan):                                    │
│    mcp_client_manager.disconnect_mcp_servers()              │
└──────────────────────────────────────────────────────────────┘
                               │
                        SSE / HTTP transport
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│  MCP Server A    │ │  MCP Server B    │ │  MCP Server C    │
│  (e.g. GitHub)   │ │  (e.g. Filesystem)│ │  (e.g. custom)  │
│                  │ │                  │ │                  │
│  Tools:          │ │  Tools:          │ │  Tools:          │
│   list_repos     │ │   read_file      │ │   greet          │
│   create_issue   │ │   write_file     │ │   ...            │
│   ...            │ │   ...            │ │                  │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

**Key flow:**

1. At startup, `connect_mcp_servers()` reads `MCP_SERVER_URLS` from the
   environment, connects to each SSE endpoint, performs the MCP
   handshake, and loads tools via `langchain-mcp-adapters`.
2. On each `/chat` request, `query_rag()` merges local tools with MCP
   tools, binds them all to the Groq LLM, and invokes. The LLM picks
   whichever tool fits the question — local tools run in-process, MCP
   tools call out over SSE.
3. At shutdown, `disconnect_mcp_servers()` closes all SSE connections.

---

## 3. Proof-of-Concept (mcp-poc/)

A minimal, standalone MCP server + client that proves end-to-end
connectivity. Kept separate from `backend/` on purpose — it's a
learning exercise, not a Cogni feature.

### Setup

```bash
cd mcp-poc
python3 -m venv .venv
source .venv/bin/activate
pip install "mcp[cli]"
```

Requires Python 3.10+ (tested with 3.11.8). Installed SDK version:
**`mcp` 2.0.0**.

### Running

Run **only the client** — it launches the server as a subprocess:

```bash
python3 mcp_client.py
```

### Test Results

**Run 1:**
```
$ python3 mcp_client.py
Available tools: ['add']
Result of add(3, 4): 7
exit code: 0
```

**Run 2 (repeated to confirm consistency):**
```
$ python3 mcp_client.py
Available tools: ['add']
Result of add(3, 4): 7
exit code: 0
```

Both runs produced identical, correct output with a clean exit code.

### Bug Encountered During POC Build

The first test run failed with:
```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

Most MCP tutorials (written against SDK 1.x) show:
```python
from mcp.server.fastmcp import FastMCP
```

The installed SDK (2.0.0) moved the server class. The correct import
for this version is:
```python
from mcp.server import MCPServer
```

This is a good example of why "install and test" matters — tutorial
code can go stale as a library evolves.

> **Note:** The backend integration (Phase 2) uses `FastMCP` from
> `mcp.server.fastmcp`, which is the correct import for the `mcp`
> version installed in the backend venv (`mcp` 1.29.1 via
> `langchain-mcp-adapters`). The POC's separate venv has a different
> `mcp` version. The two venvs are intentionally isolated.

---

## 4. Backend Integration

### What Changed

The backend's `query_rag()` function was extended to use tools from
external MCP servers alongside its existing built-in tools. Four files
were modified or created:

| File | Change | Description |
|------|--------|-------------|
| `backend/mcp_client_manager.py` | **New** | MCP client manager — connects to external servers at startup, loads tools, manages lifecycle |
| `backend/main.py` | Modified | Wired manager into FastAPI lifespan; changed `query_rag` call from `asyncio.to_thread` to `await` |
| `backend/rag_pipeline.py` | Modified | Made `query_rag` async; merged MCP tools into tool list; converted `.invoke()` to `.ainvoke()` |
| `backend/requirements.txt` | Modified | Added `langchain-mcp-adapters==0.3.2` |

### mcp_client_manager.py

The MCP client manager is responsible for:

- **Connecting** to external MCP servers over SSE at app startup
- **Loading** tools from each server as LangChain `BaseTool` objects
- **Providing** those tools to `query_rag()` via `get_mcp_tools()`
- **Cleaning up** all connections at app shutdown

```python
# Key API
get_mcp_tools()                    # → list[BaseTool] (called per /chat request)
await connect_mcp_servers()        # → called at startup
await disconnect_mcp_servers()     # → called at shutdown
```

**Connection lifecycle:**

```
connect_mcp_servers()
  │
  ├─ Read MCP_SERVER_URLS from env (comma-separated SSE endpoints)
  ├─ For each URL:
  │    ├─ Open SSE transport (sse_client)
  │    ├─ Create ClientSession
  │    ├─ MCP handshake (session.initialize)
  │    ├─ Load tools (load_mcp_tools)
  │    └─ Store in _mcp_tools + _mcp_connections
  │
  └─ On failure: log warning, clean up partial connection, continue

disconnect_mcp_servers()
  │
  ├─ For each connection:
  │    ├─ Close session (__aexit__)
  │    └─ Close transport (__aexit__)
  └─ Clear tool and connection lists
```

**Error handling:** If a server is unreachable, the connection failure
is logged as a warning and skipped. The app continues with whatever
tools are available. Partially-opened connections (where the transport
opened but the session or tool loading failed) are explicitly cleaned
up to prevent resource leaks.

### main.py Changes

The MCP manager is wired into FastAPI's lifespan context manager:

```python
from mcp_client_manager import connect_mcp_servers, disconnect_mcp_servers

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.connect_db()
    await connect_mcp_servers()    # ← new
    yield
    await disconnect_mcp_servers() # ← new
```

The `/chat` endpoint was changed from running `query_rag` in a thread
to awaiting it directly, since `query_rag` is now async:

```python
# Before (sync query_rag):
result = await asyncio.to_thread(query_rag, message, groq_api_key, history, document_id)

# After (async query_rag):
result = await query_rag(message, groq_api_key, history, document_id)
```

### rag_pipeline.py Changes

Three changes:

**1. `query_rag` is now async:**
```python
# Before:
def query_rag(question, groq_api_key, history=None, document_id=None):

# After:
async def query_rag(question, groq_api_key, history=None, document_id=None):
```

**2. MCP tools are merged into the tool list:**
```python
mcp_tools = get_mcp_tools()
call_tools = AVAILABLE_TOOLS + [search_document, web_search] + mcp_tools
tools_by_name = {
    **_TOOLS_BY_NAME,
    "search_document": search_document,
    "web_search": web_search,
    **{t.name: t for t in mcp_tools},
}
```

**3. All LLM/tool invocations use `.ainvoke()` instead of `.invoke()`:**
```python
# Before:
ai_msg = llm_with_tools.invoke(messages)
result = tool_fn.invoke(args)

# After:
ai_msg = await llm_with_tools.ainvoke(messages)
result = await tool_fn.ainvoke(args)
```

`.ainvoke()` works for both sync and async tools — LangChain wraps
sync tools automatically, so the existing local tools (`calculator`,
`search_document`, etc.) required no changes.

---

## 5. Design Decisions

### SSE transport, not stdio

The POC uses stdio (subprocess per connection). That works for a
single-run script but not for a multi-user web server:

| | stdio | SSE |
|---|---|---|
| Concurrency | One subprocess per client | One server serves many clients |
| Lifecycle | Spawn + cleanup per session | Persistent connection |
| External servers | Local only (must launch subprocess) | Can connect to remote/third-party servers |
| Latency | Spawn + handshake every session | Persistent connection, lower per-call overhead |

SSE is the transport that real external MCP servers (GitHub,
filesystem, Slack) expose. stdio cannot reach them.

### MCP is additive, not a replacement

The existing local tools — especially `search_document` — depend on
per-request state (the document's Chroma vector store, source page
tracking, conversation search budget). Those stay as local LangChain
tools. MCP tools are merged in alongside them. If no MCP servers are
configured, `get_mcp_tools()` returns an empty list and nothing
changes.

### `search_document` stays local

`search_document` is the core RAG tool and is stateful — it captures
the per-document Chroma vector store, mutates a source-pages list for
the response, and depends on an in-memory cache. Externalizing it to
MCP would require giving the MCP server its own Chroma access or
calling back into the backend (circular). The right architecture is
hybrid: stateful tools stay local, stateless tools come from MCP.

### Graceful degradation

If an MCP server is unreachable, it's logged as a warning and skipped.
The app runs with whatever tools are available. A bad MCP server never
breaks the core RAG functionality.

### Async conversion

MCP's `ClientSession` is async-only. The existing `query_rag` was
synchronous (run via `asyncio.to_thread`). Converting it to async and
using `.ainvoke()` was the clean fix — no nested event loops, no
deadlock risk.

---

## 6. Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCP_SERVER_URLS` | No | (empty) | Comma-separated SSE endpoints of external MCP servers. If empty, the app runs with local tools only. |

### Example

```bash
# Single MCP server
MCP_SERVER_URLS=http://localhost:3001/sse

# Multiple MCP servers
MCP_SERVER_URLS=http://localhost:3001/sse,http://localhost:3002/sse

# No MCP servers (default — local tools only)
# (don't set the variable, or set it to empty)
```

Add to `backend/.env` or export in the shell before starting the server.

### Dependency

| Package | Version | Purpose |
|---------|---------|---------|
| `langchain-mcp-adapters` | `0.3.2` | Converts MCP tool specs into LangChain `BaseTool` objects so `bind_tools` works unchanged |

---

## 7. Setup & Installation

### Backend (with MCP support)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### Running the Backend

**Without MCP servers (default):**
```bash
uvicorn main:app --reload
```
The app starts and works with local tools only. Logs will show:
```
INFO:cogni.mcp:No MCP_SERVER_URLS configured — running with local tools only.
```

**With MCP servers:**
```bash
MCP_SERVER_URLS=http://127.0.0.1:3001/sse uvicorn main:app --reload
```
Logs will show:
```
INFO:cogni.mcp:Connected to MCP server http://127.0.0.1:3001/sse: loaded tools ['greet']
```

### Running a Test MCP Server

A minimal test server is included at `backend/test_mcp_server.py`:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Test Server", port=3001)

@mcp.tool()
def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name} from MCP!"

if __name__ == "__main__":
    mcp.run(transport="sse")
```

**Terminal 1 — start the MCP server:**
```bash
cd backend && source venv/bin/activate && python test_mcp_server.py
```

**Terminal 2 — start the backend:**
```bash
cd backend && source venv/bin/activate
MCP_SERVER_URLS=http://127.0.0.1:3001/sse uvicorn main:app --reload
```

**Terminal 3 — test it:**
```bash
curl -X POST http://127.0.0.1:8000/chat \
  -F "message=Please greet Alice" \
  -F "device_id=test"
```

Expected response:
```json
{
    "response": "Hello, Alice from MCP!",
    "chat_id": "...",
    "tools_used": ["greet"],
    "source_pages": []
}
```

---

## 8. Testing

### Existing Test Suite

All 278+ existing tests pass after the MCP integration. Two test files
were updated to reflect the async conversion:

| Test File | Change | Reason |
|-----------|--------|--------|
| `test_critical_fixes.py` | C10 test updated | Was checking for `asyncio.to_thread(query_rag` → now checks for `async def query_rag` and `await query_rag(` |
| `test_high_fixes.py` | H10, H11 tests updated | Were checking for `.invoke(` → now checks for `.ainvoke(` |

### Test Results

| Test File | Tests | Result |
|-----------|-------|--------|
| `test_critical_fixes.py` | 90 | Pass |
| `test_high_fixes.py` | 60 | Pass |
| `test_medium_fixes.py` | 88 | Pass |
| `test_low_fixes.py` | 40 | Pass |
| `test_guardrails.py` | all | Pass |
| `test_pii_guard.py` | all | Pass |
| `test_process.py` | 1 | Pass |
| **Total** | **278+** | **0 failures** |

### Live Integration Tests

Three scenarios were verified:

| Scenario | Result |
|----------|--------|
| No MCP server configured | App boots, `/chat` works with local tools |
| MCP server unreachable (port 9999) | App boots with warning, `/chat` works with local tools |
| MCP server reachable (port 3001) | App boots, connects, LLM calls MCP `greet` tool successfully |
| Local + MCP tools together | `calculator` (local) and `greet` (MCP) both work in same session |

### Linting

| Check | Result |
|-------|--------|
| `py_compile` (all backend files) | Clean |
| `pyflakes` on `mcp_client_manager.py` | Clean |
| `pyflakes` on `main.py` | One pre-existing warning (`shutil` unused — not from MCP changes) |

---

## 9. Files Reference

### MCP Integration Files

| File | Location | Purpose |
|------|----------|---------|
| `mcp_client_manager.py` | `backend/` | MCP client manager — SSE connections, tool loading, lifecycle |
| `test_mcp_server.py` | `backend/` | Minimal MCP server for manual testing |
| `main.py` | `backend/` | FastAPI app — lifespan wiring, async `query_rag` call |
| `rag_pipeline.py` | `backend/` | Async `query_rag`, MCP tool merging, `.ainvoke()` |
| `requirements.txt` | `backend/` | Added `langchain-mcp-adapters==0.3.2` |

### Proof-of-Concept Files

| File | Location | Purpose |
|------|----------|---------|
| `mcp_server.py` | `mcp-poc/` | Standalone MCP server exposing `add(a, b)` over stdio |
| `mcp_client.py` | `mcp-poc/` | Standalone MCP client — launches server, calls `add`, prints result |
| `.venv/` | `mcp-poc/` | Isolated virtual environment (not committed) |

---

## 10. Alternatives Considered

### Transport: stdio vs SSE vs Streamable HTTP

- **stdio** (used in POC): Simplest, but one subprocess per connection.
  Unsuitable for a multi-user web server.
- **SSE** (chosen): HTTP-based, supports many clients, is what external
  MCP servers expose. Right fit for the backend.
- **Streamable HTTP**: Newer MCP transport, potentially lower overhead
  than SSE. Not yet widely supported by external servers. Can be
  swapped in later by changing one line in `mcp_client_manager.py`.

### Tool integration: replace vs extend

- **Replace all tools with MCP** (rejected): `search_document` is
  stateful and depends on per-request closures. Externalizing it
  requires giving the MCP server Chroma access or calling back into
  the backend (circular).
- **Extend with MCP** (chosen): Local tools stay as-is. MCP tools are
  merged in alongside them. Hybrid architecture — stateful tools
  local, stateless tools from MCP.

### Client API: high-level vs low-level

- **High-level `Client` class** (rejected for POC): Fewer lines, but
  hides the handshake/transport mechanics that were the learning
  objective of the POC.
- **`ClientSession` + `stdio_client`/`sse_client`** (chosen): More
  explicit, shows the actual protocol mechanics.

### MCP Inspector

The official MCP Inspector (browser/CLI tool) lets you test a server
without writing client code. Useful for future debugging, but skipped
the assignment's goal of building a client.

---

## 11. Known Limitations & Future Work

### Current Limitations

- **No reconnection:** If an MCP server goes down after startup, the
  connection is not automatically re-established. A server restart is
  required.
- **No tool name collision detection:** If an MCP server exposes a
  tool with the same name as a local tool (e.g., `calculator`), the
  MCP version silently overwrites the local one in `tools_by_name`.
- **No per-user MCP tools:** All MCP tools are global — every user
  gets the same set. There's no mechanism to scope MCP tools per
  device or session.
- **No dynamic server discovery:** MCP server URLs must be configured
  at startup via `MCP_SERVER_URLS`. Servers can't be added or removed
  at runtime.

### Future Work

- **Reconnection logic:** Detect dropped SSE connections and retry
  with backoff.
- **Tool name collision warnings:** Log a warning if an MCP tool name
  conflicts with a local tool, and skip the conflicting MCP tool.
- **Streamable HTTP transport:** Swap SSE for the newer streamable
  HTTP transport when external servers support it.
- **Exposing Cogni tools as an MCP server:** Currently the backend is
  an MCP *client* only. Exposing `calculator` and
  `get_current_datetime` as an MCP server would let other AI
  applications use them. `search_document` would require giving the
  MCP server its own Chroma access.
- **Per-user MCP tool scoping:** Allow different users to connect to
  different MCP servers based on their configuration.
