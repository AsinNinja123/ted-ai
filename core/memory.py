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

# Relationships where the user can only have ONE current value. Learning a new
# one means the old one is wrong, not additional — so we replace instead of
# accumulating. Without this, "I moved to Ames" leaves the old LIVES_IN in place
# and Ted ends up holding two contradictory facts forever.
SINGLE_VALUED = {
    "LIVES_IN", "WORKS_AT", "IS_AGE", "AGE", "NAME", "IS_NAMED", "STUDIES_AT",
    "ATTENDS", "BIRTHDAY", "PHONE", "EMAIL", "DRIVES", "MAJORS_IN",
}

# Cap on how many facts get injected into the prompt. Unbounded growth would
# silently eat the context window as the database fills up.
MAX_FACTS_IN_PROMPT = 40


def _norm_rel(relationship):
    """Normalize a relationship name: uppercase, underscores, no stray punctuation."""
    r = (relationship or "").strip().upper().replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in r if ch.isalnum() or ch == "_") or "RELATED_TO"


def _norm_obj(obj):
    """Normalize a fact's object value for comparison (not for storage)."""
    return " ".join((obj or "").strip().lower().rstrip(".!,").split())


def save_fact(subject, relationship, obj):
    """Store a fact triple, e.g. save_fact('Charlie', 'STUDIES', 'CS').

    Three behaviours worth knowing:
      • The relationship is normalized, so 'lives in' and 'LIVES_IN' are one thing.
      • For SINGLE_VALUED relationships the new value REPLACES any old one, so
        Ted never holds two contradictory answers to the same question.
      • Near-duplicate objects ('Spirit Lake' vs 'Spirit Lake, Iowa') collapse to
        the more specific (longer) form rather than both being kept.
    """
    subject = (subject or "").strip()
    obj = (obj or "").strip()
    if not subject or not obj:
        return
    rel = _norm_rel(relationship)
    new_key = _norm_obj(obj)

    existing = _query("SELECT rowid, object FROM facts WHERE subject = ? AND relationship = ?",
                      (subject, rel))

    # Specificity check runs first, for single- and multi-valued alike. Two
    # extractions of one sentence often yield "Spirit Lake" and "Spirit Lake,
    # Iowa"; that's the same fact at two levels of detail, not a change of
    # address, so keep the more specific form regardless of which arrived last.
    for rowid, old in existing:
        old_key = _norm_obj(old)
        if old_key == new_key:
            return                          # exact duplicate — nothing to do
        if old_key in new_key:
            _exec("DELETE FROM facts WHERE rowid = ?", (rowid,))   # new is richer
        elif new_key in old_key:
            return                          # what we already have is richer

    if rel in SINGLE_VALUED:
        # Genuinely different value for a one-answer question — the old one is
        # now wrong, so clear whatever survived the specificity pass.
        for rowid, _old in _query(
                "SELECT rowid, object FROM facts WHERE subject = ? AND relationship = ?",
                (subject, rel)):
            _exec("DELETE FROM facts WHERE rowid = ?", (rowid,))

    _exec("INSERT OR IGNORE INTO facts (subject, relationship, object, created) "
          "VALUES (?,?,?,?)", (subject, rel, obj, _now()))


def forget_fact(subject, relationship=None, obj=None):
    """Delete facts about a subject. Returns the number of rows removed.

    Called with just a subject it wipes everything known about them; narrow it
    with relationship and/or object to remove one specific thing.
    """
    sql = "DELETE FROM facts WHERE subject = ?"
    params = [subject]
    if relationship:
        sql += " AND relationship = ?"
        params.append(_norm_rel(relationship))
    if obj:
        sql += " AND lower(object) LIKE ?"
        params.append(f"%{_norm_obj(obj)}%")
    cur = _exec(sql, tuple(params))
    return cur.rowcount if cur is not None else 0


def get_facts_about(subject):
    """Return a space-joined string of known facts about subject, or ''.

    Newest first and capped at MAX_FACTS_IN_PROMPT so a long-lived database
    can't quietly crowd out the rest of the prompt.
    """
    rows = _query("SELECT relationship, object FROM facts WHERE subject = ? "
                  "ORDER BY created DESC LIMIT ?", (subject, MAX_FACTS_IN_PROMPT))
    return " ".join(f"{subject} {r} {o}" for r, o in rows)


def list_facts(subject):
    """Return facts as a list of (relationship, object) tuples, newest first.
    Used by the spoken 'what do you know about me' command."""
    return _query("SELECT relationship, object FROM facts WHERE subject = ? "
                  "ORDER BY created DESC LIMIT ?", (subject, MAX_FACTS_IN_PROMPT))


def close():
    """Close the database connection. Safe to call when already closed."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


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
