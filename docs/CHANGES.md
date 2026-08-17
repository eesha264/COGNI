# Cogni — Changes

This document summarizes the changes made to Cogni in this working session: adding
tool calling, fixing hallucination and conversation-memory issues, adding
handwriting/table analysis, several chat UI improvements, and fixing raw-HTML/table
overflow and math-rendering bugs found via live testing.

## Backend

### 1. Tool calling framework
`backend/rag_pipeline.py` now binds real tools to the Groq model (`llm.bind_tools(...)`)
instead of only answering from a static prompt. The model decides when to call a tool,
and a loop in `query_rag()` executes tool calls and feeds results back to the model
(capped at 5 rounds, since some models call tools one at a time across several turns
rather than all at once).

Tools added:
- **`calculator`** — evaluates arithmetic via a restricted AST parser (only numbers and
  `+ - * / % **`, no arbitrary code execution).
- **`get_current_datetime`** — returns the server's current date/time, for "today"/"now"
  questions.
- **`web_search`** — uses the Tavily API for current events, prices, weather, or any
  fact that could have changed since the model's training.
- **`search_document`** — searches the uploaded document's vector store on demand. This
  replaced an earlier design where document context was *always* injected into every
  prompt regardless of relevance (see "Source attribution" below for why).

### 2. Anti-hallucination system prompt
The original prompt explicitly told the model to fall back to general knowledge
whenever the document didn't have an answer, with no guardrails — the direct cause of
hallucination risk. The rewritten prompt in `query_rag()`:
- Answers from the document first (via `search_document`) when relevant.
- Uses a tool for anything requiring precision (math, date, current facts) instead of
  guessing.
- Answers from general knowledge only when genuinely confident, and clearly labels it
  as not from the document.
- Refuses to fabricate details and declines speculative answers on off-topic questions.

### 3. Conversation memory
**Root cause found:** `query_rag()` never received prior conversation turns — every
message was treated as a brand-new, isolated conversation, which is why follow-up
questions appeared to "forget" earlier context.

**Fix:** `backend/main.py`'s `/chat` endpoint now loads the chat's prior history via
`database.get_chat_history()` before calling `query_rag()`, which includes the last
~20 messages (~10 turns) in the message list sent to the model.

### 4. Source attribution (accurate, not inferred)
Originally, `source_pages` was computed via a similarity search that always ran
regardless of question relevance, so it could show a misleading "Source: Page X" even
on questions with nothing to do with the document (verified: a purely conversational
statement scored *higher* in similarity than a real document question, on the same
document — a fixed relevance threshold would not have reliably separated the two).

**Fix:** document retrieval became its own tool (`search_document`), called only when
the model decides it's relevant. `source_pages` is now populated only from pages the
model actually searched — accurate by construction instead of inferred from a score.

### 5. Handwriting and table extraction
`backend/rag_pipeline.py`'s `process_pdf()` previously only extracted embedded digital
text via PyMuPDF — scanned or handwritten pages returned nothing, and tables were
flattened into plain text with no structure.

- **Tables**: PyMuPDF's built-in `page.find_tables()` detects tables on typed pages and
  converts them to Markdown, free and local (no AI call).
- **Handwriting / scanned pages**: if a page has fewer than 20 non-whitespace
  characters of digital text, it's rendered as an image and sent to Groq's vision model
  (`qwen/qwen3.6-27b` — the only multimodal model currently on Groq, confirmed via
  their live models API since these preview models rotate) with a prompt to transcribe
  the page, including handwriting, and format any tables as Markdown.
- Chunking was restructured to run **per-page** (rather than joining the whole document
  into one blob first) so every chunk carries a `page` metadata field — this is what
  makes accurate source-page attribution possible at all.

Verified with a synthetic image-only "scanned page" test PDF (a rendered PNG with a
title, a handwritten-style note, and a table, embedded with zero text layer) — the
vision fallback correctly transcribed all of it, including exact table values.

## Frontend

### 6. Markdown and code rendering
Replaced a hand-rolled Markdown parser (`MainChat.jsx`) that only supported headers,
bold, bullet lists, and tables with `react-markdown` + `remark-gfm` (adds numbered
lists, strikethrough, autolinks) + `rehype-highlight` (syntax-highlighted fenced code
blocks) — both previously unsupported and would render as literal text with stray
backticks.

### 7. Source attribution UI
AI responses show a small "📄 Source: Page X" footer when the document was actually
consulted (see backend section above). Tool-use badges (🧮/📅/🔍) were built and then
removed at the user's request — visual clutter was not worth it.

### 8. Copy button, timestamps
Hover-reveal copy button on AI responses (with a checkmark confirmation), and a small
timestamp under each message bubble.

### 9. Auto-scroll + scroll-to-bottom
The chat auto-scrolls to the latest message unless the user has scrolled up to read
earlier content, in which case a "↓ Scroll to bottom" pill appears. (A streaming/
typewriter reveal effect for AI responses was also built and tested, then removed at
the user's request — the backend still returns the full response in one shot; this
would have been a purely client-side reveal animation.)

### 10. Avatars
Message avatars (✨ for AI, 🙂 for user) were added and then removed at the user's
request.

### 11. Literal `<br>` text and table overflow
Two bugs found via live testing on a real saved conversation:
- The model sometimes writes literal HTML `<br>` tags inside table cells (a common way
  to force a line break within a single markdown table cell, since markdown table rows
  can't contain real newlines). `react-markdown` doesn't interpret raw HTML by default
  (a safety default), so it printed the tag as visible text instead of a line break.
  **Fix:** added `rehype-raw` (parses the HTML) + `rehype-sanitize` (strips anything
  unsafe — scripts, event handlers, iframes — since this is model-generated content,
  not a trusted source) so `<br>` renders as a real line break without opening up
  arbitrary HTML injection.
- Tables had no overflow handling — a wide table would spill past the edge of the chat
  bubble instead of scrolling. **Fix:** tables now render inside a
  `.table-scroll-wrapper` with `overflow-x: auto`.

### 12. Math rendering (LaTeX/KaTeX)
The model would answer math questions (e.g. integrals) using LaTeX notation, but with
no math renderer in the pipeline it showed up as raw text with brackets and
backslashes. Investigating showed two stacked problems: (1) no math-rendering plugin
existed at all, and (2) plain Markdown's own backslash-escape rule was silently eating
backslashes that sat in front of punctuation (e.g. `\,` → `,`, `\[` → `[`), corrupting
the LaTeX source itself, while backslashes before letters (`\int`, `\frac`) survived —
which is why the raw output looked inconsistently mangled.

**Fix:** added `remark-math` + `rehype-katex` (+ KaTeX's CSS) to properly typeset math.
The system prompt was updated to ask the model to use `$...$` / `$$...$$` delimiters
(what `remark-math` recognizes), but the model kept using `\(...\)` / `\[...\]`
regardless of the instruction — a common LLM habit. Rather than rely on prompt
compliance, added a client-side `normalizeLatexDelimiters()` step in `MainChat.jsx`
that converts bracket-style delimiters to dollar-style before rendering, which also
resolves the backslash-eating problem as a side effect (once content is inside a
recognized math span, generic Markdown escaping no longer applies to it).

## Verification

No formal test suite exists in this project (no pytest, no jest/vitest). Verification
performed before merging:
- Frontend: `npm run lint` (oxlint, 0 issues) and `npm run build` (production build
  succeeds).
- Backend: Python syntax check on all backend files, plus a full functional smoke test
  against the running server covering:
  1. PDF upload and processing pipeline
  2. Document-grounded Q&A (`search_document` tool fires, correct `source_pages`)
  3. Calculator tool (correct answer, no false `source_pages`)
  4. Date/time tool
  5. Multi-turn conversation memory (a fact stated in one message is correctly recalled
     in a later message in the same chat)
  6. OCR/vision extraction and table structure on a synthetic scanned page
- The `<br>`/table-overflow and math-rendering fixes were additionally verified live in
  the browser against real saved conversations that reproduced each bug (not just a
  syntax/build check): confirmed `<br>` renders as an actual line break with no
  regression to the table wrapper, and confirmed a real integral question renders with
  proper mathematical typesetting (integral signs, fractions, exponents) instead of raw
  LaTeX text.

All checks passed.
