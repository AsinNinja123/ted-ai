"""core/intents.py — Spoken-command parsing: phrase tables + text → intent.

Everything in here is plain text processing: no audio, no LLM calls, no
side effects at import time. That keeps it unit-testable (see tests/) and
importable from anywhere without dragging in the whole runtime.

The one exception is _parse_list_cmd, which executes list operations via
core.assistant — kept here because the parse and the action are one regex.
"""

import re
import time
import difflib

from core import features

try:
    from config import SALES_TAX
except Exception:
    SALES_TAX = 0.0

# ---------- spoken control commands ----------
# Whisper almost never returns a clean one-word "stop" — it tacks on your name,
# filler ("okay…", "please…"), or a trailing word. So every command is matched
# the same forgiving way: normalize the utterance (drop punctuation + filler),
# then accept it if it IS a command word, or if a short utterance STARTS with a
# command phrase. Phrases are normalized the same way so the two always line up.
_FILLER = {
    "okay", "ok", "alright", "alrighty", "well", "so", "um", "uh", "er", "hey",
    "hi", "yo", "yeah", "yep", "please", "ted", "tim", "now", "just", "kindly", "could",
    "would", "can", "will", "you", "your", "a", "an", "the", "to", "i", "im",
    "lets", "let", "go", "get",
}

def _normalize_cmd(text):
    """Strip punctuation and filler words so 'okay Ted, stop it please' → 'stop it'."""
    t = text.lower().replace("’", "").replace("'", "")   # what's -> whats
    t = re.sub(r"[^\w\s]", " ", t)                       # drop all punctuation
    return " ".join(w for w in t.split() if w not in _FILLER)

def _norm_set(*phrases):
    """Pre-normalize all command phrases at import time so _matches() is O(1)."""
    return {_normalize_cmd(p) for p in phrases}

def _matches(text, phrases):
    """Return True if `text` matches a command phrase from `phrases` (normalized set).
    Single-word commands must be an exact match; multi-word commands can have
    at most one trailing filler word ('stop it please' still matches 'stop it').
    For longer batched transcriptions (Whisper groups repeated utterances), also
    checks whether the phrase appears anywhere inside the text."""
    t = _normalize_cmd(text)
    if not t:
        return False
    if t in phrases:                 # exact match (most common path)
        return True
    words = t.split()
    for p in phrases:
        pw = p.split()
        # multi-word command at the front, with at most one trailing word
        if len(pw) >= 2 and words[:len(pw)] == pw and len(words) <= len(pw) + 1:
            return True
        # sliding-window: phrase appears anywhere in a longer batched transcription
        # (only for 2+ word phrases to avoid single-word false positives)
        if len(pw) >= 2 and len(words) > len(pw) + 1:
            for i in range(len(words) - len(pw) + 1):
                if words[i:i + len(pw)] == pw:
                    return True
    return False

# These never reach the LLM — they trigger immediate action.
_STOP_PHRASES = _norm_set(
    "stop", "stop talking", "stop speaking", "stop it", "be quiet", "quiet",
    "silence", "hush", "shush", "shut up", "enough", "thats enough", "pause",
    "hold on", "hang on", "wait", "one moment", "zip it",
)
_CANCEL_PHRASES = _norm_set(
    "never mind", "nevermind", "forget it", "forget that", "forget about it",
    "cancel", "cancel that", "ignore that", "ignore it", "drop it",
    "scratch that", "leave it", "skip it",
)
_REPEAT_PHRASES = _norm_set(
    "repeat", "repeat that", "say that again", "say it again", "come again",
    "one more time", "what did you say", "say again", "pardon",
)
_SLOWER_PHRASES = _norm_set(
    "slow down", "speak slower", "talk slower", "slower", "too fast",
    "youre too fast", "not so fast",
)
_FASTER_PHRASES = _norm_set(
    "speed up", "talk faster", "speak faster", "faster", "too slow",
    "youre too slow", "hurry up",
)
_MUTE_PHRASES = _norm_set(
    "mute", "mute ted", "mute yourself", "mute you", "go mute",
    "turn off mic", "turn off microphone", "stop listening", "silence yourself",
    "shh", "go quiet",
)

def _is_stop_command(text):   return _matches(text, _STOP_PHRASES)
def _is_cancel_command(text):
    t = _normalize_cmd(text)
    # Timer/reminder cancels need their own handler — don't intercept them here
    if any(w in t for w in ("timer", "reminder", "alarm", "scheduled", "schedule")):
        return False
    return _matches(text, _CANCEL_PHRASES)
def _is_repeat_command(text): return _matches(text, _REPEAT_PHRASES)

# ---------- assistant command phrase sets ----------
_BRIEF_PHRASES = _norm_set(
    "morning briefing", "my briefing", "brief me", "whats my day", "whats my day look like",
    "what do i need to know", "catch me up", "fill me in", "the rundown", "rundown",
    "give me the rundown", "whats going on today", "how are we looking",
    "good morning", "morning",
)

_HOLD_PHRASES = _norm_set(
    "hold that thought", "hold that", "save that thought",
)
_RECALL_PHRASES = _norm_set(
    "pick that back up", "what were we talking about", "where were we",
    "go back to that", "continue from before", "resume that",
)

_THINK_ENTER = _norm_set(
    "lets think through something", "help me think", "help me think through",
    "think this through with me", "thinking partner", "think out loud",
)
_THINK_EXIT = _norm_set(
    "stop thinking mode", "done thinking", "exit thinking mode",
    "okay thanks", "thanks ted", "thanks tim", "got it thanks",
    "i think i got it", "that helped",
)

# ---------- wake phrase ----------
# Robust to how Whisper ACTUALLY transcribes "Hey Ted":
#   "Hey Ted, …"  "Hey, Ted. …"  "Hey Tad, …"  "Hated …" (glued!)  "So Ted, …"
#   "Okay, so Ted, …"  and bare "Ted, …" — all at the START of an utterance,
# plus addressing at the END: "what time is it, Ted?".
# "ted" mid-sentence never wakes, and "Ted's"/"teddy" are excluded.
_WAKE_RE = re.compile(
    r"^(?:(?:so|okay|ok|hey|hi|yo|um|uh|well|alright|now|oh)[,!.]?\s+){0,2}"
    r"(?:ted|tad|tedd|hated|heyted)(?!['’\w])[,:.!?]?\s*",
    re.IGNORECASE,
)
_WAKE_TAIL_RE = re.compile(r"[,!?\s](?:ted|tad)(?!['’\w])[.!?]?$", re.IGNORECASE)

def _strip_wake_phrase(text):
    """Detect and strip a 'Hey Ted' prefix or a trailing ', Ted'.
    Returns (stripped_text, was_wake: bool)."""
    t = text.strip()
    m = _WAKE_RE.match(t)
    if m:
        return t[m.end():].strip(), True
    m = _WAKE_TAIL_RE.search(t)
    if m:
        return t[:m.start()].strip().rstrip(",;"), True
    return text, False

# ---------- small-number words ----------
_SMALL_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a": 1, "an": 1, "couple": 2, "few": 3, "dozen": 12,
}
def _word_to_int(w):
    """Convert a spoken quantity word or digit string to an int, or return None."""
    w = w.strip().lower()
    if w.isdigit():
        return int(w)
    return _SMALL_NUM.get(w)

# ---------- reminders ----------
def _parse_reminder(text):
    """Return (task, due_ts_or_None) or None. Handles 'remind me to X in 10 minutes'."""
    m = (re.search(r"\bremind me (?:to |that |about )?(.+)", text, re.I)
         or re.search(r"\b(?:set|add) (?:a )?reminder (?:to |for |about )?(.+)", text, re.I))
    if not m:
        return None
    body = m.group(1).strip().rstrip(".")
    task, due = body, None
    if features.HAS_ASSISTANT:
        assistant = features.assistant
        when = assistant.parse_when(body)                     # "at 5pm" / "tomorrow"
        if when:
            due, task = when, assistant.strip_time_phrase(body)
        else:
            dm = re.search(r"^(.*?)\bin (.+)$", body, re.I)    # "...in <duration>"
            if dm:
                secs = assistant.parse_duration(dm.group(2))
                if secs:
                    task, due = dm.group(1).strip(" ,"), time.time() + secs
    return (task, due) if task else None

# ---------- cash & change calculator (reliable math, not the LLM) ----------
def _money(x):
    x = round(float(x), 2)
    return f"{int(x)} dollars" if x == int(x) else f"{x:.2f} dollars"

def _calc_num(s):
    s = s.replace(",", "").strip()
    if re.match(r"^\d+(\.\d+)?$", s):
        return float(s)
    return _word_to_int(s)

def _parse_calc(text):
    """Handle 'total on 3 at 45', 'change from a hundred for 67.50',
    '8 percent of 250'. Returns spoken text or None."""
    t = text.lower().replace("$", " ").replace("dollars", " ").replace("a hundred", "100")
    t = t.replace("bucks", " ")

    m = re.search(r"change (?:from|for|of)\s+([\d.,]+)(?:.*?(?:for|on|minus|less)\s+([\d.,]+))?", t)
    if m:
        if not m.group(2):
            return None
        change = float(m.group(1).replace(",", "")) - float(m.group(2).replace(",", ""))
        if change < 0:
            return f"That's {_money(-change)} short."
        return f"Change is {_money(change)}."

    m = re.search(r"([\w.,]+)\s+(?:at|times|x)\s+([\d.,]+)", t)
    if m and ("total" in t or " at " in t or "times" in t or " x " in t):
        qty = _calc_num(m.group(1))
        if qty is None:
            return None
        price = float(m.group(2).replace(",", ""))
        subtotal = qty * price
        if "tax" in t and SALES_TAX > 0:
            return (f"{int(qty) if qty == int(qty) else qty} at {_money(price)} is "
                    f"{_money(subtotal)}, or {_money(subtotal * (1 + SALES_TAX))} with tax.")
        return f"That comes to {_money(subtotal)}."

    m = re.search(r"([\d.]+)\s*percent of\s+([\d.,]+)", t)
    if m:
        return f"{_money(float(m.group(1)) / 100 * float(m.group(2).replace(',', '')))}."
    return None

# ---------- Spotify phrase sets + song parsing ----------
_SPOT_NEXT  = _norm_set("next", "skip", "skip this", "skip this song", "skip the song",
                        "next song", "next track", "skip it")
_SPOT_PREV  = _norm_set("previous", "go back a song", "last song", "previous song",
                        "previous track", "play the last song", "go back a track")
_SPOT_PAUSE = _norm_set("pause", "pause the music", "pause music", "pause spotify",
                        "pause the song", "stop the music", "stop the song")
_SPOT_PLAY  = _norm_set("play", "resume", "unpause", "play music", "play the music",
                        "play some music", "play some tunes", "resume music", "keep playing",
                        "resume the music", "resume spotify",
                        "play it again", "play it back", "play it")
_SPOT_NOW   = _norm_set("whats playing", "whats this song", "who sings this",
                        "what song is this", "name this song", "who is this",
                        "what is this song")
# NOTE: "whats on" / "whats this" were removed — too generic. As 2-word phrases they
# matched anywhere in a sentence, so "what's ON my calendar" and "what's THIS assignment"
# were being hijacked into Spotify's now-playing handler before reaching calendar/etc.
_SPOT_UP    = _norm_set("turn it up", "turn up the music", "louder", "volume up", "turn the music up")
_SPOT_DOWN  = _norm_set("turn it down", "turn down the music", "quieter", "volume down", "turn the music down")

def _parse_playlist(text):
    """'play my workout playlist', 'shuffle my chill playlist', 'play playlist road trip'."""
    m = (re.search(r"\b(?:play|put on|start|shuffle)\s+(?:my |the )?(.+?)\s+playlist\b", text, re.I)
         or re.search(r"\bplay (?:my |the )?playlist\s+(.+)$", text, re.I))
    if not m:
        return None
    name = m.group(1).strip()
    return name, bool(re.search(r"\bshuffle\b", text, re.I))

# Single words after "play" that mean "resume" not "search for a song".
# Multi-word transport phrases are handled by _SPOT_PLAY before we get here.
_PLAY_NOT_A_SONG = {
    "it", "that", "this", "again", "on", "off", "along", "nice",
    "cool", "safe", "fair", "ball", "dead", "dumb", "hard", "smart",
    "loud", "soft", "fast", "slow", "straight", "right", "wrong",
}

def _parse_song(text):
    """Parse a spoken play request into (search_query, artist_or_None).
    Handles:
      - 'play the song X [by Y]'  — explicit keyword
      - 'play X by Y'             — artist specified
      - 'play The Great Divide'   — bare title (2+ words)
      - 'play a happy song'       — mood/genre query
    Returns None for pure transport phrases (already caught by _SPOT_PLAY upstream)
    and single ambiguous words like 'it', 'that', 'nice'.
    """
    t = text.strip()

    # Pattern 1: "play the song/track X [by Y]"
    m = re.search(r"\bplay the (?:song|track)\s+(.+?)(?:\s+by\s+(.+))?$", t, re.I)
    if m:
        title = m.group(1).strip()
        artist = (m.group(2) or "").strip() or None
        return (title, artist) if title else None

    # Pattern 2: "play X by Y"
    m = re.search(r"\bplay\s+(.+?)\s+by\s+(.+)$", t, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Pattern 3: bare "play [query]" — title or mood/genre phrase.
    m = re.search(r"^(?:(?:hey |ok |okay )?(?:ted,?\s+))?(?:can you\s+|please\s+)?play\s+(.+)$",
                  t, re.I)
    if m:
        query = m.group(1).strip().rstrip(".,!?")
        if query.lower() in _PLAY_NOT_A_SONG:
            return None
        return (query, None)

    return None

def _parse_ask_claude(text):
    """Return the question after 'ask Claude', '' if none given, or None if not a Claude request."""
    m = re.search(r"\bask claude\b(?:\s+(?:about|regarding|to))?\s*(.*)", text, re.I)
    if not m:
        return None
    return m.group(1).strip(" ,.?!")

# ---------- named lists ----------
def _parse_list_cmd(text):
    """Add/remove/clear/read a named list. Returns spoken text or None."""
    if not features.HAS_ASSISTANT:
        return None
    assistant = features.assistant
    t = text.strip()
    m = (re.search(r"\badd (.+?) to (?:the |my )?(.+?) list", t, re.I)
         or re.search(r"\bput (.+?) on (?:the |my )?(.+?) list", t, re.I))
    if m:
        item, name = m.group(1).strip(), m.group(2).strip()
        assistant.list_add(name, item)
        return f"Added {item} to the {name} list."
    m = (re.search(r"\bremove (.+?) from (?:the |my )?(.+?) list", t, re.I)
         or re.search(r"\btake (.+?) off (?:the |my )?(.+?) list", t, re.I))
    if m:
        item, name = m.group(1).strip(), m.group(2).strip()
        assistant.list_remove(name, item)
        return f"Took {item} off the {name} list."
    m = re.search(r"\b(?:clear|empty|wipe) (?:out )?(?:the |my )?(.+?) list", t, re.I)
    if m:
        name = m.group(1).strip()
        assistant.list_clear(name)
        return f"Cleared the {name} list."
    m = (re.search(r"\b(?:what's|whats|what is) on (?:the |my )?(.+?) list", t, re.I)
         or re.search(r"\bread (?:me )?(?:the |my )?(.+?) list", t, re.I)
         or re.search(r"\b(?:show|check|review) (?:the |my )?(.+?) list", t, re.I))
    if m:
        name = m.group(1).strip()
        items = assistant.list_get(name)
        if not items:
            return f"The {name} list is empty."
        return f"On the {name} list: " + ", ".join(items) + "."
    return None

# ---------- scheduled-item cancellation / timers ----------
def _parse_cancel_scheduled(text):
    """'cancel the timer', 'stop the timer', 'clear my reminders'. Returns
    'timer', 'reminder', 'all', or None."""
    t = _normalize_cmd(text)
    _cancel_verbs = ("cancel", "clear", "delete", "stop", "scratch", "kill",
                     "never mind", "forget", "remove", "turn off", "disable")
    if not any(v in t for v in _cancel_verbs):
        return None
    if "timer" in t:
        return "timer"
    if "reminder" in t or "alarm" in t:
        return "reminder"
    if ("everything" in t or "all" in t) and "scheduled" in t:
        return "all"
    return None

def _is_timer_request(text):
    nt = _normalize_cmd(text)
    if any(v in nt for v in ("set timer", "start timer", "timer for", "countdown for",
                              "set countdown", "start countdown", "minute timer",
                              "second timer", "hour timer", "give timer", "set alarm")):
        return True
    if nt.startswith("timer"):
        return True
    return False

def _is_countdown_request(text):
    t = text.lower()
    if not any(p in t for p in ("days until", "how long until", "how many days", "countdown")):
        return False
    return any(k in t for k in ("fourth", "4th", "july", "new year", "fireworks", "season", "holiday"))

# ---------- chat-panel voice commands ----------
_CHAT_NOUNS = ("chat", "log", "transcript", "history", "chat log", "conversation log")
_SHOW_VERBS = ("open", "show", "pull up", "bring up", "display", "see", "give me", "let me see")
_HIDE_VERBS = ("hide", "close", "dismiss", "put away", "get rid of")

def _chat_command(text):
    """Return 'show', 'hide', 'toggle', or None."""
    t = text.strip().lower().rstrip(".!?,")
    if not any(n in t for n in _CHAT_NOUNS):
        return None
    if len(t.split()) > 6:           # a real sentence that merely mentions "chat"
        return None
    if any(v in t for v in _HIDE_VERBS):
        return "hide"
    if any(v in t for v in _SHOW_VERBS):
        return "show"
    if t in ("chat", "chat log", "the chat", "the log", "log", "transcript"):
        return "toggle"
    return None

# ---------- reminders-panel voice commands ----------
_REMINDERS_NOUNS  = ("reminders", "reminder list", "scheduled", "my schedule", "timers", "timer list", "my reminders")
_REMINDERS_SHOW_V = ("show", "pull up", "open", "see", "check", "view", "list", "what", "give me")
_REMINDERS_HIDE_V = ("hide", "close", "dismiss", "put away")

def _reminders_command(text):
    """Return 'show', 'hide', or None."""
    t = text.strip().lower().rstrip(".!?,")
    if not any(n in t for n in _REMINDERS_NOUNS):
        return None
    if len(t.split()) > 8:
        return None
    if any(v in t for v in _REMINDERS_HIDE_V):
        return "hide"
    if any(v in t for v in _REMINDERS_SHOW_V):
        return "show"
    return None

# ---------- app launcher / closer parsing ----------
_APP_SINGLE_VERBS = {"open", "launch", "start", "run", "get"}
_APP_DOUBLE_VERBS = {"pull up", "bring up", "open up"}
_CLOSE_VERBS      = {"close", "quit", "exit", "kill", "shut", "closed"}

# Phonetic corrections: Whisper commonly mishears these at the start of a command.
_CMD_PHONETIC = {
    # close
    "flows": "close", "blows": "close", "clothes": "close", "clows": "close",
    "close's": "close", "cloth": "close", "glow": "close",
    "closed": "close", "clozed": "close",
    # open
    "opan": "open", "opin": "open",
    # play
    "plays": "play", "played": "play",
    # pause
    "paws": "pause", "pos": "pause",
    # remind / reminder
    "remind": "remind", "reminde": "remind",
    # send
    "scend": "send", "end": "send",
    # set
    "said": "set", "sit": "set",
}

_FILLER_WORDS = {"hey", "ted", "okay", "ok", "um", "uh", "please", "can", "you", "just"}

def _fix_command_words(text):
    """Correct common Whisper phonetic mishearings of command verbs."""
    if not text:
        return text
    words = text.split()
    for i, w in enumerate(words):
        clean = w.lower().rstrip(".,!?")
        if clean in _FILLER_WORDS:
            continue
        if clean in _CMD_PHONETIC:
            words[i] = _CMD_PHONETIC[clean]
        break  # only fix the first non-filler word
    return " ".join(words)


def _split_app_targets(rest):
    """Split 'spotify and chrome' or 'spotify, chrome' into a list of app terms."""
    parts = [p.strip() for p in re.split(r'\band\b|,', rest) if p.strip()]
    return parts or [rest]


def _resolve_app_key(part):
    """Match a spoken app term to an APPS key, tolerating misspellings/mishearings
    ('spotifi' → 'spotify', 'crome' → 'chrome'). Returns the key or None.
    Cutoff is strict (0.8): looser values match unrelated apps ('blender'→'finder')."""
    from core.actions import APPS
    if part in APPS:
        return part
    close = difflib.get_close_matches(part, list(APPS), n=1, cutoff=0.8)
    return close[0] if close else None


def _parse_open_app(text):
    """Return the APPS key if text is a request to open a known app, else None."""
    keys = _parse_open_apps(text)
    return keys[0] if keys else None

def _parse_open_apps(text):
    """Return list of APPS keys for 'open X and Y'. Empty list if no match."""
    from core.actions import APPS
    t = _normalize_cmd(text)
    words = t.split()
    if not words:
        return []
    rest = None
    # Bare app name: "spotify"
    if t in APPS:
        return [t]
    # Single-word verb
    if words[0] in _APP_SINGLE_VERBS and len(words) >= 2:
        rest = " ".join(words[1:])
    # Two-word verb
    elif len(words) >= 3 and " ".join(words[:2]) in _APP_DOUBLE_VERBS:
        rest = " ".join(words[2:])
    if not rest:
        return []
    parts = _split_app_targets(rest)
    keys = [k for k in (_resolve_app_key(p) for p in parts) if k]
    return keys


def _parse_close_app(text):
    """Return a single app name string if text is a close/quit request, else None."""
    keys = _parse_close_apps(text)
    return keys[0] if keys else None

def _parse_close_apps(text):
    """Return list of app name strings to close. Empty list if not a close command.
    Names are passed through as spoken — core.actions.close_app does the fuzzy
    matching against what's actually running."""
    t = _normalize_cmd(text)
    words = t.split()
    if not words or words[0] not in _CLOSE_VERBS:
        return []
    rest = " ".join(words[1:])
    if not rest:
        return []
    return _split_app_targets(rest)


def _resolve_context_app(last_reply):
    """Scan Ted's last reply for a known app name. Used for 'open that / open it'."""
    from core.actions import APPS
    lower = last_reply.lower()
    for key, bundle in APPS.items():
        if key in lower or bundle.lower() in lower:
            return key
    return None

# ---------- command chaining ----------

# Verbs that signal a NEW command when they appear after "and".
# Deliberately excludes ask/tell/say so "message gavin and ask him X" stays one unit.
_CHAIN_VERBS = frozenset({
    "open", "close", "launch", "quit", "exit", "kill", "shut",
    "play", "pause", "stop", "resume", "skip",
    "send", "message", "text",
    "remind", "set", "add", "create",
    "show", "hide", "find", "search",
    "pull", "bring", "check",
    "mute", "unmute",
})

_CHAIN_HARD_SEP = re.compile(
    r'\band\s+also\b|\band\s+then\b|\bafter\s+that\b|\bthen\s+(?=[a-z])',
    re.I
)

def _split_commands(text):
    """Break a compound utterance into individual command strings.
    Splits on hard separators ('and also', 'and then') and on
    'and <action_verb>' where the verb signals a new distinct action."""
    chunks = _CHAIN_HARD_SEP.split(text)
    result = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        words = chunk.split()
        current = []
        i = 0
        while i < len(words):
            w = words[i].lower().rstrip(".,!?")
            if w == "and" and i + 1 < len(words):
                nw = words[i + 1].lower().rstrip(".,!?")
                if nw in _CHAIN_VERBS:
                    seg = " ".join(current).strip()
                    if seg:
                        result.append(seg)
                    current = []
                    i += 1          # skip the "and" — verb becomes first word
                    continue
            current.append(words[i])
            i += 1
        seg = " ".join(current).strip()
        if seg:
            result.append(seg)
    return [r for r in result if r.split()]

# ---------- message command parsing ----------

# Verb prefix that identifies a message command.
_MSG_VERB_RE = re.compile(
    r'\b(?:message|text|send\s+(?:a\s+)?(?:message|text)\s+to)\s+',
    re.I,
)

# Words after "send...to" / "message" / "text" that are NOT contact names —
# if the first captured word is one of these, the transcription probably dropped
# the real name and captured a connector word instead.
_NOT_A_CONTACT = frozenset({
    "the", "a", "an", "that", "this", "these", "those",
    "my", "your", "his", "her", "their", "him", "them",
    "it", "me",
})

# Connectors that separate the contact name from the message content.
_MSG_CONNECTOR_RE = re.compile(
    r'\s+(?:'
    r'that\s+(?:says?|is\s+)'
    r'|saying\s+'
    r'|and\s+(?:say|ask|tell)(?:ing)?(?:\s+(?:him|her|them))?(?:\s+to)?\s+'
    r'|to\s+say\s+'
    r')',
    re.I,
)

def _parse_message_cmd(text):
    """Return (contact_name, instruction_or_None) if text is a message command, else None.

    Handles:
      'message Gavin and ask him if he wants to golf at 5'
      'send a message to Calvin that says hello'
      'text Mom saying I'll be late'
    """
    m = _MSG_VERB_RE.search(text)
    if not m:
        return None
    after_verb = text[m.end():].strip()
    if not after_verb:
        return None

    # Find connector separating name from content
    conn = _MSG_CONNECTOR_RE.search(after_verb)
    if conn:
        contact = after_verb[:conn.start()].strip()
        instruction = after_verb[conn.end():].strip()
    else:
        # No connector — try splitting on bare "and" if present
        bare_and = re.search(r'\s+and\s+', after_verb, re.I)
        if bare_and:
            contact = after_verb[:bare_and.start()].strip()
            instruction = after_verb[bare_and.end():].strip()
        else:
            contact = after_verb.strip()
            instruction = None

    # If the extracted contact starts with a stop-word, the transcription likely
    # dropped the real name — bail so we don't message a nonsense contact.
    first_word = contact.split()[0].lower() if contact.split() else ""
    if first_word in _NOT_A_CONTACT or not contact:
        return None

    # Only take the first 1–3 words as the contact name (guard against runaway capture)
    contact = " ".join(contact.split()[:3])

    return contact, instruction or None

# ---------- which queries actually need a live web search ----------
# Searching every turn made Ted slow and robotic. Only reach for the web when
# the question really needs fresh, external facts.
_WEB_HINTS = (
    "weather", "temperature", "forecast", "rain", "snow", "news", "headline",
    "current", "currently", "latest", "right now", "today's",
    "price", "cost", "stock price", "score", "who is the current",
    "what is the current", "who's the current", "how much is",
    "happening now", "open now", "store hours", "2025", "2026",
)
def _needs_web(text):
    t = text.lower()
    return any(h in t for h in _WEB_HINTS)

# ---------- time string parser for proactive triggers ----------
def _parse_time_to_24h(s):
    """Convert '8am', '7:30pm', '9:00' → 'HH:MM' (24-hour) or None.
    Bare hours 1–7 default to PM; 8–12 default to AM."""
    s = s.strip().lower().replace(" ", "")
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif ap is None:
        if 1 <= h <= 7:
            h += 12   # bare "8am" rule: 1–7 → PM
    return f"{h:02d}:{mins:02d}"

# ---------- skip fact extraction on obvious non-personal turns ----------
def _worth_extracting(text):
    t = text.strip().lower()
    if len(t.split()) < 3:
        return False
    if _needs_web(t):
        return False
    # Only run the (second) fact-extraction LLM call when the user is actually
    # stating something personal — otherwise it burns a Groq call per turn for
    # nothing and adds to rate-limit pressure.
    if not re.search(r"\b(i|i'm|im|my|mine|me|we|our)\b", t):
        return False
    return True

# ---------- voice expressiveness — content-type speed adjustment ----------
def _classify_content_speed(text):
    """Return a speed multiplier for the current response based on query type."""
    t = text.lower()
    if any(k in t for k in (
        "how are you", "how do you feel", "what do you think",
        "i'm struggling", "i'm worried", "i'm sad", "i feel", "my mom", "my dad",
        "my family", "miss", "stressed", "should i", "what would you do",
    )):
        return 0.92          # slower for personal / emotional
    if any(k in t for k in (
        "list", "give me a list", "what are all", "how many", "name all",
        "tell me all", "what's the difference", "summarize",
    )):
        return 1.05          # slightly faster for lists / factual
    return 1.0               # default — no adjustment

# ---------- mood music detection ----------
_MOOD_KEYWORDS = {
    "calm":   ["stressed", "anxious", "worried", "nervous", "overwhelmed", "tense", "calm me"],
    "energy": ["energy", "pump up", "motivated", "hyped", "hype", "energized", "upbeat", "get going"],
    "soft":   ["sad", "down", "blue", "melancholy", "quiet", "something soft", "chill"],
    "focus":  ["focus", "concentrate", "productive", "studying", "working", "need to work"],
}
_MOOD_SEARCH = {
    "calm":   "ambient lo-fi calm instrumental relaxing",
    "energy": "upbeat high energy workout pump up",
    "soft":   "gentle acoustic soft mellow",
    "focus":  "lo-fi study focus instrumental",
}
_MOOD_DESC = {
    "calm":   "something calm to help you unwind",
    "energy": "something upbeat to get you going",
    "soft":   "something gentle and easy",
    "focus":  "something to help you concentrate",
}

def _detect_mood(text):
    """Return mood key ('calm'|'energy'|'soft'|'focus') or None."""
    t = text.lower()
    m = re.search(
        r"\b(?:i'?m|i am|feeling|feel|need something|play something)\s+"
        r"(stressed|anxious|worried|calm|sad|down|focused|energized?|hyped?|"
        r"motivated|melanchol|chill)\b",
        t,
    )
    if m:
        word = m.group(1)
        for mood, kws in _MOOD_KEYWORDS.items():
            if any(k in word for k in kws):
                return mood
    return None

# ---------- pattern topic extractor ----------
_PAT_STOP = {
    "the", "a", "an", "what", "how", "tell", "about", "hey", "ted", "tim",
    "is", "are", "was", "were", "do", "did", "does", "can", "could", "would",
    "should", "please", "my", "your", "me", "just", "some", "that", "this",
}

def _extract_pattern_topic(text):
    """Extract a 1-2 word label for pattern tracking."""
    words = [re.sub(r"[^\w]", "", w).lower() for w in text.split()]
    kws = [w for w in words if len(w) > 3 and w not in _PAT_STOP]
    return " ".join(kws[:2]) if kws else None

# ---------- store sales tally ----------
def _parse_sale(text):
    """Return (qty, product_name) or None. Handles 'I sold 3 Excaliburs',
    'just sold a dozen roman candles', 'log a sale of two artillery shells'."""
    m = (re.search(r"\b(?:i |we )?(?:just )?sold (\w+) (.+)", text, re.I)
         or re.search(r"\blog (?:a )?sale of (\w+) (.+)", text, re.I))
    if not m:
        return None
    qty = _word_to_int(m.group(1))
    name = m.group(2).strip().rstrip(".!?")
    if name.lower().startswith("dozen "):          # "a dozen Roman candles"
        qty = (qty or 1) * 12
        name = name[6:].strip()
    if qty is None or qty <= 0 or not name:
        return None
    return qty, name


_SALES_QUERY_RE = re.compile(
    r"\b(?:how (?:are|were) sales|sales (?:so far|today|update|summary)"
    r"|today'?s sales|what did we sell|close out the day|end of day"
    r"|how much (?:did we|have we) sold?)\b",
    re.I,
)

def _is_sales_query(text):
    return bool(_SALES_QUERY_RE.search(text))


_SALES_UNDO_RE = re.compile(
    r"\b(?:undo|scratch|remove|delete) (?:that|the) (?:last )?sale\b|\bundo (?:the )?last sale\b",
    re.I,
)

def _is_sales_undo(text):
    return bool(_SALES_UNDO_RE.search(text))


# ---------- correction of the last action ----------
# "actually make it 20 minutes", "no, change it to 5pm", "i meant friday".
# Only acted on when a timer/reminder was created moments ago (the caller
# checks recency), so ordinary sentences can't accidentally edit anything.
_CORRECTION_RE = re.compile(
    r"^(?:no,?\s+|wait,?\s+|sorry,?\s+)?(?:actually,?\s+)?"
    r"(?:make (?:it|that)|change (?:it|that)(?:\s+to)?|i meant|set it (?:to|for))\s+(.+)$",
    re.I,
)

def _parse_correction(text):
    """Return the corrected value phrase ('20 minutes', '5pm') or None."""
    m = _CORRECTION_RE.match(text.strip())
    return m.group(1).strip().rstrip(".!?") if m else None

# ---------- confused / unclear response rotation ----------
import random as _random
_CONFUSED_TED = [
    "sorry, I lost you there", "didn't quite catch that", "say that again?",
    "come again?", "I missed that — what was it?",
]
def _confused_reply():
    return _random.choice(_CONFUSED_TED)
