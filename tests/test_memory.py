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
check("recency fallback when no keyword hits", memory.get_memory("zzzunmatched") != "")

print("\n— facts —")
memory.save_fact("Charlie", "LIKES", "jazz")
memory.save_fact("Charlie", "LIKES", "jazz")   # duplicate is a no-op
check("fact recall", "LIKES jazz" in memory.get_facts_about("Charlie"))

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

memory.close()
print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
