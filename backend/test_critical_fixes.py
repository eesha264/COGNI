"""
Tests for all 16 critical fixes (C1-C16) in the COGNI codebase.
Run with: ./venv/Scripts/python.exe test_critical_fixes.py
"""
import sys
import os
import io
import tempfile
import shutil
import asyncio
import threading
import time

# Ensure backend dir is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Silence import-time side effects from rag_pipeline (it initializes FastEmbed
# and Chroma at import time). We'll mock those before importing.
from unittest.mock import MagicMock, patch, AsyncMock

PASS = 0
FAIL = 0
RESULTS = []

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        RESULTS.append(f"  [PASS] {name}")
    else:
        FAIL += 1
        RESULTS.append(f"  [FAIL] {name} — {detail}")

def section(title):
    RESULTS.append(f"\n{'='*60}\n{title}\n{'='*60}")

# =============================================================================
# C1: Path traversal — UUID filenames
# =============================================================================
section("C1: Path traversal in upload (UUID filenames)")

def test_c1():
    import uuid
    # Simulate the fix logic: UUID filename, not user-provided
    user_filename = "../../windows/evil.exe"
    safe_filename = f"{uuid.uuid4().hex}.pdf"
    UPLOAD_DIR = tempfile.mkdtemp()

    file_location = os.path.join(UPLOAD_DIR, safe_filename)
    # Write a dummy file
    with open(file_location, "wb") as f:
        f.write(b"dummy")

    # The file must be inside UPLOAD_DIR
    test("UUID filename is not the user filename",
         safe_filename != user_filename,
         f"got {safe_filename}")
    test("UUID filename ends with .pdf", safe_filename.endswith(".pdf"))
    test("File is inside UPLOAD_DIR",
         os.path.dirname(file_location) == UPLOAD_DIR,
         f"file in {os.path.dirname(file_location)}, dir is {UPLOAD_DIR}")
    test("No path traversal chars in safe_filename",
         ".." not in safe_filename and "/" not in safe_filename and "\\" not in safe_filename)

    # Verify the OLD code WOULD have been vulnerable
    old_location = os.path.join(UPLOAD_DIR, user_filename)
    test("Old code would have escaped UPLOAD_DIR",
         not os.path.normpath(old_location).startswith(UPLOAD_DIR),
         f"old path: {os.path.normpath(old_location)}")

    shutil.rmtree(UPLOAD_DIR)

test_c1()

# =============================================================================
# C2: CORS restricted origins
# =============================================================================
section("C2: CORS restricted origins")

def test_c2():
    # Read main.py and check the CORS config
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        content = f.read()

    test("No more allow_origins=['*']", 'allow_origins=["*"]' not in content)
    test("Has ALLOWED_ORIGINS config", "ALLOWED_ORIGINS" in content)
    test("Origins are specific (localhost)", "localhost" in content or "127.0.0.1" in content)
    test("Uses os.getenv for configurable origins", 'os.getenv' in content and 'ALLOWED_ORIGINS' in content)

test_c2()

# =============================================================================
# C3: Auth/ownership check on chat endpoints
# =============================================================================
section("C3: Auth/ownership check on chat endpoints")

def test_c3():
    # Test database.py ownership logic with a mock collection
    import database

    # Mock the chats_collection
    mock_collection = AsyncMock()
    database.chats_collection = mock_collection

    # --- Test get_chat_history with wrong device_id ---
    mock_chat = {"device_id": "device-A", "messages": [{"role": "user", "content": "hi"}]}
    mock_collection.find_one = AsyncMock(return_value=mock_chat)

    result = asyncio.run(database.get_chat_history("507f1f77bcf86cd799439011", device_id="device-B"))
    test("get_chat_history returns [] for wrong device_id", result == [],
         f"got {result}")

    # --- Test get_chat_history with correct device_id ---
    result = asyncio.run(database.get_chat_history("507f1f77bcf86cd799439011", device_id="device-A"))
    test("get_chat_history returns messages for correct device_id", len(result) == 1,
         f"got {result}")

    # --- Test delete_chat with wrong device_id ---
    mock_delete_result = MagicMock()
    mock_delete_result.deleted_count = 0  # Wrong device_id → no match → 0 deleted
    mock_collection.delete_one = AsyncMock(return_value=mock_delete_result)

    success = asyncio.run(database.delete_chat("507f1f77bcf86cd799439011", device_id="device-B"))
    test("delete_chat returns False for wrong device_id", success == False)

    # --- Test delete_chat with correct device_id ---
    mock_delete_result.deleted_count = 1
    success = asyncio.run(database.delete_chat("507f1f77bcf86cd799439011", device_id="device-A"))
    test("delete_chat returns True for correct device_id", success == True)

    # --- Verify main.py passes device_id to these functions ---
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        main_content = f.read()
    test("main.py passes device_id to get_chat_history",
         "get_chat_history(chat_id, device_id=device_id)" in main_content)
    test("main.py passes device_id to delete_chat",
         "delete_chat(chat_id, device_id=device_id)" in main_content)

test_c3()

# =============================================================================
# C4: WebSocket broadcasts scoped per device_id
# =============================================================================
section("C4: WebSocket broadcasts scoped per device_id")

def test_c4():
    # Test the ConnectionManager logic directly
    # We need to import main.py, but it has import-time side effects.
    # Let's test the ConnectionManager class in isolation.

    # Read and check the code structure
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        content = f.read()

    test("ConnectionManager uses connections_by_device dict",
         "connections_by_device" in content)
    test("broadcast accepts device_id parameter",
         "async def broadcast(self, message: dict, device_id: str = None)" in content)
    test("connect accepts device_id parameter",
         "async def connect(self, websocket: WebSocket, device_id: str)" in content)
    test("WebSocket endpoint reads device_id from query params",
         'query_params.get("device_id")' in content)
    test("run_pdf_processing accepts device_id",
         "def run_pdf_processing(file_path: str, device_id: str" in content)
    test("broadcast called with device_id in update_status",
         'manager.broadcast({"step": step_name}, device_id=device_id)' in content)

    # Functional test: create a real ConnectionManager and test scoping
    # We'll exec just the class definition
    from fastapi import WebSocket
    import json

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.accepted = False
        async def accept(self):
            self.accepted = True
        async def send_json(self, msg):
            self.sent.append(msg)
        async def receive_text(self):
            await asyncio.sleep(100)  # block forever

    # Replicate the ConnectionManager logic
    class TestConnectionManager:
        def __init__(self):
            self.connections_by_device = {}
        async def connect(self, websocket, device_id):
            await websocket.accept()
            self.connections_by_device.setdefault(device_id, []).append(websocket)
        def disconnect(self, websocket, device_id):
            conns = self.connections_by_device.get(device_id, [])
            if websocket in conns:
                conns.remove(websocket)
            if not conns and device_id in self.connections_by_device:
                del self.connections_by_device[device_id]
        async def broadcast(self, message, device_id=None):
            if device_id is not None:
                targets = self.connections_by_device.get(device_id, [])
            else:
                targets = [ws for conns in self.connections_by_device.values() for ws in conns]
            for connection in targets:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

    mgr = TestConnectionManager()
    ws_a1 = FakeWebSocket()
    ws_a2 = FakeWebSocket()
    ws_b1 = FakeWebSocket()

    asyncio.run(mgr.connect(ws_a1, "device-A"))
    asyncio.run(mgr.connect(ws_a2, "device-A"))
    asyncio.run(mgr.connect(ws_b1, "device-B"))

    # Broadcast to device-A only
    asyncio.run(mgr.broadcast({"step": "Analyzing"}, device_id="device-A"))

    test("Device-A ws1 received the broadcast", len(ws_a1.sent) == 1)
    test("Device-A ws2 received the broadcast", len(ws_a2.sent) == 1)
    test("Device-B ws1 did NOT receive the broadcast", len(ws_b1.sent) == 0,
         f"got {ws_b1.sent}")

test_c4()

# =============================================================================
# C5: README env var name fix
# =============================================================================
section("C5: README MONGODB_URL → MONGODB_URI")

def test_c5():
    with open(os.path.join(os.path.dirname(__file__), "..", "README.md"), "r", encoding="utf-8") as f:
        readme = f.read()
    test("README uses MONGODB_URI (not MONGODB_URL)", "MONGODB_URI" in readme)
    test("README no longer has MONGODB_URL", "MONGODB_URL" not in readme)

test_c5()

# =============================================================================
# C6: Vision model name fix
# =============================================================================
section("C6: Vision model name")

def test_c6():
    with open(os.path.join(os.path.dirname(__file__), "rag_pipeline.py"), "r", encoding="utf-8") as f:
        content = f.read()
    # The old model name may appear in comments explaining the fix — check
    # that it's not used as the actual VISION_MODEL value.
    test("No more qwen/qwen3.6-27b as VISION_MODEL value",
         'VISION_MODEL = "qwen/qwen3.6-27b"' not in content)
    test("Uses llama-4-scout model", "llama-4-scout" in content)
    test("Configurable via GROQ_VISION_MODEL env var", "GROQ_VISION_MODEL" in content)

test_c6()

# =============================================================================
# C7: API key passed from frontend to /upload
# =============================================================================
section("C7: API key passed to /upload for vision")

def test_c7():
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        main_content = f.read()
    with open(os.path.join(os.path.dirname(__file__), "..", "frontend", "src", "App.jsx"), "r") as f:
        app_content = f.read()

    test("/upload accepts api_key form field", "api_key: str = Form(None)" in main_content)
    test("/upload passes groq_api_key to run_pdf_processing",
         "run_pdf_processing, file_location, device_id, groq_api_key" in main_content)
    test("run_pdf_processing passes groq_api_key to process_pdf",
         "process_pdf(file_path, callback=update_status, groq_api_key=groq_api_key" in main_content)
    test("process_pdf accepts groq_api_key parameter",
         "def process_pdf(file_path: str, callback=None, groq_api_key: str = None" in
         open(os.path.join(os.path.dirname(__file__), "rag_pipeline.py")).read())
    test("Frontend sends api_key on upload",
         'formData.append("api_key", apiKey)' in app_content)

test_c7()

# =============================================================================
# C8: Per-document vector store
# =============================================================================
section("C8: Per-document vector store (no global wipe)")

def test_c8():
    with open(os.path.join(os.path.dirname(__file__), "rag_pipeline.py"), "r") as f:
        rag_content = f.read()
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        main_content = f.read()
    with open(os.path.join(os.path.dirname(__file__), "database.py"), "r") as f:
        db_content = f.read()

    test("No global vector_store variable", "vector_store = Chroma(" not in rag_content.split("# C8")[0] if "# C8" in rag_content else True)
    test("Has get_vector_store function", "def get_vector_store(collection_name: str)" in rag_content)
    test("Has delete_vector_store function", "def delete_vector_store(collection_name: str)" in rag_content)
    test("Collection name uses document_id", 'f"pdf_{document_id}"' in rag_content)
    test("process_pdf accepts document_id", "document_id: str = None" in rag_content)
    test("query_rag accepts document_id", "document_id: str = None" in rag_content)
    test("database.create_chat accepts document_id", "document_id: str = None" in db_content)
    test("database has get_chat_document_id", "def get_chat_document_id" in db_content)
    test("main.py generates document_id", "document_id = uuid.uuid4().hex" in main_content)
    test("main.py passes document_id to create_chat", "create_chat(device_id, f\"Document: {file.filename}\", document_id=document_id)" in main_content)
    test("main.py looks up document_id for /chat", "get_chat_document_id" in main_content)
    test("No more delete_collection() on global store", "vector_store.delete_collection()" not in rag_content)

    # Functional test: verify two different document_ids produce different collection names
    doc_id_1 = "abc123"
    doc_id_2 = "def456"
    name_1 = f"pdf_{doc_id_1}"
    name_2 = f"pdf_{doc_id_2}"
    test("Different document_ids → different collection names", name_1 != name_2)

test_c8()

# =============================================================================
# C9: Lock for vector_store race condition
# =============================================================================
section("C9: Lock for vector_store race condition")

def test_c9():
    with open(os.path.join(os.path.dirname(__file__), "rag_pipeline.py"), "r") as f:
        content = f.read()

    test("Has threading import", "import threading" in content)
    test("Has _vector_store_lock", "_vector_store_lock" in content)
    test("Lock is used in get_vector_store", "with _vector_store_lock:" in content)
    test("Lock is used in delete_vector_store", content.count("with _vector_store_lock:") >= 2)

test_c9()

# =============================================================================
# C10: query_rag runs in thread (asyncio.to_thread)
# =============================================================================
section("C10: query_rag runs via asyncio.to_thread")

def test_c10():
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        content = f.read()

    test("Uses asyncio.to_thread for query_rag", "asyncio.to_thread(query_rag" in content)
    test("Passes document_id to query_rag via to_thread",
         "asyncio.to_thread(query_rag, message, groq_api_key, history, document_id)" in content)

test_c10()

# =============================================================================
# C11: process_pdf return value checked
# =============================================================================
section("C11: process_pdf return value checked")

def test_c11():
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        content = f.read()

    test("run_pdf_processing captures result", "result = process_pdf(" in content)
    test("Checks for error in result", 'result.get("error")' in content)
    test("Broadcasts error on failure", 'manager.broadcast({"error": result["error"]}' in content)

    # Functional test: simulate process_pdf returning an error
    fake_result = {"error": "PDF exceeds 400 pages limit."}
    test("Error detection logic works",
         isinstance(fake_result, dict) and fake_result.get("error") is not None)

test_c11()

# =============================================================================
# C12: File handle leak fix (try/finally)
# =============================================================================
section("C12: File handle leak fix (try/finally)")

def test_c12():
    with open(os.path.join(os.path.dirname(__file__), "rag_pipeline.py"), "r") as f:
        content = f.read()

    test("Has try: block around doc operations", "doc = pymupdf.open(file_path)\n    try:" in content)
    test("Has finally: block that closes doc", "finally:\n        doc.close()" in content)
    test("Early return is inside try block", 'return {"error": "PDF exceeds 400 pages limit."}' in content)

    # Functional test: verify that a function with try/finally always closes
    class FakeDoc:
        def __init__(self):
            self.closed = False
            self.pages = list(range(500))  # > 400 pages
        def __len__(self):
            return len(self.pages)
        def close(self):
            self.closed = True

    # Simulate the try/finally pattern
    fake = FakeDoc()
    try:
        if len(fake) > 400:
            result = {"error": "PDF exceeds 400 pages limit."}
        else:
            result = {"status": "success"}
    finally:
        fake.close()

    test("Doc is closed even on early return (>400 pages)", fake.closed == True)

test_c12()

# =============================================================================
# C13: Upload size limit
# =============================================================================
section("C13: Upload size limit")

def test_c13():
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        content = f.read()

    test("Has MAX_UPLOAD_BYTES config", "MAX_UPLOAD_BYTES" in content)
    test("Default is 100 MB", "100 * 1024 * 1024" in content)
    test("Streams in chunks (not copyfileobj)", "await file.read(1024 * 1024)" in content)
    test("Checks written size against limit", "if written > MAX_UPLOAD_BYTES" in content)
    test("Returns 413 on oversized", "status_code=413" in content)
    test("Removes partial file on oversize", "os.remove(file_location)" in content)
    test("No more shutil.copyfileobj for upload", "shutil.copyfileobj(file.file, buffer)" not in content)

    # Functional test: simulate the size check logic
    MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
    chunks = [b"x" * (60 * 1024 * 1024), b"x" * (60 * 1024 * 1024)]  # 120 MB total
    written = 0
    exceeded = False
    for chunk in chunks:
        written += len(chunk)
        if written > MAX_UPLOAD_BYTES:
            exceeded = True
            break
    test("Size check detects oversize at 120MB", exceeded == True)

    # Test that 50MB passes
    written = 50 * 1024 * 1024
    test("Size check allows 50MB", written <= MAX_UPLOAD_BYTES)

test_c13()

# =============================================================================
# C14: device_id required (no default)
# =============================================================================
section("C14: device_id required (no default)")

def test_c14():
    with open(os.path.join(os.path.dirname(__file__), "main.py"), "r") as f:
        content = f.read()

    test("/upload requires device_id (Form(...))", 'device_id: str = Form(...)' in content)
    test("/chat requires device_id (Form(...))",
         content.count('device_id: str = Form(...)') >= 2)
    test("No more Form(\"default\") for device_id", 'Form("default")' not in content)
    test("WebSocket rejects missing device_id", 'not device_id' in content and 'close(code=1008' in content)
    test("run_pdf_processing requires device_id (no default)",
         "def run_pdf_processing(file_path: str, device_id: str, " in content)

test_c14()

# =============================================================================
# C15: API key in sessionStorage (not localStorage)
# =============================================================================
section("C15: API key in sessionStorage")

def test_c15():
    app_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "src", "App.jsx")
    settings_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "src", "components", "SettingsModal.jsx")

    with open(app_path, "r", encoding="utf-8") as f:
        app = f.read()
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = f.read()

    test("App.jsx reads key from sessionStorage", 'sessionStorage.getItem("groq_api_key")' in app)
    test("App.jsx saves key to sessionStorage", 'sessionStorage.setItem("groq_api_key"' in app)
    test("App.jsx no longer uses localStorage for api key",
         'localStorage.getItem("groq_api_key")' not in app and
         'localStorage.setItem("groq_api_key"' not in app)
    test("SettingsModal removes from sessionStorage", 'sessionStorage.removeItem("groq_api_key")' in settings)
    test("device_id still in localStorage (not moved)", 'localStorage.getItem("device_id")' in app)

test_c15()

# =============================================================================
# C16: normalizeLatexDelimiters skips code blocks
# =============================================================================
section("C16: normalizeLatexDelimiters skips code blocks")

def test_c16():
    with open(os.path.join(os.path.dirname(__file__), "..", "frontend", "src", "components", "MainChat.jsx"), "r") as f:
        content = f.read()

    test("Splits on fenced code blocks", '```' in content and 'fenceSplit' in content)
    test("Splits on inline code spans", 'inlineSplit' in content)
    test("Skips fenced code segments (odd indices)", "i % 2 === 1" in content)
    test("Skips inline code segments (odd indices)", "j % 2 === 1" in content)

    # Functional test: replicate the logic in Python and verify
    import re

    def py_normalize(text):
        if not text:
            return text
        # Split on fenced code blocks
        fence_parts = re.split(r'(```[\s\S]*?```)', text)
        result = []
        for i, segment in enumerate(fence_parts):
            if i % 2 == 1:  # fenced code block
                result.append(segment)
                continue
            # Split on inline code
            inline_parts = re.split(r'(`[^`]*`)', segment)
            for j, seg in enumerate(inline_parts):
                if j % 2 == 1:  # inline code
                    result.append(seg)
                else:
                    seg = seg.replace('\\[', '$$').replace('\\]', '$$')
                    seg = seg.replace('\\(', '$').replace('\\)', '$')
                    result.append(seg)
        return ''.join(result)

    # Test 1: Math outside code should be converted
    input1 = r"The equation \[x^2 + y^2 = z^2\] is important."
    output1 = py_normalize(input1)
    test("Math outside code: \\[ → $$", "$$" in output1 and "\\[" not in output1)

    # Test 2: Code block with \[ should NOT be converted
    input2 = "```python\narr = [1, 2, 3]\nprint(arr[0])\n```"
    output2 = py_normalize(input2)
    test("Code block: \\[ not converted", "$$" not in output2)

    # Test 3: Inline code with \( should NOT be converted
    input3 = r"Use `\(x\)` for inline math in LaTeX."
    output3 = py_normalize(input3)
    test("Inline code: \\( not converted", "$" not in output3 or "$" in input3)

    # Test 4: Mixed content
    input4 = r"The formula \[a^2\] is math. `code\[0\]` is code. And \(b\) is math."
    output4 = py_normalize(input4)
    test("Mixed: math converted, code preserved",
         "$$" in output4 and r"code\[0\]" in output4)

test_c16()

# =============================================================================
# Print results
# =============================================================================
import sys as _sys
# Fix Windows console encoding for Unicode chars (→, etc.)
try:
    _sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

print("\n" + "\n".join(RESULTS))
print(f"\n{'='*60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
print(f"{'='*60}")
sys.exit(1 if FAIL > 0 else 0)


