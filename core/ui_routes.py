"""Small, operational memory for UI routes that Ted has verified.

This is deliberately not personal memory.  It remembers that, for example,
Outlook's ``New mail`` control led to a compose form containing ``To``.  It
never stores text typed into a field, recipients, message bodies, or pixels.
"""

import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime

from core.paths import DATA


DB_PATH = os.environ.get("TED_UI_ROUTES_DB") or os.path.join(DATA, "ui_routes.db")
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_routes (
    scope          TEXT NOT NULL,
    route_key      TEXT NOT NULL,
    requested      TEXT NOT NULL,
    resolved       TEXT NOT NULL,
    expected       TEXT NOT NULL,
    successes      INTEGER NOT NULL DEFAULT 1,
    failures       INTEGER NOT NULL DEFAULT 0,
    last_success   TEXT NOT NULL,
    last_failure   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scope, route_key)
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def recall(scope, route_key):
    """Return a previously successful semantic route, or ``None``."""
    if not scope or not route_key:
        return None
    try:
        with _lock, closing(_connect()) as conn:
            row = conn.execute(
                "SELECT * FROM ui_routes WHERE scope=? AND route_key=?",
                (scope, route_key),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def remember(scope, route_key, requested, resolved, expected):
    """Upsert a route only after its expected destination was observed."""
    if not all((scope, route_key, requested, resolved, expected)):
        return False
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _lock, closing(_connect()) as conn:
            conn.execute(
                """INSERT INTO ui_routes
                   (scope,route_key,requested,resolved,expected,last_success)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(scope,route_key) DO UPDATE SET
                     requested=excluded.requested,
                     resolved=excluded.resolved,
                     expected=excluded.expected,
                     successes=ui_routes.successes+1,
                     failures=0,
                     last_success=excluded.last_success""",
                (scope, route_key, requested, resolved, expected, now),
            )
            conn.commit()
        return True
    except Exception:
        return False


def note_failure(scope, route_key):
    """Age out a stale route after two consecutive failed replays."""
    if not scope or not route_key:
        return
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _lock, closing(_connect()) as conn:
            conn.execute(
                """UPDATE ui_routes SET failures=failures+1,last_failure=?
                   WHERE scope=? AND route_key=?""",
                (now, scope, route_key),
            )
            conn.execute(
                "DELETE FROM ui_routes WHERE scope=? AND route_key=? AND failures>=2",
                (scope, route_key),
            )
            conn.commit()
    except Exception:
        pass
