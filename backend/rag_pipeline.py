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

    if callback:
        callback("Done")

    # M4 fix: return an error if no chunks were extracted (e.g. a fully-blank
    # or corrupt PDF) so the frontend doesn't show "Done" while every query
    # returns "No document uploaded."
    if not all_chunks:
        return {"error": "No text content could be extracted from this PDF. It may be blank or corrupt."}

    return {"status": "success", "chunks_processed": len(all_chunks), "document_id": document_id}


MAX_HISTORY_MESSAGES = 20  # most recent messages (~10 turns) kept for conversational context


async def query_rag(question: str, groq_api_key: str, history=None, document_id: str = None):
    """
    Queries the vector store and returns a response from Groq using the strict system prompt,
    conditioned on the prior conversation (if any) so follow-up questions have continuity.
    Returns a dict: {"answer": str, "tools_used": [str], "source_pages": [int]}.
    C8 fix: queries the per-document collection identified by document_id.
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

    # C8 fix: use the per-document collection instead of the global vector_store.
    collection_name = f"pdf_{document_id}" if document_id else "pdf_default"
    vs = get_vector_store(collection_name)

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
        """Search the uploaded document for content relevant to a query. Use this
        whenever the user's question might be about the uploaded document — never
        assume you already know its contents, and never guess at what it says
        without searching first. Returns the most relevant excerpts, each labeled
        with its page number."""
        # H12 fix: use similarity_search_with_score and filter by a score
        # threshold so totally irrelevant chunks aren't returned (which would
        # fuel hallucination). Chroma returns L2 distance — lower is better.
        # A threshold of 1.0 is a reasonable default for bge-small-en-v1.5.
        try:
            results_with_scores = vs.similarity_search_with_score(query, k=4)
        except Exception:
            results_with_scores = []

        if not results_with_scores:
            return "No document has been uploaded yet, or the document index is empty."

        # Filter out chunks with a high distance score (low relevance)
        # M2 fix: wrap in try/except so an invalid env value doesn't crash.
        try:
            SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "1.0"))
        except (ValueError, TypeError):
            SCORE_THRESHOLD = 1.0
        filtered = [(doc, score) for doc, score in results_with_scores if score <= SCORE_THRESHOLD]

        if not filtered:
            return "No sufficiently relevant content was found in the uploaded document for this query."

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
            parts.append(f"{page_label}\n{content}")
        return "\n\n".join(parts)

    system_prompt = """You are Cogni, an AI assistant integrated into a document analysis system. Your top priority is being CORRECT, not sounding confident — never state something as fact unless it is grounded in a tool result or knowledge you are genuinely confident about.

[HOW TO ANSWER]
1. DOCUMENT LOOKUP: Use the `search_document` tool whenever the question might relate to the uploaded document. Never assume you already know its contents — search first, then answer from what you find, treating it as ground truth.
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

    # Initialize Groq LLM
    # H6 fix: add a timeout so a hung Groq API call doesn't block the request
    # forever. Configurable via GROQ_TIMEOUT env var (default 60s).
    groq_timeout = float(os.getenv("GROQ_TIMEOUT", "60"))
    llm = ChatGroq(
        temperature=0,
        groq_api_key=groq_api_key,
        model_name=model_name,
        timeout=groq_timeout,
    )
    # Merge in tools from external MCP servers (loaded at startup).
    # These are LangChain BaseTool objects from langchain-mcp-adapters.
    # If no MCP servers are configured, get_mcp_tools() returns [] and
    # this is a no-op — the local tools work exactly as before.
    mcp_tools = get_mcp_tools()
    # H10 fix: track MCP tool names so we can apply a timeout to external
    # tool calls without affecting local tools.
    _mcp_tool_names = {t.name for t in mcp_tools}
    call_tools = AVAILABLE_TOOLS + [search_document, web_search] + mcp_tools
    tools_by_name = {
        **_TOOLS_BY_NAME,
        "search_document": search_document,
        "web_search": web_search,
        **{t.name: t for t in mcp_tools},
    }
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

    # Invoke, looping while the model keeps requesting tool calls (some models
    # call tools one at a time across several turns rather than all at once)
    try:
        ai_msg = await llm_with_tools.ainvoke(messages)

        max_tool_rounds = 5
        rounds = 0
        while getattr(ai_msg, "tool_calls", None) and rounds < max_tool_rounds:
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
        if getattr(ai_msg, "tool_calls", None) and rounds >= max_tool_rounds:
            final_msg = await llm.ainvoke(messages)
            answer = final_msg.content or "(The model exceeded the maximum number of tool calls and could not produce a final answer.)"
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
        raw_answer = ai_msg.content
        if raw_answer is None:
            raw_answer = "(No response was generated.)"
        elif isinstance(raw_answer, list):
            # Some models return content as a list of content blocks
            raw_answer = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in raw_answer
            )
        answer = redact_pii(raw_answer, TokenStore(), entities=RAIL_ENTITIES)
        return {"answer": answer, "tools_used": tools_used, "source_pages": sorted(source_pages_used)}
    except Exception as e:
        # Provide a clearer hint for model decommission errors
        err_str = str(e)
        if "decommissioned" in err_str or "model_decommissioned" in err_str:
            answer = (
                "Error communicating with LLM: the model you are using appears to be decommissioned. "
                "Set the environment variable GROQ_MODEL to a supported model name or visit "
                "https://console.groq.com/docs/deprecations for recommended replacements."
            )
        else:
            answer = f"Error communicating with LLM: {err_str}"
        return {"answer": answer, "tools_used": tools_used, "source_pages": []}

