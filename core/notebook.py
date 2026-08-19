"""core/notebook.py — Ted's own notebook: named pages he can read, write and edit.

This is deliberately NOT Apple Notes (`core/notes.py`) and NOT the knowledge
base. Apple Notes is Charlie's app and Ted is a guest in it; the knowledge base
is a vector store Ted searches and cannot revise. The notebook is Ted's, it is
structured, and every operation on it is exact — no embedding, no similarity, no
guessing which page was meant.

Shape: a page is a name plus an ordered list of entries. An entry is one thing
that got written down, stamped with when and by whom. "Add this to my fixes
page" appends an entry; "change the third line" edits one; "what's on my fixes
page" reads them all back verbatim.

Why entries instead of one blob of text per page: editing. A blob forces Ted to
rewrite the whole page to change one line, and a model rewriting a page it only
partly remembers is how notes silently lose content. Numbered entries mean an
edit names exactly one row and cannot touch the rest.

Design principles this follows (see the handoff, §12):
  * No second source of truth — the notebook lives in data/memory.db beside
    everything else Ted knows, so a backup or a wipe covers it.
  * Ground truth over optimism — every writer returns what actually landed,
    including the entry number, so Ted quotes the result instead of narrating
    an intention.
  * Ted must know what is in it, not guess — `index_line()` is injected into
    every turn, so page names are in front of the model before it is asked
    anything. Contents still require a read; the index is the map, not the
    territory.
"""


# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 16 (§16.1 – §16.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Ted's own notebook: named pages of numbered entries that he can read,
#      write, edit and delete. Distinct from two things it is easy to confuse
#      it with:
#          Apple Notes (core/notes.py)  — your app. Ted is a guest there.
#          the knowledge base           — searchable, not revisable.
#
#  THE ONE DESIGN DECISION THAT MATTERS
#      A page is an ORDERED LIST OF ENTRIES, not a blob of text.
#
#      With a blob, changing one line means Ted rewriting the whole page from
#      memory — and a model rewriting a page it only half remembers is how
#      notes quietly lose content. With numbered entries, an edit names exactly
#      one row and physically cannot touch the rest. If you ever find yourself
#      tempted to store pages as one string, this is the paragraph to re-read.
#
#  HOW TED KNOWS WHAT IS IN IT WITHOUT GUESSING
#      Two mechanisms, deliberately split:
#          index_line()   is loaded into EVERY prompt. Page names and entry
#                         counts only — never contents. So Ted can neither
#                         invent a page nor deny one that exists. It returns ""
#                         when the notebook is empty, so an unused feature costs
#                         nothing.
#          notebook_read  is a TOOL CALL. Contents cost a read, always. The
#                         persona says plainly: what is on a page, he reads —
#                         never from memory, never paraphrased from an earlier
#                         turn.
#      Index = the map. Read = the territory. Neither alone would have worked.
#
#  PAGE NAMES ARE CLEANED BEFORE MATCHING
#      "my fixes page", "the fixes notes", "FIXES" and "fixes" are one page.
#      `_clean_name` does that. The model does not have to normalise, and two
#      phrasings cannot silently become two pages.
#
#  IF YOU WANT TO CHANGE SOMETHING
#      "Ted keeps making duplicate pages"  -> _clean_name is not stripping the
#                                              wording you actually use.
#      "The notebook panel looks wrong"    -> dashboard/notebook.html, not here.
#      "Reading a page truncates"          -> READ_LIMIT.
# =============================================================================

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from core.paths import DATA

# TED_DB for the same reason every other store honours it: a test harness that
# redirects the database must redirect ALL of it, or it writes into the real one.
DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")

# Page names are matched case-insensitively and stored as typed, so "Fixes",
# "fixes" and "FIXES" are one page that keeps whatever capitalisation it was
# created with. Two pages differing only in case would be indistinguishable out
# loud, which makes them a bug rather than a feature.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_pages (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notebook_entries (
    id      INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES notebook_pages(id) ON DELETE CASCADE,
    body    TEXT NOT NULL,
    writer  TEXT NOT NULL DEFAULT 'ted',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notebook_entries_page
    ON notebook_entries(page_id, id);
"""

MAX_NAME = 60
MAX_BODY = 4000
# What one read hands the model. A page with hundreds of entries would otherwise
# blow the context budget in a single tool result.
READ_LIMIT = 60


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


def _clean_name(name):
    name = " ".join(str(name or "").strip().split())
    # "my fixes page" / "the fixes notes page" all mean the page called fixes.
    # Stripping the wrapper here means the model does not have to, and two
    # phrasings of the same page cannot become two pages.
    low = name.casefold()
    for prefix in ("my ", "the "):
        if low.startswith(prefix):
            name = name[len(prefix):].strip()
            low = name.casefold()
    for suffix in (" notes page", " note page", " notebook page",
                   " notes", " page"):
        if low.endswith(suffix) and len(low) > len(suffix):
            name = name[: -len(suffix)].strip()
            low = name.casefold()
    if not name:
        raise ValueError("a page needs a name")
    if len(name) > MAX_NAME:
        raise ValueError(f"page names stay under {MAX_NAME} characters")
    return name


def _clean_body(body):
    body = str(body or "").strip()
    if not body:
        raise ValueError("there is nothing to write down")
    if len(body) > MAX_BODY:
        raise ValueError(f"one entry stays under {MAX_BODY} characters")
    return body


def _find_page(conn, name):
    return conn.execute(
        "SELECT * FROM notebook_pages WHERE name = ? COLLATE NOCASE",
        (name,)).fetchone()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def list_pages():
    """Every page with its entry count and when it last changed."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.created, p.updated,
                   (SELECT COUNT(*) FROM notebook_entries e WHERE e.page_id = p.id)
                       AS entries
            FROM notebook_pages p
            ORDER BY p.updated DESC
        """).fetchall()
    return [dict(r) for r in rows]


def read_page(name, limit=READ_LIMIT):
    """A page's entries, oldest first, numbered from 1 as they read on paper.

    Returns None when the page does not exist — the caller decides whether that
    is an error or an invitation to create it, and only the caller knows which.
    """
    name = _clean_name(name)
    with _connect() as conn:
        page = _find_page(conn, name)
        if not page:
            return None
        rows = conn.execute(
            "SELECT * FROM notebook_entries WHERE page_id = ? ORDER BY id",
            (page["id"],)).fetchall()
    entries = [dict(r) for r in rows]
    total = len(entries)
    kept = entries[-limit:] if limit and total > limit else entries
    first = total - len(kept) + 1
    for offset, entry in enumerate(kept):
        entry["number"] = first + offset
    return {"name": page["name"], "created": page["created"],
            "updated": page["updated"], "total": total, "entries": kept}


def create_page(name):
    """Make an empty page. Returns it; existing pages are returned untouched."""
    name = _clean_name(name)
    now = _now()
    with _connect() as conn:
        page = _find_page(conn, name)
        if not page:
            conn.execute(
                "INSERT INTO notebook_pages (name, created, updated) VALUES (?,?,?)",
                (name, now, now))
            conn.commit()
            page = _find_page(conn, name)
    return dict(page)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def add_entry(name, body, writer="ted"):
    """Append one entry, creating the page if this is the first thing on it.

    Returns (page_name, entry_number, created_page). The entry number comes back
    because the honesty rule means Ted reports what actually landed, and because
    the next instruction is often "change that one".
    """
    name = _clean_name(name)
    body = _clean_body(body)
    now = _now()
    with _connect() as conn:
        page = _find_page(conn, name)
        created_page = page is None
        if created_page:
            conn.execute(
                "INSERT INTO notebook_pages (name, created, updated) VALUES (?,?,?)",
                (name, now, now))
            page = _find_page(conn, name)
        conn.execute(
            "INSERT INTO notebook_entries (page_id, body, writer, created, updated) "
            "VALUES (?,?,?,?,?)",
            (page["id"], body, writer or "ted", now, now))
        conn.execute("UPDATE notebook_pages SET updated = ? WHERE id = ?",
                     (now, page["id"]))
        number = conn.execute(
            "SELECT COUNT(*) FROM notebook_entries WHERE page_id = ?",
            (page["id"],)).fetchone()[0]
        conn.commit()
    return page["name"], number, created_page


def _entry_at(conn, page, number):
    """The row for a 1-based entry number, or None. Negative counts from the end
    so "the last one" needs no prior read."""
    rows = conn.execute(
        "SELECT id FROM notebook_entries WHERE page_id = ? ORDER BY id",
        (page["id"],)).fetchall()
    if not rows:
        return None
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    if number < 0:
        number = len(rows) + 1 + number
    if number < 1 or number > len(rows):
        return None
    return rows[number - 1]["id"], number


def edit_entry(name, number, body, writer="ted"):
    """Replace one entry's text. Raises KeyError when the page or entry is gone."""
    name = _clean_name(name)
    body = _clean_body(body)
    now = _now()
    with _connect() as conn:
        page = _find_page(conn, name)
        if not page:
            raise KeyError(f"there is no page called '{name}'")
        found = _entry_at(conn, page, number)
        if not found:
            raise KeyError(f"'{page['name']}' has no entry {number}")
        entry_id, resolved = found
        conn.execute(
            "UPDATE notebook_entries SET body = ?, writer = ?, updated = ? WHERE id = ?",
            (body, writer or "ted", now, entry_id))
        conn.execute("UPDATE notebook_pages SET updated = ? WHERE id = ?",
                     (now, page["id"]))
        conn.commit()
    return page["name"], resolved


def delete_entry(name, number):
    """Remove one entry. Returns (page_name, number, removed_text)."""
    name = _clean_name(name)
    with _connect() as conn:
        page = _find_page(conn, name)
        if not page:
            raise KeyError(f"there is no page called '{name}'")
        found = _entry_at(conn, page, number)
        if not found:
            raise KeyError(f"'{page['name']}' has no entry {number}")
        entry_id, resolved = found
        body = conn.execute("SELECT body FROM notebook_entries WHERE id = ?",
                            (entry_id,)).fetchone()["body"]
        conn.execute("DELETE FROM notebook_entries WHERE id = ?", (entry_id,))
        conn.execute("UPDATE notebook_pages SET updated = ? WHERE id = ?",
                     (_now(), page["id"]))
        conn.commit()
    return page["name"], resolved, body


def rename_page(name, new_name):
    name = _clean_name(name)
    new_name = _clean_name(new_name)
    with _connect() as conn:
        page = _find_page(conn, name)
        if not page:
            raise KeyError(f"there is no page called '{name}'")
        clash = _find_page(conn, new_name)
        if clash and clash["id"] != page["id"]:
            raise ValueError(f"there is already a page called '{clash['name']}'")
        conn.execute("UPDATE notebook_pages SET name = ?, updated = ? WHERE id = ?",
                     (new_name, _now(), page["id"]))
        conn.commit()
    return page["name"], new_name


def delete_page(name):
    """Remove a page and everything on it. Returns (page_name, entries_removed)."""
    name = _clean_name(name)
    with _connect() as conn:
        page = _find_page(conn, name)
        if not page:
            raise KeyError(f"there is no page called '{name}'")
        count = conn.execute(
            "SELECT COUNT(*) FROM notebook_entries WHERE page_id = ?",
            (page["id"],)).fetchone()[0]
        conn.execute("DELETE FROM notebook_entries WHERE page_id = ?", (page["id"],))
        conn.execute("DELETE FROM notebook_pages WHERE id = ?", (page["id"],))
        conn.commit()
    return page["name"], count


# ---------------------------------------------------------------------------
# Searching and the per-turn index
# ---------------------------------------------------------------------------

def search(query, limit=12):
    """Plain substring search across every entry. Exact, not semantic — this is
    a notebook, and 'find where I wrote X' should find X."""
    query = str(query or "").strip()
    if not query:
        return []
    with _connect() as conn:
        rows = conn.execute("""
            SELECT p.name AS page, e.body, e.created
            FROM notebook_entries e JOIN notebook_pages p ON p.id = e.page_id
            WHERE e.body LIKE ? ESCAPE '\\'
            ORDER BY e.updated DESC LIMIT ?
        """, ("%" + query.replace("\\", "\\\\").replace("%", "\\%")
                          .replace("_", "\\_") + "%", int(limit))).fetchall()
    return [dict(r) for r in rows]


# [BOOK §16.3] ─── THE MAP THAT RIDES ALONG ──────────────────────────────────
# Page NAMES and entry counts, on every single turn. Never contents.
#
# This is the whole reason the notebook is something Ted KNOWS rather than
# something he might remember: he can no more invent a page than deny one that
# exists. It is one local SQLite read of a table with a handful of rows, and it
# returns "" when the notebook is empty — so an unused feature costs zero
# tokens.
#
# Contents cost a notebook_read tool call, always. Index = the map, read = the
# territory. Neither alone would have been enough.
def index_line(max_pages=14):
    """One line naming every page, for injection into every turn's context.

    This is the whole answer to "he should know what's in it, not guess": the
    model never has to wonder whether a notebook page exists, because the list
    of them is already in front of it. It sees names and sizes, not contents —
    contents cost a read, which is correct, and the read is exact.

    Returns "" when the notebook is empty, so an unused feature costs no tokens.
    """
    try:
        pages = list_pages()
    except Exception:
        return ""
    if not pages:
        return ""
    shown = pages[:max_pages]
    parts = []
    for p in shown:
        n = p["entries"]
        parts.append(f"{p['name']} ({n} " + ("entry" if n == 1 else "entries") + ")")
    line = "Your notebook pages: " + "; ".join(parts) + "."
    if len(pages) > len(shown):
        line += f" (+{len(pages) - len(shown)} more.)"
    return line
