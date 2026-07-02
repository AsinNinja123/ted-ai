"""
core/memory.py — Ted's long-term memory backed by a local Neo4j graph.

Graceful degradation: if Neo4j is unreachable, every public function returns
an empty value ([], "", None, False) and never raises. Ted continues on
in-session context alone.

Retrieval: keyword search over the full message first; falls back to the most
recent exchanges when nothing matches, so there's always some grounding context.
"""

import logging
import time
from datetime import datetime

# The driver logs harmless "label/relationship does not exist" notices on every
# lookup until the first fact is saved. Quiet them so the console stays readable.
for _n in ("neo4j", "neo4j.notifications"):
    logging.getLogger(_n).setLevel(logging.ERROR)

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, SessionExpired
except Exception:                       # driver not installed
    GraphDatabase = None
    class ServiceUnavailable(Exception): pass  # noqa: E701
    class SessionExpired(Exception):     pass  # noqa: E701

try:
    from config import NEO4J_PASSWORD
except Exception:
    NEO4J_PASSWORD = "TedBennet321"

try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"

URI = "bolt://localhost:7687"
USER = "neo4j"

_driver = None
_unavailable = False                    # True once we've confirmed we can't connect
_last_failure = 0.0                     # epoch time of last connection failure
_RETRY_INTERVAL = 180                   # seconds before retrying after a failure

# Tiny stop-word list so keyword search isn't dominated by "the", "what", etc.
_STOP = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "to",
    "of", "in", "on", "at", "for", "with", "do", "did", "does", "you", "i", "me",
    "my", "your", "it", "this", "that", "what", "who", "when", "where", "how",
    "can", "could", "would", "should", "tell", "about", "please", "hey", "ted",
}


def _ensure_indexes(driver):
    """Create a full-text index on Message.text so keyword retrieval scales instead
    of doing an O(n) substring scan as history grows. Idempotent; silently skipped
    on older Neo4j that lacks full-text indexes (get_memory falls back to CONTAINS)."""
    try:
        with driver.session() as session:
            session.run(
                "CREATE FULLTEXT INDEX message_text_ft IF NOT EXISTS "
                "FOR (m:Message) ON EACH [m.text]"
            )
    except Exception as e:
        print(f"[memory] full-text index setup skipped: {e}")


def _get_driver():
    """Connect once, lazily. Returns a driver or None if Neo4j is unreachable.

    Unlike the old hard-latch, a failed connection is retried after
    _RETRY_INTERVAL seconds so a cold-start or brief outage doesn't
    permanently kill memory for the session.
    """
    global _driver, _unavailable, _last_failure
    if GraphDatabase is None:
        return None
    # If we previously failed, only retry after the cool-down period
    if _unavailable:
        if time.time() - _last_failure < _RETRY_INTERVAL:
            return None
        # Cool-down elapsed — reset and try again
        _unavailable = False
        _driver = None
        print("[memory] Retrying Neo4j connection…")
    if _driver is None:
        try:
            try:                       # newer drivers can mute server notifications
                _driver = GraphDatabase.driver(
                    URI, auth=(USER, NEO4J_PASSWORD),
                    notifications_min_severity="OFF")
            except TypeError:          # older driver — no such option
                _driver = GraphDatabase.driver(URI, auth=(USER, NEO4J_PASSWORD))
            _driver.verify_connectivity()
            _ensure_indexes(_driver)
            if _last_failure:
                print("[memory] Neo4j reconnected.")
        except (ServiceUnavailable, SessionExpired, Exception) as e:
            _unavailable = True
            _last_failure = time.time()
            _driver = None
            print(f"[memory] Neo4j unavailable — using in-session memory only. ({e})")
    return _driver


def _neo4j_exc(e):
    """Return True if this exception means Neo4j dropped the connection.
    On these errors we mark the driver as unavailable so the next call retries."""
    global _unavailable, _last_failure, _driver
    if isinstance(e, (ServiceUnavailable, SessionExpired)):
        _unavailable = True
        _last_failure = time.time()
        _driver = None
        return True
    return False


def _keywords(text):
    """Extract meaningful search terms from text, stripping stop-words and punctuation."""
    words = [w.strip(".,!?;:'\"").lower() for w in text.split()]
    return [w for w in words if len(w) > 3 and w not in _STOP]  # skip tiny/common words


# ---- Personal Conversation Memory ----

def save_memory(user_input, ted_reply):
    """Persist one exchange as Message+Reply nodes linked to the owner. No-op if Neo4j is down."""
    driver = _get_driver()
    if driver is None:
        return  # graceful degradation — session memory still works
    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (c:Person {name: $owner})
                CREATE (m:Message {text: $user_input, timestamp: $ts})
                CREATE (r:Reply  {text: $ted_reply,  timestamp: $ts})
                CREATE (c)-[:SAID]->(m)
                CREATE (m)-[:REPLIED_WITH]->(r)
                """,
                # $owner is a Cypher param, not string-interpolated — prevents Cypher injection
                owner=OWNER_NAME, user_input=user_input, ted_reply=ted_reply,
                ts=datetime.now().isoformat(),
            )
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] save skipped: {e}")


def get_memory(query, limit=3):
    """Return a short string of relevant past exchanges, or '' if none/offline.

    Two-query fallback: keyword search first; if nothing matches, returns the most
    recent exchanges so the prompt always has some grounding context.
    """
    driver = _get_driver()
    if driver is None:
        return ""
    try:
        with driver.session() as session:
            keywords = _keywords(query)
            records = []
            if keywords:
                # Primary: full-text index lookup (scales as history grows). Falls
                # back to a substring scan if the index isn't available on this
                # Neo4j version, so behaviour degrades to the old path, never errors.
                try:
                    result = session.run(
                        """
                        CALL db.index.fulltext.queryNodes('message_text_ft', $q)
                          YIELD node AS m
                        MATCH (c:Person {name: $owner})-[:SAID]->(m)-[:REPLIED_WITH]->(r:Reply)
                        RETURN m.text AS question, r.text AS answer, m.timestamp AS ts
                        ORDER BY m.timestamp DESC
                        LIMIT $limit
                        """,
                        owner=OWNER_NAME, q=" OR ".join(keywords), limit=limit,
                    )
                    records = list(result)
                except Exception:
                    result = session.run(
                        """
                        MATCH (c:Person {name: $owner})-[:SAID]->(m:Message)-[:REPLIED_WITH]->(r:Reply)
                        WHERE any(k IN $keywords WHERE toLower(m.text) CONTAINS k)
                        RETURN m.text AS question, r.text AS answer, m.timestamp AS ts
                        ORDER BY m.timestamp DESC
                        LIMIT $limit
                        """,
                        owner=OWNER_NAME, keywords=keywords, limit=limit,
                    )
                    records = list(result)

            # Fallback: keyword search found nothing — return most recent exchanges for context
            if not records:
                result = session.run(
                    """
                    MATCH (c:Person {name: $owner})-[:SAID]->(m:Message)-[:REPLIED_WITH]->(r:Reply)
                    RETURN m.text AS question, r.text AS answer, m.timestamp AS ts
                    ORDER BY m.timestamp DESC
                    LIMIT $limit
                    """,
                    owner=OWNER_NAME, limit=limit,
                )
                records = list(result)

            lines = [f"Charlie said: {rec['question']} — Ted replied: {rec['answer']}"
                     for rec in records]
            return "\n".join(lines)
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] lookup skipped: {e}")
        return ""


# ---- Fact Graph ----

def save_fact(subject, relationship, obj):
    """Store a fact triple as a graph edge, e.g. save_fact('Charlie', 'STUDIES', 'CS').

    Uses MERGE on both nodes and the edge, so re-stating a known fact is a no-op.
    """
    driver = _get_driver()
    if driver is None:
        return
    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (s:Entity {name: $subject})
                MERGE (o:Entity {name: $obj})
                MERGE (s)-[:KNOWS {relationship: $relationship}]->(o)
                """,
                subject=subject, relationship=relationship, obj=obj,
            )
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] fact skipped: {e}")


def get_facts_about(subject):
    """Return a space-joined string of all known facts about subject, or '' if none/offline."""
    driver = _get_driver()
    if driver is None:
        return ""
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (s:Entity {name: $subject})-[r:KNOWS]->(o:Entity)
                RETURN r.relationship AS relationship, o.name AS object
                """,
                subject=subject,
            )
            facts = [f"{subject} {rec['relationship']} {rec['object']}" for rec in result]
            return " ".join(facts)
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] facts skipped: {e}")
        return ""


def close():
    """Close the Neo4j driver connection and reset state. Safe to call when already closed."""
    global _driver
    if _driver is not None:
        try:
            _driver.close()
        except Exception:
            pass
        _driver = None


# -----------------------------------------------------------------------
# Store Manager memory namespace — completely isolated from Ted's memories.
# Uses :Store/:StoreMessage/:StoreReply labels so queries never cross over.
# -----------------------------------------------------------------------

def save_store_memory(user_input, store_reply):
    """Persist one store-mode exchange under the StoreMessage/StoreReply namespace.

    Distinct node labels (:Store, :StoreMessage, :StoreReply) ensure store queries
    never accidentally return Ted's personal memories, and vice versa.
    """
    driver = _get_driver()
    if driver is None:
        return
    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (s:Store {name: 'MainStore'})
                CREATE (m:StoreMessage {text: $user_input, timestamp: $ts})
                CREATE (r:StoreReply   {text: $store_reply, timestamp: $ts})
                CREATE (s)-[:ASKED]->(m)
                CREATE (m)-[:REPLIED_WITH]->(r)
                """,
                user_input=user_input, store_reply=store_reply,
                ts=datetime.now().isoformat(),
            )
    except Exception as e:
        _neo4j_exc(e)
        print(f"[store memory] save skipped: {e}")


def get_store_memory(query, limit=3):
    """Return relevant past store exchanges, or '' if none/offline.

    Same two-query fallback as get_memory(): keyword match first, recency fallback second.
    """
    driver = _get_driver()
    if driver is None:
        return ""
    try:
        with driver.session() as session:
            keywords = _keywords(query)
            records = []
            if keywords:
                # Primary: keyword match within store exchanges only
                result = session.run(
                    """
                    MATCH (s:Store {name: 'MainStore'})-[:ASKED]->(m:StoreMessage)
                          -[:REPLIED_WITH]->(r:StoreReply)
                    WHERE any(k IN $keywords WHERE toLower(m.text) CONTAINS k)
                    RETURN m.text AS question, r.text AS answer, m.timestamp AS ts
                    ORDER BY m.timestamp DESC
                    LIMIT $limit
                    """,
                    keywords=keywords, limit=limit,
                )
                records = list(result)

            # Fallback: return most recent store exchanges when keyword search finds nothing
            if not records:
                result = session.run(
                    """
                    MATCH (s:Store {name: 'MainStore'})-[:ASKED]->(m:StoreMessage)
                          -[:REPLIED_WITH]->(r:StoreReply)
                    RETURN m.text AS question, r.text AS answer, m.timestamp AS ts
                    ORDER BY m.timestamp DESC
                    LIMIT $limit
                    """,
                    limit=limit,
                )
                records = list(result)

            lines = [f"Query: {rec['question']} — Response: {rec['answer']}"
                     for rec in records]
            return "\n".join(lines)
    except Exception as e:
        _neo4j_exc(e)
        print(f"[store memory] lookup skipped: {e}")
        return ""


# -----------------------------------------------------------------------
# Goal tracking (#4) — :Goal nodes linked to the Charlie :Person node.
# -----------------------------------------------------------------------

def _norm_goal(name: str) -> str:
    """Normalize a goal name for deduplication: lowercase and strip punctuation.

    "Learn Python!" and "learn python" should not create two separate Goal nodes.
    """
    import re as _re
    return _re.sub(r"[^\w\s]", "", name.lower()).strip()


def save_goal(name: str, description: str = "") -> None:
    """Save or update a goal; silently deduplicates on normalized name.

    Two-step write: a fuzzy pre-check catches near-duplicates (substring overlap),
    then MERGE+ON CREATE/ON MATCH upserts the node — creates it fresh or refreshes
    status and last_mentioned if it already exists under the exact name.
    """
    driver = _get_driver()
    if driver is None:
        return
    name_norm = _norm_goal(name)
    if not name_norm:
        return
    try:
        with driver.session() as session:
            # Fuzzy dedup: match if stored name contains the normalized new name, or vice versa
            existing = session.run(
                """
                MATCH (c:Person {name: $owner})-[:HAS_GOAL]->(g:Goal)
                WHERE toLower(replace(replace(g.name, '.', ''), ',', '')) CONTAINS $norm
                   OR $norm CONTAINS toLower(replace(replace(g.name, '.', ''), ',', ''))
                RETURN g.name LIMIT 1
                """,
                owner=OWNER_NAME, norm=name_norm,
            )
            if existing.single():
                # Already tracked — bump last_mentioned so it doesn't trigger a check-in prompt
                session.run(
                    "MATCH (g:Goal) WHERE toLower(g.name) CONTAINS $norm "
                    "SET g.last_mentioned = $ts",
                    norm=name_norm, ts=datetime.now().isoformat(),
                )
                return
            # MERGE upserts the Goal node: ON CREATE initializes fields, ON MATCH re-activates it
            session.run(
                """
                MERGE (c:Person {name: $owner})
                MERGE (g:Goal {name: $name})
                ON CREATE SET g.description = $description,
                              g.created = $ts,
                              g.status = 'active',
                              g.last_mentioned = $ts
                ON MATCH  SET g.status = 'active',
                              g.last_mentioned = $ts
                MERGE (c)-[:HAS_GOAL]->(g)
                """,
                owner=OWNER_NAME, name=name, description=description,
                ts=datetime.now().isoformat(),
            )
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] goal save skipped: {e}")


def get_goals(active_only: bool = True) -> list:
    """Return goal dicts for the owner. Each dict has: name, description, created.

    active_only=True (default) omits completed goals; pass False to get full history.
    """
    driver = _get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as session:
            if active_only:
                result = session.run(
                    """
                    MATCH (c:Person {name: $owner})-[:HAS_GOAL]->(g:Goal)
                    WHERE g.status = 'active'
                    RETURN g.name AS name, g.description AS description,
                           g.created AS created, g.last_mentioned AS last_mentioned
                    ORDER BY g.created DESC
                    """,
                    owner=OWNER_NAME,
                )
            else:
                result = session.run(
                    """
                    MATCH (c:Person {name: $owner})-[:HAS_GOAL]->(g:Goal)
                    RETURN g.name AS name, g.description AS description,
                           g.created AS created, g.status AS status
                    ORDER BY g.created DESC
                    """,
                    owner=OWNER_NAME,
                )
            return [dict(r) for r in result]
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] goal lookup skipped: {e}")
        return []


def complete_goal(name: str) -> bool:
    """Mark a goal as completed (case-insensitive partial match). Returns True if found."""
    driver = _get_driver()
    if driver is None:
        return False
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (c:Person {name: $owner})-[:HAS_GOAL]->(g:Goal)
                WHERE toLower(g.name) CONTAINS toLower($name) AND g.status = 'active'
                SET g.status = 'completed', g.completed_at = $ts
                RETURN g.name AS name
                """,
                owner=OWNER_NAME, name=name, ts=datetime.now().isoformat(),
            )
            return bool(list(result))
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] goal complete skipped: {e}")
        return False


def goals_needing_checkin(days: int = 3) -> list:
    """Return active goals that haven't been mentioned in the last `days` days.

    A goal "needs check-in" when last_mentioned is older than the cutoff (or was
    never set). Ted uses this list to proactively ask how things are going rather
    than waiting for the user to bring it up.
    """
    driver = _get_driver()
    if driver is None:
        return []
    try:
        from datetime import timedelta
        cutoff_ts = (datetime.now() - timedelta(days=days)).isoformat()  # ISO string comparison works because timestamps are zero-padded
        with driver.session() as session:
            result = session.run(
                """
                MATCH (c:Person {name: $owner})-[:HAS_GOAL]->(g:Goal)
                WHERE g.status = 'active'
                  AND (g.last_mentioned IS NULL OR g.last_mentioned < $cutoff)
                RETURN g.name AS name, g.created AS created
                ORDER BY g.created ASC
                """,
                owner=OWNER_NAME, cutoff=cutoff_ts,
            )
            return [dict(r) for r in result]
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] goal checkin skipped: {e}")
        return []


# -----------------------------------------------------------------------
# Pattern tracking (#5) — :Pattern nodes record topic × hour-of-day counts.
# -----------------------------------------------------------------------

def log_pattern(topic: str, hour_of_day: int) -> None:
    """Record that a topic was raised at this hour of day."""
    driver = _get_driver()
    if driver is None:
        return
    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (p:Pattern {topic: $topic, hour: $hour})
                ON CREATE SET p.count = 1,
                              p.first_seen = $ts,
                              p.last_seen = $ts
                ON MATCH  SET p.count = p.count + 1,
                              p.last_seen = $ts
                """,
                topic=topic[:80], hour=hour_of_day,
                ts=datetime.now().isoformat(),
            )
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] pattern log skipped: {e}")


def get_frequent_patterns(min_count: int = 3) -> list:
    """Return patterns that have occurred at least min_count times, sorted by count."""
    driver = _get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (p:Pattern)
                WHERE p.count >= $min_count
                RETURN p.topic AS topic, p.hour AS hour, p.count AS count
                ORDER BY p.count DESC
                LIMIT 10
                """,
                min_count=min_count,
            )
            return [dict(r) for r in result]
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] pattern lookup skipped: {e}")
        return []


# -----------------------------------------------------------------------
# Session summary (#13) — :SessionSummary nodes for cross-session recall.
# -----------------------------------------------------------------------

def save_session_summary(summary_text: str) -> None:
    """Persist a SessionSummary node."""
    driver = _get_driver()
    if driver is None:
        return
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (s:SessionSummary {text: $text, created: $ts})
                """,
                text=summary_text, ts=datetime.now().isoformat(),
            )
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] session summary save skipped: {e}")


def get_last_session_summary(min_gap_hours: float = 4.0) -> str:
    """Return the text of the most recent SessionSummary that was written at
    least min_gap_hours ago, so it only fires after a real session gap.
    Returns '' if none found or Neo4j is offline.
    """
    driver = _get_driver()
    if driver is None:
        return ""
    try:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=min_gap_hours)).isoformat()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (s:SessionSummary)
                WHERE s.created <= $cutoff
                RETURN s.text AS text, s.created AS created
                ORDER BY s.created DESC
                LIMIT 1
                """,
                cutoff=cutoff,
            )
            records = list(result)
            return records[0]["text"] if records else ""
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] session summary lookup skipped: {e}")
        return ""


# -----------------------------------------------------------------------
# Habit tracking — :Habit + :HabitLog nodes for daily streak logging.
# -----------------------------------------------------------------------

def log_habit(name: str) -> bool:
    """Record a habit completion for today (idempotent — safe to call twice).
    Returns True if this is a new log today, False if already logged."""
    driver = _get_driver()
    if driver is None:
        return True
    from datetime import date as _date_cls
    today = _date_cls.today().isoformat()
    try:
        with driver.session() as session:
            result = session.run(
                """
                MERGE (p:Person {name: $owner})
                MERGE (h:Habit {name: $name})
                ON CREATE SET h.created = $ts
                MERGE (p)-[:HAS_HABIT]->(h)
                MERGE (l:HabitLog {habit: $name, date: $today})
                ON CREATE SET l.created = $ts
                RETURN l.created AS created, $ts AS now
                """,
                owner=OWNER_NAME, name=name.lower(), today=today,
                ts=datetime.now().isoformat(),
            )
            records = list(result)
            if not records:
                return True
            return records[0]["created"] == records[0]["now"]
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] log_habit skipped: {e}")
        return True


def get_habit_streak(name: str) -> dict:
    """Return current streak info for a habit.
    Returns {name, streak, last_logged} or None if habit not found."""
    driver = _get_driver()
    if driver is None:
        return None
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (l:HabitLog {habit: $name})
                RETURN l.date AS log_date
                ORDER BY l.date DESC
                LIMIT 100
                """,
                name=name.lower(),
            )
            records = list(result)

        if not records:
            return None

        from datetime import date as _d, timedelta
        today = _d.today()
        dates = sorted({_d.fromisoformat(r["log_date"]) for r in records}, reverse=True)
        last = dates[0]

        # Only count streak if last log was today or yesterday
        if last < today - timedelta(days=1):
            return {"name": name, "streak": 0, "last_logged": last.isoformat()}

        streak = 0
        expected = last
        for d in dates:
            if d == expected:
                streak += 1
                expected = d - timedelta(days=1)
            else:
                break

        return {"name": name, "streak": streak, "last_logged": last.isoformat()}
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] get_habit_streak skipped: {e}")
        return None


def get_all_habits() -> list:
    """Return all tracked habits with streak info."""
    driver = _get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (p:Person {name: $owner})-[:HAS_HABIT]->(h:Habit)
                RETURN h.name AS name
                ORDER BY h.name
                """,
                owner=OWNER_NAME,
            )
            names = [r["name"] for r in result]
        habits = []
        for name in names:
            info = get_habit_streak(name)
            habits.append(info if info else {"name": name, "streak": 0, "last_logged": None})
        return habits
    except Exception as e:
        _neo4j_exc(e)
        print(f"[memory] get_all_habits skipped: {e}")
        return []
