"""
Standalone tests for pii_guard.py (Phase 1 — not wired into the app yet).
Run directly: python test_pii_guard.py
"""
from pii_guard import redact, TokenStore


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


def main():
    all_passed = True

    # 1. Basic entity detection + redaction
    store = TokenStore()
    text = "Hi, I'm John Smith. My email is john.smith@example.com and my phone is 555-123-4567."
    redacted = redact(text, store)
    print("Input: ", text)
    print("Redacted:", redacted)
    all_passed &= check("email redacted", "john.smith@example.com" not in redacted)
    all_passed &= check("phone redacted", "555-123-4567" not in redacted)
    all_passed &= check("placeholder format present", "[EMAIL_ADDRESS_001]" in redacted)

    # 2. Stable token reuse — same value redacted twice in one document
    #    must produce the same token, not a fresh one each time.
    store2 = TokenStore()
    text2 = "Contact john.smith@example.com. Again: john.smith@example.com."
    redacted2 = redact(text2, store2)
    print("\nInput: ", text2)
    print("Redacted:", redacted2)
    token_count = redacted2.count("[EMAIL_ADDRESS_001]")
    all_passed &= check("same value reuses same token (found twice)", token_count == 2)
    all_passed &= check("no second distinct token minted", "[EMAIL_ADDRESS_002]" not in redacted2)

    # 3. Reversibility — the store can map the token back to the original value
    original = store.value_for("[EMAIL_ADDRESS_001]")
    all_passed &= check(
        f"token maps back to original value (got {original!r})",
        original == "john.smith@example.com",
    )

    # 4. Custom recognizer: Indian PAN
    store3 = TokenStore()
    pan_text = "The applicant's PAN is ABCDE1234F for verification."
    redacted3 = redact(pan_text, store3)
    print("\nInput: ", pan_text)
    print("Redacted:", redacted3)
    all_passed &= check("PAN redacted", "ABCDE1234F" not in redacted3)
    all_passed &= check("PAN entity type used", "[IN_PAN_001]" in redacted3)

    # 5. Custom recognizer: Aadhaar (grouped format)
    store4 = TokenStore()
    aadhaar_text = "Aadhaar number: 1234 5678 9012."
    redacted4 = redact(aadhaar_text, store4)
    print("\nInput: ", aadhaar_text)
    print("Redacted:", redacted4)
    all_passed &= check("Aadhaar redacted", "1234 5678 9012" not in redacted4)
    all_passed &= check("Aadhaar entity type used", "[IN_AADHAAR_001]" in redacted4)

    # 6. Non-PII text passes through unchanged
    store5 = TokenStore()
    clean_text = "What does section 4.2 of the contract say about termination?"
    redacted5 = redact(clean_text, store5)
    all_passed &= check("clean text unchanged", redacted5 == clean_text)

    print("\n" + ("ALL TESTS PASSED" if all_passed else "SOME TESTS FAILED"))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
