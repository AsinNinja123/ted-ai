"""
core/email.py — Outlook email via IMAP/SMTP.

No Azure, no OAuth, no browser. Uses your Outlook.com email + password directly.

One-time setup:
  1. Enable IMAP in Outlook.com → Settings → View all → Mail → Sync email → POP and IMAP
  2. Run: python3 ~/ted-ai/setup_email.py
  3. Say "check my email" to Ted

If you have 2-step verification on your Microsoft account, create an App Password at
account.microsoft.com → Security → Advanced security options → App passwords
and use that instead of your regular password.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 25 (§25.3)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Outlook email over IMAP and SMTP — the old, boring, universally supported
#      protocols. No OAuth, no browser, no Microsoft sign-in dance.
#
#  THE HONEST PROBLEM WITH THIS FILE
#      Your password sits in ~/.ted_email_config.json in plain text. The Microsoft
#      Graph path that would fix that was abandoned about one line from working.
#      See §35 — it is a real item, not a nitpick.
#
# =============================================================================

import imaplib
import smtplib
import json
import os
import re
import email as _email_lib
import email.header
from email.utils import parseaddr
from email.mime.text import MIMEText

IMAP_HOST  = "outlook.office365.com"
IMAP_PORT  = 993
SMTP_HOST  = "smtp.office365.com"
SMTP_PORT  = 587
CONFIG_FILE = os.path.expanduser("~/.ted_email_config.json")

_inbox_cache = []   # last-fetched emails keyed by 1-based position


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config():
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(
            "Email not set up. Run: python3 ~/ted-ai/setup_email.py"
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


def is_connected():
    if not os.path.exists(CONFIG_FILE):
        return False
    try:
        cfg = _load_config()
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(cfg["email"], cfg["password"])
        return True
    except Exception:
        return False


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def _imap():
    cfg = _load_config()
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(cfg["email"], cfg["password"])
    return conn


def _decode_header(value):
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return " ".join(out).strip()


def _strip_html(text):
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for ent, ch in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(msg):
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                try:
                    plain = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    pass
            elif ct == "text/html" and not html:
                try:
                    raw = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    html = _strip_html(raw)
                except Exception:
                    pass
        return (plain or html or "").strip()
    payload = msg.get_payload(decode=True)
    if payload:
        text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            text = _strip_html(text)
        return text.strip()
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def get_inbox(limit=5):
    """Fetch recent inbox emails. Returns list of dicts and caches by position."""
    global _inbox_cache
    limit = min(int(limit), 10)
    emails = []
    with _imap() as imap:
        imap.select("INBOX")
        typ, data = imap.uid("SEARCH", None, "ALL")
        if typ != "OK":
            return []
        all_uids = data[0].split()
        if not all_uids:
            return []
        # Newest first
        fetch_uids = list(reversed(all_uids[-limit:]))
        for uid in fetch_uids:
            typ, data = imap.uid("FETCH", uid, "(RFC822.HEADER FLAGS)")
            if typ != "OK" or not data or not data[0]:
                continue
            raw_header = data[0][1]
            flags_str  = data[0][0].decode()
            msg = _email_lib.message_from_bytes(raw_header)
            is_read = "\\Seen" in flags_str
            from_raw = msg.get("From", "")
            name, addr = parseaddr(from_raw)
            sender = _decode_header(name) or addr or "Unknown"
            emails.append({
                "index":        len(emails) + 1,
                "uid":          uid.decode(),
                "sender_name":  sender,
                "sender_email": addr,
                "subject":      _decode_header(msg.get("Subject", "(no subject)")),
                "read":         is_read,
            })
    _inbox_cache = emails
    return emails


def get_email_body(position):
    """Fetch the plain-text body of inbox email at 1-based position."""
    em = get_cached_email(position)
    if not em:
        return ""
    with _imap() as imap:
        imap.select("INBOX")
        typ, data = imap.uid("FETCH", em["uid"].encode(), "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            return ""
        msg = _email_lib.message_from_bytes(data[0][1])
        return _extract_body(msg)[:4000]


def delete_email(position):
    global _inbox_cache
    em = get_cached_email(position)
    if not em:
        return "Couldn't find that email."
    with _imap() as imap:
        imap.select("INBOX")
        imap.uid("STORE", em["uid"].encode(), "+FLAGS", "\\Deleted")
        imap.expunge()
    _inbox_cache = [e for e in _inbox_cache if e["index"] != position]
    return "Deleted."


def flag_email(position):
    em = get_cached_email(position)
    if not em:
        return "Couldn't find that email."
    with _imap() as imap:
        imap.select("INBOX")
        imap.uid("STORE", em["uid"].encode(), "+FLAGS", "\\Flagged")
    return "Flagged."


def mark_read(position):
    em = get_cached_email(position)
    if not em:
        return "Couldn't find that email."
    with _imap() as imap:
        imap.select("INBOX")
        imap.uid("STORE", em["uid"].encode(), "+FLAGS", "\\Seen")
    return "Marked as read."


def reply_to_email(position, body):
    em = get_cached_email(position)
    if not em:
        return "Couldn't find that email."
    cfg = _load_config()
    subject = em["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg = MIMEText(body, "plain")
    msg["From"]    = cfg["email"]
    msg["To"]      = em["sender_email"]
    msg["Subject"] = subject
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["email"], cfg["password"])
        smtp.sendmail(cfg["email"], [em["sender_email"]], msg.as_string())
    return "Reply sent."


def send_email(to_address, subject, body):
    cfg = _load_config()
    msg = MIMEText(body, "plain")
    msg["From"]    = cfg["email"]
    msg["To"]      = to_address
    msg["Subject"] = subject
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["email"], cfg["password"])
        smtp.sendmail(cfg["email"], [to_address], msg.as_string())
    return f"Email sent to {to_address}."


def get_cached_email(position):
    for e in _inbox_cache:
        if e["index"] == int(position):
            return e
    return None
