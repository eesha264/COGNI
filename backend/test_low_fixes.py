"""
Tests for all 6 Low-level fixes (L1-L6) in the COGNI codebase.
Run with: ./venv/Scripts/python.exe test_low_fixes.py
"""
import sys
import os

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
# L1: typing.List replaced with list[...]
# =============================================================================
section("L1: typing.List replaced with list[...]")

def test_l1():
    main = read_file("main.py")
    # Strip comments
    code_lines = [l for l in main.split("\n") if not l.strip().startswith("#")]
    code = "\n".join(code_lines)

    test("No more 'from typing import List'", "from typing import List" not in main)
    test("Uses list[WebSocket] instead of List[WebSocket]",
         "list[WebSocket]" in main and "List[WebSocket]" not in code)
    test("No remaining List[...] usage in code",
         "List[" not in code)

test_l1()

# =============================================================================
# L2: print() replaced with logging
# =============================================================================
section("L2: print() replaced with logging in database.py")

def test_l2():
    db = read_file("database.py")
    # Strip comments
    code_lines = [l for l in db.split("\n") if not l.strip().startswith("#")]
    code = "\n".join(code_lines)

    test("Imports logging", "import logging" in db)
    test("Has logger instance", "logger = logging.getLogger" in db)
    test("No more print() in actual code", "print(" not in code)
    test("Uses logger.warning for MONGODB_URI warning",
         'logger.warning("MONGODB_URI' in db)
    test("Uses logger.error for DB errors",
         "logger.error" in db)
    test("At least 2 logger calls (warning + error)",
         db.count("logger.") >= 3)

test_l2()

# =============================================================================
# L3: makeId collision-safe (crypto.randomUUID)
# =============================================================================
section("L3: makeId collision-safe in MainChat.jsx")

def test_l3():
    jsx = read_file("../frontend/src/components/MainChat.jsx")

    test("Uses crypto.randomUUID()", "crypto.randomUUID" in jsx)
    test("Has fallback for older browsers", "Math.random()" in jsx)
    test("Fallback includes random component", "toString(36)" in jsx)
    test("No longer uses only Date.now()+counter (old pattern gone)",
         "`msg-${Date.now()}-${idCounterRef.current++}`" not in jsx)

test_l3()

# =============================================================================
# L4: .gitignore has common dev artifacts
# =============================================================================
section("L4: .gitignore has common dev artifacts")

def test_l4():
    gitignore = read_file("../.gitignore")

    test("Has .venv/", ".venv/" in gitignore)
    test("Has .idea/", ".idea/" in gitignore)
    test("Has *.log", "*.log" in gitignore)
    test("Has .vscode/", ".vscode/" in gitignore)
    test("Has .pytest_cache/", ".pytest_cache/" in gitignore)
    test("Still has original entries (backend/venv/)", "backend/venv/" in gitignore)
    test("Still has frontend/node_modules/", "frontend/node_modules/" in gitignore)

test_l4()

# =============================================================================
# L5: README port fixed (5174 -> 5173)
# =============================================================================
section("L5: README port 5174 -> 5173")

def test_l5():
    readme = read_file("../README.md")

    test("No more localhost:5174", "localhost:5174" not in readme)
    test("Has localhost:5173", "localhost:5173" in readme)

test_l5()

# =============================================================================
# L6: architecture.md expanded with API contract + env vars
# =============================================================================
section("L6: architecture.md expanded")

def test_l6():
    arch = read_file("../architecture.md")

    test("Has API Contract section", "API Contract" in arch or "api contract" in arch.lower())
    test("Documents POST /upload", "/upload" in arch and "POST" in arch)
    test("Documents POST /chat", "/chat" in arch)
    test("Documents GET /chats", "/chats" in arch)
    test("Documents DELETE /chat", "DELETE" in arch)
    test("Documents WebSocket /ws/process", "/ws/process" in arch)
    test("Has Environment Variables section", "Environment Variables" in arch or "environment variables" in arch.lower())
    test("Documents MONGODB_URI", "MONGODB_URI" in arch)
    test("Documents GROQ_API_KEY", "GROQ_API_KEY" in arch)
    test("Documents GROQ_MODEL", "GROQ_MODEL" in arch)
    test("Documents GROQ_VISION_MODEL", "GROQ_VISION_MODEL" in arch)
    test("Documents TAVILY_API_KEY", "TAVILY_API_KEY" in arch)
    test("Documents ALLOWED_ORIGINS", "ALLOWED_ORIGINS" in arch)
    test("Documents MAX_UPLOAD_BYTES", "MAX_UPLOAD_BYTES" in arch)
    test("Documents RAG_SCORE_THRESHOLD", "RAG_SCORE_THRESHOLD" in arch)
    test("Has architecture diagram", "```" in arch and ("──" in arch or "-->" in arch))
    test("Mentions per-document collections", "per-document" in arch.lower())
    test("Mentions device_id ownership", "device_id" in arch)

test_l6()

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
