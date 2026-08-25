"""Durable state for requests that span tools, confirmations, or conversations."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from core.paths import DATA


DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_tasks (
    id                  INTEGER PRIMARY KEY,
    chat_id             INTEGER,
    goal                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    interpretation_json TEXT NOT NULL DEFAULT '{}',
    entities_json       TEXT NOT NULL DEFAULT '{}',
    constraints_json    TEXT NOT NULL DEFAULT '[]',
    expected_state_json TEXT NOT NULL DEFAULT '{}',
    confirmation_policy TEXT NOT NULL DEFAULT 'assume_safe',
    created             TEXT NOT NULL,
    updated             TEXT NOT NULL,
    completed_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_active_tasks_chat ON active_tasks(chat_id,status,updated);
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    tool        TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created     TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES active_tasks(id)
);
CREATE TABLE IF NOT EXISTS turn_interpretations (
    id                  INTEGER PRIMARY KEY,
    chat_id             INTEGER,
    task_id             INTEGER,
    original            TEXT NOT NULL,
    interpretation_json TEXT NOT NULL,
    created             TEXT NOT NULL
);
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
    if not row:
        return None
    item = dict(row)
    for key in ("interpretation_json", "entities_json", "constraints_json",
                "expected_state_json"):
        try:
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
        except Exception:
            item[key.removesuffix("_json")] = {} if key != "constraints_json" else []
    return item


def active_for(chat_id=None):
    with _connect() as conn:
        if chat_id is None:
            row = conn.execute(
                "SELECT * FROM active_tasks WHERE chat_id IS NULL AND status IN ('active','waiting') "
                "ORDER BY updated DESC LIMIT 1").fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM active_tasks WHERE chat_id=? AND status IN ('active','waiting') "
                "ORDER BY updated DESC LIMIT 1", (chat_id,)).fetchone()
    return _decode(row)


def begin_or_continue(chat_id, interpretation):
    data = interpretation.as_dict() if hasattr(interpretation, "as_dict") else dict(interpretation)
    active = active_for(chat_id)
    task_id = data.get("continues_task_id")
    if active and (task_id == active["id"] or data.get("references")):
        with _connect() as conn:
            conn.execute(
                "UPDATE active_tasks SET goal=?,interpretation_json=?,constraints_json=?,"
                "confirmation_policy=?,updated=? WHERE id=?",
                (data.get("goal") or active["goal"], json.dumps(data),
                 json.dumps(data.get("constraints") or []),
                 data.get("clarification_policy") or "assume_safe", _now(), active["id"]))
            conn.execute(
                "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
                (active["id"], "continued", json.dumps(data), _now()))
            conn.commit()
        return active["id"]

    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO active_tasks(chat_id,goal,interpretation_json,constraints_json,"
            "confirmation_policy,created,updated) VALUES(?,?,?,?,?,?,?)",
            (chat_id, data.get("goal") or data.get("original") or "Unspecified task",
             json.dumps(data), json.dumps(data.get("constraints") or []),
             data.get("clarification_policy") or "assume_safe", now, now))
        task_id = cur.lastrowid
        conn.execute(
            "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
            (task_id, "started", json.dumps(data), now))
        conn.commit()
    return task_id


def save_interpretation(chat_id, interpretation, task_id=None):
    data = interpretation.as_dict() if hasattr(interpretation, "as_dict") else dict(interpretation)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO turn_interpretations(chat_id,task_id,original,interpretation_json,created) "
            "VALUES(?,?,?,?,?)",
            (chat_id, task_id, data.get("original", ""), json.dumps(data), _now()))
        conn.commit()


def record_action(task_id, tool, outcome):
    if not task_id:
        return
    data = outcome.as_dict() if hasattr(outcome, "as_dict") else dict(outcome)
    ok = bool(data.get("ok") and data.get("matches_goal", True))
    status = "completed" if ok else "waiting"
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO task_events(task_id,kind,tool,detail_json,created) VALUES(?,?,?,?,?)",
            (task_id, "verified" if ok else "failed", tool, json.dumps(data), now))
        conn.execute(
            "UPDATE active_tasks SET status=?,expected_state_json=?,updated=?,completed_at=? "
            "WHERE id=?",
            (status, json.dumps(data.get("expected_state") or {}), now,
             now if ok else "", task_id))
        conn.commit()


def mark_waiting(task_id, reason="confirmation or required information"):
    if not task_id:
        return
    now = _now()
    with _connect() as conn:
        conn.execute("UPDATE active_tasks SET status='waiting',updated=? WHERE id=?",
                     (now, task_id))
        conn.execute(
            "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
            (task_id, "waiting", json.dumps({"reason": reason}), now))
        conn.commit()


def cancel_active(chat_id=None):
    active = active_for(chat_id)
    if not active:
        return False
    with _connect() as conn:
        conn.execute("UPDATE active_tasks SET status='cancelled',updated=? WHERE id=?",
                     (_now(), active["id"]))
        conn.commit()
    return True


def format_for_prompt(task):
    if not task:
        return ""
    constraints = task.get("constraints") or []
    return (
        f"ACTIVE TASK #{task['id']}: goal={task['goal']} | status={task['status']}"
        + (" | constraints=" + "; ".join(constraints) if constraints else "")
        + ". Resolve referring phrases against this task before asking Charlie to repeat himself."
    )


def list_recent(limit=30):
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM active_tasks ORDER BY updated DESC LIMIT ?",
                            (max(1, min(int(limit), 200)),)).fetchall()
    return [_decode(row) for row in rows]
