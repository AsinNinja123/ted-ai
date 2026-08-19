"""core/bouncer.py — who gets through, and who is left alone.

Charlie's description was precise: tell him Gavin texted and offer to read it
aloud or open it, and ignore the rest. So this is a doorman, not a feed. The
default posture is **silence**, and getting announced is something a sender
earns by being on the list.

That default is the important decision. A bouncer that announces everything by
default is a notification centre with extra steps, and the first unknown
short-code that interrupts Charlie mid-lecture is the last day he leaves it on.

Rules live beside everything else in SQLite so the dashboard and the running
HUD see the same list.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 25 (§25.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      A doorman for incoming texts, not a notification feed. The default posture is
#      SILENCE: getting announced is something a sender earns by being on the list.
#
#      That default is the whole design. A bouncer that announces everything is a
#      notification centre with extra steps, and the first unknown short-code that
#      interrupts you mid-lecture is the last day you leave it on.
#
#  WHERE THE RULES LIVE
#      Beside everything else, in data/memory.db, so the dashboard and the running
#      window see the same list. No second source of truth (§34).
#
# =============================================================================

from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime

from core.paths import DATA

DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")

# announce -> say who it was and offer to read it
# ignore   -> never mention, not even a badge
MODES = ("announce", "ignore")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bouncer_rules (
    id      INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL UNIQUE,
    mode    TEXT NOT NULL DEFAULT 'announce',
    note    TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bouncer_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_lock = threading.Lock()


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _digits(text):
    return re.sub(r"\D", "", text or "")


def allow(pattern, mode="announce", note=""):
    """Add or update a rule. `pattern` is a name, phone number, or email."""
    pattern = " ".join(str(pattern or "").split())
    if not pattern:
        return None, "I need a name or number"
    if mode not in MODES:
        return None, f"mode must be one of {', '.join(MODES)}"
    try:
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT INTO bouncer_rules (pattern, mode, note, created) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(pattern) DO UPDATE SET mode=excluded.mode, "
                "note=excluded.note",
                (pattern, mode, note, _now()))
            conn.commit()
    except Exception as exc:
        return None, f"couldn't save that rule ({exc})"
    return {"pattern": pattern, "mode": mode, "note": note}, ""


def forget(pattern):
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM bouncer_rules WHERE lower(pattern)=lower(?)",
                           (str(pattern or ""),))
        conn.commit()
    return cur.rowcount > 0


def rules():
    try:
        with _lock, _connect() as conn:
            return [{"id": r[0], "pattern": r[1], "mode": r[2], "note": r[3]}
                    for r in conn.execute(
                        "SELECT id,pattern,mode,note FROM bouncer_rules "
                        "ORDER BY mode, pattern")]
    except Exception as exc:
        print(f"[bouncer] could not read rules: {exc}")
        return []


def get_state(key, default=""):
    try:
        with _lock, _connect() as conn:
            row = conn.execute("SELECT value FROM bouncer_state WHERE key=?",
                               (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_state(key, value):
    try:
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT INTO bouncer_state (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            conn.commit()
    except Exception as exc:
        print(f"[bouncer] could not save state: {exc}")


def enabled():
    """Off until Charlie turns it on. Reading his texts is not a default."""
    return get_state("enabled", "0") == "1"


def set_enabled(on):
    set_state("enabled", "1" if on else "0")
    return bool(on)


def _matches(rule_pattern, handle, name):
    """Does this rule cover this sender?"""
    pattern = (rule_pattern or "").strip().lower()
    if not pattern:
        return False
    if pattern == "*":
        return True
    handle_l = (handle or "").strip().lower()
    name_l = (name or "").strip().lower()
    if pattern in (handle_l, name_l):
        return True
    # A name rule should match "Gavin" against "Gavin Meyer".
    if name_l and pattern in name_l.split():
        return True
    if name_l and name_l.startswith(pattern + " "):
        return True
    # Phone numbers are written a dozen ways; compare the last ten digits.
    pattern_digits, handle_digits = _digits(pattern), _digits(handle_l)
    if len(pattern_digits) >= 7 and len(handle_digits) >= 7:
        return pattern_digits[-10:] == handle_digits[-10:]
    return False


def decide(handle, name=""):
    """Should this sender be announced? Returns (announce, reason).

    Silence is the default and an explicit ignore always wins, so adding
    "announce everyone" (*) and then ignoring one number does what it reads
    like rather than the reverse.
    """
    if not enabled():
        return False, "the bouncer is off"
    matched = [r for r in rules() if _matches(r["pattern"], handle, name)]
    if not matched:
        return False, "not on the list"
    for rule in matched:
        if rule["mode"] == "ignore":
            return False, f"{rule['pattern']} is on the ignore list"
    return True, f"{matched[0]['pattern']} is on the announce list"


def describe_rules():
    """A sentence Ted can say about the current door policy."""
    if not rules():
        return ("Nobody is on the list yet, so I won't announce anything. "
                "Tell me who to watch for.")
    announce = [r["pattern"] for r in rules() if r["mode"] == "announce"]
    ignore = [r["pattern"] for r in rules() if r["mode"] == "ignore"]
    parts = []
    if announce:
        parts.append("I'll tell you about texts from " + ", ".join(announce))
    if ignore:
        parts.append("I'll stay quiet about " + ", ".join(ignore))
    state = "on" if enabled() else "off (say 'turn on the bouncer')"
    return ". ".join(parts) + f". The bouncer is {state}."
