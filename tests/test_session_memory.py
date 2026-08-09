"""Tests for the session-memory filter — what Ted decides is worth remembering.

Run with:  ~/ted-ai/venv/bin/python tests/test_session_memory.py

The point of this filter is that MOST sessions are Charlie testing Ted, and a
memory list full of "Charlie set a two minute timer" makes callbacks worse than
having none at all. So the bar for "yes, remember this" is deliberately high,
and these tests mostly assert that ordinary sessions are rejected.

Only the cheap Python pre-filter is covered here. The model's own judgement
(the second filter, in generate_session_summary) needs a live Groq call and is
on the manual checklist instead.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.llm constructs a Groq client at import; stub it so this runs offline.
_groq = types.ModuleType("groq")
_groq.Groq = lambda **kw: None
sys.modules.setdefault("groq", _groq)

from core.llm import session_has_substance, _looks_routine   # noqa: E402

PASS = FAIL = 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else:    FAIL += 1; print(f"  ✗ {desc}")


def convo(*user_turns):
    """Build a conversation the way TedApi holds it: system prompt, then
    alternating user/assistant turns."""
    msgs = [{"role": "system", "content": "You are Ted."}]
    for t in user_turns:
        msgs.append({"role": "user", "content": t})
        msgs.append({"role": "assistant", "content": "Sure thing."})
    return msgs


print("\n— routine-utterance detection —")
for phrase in ["play", "pause", "next song", "set a timer for five minutes",
               "open Spotify", "what time is it", "cancel", "mute",
               "hey Ted", "thanks", "turn it up", "nevermind", ""]:
    check(f"routine: {phrase!r}", _looks_routine(phrase))

for phrase in ["I've been trying to figure out why the webhook fires twice",
               "what do you think I should do about the fall semester conflict",
               "play me something and then tell me what you think about the roadmap idea"]:
    check(f"not routine: {phrase[:44]!r}…", not _looks_routine(phrase))


print("\n— sessions that should NOT be remembered —")
check("empty session", not session_has_substance(convo()))
check("single question", not session_has_substance(convo("what's the weather")))
check("two short turns",
      not session_has_substance(convo("what time is it", "thanks")))
check("pure command session (the common case)",
      not session_has_substance(convo(
          "play my workout playlist", "turn it up", "skip this one",
          "pause", "resume", "next song")))
check("mic testing",
      not session_has_substance(convo("Ted", "can you hear me", "hello",
                                      "testing", "Ted are you there")))
check("many turns but almost no words",
      not session_has_substance(convo(*(["yes"] * 12))))


print("\n— sessions that SHOULD reach the model —")
check("a real problem being worked through",
      session_has_substance(convo(
          "I keep getting a double webhook on the dispatch board",
          "it fires once from the form and once from the automation",
          "would debouncing it on the receiving end be the right fix")))
check("a conversation about his life",
      session_has_substance(convo(
          "fall semester starts in three weeks and I'm worried about time",
          "I still want to keep building on you but coursework comes first",
          "what would you cut if you were me")))
check("commands mixed with one real exchange",
      session_has_substance(convo(
          "play something", "pause",
          "actually can you explain how the barge in detection decides "
          "that I'm talking and not just background noise")))

print("\n— boundaries —")
check("exactly at the turn threshold with enough words",
      session_has_substance(convo(
          "tell me about the tradeoffs between local and cloud models",
          "which one would you pick for a laptop with 48 gigs of memory",
          "and how much does the latency actually differ in practice")))
check("one turn below the threshold is rejected",
      not session_has_substance(convo(
          "tell me about the tradeoffs between local and cloud models here",
          "which one would you pick for a laptop with 48 gigs of memory now")))
check("malformed messages don't crash it",
      not session_has_substance([{"role": "system", "content": "x"},
                                 {"role": "user"},
                                 {"role": "user", "content": None},
                                 {"role": "assistant", "content": "hi"}]))

print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
