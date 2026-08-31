"""Native window plumbing for Ted's floating pixel pet."""

import json
import os
import threading
import time


PET_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "ted_pet.html")
BASE_WIDTH = 292
BASE_HEIGHT = 248

_window = None
_lock = threading.Lock()
_native_window = None
_click_monitor = None
_first_mouse_installed = False


def _allow_first_mouse():
    """Let controls receive the click that activates Ted from another app.

    Cocoa normally consumes the first click on an inactive window. That made a
    pet beside ChatGPT look interactive on hover but require a useless first
    click before any button worked.
    """
    global _first_mouse_installed
    if _first_mouse_installed:
        return
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def acceptsFirstMouse_(view, event):
            return True

        selector = objc.selector(acceptsFirstMouse_,
                                 selector=b"acceptsFirstMouse:",
                                 signature=b"Z@:@")
        objc.classAddMethods(BrowserView.WebKitHost, [selector])
        _first_mouse_installed = True
    except Exception as exc:
        print(f"[pet] first-click support unavailable: {exc}")


def _configure_native_window():
    """Apply window policy on Cocoa's main loop without touching WebKit.

    Hover and active-window clicks remain ordinary DOM pointer events. Calling
    ``evaluate_js`` from an NSEvent monitor or NSTimer can re-enter WKWebView
    while Cocoa is dispatching an event and leave Ted beachballing, so the
    inactive first-click fallback below always dispatches from a worker.
    """
    try:
        import AppKit
        import Foundation

        def install():
            global _native_window, _click_monitor
            native = next((w for w in AppKit.NSApplication.sharedApplication().windows()
                           if w.title() == "Ted Pet"), None)
            if native is None:
                return
            if _native_window is native:
                return
            _native_window = native
            native.setAcceptsMouseMovedEvents_(True)
            native.setHidesOnDeactivate_(False)
            native.setCanHide_(False)
            native.setLevel_(AppKit.NSFloatingWindowLevel)
            native.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
                AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
            native.orderFrontRegardless()

            # A pywebview panel can leave its first inactive click with the app
            # underneath it.  Observe only that rare press and hand it to a
            # Python worker.  BrowserView.evaluate_js uses callAfter + a
            # semaphore, so invoking it on Cocoa's event thread deadlocks the
            # main loop; invoking it from this worker is safe.
            left_mask = getattr(AppKit, "NSEventMaskLeftMouseDown",
                                getattr(AppKit, "NSLeftMouseDownMask", 0))
            right_mask = getattr(AppKit, "NSEventMaskRightMouseDown",
                                 getattr(AppKit, "NSRightMouseDownMask", 0))
            last_left = [0.0, None]

            def inactive_press(event):
                point = AppKit.NSEvent.mouseLocation()
                frame = native.frame()
                if not (frame.origin.x <= point.x <= frame.origin.x + frame.size.width
                        and frame.origin.y <= point.y <= frame.origin.y + frame.size.height):
                    return
                web_x = point.x - frame.origin.x
                web_y = frame.size.height - (point.y - frame.origin.y)
                right_down = getattr(AppKit, "NSEventTypeRightMouseDown",
                                     getattr(AppKit, "NSRightMouseDown", 3))
                button = 2 if event.type() == right_down else 0
                clicks = max(1, int(event.clickCount()))
                if button == 0:
                    now = time.monotonic()
                    previous_at, previous_point = last_left
                    if (previous_point is not None and now - previous_at <= 0.42
                            and abs(web_x - previous_point[0]) <= 6
                            and abs(web_y - previous_point[1]) <= 6):
                        clicks = max(clicks, 2)
                    last_left[:] = [now, (web_x, web_y)]
                code = "tedPet.nativePress(%.1f, %.1f, %d, %d)" % (
                    web_x, web_y, clicks, button)
                threading.Thread(target=evaluate, args=(code,),
                                 name="pet-click", daemon=True).start()

            _click_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                left_mask | right_mask, inactive_press)
            print("[pet] native floating window configured")

        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(install)
    except Exception as exc:
        print(f"[pet] native window setup unavailable: {exc}")


def open_pet(webview, js_api=None):
    """Open the always-on-top pet once; return the native window or ``None``."""
    global _window
    with _lock:
        if _window is not None:
            try:
                _window.restore()
            except Exception:
                pass
            return _window
        try:
            _allow_first_mouse()
            _window = webview.create_window(
                "Ted Pet", PET_HTML, js_api=js_api,
                width=BASE_WIDTH, height=BASE_HEIGHT,
                min_size=(BASE_WIDTH, BASE_HEIGHT),
                resizable=False, frameless=True, easy_drag=True,
                on_top=True, shadow=False, transparent=True,
                background_color="#000000", focus=True,
            )
            try:
                _window.events.loaded += _configure_native_window
            except Exception:
                pass
            # Cocoa can finish a secondary window before create_window()
            # returns, which means the loaded event has already happened.
            # Queue the same idempotent setup now as well.
            _configure_native_window()
            print("[pet] pixel pet is up")
        except Exception as exc:
            _window = None
            print(f"[pet] could not open: {exc}")
        return _window


def is_open():
    return _window is not None


def close_pet():
    """Close the pet window without shutting down Ted."""
    global _window, _native_window, _click_monitor
    with _lock:
        window, _window = _window, None
    try:
        if _click_monitor is not None:
            import AppKit
            AppKit.NSEvent.removeMonitor_(_click_monitor)
    except Exception:
        pass
    _click_monitor = None
    _native_window = None
    if window is None:
        return False
    try:
        window.destroy()
    except Exception as exc:
        print(f"[pet] could not close cleanly: {exc}")
    return True


def focus_pet():
    """Make the pet key so its textarea receives actual keyboard events."""
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
            for native in app.windows():
                if native.title() == "Ted Pet":
                    native.makeKeyAndOrderFront_(None)
                    break

        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(focus_on_main)
        return True
    except Exception as exc:
        print(f"[pet] could not focus: {exc}")
        return False


def show_dashboard(window):
    """Restore and activate Ted's full HUD when the pet is double-clicked."""
    if window is None:
        return False
    try:
        window.restore()
        import AppKit
        import Foundation

        def show_on_main():
            app = AppKit.NSApplication.sharedApplication()
            app.activateIgnoringOtherApps_(True)
            for native in app.windows():
                title = native.title()
                if title != "Ted Pet" and title.startswith("Ted "):
                    native.makeKeyAndOrderFront_(None)
                    break

        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(show_on_main)
        return True
    except Exception as exc:
        print(f"[pet] could not show dashboard: {exc}")
        return False


def resize_for_input(extra_height=0):
    """Grow the composer downward while keeping the pet's top-left anchored."""
    window = _window
    if window is None:
        return False
    try:
        from webview.window import FixPoint
        extra = max(0, min(int(extra_height or 0), 58))
        window.resize(BASE_WIDTH, BASE_HEIGHT + extra,
                      FixPoint.NORTH | FixPoint.WEST)
        return True
    except Exception as exc:
        print(f"[pet] could not resize input: {exc}")
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
