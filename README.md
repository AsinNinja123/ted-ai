# Ted

A local, always-listening voice assistant for macOS — Jarvis-style. Ted streams
answers in a chosen voice, lets you talk over him (barge-in), and handles a wide
range of spoken commands: timers, reminders, lists, calendar, notes, email,
Spotify, habits, screen vision, and general questions.

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

## Launching without a terminal

```bash
bash tools/make_app.sh
```

Builds `~/ted-ai/Ted.app` — double-click it, or find it with Spotlight, or drag it
to the Dock. It activates the venv and runs `hud.py` for you. Re-run the script any
time to rebuild.

- First launch asks for **Microphone** access — allow it. Calendar/Notes/Messages
  control will each prompt once too, the first time Ted uses them.
- There's no terminal, so everything Ted prints goes to `data/ted_launch.log`.
  If Ted starts and immediately quits, that file says why (and you'll get a dialog
  with the last few lines).
- Launching twice is a no-op — two Teds would fight over the microphone.

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
  facts, habits, patterns, session summaries. No server to run; it just
  always works. (Replaced the old Neo4j backend, which required Neo4j Desktop
  to be running and usually wasn't.)
- **Knowledge base:** drop PDFs/text into `inbox/` and say "index my documents" to
  make them searchable (ChromaDB + local embeddings).

**Telling Ted to remember something.** Say it either way round — "remember I'm
twenty" or "I'm twenty, remember that". Personal statements (anything with
*I / my / we*) go into the **facts** table, which is injected into every single
prompt, so Ted always knows them. Anything impersonal goes to the knowledge base,
which is searched on demand. Ask "what do you know about me" to see the list, or
"forget everything about me" to clear it.

Facts supersede rather than pile up: for one-answer things (where you live, where
you work, your age) a new value replaces the old one, so Ted can't end up holding
two contradictory answers. When two versions differ only in detail — "Spirit Lake"
vs "Spirit Lake, Iowa" — the more specific one wins.

**Session memories.** When a conversation is worth remembering, Ted writes himself a
short memory of it and can bring it up later — "yesterday you were stuck on that
double-firing webhook". Recent memories are injected into every reply, so callbacks
happen naturally mid-conversation, not just in the greeting.

Most sessions produce **no memory at all**, on purpose. Testing, timers, music and
one-off questions are filtered out twice — first by a cheap word/turn check, then by
the model itself, which is told that declining is the right answer most of the time.
A memory list full of "Charlie set a two minute timer" makes callbacks worse than
having none. Seeing `[memory] shutdown: nothing worth remembering this session` in
the log is the system working.

A memory gets written when you've been quiet for 10 minutes, every 12 exchanges
(so a crash can't lose the session), and on exit — window close, Ctrl-C, or SIGTERM.
All three paths update the *same* row, so one conversation leaves one memory.

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
Spotify, anything you'd say out loud.

## Everyday helpers

- **Quick math:** "total on 3 at 45", "change from a hundred for 67.50",
  "8 percent of 250".
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
│   ├── memory.py          # SQLite long-term memory: exchanges, facts, habits, patterns
│   ├── knowledge.py       # ChromaDB knowledge base + inbox/PDF indexing
│   ├── speaker.py         # voice lock: enroll/verify the owner's voice (opt-in)
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
├── tools/
│   └── make_app.sh        # builds Ted.app (double-clickable launcher + icon)
├── tests/                 # use-case tests — run each with the venv python
│   ├── test_intents.py    # command parsing
│   ├── test_barge.py      # barge-in behaviour
│   ├── test_capture_gates.py
│   ├── test_memory.py
│   ├── test_session_memory.py  # what's worth remembering
│   └── test_pipeline.py
├── ui/
│   ├── ted_hud.html       # the live HUD window (particle sphere) — loaded by paths.py
│   └── ted_hud_legacy.html   # older HUD, kept as a fallback; nothing imports it
├── docs/                  # design notes and debugging handoffs
└── data/                  # Kokoro voice model, local DBs (gitignored, ~340 MB)
```

Everything in `data/` and `venv/` is generated or downloaded — neither is committed,
and both can be rebuilt from scratch. `venv/` alone is ~1.7 GB, so the folder looks
much bigger on disk than the actual project is.

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
