"""Charlie's personal shorthand, expanded before Ted routes a request.

Lingo is deliberately separate from facts.  A fact adds context for an answer;
a lingo mapping changes how Ted interprets Charlie's words everywhere: routing,
routines, and the model's compact operational context.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime

from core.paths import DATA


DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lingo (
    id          INTEGER PRIMARY KEY,
    term        TEXT NOT NULL COLLATE NOCASE UNIQUE,
    meaning     TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL,
    last_used   TEXT NOT NULL DEFAULT '',
    use_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lingo_enabled ON lingo(enabled, term);
"""


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def ensure_schema(conn=None):
    owned = conn is None
    conn = conn or _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        if owned:
            conn.close()


def _decode(row):
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    return item


def list_terms(include_disabled=True):
    with _connect() as conn:
        where = "" if include_disabled else " WHERE enabled = 1"
        rows = conn.execute(
            "SELECT * FROM lingo" + where + " ORDER BY term COLLATE NOCASE"
        ).fetchall()
    return [_decode(row) for row in rows]


def get_term(term_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM lingo WHERE id = ?", (term_id,)).fetchone()
    return _decode(row) if row else None


def save_term(data, term_id=None):
    term = " ".join(str(data.get("term") or "").strip().split())
    meaning = " ".join(str(data.get("meaning") or "").strip().split())
    note = str(data.get("note") or "").strip()
    enabled = 1 if data.get("enabled", True) else 0
    if not term:
        raise ValueError("term is required")
    if not meaning:
        raise ValueError("meaning is required")
    if len(term) > 80 or len(meaning) > 240:
        raise ValueError("keep the term under 80 characters and its meaning under 240")
    if term.casefold() == meaning.casefold():
        raise ValueError("term and meaning must be different")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            if term_id is None:
                cur = conn.execute(
                    "INSERT INTO lingo(term,meaning,note,enabled,created,updated) "
                    "VALUES(?,?,?,?,?,?)",
                    (term, meaning, note, enabled, now, now),
                )
                term_id = cur.lastrowid
            else:
                cur = conn.execute(
                    "UPDATE lingo SET term=?,meaning=?,note=?,enabled=?,updated=? "
                    "WHERE id=?",
                    (term, meaning, note, enabled, now, term_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"lingo term {term_id} not found")
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"'{term}' already has a lingo definition") from exc
    return get_term(term_id)


def remember(term, meaning, note="Learned in conversation"):
    """Create or update one case-insensitive term from a conversation."""
    existing = next((row for row in list_terms()
                     if row["term"].casefold() == str(term).strip().casefold()), None)
    return save_term({"term": term, "meaning": meaning, "note": note,
                      "enabled": True}, existing["id"] if existing else None)


def delete_term(term_id):
    with _connect() as conn:
        cur = conn.execute("DELETE FROM lingo WHERE id = ?", (term_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"lingo term {term_id} not found")


def _term_pattern(term):
    # Flexible whitespace keeps multi-word shorthand robust to voice
    # transcription while the word boundaries prevent doc from changing doctor.
    pieces = [re.escape(piece) for piece in term.split()]
    return re.compile(r"(?<![A-Za-z0-9_])" + r"\s+".join(pieces)
                      + r"(?![A-Za-z0-9_])", re.I)


def expand(text, record_usage=False):
    """Return ``(expanded_text, matched_rows)`` using longest terms first."""
    expanded = str(text or "")
    matched = []
    for row in sorted(list_terms(include_disabled=False),
                      key=lambda item: len(item["term"]), reverse=True):
        pattern = _term_pattern(row["term"])
        if not pattern.search(expanded):
            continue
        expanded = pattern.sub(row["meaning"], expanded)
        matched.append(row)
    if record_usage and matched:
        now = datetime.now().isoformat(timespec="seconds")
        with _connect() as conn:
            conn.executemany(
                "UPDATE lingo SET last_used=?, use_count=use_count+1 WHERE id=?",
                [(now, row["id"]) for row in matched],
            )
            conn.commit()
    return expanded, matched


def context_line(matched):
    if not matched:
        return ""
    pairs = "; ".join(f'“{row["term"]}” means “{row["meaning"]}”'
                      for row in matched)
    return "Charlie's personal lingo resolved for this request: " + pairs + "."


_DEFINITION_PATTERNS = (
    re.compile(r"^(?:remember(?: that)?\s+)?when i say\s+['\"“]?(.+?)['\"”]?[,]?\s+"
               r"i mean\s+['\"“]?(.+?)['\"”]?[.!?]*$", re.I),
    re.compile(r"^(?:remember(?: that)?\s+|in my lingo[,]?\s+|for me[,]?\s+)"
               r"['\"“]?(.+?)['\"”]?\s+means\s+['\"“]?(.+?)['\"”]?[.!?]*$", re.I),
    # A short direct definition such as "doc means document" is unambiguous.
    re.compile(r"^['\"“]?([A-Za-z0-9][A-Za-z0-9 '\-]{0,39}?)['\"”]?\s+means\s+"
               r"['\"“]?([A-Za-z0-9][A-Za-z0-9 '\-]{0,79}?)['\"”]?[.!?]*$", re.I),
)


def parse_definition(text):
    """Extract an explicit lingo definition without treating normal facts as one."""
    raw = " ".join(str(text or "").strip().split())
    for pattern in _DEFINITION_PATTERNS:
        match = pattern.match(raw)
        if not match:
            continue
        term = match.group(1).strip(" '\"“”.,!?")
        meaning = match.group(2).strip(" '\"“”.,!?")
        if 1 <= len(term.split()) <= 5 and 1 <= len(meaning.split()) <= 12:
            return term, meaning
    return None
