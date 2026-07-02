"""
core/computer.py — Basic computer control via AppleScript / subprocess.

Public API:
    type_text(text)        → types text at current cursor position
    press_key(key)         → presses a named key (enter, escape, tab, …)
    get_focused_app()      → name of currently focused app
    get_clipboard()        → clipboard text (pbpaste)
    set_clipboard(text)    → writes text to clipboard (pbcopy)
"""

import subprocess


def _run(script: str) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception as e:
        print(f"[computer] AppleScript error: {e}")
        return ""


_KEY_CODES = {
    "enter":   36,
    "return":  36,
    "escape":  53,
    "esc":     53,
    "tab":     48,
    "space":   49,
    "delete":  51,
    "backspace": 51,
    "forward delete": 117,
    "left":    123,
    "right":   124,
    "down":    125,
    "up":      126,
    "home":    115,
    "end":     119,
}

_KEY_SHORTCUTS = {
    "copy":       "keystroke \"c\" using {command down}",
    "paste":      "keystroke \"v\" using {command down}",
    "cut":        "keystroke \"x\" using {command down}",
    "undo":       "keystroke \"z\" using {command down}",
    "redo":       "keystroke \"z\" using {shift down, command down}",
    "select all": "keystroke \"a\" using {command down}",
    "save":       "keystroke \"s\" using {command down}",
}


def type_text(text: str) -> str:
    safe = text.replace('"', '\\"').replace("\\", "\\\\")
    script = f'tell application "System Events" to keystroke "{safe}"'
    _run(script)
    return f"Typed: {text[:40]}{'...' if len(text) > 40 else ''}"


def press_key(key: str) -> str:
    k = key.lower().strip()
    if k in _KEY_SHORTCUTS:
        script = f'tell application "System Events" to {_KEY_SHORTCUTS[k]}'
        _run(script)
        return f"Pressed {key}."
    code = _KEY_CODES.get(k)
    if code is not None:
        script = f'tell application "System Events" to key code {code}'
        _run(script)
        return f"Pressed {key}."
    return f"Unknown key '{key}'."


def get_focused_app() -> str:
    name = _run('tell application "System Events" to return name of first application process whose frontmost is true')
    return name or "Unknown"


def get_clipboard() -> str:
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception:
        return ""


def set_clipboard(text: str) -> str:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        return "Copied."
    except Exception as e:
        return f"Clipboard error: {e}"
