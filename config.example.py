# config.example.py — Ted AI configuration template.
#
# SETUP:  cp config.example.py config.py   then fill in your real values.
# config.py is gitignored so your keys never get committed.

# ── LLM (required) ───────────────────────────────────────────────────────────
GROQ_API_KEY = ""          # required — get one free at https://console.groq.com
USE_GROQ_STT = True        # True = Groq Whisper cloud STT; False = local Whisper

# ── TTS ──────────────────────────────────────────────────────────────────────
# Ted uses Kokoro (local) by default. Set USE_ELEVENLABS = True and add a key
# below to use ElevenLabs cloud TTS instead.
USE_ELEVENLABS = False
ELEVENLABS_API_KEY = ""
ELEVEN_LABS_VOICE_ID = "onwK4e9ZLuTAKqWW03F9"

# ── Attention ────────────────────────────────────────────────────────────────
# After this many seconds without an interaction, Ted goes to STANDBY and only
# "Hey Ted" / "Ted, …" (or typing) re-engages him — so room conversation isn't
# answered. While engaged, just talk. Set 0 to always listen (old behaviour).
ATTENTION_WINDOW = 90

# ── Voice lock (optional — needs `pip install resemblyzer`, a big install) ───
# Say "Ted, learn my voice" once, then set True: Ted ignores voices that
# aren't yours (kids, TV). Raise the threshold if strangers get through,
# lower it if Ted starts ignoring YOU (whispering lowers similarity).
VOICE_LOCK = False
VOICE_LOCK_THRESHOLD = 0.68

# ── Audio ────────────────────────────────────────────────────────────────────
# Voice barge-in (talking over Ted). Without the native AEC engine, Ted's own
# voice can leak through SPEAKERS and interrupt himself. Set False to disable
# voice barge-in unless the native engine is built (use the mute button / typing
# / headphones to interrupt instead). Has no effect when the AEC engine is active.
FALLBACK_VOICE_BARGEIN = True

# ── Personal settings ────────────────────────────────────────────────────────
OWNER_NAME = "Charlie"     # used in greetings: "Good morning, [name]"
WEATHER_LOCATION = ""      # e.g. "Boise, ID" — for weather; auto-detected via IP if blank

# ── Daily briefing (optional) ────────────────────────────────────────────────
DAILY_BRIEFING_TIME = ""   # e.g. "7:30am" — Ted speaks the morning rundown daily

# ── Remote HTTP endpoint (iOS Shortcuts / curl access) ───────────────────────
REMOTE_PORT = 5150         # Ted listens on this port for /ask requests
REMOTE_TOKEN = ""          # set a passphrase to require it on every remote request

# ── Spotify Web API (optional — playlists + song search; needs Premium) ──────
# Create a free app at https://developer.spotify.com/dashboard, add the redirect
# URI below to it, then run:  python authorize_spotify.py
SPOTIFY_CLIENT_ID = ""
SPOTIFY_CLIENT_SECRET = ""
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
