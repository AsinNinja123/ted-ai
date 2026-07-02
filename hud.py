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
import signal

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
from core import llm
from core.app import TedApi
from core.memory import save_session_summary

print("Ted is ready.")

api = TedApi()


def _shutdown(signum, frame):
    """Clean shutdown: stop audio, write session summary, close Neo4j, exit."""
    print(f"\n[shutdown] signal {signum} — saving state and exiting cleanly")
    try:
        engine.stop_playback()
    except Exception:
        pass
    try:
        summary = llm.generate_session_summary(api.active_conversation)
        if summary:
            save_session_summary(summary)
            print(f"[shutdown] session summary written ({len(summary)} chars)")
    except Exception as e:
        print(f"[shutdown] summary write skipped: {e}")
    try:
        from core.memory import close as _mem_close
        _mem_close()
    except Exception:
        pass
    try:
        engine.close()
    except Exception:
        pass
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ---- Entry point ------------------------------------------------------------
if __name__ == "__main__":
    window = webview.create_window(
        f"Ted {TED_VERSION}",
        UI_HTML,
        js_api=api,       # exposes TedApi methods to the JS side as window.pywebview.api.*
        width=1100,
        height=720,
        min_size=(760, 560),
        background_color="#0A0E14",
    )
    api.window = window
    webview.start(api.start)  # api.start() is called once the window is ready
