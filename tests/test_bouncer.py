"""Checks for the incoming-text bouncer.

Charlie asked for a doorman: tell him Gavin texted, offer to read it aloud or
open it, leave everything else alone. The default posture is therefore silence,
and most of what follows is that default being held under pressure.

chat.db itself is never touched here. Reading it needs Full Disk Access, which
CI will not have and which this machine did not have while the code was
written — so the parts that can only be verified against a real database are
noted as such rather than faked into a false green.
"""

import os
import sys
import tempfile
from datetime import datetime

_SCRATCH = os.path.join(tempfile.mkdtemp(), "bouncer_test.db")
os.environ["TED_DB"] = _SCRATCH

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bouncer, messages, routing

bouncer.DB_PATH = _SCRATCH


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("— silence is the default —")
check("the bouncer starts off", not bouncer.enabled())
allowed, why = bouncer.decide("+15155550123", "Gavin Meyer")
check("nothing is announced while it is off", not allowed and "off" in why)
bouncer.set_enabled(True)
check("it can be turned on", bouncer.enabled())
allowed, why = bouncer.decide("+15155550123", "Gavin Meyer")
check("an unknown sender is still not announced",
      not allowed and "not on the list" in why)

print("\n— getting through the door —")
bouncer.allow("Gavin")
allowed, _ = bouncer.decide("+15155550123", "Gavin Meyer")
check("a first-name rule matches a full name", allowed)
check("…but not a business that merely starts with the same word",
      not bouncer.decide("+15155559999", "Gavin's Pizza Shop")[0])
check("someone else entirely is still silent",
      not bouncer.decide("+15155557777", "Random Person")[0])

bouncer.allow("+1 (515) 555-0456")
check("a phone rule matches however the number is written",
      bouncer.decide("+15155550456", "")[0])
check("…and matches the bare ten digits", bouncer.decide("5155550456", "")[0])
check("a different number does not", not bouncer.decide("+15155550457", "")[0])

bouncer.allow("mom@example.com")
check("an email rule matches", bouncer.decide("mom@example.com", "")[0])

print("\n— ignore always wins —")
bouncer.allow("*", mode="announce")
check("a wildcard lets everyone through",
      bouncer.decide("+15155551111", "Anyone")[0])
bouncer.allow("+15155551111", mode="ignore")
allowed, why = bouncer.decide("+15155551111", "Spam Number")
check("an explicit ignore beats the wildcard",
      not allowed and "ignore list" in why)
check("…and everyone else still gets through",
      bouncer.decide("+15155552222", "Someone")[0])
check("a rule can be removed", bouncer.forget("*") and
      not bouncer.decide("+15155552222", "Someone")[0])

print("\n— the door policy can be described —")
described = bouncer.describe_rules()
check("it names who gets announced", "Gavin" in described)
check("…and who does not", "quiet" in described)
bouncer.set_enabled(False)
check("…and whether it is even on", "off" in bouncer.describe_rules())

print("\n— reading the message body —")
# Modern macOS leaves message.text NULL and puts the words in attributedBody,
# a typedstream archive. A reader that only looks at `text` shows blank texts,
# which is the single most likely way this feature quietly fails.
def blob(s):
    body = s.encode()
    head = b"\x04\x0bstreamtyped...NSString\x01\x94\x84\x01+"
    if len(body) < 0x80:
        return head + bytes([len(body)]) + body
    return head + b"\x81" + len(body).to_bytes(2, "little") + body


check("the plain text column is used when present",
      messages.decode_body("hello", blob("ignored")) == "hello")
check("a short attributedBody is decoded",
      messages.decode_body(None, blob("yo are we still on?")) == "yo are we still on?")
check("a body over 127 bytes is decoded",
      messages.decode_body(None, blob("x" * 200)) == "x" * 200)
check("emoji survive", messages.decode_body(None, blob("on my way 🚗")) == "on my way 🚗")
# A wrong guess must yield nothing rather than mojibake presented as Gavin's
# words, which is why the parser refuses unknown length encodings.
check("an unparseable blob yields nothing, not garbage",
      messages.decode_body(None, b"\x00\x01\x02nonsense") == "")
check("no body at all is empty", messages.decode_body(None, None) == "")

print("\n— Apple's timestamps —")
delta = (datetime(2026, 8, 16) - datetime(2001, 1, 1)).total_seconds()
check("nanoseconds since 2001 (modern macOS)",
      messages._apple_time(delta * 1e9).year == 2026)
check("seconds since 2001 (old installs)",
      messages._apple_time(delta).year == 2026)
check("zero is not a date", messages._apple_time(0) is None)
check("nonsense is not a date", messages._apple_time("abc") is None)

print("\n— the permission wall —")
ok, reason = messages.available()
if ok:
    check("Full Disk Access is granted, so the database opens", True)
else:
    # The failure Charlie will actually hit. It must name the fix, not just say
    # "no messages" — those are different answers and only one is true.
    check("a blocked read explains how to fix it",
          "Full Disk Access" in reason and "System Settings" in reason)
    check("…and does not pretend there were no messages",
          "no new" not in reason.lower())

print("\n— what Ted says —")
msg = {"handle": "+15155550123", "body": "hey", "group": "", "has_attachment": False}
check("an announcement names the sender",
      "Gavin" in messages.describe(msg, "Gavin Meyer"))
check("a group chat is identified as one",
      "in Robotics" in messages.describe(dict(msg, group="Robotics"), "Gavin"))
check("an attachment with no text is described honestly",
      "attachment" in messages.describe(
          dict(msg, body="", has_attachment=True), "Gavin"))
check("a preview is trimmed",
      len(messages.preview({"body": "x" * 400}, 50)) <= 50)
check("an empty body has no preview", messages.preview({"body": "  "}) == "")


def names_for(text):
    return {routing.tool_name(s) for s in routing.select_tool_schemas(text)}


print("\n— routing —")
for phrase in ("tell me when gavin texts me", "let me know if mom messages",
               "ignore texts from that number", "who are you watching for",
               "turn on the bouncer"):
    check(f"{phrase!r} reaches the bouncer",
          any(n.startswith("bouncer_") for n in names_for(phrase)))
for phrase in ("read it", "open it", "what does it say"):
    check(f"{phrase!r} answers the announcement",
          "text_respond" in names_for(phrase))
# Sending a text is a completely different intent and must not be captured.
for phrase in ("text gavin that i'm running late", "send mom a message"):
    check(f"{phrase!r} still sends a message",
          "send_message" in names_for(phrase)
          and not any(n.startswith("bouncer_") for n in names_for(phrase)))

print("\n— the HUD prompt —")
HUD = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "ted_hud.html"), encoding="utf-8").read()
check("there is an incoming-text card", "incomingText:function" in HUD)
check("it offers both things Charlie asked for",
      "Read it aloud" in HUD and "Open in Messages" in HUD)
check("…and a way to do nothing", "Leave it" in HUD)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
