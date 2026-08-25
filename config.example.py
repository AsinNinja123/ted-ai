# config.example.py — Ted AI configuration template.
#
# SETUP:  cp config.example.py config.py   then fill in your real values.
# config.py is gitignored so your keys never get committed.

# ── LLM ──────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = ""        # optional; when set, GPT-5.6 Luna is tried first
PRIMARY_CHAT_MODEL = "gpt-5.6-luna"
GROQ_API_KEY = ""          # optional free hosted brain; local Ollama works without it
CLOUD_CHAT_MODEL = "qwen/qwen3.6-27b"  # Groq fallback
LOCAL_CHAT_MODEL = "qwen3.5:9b-q4_K_M"
LOCAL_TOOL_MODEL = "qwen3.5:35b-a3b"
# One-word "does this need the cloud?" verdicts only, never an answer to you.
# Keep it small: asking has to be cheaper than guessing wrong. ~0.2s here.
LOCAL_ROUTER_MODEL = "llama3.2:3b"
OLLAMA_URL = "http://127.0.0.1:11434"
SITE_BROWSER_PREFERENCES = {"youtube": "Brave"}
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
REMOTE_TOKEN = ""          # required for LAN/iPhone access; blank disables the server

# ── Spotify Web API (optional — playlists + song search; needs Premium) ──────
# Create a free app at https://developer.spotify.com/dashboard, add the redirect
# URI below to it, then run:  python authorize_spotify.py
SPOTIFY_CLIENT_ID = ""
SPOTIFY_CLIENT_SECRET = ""
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"

# How close a knowledge-base chunk has to be before it is worth spending tokens
# on. Cosine distance: 0 is identical, 2 is opposite. A vector store always has
# a nearest neighbour, so without a cutoff every question — including "how are
# you" — pulled in the closest four chunks whether or not they were related.
# Raise it to retrieve more eagerly, lower it to retrieve only strong matches.
KNOWLEDGE_MAX_DISTANCE = 0.45
