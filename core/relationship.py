"""Evidence-backed working relationship memory.

Explicit preferences can become active immediately. Inferences and repeated
feedback are proposals until Charlie approves them; one odd exchange therefore
cannot silently rewrite Ted's personality.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from core.paths import DATA


DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")
KINDS = {"identity", "preference", "episode", "commitment", "interaction_lesson"}
STATUSES = {"active", "proposed", "rejected", "superseded"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationship_memory (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    memory_key    TEXT NOT NULL,
    value         TEXT NOT NULL,
    explicit      INTEGER NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL DEFAULT 0.5,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'proposed',
    source        TEXT NOT NULL DEFAULT '',
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    last_used     TEXT NOT NULL DEFAULT '',
    use_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_relationship_status
ON relationship_memory(status,kind,confidence,updated);
CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_live_key
ON relationship_memory(kind,memory_key,status) WHERE status IN ('active','proposed');
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def ensure_schema(conn=None):
    if conn is not None:
        conn.executescript(_SCHEMA)
        return
    with _connect() as owned:
        owned.commit()


def _decode(row):
    item = dict(row)
    item["explicit"] = bool(item["explicit"])
    try:
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    except Exception:
        item["evidence"] = []
    return item


def list_memories(status=None, kind=None, limit=100):
    clauses, args = [], []
    if status:
        clauses.append("status=?")
        args.append(status)
    if kind:
        clauses.append("kind=?")
        args.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    args.append(max(1, min(int(limit), 500)))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM relationship_memory" + where
            + " ORDER BY explicit DESC,confidence DESC,updated DESC LIMIT ?", args).fetchall()
    return [_decode(row) for row in rows]


def save(kind, key, value, *, explicit=False, confidence=0.5, evidence=(),
         status=None, source=""):
    kind = str(kind or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    key = " ".join(str(key or "").strip().split()).casefold().replace(" ", "_")
    value = " ".join(str(value or "").strip().split())
    if not key or not value:
        raise ValueError("key and value are required")
    confidence = max(0.0, min(1.0, float(confidence)))
    status = status or ("active" if explicit else "proposed")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    now = _now()
    payload = json.dumps(list(evidence or []))
    with _connect() as conn:
        row = conn.execute(
            "SELECT id,status FROM relationship_memory WHERE kind=? AND memory_key=? "
            "AND status IN ('active','proposed') ORDER BY id DESC LIMIT 1",
            (kind, key)).fetchone()
        if row:
            if row["status"] == "active" and status == "proposed" and not explicit:
                current = conn.execute(
                    "SELECT * FROM relationship_memory WHERE id=?", (row["id"],)).fetchone()
                return _decode(current)
            conn.execute(
                "UPDATE relationship_memory SET value=?,explicit=?,confidence=?,"
                "evidence_json=?,status=?,source=?,updated=? WHERE id=?",
                (value, int(bool(explicit)), confidence, payload, status, source, now, row["id"]))
            memory_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO relationship_memory(kind,memory_key,value,explicit,confidence,"
                "evidence_json,status,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (kind, key, value, int(bool(explicit)), confidence, payload,
                 status, source, now, now))
            memory_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM relationship_memory WHERE id=?", (memory_id,)).fetchone()
    return _decode(row)


def review(memory_id, decision):
    status = {"approve": "active", "reject": "rejected"}.get(decision)
    if not status:
        raise ValueError("decision must be approve or reject")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE relationship_memory SET status=?,updated=? WHERE id=? AND status='proposed'",
            (status, _now(), int(memory_id)))
        conn.commit()
    return cur.rowcount == 1


def working_context(limit=10):
    rows = list_memories(status="active", limit=limit)
    if not rows:
        return ""
    now = _now()
    with _connect() as conn:
        conn.executemany(
            "UPDATE relationship_memory SET last_used=?,use_count=use_count+1 WHERE id=?",
            [(now, row["id"]) for row in rows])
        conn.commit()
    lines = [f"- [{row['kind']}] {row['value']}" for row in rows]
    return "WORKING RELATIONSHIP MEMORY (approved or explicitly stated):\n" + "\n".join(lines)


_FEEDBACK_LESSONS = {
    "Too wordy": ("response_length", "Lead with the answer and keep routine replies shorter."),
    "Misunderstood the question": (
        "resolve_references", "Resolve referring words from the active task and recent conversation before answering."),
    "Missed context": ("use_context", "Use relevant established context instead of restarting from zero."),
    "Wrong tone": ("grounded_tone", "Use grounded, direct language without performative enthusiasm."),
    "Tool or result was wrong": (
        "verify_actions", "Report an action complete only after its resulting state is verified."),
    "Weak reasoning": ("check_assumptions", "Check assumptions before committing to an answer."),
}


def propose_from_feedback(reason, turn_id, *, threshold=2):
    """Create a reviewable lesson after the same correction repeats."""
    lesson = _FEEDBACK_LESSONS.get(reason)
    if not lesson:
        return None
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM turn_log WHERE rating=-1 AND feedback_reason=? ORDER BY id DESC LIMIT ?",
            (reason, threshold)).fetchall()
    ids = [int(row[0]) for row in rows]
    if turn_id and int(turn_id) not in ids:
        ids.insert(0, int(turn_id))
    ids = list(dict.fromkeys(ids))
    if len(ids) < threshold:
        return None
    key, value = lesson
    return save("interaction_lesson", key, value, explicit=False,
                confidence=min(0.95, 0.55 + 0.12 * len(ids)),
                evidence=[{"turn_id": item, "feedback_reason": reason} for item in ids],
                status="proposed", source="repeated_feedback")


def reflect_session(conversation):
    """Derive at most two reviewable lessons from a substantive session.

    Runs in the existing background session-memory lane. Inferences never enter
    working context automatically; an allegedly explicit preference is active
    only when the model supplies a short quote that actually occurs in one of
    Charlie's messages.
    """
    user_lines = [str(m.get("content") or "").strip() for m in conversation
                  if m.get("role") == "user" and str(m.get("content") or "").strip()]
    if len(user_lines) < 4:
        return []
    transcript = "\n".join(f"Charlie: {line[:260]}" for line in user_lines[-20:])
    try:
        from core.providers import chat_create
        response = chat_create(
            messages=[
                {"role": "system", "content": (
                    "Extract zero to two durable interaction preferences or communication lessons. "
                    "Do not extract temporary moods, task details, identity facts, or guesses. "
                    "Return JSON: {\"observations\":[{\"key\": short_snake_case,"
                    "\"value\": one plain sentence,\"kind\": \"preference\" or "
                    "\"interaction_lesson\",\"confidence\": 0..1,\"explicit\": bool,"
                    "\"quote\": exact short Charlie quote or empty string}]}." )},
                {"role": "user", "content": transcript},
            ],
            response_format={"type": "json_object"}, max_tokens=260,
            reasoning_effort="none", _ted_workload="background",
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
    except Exception as exc:
        print(f"[relationship] reflection skipped: {exc}")
        return []
    saved = []
    for item in (data.get("observations") or [])[:2]:
        if not isinstance(item, dict) or item.get("kind") not in {
                "preference", "interaction_lesson"}:
            continue
        quote = " ".join(str(item.get("quote") or "").split())
        explicit = bool(item.get("explicit") and quote
                        and any(quote.casefold() in line.casefold() for line in user_lines))
        try:
            saved.append(save(
                item["kind"], item.get("key"), item.get("value"),
                explicit=explicit,
                confidence=1.0 if explicit else item.get("confidence", 0.5),
                evidence=[{"quote": quote}] if quote else [],
                status="active" if explicit else "proposed",
                source="session_reflection",
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return saved
