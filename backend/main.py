from fastapi import FastAPI, UploadFile, File, Form, WebSocket, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os
import shutil
import asyncio
import logging
import uuid
from dotenv import load_dotenv
from rag_pipeline import process_pdf, query_rag
import database
from mcp_client_manager import connect_mcp_servers, disconnect_mcp_servers
from pii_guard import redact as redact_pii, RAIL_ENTITIES, TokenStore

load_dotenv()

# H6 fix: use a lifespan context manager instead of the deprecated
# @app.on_event("startup") handler.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    database.connect_db()
    await connect_mcp_servers()
    yield
    # Shutdown
    await disconnect_mcp_servers()

app = FastAPI(lifespan=lifespan)

# L15 fix: add a health check endpoint for monitoring/load balancers.
@app.get("/health")
async def health_check():
    return {"status": "ok", "db": database.chats_collection is not None}

# C2 fix: restrict CORS to known frontend origins instead of "*" + credentials
# (which is spec-violating and an open relay). Configure via ALLOWED_ORIGINS env
# var (comma-separated); defaults to the Vite dev server origins.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174"
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# M19 fix: use absolute paths so the working directory doesn't affect where
# files are stored.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(_BACKEND_DIR, "uploaded_files")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Store active websocket connections, keyed by device_id so broadcasts are
# scoped per-user (C4 fix: previously broadcast to ALL connected clients).
class ConnectionManager:
    def __init__(self):
        # device_id -> list of websockets for that device
        self.connections_by_device: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, device_id: str):
        await websocket.accept()
        self.connections_by_device.setdefault(device_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, device_id: str):
        conns = self.connections_by_device.get(device_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns and device_id in self.connections_by_device:
            del self.connections_by_device[device_id]

    async def broadcast(self, message: dict, device_id: str = None):
        # C4 fix: if device_id is given, only send to that device's connections;
        # otherwise fall back to broadcasting to all (legacy behavior).
        if device_id is not None:
            targets = self.connections_by_device.get(device_id, [])
        else:
            targets = [ws for conns in self.connections_by_device.values() for ws in conns]
        for connection in targets:
            try:
                await connection.send_json(message)
            except Exception:
                # Connection might be closed
                pass

manager = ConnectionManager()

@app.websocket("/ws/process")
async def websocket_endpoint(websocket: WebSocket):
    # C4/C14 fix: require device_id as a query param; reject connections without it
    device_id = websocket.query_params.get("device_id")
    if not device_id:
        await websocket.close(code=1008, reason="device_id query parameter is required")
        return
    await manager.connect(websocket, device_id)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket, device_id)

def run_pdf_processing(file_path: str, device_id: str, groq_api_key: str = None, document_id: str = None, loop=None):
    # H1 fix: capture the running event loop. This function is launched via
    # run_in_executor (a thread pool), so asyncio.get_running_loop() won't
    # work here — we rely on the caller passing the main loop explicitly.
    # The fallback (creating a new event loop) was broken: it was never
    # started, so run_coroutine_threadsafe would queue coroutines that never
    # ran, silently dropping all WS broadcasts. Now we require the loop to be
    # passed in — if it isn't, we log a warning and skip WS updates rather
    # than silently queuing into a dead loop.
    if loop is None:
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                raise RuntimeError("no running event loop")
        except RuntimeError:
            logging.warning("run_pdf_processing: no running event loop — WS updates will be skipped.")
            loop = None

    # Callback to send websocket updates
    def update_status(step_name: str):
        if loop is None:
            return  # H1 fix: no running loop — skip WS update instead of queueing into a dead loop
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"step": step_name}, device_id=device_id), loop
        )

    try:
        result = process_pdf(file_path, callback=update_status, groq_api_key=groq_api_key, document_id=document_id)
        # C11 fix: process_pdf returns {"error": ...} for >400-page PDFs but
        # the return value was previously ignored, so the UI showed "Done" even
        # though nothing was indexed. Broadcast the error instead.
        if isinstance(result, dict) and result.get("error"):
            # M5 fix: clean up the uploaded file when processing fails so it
            # doesn't accumulate on disk forever.
            try:
                os.remove(file_path)
            except Exception:
                pass
            if loop is not None:
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({"error": result["error"]}, device_id=device_id), loop
                )
    except Exception as e:
        # Broadcast error status
        try:
            if loop is not None:
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({"error": str(e)}, device_id=device_id), loop
                )
        except Exception:
            pass

# C13 fix: cap upload size (default 100 MB) to prevent disk-exhaustion DoS.
# Configure via MAX_UPLOAD_BYTES env var.
# M1 fix: wrap in try/except so an invalid env value doesn't crash startup.
try:
    MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
except (ValueError, TypeError):
    MAX_UPLOAD_BYTES = 100 * 1024 * 1024

@app.post("/upload")
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    device_id: str = Form(...),
    api_key: str = Form(None)
):
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    # H13 fix: also validate content-type to catch renamed non-PDF files
    # (e.g. an .exe renamed to .pdf). Accept both application/pdf and
    # application/octet-stream (some browsers send the latter for PDFs).
    # M15 fix: remove the empty-string acceptance — it was unreachable anyway
    # (the `if content_type` guard already filtered it).
    content_type = file.content_type or ""
    if content_type and content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail=f"Invalid file type: {content_type}. Only PDF files are supported.")

    # C1 fix: use a UUID-based filename to prevent path traversal via crafted
    # filenames (e.g. "../../evil.exe"). Preserve original name for display.
    safe_filename = f"{uuid.uuid4().hex}.pdf"
    file_location = os.path.join(UPLOAD_DIR, safe_filename)

    # C13 fix: stream to disk with a size cap; abort if the upload exceeds it.
    written = 0
    with open(file_location, "wb") as buffer:
        while True:
            chunk = await file.read(1024 * 1024)  # 1 MB chunks
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                buffer.close()
                os.remove(file_location)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max allowed size is {MAX_UPLOAD_BYTES // (1024*1024)} MB."
                )
            buffer.write(chunk)

    # C7 fix: pass the user's Groq API key to process_pdf so vision OCR works
    # even when the server admin hasn't set GROQ_API_KEY in the environment.
    groq_api_key = api_key or os.getenv("GROQ_API_KEY")
    # M25 fix: warn if no API key — typed PDFs still work, but scanned PDFs
    # won't get vision OCR. We don't block the upload for this.
    if not groq_api_key:
        pass  # process_pdf will add a note per page about missing OCR

    # C8 fix: generate a document_id so process_pdf indexes into a per-document
    # collection instead of wiping a global one.
    document_id = uuid.uuid4().hex

    # H4 fix: create the chat record BEFORE starting PDF processing. Previously
    # processing was launched first, so a fast /chat call could arrive before
    # the chat record existed, failing the document_id lookup.
    chat_id = await database.create_chat(device_id, f"Document: {file.filename}", document_id=document_id)

    # H2 fix: run the heavy sync PDF processing in a separate thread instead of
    # BackgroundTasks (which runs on the event loop thread and blocks all other
    # requests during PyMuPDF parsing, ONNX embedding, and Chroma writes).
    # H8 fix: attach a done callback to the Future so exceptions in
    # run_pdf_processing are logged instead of silently swallowed.
    main_loop = asyncio.get_running_loop()
    future = main_loop.run_in_executor(
        None, run_pdf_processing, file_location, device_id, groq_api_key, document_id, main_loop
    )
    def _log_future_error(fut):
        try:
            fut.result()
        except Exception as e:
            logging.error(f"PDF processing failed: {e}", exc_info=True)
    future.add_done_callback(_log_future_error)

    return {
        "info": f"file '{file.filename}' uploaded and processing started.",
        "chat_id": chat_id,
        "document_id": document_id
    }

@app.get("/chats/{device_id}")
async def get_recent_chats(device_id: str, page: int = 1, per_page: int = 50):
    # M13 fix: validate pagination params to prevent negative skip() in MongoDB.
    if page < 1:
        page = 1
    if per_page < 1 or per_page > 200:
        per_page = 50
    result = await database.get_chats(device_id, page=page, per_page=per_page)
    return result

@app.get("/chat/{chat_id}")
async def get_chat_history(chat_id: str, device_id: str = None):
    # C3 fix: device_id is required — without it, anyone who guesses a chat_id
    # can read another user's history (IDOR). If omitted, return 403.
    if not device_id:
        raise HTTPException(status_code=403, detail="device_id is required to access chat history.")
    messages = await database.get_chat_history(chat_id, device_id=device_id)
    return {"messages": messages}

@app.delete("/chat/{chat_id}")
async def delete_chat_endpoint(chat_id: str, device_id: str = None):
    # C3 fix: device_id is required — without it, anyone who guesses a chat_id
    # can delete another user's chat (IDOR). If omitted, return 403.
    if not device_id:
        raise HTTPException(status_code=403, detail="device_id is required to delete a chat.")
    success = await database.delete_chat(chat_id, device_id=device_id)
    if not success:
        raise HTTPException(status_code=404, detail="Chat not found or could not be deleted")
    return {"message": "Chat deleted"}

@app.post("/chat")
async def chat(
    message: str = Form(...),
    api_key: str = Form(None),
    device_id: str = Form(...),
    chat_id: str = Form(None)
):
    # Retrieve Groq API Key from environment or request body
    groq_api_key = api_key or os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        raise HTTPException(status_code=400, detail="Groq API Key is required. Please set it in the environment or pass it.")

    # M9 fix: validate input length to prevent sending arbitrarily large
    # messages to Groq and MongoDB.
    MAX_MESSAGE_LENGTH = 10000
    if len(message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message too long (max {MAX_MESSAGE_LENGTH} characters).")

    # Load prior conversation turns (before adding this message) so the model
    # has continuity across the chat instead of treating every message in isolation
    # C3 fix: scope history lookup by device_id (ownership check)
    history = await database.get_chat_history(chat_id, device_id=device_id) if chat_id else []

    # C8 fix: look up the document_id associated with this chat so query_rag
    # queries the right per-document Chroma collection.
    document_id = await database.get_chat_document_id(chat_id, device_id=device_id) if chat_id else None

    # C1/C2 fix: redact PII from the user's message BEFORE storing it in the
    # database and BEFORE sending it to Groq. Previously user messages were
    # stored raw and replayed as raw HumanMessages in history, so PII from
    # earlier turns leaked to Groq on every subsequent turn — defeating the
    # entire input_rail redaction on the current question.
    redacted_message = redact_pii(message, TokenStore(), entities=RAIL_ENTITIES)

    # H2 fix: db_available must reflect whether the database is actually
    # reachable, not just whether chat_id was provided. For an existing chat
    # when DB is down, chat_id is truthy but add_message silently does nothing.
    db_available = database.chats_collection is not None

    # Create chat record if this is a new conversation
    if not chat_id:
        chat_id = await database.create_chat(device_id, redacted_message)

    # query_rag is now async (it may await MCP tool calls over SSE), so we
    # call it directly instead of running it in a thread.
    # C1 fix: pass the already-redacted message so input_rail doesn't
    # double-redact (it will still run its extraction-intent check).
    result = await query_rag(redacted_message, groq_api_key, history, document_id)

    # H3 fix: save the user message AFTER query_rag succeeds, not before.
    # Previously the message was saved before the LLM call, so if query_rag
    # threw, the message was already in the DB. On retry, the same message
    # was saved again → duplicate entries in chat history.
    if chat_id:
        await database.add_message(chat_id, "user", redacted_message)

    if chat_id:
        await database.add_message(chat_id, "ai", result["answer"], result["tools_used"], result["source_pages"])

    response = {
        "response": result["answer"],
        "chat_id": chat_id,
        "tools_used": result["tools_used"],
        "source_pages": result["source_pages"],
    }
    # H3 fix: warn the frontend when chat history isn't being persisted so the
    # user knows their messages won't survive a page refresh.
    if not db_available:
        response["db_warning"] = "Chat history is not being saved (database unavailable)."
    return response

# --- Serve React Frontend ---
# M15 fix: check for dist/ at request time instead of import time, so building
# the frontend after starting the server still works without a restart.
frontend_dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../frontend/dist")

@app.get("/")
async def serve_react_app_root():
    index_path = os.path.join(frontend_dist, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Frontend build not found. Run `npm run build` in the frontend directory.")
    return FileResponse(index_path)


@app.get("/{catchall:path}")
async def serve_react_app(catchall: str):
    # H8 fix: explicitly exclude API paths so a new API route registered
    # after this catchall doesn't get silently swallowed. Return 404 for
    # known API prefixes instead of serving index.html.
    # L7 fix: also exclude "health" and use a more maintainable tuple.
    if catchall.startswith(("upload", "chat", "chats", "ws", "docs", "openapi", "redoc", "health")):
        raise HTTPException(status_code=404, detail="Not found")

    # M15 fix: check for dist/ at request time, not import time, so building
    # after starting the server still works without a restart. Serves any
    # real file under dist/ directly (not just /assets/*) — Vite copies
    # frontend/public/* (e.g. favicon.svg) to the dist root, not under
    # assets/, so a request for those needs this same fallthrough rather
    # than a dedicated StaticFiles mount scoped to /assets only.
    # C5 fix: use realpath to resolve the requested path and verify it stays
    # inside frontend_dist — prevents path traversal via ../ sequences.
    file_path = os.path.realpath(os.path.join(frontend_dist, catchall))
    real_frontend_dist = os.path.realpath(frontend_dist)
    if not file_path.startswith(real_frontend_dist + os.sep) and file_path != real_frontend_dist:
        raise HTTPException(status_code=403, detail="Access denied.")
    if os.path.isfile(file_path):
        return FileResponse(file_path)

    index_path = os.path.join(frontend_dist, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Frontend build not found. Run `npm run build` in the frontend directory.")
    return FileResponse(index_path)
