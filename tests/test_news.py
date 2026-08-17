"""Checks for standing news subscriptions.

The network is faked throughout. A test suite that hits Hacker News and
DuckDuckGo is slow, flaky, and tests their uptime rather than Ted's logic —
and the logic worth pinning is all local: deduplication, freshness, ranking,
and the read/unread boundary.
"""

import os
import sys
import tempfile

_SCRATCH = os.path.join(tempfile.mkdtemp(), "news_test.db")
os.environ["TED_DB"] = _SCRATCH

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

from core import news
from core import routing

news.DB_PATH = _SCRATCH


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def iso(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# Every fetch in this file is served from here.
FEED = {"hn": [], "web": []}
news._fetch_hn = lambda q, limit: list(FEED["hn"])
news._fetch_web = lambda q, limit: list(FEED["web"])


def item(title, url="", source="Hacker News", days=0, points=0):
    return {"title": title, "url": url, "source": source,
            "published": iso(days), "points": points}


print("— watching a subject —")
topic, error = news.add_topic("AI models", "new AI model release")
check("a topic can be added", topic and not error)
check("…with its query kept", topic["query"] == "new AI model release")
_, dup = news.add_topic("AI models")
check("the same topic twice is refused", "already watching" in dup)
_, blank = news.add_topic("")
check("a nameless topic is refused", "needs a name" in blank)
_, bad_src = news.add_topic("Bad source", "x", sources="carrier-pigeon")
check("an unknown source falls back to the defaults rather than failing",
      news.get_topic(_["id"])["sources"] == "hn,web" if _ else False)

print("\n— what counts as new —")
FEED["hn"] = [item("Qwen 4 released", "https://example.com/qwen4", points=310)]
FEED["web"] = [item("Qwen 4 is out", "https://other.com/qwen", source="The Verge")]
first = news.check_topic(topic)
check("a first check finds both stories", len(first) == 2)

second = news.check_topic(topic)
check("checking again finds nothing new", second == [])

# The same story reaches both sources under different headlines, and the same
# link arrives decorated with tracking parameters.
FEED["hn"] = [item("Qwen 4 released",
                   "https://example.com/qwen4?utm_source=newsletter&ref=hn",
                   points=310)]
FEED["web"] = []
check("the same URL with tracking parameters is not a new story",
      news.check_topic(topic) == [])
check("…and a trailing slash is not either",
      (FEED.__setitem__("hn", [item("Qwen 4 released",
                                    "https://www.example.com/qwen4/")]) or
       news.check_topic(topic)) == [])

FEED["hn"] = [item("Ancient news", "https://example.com/old", days=30)]
FEED["web"] = []
check("a month-old story is not 'new'", news.check_topic(topic) == [])

FEED["hn"] = [item("Undated but real", "https://example.com/undated")]
FEED["hn"][0]["published"] = ""
check("an undated story is kept rather than silently dropped",
      len(news.check_topic(topic)) == 1)

print("\n— one dead source does not silence the other —")


def _boom(q, limit):
    raise RuntimeError("Algolia is down")


_real_hn = news._fetch_hn
news._fetch_hn = _boom
FEED["web"] = [item("Web still works", "https://example.com/web-only",
                    source="Reuters")]
survived = news.check_topic(topic)
news._fetch_hn = _real_hn
check("the working source still returns its story", len(survived) == 1)
check("…and the failure is recorded against the topic",
      "Algolia is down" in news.get_topic(topic["id"])["last_error"])

print("\n— reading —")
news.mark_seen()
check("everything can be marked read", news.unseen_count() == 0)
FEED["hn"] = [item("Small story", "https://example.com/small", points=2),
              item("Huge story", "https://example.com/huge", points=980)]
FEED["web"] = []
news.check_topic(topic)
items = news.unseen(10)
check("unread stories come back", len(items) == 2)
# A chronological digest buries the only item that mattered.
check("the story that mattered is ranked first",
      items[0]["title"] == "Huge story")

digest = news.format_digest(items)
check("the digest names the topic", "AI models:" in digest)
check("…the points", "980 pts" in digest)
check("…and the link", "https://example.com/huge" in digest)
check("reading the digest marks them read", news.unseen_count() == 0)
check("an empty digest is empty, not a sentence about nothing",
      news.format_digest([]) == "")

print("\n— unwatching —")
removed, error = news.remove_topic("AI models")
check("a topic can be removed by name", removed == "AI models" and not error)
check("…and its stored stories go with it", news.unseen_count() == 0)
_, missing = news.remove_topic("something never watched")
check("removing an unwatched topic says so", "not watching" in missing)


def names_for(text):
    return {routing.tool_name(s) for s in routing.select_tool_schemas(text)}


print("\n— when the news tools are offered —")
for phrase in ("watch AI model releases for me", "keep me posted on rust news",
               "what's new", "anything new", "stop watching AI models",
               "what are you monitoring"):
    check(f"{phrase!r} loads them", any(n.startswith("news_")
                                        for n in names_for(phrase)))
# Asking once is not subscribing; that is web_search's job.
for phrase in ("search the news for the election result",
               "what happened in the game last night", "play some music"):
    check(f"{phrase!r} does not", not any(n.startswith("news_")
                                          for n in names_for(phrase)))

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
