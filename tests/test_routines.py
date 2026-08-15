"""Checks for dashboard-authored phrase -> action routines."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import routines


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
    routines.DB_PATH = os.path.join(tmp, "memory.db")
    created = routines.save_routine({
        "name": "Comp Org homework",
        "phrases": ["comp org homework", "time for computer organization"],
        "steps": [
            {"tool": "open_app", "args": {"name": "Claude"}},
            {"tool": "open_app", "args": {"name": "ChatGPT"}},
            {"tool": "open_app", "args": {"name": "Chrome"}},
        ],
        "parallel": True,
        "enabled": True,
    })
    check("routine is persisted with typed JSON fields",
          created["name"] == "Comp Org homework" and len(created["steps"]) == 3)
    check("natural filler around a phrase still matches",
          routines.match_routine(
              "alight Ted, uh, let's do some comp org homework please")["id"]
          == created["id"])
    check("one-word fragments never trigger inside normal conversation",
          not routines._phrase_matches("I need to study this later", "study"))
    check("minor ASR errors in a long saying are tolerated",
          routines._phrase_matches("time for computer organizaton",
                                   "time for computer organization"))

    disabled = routines.save_routine({**created, "enabled": False}, created["id"])
    check("disabled routines do not fire",
          disabled["enabled"] is False and routines.match_routine("comp org homework") is None)
    routines.note_run(created["id"])
    check("run count and timestamp are tracked",
          routines.get_routine(created["id"])["run_count"] == 1
          and bool(routines.get_routine(created["id"])["last_run"]))

    rejected = False
    try:
        routines.save_routine({
            "name": "Unsafe", "phrases": ["send it"],
            "steps": [{"tool": "send_message", "args": {"contact": "x"}}],
        })
    except ValueError:
        rejected = True
    check("consequential tools cannot be hardwired around confirmation", rejected)

    routines.delete_routine(created["id"])
    check("routine deletion is durable", routines.list_routines() == [])

print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
