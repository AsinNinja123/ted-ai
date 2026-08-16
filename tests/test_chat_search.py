"""Finding a message in eleven hundred turns.

search_memories answers "which conversation was that" from titles and
summaries. This answers "what did we actually say", which is the question the
sidebar could never answer: titles are auto-generated summaries and rarely
contain the word you remember.

Run with:  ~/ted-ai/venv/bin/python tests/test_chat_search.py
Uses a throwaway temp database — never touches data/memory.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRATCH = os.path.join(tempfile.mkdtemp(), "test_search.db")
os.environ["TED_DB"] = _SCRATCH

from dashboard import db          # noqa: E402
from core import memory           # noqa: E402
from core import tool_handlers as th  # noqa: E402

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


conn = db.get_conn()
memory._get_driver()

print("\n— the index —")
check("FTS5 is available in this build", memory._has_fts is True)
have = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("chat_turns_fts exists", "chat_turns_fts" in have)

print("\n— finding a message —")
kayak = db.create_chat()
db.set_chat_meta(kayak, title="Weekend Plans", summary="Plans for the weekend.")
db.add_chat_turn(kayak, "user", "the red kayak is still strapped to the Corolla")
db.add_chat_turn(kayak, "ted", "Noted. I'll remind you to take it down.")

other = db.create_chat()
db.set_chat_meta(other, title="Homework", summary="Assignment questions.")
db.add_chat_turn(other, "user", "when is the discrete math assignment due")

hits = memory.search_chat_turns("kayak")
check("a word from the message body is found", len(hits) == 1)
check("and the message itself comes back", "red kayak" in hits[0]["content"])
check("with the thread it belongs to", hits[0]["title"] == "Weekend Plans")
check("and the session id, so the UI can open it", hits[0]["session_id"] == kayak)
check("and who said it", hits[0]["role"] == "user")

# The title says "Weekend Plans" and contains none of these words. That is the
# whole point: searching titles could never have found this.
check("a word in no title is still findable",
      len(memory.search_chat_turns("Corolla")) == 1)
check("an unrelated thread is not returned",
      all(h["session_id"] == kayak for h in memory.search_chat_turns("kayak")))
check("a word from the other thread finds only that one",
      [h["session_id"] for h in memory.search_chat_turns("discrete")] == [other])

print("\n— nothing, and nonsense —")
check("no match returns nothing", memory.search_chat_turns("xyzzyqqq") == [])
check("an empty query returns nothing", memory.search_chat_turns("") == [])
check("stop-words alone return nothing", memory.search_chat_turns("the and a") == [])
# FTS5 treats these as operators; unquoted they raise instead of searching.
for bad in ('kayak OR', 'kayak*', 'kayak "', 'NEAR(kayak)', 'kayak AND AND'):
    try:
        memory.search_chat_turns(bad)
        ok = True
    except Exception as e:
        ok = False
        print(f"    raised on {bad!r}: {e}")
    check(f"FTS syntax in {bad!r} does not raise", ok)

print("\n— newest first —")
recent = db.create_chat()
db.set_chat_meta(recent, title="Later", summary="")
db.add_chat_turn(recent, "user", "one more kayak thought")
hits = memory.search_chat_turns("kayak")
check("the newest matching message leads", hits[0]["session_id"] == recent)
check("older ones still follow", len(hits) == 2)

print("\n— deleted threads are still searchable —")
# A chat hidden from the sidebar is still something Ted knows. Excluding it
# here would turn the soft delete into a real one through the back door.
db.set_chat_hidden(kayak, True)
hits = memory.search_chat_turns("Corolla")
check("a hidden thread's messages are still found", len(hits) == 1)
check("and are flagged as hidden so the UI can say so", hits[0]["hidden"] is True)
check("a visible thread is not flagged",
      memory.search_chat_turns("discrete")[0]["hidden"] is False)

print("\n— the index keeps up —")
db.add_chat_turn(recent, "user", "a brand new unrepeated word: zorblat")
check("a turn written after the index exists is findable",
      len(memory.search_chat_turns("zorblat")) == 1)
conn.execute("DELETE FROM chat_turns WHERE content LIKE '%zorblat%'")
conn.commit()
check("and disappears when the turn is deleted",
      memory.search_chat_turns("zorblat") == [])

print("\n— backfill —")
# Turns written before the index existed must be indexed, or search is useless
# on exactly the history worth searching.
conn.execute("DROP TABLE IF EXISTS chat_turns_fts")
conn.commit()
db.ensure_schema(conn)
indexed = conn.execute("SELECT COUNT(*) FROM chat_turns_fts").fetchone()[0]
actual = conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
check("rebuilding the index covers every existing turn", indexed == actual)
check("and pre-existing turns are findable again",
      len(memory.search_chat_turns("kayak")) == 2)

print("\n— what Ted says —")
said = th.tool_search_chats("kayak")
check("the tool names the thread", "Weekend Plans" in said)
check("and quotes the message", "red kayak" in said)
check("and attributes it", "You said" in said)
check("an empty result is stated plainly",
      "Nothing in our past chats" in th.tool_search_chats("xyzzyqqq"))

from core.tools import TOOL_SCHEMAS  # noqa: E402
check("search_chats is a registered schema",
      "search_chats" in [s["function"]["name"] for s in TOOL_SCHEMAS])

from core import routing  # noqa: E402
for phrase in ("what did I say about the kayak",
               "find the chat where we talked about kayaks",
               "when did I mention the kayak"):
    picked = [routing.tool_name(s) for s in routing.select_tool_schemas(phrase)]
    check(f"'{phrase[:34]}…' offers search_chats", "search_chats" in picked)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
