"""core/news.py — standing subscriptions to things Charlie wants watched.

Not a search tool. web_search already answers "what happened today"; this
answers "tell me when something happens", which needs three things a search
does not: the topics persist, what has already been shown is remembered, and
the checking happens without being asked.

Two sources, both free and keyless:

* **Hacker News** via the Algolia API, for anything computing-shaped. Carries a
  points count, which is the only cheap signal of whether a story mattered.
* **DuckDuckGo news** for everything else and for mainstream coverage.

Deduplication is by normalised URL rather than title, because the same story
reaches both sources with different headlines, and by title only as a
fallback for the items that arrive with no link at all.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from core.paths import DATA

DB_PATH = os.environ.get("TED_DB") or os.path.join(DATA, "memory.db")

# Long enough that Ted is not hammering two public APIs all day, short enough
# that "the latest" means today. Charlie can always ask, which checks now.
DEFAULT_INTERVAL = 45 * 60
MAX_ITEMS_PER_TOPIC = 12
# Stories older than this are not "new" however recently Ted first saw them —
# it stops a newly added topic dumping a month of history into a toast.
FRESH_DAYS = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_topics (
    id           INTEGER PRIMARY KEY,
    label        TEXT NOT NULL UNIQUE,
    query        TEXT NOT NULL,
    sources      TEXT NOT NULL DEFAULT 'hn,web',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created      TEXT NOT NULL,
    last_checked TEXT NOT NULL DEFAULT '',
    last_error   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS news_items (
    id        INTEGER PRIMARY KEY,
    topic_id  INTEGER NOT NULL,
    title     TEXT NOT NULL,
    url       TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    points    INTEGER NOT NULL DEFAULT 0,
    dedup_key TEXT NOT NULL,
    seen      INTEGER NOT NULL DEFAULT 0,
    created   TEXT NOT NULL,
    UNIQUE(topic_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_news_unseen ON news_items(seen, created DESC);
"""

_lock = threading.Lock()


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _dedup_key(title, url):
    """Same story from two sources must collapse to one row.

    The URL is the identity when there is one, stripped of the tracking
    parameters that make the same link look like three different ones. A story
    with no URL falls back to its flattened title.
    """
    url = (url or "").strip()
    if url:
        try:
            parts = urllib.parse.urlsplit(url.lower())
            query = urllib.parse.parse_qsl(parts.query)
            keep = [(k, v) for k, v in query
                    if not k.startswith(("utm_", "ref", "fbclid", "gclid"))]
            host = parts.netloc.removeprefix("www.")
            path = parts.path.rstrip("/")
            return f"{host}{path}?{urllib.parse.urlencode(sorted(keep))}".rstrip("?")
        except Exception:
            return url.lower()
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:120]


# ---------- topics ----------

def add_topic(label, query="", sources="hn,web"):
    """Start watching something. Returns (topic_dict, error)."""
    label = " ".join(str(label or "").split())[:80]
    if not label:
        return None, "a topic needs a name"
    query = " ".join(str(query or label).split())[:200]
    valid = [s for s in str(sources or "").split(",") if s.strip() in ("hn", "web")]
    sources = ",".join(valid) or "hn,web"
    try:
        with _lock, _connect() as conn:
            cur = conn.execute(
                "INSERT INTO news_topics (label, query, sources, created) "
                "VALUES (?,?,?,?)", (label, query, sources, _now()))
            conn.commit()
            topic_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return None, f"already watching {label!r}"
    except Exception as exc:
        return None, f"couldn't save that topic ({exc})"
    return get_topic(topic_id), ""


def get_topic(topic_id):
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id,label,query,sources,enabled,created,last_checked,last_error "
            "FROM news_topics WHERE id=?", (topic_id,)).fetchone()
    return _topic_row(row) if row else None


def _topic_row(row):
    return {"id": row[0], "label": row[1], "query": row[2], "sources": row[3],
            "enabled": bool(row[4]), "created": row[5],
            "last_checked": row[6], "last_error": row[7]}


def list_topics(include_disabled=True):
    sql = ("SELECT id,label,query,sources,enabled,created,last_checked,last_error "
           "FROM news_topics")
    if not include_disabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY label"
    try:
        with _lock, _connect() as conn:
            return [_topic_row(r) for r in conn.execute(sql)]
    except Exception as exc:
        print(f"[news] could not list topics: {exc}")
        return []


def remove_topic(label_or_id):
    """Stop watching. Accepts the label or the id; returns (removed, error)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id,label FROM news_topics WHERE id=? OR lower(label)=lower(?)",
            (label_or_id if str(label_or_id).isdigit() else -1,
             str(label_or_id))).fetchone()
        if not row:
            return None, f"I'm not watching {label_or_id!r}"
        conn.execute("DELETE FROM news_items WHERE topic_id=?", (row[0],))
        conn.execute("DELETE FROM news_topics WHERE id=?", (row[0],))
        conn.commit()
    return row[1], ""


# ---------- fetching ----------

def _fetch_hn(query, limit):
    params = urllib.parse.urlencode({
        "query": query, "tags": "story", "hitsPerPage": limit})
    url = f"https://hn.algolia.com/api/v1/search_by_date?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "Ted/1.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.load(response)
    out = []
    for hit in data.get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title[:300],
            # A self-post has no URL of its own; the discussion is the story.
            "url": (hit.get("url")
                    or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"),
            "source": "Hacker News",
            "published": (hit.get("created_at") or "")[:19],
            "points": int(hit.get("points") or 0),
        })
    return out


def _fetch_web(query, limit):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    with DDGS(timeout=10) as ddgs:
        raw = list(ddgs.news(query, max_results=limit))
    out = []
    for item in raw:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title[:300],
            "url": (item.get("url") or "").strip(),
            "source": (item.get("source") or "web")[:80],
            "published": (item.get("date") or "")[:19],
            "points": 0,
        })
    return out


def _is_fresh(published):
    """Reject anything old enough that calling it new would be a lie."""
    if not published:
        return True                # undated: let dedup decide instead
    text = published.replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return age <= FRESH_DAYS * 86400


def check_topic(topic, limit=MAX_ITEMS_PER_TOPIC):
    """Fetch one topic and store only what has not been stored before.

    Returns the list of genuinely new items. A source that fails is recorded
    against the topic and skipped — one dead API must not silence the other.
    """
    found, errors = [], []
    wanted = [s.strip() for s in (topic.get("sources") or "hn,web").split(",")]
    for source in wanted:
        try:
            if source == "hn":
                found.extend(_fetch_hn(topic["query"], limit))
            elif source == "web":
                found.extend(_fetch_web(topic["query"], limit))
        except Exception as exc:
            errors.append(f"{source}: {str(exc)[:80]}")
            print(f"[news] {topic['label']} via {source}: {exc}")

    fresh = [i for i in found if _is_fresh(i["published"])]
    new_rows = []
    try:
        with _lock, _connect() as conn:
            for item in fresh:
                key = _dedup_key(item["title"], item["url"])
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO news_items (topic_id,title,url,"
                        "source,published,points,dedup_key,seen,created) "
                        "VALUES (?,?,?,?,?,?,?,0,?)",
                        (topic["id"], item["title"], item["url"], item["source"],
                         item["published"], item["points"], key, _now()))
                    if cur.rowcount:
                        new_rows.append(dict(item, id=cur.lastrowid,
                                             topic=topic["label"]))
                except Exception as exc:
                    print(f"[news] insert skipped: {exc}")
            conn.execute(
                "UPDATE news_topics SET last_checked=?, last_error=? WHERE id=?",
                (_now(), "; ".join(errors)[:200], topic["id"]))
            conn.commit()
    except Exception as exc:
        print(f"[news] could not store items: {exc}")
    return new_rows


def check_all():
    """Check every enabled topic. Returns all newly found items."""
    fresh = []
    for topic in list_topics(include_disabled=False):
        fresh.extend(check_topic(topic))
    return fresh


# ---------- reading ----------

def unseen(limit=12, topic=""):
    sql = ("SELECT i.id,i.title,i.url,i.source,i.published,i.points,t.label "
           "FROM news_items i JOIN news_topics t ON t.id=i.topic_id "
           "WHERE i.seen=0")
    params = []
    if topic:
        sql += " AND lower(t.label)=lower(?)"
        params.append(topic)
    # Points first so a 400-point story outranks a 2-point one posted later;
    # a chronological digest buries the only item that mattered.
    sql += " ORDER BY i.points DESC, i.created DESC LIMIT ?"
    params.append(max(1, min(int(limit or 12), 40)))
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        print(f"[news] could not read items: {exc}")
        return []
    return [{"id": r[0], "title": r[1], "url": r[2], "source": r[3],
             "published": r[4], "points": r[5], "topic": r[6]} for r in rows]


def mark_seen(ids=()):
    """Mark items read. With no ids, marks everything."""
    try:
        with _lock, _connect() as conn:
            if ids:
                conn.executemany("UPDATE news_items SET seen=1 WHERE id=?",
                                 [(int(i),) for i in ids])
            else:
                conn.execute("UPDATE news_items SET seen=1 WHERE seen=0")
            conn.commit()
    except Exception as exc:
        print(f"[news] could not mark seen: {exc}")


def unseen_count():
    try:
        with _lock, _connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM news_items WHERE seen=0").fetchone()[0]
    except Exception:
        return 0


def format_digest(items, mark=True):
    """Human-readable digest. Marks the items read, since they have been."""
    if not items:
        return ""
    by_topic = {}
    for item in items:
        by_topic.setdefault(item["topic"], []).append(item)
    lines = []
    for label, group in by_topic.items():
        lines.append(f"{label}:")
        for item in group:
            points = f" [{item['points']} pts]" if item.get("points") else ""
            where = f" — {item['source']}" if item.get("source") else ""
            lines.append(f"  • {item['title']}{where}{points}")
            if item.get("url"):
                lines.append(f"    {item['url']}")
    if mark:
        mark_seen([i["id"] for i in items if i.get("id")])
    return "\n".join(lines)
