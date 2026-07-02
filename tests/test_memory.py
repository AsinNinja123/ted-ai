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
memory.save_memory("what fireworks sell best", "Artillery shells and the big cakes.")
memory.save_memory("remind me to restock sparklers", "Reminder set.")
got = memory.get_memory("fireworks")
check("keyword recall finds the fireworks exchange", "Artillery shells" in got)
check("recency fallback when no keyword hits", memory.get_memory("zzzunmatched") != "")

print("\n— facts —")
memory.save_fact("Charlie", "OWNS", "a fireworks store")
memory.save_fact("Charlie", "OWNS", "a fireworks store")   # duplicate is a no-op
check("fact recall", "OWNS a fireworks store" in memory.get_facts_about("Charlie"))

print("\n— goals —")
memory.save_goal("learn python")
memory.save_goal("Learn Python!")     # fuzzy duplicate — must not create a second goal
check("fuzzy dedup keeps one goal", len(memory.get_goals()) == 1)
check("complete_goal matches partial name", memory.complete_goal("python"))
check("completed goal leaves active list", memory.get_goals() == [])
check("completing again returns False", not memory.complete_goal("python"))

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
memory.save_session_summary("Talked about fireworks inventory.")
check("fresh summary hidden until gap passes", memory.get_last_session_summary(4.0) == "")
check("summary visible with zero gap", "fireworks" in memory.get_last_session_summary(0.0))

print("\n— store namespace stays separate —")
memory.save_store_memory("stock question", "store answer")
check("store recall", "store answer" in memory.get_store_memory("stock"))
check("ted recall unpolluted", "store answer" not in memory.get_memory("stock question"))

memory.close()
print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
