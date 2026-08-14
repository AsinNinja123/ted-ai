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
check("Ted is told the words are the user's to choose",
      "Do not improve the grammar" in llm_src)
check("…and not to argue about tone or jokes",
      "too casual" in llm_src or "misread" in llm_src)
check("…while still refusing slurs and abuse",
      "slurs" in llm_src)

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
