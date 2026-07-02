# Ted

A local, always-listening voice assistant for macOS — Jarvis-style. Ted streams
answers in a chosen voice, lets you talk over him (barge-in), and handles a wide
range of spoken commands: timers, reminders, lists, calendar, notes, email,
Spotify, habits, goals, screen vision, and general questions.

## Setup

```bash
cd ~/ted-ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py     # then fill in your keys (at minimum GROQ_API_KEY)
python hud.py
```

The Ted window opens, calibrates the mic for a second, greets you, and starts
listening. Just talk — no clicking, no wake word required (though "Hey Ted" works).
Typing in the box also works.

## Talk-over (echo cancellation)

Barging in over **speakers** needs hardware echo cancellation from a small native
macOS engine you build once:

```bash
cd ~/ted-ai/native
./build.sh
```

(Needs Apple's Swift toolchain — if `swiftc` is missing, run `xcode-select --install`.)
The first launch after building asks for **microphone permission** — allow it.

- **Engine built**  → `🎧 native engine with echo cancellation — voice barge-in ON`
- **Not built**     → `🔉 sounddevice fallback` (works fully; to interrupt Ted use
  typing, the mute button, or headphones)

## Voice & STT/TTS

- **STT:** Groq Whisper (cloud) by default; set `USE_GROQ_STT = False` for local Whisper.
- **TTS:** Kokoro (local, voice `am_michael`) by default; set `USE_ELEVENLABS = True`
  with a key for ElevenLabs cloud TTS.
- **LLM:** Groq `openai/gpt-oss-120b` for replies + tool calling (auto-falls back
  to `llama-3.3-70b-versatile` when rate-limited), `llama-3.1-8b-instant` for
  fact extraction/summaries, Llama-4-Scout for screen vision.
- **Live info:** questions about today's games, news, prices, schedules, etc. route
  to `groq/compound-mini`, which runs a real web search before answering — so
  "what World Cup games are on today" gets today's actual schedule. Falls back
  to DuckDuckGo + summarisation when compound is rate-limited (free tier limits).
  "Look up X" / "google X" forces a web answer for anything.

## Memory

- **In-session:** recent turns are sent to the model each reply.
- **Long-term:** stored in a local SQLite file (`data/memory.db`) — exchanges,
  facts, goals, habits, patterns, session summaries. No server to run; it just
  always works. (Replaced the old Neo4j backend, which required Neo4j Desktop
  to be running and usually wasn't.)
- **Knowledge base:** drop PDFs/text into `inbox/` and say "index my documents" to
  make them searchable (ChromaDB + local embeddings).

## Optional integrations

- **Email (Outlook IMAP):** run `python setup_email.py` once, then "check my email".
- **Spotify Web API** (playlists + song search, needs Premium): add credentials to
  `config.py`, then run `python authorize_spotify.py`.
- **Remote control:** Ted runs a small HTTP server for iOS Shortcuts/curl.
  Set `REMOTE_TOKEN` in config.py so only you can use it.

## Ask Ted from your iPhone (Siri Shortcut)

1. Shortcuts app → **+** → name it "Ask Ted".
2. Add **Ask for Input** (Text, prompt "What should Ted do?").
3. Add **Get Contents of URL**:
   `http://<your-mac's-local-IP>:5150/ask?token=<REMOTE_TOKEN>&text=[Provided Input]`
   (Method GET; find the Mac's IP in System Settings → Wi-Fi → Details.)
4. Add **Get Dictionary Value** for key `reply`, then **Show Result** (or **Speak Text**).

Now "Hey Siri, Ask Ted" works anywhere on your home Wi-Fi — timers, reminders,
Spotify, sales tallies, anything you'd say out loud.

## Store mode helpers

- **Sales tally:** "I sold three Excaliburs" / "just sold a dozen roman candles"
  logs to `data/sales_log.json`. "How are sales today" / "close out the day"
  gives the running summary; "undo that last sale" scratches a mistake.
- **Cash math:** "total on 3 at 45", "change from a hundred for 67.50"
  (set `SALES_TAX` in config for with-tax totals).
- **Daily briefing:** set `DAILY_BRIEFING_TIME = "7:30am"` in config and Ted
  gives the weather/calendar/reminders rundown every morning unprompted.

## Layout

```
ted-ai/
├── hud.py                 # entry point — run this
├── config.example.py      # copy to config.py and fill in keys
├── authorize_spotify.py   # one-time Spotify OAuth
├── setup_email.py         # one-time Outlook IMAP setup
├── shortcuts.json         # custom voice shortcuts
├── core/
│   ├── app.py             # TedApi — conversation loop, command routing, threads
│   ├── voice.py           # TTS (Kokoro/ElevenLabs), STT capture, audio engine init
│   ├── llm.py             # Groq client, persona, streaming replies, ask-Claude
│   ├── intents.py         # spoken-command parsing (pure — unit-tested in tests/)
│   ├── music.py           # spoken Spotify routing (local app + Web API fallback)
│   ├── tool_handlers.py   # handlers behind the LLM's function-calling tools
│   ├── hud_bridge.py      # Python → JS calls into the HUD webview
│   ├── features.py        # optional-module availability flags
│   ├── paths.py / logs.py # canonical paths, rotating error log
│   ├── audio.py           # audio engine: capture, playback, barge-in (+ fallback)
│   ├── actions.py         # app/website launchers, Spotify transport, contacts/iMessage
│   ├── assistant.py       # reminders, timers, lists, duration/time parsing, weather, location
│   ├── memory.py          # Neo4j long-term memory, facts, goals, habits, patterns (graceful)
│   ├── knowledge.py       # ChromaDB knowledge base + inbox/PDF indexing
│   ├── calendar_app.py    # read/write Calendar.app via AppleScript
│   ├── notes.py           # read/write Apple Notes via AppleScript
│   ├── email.py           # Outlook email via IMAP/SMTP
│   ├── computer.py        # type text, press keys, clipboard
│   ├── screen.py          # screenshot + vision description (Groq)
│   ├── spotify_web.py     # Spotify Web API (playlists, search, device transport)
│   ├── proactive.py       # calendar alerts + user-defined scheduled triggers
│   ├── remote.py          # local HTTP endpoint for remote control
│   └── tools.py           # LLM tool schemas (function-calling)
├── native/
│   ├── ted_audio.swift    # echo-cancelling audio engine
│   └── build.sh           # builds it
├── tests/test_intents.py  # use-case tests for command parsing (run with venv python)
├── ui/ted_hud.html        # the HUD window (particle sphere)
├── ui/ted_interface_v2.html  # previous HUD, kept as fallback
└── data/                  # Kokoro voice model, scratch audio, local DBs
```

## HUD sphere colors

The particle sphere is a live health indicator:

- **Green** — everything working
- **Yellow** — something's wrong (Groq unreachable, or an action just failed —
  the yellow toast at the top says exactly what)
- **Red** — offline: the Python side stopped sending heartbeats (crashed or hung)

Motion shows activity: slow drift when idle, pulse ring while listening, fast
swirl while thinking, amplitude-driven while speaking. The GROQ / MEMORY /
SPOTIFY dots (bottom-left) show individual connections; MEMORY or SPOTIFY
being down doesn't yellow the sphere since both are optional.

## Keys & security

`config.py` holds your API keys and is **gitignored** — it is never committed.
Only `config.example.py` (placeholders) goes to GitHub. If you ever expose a key,
rotate it: keys cannot be un-leaked from git history.
