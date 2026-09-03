"""Durable state for requests that span tools, confirmations, or conversations."""

from __future__ import annotations

import json
import os
import re
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
    failed_steps_json    TEXT NOT NULL DEFAULT '[]',
    observations_json    TEXT NOT NULL DEFAULT '[]',
    planned_steps_json   TEXT NOT NULL DEFAULT '[]',
    required_actions     INTEGER NOT NULL DEFAULT 0
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
        ("observations_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("planned_steps_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("required_actions", "INTEGER NOT NULL DEFAULT 0"),
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
                "expected_state_json", "completed_steps_json", "failed_steps_json",
                "observations_json", "planned_steps_json"):
        try:
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
        except Exception:
            item[key.removesuffix("_json")] = (
                [] if key in ("constraints_json", "completed_steps_json",
                              "failed_steps_json", "observations_json",
                              "planned_steps_json") else {})
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
        repeating = bool(re.search(
            r"\b(?:again|repeat|one more time|same (?:thing|task))\b",
            data.get("original") or "", re.I))
        old_constraints = active.get("constraints") or []
        constraints = list(dict.fromkeys(
            [*old_constraints, *(data.get("constraints") or [])]))[-6:]
        with _connect() as conn:
            reset = ("completed_steps_json='[]',failed_steps_json='[]',"
                     "observations_json='[]',planned_steps_json='[]',"
                     "required_actions=0,") if repeating else ""
            conn.execute(
                "UPDATE active_tasks SET status='active',interpretation_json=?,"
                "constraints_json=?,confirmation_policy=?," + reset + "current_step=?,"
                "last_user_text=?,updated=?,completed_at='' WHERE id=?",
                (json.dumps(data), json.dumps(constraints),
                 data.get("clarification_policy") or "assume_safe",
                 ("Repeat the objective from the beginning." if repeating else
                  data.get("expanded") or data.get("original") or data.get("goal") or ""),
                 data.get("original") or "", _now(), active["id"]))
            conn.execute(
                "INSERT INTO task_events(task_id,kind,detail_json,created) VALUES(?,?,?,?)",
                (active["id"], "restarted" if repeating else "continued",
                 json.dumps(data), _now()))
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


def _loads(value, default):
    try:
        return json.loads(value or json.dumps(default))
    except Exception:
        return default


def _unresolved(failed):
    return [step for step in failed if not step.get("resolved")]


def _next_step(required, completed, failed, planned_steps=None):
    unresolved = _unresolved(failed)
    if unresolved:
        tool = unresolved[-1].get("tool") or "action"
        return f"Verify or recover the failed {tool} step, then continue the objective."
    remaining = max(0, int(required or 0) - len(completed))
    if remaining:
        planned_steps = planned_steps or []
        index = len(completed)
        if index < len(planned_steps):
            return f"Next: {planned_steps[index]} ({remaining} action(s) remain unverified)."
        return (f"{remaining} requested action(s) still unverified; inspect the objective "
                "and perform the next missing step.")
    if completed:
        return "All required actions are verified; audit the original objective and finish."
    return "Choose and perform the first action required by the objective."


def set_plan(task_id, required_actions, requested_steps=None):
    """Persist the lower bound for this run before any tool is called."""
    if not task_id:
        return None
    required_actions = max(0, int(required_actions or 0))
    with _connect() as conn:
        row = conn.execute(
            "SELECT required_actions,completed_steps_json,failed_steps_json,"
            "planned_steps_json "
            "FROM active_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        completed = _loads(row[1], [])
        failed = _loads(row[2], [])
        planned = _loads(row[3], [])
        # A continuation adds work to already verified steps. A repeated run is
        # reset in begin_or_continue, so this also gives repetitions a clean plan.
        required = max(int(row[0] or 0), len(completed) + required_actions)
        incoming = [str(step).strip()[:180] for step in (requested_steps or [])
                    if str(step).strip()]
        if not planned:
            planned = incoming
        elif required > int(row[0] or 0) and incoming:
            planned.extend(step for step in incoming if step not in planned)
        while len(planned) < required:
            planned.append("Complete the next missing part of the objective")
        planned = planned[:12]
        current = _next_step(required, completed, failed, planned)
        conn.execute(
            "UPDATE active_tasks SET required_actions=?,planned_steps_json=?,"
            "current_step=?,updated=? WHERE id=?",
            (required, json.dumps(planned), current, _now(), task_id))
        conn.commit()
    return progress(task_id)


def record_action(task_id, tool, outcome):
    if not task_id:
        return
    data = outcome.as_dict() if hasattr(outcome, "as_dict") else dict(outcome)
    ok = bool(data.get("ok") and data.get("matches_goal", True))
    status = "active" if ok else "waiting"
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT completed_steps_json,failed_steps_json,required_actions,"
            "planned_steps_json "
            "FROM active_tasks WHERE id=?",
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
            "args": (data.get("expected_state") or {}).get("args") or {},
            "report": str(data.get("report") or data.get("failure") or "")[:320],
            "changed": bool(data.get("changed")) if ok else False,
            "matches_goal": bool(data.get("matches_goal")),
            "expected_state": data.get("expected_state") or {},
            "observed_state": data.get("observed_state") or {},
            "at": now,
        }
        planned = _loads(row[3], [])
        if ok and len(completed) < len(planned):
            step["plan_step"] = planned[len(completed)]
        if ok:
            completed = [*completed, step][-10:]
        else:
            failed = [*failed, step][-6:]
        conn.execute(
            "INSERT INTO task_events(task_id,kind,tool,detail_json,created) VALUES(?,?,?,?,?)",
            (task_id, "verified" if ok else "failed", tool, json.dumps(data), now))
        current = _next_step(row[2], completed, failed, planned)
        conn.execute(
            "UPDATE active_tasks SET status=?,expected_state_json=?,current_step=?,"
            "completed_steps_json=?,failed_steps_json=?,updated=?,completed_at='' "
            "WHERE id=?",
            (status, json.dumps(data.get("expected_state") or {}), current,
             json.dumps(completed), json.dumps(failed), now, task_id))
        conn.commit()


def _verification_terms(step):
    """Concrete target words an observation must contain to resolve a failure."""
    args = step.get("args") or (step.get("expected_state") or {}).get("args") or {}
    if step.get("tool") == "open_app":
        value = str(args.get("name") or "")
    elif step.get("tool") == "browse_to":
        value = str(args.get("site") or "")
    else:
        return []
    value = re.sub(r"https?://|www\.", " ", value.lower())
    return [word for word in re.findall(r"[a-z0-9]+", value)
            if len(word) > 2 and word not in {"com", "app", "application", "mail", "inbox"}]


def record_observation(task_id, tool, args, result, failed=False):
    """Store page evidence and reconcile launch failures disproved by it."""
    if not task_id:
        return None
    now = _now()
    report = str(result or "")[-900:]
    with _connect() as conn:
        row = conn.execute(
            "SELECT completed_steps_json,failed_steps_json,observations_json,"
            "required_actions,status,planned_steps_json FROM active_tasks WHERE id=?",
            (task_id,)).fetchone()
        if not row:
            return None
        completed = _loads(row[0], [])
        failures = _loads(row[1], [])
        observations = _loads(row[2], [])
        planned = _loads(row[5], [])
        observation = {
            "tool": tool, "query": dict(args or {}), "report": report,
            "ok": not failed, "at": now,
        }
        observations = [*observations, observation][-6:]
        recovered = []
        if not failed:
            haystack = report.lower()
            for item in failures:
                if item.get("resolved"):
                    continue
                terms = _verification_terms(item)
                if terms and all(term in haystack for term in terms):
                    item["resolved"] = True
                    item["resolved_at"] = now
                    item["resolved_by"] = tool
                    recovered.append(item)
                    completed.append({
                        "tool": item.get("tool") or "action",
                        "args": item.get("args") or {},
                        "report": ("Later page evidence verified the requested target: "
                                   + report[:240]),
                        # Observation proves current state, but not which earlier
                        # attempt caused it. Preserve that distinction explicitly.
                        "changed": "unknown",
                        "matches_goal": True,
                        "expected_state": item.get("expected_state") or {},
                        "observed_state": {"verified_by": tool, "result": report},
                        "recovered": True,
                        "at": now,
                    })
                    if len(completed) <= len(planned):
                        completed[-1]["plan_step"] = planned[len(completed) - 1]
        completed = completed[-10:]
        status = "active" if not _unresolved(failures) else "waiting"
        current = _next_step(row[3], completed, failures, planned)
        conn.execute(
            "INSERT INTO task_events(task_id,kind,tool,detail_json,created) VALUES(?,?,?,?,?)",
            (task_id, "observed", tool,
             json.dumps({"observation": observation,
                         "recovered": [item.get("tool") for item in recovered]}), now))
        conn.execute(
            "UPDATE active_tasks SET status=?,current_step=?,completed_steps_json=?,"
            "failed_steps_json=?,observations_json=?,updated=? WHERE id=?",
            (status, current, json.dumps(completed), json.dumps(failures),
             json.dumps(observations), now, task_id))
        conn.commit()
    return progress(task_id)


def progress(task_id):
    if not task_id:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM active_tasks WHERE id=?", (task_id,)).fetchone()
    task = _decode(row)
    if not task:
        return None
    completed = task.get("completed_steps") or []
    unresolved = _unresolved(task.get("failed_steps") or [])
    required = int(task.get("required_actions") or 0)
    remaining = max(0, required - len(completed))
    return {
        "required": required, "verified": len(completed), "remaining": remaining,
        "unresolved_failures": len(unresolved),
        "ready": bool(completed) and remaining == 0 and not unresolved,
        "next_step": task.get("current_step") or "",
        "status": task.get("status") or "",
    }


def complete(task_id, reply=""):
    """Close a task after the whole turn—not merely its first successful tool."""
    if not task_id:
        return False
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT status,completed_steps_json,failed_steps_json,required_actions "
            "FROM active_tasks WHERE id=?",
            (task_id,)).fetchone()
        if not row or row[0] not in ("active", "waiting"):
            return False
        try:
            steps = json.loads(row[1] or "[]")
        except Exception:
            steps = []
        if not steps:
            return False
        failures = _loads(row[2], [])
        required = int(row[3] or 0)
        if _unresolved(failures) or (required and len(steps) < required):
            return False
        conn.execute(
            "UPDATE active_tasks SET status='completed',current_step=?,updated=?,"
            "completed_at=? WHERE id=?",
            ("No remaining step; wait for Charlie's follow-up.", now, now, task_id))
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
    observations = task.get("observations") or []
    planned = task.get("planned_steps") or []
    required = int(task.get("required_actions") or 0)
    unresolved = _unresolved(failed)
    parts = [
        f"SESSION TASK CARD #{task['id']}",
        f"objective={task['goal']}",
        f"status={task['status']}",
        f"progress={len(completed)}/{required or '?'} verified actions",
    ]
    if task.get("last_user_text"):
        parts.append("latest request=" + task["last_user_text"][:180])
    # The card is deliberately capped. Put the decision-critical next step
    # before verbose evidence so it can never be truncated away.
    if task.get("current_step") and task["status"] != "completed":
        parts.append("next required step=" + task["current_step"][:240])
    if planned:
        parts.append("requested stages=" + " -> ".join(planned[:6]))
    if completed:
        parts.append("verified completed steps=" + "; ".join(
            f"{step.get('plan_step') or step.get('tool', 'action')} "
            f"[via={step.get('tool', 'action')}, changed={str(step.get('changed', 'unknown')).lower()}]: "
            f"{step.get('report', '')[:125]}"
            for step in completed[-4:]))
    if unresolved:
        parts.append("unresolved failures=" + "; ".join(
            f"{step.get('tool', 'action')}: {step.get('report', '')[:140]}"
            for step in unresolved[-2:]))
    if observations:
        parts.append("latest observed state=" + observations[-1].get("report", "")[:220])
    if constraints:
        parts.append("constraints=" + "; ".join(constraints))
    card = " | ".join(parts) + (
        ". This is verified per-chat continuity. Observations are evidence, not "
        "completed actions. Never claim a change from changed=unknown. Continue until "
        "remaining work is zero, or name the exact blocker. Resolve 'continue', "
        "'again', and similar references from this card."
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
