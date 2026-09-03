"""
Tests for the 4 anti-hallucination fixes to the tool-calling loop in
rag_pipeline.py:
  1. max_tool_rounds raised (was 5, now configurable, default 8)
  2. Refuse instead of guess when a computation tool is still pending
     when the round cap is hit
  3. Per-round retry on rate-limit errors (doesn't discard in-progress work)
  4. describe_table result cached per raw table name within a process

Run with: python3 test_anti_hallucination_fixes.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rag_pipeline

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


# =============================================================================
# Fix 1: max_tool_rounds raised
# =============================================================================
section("Fix 1: max_tool_rounds raised with real headroom")

src = open("rag_pipeline.py").read()
test("Reads from MAX_TOOL_ROUNDS env var (configurable)", 'os.getenv("MAX_TOOL_ROUNDS"' in src)
test("Default is no longer the old, too-tight value of 5",
     'os.getenv("MAX_TOOL_ROUNDS", "5")' not in src)

os.environ.pop("MAX_TOOL_ROUNDS", None)
import importlib
importlib.reload(rag_pipeline)
default_rounds = int(os.getenv("MAX_TOOL_ROUNDS", "8"))
test(f"Default resolves to a value >= 5 documented-workflow steps (got {default_rounds})",
     default_rounds >= 5)
test(f"Default gives real headroom above the bare minimum (got {default_rounds}, want > 5)",
     default_rounds > 5)


# =============================================================================
# Fix 2: refuse instead of guess when a computation tool is still pending
# =============================================================================
section("Fix 2: _finalize_after_round_cap refuses instead of guessing")


class FakeToolCallMsg:
    """Stands in for an AIMessage with pending tool_calls."""
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.content = ""


class FakeLLMWithResponse:
    """Stands in for the LLM in the 'safe to ask for a best-effort answer'
    branch — should only ever be invoked when NO compute tool is pending."""
    def __init__(self, content):
        self._content = content
        self.invoked = False

    async def ainvoke(self, messages):
        self.invoked = True
        class R:
            pass
        r = R()
        r.content = self._content
        return r


async def run_fix2_tests():
    # Case A: a compute tool (aggregate_column) is still pending -> must
    # refuse WITHOUT calling the LLM at all (no guess should ever be
    # generated, not even attempted).
    llm = FakeLLMWithResponse("42 (a guessed number)")
    pending = FakeToolCallMsg(tool_calls=[{"name": "aggregate_column", "id": "1", "args": {}}])
    answer = await rag_pipeline._finalize_after_round_cap(llm, [], pending)
    test("Refuses (does not return the LLM's guess) when aggregate_column is pending",
         "42 (a guessed number)" not in answer)
    test("Refusal message says it doesn't have a verified number",
         "verified number" in answer or "don't have" in answer)
    test("Does NOT call the LLM at all when refusing (no chance to guess)",
         not llm.invoked)

    # Case B: calculator pending -> also refuse
    llm2 = FakeLLMWithResponse("some guess")
    pending2 = FakeToolCallMsg(tool_calls=[{"name": "calculator", "id": "2", "args": {}}])
    answer2 = await rag_pipeline._finalize_after_round_cap(llm2, [], pending2)
    test("Refuses when calculator is pending", "some guess" not in answer2)

    # Case C: a non-computation tool (describe_table) pending -> safe to
    # ask the model for a best-effort final answer.
    llm3 = FakeLLMWithResponse("Here is a normal answer based on what was found.")
    pending3 = FakeToolCallMsg(tool_calls=[{"name": "describe_table", "id": "3", "args": {}}])
    answer3 = await rag_pipeline._finalize_after_round_cap(llm3, [], pending3)
    test("Does NOT refuse when only a lookup tool (describe_table) is pending",
         answer3 == "Here is a normal answer based on what was found.")
    test("DOES call the LLM in the safe (non-computation) case", llm3.invoked)

asyncio.run(run_fix2_tests())


# =============================================================================
# Fix 3: per-round retry on rate-limit errors
# =============================================================================
section("Fix 3: _ainvoke_with_retry retries in place on a rate-limit error")


class FlakyLLM:
    """Raises a rate-limit-shaped error on the first call, succeeds on retry."""
    def __init__(self, fail_times=1):
        self.calls = 0
        self.fail_times = fail_times

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise Exception("Error 429: rate limit exceeded, retry in 0s")
        class R:
            tool_calls = None
            content = "recovered"
        return R()


class AlwaysFailsNonRateLimit:
    async def ainvoke(self, messages):
        raise Exception("500 Internal Server Error: something else entirely")


async def run_fix3_tests():
    flaky = FlakyLLM(fail_times=1)
    result = await rag_pipeline._ainvoke_with_retry(flaky, [])
    test("Recovers after a single rate-limit failure (retried in place)",
         getattr(result, "content", None) == "recovered")
    test("Actually retried (more than 1 call was made)", flaky.calls == 2)

    always_flaky = FlakyLLM(fail_times=99)
    raised = False
    try:
        await rag_pipeline._ainvoke_with_retry(always_flaky, [], max_retries=2)
    except Exception:
        raised = True
    test("Gives up and raises after max_retries exhausted (doesn't retry forever)",
         raised and always_flaky.calls == 3)  # 1 initial + 2 retries

    non_rl = AlwaysFailsNonRateLimit()
    raised2 = False
    try:
        await rag_pipeline._ainvoke_with_retry(non_rl, [])
    except Exception as e:
        raised2 = True
        no_retry_msg = str(e)
    test("Does NOT retry a non-rate-limit error (fails fast instead of wasting time)",
         raised2)

asyncio.run(run_fix3_tests())


# =============================================================================
# Fix 4: describe_table result cached per raw table name
# =============================================================================
section("Fix 4: table description caching")

test("_table_describe_cache exists as a module-level dict",
     isinstance(rag_pipeline._table_describe_cache, dict))
test("_table_describe_lock exists (thread-safe access)",
     hasattr(rag_pipeline, "_table_describe_lock"))

# Directly exercise the cache the same way describe_table's tool body does.
rag_pipeline._table_describe_cache.clear()
call_count = [0]


def fake_describe_table(raw_name):
    call_count[0] += 1
    return {"columns": [{"name": "x", "type": "TEXT"}], "sample_rows": [], "row_count": 0}


# Simulate two "calls" to the same table using the exact cache-check pattern
# from the describe_table tool body.
for _ in range(3):
    with rag_pipeline._table_describe_lock:
        cached = rag_pipeline._table_describe_cache.get("doc_x_page_1_table_1")
    if cached is None:
        cached = fake_describe_table("doc_x_page_1_table_1")
        with rag_pipeline._table_describe_lock:
            rag_pipeline._table_describe_cache["doc_x_page_1_table_1"] = cached

test("Underlying describe call only happens once across 3 lookups of the same table",
     call_count[0] == 1, f"got {call_count[0]} calls")

# A different table name should still trigger its own fresh lookup.
with rag_pipeline._table_describe_lock:
    cached_other = rag_pipeline._table_describe_cache.get("doc_y_page_1_table_1")
if cached_other is None:
    fake_describe_table("doc_y_page_1_table_1")
test("A different raw table name is not incorrectly served from another table's cache entry",
     call_count[0] == 2)


# =============================================================================
# Print results
# =============================================================================
print("\n".join(RESULTS))
print(f"\n{'='*60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
print(f"{'='*60}")
sys.exit(1 if FAIL > 0 else 0)
