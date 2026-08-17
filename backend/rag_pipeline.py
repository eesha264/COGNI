import os
import ast
import base64
import operator
from datetime import datetime
import pymupdf  # fitz
from tavily import TavilyClient
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_groq import ChatGroq
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

# Initialize the Embeddings
# FastEmbed is lightweight and runs locally without needing an API key for embeddings
embeddings = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")

CHROMA_PERSIST_DIR = "./chroma_db"

# C8 fix: per-document collections instead of a single global vector_store.
# Previously process_pdf wiped the whole collection on every upload, so only
# the most recent PDF was queryable. Now each upload gets its own collection
# keyed by a document_id, and query_rag looks up the right collection per chat.
# A module-level cache keeps Chroma clients cheap to reuse.
_vector_store_cache: dict[str, Chroma] = {}
import threading
_vector_store_lock = threading.Lock()


def get_vector_store(collection_name: str) -> Chroma:
    """Return (and cache) a Chroma store for the given per-document collection."""
    with _vector_store_lock:
        vs = _vector_store_cache.get(collection_name)
        if vs is None:
            vs = Chroma(
                collection_name=collection_name,
                embedding_function=embeddings,
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
                embedding_function=embeddings,
                persist_directory=CHROMA_PERSIST_DIR,
            )
        try:
            vs.delete_collection()
        except Exception:
            # Collection didn't exist — nothing to delete.
            pass


import time

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
    now = datetime.now()
    return now.strftime("%A, %B %d, %Y at %I:%M %p (server local time)")


@tool
def web_search(query: str) -> str:
    """Search the live web for information not in the uploaded document and not reliably
    known from memory — current events, weather, prices, or any fact that could have
    changed since training. Use this instead of guessing whenever precision matters."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Error: web search is not configured (missing TAVILY_API_KEY)."
    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(query, max_results=3, include_answer=True)
        if response.get("answer"):
            return response["answer"]
        results = response.get("results", [])
        if not results:
            return "No web results found for that query."
        return "\n\n".join(f"{r['title']}: {r['content']}" for r in results[:3])
    except Exception as e:
        return f"Error performing web search: {e}"


AVAILABLE_TOOLS = [calculator, get_current_datetime, web_search]
_TOOLS_BY_NAME = {t.name: t for t in AVAILABLE_TOOLS}

# A page with less than this many non-whitespace characters of embedded digital
# text is treated as scanned/handwritten/image-only, and routed to vision instead.
MIN_TEXT_CHARS = 20

# C6 fix: use a real Groq vision-capable model. "qwen/qwen3.6-27b" does not
# exist on Groq and always failed at runtime. Llama 4 Scout is Groq's current
# multimodal model. Allow override via env var for future model swaps.
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


def _extract_text_via_vision(page: "pymupdf.Page", groq_api_key: str) -> str:
    """Renders a page as an image and asks a Groq vision model to transcribe it,
    including handwriting, and to format any tables as Markdown."""
    try:
        pix = page.get_pixmap(dpi=150)
        b64_image = base64.b64encode(pix.tobytes("png")).decode("utf-8")

        vision_llm = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=VISION_MODEL)
        message = HumanMessage(content=[
            {"type": "text", "text": (
                "Transcribe every piece of text visible on this page exactly as written, "
                "including any handwriting. If the page contains tables, format them as "
                "clean Markdown tables. Output only the transcribed content — no commentary."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
        ])
        response = vision_llm.invoke([message])
        return response.content or ""
    except Exception as e:
        return f"[Vision extraction failed for this page: {e}]"


def process_pdf(file_path: str, callback=None, groq_api_key: str = None, document_id: str = None):
    """
    Extracts text, tables, and image metadata from a PDF up to 400 pages.
    C8 fix: indexes into a per-document collection (named by document_id) so
    uploading a new PDF no longer wipes the previous one.
    """
    if callback:
        callback("Analyzing the pdf")
    time.sleep(1)

    # C12 fix: use try/finally so the file handle is always closed, even on
    # early returns (e.g. >400 pages) or exceptions.
    doc = pymupdf.open(file_path)
    try:
        # Check page limit
        if len(doc) > 400:
            return {"error": "PDF exceeds 400 pages limit."}

        if callback:
            callback("Analyze images")
        time.sleep(1)

        # C7 fix: prefer the caller-provided key (from the browser) over the env var
        # so vision OCR works even when the server admin hasn't set GROQ_API_KEY.
        if not groq_api_key:
            groq_api_key = os.getenv("GROQ_API_KEY")

        # Chunk each page separately (rather than joining all pages into one blob
        # first) so every chunk can be tagged with its source page number — this is
        # what lets query_rag report which pages an answer actually came from.
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len
        )

        all_chunks = []
        all_metadatas = []

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)

            # Extract embedded digital text (fast, free — works for typed PDFs)
            text = page.get_text("text")

            # If almost no digital text was found, this page is likely scanned or
            # handwritten — fall back to a Groq vision model to read it from an image
            if len(text.strip()) < MIN_TEXT_CHARS:
                if groq_api_key:
                    text = _extract_text_via_vision(page, groq_api_key) or text
                else:
                    text += "\n[Note: this page appears to be scanned/handwritten but could not be analyzed — GROQ_API_KEY not configured]"

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

            page_chunks = text_splitter.split_text(page_content)
            all_chunks.extend(page_chunks)
            all_metadatas.extend([{"page": page_num + 1}] * len(page_chunks))

        if callback:
            callback("Analyze tables")
        time.sleep(1)

        if callback:
            callback("Convert to text")
        time.sleep(1)

    finally:
        doc.close()

    # C8 fix: index into a per-document collection instead of wiping a global one.
    collection_name = f"pdf_{document_id}" if document_id else "pdf_default"
    # Replace any previous collection for this document_id (re-upload of same doc)
    delete_vector_store(collection_name)
    vs = get_vector_store(collection_name)

    # Add new chunks, tagged with their source page
    vs.add_texts(all_chunks, metadatas=all_metadatas)

    if callback:
        callback("Create embeddings")
    time.sleep(1)

    if callback:
        callback("Save to Vector DB")
    time.sleep(1)

    if callback:
        callback("Done")

    return {"status": "success", "chunks_processed": len(all_chunks), "document_id": document_id}


MAX_HISTORY_MESSAGES = 20  # most recent messages (~10 turns) kept for conversational context


def query_rag(question: str, groq_api_key: str, history=None, document_id: str = None):
    """
    Queries the vector store and returns a response from Groq using the strict system prompt,
    conditioned on the prior conversation (if any) so follow-up questions have continuity.
    Returns a dict: {"answer": str, "tools_used": [str], "source_pages": [int]}.
    C8 fix: queries the per-document collection identified by document_id.
    """
    if not groq_api_key:
        return {"answer": "Error: Groq API key is missing. Please add it to your environment.", "tools_used": [], "source_pages": []}

    # C8 fix: use the per-document collection instead of the global vector_store.
    collection_name = f"pdf_{document_id}" if document_id else "pdf_default"
    vs = get_vector_store(collection_name)

    # Pages actually consulted this turn — populated only when search_document is
    # called, so the "Source: Page X" shown to the user is precise by construction
    # rather than inferred from a similarity score (scores don't reliably separate
    # relevant from irrelevant content, especially on small documents).
    source_pages_used = []

    @tool
    def search_document(query: str) -> str:
        """Search the uploaded document for content relevant to a query. Use this
        whenever the user's question might be about the uploaded document — never
        assume you already know its contents, and never guess at what it says
        without searching first. Returns the most relevant excerpts, each labeled
        with its page number."""
        results = vs.similarity_search(query, k=4)
        if not results:
            return "No document has been uploaded yet, or the document index is empty."
        parts = []
        for doc in results:
            page = doc.metadata.get("page")
            if page is not None and page not in source_pages_used:
                source_pages_used.append(page)
            parts.append(f"[Page {page}]\n{doc.page_content}")
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
8. MATH: For any mathematical notation (equations, fractions, integrals, exponents, etc.), use LaTeX wrapped in dollar signs — `$...$` for inline math and `$$...$$` for standalone/display equations. Do NOT use `\\\\[...\\\\]` or `\\\\(...\\\\)` delimiters."""

    # Determine model to use (allow override via env var)
    model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    # Initialize Groq LLM
    llm = ChatGroq(
        temperature=0,
        groq_api_key=groq_api_key,
        model_name=model_name,
    )
    call_tools = AVAILABLE_TOOLS + [search_document]
    tools_by_name = {**_TOOLS_BY_NAME, "search_document": search_document}
    llm_with_tools = llm.bind_tools(call_tools)

    messages = [SystemMessage(content=system_prompt)]
    for turn in (history or [])[-MAX_HISTORY_MESSAGES:]:
        role = turn.get("role")
        content = turn.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "ai":
            messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=question))

    tools_used = []

    # Invoke, looping while the model keeps requesting tool calls (some models
    # call tools one at a time across several turns rather than all at once)
    try:
        ai_msg = llm_with_tools.invoke(messages)

        max_tool_rounds = 5
        rounds = 0
        while getattr(ai_msg, "tool_calls", None) and rounds < max_tool_rounds:
            messages.append(ai_msg)
            for call in ai_msg.tool_calls:
                if call["name"] not in tools_used:
                    tools_used.append(call["name"])
                tool_fn = tools_by_name.get(call["name"])
                result = tool_fn.invoke(call["args"]) if tool_fn else f"Error: unknown tool '{call['name']}'"
                messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
            ai_msg = llm_with_tools.invoke(messages)
            rounds += 1

        return {"answer": ai_msg.content, "tools_used": tools_used, "source_pages": sorted(source_pages_used)}
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
