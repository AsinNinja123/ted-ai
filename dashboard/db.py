"""
dashboard/db.py — schema, provenance, and audit plumbing for Ted's memory dashboard.

Everything here operates on the SAME data/memory.db that core/memory.py uses.
The audit log is implemented as SQLite triggers stored in the database file
itself, which means Ted's own writes (from core/memory.py, a different
process) get logged too — not just edits made through the dashboard.

Actor attribution works through a one-row `audit_context` table. It defaults
to 'ted'. The dashboard flips it to 'user' inside its own (uncommitted)
transaction while writing, then flips it back before committing — so a
concurrent write from Ted's process never sees the 'user' flag.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

# Same resolution as core/paths.py, without importing config side effects.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("TED_DB") or os.path.join(_ROOT, "data", "memory.db")

_lock = threading.Lock()
_conn = None

# ---------------------------------------------------------------------------
# Table registry — what the dashboard is allowed to touch, and how.
# pk: primary-key expression usable in WHERE. cols: displayed. editable:
# accepted from the API. text_cols: searched with LIKE.
# ---------------------------------------------------------------------------
TABLES = {
    "facts": {
        "pk": "rowid",
        "cols": ["subject", "relationship", "object", "writer", "created"],
        "editable": ["subject", "relationship", "object"],
        "required": ["subject", "relationship", "object"],
        "text_cols": ["subject", "relationship", "object"],
        "order": "created DESC",
    },
    "session_summaries": {
        "pk": "id",
        "cols": ["text", "topics", "exchanges", "writer", "created"],
        "editable": ["text", "topics"],
        "required": ["text"],
        "text_cols": ["text", "topics"],
        "order": "created DESC",
    },
    "exchanges": {
        "pk": "id",
        "cols": ["who", "question", "answer", "ts"],
        "editable": ["question", "answer"],
        "required": ["question", "answer"],
        "text_cols": ["question", "answer"],
        "order": "ts DESC",
    },
    "goals": {
        "pk": "id",
        "cols": ["name", "description", "status", "created"],
        "editable": ["name", "description", "status"],
        "required": ["name"],
        "text_cols": ["name", "description", "status"],
        "order": "created DESC",
    },
}

# Columns captured into the audit log's old/new JSON snapshots.
_AUDIT_COLS = {
    "facts": ["subject", "relationship", "object"],
    "session_summaries": ["text", "topics"],
    "exchanges": ["who", "question", "answer"],
    "goals": ["name", "description", "status"],
}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def get_conn():
    """Open (once) the shared connection: WAL, busy timeout, schema ensured."""
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        ensure_schema(conn)
        _conn = conn
    return _conn


# ---------------------------------------------------------------------------
# Schema: provenance columns, audit table, triggers.
# ---------------------------------------------------------------------------

def _add_missing_columns(conn, table, columns):
    """ALTER TABLE ADD COLUMN for anything not already there. Never destructive."""
    try:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return
    if not have:
        return
    for name, decl in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _json_obj(prefix, table):
    """Build a json_object(...) SQL expression over the audit columns."""
    parts = []
    for c in _AUDIT_COLS[table]:
        parts.append(f"'{c}', {prefix}.{c}")
    return "json_object(" + ", ".join(parts) + ")"


def _make_audit_triggers(conn, table):
    pk = "new.rowid" if TABLES[table]["pk"] == "rowid" else "new.id"
    pk_old = pk.replace("new.", "old.")
    actor = "COALESCE((SELECT actor FROM audit_context WHERE id = 1), 'ted')"
    ts = "datetime('now', 'localtime')"
    new_j, old_j = _json_obj("new", table), _json_obj("old", table)

    conn.execute(f"""
        CREATE TRIGGER IF NOT EXISTS audit_{table}_ai AFTER INSERT ON {table} BEGIN
            INSERT INTO memory_audit (ts, actor, action, table_name, row_key, new_value)
            VALUES ({ts}, {actor}, 'created', '{table}', {pk}, {new_j});
        END""")
    conn.execute(f"""
        CREATE TRIGGER IF NOT EXISTS audit_{table}_au AFTER UPDATE ON {table} BEGIN
            INSERT INTO memory_audit (ts, actor, action, table_name, row_key, old_value, new_value)
            VALUES ({ts}, {actor}, 'edited', '{table}', {pk}, {old_j}, {new_j});
        END""")
    conn.execute(f"""
        CREATE TRIGGER IF NOT EXISTS audit_{table}_ad AFTER DELETE ON {table} BEGIN
            INSERT INTO memory_audit (ts, actor, action, table_name, row_key, old_value)
            VALUES ({ts}, {actor}, 'forgotten', '{table}', {pk_old}, {old_j});
        END""")


def ensure_schema(conn):
    """Idempotent. Safe to run on every dashboard start, and safe for Ted:
    core/memory.py uses explicit column lists everywhere, so added columns
    with defaults never break its inserts."""

    # Core tables might not all exist on a fresh DB — memory.py's schema is
    # authoritative; only create what the dashboard itself introduces, plus
    # the goals table that exists in the live DB but not in memory.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id             INTEGER PRIMARY KEY,
            name           TEXT NOT NULL UNIQUE,
            description    TEXT DEFAULT '',
            created        TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'active',
            last_mentioned TEXT,
            completed_at   TEXT
        )""")

    # Provenance (handoff spec: source session, timestamp, writer, confidence).
    _add_missing_columns(conn, "facts", [
        ("writer", "TEXT NOT NULL DEFAULT 'ted'"),
        ("confidence", "REAL NOT NULL DEFAULT 1.0"),
        ("source_session", "TEXT NOT NULL DEFAULT ''"),
        ("updated", "TEXT NOT NULL DEFAULT ''"),
    ])
    _add_missing_columns(conn, "session_summaries", [
        ("writer", "TEXT NOT NULL DEFAULT 'ted'"),
    ])

    # Actor context for trigger attribution.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_context (
            id    INTEGER PRIMARY KEY CHECK (id = 1),
            actor TEXT NOT NULL DEFAULT 'ted'
        )""")
    conn.execute("INSERT OR IGNORE INTO audit_context (id, actor) VALUES (1, 'ted')")

    # Chat sessions — the HUD's Claude-style sidebar. Deliberately NOT audited:
    # every turn of every chat would drown the memory history in noise.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id      INTEGER PRIMARY KEY,
            title   TEXT NOT NULL DEFAULT 'New chat',
            summary TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            hidden  INTEGER NOT NULL DEFAULT 0
        )""")
    # Deleting a chat from the sidebar hides the thread; it does not destroy
    # what Ted learned in it. The turns, any session summary, and any fact
    # extracted from it all stay exactly where they were, and core/memory.py's
    # search_memories still reads this table unfiltered on purpose — that is
    # what keeps "he still remembers it" true after a delete.
    _add_missing_columns(conn, "chat_sessions", [
        ("hidden", "INTEGER NOT NULL DEFAULT 0"),
    ])
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_turns (
            id         INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            role       TEXT NOT NULL,          -- 'user' | 'ted'
            content    TEXT NOT NULL,
            ts         TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_session "
                 "ON chat_turns(session_id, id)")

    # Full-text search over chat turns. Same shape as memory.py's exchanges_fts
    # — an external-content FTS5 index plus triggers that keep it in step — so
    # there is one pattern for searchable text in this database rather than two.
    # The index lives here because this is where chat_turns is defined; the
    # reader lives in core/memory.py, which is where Ted's retrieval lives.
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chat_turns_fts USING fts5("
            "content, content='chat_turns', content_rowid='id')")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chat_turns_ai AFTER INSERT ON chat_turns BEGIN
                INSERT INTO chat_turns_fts(rowid, content) VALUES (new.id, new.content);
            END""")
        # Delete and update complete the contract. exchanges_fts shipped without
        # them and drifted; there is no reason to repeat that here.
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chat_turns_ad AFTER DELETE ON chat_turns BEGIN
                INSERT INTO chat_turns_fts(chat_turns_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END""")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chat_turns_au AFTER UPDATE ON chat_turns BEGIN
                INSERT INTO chat_turns_fts(chat_turns_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO chat_turns_fts(rowid, content) VALUES (new.id, new.content);
            END""")
        # Every turn written before the index existed is invisible to it, which
        # would make search quietly useless on exactly the history worth
        # searching. Rebuild when the counts disagree.
        indexed = conn.execute("SELECT COUNT(*) FROM chat_turns_fts").fetchone()[0]
        actual = conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
        if indexed != actual:
            conn.execute("INSERT INTO chat_turns_fts(chat_turns_fts) VALUES('rebuild')")
            print(f"[dashboard] chat search index rebuilt for {actual} turns")
    except sqlite3.Error as e:
        print(f"[dashboard] chat search index skipped: {e}")

    # The audit log itself.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_audit (
            id         INTEGER PRIMARY KEY,
            ts         TEXT NOT NULL,
            actor      TEXT NOT NULL,              -- 'ted' | 'user'
            action     TEXT NOT NULL,              -- 'created' | 'edited' | 'forgotten'
            table_name TEXT NOT NULL,
            row_key    INTEGER,
            old_value  TEXT,                       -- JSON snapshot before
            new_value  TEXT                        -- JSON snapshot after
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON memory_audit(ts DESC)")

    # Phrase-triggered routines are managed by a custom dashboard panel rather
    # than the generic memory table editor, but they share this database so a
    # running Ted sees edits immediately.
    try:
        from core import routines
        routines.ensure_schema(conn)
    except Exception as e:
        print(f"[dashboard] routines schema skipped: {e}")
    try:
        from core import lingo
        lingo.ensure_schema(conn)
    except Exception as e:
        print(f"[dashboard] lingo schema skipped: {e}")

    for table in TABLES:
        try:
            _make_audit_triggers(conn, table)
        except sqlite3.Error as e:
            print(f"[dashboard] trigger setup skipped for {table}: {e}")

    # Fix a latent desync: memory.py only syncs exchanges_fts on INSERT.
    # Deleting or editing an exchange (which the dashboard allows) would leave
    # stale rows in the FTS index. These complete the contract.
    try:
        has_fts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'exchanges_fts'").fetchone()
        if has_fts:
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS exchanges_ad AFTER DELETE ON exchanges BEGIN
                    INSERT INTO exchanges_fts(exchanges_fts, rowid, question, answer)
                    VALUES ('delete', old.id, old.question, old.answer);
                END""")
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS exchanges_au AFTER UPDATE ON exchanges BEGIN
                    INSERT INTO exchanges_fts(exchanges_fts, rowid, question, answer)
                    VALUES ('delete', old.id, old.question, old.answer);
                    INSERT INTO exchanges_fts(rowid, question, answer)
                    VALUES (new.id, new.question, new.answer);
                END""")
    except sqlite3.Error as e:
        print(f"[dashboard] FTS sync triggers skipped: {e}")

    conn.commit()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_rows(table, search="", limit=100, offset=0):
    cfg = TABLES[table]
    conn = get_conn()
    pk, cols = cfg["pk"], cfg["cols"]
    select = f"SELECT {pk} AS _pk, {', '.join(cols)} FROM {table}"
    where, params = "", []
    if search:
        like = " OR ".join(f"{c} LIKE ?" for c in cfg["text_cols"])
        where = f" WHERE ({like})"
        params = [f"%{search}%"] * len(cfg["text_cols"])
    count_sql = f"SELECT COUNT(*) FROM {table}{where}"
    sql = f"{select}{where} ORDER BY {cfg['order']} LIMIT ? OFFSET ?"
    with _lock:
        total = conn.execute(count_sql, params).fetchone()[0]
        rows = [dict(r) for r in conn.execute(sql, [*params, limit, offset])]
    return {"rows": rows, "total": total}


def list_history(table="", actor="", action="", search="", limit=100, offset=0):
    conn = get_conn()
    where, params = [], []
    if table:
        where.append("table_name = ?"); params.append(table)
    if actor:
        where.append("actor = ?"); params.append(actor)
    if action:
        where.append("action = ?"); params.append(action)
    if search:
        where.append("(old_value LIKE ? OR new_value LIKE ?)")
        params.extend([f"%{search}%"] * 2)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        total = conn.execute(f"SELECT COUNT(*) FROM memory_audit{w}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, ts, actor, action, table_name, row_key, old_value, new_value "
            f"FROM memory_audit{w} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("old_value", "new_value"):
            if d[k]:
                try:
                    d[k] = json.loads(d[k])
                except (ValueError, TypeError):
                    pass
        out.append(d)
    return {"rows": out, "total": total}


def summary():
    """Row counts for the tab bar. A table that does not exist counts zero.

    memory.py's schema is authoritative for the core tables, so on a database
    Ted has not opened yet they are simply absent. Every count here used to be
    a bare query, which meant one missing table took the whole dashboard down
    with a 500 on its first request — including the tabs for the tables that
    were present.
    """
    conn = get_conn()

    def count(table):
        try:
            with _lock:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            return 0

    counts = {t: count(t) for t in TABLES}
    counts["routines"] = count("routines")
    counts["lingo"] = count("lingo")
    counts["chats"] = count("chat_sessions")
    counts["history"] = count("memory_audit")
    return {"db_path": DB_PATH, "counts": counts}


# ---------------------------------------------------------------------------
# Writes — always attributed to 'user', always inside one transaction.
# ---------------------------------------------------------------------------

def _as_user(conn, fn):
    """Run fn inside a transaction with audit_context.actor = 'user'.

    The flag flips inside the same uncommitted transaction as the write, so
    Ted's process (separate connection) can never observe actor='user'.
    """
    with _lock:
        try:
            conn.execute("UPDATE audit_context SET actor = 'user' WHERE id = 1")
            result = fn(conn)
            conn.execute("UPDATE audit_context SET actor = 'ted' WHERE id = 1")
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _clean(table, data, creating):
    cfg = TABLES[table]
    vals = {k: data[k] for k in cfg["editable"] if k in data}
    if creating:
        missing = [k for k in cfg["required"] if not str(vals.get(k, "")).strip()]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")
    if not vals:
        raise ValueError("no editable fields supplied")
    return vals


def create_row(table, data):
    vals = _clean(table, data, creating=True)
    have = {r[1] for r in get_conn().execute(f"PRAGMA table_info({table})")}
    now = _now()
    if "created" in have:
        vals["created"] = now
    if table == "exchanges":
        vals.setdefault("who", "ted")
        vals["ts"] = now
    if "writer" in have:
        vals["writer"] = "user"

    def _do(conn):
        cols = ", ".join(vals)
        marks = ", ".join("?" * len(vals))
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                           list(vals.values()))
        return cur.lastrowid

    return _as_user(get_conn(), _do)


def update_row(table, pk_value, data):
    cfg = TABLES[table]
    vals = _clean(table, data, creating=False)
    have = {r[1] for r in get_conn().execute(f"PRAGMA table_info({table})")}
    if "writer" in have:
        vals["writer"] = "user"
    if "updated" in have:
        vals["updated"] = _now()

    def _do(conn):
        sets = ", ".join(f"{k} = ?" for k in vals)
        cur = conn.execute(
            f"UPDATE {table} SET {sets} WHERE {cfg['pk']} = ?",
            [*vals.values(), pk_value])
        if cur.rowcount == 0:
            raise KeyError(f"{table} row {pk_value} not found")
        return cur.rowcount

    return _as_user(get_conn(), _do)


def delete_row(table, pk_value):
    cfg = TABLES[table]

    def _do(conn):
        cur = conn.execute(f"DELETE FROM {table} WHERE {cfg['pk']} = ?", [pk_value])
        if cur.rowcount == 0:
            raise KeyError(f"{table} row {pk_value} not found")
        return cur.rowcount

    return _as_user(get_conn(), _do)


# ---------------------------------------------------------------------------
# Chat sessions (HUD sidebar) — plain reads/writes, no audit, no actor dance.
# ---------------------------------------------------------------------------

def list_chats(limit=100, include_hidden=False):
    """Threads for the sidebar. Hidden ones are left out unless asked for —
    the memory dashboard asks for them, because that is the one place where
    a deleted chat can be looked at again or destroyed for real."""
    conn = get_conn()
    where = "" if include_hidden else " WHERE s.hidden = 0"
    with _lock:
        rows = conn.execute(
            "SELECT s.id, s.title, s.summary, s.created, s.updated, s.hidden, "
            "       (SELECT COUNT(*) FROM chat_turns t WHERE t.session_id = s.id) AS turns "
            f"FROM chat_sessions s{where} ORDER BY s.updated DESC LIMIT ?",
            [limit]).fetchall()
    return [dict(r) for r in rows]


def create_chat():
    conn = get_conn()
    now = _now()
    with _lock:
        cur = conn.execute(
            "INSERT INTO chat_sessions (title, created, updated) VALUES (?,?,?)",
            ("New chat", now, now))
        conn.commit()
        return cur.lastrowid


def get_chat(chat_id):
    conn = get_conn()
    with _lock:
        s = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", [chat_id]).fetchone()
        if s is None:
            raise KeyError(f"chat {chat_id} not found")
        turns = conn.execute(
            "SELECT id, role, content, ts FROM chat_turns "
            "WHERE session_id = ? ORDER BY id", [chat_id]).fetchall()
    return {**dict(s), "turns": [dict(t) for t in turns]}


def add_chat_turn(chat_id, role, content):
    if role not in ("user", "ted"):
        raise ValueError("role must be 'user' or 'ted'")
    if not (content or "").strip():
        raise ValueError("empty turn")
    conn = get_conn()
    now = _now()
    with _lock:
        cur = conn.execute(
            "INSERT INTO chat_turns (session_id, role, content, ts) VALUES (?,?,?,?)",
            [chat_id, role, content, now])
        conn.execute("UPDATE chat_sessions SET updated = ? WHERE id = ?", [now, chat_id])
        conn.commit()
        return cur.lastrowid


def set_chat_meta(chat_id, title=None, summary=None):
    conn = get_conn()
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title[:80])
    if summary is not None:
        sets.append("summary = ?"); params.append(summary[:500])
    if not sets:
        return
    params.append(chat_id)
    with _lock:
        conn.execute(f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()


def set_chat_hidden(chat_id, hidden):
    """Soft delete, and its undo. The only thing that changes is whether the
    thread appears in the sidebar — no turn is touched.

    One primitive for both directions on purpose: hiding and restoring are the
    same write with a different value, and two functions would be two places
    that could disagree about what a hidden chat is.
    """
    conn = get_conn()
    with _lock:
        cur = conn.execute("UPDATE chat_sessions SET hidden = ? WHERE id = ?",
                           [1 if hidden else 0, chat_id])
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"chat {chat_id} not found")
    return bool(hidden)


def delete_chat(chat_id):
    """Hard delete: the thread and every turn in it, gone.

    Reachable only from the memory dashboard. The sidebar's delete calls
    set_chat_hidden instead, because "clear this off my list" and "destroy the
    record" are different intentions and only one of them is recoverable.

    Note what this does NOT cascade to: session_summaries and facts carry no
    reference back to a chat session (facts.source_session is empty in every
    row), so there is nothing to follow. Anything Ted learned in this thread
    survives it, by construction rather than by choice.
    """
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM chat_turns WHERE session_id = ?", [chat_id])
        cur = conn.execute("DELETE FROM chat_sessions WHERE id = ?", [chat_id])
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"chat {chat_id} not found")
