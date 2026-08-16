"""Explicit memory control, and the single event emitter behind the HUD toast.

Two things are pinned here:

  1. Every path that changes what Ted knows announces it exactly once, from
     inside the write — so the toast reports a row that exists rather than an
     intention that may have failed.
  2. "remember this" / "forget that" resolve to the right referent, including
     the ambiguous case where "forget that" could equally mean "never mind".

Run with:  ~/ted-ai/venv/bin/python tests/test_memory_events.py
Uses a throwaway temp database — never touches data/memory.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import memory, intents  # noqa: E402

memory.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_mem_events.db")

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


EVENTS = []
memory.set_event_sink(EVENTS.append)


def drain():
    out = list(EVENTS)
    EVENTS.clear()
    return out


print("\n— one emitter, at the write —")
memory._get_driver()
memory.save_fact("Charlie", "LIKES", "Chick-fil-A")
evs = drain()
check("saving a fact emits exactly one event", len(evs) == 1)
check("it is an addition", evs and evs[0]["kind"] == "added")
check("it reads as a sentence", evs and evs[0]["text"] == "Charlie likes Chick-fil-A")
check("it names the table", evs and evs[0]["table"] == "facts")
check("it carries the row id, so the toast can link to it",
      evs and isinstance(evs[0]["id"], int))

# A write that does nothing must say nothing. This is the whole reason the
# emitter lives at the write instead of at the call site.
memory.save_fact("Charlie", "LIKES", "Chick-fil-A")
check("re-saving the same fact emits nothing", drain() == [])

memory.save_fact("Charlie", "", "")
check("an empty fact emits nothing", drain() == [])

print("\n— replacement reports once, not twice —")
memory.save_fact("Charlie", "LIVES_IN", "Spirit Lake")
drain()
memory.save_fact("Charlie", "LIVES_IN", "Orange City")
evs = drain()
check("changing a single-valued fact emits one event", len(evs) == 1)
check("and it is the new value, not a removal",
      evs and evs[0]["kind"] == "added" and "Orange City" in evs[0]["text"])

print("\n— removals —")
rows = memory._query(
    "SELECT rowid FROM facts WHERE subject='Charlie' AND relationship='LIKES'")
n = memory.forget_fact_by_rowid(rows[0][0])
evs = drain()
check("forget_fact_by_rowid deletes exactly one row", n == 1)
check("and announces the removal",
      len(evs) == 1 and evs[0]["kind"] == "removed")
check("naming what went", evs and "Chick-fil-A" in evs[0]["text"])
check("deleting a row that is not there emits nothing",
      memory.forget_fact_by_rowid(999999) == 0 and drain() == [])

memory.save_fact("Dana", "STUDIES", "biology")
drain()
check("forget_fact by subject removes it", memory.forget_fact("Dana") == 1)
check("and announces it", any(e["kind"] == "removed" for e in drain()))
check("forgetting an unknown subject emits nothing",
      memory.forget_fact("Nobody") == 0 and drain() == [])

print("\n— session summaries announce too —")
rid = memory.save_session_summary("Charlie debugged the HUD layout.", topics="hud")
evs = drain()
check("a new session memory emits one event", len(evs) == 1)
check("tagged as a session summary",
      evs and evs[0]["table"] == "session_summaries")
# The periodic flush rewrites the same row every few minutes; toasting each
# pass would announce one memory a dozen times.
memory.save_session_summary("Charlie debugged the HUD layout, then the panels.",
                            topics="hud", row_id=rid)
check("refining the same session memory stays quiet", drain() == [])

print("\n— a broken sink cannot break a write —")
memory.set_event_sink(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
memory.save_fact("Charlie", "DRIVES", "a Corolla")
check("the fact is still stored when the HUD raises",
      any("Corolla" in f[1] for f in memory.list_facts("Charlie")))
memory.set_event_sink(EVENTS.append)
drain()

memory.set_event_sink(None)
memory.save_fact("Charlie", "OWNS", "a kayak")
check("no sink registered is not an error", drain() == [])
memory.set_event_sink(EVENTS.append)

print("\n— phrases: adding —")
for p in ("remember this", "remember that", "add this to memory",
          "save this to memory", "note this down", "add this to your memory"):
    check(f"'{p}' is an add instruction", intents.is_memory_add_command(p))
check("'remember this: the code is 4417' is still an add",
      intents.is_memory_add_command("remember this: the code is 4417"))
check("'what do you remember' is not an add",
      not intents.is_memory_add_command("what do you remember"))

print("\n— phrases: removing —")
for p in ("dont remember that", "don't remember that", "forget that",
          "not this", "not that", "forget what I just said",
          "remove that from memory"):
    check(f"'{p}' is a drop instruction", intents.is_memory_drop_command(p))
check("'remember that' is not a drop", not intents.is_memory_drop_command("remember that"))

print("\n— the referent —")
check("a body in the same message wins",
      intents.memory_referent("remember this: the router password is on the fridge",
                              "something older")
      == "the router password is on the fridge")
check("a bare instruction falls back to the previous turn",
      intents.memory_referent("remember this", "I'm allergic to shellfish")
      == "I'm allergic to shellfish")
check("'add this to memory' is bare too",
      intents.memory_referent("add this to memory", "my locker code is 3391")
      == "my locker code is 3391")
check("a trailing filler word is not a referent",
      intents.memory_referent("remember that", "I drive a Corolla")
      == "I drive a Corolla")
check("nothing to point at yields nothing",
      intents.memory_referent("remember this", "") == "")
check("a drop instruction resolves the same way",
      intents.memory_referent("forget that", "I hate cilantro") == "I hate cilantro")

print("\n— 'forget that' is ambiguous, and stays that way —")
# With no recent memory it keeps its old meaning: cancel whatever is in flight.
check("'forget that' still cancels when no memory is pending",
      intents._is_cancel_command("forget that", memory_pending=False))
check("'forget that' defers to memory when one was just written",
      not intents._is_cancel_command("forget that", memory_pending=True))
check("'never mind' cancels either way",
      intents._is_cancel_command("never mind", memory_pending=True))
check("timer cancels are still never intercepted",
      not intents._is_cancel_command("cancel the timer", memory_pending=True))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
