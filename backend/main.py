from fastapi import FastAPI, UploadFile, File, Form, WebSocket, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List
import os
import shutil
import asyncio
from dotenv import load_dotenv
from rag_pipeline import process_pdf, query_rag

load_dotenv()

app = FastAPI()

# Allow CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploaded_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Store active websocket connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Connection might be closed
                pass

manager = ConnectionManager()

@app.websocket("/ws/process")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)

def run_pdf_processing(file_path: str):
    # Callback to send websocket updates
    def update_status(step_name: str):
        # We need an event loop to run async broadcast inside sync callback
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(manager.broadcast({"step": step_name}), loop)
        else:
            loop.run_until_complete(manager.broadcast({"step": step_name}))

    try:
        process_pdf(file_path, callback=update_status)
    except Exception as e:
        # Broadcast error status
        try:
            loop = asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(manager.broadcast({"error": str(e)}), loop)
        except Exception:
            pass

@app.post("/upload")
async def upload_pdf(
    background_tasks: BackgroundTasks, 
    file: UploadFile = File(...),
    device_id: str = Form("default")
):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    file_location = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # Run processing in background
    background_tasks.add_task(run_pdf_processing, file_location)
    
    # Create a chat record immediately for the uploaded document
    import database
    chat_id = await database.create_chat(device_id, f"Document: {file.filename}")
    
    return {
        "info": f"file '{file.filename}' uploaded and processing started.",
        "chat_id": chat_id
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
async def get_chat_history(chat_id: str):
    messages = await database.get_chat_history(chat_id)
    return {"messages": messages}

@app.delete("/chat/{chat_id}")
async def delete_chat_endpoint(chat_id: str):
    success = await database.delete_chat(chat_id)
    if not success:
        raise HTTPException(status_code=404, detail="Chat not found or could not be deleted")
    return {"message": "Chat deleted"}

@app.post("/chat")
async def chat(
    message: str = Form(...), 
    api_key: str = Form(None),
    device_id: str = Form("default"),
    chat_id: str = Form(None)
):
    # Retrieve Groq API Key from environment or request body
    groq_api_key = api_key or os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        raise HTTPException(status_code=400, detail="Groq API Key is required. Please set it in the environment or pass it.")
    
    # Save user message
    if not chat_id:
        chat_id = await database.create_chat(device_id, message)
    
    if chat_id:
        await database.add_message(chat_id, "user", message)
    
    response = query_rag(message, groq_api_key)
    
    if chat_id:
        await database.add_message(chat_id, "ai", response)
        
    return {"response": response, "chat_id": chat_id}

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
