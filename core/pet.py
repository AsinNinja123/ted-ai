"""core/pet.py — the floating desk pet, and the one place that owns its window.

A frameless, transparent, always-on-top pywebview window holding the pixel
teddy bear in ui/ted_pet.html. It sits above everything until Charlie closes it
or Ted exits.

Two rules shape this module:

* **Nothing here may take Ted down.** The pet is decoration attached to a real
  status readout; a second native window is exactly the kind of thing that
  fails on one macOS version and not another. Every public function swallows
  its own exceptions and reports through the return value or a print.
* **The state is Ted's, not the pet's.** Every state is pushed from core/app.py
  off what Ted is really doing, so a glance at the bear answers "is it working?"
  without opening the HUD. The pet never invents a mood, which is why there is
  no timer in this file.

The bear itself is drawn by ui/ted_bear.js, shared with the companion in the
chat header. This module owns the window and the state; it does not own the
pixels, so the two surfaces cannot disagree about what a teddy bear looks like.
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

# The contract, shared with ui/ted_bear.js and with the in-chat companion.
# "responding" is Ted producing the reply, which looks different from thinking
# about it; "success" and "error" report what a tool actually did.
STATES = ("idle", "thinking", "responding", "success", "error")

# How long with no exchange before the bear starts to doze. Long enough that it
# is not commenting on Charlie reading Ted's last answer.
#
# Dozing is a LOOK, not a sixth state: it is a flavour of idle, so the five
# states above stay the whole contract and a caller reading get_state() is
# never surprised by a value that is not in the list.
BORED_AFTER = 240.0

_window = None
_lock = threading.Lock()
_state = "idle"
_long_idle = False


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
    return _save_flag("pet_visible", value)


# The in-chat companion is a separate preference from the floating window,
# because they are separate things to want: the bear beside Ted's name is part
# of the interface, while a window sitting on top of every other application is
# a much bigger ask. Both default on, and each has its own control.

def companion_enabled() -> bool:
    """True unless Charlie has hidden the bear in the chat window."""
    return bool(_read_runtime().get("companion_visible", True))


def set_companion_enabled(value: bool) -> bool:
    return _save_flag("companion_visible", value)


def _save_flag(key, value):
    """Write one boolean into runtime.json without disturbing the rest.

    Read-modify-write through a temp file and os.replace, because this file is
    shared with the provider pin and a partial write would lose it.
    """
    data = _read_runtime()
    data[key] = bool(value)
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = _RUNTIME + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _RUNTIME)
    except Exception as exc:
        print(f"[pet] could not save {key}: {exc}")
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


def react(state="success", hold_ms=2200):
    """A momentary reaction that decays back to whatever Ted is actually doing.

    Kept separate from set_state so a finished tool call cannot leave the bear
    celebrating for the rest of the evening.
    """
    set_state(state, hold_ms=hold_ms)


def set_long_idle(on):
    """Dozing on or off. Not a state — see the note beside BORED_AFTER."""
    global _long_idle
    on = bool(on)
    if on == _long_idle:
        return
    _long_idle = on
    window = _window
    if window is None:
        return
    try:
        window.evaluate_js(f"tedPet.setLongIdle({json.dumps(on)})")
    except Exception:
        pass


def is_long_idle(last_exchange_time):
    """Has it been quiet long enough for the bear to doze?"""
    return bool(last_exchange_time
                and (time.time() - last_exchange_time) > BORED_AFTER)


def rest(last_exchange_time):
    """Settle the bear into idle, dozing or not depending on the silence."""
    set_long_idle(is_long_idle(last_exchange_time))
    set_state("idle")
