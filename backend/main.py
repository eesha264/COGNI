from fastapi import FastAPI, UploadFile, File, Form, WebSocket, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List
import os
import shutil
import asyncio
import uuid
from dotenv import load_dotenv
from rag_pipeline import process_pdf, query_rag

load_dotenv()

app = FastAPI()

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
        self.connections_by_device: dict[str, List[WebSocket]] = {}

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

def run_pdf_processing(file_path: str, device_id: str, groq_api_key: str = None, document_id: str = None):
    # Callback to send websocket updates
    def update_status(step_name: str):
        # We need an event loop to run async broadcast inside sync callback
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"step": step_name}, device_id=device_id), loop
            )
        else:
            loop.run_until_complete(
                manager.broadcast({"step": step_name}, device_id=device_id)
            )

    try:
        result = process_pdf(file_path, callback=update_status, groq_api_key=groq_api_key, document_id=document_id)
        # C11 fix: process_pdf returns {"error": ...} for >400-page PDFs but
        # the return value was previously ignored, so the UI showed "Done" even
        # though nothing was indexed. Broadcast the error instead.
        if isinstance(result, dict) and result.get("error"):
            try:
                loop = asyncio.get_event_loop()
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({"error": result["error"]}, device_id=device_id), loop
                )
            except Exception:
                pass
    except Exception as e:
        # Broadcast error status
        try:
            loop = asyncio.get_event_loop()
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

    # Run processing in background (C4 fix: pass device_id for scoped broadcasts)
    background_tasks.add_task(run_pdf_processing, file_location, device_id, groq_api_key, document_id)

    # Create a chat record immediately for the uploaded document
    import database
    chat_id = await database.create_chat(device_id, f"Document: {file.filename}", document_id=document_id)

    return {
        "info": f"file '{file.filename}' uploaded and processing started.",
        "chat_id": chat_id,
        "document_id": document_id
    }

import database

@app.on_event("startup")
async def startup_event():
    database.connect_db()

@app.get("/chats/{device_id}")
async def get_recent_chats(device_id: str):
    chats = await database.get_chats(device_id)
    return {"chats": chats}

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

    if chat_id:
        await database.add_message(chat_id, "user", message)

    # C10 fix: run the sync query_rag in a thread so it doesn't block the
    # event loop while waiting on Groq.
    result = await asyncio.to_thread(query_rag, message, groq_api_key, history, document_id)

    if chat_id:
        await database.add_message(chat_id, "ai", result["answer"], result["tools_used"], result["source_pages"])

    return {
        "response": result["answer"],
        "chat_id": chat_id,
        "tools_used": result["tools_used"],
        "source_pages": result["source_pages"],
    }

# --- Serve React Frontend ---
frontend_dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../frontend/dist")

if os.path.exists(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="assets")

    @app.get("/")
    async def serve_react_app_root():
        return FileResponse(os.path.join(frontend_dist, "index.html"))

    @app.get("/{catchall:path}")
    async def serve_react_app(catchall: str):
        return FileResponse(os.path.join(frontend_dist, "index.html"))
