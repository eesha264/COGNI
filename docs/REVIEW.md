# COGNI — Code Review: Critical Errors & Problems Catalog

**Repository:** https://github.com/eesha264/COGNI.git
**Cloned to:** `experiment/eesha`
**Reviewed:** 2026-08-17
**Reviewer:** Devin (GLM-5.2 High)

---

## Will it run?

| Component | Verdict | Notes |
|-----------|---------|-------|
| **Frontend** | ✅ Runs cleanly | `npm install` → 0 vulnerabilities; `npm run lint` → 0 errors; `npm run build` → built in 6.66s |
| **Backend** | ⚠️ Not on this machine | Only Python 3.14 available; several deps (`langchain-groq`, `chromadb`, `onnxruntime`, `fastembed`) lack cp314 wheels. Should install/run on Python 3.10–3.13. |

**Runtime requirements (even on a correct Python):**
- `MONGODB_URI` in `backend/.env` (gracefully degrades — chat history won't persist)
- `GROQ_API_KEY` (entered in UI Settings, sent per-request)
- `TAVILY_API_KEY` (optional — only for `web_search` tool)
- Hardcoded `ws://127.0.0.1:8000` and `http://127.0.0.1:8000` in frontend — local only

---

## 🔴 CRITICAL (Security / Data Loss / Broken-on-first-use)

| # | File | Lines | Problem | Impact |
|---|------|-------|---------|--------|
| C1 | `backend/main.py` | 95-97 | **Path traversal** — `os.path.join(UPLOAD_DIR, file.filename)` uses raw user filename | Write files anywhere on disk (`../../windows/...`) |
| C2 | `backend/main.py` | 17-23 | **CORS `*` + `allow_credentials=True`** | Spec-violating + open relay; any website can call your API |
| C3 | `backend/main.py` | 122-132 | **No auth + enumerable `chat_id`** (Mongo ObjectId) | Anyone can `GET/DELETE /chat/{id}` on anyone's chats |
| C4 | `backend/main.py` | 41-47 | **WebSocket broadcasts to ALL clients** | User A's upload progress leaks to User B's UI |
| C5 | `backend/database.py` vs `README.md` | 12-15 / 140 | **Env var name mismatch**: README says `MONGODB_URL`, code reads `MONGODB_URI` | Following the README → silent no-history mode |
| C6 | `backend/rag_pipeline.py` | 104-105 | **Vision model `qwen/qwen3.6-27b` doesn't exist on Groq** | Scanned/handwritten PDF OCR always fails |
| C7 | `backend/rag_pipeline.py` | 148-174 | **`process_pdf` reads server `GROQ_API_KEY`, but key lives in browser** | Vision fallback never works unless admin sets env var |
| C8 | `backend/rag_pipeline.py` | 207-216 | **Single global vector store, wiped on every upload** | Only 1 PDF queryable at a time; chat #2 queries chat #1's doc |
| C9 | `backend/rag_pipeline.py` | 133, 207-216 | **Race condition on `vector_store` global** (no lock) | Concurrent uploads corrupt the index |
| C10 | `backend/main.py` | 157-160 | **`query_rag` (sync, 10-30s) called inside `async def chat`** | Blocks event loop; all other requests stall |
| C11 | `backend/main.py` | 76-84 | **`process_pdf` return value ignored** (>400 pages returns error) | UI shows "Done" but nothing was indexed |
| C12 | `backend/rag_pipeline.py` | 138-142 | **File handle leak** — `pymupdf.open()` then early `return` without `close()` | FD leak on every oversized PDF |
| C13 | `backend/main.py` | 95-97 | **No upload size limit** — `shutil.copyfileobj` streams unbounded bytes | Disk exhaustion DoS |
| C14 | `backend/main.py` | 90, 138 | **`device_id` defaults to `"default"`** | All anonymous clients share one global chat pool |
| C15 | `frontend/.../MainChat.jsx` + `App.jsx` | 147-158 / 8,25-28 | **Groq API key in `localStorage` plaintext + sent in form body** | XSS / malicious extension can steal it; persists forever |
| C16 | `frontend/.../MainChat.jsx` | 33-40 | **`normalizeLatexDelimiters` runs on full string before markdown parse** | Mangles `\[`/`\(` inside code blocks → broken rendering |

---

## 🟠 HIGH (Functional bugs / loopholes)

| # | File | Lines | Problem | Impact |
|---|------|-------|---------|--------|
| H1 | `backend/main.py` | 61-84 | **`asyncio.get_event_loop()` hack** deprecated + can deadlock | Silent broadcast failures; possible hang |
| H2 | `backend/main.py` | 99-100 | **`BackgroundTasks` runs heavy sync work on event loop** | Blocks all requests during PDF processing |
| H3 | `backend/main.py` | 151-160 | **`chat_id` silently `None` if DB unavailable** | User messages lost with no error shown |
| H4 | `backend/database.py` | 43-49 | **`ObjectId(chat_id)` raises `InvalidId` uncaught** on malformed id | 500 error instead of clean 400 |
| H5 | `backend/rag_pipeline.py` | 281 | **Default `GROQ_MODEL="openai/gpt-oss-120b"`** may be decommissioned | Broken out-of-box; relies on env override |
| H6 | `backend/main.py` | 113-115 | **`@app.on_event("startup")` deprecated** in modern FastAPI | Future breakage; should use lifespan |
| H7 | `backend/database.py` | 27, 37 | **`datetime.utcnow()` deprecated** in Python 3.12+ | Warnings; breaks on future Python |
| H8 | `backend/main.py` | 179-181 | **Catchall route registered last** — fragile ordering | New API route added after it silently 404s |
| H9 | `backend/rag_pipeline.py` | 294-301 | **History loaded but `ToolMessage`/`tool_calls` not replayed** | Multi-turn tool conversations lose context; model may re-call tools |
| H10 | `backend/rag_pipeline.py` | 312-321 | **`max_tool_rounds=5` exits silently** when exceeded | Returns whatever partial `ai_msg` is — may be empty or a tool-call object |
| H11 | `backend/rag_pipeline.py` | 318 | **`tool_fn.invoke(call["args"])`** — no schema validation of args | Malformed LLM args → unhandled exception inside loop |
| H12 | `backend/rag_pipeline.py` | 257 | **`similarity_search(query, k=4)`** — no score threshold | Returns 4 chunks even if totally irrelevant → hallucination fuel |
| H13 | `backend/main.py` | 92 | **`.endswith('.pdf')` only** — case-sensitive, no content-type check | `.PDF` rejected; `.pdf`-named EXE accepted |
| H14 | `backend/rag_pipeline.py` | 170-174 | **`MIN_TEXT_CHARS=20` threshold** — blank page triggers vision call | Wastes Groq quota on intentionally-blank pages |
| H15 | `backend/main.py` | 111 | **`import database` mid-file** (also at line 103) | Style/maintainability; circular-import risk if refactored |

---

## 🟡 MEDIUM (Quality / robustness / UX)

| # | File | Lines | Problem | Impact |
|---|------|-------|---------|--------|
| M1 | `backend/requirements.txt` | 1-13 | **No version pins** | Non-reproducible; future breaking change silently breaks app |
| M2 | `frontend/src/App.jsx` | 12-19 | **`Math.random().toString(36).substr(2,9)`** — `substr` deprecated, ~46 bits entropy | Collisions possible; not a real device fingerprint |
| M3 | `frontend/src/App.jsx` | 40, 71, 93, 121, 125, 155 | **Hardcoded `http://127.0.0.1:8000`** in 6 places | Breaks on deploy; no env config |
| M4 | `frontend/.../MainChat.jsx` | 20-28 | **Custom `sanitizeSchema` adds `className` to div/span** | Minor XSS surface from LLM output; worth a second look |
| M5 | `frontend/.../MainChat.jsx` | 160-163 | **`response.json()` on error** assumes JSON body | Crashes if server returns text/HTML error |
| M6 | `frontend/.../MainChat.jsx` | 119-130 | **`useEffect` missing `makeId` in deps** (ESLint-clean only because it's a ref) | Stale closure risk if refactored |
| M7 | `frontend/.../MainChat.jsx` | 124 | **`setMessages(...map(m => ({...m, id: makeId()})))`** regenerates IDs every load | React remounts every bubble → loses copy state, re-runs KaTeX |
| M8 | `frontend/.../LeftSidebar.jsx` | 11 | **Collapse button `◫` has no `onClick`** | Dead UI element |
| M9 | `frontend/.../SettingsModal.jsx` | 14-18 | **"Log out" reloads page** instead of clearing state in-app | Jarring UX; loses any in-flight upload |
| M10 | `frontend/.../MainChat.jsx` | 90-93 | **`navigator.clipboard.writeText` no HTTPS check** | Fails on non-localhost HTTP deploy silently |
| M11 | `backend/rag_pipeline.py` | 26 | **`import time` mid-file** | Style inconsistency |
| M12 | `backend/rag_pipeline.py` | 136, 146, 199, 203, 220, 224 | **6× `time.sleep(1)`** purely for UI animation pacing | Adds 6s artificial latency to every upload |
| M13 | `backend/rag_pipeline.py` | 17-24 | **`embeddings` + `vector_store` initialized at import time** | Slow startup; fails import if Chroma dir locked; can't reconfigure |
| M14 | `backend/database.py` | 51-65 | **`get_chats` returns max 100, no pagination** | Scaling ceiling |
| M15 | `backend/main.py` | 170-181 | **Frontend served only if `dist/` exists at import time** | Build after server start → not served until restart |
| M16 | `frontend/.../MainChat.jsx` | 277 | **Enter sends, no Shift+Enter for newline** | Can't write multi-line questions |
| M17 | `frontend/.../RightSidebar.jsx` | 4-12 | **Step labels duplicated as magic strings** in both backend & frontend | Drift between sides; rename in one place breaks the other |
| M18 | `frontend/package.json` | 12-25 | **1MB bundle, no code-splitting** (KaTeX + highlight.js + react-markdown) | Slow first load |
| M19 | whole repo | — | **Zero tests** (no unit/integration/e2e) | Refactors are unsafe |
| M20 | `backend/rag_pipeline.py` | 81-94 | **`web_search` tool has no rate limit / caching** | One conversation can burn the Tavily key quota |

---

## 🟢 LOW (Nitpicks)

| # | File | Lines | Problem |
|---|------|-------|---------|
| L1 | `backend/main.py` | 5 | `from typing import List` — could use `list[...]` (3.9+) |
| L2 | `backend/database.py` | 14 | `print()` for warnings — should use `logging` |
| L3 | `frontend/.../MainChat.jsx` | 83 | `makeId` uses `Date.now()` + counter — not collision-safe across tabs |
| L4 | `.gitignore` | — | No `.venv/`, `.idea/`, `*.log` entries |
| L5 | `README.md` | 157 | Says port `5174` — Vite default is `5173` |
| L6 | `architecture.md` | — | One-paragraph doc; no API contract, no env var reference |

---

## Things that are actually good

- **`_safe_eval` AST calculator** (`rag_pipeline.py:45-65`) — genuinely safe arithmetic, no `eval()`, no name/call/attribute access.
- **Bounded tool-calling loop** (`max_tool_rounds=5`) — no infinite tool-call loops.
- **`source_pages_used` tracked from actual tool results**, not inferred from similarity scores — honest citation.
- **Graceful MongoDB degradation** when `MONGODB_URI` is missing.
- **`rehype-sanitize` actually applied** (most tutorials skip it).
- **Strict system prompt** explicitly tells the model not to fabricate — good RAG hygiene.

---

## Summary

| Severity | Count |
|----------|-------|
| 🔴 Critical | **16** |
| 🟠 High | **15** |
| 🟡 Medium | **20** |
| 🟢 Low | **6** |
| **Total** | **57** |

---

## Top 5 to fix before anything else

1. **C1** — Path traversal in upload (`os.path.basename` or UUID filename)
2. **C2** — CORS `*` + credentials (restrict origins)
3. **C3** — No auth + enumerable chat IDs (add ownership check, use UUIDs)
4. **C8** — Single global vector store (per-document collections or namespaces)
5. **C10** — Sync LLM call blocking event loop (`await asyncio.to_thread(...)`)

---

## Bottom line

**Frontend: production-quality for a demo. Backend: a solid prototype with real security holes.**

It will run on Python 3.10–3.13 with the right env vars, but it is **not safe to deploy publicly** as-is: open CORS + no auth + enumerable ObjectIds + path traversal + plaintext API keys in localStorage are the five that matter. For a personal/local project it's fine; for anything exposed to the internet, fix at least C1, C2, C3, and C15 before putting it online.
