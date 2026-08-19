"""
core/assistant.py — Ted's personal-assistant memory: reminders, timers, lists,
and pure helpers like duration parsing.

Design notes
------------
* Plain Python + ONE JSON file (data/assistant.json). No network, no Neo4j, so it
  survives restarts and can never crash Ted — every call degrades to a no-op.
* This module owns the DATA and the PARSING. The HUD owns the voice. Keeping the
  two apart means the fiddly bits here are unit-testable without audio hardware.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 28 (§28.1)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Reminders, timers, and the small parsing that turns "in twenty minutes" into
#      a number of seconds. Backed by ONE JSON file — data/assistant.json — which
#      means it survives restarts and cannot take Ted down with it.
#
#  THE SPLIT THIS FILE KEEPS
#      This module owns the DATA and the PARSING. It never speaks and never touches
#      the window. That separation is what lets it be tested without audio hardware.
#
# =============================================================================

import os
import re
import json
import time
from datetime import datetime, timedelta

HOME = os.path.expanduser("~/ted-ai")
STORE = os.path.join(HOME, "data", "assistant.json")


# ---- Storage ----

def _load():
    """Read the JSON store from disk and return it as a dict.
    Every public function calls _load() fresh so concurrent writes from other
    processes (or a future web UI) are always seen without a stale cache."""
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}  # first run or corrupted file — start clean
    d.setdefault("reminders", [])
    d.setdefault("lists", {})
    return d


def _save(d):
    """Persist the in-memory dict back to the JSON store. Silently drops errors
    so a permissions problem never crashes Ted mid-conversation."""
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        with open(STORE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        print(f"[assistant] save skipped: {e}")


# ---- Reminders ----

def add_reminder(text, due_ts, kind="reminder", label=None):
    """Store a reminder/timer. due_ts is an epoch time, or None for a standing
    reminder that only surfaces in the briefing. label is an optional name for
    the timer (e.g. "pasta") enabling cancel-by-name."""
    d = _load()
    rid = int(time.time() * 1000)
    entry = {"id": rid, "text": text, "due": due_ts, "kind": kind, "done": False}
    if label:
        entry["label"] = label.lower()
    d["reminders"].append(entry)
    _save(d)
    return rid


def pop_due(now=None):
    """Return (and mark done) every reminder whose time has come."""
    now = now or time.time()
    d = _load()
    fired = [r for r in d["reminders"]
             if not r["done"] and r["due"] and r["due"] <= now]
    if fired:
        for r in fired:
            r["done"] = True
        _save(d)
    return fired


def fire_due(now=None):
    """Return (and mark done) reminders that came due — including any that fired
    while Ted was offline. Called at startup to surface missed items."""
    return pop_due(now)


def pending_reminders():
    """Return open reminders (not timers) for the morning briefing."""
    return [r for r in _load()["reminders"]
            if not r["done"] and r.get("kind", "reminder") == "reminder"]


def clear_reminders():
    """Delete all reminders from the store."""
    d = _load()
    d["reminders"] = []
    _save(d)


def cancel_pending(kind=None):
    """Cancel pending timers/reminders. kind='timer', 'reminder', or None for all.
    Returns how many were cancelled."""
    d = _load()
    n = 0
    for r in d["reminders"]:
        if not r["done"] and (kind is None or r.get("kind", "reminder") == kind):
            r["done"] = True
            n += 1
    if n:
        _save(d)
    return n


def cancel_by_id(rid) -> bool:
    """Cancel a single pending reminder/timer by its id (from add_reminder).
    Returns True if something was cancelled. Used by the HUD's click-to-cancel."""
    d = _load()
    for r in d["reminders"]:
        if r.get("id") == rid and not r["done"]:
            r["done"] = True
            _save(d)
            return True
    return False


def cancel_by_label(label: str) -> int:
    """Cancel pending timers whose label contains the given string (case-insensitive).
    Returns how many were cancelled."""
    label = label.lower()
    d = _load()
    n = 0
    for r in d["reminders"]:
        if not r["done"] and label in r.get("label", "").lower():
            r["done"] = True
            n += 1
    if n:
        _save(d)
    return n


# ---- Lists ----

def _key(name):
    """Normalise a list name to a lowercase, stripped string for dict lookup."""
    return (name or "").strip().lower()


def list_add(name, item):
    """Add item to the named list (case-insensitive dedup). Returns the full list."""
    d = _load()
    k = _key(name)
    items = d["lists"].setdefault(k, [])
    if item and item.lower() not in [x.lower() for x in items]:
        items.append(item)
    _save(d)
    return items


def list_get(name):
    """Return all items on the named list, or [] if it doesn't exist."""
    return _load()["lists"].get(_key(name), [])


def list_remove(name, item):
    """Remove one item from the named list (case-insensitive). Returns the updated list."""
    d = _load()
    k = _key(name)
    items = [x for x in d["lists"].get(k, []) if x.lower() != item.lower()]
    d["lists"][k] = items
    _save(d)
    return items


def list_clear(name):
    """Delete an entire named list from the store."""
    d = _load()
    d["lists"].pop(_key(name), None)
    _save(d)


def list_names():
    """Return the names of all stored lists."""
    return list(_load()["lists"].keys())


# ---- Duration Parsing ----

# Maps spoken number words to their numeric values.
# "a"/"an" are treated as articles (ignored), so "a half" == 0.5, not 1.5.
# "half" and "quarter" are fractional so they combine naturally with a unit:
#   "an hour and a half" → pending=0.5 after "half", then * 3600 = 1800s.
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90,
    "half": 0.5, "quarter": 0.25, "couple": 2, "few": 3,
}

# Maps every accepted unit spelling to its value in seconds.
_UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}


def parse_duration(text):
    """'10 minutes', 'an hour and a half', '90 seconds', 'two and a half hours'.
    Returns seconds (float) or None if no duration is present."""
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", text.lower())
    total, pending, last_unit, found = 0.0, None, None, False
    for tok in tokens:
        if re.match(r"^\d", tok):
            pending = float(tok)  # bare digit resets accumulator ("10 and 20 min" → 20 min, not 30)
        elif tok in _NUM_WORDS:
            pending = (pending or 0) + _NUM_WORDS[tok]  # accumulate word numbers
        elif tok in _UNIT_SECONDS:
            qty = pending if pending is not None else 1  # "an hour" → pending=None → qty=1
            total += qty * _UNIT_SECONDS[tok]
            last_unit = _UNIT_SECONDS[tok]  # remember for the trailing-fraction case below
            pending = None
            found = True
        # everything else ("a", "an", "and", "for", filler) is ignored
    if pending is not None and last_unit:
        # Handles trailing fractions like "two and a half hours" where "half" (0.5)
        # is left in pending after "hours" has already been consumed.
        total += pending * last_unit
    return total if found and total > 0 else None


def human_duration(secs):
    """Speak-friendly duration: 90 -> 'a minute and a half'-ish, 600 -> '10 minutes'."""
    secs = int(round(secs))
    if secs < 60:
        return f"{secs} second" + ("s" if secs != 1 else "")
    if secs < 3600:
        m, s = secs // 60, secs % 60
        out = f"{m} minute" + ("s" if m != 1 else "")
        return out + (f" {s} seconds" if s else "")
    h, rem = secs // 3600, secs % 3600
    m = rem // 60
    out = f"{h} hour" + ("s" if h != 1 else "")
    return out + (f" {m} minutes" if m else "")


# ---- Absolute Time Parsing ----

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def parse_when(text, now=None):
    """Absolute reminder times: 'at 5pm', 'tomorrow morning', 'Friday at 9',
    'tonight', 'at noon'. Returns an epoch timestamp, or None if no clock/date
    is present (caller should then try parse_duration for 'in 10 minutes').

    day_set tracks whether the user named a specific date — if they did, we
    never roll the target forward by a day even if the time has already passed
    today (e.g. "remind me Friday at 3am" shouldn't become the next Friday)."""
    now = now or datetime.now()
    t = text.lower()
    base = now.date()
    day_set = False  # True once a date word (tomorrow/weekday) has been matched

    if "day after tomorrow" in t:
        base = (now + timedelta(days=2)).date(); day_set = True
    elif "tomorrow" in t:
        base = (now + timedelta(days=1)).date(); day_set = True
    else:
        for name, idx in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", t):
                ahead = (idx - now.weekday()) % 7 or 7  # % 7 is 0 if today → force +7
                base = (now + timedelta(days=ahead)).date(); day_set = True
                break

    hour = minute = None
    m = re.search(r"\bat (\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", t)
    if m:
        hour = int(m.group(1)); minute = int(m.group(2) or 0)
        ap = (m.group(3) or "").replace(".", "")
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        elif not ap and 1 <= hour <= 6:
            # Bare "at 3" almost always means afternoon in daily conversation;
            # nobody sets a reminder "at 3" meaning 3am unless they say "am".
            hour += 12
    elif "noon" in t:
        hour, minute = 12, 0
    elif "midnight" in t:
        hour, minute = 0, 0
    elif "morning" in t:
        hour, minute = 8, 0
    elif "afternoon" in t:
        hour, minute = 14, 0
    elif "evening" in t or "tonight" in t:
        hour, minute = 19, 0

    if hour is None and not day_set:
        return None  # no usable time or date — let the caller try parse_duration
    if hour is None:
        hour, minute = 9, 0  # a day with no time → default to 9am

    target = datetime.combine(base, datetime.min.time()).replace(hour=hour, minute=minute)
    if target <= now and not day_set:  # time already passed today → roll to tomorrow
        target += timedelta(days=1)
    return target.timestamp()


def strip_time_phrase(body):
    """Remove a trailing time clause so the reminder text stays clean."""
    pat = (r"\s*\b(?:at \d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|at noon|at midnight"
           r"|tonight|today|day after tomorrow|tomorrow(?: morning| afternoon| evening| night)?"
           r"|this (?:morning|afternoon|evening)|in the (?:morning|afternoon|evening)"
           r"|(?:on |by |this |next )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))"
           r"\b.*$")
    return re.sub(pat, "", body, flags=re.I).strip(" ,")


# ---- Location ----

_location_cache = None   # cached dict from ip-api.com, or None if not yet fetched

def get_location():
    """Return location info from ip-api.com as a dict with keys:
    city, region, country, lat, lon, timezone.
    Returns None on failure. Result is cached for the session."""
    global _location_cache
    if _location_cache is not None:
        return _location_cache
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            "http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon,timezone",
            headers={"User-Agent": "curl/8"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = _json.loads(r.read())
        if data.get("status") == "success":
            _location_cache = {
                "city":     data.get("city", ""),
                "region":   data.get("regionName", ""),
                "country":  data.get("country", ""),
                "lat":      data.get("lat"),
                "lon":      data.get("lon"),
                "timezone": data.get("timezone", ""),
            }
            return _location_cache
    except Exception:
        pass
    return None


def get_location_string(config_location=""):
    """Best available location string for weather/display.
    Prefers config_location if set, falls back to IP geolocation."""
    if config_location and config_location.strip():
        return config_location.strip()
    loc = get_location()
    if loc:
        return f"{loc['city']}, {loc['region']}"
    return ""


# ---- Weather ----

_WMO_CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "scattered showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def get_weather(location=""):
    """Speak-friendly current conditions + today's forecast via Open-Meteo (no API key).
    Falls back to wttr.in if Open-Meteo fails. Returns '' on total failure."""
    loc = get_location()
    if not loc or loc.get("lat") is None:
        return _get_weather_wttr(location)
    try:
        import urllib.request, urllib.parse, json as _json
        lat, lon = loc["lat"], loc["lon"]
        tz = loc.get("timezone", "auto") or "auto"
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current_weather=true"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&forecast_days=1&timezone={urllib.parse.quote(tz, safe='/')}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read())
        cw   = data.get("current_weather", {})
        temp = round(cw.get("temperature", 0))
        code = int(cw.get("weathercode", 0))
        cond = _WMO_CODES.get(code, "mixed conditions")
        daily = data.get("daily", {})
        hi    = round(daily.get("temperature_2m_max", [temp])[0])
        lo    = round(daily.get("temperature_2m_min", [temp])[0])
        rain  = daily.get("precipitation_probability_max", [0])[0] or 0
        parts = [f"{cond}, {temp} degrees"]
        parts.append(f"high of {hi}, low of {lo}")
        if rain >= 30:
            parts.append(f"{int(rain)} percent chance of rain")
        return ". ".join(parts)
    except Exception:
        return _get_weather_wttr(location)


def _get_weather_wttr(location=""):
    """Fallback: current conditions only via wttr.in in Fahrenheit."""
    place = get_location_string(location)
    if not place:
        return ""
    try:
        import urllib.request, urllib.parse
        url = "https://wttr.in/" + urllib.parse.quote(place) + "?format=%C+and+%t&u"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=6) as r:
            out = r.read().decode("utf-8", "ignore").strip()
        return (out.replace("+", "").replace("°F", " degrees")
                   .replace("°C", " degrees").strip())
    except Exception:
        return ""

