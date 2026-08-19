"""
ted_daemon.py — Ted's calendar watch, running whether or not the HUD is open.

WHY THIS EXISTS
    core/proactive.py has always worked, but it ran as a thread inside the HUD
    process, so it died the moment the window closed. Every proactive feature
    was therefore only available while Ted was already on screen — which is
    exactly when you least need to be told about your next class.

SCOPE — deliberately narrow
    This daemon does ONE thing: it watches Calendar.app and posts a macOS
    notification for events starting within the next ~16 minutes.

    It does NOT fire user-defined triggers. Those can carry actions like "give
    me the rundown" that only mean something when TedApi is loaded and can
    speak, and half-executing them from a headless process would either
    produce useless notifications or a second, divergent command path. Triggers
    stay with the in-process scheduler in core/proactive.py. If the daemon
    should own them later, the honest way is to give it a real channel into the
    running HUD, not a copy of the dispatch logic.

    Delivery is a notification, not speech. Ted is used at college now; a
    laptop announcing your schedule out loud is the failure mode, not the
    feature.

RUNNING IT
    Foreground, for testing:   python ted_daemon.py --once   (one poll, verbose)
                               python ted_daemon.py          (loop, Ctrl-C to stop)
    As a launchd agent:        bash tools/install_daemon.sh
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 28 (§28.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The calendar watch, running as a separate program under launchd (macOS's
#      service manager) rather than as a thread inside Ted's window.
#
#      That is the entire point. A thread inside the window dies when you close the
#      window — which is exactly when being told about your next class matters most.
#
#  DELIBERATELY NARROW
#      It watches Calendar.app and posts a macOS notification for events starting in
#      the next ~16 minutes. It does NOT fire user-defined triggers, because those
#      can carry actions that only mean something when the assistant is running.
#
#  STATUS, HONESTLY
#      The logic is unit-tested; the launchd install has never been verified on
#      macOS. The likely failure is permissions — macOS gates AppleEvents per
#      calling binary, and a launchd-spawned python is a different caller from your
#      terminal. §35.
#
# =============================================================================

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOME)

from core.proactive import get_upcoming_events, HEARTBEAT_FILE  # noqa: E402

STATE_FILE = os.path.join(HOME, "data", "daemon_alerted.json")
LOOKAHEAD_MINUTES = 16
POLL_SECONDS = 60
STATE_TTL_SECONDS = 6 * 3600      # forget an alerted event 6 h after it started


def log(message: str) -> None:
    """Timestamped line to stdout. launchd redirects this to data/ted_daemon.log."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


# ── de-dupe state ────────────────────────────────────────────────────────────
# Kept on disk rather than in memory: launchd restarts this process (on crash,
# on logout, on reboot), and an in-memory set would re-announce every upcoming
# event on each restart. That is the difference between a useful daemon and one
# you turn off.

def _load_alerted() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alerted(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)      # atomic: never leave a half-written file
    except Exception as e:
        log(f"[state] save failed: {e}")


def _prune(state: dict, now: float) -> dict:
    return {k: v for k, v in state.items() if now - float(v) < STATE_TTL_SECONDS}


# ── delivery ─────────────────────────────────────────────────────────────────

def notify(title: str, message: str) -> bool:
    """Post a macOS notification. Returns True if osascript accepted it.

    Text is passed as an argument, never interpolated into the script source,
    so an event titled with a quote cannot break or inject AppleScript.
    """
    script = (
        'on run argv\n'
        '  display notification (item 2 of argv) '
        'with title "Ted" subtitle (item 1 of argv)\n'
        'end run'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script, title, message],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log(f"[notify] osascript failed: {result.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:
        log(f"[notify] failed: {e}")
        return False


# ── the watch ────────────────────────────────────────────────────────────────

def check_calendar(state: dict, verbose: bool = False) -> int:
    """Notify for events starting soon. Returns how many notifications fired."""
    now = time.time()
    try:
        events = get_upcoming_events(lookahead_minutes=LOOKAHEAD_MINUTES)
    except Exception as e:
        log(f"[calendar] fetch failed: {e}")
        return 0

    if verbose:
        log(f"[calendar] {len(events)} event(s) in the next {LOOKAHEAD_MINUTES} min")

    fired = 0
    for event in events:
        title = event.get("title", "")
        start_epoch = event.get("start_epoch")
        location = event.get("location", "")
        if not title or not start_epoch:
            continue

        key = f"{title}_{int(start_epoch)}"
        if key in state:
            if verbose:
                log(f"[calendar] already alerted: {title}")
            continue

        # round, not int: truncation turns an event 11.9 minutes out into
        # "in 11 minutes", and every alert reads one minute early.
        minutes_away = round((start_epoch - now) / 60)
        if not (0 <= minutes_away <= LOOKAHEAD_MINUTES):
            continue

        if minutes_away <= 1:
            message = "Starting now."
        else:
            message = f"In {minutes_away} minutes."
        if location:
            message += f" {location}"

        if notify(title, message):
            state[key] = now
            fired += 1
            log(f"[calendar] notified: {title} ({minutes_away} min)")

    return fired


def heartbeat() -> None:
    """Touch a file so the in-process scheduler knows to stand down.

    Without this, opening the HUD while the daemon runs gives two calendar
    watchers and two alerts per event. core.proactive.daemon_alive() reads it.
    """
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception as e:
        log(f"[heartbeat] write failed: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ted's calendar daemon.")
    parser.add_argument("--once", action="store_true",
                        help="run a single poll with verbose output, then exit")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS,
                        help=f"seconds between polls (default {POLL_SECONDS})")
    args = parser.parse_args()

    log(f"[daemon] starting (pid {os.getpid()}, interval {args.interval}s)")
    state = _prune(_load_alerted(), time.time())

    if args.once:
        heartbeat()
        fired = check_calendar(state, verbose=True)
        _save_alerted(state)
        log(f"[daemon] single poll done — {fired} notification(s)")
        return 0

    while True:
        try:
            heartbeat()
            before = len(state)
            check_calendar(state)
            state = _prune(state, time.time())
            if len(state) != before:
                _save_alerted(state)
        except Exception as e:
            log(f"[daemon] loop error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
