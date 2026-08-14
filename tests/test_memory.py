"""Use-case tests for the SQLite memory backend.

Run with:  ~/ted-ai/venv/bin/python tests/test_memory.py
Uses a throwaway temp database — never touches data/memory.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import memory

# Point the module at a scratch database BEFORE first use
memory.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_memory.db")

PASS = FAIL = 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else:    FAIL += 1; print(f"  ✗ {desc}")


print("\n— exchanges + recall —")
check("driver opens", memory._get_driver() is not None)
memory.save_memory("what's a good road trip snack", "Beef jerky and trail mix.")
memory.save_memory("remind me to water the plants", "Reminder set.")
got = memory.get_memory("road trip")
check("keyword recall finds the road-trip exchange", "Beef jerky" in got)
# Changed Aug 14. This used to assert that an unmatched query still returned
# the most recent exchanges "so the prompt always has some grounding context".
# It cost ~300 tokens on every greeting, and what it returned was the same
# recent conversation the prompt already carries as history — the same fact
# from two places, billed twice. Retrieval now answers the question it was
# asked: relevance, not recency. Nothing relevant means nothing returned.
check("an unmatched query retrieves nothing rather than padding the prompt",
      memory.get_memory("zzzunmatched") == "")
check("…and the caller can still ask for recency explicitly",
      memory.get_memory("zzzunmatched", fallback_recent=True) != "")

print("\n— facts —")
memory.save_fact("Charlie", "LIKES", "jazz")
memory.save_fact("Charlie", "LIKES", "jazz")   # duplicate is a no-op
check("fact recall", "LIKES jazz" in memory.get_facts_about("Charlie"))

print("\n— facts: normalization, supersede, dedupe —")
memory.save_fact("Charlie", "lives in", "Spirit Lake")      # lowercase + spaces
check("relationship normalized to LIVES_IN",
      "LIVES_IN Spirit Lake" in memory.get_facts_about("Charlie"))
memory.save_fact("Charlie", "LIVES_IN", "Spirit Lake, Iowa")
_towns = [o for r, o in memory.list_facts("Charlie") if r == "LIVES_IN"]
check("single-valued fact keeps exactly one value", len(_towns) == 1)
check("newest value wins (no contradictory pair)", _towns[0] == "Spirit Lake, Iowa")
memory.save_fact("Charlie", "LIVES_IN", "Ames")
check("moving supersedes the old town",
      [o for r, o in memory.list_facts("Charlie") if r == "LIVES_IN"] == ["Ames"])

memory.save_fact("Charlie", "LIKES", "jazz music")   # more specific than "jazz"
_likes = [o for r, o in memory.list_facts("Charlie") if r == "LIKES"]
check("more specific value replaces the vaguer one",
      "jazz music" in _likes and "jazz" not in _likes)

memory.save_fact("", "LIKES", "nothing")
memory.save_fact("Charlie", "LIKES", "")
check("blank subject/object are ignored",
      not any(o == "" for _, o in memory.list_facts("Charlie")))

print("\n— facts: forget —")
memory.save_fact("Charlie", "LIKES", "fireworks")
check("forget one relationship only", memory.forget_fact("Charlie", "LIKES") == 2)
check("other facts survive", memory.list_facts("Charlie") != [])
check("forget everything about a subject", memory.forget_fact("Charlie") >= 1)
check("subject reads empty afterwards", memory.get_facts_about("Charlie") == "")

print("\n— habits —")
check("first log today is new", memory.log_habit("workout"))
check("second log today is not", not memory.log_habit("workout"))
info = memory.get_habit_streak("workout")
check("streak is 1", info and info["streak"] == 1)
check("unknown habit → None", memory.get_habit_streak("juggling") is None)
check("get_all_habits lists it", any(h["name"] == "workout" for h in memory.get_all_habits()))

print("\n— patterns + session summaries —")
for _ in range(3):
    memory.log_pattern("weather", 8)
pats = memory.get_frequent_patterns(min_count=3)
check("pattern reaches threshold", pats and pats[0]["topic"] == "weather" and pats[0]["count"] == 3)
memory.save_session_summary("Talked about weekend plans.")
check("fresh summary hidden until gap passes", memory.get_last_session_summary(4.0) == "")
check("summary visible with zero gap", "weekend" in memory.get_last_session_summary(0.0))

print("\n— session memories: upsert, recall, search —")
rid = memory.save_session_summary(
    "Charlie was debugging a webhook that fired twice on his dispatch board.",
    topics="crew dispatch, webhooks", exchanges=9)
check("insert returns a row id", isinstance(rid, int) and rid > 0)

same = memory.save_session_summary(
    "Charlie fixed the double-firing webhook by debouncing it.",
    topics="crew dispatch, webhooks", exchanges=14, row_id=rid)
check("upsert reuses the same row", same == rid)

mems = memory.get_recent_memories(limit=10)
texts = [m["text"] for m in mems]
check("upsert replaced rather than duplicated",
      sum("webhook" in t for t in texts) == 1)
check("upsert kept the newer wording", any("debouncing" in t for t in texts))
check("exchange count persisted",
      any(m["exchanges"] == 14 for m in mems))
check("recent memories carry a human date",
      all(m["when"] for m in mems))

check("search finds by topic keyword",
      any("debouncing" in m["text"] for m in memory.search_memories("dispatch")))
check("search finds by body keyword",
      any("debouncing" in m["text"] for m in memory.search_memories("webhook")))
check("search misses cleanly", memory.search_memories("xylophone") == [])
check("search with only stop-words returns nothing",
      memory.search_memories("what is the") == [])

check("prompt format is dated and joined",
      ":" in memory.format_memories_for_prompt() and
      "debouncing" in memory.format_memories_for_prompt())

check("empty text is not stored",
      memory.save_session_summary("   ") is None)
before = len(memory.get_recent_memories(limit=50))
memory.save_session_summary("")
check("blank write left the table alone",
      len(memory.get_recent_memories(limit=50)) == before)

check("stale memories fall out of the window",
      memory.get_recent_memories(limit=10, max_age_days=0) == [])

print("\n— migration is idempotent —")
memory.close()
memory._conn = None
check("reopening an existing db still works",
      memory._get_driver() is not None and
      len(memory.get_recent_memories(limit=50)) == before)

memory.close()
print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
