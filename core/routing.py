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
    (r"\b(?:website|browser|browse|navigate|url|\.com\b|youtube|video|watch|reddit|github|google)\b",
     ("play_youtube", "browse_to", "open_app")),
    # "play" itself was missing here, so "play a different one" arrived with an
    # empty menu, burned a find_tools round trip, hit the rate limit and fell
    # through to the local brain — 7.8 seconds to change a song.
    # "listen" without the suffix never matched "what am I listening to", which
    # is how the question is actually asked.
    (r"\b(?:play(?:ing|ed|s)?|song|music|spotify|playlist|album|artist|track|"
     r"listen(?:ing)?|pause|resume|skip|next song|previous track|volume)\b",
     ("play_music", "play_playlist", "spotify_control", "now_playing")),
    # "what am I watching" is the browser half of the same question and shares
    # no words with the music family above.
    (r"\bwhat(?:'?s| is| am i)\b[^.?!]*\b(?:playing|watching|open|on screen|"
     r"this (?:song|video|tab))\b",
     ("now_playing",)),
    # Playlist EDITING is its own family on purpose. Folded into the music
    # family above it would put four more schemas on every "skip this song",
    # and that family already fires on the bare word "play".
    (r"\b(?:add|remove|delete|create|make|save)\b[^.?!]*\bplaylist\b|"
     r"\bplaylist\b[^.?!]*\b(?:add|remove|delete|create|make|save)\b|"
     r"\b(?:add|save|remove|delete)\s+(?:this|that|it|the current)\b",
     ("add_to_playlist", "remove_from_playlist", "create_playlist",
      "delete_playlist", "play_playlist")),
    (r"\b(?:message|text|imessage|send .*? to|tell .*? that)\b",
     ("send_message",)),
    (r"\b(?:remind|reminder|timer|alarm|clock)\b",
     ("set_reminder", "set_timer", "get_reminders")),
    (r"\b(?:weather|forecast|temperature|rain|snow)\b", ("get_weather",)),
    (r"\b(?:email|mail|inbox|subject|reply|flag|unread)\b",
     ("get_emails", "read_email", "email_action", "send_email")),
    (r"\b(?:calendar|event|meeting|appointment|schedule)\b",
     ("calendar_get", "calendar_add")),
    (r"\b(?:note|notes|write down|jot down)\b", ("notes_add", "notes_get")),
    (r"\b(?:clipboard|copy|paste)\b", ("clipboard_read", "clipboard_write")),
    (r"\b(?:volume|brightness|screen|display|type|keyboard|cursor|click|tap|"
     r"press|button|link|video|scroll|field|control)\b",
     ("system_volume", "system_brightness", "screen_describe", "ui_inspect",
      "ui_press", "ui_fill", "type_text", "create_document", "press_key", "scroll")),
    (r"\b(?:habit|streak|workout|worked out|exercise)\b",
     ("log_habit", "get_habit_streak")),
    (r"\b(?:calculate|math|percent|plus|minus|times|divided|\d\s*[-+*/]\s*\d)\b",
     ("calculate",)),
    (r"\b(?:search the web|look up|current|latest|news|score|price|verify online)\b",
     ("web_search",)),
    (r"\b(?:remember|knowledge|docs?|documents?|google docs?|textedit|file|files|what do you know)\b",
     ("create_document", "search_knowledge", "add_knowledge")),
    # Finding a past MESSAGE is a different job from recalling a fact, and the
    # phrasings barely overlap — "what did I say about…" names neither.
    (r"\b(?:what did (?:i|we|you) say|did i (?:ever )?(?:say|mention|tell)|"
     r"when did i (?:say|mention)|find the (?:chat|conversation)|"
     r"search (?:my |our )?(?:chats?|conversations?|history)|"
     r"earlier (?:chat|conversation)|previous conversation)\b",
     ("search_chats",)),
    (r"\b(?:means|meaning|when i say|my lingo|shorthand|slang)\b",
     ("learn_lingo", "clarify_lingo")),
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
    r"imessage|paste|screenshot|click|tap|press|scroll|type)\b",
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
    (re.compile(r"^(?:create|start|make|write|draft)\b", re.I),
     re.compile(r"\b(?:new|blank)\b.{0,20}\b(?:document|google doc|textedit)\b", re.I)),
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
    # "another", "a different one" and "next" are continuations too — they point
    # at the last action just as hard as "it" does, and leaving them out is what
    # sent "play a different one" to the model with no music tools loaded.
    if re.search(r"\b(?:it|its|it's|that|those|them|again|same|one|ones|"
                 r"another|different|next|else|instead)\b", text or "", re.I):
        names.extend(_family_names(recent_action_text))
    chosen = []
    seen = set()
    for name in names:
        if name in _SCHEMA_BY_NAME and name not in seen:
            chosen.append(_SCHEMA_BY_NAME[name])
            seen.add(name)
    # Discovery first makes its purpose visible even when the initial list is empty.
    return [FIND_TOOLS_SCHEMA, *chosen]


def plan_document(text):
    """Parse an explicit create-and-write request into a compact workflow spec."""
    raw = " ".join(str(text or "").strip().split())
    lower = raw.lower()
    has_document = re.search(r"\b(?:docs?|documents?|google docs?|textedit)\b", lower)
    has_write = re.search(r"\b(?:write|draft|type|compose|paper|essay|report|paragraph)\b", lower)
    has_create = re.search(
        r"^(?:(?:hey|okay|ok|alright|alight|please|ted|um|uh)[, ]+)*"
        r"(?:(?:can|could|would|will)\s+you\s+|"
        r"i\s+(?:want|need|would like)\s+you\s+to\s+)?"
        r"(?:open|create|start|make|launch)\b", lower)
    if not (has_document and has_write and has_create):
        return None
    pages_match = re.search(r"\b(\d{1,2})\s*pages?\b", lower)
    pages = max(1, min(20, int(pages_match.group(1)))) if pages_match else 0
    spacing = None
    if re.search(r"\bdouble[ -]?spaced?\b", lower):
        spacing = "double"
    elif re.search(r"\b1[.]5[ -]?spaced?\b", lower):
        spacing = "1.5"
    elif re.search(r"\bsingle[ -]?spaced?\b", lower):
        spacing = "single"
    font_match = re.search(
        r"\b(1[0-8]|[8-9])(?:\s*[- ]?\s*)(?:pt|point|font(?: size)?)\b", lower)
    if not font_match:
        font_match = re.search(r"\bfont(?: size)?\s*(1[0-8]|[8-9])\b", lower)
    font_size = int(font_match.group(1)) if font_match else None
    if pages:
        target_words = pages * (300 if spacing == "double" else 500)
    elif "paragraph" in lower:
        target_words = 150
    else:
        target_words = 600
    browser = next((name for name in ("Chrome", "Safari", "Brave")
                    if name.lower() in lower), None)
    return {
        "instructions": raw,
        "target_words": target_words,
        "font_size": font_size,
        "line_spacing": spacing,
        "app": "textedit" if "textedit" in lower else "google_docs",
        "browser": browser or "Chrome",
    }


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
    t = re.sub(r"^(?:(?:hey|okay|ok|alright|alight|um|uh|well|so)\s+ted[,.]?\s*|"
               r"(?:hey|okay|ok|alright|alight|um|uh|well|so|please)[,.]?\s*|"
               r"(?:let'?s|let us)\s+)+", "", t,
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
    # create_document owns the whole open → focus → type outcome, so do not
    # force the model to perform a redundant second action for that phrase.
    lower_text = (text or "").lower()
    if (re.search(r"\b(?:new|blank)\b.{0,20}\b(?:document|google doc|textedit)\b", lower_text)
            and re.search(r"\b(?:type|write|start|draft|paragraph)\b", lower_text)):
        return 1
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

    ``none`` is for operational actions, ``light`` for greetings, ``relevant``
    for ordinary conversation, and ``full`` for explicit recall/profile requests.
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
    # Greetings need personality, facts and recent in-session history, but a
    # vector search over documents and old exchanges adds latency and often
    # injects the nearest *unrelated* chunk. Keep that retrieval for actual
    # topics, where it has something to search for.
    greeting = re.fullmatch(
        r"\s*(?:(?:hey|hi|hello|yo|okay|ok|alright|alight|um|uh|so|well)\s+)*"
        r"(?:ted[,.]?\s+)?(?:hey|hi|hello|yo|how are you|how(?:'s| is) it going|"
        r"what(?:'s| is) up|what(?:'s| is) going on|good morning|good afternoon|"
        r"good evening)[.!?\s]*",
        text or "", re.I)
    if greeting:
        return "light"
    return "relevant"


# ── which brain answers this turn ─────────────────────────────────────────
#
# Ted already had two tiers: a zero-model reflex for fully resolved app actions,
# and one streamed cloud turn for everything else. Everything that was not a
# reflex therefore spent cloud tokens, including "thanks" and "what time is it".
#
# This adds the missing middle. The rules below are pure and instant; only a
# genuinely ambiguous turn pays for a tiebreak, and that tiebreak is a ~0.2s
# call to a small local model, which is what Charlie asked for — the local brain
# deciding what the cloud brain has to be woken up for.
#
# The bias is deliberate and asymmetric. Sending thinking work to the local model
# costs answer quality, which is the product; sending a trivial turn to the cloud
# costs tokens, which are replenished every minute. So anything uncertain goes to
# the cloud, and only demonstrably simple turns stay local.

BRAIN_LOCAL = "local"
BRAIN_CLOUD = "cloud"


@dataclass(frozen=True)
class BrainChoice:
    """Which brain should answer, and the honest reason why."""
    brain: str
    reason: str
    decided_by: str            # "rule" | "model" | "pin"

    @property
    def is_local(self):
        return self.brain == BRAIN_LOCAL


# Work the 9B local chat model measurably does worse: anything where being
# wrong is expensive or where the answer is longer than a couple of sentences.
_THINKING_WORK = re.compile(
    r"```|\b(?:code|function|class|regex|algorithm|bug|traceback|stack trace|"
    r"refactor|compile|debug|syntax|api|schema|query|sql|python|javascript|"
    r"swift|essay|paper|outline|draft|write (?:me|a|an)|rewrite|summar(?:ize|y)|"
    r"translate|analy[sz]e|compare|contrast|pros and cons|explain|why (?:does|do|is|are|did)|"
    r"how (?:does|do|did|would|should)|walk me through|step by step|plan|design|"
    r"brainstorm|recommend|advice|should i|help me (?:with|figure)|figure out|"
    r"what if|troubleshoot|fix)\b",
    re.I,
)

# Turns that are their own answer. Short, closed, and socially fixed.
_SMALL_TALK = re.compile(
    r"^\s*(?:(?:hey|hi|hello|yo|sup|okay|ok|alright|alight|cool|nice|great|"
    r"thanks|thank you|ty|thx|yes|yeah|yep|no|nope|nah|sure|got it|gotcha|"
    r"never ?mind|nvm|please|sorry|goodnight|good night|bye|see ya|later)"
    r"[\s,.!?]*)+$",
    re.I,
)

# Questions answered from Ted's own live state or a clock, not from knowledge.
_LOCAL_LOOKUP = re.compile(
    r"^\s*(?:what(?:'s| is| are)?\s+)?(?:the\s+)?(?:"
    r"time|date|day (?:is it|of the week)|today'?s date|"
    r"playing|song is (?:this|playing)|(?:apps?|windows?|tabs?) (?:are )?open|"
    r"battery|volume)\b",
    re.I,
)


def classify_brain(text, schemas=(), has_attachment=False, pinned=""):
    """Decide which brain answers, without calling anything.

    Returns a :class:`BrainChoice`. ``brain`` is advisory: the provider layer
    still falls back to the other side if the chosen one is unavailable, so a
    wrong guess here costs latency, never an answer.

    ``decided_by == "model"`` is never produced by this function — it is
    reserved for :func:`classify_brain_with_model`, which consults the small
    local router only for the turns this one declines to settle.
    """
    if pinned in (BRAIN_LOCAL, BRAIN_CLOUD):
        return BrainChoice(pinned, f"pinned to the {pinned} brain", "pin")

    body = (text or "").strip()
    if not body:
        return BrainChoice(BRAIN_LOCAL, "empty message", "rule")

    # An image needs the multimodal path, and the tool menu needs the 35B local
    # model whose whole purpose is being the rescue brain — neither is a saving.
    if has_attachment:
        return BrainChoice(BRAIN_CLOUD, "an attachment needs the vision model", "rule")
    if schemas:
        return BrainChoice(BRAIN_CLOUD, "tool use needs the stronger brain", "rule")

    if _SMALL_TALK.match(body):
        return BrainChoice(BRAIN_LOCAL, "small talk", "rule")
    if _LOCAL_LOOKUP.match(body):
        return BrainChoice(BRAIN_LOCAL, "answered from live state, not knowledge", "rule")
    if _THINKING_WORK.search(body):
        return BrainChoice(BRAIN_CLOUD, "reasoning or long-form work", "rule")

    words = body.split()
    if len(words) > 30:
        return BrainChoice(BRAIN_CLOUD, "a long request", "rule")
    if len([p for p in _SEQUENCE_SEP.split(body) if p.strip(" ,")]) > 1:
        return BrainChoice(BRAIN_CLOUD, "more than one stage", "rule")
    if len(words) <= 4:
        return BrainChoice(BRAIN_LOCAL, "a very short turn", "rule")

    return BrainChoice(BRAIN_CLOUD, "not obviously simple", "rule")


# A 3B model asked "is this simple?" about "what's the capital of Iowa" will
# happily reply "Des Moines". The message therefore has to arrive as quoted
# data with the instruction on both sides of it, never as a bare question.
_ROUTER_SYSTEM = (
    "You label messages. You never answer them.\n"
    "Reply with exactly one word: LOCAL or CLOUD.\n"
    "LOCAL — a small model can handle it: greetings, chatter, acknowledgements, "
    "one-sentence answers.\n"
    "CLOUD — it needs reasoning, code, planning, accuracy, or a long answer.\n"
    "If the message is a question, you are labelling the question, not "
    "answering it. When unsure, reply CLOUD."
)

_ROUTER_USER = (
    "Label the message between the markers. Do not answer it.\n"
    "--- BEGIN MESSAGE ---\n{text}\n--- END MESSAGE ---\n"
    "One word, LOCAL or CLOUD:"
)

_VERDICT = re.compile(r"\b(LOCAL|CLOUD)\b")


def classify_brain_with_model(text, schemas=(), has_attachment=False, pinned="",
                              ask=None):
    """Same decision, but let the small local model settle the unclear turns.

    ``ask`` is injected so this stays unit-testable and so the provider import
    stays lazy. It receives the user's text and returns the model's raw reply.
    Any failure, timeout or unrecognised answer keeps the rule-based choice —
    the router is an optimisation and is never allowed to be a point of failure.
    """
    choice = classify_brain(text, schemas, has_attachment, pinned)
    # Only "not obviously simple" is a genuine shrug. Every other reason above
    # is a positive finding and does not deserve a second opinion.
    if choice.reason != "not obviously simple":
        return choice
    if ask is None:
        from core.providers import route_hint
        ask = route_hint
    try:
        raw = ask(_ROUTER_SYSTEM, _ROUTER_USER.format(text=(text or "")[:500])) or ""
    except Exception as exc:
        print(f"[router] local tiebreak unavailable: {exc}")
        return choice
    # A small model that ignored the instruction and answered the question
    # produces no verdict token at all, which is exactly the signal to keep the
    # rule-based choice rather than to read meaning into prose.
    found = _VERDICT.search(raw.upper())
    if not found:
        print(f"[router] no verdict in {raw[:60]!r} — keeping the rule")
        return choice
    if found.group(1) == "LOCAL":
        return BrainChoice(BRAIN_LOCAL, "the local router took it", "model")
    return BrainChoice(BRAIN_CLOUD, "the local router escalated it", "model")


@dataclass(frozen=True)
class ReflexPlan:
    calls: tuple[tuple[str, dict], ...]


_POLITE_PREFIX = re.compile(
    r"^(?:(?:hey|okay|ok|alright|alight|um|uh|well|so)\s+ted[,.]?\s*|"
    r"(?:hey|okay|ok|alright|alight|um|uh|well|so)[,.]?\s*|"
    r"(?:can|could|would|will)\s+you\s+|(?:let'?s|let us)\s+|"
    r"please\s+|just\s+)+",
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
    youtube = re.match(
        r"^(?:open\s+)?youtube(?:\s+and)?\s+(?:play|start|watch)\s+"
        r"(?:me\s+)?(?:(?:a|an|any|some|the)\s+)?(.*?)\s*video$",
        cleaned, re.I)
    if youtube:
        query = youtube.group(1).strip()
        if query.lower() in {"", "random", "popular", "youtube"}:
            query = ""
        return ReflexPlan((("play_youtube", {"query": query}),))
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
