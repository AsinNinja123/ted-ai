"""Checks for Charlie-specific shorthand memory and safe expansion."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import lingo


PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


with tempfile.TemporaryDirectory() as tmp:
    lingo.DB_PATH = os.path.join(tmp, "memory.db")
    created = lingo.remember("doc", "document", note="Charlie's shorthand")
    check("a lingo term is stored separately with its note",
          created["term"] == "doc" and created["meaning"] == "document"
          and created["note"] == "Charlie's shorthand")

    expanded, matched = lingo.expand(
        "alright Ted, open a doc and write in it", record_usage=True)
    check("whole-word shorthand expands before routing",
          expanded == "alright Ted, open a document and write in it"
          and [row["term"] for row in matched] == ["doc"])
    check("usage is tracked for dashboard feedback",
          lingo.get_term(created["id"])["use_count"] == 1)
    untouched, matched = lingo.expand("call the doctor")
    check("short terms never rewrite part of a normal word",
          untouched == "call the doctor" and matched == [])

    updated = lingo.remember("DOC", "Google document")
    check("conversation learning updates case-insensitively instead of duplicating",
          updated["id"] == created["id"] and len(lingo.list_terms()) == 1)
    disabled = lingo.save_term({**updated, "enabled": False}, updated["id"])
    check("disabled lingo is retained but no longer expands",
          not disabled["enabled"] and lingo.expand("open doc")[0] == "open doc")

    check("direct definitions are parsed",
          lingo.parse_definition("doc means document") == ("doc", "document"))
    check("natural personal definitions are parsed",
          lingo.parse_definition("when I say comp org, I mean computer organization")
          == ("comp org", "computer organization"))
    check("ordinary sentences are not mistaken for definitions",
          lingo.parse_definition("what does that mean for my document?") is None)

    lingo.delete_term(created["id"])
    check("lingo deletion is durable", lingo.list_terms() == [])


print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
