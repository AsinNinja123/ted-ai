"""Cheap routing decisions that reduce prompt weight without replacing reasoning.

This module does two deliberately different jobs:

* ``plan_reflex`` recognizes only complete, reversible app open/close requests.
  If even one target is unclear it returns ``None`` and the model gets the turn.
* ``select_tool_schemas`` retrieves a small capability menu for natural requests.
  ``find_tools`` is reserved for unmistakable actions whose capability family
  could not be identified locally; ordinary conversation carries no tools.

The router chooses *capabilities*, never the final action for ambiguous requests.
That is the line between a fast reflex and the old regex ladder that tried to be
the assistant.
"""


# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 7 (§7.1 – §7.7)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The bouncer for the prompt. Its job is to keep messages cheap.
#
#      Ted has dozens of tools. Every tool's description ("schema") is real text
#      that gets sent to the model with every single message, and the free tier
#      counts those characters against a per-minute ceiling. Sending the whole
#      catalogue on "how are you" is how you run out of budget saying hello.
#
#      So this file makes three cheap guesses before the model is ever asked:
#          1. Which small handful of tools might this message need?
#          2. Is this message so simple that no model is needed at all?
#          3. Should this go to the fast cloud brain or the local one?
#
#      It guesses with regular expressions, which are dumb and instant. Getting
#      it slightly wrong is fine on purpose: if the model finds itself without
#      the tool it needs, it can call `find_tools` and ask for more. That escape
#      hatch is why this file is allowed to be approximate.
#
#  THE LINE THIS FILE MUST NOT CROSS
#      This module picks CAPABILITIES. It does not decide what Ted does.
#      An earlier version of Ted had ~50 regular expressions that tried to be
#      the assistant, and it made him feel like a vending machine — any phrasing
#      the author had not thought of simply did not work. That was deleted
#      deliberately. If you find yourself adding a pattern that decides an
#      *answer* rather than a *menu*, you are rebuilding the thing that was
#      removed. See §34.
#
#  THE SHAPE OF IT
#      FIND_TOOLS_SCHEMA      one-use escape hatch for unmatched clear actions
#      _FAMILIES              regex -> tool names. Rough groupings, not commands
#      select_tool_schemas    the main entry: message in, small tool list out
#      discover_tool_schemas  what find_tools actually runs
#      plan_reflex            "open Spotify" — complete, reversible, zero tokens
#      plan_document          same idea for document creation
#      classify_brain         local model or cloud model, by rules
#      classify_brain_with_model   the tiebreak, asked of a tiny local model
#      memory_scope_for       how much memory retrieval this turn earns
#      operational_context    a short note about what Ted just did
#
#  IF YOU WANT TO CHANGE SOMETHING
#      "Ted never offers tool X when I ask for it"
#            -> add a word to the matching _FAMILIES row. §7.2.
#      "Ted uses the cloud when it should stay local"
#            -> the thresholds are in classify_brain. §7.5.
#      "Opening an app should not cost a model call"
#            -> that is plan_reflex. §7.4.
#
#  PYTHON YOU'LL SEE HERE THAT MIGHT BE NEW
#      re.compile(r"...")
#          A regular expression: a pattern for matching text. The `r` before the
#          quotes means "raw string" — backslashes stay literal, which matters
#          because regex is full of them. \b means "word boundary",
#          (?: ... ) is a group you do not want to capture, | means "or".
#          You do not need to be able to write these to work on Ted; you do need
#          to be able to read them well enough to add a word to a list.
#
#      @dataclass
#          A shortcut for "a class that is mostly just a bag of named values".
#          Python writes __init__ for you from the field list.
#
#      from __future__ import annotations
#          A compatibility line about type hints. It changes nothing you can
#          see. Ignore it.
#
#      a dict comprehension:  {s["function"]["name"]: s for s in TOOL_SCHEMAS}
#          Build a dictionary in one line — here, "tool name -> its schema", so
#          a lookup by name is instant instead of a loop.
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass

from core.actions import APPS, match_running_app, resolve_app_alias
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

# Words that carry no capability signal. Every one of these appears in several
# tool descriptions because the descriptions are written as instructions, so a
# query sharing one of them tells this module nothing.
_DISCOVERY_STOPWORDS = frozenset("""
a an and or the to of for in on at by with from is are be been was were it its
this that these those use uses used using when whenever what which who whom how
why if then than as into out up down about after before during while any all
each every some no not do does did done can could should would may might must
user users charlie ted his him he her she they them your you i me my mine our
right now current currently recent recently new newest latest thing things
something anything one two first second next last other another same only just
also more most much many few least best good default e.g eg etc via per over
under between across around back again still yet ever never always sometimes
""".split())

# A single shared common word scores 1 and must not qualify. A word appearing
# in the tool's own name scores 3, which does.
_DISCOVERY_MIN_SCORE = 3


def _content_words(text):
    """Lowercase word set with the noise removed."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return {w for w in words if len(w) > 2 and w not in _DISCOVERY_STOPWORDS}


# These are capability-family hints, not command parsers. False positives cost a
# few schema tokens; false negatives are recovered by find_tools.
_FAMILIES = (
    (r"\b(?:open|close|quit|launch|start|bring up|pull up|app|application|window)\b",
     ("open_app", "close_app")),
    (r"\b(?:terminal|shell|command[- ]line|cli)\b[^.?!]*"
     r"\b(?:run|execute|start|type|enter)\b|"
     r"\b(?:run|execute)\b[^.?!]*\b(?:in|using)\s+(?:the\s+)?"
     r"(?:terminal|shell)\b",
     ("ui_inspect", "ui_fill", "type_text", "press_key")),
    # "clean up" shares no words with the family above, so without its own row
    # the one tool that turns four round trips into one is never on the menu.
    (r"\b(?:clean(?:\s*up|ing)?|tidy|declutter|close everything|close them all|"
     r"shut everything|clear (?:my )?(?:desktop|screen))\b",
     ("clean_up", "close_app")),
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
     r"\b(?:add|save|remove|delete)\s+(?:this|that|it|the current)\b"
     r"[^.?!]*\b(?:song|track|music)\b",
     ("add_to_playlist", "remove_from_playlist", "create_playlist",
      "delete_playlist", "play_playlist")),
    # The text bouncer. "tell me when X texts" is a standing rule; "text X" is
    # send_message and must not land here, which is why every pattern below
    # needs the notification sense — when/if someone texts ME, not text them.
    (r"\b(?:tell|let) me (?:know )?(?:when|if)\b[^.?!]*\b(?:texts?|messages?)\b|"
     r"\bwhen\b[^.?!]*\b(?:texts? me|messages? me)\b|"
     r"\b(?:ignore|mute|silence|don'?t tell me about)\b[^.?!]*"
     r"\b(?:texts?|messages?|notifications?)\b|"
     r"\bbouncer\b|"
     r"\bwho are you watching for\b|"
     r"\b(?:announce|notify me about)\b[^.?!]*\b(?:texts?|messages?)\b",
     ("bouncer_watch", "bouncer_status", "bouncer_toggle")),
    # Answering the announcement. Deliberately narrow: these are the words
    # Charlie will actually say to a "want me to read it, or open it?" prompt.
    (r"^\s*(?:yes,?\s*)?(?:read|open|show)\s*(?:it|that|the (?:text|message))?"
     r"\s*(?:out ?loud|aloud|to me)?\s*[.!?]?\s*$|"
     r"^\s*what does it say\b",
     ("text_respond",)),
    # Ted's own source. Deliberately keyed on possessives and self-reference —
    # "your code", "how are you built" — because "the code" on its own is far
    # more often Charlie's homework or a project he is discussing than it is
    # Ted, and loading seven schemas for that is exactly the prompt weight this
    # router exists to avoid.
    (r"\b(?:your|ted'?s|his|own|the)\s+(?:own\s+)?"
     r"(?:code|codebase|source|source code|repo|repository|implementation)\b|"
     r"\b(?:how (?:are|were) you (?:built|made|written|implemented)|"
     r"what (?:are|were) you (?:built|made|written) (?:with|in)|"
     r"read your own|look at your (?:code|source)|your architecture|"
     r"which file|what file|show me the (?:code|source|file)|"
     r"in (?:core|tests|ui)/|\.py\b)\b",
     ("code_overview", "code_search", "code_read", "code_tree",
      "code_history", "code_diff")),
    # A request to change Ted still gets read/search context, but no write
    # schema. Charlie deliberately removed self-modifying code from the model's
    # menu; confirmation around an implementation is weaker than no capability.
    (r"\b(?:change|edit|modify|fix|update|rewrite|patch|refactor|add)\b"
     r"[^.?!]*\b(?:your|ted'?s|own)\s+(?:own\s+)?"
     r"(?:code|codebase|source|file|module)\b|"
     r"\bedit yourself\b|\bchange yourself\b|\bmodify your own\b",
     ("code_read", "code_search")),
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
    # Ted's own notebook is a different store from Apple Notes, and it had no
    # family at all — "read my notebook page about X" produced an EMPTY menu, so
    # no tools were sent and there was nothing for the model to recover with.
    # "notebook" does not match the \bnote\b family above (the word continues),
    # so this needs its own row rather than an extra alternative up there.
    (r"\bnotebook\b|\bnote ?book\b",
     ("notebook_read", "notebook_write", "notebook_edit", "notebook_search",
      "notebook_delete")),
    (r"\b(?:clipboard|copy|paste)\b", ("clipboard_read", "clipboard_write")),
    (r"\bvolume\b", ("system_volume",)),
    (r"\bbrightness\b", ("system_brightness",)),
    (r"\b(?:screen|display|video)\b", ("screen_describe",)),
    (r"\b(?:click|tap|button|link|cursor|control)\b",
     ("ui_inspect", "ui_press", "screen_describe")),
    # Text entry is broader than the literal verb "type". People naturally say
    # "enter this", "fill in X", or "prompt an assistant with Y". Missing those
    # words left compound requests with only app-opening tools, so the model
    # could do the first clause and had no visible way to finish the rest.
    (r"\b(?:type|enter|input|fill(?:\s+in)?|prompt|promt|paste)\b",
     ("ui_inspect", "ui_fill", "type_text", "press_key")),
    (r"\b(?:press|keyboard|key)\b", ("press_key", "ui_press")),
    (r"\bscroll\b", ("scroll",)),
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
    r"imessage|paste|screenshot|click|tap|press|scroll|type|enter|input|"
    r"fill|prompt|promt)\b",
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


def catalog():
    """Every tool contract Ted owns, keyed by name.

    `select_tool_schemas` hands the model a small menu, so the model will
    sometimes name a real capability the menu left out. That is a router miss,
    not a hallucination, and llm.py resolves it from this mapping instead of
    spending two extra rounds on find_tools. Returned as a copy: the runtime
    mutates its own menu, never this one.
    """
    return dict(_SCHEMA_BY_NAME)


def _family_names(text):
    names = []
    lowered = (text or "").lower()
    for pattern, family in _FAMILIES:
        if re.search(pattern, lowered, re.I):
            names.extend(family)
    return names


# [BOOK §7.2] ─── PICKING THE MENU ───────────────────────────────────────────
# Message in, a short list of tool schemas out. This is the function that keeps
# a turn affordable.
#
# It works by running the message past _FAMILIES above — rough regex groupings
# of the shape "if the message mentions opening or launching, they probably want
# open_app and close_app". These are capability HINTS, not command parsers.
#
# It is allowed to be wrong in both directions:
#   too many tools  -> costs a few tokens
#   too few tools   -> the model calls find_tools and asks for more (§7.3)
# Only the second one is even visible, and it self-corrects. That asymmetry is
# what lets this file stay simple.
def select_tool_schemas(text, recent_action_text=""):
    """Return only the capability contracts this turn is likely to use.

    A known family does not also pay for discovery: if its menu is incomplete,
    the model can say so honestly. The discovery escape hatch is kept only for
    clear action requests whose wording matched no family at all. This makes a
    greeting a genuinely tool-free call and prevents every normal tool turn
    from acquiring another model round by default.
    """
    names = _family_names(text)
    # When one phrase mentions both media and a concrete UI gesture ("tap the
    # play button on this video"), broad media families appear earlier in the
    # catalog. Put the explicitly requested interaction first so the global
    # eight-contract cap cannot evict the tool that performs the verb.
    if re.search(r"\b(?:click|tap|button|control)\b", text or "", re.I):
        names = ["ui_inspect", "ui_press", "screen_describe", *names]
    elif re.search(r"\b(?:type|enter|field|input|fill(?:\s+in)?|prompt|promt|paste)\b",
                   text or "", re.I):
        names = ["ui_inspect", "ui_fill", "type_text", "press_key", *names]
    if re.search(r"\b(?:new|blank)\b.{0,20}\b(?:document|google doc|textedit)\b",
                 text or "", re.I):
        names = ["create_document", *names]
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
        if len(chosen) >= 8:
            break
    if chosen:
        return chosen
    return [FIND_TOOLS_SCHEMA] if likely_action_request(text) else []


# [BOOK §7.7] ─── THE CLEANUP REFLEX ─────────────────────────────────────────
# "clean up" is a COMPLETE request: there is no target to resolve and exactly
# one sensible action. It still went to the model, and qwen3.6-27b answered it
# by chaining close_app one app at a time — two rounds, two rate-limited calls,
# for a job Python can enumerate itself.
#
# This does not decide an ANSWER, which is the line this file must not cross.
# It picks one capability for an unambiguous phrase, and the ordinary
# confirmation gate still asks before anything closes.
#
# Anything naming an app ("clean up Chrome", "close Safari") is deliberately
# NOT a match — a named target is close_app's job and the model resolves it.
_CLEANUP_RE = re.compile(
    r"^(?:(?:hey|okay|ok|alright|please|ted|let\'?s)[, ]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|need)\s+you\s+to\s+)?"
    r"(?:clean\s*(?:up|things up|it up)?|tidy\s*(?:up)?|declutter"
    r"|close\s+(?:everything|all\s+(?:my\s+)?apps|them\s+all)"
    r"|shut\s+everything\s+down"
    r"|close\s+(?:every|all)\s+(?:other\s+)?app(?:lication)?s?"
    r"|clear\s+(?:my\s+)?(?:desktop|screen))"
    r"[\s.!?]*$", re.I)

# Same opening, without the end anchor: "clean up but leave brave" is a cleanup
# whose tail is a constraint, not a different request. What the tail MEANS is
# extract_kept_apps' job.
_CLEANUP_PREFIX_RE = re.compile(_CLEANUP_RE.pattern.replace(r"[\s.!?]*$", r"\b"), re.I)


def cleanup_reflex(text):
    """True when the message is a whole-desktop cleanup and nothing else."""
    return bool(_CLEANUP_RE.match(" ".join(str(text or "").strip().split())))


def cleanup_request(text):
    """True when the message STARTS with a cleanup idiom but says more.

    "clean up but leave brave" is still a cleanup; the tail is a constraint on
    it. Only the opening idiom is recognised here — working out what the tail
    means is :func:`extract_kept_apps`'s job, and it asks a model rather than a
    pattern, because the ways to say "except that one" do not enumerate.
    """
    return bool(_CLEANUP_PREFIX_RE.match(" ".join(str(text or "").strip().split())))


_KEEP_CLAUSE_RE = re.compile(
    r"\b(?:leave|keep|spare|save|except(?:\s+for)?|without\s+(?:closing|quitting)|"
    r"don['’]?t\s+(?:close|quit)|do\s+not\s+(?:close|quit)|but\s+not)\b\s*(.+)$",
    re.I,
)


def _explicit_kept_apps(text, running):
    """Resolve app names directly from an explicit cleanup keep-clause.

    This is intentionally narrower than understanding the whole request. The
    cleanup prefix already chose the capability; this only prevents a small
    model from silently dropping one of several named exclusions. Every named
    piece must resolve, otherwise the caller fails closed and asks the brain.
    """
    match = _KEEP_CLAUSE_RE.search(" ".join(str(text or "").strip().split()))
    if not match:
        return None
    tail = re.sub(r"[.!?]+$", "", match.group(1)).strip()
    pieces = [piece.strip() for piece in re.split(r"\s+(?:and|or)\s+|[,;&]", tail, flags=re.I)
              if piece.strip()]
    if not pieces:
        return None
    kept = []
    for piece in pieces:
        cleaned = re.sub(
            r"\b(?:open|opened|running|up|please|the|my|app|apps|application|applications)\b",
            " ", piece, flags=re.I)
        cleaned = " ".join(cleaned.split())
        app = match_running_app(cleaned, running)
        if app is None:
            return None
        if app not in kept:
            kept.append(app)
    return kept or None


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


def discover_tool_schemas(query, exclude=(), limit=4):
    """Retrieve schemas for a capability query; used by the find_tools meta-tool.

    Two sources, deliberately not mixed. A capability FAMILY match is knowledge
    this module actually has; bare word overlap against a description is a
    guess. Mixing them is what put `code_overview`, `bouncer_status` and
    `get_emails` on a turn asking for sports scores — the junk scored 2 on
    "current" and "recent" while `web_search` scored 1, and the model paid a
    round trip for a menu that could not answer it.

    So: if any family matched, return only that. Word overlap fills the menu
    only when this module recognized nothing at all, and then it must clear a
    threshold no single common word can reach.
    """
    excluded = set(exclude)
    family = [n for n in _family_names(query) if n not in excluded]
    if family:
        ordered, seen = [], set()
        for name in family:
            if name in _SCHEMA_BY_NAME and name not in seen:
                seen.add(name)
                ordered.append(_SCHEMA_BY_NAME[name])
        if ordered:
            return ordered[:limit]

    query_words = _content_words(query)
    if not query_words:
        return []
    ranked = []
    for schema in TOOL_SCHEMAS:
        name = tool_name(schema)
        if name in excluded:
            continue
        fn = schema["function"]
        # A word in the tool's NAME is a much stronger signal than the same
        # word buried in prose that exists to teach the model when to call it.
        name_hits = len(query_words & set(name.split("_")))
        desc_hits = len(query_words & _content_words(fn.get("description", "")))
        score = 3 * name_hits + desc_hits
        if score >= _DISCOVERY_MIN_SCORE:
            ranked.append((score, name, schema))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    # Returning nothing tells the model to ask one useful question rather than
    # silently loading contracts that cannot serve the request.
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
    if any(verb.match(t) and target.search(t)
           for verb, target in _TARGETED_ACTIONS):
        return True
    # A read-then-write request begins with a harmless lookup ("check the
    # weather") but still contains an action that must complete. Classify each
    # explicit stage instead of judging only the first verb. This is what makes
    # a provider failure after the lookup retry rather than leak a raw 500.
    stages = _request_stages(t)
    return len(stages) > 1 and any(likely_action_request(stage) for stage in stages)


_SEQUENCE_SEP = re.compile(
    r"\b(?:and then|then|after that|afterwards|followed by|once that)\b|"
    r"\band\s+(?=(?:open|close|quit|launch|play|pause|send|message|text|email|"
    r"set|add|create|write|copy|paste|type|enter|input|fill|prompt|promt|run|execute|"
    r"delete|flag|mark|change|turn|search|"
    r"find|look up|show|hide|read|check|log|calculate|browse|navigate|remove|tell)\b)",
    re.I,
)

_PUNCTUATED_STAGE_SEP = re.compile(
    r"[,;]\s*(?=(?:open|close|quit|launch|play|pause|send|message|text|email|"
    r"set|add|create|write|copy|paste|type|enter|input|fill|prompt|promt|run|execute|"
    r"delete|flag|mark|change|turn|search|find|look up|show|hide|read|check|"
    r"log|calculate|browse|navigate|remove|tell)\b)",
    re.I,
)


def _request_stages(text):
    """Split punctuation only when it introduces another action clause.

    A blanket comma split would corrupt payloads such as ``type hello, world``.
    Requiring an action verb after punctuation preserves those payloads while
    recognizing natural dictated lists separated by commas or semicolons.
    """
    punctuated = _PUNCTUATED_STAGE_SEP.sub(" then ", text or "")
    return [part.strip(" ,;") for part in _SEQUENCE_SEP.split(punctuated)
            if part.strip(" ,;")]


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
    segments = _request_stages(text)
    total = 0
    previous_targets = 1
    for segment in segments or [text or ""]:
        lower = segment.lower()
        # Prompting an interactive agent or running a typed command takes two
        # distinct operations: enter the text, then submit it. This remains a
        # lower bound; the model chooses the actual UI/keyboard tools.
        if re.match(r"\s*(?:prompt|promt|run|execute)\b", lower):
            total += 2
            continue
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
        # least one completed tool call. The model still decides which tool it is.
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


# [BOOK §7.5] ─── CLOUD OR LOCAL ─────────────────────────────────────────────
# Which brain earns this turn. Rules first, because rules are instant; only a
# genuinely ambiguous message pays about a tenth of a second to ask a tiny local
# router model (classify_brain_with_model, below).
#
# The bias is deliberately toward the cloud. Getting LOCAL wrong costs answer
# quality, which you notice and cannot undo; getting CLOUD wrong costs tokens,
# which refill every minute. Asymmetric mistakes deserve an asymmetric default.
#
# This verdict is ADVISORY. providers.chat_create will still escalate to the
# cloud if the local brain is not actually available.
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


# [BOOK §7.7] ─── ASKING THE SMALL MODEL WHICH APPS TO SPARE ─────────────────
# The first version of this was a regex with fourteen alternatives — except,
# besides, minus, without, save for, barring, but not, leave X open… and it
# still missed phrasings on the first try. That is the vending machine this
# file exists to avoid: any wording the author did not imagine simply fails.
#
# So the reflex decides the ACTION (a cleanup, one call, not four) and a model
# reads the LANGUAGE. Same split classify_brain_with_model already uses, and
# the same local llama3.2:3b, so it costs ~0.1s and no cloud tokens.
#
# Two safety properties matter more than accuracy here:
#   * The candidate list is the apps actually running, and anything the model
#     says that is not on it is dropped. A hallucinated name cannot spare a
#     process that does not exist, or worse, be read as something else.
#   * None (router unavailable) is NOT the same as [] (nothing to spare). The
#     caller must fall through to the normal path on None rather than close an
#     app the user may have asked it to keep.
_KEEP_SYSTEM = (
    "You label a request about closing macOS apps. You never answer it.\n"
    "You are given the apps that are currently open.\n"
    "Reply with exactly one of:\n"
    "  NO     - the request is not about closing most or all open apps\n"
    "  NONE   - it is a general cleanup and no app is spared\n"
    "  <names> - it is a general cleanup, sparing these apps (comma separated,\n"
    "            spelled exactly as in the open-apps list)\n"
    "Closing ONE named app is NO. Tidying files, folders or anything that is\n"
    "not an open app is NO. When unsure, reply NO."
)

_KEEP_USER = (
    "Open apps: {apps}\n"
    "--- BEGIN MESSAGE ---\n{text}\n--- END MESSAGE ---\n"
    "NO, NONE, or names from the list:"
)

# The router said this is not a general cleanup at all.
NOT_A_CLEANUP = "not-a-cleanup"


def extract_kept_apps(text, running, ask=None):
    """Decide whether a request is a general cleanup, and what it spares.

    Returns one of:
      ``NOT_A_CLEANUP`` - the small model says this is something else
      ``None``          - the router could not answer; the caller must NOT act
      ``[]``            - a cleanup that spares nothing
      ``[names]``       - a cleanup sparing these exact running apps

    ``running`` is the candidate list and the whitelist at once: a name the
    model invents is dropped rather than guessed at. ``ask`` is injected so
    this is testable without Ollama, exactly as classify_brain_with_model does.
    """
    candidates = [str(app) for app in (running or []) if str(app).strip()]
    # Explicit named exclusions are safer and faster to resolve directly. The
    # small router previously returned only Spotify for "leave ChatGPT and
    # Spotify", producing a confirmation that contradicted the request.
    explicit = _explicit_kept_apps(text, candidates)
    if explicit is not None:
        return explicit
    if ask is None:
        from core.providers import route_hint
        ask = route_hint
    try:
        raw = ask(_KEEP_SYSTEM,
                  _KEEP_USER.format(apps=", ".join(candidates) or "(none)",
                                    text=(text or "")[:500]),
                  num_predict=48) or ""
    except Exception as exc:
        print(f"[router] keep-list unavailable: {exc}")
        return None
    raw = raw.strip()
    if not raw:
        return None
    if re.match(r"^\W*NO\b", raw, re.I):
        return NOT_A_CLEANUP
    if re.search(r"\bNONE\b", raw, re.I):
        return []

    by_lower = {app.lower(): app for app in candidates}
    kept = []
    for piece in re.split(r"[,\n;]+", raw):
        piece = piece.strip().strip('.\'"` ')
        if not piece:
            continue
        app = by_lower.get(piece.lower())
        if app is None:
            # Substring both ways catches "brave" for "Brave Browser" without
            # inviting the fuzzy matching that once turned blender into Finder.
            app = next((c for c in candidates
                        if piece.lower() in c.lower() or c.lower() in piece.lower()),
                       None)
        if app and app not in kept:
            kept.append(app)
    if not kept:
        # It answered, but with nothing recognisable. Refusing to act is the
        # only safe reading: it may have named an app it could not spell.
        print(f"[router] no known app in keep-list {raw[:60]!r}")
        return None
    return kept


@dataclass(frozen=True)
class ReflexPlan:
    calls: tuple[tuple[str, dict], ...]


def plan_system_volume(text, recent_actions=()):
    """Plan an unambiguous system-volume read/change without a model call.

    A short follow-up such as "set it to 50" is accepted only when the most
    recent verified action was system_volume. That preserves natural context
    without turning every context-free "set it" into a device command.
    """
    raw = " ".join(str(text or "").strip().rstrip(".!?").split())
    lower = raw.lower()
    recent_volume = bool(recent_actions and
                         (recent_actions[-1] or {}).get("tool") == "system_volume")

    set_match = re.fullmatch(
        r"(?:(?:please\s+)?(?:set|change|turn)\s+)?"
        r"(?:(?:the|my)\s+)?(?:system|computer|mac)?\s*volume\s+(?:to\s+)?"
        r"(\d{1,3})\s*%?",
        lower,
    )
    if set_match:
        return ReflexPlan((("system_volume", {
            "action": "set", "level": max(0, min(100, int(set_match.group(1))))
        }),))
    followup = re.fullmatch(r"(?:please\s+)?(?:set|change|turn)\s+it\s+to\s+(\d{1,3})\s*%?",
                            lower)
    if followup and recent_volume:
        return ReflexPlan((("system_volume", {
            "action": "set", "level": max(0, min(100, int(followup.group(1))))
        }),))
    if re.fullmatch(
            r"(?:what(?:'s| is)|check|tell me|show me)?\s*"
            r"(?:(?:the|my)\s+)?(?:system|computer|mac)?\s*volume"
            r"(?:\s+(?:level|at|at right now|right now|currently))?",
            lower):
        return ReflexPlan((("system_volume", {"action": "get"}),))
    if re.fullmatch(r"(?:turn\s+)?(?:the\s+)?(?:system|computer|mac)\s+volume\s+"
                    r"(?:up|louder|higher)", lower):
        return ReflexPlan((("system_volume", {"action": "up"}),))
    if re.fullmatch(r"(?:turn\s+)?(?:the\s+)?(?:system|computer|mac)\s+volume\s+"
                    r"(?:down|lower|quieter)", lower):
        return ReflexPlan((("system_volume", {"action": "down"}),))
    return None


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
    return resolve_app_alias(target)


# [BOOK §7.4] ─── THE ZERO-TOKEN LANE ────────────────────────────────────────
# "open Spotify" does not need a language model. This recognises COMPLETE,
# REVERSIBLE app open/close requests and returns a plan that core/app.py can
# execute directly — no prompt, no tokens, no waiting.
#
# The word doing the work is COMPLETE. If even one target is unclear, this
# returns None and the whole turn goes to the model instead of half-guessing.
# A reflex that fires on an ambiguous request is the old regex ladder coming
# back, and the old regex ladder is what made Ted feel like a robot. §34.
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
