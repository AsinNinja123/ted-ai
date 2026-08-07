"""tests/test_capture_gates.py — the pure text-side filters in core/voice.py.

These guard the "Ted acted on something I never said" class of bug. The functions
under test are pure string logic, so they're loaded straight from source rather
than importing core.voice (which would pull in numpy, soundfile, Kokoro and a
live Groq client just to test two predicates).

Run with the venv python:  python tests/test_capture_gates.py
"""

import ast
import os
import sys

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def _load_from_voice(names):
    """Exec just the named functions (and module-level constants) out of voice.py."""
    path = os.path.join(os.path.dirname(__file__), "..", "core", "voice.py")
    tree = ast.parse(open(path).read())
    ns = {}
    for node in tree.body:
        wanted = (isinstance(node, ast.FunctionDef) and node.name in names)
        if wanted or isinstance(node, ast.Assign):
            try:
                exec(compile(ast.Module([node], []), path, "exec"), ns)
            except Exception:
                pass          # skip assignments that need imports we didn't load
    return ns


_ns = _load_from_voice({"_looks_hallucinated", "_is_junk_fragment"})
looks_hallucinated = _ns["_looks_hallucinated"]
is_junk = _ns["_is_junk_fragment"]

print("— hallucination blocklist —")
for phrase in ("thank you", "Thank you.", "thanks for watching", "you", "..."):
    check(f"drops Whisper phantom {phrase!r}", looks_hallucinated(phrase))
check("a real sentence containing 'thank you' survives",
      not looks_hallucinated("thank you for the help with the reorder"))

print("\n— short-fragment guard —")
# A cough or a door bump clears the energy and confidence gates and comes back
# as a short plausible word. Before this guard, "Start." made Ted resume playback.
for junk in ("Tep.", "Start.", "Hmm.", "Mm-hmm.", "Uh", "Ah.", "Tik", "Beep", ".", "   "):
    check(f"drops junk fragment {junk!r}", is_junk(junk))

for cmd in ("stop", "Play.", "next", "skip", "yes", "no", "cancel", "pause",
            "Hey Ted", "louder", "mute", "play next"):
    check(f"keeps real short command {cmd!r}", not is_junk(cmd))

for sentence in ("what's the weather like", "remind me to call mom",
                 "turn off the kitchen lights", "who won the game last night"):
    check(f"never touches full sentence {sentence[:32]!r}", not is_junk(sentence))

print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
