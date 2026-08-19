"""core/messages.py — reading incoming texts, and deciding which are worth a word.

Charlie asked for a bouncer: tell him when Gavin texts, offer to read it aloud
or open it, and leave everything else alone.

macOS gives no supported way to observe another app's notifications, so this
reads the Messages database directly. Consequences, stated plainly because they
are the whole cost of the feature:

* It needs **Full Disk Access** granted to whatever binary runs Ted. Until then
  every function here reports that clearly rather than returning empty results,
  because "no new messages" and "I am not allowed to look" are different
  answers and only one of them is true.
* The database is opened **read-only**, through a URI with mode=ro. Charlie's
  message history is not Ted's to modify, and a corrupted chat.db is not a
  recoverable mistake.
* Ted sees everything in there. The bouncer decides what is *said*, but there
  is no way to read only some rows.

Nothing is copied out of chat.db into Ted's own storage except the last row id
seen. Message text stays where Apple put it.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 25 (§25.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Reading incoming iMessages, by opening the Messages database directly.
#      macOS gives no supported way to observe another app's notifications, so this
#      is the only route — and it has two costs, both stated plainly in the file:
#
#      1. It needs Full Disk Access granted to whatever launches Ted. Until then
#         every function here says so, clearly, rather than returning empty results.
#         "No new messages" and "I am not allowed to look" are different answers and
#         only one of them is true.
#      2. The database is opened READ ONLY, always.
#
# =============================================================================

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# Apple stores message timestamps as nanoseconds since 2001-01-01 UTC on
# anything modern, and as seconds on very old installs. Both are handled.
APPLE_EPOCH = datetime(2001, 1, 1)

_contact_cache = {}
_contact_lock = threading.Lock()


def available():
    """Return (ok, reason). Never raises, and never lies about why."""
    if not os.path.exists(CHAT_DB):
        return False, ("there's no Messages database on this Mac — "
                       "sign in to Messages first")
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=4)
        conn.execute("SELECT COUNT(*) FROM message LIMIT 1").fetchone()
        conn.close()
        return True, ""
    except sqlite3.OperationalError as exc:
        if "unable to open" in str(exc).lower() or "authoriz" in str(exc).lower():
            return False, (
                "macOS is blocking access to your messages. Give Ted Full Disk "
                "Access: System Settings → Privacy & Security → Full Disk Access, "
                "add Ted (or your terminal, if you launch Ted from one), then "
                "restart Ted.")
        return False, f"the Messages database would not open ({exc})"
    except Exception as exc:
        return False, f"the Messages database would not open ({exc})"


def _connect():
    return sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=6)


def _apple_time(value):
    """Apple's message date → datetime. Tolerates both known encodings."""
    try:
        raw = float(value or 0)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    seconds = raw / 1e9 if raw > 1e11 else raw
    try:
        return APPLE_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError):
        return None


# A typedstream archive, not a plist, so plistlib cannot read it. The body sits
# after an NSString marker with a length prefix that is one byte under 128 and
# four bytes above it. Tolerant on purpose: a wrong guess must yield nothing
# rather than mojibake presented as Gavin's words.
_ATTR_MARKER = re.compile(rb"NSString\x01\x94\x84\x01\+")


def decode_body(text, attributed):
    """Get the message text. Modern macOS leaves `text` NULL and fills
    `attributedBody` instead, which is why a naive reader shows blank texts."""
    if text:
        return str(text)
    if not attributed:
        return ""
    try:
        data = bytes(attributed)
    except Exception:
        return ""
    match = _ATTR_MARKER.search(data)
    if not match:
        return ""
    pos = match.end()
    if pos >= len(data):
        return ""
    length = data[pos]
    pos += 1
    if length == 0x81:                      # 2-byte little-endian length
        if pos + 2 > len(data):
            return ""
        length = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
    elif length >= 0x80:                    # unknown widening — do not guess
        return ""
    body = data[pos:pos + length]
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", "replace")


def contact_name(handle):
    """Turn +15155550123 or an email into a name, or return the handle.

    Reverse lookup, so core/actions.search_contacts (which searches by name)
    is the wrong direction and cannot be reused.
    """
    handle = (handle or "").strip()
    if not handle:
        return ""
    with _contact_lock:
        if handle in _contact_cache:
            return _contact_cache[handle]
    name = handle
    # Match on the last 10 digits: Contacts stores "(515) 555-0123" while
    # Messages reports "+15155550123", and neither normalises to the other.
    digits = re.sub(r"\D", "", handle)
    tail = digits[-10:] if len(digits) >= 10 else ""
    safe = handle.replace('"', '')
    script = f'''
    tell application "Contacts"
        set out to ""
        repeat with p in people
            repeat with e in emails of p
                if (value of e as string) is "{safe}" then
                    set out to name of p
                    exit repeat
                end if
            end repeat
            if out is not "" then exit repeat
            repeat with ph in phones of p
                set v to value of ph as string
                set d to ""
                repeat with c in characters of v
                    if c is in "0123456789" then set d to d & c
                end repeat
                if (length of d) >= 10 and "{tail}" is not "" then
                    if (text -10 thru -1 of d) is "{tail}" then
                        set out to name of p
                        exit repeat
                    end if
                end if
            end repeat
            if out is not "" then exit repeat
        end repeat
        return out
    end tell'''
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=12)
        found = (result.stdout or "").strip()
        if found:
            name = found
    except Exception as exc:
        print(f"[messages] contact lookup failed: {exc}")
    with _contact_lock:
        _contact_cache[handle] = name
    return name


def latest_rowid():
    """The newest message id, so a first run does not replay the archive."""
    ok, _ = available()
    if not ok:
        return 0
    try:
        conn = _connect()
        row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
        conn.close()
        return int(row[0] or 0)
    except Exception as exc:
        print(f"[messages] could not read latest id: {exc}")
        return 0


def incoming_since(rowid, limit=20):
    """Incoming messages newer than `rowid`, oldest first.

    Returns (messages, error). Only messages FROM other people: is_from_me
    filters out Charlie's own, which would otherwise be announced back to him.
    """
    ok, reason = available()
    if not ok:
        return [], reason
    sql = """
        SELECT m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
               m.cache_has_attachments, h.id, m.service,
               c.display_name, c.chat_identifier, m.is_read
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE m.ROWID > ? AND m.is_from_me = 0
        ORDER BY m.ROWID ASC
        LIMIT ?
    """
    try:
        conn = _connect()
        rows = conn.execute(sql, (int(rowid or 0), int(limit))).fetchall()
        conn.close()
    except Exception as exc:
        return [], f"I couldn't read your messages ({exc})"
    out = []
    for r in rows:
        handle = r[6] or ""
        when = _apple_time(r[3])
        body = decode_body(r[1], r[2])
        out.append({
            "id": int(r[0]),
            "body": body,
            "when": when.isoformat(timespec="seconds") if when else "",
            "has_attachment": bool(r[5]),
            "handle": handle,
            "service": r[7] or "",
            # A group chat has a display name; a one-to-one does not.
            "group": (r[8] or "") if (r[8] or "") else "",
            "is_read": bool(r[10]),
        })
    return out, ""


def describe(message, name=None):
    """One line a person would say. Used for the toast and for speech."""
    who = name or contact_name(message.get("handle", "")) or "Someone"
    where = f" in {message['group']}" if message.get("group") else ""
    if message.get("has_attachment") and not message.get("body"):
        return f"{who} sent you an attachment{where}."
    return f"{who} sent you a text{where}."


def preview(message, limit=160):
    """The message itself, trimmed. Empty when there is nothing readable."""
    body = " ".join((message.get("body") or "").split())
    if not body:
        return ""
    return body if len(body) <= limit else body[:limit - 1] + "…"


def open_conversation(handle):
    """Bring the thread up in Messages. Verified, not assumed."""
    handle = (handle or "").strip()
    if not handle:
        return "I don't know which conversation to open."
    try:
        result = subprocess.run(
            ["open", f"imessage://{handle}"], capture_output=True, timeout=8)
        if result.returncode == 0:
            return f"Opened your conversation with {contact_name(handle)}."
    except Exception as exc:
        print(f"[messages] open failed: {exc}")
    # Falling back to activating the app is honest about the difference: the
    # window is up, but not necessarily on the right thread.
    try:
        subprocess.run(["open", "-a", "Messages"], capture_output=True, timeout=8)
        return ("I opened Messages, but I couldn't jump straight to that "
                "conversation.")
    except Exception:
        return "I couldn't open Messages."
