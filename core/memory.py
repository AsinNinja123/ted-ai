"""
core/memory.py — Ted's long-term memory, backed by a local SQLite file.

Replaces the old Neo4j graph backend. Neo4j Desktop was almost never running,
so memory silently fell back to in-session only; SQLite needs no server, so
long-term memory now simply always works. The database lives at
data/memory.db (WAL mode, safe across Ted's daemon threads).

Public API is unchanged from the Neo4j version: every function degrades
gracefully (returns [], "", None, False) and never raises.

Retrieval: FTS5 full-text search over past exchanges when available, LIKE
keyword match otherwise; falls back to the most recent exchanges when nothing
matches, so there's always some grounding context.
"""

import os
import sqlite3
import threading
import time
from datetime import date as _date_cls, datetime, timedelta

from core.paths import DATA

try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"

DB_PATH = os.path.join(DATA, "memory.db")

_conn = None
_lock = threading.Lock()          # sqlite objects are shared across Ted's threads
_has_fts = False

# Tiny stop-word list so keyword search isn't dominated by "the", "what", etc.
_STOP = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "to",
    "of", "in", "on", "at", "for", "with", "do", "did", "does", "you", "i", "me",
    "my", "your", "it", "this", "that", "what", "who", "when", "where", "how",
    "can", "could", "would", "should", "tell", "about", "please", "hey", "ted",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id       INTEGER PRIMARY KEY,
    who      TEXT NOT NULL DEFAULT 'ted',      -- 'ted' | 'store'
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    ts       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    subject      TEXT NOT NULL,
    relationship TEXT NOT NULL,
    object       TEXT NOT NULL,
    created      TEXT NOT NULL,
    UNIQUE(subject, relationship, object)
);
CREATE TABLE IF NOT EXISTS goals (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    description    TEXT DEFAULT '',
    created        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    last_mentioned TEXT,
    completed_at   TEXT
);
CREATE TABLE IF NOT EXISTS patterns (
    topic      TEXT NOT NULL,
    hour       INTEGER NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE(topic, hour)
);
CREATE TABLE IF NOT EXISTS session_summaries (
    id      INTEGER PRIMARY KEY,
    text    TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_logs (
    habit   TEXT NOT NULL,
    date    TEXT NOT NULL,
    created TEXT NOT NULL,
    UNIQUE(habit, date)
);
CREATE INDEX IF NOT EXISTS idx_exchanges_ts ON exchanges(who, ts DESC);
"""


def _get_driver():
    """Open the SQLite connection once, lazily. Returns the connection or None.

    Kept under the old name because core/app.py's health watcher calls
    memory._get_driver() to light the MEMORY dot on the HUD.
    """
    global _conn, _has_fts
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        try:
            os.makedirs(DATA, exist_ok=True)
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_fts USING fts5("
                    "question, answer, content='exchanges', content_rowid='id')"
                )
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS exchanges_ai AFTER INSERT ON exchanges BEGIN
                        INSERT INTO exchanges_fts(rowid, question, answer)
                        VALUES (new.id, new.question, new.answer);
                    END""")
                _has_fts = True
            except sqlite3.OperationalError:
                _has_fts = False    # this Python's sqlite lacks FTS5 — LIKE fallback
            conn.commit()
            _conn = conn
            print(f"[memory] SQLite ready ({DB_PATH})"
                  + ("" if _has_fts else " — no FTS5, using LIKE search"))
        except Exception as e:
            print(f"[memory] SQLite unavailable — in-session memory only. ({e})")
            return None
    return _conn


def _exec(sql, params=()):
    """Run a write statement under the module lock. Returns the cursor or None."""
    conn = _get_driver()
    if conn is None:
        return None
    try:
        with _lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
    except Exception as e:
        print(f"[memory] write skipped: {e}")
        return None


def _query(sql, params=()):
    """Run a read statement. Returns a list of rows ([] on any failure)."""
    conn = _get_driver()
    if conn is None:
        return []
    try:
        with _lock:
            return conn.execute(sql, params).fetchall()
    except Exception as e:
        print(f"[memory] lookup skipped: {e}")
        return []


def _keywords(text):
    """Extract meaningful search terms from text, stripping stop-words and punctuation."""
    words = [w.strip(".,!?;:'\"").lower() for w in text.split()]
    return [w for w in words if len(w) > 3 and w not in _STOP]


def _now():
    return datetime.now().isoformat()


# ---- Personal Conversation Memory ----

def save_memory(user_input, ted_reply, who="ted"):
    """Persist one exchange. No-op if the database can't be opened."""
    _exec("INSERT INTO exchanges (who, question, answer, ts) VALUES (?,?,?,?)",
          (who, user_input, ted_reply, _now()))


def get_memory(query, limit=3, who="ted"):
    """Return a short string of relevant past exchanges, or '' if none.

    Keyword search first (FTS5 when available); if nothing matches, returns the
    most recent exchanges so the prompt always has some grounding context.
    """
    keywords = _keywords(query)
    rows = []
    if keywords and _has_fts:
        # FTS5: OR-join the keywords; quotes guard against operator characters
        q = " OR ".join('"' + k.replace('"', "") + '"' for k in keywords)
        rows = _query(
            "SELECT e.question, e.answer FROM exchanges_fts f "
            "JOIN exchanges e ON e.id = f.rowid "
            "WHERE exchanges_fts MATCH ? AND e.who = ? "
            "ORDER BY e.ts DESC LIMIT ?", (q, who, limit))
    if keywords and not rows:
        like = " OR ".join("lower(question) LIKE ?" for _ in keywords)
        rows = _query(
            f"SELECT question, answer FROM exchanges WHERE who = ? AND ({like}) "
            "ORDER BY ts DESC LIMIT ?",
            (who, *[f"%{k}%" for k in keywords], limit))
    if not rows:
        rows = _query("SELECT question, answer FROM exchanges WHERE who = ? "
                      "ORDER BY ts DESC LIMIT ?", (who, limit))
    return "\n".join(f"{OWNER_NAME} said: {q} — Ted replied: {a}" for q, a in rows)


# ---- Facts ----

def save_fact(subject, relationship, obj):
    """Store a fact triple, e.g. save_fact('Charlie', 'STUDIES', 'CS').
    Re-stating a known fact is a no-op (UNIQUE constraint)."""
    _exec("INSERT OR IGNORE INTO facts (subject, relationship, object, created) "
          "VALUES (?,?,?,?)", (subject, relationship, obj, _now()))


def get_facts_about(subject):
    """Return a space-joined string of all known facts about subject, or ''."""
    rows = _query("SELECT relationship, object FROM facts WHERE subject = ?", (subject,))
    return " ".join(f"{subject} {r} {o}" for r, o in rows)


def close():
    """Close the database connection. Safe to call when already closed."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


# ---- Store-mode namespace (kept for API compatibility) ----

def save_store_memory(user_input, store_reply):
    save_memory(user_input, store_reply, who="store")


def get_store_memory(query, limit=3):
    return get_memory(query, limit=limit, who="store")


# ---- Goal tracking ----

def _norm_goal(name):
    """Normalize a goal name for deduplication: lowercase, strip punctuation."""
    import re as _re
    return _re.sub(r"[^\w\s]", "", name.lower()).strip()


def save_goal(name, description=""):
    """Save or update a goal; deduplicates on fuzzy (substring) name overlap."""
    name_norm = _norm_goal(name)
    if not name_norm:
        return
    rows = _query("SELECT id, name FROM goals")
    for gid, gname in rows:
        gnorm = _norm_goal(gname)
        if gnorm and (name_norm in gnorm or gnorm in name_norm):
            # Already tracked — bump last_mentioned and re-activate
            _exec("UPDATE goals SET last_mentioned = ?, status = 'active' WHERE id = ?",
                  (_now(), gid))
            return
    _exec("INSERT OR IGNORE INTO goals (name, description, created, status, last_mentioned) "
          "VALUES (?,?,?,'active',?)", (name, description, _now(), _now()))


def get_goals(active_only=True):
    """Return goal dicts: name, description, created (+status when active_only=False)."""
    if active_only:
        rows = _query("SELECT name, description, created, last_mentioned FROM goals "
                      "WHERE status = 'active' ORDER BY created DESC")
        return [{"name": n, "description": d, "created": c, "last_mentioned": lm}
                for n, d, c, lm in rows]
    rows = _query("SELECT name, description, created, status FROM goals "
                  "ORDER BY created DESC")
    return [{"name": n, "description": d, "created": c, "status": s}
            for n, d, c, s in rows]


def complete_goal(name):
    """Mark a goal as completed (case-insensitive partial match). True if found."""
    cur = _exec("UPDATE goals SET status = 'completed', completed_at = ? "
                "WHERE status = 'active' AND lower(name) LIKE ?",
                (_now(), f"%{name.lower()}%"))
    return bool(cur and cur.rowcount)


def goals_needing_checkin(days=3):
    """Return active goals not mentioned in the last `days` days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _query("SELECT name, created FROM goals WHERE status = 'active' "
                  "AND (last_mentioned IS NULL OR last_mentioned < ?) "
                  "ORDER BY created ASC", (cutoff,))
    return [{"name": n, "created": c} for n, c in rows]


# ---- Pattern tracking (topic × hour-of-day counts) ----

def log_pattern(topic, hour_of_day):
    """Record that a topic was raised at this hour of day."""
    ts = _now()
    _exec("INSERT INTO patterns (topic, hour, count, first_seen, last_seen) "
          "VALUES (?,?,1,?,?) "
          "ON CONFLICT(topic, hour) DO UPDATE SET count = count + 1, last_seen = ?",
          (topic[:80], hour_of_day, ts, ts, ts))


def get_frequent_patterns(min_count=3):
    """Return patterns seen at least min_count times, most frequent first."""
    rows = _query("SELECT topic, hour, count FROM patterns WHERE count >= ? "
                  "ORDER BY count DESC LIMIT 10", (min_count,))
    return [{"topic": t, "hour": h, "count": c} for t, h, c in rows]


# ---- Session summaries (cross-session recall) ----

def save_session_summary(summary_text):
    _exec("INSERT INTO session_summaries (text, created) VALUES (?,?)",
          (summary_text, _now()))


def get_last_session_summary(min_gap_hours=4.0):
    """Return the most recent summary written at least min_gap_hours ago, or ''."""
    cutoff = (datetime.now() - timedelta(hours=min_gap_hours)).isoformat()
    rows = _query("SELECT text FROM session_summaries WHERE created <= ? "
                  "ORDER BY created DESC LIMIT 1", (cutoff,))
    return rows[0][0] if rows else ""


# ---- Habit tracking (daily streaks) ----

def log_habit(name):
    """Record a habit completion for today (idempotent).
    Returns True if this is a new log today, False if already logged."""
    cur = _exec("INSERT OR IGNORE INTO habit_logs (habit, date, created) VALUES (?,?,?)",
                (name.lower(), _date_cls.today().isoformat(), _now()))
    if cur is None:
        return True          # degraded mode — behave like the old backend
    return bool(cur.rowcount)


def get_habit_streak(name):
    """Return {name, streak, last_logged} for a habit, or None if never logged."""
    rows = _query("SELECT date FROM habit_logs WHERE habit = ? "
                  "ORDER BY date DESC LIMIT 100", (name.lower(),))
    if not rows:
        return None
    today = _date_cls.today()
    dates = sorted({_date_cls.fromisoformat(r[0]) for r in rows}, reverse=True)
    last = dates[0]
    # Only count a live streak if the last log was today or yesterday
    if last < today - timedelta(days=1):
        return {"name": name, "streak": 0, "last_logged": last.isoformat()}
    streak = 0
    expected = last
    for d in dates:
        if d == expected:
            streak += 1
            expected = d - timedelta(days=1)
        else:
            break
    return {"name": name, "streak": streak, "last_logged": last.isoformat()}


def get_all_habits():
    """Return all tracked habits with streak info."""
    rows = _query("SELECT DISTINCT habit FROM habit_logs ORDER BY habit")
    habits = []
    for (name,) in rows:
        info = get_habit_streak(name)
        habits.append(info if info else {"name": name, "streak": 0, "last_logged": None})
    return habits
