"""Deleting a chat from the sidebar must not delete what Ted knows.

The whole point of the soft delete is the gap between two things that sound
identical to a user: taking a thread off a list, and destroying the record of a
conversation. These tests pin that gap open.

Run with:  ~/ted-ai/venv/bin/python tests/test_chat_soft_delete.py
Uses a throwaway temp database — never touches data/memory.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both modules must be pointed at the same scratch file BEFORE first use: the
# dashboard writes chat_sessions and core/memory.py reads it, and that shared
# table is exactly what is under test.
_SCRATCH = os.path.join(tempfile.mkdtemp(), "test_chats.db")
os.environ["TED_DB"] = _SCRATCH

from dashboard import db          # noqa: E402
from core import memory           # noqa: E402

db.DB_PATH = _SCRATCH
memory.DB_PATH = _SCRATCH

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def ids(rows):
    return [r["id"] for r in rows]


print("\n— schema —")
conn = db.get_conn()
cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
check("chat_sessions has a hidden column", "hidden" in cols)

# Migration path: a pre-existing table without the column must gain it rather
# than being rebuilt, because rebuilding it would take the turns with it.
conn.execute("CREATE TABLE IF NOT EXISTS legacy_probe (id INTEGER PRIMARY KEY)")
db._add_missing_columns(conn, "legacy_probe", [("hidden", "INTEGER NOT NULL DEFAULT 0")])
probe = {r[1] for r in conn.execute("PRAGMA table_info(legacy_probe)")}
check("add-column migration is additive, not destructive", "hidden" in probe)

print("\n— hiding a chat —")
keep = db.create_chat()
db.add_chat_turn(keep, "user", "what did I say about the kayak trip")
db.add_chat_turn(keep, "ted", "You said you were bringing the red kayak.")
db.set_chat_meta(keep, title="Kayak Trip Planning",
                 summary="Packing list for the kayak trip to Okoboji.")

other = db.create_chat()
db.add_chat_turn(other, "user", "unrelated")
db.set_chat_meta(other, title="Something Else", summary="Nothing to do with boats.")

check("both chats start visible", set(ids(db.list_chats())) == {keep, other})

db.set_chat_hidden(keep, True)
visible = ids(db.list_chats())
check("hidden chat leaves the sidebar list", keep not in visible)
check("the other chat is untouched", other in visible)
check("include_hidden brings it back", keep in ids(db.list_chats(include_hidden=True)))

row = [r for r in db.list_chats(include_hidden=True) if r["id"] == keep][0]
check("hidden flag is reported to the dashboard", row["hidden"] == 1)

print("\n— the turns survive —")
still = db.get_chat(keep)
check("get_chat still returns the hidden thread", still["id"] == keep)
check("every turn is still there", len(still["turns"]) == 2)
check("the words are unchanged", "red kayak" in still["turns"][1]["content"])

print("\n— Ted still remembers it —")
# This is the assertion the feature exists for. core/memory.search_memories
# reads chat_sessions unfiltered on purpose; if someone ever "helpfully" adds
# a hidden = 0 filter there, the soft delete silently becomes a real one and
# this check is what says so.
hits = memory.search_memories("kayak")
check("searching memory finds the hidden conversation",
      any("kayak" in (h["text"] or "").lower() for h in hits))
check("it is found by its summary, not just its title",
      any("Okoboji" in (h["text"] or "") for h in hits))

print("\n— restoring —")
db.set_chat_hidden(keep, False)
check("restore puts it back in the sidebar", keep in ids(db.list_chats()))

print("\n— hard delete —")
db.set_chat_hidden(keep, True)
turns_before = len(db.get_chat(keep)["turns"])
check("thread has turns before the purge", turns_before == 2)
db.delete_chat(keep)
gone = False
try:
    db.get_chat(keep)
except KeyError:
    gone = True
check("hard delete removes the thread", gone)
left = conn.execute("SELECT COUNT(*) FROM chat_turns WHERE session_id = ?",
                    [keep]).fetchone()[0]
check("hard delete removes its turns too", left == 0)
check("it no longer turns up in memory search",
      not any("Okoboji" in (h["text"] or "") for h in memory.search_memories("kayak")))
check("the unrelated chat is still there", other in ids(db.list_chats()))

print("\n— missing rows —")
for fn, label in ((lambda: db.set_chat_hidden(99999, True), "set_chat_hidden"),
                  (lambda: db.delete_chat(99999), "delete_chat")):
    raised = False
    try:
        fn()
    except KeyError:
        raised = True
    check(f"{label} raises KeyError for an unknown chat", raised)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
