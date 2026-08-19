"""
Privacy guardrail rails wired into query_rag() (see rag_pipeline.py).

Phase 3 adds the input rail: redact incidental PII in the user's query, and
block requests whose intent is to extract or unmask sensitive data.

This is a deterministic, pattern-based layer by design — the plan
deliberately skips adopting NeMo Guardrails' full Colang/LLM-based intent
classification for privacy rails specifically, since NeMo's install and
runtime overhead (2GB+, sentence-transformers, NVIDIA-specific deps) isn't
justified just for this. This heuristic layer covers the explicit
extraction/unmasking patterns in the org's policy; it is not a substitute
for a semantic intent classifier if softer paraphrases need catching later.
"""
from __future__ import annotations

import re

from pii_guard import redact as redact_pii, RAIL_ENTITIES, TokenStore

# Attempts to defeat the token scheme directly — unambiguous regardless of
# context, since there's no legitimate reason for a chat request to ask for
# a placeholder's original value back.
_TOKEN_REVEAL_PATTERNS = [
    r"\breveal\b.{0,40}\[[A-Z_]+_\d+\]",                    # "reveal original value for [ACCOUNT_001]"
    r"\[[A-Z_]+_\d+\].{0,40}\b(?:original|real|actual)\b",  # "[ACCOUNT_001] ... the real value"
    r"\bunmask\b",
    r"\bdecrypt\b",
    r"\bde-?anonymi[sz]e\b",
]
_TOKEN_REVEAL_RE = re.compile("|".join(_TOKEN_REVEAL_PATTERNS), re.IGNORECASE)

# Clearly system- or cross-customer-scoped requests — unambiguous regardless
# of phrasing, since Cogni has no legitimate reason to expose another
# party's stored data through a single-document chat session.
_SYSTEM_SCOPE_PATTERNS = [
    r"\bcustomer\s+(?:database|records?|list)\b",
    r"\ball\s+customers?\b",
    r"\bevery\s+customer\b",
    r"\ball\s+users?\b",
    r"\bevery\s+user\b",
    r"\bdatabase\b",
    r"\bthe\s+system\b",
]
_SYSTEM_SCOPE_RE = re.compile("|".join(_SYSTEM_SCOPE_PATTERNS), re.IGNORECASE)

# Bulk-PII phrasing ("all the emails", "every account number") is only a
# blocking signal when combined with system/cross-customer scope above.
# On its own it's indistinguishable from an ordinary question about the
# uploaded document — "what are all the email addresses in this contract?"
# is Cogni's core, expected use case, not an attack, and the actual values
# are already protected by the retrieval/output rails redacting them
# regardless of how the question is phrased. An earlier version of this
# module blocked bulk-PII phrasing unconditionally, which incorrectly
# rejected ordinary document questions — verified against the app's own
# core use case before landing this version.
_BULK_PII_RE = re.compile(
    r"\b(?:all|every)\s+(?:the\s+)?(?:account numbers?|emails?|e-?mail addresses?|"
    r"phone numbers?|ssns?|pan(?:\s*numbers?)?|aadhaar(?:\s*numbers?)?|card numbers?)\b",
    re.IGNORECASE,
)

_BANK_DETAILS_RE = re.compile(
    r"\bcustomers?\b.{0,40}\bbank\s+details\b|\bbank\s+details\b.{0,40}\bcustomers?\b",
    re.IGNORECASE,
)

_REFUSAL_MESSAGE = (
    "I can't help with that — this request looks like it's asking for bulk or "
    "unmasked sensitive data, which isn't permitted."
)


def input_rail(query: str) -> dict:
    """Gate + sanitize an incoming user query before it reaches the LLM.

    Returns {"allowed": bool, "query": str | None, "reason": str | None}.
    When allowed is False, `query` is None and `reason` is a user-facing
    refusal message. When allowed is True, `query` is the (possibly
    PII-redacted) text that should actually be sent to the model.
    """
    if not query:
        return {"allowed": True, "query": query, "reason": None}

    blocked = (
        _TOKEN_REVEAL_RE.search(query)
        or _SYSTEM_SCOPE_RE.search(query)
        or _BANK_DETAILS_RE.search(query)
        or (_BULK_PII_RE.search(query) and _SYSTEM_SCOPE_RE.search(query))
    )
    if blocked:
        return {"allowed": False, "query": None, "reason": _REFUSAL_MESSAGE}

    # Not blocked — still redact any incidental PII before it reaches the LLM,
    # so a benign question that happens to contain a real email/phone/ID
    # doesn't send that raw value to Groq.
    redacted_query = redact_pii(query, TokenStore(), entities=RAIL_ENTITIES)
    return {"allowed": True, "query": redacted_query, "reason": None}
