<div align="center">
  <div style="background-color: #f97316; width: 60px; height: 60px; border-radius: 12px; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px;">
    <h1 style="color: white; margin: 0; font-family: sans-serif; font-size: 32px;"></h1>
  </div>
  <h1>COGNI</h1>
  <p><strong>A Local, Privacy-First RAG Document Analysis Chatbot</strong></p>
</div>

---

**Cogni** is an intelligent, full-stack Retrieval-Augmented Generation (RAG) application. It allows users to upload PDF documents and ask questions about them. Cogni extracts text, analyzes it, creates vector embeddings locally, and provides accurate, context-aware answers using the Groq LLM API — with built-in privacy protections and support for external tool servers via the Model Context Protocol (MCP).

---

## 🚀 Key Features

- **Privacy-First**: Built-in PII detection and redaction using Presidio, with multi-layer guardrails (input, retrieval, output) to protect sensitive information like emails, phone numbers, SSNs, PAN, and Aadhaar
- **Tool Calling**: AI uses dedicated tools for precision — calculator for math, datetime for "today" questions, web search for current events, and document search for PDF content
- **MCP Integration**: Connect to external MCP servers (GitHub, filesystem, Slack, etc.) to extend capabilities with additional tools without modifying core code
- **Anonymous Chat History**: No login required — chat histories are saved persistently via an anonymous browser `deviceId` in MongoDB Atlas
- **Local Vector Embeddings**: Document embeddings are generated entirely locally using FastEmbed and saved to ChromaDB, keeping processing fast and secure
- **Real-Time Progress UI**: Watch your document get processed in real-time with a dynamic, animated progress graph streamed via WebSocket
- **Comprehensive Testing**: 278+ tests covering security, privacy, and functionality with CI-ready test suite
- **Premium UI/UX**: React + Vite frontend with Markdown rendering (KaTeX math, syntax-highlighted code, tables), copy buttons, timestamps, and auto-scroll

---

## 🏗️ Architecture Overview

```
User (React) ──HTTP/WS──> FastAPI Backend
                            ├── PyMuPDF (PDF parsing + vision OCR)
                            ├── FastEmbed (local embeddings)
                            ├── ChromaDB (per-document vector store)
                            ├── Groq API (LLM + tool calling)
                            ├── MongoDB (chat history, per device_id)
                            ├── Presidio (PII detection & redaction)
                            ├── Tavily API (web search tool)
                            └── MCP Servers (external tools via SSE)
```

For detailed architecture diagrams, component breakdown, data flows, and deployment considerations, see the [Architecture Documentation](architecture.md).

---

## 📂 Folder Structure

```text
COGNI-main/
├── backend/                     # Python FastAPI Backend
│   ├── main.py                  # FastAPI app, endpoints, WebSocket handler
│   ├── rag_pipeline.py          # RAG logic, tool calling, PDF processing
│   ├── database.py              # MongoDB chat history, device ownership
│   ├── guardrails.py            # Privacy input rail (blocks extraction requests)
│   ├── pii_guard.py             # PII detection & redaction (Presidio)
│   ├── mcp_client_manager.py    # MCP client (external tool integration)
│   ├── requirements.txt         # Python dependencies
│   ├── test_*.py                # Test suite (278+ tests)
│   ├── uploaded_files/          # Temporary storage for uploaded PDFs
│   └── chroma_db/               # Local persistent storage for Vector Embeddings
│
├── frontend/                    # React + Vite Frontend
│   ├── index.html               # Main HTML entry point
│   ├── package.json             # Node dependencies
│   ├── public/                  # Static assets
│   └── src/                     
│       ├── App.jsx              # Main app component, state, WebSocket
│       ├── main.jsx             # React entry point
│       ├── config.js            # Backend URL configuration
│       ├── constants.js         # Shared step labels for progress graph
│       ├── index.css            # Global CSS
│       └── components/          # UI Components
│           ├── LeftSidebar.jsx  # Chat history list
│           ├── MainChat.jsx     # Chat interface, Markdown rendering
│           ├── RightSidebar.jsx # Animated progress graph
│           ├── SettingsModal.jsx # API key configuration
│           └── MarkdownRenderer.jsx # Lazy-loaded Markdown + math + code
│
├── docs/                        # Documentation
│   ├── MCP_POC.md               # MCP integration documentation
│   ├── CHANGES.md               # Change log
│   └── REVIEW.md                # Code review findings
│
├── mcp-poc/                     # Standalone MCP proof-of-concept (not connected to app)
│   ├── mcp_server.py            # Minimal MCP server (stdio transport)
│   ├── mcp_client.py            # Minimal MCP client
│   └── .venv/                    # Isolated virtual environment (not committed)
│
├── architecture.md              # Comprehensive architecture documentation
├── .gitignore                   # Git ignore rules
└── README.md                    # This file
```

---

## 💻 Tech Stack

### Frontend
- **React.js**: UI Framework
- **Vite**: Build tool and dev server
- **react-markdown**: Markdown rendering
- **KaTeX**: LaTeX math typesetting
- **highlight.js**: Syntax highlighting for code blocks
- **@lottiefiles/dotlottie-react**: Lottie animations

### Backend
- **FastAPI**: High-performance async REST & WebSocket API framework
- **Uvicorn**: ASGI server to run FastAPI
- **Motor**: Async Python driver for MongoDB Atlas

### AI & Data Pipeline (RAG)
- **LangChain**: Orchestration framework for LLMs and document splitting
- **PyMuPDF (fitz)**: High-speed PDF text extraction, table detection, vision OCR fallback
- **FastEmbed**: Lightweight, local embedding generation (BAAI/bge-small-en-v1.5)
- **Groq API**: Ultra-fast LLM inference for responses and vision OCR
- **ChromaDB**: Open-source local vector database

### Privacy & Security
- **Presidio**: PII detection and redaction (Microsoft framework)
- **spaCy**: NLP engine for Presidio (en_core_web_sm model)

### MCP Integration
- **langchain-mcp-adapters**: Converts MCP tool specs into LangChain BaseTool objects
- **mcp**: Python SDK for Model Context Protocol

---

## 🛠️ Getting Started

### Prerequisites
- Node.js (v18+)
- Python (3.10+)
- A Groq API Key (configure in UI settings)
- A MongoDB Atlas connection string (or run MongoDB locally)

### 1. Clone the repository
```bash
git clone https://github.com/eesha264/COGNI.git
cd COGNI-main
```

### 2. Setup the Backend
```bash
cd backend
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Create a `.env` file in the `backend/` directory:
```env
MONGODB_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?appName=COGNI
GROQ_API_KEY=your-groq-api-key  # Optional: can be set in UI
TAVILY_API_KEY=your-tavily-api-key  # Optional: for web search tool
```

Start the backend server:
```bash
uvicorn main:app --reload --port 8000
```

### 3. Setup the Frontend
```bash
cd frontend
npm install
npm run dev
```

### 4. Use the App
Open your browser to `http://localhost:5173`:
1. Click **Settings** (gear icon) and enter your Groq API Key
2. Click the **Upload PDF** box in the right sidebar
3. Watch the progress graph animate
4. Start asking questions about your document

---

## 🔒 Privacy & Security

### PII Protection
Cogni uses Presidio to detect and redact sensitive information across three layers:

| Layer | When it runs | What it protects |
|-------|-------------|------------------|
| **Input Rail** | Before your question reaches the LLM | Blocks extraction attempts, redacts incidental PII |
| **Retrieval Rail** | Before document chunks are sent to the LLM | Redacts PII from retrieved chunks |
| **Output Rail** | Before the LLM's answer is shown to you | Redacts PII from the final response |

**Protected entities:** Email addresses, phone numbers, credit cards, US SSN, IBAN codes, PAN (India), Aadhaar (India).

### Security Features
- UUID-based filenames (prevents path traversal)
- Upload size limits (configurable, default 100 MB)
- Device-based chat ownership (you can only access your own chats)
- Restricted CORS origins (configurable)
- Malformed chat_id handling (graceful, no 500 errors)
- Tool argument validation (prevents code injection)
- Web search quota enforcement (max 5 per conversation)

---

## 🔌 MCP Integration

Cogni can consume tools from external MCP servers (GitHub, filesystem, Slack, etc.) alongside its built-in tools.

### Configuration
Set the `MCP_SERVER_URLS` environment variable to connect to external MCP servers:

```bash
# Single MCP server
MCP_SERVER_URLS=http://localhost:3001/sse uvicorn main:app

# Multiple MCP servers
MCP_SERVER_URLS=http://localhost:3001/sse,http://localhost:3002/sse uvicorn main:app
```

If no servers are configured, Cogni runs with local tools only.

### How it works
1. At startup, the backend connects to configured MCP servers over SSE
2. Tools from these servers are merged into the LLM's tool list
3. The LLM can choose to use these tools just like local tools
4. If a server is unreachable, the app continues with available tools

For detailed MCP documentation, see [docs/MCP_POC.md](docs/MCP_POC.md).

---

## 🧪 Testing

The backend includes a comprehensive test suite with 278+ tests:

```bash
cd backend
source venv/bin/activate
pip install pytest pytest-asyncio
python test_critical_fixes.py  # Security-critical tests
python test_high_fixes.py       # High-severity tests
python test_medium_fixes.py     # Medium-severity tests
python test_low_fixes.py        # Low-severity tests
python test_guardrails.py       # Privacy guardrail tests
python test_pii_guard.py        # PII redaction tests
python test_process.py          # PDF processing tests
```

All tests can also be run with pytest:
```bash
pytest test_*.py -v
```

---

## 🔧 Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONGODB_URI` | No | — | MongoDB connection string. If absent, chat history won't persist. |
| `GROQ_API_KEY` | No | — | Server-side Groq key. Can also be provided from the UI. |
| `GROQ_MODEL` | No | `llama-3.3-70b-versatile` | Groq LLM model for chat responses. |
| `GROQ_VISION_MODEL` | No | `qwen/qwen3.6-27b` | Groq vision model for scanned PDF OCR. |
| `TAVILY_API_KEY` | No | — | Tavily API key for the `web_search` tool. |
| `ALLOWED_ORIGINS` | No | `localhost:5173,5174` | Comma-separated CORS origins. |
| `MAX_UPLOAD_BYTES` | No | `104857600` (100 MB) | Max upload file size in bytes. |
| `RAG_SCORE_THRESHOLD` | No | `1.0` | Max L2 distance for relevant search results. |
| `MCP_SERVER_URLS` | No | (empty) | Comma-separated SSE endpoints of external MCP servers. |

### Frontend Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | Backend API URL. |
| `VITE_WS_BASE_URL` | `ws://127.0.0.1:8000` | WebSocket URL. |

---

## 📊 Available Tools

The LLM can use these tools to answer your questions:

| Tool | Purpose | Source |
|------|---------|--------|
| `calculator` | Evaluate arithmetic expressions | Local |
| `get_current_datetime` | Return server date/time | Local |
| `web_search` | Search live web (Tavily API) | Local |
| `search_document` | Search the uploaded PDF | Local |
| **MCP tools** | External tools (GitHub, filesystem, etc.) | External servers |

---

## 📖 Documentation

- [Architecture Documentation](architecture.md) — Comprehensive system architecture, data flows, and deployment guide
- [MCP Documentation](docs/MCP_POC.md) — Model Context Protocol integration details
- [Change Log](docs/CHANGES.md) — Recent changes and feature additions
- [Code Review](docs/REVIEW.md) — Security and code quality findings

---

## 🚀 Deployment

### Production Checklist
- Set `ALLOWED_ORIGINS` to production frontend domain
- Set `GROQ_API_KEY` in backend environment
- Set `MONGODB_URI` to production MongoDB Atlas cluster
- Configure reverse proxy (nginx/Caddy) with HTTPS
- Run frontend in production mode (`npm run build`)
- Set process manager (systemd, supervisor) for backend

### Example nginx Configuration
```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

---