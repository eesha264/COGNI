# MCP Proof-of-Concept — Documentation

A minimal, working example of the Model Context Protocol (MCP): a Python server
exposing one tool, and a Python client that connects to it, calls the tool,
and prints the result.

## Architecture

**stdio transport** is the simplest way for a client and server to talk to
each other: the client launches the server as a subprocess and the two
communicate over standard input/output — no network, no ports, nothing to
configure. That's why it's the standard choice for a local proof-of-concept
like this one.


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

The client never imports the server's Python code directly — the two only
ever communicate through the MCP protocol messages sent over stdio. That
separation is the entire point: the server could be rewritten in a different
language and the client wouldn't need to change at all.

## Files

| File | Purpose |
|---|---|
| `mcp-poc/mcp_server.py` | Defines and exposes one tool, `add(a, b)`, over MCP via stdio |
| `mcp-poc/mcp_client.py` | Launches the server, connects, lists tools, calls `add`, prints the result |
| `mcp-poc/.venv/` | Isolated virtual environment, separate from `backend/venv/` (not committed) |
| `docs/MCP_POC.md` | This file |

This lives inside the Cogni repo as its own `mcp-poc/` folder, kept separate
from `backend/` on purpose — it's an unrelated learning exercise (a generic
MCP proof-of-concept, not a Cogni feature), so it gets its own virtual
environment rather than adding its dependencies to Cogni's actual,
carefully-trimmed backend venv.

## Setup

```bash
cd mcp-poc
python3 -m venv .venv
source .venv/bin/activate
pip install "mcp[cli]"
```

Requires Python 3.10+ (used Python 3.11.8). Installed SDK version: **`mcp` 2.0.0**.

## How to run

Run **only the client** — it launches the server itself as a subprocess:

```bash
python3 mcp_client.py
```

You do not need to run `mcp_server.py` manually; doing so would just start
it waiting on stdio with nothing connected to the other end.

## Test results — run twice, as required

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

Both runs produced identical, correct output with a clean exit code —
confirms the server starts fresh and connects successfully every time,
not just on a lucky first attempt.

## A real bug hit and fixed during this build

The first test run failed with:
```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

Most MCP tutorials (written against SDK version 1.x) show:
```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("Demo Server")
```

But the actually-installed SDK is **version 2.0.0**, where the server class
was renamed and moved. Inspected the installed package directly
(`mcp/server/mcpserver/`) to find the current correct import:
```python
from mcp.server import MCPServer
mcp = MCPServer("Demo Server")
```
Everything else (the `@mcp.tool()` decorator, `mcp.run(transport="stdio")`,
the client-side `ClientSession`/`stdio_client` API) was unchanged between
versions. This is a good example of why "install and test" matters even for
boilerplate code — tutorial code can silently go stale as a library evolves,
and the only way to be sure is to actually run it.

## Before MCP vs. after MCP (conceptually)

Without MCP, a tool is just a Python function living inside the same
process/file as the code that calls it (this is how tool-calling normally
works with frameworks like LangChain, for example).

With MCP, the tool-providing code becomes a **separate program**, and the
calling code talks to it over a standardized protocol instead of a direct
function call. Nothing about the *result* changes for a single self-contained
project — the value shows up when:
- the same tool server needs to be reused by more than one AI application, or
- the calling application wants to consume tools it didn't write itself
  (a growing ecosystem of pre-built MCP servers exists for things like
  GitHub, Slack, filesystems, browsers, etc.)

## Alternatives considered

- **High-level `Client` class** instead of `ClientSession` + `stdio_client`:
  fewer lines, but hides the handshake/transport mechanics that are the
  actual learning objective of this exercise. Used the more explicit,
  lower-level pattern on purpose.
- **MCP Inspector** (official browser/CLI tool): lets you test a server
  without writing any client code at all. Useful for future debugging, but
  would have skipped the assignment's actual goal of building a client.
- **Low-level `mcp.server.lowlevel.Server` API**: the pre-decorator way of
  defining a server, strictly more boilerplate for the same result. No
  reason to use it here.
