"""core/llm.py — Ted's brain: Groq client, prompts, streaming replies,
web search, and the small composition/summarisation
helpers used by messaging and email.
"""

import json
import re
import threading
import time
import traceback
from datetime import date

from core import features, intents
from core.actions import detect_action
from core.hud_bridge import show_issue
from core.logs import error_log
from core import providers
from core.memory import (save_memory, get_memory, save_fact, get_facts_about,
                         format_memories_for_prompt)

try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"

# ONE provider path. Everything that thinks — replies, tool calls, fact
# extraction, session summaries, web synthesis, and vision — goes through
# chat_create(). The free hosted Qwen is tried first; the local Qwen is a true
# offline fallback for the same request, not a separate lower-quality workflow.
CHAT_MODEL          = providers.CLOUD_CHAT_MODEL
CHAT_FALLBACK_MODEL = providers.LOCAL_CHAT_MODEL

# Kept as an attribute for the voice module's cloud Whisper path. It may be
# None when Ted is configured for completely local/offline use.
groq_client = providers.groq_client()


def chat_create(**kwargs):
    """Use the free hosted brain, then the genuinely local offline brain."""
    return providers.chat_create(**kwargs)

# Hard ceiling on how long memory retrieval may delay a reply. Everything it
# gathers is optional context; the answer is not.
CONTEXT_BUDGET = 4.0

MAX_HISTORY = 20        # messages sent to LLM per turn
MAX_CONV_MESSAGES = 40  # hard cap on stored conversation length (keeps system msg at [0])


_DEEP_REQUEST_RE = re.compile(
    r"\b(?:why|how does|how do|explain|analy[sz]e|compare|evaluate|research|"
    r"investigate|reason|figure out|debug|design|architect|plan|prove|solve|"
    r"what do you think|should i|pros and cons|step[- ]by[- ]step)\b",
    re.I,
)


def reasoning_effort_for(text):
    """Choose thinking depth from request complexity, not command phrases.

    Short, single-clause turns do not need hidden chain-of-thought just to pick
    a tool or answer a greeting. Longer, chained, explanatory, and analytical
    requests retain Qwen's normal reasoning mode. The model still sees the same
    complete tool menu in both modes and remains the sole intent router.
    """
    words = re.findall(r"[A-Za-z0-9']+", text or "")
    if len(words) > 12:
        return "default"
    if re.search(r"\b(?:and then|then|after that|before you|followed by)\b", text or "", re.I):
        return "default"
    if _DEEP_REQUEST_RE.search(text or ""):
        return "default"
    return "none"


_ACTION_VERBS = (r"closed|opened|quit|launched|sent|texted|emailed|played|paused|"
                 r"skipped|scheduled|created|deleted|typed|copied|muted")

# Only Ted's OWN completed actions count. The verb has to either start a
# sentence — Ted's house style is terse, "Closed VS Code." — or follow "I".
# Without that anchor, "you closed that tab yourself" tripped the check, and a
# warning that fires on ordinary conversation is one you learn to ignore.
_ACTION_CLAIM_RE = re.compile(
    r"(?:^|(?<=[.!?]\s)|(?<=[.!?]\s\s))\s*(?:" + _ACTION_VERBS + r")\b"
    r"|\bi(?:'ve| have)?\s+(?:just\s+)?(?:" + _ACTION_VERBS + r")\b",
    re.I,
)
# Reads as past tense but is not a claim that something got done.
_NOT_A_CLAIM_RE = re.compile(
    r"\b(?:can't|cannot|couldn't|could not|didn't|did not|won't|will not|"
    r"unable|haven't|have not|never|want me to|shall i|should i)\b", re.I,
)


def claims_completed_action(text):
    """True if the reply says Ted DID something.

    Ted told Charlie "Closed VS Code and Notes." having called no tool at all,
    then a minute later insisted it had no way to close apps — while close_app
    was in its menu the whole time, and close_app itself verifies properly
    before confirming. The lie was upstream of every safeguard: the model
    narrated the outcome instead of acting, and text with no tool call attached
    streams straight to the user.

    A prompt rule already forbade this and did not hold, so this is the check in
    code. Deliberately crude — past tense, first person or sentence-initial,
    negations excluded — because its job is to notice and say so, not to parse
    English.
    """
    body = (text or "").strip()
    if not body:
        return False
    return bool(_ACTION_CLAIM_RE.search(body)) and not _NOT_A_CLAIM_RE.search(body)


def stable_window(items, min_keep, chunk=8):
    """Return a recent-suffix of `items` whose START only moves once every
    `chunk` appends (window length ranges min_keep .. min_keep+chunk-1).

    Why not a plain [-min_keep:]? A sliding window shifts by one every turn,
    which changes the prompt prefix every call and kills the provider's
    prefix cache — every turn reprocesses the whole prompt (system, tool
    schemas, history) from scratch. This was the 'fast for four replies,
    then slow' cliff: the tool probe's 8-message window filled after four
    exchanges and started sliding. Chunked trimming keeps the prefix
    byte-identical for whole stretches, so cached turns stay fast."""
    n = len(items)
    if n <= min_keep:
        return items
    start = ((n - min_keep) // chunk) * chunk
    return items[start:]

# Tracks Groq reachability for the HUD health dot (flipped inside ask_streaming).
_GROQ_OK = True

def groq_ok():
    # active_provider() is "none" until the first completion of the session, so
    # gating on == "groq" alone made the HUD report a Groq outage at boot that
    # had not happened. Only a real local-fallback turn counts as Groq down.
    return _GROQ_OK and providers.active_provider() != "ollama"

# ---------- persona ----------
SYSTEM_PROMPT = (
    f"You are Ted, {OWNER_NAME}'s personal AI chatbot — his primary AI, the one "
    "he talks to all day in a chat window. You know him, remember him, and are "
    "proactively involved in his daily life: classes, schedule, to-dos, ideas. "

    # Brevity — still the most important rule
    "Default to short and direct: a sentence or two for simple things. "
    "Go longer when the question deserves it — explanations, how-tos, working "
    "through a problem — but never pad. Never recap what was just said. "
    "Never summarise your own answer. "

    # Openers — hard ban
    "Never open with: 'Got it', 'Sure', 'Of course', 'Certainly', 'Absolutely', "
    "'Great', 'Sounds good', 'Happy to', 'No problem', 'I'd be happy to'. "
    "Just answer. The first word out of your mouth should be useful. "

    # Hedging — hard ban
    "Never hedge: no 'I think', 'it seems like', 'you might want to', 'perhaps', "
    "'it could be'. State the answer. If you're genuinely uncertain, say 'Not sure — "
    "double-check that' and move on. One sentence, not a paragraph of caveats. "

    # Tone — confident, dry, slightly formal
    "You're confident and direct, with a dry wit. Not warm and chatty — more like a "
    "brilliant, efficient colleague who respects your time. Occasional dry humour is "
    "fine, never forced. Contractions are fine. Don't be stiff. "

    # Pushback
    "If something seems off, say so plainly: 'That doesn't add up.' or 'I'd push back "
    "on that.' You don't just confirm whatever you're told. "

    # Name use
    f"Use {OWNER_NAME}'s name sparingly — once every several exchanges at most. "

    # Chat format
    "Replies appear in a chat window. Write naturally: plain sentences and short "
    "paragraphs. Light formatting (a short list, `code`) only when it truly helps. "
    "ALWAYS wrap code in fenced blocks with the language name, like "
    "```python ... ``` — never paste code as plain text. "
    "No emojis. If voice mode is on your words are also spoken aloud, so keep "
    "sentences readable out loud. "

    # Memory
    "You remember this conversation and facts about the user. Reference them naturally "
    "when relevant — never announce it with phrases like 'according to my memory'. "

    # Capabilities
    "You can set timers and reminders, give a morning briefing, read and add "
    "calendar events, control Spotify, and relay hard questions to Claude. Nudge "
    "toward clear phrasing if needed — 'set a timer for ten minutes', 'remind me "
    "to call back in an hour'. "

    # Honesty about actions — the one rule you never break
    "NEVER write a sentence like 'Closed VS Code' or 'Sent it' unless the tool "
    "call for it is in this same reply. If you intend to do something, call the "
    "tool — saying it is not doing it, and the user cannot tell the difference "
    "until they look. If you are unsure whether a tool exists for something, "
    "check your tool list before saying you cannot: close_app, open_app and the "
    "rest are always available to you. "
    "You can ONLY perform actions through your tools. Never claim to have opened, "
    "closed, sent, set, scheduled, added, played, typed, or done anything unless a "
    "tool actually ran and confirmed it. If you can't run the tool or aren't sure it "
    "worked, say what you need or that you couldn't — never pretend it's done. "
    "Describing an action is not the same as doing it. "

    # Intent over literal words
    "The user speaks naturally — sometimes with filler words, imperfect phrasing, or "
    "speech recognition artifacts (e.g. 'klose' means 'close', 'spotify' misspelled). "
    "Always interpret intent rather than requiring exact or perfect wording. "

    # Handling gaps — never be confused, always have a move
    "When a request is ambiguous or missing a detail, you have exactly two moves: "
    "(1) make the most reasonable assumption, act on it, and say which assumption "
    "you made — 'Assuming you meant tomorrow's 9am class…'; or (2) if the choice "
    "genuinely changes the outcome, ask ONE short question. Never say you're "
    "confused, never list every interpretation, never freeze. A wrong-but-stated "
    "assumption beats a stalled conversation — the user will just correct you. "

    # Whose words these are
    "Messages, emails and notes you write are sent BY the user, from the user's "
    "own device, to people the user chose. When they give you the words, send "
    "those words. Do not improve the grammar, do not add a greeting, do not "
    "soften the tone, and do not append anything. Their slang and their typos "
    "are how they talk. "
    "Do not argue about whether a message is a good idea, whether it is too "
    "blunt, too casual, or too silly, and do not object that a joke between the "
    "user and their friend might be misread — that is theirs to judge, and the "
    "friend knows them. You are drafting on their behalf, not approving them. "
    "You still don't write slurs or abuse at anyone, and you still say so "
    "plainly in one line if asked to. Everything short of that, just send. "

    # Knowing your limits
    "If a question needs deeper reasoning than you can give — hard math, tricky "
    "code, multi-step analysis — give your best take and be honest about how "
    "confident you are, rather than bluffing."
)

THINKING_CONTEXT = (
    "THINKING PARTNER MODE: Do NOT give advice, solutions, or recommendations. "
    "Instead, briefly reflect back what the user just said (one sentence), "
    "then ask ONE focused Socratic follow-up question — for example: "
    "'What makes you say that?', 'What's the ideal outcome here?', "
    "'What's stopping you?', 'What would success look like?', "
    "'What have you actually tried so far?', 'What does your gut tell you?'. "
    "Keep the total response to 2-3 sentences. Never offer advice unless explicitly asked."
)

# ---------- fact extraction (background, never blocks Ted) ----------
def _parse_fact_payload(raw):
    """Pull a list of fact dicts out of whatever the model returned.

    Small models wrap JSON in prose ("Here is the array:") or fences even when
    told not to, and json.loads on that raises — which is how fact extraction
    used to fail silently and lose everything. So: try strict JSON first, then
    salvage the first {...} or [...] block out of the text.
    """
    if not raw:
        return []
    raw = raw.replace("```json", "").replace("```", "").strip()

    def _coerce(obj):
        # JSON mode returns an object; accept {"facts": [...]}, a bare list, or
        # a single fact dict.
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("facts", "results", "items", "data"):
                if isinstance(obj.get(key), list):
                    return obj[key]
            if all(k in obj for k in ("subject", "relationship", "object")):
                return [obj]
        return []

    try:
        return _coerce(json.loads(raw))
    except Exception:
        pass
    # Groq occasionally returns JSON whose formatting whitespace is itself
    # escaped (literal ``\\n`` outside a JSON string). It looks valid in logs
    # but json.loads correctly rejects it. Turning only escaped whitespace into
    # spaces keeps string values safe and recovers the otherwise valid object.
    whitespace_fixed = re.sub(r"\\[nrt]", " ", raw)
    if whitespace_fixed != raw:
        try:
            return _coerce(json.loads(whitespace_fixed))
        except Exception:
            pass
    # Salvage: grab the outermost JSON-looking span and retry.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return _coerce(json.loads(raw[start:end + 1]))
            except Exception:
                continue
    return []


def extract_and_save_facts(user_input, ted_reply):
    """Fire-and-forget background task: ask the fast LLM to extract structured
    facts from the exchange and persist them. Never raises — it runs on a daemon
    thread and must not be able to take the conversation loop down with it."""
    try:
        resp = chat_create(
            # JSON mode: the model is constrained to emit a parseable object, so
            # it can't prepend "Sure! Here's the JSON:" and break the parse.
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": (
                    f"Extract facts about the user ({OWNER_NAME})'s OWN LIFE from this exchange: "
                    "things about him, his family, friends, pets, school, work, preferences, "
                    "possessions, plans. Only clear, explicit facts — not guesses. "
                    'Respond with a JSON object of the form {"facts": [...]} where each element '
                    "has the keys: subject, relationship, object. "
                    "Use short uppercase relationship names like WORKS_AT, LIKES, OWNS, STUDIES, "
                    "LIVES_IN, PREFERS, IS_AGE, HAS_PET, DISLIKES. "
                    f'Example: {{"facts": [{{"subject": "{OWNER_NAME}", "relationship": "LIKES", "object": "jazz"}}]}}. '
                    "HARD RULES — return {\"facts\": []} rather than break these: "
                    "NEVER extract general knowledge, trivia, or facts about the world, even if "
                    "they appear in Ted's reply (e.g. 'bananas are berries' is trivia, NOT a fact "
                    "about the user — do not save it). Ted's reply is context for understanding "
                    "the user only; facts must come from what the USER revealed about himself. "
                    "Never include Ted's statements about himself. Never include questions as facts."
                )},
                {"role": "user", "content": f"User said: {user_input}\nTed replied: {ted_reply}"}
            ],
            max_tokens=300,
            timeout=10.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        facts = _parse_fact_payload(raw)
        if not facts and raw and raw not in ('{"facts": []}', '{"facts":[]}'):
            # Nothing parsed out of a non-empty reply — that's a real failure,
            # not "no facts here". Surface it instead of losing it to a print.
            error_log.error(f"[memory] fact extraction returned unparseable output: {raw[:200]!r}")
        # Hard gate, because the prompt alone doesn't stop the fast model from
        # harvesting trivia out of Ted's own replies ("bananas ARE berries"):
        # a real fact about the user has a subject the USER brought up. If the
        # subject never appears in what the user said and isn't the user
        # himself, it's world knowledge — drop it.
        _ui_low = f" {user_input.lower()} "
        saved = 0
        for f in facts:
            if isinstance(f, dict) and all(k in f for k in ("subject", "relationship", "object")):
                subj = str(f["subject"]).strip()
                subj_low = subj.lower()
                mentioned = (OWNER_NAME.lower() in subj_low
                             or subj_low in _ui_low
                             or any(len(w) > 2 and w in _ui_low
                                    for w in subj_low.split()))
                if not mentioned:
                    print(f"[memory] fact rejected (subject not from user): "
                          f"{subj} → {f['relationship']} → {f['object']}")
                    continue
                save_fact(subj, f["relationship"], f["object"])
                saved += 1
                print(f"[memory] fact saved: {subj} → {f['relationship']} → {f['object']}")
        return saved
    except Exception as e:
        print(f"[memory] fact extraction skipped: {e}")
        error_log.error(f"[memory] fact extraction failed: {e}")
        return 0

# ---------- web search ----------
_NEWSY_RE = re.compile(
    r"\b(?:news|game|games|match|score|won|win|lost|schedule|standings|playoffs"
    r"|world cup|election|happened|today|tonight|yesterday)\b", re.I)

def _newsy(query):
    """True when the query wants time-sensitive info — use the news vertical."""
    return bool(_NEWSY_RE.search(query))


def search_web(query):
    """Search the web via DuckDuckGo. Returns a sentinel string on failure —
    never blocks > 8s. This is now the ONLY source of live web data: the
    snippets go into the reply's context block and the one reasoning model
    answers from them, streaming like any other turn. News-ish queries use the
    news vertical (dated, fresh results) instead of generic web text."""
    try:
        try:
            from ddgs import DDGS          # newer package name
        except ImportError:
            from duckduckgo_search import DDGS  # older package name
        year = date.today().year           # freshness bias in the query
        with DDGS(timeout=6) as ddgs:
            if _newsy(query):
                results = list(ddgs.news(query, max_results=6))
            else:
                results = list(ddgs.text(f"{query} {year}", max_results=6))
        if not results:
            return "__NO_RESULTS__"   # sentinel — caller can speak a fallback
        parts = []
        for r in results:
            body = r.get("body") or r.get("excerpt") or ""
            when = r.get("date", "")
            title = r.get("title", "")
            url = r.get("url") or r.get("href") or ""
            prefix = f"[{when}] " if when else ""
            source = f" (Source: {url})" if url else ""
            parts.append(f"{prefix}{title}: {body}{source}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[web] search failed: {e}")
        return "__SEARCH_ERROR__"


# ---------- live web answers ----------
_web_location = None   # cached "City, Region" for the web prompt

def _get_web_location():
    global _web_location
    if _web_location is None:
        _web_location = ""
        try:
            if features.HAS_ASSISTANT:
                loc = features.assistant.get_location()
                if loc:
                    _web_location = f"{loc['city']}, {loc['region']}"
        except Exception:
            pass
    return _web_location


def web_answer(question):
    """Live-web answer for explicit 'look up X' commands: DuckDuckGo for the
    facts, the one reasoning model to turn them into a sentence, honest
    failure if the search comes back empty."""
    raw = search_web(question)
    if raw in ("__NO_RESULTS__", "__SEARCH_ERROR__"):
        return "I couldn't find anything solid on that right now."
    today = date.today().strftime("%A, %B %d, %Y")
    try:
        r = chat_create(
            messages=[{"role": "user", "content":
                       f"Today is {today}. Using ONLY these web search snippets, answer "
                       f"the question for a chat assistant in one or two short "
                       f"sentences. If they don't fully answer it, give the closest "
                       f"useful information they contain — never mention searching, "
                       f"snippets, or being unable.\n\nQuestion: {question}\n\n"
                       f"Snippets:\n{raw[:4000]}"}],
            max_tokens=140, timeout=10.0,
        )
        return (r.choices[0].message.content or "").strip() or raw[:250]
    except Exception:
        return raw[:250]


def _remember_exchange(user_input, full_reply, conversation):
    """Append a finished exchange to the conversation and kick off the
    background memory/fact writes. Shared by the chat and web paths."""
    _clean = re.sub(r"[\s.,!?;:'\"-]+", "", full_reply)
    if not (full_reply.strip() and _clean):
        return
    conversation.append({"role": "user",      "content": user_input})
    conversation.append({"role": "assistant", "content": full_reply})
    if len(conversation) > MAX_CONV_MESSAGES + 1:
        del conversation[1:len(conversation) - MAX_CONV_MESSAGES]
    threading.Thread(target=save_memory,
                     args=(user_input, full_reply), daemon=True).start()
    if intents._worth_extracting(user_input):
        threading.Thread(target=extract_and_save_facts,
                         args=(user_input, full_reply), daemon=True).start()

# ---------- tool runtime ----------
def validate_tool_arguments(schema, args):
    """Return a concise validation error, or ``None`` for valid arguments."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    params = ((schema or {}).get("function") or {}).get("parameters") or {}
    props = params.get("properties") or {}
    missing = [name for name in params.get("required", [])
               if name not in args or args[name] is None or args[name] == ""]
    if missing:
        return "missing required argument(s): " + ", ".join(missing)
    unknown = [name for name in args if name not in props]
    if unknown:
        return "unknown argument(s): " + ", ".join(unknown)
    py_types = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "array": list, "object": dict,
    }
    for name, value in args.items():
        rule = props.get(name) or {}
        expected = py_types.get(rule.get("type"))
        numeric_bool = rule.get("type") in ("integer", "number") and isinstance(value, bool)
        if expected and (not isinstance(value, expected) or numeric_bool):
            return f"{name} must be {rule['type']}"
        if "enum" in rule and value not in rule["enum"]:
            return f"{name} must be one of: {', '.join(map(str, rule['enum']))}"
    return None


class ToolRuntime:
    """Everything ask_streaming needs to run a tool, without llm.py importing
    the tool layer (which would be a circular import).

    schemas       — the tool menu handed to the model
    dispatch      — dispatch(name, args) -> result string
    action_tools  — names whose result IS the reply and is spoken verbatim
    on_failure    — called with a failed action's message, for the HUD
    """
    __slots__ = ("schemas", "dispatch", "action_tools", "on_failure",
                 "is_failure", "schema_by_name")

    def __init__(self, schemas, dispatch, action_tools=(), on_failure=None,
                 is_failure=None):
        # Keep one mutable list. The find_tools meta-tool can append schemas
        # between reasoning rounds and the next provider call sees the expansion.
        self.schemas = list(schemas)
        self.dispatch = dispatch
        self.action_tools = frozenset(action_tools)
        self.on_failure = on_failure or (lambda _m: None)
        self.is_failure = is_failure or (lambda _m: False)
        self.schema_by_name = {
            (s.get("function") or {}).get("name"): s for s in schemas
            if (s.get("function") or {}).get("name")
        }

    def add_schemas(self, schemas):
        """Add newly discovered tools without duplicating the request menu."""
        added = []
        for schema in schemas:
            name = (schema.get("function") or {}).get("name")
            if not name or name in self.schema_by_name:
                continue
            self.schemas.append(schema)
            self.schema_by_name[name] = schema
            added.append(name)
        return added


# Tool-selection instructions. These live in the STATIC system message
# (appended to the persona once, identically every turn) rather than in the
# per-turn context block, so they stay inside the cacheable prefix.
TOOL_GUIDANCE = (
    "\n\nYou have tools. Understand the user's whole outcome, not just the first "
    "verb. Use tools when they want something done; answer directly when they "
    "only want conversation. For multi-step work, call every independent tool "
    "together, then use returned results to choose the next dependent tool. "
    "Continue until the complete outcome is reached. Never claim an action "
    "happened unless its tool returned success, and never repeat an action that "
    "already succeeded. Use web_search whenever an answer depends on current or "
    "changing information, or when the user asks you to search or verify. Base "
    "current claims on its results and include the relevant source URLs in the answer. Input "
    "may contain speech-recognition errors or unusual phrasing: infer intent over "
    "literal words. The initial menu is intentionally small: if it lacks a needed "
    "capability, call find_tools, then use the tools it loads. Use known preferences "
    "and reasonable low-risk defaults. Ask "
    "one short question only when a missing value materially changes the result "
    "or makes an action unsafe."
)

MAX_TOOL_ROUNDS = 5
MAX_TOOL_CALLS = 10


_NEXT_ACTION_RE = re.compile(
    r"\b(?:and then|then|after that|afterwards|followed by|once that)\b",
    re.I,
)


def _multi_step_request(text):
    """Generic sequence signal used only to decide whether to keep planning."""
    return bool(_NEXT_ACTION_RE.search(text or ""))

# How much text to hold back before committing to "this turn is a text reply".
# See _stream_turn for why this buffer exists.
_TOOL_DECIDE_CHARS = 40


def _stream_turn(resp, calls, suppress_text=False):
    """Consume ONE streamed completion. Yields text deltas to the caller and
    fills `calls` (index -> {id, name, args}) with any tool calls the model
    emitted. Returns the text it yielded.

    Content is held back for the first few tokens on purpose. If the model is
    calling a tool its tool-call deltas arrive first, but it sometimes emits a
    preamble alongside them ("Sure, opening that for you!"). That preamble must
    never reach the user, because the tool result is the ground truth and it
    might be a failure. Streaming the preamble and then appending "Spotify
    isn't open" is exactly the cheerful lie the honesty rule exists to stop.
    So: buffer, and if tool calls show up, throw the buffer away.

    The cost is holding ~40 characters, which at streaming speed is a few tens
    of milliseconds — far less than the round trip this replaces.
    """
    buf = ""
    out = ""
    committed = False
    t0 = time.time()
    try:
        for chunk in resp:
            delta_obj = chunk.choices[0].delta
            tcs = getattr(delta_obj, "tool_calls", None)
            if tcs:
                for t in tcs:
                    idx = getattr(t, "index", 0) or 0
                    slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if getattr(t, "id", None):
                        slot["id"] = t.id
                    fn = getattr(t, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args"] += fn.arguments
                buf = ""          # discard any preamble — the tool owns this turn
            delta = getattr(delta_obj, "content", None) or ""
            if not delta or calls:
                continue
            if committed:
                out += delta
                if not suppress_text:
                    yield delta
                continue
            buf += delta
            if len(buf) >= _TOOL_DECIDE_CHARS:
                committed = True
                print(f"[timing] first token {int((time.time() - t0) * 1000)}ms")
                out += buf
                if not suppress_text:
                    yield buf
                buf = ""
        if buf and not calls:
            if not committed:
                print(f"[timing] first token {int((time.time() - t0) * 1000)}ms")
            out += buf
            if not suppress_text:
                yield buf
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return out


# ---------- streaming conversation ----------
def ask_streaming(user_input, conversation, frustrated=False, thinking_mode=False,
                  window=None, voice_mode=False, tool_runtime=None,
                  context_scope="full", operational_context="",
                  require_tool=False, min_action_calls=0):
    """Yield LLM reply text chunks from Groq (streaming).

    frustrated      — True → append a tone-adjustment note so Ted is more direct.
    thinking_mode   — True → inject Socratic partner instructions (no advice).
    voice_mode      — True → reply is spoken aloud: short, no formatting.
                      False → text chat: fuller answers, markdown/code allowed.
    tool_runtime    — a ToolRuntime, or None for a pure-conversation call.

    With tool_runtime set this is the ONLY model call a turn needs. The request
    carries the tool schemas and is streamed, so the model either answers in
    text (streamed straight through — one round trip) or calls a tool (executed
    here, then narrated). The old design made a separate non-streamed "probe"
    call first, told it to reply CHAT when no tool was needed, threw that answer
    away, and then made the real streaming call — two round trips on every
    message, when the overwhelming majority of messages are conversation.

    Mutates `conversation` in-place so the history accumulates.
    Saves the final reply to memory and logs facts, both on background threads.
    """
    global _GROQ_OK
    _turn_t0 = time.time()
    action = detect_action(user_input)
    if action:
        yield action
        return

    today = date.today().strftime("%B %d, %Y")

    # --- selective memory retrieval (run concurrently when this turn earns it) ---
    #     so doing them in parallel instead of back-to-back cuts pre-reply latency) ---
    _ctx = {"mem": "", "facts": "", "know": "", "sessions": ""}
    def _load_mem():   _ctx["mem"]   = get_memory(user_input)
    def _load_facts(): _ctx["facts"] = get_facts_about(OWNER_NAME)
    def _load_sessions(): _ctx["sessions"] = format_memories_for_prompt()
    def _load_know():
        if features.HAS_KNOWLEDGE:
            _ctx["know"] = features.knowledge.search(user_input, k=3)
    # Facts load on EVERY turn, including operational ones. They are one local
    # SQLite read, capped at 1200 characters downstream, and they are exactly
    # what makes an action honor a standing preference — "open YouTube in Brave
    # from now on" is stored as a fact, and the turn that needs it is an action
    # turn. Scoping them out re-broke that (see the handoff, §7.4). Episodic
    # retrieval is the expensive part and stays scoped.
    loaders = [_load_facts]
    if context_scope in ("relevant", "full"):
        loaders.extend((_load_mem, _load_know))
    if context_scope == "full":
        loaders.append(_load_sessions)
    _lk_threads = [threading.Thread(target=f, daemon=True) for f in loaders]
    _ctx_t0 = time.time()
    for _t in _lk_threads: _t.start()
    # ONE deadline for all four, not four independent timeouts. `join(timeout=4)`
    # per thread meant the budget was 4s each and therefore 16s total: the reply
    # had not even been requested yet. Retrieval is best-effort context, so a
    # slow source is dropped rather than waited on.
    _ctx_deadline = _ctx_t0 + CONTEXT_BUDGET
    for _t in _lk_threads:
        _t.join(timeout=max(0.0, _ctx_deadline - time.time()))
    _ctx_ms = int((time.time() - _ctx_t0) * 1000)
    if _ctx_ms > 250:
        _slow = [n for n, v in (("memory", _ctx["mem"]), ("facts", _ctx["facts"]),
                                ("knowledge", _ctx["know"]), ("sessions", _ctx["sessions"]))
                 if not v]
        # Silent latency is the expensive kind. Name what was still missing when
        # the budget ran out, so a slow source is findable instead of felt.
        print(f"[timing] context {_ctx_ms}ms"
              + (f" (empty: {', '.join(_slow)})" if _slow else ""))
    past_memory   = _ctx["mem"]
    known_facts   = _ctx["facts"]
    knowledge_ctx = _ctx["know"]
    past_sessions = _ctx["sessions"]

    # The user turn is appended to `conversation` only after a successful reply
    # (see the streaming finally below) so failed calls don't leave a dangling
    # user message with no assistant response.

    # Build the context string injected as a system message just before recent history.
    # Keeping it as a single message (rather than modifying the system prompt) means
    # per-turn data stays out of the prompt cache. Live information is no longer
    # keyword-injected here: web_search is a normal tool selected by the brain.
    # Hard caps on retrieved context. Without these the block grows with the
    # database — more facts, longer recalled exchanges — and every turn pays
    # to reprocess it, which is the slow creep that shows up after a database
    # has been in use for a while. Truncation is cheap insurance.
    def _cap(s, n):
        s = (s or "").strip()
        return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"

    context_parts = [f"Today is {today}."]
    if operational_context:
        context_parts.append(_cap(operational_context, 900) + ".")
    if known_facts:
        context_parts.append(f"Known facts about {OWNER_NAME}: {_cap(known_facts, 1200)}.")
    if past_memory:
        context_parts.append(f"Relevant past exchanges: {_cap(past_memory, 1200)}.")
    if knowledge_ctx:
        context_parts.append(f"Personal knowledge base: {_cap(knowledge_ctx, 1500)}.")

    # Memories of previous sessions — this is what lets Ted say "last time you
    # were stuck on X". Only injected when there are any; most days there won't be.
    if past_sessions:
        context_parts.append(
            f"Things you remember from earlier conversations: {_cap(past_sessions, 1200)}.")
        context_parts.append(
            "Those are your own memories of past conversations. Refer back to them the way a "
            "friend would — only when it actually fits what's being said, at most once, and "
            "without listing them. Never recite a memory that isn't relevant, and never say "
            "you have no memory of something when one of these covers it."
        )

    # Memory continuity nudge
    context_parts.append(
        "If something from earlier in this conversation is relevant to the current message, "
        "reference it naturally — 'like you mentioned', 'you said earlier' — "
        "but only when it genuinely fits."
    )

    # Tone adjustment when frustration has been detected
    if frustrated:
        context_parts.append(
            "The user seems frustrated. Drop any cheerful energy. Be direct, "
            "get to the point faster, and skip filler phrases."
        )

    # Thinking partner: override the persona to Socratic mode (no advice)
    if thinking_mode:
        context_parts.append(THINKING_CONTEXT)

    # Mode: Ted should know whether he's talking or typing, and act like it.
    # NOTE: this line is regenerated fresh every turn and is the ONLY truth
    # about the current mode — the model must not trust earlier turns, because
    # the user flips modes mid-conversation and old claims go stale.
    if voice_mode:
        context_parts.append(
            "CURRENT MODE: VOICE (right now, this exact reply is spoken aloud). "
            "If asked which mode you're in, the answer is VOICE — anything said "
            "about modes earlier in this conversation is outdated; trust only "
            "this line. One or two short sentences. No markdown, no lists, no "
            "code blocks; numbers as words. If the real answer needs code or "
            "lots of detail, give the one-sentence version and offer to put "
            "the rest in chat."
        )
    else:
        context_parts.append(
            "CURRENT MODE: CHAT (right now, this reply appears as text in the "
            "chat window; the mic is off). If asked which mode you're in, the "
            "answer is CHAT — anything said about modes earlier in this "
            "conversation is outdated; trust only this line. Answer properly "
            "and completely like a modern AI chat assistant: full explanations "
            "when the question deserves them, fenced code blocks for any code, "
            "short paragraphs. Still no padding or filler."
        )

    context = "(Context: " + " ".join(context_parts) + ")"

    # Trim conversation to MAX_HISTORY recent turns; always keep [0] (system prompt).
    # ORDER MATTERS FOR SPEED: the per-turn context block changes every turn,
    # so it goes LAST — [static system, ...history, context, user]. With the
    # static prefix byte-identical across calls, Groq's automatic prefix
    # caching skips reprocessing it, which directly cuts time-to-first-token.
    # (Putting instructions closest to the user message also makes the model
    # follow them more reliably — recency wins in attention.)
    history_limit = MAX_HISTORY if context_scope == "full" else (8 if context_scope == "relevant" else 4)
    recent = stable_window(conversation[1:], history_limit)
    # Tool guidance is concatenated onto the persona rather than sent as its own
    # message: it is byte-identical every turn, so it stays in the cached prefix.
    _system = conversation[0]
    if tool_runtime is not None:
        _system = {"role": "system",
                   "content": conversation[0]["content"] + TOOL_GUIDANCE}
    messages = ([_system] + recent
                + [{"role": "system", "content": context},
                   {"role": "user", "content": user_input}])

    # Schemas still make one request do the old probe + response job, but they
    # are real input tokens and the free tier bills them. Routing therefore
    # supplies a focused initial menu instead of the complete catalog.
    #
    # tool_choice stays "auto" on the first call even for an obvious action.
    # Forcing it removes the model's only honest escape route: a turn that
    # cannot be satisfied by any loaded tool has nowhere to go but a wrong call
    # or a dead end, and the classifier deciding "this is an action" is a
    # regex. The recovery path below already forces tool choice on the retry —
    # after the model has demonstrably narrated an action instead of taking
    # one, which is evidence rather than a guess.
    _tools_kw = ({"tools": tool_runtime.schemas, "tool_choice": "auto"}
                 if tool_runtime is not None else {})
    # Provider tokenizers differ, so this is intentionally a stable estimate,
    # not fake precision. It makes prompt regressions visible in the same launch
    # log as latency and rate-limit failures.
    _prompt_chars = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    if tool_runtime is not None:
        _prompt_chars += len(json.dumps(tool_runtime.schemas, ensure_ascii=False,
                                        separators=(",", ":")))
    print(f"[prompt] scope={context_scope} tools="
          f"{len(tool_runtime.schemas) if tool_runtime else 0} "
          f"~{max(1, round(_prompt_chars / 4))} input tokens")

    def _do_groq_call(msgs=None, force_tool=False):
        """Inner helper so the retry logic below can call the same request.
        chat_create handles the primary → availability fallback internally."""
        tool_kwargs = dict(_tools_kw)
        if force_tool and tool_runtime is not None:
            tool_kwargs["tool_choice"] = "required"
        return chat_create(
            messages=messages if msgs is None else msgs,
            # Voice keeps the old tight cap; chat gets room for real answers —
            # 250 was silently truncating anything longer than a short paragraph.
            max_tokens=250 if voice_mode else 1200,
            stream=True,
            timeout=12.0 if voice_mode else 30.0,
            reasoning_effort=reasoning_effort_for(user_input),
            **tool_kwargs,
        )

    # The provider has already attempted cloud -> local fallback. These retries
    # cover a transient failure only when neither brain completed the request.
    import groq as _groq_mod
    resp = None
    closing = False
    _req_t0 = time.time()
    try:
        resp = _do_groq_call()
    except _groq_mod.RateLimitError:
        print("[groq] rate limited — waiting 3s then retrying")
        time.sleep(3)
        try:
            resp = _do_groq_call()
        except _groq_mod.RateLimitError:
            print("[groq] still rate limited — waiting 5s")
            time.sleep(5)
            try:
                resp = _do_groq_call()
            except Exception as e:
                print(f"[groq] rate limit retry failed: {e}")
                _GROQ_OK = False
                if window: show_issue(window, "Both Ted brains are temporarily unavailable.")
                yield "Both my online and offline brains are unavailable right now — try again in a moment."
                return
    except (_groq_mod.APITimeoutError, Exception) as e:
        if "timeout" in str(e).lower() or isinstance(e, _groq_mod.APITimeoutError):
            print(f"[groq] timeout on first attempt — retrying: {e}")
            time.sleep(1)
            try:
                resp = _do_groq_call()
            except Exception as e2:
                print(f"[groq] timeout retry failed: {e2}")
                _GROQ_OK = False
                if window: show_issue(window, "Both Ted brains timed out.")
                yield "Both my online and offline brains timed out — try again in a moment."
                return
        elif "tool_use_failed" in str(e):
            # The model emitted a syntactically broken call, or invented a tool
            # name that isn't in the menu. One clean retry usually produces a
            # valid call instead of losing the turn.
            print(f"[groq] malformed tool call from model — retrying once: {str(e)[:120]}")
            try:
                resp = _do_groq_call()
            except Exception as e2:
                print(f"[groq] malformed-call retry failed: {e2}")
                error_log.error(f"Groq tool_use_failed twice: {e2}")
                _GROQ_OK = False
                yield "I tried to use a tool for that and it didn't take — say it again?"
                return
        else:
            print(f"[provider] both brains failed: {e}")
            error_log.error(f"Provider failure: {e}\n{traceback.format_exc()}")
            _GROQ_OK = False
            if window: show_issue(window, "Both Ted brains failed to generate a reply.")
            yield "I ran into an issue — give me a second and try again."
            return

    # How long the request itself took to be accepted, separately from how long
    # the model then took to say something. A slow turn is one or the other and
    # they have completely different causes: this one is network, retries, and
    # the local-brain attempt; the [timing] first token line below is the model.
    _req_ms = int((time.time() - _req_t0) * 1000)
    if _req_ms > 400:
        print(f"[timing] request accepted after {_req_ms}ms "
              f"({providers.active_provider()})")

    _GROQ_OK = providers.active_provider() == "groq"
    full_reply = ""
    # Results from the most recent round that produced any. If the loop ends
    # without the model writing a sentence, these are said instead of nothing.
    # Ending silent is the worst outcome: _respond turns an empty stream into a
    # rotated "didn't quite catch that", which blames the user for a tool that
    # actually ran.
    last_results = []
    # (name, arguments) already executed this turn. Models can call the same
    # tool again after seeing its result, and for a slow tool like
    # screen_describe — screenshot plus a vision call — three rounds of that is
    # a minute of silence with two wasted screenshots.
    seen_calls = set()
    action_results = []
    had_non_action = False
    planning_sequence = _multi_step_request(user_input)
    # App.py supplies an exact lower bound for recognized multi-target requests.
    # Keep ask_streaming safe for direct callers too: an explicit dependency
    # connector ("and then", "after that") necessarily asks for at least two
    # completed stages even when the caller did not pre-compute a count.
    effective_min_action_calls = max(
        min_action_calls, 2 if planning_sequence else 1)
    tool_retry_used = False
    stream_retry_used = False
    completion_retry_used = False
    completed_tool_calls = 0
    try:
        msgs = messages
        rounds = 0
        total_calls = 0
        while True:
            rounds += 1
            calls = {}
            # Hold back prose on a turn that is unmistakably a Mac command, so
            # a fake "Opened it" never reaches the user ahead of the real tool
            # result. This is only safe because require_tool is now a narrow
            # device-verb test; when it covered write/check/show/tell it made
            # "write me a poem" come back silent. Withheld text is not thrown
            # away — if no tool call materialises it is released below.
            expecting_tool = (require_tool or tool_retry_used) and total_calls == 0
            try:
                turn_text = yield from _stream_turn(
                    resp, calls,
                    suppress_text=bool(action_results) or expecting_tool)
            except Exception as e:
                # Groq can accept a completion and only report malformed tool
                # JSON while the stream is being consumed. Required-action text
                # is suppressed and completed calls are recorded in ``msgs``, so
                # one retry can safely continue without repeating real work.
                incomplete_required_action = (
                    tool_runtime is not None and require_tool
                    and completed_tool_calls < effective_min_action_calls)
                if (incomplete_required_action and not stream_retry_used
                        and rounds < MAX_TOOL_ROUNDS):
                    stream_retry_used = True
                    print(f"[tools] provider stream failed before an action — "
                          f"retrying once: {str(e)[:120]}", flush=True)
                    msgs = msgs + [{"role": "system", "content": (
                        "The previous tool response was malformed. Preserve any tools "
                        "that already succeeded, do not repeat them, and continue the "
                        "missing requested action now with valid tool arguments."
                    )}]
                    resp = _do_groq_call(msgs, force_tool=True)
                    continue
                raise
            if rounds == 1:
                # The number that matches what the user felt: their key press to
                # the first word on screen, retrieval, retries and all.
                print(f"[timing] turn to first output "
                      f"{int((time.time() - _turn_t0) * 1000)}ms")
            elif calls:
                print(f"[timing] round {rounds} after "
                      f"{int((time.time() - _turn_t0) * 1000)}ms")

            # Text emitted ALONGSIDE a tool call was already streamed to the
            # HUD and the speaker, so it has to land in full_reply too:
            # full_reply is the turn that gets stored, and memory and the chat
            # transcript are built from it. Dropping it desynced what Ted said
            # from what Ted remembers saying. Suppressed rounds yield nothing,
            # so there is nothing to capture once action_results is non-empty.
            if calls and turn_text and not action_results:
                full_reply += turn_text

            # Prose that CLAIMS a completed action while calling nothing is a
            # model-routing failure, not a reason to make the user repeat
            # himself: retry once with tool choice forced. The trigger is the
            # claim itself, not a guess about intent — a turn that simply
            # answers in words is a legitimate answer and is left alone.
            claimed_without_tool = (
                not calls and tool_runtime is not None and total_calls == 0
                and claims_completed_action(turn_text)
            )
            if (not calls and tool_runtime is not None and total_calls == 0
                    and (expecting_tool or claimed_without_tool)
                    and not tool_retry_used and rounds < MAX_TOOL_ROUNDS):
                tool_retry_used = True
                print("[honesty] action request returned no tool — retrying automatically",
                      flush=True)
                if not expecting_tool and turn_text:
                    # Auto-mode prose may already have streamed before its claim
                    # was recognizable. Preserve what the user saw in memory.
                    full_reply += turn_text
                msgs = msgs + [
                    {"role": "assistant", "content": turn_text},
                    {"role": "system", "content": (
                        "That response described an action without performing it. "
                        "Do not narrate. Call the required tool now; if a capability "
                        "is missing, call find_tools first."
                    )},
                ]
                try:
                    resp = _do_groq_call(msgs, force_tool=True)
                    continue
                except Exception as e:
                    print(f"[honesty] automatic tool retry failed: {e}")

            if (not calls and action_results
                    and completed_tool_calls < effective_min_action_calls
                    and not completion_retry_used and rounds < MAX_TOOL_ROUNDS):
                completion_retry_used = True
                print(f"[tools] outcome incomplete ({completed_tool_calls}/"
                      f"{effective_min_action_calls} actions) — continuing", flush=True)
                msgs = msgs + [
                    {"role": "assistant", "content": turn_text},
                    {"role": "system", "content": (
                        f"Only {completed_tool_calls} of at least "
                        f"{effective_min_action_calls} "
                        "requested actions ran. Continue with the missing target or "
                        "stage now. Do not repeat completed calls."
                    )},
                ]
                try:
                    resp = _do_groq_call(msgs, force_tool=True)
                    continue
                except Exception as e:
                    print(f"[tools] completion retry failed: {e}")

            # Plain conversation: the answer already streamed. One round trip.
            if not calls or tool_runtime is None:
                if action_results:
                    # Action claims come only from handlers. A model's final
                    # narration may be appended for mixed read+act chains, but
                    # it can never replace the verified action results.
                    parts = [str(r) for r in action_results]
                    if had_non_action and turn_text.strip():
                        parts.append(turn_text.strip())
                    final = " ".join(parts)
                    full_reply += final
                    yield final
                else:
                    if expecting_tool:
                        # An action turn ended with no tool call and no retry
                        # left. The withheld text is released rather than
                        # replaced by a canned line: "Notes isn't installed" is
                        # a real answer and throwing it away to say "I couldn't
                        # turn that into an action" loses the only useful part.
                        # If it also CLAIMED to have acted, it gets corrected.
                        full_reply += turn_text
                        if turn_text:
                            yield turn_text
                        if claims_completed_action(turn_text):
                            print("[honesty] action turn claimed an outcome with "
                                  "no tool call", flush=True)
                            error_log.error(
                                "Action turn claimed an outcome with no tool call: "
                                f"{turn_text.strip()[:200]}")
                            correction = ("\n\n(Correction: I didn't actually run "
                                          "anything just then.)")
                            full_reply += correction
                            yield correction
                        elif not turn_text.strip():
                            fallback = ("I couldn't turn that into an action, "
                                        "so nothing ran.")
                            full_reply += fallback
                            yield fallback
                        break
                    full_reply += turn_text
                    # No tool ran this turn. If the model nonetheless said it
                    # did something, say so rather than letting it stand.
                    if (tool_runtime is not None and total_calls == 0
                            and claims_completed_action(turn_text)):
                        print("[honesty] model claimed a completed action with "
                              "no tool call", flush=True)
                        error_log.error(
                            "Model claimed a completed action with no tool call: "
                            f"{turn_text.strip()[:200]}")
                        correction = ("\n\n(Correction: I didn't actually run "
                                      "anything just then.)")
                        full_reply += correction
                        yield correction
                break

            ordered = [calls[i] for i in sorted(calls)]
            prepared = []
            for c in ordered:
                parse_error = None
                try:
                    args = json.loads(c["args"] or "{}")
                except Exception:
                    args = None
                    parse_error = "arguments were not valid JSON"
                schema = tool_runtime.schema_by_name.get(c["name"])
                if schema is None:
                    parse_error = f"unknown tool '{c['name']}'"
                elif parse_error is None:
                    parse_error = validate_tool_arguments(schema, args)
                canonical = (c["name"], json.dumps(args, sort_keys=True)
                             if isinstance(args, dict) else c["args"] or "{}")
                if canonical in seen_calls:
                    continue
                seen_calls.add(canonical)
                prepared.append((c, args, parse_error))
            if not prepared:
                print("[tools] model repeated a call it already made — stopping")
                break
            if total_calls + len(prepared) > MAX_TOOL_CALLS:
                print(f"[tools] hit MAX_TOOL_CALLS ({MAX_TOOL_CALLS})")
                break
            total_calls += len(prepared)
            msgs = msgs + [{
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": c["id"] or f"call_{n}",
                                "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": c["args"] or "{}"}}
                               for n, (c, _args, _err) in enumerate(prepared)],
            }]

            results = []
            all_actions = True
            action_failed = False
            for n, (c, args, validation_error) in enumerate(prepared):
                if not c["name"]:
                    continue
                if validation_error:
                    all_actions = False
                    repair = (f"TOOL_ARGUMENT_ERROR for {c['name']}: {validation_error}. "
                              "Correct the arguments and call the tool again; nothing ran.")
                    print(f"[tools] rejected {c['name']}: {validation_error}")
                    msgs.append({"role": "tool",
                                 "tool_call_id": c["id"] or f"call_{n}",
                                 "name": c["name"],
                                 "content": repair})
                    continue
                result = tool_runtime.dispatch(c["name"], args)
                if result is None:
                    # None means the handler crashed or the tool is unknown.
                    # Never turn that into a cheerful "Done." — report the truth.
                    result = "That didn't go through — something failed on my end."
                print(f"[tools] {c['name']}({args}) → {str(result)[:80]}")
                results.append(result)
                if c["name"] != "find_tools":
                    completed_tool_calls += 1
                if c["name"] not in tool_runtime.action_tools:
                    all_actions = False
                    had_non_action = True
                else:
                    # The hook decides whether this reads as a failure worth
                    # surfacing on the HUD — that check lives in the tool layer.
                    tool_runtime.on_failure(result)
                    action_results.append(result)
                    action_failed = action_failed or tool_runtime.is_failure(result)
                msgs.append({"role": "tool",
                             "tool_call_id": c["id"] or f"call_{n}",
                             "name": c["name"],
                             "content": str(result)})

            if results:
                last_results = results

            # A simple action still completes in one model call. A request that
            # explicitly contains a sequence keeps planning after successful
            # actions, so "open Notes and then type..." can issue the dependent
            # second tool. Failed actions stop immediately for safety.
            if all_actions and results:
                outcome_complete = completed_tool_calls >= effective_min_action_calls
                if action_failed or (outcome_complete and not planning_sequence):
                    final = " ".join(str(r) for r in action_results)
                    full_reply += final
                    yield final
                    break
                if outcome_complete and planning_sequence:
                    # The lower bound accounts for each explicit dependent stage.
                    # Once reached, another model round would only invite repeats.
                    final = " ".join(str(r) for r in action_results)
                    full_reply += final
                    yield final
                    break

            if rounds >= MAX_TOOL_ROUNDS:
                print(f"[tools] hit MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS})")
                if action_results and not full_reply.strip():
                    final = " ".join(str(r) for r in action_results)
                    full_reply += final
                    yield final
                break

            try:
                resp = _do_groq_call(msgs)
            except Exception as e:
                print(f"[groq] follow-up call failed: {e}")
                break
    except GeneratorExit:
        # speak_streaming closes this generator when the user interrupts or the
        # window exits. Yielding a fallback from ``finally`` during close raises
        # "generator ignored GeneratorExit" and was leaking into launch logs.
        closing = True
        raise
    except Exception as e:
        print(f"[groq] stream error mid-response: {e}")
        error_log.error(f"Groq stream error: {e}\n{traceback.format_exc()}")
    finally:
        # One exit for every path above. A turn that ran tools but produced no
        # sentence says what the tools returned; a turn that produced nothing at
        # all says so honestly. Never silence.
        if closing:
            return
        if not full_reply.strip():
            if last_results:
                final = " ".join(str(r) for r in last_results)
                full_reply = final
                yield final
            else:
                yield "Something cut out — ask me again."
        _remember_exchange(user_input, full_reply, conversation)

# ---------- composition helpers (messages / email) ----------
def generate_message_text(instruction, contact):
    """Use a quick Groq call to turn a spoken instruction into an actual message text."""
    try:
        resp = chat_create(
            messages=[
                {"role": "system", "content":
                 "Convert the following spoken instruction into a short, natural, casual "
                 "text message (1–2 sentences). Return ONLY the message text — no quotes, "
                 "no preamble, no commentary."},
                {"role": "user", "content":
                 f"Recipient: {contact}\nInstruction: {instruction}"},
            ],
            max_tokens=80,
            timeout=8.0,
        )
        return resp.choices[0].message.content.strip().strip("\"'")
    except Exception:
        return instruction   # fallback: send the raw instruction


def generate_message_with_style(instruction, contact_name, style):
    """LLM-compose an iMessage given a plain-language instruction and style."""
    prompt = (
        f"Write a text message to {contact_name}. "
        f"The message should: {instruction}. "
        f"Tone/style: {style}. "
        "Reply with ONLY the message text — no quotes, no preamble, no sign-off."
    )
    try:
        resp = chat_create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.75,
        )
        return resp.choices[0].message.content.strip().strip('"').strip("'")
    except Exception:
        return f"Hey, just checking — {instruction}"


def generate_email_body(instruction, to_address, subject, style):
    """LLM-compose a full email body."""
    prompt = (
        f"Write a professional email to {to_address} with subject '{subject}'. "
        f"The email should: {instruction}. "
        f"Tone/style: {style or 'professional and clear'}. "
        "Reply with ONLY the email body text — no subject line, no 'Dear X', just the body and a short sign-off."
    )
    try:
        resp = chat_create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return instruction


def summarize_email_body(body, contact_name):
    """Summarize email content via LLM."""
    if len(body.split()) < 40:
        return body
    try:
        resp = chat_create(
            messages=[{
                "role": "user",
                "content": f"Summarize this email from {contact_name} in 2 sentences max, spoken aloud style:\n\n{body[:2000]}"
            }],
            max_tokens=80,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return body[:300]


# ── Session memories ──────────────────────────────────────────────────────────
#
# Most sessions are Charlie testing something: "set a timer", "play Spotify",
# "what can you do". Writing a memory for every one of those buries the handful
# of real conversations and makes callbacks worse than having none — Ted opening
# with "last time you set a two minute timer" is worse than saying nothing.
#
# So there are two filters. A cheap Python one kills the obviously-empty
# sessions without spending a token, and the model itself decides whether what's
# left is worth remembering, with permission to say no.

# Minimum bar before a session is even sent to the model. The word count is
# measured over SUBSTANTIVE turns only — counting "pause" and "play" toward the
# total meant a session with one real question buried in commands got dropped.
MIN_MEMORY_USER_TURNS       = 3
MIN_MEMORY_SUBSTANTIVE_WORDS = 15

# Single-purpose command openers. A session made of nothing but these is Ted
# being operated, not a conversation, no matter how many turns it ran.
_ROUTINE_OPENERS = (
    "play", "pause", "resume", "skip", "next song", "previous", "stop",
    "set a timer", "set timer", "cancel", "mute", "unmute", "volume",
    "open ", "close ", "quit ", "launch ", "what time", "what's the time",
    "remind me", "shuffle", "turn it up", "turn it down", "louder", "quieter",
    "hey ted", "ted", "thanks", "thank you", "never mind", "nevermind",
)


def _looks_routine(text):
    t = (text or "").strip().lower().rstrip(".!?")
    if not t:
        return True
    if len(t.split()) <= 2:
        return True
    return any(t.startswith(op) for op in _ROUTINE_OPENERS) and len(t.split()) < 8


def session_has_substance(conversation):
    """Cheap pre-filter: is this session even worth asking the model about?

    Pure function over the message list — unit-tested in tests/test_memory.py.
    """
    turns = [m for m in conversation[1:] if m.get("role") == "user"]
    if len(turns) < MIN_MEMORY_USER_TURNS:
        return False
    # Only turns that aren't bare commands or acknowledgements count as content.
    real = [(m.get("content") or "") for m in turns if not _looks_routine(m.get("content"))]
    if not real:
        return False
    return sum(len(t.split()) for t in real) >= MIN_MEMORY_SUBSTANTIVE_WORDS


_MEMORY_SYSTEM = """You are Ted's memory. You decide what Ted remembers about a conversation with Charlie.

Write a memory ONLY if this conversation contains something Ted would plausibly want to bring up later: a project or problem worked on, a decision made, plans, something going on in Charlie's life, an opinion or preference he expressed, or a real back-and-forth about a topic.

Do NOT write a memory for: testing and debugging Ted himself, timers/reminders/music/app commands, one-off factual questions, greetings, or small talk. These are the majority. Saying no is the correct answer most of the time.

Reply with JSON only:
{"worth_remembering": true/false, "memory": "...", "topics": "..."}

If worth_remembering is false, leave memory and topics empty.

The memory must be written from Ted's point of view, in past tense, as something he could naturally say out loud later. Two sentences maximum. Be specific — name the actual thing, not the category.

Good:  "Charlie was trying to get his crew dispatch board to update from Airtable and kept hitting a webhook that fired twice. He was going to try debouncing it."
Good:  "Charlie mentioned he's starting fall semester in a few weeks and is worried about keeping up with Ted on top of coursework."
Bad:   "Charlie and I discussed various topics including technology." (vague)
Bad:   "Charlie asked me to set a timer and play music." (routine)

topics: 2-5 lowercase comma-separated keywords for later search, e.g. "crew dispatch, airtable, webhooks"."""


def generate_session_summary(conversation):
    """Decide whether this session is worth remembering, and if so write the memory.

    Returns {"text": str, "topics": str} — or None when the session isn't worth
    storing (which is the common, intended case) or generation failed.
    """
    if not session_has_substance(conversation):
        return None

    recent = conversation[1:][-30:]
    transcript = "\n".join(
        f"{'Charlie' if m.get('role') == 'user' else 'Ted'}: {(m.get('content') or '')[:240]}"
        for m in recent
        if m.get("role") in ("user", "assistant")
    )
    if not transcript.strip():
        return None

    try:
        resp = chat_create(
            messages=[
                {"role": "system", "content": _MEMORY_SYSTEM},
                {"role": "user", "content": transcript},
            ],
            response_format={"type": "json_object"},
            max_tokens=220,
            temperature=0.2,
            timeout=12.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        error_log.error(f"[memory] session summary generation failed: {e}")
        return None

    try:
        data = json.loads(raw)
    except Exception:
        # Salvage a JSON object out of a chatty reply, same as the fact extractor.
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            error_log.error(f"[memory] session summary returned non-JSON: {raw[:200]!r}")
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except Exception:
            error_log.error(f"[memory] session summary unparseable: {raw[:200]!r}")
            return None

    if not isinstance(data, dict) or not data.get("worth_remembering"):
        return None
    text = (data.get("memory") or "").strip()
    if len(text) < 20:                     # empty or a stub — treat as "not worth it"
        return None
    return {"text": text, "topics": (data.get("topics") or "").strip().lower()[:120]}
