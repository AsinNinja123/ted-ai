"""User-defined phrase -> action routines for Ted's zero-model fast path.

Routines live beside Ted's memory in SQLite so the dashboard and the running
HUD see the same data immediately.  Matching is deliberately conservative:
filler words are ignored, multi-word phrases may appear inside a natural
utterance, and one-word phrases must match the whole utterance.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from datetime import datetime

from core.paths import DATA


DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")


# Only low-risk, reversible actions belong in a phrase-triggered fast path.
# Anything consequential (messages, email, purchases, deletion) must continue
# through Ted's ordinary confirmation-aware tool loop.
ROUTINE_ACTIONS = {
    "open_app": {
        "label": "Open an app",
        "fields": {
            "name": {"label": "App name", "type": "text", "required": True,
                     "placeholder": "Claude, ChatGPT, VS Code…"},
        },
    },
    "browse_to": {
        "label": "Open a website",
        "fields": {
            "site": {"label": "Website or URL", "type": "text", "required": True,
                     "placeholder": "canvas.instructure.com or Google Drive"},
            "browser": {"label": "Browser", "type": "select", "required": False,
                        "options": ["", "Chrome", "Brave", "Safari", "Firefox"]},
            "new_window": {"label": "New window", "type": "checkbox",
                           "required": False},
        },
    },
    "play_youtube": {
        "label": "Play a YouTube video",
        "fields": {
            "query": {"label": "Video search", "type": "text", "required": False,
                      "placeholder": "Leave blank for a popular video"},
            "browser": {"label": "Browser", "type": "select", "required": False,
                        "options": ["", "Brave", "Chrome", "Safari"]},
        },
    },
    "play_music": {
        "label": "Play music",
        "fields": {
            "query": {"label": "Song or search", "type": "text", "required": True},
            "artist": {"label": "Artist", "type": "text", "required": False},
        },
    },
    "play_playlist": {
        "label": "Play a playlist",
        "fields": {
            "name": {"label": "Playlist", "type": "text", "required": True},
            "shuffle": {"label": "Shuffle", "type": "checkbox", "required": False},
        },
    },
    "spotify_control": {
        "label": "Control Spotify",
        "fields": {
            "action": {"label": "Action", "type": "select", "required": True,
                       "options": ["play", "pause", "next", "previous"]},
        },
    },
    "ui_press": {
        "label": "Press a visible control",
        "fields": {
            "target": {"label": "Button or control label", "type": "text",
                       "required": True, "placeholder": "Play"},
        },
    },
    "system_volume": {
        "label": "Set system volume",
        "fields": {
            "action": {"label": "Action", "type": "select", "required": True,
                       "options": ["set", "up", "down", "mute", "unmute"]},
            "level": {"label": "Level (0–100)", "type": "number", "required": False},
        },
    },
    "system_brightness": {
        "label": "Change brightness",
        "fields": {
            "action": {"label": "Direction", "type": "select", "required": True,
                       "options": ["up", "down"]},
        },
    },
}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    phrases     TEXT NOT NULL,
    steps       TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    parallel    INTEGER NOT NULL DEFAULT 1,
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL,
    last_run    TEXT NOT NULL DEFAULT '',
    run_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_routines_enabled ON routines(enabled, name);
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
    """Create the routines table on an existing or short-lived connection."""
    owned = conn is None
    conn = conn or _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        if owned:
            conn.close()


_NOISE = {
    "ted", "tad", "hey", "hi", "okay", "ok", "alright", "alrighty",
    "alight", "um", "uh", "er", "please", "kindly", "just", "now",
    "so", "well", "yo", "lets", "let", "us",
}


def normalize_phrase(text):
    """Normalize punctuation and conversational filler without erasing intent."""
    raw = (text or "").lower().replace("’", "'")
    raw = raw.replace("chat gpt", "chatgpt")
    raw = raw.replace("let's", "lets")
    words = re.findall(r"[a-z0-9]+", raw)
    return " ".join(word for word in words if word not in _NOISE)


def _phrase_matches(utterance, phrase):
    utterance = normalize_phrase(utterance)
    phrase = normalize_phrase(phrase)
    if not utterance or not phrase:
        return False
    if utterance == phrase:
        return True
    uw, pw = utterance.split(), phrase.split()
    # A generic one-word alias such as "study" should never fire inside a
    # longer sentence. Multi-word personal sayings are specific enough to use
    # naturally: "alright Ted, let's do some comp org homework".
    if len(pw) >= 2:
        for index in range(len(uw) - len(pw) + 1):
            if uw[index:index + len(pw)] == pw:
                return True
        # Light ASR/typing tolerance for longer sayings, while keeping the bar
        # too high for unrelated phrases to become actions.
        if len(pw) >= 3 and abs(len(uw) - len(pw)) <= 1:
            return difflib.SequenceMatcher(None, utterance, phrase).ratio() >= 0.92
    return False


def _decode(row):
    item = dict(row)
    try:
        item["phrases"] = json.loads(item.get("phrases") or "[]")
    except Exception:
        item["phrases"] = []
    try:
        item["steps"] = json.loads(item.get("steps") or "[]")
    except Exception:
        item["steps"] = []
    item["enabled"] = bool(item.get("enabled"))
    item["parallel"] = bool(item.get("parallel"))
    return item


def list_routines(include_disabled=True):
    with _connect() as conn:
        where = "" if include_disabled else " WHERE enabled = 1"
        rows = conn.execute(
            "SELECT * FROM routines" + where + " ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_decode(row) for row in rows]


def get_routine(routine_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
    return _decode(row) if row else None


def _validate(data):
    name = str(data.get("name") or "").strip()
    phrases = data.get("phrases") or []
    steps = data.get("steps") or []
    if not name:
        raise ValueError("name is required")
    if isinstance(phrases, str):
        phrases = [part.strip() for part in phrases.splitlines() if part.strip()]
    phrases = list(dict.fromkeys(str(part).strip() for part in phrases if str(part).strip()))
    if not phrases:
        raise ValueError("add at least one saying or phrase")
    if any(not normalize_phrase(phrase) for phrase in phrases):
        raise ValueError("a phrase cannot contain only filler words")
    if not isinstance(steps, list) or not steps:
        raise ValueError("add at least one action")
    cleaned_steps = []
    for index, step in enumerate(steps, 1):
        tool = str((step or {}).get("tool") or "")
        if tool not in ROUTINE_ACTIONS:
            raise ValueError(f"action {index} is not allowed in routines")
        args = (step or {}).get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"action {index} arguments must be an object")
        fields = ROUTINE_ACTIONS[tool]["fields"]
        unknown = set(args) - set(fields)
        if unknown:
            raise ValueError(f"action {index} has unknown field: {sorted(unknown)[0]}")
        clean_args = {}
        for key, spec in fields.items():
            value = args.get(key)
            if spec["type"] == "checkbox":
                value = bool(value)
            elif spec["type"] == "number" and value not in (None, ""):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"action {index} {key} must be a number")
            elif value is not None:
                value = str(value).strip()
            if spec.get("required") and value in (None, ""):
                raise ValueError(f"action {index} needs {spec['label'].lower()}")
            if spec.get("options") and value not in spec["options"]:
                raise ValueError(f"action {index} has an invalid {spec['label'].lower()}")
            if value not in (None, "", False):
                clean_args[key] = value
            elif spec["type"] == "checkbox":
                clean_args[key] = False
        cleaned_steps.append({"tool": tool, "args": clean_args})
    return {
        "name": name,
        "phrases": phrases,
        "steps": cleaned_steps,
        "enabled": bool(data.get("enabled", True)),
        "parallel": bool(data.get("parallel", True)),
    }


def save_routine(data, routine_id=None):
    clean = _validate(data)
    now = datetime.now().isoformat(timespec="seconds")
    values = (
        clean["name"], json.dumps(clean["phrases"]), json.dumps(clean["steps"]),
        int(clean["enabled"]), int(clean["parallel"]), now,
    )
    try:
        with _connect() as conn:
            if routine_id is None:
                cur = conn.execute(
                    "INSERT INTO routines "
                    "(name, phrases, steps, enabled, parallel, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (*values, now),
                )
                routine_id = cur.lastrowid
            else:
                cur = conn.execute(
                    "UPDATE routines SET name=?, phrases=?, steps=?, enabled=?, "
                    "parallel=?, updated=? WHERE id=?", (*values, routine_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"routine {routine_id} not found")
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("routine names must be unique") from exc
    return get_routine(routine_id)


def delete_routine(routine_id):
    with _connect() as conn:
        cur = conn.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"routine {routine_id} not found")


def match_routine(text):
    """Return the best enabled routine for this utterance, or ``None``."""
    variants = [text]
    try:
        from core import lingo
        expanded, matched = lingo.expand(text)
        if matched and expanded != text:
            variants.append(expanded)
    except Exception:
        pass
    matches = []
    for routine in list_routines(include_disabled=False):
        for phrase in routine["phrases"]:
            if any(_phrase_matches(variant, phrase) for variant in variants):
                # Prefer the most specific phrase when two aliases overlap.
                matches.append((len(normalize_phrase(phrase)), routine))
                break
    return max(matches, key=lambda item: item[0])[1] if matches else None


def note_run(routine_id):
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "UPDATE routines SET last_run = ?, run_count = run_count + 1 WHERE id = ?",
            (now, routine_id),
        )
        conn.commit()
