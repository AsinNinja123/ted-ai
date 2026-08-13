"""core/tool_handlers.py — Handlers behind the LLM's function-calling tools.

Every ACTION handler returns a spoken-style string that is the ground truth of
what happened. The tool loop speaks these verbatim — the LLM never gets to
re-narrate an action, because that's where it invents successes that didn't
happen.
"""

import subprocess
import time
import re
import os

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
    "calendar_add", "notes_add", "clipboard_write",
    "system_volume", "system_brightness", "type_text", "log_habit",
    "email_action", "send_email",
})

# Consequential actions require an explicit user confirmation in a pending
# follow-up flow. Opening apps, typing locally, reminders, and reversible UI
# controls remain immediate; communication and destructive email changes do not.
CONFIRMATION_TOOLS = frozenset({"send_message", "send_email", "email_action"})

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
        if key in WEB_APPS:
            return tool_browse_to(key)
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


_BROWSERS = {
    "brave": "Brave Browser", "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "safari": "Safari", "firefox": "Firefox", "edge": "Microsoft Edge",
    "arc": "Arc", "opera": "Opera",
}

try:
    from config import SITE_BROWSER_PREFERENCES
except Exception:
    # Charlie's explicit default. Keeping it here also makes an existing
    # config.py pick up the preference without requiring a manual edit.
    SITE_BROWSER_PREFERENCES = {"youtube": "Brave"}


def preferred_browser_for(site):
    """Return a configured per-site browser preference, if any."""
    key = (site or "").strip().lower().rstrip("/")
    return (SITE_BROWSER_PREFERENCES or {}).get(key)


def _browser_window_count(app_name):
    """Count real browser windows, ignoring menu-bar and helper surfaces.

    A browser process is not proof that a page opened: Chromium can remain in
    ``--no-startup-window`` mode with no usable window. CoreGraphics gives us
    window ownership and bounds without reading page contents, so this verifies
    the thing the user can actually see while avoiding fragile frontmost-app
    checks (Ted may legitimately retain focus while the tool result renders).
    """
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        count = 0
        for window in windows:
            if window.get(Quartz.kCGWindowOwnerName) != app_name:
                continue
            bounds = window.get(Quartz.kCGWindowBounds) or {}
            width = float(bounds.get("Width", 0) or 0)
            height = float(bounds.get("Height", 0) or 0)
            layer = int(window.get(Quartz.kCGWindowLayer, 0) or 0)
            alpha = float(window.get(Quartz.kCGWindowAlpha, 1) or 0)
            if layer == 0 and alpha > 0 and width >= 240 and height >= 120:
                count += 1
        return count
    except Exception:
        return None


def _open_verified_browser(app_name, url):
    """Send ``url`` to a browser and verify a real browser window exists.

    Brave's AppleScript interface can hang while macOS waits on an Automation
    permission dialog. Launch Services does not need that permission, so use it
    to send the URL. Chromium receives ``--new-window`` directly to recover from
    its common background-only launch mode. Success requires a substantial
    browser window, never merely a process or a change in keyboard focus.
    """
    before = _browser_window_count(app_name)
    try:
        # Chromium can sit in a background-only ``--no-startup-window`` process,
        # in which case Launch Services blocks for a full minute with error
        # -1712. Sending --new-window to the browser executable recovers that
        # state without force-quitting or discarding the user's session.
        executable = f"/Applications/{app_name}.app/Contents/MacOS/{app_name}"
        if app_name in {"Brave Browser", "Google Chrome", "Microsoft Edge"} \
                and os.path.exists(executable):
            subprocess.Popen(
                [executable, "--new-window", url], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            # Activation is separate from URL delivery. It is best-effort: Ted
            # may take focus back while showing the result, which is not failure.
            subprocess.run(
                ["open", "-a", app_name], capture_output=True, text=True, timeout=8,
            )
        else:
            sent = subprocess.run(
                ["open", "-a", app_name, url], capture_output=True, text=True, timeout=8,
            )
            if sent.returncode != 0:
                return False, (sent.stderr or "Launch Services rejected the URL").strip()
            subprocess.run(
                ["open", "-a", app_name], capture_output=True, text=True, timeout=8,
            )
    except Exception as exc:
        return False, str(exc)
    for _ in range(20):
        windows = _browser_window_count(app_name)
        if windows is not None and windows > 0:
            # --new-window normally increases the count. Accept an existing
            # substantial window too: URL delivery may be forwarded to it by a
            # browser that ignores the flag, and it is still visible ground truth.
            change = "new window appeared" if before is not None and windows > before else "window is visible"
            return True, f"{app_name} {change}"
        time.sleep(0.2)
    if _browser_window_count(app_name) == 0:
        return False, (f"{app_name} is running without a browser window; "
                       "quit and reopen it once")
    return False, "macOS window verification was unavailable"


def tool_browse_to(site, browser=None):
    """Open a website, optionally in a SPECIFIC browser ('youtube in Brave').
    Without a browser it uses the old Chrome-then-default path. Verifies the
    open actually succeeded before claiming it did."""
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
    browser = browser or preferred_browser_for(key)
    # Send the URL and verify the requested browser actually appears.
    if browser:
        app_name = _BROWSERS.get(browser.strip().lower(), browser.strip())
        verified, detail = _open_verified_browser(app_name, url)
        if verified:
            return f"Opened {label} in {app_name}."
        return (f"I couldn't verify that {label} opened in {app_name}; "
                f"nothing is being claimed as complete. ({detail})")

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


# ---------- arithmetic ----------
# Standing principle on this project: math in Python, words in the model. The
# model decides WHEN to compute and how to say the answer; it never does the
# computing. This replaces the hand-written math regexes in _assistant_command,
# which only fired on the phrasings someone thought to write down.
import ast as _ast
import operator as _op

_MATH_OPS = {
    _ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul,
    _ast.Div: _op.truediv, _ast.FloorDiv: _op.floordiv, _ast.Mod: _op.mod,
    _ast.Pow: _op.pow, _ast.USub: _op.neg, _ast.UAdd: _op.pos,
}
_MATH_MAX_POW = 1e6      # keep 9**9**9 from hanging the process


def _eval_math(node):
    """Walk a parsed expression with an explicit operator whitelist. Not eval()
    — eval on a string from a model is a remote code execution hole."""
    if isinstance(node, _ast.Expression):
        return _eval_math(node.body)
    if isinstance(node, _ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers")
        return node.value
    if isinstance(node, _ast.UnaryOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_eval_math(node.operand))
    if isinstance(node, _ast.BinOp) and type(node.op) in _MATH_OPS:
        left, right = _eval_math(node.left), _eval_math(node.right)
        if isinstance(node.op, _ast.Pow) and (abs(right) > 64 or abs(left) > _MATH_MAX_POW):
            raise ValueError("exponent too large")
        return _MATH_OPS[type(node.op)](left, right)
    raise ValueError("unsupported expression")


def _format_number(n):
    """Round float noise away (0.1+0.2), keep integers integral, group thousands."""
    if isinstance(n, float):
        r = round(n, 10)
        n = int(r) if r == int(r) else round(r, 4)
    return f"{n:,}" if isinstance(n, int) else f"{n:,}".rstrip("0").rstrip(".")


def tool_calculate(expression):
    expr = (expression or "").strip().lstrip("=").replace("^", "**").replace("\u00d7", "*").replace("\u00f7", "/")
    expr = expr.replace(",", "").replace("$", "").strip()
    if not expr:
        return "I didn't catch what to calculate."
    if len(expr) > 200:
        return "That expression is too long for me to work through."
    try:
        value = _eval_math(_ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        return "That's a divide by zero — no answer to give."
    except Exception:
        return f"I couldn't parse '{expression}' as a calculation."
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return "That doesn't come out to a real number."
    return _format_number(value)
