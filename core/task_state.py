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
    completed_at         TEXT NOT NULL DEFAULT '',
    current_step         TEXT NOT NULL DEFAULT '',
    last_user_text       TEXT NOT NULL DEFAULT '',
    completed_steps_json TEXT NOT NULL DEFAULT '[]',
    failed_steps_json    TEXT NOT NULL DEFAULT '[]'
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
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add ledger columns to databases created by earlier Ted versions."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(active_tasks)")}
    for name, declaration in (
        ("current_step", "TEXT NOT NULL DEFAULT ''"),
        ("last_user_text", "TEXT NOT NULL DEFAULT ''"),
        ("completed_steps_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("failed_steps_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in existing:
            try:
                conn.execute(
                    f"ALTER TABLE active_tasks ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError as exc:
                # Ted and the dashboard open this database from separate
                # processes. If both migrate at startup, the second ADD sees
                # the column the first one just created and can safely continue.
                if "duplicate column" not in str(exc).lower():
                    raise


def ensure_schema(conn=None):
    if conn is not None:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        return
    with _connect() as owned:
        owned.commit()


def _decode(row):
    if not row:
        return None
    item = dict(row)
    for key in ("interpretation_json", "entities_json", "constraints_json",
                "expected_state_json", "completed_steps_json", "failed_steps_json"):
        try:
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
        except Exception:
            item[key.removesuffix("_json")] = (
                [] if key in ("constraints_json", "completed_steps_json",
                              "failed_steps_json") else {})
    return item


def active_for(chat_id=None):
    """Return the current task card, including a recently completed card.

    Completed cards remain referents inside their own chat so "do that again"
    works after a restart. A new unrelated action supersedes the old card.
    """
    with _connect() as conn:
        if chat_id is None:
            row = conn.execute(
                "SELECT * FROM active_tasks WHERE chat_id IS NULL "
                "AND status IN ('active','waiting','completed') "
                "ORDER BY updated DESC LIMIT 1").fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM active_tasks WHERE chat_id=? "
                "AND status IN ('active','waiting','completed') "
                "ORDER BY updated DESC LIMIT 1", (chat_id,)).fetchone()
    return _decode(row)


def begin_or_continue(chat_id, interpretation):
    data = interpretation.as_dict() if hasattr(interpretation, "as_dict") else dict(interpretation)
    active = active_for(chat_id)
    task_id = data.get("continues_task_id")
    if active and (task_id == active["id"] or data.get("references")):
        old_constraints = active.get("constraints") or []
        constraints = list(dict.fromkeys(
            [*old_constraints, *(data.get("constraints") or [])]))[-6:]
        with _connect() as conn:
            conn.execute(
                "UPDATE active_tasks SET status='active',interpretation_json=?,"
                "constraints_json=?,confirmation_policy=?,current_step=?,"
                "last_user_text=?,updated=?,completed_at='' WHERE id=?",
                (json.dumps(data), json.dumps(constraints),
                 data.get("clarification_policy") or "assume_safe",
                 data.get("expanded") or data.get("original") or data.get("goal") or "",
                 data.get("original") or "", _now(), active["id"]))
            conn.execute(
                "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
                (active["id"], "continued", json.dumps(data), _now()))
            conn.commit()
        return active["id"]

    now = _now()
    with _connect() as conn:
        if active and active.get("status") in ("active", "waiting"):
            conn.execute(
                "UPDATE active_tasks SET status='superseded',updated=? WHERE id=?",
                (now, active["id"]))
        cur = conn.execute(
            "INSERT INTO active_tasks(chat_id,goal,interpretation_json,constraints_json,"
            "confirmation_policy,current_step,last_user_text,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (chat_id, data.get("goal") or data.get("original") or "Unspecified task",
             json.dumps(data), json.dumps(data.get("constraints") or []),
             data.get("clarification_policy") or "assume_safe",
             data.get("expanded") or data.get("original") or "",
             data.get("original") or "", now, now))
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
    status = "active" if ok else "waiting"
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT completed_steps_json,failed_steps_json FROM active_tasks WHERE id=?",
            (task_id,)).fetchone()
        if row is None:
            return
        try:
            completed = json.loads(row[0] or "[]")
        except Exception:
            completed = []
        try:
            failed = json.loads(row[1] or "[]")
        except Exception:
            failed = []
        step = {
            "tool": tool,
            "report": str(data.get("report") or data.get("failure") or "")[:320],
            "at": now,
        }
        if ok:
            completed = [*completed, step][-10:]
        else:
            failed = [*failed, step][-6:]
        conn.execute(
            "INSERT INTO task_events(task_id,kind,tool,detail_json,created) VALUES(?,?,?,?,?)",
            (task_id, "verified" if ok else "failed", tool, json.dumps(data), now))
        conn.execute(
            "UPDATE active_tasks SET status=?,expected_state_json=?,current_step=?,"
            "completed_steps_json=?,failed_steps_json=?,updated=?,completed_at='' "
            "WHERE id=?",
            (status, json.dumps(data.get("expected_state") or {}), step["report"],
             json.dumps(completed), json.dumps(failed), now, task_id))
        conn.commit()


def complete(task_id, reply=""):
    """Close a task after the whole turn—not merely its first successful tool."""
    if not task_id:
        return False
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT status,completed_steps_json FROM active_tasks WHERE id=?",
            (task_id,)).fetchone()
        if not row or row[0] != "active":
            return False
        try:
            steps = json.loads(row[1] or "[]")
        except Exception:
            steps = []
        if not steps:
            return False
        conn.execute(
            "UPDATE active_tasks SET status='completed',current_step=?,updated=?,"
            "completed_at=? WHERE id=?",
            ((reply or steps[-1].get("report") or "Completed")[:320], now, now, task_id))
        conn.execute(
            "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
            (task_id, "completed", json.dumps({"reply": (reply or "")[:500]}), now))
        conn.commit()
    return True


def mark_waiting(task_id, reason="confirmation or required information"):
    if not task_id:
        return
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE active_tasks SET status='waiting',current_step=?,updated=? WHERE id=?",
            (reason[:320], now, task_id))
        conn.execute(
            "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
            (task_id, "waiting", json.dumps({"reason": reason}), now))
        conn.commit()


def cancel_active(chat_id=None):
    with _connect() as conn:
        if chat_id is None:
            row = conn.execute(
                "SELECT id FROM active_tasks WHERE chat_id IS NULL "
                "AND status IN ('active','waiting') ORDER BY updated DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM active_tasks WHERE chat_id=? "
                "AND status IN ('active','waiting') ORDER BY updated DESC LIMIT 1",
                (chat_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE active_tasks SET status='cancelled',updated=? WHERE id=?",
                     (_now(), row[0]))
        conn.commit()
    return True


def format_for_prompt(task):
    if not task:
        return ""
    constraints = task.get("constraints") or []
    completed = task.get("completed_steps") or []
    failed = task.get("failed_steps") or []
    parts = [
        f"SESSION TASK CARD #{task['id']}",
        f"objective={task['goal']}",
        f"status={task['status']}",
    ]
    if task.get("last_user_text"):
        parts.append("latest request=" + task["last_user_text"][:180])
    if completed:
        parts.append("verified completed steps=" + "; ".join(
            f"{step.get('tool', 'action')}: {step.get('report', '')[:140]}"
            for step in completed[-4:]))
    if failed:
        parts.append("failed/waiting steps=" + "; ".join(
            f"{step.get('tool', 'action')}: {step.get('report', '')[:140]}"
            for step in failed[-2:]))
    if task.get("current_step") and task["status"] != "completed":
        parts.append("current step=" + task["current_step"][:240])
    if constraints:
        parts.append("constraints=" + "; ".join(constraints))
    card = " | ".join(parts) + (
        ". This is verified per-chat continuity. Resolve 'continue', 'again', "
        "and similar references from it before asking Charlie to repeat himself."
    )
    return card[:1600]


def load_chat_history(chat_id, limit=20, exclude_trailing_user=""):
    """Restore a visible HUD chat into the in-process model conversation."""
    if chat_id is None:
        return []
    with _connect() as conn:
        try:
            rows = conn.execute(
                "SELECT role,content FROM chat_turns WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?", (chat_id, max(2, min(int(limit), 40))),
            ).fetchall()
        except sqlite3.OperationalError:
            # The dashboard owns this table and may still be starting on a new
            # install. Missing history must degrade to an empty chat, not block it.
            return []
    items = [{"role": "assistant" if row[0] == "ted" else "user",
              "content": row[1]} for row in reversed(rows)
             if row[0] in ("user", "ted") and str(row[1] or "").strip()]
    if (exclude_trailing_user and items and items[-1]["role"] == "user"
            and " ".join(items[-1]["content"].split())
            == " ".join(str(exclude_trailing_user).split())):
        items.pop()
    return items


def list_recent(limit=30):
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM active_tasks ORDER BY updated DESC LIMIT ?",
                            (max(1, min(int(limit), 200)),)).fetchall()
    return [_decode(row) for row in rows]
