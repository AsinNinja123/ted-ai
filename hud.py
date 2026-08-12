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

import os
import sys
import atexit
import signal
import threading

# Make `from core.xxx import …` work from any cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TED_VERSION = "v4"
print(f"▶ Launching Ted {TED_VERSION}  ({os.path.abspath(__file__)})")

from config import GROQ_API_KEY  # required — app won't start without this
if not GROQ_API_KEY:
    sys.exit("FATAL: GROQ_API_KEY is missing from config.py — add it and restart.")

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
        api.write_session_memory(reason="shutdown", end_session=True)
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
    sys.exit(0)


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

    webview.start(api.start)  # api.start() is called once the window is ready
    _teardown("webview stopped")
