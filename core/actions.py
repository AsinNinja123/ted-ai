
# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 24 (§24.1)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Ted's hands for the everyday things: opening and closing applications,
#      opening a URL in a specific browser, instant Spotify transport (play, pause,
#      skip), looking up a contact, and sending an iMessage.
#
#      Nearly everything here works by asking macOS to do something through
#      AppleScript — a scripting language Apple built into the system that lets one
#      program tell another program what to do. Ted writes a short AppleScript,
#      hands it to the `osascript` command, and reads what comes back.
#
#  THE APPS DICTIONARY
#      `APPS` maps the words you say to the real application names macOS expects.
#      "vs code" -> "Visual Studio Code". When Ted cannot open something you asked
#      for by name, this dictionary is the first place to look, and adding a line to
#      it is usually the whole fix.
#
#  WHY ACTIONS RETURN SENTENCES
#      Every function here returns the text Ted will say. Not a boolean, not a
#      status code — the sentence itself. That is deliberate: it makes it hard to
#      throw away what really happened, which is the honesty rule (§11.8).
#
#  IF YOU WANT TO CHANGE SOMETHING
#      'Ted cannot open X'          -> add it to APPS.
#      'Ted opens the wrong browser' -> browse_to takes an optional browser argument;
#                                       check the tool schema is passing it.
#
# =============================================================================
import subprocess
import difflib
import time
import re as _re
from datetime import date, datetime

# ---- App Name Mapping ----

# Maps lowercase spoken/typed app names to their macOS .app bundle names.
# Used by open_app() to resolve "open spotify" → `open -a Spotify`.
APPS = {
    "spotify": "Spotify",
    "safari": "Safari",
    "chrome": "Google Chrome",
    "google": "Google Chrome",
    "google chrome": "Google Chrome",
    "browser": "Google Chrome",
    "brave": "Brave Browser",
    "firefox": "Firefox",
    "messages": "Messages",
    "facetime": "FaceTime",
    "calendar": "Calendar",
    "my calendar": "Calendar",
    "notes": "Notes",
    "my notes": "Notes",
    "mail": "Mail",
    "apple mail": "Mail",
    "maps": "Maps",
    "photos": "Photos",
    "podcasts": "Podcasts",
    "terminal": "Terminal",
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "finder": "Finder",
    "settings": "System Settings",
    "system preferences": "System Preferences",
    "system settings": "System Settings",
    "preferences": "System Settings",
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "chat gpt": "ChatGPT",
    "gpt": "ChatGPT",
    "openai": "ChatGPT",
    "slack": "Slack",
    "discord": "Discord",
    "zoom": "Zoom",
    "teams": "Microsoft Teams",
    "microsoft teams": "Microsoft Teams",
    "word": "Microsoft Word",
    "microsoft word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "microsoft excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
    "microsoft powerpoint": "Microsoft PowerPoint",
    # Outlook entries removed — not installed; handled via WEB_APPS → outlook.office.com
    "notion": "Notion",
    "figma": "Figma",
    "sketch": "Sketch",
    "xcode": "Xcode",
    "textedit": "TextEdit",
    "preview": "Preview",
    "quicktime": "QuickTime Player",
    "quicktime player": "QuickTime Player",
    "activity monitor": "Activity Monitor",
    "arc": "Arc",
    "brave": "Brave Browser",
    "iterm": "iTerm",
    "iterm2": "iTerm2",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "signal": "Signal",
    "imessage": "Messages",
    "numbers": "Numbers",
    "pages": "Pages",
    "keynote": "Keynote",
    "reminders": "Reminders",
    "shortcuts": "Shortcuts",
    "music": "Music",
    "apple music": "Music",
    "tv": "TV",
    "apple tv": "TV",
    "books": "Books",
    "voice memos": "Voice Memos",
    "stocks": "Stocks",
    "news": "News",
    "weather": "Weather",
    # Installed on this Mac but previously unresolvable, so Ted could neither
    # open them nor answer "is Contacts open?" while Contacts sat in the very
    # app list he was reading from. Audited against /Applications and
    # /System/Applications rather than guessed.
    "contacts": "Contacts",
    "address book": "Contacts",
    "my contacts": "Contacts",
    "calculator": "Calculator",
    "app store": "App Store",
    "freeform": "Freeform",
    "home": "Home",
    "passwords": "Passwords",
    "phone": "Phone",
    "journal": "Journal",
    "clock": "Clock",
    "dictionary": "Dictionary",
    "find my": "FindMy",
    "findmy": "FindMy",
    "stickies": "Stickies",
    "photo booth": "Photo Booth",
    "iphone mirroring": "iPhone Mirroring",
    "mirror my phone": "iPhone Mirroring",
    "steam": "Steam",
    "automator": "Automator",
    "disk utility": "Disk Utility",
    "console": "Console",
    "screenshot": "Screenshot",
    "script editor": "Script Editor",
    "system information": "System Information",
    "font book": "Font Book",
    "image capture": "Image Capture",
    "time machine": "Time Machine",
    # Charlie's own tooling.
    "db browser": "DB Browser for SQLite",
    "db browser for sqlite": "DB Browser for SQLite",
    "sqlite browser": "DB Browser for SQLite",
    "neo4j": "Neo4j Desktop 2",
    "neo4j desktop": "Neo4j Desktop 2",
}

# Web services that have no macOS app — "open YouTube" opens the URL in Chrome
# instead of failing with "I don't have YouTube in my app list."
WEB_APPS = {
    "outlook":          "https://outlook.office.com/mail/inbox",
    "microsoft outlook":"https://outlook.office.com/mail/inbox",
    "my outlook":       "https://outlook.office.com/mail/inbox",
    "email":            "https://outlook.office.com/mail/inbox",
    "my email":         "https://outlook.office.com/mail/inbox",
    "youtube":          "https://youtube.com",
    "gmail":            "https://mail.google.com",
    "google mail":      "https://mail.google.com",
    "drive":            "https://drive.google.com",
    "google drive":     "https://drive.google.com",
    "docs":             "https://docs.google.com",
    "google docs":      "https://docs.google.com",
    "sheets":           "https://sheets.google.com",
    "google sheets":    "https://sheets.google.com",
    "reddit":           "https://reddit.com",
    "twitter":          "https://x.com",
    "x":            "https://x.com",
    "linkedin":     "https://linkedin.com/feed",
    "netflix":      "https://netflix.com",
    "hulu":         "https://hulu.com",
    "amazon":       "https://amazon.com",
    "amazon prime": "https://amazon.com/prime",
    "github":       "https://github.com",
}

# Canonical URLs for "go to X" / "browse to X" — overrides the dumb ".com" fallback
# so Ted opens the right page instead of a sign-in landing page.
SITE_URLS = {
    "blackboard":       "https://nwciowa.blackboard.com/ultra/course",
    "outlook":          "https://outlook.office.com/mail/inbox",
    "outlook mail":     "https://outlook.office.com/mail/inbox",
    "my outlook":       "https://outlook.office.com/mail/inbox",
    "gmail":            "https://mail.google.com",
    "google mail":      "https://mail.google.com",
    "drive":            "https://drive.google.com",
    "google drive":     "https://drive.google.com",
    "docs":             "https://docs.google.com",
    "google docs":      "https://docs.google.com",
    "sheets":           "https://sheets.google.com",
    "google sheets":    "https://sheets.google.com",
    "youtube":          "https://youtube.com",
    "reddit":           "https://reddit.com",
    "twitter":          "https://x.com",
    "x":                "https://x.com",
    "linkedin":         "https://linkedin.com/feed",
    "github":           "https://github.com",
    "netflix":          "https://netflix.com",
    "hulu":             "https://hulu.com",
    "amazon":           "https://amazon.com",
    "slack":            "https://app.slack.com",
    "notion":           "https://notion.so",
    "figma":            "https://figma.com",
    "teams":            "https://teams.microsoft.com",
    "google calendar":  "https://calendar.google.com",
    "google":           "https://google.com",
}

# ---- Date/Time Detection ----

_TIME_Q = _re.compile(
    r"\b("
    r"what('s| is)\s+(the\s+)?time"
    r"|what\s+time\s+is\s+it"
    r"|current\s+time"
    r"|time\s+is\s+it"
    r"|do you have the time"
    r")\b",
    _re.I,
)

_DATE_Q = _re.compile(
    r"\b("
    r"what('s| is)\s+(today'?s?\s+)?(date|day)"
    r"|what\s+day\s+(is\s+)?(it|today)"
    r"|today'?s\s+date"
    r"|what\s+is\s+today"
    r")\b",
    _re.I,
)


def answer_date_question(user_input):
    """Return a spoken time/date string for explicit time or date questions, else None."""
    wants_time = bool(_TIME_Q.search(user_input))
    wants_date = bool(_DATE_Q.search(user_input))
    if not wants_time and not wants_date:
        return None
    parts = []
    if wants_time:
        t = datetime.now().strftime("%-I:%M %p")   # "3:45 PM"
        parts.append(f"It's {t}")
    if wants_date:
        d = date.today().strftime("%A, %B %d, %Y")
        parts.append(f"today is {d}" if wants_time else f"Today is {d}")
    return ". ".join(parts) + "."


# ---- App / Website Launchers ----

def open_app(app_name):
    """Launch a macOS app by spoken name. Falls back to WEB_APPS for web-only services.
    VERIFIES the launch actually succeeded (`open -a` exits non-zero when the app
    can't be found) so Ted never claims to have opened something that didn't open."""
    key = app_name.lower().strip()
    if key in APPS:
        bundle = APPS[key]
        try:
            r = subprocess.run(["open", "-a", bundle], capture_output=True, timeout=8)
        except Exception:
            return f"I couldn't open {bundle}."
        if r.returncode != 0:
            return f"I couldn't open {bundle} — it may not be installed."
        # ``open`` returning zero only means Launch Services accepted the
        # request. Verify that the process actually appears before claiming
        # success; permissions and damaged app bundles can otherwise produce a
        # convincing lie.
        for _ in range(10):
            if any(bundle.lower() == name.lower() for name in get_running_apps()):
                return f"Opened {bundle}."
            time.sleep(0.2)
        return f"macOS accepted the request, but I couldn't verify that {bundle} opened."
    if key in WEB_APPS:
        try:
            r = subprocess.run(["open", WEB_APPS[key]], capture_output=True, timeout=8)
        except Exception:
            return f"I couldn't open {key.title()}."
        return (f"I sent {key.title()} to your default browser, but can't verify the page."
                if r.returncode == 0 else f"I couldn't open {key.title()} in your browser.")
    # Last-ditch: let macOS try to resolve the name itself. `open -a` exits non-zero
    # when the app doesn't exist, so check before confirming we launched anything —
    # otherwise a misheard word makes Ted claim it opened an app that isn't there.
    try:
        r = subprocess.run(["open", "-a", app_name], capture_output=True, timeout=8)
    except Exception:
        return f"I couldn't open {app_name}."
    if r.returncode != 0:
        return f"I couldn't find an app called {app_name}."
    for _ in range(10):
        if any(app_name.lower() == name.lower() for name in get_running_apps()):
            return f"Opened {app_name}."
        time.sleep(0.2)
    return f"macOS accepted the request, but I couldn't verify that {app_name} opened."


# Spoken references to "whatever app I'm looking at" — resolved to the frontmost app.
_THIS_APP_WORDS = {
    "this app", "that app", "the app", "this", "that", "it", "app",
    "current app", "the current app", "this one", "this window", "the window",
    "this program", "that program",
}

# Processes that are Ted himself (or his host) — never quit these via "close this app".
# This was found emptied to {""} in the working tree on 2026-08-22, which protects
# nothing: every membership test below fails, so "close this app" could quit Ted,
# his Terminal, or the Python running him. Restored, and covered by a test now.
# MacAgent.DEFAULT_PROTECTED_APPS is a second, independent guard — clean_up closes
# everything in one shot with no per-app judgment, so it does not rely on this one.
_SELF_PROCESSES = {"python", "python3", "ted", "terminal"}


def get_frontmost_app():
    """Return the name of the frontmost (focused) app, or '' on failure."""
    return _osa('tell application "System Events" to get name of first application '
                'process whose frontmost is true')


def match_running_app(name, running=None):
    """Resolve a spoken/misspelled app name to an actually-running app name.

    Tries, in order: exact match, APPS alias → bundle, substring either way,
    word overlap for multi-word names, then difflib fuzzy match (catches Whisper
    mishearings like 'spotifi'/'crome'). The fuzzy cutoff is deliberately strict
    (0.8) — at 0.6, 'blender' matched 'Finder' and Ted closed the wrong app.
    Returns None when nothing plausible is running."""
    if running is None:
        running = get_running_apps()
    n = name.lower().strip()
    if not n or not running:
        return None
    lower_map = {a.lower(): a for a in running}
    if n in lower_map:
        return lower_map[n]
    bundle = APPS.get(n)
    if bundle and bundle.lower() in lower_map:
        return lower_map[bundle.lower()]
    for lo, orig in lower_map.items():
        if n in lo or lo in n:
            return orig
    # Word overlap: "studio code" → "Visual Studio Code"
    n_words = set(n.split())
    best, best_score = None, 0
    for lo, orig in lower_map.items():
        score = len(set(lo.split()) & n_words)
        if score > best_score:
            best, best_score = orig, score
    if best:
        return best
    # Fuzzy against running app names: "spotifi" → "Spotify"
    close = difflib.get_close_matches(n, list(lower_map), n=1, cutoff=0.8)
    if close:
        return lower_map[close[0]]
    # Fuzzy against APPS aliases: "crome" → "chrome" → Google Chrome (if running)
    close = difflib.get_close_matches(n, list(APPS), n=1, cutoff=0.8)
    if close:
        bundle = APPS[close[0]]
        if bundle.lower() in lower_map:
            return lower_map[bundle.lower()]
    return None


def close_app(app_name):
    """Quit a macOS app by spoken name, then VERIFY it actually quit before
    confirming — Ted never claims 'closed' for something that's still running.

    Handles 'close this app' (frontmost app) and fuzzy/misheard names by
    matching against the list of apps that are actually running."""
    name = app_name.lower().strip()
    running = get_running_apps()

    if name in _THIS_APP_WORDS:
        target = get_frontmost_app()
        if not target or target.lower() in _SELF_PROCESSES:
            return "I couldn't tell which app you mean — say its name."
    else:
        target = match_running_app(name, running)

    if not target:
        # Nothing running matches — report honestly, using the canonical name if known
        bundle = APPS.get(name, app_name.strip().title())
        return f"{bundle} isn't open."
    if target.lower() in _SELF_PROCESSES:
        return "That's me — I'll stay running."

    try:
        _osa(f'tell application "{target}" to quit')
    except Exception:
        return f"Couldn't close {target}."
    # Verify: poll up to ~3s for the app to leave the running list
    for _ in range(6):
        time.sleep(0.5)
        if target not in get_running_apps():
            return f"Closed {target}."
    return f"I asked {target} to quit, but it's still open — it may be waiting on a save dialog."


def get_running_apps():
    """Return a list of visible (non-background) running app names."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process'
             ' whose background only is false'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return [a.strip() for a in result.stdout.strip().split(",") if a.strip()]
    except Exception:
        pass
    return []


def open_website(url):
    """Open a URL in the default browser. Resolves known service names to canonical URLs.
    Verifies the `open` call succeeded so Ted never claims to open a broken URL."""
    key = url.lower().strip().rstrip("/")
    if key in SITE_URLS:
        url = SITE_URLS[key]
    elif not url.startswith("http"):
        # No dot → bare name → .com fallback; has dot → treat as domain
        if "." not in url:
            url = "https://" + url + ".com"
        else:
            url = "https://" + url
    label = key.title() if key in SITE_URLS else url
    try:
        r = subprocess.run(["open", url], capture_output=True, timeout=8)
    except Exception:
        return f"I couldn't open {label}."
    return f"Opening {label}." if r.returncode == 0 else f"I couldn't open {label}."


def search_contacts(query):
    """Search macOS Contacts.app for people whose name contains query (case-insensitive).
    Returns a list of (full_name, phone_or_email) tuples — up to 6 results."""
    safe_q = query.strip().replace('"', '\\"')
    # AppleScript string comparisons are case-insensitive by default.
    # We return lines of "Name|address" joined by newlines.
    script = (
        'tell application "Contacts"\n'
        f'    set found to every person whose name contains "{safe_q}"\n'
        '    set out to ""\n'
        '    repeat with p in found\n'
        '        set pn to name of p\n'
        '        set pa to ""\n'
        '        try\n'
        '            set pa to value of first phone of p\n'
        '        end try\n'
        '        if pa is "" then\n'
        '            try\n'
        '                set pa to value of first email of p\n'
        '            end try\n'
        '        end if\n'
        '        set out to out & pn & "|" & pa & "\n"\n'
        '    end repeat\n'
        '    return out\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
        raw = result.stdout.strip()
    except Exception:
        return []
    contacts = []
    for line in raw.splitlines():
        line = line.strip()
        if "|" in line:
            name, addr = line.split("|", 1)
            name, addr = name.strip(), addr.strip()
            if name:
                contacts.append((name, addr))
    return contacts[:6]


def send_imessage_to_address(address, message_text):
    """Send an iMessage to a phone number or email address via Messages.app."""
    safe_msg = message_text.replace("\\", "\\\\").replace('"', '\\"')
    safe_addr = address.strip().replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '    set theService to 1st service whose service type = iMessage\n'
        f'    send "{safe_msg}" to participant "{safe_addr}" of theService\n'
        'end tell'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def send_imessage(contact, message_text):
    """Legacy: send iMessage by buddy name. Prefer search_contacts + send_imessage_to_address."""
    safe_msg = message_text.replace("\\", "\\\\").replace('"', '\\"')
    safe_contact = contact.strip().title().replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '    set theService to 1st service whose service type = iMessage\n'
        f'    set theBuddy to buddy "{safe_contact}" of theService\n'
        f'    send "{safe_msg}" to theBuddy\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ---- Spotify Control ----

def _osa(script):
    """Run a tiny AppleScript and return its stdout (or '' on any failure)."""
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception as e:
        print("osascript error:", e)
        return ""


def spotify_is_running():
    """True when the Spotify desktop app is running."""
    return _osa('tell application "System Events" to (name of processes) '
                'contains "Spotify"') == "true"


def ensure_spotify_open(timeout=10.0):
    """Launch the Spotify desktop app if it isn't running and wait until the
    process is up. Returns True when Spotify is running (already or newly)."""
    if spotify_is_running():
        return True
    try:
        r = subprocess.run(["open", "-a", "Spotify"], capture_output=True, timeout=8)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if spotify_is_running():
            time.sleep(1.0)   # give it a beat to finish initialising
            return True
        time.sleep(0.5)
    return False


def spotify_now_playing():
    """Return 'Track — Artist' if the desktop app is playing, else None.
    Cheap enough to poll (single osascript call when Spotify is running)."""
    if not spotify_is_running():
        return None
    state = _osa('tell application "Spotify" to player state')
    if state != "playing":
        return None
    name = _osa('tell application "Spotify" to name of current track')
    artist = _osa('tell application "Spotify" to artist of current track')
    if not name:
        return None
    return f"{name} — {artist}" if artist else name


def spotify_command(action):
    """Control the Spotify desktop app locally (no login needed).
    action: play | pause | playpause | next | previous | current | up | down
    Returns a short spoken-style string."""
    app = 'tell application "Spotify"'
    # play/resume auto-launches Spotify — "play some music" should just work.
    # Everything else (pause/skip/volume) only makes sense on a running app, so
    # check first and report the truth instead of no-op-ing into a closed app.
    if action in ("play", "resume"):
        if not spotify_is_running() and not ensure_spotify_open():
            return "I couldn't get Spotify open."
    elif action in ("pause", "playpause", "next", "previous", "up", "down"):
        if not spotify_is_running():
            return "Spotify isn't open right now."
    if action in ("play", "resume"):
        _osa(f"{app} to play")
        # Verify playback actually started before claiming it did
        time.sleep(0.4)
        state = _osa(f"{app} to player state")
        if state == "playing":
            return "Playing."
        return "Spotify's open but nothing started — it may have no track queued. Name a song and I'll put it on."
    if action == "pause":
        _osa(f"{app} to pause")
        return "Paused."
    if action == "playpause":
        _osa(f"{app} to playpause")
        return "Okay."
    if action == "next":
        _osa(f"{app} to next track")
        return "Skipping ahead."
    if action == "previous":
        _osa(f"{app} to previous track")
        return "Going back."
    if action in ("up", "down"):
        vol = _osa(f"{app} to sound volume")
        try:
            cur = int(vol)
        except ValueError:
            cur = 50  # safe default if Spotify returns something unexpected
        new = max(0, min(100, cur + (15 if action == "up" else -15)))  # clamp to [0, 100]
        _osa(f"{app} to set sound volume to {new}")
        return "Turned it up." if action == "up" else "Turned it down."
    if action == "current":
        running = _osa('tell application "System Events" to (name of processes) contains "Spotify"')
        if running != "true":
            return "Spotify isn't open right now."
        state = _osa(f"{app} to player state")
        if state != "playing":
            return "Nothing's playing right now."
        name = _osa(f"{app} to name of current track")
        artist = _osa(f"{app} to artist of current track")
        if not name:
            return "I can't tell what's playing."
        return f"{name} by {artist}." if artist else f"{name}."
    return None  # unrecognised action — caller falls through to LLM


# ---- Action Dispatcher ----

_LOC_Q = _re.compile(
    r"\b(where (am i|are we|is this)|what (city|state|town) (am i|are we) in"
    r"|what('s| is) (my|our) (location|city|address))\b",
    _re.I,
)


def _answer_location_question(user_input):
    """Return a spoken location string for 'where am I' type questions, else None."""
    if not _LOC_Q.search(user_input):
        return None
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from core import assistant as _asst
        loc = _asst.get_location()
        if loc:
            return f"You're in {loc['city']}, {loc['region']}."
    except Exception:
        pass
    return "I couldn't get your location right now."


def detect_action(user_input):
    """Answer local date/location facts before invoking a model.

    App and website requests deliberately go through the model's tool menu so
    they can participate in multi-step plans and honor remembered preferences.
    Returns a spoken response string if handled, or None to let the LLM answer."""
    date_answer = answer_date_question(user_input)
    if date_answer:
        return date_answer

    loc_answer = _answer_location_question(user_input)
    if loc_answer:
        return loc_answer

    return None  # nothing matched — let the LLM handle it
