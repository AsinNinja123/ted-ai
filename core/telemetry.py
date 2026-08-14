"""core/telemetry.py — what actually happened on each turn, in the database.

Charlie has been reading Ted's behaviour out of a terminal, which means the
answer to "why was that slow" only exists while the terminal is open and only
if he happened to be looking. Everything the launch log prints about a turn —
which brain answered, how many tokens it cost, where the wait went, what failed
— is written here as one row per turn instead, so the dashboard can show it and
so a bad session can be read after the fact.

Three rules this module follows, because it sits on the reply path:

1. **It never raises into Ted.** Every public function swallows its own errors.
   A telemetry bug must not be able to break a conversation.
2. **It never blocks the reply.** Writes happen after the turn is already on
   screen, and the connection is opened once and reused.
3. **It records what happened, not what was intended.** A token count that is
   an estimate says so. A provider that was forced says so. This is the same
   ground-truth rule the action tools follow — a diagnostics panel that
   flatters the system is worse than no panel, because it gets believed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from core.paths import DATA

# TED_DB is honoured for the same reason dashboard/db.py honours it: the test
# suites drive ask_streaming() for real, and a turn log that writes into the
# live memory.db during a test run would put fake conversations in the panel
# Charlie uses to judge real ones. Setting it to a temp path (or to "" to
# disable writing entirely) keeps the diagnostics honest.
DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")
DISABLED = os.environ.get("TED_DB") == ""

# Groq reports the free-account ceiling in a response header; 8,000 is what
# Charlie's account returned on Aug 14. It is only used to draw the gauge, and
# the dashboard lets it be overridden, because being wrong about this number in
# the optimistic direction is how you conclude "we have headroom" and keep
# getting rate limited.
DEFAULT_TPM_LIMIT = 8000

_lock = threading.Lock()
_conn = None
_failed = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_log (
    id                INTEGER PRIMARY KEY,
    ts                TEXT    NOT NULL,
    epoch             REAL    NOT NULL,
    source            TEXT    NOT NULL DEFAULT 'chat',
    user_text         TEXT    NOT NULL DEFAULT '',
    reply             TEXT    NOT NULL DEFAULT '',
    provider          TEXT    NOT NULL DEFAULT '',
    model             TEXT    NOT NULL DEFAULT '',
    forced            TEXT    NOT NULL DEFAULT 'auto',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    tokens_estimated  INTEGER NOT NULL DEFAULT 0,
    reasoning         TEXT    NOT NULL DEFAULT '',
    context_scope     TEXT    NOT NULL DEFAULT '',
    history_msgs      INTEGER NOT NULL DEFAULT 0,
    tools_offered     TEXT    NOT NULL DEFAULT '',
    tools_called      TEXT    NOT NULL DEFAULT '',
    tool_rounds       INTEGER NOT NULL DEFAULT 0,
    ms_retrieval      INTEGER NOT NULL DEFAULT 0,
    ms_accepted       INTEGER NOT NULL DEFAULT 0,
    ms_first_token    INTEGER NOT NULL DEFAULT 0,
    ms_total          INTEGER NOT NULL DEFAULT 0,
    retries           TEXT    NOT NULL DEFAULT '',
    rate_limited      INTEGER NOT NULL DEFAULT 0,
    error             TEXT    NOT NULL DEFAULT '',
    ctx_breakdown     TEXT    NOT NULL DEFAULT '',
    ok                INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_turn_log_epoch ON turn_log(epoch);
"""


def _connect():
    global _conn, _failed
    if DISABLED:
        return None
    if _conn is not None or _failed:
        return _conn
    try:
        os.makedirs(DATA, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Existing installs already have turn_log without this column.
        try:
            conn.execute("ALTER TABLE turn_log ADD COLUMN "
                         "ctx_breakdown TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass                     # already there
        conn.commit()
        _conn = conn
    except Exception as e:                                   # pragma: no cover
        # One warning, then stay quiet. A dashboard that cannot record is a
        # nuisance; a dashboard that prints a traceback on every turn is worse
        # than the problem it was built to solve.
        print(f"[telemetry] disabled — {e}")
        _failed = True
    return _conn


def _trim(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "…"


class Turn:
    """A single interaction, collected as it happens and written once at the end.

    Deliberately a plain mutable object rather than a context manager: the
    streaming generator in llm.py yields control to the caller many times
    mid-turn, so there is no single block to wrap.
    """

    __slots__ = ("t0", "source", "user_text", "reply", "provider", "model",
                 "forced", "prompt_tokens", "completion_tokens",
                 "tokens_estimated", "reasoning", "context_scope",
                 "history_msgs", "tools_offered", "tools_called", "tool_rounds",
                 "ms_retrieval", "ms_accepted", "ms_first_token", "retries",
                 "rate_limited", "error", "ctx_breakdown", "written")

    def __init__(self, user_text="", source="chat"):
        self.t0 = time.time()
        self.source = source
        self.user_text = user_text or ""
        self.reply = ""
        self.provider = ""
        self.model = ""
        self.forced = "auto"
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tokens_estimated = True
        self.reasoning = ""
        self.context_scope = ""
        self.history_msgs = 0
        self.tools_offered = []
        self.tools_called = []
        self.tool_rounds = 0
        self.ms_retrieval = 0
        self.ms_accepted = 0
        self.ms_first_token = 0
        self.retries = []
        self.rate_limited = False
        self.error = ""
        self.ctx_breakdown = ""
        self.written = False

    # -- collection ------------------------------------------------------
    def note_retry(self, kind):
        self.retries.append(kind)

    def note_tool(self, name):
        self.tools_called.append(name)

    def elapsed_ms(self):
        return int((time.time() - self.t0) * 1000)

    # -- write -----------------------------------------------------------
    def finish(self, reply=None, error=""):
        """Write the row. Safe to call twice; the second call is ignored."""
        if self.written:
            return
        self.written = True
        if reply is not None:
            self.reply = reply
        if error:
            self.error = str(error)
        try:
            conn = _connect()
            if conn is None:
                return
            total = self.prompt_tokens + self.completion_tokens
            with _lock:
                conn.execute(
                    "INSERT INTO turn_log (ts, epoch, source, user_text, reply, "
                    "provider, model, forced, prompt_tokens, completion_tokens, "
                    "total_tokens, tokens_estimated, reasoning, context_scope, "
                    "history_msgs, tools_offered, tools_called, tool_rounds, "
                    "ms_retrieval, ms_accepted, ms_first_token, ms_total, "
                    "retries, rate_limited, error, ctx_breakdown, ok) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), self.t0,
                     self.source, _trim(self.user_text, 2000),
                     _trim(self.reply, 4000), self.provider, self.model,
                     self.forced, self.prompt_tokens, self.completion_tokens,
                     total, 1 if self.tokens_estimated else 0, self.reasoning,
                     self.context_scope, self.history_msgs,
                     ",".join(self.tools_offered), ",".join(self.tools_called),
                     self.tool_rounds, self.ms_retrieval, self.ms_accepted,
                     self.ms_first_token, self.elapsed_ms(),
                     ",".join(self.retries), 1 if self.rate_limited else 0,
                     _trim(self.error, 1000), self.ctx_breakdown,
                     0 if self.error else 1))
                conn.commit()
        except Exception as e:                               # pragma: no cover
            print(f"[telemetry] write failed — {e}")


# ---------- reads, for the dashboard ----------

def recent(limit=50, offset=0):
    """Most recent turns, newest first."""
    conn = _connect()
    if conn is None:
        return []
    try:
        with _lock:
            cur = conn.execute(
                "SELECT * FROM turn_log ORDER BY id DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def token_rate(window=60.0):
    """Tokens billed in the last `window` seconds.

    This is the number that decides whether the next message gets served or
    rate limited, and it was invisible until now — the free-tier ceiling is
    per minute, so a per-turn count alone never told Charlie how close he was.
    """
    conn = _connect()
    if conn is None:
        return 0
    try:
        with _lock:
            cur = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM turn_log "
                "WHERE epoch >= ?", (time.time() - window,))
            return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def stats(window=3600.0):
    """Rollup for the diagnostics header."""
    conn = _connect()
    if conn is None:
        return {}
    try:
        since = time.time() - window
        with _lock:
            row = conn.execute(
                "SELECT COUNT(*), "
                "       COALESCE(SUM(total_tokens), 0), "
                "       COALESCE(AVG(NULLIF(total_tokens, 0)), 0), "
                "       COALESCE(AVG(NULLIF(ms_total, 0)), 0), "
                "       SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), "
                "       SUM(rate_limited), "
                "       SUM(CASE WHEN provider = 'ollama' THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN provider = 'reflex' THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN retries != '' THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN tools_called LIKE '%find_tools%' "
                "                THEN 1 ELSE 0 END) "
                "FROM turn_log WHERE epoch >= ?", (since,)).fetchone()
        keys = ("turns", "tokens", "avg_tokens", "avg_ms", "errors",
                "rate_limited", "local_turns", "reflex_turns", "retry_turns",
                "find_tools_turns")
        out = {k: (v or 0) for k, v in zip(keys, row)}
        out["avg_tokens"] = round(out["avg_tokens"])
        out["avg_ms"] = round(out["avg_ms"])
        out["tpm"] = token_rate()
        out["tpm_limit"] = DEFAULT_TPM_LIMIT
        return out
    except Exception:
        return {}


def as_report(limit=25):
    """A plain-text digest of the last N turns.

    Exists so Charlie can hand a session to another AI — which is how this
    project actually gets debugged — without copying a terminal buffer.
    """
    rows = recent(limit)
    if not rows:
        return "No turns recorded yet."
    s = stats()
    out = [
        f"Ted session report — {len(rows)} most recent turns",
        f"Last hour: {s.get('turns', 0)} turns, {s.get('tokens', 0)} tokens, "
        f"avg {s.get('avg_tokens', 0)} tok / {s.get('avg_ms', 0)}ms, "
        f"{s.get('errors', 0)} errors, {s.get('rate_limited', 0)} rate limited, "
        f"{s.get('local_turns', 0)} served locally.",
        f"Tokens in the last minute: {s.get('tpm', 0)} of {s.get('tpm_limit', 0)}.",
        "",
    ]
    for r in reversed(rows):
        out.append(
            f"[{r['ts']}] {r['provider'] or '-'}"
            f"{'(forced ' + r['forced'] + ')' if r['forced'] != 'auto' else ''} "
            f"{r['total_tokens']}tok{'~' if r['tokens_estimated'] else ''} "
            f"{r['ms_total']}ms"
            f"{' RATE-LIMITED' if r['rate_limited'] else ''}"
            f"{' retries=' + r['retries'] if r['retries'] else ''}")
        out.append(f"    > {_trim(r['user_text'], 160)}")
        if r["tools_called"]:
            out.append(f"    tools: {r['tools_called']}")
        if r["error"]:
            out.append(f"    ERROR: {_trim(r['error'], 300)}")
        out.append(f"    < {_trim(r['reply'], 200)}")
    return "\n".join(out)


def clear():
    """Wipe the log. Used from the dashboard before a clean test run."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with _lock:
            n = conn.execute("SELECT COUNT(*) FROM turn_log").fetchone()[0]
            conn.execute("DELETE FROM turn_log")
            conn.commit()
        return n
    except Exception:
        return 0
