"""core/tool_handlers.py — Handlers behind the LLM's function-calling tools.

Every ACTION handler returns a spoken-style string that is the ground truth of
what happened. The tool loop speaks these verbatim — the LLM never gets to
re-narrate an action, because that's where it invents successes that didn't
happen.
"""


# =============================================================================
#  READING THIS FILE       The Ted Code Book — Chapter 11 (§11.5, §11.7 – §11.8)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The kitchen. core/tools.py describes what Ted can do; this file is where
#      several of those things actually get done, and where two rules that keep
#      Ted honest are enforced.
#
#  THE HONESTY RULE  (do not weaken this)
#      An action tool reports GROUND TRUTH and Ted speaks that result verbatim.
#      Not "what we asked for", not "what probably happened" — what actually
#      happened.
#
#      The reason is a real incident. Spotify's API accepts a play request and
#      returns success in plenty of cases where nothing starts playing, and Ted
#      used to say "Playing X" whenever the call did not raise an error. Worse,
#      on another occasion Ted said "Closed VS Code and Notes." having called no
#      tool at all. Both are the same failure: intent reported as outcome.
#
#      So: `ACTION_TOOLS` marks which tools make a real change in the world, and
#      `looks_like_failure()` gives one shared answer to "did that go wrong?" —
#      one function, used by every caller, because two pieces of code answering
#      that question differently is the most common bug in this codebase (§34).
#
#  THE CONFIRMATION RULE
#      `needs_confirmation(name, args)` decides whether Ted must ask before
#      doing something. Note that it takes the ARGUMENTS, not just the name:
#      crossing out one notebook entry is an ordinary edit, deleting a whole
#      page throws work away. A plain "is this tool on the dangerous list"
#      check could not tell those apart.
#
#      Both places in core/app.py that gate an action call this one function,
#      deliberately. See §11.7.
#
#  IF YOU WANT TO CHANGE SOMETHING
#      "Ted should ask before doing X"    -> add it to needs_confirmation.
#      "Ted claims success when it fails" -> the handler is returning intent
#                                            instead of a verified result. Make
#                                            it check, then say what it found.
#      "Ted treats a fine result as an error"
#                                         -> looks_like_failure is matching a
#                                            word in your success message.
#
#  PYTHON YOU'LL SEE HERE THAT MIGHT BE NEW
#      A frozenset / tuple used as a membership list:  `name in ACTION_TOOLS`
#          Fast "is this one of these" check.
#
#      Functions that return a plain string
#          Nearly every handler returns text, because that text is what Ted
#          says. There is no separate "status code" layer; the sentence IS the
#          result. That is unusual, and it is on purpose — it makes it very hard
#          to accidentally throw away what really happened.
# =============================================================================

import subprocess
import time
import re
import os
import urllib.parse
import urllib.request

from core import features
from core.actions import (APPS, WEB_APPS, open_app, resolve_app_alias,
                          spotify_command)

try:
    from config import WEATHER_LOCATION
except Exception:
    WEATHER_LOCATION = ""   # auto-detected via IP if blank


# Tools that CHANGE something (side effects). Their handler return value is the
# ground truth and must be spoken verbatim.
# [BOOK §11.8] ─── WHICH TOOLS CHANGE THE WORLD ──────────────────────────────
# Names in this set do something real: send a message, open an app, write a
# file. Names outside it only look things up.
#
# The distinction matters for exactly one reason — the honesty check. If Ted
# claims in past tense to have DONE something on a turn where no ACTION tool
# ran, that claim is false and gets corrected. A turn that only read the weather
# and then said "I checked the weather" is fine; a turn that called nothing and
# said "I closed VS Code" is not. (core/llm.py, claims_completed_action)
ACTION_TOOLS = frozenset({
    "open_app", "close_app", "clean_up", "browse_to", "play_youtube", "play_music", "play_playlist",
    "spotify_control", "send_message", "set_reminder", "set_timer",
    "add_to_playlist", "remove_from_playlist", "create_playlist", "delete_playlist",
    "calendar_add", "notes_add", "clipboard_write",
    "notebook_write", "notebook_edit", "notebook_delete",
    "system_volume", "system_brightness", "ui_press", "ui_fill", "type_text",
    "create_document", "learn_lingo", "clarify_lingo",
    "press_key", "scroll", "log_habit",
    "email_action", "send_email",
})

# Consequential actions require an explicit user confirmation in a pending
# follow-up flow. Opening apps, typing locally, reminders, and reversible UI
# controls remain immediate; communication and destructive email changes do not.
CONFIRMATION_TOOLS = frozenset({"send_message", "send_email", "email_action",
                                "delete_playlist",
                                # No target was named and the blast radius is
                                # every open app, so this one asks first.
                                "clean_up",
                                # Ted may read all of his own source and change
                                # none of it without Charlie saying yes first.
                                "code_write"})


# [BOOK §11.7] ─── ASK FIRST? ────────────────────────────────────────────────
# One function, called by BOTH places in core/app.py that gate an action. That
# is the whole design: two callers disagreeing about whether something was
# consequential is the duplicated-judgment bug again (§34).
#
# Note it takes the ARGUMENTS, not just the name. Crossing out one notebook
# entry is an ordinary edit; deleting a whole page throws work away. A plain
# "is this tool on the dangerous list" check cannot tell those apart, which is
# why the older `name in CONFIRMATION_TOOLS` version was replaced.
def needs_confirmation(name, args=None):
    """Whether this exact call must be approved before it runs.

    Most tools are consequential or not by name alone. notebook_delete is the
    exception: crossing out one entry is an ordinary edit, deleting a whole page
    throws away everything on it. Both gates in app.py ask this one function, so
    the rule lives in one place — two callers disagreeing about whether a call
    was consequential is exactly the duplicated-judgment bug this codebase keeps
    producing.
    """
    if name in CONFIRMATION_TOOLS:
        return True
    if name == "notebook_delete":
        return (args or {}).get("entry") in (None, "")
    return False

# Phrases the handlers use when an action did NOT succeed. Lets the HUD surface the
# real problem (yellow sphere / issue popup) instead of pretending everything's fine.
_FAILURE_MARKERS = (
    "couldn't", "could not", "can't", "cannot", "isn't open", "is not open",
    "not installed", "unavailable", "didn't catch", "couldn't find",
    "couldn't parse", "couldn't reach", "no app", "don't have", "failed",
    "didn't go through", "still open", "didn't work", "isn't set",
    "verification was unavailable", "unexpectedly created",
)
# [BOOK §11.8] ─── DID THAT GO WRONG? ────────────────────────────────────────
# One shared answer to that question, used by every caller — the window's issue
# toast, the routine runner, the reflex runner, the telemetry row.
#
# It works on the TEXT of the result, because in this codebase the sentence Ted
# says IS the result. That is unusual and deliberate: there is no separate
# status-code layer that can drift out of step with what the user is told.
def looks_like_failure(result):
    r = (result or "").lower()
    return any(m in r for m in _FAILURE_MARKERS)


# [BOOK §11.9] ─── A WALL, NOT A FAILURE ─────────────────────────────────────
# Some things Ted cannot do are not bugs and never will be. macOS refuses
# synthetic clicks on its own permission, keychain and admin dialogs — that
# refusal IS the security boundary, and an assistant able to grant itself file
# access would not have one. Accessibility being switched off is the same
# shape: a human hand has to move.
#
# These have to read differently from "the tool broke". A failure invites a
# retry; a wall invites Charlie. Reported as a failure Ted loops on it; reported
# as nothing at all he stalls in silence, which is exactly what happened when a
# trust prompt appeared and the only way forward was Charlie typing "say yes"
# into a turn that had no idea a task was in flight.
_HUMAN_HAND_MARKERS = (
    "accessibility permission", "accessibility access", "not trusted for",
    "screen recording permission", "automation permission",
    "grant access", "allow access", "system settings > privacy",
    "requires your permission", "administrator password", "keychain",
    "would like to access",
)


def needs_human_hand(result):
    """True when the only way past this is Charlie clicking something.

    Same technique as looks_like_failure and for the same reason: in this
    codebase the sentence Ted says IS the result, so there is no status-code
    layer to drift out of step with it.
    """
    r = (result or "").lower()
    return any(m in r for m in _HUMAN_HAND_MARKERS)


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
    "click", "tap", "press", "scroll", "type this", "write this",
    "new document", "google doc", "google docs",
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
    n = re.sub(r"\b(?:app|application)\b", " ", n)
    n = " ".join(n.split())
    all_keys = {**APPS, **WEB_APPS}
    if n in all_keys:
        return n
    app_key = resolve_app_alias(n)
    if app_key is not None:
        return app_key
    for key in all_keys:
        # A shorter utterance may be a useful partial app name ("visual" for
        # "visual studio code"). The reverse is unsafe: a known GUI name inside
        # a longer product name may identify a different command-line program.
        # For example, silently discarding the extra words can open the wrong
        # product while reporting success.
        if n in key:
            return key
    n_words = set(n.split())
    best, best_score = None, 0
    for key in all_keys:
        score = len(set(key.split()) & n_words)
        if score > best_score:
            best, best_score = key, score
    # One shared word in a longer product name is not enough. It turned
    # "Claude Code" into "VS Code" after correctly refusing to call it the
    # Claude GUI. All supplied words must identify the same known app here;
    # spelling errors were already handled by resolve_app_alias above.
    return best if best_score == len(n_words) and best_score > 0 else None


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
    # music.transport preserves fast local control while reaching a genuinely
    # active phone/speaker when the desktop app is closed. The Web resume path
    # verifies playback before it is allowed to say "Playing."
    if cmd in {"play", "pause", "next", "previous", "current"}:
        from core.music import transport
        result = transport(cmd)
    else:
        result = spotify_command(cmd)
    if result is None:
        return f"I don't have a Spotify action called '{action}'."
    return result


_BROWSERS = {
    "brave": "Brave Browser", "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "google": "Google Chrome",
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
    """Apply Charlie's browser boundary: Brave is YouTube-only by default."""
    key = (site or "").strip().lower().rstrip("/")
    is_youtube = (key == "youtube"
                  or bool(re.search(r"(?:^|[/.])youtube\.com(?:$|/)", key)))
    if is_youtube:
        return (SITE_BROWSER_PREFERENCES or {}).get("youtube", "Brave")
    return "Chrome"


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


def _open_verified_browser(app_name, url, new_window=False):
    """Navigate once, reusing an existing browser window unless explicitly told not to.

    Launch Services opens a URL as a new tab in an existing browser window and
    creates one window when none exists. The old path forced ``--new-window``
    and then separately activated Brave; on a cold launch that produced the
    requested page in one window and a second empty window. Explicit new-window
    requests use the Chromium flag once, with no second activation.
    """
    before = _browser_window_count(app_name)
    try:
        executable = f"/Applications/{app_name}.app/Contents/MacOS/{app_name}"
        chromium = app_name in {"Brave Browser", "Google Chrome", "Microsoft Edge"}
        if new_window and chromium and os.path.exists(executable):
            subprocess.Popen(
                [executable, "--new-window", url], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        else:
            sent = subprocess.run(
                ["open", "-a", app_name, url], capture_output=True, text=True, timeout=8,
            )
            if sent.returncode != 0:
                return False, (sent.stderr or "Launch Services rejected the URL").strip()
    except Exception as exc:
        return False, str(exc)
    for _ in range(20):
        windows = _browser_window_count(app_name)
        if windows is not None and windows > 0:
            if (not new_window and before is not None and before > 0
                    and windows > before):
                return False, (f"{app_name} created a new window instead of "
                               "reusing the existing one")
            if not new_window and before == 0 and windows > 1:
                return False, f"{app_name} unexpectedly created {windows} windows"
            if new_window and before is not None and windows <= before:
                time.sleep(0.2)
                continue
            change = ("new window appeared" if new_window else
                      "existing window reused" if before else "one window is visible")
            return True, f"{app_name} {change}"
        time.sleep(0.2)
    if _browser_window_count(app_name) == 0:
        return False, (f"{app_name} is running without a browser window; "
                       "quit and reopen it once")
    return False, "macOS window verification was unavailable"


def _active_tab(app_name, tries=3, delay=0.7):
    """What the browser is actually showing — title and URL of the front tab.

    The window check above proves a window exists. It says nothing about what
    is in it, which is why Ted could open a video and then honestly report that
    he could not confirm anything: the HUD could see the tab and he could not.
    The information was already being collected for the computer panel, so this
    reads the same source rather than adding a second AppleScript that could
    disagree with it.

    Bounded retry, because a page that has been ASKED for is not a page that
    has loaded: Chromium reports the URL as the title until the document
    commits, so an immediate read gets a URL where the video title will be.
    Returns the tab dict, or None if nothing could be read at all.
    """
    from core import system_state
    best = None
    for attempt in range(max(1, tries)):
        try:
            tabs, _windows = system_state._browser_tabs(app_name)
        except Exception as exc:
            print(f"[browse] tab read failed for {app_name}: {exc}")
            tabs = []
        active = [t for t in tabs if t.get("active")] or tabs
        if active:
            tab = active[0]
            best = tab
            title = (tab.get("title") or "").strip()
            settled = (title and title != tab.get("url")
                       and not title.lower().startswith(("about:", "new tab", "untitled")))
            if settled:
                return tab
        if attempt < tries - 1:
            time.sleep(delay)
    return best


def _tab_report(app_name, label):
    """One sentence about what a browser ended up showing.

    Kept separate from the opening so both the named-browser path and the
    default-browser path say the same thing the same way, and so the honest
    'I opened it but cannot read it back' case is written once.
    """
    tab = _active_tab(app_name)
    title = (tab or {}).get("title", "").strip()
    url = (tab or {}).get("url", "").strip()
    if title and title != url:
        return f"Opened {label} in {app_name} — showing \"{title}\"."
    if url:
        return f"Opened {label} in {app_name} — currently at {url}."
    # Opening was verified; only the read-back failed. Say which is which
    # rather than falling back on a blanket "I can't verify".
    return (f"Opened {label} in {app_name}. The window is up, but I couldn't "
            f"read the tab back, so I can't tell you what's on the page.")


def tool_browse_to(site, browser=None, new_window=False):
    """Open a website, optionally in a SPECIFIC browser ('youtube in Brave').
    Existing browser windows are reused unless ``new_window`` is explicit.
    Verifies the open actually succeeded before claiming it did."""
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
    # An explicitly named browser is honored. Otherwise Chrome owns the web,
    # with exactly one standing exception: YouTube goes to Brave for ad blocking.
    browser = browser or preferred_browser_for(url)
    # Send the URL and verify the requested browser actually appears.
    if browser:
        app_name = _BROWSERS.get(browser.strip().lower(), browser.strip())
        verified, detail = _open_verified_browser(app_name, url, bool(new_window))
        if verified:
            return _tab_report(app_name, label)
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
            return _tab_report("Google Chrome", label)
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


def tool_search_chats(query, limit=6):
    """Find what was actually said in an earlier chat thread."""
    from core import memory
    try:
        hits = memory.search_chat_turns(query, limit=max(1, min(int(limit or 6), 20)))
    except Exception as exc:
        print("[search_chats]", exc)
        return f"I couldn't search the chat history: {exc}"
    if not hits:
        return f"Nothing in our past chats matches '{query}'."
    lines = []
    for h in hits:
        who = "You" if h["role"] == "user" else "I"
        when = (h["ts"] or "")[:10]
        text = " ".join((h["content"] or "").split())
        if len(text) > 220:
            text = text[:220].rstrip() + "…"
        lines.append(f'[{h["title"]}, {when}] {who} said: {text}')
    return "\n".join(lines)


def tool_now_playing():
    """What is playing right now — the Spotify track and the front browser tab.

    Exists because "what's playing?" had no answer that did not require Ted to
    guess. The HUD could see both of these and Ted could not, so he fell back on
    saying he was unable to check while the answer was on screen. Same source as
    the HUD uses for each, so the two cannot contradict each other.
    """
    parts = []

    try:
        from core import features
        if features.HAS_SPOTIFY_WEB and features.spotify_web is not None:
            np = features.spotify_web.now_playing() or {}
            title = (np.get("title") or "").strip()
            if title:
                artist = (np.get("artist") or "").strip()
                label = f"{title} by {artist}" if artist else title
                parts.append(f"Spotify: {label}"
                             + ("" if np.get("playing") else " (paused)"))
            else:
                parts.append("Spotify: nothing playing.")
        else:
            parts.append("Spotify isn't connected.")
    except Exception as exc:
        print("[now_playing] spotify:", exc)
        parts.append("Spotify: couldn't check.")

    # Browser playback, then whichever browser tab is merely open. These are
    # different claims and are kept apart: macOS confirms the first, while the
    # second is only "this tab exists". Collapsing them is how "what's playing?"
    # used to get answered with the name of a tab nobody was watching.
    try:
        from core import system_state
        state = system_state.collect(include_remote=False) or {}
        playing = state.get("browser_media")
        if playing:
            what = "a video" if playing.get("kind") == "video" else "audio"
            if playing.get("title") and playing.get("confidence") != "browser":
                hedge = ("" if playing["confidence"] == "certain"
                         else ", most likely its active tab")
                parts.append(f"{playing['app']} is playing {what}: "
                             f"{playing['title']}{hedge}")
            else:
                extra = ""
                if playing.get("candidates"):
                    extra = " Video tabs open: " + ", ".join(playing["candidates"])
                parts.append(f"{playing['app']} is playing {what}, but I can't "
                             f"tell which tab.{extra}")
        else:
            front = state.get("frontmost", "")
            details = state.get("details", {}) or {}
            ordered = ([front] if front in details else []) + [
                name for name in details if name != front]
            for name in ordered:
                tabs = (details.get(name) or {}).get("tabs") or []
                if not tabs:
                    continue
                active = [t for t in tabs if t.get("active")] or tabs
                title = (active[0].get("title") or "").strip()
                if title:
                    parts.append(f"No browser is playing anything. {name}'s "
                                 f"open tab is {title}")
                break
            else:
                parts.append("No browser is playing anything, and no tab is open.")
    except Exception as exc:
        print("[now_playing] browser:", exc)
        parts.append("Couldn't read the browser.")

    # Joined as sentences, not with a bullet: this string is spoken as often as
    # it is read, and a separator that looks tidy reads as noise out loud.
    return " ".join(p if p.endswith(".") else p + "." for p in parts)


def find_images(query, count=3):
    """Search the web for pictures. Returns a list of dicts, or [] on failure.

    Kept free of any HUD reference so it stays testable and so the decision
    about what to *show* lives with the code that owns the window.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS(timeout=8) as ddgs:
            raw = list(ddgs.images(query, max_results=max(1, min(int(count), 6))))
    except Exception as exc:
        print(f"[images] search failed: {exc}")
        return []
    out = []
    for item in raw:
        url = (item.get("image") or "").strip()
        if not url.startswith("http"):
            continue
        out.append({
            "title": (item.get("title") or query)[:160],
            # The full-size image can be a 4000px TIFF on a slow host. The
            # thumbnail is what actually renders in a chat bubble; the full one
            # is kept so clicking through is possible.
            "url": url,
            "thumbnail": (item.get("thumbnail") or url).strip(),
            "source": (item.get("url") or "").strip(),
            "width": item.get("width"),
            "height": item.get("height"),
        })
    return out


def _youtube_first_video_id(query):
    """Resolve a search to a public video ID without requiring an API key."""
    search = query.strip() or "popular videos"
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": search})
    request = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/126 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            page = response.read().decode("utf-8", "ignore")
    except Exception as exc:
        return "", str(exc)
    for video_id in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', page):
        if video_id:
            return video_id, ""
    return "", "YouTube returned no playable search result"


def _browser_video_state(app_name):
    """Return ``playing``, ``paused``, or ``""`` when state is unavailable.

    Asks macOS first. A browser holds a "Video Wake Lock" power assertion for
    precisely as long as a video plays, which is an operating-system fact that
    costs one subprocess and needs no permission. The two paths below it are
    both conditional: Chromium disables JavaScript from Apple Events by
    default, and the accessibility title only works while the browser is
    frontmost. Ted used to try only those two and therefore reported "I
    couldn't verify that playback started" while a video was plainly playing.
    """
    from core import system_state
    try:
        assertions = system_state._media_assertions()
        if assertions:
            by_pid = system_state._gui_pids([app_name])
            for pid, flags in assertions.items():
                if by_pid.get(pid) == app_name and (flags["video"] or flags["audio"]):
                    return "playing"
    except Exception as exc:
        print("[browser-video] assertion check:", exc)

    script = (
        f'tell application "{app_name}" to execute active tab of front window '
        'javascript "(() => { const v=document.querySelector(\'video\'); '
        'return v ? (v.paused ? \'paused\' : \'playing\') : \'missing\'; })()"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        value = result.stdout.strip().lower()
        if result.returncode == 0 and value in {"playing", "paused"}:
            return value
    except Exception:
        pass
    # Its Accessibility window title still exposes "Audio playing", which gives
    # a semantic, image-free verification path without asking Charlie to weaken
    # a browser security setting.
    try:
        from core import computer
        if (computer.accessibility_status().get("frontmost") == app_name
                and computer.has_accessible_text("Audio playing")):
            return "playing"
    except Exception:
        pass
    return ""


def tool_play_youtube(query="", browser=None):
    """Open a concrete YouTube watch URL and verify or start its video player."""
    video_id, error = _youtube_first_video_id(query or "")
    if not video_id:
        return f"I couldn't find a YouTube video to play. ({error})"
    url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
    browser = browser or preferred_browser_for("youtube") or "Brave"
    opened = tool_browse_to(url, browser)
    if looks_like_failure(opened):
        return opened
    app_name = _BROWSERS.get(str(browser).strip().lower(), str(browser).strip())
    state = ""
    for _ in range(16):
        state = _browser_video_state(app_name)
        if state in {"playing", "paused"}:
            break
        time.sleep(0.25)
    if state in {"paused", ""}:
        try:
            from core import computer
            pressed = computer.press_target("Play")
            if looks_like_failure(pressed):
                # Autoplay may already have started even when the browser did
                # not expose its player button. Re-check before reporting a
                # failure, since Chromium's window title is authoritative here.
                state = _browser_video_state(app_name)
                if state != "playing":
                    return (f"Opened the YouTube video in {app_name}, but I couldn't "
                            "start playback. " + pressed)
            time.sleep(0.5)
            state = _browser_video_state(app_name)
        except Exception:
            state = ""
    label = query.strip() or "a popular video"
    if state == "playing":
        return f"Playing {label} on YouTube in {app_name}."
    return (f"Opened {label} on YouTube in {app_name}, but I couldn't verify "
            "that playback started.")


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
