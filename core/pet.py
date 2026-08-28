"""Floating pixel-pet window for Ted.

The pet is a second, transparent pywebview surface backed by the same TedApi as
the main HUD.  It owns presentation only: voice capture, transcription, chat,
and shutdown all go through the existing runtime so there is still one Ted.
"""

import json
import os
import threading


PET_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "ted_pet.html")

_window = None
_lock = threading.Lock()


def open_pet(webview, js_api=None):
    """Open the always-on-top pet once; return the native window or ``None``."""
    global _window
    with _lock:
        if _window is not None:
            return _window
        try:
            _window = webview.create_window(
                "Ted Pet", PET_HTML, js_api=js_api,
                width=270, height=320, min_size=(270, 320),
                resizable=False, frameless=True, easy_drag=True,
                on_top=True, shadow=False, transparent=True,
                background_color="#000000", focus=False,
            )
            print("[pet] pixel pet is up")
        except Exception as exc:
            _window = None
            print(f"[pet] could not open: {exc}")
        return _window


def close_pet():
    """Close the pet window without changing what appears next launch."""
    global _window
    with _lock:
        window, _window = _window, None
    if window is None:
        return False
    try:
        window.destroy()
    except Exception as exc:
        print(f"[pet] could not close cleanly: {exc}")
    return True


def focus_pet():
    """Bring the pet forward so its text field can accept keyboard input."""
    window = _window
    if window is None:
        return False
    try:
        window.restore()
        import AppKit
        import Foundation

        def focus_on_main():
            app = AppKit.NSApplication.sharedApplication()
            app.activateIgnoringOtherApps_(True)
            for native_window in app.windows():
                if native_window.title() == "Ted Pet":
                    native_window.makeKeyAndOrderFront_(None)
                    break

        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
            focus_on_main)
        return True
    except Exception as exc:
        print(f"[pet] could not focus: {exc}")
        return False


def evaluate(code):
    window = _window
    if window is None:
        return
    try:
        window.evaluate_js(code)
    except Exception:
        pass


def set_state(state):
    evaluate(f"tedPet.setState({json.dumps(state)})")


def add_message(role, text):
    evaluate(f"tedPet.showMessage({json.dumps(role)}, {json.dumps(text)})")


def set_mode(mode):
    evaluate(f"tedPet.setMode({json.dumps(mode)})")
