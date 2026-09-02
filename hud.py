"""
hud.py — Ted voice assistant, entry point. Run this:  python hud.py

The runtime lives in core/:
    core/app.py            TedApi — conversation loop, command routing, threads
    core/voice.py          audio engine, TTS (Kokoro/ElevenLabs), STT capture
    core/llm.py            Groq client, persona, streaming replies, ask-Claude
    core/intents.py        spoken-command parsing (pure, unit-tested)
    core/actions.py        app/website launchers, Spotify transport, contacts
    core/music.py          spoken Spotify routing (local app + Web API)
    core/tool_handlers.py  handlers behind the LLM's function-calling tools
    core/hud_bridge.py     Python → JS calls into the HUD webview
    core/features.py       optional-module availability flags
    ui/ted_hud.html        the HUD window (particle sphere)
"""


# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 4 (§4.1 – §4.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The starting pistol. When you type `python hud.py`, this is the file
#      Python reads top to bottom. Everything else in Ted only runs because
#      something in here reached out and started it.
#
#      Nothing in this file thinks. It has no idea what a prompt is. Its whole
#      job is: open a window, hand that window a Python object it can call,
#      start some background threads, and make sure that when the window
#      closes, Ted saves what it learned before the process dies.
#
#  WHERE IT SITS
#      hud.py  ──creates──>  the pywebview window (shows ui/ted_hud.html)
#              ──creates──>  TedApi (core/app.py) — the actual assistant
#              ──starts───>  the dashboard web server on port 5175, on a thread
#              ──registers>  three separate shutdown paths, all landing in
#                            _teardown()
#
#  THE SHAPE OF IT, TOP TO BOTTOM
#      1. Signal setup for the native Dock launcher   (macOS plumbing — skip it
#         on a first read; it is not part of how Ted thinks)
#      2. sys.path fix so `from core.x import y` works from any folder
#      3. Import webview, then TedApi. Creating TedApi() is what wires up the
#         whole assistant.
#      4. _start_memory_dashboard()  — the Flask server behind the Memory panel
#      5. _teardown()                — save the session memory, close audio + DB
#      6. The `if __name__ == "__main__":` block — build the window and start it
#
#  IF YOU WANT TO CHANGE SOMETHING
#      Window size, title, background colour     -> webview.create_window(...)
#      What happens when Ted quits               -> _teardown()
#      Something that must run once at startup   -> add a thread near
#                                                   _start_memory_dashboard, OR
#                                                   put it in TedApi.start()
#                                                   (core/app.py) if it needs the
#                                                   assistant to already exist.
#
#  PYTHON YOU'LL SEE HERE THAT MIGHT BE NEW
#      `if __name__ == "__main__":`
#          True only when this file is the one you ran directly, rather than
#          one that got imported by another file. It is Python's "main()".
#
#      threading.Thread(target=f, daemon=True).start()
#          Run function f at the same time as everything else. `daemon=True`
#          means "do not keep the program alive just for this thread" — when
#          the main program wants to quit, this thread is killed rather than
#          waited on. Ted uses this a lot: every background watcher is a daemon
#          thread.
#
#      atexit.register(fn, arg)
#          "Call fn(arg) on the way out, whatever happens." A safety net.
#
#      `try: ... except Exception: pass`
#          Try it; if it explodes, carry on silently. Used here only for
#          cleanup steps where failing is genuinely not worth crashing over.
#          Note that Ted does NOT do this for real work — see §34, the rule
#          about silent failures being the expensive ones.
# =============================================================================

import os
import sys
import atexit
import signal
import threading

# The native Dock host uses SIGUSR1 as a private "show your window" message.
#
# This has to be a BLOCKED signal consumed by a dedicated sigwait thread, not a
# signal.signal() handler. Python runs signal handlers only when the main
# thread executes bytecode, and webview.start() hands the main thread to
# AppKit's run loop for the rest of the session — so the handler that used to
# be installed below never ran once Ted was actually up. The Dock host was
# sending SIGUSR1 correctly and the interpreter was dropping it. Measured with
# a standalone AppKit run loop, not inferred.
#
# Blocking must happen HERE, at import, before AppKit exists and before any
# thread starts: threads inherit this mask, so none of them can take SIGUSR1's
# default action and kill Ted mid-startup. A click that arrives before the
# window is ready now stays pending and is honored when the watcher starts,
# instead of being ignored outright.
if os.environ.get("TED_NATIVE_HOST") == "1":
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)

# Make `from core.xxx import …` work from any cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TED_VERSION = "v4"
print(f"▶ Launching Ted {TED_VERSION}  ({os.path.abspath(__file__)})")

try:
    from config import GROQ_API_KEY
except Exception:
    GROQ_API_KEY = ""
if not GROQ_API_KEY:
    print("[provider] No Groq key configured — Ted will use the local Ollama brain.")

import webview

from core.paths import UI_HTML
from core.voice import engine
from core.app import TedApi

print("Ted is ready.")

api = TedApi()

# ---- Memory dashboard (HUD "Memory" panel) ----------------------------------
# Same Flask app as `python -m dashboard`, served from a daemon thread so the
# HUD's Memory button always has something to load. If port 5175 is already
# taken (dashboard running standalone), we just use that one.
def _start_memory_dashboard():
    try:
        from dashboard.app import app as _dash_app
        from dashboard import db as _dash_db
        _dash_db.get_conn()          # ensure audit schema/triggers exist
        _dash_app.run(host="127.0.0.1", port=5175, threaded=True, use_reloader=False)
    except OSError:
        # Port taken. If it's a CURRENT dashboard that's fine — but an old
        # process (pre-chat-API) silently breaks the HUD sidebar, so check.
        try:
            import json as _json
            from urllib.request import urlopen
            v = _json.load(urlopen("http://127.0.0.1:5175/api/version", timeout=2))
            if v.get("chats"):
                print("[dashboard] port 5175 already serving a current dashboard — using it")
            else:
                raise ValueError("no chat api")
        except Exception:
            print("=" * 70)
            print("[dashboard] WARNING: something OLD is holding port 5175 —")
            print("            chat history will NOT save. Quit the other dashboard")
            print("            (or run: lsof -ti :5175 | xargs kill) and restart Ted.")
            print("=" * 70)
    except Exception as e:
        print(f"[dashboard] memory dashboard not started: {e}")


threading.Thread(target=_start_memory_dashboard, daemon=True,
                 name="memory-dashboard").start()

# Shutdown runs from several places that can race each other (window close fires
# the pywebview hook AND then atexit; Ctrl-C fires a signal AND then atexit).
# Guard so the session memory is only generated once.
_shutdown_lock = threading.Lock()
_shutdown_done = False


def _teardown(reason):
    """Write the session memory and release audio/DB. Idempotent.

    This is why session_summaries sat empty for months: the only write paths were
    a 30-minute idle timer and a signal handler, and closing the window fires
    neither — pywebview just returns from start() and the process exits normally.
    Now every exit route lands here.
    """
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True
    print(f"[shutdown] {reason} — saving state")
    try:
        engine.stop_playback()
    except Exception:
        pass
    try:
        # This is optional housekeeping, not a reason to leave Ted frozen on
        # exit. A provider call once ignored its nominal request timeout and
        # held the process in "saving state" indefinitely. Chat turns are
        # already persisted individually, so give the summary a firm deadline.
        save_error = []

        def _save_memory():
            try:
                api.write_session_memory(reason="shutdown", end_session=True)
            except Exception as exc:
                save_error.append(exc)

        saver = threading.Thread(target=_save_memory, daemon=True,
                                 name="shutdown-memory")
        saver.start()
        saver.join(timeout=15.0)
        if saver.is_alive():
            print("[shutdown] session summary timed out; chat turns are already saved")
        elif save_error:
            print(f"[shutdown] session memory skipped: {save_error[0]}")
    except Exception as e:
        print(f"[shutdown] session memory skipped: {e}")
    try:
        from core.memory import close as _mem_close
        _mem_close()
    except Exception:
        pass
    try:
        engine.close()
    except Exception:
        pass


def _shutdown(signum, frame):
    _teardown(f"signal {signum}")
    # SystemExit can be swallowed by pywebview's native AppKit event loop. In
    # practice the UI disappeared while the Python child, Flask server, and
    # audio engine stayed alive indefinitely, leaving the native launcher
    # waiting forever on quit. Teardown above has already saved memory and
    # closed owned resources, so finish the signal path at the process level.
    os._exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)
atexit.register(_teardown, "process exit")   # catches a normal window close

# ---- Entry point ------------------------------------------------------------
if __name__ == "__main__":
    window = webview.create_window(
        f"Ted {TED_VERSION}",
        UI_HTML,
        js_api=api,       # exposes TedApi methods to the JS side as window.pywebview.api.*
        width=1100,
        height=720,
        min_size=(760, 560),
        background_color="#171614",
        text_select=True,       # allow selecting/copying chat text
    )
    api.window = window

    # Preferred path: fires while the interpreter is still healthy, so the Groq
    # call that writes the memory can actually complete. atexit is the backstop
    # for exits this doesn't catch.
    try:
        window.events.closing += lambda: _teardown("window closed")
    except Exception:
        pass   # older pywebview without the events API — atexit still covers us

    # Configure the child on AppKit's main thread *before* pywebview creates
    # its native window. Changing activation policy from pywebview's ready
    # callback runs on a worker thread and can leave the child alive without a
    # visible window. Accessory apps can own normal windows but do not get a
    # second Dock tile; the regular native Ted host remains the Dock identity.
    native_host = os.environ.get("TED_NATIVE_HOST") == "1"
    # Whether the Dock host launched us decides whether the SIGUSR1 handler
    # below is installed at all, so a Dock icon that does nothing has two very
    # different causes. Say which one applies instead of leaving it to be
    # re-derived from a traceback that never gets written.
    print(f"[app] native Dock host: {'yes' if native_host else 'no'} — "
          f"window raise on Dock click is "
          f"{'enabled' if native_host else 'disabled'}")

    def _make_python_accessory():
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)

    if native_host:
        try:
            _make_python_accessory()
        except Exception as exc:
            print(f"[app] could not prepare Python as an accessory: {exc}")

        def _raise_ted_window():
            """Handle the native Dock host's request inside the UI process."""
            try:
                import AppKit
                import Foundation

                def raise_on_main():
                    app = AppKit.NSApplication.sharedApplication()
                    # Activate the accessory process before ordering its window.
                    # Doing this afterward produces the exact Dock-click flicker
                    # Charlie saw: the window is briefly ordered, then Chrome
                    # remains the active application and covers it again.
                    options = (AppKit.NSApplicationActivateIgnoringOtherApps |
                               AppKit.NSApplicationActivateAllWindows)
                    try:
                        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
                            options)
                    except Exception:
                        app.activateIgnoringOtherApps_(True)
                    for native_window in app.windows():
                        if native_window.title().startswith("Ted"):
                            # deminiaturize: is the documented way back out of
                            # the Dock. On this macOS makeKeyAndOrderFront:
                            # happens to restore a miniaturized window too
                            # (checked), but that is not a promise AppKit
                            # makes, and asking for what we actually want
                            # costs one line.
                            if native_window.isMiniaturized():
                                native_window.deminiaturize_(None)
                            native_window.orderFrontRegardless()
                            native_window.makeKeyAndOrderFront_(None)

                Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    raise_on_main)
            except Exception as exc:
                print(f"[app] could not raise Ted's window: {exc}")

        def _watch_for_raise_requests():
            """Consume Dock-host raise requests on a thread that can actually run.

            sigwait blocks here instead of in the interpreter's signal
            machinery, so delivery does not depend on the main thread ever
            leaving AppKit's run loop — which it does not.
            """
            while True:
                try:
                    signal.sigwait({signal.SIGUSR1})
                except Exception as exc:
                    print(f"[app] Dock raise watcher stopped: {exc}")
                    return
                _raise_ted_window()

        threading.Thread(target=_watch_for_raise_requests, daemon=True,
                         name="dock-raise").start()

    def _ready():
        # pywebview sets its process back to a regular application while it
        # creates the native window. Once the window exists, repeat the policy
        # change on AppKit's main queue so the window remains visible while the
        # duplicate Python Dock tile disappears.
        if native_host:
            try:
                import Foundation
                Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    _make_python_accessory)
            except Exception as exc:
                print(f"[app] could not hide Python Dock identity: {exc}")
        # The companion pet is opt-in. The HUD's pet button calls
        # TedApi.pet_open(), which creates it on demand; launching Ted itself
        # should open only the main chat window.
        api.start()

    webview.start(_ready)  # starts the runtime once the window is ready
    _teardown("webview stopped")
