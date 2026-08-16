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
            # Wait out brief write locks (dashboard edits, chat recording)
            # instead of instantly dropping the write with 'database is locked'.
            conn.execute("PRAGMA busy_timeout=5000")
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
            _migrate(conn)
            conn.commit()
            _conn = conn
            print(f"[memory] SQLite ready ({DB_PATH})"
                  + ("" if _has_fts else " — no FTS5, using LIKE search"))
        except Exception as e:
            print(f"[memory] SQLite unavailable — in-session memory only. ({e})")
            return None
    return _conn


def _migrate(conn):
    """Add columns introduced after the original schema shipped.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, so read the existing columns and
    only add what's missing. Safe to run on every open; never destructive.
    """
    wanted = {
        "session_summaries": [
            ("topics",    "TEXT NOT NULL DEFAULT ''"),   # comma-separated, for search
            ("started",   "TEXT NOT NULL DEFAULT ''"),   # ISO time the session began
            ("exchanges", "INTEGER NOT NULL DEFAULT 0"),  # how many turns it covered
        ],
    }
    for table, columns in wanted.items():
        try:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        if not have:
            continue                      # table doesn't exist yet — schema will make it
        for name, decl in columns:
            if name not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except Exception as e:
                    print(f"[memory] migration skipped ({table}.{name}): {e}")


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


# ---- Memory events ----------------------------------------------------------
# One emitter, and only one. Everything that changes what Ted knows announces
# it from inside the write function that did the changing — explicit "remember
# that…", background fact extraction, and the end-of-session summary all land
# here without knowing they have. Nothing outside this module decides what a
# memory event is.
#
# The reason it lives at the write and not at the call site: a caller that
# announces its own writes can announce one that failed, or forget to announce
# one that worked. Announcing from the row that was actually inserted or deleted
# makes the toast a report rather than a prediction — the same honesty rule the
# tools follow (§5.3).

_event_sink = None


def set_event_sink(fn):
    """Register the one consumer of memory events; core/app.py points it at the
    HUD. Pass None to unregister (the tests do, so they never touch a window)."""
    global _event_sink
    _event_sink = fn


def _fact_phrase(subject, relationship, obj):
    """('Charlie', 'LIKES', 'Chick-fil-A') → 'Charlie likes Chick-fil-A'."""
    rel = (relationship or "").replace("_", " ").strip().lower()
    return " ".join(p for p in ((subject or "").strip(), rel, (obj or "").strip()) if p)


def memory_event(kind, text, table="facts", row_id=None):
    """Announce one change to what Ted knows.

    kind: 'added' (the HUD says "Memory updated") or 'removed'.
    A sink that raises is swallowed — a memory write must never fail because
    the window that wanted to draw a toast has gone away.
    """
    text = (text or "").strip()
    if not text or kind not in ("added", "removed"):
        return
    print(f"[memory-event] {kind}: {text[:120]}")
    sink = _event_sink
    if sink is None:
        return
    try:
        sink({"kind": kind, "text": text, "table": table, "id": row_id})
    except Exception as e:
        print(f"[memory-event] sink failed: {e}")


# ---- Personal Conversation Memory ----

def save_memory(user_input, ted_reply, who="ted"):
    """Persist one exchange. No-op if the database can't be opened."""
    _exec("INSERT INTO exchanges (who, question, answer, ts) VALUES (?,?,?,?)",
          (who, user_input, ted_reply, _now()))


def get_memory(query, limit=3, who="ted", fallback_recent=False):
    """Return a short string of RELEVANT past exchanges, or '' if none.

    This used to end with "if nothing matched, return the most recent exchanges
    so the prompt always has some grounding context", and that fallback was
    costing about 300 tokens on every turn whose words matched nothing — which
    is most greetings and most short replies. Two problems with it:

    * The prompt already carries the last several messages as conversation
      history. "Recent exchanges" and "history" are the same information from
      two places, which is the duplication this codebase keeps getting bitten
      by, only here it is paid for in tokens on an 8,000-per-minute ceiling.
    * Irrelevant retrieved text is not neutral. It is context the model has to
      read, and it competes with the part of the prompt that matters.

    Returning nothing is now a valid, common, and cheap answer. The caller drops
    an empty block entirely, so an unmatched turn costs zero.

    ``fallback_recent=True`` restores the old behaviour for any caller that
    genuinely wants recency rather than relevance.
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
    if not rows and fallback_recent:
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

    cur = _exec("INSERT OR IGNORE INTO facts (subject, relationship, object, created) "
                "VALUES (?,?,?,?)", (subject, rel, obj, _now()))
    # Only announce a row that was really written. INSERT OR IGNORE quietly does
    # nothing on a duplicate, and the supersession above is part of THIS change
    # rather than a separate forgetting — so a replaced single-valued fact
    # reports one "updated", not an "updated" chased by a "removed".
    if cur is not None and cur.rowcount:
        memory_event("added", _fact_phrase(subject, rel, obj), "facts", cur.lastrowid)


def forget_fact(subject, relationship=None, obj=None):
    """Delete facts about a subject. Returns the number of rows removed.

    Called with just a subject it wipes everything known about them; narrow it
    with relationship and/or object to remove one specific thing.
    """
    where = " WHERE subject = ?"
    params = [subject]
    if relationship:
        where += " AND relationship = ?"
        params.append(_norm_rel(relationship))
    if obj:
        where += " AND lower(object) LIKE ?"
        params.append(f"%{_norm_obj(obj)}%")
    # Read the doomed rows before deleting them: after the DELETE there is
    # nothing left to name, and "Memory removed" with no subject is a worse
    # message than none at all.
    doomed = _query("SELECT subject, relationship, object FROM facts" + where,
                    tuple(params))
    cur = _exec("DELETE FROM facts" + where, tuple(params))
    n = cur.rowcount if cur is not None else 0
    if n:
        for s, r, o in doomed[:5]:
            memory_event("removed", _fact_phrase(s, r, o), "facts")
    return n


def forget_fact_by_rowid(rowid):
    """Delete exactly one fact, by the id its memory event carried.

    Exists so "forget that" removes the row Ted just announced rather than
    whatever a phrase happens to match. Matching by words is how you delete
    'Charlie lives in Spirit Lake' and take 'Charlie lives for hockey' with it.
    """
    if not rowid:
        return 0
    rows = _query("SELECT subject, relationship, object FROM facts WHERE rowid = ?",
                  (rowid,))
    cur = _exec("DELETE FROM facts WHERE rowid = ?", (rowid,))
    n = cur.rowcount if cur is not None else 0
    if n and rows:
        memory_event("removed", _fact_phrase(*rows[0]), "facts", rowid)
    return n


def get_facts_about(subject):
    """Return known facts about subject as a compact string, or ''.

    Newest first and capped at MAX_FACTS_IN_PROMPT so a long-lived database
    can't quietly crowd out the rest of the prompt.

    The encoding matters more than it looks. This used to emit one full triple
    per fact — "Charlie LIVES_IN Spirit Lake, Iowa Charlie STUDIES computer
    science Charlie ATTENDS ..." — repeating the subject 25 times and spelling
    every relationship in SCREAMING_SNAKE_CASE. That was 234 tokens on every
    single request, and roughly a third of it was the word "Charlie" and some
    underscores. The facts themselves are worth their space; their packaging
    was not.

    Now: the subject is stated once, relationships are lower-cased with the
    underscores removed, and facts sharing a relationship are merged. Same
    information, and it reads more like a sentence than a database dump, which
    the model handles at least as well.
    """
    rows = _query("SELECT relationship, object FROM facts WHERE subject = ? "
                  "ORDER BY created DESC LIMIT ?", (subject, MAX_FACTS_IN_PROMPT))
    if not rows:
        return ""
    grouped = {}
    for rel, obj in rows:
        grouped.setdefault(rel.replace("_", " ").lower(), []).append(obj)
    parts = [f"{rel} {', '.join(objs)}" for rel, objs in grouped.items()]
    return f"{subject}: " + "; ".join(parts)


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


# ---- Session memories (cross-session recall) ----
#
# One row per *session worth remembering*. Ted deliberately does NOT store a row
# for every launch — most sessions are testing and small talk, and a memory list
# full of "Charlie set a one minute timer" makes callbacks worse, not better.
# The filtering lives in llm.generate_session_summary(); this layer just stores
# what survives it.
#
# Rows are upserted by id so a long session gets ONE memory that keeps getting
# refined, rather than five near-duplicates from periodic flushes.

MAX_MEMORIES_IN_PROMPT = 6


def save_session_summary(summary_text, topics="", started="", exchanges=0, row_id=None):
    """Insert or update a session memory. Returns the row id, or None if the DB
    is unavailable.

    Pass the id returned by a previous call to update that same memory in place
    (used by the periodic flush so a crash can't lose the session, and the final
    write can't duplicate it).
    """
    if not (summary_text or "").strip():
        return row_id
    now = _now()
    if row_id:
        cur = _exec("UPDATE session_summaries SET text=?, topics=?, exchanges=?, created=? "
                    "WHERE id=?",
                    (summary_text, topics, exchanges, now, row_id))
        if cur is not None and cur.rowcount:
            # Deliberately silent. The periodic flush rewrites this same row
            # every few minutes of one session; announcing each pass would
            # toast the same memory over and over for a single memory.
            return row_id
        # Row vanished (db reset mid-session) — fall through and insert a fresh one.
    cur = _exec("INSERT INTO session_summaries (text, topics, started, exchanges, created) "
                "VALUES (?,?,?,?,?)",
                (summary_text, topics, started or now, exchanges, now))
    if cur is None:
        return None
    memory_event("added", summary_text, "session_summaries", cur.lastrowid)
    return cur.lastrowid


def get_last_session_summary(min_gap_hours=4.0):
    """Return the most recent summary written at least min_gap_hours ago, or ''.

    Kept for the startup recap line and the existing tests.
    """
    cutoff = (datetime.now() - timedelta(hours=min_gap_hours)).isoformat()
    rows = _query("SELECT text FROM session_summaries WHERE created <= ? "
                  "ORDER BY created DESC LIMIT 1", (cutoff,))
    return rows[0][0] if rows else ""


def _humanize_date(iso_ts):
    """'2026-08-09T21:14:02' → 'today' / 'yesterday' / 'last Tuesday' / 'Jul 12'."""
    try:
        when = datetime.fromisoformat(iso_ts)
    except Exception:
        return ""
    days = (_date_cls.today() - when.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"last {when.strftime('%A')}"
    if days < 365:
        return when.strftime("%b %-d") if os.name != "nt" else when.strftime("%b %d")
    return when.strftime("%b %Y")


def get_recent_memories(limit=MAX_MEMORIES_IN_PROMPT, max_age_days=60):
    """Return recent session memories, newest first.

    Each item: {"id", "text", "topics", "when", "created", "exchanges"}
    """
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    rows = _query("SELECT id, text, topics, created, exchanges FROM session_summaries "
                  "WHERE created >= ? ORDER BY created DESC LIMIT ?", (cutoff, limit))
    return [{"id": r[0], "text": r[1], "topics": r[2] or "",
             "created": r[3], "when": _humanize_date(r[3]), "exchanges": r[4] or 0}
            for r in rows]


def search_memories(query, limit=3):
    """Keyword search across past session memories AND chat-thread summaries.
    Returns the same shape as get_recent_memories(). Used when the user asks
    about an earlier conversation.

    Chat threads (the HUD sidebar) are recorded by the dashboard server into
    chat_sessions in this same database; searching their titles/summaries here
    is what lets 'when did I ask about rain?' find a different chat thread."""
    terms = _keywords(query)[:4]
    if not terms:
        return []
    where = " OR ".join(["text LIKE ? OR topics LIKE ?"] * len(terms))
    params = []
    for t in terms:
        params.extend([f"%{t}%", f"%{t}%"])
    params.append(limit)
    rows = _query(f"SELECT id, text, topics, created, exchanges FROM session_summaries "
                  f"WHERE {where} ORDER BY created DESC LIMIT ?", tuple(params))
    out = [{"id": r[0], "text": r[1], "topics": r[2] or "",
            "created": r[3], "when": _humanize_date(r[3]), "exchanges": r[4] or 0}
           for r in rows]

    # Chat-thread summaries (table exists once the dashboard/HUD has run)
    cwhere = " OR ".join(["title LIKE ? OR summary LIKE ?"] * len(terms))
    cparams = []
    for t in terms:
        cparams.extend([f"%{t}%", f"%{t}%"])
    cparams.append(limit)
    crows = _query(f"SELECT id, title, summary, updated FROM chat_sessions "
                   f"WHERE {cwhere} ORDER BY updated DESC LIMIT ?", tuple(cparams))
    for r in crows:
        text = (r[2] or r[1] or "").strip()
        if text:
            out.append({"id": f"chat-{r[0]}", "text": f"(chat: {r[1]}) {r[2] or ''}".strip(),
                        "topics": "", "created": r[3],
                        "when": _humanize_date(r[3]), "exchanges": 0})
    out.sort(key=lambda m: m["created"], reverse=True)
    return out[:limit]


def format_memories_for_prompt(limit=MAX_MEMORIES_IN_PROMPT):
    """Dated one-liners for injection into the per-turn context block, e.g.

        yesterday: Charlie was debugging why I kept talking over him…
        last Tuesday: We went through his Spotify setup…

    Returns '' when there's nothing worth injecting.
    """
    mems = get_recent_memories(limit=limit)
    if not mems:
        return ""
    return " | ".join(f"{m['when']}: {m['text']}" for m in mems)


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
