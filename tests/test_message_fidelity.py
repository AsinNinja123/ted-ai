"""tests/test_message_fidelity.py — the user's words are sent, not rewritten.

Two failures from one real conversation:

  1. Charlie said: send him "hey this is ted". Ted asked "How should it sound —
     casual, formal, short, funny?" and then generated its own message. There
     was no way to express "these exact words": send_message took an
     `instruction` (a brief) and a `style`, and every message went through a
     model rewrite. Words the user typed are not a brief.

  2. Ted asked him to approve "Ready to message Gavin." with no message shown.
     He replied: "what message where you going to send. I didnt tell you waht
     to send." Confirming something you cannot see is not consent.

Run with the venv python:  python tests/test_message_fidelity.py
"""

import os
import sys
import ast
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("— the tool can express 'these exact words' —")

src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "tools.py"), encoding="utf-8").read()
schemas = None
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TOOL_SCHEMAS":
        schemas = ast.literal_eval(node.value)
send = next(e["function"] for e in schemas
            if e.get("function", {}).get("name") == "send_message")
props = send["parameters"]["properties"]

check("send_message takes verbatim text", "text" in props)
check("…and still takes an instruction for 'tell him I'll be late'",
      "instruction" in props)
desc = send["description"]
check("…and the description tells the model which is which",
      "verbatim" in desc.lower() or "exactly as written" in desc.lower())
check("…and says not to tidy the user's wording",
      "tidy" in desc.lower() or "rephrase" in desc.lower())
check("…and forbids setting both", "Never set both" in desc)

print("\n— verbatim text skips the rewrite and the vibe question —")

voice_stub = types.ModuleType("core.voice")
voice_stub.SPEED = 1.1
sys.modules.setdefault("core.voice", voice_stub)

import importlib.util
app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "app.py")
app_src = open(app_path, encoding="utf-8").read()
tree = ast.parse(app_src)


def func(name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(app_src, n)
    return ""


cont = func("_continue_compose")
check("_continue_compose accepts text", "text=None" in cont)
# The verbatim branch has to come BEFORE the style question, or the user is
# still asked for a vibe on words they already wrote.
i_text = cont.find("if text and text.strip():")
i_style = cont.find("How should it sound")
check("…and sends it before asking about style",
      i_text != -1 and i_style != -1 and i_text < i_style)
check("…without calling the message generator on it",
      "generate_message_with_style" not in cont[i_text:i_style])

compose = func("_compose_and_send")
check("_compose_and_send passes text through", "text=None" in compose)
check("…and carries it across contact disambiguation",
      '"text": text' in compose)

print("\n— you are shown what you are approving —")

resp = func("_dispatch_tool") or app_src
check("the confirmation quotes the actual message",
      'body = (args.get("text") or "").strip()' in app_src
      and "Ready to send" in app_src)
check("…and refuses to ask for approval of nothing",
      "I don't have anything to say to" in app_src)

print("\n— the persona stops second-guessing the user's own wording —")

llm_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "core", "llm.py"), encoding="utf-8").read()
# These check the RULES survive, not their wording. The persona was rewritten
# on Aug 14 (~1,120 tokens down to ~560) and these three assertions caught it,
# which is exactly their job — but matching one phrase each made them fail on a
# rewrite that kept every rule intact. Import the prompt and check the
# behaviour it describes rather than grepping the source for a sentence.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.llm import SYSTEM_PROMPT as _PERSONA, TOOL_RULES as _TOOL_RULES

# The action and message rules moved OUT of the always-on persona on Aug 14 and
# are attached whenever a real tool is in the menu. They were costing ~190
# tokens on every "how are you", a turn where no tool is attached and neither
# rule can be reached. Checking the text alone is no longer enough — the
# wiring is now the thing that could break — so the attachment is checked too,
# further down.
_p = (_PERSONA + _TOOL_RULES).lower()
check("Ted is told to send the user's words unchanged",
      "send them exactly" in _p
      and "no fixed grammar" in _p and "no added greeting" in _p)
check("…and not to argue about tone or jokes",
      ("too blunt" in _p or "too casual" in _p)
      and ("land wrong" in _p or "misread" in _p))
check("…while still refusing slurs and abuse",
      "slurs" in _p and "abuse" in _p)
check("…and the honesty rule is still stated in the persona",
      "only through tools" in _p and "saying it is not doing it" in _p)

print("\n— Ted only says 'playing' when something is actually playing —")

# start_playback returning without an exception does not mean audio started.
# Ted said "Playing top Noah Kahan songs" to a silent room; Charlie said "its
# not playing". Same class of bug as the message rewrite: reporting intent as
# if it were outcome.
from core import spotify_web  # noqa: E402


class _FakeSp:
    def __init__(self, states):
        self.states = list(states)

    def current_playback(self):
        return self.states.pop(0) if self.states else None


_uri = "spotify:track:abc"
check("confirmed playing reads as playing",
      spotify_web._confirm_playing(_FakeSp([{"is_playing": True, "item": {"uri": _uri}}]),
                                   _uri, timeout=1.0) is True)
check("a device reporting not-playing is not claimed as playing",
      spotify_web._confirm_playing(_FakeSp([{"is_playing": False, "item": {"uri": _uri}}]),
                                   _uri, timeout=0.8) is False)
check("a playlist that starts on some other track still counts",
      spotify_web._confirm_playing(
          _FakeSp([{"is_playing": True, "item": {"uri": "spotify:track:other"},
                    "context": {"uri": "spotify:playlist:xyz"}}]),
          "spotify:playlist:xyz", timeout=1.0) is True)
check("no playback state at all is unknown, not success",
      spotify_web._confirm_playing(_FakeSp([]), _uri, timeout=0.6) is None)


class _Boom:
    def current_playback(self):
        raise RuntimeError("api down")


check("an API error is unknown, not success",
      spotify_web._confirm_playing(_Boom(), _uri, timeout=0.6) is None)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)


print("\n— …and those rules are attached whenever a tool could act —")

llm_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "core", "llm.py"), encoding="utf-8").read()
check("a real tool in the menu brings the rules with it",
      "TOOL_RULES + TOOL_GUIDANCE if _real_tools" in llm_src)
check("…and 'real' means something other than the discovery tool",
      'name") != "find_tools"' in llm_src)
check("a discovery-only turn gets the short guidance instead",
      "else DISCOVERY_GUIDANCE" in llm_src)
check("there are exactly two shapes, so prefix caching still works",
      llm_src.count("DISCOVERY_GUIDANCE") >= 2)


print("\n— the persona has takes of its own, and Charlie can override it —")

from core.llm import SYSTEM_PROMPT as _P2
_l = _P2.lower()

# These check BEHAVIOURS, not phrasing. The block was rewritten twice in one
# day — once for size, once because Charlie said it read like a butler — and a
# test that pins wording just breaks on every rewrite without protecting
# anything. What must survive is what the block makes Ted do.
check("memory is named as HIS taste, not Ted's opinions",
      "what he likes" in _l and "you have your own taste" in _l)
check("…and an opinion has to land somewhere",
      "land somewhere" in _l)
check("…and disagreeing is explicitly invited",
      "argue with him" in _l and "pushed back on" in _l)

check("Charlie can override a default", "outranks your instincts" in _l)
check("…including taking a side Ted does not hold",
      "side you don't hold" in _l and "devil's advocate" in _l)
check("…without a lecture first", "no lecture" in _l)
check("…and the override still has exactly one boundary",
      "really hurting someone" in _l)

check("multi-step work is thought through before it is answered",
      "multi-step before answering" in _l)
check("half-remembered facts are flagged rather than asserted",
      "flagged or looked up" in _l)
check("…stated as the failure he cannot catch", "he can't catch" in _l)

check("speech-recognition and typo tolerance survived the rewrite",
      "what he meant" in _l)
check("the voice is described rather than legislated",
      "like a person, not a product" in _l)

# This block is the floor under every single request. It has been 371, then
# 605, and is now 310; the ceiling exists so the next rewrite has to argue for
# its size rather than drift into it.
check("the persona stays under its token budget", len(_P2) / 4 < 400)
