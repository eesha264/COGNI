import os
import ast
import asyncio
import base64
import operator
import re
import time
import threading
from datetime import datetime, timezone
import pymupdf  # fitz
from tavily import TavilyClient
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from pii_guard import redact as redact_pii, RAIL_ENTITIES, TokenStore
from guardrails import input_rail
from mcp_client_manager import get_mcp_tools, MCP_TOOL_TIMEOUT

# Embeddings are expensive to set up (FastEmbed loads/downloads a model) —
# initialize lazily on first actual use instead of at import time, so the
# server starts instantly and doesn't crash on import if something's wrong.
# M13 fix: double-checked locking so concurrent requests racing to
# initialize don't each construct their own FastEmbedEmbeddings instance.
_embeddings = None
_embeddings_lock = threading.Lock()


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        with _embeddings_lock:
            if _embeddings is None:
                # FastEmbed is lightweight and runs locally without needing an API key
                _embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")
    return _embeddings

# M19 fix: use absolute path so Chroma persists regardless of working directory.
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_db")

# C8 fix: per-document collections instead of a single global vector_store.
# Previously process_pdf wiped the whole collection on every upload, so only
# the most recent PDF was queryable. Now each upload gets its own collection
# keyed by a document_id, and query_rag looks up the right collection per chat.
# A module-level cache keeps Chroma clients cheap to reuse.
_vector_store_cache: dict[str, Chroma] = {}
_vector_store_lock = threading.Lock()
# M6 fix: cap the cache size to prevent unbounded memory growth.
_MAX_VECTOR_STORE_CACHE = 50


def get_vector_store(collection_name: str) -> Chroma:
    """Return (and cache) a Chroma store for the given per-document collection."""
    with _vector_store_lock:
        vs = _vector_store_cache.get(collection_name)
        if vs is None:
            # M6 fix: evict oldest entries if cache is full.
            if len(_vector_store_cache) >= _MAX_VECTOR_STORE_CACHE:
                oldest_key = next(iter(_vector_store_cache))
                _vector_store_cache.pop(oldest_key, None)
            vs = Chroma(
                collection_name=collection_name,
                embedding_function=_get_embeddings(),
                persist_directory=CHROMA_PERSIST_DIR,
            )
            _vector_store_cache[collection_name] = vs
        return vs


def delete_vector_store(collection_name: str) -> None:
    """Delete a per-document collection if it exists, and drop it from the cache."""
    with _vector_store_lock:
        vs = _vector_store_cache.pop(collection_name, None)
        if vs is None:
            vs = Chroma(
                collection_name=collection_name,
                embedding_function=_get_embeddings(),
                persist_directory=CHROMA_PERSIST_DIR,
            )
        try:
            vs.delete_collection()
        except Exception:
            # Collection didn't exist — nothing to delete.
            pass

# --- Tools available to the LLM (bound in query_rag via bind_tools) ---

_ALLOWED_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(node):
    """Evaluate only numeric arithmetic AST nodes — no names, calls, or attribute access."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BIN_OPS:
        # H5 fix: cap exponent values to prevent DoS via expressions like
        # 2**999999999 which would hang the thread indefinitely computing
        # an astronomically large number.
        if isinstance(node.op, ast.Pow):
            exp_val = _safe_eval(node.right)
            if isinstance(exp_val, (int, float)) and exp_val > 1000:
                raise ValueError("Exponent too large (max 1000).")
        return _ALLOWED_BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY_OPS:
        return _ALLOWED_UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Expression contains unsupported operations.")


@tool
def calculator(expression: str) -> str:
    """Evaluate a precise arithmetic expression (e.g. "12.5 * (3 + 7) / 2").
    Use this whenever the user asks for a calculation instead of computing it mentally."""
    try:
        parsed = ast.parse(expression, mode="eval").body
        result = _safe_eval(parsed)
        return str(result)
    except Exception:
        return "Error: could not evaluate that expression. Only numbers and + - * / % ** are supported."


@tool
def get_current_datetime() -> str:
    """Return the current server date and time. Use this whenever the user asks what
    today's date is, what time it is, or a question relative to "today"/"now"."""
    # M26 fix: use timezone-aware UTC instead of naive local time.
    now = datetime.now(timezone.utc)
    return now.strftime("%A, %B %d, %Y at %I:%M %p (server local time)")


MAX_WEB_SEARCHES_PER_CONVERSATION = 5  # web_search is created per-call in query_rag(), see there

# L6 fix: reuse a single TavilyClient instead of instantiating one per call.
_tavily_client = None
_tavily_lock = threading.Lock()

def _get_tavily_client():
    global _tavily_client
    if _tavily_client is None:
        with _tavily_lock:
            if _tavily_client is None:
                api_key = os.getenv("TAVILY_API_KEY")
                if api_key:
                    _tavily_client = TavilyClient(api_key=api_key)
    return _tavily_client

# M20 fix: cache web_search results across calls/conversations so a repeated
# identical query doesn't burn Tavily quota. TTL-bounded rather than kept
# forever, since "current events/prices" queries can go stale — the whole
# point of this tool is to fetch live data, so an indefinitely-lived cache
# would silently start returning wrong answers.
_web_search_cache: dict[str, tuple[float, str]] = {}
_web_search_cache_lock = threading.Lock()
WEB_SEARCH_CACHE_TTL = 300  # 5 minutes
# M7 fix: cap the cache size to prevent unbounded growth.
_MAX_WEB_SEARCH_CACHE = 100

AVAILABLE_TOOLS = [calculator, get_current_datetime]
_TOOLS_BY_NAME = {t.name: t for t in AVAILABLE_TOOLS}

# L10 fix: create the text splitter once at module level instead of per
# process_pdf call — it's stateless and thread-safe.
_TEXT_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=200,
    length_function=len,
)

# A page with less than this many non-whitespace characters of embedded digital
# text is treated as scanned/handwritten/image-only, and routed to vision instead.
MIN_TEXT_CHARS = 20

# --- Vision OCR configuration ---
# DPI for page rendering when sending to Groq vision. 100 (down from 150)
# reduces image token count ~40% while remaining readable for OCR.
VISION_DPI = int(os.getenv("VISION_DPI", "100"))
# Number of pages to send per Groq vision call. Vision models accept multiple
# images in one message — batching reduces API calls (and RPM pressure) by
# this factor. Most Groq vision models support up to 3 images per request;
# if a batch exceeds the model's limit, the code auto-falls back to
# individual page calls (see _extract_pages_via_vision_batch).
VISION_BATCH_SIZE = int(os.getenv("VISION_BATCH_SIZE", "3"))
# Max retry attempts on rate-limit (429) or transient errors.
VISION_MAX_RETRIES = int(os.getenv("VISION_MAX_RETRIES", "4"))
# Target requests per minute — stay just under Groq's RPM limit. Free tier
# is 30 RPM; defaulting to 28 leaves headroom. Set higher for paid tiers.
VISION_RPM_LIMIT = int(os.getenv("VISION_RPM", "28"))
# Base delay (seconds) for exponential backoff on 429. Doubled each retry:
# 2s -> 4s -> 8s -> 16s.
VISION_RETRY_BASE_DELAY = 2.0

# The C6 finding from the original review claimed "qwen/qwen3.6-27b" doesn't
# exist on Groq and proposed replacing it with "meta-llama/llama-4-scout-17b-
# 16e-instruct" — re-verified against Groq's live /models endpoint just before
# this merge: qwen/qwen3.6-27b IS currently listed and working (confirmed via
# real vision transcription tests), while the proposed replacement is NOT in
# the current model list at all. Keeping the verified-working model; still
# allow override via env var since Groq's vision lineup does rotate.
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")


def _extract_text_via_vision(page: "pymupdf.Page", groq_api_key: str, vision_llm=None) -> str:
    """Renders a page as an image and asks a Groq vision model to transcribe it,
    including handwriting, and to format any tables as Markdown.

    Uses VISION_DPI (default 100, down from 150) to reduce image token count.
    Retries on rate-limit (429) errors with exponential backoff. Accepts an
    optional reused ChatGroq instance to avoid constructing one per page.
    """
    if vision_llm is None:
        vision_llm = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=VISION_MODEL)

    pix = page.get_pixmap(dpi=VISION_DPI)
    b64_image = base64.b64encode(pix.tobytes("png")).decode("utf-8")

    message = HumanMessage(content=[
        {"type": "text", "text": (
            "Transcribe every piece of text visible on this page exactly as written, "
            "including any handwriting. If the page contains tables, format them as "
            "clean Markdown tables. Output only the transcribed content — no commentary."
        )},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
    ])

    for attempt in range(VISION_MAX_RETRIES + 1):
        try:
            response = vision_llm.invoke([message])
            return response.content or ""
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in err_str or "rate limit" in err_str or "rate_limit" in err_str
            if attempt < VISION_MAX_RETRIES:
                if is_rate_limit:
                    delay = VISION_RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
                elif attempt == 0:
                    time.sleep(1)
                    continue
                else:
                    return f"[Vision extraction failed for this page: {e}]"
            else:
                return f"[Vision extraction failed for this page: {e}]"
    return "[Vision extraction failed: max retries exceeded]"


def _extract_pages_via_vision_batch(pages_and_nums, groq_api_key, vision_llm=None):
    """Transcribe a batch of pages in a single Groq vision call.

    Sends multiple page images in one message (vision models support this),
    asking the model to label each page's transcription with '=== Page N ==='.
    Reduces API calls by VISION_BATCH_SIZE compared to per-page calls.

    Args:
        pages_and_nums: list of (page_num_0indexed, page) tuples
        groq_api_key: Groq API key
        vision_llm: optional reused ChatGroq instance

    Returns:
        dict: {page_num_1indexed: transcribed_text}. Pages that couldn't be
        parsed from the response get an empty string; fully failed batches
        return an error string for every page.
    """
    if vision_llm is None:
        vision_llm = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=VISION_MODEL)

    page_labels = [p[0] + 1 for p in pages_and_nums]

    content_parts = [
        {"type": "text", "text": (
            f"Transcribe every piece of text visible on each page exactly as written, "
            f"including any handwriting. If a page contains tables, format them as "
            f"clean Markdown tables. There are {len(pages_and_nums)} pages. "
            f"Label each page's transcription with '=== Page N ===' on its own line "
            f"before that page's content (where N is the page number: {', '.join(str(l) for l in page_labels)}). "
            f"Output only the transcribed content — no commentary."
        )}
    ]

    for page_num, page in pages_and_nums:
        pix = page.get_pixmap(dpi=VISION_DPI)
        b64_image = base64.b64encode(pix.tobytes("png")).decode("utf-8")
        content_parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
        )

    message = HumanMessage(content=content_parts)

    for attempt in range(VISION_MAX_RETRIES + 1):
        try:
            response = vision_llm.invoke([message])
            raw = response.content or ""

            # Parse response: split on "=== Page N ===" markers
            results = {}
            parts = re.split(r'===\s*Page\s*(\d+)\s*===', raw)
            for i in range(1, len(parts), 2):
                pnum = int(parts[i])
                text = parts[i + 1].strip() if i + 1 < len(parts) else ""
                results[pnum] = text

            # If parsing failed (no markers), fall back to assigning all
            # text to the first page — the retry pass will handle the rest.
            if not results:
                results[page_labels[0]] = raw

            # Ensure every page has an entry
            for label in page_labels:
                if label not in results:
                    results[label] = ""
            return results
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in err_str or "rate limit" in err_str or "rate_limit" in err_str
            is_too_many_images = "too many images" in err_str or "maximum.*image" in err_str

            # Auto-fallback: if the model doesn't support this many images,
            # split the batch into individual page calls instead of retrying.
            if is_too_many_images and len(pages_and_nums) > 1:
                individual_results = {}
                for pn, pg in pages_and_nums:
                    individual_results[pn + 1] = _extract_text_via_vision(
                        pg, groq_api_key, vision_llm=vision_llm
                    )
                return individual_results

            if attempt < VISION_MAX_RETRIES:
                if is_rate_limit:
                    delay = VISION_RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
                elif attempt == 0:
                    time.sleep(1)
                    continue
                else:
                    return {label: f"[Vision extraction failed: {e}]" for label in page_labels}
            else:
                return {label: f"[Vision extraction failed: {e}]" for label in page_labels}
    return {label: "[Vision extraction failed: max retries exceeded]" for label in page_labels}


def process_pdf(file_path: str, callback=None, groq_api_key: str = None, document_id: str = None):
    """
    Extracts text, tables, and image metadata from a PDF up to 400 pages.
    C8 fix: indexes into a per-document collection (named by document_id) so
    uploading a new PDF no longer wipes the previous one.
    """
    if callback:
        callback("Analyzing the pdf")


    # C12 fix: use try/finally so the file handle is always closed, even on
    # early returns (e.g. >400 pages) or exceptions.
    doc = pymupdf.open(file_path)
    try:
        # Check page limit
        if len(doc) > 400:
            return {"error": "PDF exceeds 400 pages limit."}

        if callback:
            callback("Analyze images")
    

        # C7 fix: prefer the caller-provided key (from the browser) over the env var
        # so vision OCR works even when the server admin hasn't set GROQ_API_KEY.
        if not groq_api_key:
            groq_api_key = os.getenv("GROQ_API_KEY")

        # L10 fix: use the module-level text splitter instead of creating one per call.
        all_chunks = []
        all_metadatas = []

        # --- Pass 1: extract digital text, identify scanned pages ---
        # Typed PDFs have embedded text (fast, free, no API calls). Scanned
        # or handwritten pages have little/no digital text and need Groq
        # vision OCR. Collect all scanned pages first so we can batch them
        # and respect Groq's rate limits instead of firing one call per page
        # in a tight loop (which blows past RPM/TPM limits on large PDFs).
        scanned_pages = []  # list of (page_num_0indexed, page) tuples
        page_texts = {}     # page_num_0indexed -> extracted text

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text = page.get_text("text")

            if len(text.strip()) < MIN_TEXT_CHARS:
                page_images = page.get_images()
                if not page_images and len(text.strip()) == 0:
                    # Truly blank page — no text, no images. Skip vision entirely.
                    page_texts[page_num] = "[Blank page]"
                elif groq_api_key:
                    # Needs vision OCR — collect for batch processing below.
                    scanned_pages.append((page_num, page))
                else:
                    page_texts[page_num] = text + "\n[Note: this page appears to be scanned/handwritten but could not be analyzed — GROQ_API_KEY not configured]"
            else:
                page_texts[page_num] = text

        # --- Batch vision OCR for scanned pages ---
        # Send VISION_BATCH_SIZE pages per Groq call (vision models accept
        # multiple images in one message). Rate-limit between batches to
        # stay under VISION_RPM_LIMIT. Failed pages get retried individually
        # at the end (one image per call is more reliable for parsing).
        if scanned_pages:
            vision_llm = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=VISION_MODEL)
            failed_pages = []
            min_delay = 60.0 / VISION_RPM_LIMIT  # seconds between API calls
            last_request_time = 0.0

            for batch_start in range(0, len(scanned_pages), VISION_BATCH_SIZE):
                batch = scanned_pages[batch_start:batch_start + VISION_BATCH_SIZE]

                # Rate limit: wait if we're sending requests too fast
                if last_request_time > 0:
                    elapsed = time.time() - last_request_time
                    if elapsed < min_delay:
                        time.sleep(min_delay - elapsed)

                batch_results = _extract_pages_via_vision_batch(
                    batch, groq_api_key, vision_llm=vision_llm
                )
                last_request_time = time.time()

                for page_num, page in batch:
                    page_1indexed = page_num + 1
                    text = batch_results.get(page_1indexed)
                    if text is None or text.startswith("[Vision extraction failed"):
                        # Batch parsing may have failed for this page —
                        # retry it individually below.
                        failed_pages.append((page_num, page))
                    else:
                        page_texts[page_num] = text

            # Retry failed pages individually (one image per call)
            if failed_pages:
                for page_num, page in failed_pages:
                    if last_request_time > 0:
                        elapsed = time.time() - last_request_time
                        if elapsed < min_delay:
                            time.sleep(min_delay - elapsed)

                    page_texts[page_num] = _extract_text_via_vision(
                        page, groq_api_key, vision_llm=vision_llm
                    )
                    last_request_time = time.time()

        # --- Pass 2: tables, image metadata, chunking ---
        for page_num in range(len(doc)):
            try:
                page = doc.load_page(page_num)
                text = page_texts.get(page_num, "")

                # Extract structured tables locally (free, no AI) for typed pages
                table_markdown = ""
                try:
                    found = page.find_tables()
                    if found.tables:
                        table_markdown = "\n\n".join(t.to_markdown() for t in found.tables)
                except Exception:
                    pass

                # Get image metadata
                images = page.get_images()
                image_info = f"\n[Page {page_num+1} contains {len(images)} images]\n" if images else ""

                page_content = f"--- Page {page_num+1} ---\n{text}\n{image_info}"
                if table_markdown:
                    page_content += f"\n[Tables detected on page {page_num+1}]\n{table_markdown}\n"

                page_chunks = _TEXT_SPLITTER.split_text(page_content)
                all_chunks.extend(page_chunks)
                all_metadatas.extend([{"page": page_num + 1}] * len(page_chunks))
            except Exception as e:
                # M17 fix: don't let one corrupted page crash entire processing.
                all_chunks.append(f"--- Page {page_num+1} ---\n[Error processing this page: {e}]")
                all_metadatas.append({"page": page_num + 1})

        if callback:
            callback("Analyze tables")
    

        if callback:
            callback("Convert to text")
    

    finally:
        doc.close()

    if callback:
        callback("Create embeddings")

    # C8 fix: index into a per-document collection instead of wiping a global one.
    collection_name = f"pdf_{document_id}" if document_id else "pdf_default"
    # Replace any previous collection for this document_id (re-upload of same doc)
    delete_vector_store(collection_name)
    vs = get_vector_store(collection_name)

    if callback:
        callback("Save to Vector DB")

    # Add new chunks, tagged with their source page
    vs.add_texts(all_chunks, metadatas=all_metadatas)

    # Phase 2: Extract tables into SQLite for structured queries.
    # This runs in parallel with the text RAG path — text chunks go to
    # ChromaDB (for qualitative questions), table rows go to SQLite (for
    # data/comparison/aggregation questions). The LLM picks which tool to
    # use based on the question type.
    if callback:
        callback("Extracting tables")
    try:
        import table_store
        table_store.extract_and_store_tables(file_path, document_id)
    except Exception as e:
        # Table extraction failure shouldn't fail the whole upload —
        # text RAG still works, just no structured queries for this doc.
        print(f"[table_store] Warning: table extraction failed for {document_id}: {e}", flush=True)

    if callback:
        callback("Done")

    # M4 fix: return an error if no chunks were extracted (e.g. a fully-blank
    # or corrupt PDF) so the frontend doesn't show "Done" while every query
    # returns "No document uploaded."
    if not all_chunks:
        return {"error": "No text content could be extracted from this PDF. It may be blank or corrupt."}

    return {"status": "success", "chunks_processed": len(all_chunks), "document_id": document_id}


MAX_HISTORY_MESSAGES = 5  # most recent messages kept for conversational context


def _can_failover(err_str: str) -> bool:
    """Check if an error is worth retrying on the fallback provider.
    Returns True for rate limits, server errors, and tool-calling issues.
    Returns False for decommissioned models (switching providers won't help
    — the user needs to update their model config)."""
    err_lower = err_str.lower()
    # Don't failover on decommission errors — the user needs to fix their config
    if "decommissioned" in err_lower or "model_decommissioned" in err_lower:
        return False
    # Failover on rate limits, server errors, tool calling issues, etc.
    failover_keywords = [
        "429", "rate limit", "resource_exhausted", "quota",
        "500", "503", "server error", "timeout",
        "tool choice", "e1041", "invalid_request", "invalid_argument",
        "api key not valid", "api_key_invalid", "unauthorized",
        "request too large", "tokens per minute", "tpm",
    ]
    return any(kw in err_lower for kw in failover_keywords)


# --- Phase 4: Retry/backoff helpers ---
# Module-level retry counter to prevent retry storms across concurrent calls.
# Each query_rag call gets at most 1 same-provider retry before failover.
_retry_count = [0]


def _is_rate_limit(err_str: str) -> bool:
    """Check if the error is a rate-limit error (worth retrying after a wait)."""
    err_lower = err_str.lower()
    return any(kw in err_lower for kw in ["429", "rate limit", "resource_exhausted", "quota"])


def _extract_retry_seconds(err_str: str) -> int:
    """Extract the retry delay from a rate-limit error message.
    Gemini returns 'retry in 5s' or 'retryDelay': '5s'.
    Groq returns 'please try again in 375ms'.
    Returns the delay in seconds (rounded up), or 0 if not found."""
    import re
    # Look for patterns like "retry in 5s", "retry in 59.6s", "retryDelay': '5s'"
    match = re.search(r'retry[^0-9]*(\d+(?:\.\d+)?)\s*s', err_str, re.IGNORECASE)
    if match:
        return int(float(match.group(1))) + 1
    # Look for milliseconds: "try again in 375ms"
    match = re.search(r'try again in (\d+)\s*ms', err_str, re.IGNORECASE)
    if match:
        return int(int(match.group(1)) / 1000) + 1
    return 0


# --- Phase 3: Smart routing ---
# Keywords that indicate a question needs table tools (and therefore the
# provider with reliable tool calling — Gemini). Questions without these
# keywords are routed to Groq for speed.
_TABLE_KEYWORDS = [
    "compare", "comparison", "vs", "versus", "difference", "differ",
    "total", "sum", "cost", "price", "amount", "calculate",
    "count", "how many", "number of", "quantity",
    "average", "avg", "mean", "min", "max", "minimum", "maximum",
    "aggregate", "join", "cross-reference", "rate", "budget",
    "all items", "list all", "every item", "unique to",
    "has but", "does not have", "doesn't have", "not in",
    "more than", "less than", "greater than", "cheaper", "expensive",
]


def _needs_table_tools(question: str, document_ids: list) -> bool:
    """Heuristic: does this question need table tools (join, compare, aggregate)?
    If so, route to Gemini which handles multi-tool sequences reliably.
    If not, route to Groq which is faster for simple Q&A."""
    if not document_ids:
        return False
    q_lower = question.lower()
    return any(kw in q_lower for kw in _TABLE_KEYWORDS)


async def query_rag(question: str, groq_api_key: str, history=None, document_ids=None, document_names: dict = None, provider: str = None):
    """
    Queries the vector store and returns a response from the LLM using the strict system prompt,
    conditioned on the prior conversation (if any) so follow-up questions have continuity.
    Returns a dict: {"answer": str, "tools_used": [str], "source_pages": [int]}.
    C8 fix: queries the per-document collection identified by document_id.
    Multi-PDF fix: document_ids may be a list — a chat can have more than one
    PDF attached (uploaded together in one batch), and search_document below
    searches every one of them, merging results by relevance. A single string
    is still accepted for backward compatibility. document_names optionally
    maps document_id -> original filename, so search results can be labeled
    with a real filename instead of an opaque document_id.
    """
    if not groq_api_key:
        return {"answer": "Error: Groq API key is missing. Please add it to your environment.", "tools_used": [], "source_pages": []}

    # Input rail (privacy guardrails, Phase 3): block requests whose intent is
    # to extract or unmask sensitive data before any retrieval or LLM call
    # happens, and redact any incidental PII in an otherwise-benign question
    # before it's ever sent to Groq.
    gate = input_rail(question)
    if not gate["allowed"]:
        return {"answer": gate["reason"], "tools_used": [], "source_pages": []}
    question = gate["query"]

    # C8 fix: use the per-document collection(s) instead of the global vector_store.
    # Multi-PDF fix: normalize document_ids to a list so both single-PDF chats
    # (old behavior) and multi-PDF chats (new behavior) are handled the same way.
    if isinstance(document_ids, str):
        document_ids = [document_ids]
    doc_ids = [d for d in (document_ids or []) if d]
    collection_names = [f"pdf_{d}" for d in doc_ids] or ["pdf_default"]
    vector_stores = [get_vector_store(name) for name in collection_names]
    # Label for each vector store index, used when a chunk's source needs to be
    # attributed to a specific document in a multi-PDF chat (falls back to a
    # generic "Document N" label if no filename was recorded for that upload).
    doc_labels = [
        (document_names or {}).get(d) or f"Document {i + 1}"
        for i, d in enumerate(doc_ids)
    ] or ["the uploaded document"]

    # Pages actually consulted this turn — populated only when search_document is
    # called, so the "Source: Page X" shown to the user is precise by construction
    # rather than inferred from a similarity score (scores don't reliably separate
    # relevant from irrelevant content, especially on small documents).
    source_pages_used = []

    # web_search call budget for this conversation: count turns that already used
    # it (from history) plus calls made in this turn, so one long conversation
    # can't burn through the Tavily quota unbounded.
    prior_search_count = sum(
        1 for m in (history or []) if m.get("role") == "ai" and "web_search" in (m.get("tools_used") or [])
    )
    search_calls_this_turn = [0]

    @tool
    def web_search(query: str) -> str:
        """Search the live web for information not in the uploaded document and not reliably
        known from memory — current events, weather, prices, or any fact that could have
        changed since training. Use this instead of guessing whenever precision matters."""
        # M20 fix: serve from cache first. A cache hit doesn't touch Tavily
        # or count against the per-conversation limit below, since no real
        # search ran — only a live API call should consume that budget.
        cache_key = query.lower().strip()
        with _web_search_cache_lock:
            cached = _web_search_cache.get(cache_key)
            if cached is not None:
                cached_time, cached_result = cached
                if time.time() - cached_time < WEB_SEARCH_CACHE_TTL:
                    return cached_result
                del _web_search_cache[cache_key]

        if prior_search_count + search_calls_this_turn[0] >= MAX_WEB_SEARCHES_PER_CONVERSATION:
            return (
                f"Error: web search limit reached for this conversation (max {MAX_WEB_SEARCHES_PER_CONVERSATION}). "
                "Answer using what you already know or have already found, and tell the user "
                "live search is unavailable for the rest of this conversation."
            )
        search_calls_this_turn[0] += 1
        # L6 fix: reuse the shared TavilyClient
        client = _get_tavily_client()
        if client is None:
            return "Error: web search is not configured (missing TAVILY_API_KEY)."
        try:
            response = client.search(query, max_results=3, include_answer=True)
            if response.get("answer"):
                result = response["answer"]
            else:
                results = response.get("results", [])
                if not results:
                    result = "No web results found for that query."
                else:
                    result = "\n\n".join(f"{r['title']}: {r['content']}" for r in results[:3])
            with _web_search_cache_lock:
                # M7 fix: evict oldest entries if cache is full.
                if len(_web_search_cache) >= _MAX_WEB_SEARCH_CACHE:
                    oldest_key = next(iter(_web_search_cache))
                    _web_search_cache.pop(oldest_key, None)
                _web_search_cache[cache_key] = (time.time(), result)
            return result
        except Exception as e:
            return f"Error performing web search: {e}"

    @tool
    def search_document(query: str) -> str:
        """Search the uploaded document(s) for content relevant to a query. Use this
        whenever the user's question might be about the uploaded document(s) — never
        assume you already know its contents, and never guess at what it says
        without searching first. If more than one document was uploaded, this
        searches all of them together. Returns the most relevant excerpts, each
        labeled with its source document and page number."""
        # H12 fix: use similarity_search_with_score and filter by a score
        # threshold so totally irrelevant chunks aren't returned (which would
        # fuel hallucination). Chroma returns L2 distance — lower is better.
        # A threshold of 1.0 is a reasonable default for bge-small-en-v1.5.
        #
        # Multi-PDF fix: query every attached document's collection and merge
        # the results by score, so a question can be answered from whichever
        # document(s) actually contain the relevant content — the model isn't
        # told in advance which of several uploaded PDFs holds the answer.
        results_with_scores = []
        for i, one_vs in enumerate(vector_stores):
            try:
                for doc, score in one_vs.similarity_search_with_score(query, k=4):
                    doc.metadata["_doc_index"] = i
                    results_with_scores.append((doc, score))
            except Exception:
                continue
        results_with_scores.sort(key=lambda pair: pair[1])

        if not results_with_scores:
            return "No document has been uploaded yet, or the document index is empty."

        # Filter out chunks with a high distance score (low relevance)
        # M2 fix: wrap in try/except so an invalid env value doesn't crash.
        try:
            SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "1.0"))
        except (ValueError, TypeError):
            SCORE_THRESHOLD = 1.0
        filtered = [(doc, score) for doc, score in results_with_scores if score <= SCORE_THRESHOLD][:4]

        if not filtered:
            return "No sufficiently relevant content was found in the uploaded document(s) for this query."

        # Retrieval rail (privacy guardrails, Phase 4): redact high-confidence
        # PII (emails, phone numbers, card/SSN/PAN/Aadhaar numbers) out of
        # retrieved chunks before they're appended to the message list the
        # model sees. This is a safety net, not the primary control — the
        # real fix is redacting at ingestion time so these values are never
        # embedded/stored in the first place, which is separate, larger work
        # not yet built. Deliberately uses RAIL_ENTITIES (not the broader
        # PERSON/LOCATION set) for the same reason discovered in Phase 3:
        # blanket-redacting every name or place in a document chunk breaks
        # ordinary answers that legitimately reference a named person or
        # place in the source text (e.g. "What did John Smith conclude?").
        # Ingestion-time redaction is the right place for that broader,
        # name-aware coverage, using a consistent per-document token scheme
        # instead of ad hoc redaction on every retrieval.
        parts = []
        for doc, score in filtered:
            page = doc.metadata.get("page")
            # L13 fix: handle None page numbers gracefully
            page_label = f"[Page {page}]" if page is not None else "[Page unknown]"
            if page is not None and page not in source_pages_used:
                source_pages_used.append(page)
            content = redact_pii(doc.page_content, TokenStore(), entities=RAIL_ENTITIES)
            doc_idx = doc.metadata.get("_doc_index", 0)
            label = doc_labels[doc_idx] if doc_idx < len(doc_labels) else "the uploaded document"
            # Multi-PDF fix: only prefix the source label when more than one
            # document is attached — keeps single-PDF output identical to before.
            if len(vector_stores) > 1:
                parts.append(f"[{label}, Page {page}]\n{content}")
            else:
                parts.append(f"{page_label}\n{content}")
        return "\n\n".join(parts)

    # ── Phase 2: Structured table query tools ──────────────────────────
    # These tools query the SQLite database populated by table_store.py
    # during process_pdf. They give the LLM exact rows from extracted
    # tables — for comparison, aggregation, and filtered lookups that
    # text-based search_document can't do accurately.
    import table_store as _table_store
    import json as _json
    import re as _re

    # Build a human-readable name for each SQLite table so the LLM never
    # sees the raw hex document_id in table names. The raw SQLite names
    # look like "doc_500753c082b94178959e25b6a9f37607_page_1_table_1" —
    # useless to the LLM and ugly if it repeats them in an answer. We map
    # them to readable labels like "Villa 101 Inventory — Page 1, Table 1"
    # and keep a reverse map so the other table tools can translate back.
    def _make_table_label(raw_name: str) -> str:
        """Convert a raw SQLite table name to a human-readable label."""
        # Parse: doc_{document_id}_page_{N}_table_{M}
        m = _re.match(r"doc_(.+?)_page_(\d+)_table_(\d+)", raw_name)
        if not m:
            return raw_name
        doc_id, page, table_idx = m.group(1), int(m.group(2)), int(m.group(3))
        # Look up the filename for this document_id
        filename = (document_names or {}).get(doc_id)
        if filename:
            # Strip extension and common prefixes for a clean label
            label = _re.sub(r"\.(pdf|PDF)$", "", filename)
        else:
            # Fall back to "Document N" using the index in doc_ids
            try:
                idx = doc_ids.index(doc_id) + 1
                label = f"Document {idx}"
            except ValueError:
                label = "Document"
        return f"{label} — Page {page}, Table {table_idx}"

    # Build the mapping once per query_rag call
    _raw_tables = _table_store.list_tables(document_ids or [])
    _table_label_to_raw = {}  # human-readable → raw SQLite name
    _table_raw_to_label = {}  # raw SQLite name → human-readable
    for rt in _raw_tables:
        lbl = _make_table_label(rt)
        # Ensure uniqueness — if two tables somehow produce the same label,
        # append a suffix
        if lbl in _table_label_to_raw:
            lbl = f"{lbl} (2)"
        _table_label_to_raw[lbl] = rt
        _table_raw_to_label[rt] = lbl

    def _resolve_table(name: str) -> str:
        """Resolve a human-readable table label back to the raw SQLite name.
        Falls back to the raw name if no mapping exists (backward compat)."""
        return _table_label_to_raw.get(name, name)

    @tool
    def list_tables() -> str:
        """List all structured data tables extracted from the uploaded document(s).
        Call this first when the user asks about tabular data, inventory items,
        quantities, rates, costs, or comparisons between documents. Returns
        table names that can be used with describe_table, query_table,
        compare_tables, and aggregate_column."""
        if not _raw_tables:
            return "No tables were extracted from the uploaded document(s). This may mean the PDFs have no detectable table structure, or table extraction failed."
        return "Available tables:\n" + "\n".join(f"  - {lbl}" for lbl in _table_label_to_raw)

    @tool
    def describe_table(table_name: str) -> str:
        """Describe the structure of a table — its column names, types, row count,
        and 3 sample rows. Use this to understand what data a table contains
        before querying it. You must call list_tables first to get valid table
        names.

        Args:
            table_name: The table name returned by list_tables."""
        try:
            raw = _resolve_table(table_name)
            desc = _table_store.describe_table(raw)
            cols = ", ".join(f"{c['name']} ({c['type']})" for c in desc["columns"])
            sample = "\n".join(f"  {row}" for row in desc["sample_rows"])
            # Use the human-readable label, not the raw name
            label = _table_raw_to_label.get(raw, table_name)
            return f"Table: {label}\nColumns: {cols}\nRow count: {desc['row_count']}\nSample rows:\n{sample}"
        except Exception as e:
            return f"Error describing table '{table_name}': {e}"

    @tool
    def query_table(table_name: str, columns: str = "", where: str = "", limit: int = 50) -> str:
        """Query rows from a structured data table. Use this for exact lookups,
        filtered searches, or listing specific items from a table.

        Args:
            table_name: The table name from list_tables.
            columns: Comma-separated column names to return (e.g. "code,item,qty").
                     Leave empty to return all columns.
            where: SQL WHERE clause without the keyword (e.g. "qty > 5" or
                   "code = 'FLR-101'"). Leave empty for all rows.
            limit: Max rows to return (default 50).

        Returns:
            JSON array of matching rows, or an error message."""
        try:
            raw = _resolve_table(table_name)
            cols = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
            rows = _table_store.query_table(raw, columns=cols, where=where or None, limit=limit)
            if not rows:
                return "No rows matched the query."
            # Format as readable text instead of raw JSON for the LLM
            lines = []
            for r in rows[:limit]:
                lines.append("  " + " | ".join(f"{k}={v}" for k, v in r.items()))
            return f"{len(rows)} row(s):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error querying table '{table_name}': {e}"

    @tool
    def compare_tables(table_a: str, table_b: str, key_column: str, mode: str = "a_not_in_b") -> str:
        """Compare two tables to find rows that exist in one but not the other,
        or in both. Use this for questions like "what items does document A have
        that document B does not?" or "what items appear in both documents?".

        Args:
            table_a: First table name (from list_tables).
            table_b: Second table name (from list_tables).
            key_column: The column to compare on — must exist in both tables
                        (e.g. "code", "item", "id").
            mode: "a_not_in_b" = rows in A but not in B
                  "b_not_in_a" = rows in B but not in A
                  "in_both" = rows in both A and B

        Returns:
            The matching rows, or a message if none match."""
        try:
            raw_a = _resolve_table(table_a)
            raw_b = _resolve_table(table_b)
            rows = _table_store.compare_tables(raw_a, raw_b, key_column, mode)
            if not rows:
                return f"No rows found for mode '{mode}' on column '{key_column}'."
            lines = []
            for r in rows:
                lines.append("  " + " | ".join(f"{k}={v}" for k, v in r.items()))
            return f"{len(rows)} row(s) ({mode}):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error comparing tables: {e}"

    @tool
    def aggregate_column(table_name: str, column: str, operation: str = "sum") -> str:
        """Aggregate a numeric column in a table. Use this for questions about
        totals, averages, counts, min, or max of any numeric data.

        Args:
            table_name: The table name from list_tables.
            column: The numeric column to aggregate (e.g. "qty", "amount", "rate").
            operation: One of: sum, avg, count, min, max.

        Returns:
            The aggregation result."""
        try:
            raw = _resolve_table(table_name)
            result = _table_store.aggregate_column(raw, column, operation)
            label = _table_raw_to_label.get(raw, table_name)
            return f"{operation}({column}) on {label} = {result['result']}"
        except Exception as e:
            return f"Error aggregating column '{column}': {e}"

    @tool
    def join_tables(table_a: str, table_b: str, left_key: str, right_key: str,
                    compute: str = "", limit: int = 100) -> str:
        """Join two tables on a shared key column. Use this when a question requires
        data from two different documents — for example, quantities from an inventory
        table and rates from a price schedule, joined on a shared "code" column.

        Call list_tables and describe_table first to find tables with a shared
        key column (e.g. "code", "id", "item_number").

        Args:
            table_a: Left table name (from list_tables).
            table_b: Right table name (from list_tables).
            left_key: Column in table_a to join on (e.g. "code").
            right_key: Column in table_b to join on (e.g. "code").
            compute: Optional computed column expression using prefixed column
                     names. Columns from table_a are prefixed "a_" and columns
                     from table_b are prefixed "b_". Example: "a_qty * b_rate_inr"
                     to compute total cost. Leave empty for a plain join.
            limit: Max rows to return (default 100).

        Returns:
            Joined rows with prefixed column names (a_code, b_code, etc.)
            and optionally a "computed" column."""
        try:
            raw_a = _resolve_table(table_a)
            raw_b = _resolve_table(table_b)
            rows = _table_store.join_tables(raw_a, raw_b, left_key, right_key,
                                             compute=compute or None, limit=limit)
            if not rows:
                return f"No rows matched the join (no common '{left_key}'/'{right_key}' values)."
            lines = []
            for r in rows[:limit]:
                lines.append("  " + " | ".join(f"{k}={v}" for k, v in r.items()))
            return f"{len(rows)} joined row(s):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error joining tables: {e}"

    system_prompt = """You are Cogni, an AI assistant integrated into a document analysis system. Your top priority is being CORRECT, not sounding confident — never state something as fact unless it is grounded in a tool result or knowledge you are genuinely confident about.

[HOW TO ANSWER]
1. DOCUMENT LOOKUP: Use the `search_document` tool whenever the question might relate to the uploaded document(s). If more than one document was uploaded, `search_document` searches all of them together and labels each result with its source document — never assume you already know their contents, search first, then answer from what you find, treating it as ground truth.
1b. STRUCTURED DATA: For questions about tabular data — inventory items, quantities, rates, costs, comparisons between documents, totals, counts, or any question that requires exact data from tables — use the table query tools instead of search_document. Call `list_tables` first to see what tables exist, then `describe_table` to understand their columns, then `query_table`, `compare_tables`, or `aggregate_column` to get exact data. These tools return precise rows from the extracted tables — never guess at tabular data when a table tool can give you the exact answer.
1c. COMPARISON QUESTIONS: For questions like "what items does document A have that document B does not?" or "what appears in both documents?", always use `compare_tables` — never try to answer from search_document results, which only show text fragments and will give incomplete or wrong answers.
1d. NUMERICAL QUESTIONS: For questions about totals, sums, averages, counts, min, max, or any aggregation, use `aggregate_column` — never try to sum numbers yourself from text fragments.
1e. CROSS-DOCUMENT QUESTIONS: For questions that need data from two different documents (e.g. "what is the total cost of items in document A?" where quantities are in doc A and rates are in doc B), use `join_tables` to join the two tables on their shared key column, with a compute expression like "a_qty * b_rate_inr". Then use `aggregate_column` or `calculator` on the result if a total is needed. Always call `describe_table` on both tables first to find the shared key column and understand which columns have the data you need.
2. USE TOOLS FOR PRECISION: For arithmetic, use `calculator`. For "today"/"now"/current date or time questions, use `get_current_datetime`. For anything current, real-time, or that could have changed since your training (weather, prices, news, live facts), use `web_search`. Never guess a number or fact a tool could give you exactly — call the tool instead.
3. OUT-OF-DOCUMENT QUESTIONS: If `search_document` doesn't return a relevant answer and no other tool applies, you may answer from general knowledge ONLY if you are genuinely confident it is correct, and you must clearly tell the user this specific information was not found in their uploaded document. If you are not confident, say so plainly instead of guessing — never fabricate details, numbers, names, dates, or citations.
4. STAY RELEVANT: If a question is unrelated to the document, the available tools, and anything you can answer confidently, do not invent a speculative answer. Briefly say it's outside what you can reliably help with, and ask a clarifying question if that would help.
5. USE CONVERSATION HISTORY: Prior messages in this conversation (if any) are included below. Use them to understand follow-up questions, pronouns ("it", "that"), and context the user already established — don't treat every message as a brand-new, unrelated conversation.
6. TONE: Be conversational, polite, and helpful — like a careful research partner who is upfront about the limits of what they actually know.
7. FORMATTING: Use natural paragraphs and clear Markdown formatting (headers, bullet points, bold text) to make your response easy to read. Do NOT use Markdown tables unless the user explicitly requests one, or you are directly extracting a table from the document.
8. MATH: For any mathematical notation (equations, fractions, integrals, exponents, etc.), use LaTeX wrapped in dollar signs — `$...$` for inline math and `$$...$$` for standalone/display equations. Do NOT use `\[...\]` or `\(...\)` delimiters."""

    # The H5 finding proposed defaulting to "llama-3.3-70b-versatile" in case
    # openai/gpt-oss-120b gets decommissioned — re-verified against Groq's live
    # /models endpoint just before this merge: llama-3.3-70b-versatile is NOT
    # currently listed at all, while openai/gpt-oss-120b is (and has been used
    # successfully throughout this project's testing). Keeping the verified-
    # working default; GROQ_MODEL env var override (and the decommission-error
    # handling below) already cover the "model gets deprecated later" case.
    model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    # --- Provider selection (Phase 3: smart routing) ---
    # If provider is explicitly specified, use it. Otherwise, auto-route:
    # - Simple questions (no table keywords) → Groq (fast, 500 TPS)
    # - Complex questions (compare, total, join, etc.) → Gemini (reliable tools)
    # - If only one provider's key is available, use that one.
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    doc_ids = [d for d in (document_ids or []) if d]

    if provider is None:
        if gemini_api_key and groq_api_key:
            # Both keys available — route by question complexity
            provider = "gemini" if _needs_table_tools(question, doc_ids) else "groq"
        elif gemini_api_key:
            provider = "gemini"
        else:
            provider = "groq"

    def _make_gemini_llm():
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        return ChatGoogleGenerativeAI(
            temperature=0,
            google_api_key=gemini_api_key,
            model=gemini_model,
        )

    def _make_groq_llm():
        # H6 fix: add a timeout so a hung Groq API call doesn't block the request
        # forever. Configurable via GROQ_TIMEOUT env var (default 60s).
        groq_timeout = float(os.getenv("GROQ_TIMEOUT", "60"))
        return ChatGroq(
            temperature=0,
            groq_api_key=groq_api_key,
            model_name=model_name,
            timeout=groq_timeout,
        )

    # Build the primary LLM. If it fails during tool calling, we'll retry
    # with the fallback provider (Phase 2 failover).
    primary_provider = provider
    # Phase 3: fallback works in both directions — Gemini→Groq and Groq→Gemini
    if primary_provider == "gemini":
        fallback_provider = "groq" if groq_api_key else None
    elif primary_provider == "groq":
        fallback_provider = "gemini" if gemini_api_key else None
    else:
        fallback_provider = None

    if primary_provider == "gemini" and gemini_api_key:
        llm = _make_gemini_llm()
    else:
        llm = _make_groq_llm()
    # Merge in tools from external MCP servers (loaded at startup).
    # These are LangChain BaseTool objects from langchain-mcp-adapters.
    # If no MCP servers are configured, get_mcp_tools() returns [] and
    # this is a no-op — the local tools work exactly as before.
    mcp_tools = get_mcp_tools()
    # H10 fix: track MCP tool names so we can apply a timeout to external
    # tool calls without affecting local tools.
    _mcp_tool_names = {t.name for t in mcp_tools}

    # Selective tool binding: only bind table tools when the question actually
    # needs them. Simple questions (e.g. "what flooring is in the kitchen?")
    # get only search_document + calculator + datetime + web_search — 4 tools
    # instead of 10 — so the LLM doesn't misroute to table tools and waste
    # 4-5 rounds. This cuts token usage from ~5,000 to ~2,500 per round and
    # reduces simple-question latency from ~63s to ~3-5s.
    table_tools = [list_tables, describe_table, query_table,
                   compare_tables, aggregate_column, join_tables]
    use_table_tools = _needs_table_tools(question, doc_ids)
    if use_table_tools:
        call_tools = AVAILABLE_TOOLS + [search_document, web_search] + table_tools + mcp_tools
    else:
        call_tools = AVAILABLE_TOOLS + [search_document, web_search] + mcp_tools
    tools_by_name = {
        **_TOOLS_BY_NAME,
        "search_document": search_document,
        "web_search": web_search,
        **{t.name: t for t in mcp_tools},
    }
    if use_table_tools:
        tools_by_name.update({
            "list_tables": list_tables,
            "describe_table": describe_table,
            "query_table": query_table,
            "compare_tables": compare_tables,
            "aggregate_column": aggregate_column,
            "join_tables": join_tables,
        })
    llm_with_tools = llm.bind_tools(call_tools)

    messages = [SystemMessage(content=system_prompt)]
    for turn in (history or [])[-MAX_HISTORY_MESSAGES:]:
        role = turn.get("role")
        content = turn.get("content", "")
        # C1 fix: redact PII from historical messages before sending to Groq.
        # This is defense-in-depth — main.py now redacts before storing, but
        # old messages from before that fix may still contain raw PII.
        if role == "user" and content:
            content = redact_pii(content, TokenStore(), entities=RAIL_ENTITIES)
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "ai":
            # H9 fix: replay tool_calls if they were stored on the AI message,
            # so multi-turn tool conversations maintain full context.
            tool_calls = turn.get("tool_calls")
            if tool_calls:
                messages.append(AIMessage(content=content, tool_calls=tool_calls))
            else:
                messages.append(AIMessage(content=content))
        elif role == "tool":
            # H9 fix: replay ToolMessage with its tool_call_id so the LLM can
            # connect it to the original tool call in the conversation.
            messages.append(ToolMessage(
                content=content,
                tool_call_id=turn.get("tool_call_id", "unknown")
            ))
    messages.append(HumanMessage(content=question))

    tools_used = []
    max_tool_rounds = 5

    # Invoke, looping while the model keeps requesting tool calls (some models
    # call tools one at a time across several turns rather than all at once)
    try:
        ai_msg = await llm_with_tools.ainvoke(messages)

        rounds = 0
        while getattr(ai_msg, "tool_calls", None) and rounds < max_tool_rounds:
            # Gemini 3.x requires thought_signatures in function call parts.
            # The langchain-google-genai package stores these in
            # response_metadata — so we MUST append the original ai_msg
            # unmodified, NOT a reconstructed AIMessage.
            messages.append(ai_msg)
            for call in ai_msg.tool_calls:
                # H7 fix: use .get() instead of [] to avoid KeyError on
                # malformed tool call dicts from some models.
                call_name = call.get("name", "unknown")
                call_id = call.get("id", "unknown")
                if call_name not in tools_used:
                    tools_used.append(call_name)
                tool_fn = tools_by_name.get(call_name)
                # H11 fix: validate that call["args"] is a dict before invoking;
                # some models return malformed args (string, None) that crash
                # the tool function.
                args = call.get("args")
                if not isinstance(args, dict):
                    result = f"Error: invalid tool arguments (expected a JSON object, got {type(args).__name__})"
                elif tool_fn:
                    try:
                        # H11 fix: add timeout for MCP tool calls to prevent
                        # a hung external server from blocking the request.
                        if call_name in _mcp_tool_names:
                            result = await asyncio.wait_for(
                                tool_fn.ainvoke(args), timeout=MCP_TOOL_TIMEOUT
                            )
                        else:
                            result = await tool_fn.ainvoke(args)
                    except asyncio.TimeoutError:
                        result = f"Error: tool '{call_name}' timed out (limit: {MCP_TOOL_TIMEOUT}s)."
                    except Exception as tool_err:
                        result = f"Error executing tool '{call_name}': {tool_err}"
                else:
                    result = f"Error: unknown tool '{call_name}'"
                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
            ai_msg = await llm_with_tools.ainvoke(messages)
            rounds += 1

        # H10 fix: if we hit max_tool_rounds and the model is still requesting
        # tools, do a final invoke WITHOUT tools bound so it must produce a
        # text answer instead of returning an empty/tool-call-only message.
        # M10 fix: don't append ai_msg (which has tool_calls but no matching
        # ToolMessages) — just invoke with the existing messages.
        # Gemini doesn't support "model prefilling" (AIMessage as the last
        # message), so we add a HumanMessage prompting the model to answer.
        if getattr(ai_msg, "tool_calls", None) and rounds >= max_tool_rounds:
            final_msg = await llm.ainvoke(messages + [HumanMessage(
                content="Please provide a final answer based on the tool results above. Do not call any more tools."
            )])
            # Normalize Gemini list content to string
            fc = final_msg.content
            if isinstance(fc, list):
                parts = []
                for part in fc:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict) and "text" in part:
                        parts.append(part["text"])
                    else:
                        parts.append(str(part))
                fc = "".join(parts) if parts else ""
            answer = fc or "(The model exceeded the maximum number of tool calls and could not produce a final answer.)"
            answer = redact_pii(answer, TokenStore(), entities=RAIL_ENTITIES)
            return {"answer": answer, "tools_used": tools_used, "source_pages": sorted(source_pages_used)}

        # Output rail (privacy guardrails, Phase 2): scan the generated answer
        # for PII before it ever reaches the frontend or gets written to chat
        # history — a safety net for cases where the model reconstructs or
        # guesses a sensitive value from context even when no raw PII was in
        # its input. A fresh TokenStore per call is intentional here: this is
        # a last-line redaction pass, not the reversible per-document mapping
        # (that belongs to ingestion-time redaction, a separate piece of work).
        # M11 fix: handle None or list content from some models.
        # Gemini returns content as a list of parts — normalize to a string
        # so downstream processing (redaction, history storage, frontend) works.
        raw_content = ai_msg.content
        if raw_content is None:
            raw_content = "(No response was generated.)"
        elif isinstance(raw_content, list):
            parts = []
            for part in raw_content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
                else:
                    parts.append(str(part))
            raw_content = "".join(parts) if parts else ""

        answer = redact_pii(raw_content, TokenStore(), entities=RAIL_ENTITIES)
        return {"answer": answer, "tools_used": tools_used, "source_pages": sorted(source_pages_used), "provider": primary_provider}
    except Exception as e:
        err_str = str(e)

        # --- Phase 4: Bounded retry with backoff for rate-limit errors ---
        # If the primary provider hit a rate limit, wait briefly and retry
        # the same provider once before failing over. This handles transient
        # rate limits (e.g. "retry in 5s") without a full provider switch.
        if _is_rate_limit(err_str) and _retry_count[0] < 1:
            _retry_count[0] += 1
            wait_secs = _extract_retry_seconds(err_str)
            if wait_secs and wait_secs <= 30:
                await asyncio.sleep(wait_secs)
                try:
                    ai_msg = await llm_with_tools.ainvoke(messages)
                    rounds = 0
                    while getattr(ai_msg, "tool_calls", None) and rounds < max_tool_rounds:
                        messages.append(ai_msg)
                        for call in ai_msg.tool_calls:
                            if call["name"] not in tools_used:
                                tools_used.append(call["name"])
                            tool_fn = tools_by_name.get(call["name"])
                            args = call.get("args")
                            if not isinstance(args, dict):
                                result = f"Error: invalid tool arguments"
                            elif tool_fn:
                                try:
                                    result = await tool_fn.ainvoke(args)
                                except Exception as tool_err:
                                    result = f"Error executing tool: {tool_err}"
                            else:
                                result = f"Error: unknown tool"
                            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
                        ai_msg = await llm_with_tools.ainvoke(messages)
                        rounds += 1
                    raw_content = ai_msg.content
                    if isinstance(raw_content, list):
                        parts = []
                        for part in raw_content:
                            if isinstance(part, str):
                                parts.append(part)
                            elif isinstance(part, dict) and "text" in part:
                                parts.append(part["text"])
                            else:
                                parts.append(str(part))
                        raw_content = "".join(parts) if parts else ""
                    answer = redact_pii(raw_content, TokenStore(), entities=RAIL_ENTITIES)
                    _retry_count[0] = 0
                    return {"answer": answer, "tools_used": tools_used, "source_pages": sorted(source_pages_used), "provider": primary_provider}
                except Exception:
                    pass  # fall through to failover
            _retry_count[0] = 0

        # --- Phase 2: Failover to the backup provider ---
        # If the primary provider failed (rate limit, tool error, etc.) and a
        # fallback provider is available, retry the entire query with the
        # fallback. This is NOT a retry of the same provider — it switches to
        # a completely different LLM (e.g. Gemini → Groq or vice versa).
        if fallback_provider and _can_failover(err_str):
            try:
                if fallback_provider == "groq" and groq_api_key:
                    llm = _make_groq_llm()
                elif fallback_provider == "gemini" and gemini_api_key:
                    llm = _make_gemini_llm()
                else:
                    raise Exception("No fallback provider available")

                llm_with_tools = llm.bind_tools(call_tools)
                # Rebuild messages without the failed provider's partial state
                # (tool calls from the primary may have provider-specific format)
                retry_messages = [SystemMessage(content=system_prompt)]
                for turn in (history or [])[-MAX_HISTORY_MESSAGES:]:
                    role = turn.get("role")
                    content = turn.get("content", "")
                    if role == "user":
                        retry_messages.append(HumanMessage(content=content))
                    elif role == "ai" and not turn.get("tool_calls"):
                        retry_messages.append(AIMessage(content=content))
                retry_messages.append(HumanMessage(content=question))

                ai_msg = await llm_with_tools.ainvoke(retry_messages)
                retry_tools_used = []
                rounds = 0
                while getattr(ai_msg, "tool_calls", None) and rounds < max_tool_rounds:
                    retry_messages.append(ai_msg)
                    for call in ai_msg.tool_calls:
                        if call["name"] not in retry_tools_used:
                            retry_tools_used.append(call["name"])
                        tool_fn = tools_by_name.get(call["name"])
                        args = call.get("args")
                        if not isinstance(args, dict):
                            result = f"Error: invalid tool arguments"
                        elif tool_fn:
                            try:
                                result = await tool_fn.ainvoke(args)
                            except Exception as tool_err:
                                result = f"Error executing tool: {tool_err}"
                        else:
                            result = f"Error: unknown tool"
                        retry_messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
                    ai_msg = await llm_with_tools.ainvoke(retry_messages)
                    rounds += 1

                # H10 fix for fallback: force a final text answer if still
                # requesting tools after max rounds.
                if getattr(ai_msg, "tool_calls", None) and rounds >= max_tool_rounds:
                    final_msg = await llm.ainvoke(retry_messages + [HumanMessage(
                        content="Please provide a final answer based on the tool results above. Do not call any more tools."
                    )])
                    ai_msg = final_msg

                raw_content = ai_msg.content
                if isinstance(raw_content, list):
                    parts = []
                    for part in raw_content:
                        if isinstance(part, str):
                            parts.append(part)
                        elif isinstance(part, dict) and "text" in part:
                            parts.append(part["text"])
                        else:
                            parts.append(str(part))
                    raw_content = "".join(parts) if parts else ""

                answer = redact_pii(raw_content, TokenStore(), entities=RAIL_ENTITIES)
                return {"answer": answer, "tools_used": retry_tools_used, "source_pages": sorted(source_pages_used), "provider": fallback_provider}
            except Exception as fallback_err:
                fb_err = str(fallback_err)
                if _is_rate_limit(fb_err):
                    answer = (
                        "Both AI providers are currently rate-limited. "
                        "This is common on free tiers — please wait a minute and try again. "
                        f"(Detail: {fb_err[:200]})"
                    )
                elif "api key not valid" in fb_err.lower() or "unauthorized" in fb_err.lower():
                    answer = (
                        "Both AI providers rejected the API key. "
                        "Please check that valid API keys are configured for at least one provider."
                    )
                else:
                    answer = f"Error communicating with LLM: {fb_err}"
                return {"answer": answer, "tools_used": tools_used, "source_pages": [], "provider": fallback_provider}

        # --- Phase 4: User-friendly error when both providers fail ---
        if _is_rate_limit(err_str):
            answer = (
                "Both AI providers are currently rate-limited. "
                "This is common on free tiers — please wait a minute and try again. "
                f"(Detail: {err_str[:200]})"
            )
        elif "decommissioned" in err_str or "model_decommissioned" in err_str:
            answer = (
                "Error communicating with LLM: the model you are using appears to be decommissioned. "
                "Set the environment variable GROQ_MODEL to a supported model name or visit "
                "https://console.groq.com/docs/deprecations for recommended replacements."
            )
        else:
            answer = f"Error communicating with LLM: {err_str}"
        return {"answer": answer, "tools_used": tools_used, "source_pages": [], "provider": primary_provider}



