"""core/hud_bridge.py — Python → JS calls into the HUD webview.

Every call is wrapped so a JS exception (or a not-yet-ready window) can never
crash the Python audio threads.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 20 (§20.3)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The Python-to-JavaScript direction of the bridge. Small, and worth reading in
#      full — it is four functions.
#
#      When Python wants the window to do something, it does not manipulate HTML. It
#      asks pywebview to RUN A LINE OF JAVASCRIPT in the page, and that JavaScript
#      calls a function on the `tedHud` object defined in ui/ted_hud.html.
#
#  WHY EVERY CALL IS WRAPPED IN try/except
#      These are called from audio threads. A JavaScript error, or a window that has
#      not finished loading, must never be able to take down the thread that is
#      listening to your microphone.
#
# =============================================================================

import json


def js(window, code):
    """Evaluate `code` in the webview JS context. Silently swallows any error."""
    try:
        window.evaluate_js(code)
    except Exception:
        pass


def set_state(window, s):
    """Drive the HUD state indicator: 'idle' | 'listening' | 'thinking' | 'speaking' | 'error'."""
    js(window, f"tedHud.setState('{s}')")


def add_message(window, role, text):
    """Append a chat message to the HUD log. role: 'user' | 'ted'."""
    js(window, f"tedHud.addMessage('{role}', {json.dumps(text)})")


def show_issue(window, text):
    """Surface a real problem on the HUD (yellow state + toast)."""
    js(window, f"tedHud.showIssue({json.dumps(text)})")


def amp_cb(window):
    """Return a callback that converts audio RMS → [0,1] amplitude for the HUD orb.
    Power-law curve (0.7 exponent) compresses loud peaks so the animation isn't jumpy."""
    def cb(rms):
        amp = min(1.0, (rms / 0.18) ** 0.7) if rms > 0 else 0.0
        js(window, f"tedHud.pushAmplitude({amp:.3f})")
    return cb
