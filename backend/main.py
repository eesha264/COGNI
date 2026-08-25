from fastapi import FastAPI, UploadFile, File, Form, WebSocket, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os
import shutil
import asyncio
import uuid
from dotenv import load_dotenv
from rag_pipeline import process_pdf, query_rag
import database
from mcp_client_manager import connect_mcp_servers, disconnect_mcp_servers

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

UPLOAD_DIR = "uploaded_files"
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
    # H1 fix: capture the running event loop once at entry instead of calling
    # the deprecated asyncio.get_event_loop() inside every callback (which can
    # also deadlock if it picks up a non-running loop). Since this function is
    # launched via BackgroundTasks (which runs on the event loop thread), we
    # capture the loop here and always use run_coroutine_threadsafe.
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

    # Callback to send websocket updates
    def update_status(step_name: str):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"step": step_name}, device_id=device_id), loop
        )

    try:
        result = process_pdf(file_path, callback=update_status, groq_api_key=groq_api_key, document_id=document_id)
        # C11 fix: process_pdf returns {"error": ...} for >400-page PDFs but
        # the return value was previously ignored, so the UI showed "Done" even
        # though nothing was indexed. Broadcast the error instead.
        if isinstance(result, dict) and result.get("error"):
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"error": result["error"]}, device_id=device_id), loop
            )
    except Exception as e:
        # Broadcast error status
        try:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"error": str(e)}, device_id=device_id), loop
            )
        except Exception:
            pass

# C13 fix: cap upload size (default 100 MB) to prevent disk-exhaustion DoS.
# Configure via MAX_UPLOAD_BYTES env var.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))

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
    content_type = file.content_type or ""
    if content_type and content_type not in ("application/pdf", "application/octet-stream", ""):
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

    # C8 fix: generate a document_id so process_pdf indexes into a per-document
    # collection instead of wiping a global one.
    document_id = uuid.uuid4().hex

    # H2 fix: run the heavy sync PDF processing in a separate thread instead of
    # BackgroundTasks (which runs on the event loop thread and blocks all other
    # requests during PyMuPDF parsing, ONNX embedding, and Chroma writes).
    main_loop = asyncio.get_running_loop()
    main_loop.run_in_executor(
        None, run_pdf_processing, file_location, device_id, groq_api_key, document_id, main_loop
    )

    # Create a chat record immediately for the uploaded document
    # H15 fix: database is now imported at the top of the file
    chat_id = await database.create_chat(device_id, f"Document: {file.filename}", document_id=document_id)

    return {
        "info": f"file '{file.filename}' uploaded and processing started.",
        "chat_id": chat_id,
        "document_id": document_id
    }

@app.get("/chats/{device_id}")
async def get_recent_chats(device_id: str, page: int = 1, per_page: int = 50):
    # M14 fix: pass pagination params to get_chats
    result = await database.get_chats(device_id, page=page, per_page=per_page)
    return result

@app.get("/chat/{chat_id}")
async def get_chat_history(chat_id: str, device_id: str = None):
    # C3 fix: pass device_id for ownership check; accept via query param
    messages = await database.get_chat_history(chat_id, device_id=device_id)
    return {"messages": messages}

@app.delete("/chat/{chat_id}")
async def delete_chat_endpoint(chat_id: str, device_id: str = None):
    # C3 fix: scope deletion by device_id so only the owner can delete
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

    # Load prior conversation turns (before adding this message) so the model
    # has continuity across the chat instead of treating every message in isolation
    # C3 fix: scope history lookup by device_id (ownership check)
    history = await database.get_chat_history(chat_id, device_id=device_id) if chat_id else []

    # C8 fix: look up the document_id associated with this chat so query_rag
    # queries the right per-document Chroma collection.
    document_id = await database.get_chat_document_id(chat_id, device_id=device_id) if chat_id else None

    # Save user message
    if not chat_id:
        chat_id = await database.create_chat(device_id, message)

    # H3 fix: if chat_id is None (DB unavailable), warn the user instead of
    # silently losing messages. The chat still works for the current turn.
    db_available = chat_id is not None
    if not db_available:
        # Append a notice so the user knows their history isn't being saved
        pass  # handled below in the response

    if chat_id:
        await database.add_message(chat_id, "user", message)

    # query_rag is now async (it may await MCP tool calls over SSE), so we
    # call it directly instead of running it in a thread.
    result = await query_rag(message, groq_api_key, history, document_id)

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
    if catchall.startswith(("upload", "chat", "chats", "ws", "docs", "openapi", "redoc")):
        raise HTTPException(status_code=404, detail="Not found")

    # M15 fix: check for dist/ at request time, not import time, so building
    # after starting the server still works without a restart. Serves any
    # real file under dist/ directly (not just /assets/*) — Vite copies
    # frontend/public/* (e.g. favicon.svg) to the dist root, not under
    # assets/, so a request for those needs this same fallthrough rather
    # than a dedicated StaticFiles mount scoped to /assets only.
    file_path = os.path.join(frontend_dist, catchall)
    if os.path.isfile(file_path):
        return FileResponse(file_path)

    index_path = os.path.join(frontend_dist, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Frontend build not found. Run `npm run build` in the frontend directory.")
    return FileResponse(index_path)
