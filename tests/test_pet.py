"""Checks for the floating desk pet.

The window itself needs a real macOS AppKit run loop, so what is covered here
is everything around it: the sprite's integrity, the persisted visibility, the
resting-state clock, and — most importantly — that every path tolerates the pet
not existing. The pet is decoration attached to a status readout, and the one
thing it must never do is take a turn down with it.
"""

import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pet


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


PET_HTML = open(pet.PET_HTML, encoding="utf-8").read()

print("— the sprite —")
# A row of the wrong width does not raise; it silently draws a bear with a
# dent in it. Verified here rather than left to the eye.
rows = re.search(r"var BEAR = \[(.*?)\];", PET_HTML, re.S).group(1)
sprite = re.findall(r"'([^']*)'", rows)
check("the bear is 16 rows", len(sprite) == 16)
check("every row is exactly 16 cells", all(len(r) == 16 for r in sprite))
palette = set(re.findall(r"^\s*([A-Z]):\s*'#", PET_HTML, re.M))
used = {c for row in sprite for c in row} - {"."}
check("every colour used by the sprite is defined in the palette",
      used <= palette)
check("the canvas is pinned, not stretched to the viewport",
      "#pet{display:block;width:160px;height:160px" in PET_HTML)
check("pixels are not smoothed away", "image-rendering:pixelated" in PET_HTML)
check("the window paints no background of its own",
      "background:transparent" in PET_HTML)

print("\n— states —")
check("all four states Charlie asked for exist",
      set(pet.STATES) == {"idle", "thinking", "bored", "excited"})
for state in pet.STATES:
    check(f"…and the page handles {state!r}",
          f"'{state}'" in PET_HTML or f'"{state}"' in PET_HTML)
check("excitement decays instead of sticking",
      "revertTo" in PET_HTML and "clearTimeout(revertTimer)" in PET_HTML)

print("\n— resting state follows the clock —")
now = time.time()
check("just-spoken-to is idle", pet.idle_or_bored(now) == "idle")
check("a long silence is bored",
      pet.idle_or_bored(now - pet.BORED_AFTER - 5) == "bored")
check("never having spoken is not boredom", pet.idle_or_bored(0) == "idle")

print("\n— a closed pet is not an error —")
# Every one of these runs with no window at all, which is the state after
# Charlie closes it and on any platform that refused to open it.
pet._window = None
try:
    pet.set_state("thinking")
    pet.react("excited")
    pet.set_state("not-a-real-state")
    ok = True
except Exception as exc:                                   # pragma: no cover
    ok = False
    print("      raised:", exc)
check("driving a pet that isn't there does nothing quietly", ok)
check("is_open reports the truth", pet.is_open() is False)
check("closing an already-closed pet returns False, not an exception",
      pet.close_pet(remember=False) is False)

print("\n— visibility survives a restart —")
_tmp = tempfile.mkdtemp()
_real_runtime = pet._RUNTIME
pet._RUNTIME = os.path.join(_tmp, "runtime.json")
try:
    check("a fresh install shows the pet", pet.is_enabled() is True)
    pet.set_enabled(False)
    check("closing it is remembered", pet.is_enabled() is False)
    pet.set_enabled(True)
    check("…and so is bringing it back", pet.is_enabled() is True)

    # The file is shared with the provider pin, so writing one must not clear
    # the other — they are separate settings in one document.
    with open(pet._RUNTIME, "w", encoding="utf-8") as fh:
        json.dump({"provider_mode": "local"}, fh)
    pet.set_enabled(False)
    saved = json.load(open(pet._RUNTIME, encoding="utf-8"))
    check("the provider pin sharing runtime.json is left intact",
          saved.get("provider_mode") == "local" and saved.get("pet_visible") is False)

    os.remove(pet._RUNTIME)
    check("a missing runtime.json defaults to showing the pet",
          pet.is_enabled() is True)
    with open(pet._RUNTIME, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    check("a corrupt runtime.json does not crash the pet",
          pet.is_enabled() is True)
finally:
    pet._RUNTIME = _real_runtime

print("\n— the HUD can always get it back —")
HUD = open(os.path.join(os.path.dirname(pet.PET_HTML), "ted_hud.html"),
           encoding="utf-8").read()
check("there is a pet control in the HUD", 'id="petbtn"' in HUD)
check("it calls through to Python", "pet_toggle" in HUD)
check("and the bear's own close button calls back",
      "pet_close" in PET_HTML)

print("\n— the pet mirrors the HUD rather than guessing —")
APP = open(os.path.join(os.path.dirname(os.path.dirname(pet.PET_HTML)),
                        "core", "app.py"), encoding="utf-8").read()
check("HUD state changes are the single source of the pet's state",
      "_PET_FOR_HUD_STATE" in APP and "def set_state(window, s)" in APP)
check("a thinking HUD means a thinking bear",
      '"thinking": "thinking"' in APP)
check("only genuinely important memories excite it",
      'importance", 2)) >= 3' in APP)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
