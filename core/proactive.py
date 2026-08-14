"""
core/proactive.py — Ted's proactive monitoring loop.

Two responsibilities:
  1. Calendar monitoring — AppleScript reads Calendar.app every ~60 s and
     speaks an alert for events starting within the next 16 minutes (once
     per event, de-duped by title + start epoch).
  2. User-defined triggers — persisted in data/proactive_triggers.json.
     Supported schedule types:
       daily_at     — fires once per calendar day at HH:MM
       interval_mins — fires every N minutes
       weekday_at   — fires once on a given weekday (MON–SUN) at HH:MM

Usage in hud.py:
    from core.proactive import ProactiveScheduler
    sched = ProactiveScheduler(api, speak_fn=speak, add_message_fn=add_message)
    threading.Thread(target=sched.run, daemon=True).start()

Public helpers (used by voice commands in hud.py):
    add_trigger(description, schedule_type, schedule_value, action_text)
    remove_trigger(trigger_id)
    list_triggers()
    get_upcoming_events(lookahead_minutes)
"""

import os
import re
import json
import time
import subprocess
from datetime import datetime, timedelta

HOME = os.path.expanduser("~/ted-ai")
TRIGGERS_FILE = os.path.join(HOME, "data", "proactive_triggers.json")
HEARTBEAT_FILE = os.path.join(HOME, "data", "daemon_heartbeat")

# A heartbeat older than this means the daemon is not running, so the
# in-process scheduler takes the calendar watch back. Three missed 60 s polls.
HEARTBEAT_STALE_SECONDS = 195


def daemon_alive() -> bool:
    """True if ted_daemon.py has written a heartbeat recently.

    When it has, the daemon owns calendar alerts and this scheduler must not
    also post them — otherwise every event fires twice, once as a macOS
    notification and once out loud. Triggers are unaffected: the daemon does
    not fire those.
    """
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            return (time.time() - float(f.read().strip())) < HEARTBEAT_STALE_SECONDS
    except Exception:
        return False


# ── Calendar ──────────────────────────────────────────────────────────────────

def get_upcoming_events(lookahead_minutes: int = 16) -> list:
    """Return Calendar.app events starting within the next lookahead_minutes.
    Each event: {title, start_epoch, location}.
    Returns [] on any AppleScript failure."""
    script = f"""
tell application "Calendar"
    set outList to {{}}
    set nowDate to current date
    set thenDate to nowDate + ({lookahead_minutes} * minutes)
    repeat with cal in calendars
        try
            set evts to (every event of cal whose start date >= nowDate \\
                         and start date <= thenDate)
            repeat with evt in evts
                set evtTitle to summary of evt
                set evtStart to start date of evt
                set evtLoc to ""
                try
                    set evtLoc to location of evt
                end try
                set end of outList to (evtTitle & "|" & \\
                    (evtStart as string) & "|" & evtLoc)
            end repeat
        end try
    end repeat
    return outList
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        raw = result.stdout.strip()
        if not raw:
            return []
        events = []
        for line in raw.split(", "):
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|", 2)
            title = parts[0].strip()
            start_str = parts[1].strip() if len(parts) > 1 else ""
            location = parts[2].strip() if len(parts) > 2 else ""
            start_epoch = _parse_applescript_date(start_str)
            if title:
                events.append({
                    "title": title,
                    "start_epoch": start_epoch,
                    "location": location,
                })
        return events
    except Exception as e:
        print(f"[proactive] calendar fetch failed: {e}")
        return []


def _parse_applescript_date(s: str):
    """Parse AppleScript date string → epoch float, or None on failure.
    AppleScript returns dates like 'Wednesday, June 25, 2026 at 3:00:00 PM'."""
    fmts = [
        "%A, %B %d, %Y at %I:%M:%S %p",
        "%A, %B %d, %Y at %I:%M %p",
        "%B %d, %Y at %I:%M:%S %p",
        "%B %d, %Y at %I:%M %p",
    ]
    s = s.strip()
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


# ── Trigger persistence ───────────────────────────────────────────────────────

def _load_triggers() -> list:
    try:
        with open(TRIGGERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_triggers(triggers: list) -> None:
    try:
        os.makedirs(os.path.dirname(TRIGGERS_FILE), exist_ok=True)
        with open(TRIGGERS_FILE, "w", encoding="utf-8") as f:
            json.dump(triggers, f, indent=2)
    except Exception as e:
        print(f"[proactive] save triggers failed: {e}")


def add_trigger(description: str, schedule_type: str,
                schedule_value: str, action_text: str) -> dict:
    """Register a new proactive trigger. Returns the new trigger dict."""
    triggers = _load_triggers()
    trigger = {
        "id": int(time.time() * 1000),
        "description": description,
        "schedule_type": schedule_type,   # daily_at | interval_mins | weekday_at
        "schedule_value": schedule_value, # HH:MM  |  N (mins)       | MON:HH:MM
        "action_text": action_text,
        "last_fired": None,
        "active": True,
    }
    triggers.append(trigger)
    _save_triggers(triggers)
    return trigger


def remove_trigger(trigger_id: int) -> bool:
    """Remove a trigger by ID. Returns True if found and removed."""
    triggers = _load_triggers()
    new = [t for t in triggers if t.get("id") != trigger_id]
    if len(new) == len(triggers):
        return False
    _save_triggers(new)
    return True


def list_triggers() -> list:
    """Return all active trigger dicts."""
    return [t for t in _load_triggers() if t.get("active", True)]


def _should_fire(trigger: dict, now: datetime) -> bool:
    """Return True if this trigger should fire at `now`."""
    st = trigger.get("schedule_type", "")
    sv = str(trigger.get("schedule_value", ""))
    last = trigger.get("last_fired")  # ISO string or None

    if st == "daily_at":
        try:
            h, m = map(int, sv.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target and (now - target).seconds < 90:
                if last is None or datetime.fromisoformat(last).date() < now.date():
                    return True
        except Exception:
            pass

    elif st == "interval_mins":
        try:
            mins = int(sv)
            if last is None:
                return True
            elapsed = (now - datetime.fromisoformat(last)).total_seconds()
            if elapsed >= mins * 60:
                return True
        except Exception:
            pass

    elif st == "weekday_at":
        # sv format: "MON:HH:MM"
        try:
            day_part, time_part = sv.split(":", 1)
            day_map = {
                "MON": 0, "TUE": 1, "WED": 2,
                "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
            }
            target_day = day_map.get(day_part.upper(), -1)
            h, m = map(int, time_part.split(":"))
            if now.weekday() == target_day:
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now >= target and (now - target).seconds < 90:
                    if last is None or datetime.fromisoformat(last).date() < now.date():
                        return True
        except Exception:
            pass

    return False


# ── Scheduler ─────────────────────────────────────────────────────────────────

class ProactiveScheduler:
    """Background proactive monitor.

    Takes the TedApi instance plus two callbacks (speak, add_message) to
    avoid a circular import with hud.py.
    """

    def __init__(self, api, speak_fn, add_message_fn):
        self.api = api
        self._speak = speak_fn
        self._add_message = add_message_fn
        self._alerted_events: set = set()   # event keys already spoken

    # ── internal helpers ──────────────────────────────────────────────────────

    def _speak_if_free(self, message: str, timeout: float = 15.0) -> None:
        """Acquire the busy lock (spin up to timeout s) then speak."""
        if self.api.muted:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.api.muted:
                return
            if self.api._busy.acquire(blocking=False):
                try:
                    self._add_message(self.api.window, "ted", message)
                    self._speak(self.api.window, message, self.api)
                    # Ted initiated — open the attention window so the user's
                    # reply is heard without a wake word.
                    try:
                        self.api._touch_attention()
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[proactive] speak failed: {e}")
                finally:
                    try:
                        self.api._busy.release()
                    except RuntimeError:
                        pass
                return
            time.sleep(0.5)

    # ── monitors ──────────────────────────────────────────────────────────────

    def _check_calendar(self) -> None:
        """Alert for events starting within the next ~15 minutes.

        No-op while ted_daemon.py is alive — it posts these as notifications
        whether or not the HUD is open, and two watchers means two alerts.
        """
        if daemon_alive():
            return
        try:
            events = get_upcoming_events(lookahead_minutes=16)
        except Exception:
            return
        for evt in events:
            title = evt.get("title", "")
            start_epoch = evt.get("start_epoch")
            location = evt.get("location", "")
            if not title or not start_epoch:
                continue
            key = f"{title}_{int(start_epoch)}"
            if key in self._alerted_events:
                continue
            mins_away = int((start_epoch - time.time()) / 60)
            if 0 <= mins_away <= 16:
                self._alerted_events.add(key)
                if mins_away <= 1:
                    msg = f"Heads up — {title} is starting now."
                else:
                    msg = f"Heads up — {title} in {mins_away} minutes."
                if location:
                    msg += f" Location: {location}."
                self._speak_if_free(msg)

    def _fire_action(self, action: str) -> None:
        """Fire a trigger's action. If the text parses as one of Ted's commands
        ('give me the rundown', 'give me the weather'), EXECUTE it and speak the
        result; otherwise speak the text verbatim ('take your medicine')."""
        result = None
        try:
            result = self.api._assistant_command(action)
        except Exception as e:
            print(f"[proactive] action exec failed: {e}")
        self._speak_if_free(result if result else action)

    def _check_triggers(self) -> None:
        """Fire any user-defined triggers whose schedule has come."""
        triggers = _load_triggers()
        now = datetime.now()
        changed = False
        for trigger in triggers:
            if not trigger.get("active", True):
                continue
            if _should_fire(trigger, now):
                action = trigger.get("action_text", "")
                if action:
                    self._fire_action(action)
                trigger["last_fired"] = now.isoformat()
                changed = True
        if changed:
            _save_triggers(triggers)

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self, interval: int = 60) -> None:
        """Thread target — polls calendar + triggers every `interval` seconds."""
        _last_cleanup = time.time()
        while True:
            try:
                if not self.api.muted:
                    self._check_calendar()
                    self._check_triggers()
            except Exception as e:
                print(f"[proactive] loop error: {e}")

            # Prune stale alert keys every 2 hours to prevent unbounded growth
            if time.time() - _last_cleanup > 7200:
                self._alerted_events.clear()
                _last_cleanup = time.time()

            time.sleep(interval)
