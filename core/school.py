"""Manual school planner shared by Ted and the School dashboard.

The dashboard is the only writer for now. Ted receives one read-only tool that
calls :func:`format_for_ted`, so the assistant and the UI always see the same
SQLite rows without putting a semester of assignments into every prompt.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from core.paths import DATA


DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")

TASK_KINDS = ("assignment", "test", "quiz", "project", "reading", "study", "email", "other")
TASK_STATUSES = ("todo", "in_progress", "submitted", "done")
PRIORITIES = ("low", "normal", "high")
MAX_TASKS_FOR_TED = 250

_SCHEMA = """
CREATE TABLE IF NOT EXISTS school_classes (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL COLLATE NOCASE UNIQUE,
    code          TEXT NOT NULL DEFAULT '',
    instructor    TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    meeting_times TEXT NOT NULL DEFAULT '',
    term          TEXT NOT NULL DEFAULT '',
    color         TEXT NOT NULL DEFAULT '#7c8cff',
    archived      INTEGER NOT NULL DEFAULT 0,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS school_tasks (
    id                INTEGER PRIMARY KEY,
    class_id          INTEGER REFERENCES school_classes(id) ON DELETE SET NULL,
    title             TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'assignment',
    status            TEXT NOT NULL DEFAULT 'todo',
    priority          TEXT NOT NULL DEFAULT 'normal',
    due_at            TEXT,
    estimated_minutes INTEGER,
    notes             TEXT NOT NULL DEFAULT '',
    source_kind       TEXT NOT NULL DEFAULT 'manual',
    source_label      TEXT NOT NULL DEFAULT '',
    source_url        TEXT NOT NULL DEFAULT '',
    completed_at      TEXT,
    created           TEXT NOT NULL,
    updated           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_school_tasks_class_due
    ON school_tasks(class_id, due_at);
CREATE INDEX IF NOT EXISTS idx_school_tasks_open_due
    ON school_tasks(status, due_at)
    WHERE status NOT IN ('submitted', 'done');
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA optimize")
    return conn


def ensure_schema(conn=None):
    owned = conn is None
    conn = conn or _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA optimize")
        conn.commit()
    finally:
        if owned:
            conn.close()


def _text(value, field, *, required=False, maximum=4000):
    value = " ".join(str(value or "").strip().split()) if maximum <= 200 else str(value or "").strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} stays under {maximum} characters")
    return value


def _choice(value, choices, field, default):
    value = str(value or default).strip().lower()
    if value not in choices:
        raise ValueError(f"unknown {field} '{value}'")
    return value


def _color(value):
    value = str(value or "#7c8cff").strip()
    if len(value) != 7 or value[0] != "#" or any(c not in "0123456789abcdefABCDEF" for c in value[1:]):
        raise ValueError("color must be a six-digit hex color")
    return value.lower()


def _due(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("due date must be a valid local date and time") from exc
    return parsed.isoformat(timespec="minutes")


def _minutes(value):
    if value in (None, ""):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("estimated minutes must be a whole number") from exc
    if value < 1 or value > 100000:
        raise ValueError("estimated minutes must be between 1 and 100000")
    return value


def _source_url(value):
    value = _text(value, "source URL", maximum=2000)
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("source link must be a full http or https URL")
    return value


def list_classes(include_archived=False):
    where = "" if include_archived else "WHERE c.archived = 0"
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT c.*,
                   COALESCE(SUM(CASE WHEN t.status NOT IN ('submitted','done') THEN 1 ELSE 0 END), 0) AS open_tasks,
                   COUNT(t.id) AS total_tasks
            FROM school_classes c
            LEFT JOIN school_tasks t ON t.class_id = c.id
            {where}
            GROUP BY c.id
            ORDER BY c.archived, c.name COLLATE NOCASE
        """).fetchall()
    return [dict(row) for row in rows]


def _class_values(data, creating):
    values = {}
    allowed = ("name", "code", "instructor", "location", "meeting_times", "term", "color", "archived")
    for key in allowed:
        if key not in data:
            continue
        if key == "name":
            values[key] = _text(data[key], "class name", required=True, maximum=100)
        elif key == "color":
            values[key] = _color(data[key])
        elif key == "archived":
            values[key] = 1 if data[key] else 0
        else:
            values[key] = _text(data[key], key.replace("_", " "), maximum=200)
    if creating and "name" not in values:
        raise ValueError("class name is required")
    if not values:
        raise ValueError("no class fields supplied")
    return values


def create_class(data):
    values = _class_values(data, True)
    now = _now()
    values.update(created=now, updated=now)
    try:
        with _connect() as conn:
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            cur = conn.execute(f"INSERT INTO school_classes ({cols}) VALUES ({marks})", tuple(values.values()))
            conn.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError("a class with that name already exists") from exc


def update_class(class_id, data):
    values = _class_values(data, False)
    values["updated"] = _now()
    try:
        with _connect() as conn:
            sets = ", ".join(f"{key} = ?" for key in values)
            cur = conn.execute(f"UPDATE school_classes SET {sets} WHERE id = ?", (*values.values(), int(class_id)))
            if cur.rowcount == 0:
                raise KeyError(f"class {class_id} not found")
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("a class with that name already exists") from exc


def delete_class(class_id):
    """Delete a class but keep its tasks, moving them to the General section."""
    with _connect() as conn:
        row = conn.execute("SELECT name FROM school_classes WHERE id = ?", (int(class_id),)).fetchone()
        if row is None:
            raise KeyError(f"class {class_id} not found")
        moved = conn.execute("SELECT COUNT(*) FROM school_tasks WHERE class_id = ?", (int(class_id),)).fetchone()[0]
        conn.execute("DELETE FROM school_classes WHERE id = ?", (int(class_id),))
        conn.commit()
    return {"name": row["name"], "tasks_moved": moved}


def _task_values(data, creating):
    values = {}
    allowed = ("class_id", "title", "kind", "status", "priority", "due_at",
               "estimated_minutes", "notes", "source_label", "source_url")
    for key in allowed:
        if key not in data:
            continue
        value = data[key]
        if key == "class_id":
            values[key] = int(value) if value not in (None, "", 0, "0") else None
        elif key == "title":
            values[key] = _text(value, "task title", required=True, maximum=220)
        elif key == "kind":
            values[key] = _choice(value, TASK_KINDS, "task type", "assignment")
        elif key == "status":
            values[key] = _choice(value, TASK_STATUSES, "status", "todo")
        elif key == "priority":
            values[key] = _choice(value, PRIORITIES, "priority", "normal")
        elif key == "due_at":
            values[key] = _due(value)
        elif key == "estimated_minutes":
            values[key] = _minutes(value)
        elif key == "notes":
            values[key] = _text(value, "notes", maximum=8000)
        elif key == "source_url":
            values[key] = _source_url(value)
        else:
            values[key] = _text(value, "source label", maximum=200)
    if creating and "title" not in values:
        raise ValueError("task title is required")
    if not values:
        raise ValueError("no task fields supplied")
    if "status" in values:
        values["completed_at"] = _now() if values["status"] in ("submitted", "done") else None
    return values


def _check_class(conn, class_id):
    if class_id is not None and conn.execute("SELECT 1 FROM school_classes WHERE id = ?", (class_id,)).fetchone() is None:
        raise ValueError("that class no longer exists")


def create_task(data):
    values = _task_values(data, True)
    now = _now()
    values.update(source_kind="manual", created=now, updated=now)
    with _connect() as conn:
        _check_class(conn, values.get("class_id"))
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = conn.execute(f"INSERT INTO school_tasks ({cols}) VALUES ({marks})", tuple(values.values()))
        conn.commit()
        return cur.lastrowid


def update_task(task_id, data):
    values = _task_values(data, False)
    values["updated"] = _now()
    with _connect() as conn:
        if "class_id" in values:
            _check_class(conn, values["class_id"])
        sets = ", ".join(f"{key} = ?" for key in values)
        cur = conn.execute(f"UPDATE school_tasks SET {sets} WHERE id = ?", (*values.values(), int(task_id)))
        if cur.rowcount == 0:
            raise KeyError(f"task {task_id} not found")
        conn.commit()


def delete_task(task_id):
    with _connect() as conn:
        row = conn.execute("SELECT title FROM school_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found")
        conn.execute("DELETE FROM school_tasks WHERE id = ?", (int(task_id),))
        conn.commit()
    return row["title"]


def list_tasks(view="all", class_id=None, query="", limit=500):
    view = str(view or "all").lower()
    if view not in ("all", "today", "upcoming", "overdue", "completed"):
        raise ValueError(f"unknown school view '{view}'")
    where, params = [], []
    if class_id not in (None, ""):
        where.append("t.class_id = ?")
        params.append(int(class_id))
    if query:
        where.append("(t.title LIKE ? OR t.notes LIKE ? OR c.name LIKE ?)")
        like = f"%{str(query).strip()}%"
        params.extend((like, like, like))
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)
    week = today_start + timedelta(days=8)
    if view == "today":
        where.extend(("t.status NOT IN ('submitted','done')", "t.due_at >= ?", "t.due_at < ?"))
        params.extend((today_start.isoformat(timespec="minutes"), tomorrow.isoformat(timespec="minutes")))
    elif view == "upcoming":
        where.extend(("t.status NOT IN ('submitted','done')", "t.due_at >= ?", "t.due_at < ?"))
        params.extend((tomorrow.isoformat(timespec="minutes"), week.isoformat(timespec="minutes")))
    elif view == "overdue":
        where.extend(("t.status NOT IN ('submitted','done')", "t.due_at IS NOT NULL", "t.due_at < ?"))
        params.append(now.isoformat(timespec="minutes"))
    elif view == "completed":
        where.append("t.status IN ('submitted','done')")
    w = " WHERE " + " AND ".join(where) if where else ""
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT t.*, c.name AS class_name, c.code AS class_code, c.color AS class_color
            FROM school_tasks t
            LEFT JOIN school_classes c ON c.id = t.class_id
            {w}
            ORDER BY CASE WHEN t.status IN ('submitted','done') THEN 1 ELSE 0 END,
                     CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END,
                     t.due_at, t.priority DESC, t.id DESC
            LIMIT ?
        """, (*params, int(limit))).fetchall()
    return [dict(row) for row in rows]


def dashboard_data():
    tasks = list_tasks(limit=1000)
    active = [t for t in tasks if t["status"] not in ("submitted", "done")]
    now = datetime.now()
    today = now.date()
    counts = {"open": len(active), "today": 0, "overdue": 0, "high": 0, "completed": len(tasks) - len(active)}
    for task in active:
        if task["priority"] == "high":
            counts["high"] += 1
        if task["due_at"]:
            due = datetime.fromisoformat(task["due_at"])
            if due < now:
                counts["overdue"] += 1
            if due.date() == today:
                counts["today"] += 1
    return {"classes": list_classes(), "tasks": tasks, "counts": counts,
            "task_kinds": TASK_KINDS, "task_statuses": TASK_STATUSES,
            "priorities": PRIORITIES}


def format_for_ted(view="all", class_name=""):
    """Return exact dashboard rows as compact text for Ted's read-only tool."""
    classes = list_classes()
    class_id = None
    if class_name:
        needle = str(class_name).strip().casefold()
        match = next((c for c in classes if c["name"].casefold() == needle or c["code"].casefold() == needle), None)
        if match is None:
            known = ", ".join(c["name"] for c in classes) or "none yet"
            return f"There is no school class named '{class_name}'. Classes: {known}."
        class_id = match["id"]
    tasks = list_tasks(view=view, class_id=class_id, limit=MAX_TASKS_FOR_TED + 1)
    heading = f"School dashboard — {view}"
    if class_name:
        heading += f" for {match['name']}"
    class_line = "; ".join(
        f"{c['name']}" + (f" ({c['code']})" if c["code"] else "")
        + f", {c['open_tasks']} open" for c in classes) or "No classes have been added."
    if not tasks:
        return f"{heading}. Classes: {class_line}\nNo matching school items."
    lines = []
    for task in tasks[:MAX_TASKS_FOR_TED]:
        due = task["due_at"].replace("T", " ") if task["due_at"] else "no due date"
        course = task["class_name"] or "General"
        extra = []
        if task["estimated_minutes"]:
            extra.append(f"estimate {task['estimated_minutes']} min")
        if task["notes"]:
            extra.append("notes: " + task["notes"])
        if task["source_url"]:
            extra.append("source: " + task["source_url"])
        suffix = " | " + " | ".join(extra) if extra else ""
        lines.append(f"#{task['id']} [{task['status']}] {course} — {task['title']} "
                     f"({task['kind']}, {task['priority']} priority, due {due}){suffix}")
    if len(tasks) > MAX_TASKS_FOR_TED:
        lines.append(f"…and {len(tasks) - MAX_TASKS_FOR_TED} more items not shown.")
    return f"{heading}. Classes: {class_line}\n" + "\n".join(lines)
