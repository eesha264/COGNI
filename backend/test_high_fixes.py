"""
Tests for all 15 High-level fixes (H1-H15) in the COGNI codebase.
Run with: ./venv/Scripts/python.exe test_high_fixes.py
"""
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
        RESULTS.append(f"  [FAIL] {name} -- {detail}")

def section(title):
    RESULTS.append(f"\n{'='*60}\n{title}\n{'='*60}")

def read_file(rel_path):
    full_path = os.path.join(os.path.dirname(__file__), rel_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()

# =============================================================================
# H1: asyncio.get_event_loop() hack fixed
# =============================================================================
section("H1: asyncio.get_event_loop() hack fixed")

def test_h1():
    main = read_file("main.py")
    # Strip comments to avoid false positives from fix-explanation comments
    code_lines = [l for l in main.split("\n") if not l.strip().startswith("#")]
    code = "\n".join(code_lines)
    test("Uses get_running_loop() (not get_event_loop())",
         "asyncio.get_running_loop()" in code)
    test("No more get_event_loop() in actual code (only in comments)",
         "asyncio.get_event_loop()" not in code)
    test("No more loop.run_until_complete (deadlock-prone)",
         "loop.run_until_complete" not in code)
    test("Captures loop once at function entry",
         "loop = asyncio.get_running_loop()" in code)

test_h1()

# =============================================================================
# H2: Heavy sync work moved off event loop
# =============================================================================
section("H2: Heavy sync work moved off event loop")

def test_h2():
    main = read_file("main.py")
    test("Uses run_in_executor for PDF processing",
         "run_in_executor" in main)
    test("No longer uses background_tasks.add_task for run_pdf_processing",
         "background_tasks.add_task(run_pdf_processing" not in main)

test_h2()

# =============================================================================
# H3: Handle chat_id=None when DB unavailable
# =============================================================================
section("H3: Handle chat_id=None when DB unavailable")

def test_h3():
    main = read_file("main.py")
    test("Tracks db_available flag",
         "db_available" in main)
    test("Returns db_warning in response when DB unavailable",
         'response["db_warning"]' in main)

    # Functional test: simulate create_chat returning None (DB unavailable)
    import database
    database.chats_collection = None  # Simulate no DB
    chat_id = asyncio.run(database.create_chat("device-X", "test message"))
    test("create_chat returns None when DB unavailable", chat_id is None)

test_h3()

# =============================================================================
# H4: Catch InvalidId on malformed chat_id
# =============================================================================
section("H4: Catch InvalidId on malformed chat_id")

def test_h4():
    db = read_file("database.py")
    test("Imports InvalidId from bson.errors",
         "from bson.errors import InvalidId" in db)
    test("Has _to_objectid helper",
         "def _to_objectid" in db)
    test("_to_objectid returns None for invalid ID",
         "return None" in db)

    # Functional test: _to_objectid with malformed IDs
    import database
    test("_to_objectid returns None for 'not-a-valid-id'",
         database._to_objectid("not-a-valid-id") is None)
    test("_to_objectid returns None for empty string",
         database._to_objectid("") is None)
    test("_to_objectid returns None for None",
         database._to_objectid(None) is None)
    test("_to_objectid returns ObjectId for valid ID",
         database._to_objectid("507f1f77bcf86cd799439011") is not None)

    # Functional test: get_chat_history with malformed ID doesn't raise
    database.chats_collection = AsyncMock()
    result = asyncio.run(database.get_chat_history("malformed-id", device_id="dev"))
    test("get_chat_history returns [] for malformed chat_id", result == [])

    # Functional test: delete_chat with malformed ID returns False
    result = asyncio.run(database.delete_chat("malformed-id", device_id="dev"))
    test("delete_chat returns False for malformed chat_id", result == False)

test_h4()

# =============================================================================
# H5: Default GROQ_MODEL fixed
# =============================================================================
section("H5: Default GROQ_MODEL fixed")

def test_h5():
    rag = read_file("rag_pipeline.py")
    # This finding originally proposed replacing openai/gpt-oss-120b with
    # llama-3.3-70b-versatile. Re-verified against Groq's live /models
    # endpoint during the merge: llama-3.3-70b-versatile is NOT in the
    # current model list at all, while openai/gpt-oss-120b is (and has
    # been used successfully throughout this project's testing) — so the
    # original default was kept. See the comment above VISION_MODEL and
    # model_name in rag_pipeline.py for the same fact-check on the vision
    # model default.
    test("Keeps the verified-working openai/gpt-oss-120b default",
         'os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")' in rag)
    test("Still configurable via GROQ_MODEL env var",
         'os.getenv("GROQ_MODEL"' in rag)

test_h5()

# =============================================================================
# H6: Replaced deprecated @app.on_event with lifespan
# =============================================================================
section("H6: Replaced @app.on_event with lifespan handler")

def test_h6():
    main = read_file("main.py")
    # Strip comments to avoid false positives
    code_lines = [l for l in main.split("\n") if not l.strip().startswith("#")]
    code = "\n".join(code_lines)
    test("Imports asynccontextmanager",
         "from contextlib import asynccontextmanager" in main)
    test("Has lifespan function",
         "async def lifespan" in main)
    test("Passes lifespan to FastAPI()",
         "FastAPI(lifespan=lifespan)" in main)
    test("No more @app.on_event in actual code (only in comments)",
         "@app.on_event" not in code)
    test("Calls database.connect_db() in lifespan",
         "database.connect_db()" in main)

test_h6()

# =============================================================================
# H7: datetime.utcnow() replaced with datetime.now(timezone.utc)
# =============================================================================
section("H7: datetime.utcnow() replaced with datetime.now(timezone.utc)")

def test_h7():
    db = read_file("database.py")
    # Strip comments to avoid false positives
    code_lines = [l for l in db.split("\n") if not l.strip().startswith("#")]
    code = "\n".join(code_lines)
    test("Imports timezone from datetime",
         "from datetime import datetime, timezone" in db)
    test("No more datetime.utcnow() in actual code (only in comments)",
         "datetime.utcnow()" not in code)
    test("Uses datetime.now(timezone.utc)",
         "datetime.now(timezone.utc)" in code)

    # Count occurrences in code only
    count = code.count("datetime.now(timezone.utc)")
    test(f"datetime.now(timezone.utc) used {count} times (should be >= 2)", count >= 2)

test_h7()

# =============================================================================
# H8: Catchall route excludes API paths
# =============================================================================
section("H8: Catchall route excludes API paths")

def test_h8():
    main = read_file("main.py")
    test("Catchall route checks for API prefixes",
         "catchall.startswith" in main)
    test("Catchall returns 404 for API paths",
         'status_code=404' in main and "catchall.startswith" in main)
    test("Excludes 'upload' path",
         '"upload"' in main and "catchall.startswith" in main)
    test("Excludes 'chat' path",
         '"chat"' in main and "catchall.startswith" in main)
    test("Excludes 'chats' path",
         '"chats"' in main and "catchall.startswith" in main)

test_h8()

# =============================================================================
# H9: Replay ToolMessage/tool_calls in history
# =============================================================================
section("H9: Replay ToolMessage/tool_calls in history")

def test_h9():
    rag = read_file("rag_pipeline.py")
    test("Handles 'tool' role in history replay",
         'role == "tool"' in rag)
    test("Creates ToolMessage for tool role",
         "ToolMessage(" in rag and "tool_call_id" in rag)
    test("Replays tool_calls on AI messages if present",
         'turn.get("tool_calls")' in rag)
    test("Creates AIMessage with tool_calls",
         "AIMessage(content=content, tool_calls=tool_calls)" in rag)

test_h9()

# =============================================================================
# H10: max_tool_rounds exceeded handled gracefully
# =============================================================================
section("H10: max_tool_rounds exceeded handled gracefully")

def test_h10():
    rag = read_file("rag_pipeline.py")
    test("Checks if rounds >= max_tool_rounds after loop",
         "rounds >= max_tool_rounds" in rag)
    test("Does final invoke without tools bound",
         "llm.invoke(messages" in rag)
    test("Returns meaningful message when max rounds exceeded",
         "exceeded the maximum number of tool calls" in rag)

test_h10()

# =============================================================================
# H11: Validate tool call args before invoking
# =============================================================================
section("H11: Validate tool call args before invoking")

def test_h11():
    rag = read_file("rag_pipeline.py")
    test("Checks if args is a dict",
         "isinstance(args, dict)" in rag)
    test("Returns error for non-dict args",
         "invalid tool arguments" in rag)
    test("Wraps tool invocation in try/except",
         "tool_fn.invoke(args)" in rag and "except Exception as tool_err" in rag)

test_h11()

# =============================================================================
# H12: Score threshold for similarity_search
# =============================================================================
section("H12: Score threshold for similarity_search")

def test_h12():
    rag = read_file("rag_pipeline.py")
    test("Uses similarity_search_with_score (not just similarity_search)",
         "similarity_search_with_score" in rag)
    test("Has SCORE_THRESHOLD",
         "SCORE_THRESHOLD" in rag)
    test("Threshold is configurable via env var",
         'os.getenv("RAG_SCORE_THRESHOLD"' in rag)
    test("Filters results by score",
         "score <= SCORE_THRESHOLD" in rag)
    test("Returns message when no relevant content found",
         "No sufficiently relevant content" in rag)

test_h12()

# =============================================================================
# H13: Case-insensitive .pdf check + content-type validation
# =============================================================================
section("H13: Case-insensitive .pdf check + content-type validation")

def test_h13():
    main = read_file("main.py")
    test("Uses .lower().endswith('.pdf')",
         ".lower().endswith('.pdf')" in main)
    test("Validates content_type",
         "content_type" in main and "file.content_type" in main)
    test("Rejects non-PDF content types",
         "Invalid file type" in main)
    test("Accepts application/pdf",
         '"application/pdf"' in main)
    test("Accepts application/octet-stream (browser fallback)",
         '"application/octet-stream"' in main)

test_h13()

# =============================================================================
# H14: Skip vision call on truly blank pages
# =============================================================================
section("H14: Skip vision call on truly blank pages")

def test_h14():
    rag = read_file("rag_pipeline.py")
    test("Checks page_images before vision call",
         "page.get_images()" in rag)
    test("Skips vision for blank pages (no text, no images)",
         "page_images" in rag and "not page_images" in rag)
    test("Returns '[Blank page]' for blank pages",
         '"[Blank page]"' in rag)

test_h14()

# =============================================================================
# H15: import database moved to top of file
# =============================================================================
section("H15: import database moved to top of file")

def test_h15():
    main = read_file("main.py")
    lines = main.split("\n")
    # Find the line number of the first "import database"
    import_line = None
    for i, line in enumerate(lines):
        if line.strip() == "import database":
            import_line = i + 1
            break
    test("import database exists", import_line is not None)
    test("import database is near the top (line <= 20)", import_line is not None and import_line <= 20,
         f"found at line {import_line}")
    test("Only one import database statement",
         main.count("import database") == 1,
         f"found {main.count('import database')} occurrences")

test_h15()

# =============================================================================
# Print results
# =============================================================================
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

print("\n" + "\n".join(RESULTS))
print(f"\n{'='*60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
print(f"{'='*60}")
sys.exit(1 if FAIL > 0 else 0)
