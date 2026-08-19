"""
Standalone Presidio-based PII detection and redaction.

Phase 1 of the privacy guardrails plan: analysis + redaction primitives,
unit-tested in isolation via test_pii_guard.py. Wired into rag_pipeline.py's
query_rag() as the input rail (via guardrails.py), output rail, and
retrieval rail (Phases 2-4).

Presidio's own reversible operator (`encrypt`/`decrypt`) embeds ciphertext
in-place and only that operator is reversible via DeanonymizeEngine. That's
not what we want: stable, readable placeholders like "[EMAIL_001]" with the
real value kept in a separate store. TokenStore below implements that
instead of using Presidio's built-in operator.
"""
from __future__ import annotations

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

_NLP_CONFIGURATION = {
    "nlp_engine_name": "spacy",
    # en_core_web_sm over en_core_web_lg: 15MB vs 432MB, and the NER F-score
    # gap is under 1 point (85.86 vs 86.62). Verified empirically against
    # this module's own test suite before switching — no assertion changed
    # behavior. The live rails (RAIL_ENTITIES below) don't even use PERSON,
    # the one entity type NER quality affects; only the broader
    # DEFAULT_ENTITIES set (reserved for future ingestion-time redaction)
    # does, and the same negligible gap applies there.
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}

# India-specific identifiers Presidio doesn't ship recognizers for out of
# the box in this configuration — PAN and Aadhaar formats, per the org's
# stated PII scope.
#  Scored above 0.85 (spaCy's typical PERSON/DATE_TIME confidence) so that
# when a match's span exactly coincides with a generic NER guess — e.g.
# "ABCDE1234F" read as both a PAN and, by NER, a PERSON name — the specific,
# regex-certain recognizer deterministically wins the overlap-resolution
# tiebreak in redact() below, rather than depending on incidental result
# ordering from the analyzer.
_pan_recognizer = PatternRecognizer(
    supported_entity="IN_PAN",
    patterns=[Pattern(name="pan", regex=r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", score=0.9)],
)

_aadhaar_recognizer = PatternRecognizer(
    supported_entity="IN_AADHAAR",
    patterns=[
        Pattern(name="aadhaar_grouped", regex=r"\b\d{4}[\s-]\d{4}[\s-]\d{4}\b", score=0.9),
        # Bare 12-digit run — much more ambiguous (could be a phone/account
        # number too), so a lower confidence score than the grouped format.
        Pattern(name="aadhaar_plain", regex=r"\b\d{12}\b", score=0.3),
    ],
)

_analyzer = AnalyzerEngine(
    nlp_engine=NlpEngineProvider(nlp_configuration=_NLP_CONFIGURATION).create_engine()
)
_analyzer.registry.add_recognizer(_pan_recognizer)
_analyzer.registry.add_recognizer(_aadhaar_recognizer)

DEFAULT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "LOCATION",
    "CREDIT_CARD",
    "US_SSN",
    "DATE_TIME",
    "IBAN_CODE",
    "IN_PAN",
    "IN_AADHAAR",
]

# Entities safe to redact in live query/answer rails without a high
# false-positive rate on ordinary factual questions — deliberately excludes
# PERSON, LOCATION, and DATE_TIME, which routinely appear in benign
# questions ("What is the capital of France?", "Who was Marie Curie?",
# "What happened in 1990?") and would otherwise be incorrectly redacted,
# breaking the question itself. DEFAULT_ENTITIES above stays the broader
# set intended for ingestion-time document redaction, where a name or
# place genuinely can be the private data being protected and the
# false-positive tradeoff is different.
RAIL_ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "US_SSN",
    "IBAN_CODE",
    "IN_PAN",
    "IN_AADHAAR",
]


class TokenStore:
    """Maps stable placeholder tokens ("[EMAIL_001]") to original values.

    Phase 1: in-memory only, for standalone testing. A later phase swaps
    this for an encrypted DB-backed store — everything above this class
    is agnostic to where the mapping actually lives.
    """

    def __init__(self):
        self._value_to_token: dict[tuple[str, str], str] = {}
        self._token_to_value: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def token_for(self, entity_type: str, original_value: str) -> str:
        key = (entity_type, original_value)
        if key in self._value_to_token:
            return self._value_to_token[key]
        self._counters[entity_type] = self._counters.get(entity_type, 0) + 1
        token = f"[{entity_type}_{self._counters[entity_type]:03d}]"
        self._value_to_token[key] = token
        self._token_to_value[token] = original_value
        return token

    def value_for(self, token: str) -> str | None:
        return self._token_to_value.get(token)


def analyze(text: str, entities: list[str] | None = None):
    if not text:
        return []
    return _analyzer.analyze(text=text, language="en", entities=entities or DEFAULT_ENTITIES)


def _resolve_overlaps(results):
    """Keep only non-overlapping matches: highest score first, then longest
    span, then earliest start. Different recognizers can return matches on
    the same or overlapping spans (e.g. a custom regex recognizer and
    spaCy's generic NER both firing on the same substring) — replacing all
    of them independently corrupts the text, since a later replacement's
    stored offsets no longer line up once an earlier one has changed the
    string's length. Only ever keep one match per character."""
    ordered = sorted(results, key=lambda r: (-r.score, -(r.end - r.start), r.start))
    accepted = []
    for r in ordered:
        if any(r.start < a.end and a.start < r.end for a in accepted):
            continue
        accepted.append(r)
    return accepted


def redact(text: str, store: TokenStore, entities: list[str] | None = None) -> str:
    """Replace detected PII with stable placeholder tokens, recorded in `store`."""
    if not text:
        return text
    results = _resolve_overlaps(analyze(text, entities=entities))
    # Right-to-left so earlier match offsets aren't shifted by replacements
    # made later in the same pass.
    results = sorted(results, key=lambda r: r.start, reverse=True)
    redacted = text
    for r in results:
        original_value = text[r.start : r.end]
        token = store.token_for(r.entity_type, original_value)
        redacted = redacted[: r.start] + token + redacted[r.end :]
    return redacted
