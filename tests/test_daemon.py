"""Calendar daemon: de-dupe, heartbeat handover, and notification safety.

No AppleScript runs here — osascript is stubbed, so this suite is portable and
says nothing about whether Calendar access actually works on the Mac. That
part is verified by hand; see docs/DAEMON_HANDOFF.md.

Run with: venv/bin/python tests/test_daemon.py
"""

import os
import sys
import time
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import proactive
import ted_daemon

PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


tmp = tempfile.mkdtemp()
proactive.HEARTBEAT_FILE = os.path.join(tmp, "daemon_heartbeat")
ted_daemon.HEARTBEAT_FILE = proactive.HEARTBEAT_FILE
ted_daemon.STATE_FILE = os.path.join(tmp, "daemon_alerted.json")

# Nothing in this suite may reach AppleScript.
SENT = []
ted_daemon.notify = lambda title, message: (SENT.append((title, message)), True)[1]


def fake_events(*events):
    ted_daemon.get_upcoming_events = lambda lookahead_minutes=16: list(events)


def event(title, minutes_away, location=""):
    return {"title": title, "start_epoch": time.time() + minutes_away * 60,
            "location": location}


print("— the daemon does not repeat itself —")

state = {}
fake_events(event("CS 240 Lecture", 12, "Science 118"))
check("an upcoming event notifies once", ted_daemon.check_calendar(state) == 1)
check("…with the minutes and the room", SENT[-1] == ("CS 240 Lecture", "In 12 minutes. Science 118"))
check("the same event on the next poll is silent", ted_daemon.check_calendar(state) == 0)

SENT.clear()
fake_events(event("Standup", 0))
check("an event starting now says so", ted_daemon.check_calendar(state) == 1
      and SENT[-1][1].startswith("Starting now"))

fake_events(event("Next week", 4000))
check("an event outside the window is ignored", ted_daemon.check_calendar(state) == 0)

fake_events({"title": "", "start_epoch": time.time()},
            {"title": "No start", "start_epoch": None})
check("malformed events are skipped, not crashed on",
      ted_daemon.check_calendar(state) == 0)

print("\n— de-dupe survives a restart —")

# The reason this state is on disk at all: launchd restarts the process, and an
# in-memory set would re-announce every upcoming event each time it did.
ted_daemon._save_alerted(state)
reloaded = ted_daemon._load_alerted()
check("alerted events persist to disk", reloaded == state and len(reloaded) > 0)
fake_events(event("CS 240 Lecture", 12, "Science 118"))
check("…so a restarted daemon stays quiet about them",
      ted_daemon.check_calendar(dict(reloaded)) == 0)

old = {"stale_event": time.time() - ted_daemon.STATE_TTL_SECONDS - 10,
       "fresh_event": time.time()}
pruned = ted_daemon._prune(old, time.time())
check("state older than the TTL is pruned", list(pruned) == ["fresh_event"])

print("\n— handover between daemon and HUD —")

check("no heartbeat file means the HUD keeps the calendar watch",
      not proactive.daemon_alive())
ted_daemon.heartbeat()
check("a fresh heartbeat hands the watch to the daemon", proactive.daemon_alive())
with open(proactive.HEARTBEAT_FILE, "w") as f:
    f.write(str(time.time() - proactive.HEARTBEAT_STALE_SECONDS - 1))
check("a stale heartbeat hands it back, so alerts never stop",
      not proactive.daemon_alive())
with open(proactive.HEARTBEAT_FILE, "w") as f:
    f.write("not a number")
check("a corrupt heartbeat fails safe toward the HUD", not proactive.daemon_alive())


class _FakeApi:
    muted = False
    _alerted = None


sched = proactive.ProactiveScheduler(_FakeApi(), speak_fn=None, add_message_fn=None)
sched._alerted_events = set()
called = []
proactive.get_upcoming_events = lambda lookahead_minutes=16: called.append(1) or []
ted_daemon.heartbeat()
sched._check_calendar()
check("the in-process scheduler stands down while the daemon is alive",
      called == [])
os.remove(proactive.HEARTBEAT_FILE)
sched._check_calendar()
check("…and resumes the moment the daemon stops", called == [1])

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
