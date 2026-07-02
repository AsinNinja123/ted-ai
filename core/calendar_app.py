"""
core/calendar_app.py — Read and write Calendar.app events via AppleScript.

Public API:
    get_today_events()     → list of event dicts for today
    get_tomorrow_events()  → list of event dicts for tomorrow
    get_week_events()      → list for the next 7 days
    get_next_event()       → single soonest upcoming event, or None
    add_event(title, start_dt, end_dt, notes) → confirmation string
    format_events_for_speech(events) → human-readable summary string

All functions return [] or "" on any AppleScript failure — never raise.
"""

import os
import subprocess
from datetime import datetime, timedelta

HOME = os.path.expanduser("~/ted-ai")


def _run_script(script: str) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip()
    except Exception as e:
        print(f"[calendar] AppleScript error: {e}")
        return ""


def _parse_applescript_date(s: str):
    fmts = [
        "%A, %B %d, %Y at %I:%M:%S %p",
        "%A, %B %d, %Y at %I:%M %p",
        "%B %d, %Y at %I:%M:%S %p",
        "%B %d, %Y at %I:%M %p",
    ]
    s = s.strip()
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _get_events_in_range(start_dt: datetime, end_dt: datetime) -> list:
    start_str = start_dt.strftime("%B %d, %Y at %I:%M:%S %p")
    end_str   = end_dt.strftime(  "%B %d, %Y at %I:%M:%S %p")
    script = f"""
tell application "Calendar"
    set outList to {{}}
    set startDate to date "{start_str}"
    set endDate to date "{end_str}"
    repeat with cal in calendars
        try
            set evts to (every event of cal whose start date >= startDate and start date <= endDate)
            repeat with evt in evts
                set evtTitle to summary of evt
                set evtStart to start date of evt
                set evtEnd to end date of evt
                set evtLoc to ""
                try
                    set evtLoc to location of evt
                end try
                set end of outList to (evtTitle & "|" & (evtStart as string) & "|" & (evtEnd as string) & "|" & evtLoc)
            end repeat
        end try
    end repeat
    return outList
end tell
"""
    raw = _run_script(script)
    if not raw:
        return []

    events = []
    for chunk in raw.split(", "):
        chunk = chunk.strip()
        if "|" not in chunk:
            continue
        parts = chunk.split("|", 3)
        if len(parts) < 1:
            continue
        title    = parts[0].strip()
        start_s  = parts[1].strip() if len(parts) > 1 else ""
        end_s    = parts[2].strip() if len(parts) > 2 else ""
        location = parts[3].strip() if len(parts) > 3 else ""
        if not title:
            continue
        start_obj = _parse_applescript_date(start_s)
        end_obj   = _parse_applescript_date(end_s)
        events.append({
            "title":       title,
            "start_dt":    start_obj,
            "end_dt":      end_obj,
            "location":    location,
            "start_epoch": start_obj.timestamp() if start_obj else None,
        })

    events.sort(key=lambda e: e.get("start_epoch") or 0)
    return events


def get_today_events() -> list:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return _get_events_in_range(today, today + timedelta(days=1))


def get_tomorrow_events() -> list:
    tomorrow = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return _get_events_in_range(tomorrow, tomorrow + timedelta(days=1))


def get_week_events() -> list:
    now = datetime.now()
    return _get_events_in_range(now, now + timedelta(days=7))


def get_next_event() -> dict:
    events = _get_events_in_range(datetime.now(), datetime.now() + timedelta(days=7))
    return events[0] if events else None


def add_event(title: str, start_dt: datetime,
              end_dt: datetime = None, notes: str = "") -> str:
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)

    def _dt_block(dt: datetime, var: str) -> str:
        secs = dt.hour * 3600 + dt.minute * 60 + dt.second
        return (
            f"set {var} to current date\n"
            f"        set year of {var} to {dt.year}\n"
            f"        set month of {var} to {dt.month}\n"
            f"        set day of {var} to {dt.day}\n"
            f"        set time of {var} to {secs}"
        )

    safe_title = title.replace('"', "'")
    notes_block = f', description: "{notes.replace(chr(34), chr(39))}"' if notes else ""

    script = f"""
tell application "Calendar"
    tell calendar 1
        {_dt_block(start_dt, "startDate")}
        {_dt_block(end_dt, "endDate")}
        make new event with properties {{summary: "{safe_title}", start date: startDate, end date: endDate{notes_block}}}
    end tell
end tell
"""
    _run_script(script)
    time_str = start_dt.strftime("%-I:%M %p on %A, %B %-d")
    return f"Added: {title} at {time_str}."


def format_events_for_speech(events: list) -> str:
    if not events:
        return "Nothing on the calendar."
    parts = []
    for e in events[:5]:
        title = e["title"]
        start = e.get("start_dt")
        if start:
            parts.append(f"{title} at {start.strftime('%-I:%M %p')}")
        else:
            parts.append(title)
    if len(events) > 5:
        parts.append(f"and {len(events) - 5} more")
    return ", ".join(parts) + "."
