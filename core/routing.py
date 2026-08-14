"""Cheap routing decisions that reduce prompt weight without replacing reasoning.

This module does two deliberately different jobs:

* ``plan_reflex`` recognizes only complete, reversible app open/close requests.
  If even one target is unclear it returns ``None`` and the model gets the turn.
* ``select_tool_schemas`` retrieves a small capability menu for natural requests.
  A ``find_tools`` meta-tool is always present, so a novel phrasing can expand the
  menu during the reasoning loop instead of being locked out by this router.

The router chooses *capabilities*, never the final action for ambiguous requests.
That is the line between a fast reflex and the old regex ladder that tried to be
the assistant.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from core.actions import APPS
from core.tools import TOOL_SCHEMAS


FIND_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_tools",
        "description": (
            "Load additional Ted capabilities when the tools currently shown do not "
            "cover the user's request. Describe the missing capability briefly; after "
            "the result, call one of the newly available tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Missing capability, e.g. send a message or control an app",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in TOOL_SCHEMAS}

# These are capability-family hints, not command parsers. False positives cost a
# few schema tokens; false negatives are recovered by find_tools.
_FAMILIES = (
    (r"\b(?:open|close|quit|launch|start|bring up|pull up|app|application|window)\b",
     ("open_app", "close_app")),
    (r"\b(?:website|browser|browse|navigate|url|\.com\b|youtube|reddit|github|google)\b",
     ("browse_to", "open_app")),
    (r"\b(?:song|music|spotify|playlist|album|artist|pause|resume|skip|previous track)\b",
     ("play_music", "play_playlist", "spotify_control")),
    (r"\b(?:message|text|imessage|send .*? to|tell .*? that)\b",
     ("send_message",)),
    (r"\b(?:remind|reminder|timer|alarm|clock)\b",
     ("set_reminder", "set_timer", "get_reminders", "toggle_clock")),
    (r"\b(?:weather|forecast|temperature|rain|snow)\b", ("get_weather",)),
    (r"\b(?:email|mail|inbox|subject|reply|flag|unread)\b",
     ("get_emails", "read_email", "email_action", "send_email")),
    (r"\b(?:calendar|event|meeting|appointment|schedule)\b",
     ("calendar_get", "calendar_add")),
    (r"\b(?:note|notes|write down|jot down)\b", ("notes_add", "notes_get")),
    (r"\b(?:clipboard|copy|paste)\b", ("clipboard_read", "clipboard_write")),
    (r"\b(?:volume|brightness|screen|display|type|keyboard|cursor)\b",
     ("system_volume", "system_brightness", "screen_describe", "type_text")),
    (r"\b(?:habit|streak|workout|worked out|exercise)\b",
     ("log_habit", "get_habit_streak")),
    (r"\b(?:calculate|math|percent|plus|minus|times|divided|\d\s*[-+*/]\s*\d)\b",
     ("calculate",)),
    (r"\b(?:search the web|look up|current|latest|news|score|price|verify online)\b",
     ("web_search",)),
    (r"\b(?:remember|knowledge|document|documents|file|files|what do you know)\b",
     ("search_knowledge", "add_knowledge")),
)

# Verbs that can only mean "do something to my Mac or my accounts". Anything a
# person also says to a chatbot in ordinary conversation is deliberately absent.
#
# The earlier list included write, create, check, show, find, read, tell, search
# and remove. Every one of those opens a normal request — "write me a poem",
# "check my code for bugs", "tell me what you think" — and classifying them as
# actions suppressed the prose answer and forced a tool call that did not exist.
# A verb missing from this list is not a failure: the turn simply goes to the
# model with tool_choice="auto", which is the behaviour that already worked.
_ACTION_WORDS = re.compile(
    r"^(?:open|close|quit|launch|relaunch|reopen|browse|navigate|"
    r"play|pause|resume|skip|mute|unmute|"
    r"imessage|paste|screenshot)\b",
    re.I,
)

# The remaining verbs are actions only when they carry an unmistakable target:
# a person to message, a device surface to change, or an explicit destination.
_TARGETED_ACTIONS = (
    (re.compile(r"^(?:text|message|send|email)\b", re.I),
     re.compile(r"\b(?:to|for)\s+\w|^(?:text|message|email)\s+\w+\s+\w", re.I)),
    (re.compile(r"^(?:set|add|log)\b", re.I),
     re.compile(r"\b(?:reminder|timer|alarm|calendar|event|meeting|appointment|"
                r"note|notes|habit|streak|volume|brightness|workout|exercise|"
                r"gym|run|lift)\b", re.I)),
    (re.compile(r"^(?:copy|type)\b", re.I),
     re.compile(r"\b(?:clipboard|to my clipboard|into|out loud)\b", re.I)),
)


def tool_name(schema):
    return (schema.get("function") or {}).get("name", "")


def _family_names(text):
    names = []
    lowered = (text or "").lower()
    for pattern, family in _FAMILIES:
        if re.search(pattern, lowered, re.I):
            names.extend(family)
    return names


def select_tool_schemas(text, recent_action_text=""):
    """Return a small initial tool menu plus the discovery escape hatch."""
    names = _family_names(text)
    # Pronouns such as "close it" benefit from the last structured action, but
    # only use it to choose a family; the model still resolves the actual target.
    if re.search(r"\b(?:it|that|those|them|again|same)\b", text or "", re.I):
        names.extend(_family_names(recent_action_text))
    chosen = []
    seen = set()
    for name in names:
        if name in _SCHEMA_BY_NAME and name not in seen:
            chosen.append(_SCHEMA_BY_NAME[name])
            seen.add(name)
    # Discovery first makes its purpose visible even when the initial list is empty.
    return [FIND_TOOLS_SCHEMA, *chosen]


def discover_tool_schemas(query, exclude=(), limit=8):
    """Retrieve schemas for a capability query; used by the find_tools meta-tool."""
    excluded = set(exclude)
    family = _family_names(query)
    ranked = []
    query_words = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    for schema in TOOL_SCHEMAS:
        name = tool_name(schema)
        if name in excluded:
            continue
        fn = schema["function"]
        haystack = (name.replace("_", " ") + " " + fn.get("description", "")).lower()
        words = set(re.findall(r"[a-z0-9]+", haystack))
        score = len(query_words & words)
        if name in family:
            score += 20 - family.index(name)
        if score:
            ranked.append((score, name, schema))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        # Returning nothing tells the model to ask one useful question rather than
        # silently loading all 32 contracts and recreating the original problem.
        return []
    return [schema for _score, _name, schema in ranked[:limit]]


def likely_action_request(text):
    """Signal an unmistakable action request without classifying discussion as one."""
    t = (text or "").strip()
    # Questions *about* doing something are conversation. "Can you open Notes"
    # is deliberately not excluded; "how do I open Notes" is.
    if re.search(r"\b(?:how (?:do|can|should|would) i|how to|why (?:does|did|cant|can't)|"
                 r"explain how|what happens (?:if|when)|is it possible to)\b", t, re.I):
        return False
    # Peel only explicit request wrappers. A verb appearing later in discussion
    # ("I wonder whether I should remove it") must not force a tool call.
    t = re.sub(r"^(?:(?:hey|okay|ok)\s+ted[,.]?\s*|please\s+)+", "", t,
               flags=re.I)
    t = re.sub(
        r"^(?:(?:can|could|would|will)\s+you|i\s+(?:want|need|would like)\s+you\s+to)\s+",
        "", t, flags=re.I)
    if _ACTION_WORDS.match(t):
        return True
    return any(verb.match(t) and target.search(t)
               for verb, target in _TARGETED_ACTIONS)


_SEQUENCE_SEP = re.compile(
    r"\b(?:and then|then|after that|afterwards|followed by|once that)\b|"
    r"\band\s+(?=(?:open|close|quit|launch|play|pause|send|message|text|email|"
    r"set|add|create|write|copy|paste|type|delete|flag|mark|change|turn|search|"
    r"find|look up|show|hide|read|check|log|calculate|browse|navigate|remove|tell)\b)",
    re.I,
)


def expected_action_calls(text):
    """Conservative lower bound for whether a requested outcome is complete.

    This does not pick tools. It only prevents the agent loop from stopping after
    one success when the user plainly named two targets or two stages.
    """
    if not likely_action_request(text):
        return 0
    segments = [part.strip(" ,") for part in _SEQUENCE_SEP.split(text or "")
                if part.strip(" ,")]
    total = 0
    previous_targets = 1
    for segment in segments or [text or ""]:
        lower = segment.lower()
        app_match = re.search(
            r"\b(?:open|close|quit|launch|start|run|pull up|bring up|exit|kill|shut)\s+(.+)",
            lower,
        )
        if app_match:
            rest = app_match.group(1)
            if re.search(r"\b(?:both|two apps|two applications)\b", rest):
                count = max(2, previous_targets)
            else:
                targets = [p for p in re.split(
                    r"\s+and\s+(?:(?:open|close|quit|launch)\s+)?|,", rest)
                           if p.strip()]
                count = max(1, len(targets))
            previous_targets = count
            total += count
            continue
        # A non-app stage (copy then read, search then write, etc.) represents at
        # least one tool action. The model still decides which tool it is.
        total += 1
    return max(1, total)


def memory_scope_for(text, schemas):
    """Choose how much personal context this turn earns.

    ``none`` is for operational actions, ``relevant`` for ordinary conversation,
    and ``full`` for explicit recall/profile requests.
    """
    t = (text or "").lower()
    explicit = re.search(
        r"\b(?:remember|recall|last time|earlier|previous conversation|what do you "
        r"know about me|who am i|my preference|continue where|pick "
        r"back up|what was i)\b", t,
    )
    if explicit:
        return "full"
    if likely_action_request(text):
        return "none"
    return "relevant"


@dataclass(frozen=True)
class ReflexPlan:
    calls: tuple[tuple[str, dict], ...]


_POLITE_PREFIX = re.compile(
    r"^(?:(?:hey|okay|ok)\s+ted[,.]?\s*|(?:hey|okay|ok)[,.]?\s*|"
    r"(?:can|could|would|will)\s+you\s+|please\s+|just\s+)+",
    re.I,
)
_OPEN_RE = re.compile(r"^(?:open|launch|start|run|pull up|bring up|open up)\s+(.+)$", re.I)
_CLOSE_RE = re.compile(r"^(?:close|quit|exit|kill|shut)\s+(.+)$", re.I)
_DEPENDENCY_WORDS = re.compile(r"\b(?:then|after|before|if|unless|while)\b", re.I)


def _resolve_known_app(raw):
    target = re.sub(r"^(?:the|my)\s+", "", raw.strip().lower())
    if target in APPS:
        return target
    fuzzy = difflib.get_close_matches(target, list(APPS), n=1, cutoff=0.86)
    return fuzzy[0] if fuzzy else None


def plan_reflex(text):
    """Plan an exact reversible app request, or decline the entire turn.

    All targets must resolve to installed-app aliases. Websites, contextual
    pronouns and chained work deliberately go to the reasoning path.
    """
    cleaned = _POLITE_PREFIX.sub("", (text or "").strip().rstrip(".!?"))
    cleaned = re.sub(r"[, ]+(?:please|thanks|thank you)$", "", cleaned, flags=re.I)
    if not cleaned or _DEPENDENCY_WORDS.search(cleaned):
        return None
    match = _OPEN_RE.match(cleaned)
    tool = "open_app"
    if not match:
        match = _CLOSE_RE.match(cleaned)
        tool = "close_app"
    if not match:
        return None
    raw_targets = [p.strip() for p in re.split(r"\s+and\s+|,", match.group(1), flags=re.I)
                   if p.strip()]
    if not raw_targets:
        return None
    resolved = [_resolve_known_app(target) for target in raw_targets]
    if any(not target for target in resolved):
        return None
    # Avoid issuing the same app twice through aliases such as "Google and Chrome".
    unique = []
    bundles = set()
    for target in resolved:
        bundle = APPS[target]
        if bundle in bundles:
            continue
        bundles.add(bundle)
        unique.append((tool, {"name": target}))
    return ReflexPlan(tuple(unique)) if unique else None


def operational_context(actions, limit=4):
    """Compact structured recent-action context for pronouns and corrections."""
    parts = []
    for item in list(actions or [])[-limit:]:
        name = item.get("tool", "")
        args = item.get("args") or {}
        result = item.get("result", "")
        parts.append(f"{name}({args}) -> {result}")
    return "Recent verified actions: " + " | ".join(parts) if parts else ""
