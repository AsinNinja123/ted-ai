"""core/tool_handlers.py — Handlers behind the LLM's function-calling tools.

Every ACTION handler returns a spoken-style string that is the ground truth of
what happened. The tool loop speaks these verbatim — the LLM never gets to
re-narrate an action, because that's where it invents successes that didn't
happen.
"""

import subprocess
import time

from core import features
from core.actions import APPS, WEB_APPS, open_app, spotify_command

try:
    from config import WEATHER_LOCATION
except Exception:
    WEATHER_LOCATION = ""   # auto-detected via IP if blank


# Tools that CHANGE something (side effects). Their handler return value is the
# ground truth and must be spoken verbatim.
ACTION_TOOLS = frozenset({
    "open_app", "close_app", "browse_to", "play_music", "play_playlist",
    "spotify_control", "send_message", "set_reminder", "set_timer",
    "list_add", "calendar_add", "notes_add", "clipboard_write",
    "system_volume", "system_brightness", "type_text", "log_habit",
    "email_action", "send_email",
})

# Phrases the handlers use when an action did NOT succeed. Lets the HUD surface the
# real problem (yellow sphere / issue popup) instead of pretending everything's fine.
_FAILURE_MARKERS = (
    "couldn't", "could not", "can't", "cannot", "isn't open", "is not open",
    "not installed", "unavailable", "didn't catch", "couldn't find",
    "couldn't parse", "couldn't reach", "no app", "don't have", "failed",
    "didn't go through", "still open", "didn't work", "isn't set",
)
def looks_like_failure(result):
    r = (result or "").lower()
    return any(m in r for m in _FAILURE_MARKERS)


# Utterances likely to be action commands vs. conversation — used to skip the
# tool-calling LLM round-trip for pure conversational messages.
_TOOL_VERBS = frozenset({
    "open", "launch", "close", "quit", "kill", "exit",
    "play", "pause", "skip", "next", "previous", "stop music",
    "send", "text", "message", "email",
    "remind", "reminder", "timer", "set a timer", "set timer",
    "check my email", "read email", "delete email", "flag email",
    "weather", "go to", "browse", "navigate",
    "mute spotify", "volume",
    # calendar — specific multi-word phrases only
    "on my calendar", "add a meeting", "add an event", "schedule a meeting",
    # notes — command phrases only, not the word "note"
    "make a note", "write a note", "add a note",
    # clipboard — specific enough to not appear in conversation
    "clipboard",
    # system controls — multi-word, unambiguous
    "system volume", "screen brightness",
    # inbox — specific enough
    "index my documents", "scan my inbox",
})

def likely_command(text):
    """Return True if the utterance is likely an action command vs. conversation."""
    t = text.lower()
    return any(v in t for v in _TOOL_VERBS)


# ── App tools ─────────────────────────────────────────────────────────────────

def tool_find_app_key(name):
    """Fuzzy-match a natural-language app name to an APPS or WEB_APPS dict key."""
    n = name.lower().strip()
    all_keys = {**APPS, **WEB_APPS}
    if n in all_keys:
        return n
    for key in all_keys:
        if key in n or n in key:
            return key
    n_words = set(n.split())
    best, best_score = None, 0
    for key in all_keys:
        score = len(set(key.split()) & n_words)
        if score > best_score:
            best, best_score = key, score
    return best if best_score > 0 else None


def tool_open_app(name):
    key = tool_find_app_key(name)
    if key:
        return open_app(key)
    # Not in APPS — delegate to open_app which tries WEB_APPS then best-effort
    return open_app(name)


def tool_spotify_control(action):
    # NOTE: keys must map to actions spotify_command actually understands —
    # "volume_up" used to map to "volume up", which spotify_command didn't
    # recognise, so Ted said "Done." while doing nothing.
    _map = {
        "play": "play", "pause": "pause",
        "next": "next", "previous": "previous",
        "volume_up": "up", "volume_down": "down",
    }
    cmd = _map.get(action, action)
    result = spotify_command(cmd)
    if result is None:
        return f"I don't have a Spotify action called '{action}'."
    return result


def tool_browse_to(site):
    """Open a website in Chrome, resolving known service names to canonical URLs.
    Verifies the AppleScript actually succeeded; falls back to the default
    browser before admitting defeat."""
    from core.actions import SITE_URLS
    key = site.strip().lower().rstrip("/")
    if key in SITE_URLS:
        url = SITE_URLS[key]
        label = key.title()
    else:
        url = site.strip()
        if not url.startswith(("http://", "https://")):
            domain = url.lower().replace(" ", "")
            if "." not in domain:
                domain += ".com"
            url = f"https://{domain}"
        label = url.replace("https://", "").replace("http://", "").split("/")[0]
    safe = url.replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
        '    activate\n'
        f'    open location "{safe}"\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=8)
        if r.returncode == 0:
            return f"Opening {label}."
    except Exception:
        pass
    # Chrome unavailable — try the default browser
    try:
        r = subprocess.run(["open", url], capture_output=True, timeout=8)
        if r.returncode == 0:
            return f"Opening {label} in your default browser."
    except Exception:
        pass
    return f"I couldn't open {label}."


# ── Reminder / timer / list tools ─────────────────────────────────────────────

def tool_set_reminder(text, when_phrase):
    assistant = features.assistant
    ts = assistant.parse_when(when_phrase)
    if ts is None:
        dur = assistant.parse_duration(when_phrase)
        ts = (time.time() + dur) if dur else None
    if not ts:
        return f"I couldn't make sense of '{when_phrase}' as a time — the reminder isn't set. Try 'at 5pm' or 'in 20 minutes'."
    clean = assistant.strip_time_phrase(text).rstrip(".")
    assistant.add_reminder(f"Reminder — {clean}.", ts)
    return f"Reminder set for {time.strftime('%-I:%M %p', time.localtime(ts))}."


def tool_set_timer(duration_phrase):
    assistant = features.assistant
    secs = assistant.parse_duration(duration_phrase)
    if not secs:
        return "I didn't catch the duration — try something like 'ten minutes'."
    label = assistant.human_duration(secs)
    assistant.add_reminder(f"Time's up — your {label} timer is done.", time.time() + secs, kind="timer")
    return f"Timer set for {label}."


def tool_get_reminders():
    assistant = features.assistant
    pend = assistant.pending_reminders()
    d = assistant._load()
    timers = [r for r in d["reminders"]
              if not r["done"] and r.get("kind") == "timer" and r.get("due")]
    if not pend and not timers:
        return "Nothing on your schedule."
    parts = []
    for r in pend:
        label = r["text"].replace("Reminder — ", "").rstrip(".")
        ts = r.get("due")
        when = time.strftime("%-I:%M %p", time.localtime(ts)) if ts else "standing"
        parts.append(f"{label} at {when}")
    for r in timers:
        secs_left = max(0, r["due"] - time.time())
        m, s = int(secs_left) // 60, int(secs_left) % 60
        label = r["text"].replace("Time's up — your ", "").replace(" timer is done.", "")
        parts.append(f"{label} timer with {m}:{s:02d} left")
    return ("You've got " + (parts[0] if len(parts) == 1
            else ", ".join(parts[:-1]) + f", and {parts[-1]}") + ".")


def tool_list_add(list_name, item):
    items = features.assistant.list_add(list_name, item)
    return f"Added {item} to your {list_name} list. {len(items)} item{'s' if len(items) != 1 else ''} on it now."


def tool_list_get(list_name):
    items = features.assistant.list_get(list_name)
    if not items:
        return f"Your {list_name} list is empty."
    if len(items) == 1:
        return f"Just {items[0]} on your {list_name} list."
    return f"Your {list_name} list: {', '.join(items[:-1])}, and {items[-1]}."


def tool_get_weather():
    w = features.assistant.get_weather(WEATHER_LOCATION)
    if not w:
        return "Couldn't get the weather right now."
    # Open-Meteo returns a multi-part string ("clear skies, 72 degrees. high of 80, low of 65.")
    # wttr fallback returns a single phrase ("Sunny and 72 degrees")
    return f"Right now it's {w}."


# ── Email tools ───────────────────────────────────────────────────────────────

def tool_get_emails(limit=5):
    from core.email import get_inbox, is_connected
    if not is_connected():
        return "Email isn't connected yet. Say 'connect my email' and I'll walk you through it."
    limit = min(int(limit), 10)
    try:
        emails = get_inbox(limit)
    except Exception as e:
        return f"Couldn't fetch emails: {e}"
    if not emails:
        return "Your inbox appears to be empty."
    unread = sum(1 for e in emails if not e["read"])
    lines = []
    for e in emails:
        marker = "" if e["read"] else "• "
        lines.append(f"{marker}{e['index']}. {e['sender_name']} — {e['subject']}")
    header = f"You have {unread} unread. " if unread else "All read. "
    return header + " ".join(lines)


def tool_read_email(number, mode="summarized"):
    from core.email import get_email_body, get_cached_email
    from core.llm import summarize_email_body
    meta = get_cached_email(number)
    body = get_email_body(number)
    if not body:
        return f"Couldn't read email {number}."
    sender = meta["sender_name"] if meta else "Unknown"
    subject = meta["subject"] if meta else ""
    if mode == "full":
        content = body[:1000]  # cap spoken length
    else:
        content = summarize_email_body(body, sender)
    return f"Email {number} from {sender}, subject: {subject}. {content}"


def tool_email_action(number, action, reply_text=None):
    from core.email import delete_email, flag_email, mark_read, reply_to_email
    if action == "delete":
        return delete_email(number)
    if action == "flag":
        return flag_email(number)
    if action == "mark_read":
        return mark_read(number)
    if action == "reply" and reply_text:
        return reply_to_email(number, reply_text)
    return f"I don't have an email action called '{action}'."


def tool_send_email_composed(to, subject, instruction, style):
    from core.email import send_email
    from core.llm import generate_email_body
    body = generate_email_body(instruction, to, subject, style)
    return send_email(to, subject, body)
