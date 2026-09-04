"""Release gates distilled from Charlie's real daily-use failures."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import routing, understanding  # noqa: E402

PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


print("— ordinary conversation does not become durable Mac work —")
for request in (
    "generate me code in python that converts a number into binary",
    "write me a paragraph about Beethoven",
    "who is Joshua Wiersema",
    "what classes do I have tomorrow?",
):
    check(request, not routing.likely_action_request(request))

print("\n— real Mac work remains action-shaped —")
for request in (
    "open Outlook",
    "close Chrome",
    "click New mail",
    "type hello into Outlook",
    "draft an email in Outlook with the cursor",
):
    check(request, routing.likely_action_request(request))

print("\n— explicit research cannot silently skip live search —")
for request in (
    "look up Mark Haselhoff",
    "look him up",
    "search for Northwestern College golf",
    "search up Charlie Rowenhorst",
    "google Joshua Wiersema",
    "find out about Ray Greller",
):
    check(request, routing.explicit_web_lookup(request))

active = {"id": 43, "goal": "open Outlook and draft an email", "status": "active"}
lookup = understanding.resolve("look him up", active_task=active)
check("a conversational pronoun lookup is information", lookup.mode == "information")
check("a pronoun lookup does not inherit an old Mac task", lookup.continues_task_id is None)
check("a pronoun lookup leaves resolution to recent chat", not lookup.references)

action = understanding.resolve("do it again", active_task=active)
check("an actual action continuation still uses the task", action.continues_task_id == 43)
check("an actual action continuation remains action mode", action.mode == "action")

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
