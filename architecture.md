# COGNI Architecture

## Overview

COGNI is a full-stack Retrieval-Augmented Generation (RAG) application. Users
upload PDF documents and ask questions about them. The backend extracts text,
generates local vector embeddings, stores them in ChromaDB, and uses the Groq
LLM API to answer questions grounded in the document.

## 1. Backend (FastAPI + LangChain + Groq)

The brain of the application — a RAG system with the following components:

### PDF Parsing (`rag_pipeline.py`)
- Uses **PyMuPDF (fitz)** to read uploaded PDFs page-by-page
- Extracts embedded digital text, structured tables (Markdown), and image metadata
- Falls back to a **Groq vision model** (Llama 4 Scout) for scanned/handwritten pages
- Skips vision calls on truly blank pages (no text, no images)
- Rejects PDFs longer than 400 pages

### Local Embeddings
- Uses **FastEmbed** with the `BAAI/bge-small-en-v1.5` model
- Runs entirely locally — no API key needed for embeddings

### Vector Database (ChromaDB)
- Embeddings saved locally to `./chroma_db`
- **Per-document collections** (`pdf_{document_id}`) — each upload gets its own collection
- `similarity_search_with_score` with a configurable score threshold (`RAG_SCORE_THRESHOLD`)
- Thread-safe access via `_vector_store_lock`

### The LLM (Groq)
- Strict system prompt instructs the AI to ground answers in tool results
- Tool-calling loop with `calculator`, `get_current_datetime`, `web_search`, and `search_document`
- Bounded to `max_tool_rounds=5`; falls back to a no-tools invoke if exceeded
- Default model: `llama-3.3-70b-versatile` (configurable via `GROQ_MODEL`)

### Database (`database.py`)
- Uses **MongoDB** (via Motor async driver) to save chat histories
- Per-device chat ownership checks (`device_id`)
- Validates `chat_id` via `_to_objectid()` (no 500 on malformed IDs)
- Gracefully degrades when `MONGODB_URI` is not set

### WebSockets
- `/ws/process?device_id=...` streams real-time upload progress
- Broadcasts are **scoped per `device_id`** — users only see their own progress

## 2. Frontend (React + Vite)

A React application (`frontend/src`) that provides:
- Chat UI with Markdown rendering (KaTeX math, syntax-highlighted code, tables)
- File upload with live WebSocket progress graph
- Settings modal for Groq API key (stored in `sessionStorage`)
- Anonymous device ID for chat history (stored in `localStorage`)

## API Contract

| Method | Endpoint | Parameters | Returns |
|--------|----------|------------|---------|
| `POST` | `/upload` | `file` (PDF), `device_id`, `api_key` (optional) | `{info, chat_id, document_id}` |
| `POST` | `/chat` | `message`, `api_key`, `device_id`, `chat_id` (optional) | `{response, chat_id, tools_used, source_pages, db_warning?}` |
| `GET` | `/chats/{device_id}` | — | `{chats: [{id, title, created_at}]}` |
| `GET` | `/chat/{chat_id}?device_id=...` | `device_id` (query) | `{messages: [{role, content, timestamp}]}` |
| `DELETE` | `/chat/{chat_id}?device_id=...` | `device_id` (query) | `{message}` |
| `WS` | `/ws/process?device_id=...` | `device_id` (query) | `{step}` or `{error}` |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONGODB_URI` | No | — | MongoDB connection string. If absent, chat history won't persist. |
| `GROQ_API_KEY` | No | — | Server-side Groq key. Can also be provided per-request from the UI. |
| `GROQ_MODEL` | No | `llama-3.3-70b-versatile` | Groq LLM model for chat responses. |
| `GROQ_VISION_MODEL` | No | `meta-llama/llama-4-scout-17b-16e-instruct` | Groq vision model for scanned PDF OCR. |
| `TAVILY_API_KEY` | No | — | Tavily API key for the `web_search` tool. |
| `ALLOWED_ORIGINS` | No | `localhost:5173,5174` | Comma-separated CORS origins. |
| `MAX_UPLOAD_BYTES` | No | `104857600` (100 MB) | Max upload file size in bytes. |
| `RAG_SCORE_THRESHOLD` | No | `1.0` | Max L2 distance for relevant search results. |

## Architecture Diagram

```
User (React) ──HTTP/WS──> FastAPI Backend
                            ├── PyMuPDF (PDF parsing)
                            ├── FastEmbed (local embeddings)
                            ├── ChromaDB (per-document vector store)
                            ├── Groq API (LLM + vision OCR)
                            ├── MongoDB (chat history, per device_id)
                            └── Tavily API (web search tool)
```
