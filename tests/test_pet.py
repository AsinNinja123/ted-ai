"""Checks for Ted Bear — the companion in the chat window and the desk pet.

Both surfaces render the SAME bear from ui/ted_bear.js. That sharing is the
thing most worth protecting: two copies of a sprite drift the first time either
is touched, and then Ted has two different faces.

The sprite and the state engine are executed for real in Node rather than
grepped, because every interesting failure here — an off-centre face, a state
that silently falls back to idle, a mouth drawn over a nose — would pass a
grep for the right identifier. Node is a JS engine only; if it is missing those
checks are skipped rather than failed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pet

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
BEAR_JS = open(os.path.join(UI, "ted_bear.js"), encoding="utf-8").read()
PET_HTML = open(os.path.join(UI, "ted_pet.html"), encoding="utf-8").read()
HUD = open(os.path.join(UI, "ted_hud.html"), encoding="utf-8").read()
APP = open(os.path.join(os.path.dirname(UI), "core", "app.py"), encoding="utf-8").read()


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def run_js(body):
    """Execute the real ted_bear.js against a fake canvas and return its JSON."""
    harness = """
var painted = [];
function makeCtx(){
  return { fillStyle:'#000', imageSmoothingEnabled:true,
           fillRect:function(x,y,w,h){
             painted.push({x:x, y:y, w:w, h:h, c:this.fillStyle}); },
           clearRect:function(){ painted.length = 0; } };
}
function makeCanvas(){
  var ctx = makeCtx();
  return { width:0, height:0, style:{}, getContext:function(){ return ctx; } };
}
var _raf = [];
var window = {
  requestAnimationFrame:function(fn){ _raf.push(fn); return _raf.length; },
  cancelAnimationFrame:function(){},
  setTimeout:function(fn, ms){ return 0; },
  clearTimeout:function(){}
};
var console = { error:function(m){ ERRORS.push(m); }, log:function(){} };
var ERRORS = [];
"""
    script = harness + BEAR_JS.replace("})(window);", "})(window);") + """
function step(n){ for(var i=0;i<(n||1);i++){ var f=_raf.shift(); if(f) f(); } }
var OUT = {};
""" + body + """
console_out(JSON.stringify(OUT));
"""
    script = script.replace("console_out(", "process.stdout.write(")
    path = os.path.join(tempfile.mkdtemp(), "bear_test.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script)
    out = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:500])
    return json.loads(out.stdout.strip().splitlines()[-1])


print("— the sprite —")
rows = re.findall(r"'([.OBSTD]*)'", BEAR_JS.split("var BEAR = [")[1].split("];")[0])
check("the bear is 20 rows", len(rows) == 20)
check("every row is exactly 20 cells", all(len(r) == 20 for r in rows))
palette = set(re.findall(r"^\s*([A-Z]):\s*'#", BEAR_JS, re.M))
used = {c for row in rows for c in row} - {"."}
check("every colour used is defined in the palette", used <= palette)
check("the palette stays limited", len(palette) <= 6)

# A face that is not symmetric about the centre reads as a mistake rather than
# as a style. The shading column is the one intentional exception.
FEATURE_ROWS = [r for r in rows if "D" in r]
asym = []
for i, r in enumerate(rows):
    flipped = r[::-1]
    diff = [c for c in range(20) if r[c] != flipped[c]]
    # 'S' is the deliberate one-sided shadow; ignore columns that only differ
    # because of it.
    real = [c for c in diff if "S" not in (r[c], flipped[c])]
    if real:
        asym.append(i)
check("the bear is symmetric apart from its shading", not asym)
check("it has bead eyes, a nose and a mouth", len(FEATURE_ROWS) >= 4)
check("it has a tan muzzle and a separate tan belly",
      sum(1 for r in rows[8:13] if "T" in r) >= 4
      and sum(1 for r in rows[14:19] if "T" in r) >= 3)
# The gap is what stops an open mouth merging into the nose.
check("a clear row separates the nose from the mouth",
      "D" not in rows[10])

print("\n— the state engine —")
if not shutil.which("node"):
    print("  · node not installed — engine checks skipped")
else:
    try:
        got = run_js("""
        var c = makeCanvas();
        var bear = window.TedBear.mount(c, {scale:2, state:'idle'});
        OUT.sprite_errors = ERRORS;
        OUT.states = window.TedBear.STATES;
        OUT.backing = c.width + 'x' + c.height;
        OUT.css = c.style.width + ' x ' + c.style.height;
        OUT.start = bear.getState();

        var seen = {};
        window.TedBear.STATES.forEach(function(s){
          bear.setState(s); seen[s] = bear.getState();
        });
        OUT.roundtrip = seen;

        bear.setState('not-a-state');
        OUT.bogus = bear.getState();

        // Cyan must appear ONLY for the working states.
        var cyan = {};
        window.TedBear.STATES.forEach(function(s){
          bear.setState(s);
          var found = false;
          for (var i = 0; i < 40; i++) {
            step(1);
            if (painted.some(function(p){ return p.c === '#16dede'; })) found = true;
          }
          cyan[s] = found;
        });
        OUT.cyan = cyan;

        bear.setScale(4);
        OUT.rescaled = c.width + 'x' + c.height + ' css ' + c.style.width;

        bear.setState('idle');
        bear.destroy();
        var before = _raf.length;
        step(2);
        OUT.stopped_after_destroy = (_raf.length <= before);
        """)
    except Exception as exc:                                    # pragma: no cover
        got = None
        FAIL += 1
        print(f"  ✗ could not run ted_bear.js in node: {exc}")

    if got:
        check("the sprite loads without complaint", got["sprite_errors"] == [])
        check("the five documented states are the contract",
              got["states"] == ["idle", "thinking", "responding",
                                "success", "error"])
        check("it starts where it was told to", got["start"] == "idle")
        check("every state can be set and read back",
              all(got["roundtrip"][s] == s for s in got["states"]))
        # A typo'd state must not silently become a different mood.
        check("an unknown state falls back to idle", got["bogus"] == "idle")

        # Backing store and CSS box identical, or the pixels smear. This
        # project has already shipped that bug once.
        check("the canvas is 1:1 with its CSS box",
              got["backing"] == "40x40" and got["css"] == "40px x 40px")
        check("…and stays 1:1 after a rescale",
              got["rescaled"] == "80x80 css 80px")

        check("thinking shows cyan", got["cyan"]["thinking"])
        check("success shows cyan", got["cyan"]["success"])
        check("idle shows none", not got["cyan"]["idle"])
        check("error shows none", not got["cyan"]["error"])
        # responding reads through motion; a third cyan device would be noise.
        check("responding shows none", not got["cyan"]["responding"])
        check("destroy stops the animation", got["stopped_after_destroy"])

print("\n— one bear, two surfaces —")
check("the sprite lives in exactly one file",
      "var BEAR = [" in BEAR_JS
      and "var BEAR = [" not in PET_HTML and "var BEAR = [" not in HUD)
check("the desk pet loads the shared bear",
      'src="ted_bear.js"' in PET_HTML)
check("the chat window loads the shared bear",
      'src="ted_bear.js"' in HUD)
check("the chat window mounts both placements",
      "bear-id" in HUD and "bear-mini" in HUD)
check("…and drives them as a group",
      "bears.forEach" in HUD)
check("the window still works if the script fails to load",
      "if(window.TedBear)" in HUD)

print("\n— placement —")
check("the identity bear sits with Ted's name",
      re.search(r'class="brand"[\s\S]{0,200}bear-id', HUD) is not None)
check("the compact bear sits in the chat header",
      re.search(r'id="top"[\s\S]{0,400}bear-mini', HUD) is not None)
check("the compact bear appears only once a conversation is live",
      "#bear-mini{" in HUD and "#bear-mini.live" in HUD
      and "markConversationLive" in HUD)
check("…and steps back out on a new chat",
      re.search(r"function newChat\(\)[\s\S]{0,600}bear-mini", HUD) is not None)
check("it is dropped first on a narrow window",
      "#top.tight #bear-mini{display:none}" in HUD)
check("the identity bear is not dropped — it lives in the sidebar",
      "#top.tight #bear-id" not in HUD)

print("\n— the preference —")
_tmp = tempfile.mkdtemp()
_real = pet._RUNTIME
pet._RUNTIME = os.path.join(_tmp, "runtime.json")
try:
    check("the companion is on for a fresh install", pet.companion_enabled())
    pet.set_companion_enabled(False)
    check("hiding it is remembered", not pet.companion_enabled())
    pet.set_companion_enabled(True)
    check("…and so is restoring it", pet.companion_enabled())

    # runtime.json is shared with the provider pin and the floating pet.
    with open(pet._RUNTIME, "w", encoding="utf-8") as fh:
        json.dump({"provider_mode": "local", "pet_visible": False}, fh)
    pet.set_companion_enabled(False)
    saved = json.load(open(pet._RUNTIME, encoding="utf-8"))
    check("saving it leaves the other settings alone",
          saved["provider_mode"] == "local" and saved["pet_visible"] is False
          and saved["companion_visible"] is False)

    os.remove(pet._RUNTIME)
    check("a missing runtime.json defaults to showing it",
          pet.companion_enabled())
    with open(pet._RUNTIME, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    check("a corrupt runtime.json does not crash it", pet.companion_enabled())
finally:
    pet._RUNTIME = _real

check("there is a control to hide and restore it", 'id="bearbtn"' in HUD)
check("…wired to Python so it survives a restart",
      "companion_toggle" in HUD and "def companion_toggle" in APP)
check("the window is told the saved value at startup",
      "_push_companion_state" in APP)
check("it is a separate preference from the floating window",
      "companion_visible" in open(
          os.path.join(os.path.dirname(UI), "core", "pet.py"),
          encoding="utf-8").read())

print("\n— a closed pet is not an error —")
pet._window = None
try:
    pet.set_state("thinking")
    pet.react("success")
    pet.set_long_idle(True)
    pet.set_state("not-a-real-state")
    pet.rest(time.time())
    ok = True
except Exception as exc:                                       # pragma: no cover
    ok = False
    print("      raised:", exc)
check("driving a pet that isn't there does nothing quietly", ok)
check("closing an already-closed pet returns False",
      pet.close_pet(remember=False) is False)

print("\n— dozing is a look, not a sixth state —")
check("the Python contract matches the JS one",
      list(pet.STATES) == ["idle", "thinking", "responding", "success", "error"])
check("just-spoken-to is not dozing", not pet.is_long_idle(time.time()))
check("a long silence is", pet.is_long_idle(time.time() - pet.BORED_AFTER - 5))
check("never having spoken is not", not pet.is_long_idle(0))

print("\n— the bears mirror Ted rather than guessing —")
check("one helper drives every surface", "def companion(window, state" in APP)
check("HUD state changes are the single source",
      "_PET_FOR_HUD_STATE" in APP and "def set_state(window, s)" in APP)
check("Ted talking maps to responding", '"speaking": "responding"' in APP)
check("a real problem maps to error",
      '"error": "error"' in APP and "def show_issue(window, text)" in APP)
# The point of success/error: they report what a tool DID, judged by the same
# failure test the rest of the app uses.
check("a verified tool result decides success or error",
      "companion_pulse" in APP and "th.looks_like_failure(result)" in APP)
check("a tool that only armed a confirmation gets no reaction",
      re.search(r"acted = \(name in th\.ACTION_TOOLS", APP) is not None)
check("streaming raises responding where Python cannot",
      "tedHud.setBearState('responding')" in HUD)
check("…and finishing a reply is not treated as an achievement",
      re.search(r"endTedReply[\s\S]{0,900}setBearState\('idle'\)", HUD) is not None)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
