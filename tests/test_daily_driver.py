"""Release gates distilled from Charlie's real daily-use failures."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from core import assistant, calendar_app, intents, routing, understanding  # noqa: E402

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

print("\n— calendar clarification preserves the requested event —")
check("a misspelled calendar request keeps its title",
      intents._parse_calendar_add('can you add “playing at church” on my calender')
      == ("playing at church", ""))
check("a complete calendar request keeps its time",
      intents._parse_calendar_add("add playing at church to my calendar for Sunday at 7:30 am")
      == ("playing at church", "Sunday at 7:30 am"))
now = datetime(2026, 9, 4, 14, 0)  # Friday
parsed = datetime.fromtimestamp(assistant.parse_when("its 7:30 am on sunday", now=now))
check("a follow-up time without 'at' stays 7:30 AM",
      parsed == datetime(2026, 9, 6, 7, 30))

real_calendar_script = calendar_app._run_script
calendar_app._run_script = lambda _script: ""
check("a failed Calendar write never claims success",
      "couldn't verify" in calendar_app.add_event("Playing at church", parsed))
calendar_app._run_script = lambda _script: "event id 123"
check("a verified Calendar write names the real Sunday date",
      calendar_app.add_event("Playing at church", parsed)
      == "Added: Playing at church at 7:30 AM on Sunday, September 6.")
calendar_app._run_script = real_calendar_script

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
