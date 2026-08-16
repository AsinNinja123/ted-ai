"""core/pet.py — the floating desk pet, and the one place that owns its window.

A frameless, transparent, always-on-top pywebview window holding the pixel
teddy bear in ui/ted_pet.html. It sits above everything until Charlie closes it
or Ted exits.

Two rules shape this module:

* **Nothing here may take Ted down.** The pet is decoration attached to a real
  status readout; a second native window is exactly the kind of thing that
  fails on one macOS version and not another. Every public function swallows
  its own exceptions and reports through the return value or a print.
* **The state is Ted's, not the pet's.** ``thinking``/``excited``/``bored`` are
  pushed from core/app.py off what Ted is really doing, so a glance at the bear
  answers "is it working?" without opening the HUD. The pet never invents a
  mood, which is why there is no timer in this file.
"""

import json
import os
import threading
import time

from core.paths import DATA

PET_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "ted_pet.html")

# Shares runtime.json with the provider pin, for the same reason: the dashboard
# is sometimes a separate process, and a preference held only in module state
# is a preference that resets without being changed.
_RUNTIME = os.path.join(DATA, "runtime.json")

STATES = ("idle", "thinking", "bored", "excited")

# How long with no exchange before the bear starts looking bored. Long enough
# that it is not commenting on Charlie reading Ted's last answer.
BORED_AFTER = 240.0

_window = None
_lock = threading.Lock()
_state = "idle"


# ---------- persisted visibility ----------

def _read_runtime():
    try:
        with open(_RUNTIME, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def is_enabled() -> bool:
    """True unless Charlie has closed the pet. Defaults to on for a new install."""
    return bool(_read_runtime().get("pet_visible", True))


def set_enabled(value: bool) -> bool:
    """Persist whether the pet should exist. Returns the value actually stored."""
    data = _read_runtime()
    data["pet_visible"] = bool(value)
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = _RUNTIME + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _RUNTIME)
    except Exception as exc:
        print(f"[pet] could not save visibility: {exc}")
    return bool(value)


# ---------- the window ----------

def is_open() -> bool:
    return _window is not None


def open_pet(webview, js_api=None):
    """Create the pet window. Returns it, or None if the platform refused.

    Called after the HUD window exists, because a frameless accessory window
    created first can end up owning the application activation state.
    """
    global _window
    with _lock:
        if _window is not None:
            return _window
        try:
            _window = webview.create_window(
                "Ted's pet",
                PET_HTML,
                js_api=js_api,
                width=160, height=160,
                resizable=False,
                frameless=True,
                easy_drag=True,        # drag the bear itself; there is no title bar
                on_top=True,
                shadow=False,          # a drop shadow on a transparent window
                                       # draws a grey box around the bear
                transparent=True,
                background_color="#000000",
                focus=False,           # appearing must not steal the caret out
                                       # of whatever Charlie is typing in
            )
            print("[pet] floating teddy is up")
        except Exception as exc:
            _window = None
            print(f"[pet] this platform would not open the pet window: {exc}")
        return _window


def close_pet(remember=True):
    """Destroy the pet window. ``remember`` persists the choice across launches."""
    global _window
    with _lock:
        window, _window = _window, None
    if remember:
        set_enabled(False)
    if window is None:
        return False
    try:
        window.destroy()
    except Exception as exc:
        print(f"[pet] window would not close cleanly: {exc}")
    return True


def set_state(state, hold_ms=0):
    """Push one of STATES to the bear. Unknown states are ignored, not guessed."""
    global _state
    if state not in STATES:
        return
    window = _window
    _state = state
    if window is None:
        return
    try:
        window.evaluate_js(f"tedPet.setState({json.dumps(state)}, {int(hold_ms)})")
    except Exception:
        # The window was closed between the check above and this call, or the
        # page has not finished parsing. Neither is worth a log line every turn.
        pass


def react(state="excited", hold_ms=2200):
    """A momentary reaction that decays back to whatever Ted is actually doing.

    Kept separate from set_state so a successful tool call cannot leave the bear
    grinning for the rest of the evening.
    """
    set_state(state, hold_ms=hold_ms)


def idle_or_bored(last_exchange_time):
    """The resting state, given when Charlie last said something."""
    if last_exchange_time and (time.time() - last_exchange_time) > BORED_AFTER:
        return "bored"
    return "idle"
