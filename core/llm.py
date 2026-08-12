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

from groq import Groq

from core import features, intents
from core.actions import detect_action
from core.hud_bridge import show_issue
from core.logs import error_log
from core.memory import (save_memory, get_memory, save_fact, get_facts_about,
                         format_memories_for_prompt)

from config import GROQ_API_KEY  # required — app won't start without this
try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"

# ONE reasoning model. Everything that thinks — replies, tool calls, fact
# extraction, session summaries, summarising a web result — goes through
# chat_create() and lands on CHAT_MODEL.
#
# CHAT_FALLBACK_MODEL is not a second brain; it is an availability twin that
# only runs when the primary is rate-limited or down, and it says so in the log
# when it does. Aug 2026: three other models were removed. llama-3.1-8b-instant
# was running fact extraction and session memory — an 8B doing judgment work,
# and the source of both the five-week silent JSON failure and the "bananas are
# berries" facts. groq/compound-mini was answering anything matching a keyword
# list, unstreamed, with a 14s timeout and a retry, deciding before the model
# ever saw the message. claude-sonnet-5 was a relay that never had a key.
#
# gpt-oss-120b benchmarked 5-9x faster than llama-3.3-70b with better tool
# calling (it handled the misheard-verb cases that made llama emit malformed
# tool calls).
CHAT_MODEL          = "openai/gpt-oss-120b"
CHAT_FALLBACK_MODEL = "llama-3.3-70b-versatile"

groq_client = Groq(api_key=GROQ_API_KEY)


def chat_create(**kwargs):
    """chat.completions.create on the primary chat model, with automatic
    fallback to CHAT_FALLBACK_MODEL when the primary is rate-limited or
    erroring (they share limits with the web-answer model). gpt-oss is a
    reasoning model — reasoning_effort='low' keeps voice latency snappy and
    is only sent to models that accept it."""
    last_exc = None
    for model in (CHAT_MODEL, CHAT_FALLBACK_MODEL):
        params = dict(kwargs)
        params["model"] = model
        if model.startswith("openai/gpt-oss"):
            params["reasoning_effort"] = "low"
        try:
            return groq_client.chat.completions.create(**params)
        except Exception as e:
            last_exc = e
            msg = str(e)
            retryable = any(k in msg for k in
                            ("429", "rate_limit", "over capacity", "503",
                             "500", "413", "request_too_large",
                             # model retired/renamed — keep Ted alive on llama
                             "404", "model_not_found", "decommissioned"))
            if model != CHAT_FALLBACK_MODEL and retryable:
                print(f"[llm] {model} unavailable ({msg[:80]}) — "
                      f"falling back to {CHAT_FALLBACK_MODEL}")
                continue
            raise
    raise last_exc

MAX_HISTORY = 20        # messages sent to LLM per turn
MAX_CONV_MESSAGES = 40  # hard cap on stored conversation length (keeps system msg at [0])


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
    return _GROQ_OK

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
            parts.append(f"[{when}] {title}: {body}" if when else f"{title}: {body}")
        return " ".join(parts)
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
class ToolRuntime:
    """Everything ask_streaming needs to run a tool, without llm.py importing
    the tool layer (which would be a circular import).

    schemas       — the tool menu handed to the model
    dispatch      — dispatch(name, args) -> result string
    action_tools  — names whose result IS the reply and is spoken verbatim
    on_failure    — called with a failed action's message, for the HUD
    """
    __slots__ = ("schemas", "dispatch", "action_tools", "on_failure")

    def __init__(self, schemas, dispatch, action_tools=(), on_failure=None):
        self.schemas = schemas
        self.dispatch = dispatch
        self.action_tools = frozenset(action_tools)
        self.on_failure = on_failure or (lambda _m: None)


# Tool-selection instructions. These live in the STATIC system message
# (appended to the persona once, identically every turn) rather than in the
# per-turn context block, so they stay inside the cacheable prefix.
TOOL_GUIDANCE = (
    "\n\nYou have tools. Use one when the user wants something done rather than "
    "discussed, and answer directly when they don't — most turns are conversation "
    "and need no tool at all. Never call a tool to look busy, and never call one "
    "to look up something you already know. Input may contain speech-recognition "
    "errors or unusual phrasing — read intent over literal words. If a detail a "
    "tool needs is missing, pick the reasonable default and proceed rather than "
    "stalling. Honour stated preferences: if the user has said they want a site "
    "opened in a particular browser, pass that browser."
)

MAX_TOOL_ROUNDS = 3

# How much text to hold back before committing to "this turn is a text reply".
# See _stream_turn for why this buffer exists.
_TOOL_DECIDE_CHARS = 40


def _stream_turn(resp, calls):
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
                yield delta
                continue
            buf += delta
            if len(buf) >= _TOOL_DECIDE_CHARS:
                committed = True
                print(f"[timing] first token {int((time.time() - t0) * 1000)}ms")
                out += buf
                yield buf
                buf = ""
        if buf and not calls:
            if not committed:
                print(f"[timing] first token {int((time.time() - t0) * 1000)}ms")
            out += buf
            yield buf
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return out


# ---------- streaming conversation ----------
def ask_streaming(user_input, conversation, frustrated=False, thinking_mode=False,
                  window=None, voice_mode=False, tool_runtime=None):
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
    action = detect_action(user_input)
    if action:
        yield action
        return

    today = date.today().strftime("%B %d, %Y")

    # --- live-web questions: search, then let the one model answer from it ---
    # This used to hop to groq/compound-mini, which answered by itself, was not
    # streamed, and had a 14s timeout plus a retry — so a message containing
    # "news" could sit silent for half a minute and then arrive all at once.
    # Now the snippets go into the context block and the normal streamed reply
    # uses them, which means live-web answers stream like everything else.
    search_results = ""
    _web_error_msg = None
    if intents._needs_web(user_input):
        _t_web = time.time()
        raw = search_web(user_input)
        print(f"[timing] web search {int((time.time() - _t_web) * 1000)}ms")
        if raw == "__NO_RESULTS__":
            _web_error_msg = "I couldn't find anything on that."
        elif raw == "__SEARCH_ERROR__":
            _web_error_msg = "I couldn't reach the web right now."
        else:
            search_results = raw

    # If web search completely failed, short-circuit rather than hallucinating
    if _web_error_msg and not search_results:
        yield _web_error_msg
        return

    # --- memory retrieval (run concurrently — independent network/DB round-trips,
    #     so doing them in parallel instead of back-to-back cuts pre-reply latency) ---
    _ctx = {"mem": "", "facts": "", "know": "", "sessions": ""}
    def _load_mem():   _ctx["mem"]   = get_memory(user_input)
    def _load_facts(): _ctx["facts"] = get_facts_about(OWNER_NAME)
    def _load_sessions(): _ctx["sessions"] = format_memories_for_prompt()
    def _load_know():
        if features.HAS_KNOWLEDGE:
            _ctx["know"] = features.knowledge.search(user_input, k=3)
    _lk_threads = [threading.Thread(target=f, daemon=True)
                   for f in (_load_mem, _load_facts, _load_know, _load_sessions)]
    for _t in _lk_threads: _t.start()
    for _t in _lk_threads: _t.join(timeout=4.0)
    past_memory   = _ctx["mem"]
    known_facts   = _ctx["facts"]
    knowledge_ctx = _ctx["know"]
    past_sessions = _ctx["sessions"]

    # The user turn is appended to `conversation` only after a successful reply
    # (see the streaming finally below) so failed calls don't leave a dangling
    # user message with no assistant response.

    # Build the context string injected as a system message just before recent history.
    # Keeping it as a single message (rather than modifying the system prompt) means
    # per-turn data (today's date, web results) stays out of the prompt cache.
    # Hard caps on retrieved context. Without these the block grows with the
    # database — more facts, longer recalled exchanges — and every turn pays
    # to reprocess it, which is the slow creep that shows up after a database
    # has been in use for a while. Truncation is cheap insurance.
    def _cap(s, n):
        s = (s or "").strip()
        return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"

    context_parts = [f"Today is {today}."]
    if search_results:
        context_parts.append(f"Relevant web results: {_cap(search_results, 2000)}.")
    context_parts.append(f"Known facts about {OWNER_NAME}: {_cap(known_facts, 1200) or 'none'}.")
    context_parts.append(f"Relevant past exchanges: {_cap(past_memory, 1200) or 'none'}.")
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
    recent = stable_window(conversation[1:], MAX_HISTORY)
    # Tool guidance is concatenated onto the persona rather than sent as its own
    # message: it is byte-identical every turn, so it stays in the cached prefix.
    _system = conversation[0]
    if tool_runtime is not None:
        _system = {"role": "system",
                   "content": conversation[0]["content"] + TOOL_GUIDANCE}
    messages = ([_system] + recent
                + [{"role": "system", "content": context},
                   {"role": "user", "content": user_input}])

    # The schemas are part of the static request shape, so prefix caching covers
    # them — attaching them to every turn costs close to nothing, and it is what
    # lets one call do the job the probe + streaming pair used to do.
    _tools_kw = ({"tools": tool_runtime.schemas, "tool_choice": "auto"}
                 if tool_runtime is not None else {})

    def _do_groq_call(msgs=None):
        """Inner helper so the retry logic below can call the same request.
        chat_create handles the gpt-oss → llama fallback internally."""
        return chat_create(
            messages=messages if msgs is None else msgs,
            # Voice keeps the old tight cap; chat gets room for real answers —
            # 250 was silently truncating anything longer than a short paragraph.
            max_tokens=250 if voice_mode else 1200,
            stream=True,
            timeout=12.0 if voice_mode else 30.0,
            **_tools_kw,
        )

    # Auto-retry on rate limits (up to 3×) and transient timeouts (up to 2×).
    # Any other API error is logged and surfaced as a spoken fallback.
    import groq as _groq_mod
    resp = None
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
                if window: show_issue(window, "Groq is rate-limiting Ted right now — give it a moment.")
                yield "I'm being throttled right now — give me a moment and try again."
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
                if window: show_issue(window, "Ted can't reach Groq (timeout) — check your connection.")
                yield "I can't reach my brain right now — try again in a moment."
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
            print(f"[groq] API error: {e}")
            error_log.error(f"Groq API error: {e}\n{traceback.format_exc()}")
            _GROQ_OK = False
            if window: show_issue(window, "Groq API error — Ted couldn't generate a reply.")
            yield "I ran into an issue — give me a second and try again."
            return

    _GROQ_OK = True   # we have a response object — Groq is reachable
    full_reply = ""
    # Results from the most recent round that produced any. If the loop ends
    # without the model writing a sentence, these are said instead of nothing.
    # Ending silent is the worst outcome: _respond turns an empty stream into a
    # rotated "didn't quite catch that", which blames the user for a tool that
    # actually ran.
    last_results = []
    # (name, arguments) already executed this turn. gpt-oss will happily call
    # the same tool again after seeing its result, and for a slow tool like
    # screen_describe — screenshot plus a vision call — three rounds of that is
    # a minute of silence with two wasted screenshots.
    seen_calls = set()
    try:
        msgs = messages
        rounds = 0
        while True:
            rounds += 1
            calls = {}
            full_reply += yield from _stream_turn(resp, calls)

            # Plain conversation: the answer already streamed. One round trip.
            if not calls or tool_runtime is None:
                break

            ordered = [calls[i] for i in sorted(calls)]
            fresh = [c for c in ordered
                     if (c["name"], c["args"] or "{}") not in seen_calls]
            if not fresh:
                print("[tools] model repeated a call it already made — stopping")
                break
            for c in fresh:
                seen_calls.add((c["name"], c["args"] or "{}"))
            ordered = fresh
            msgs = msgs + [{
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": c["id"] or f"call_{n}",
                                "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": c["args"] or "{}"}}
                               for n, c in enumerate(ordered)],
            }]

            results = []
            all_actions = True
            for n, c in enumerate(ordered):
                if not c["name"]:
                    continue
                try:
                    args = json.loads(c["args"] or "{}")
                except Exception:
                    args = {}
                result = tool_runtime.dispatch(c["name"], args)
                if result is None:
                    # None means the handler crashed or the tool is unknown.
                    # Never turn that into a cheerful "Done." — report the truth.
                    result = "That didn't go through — something failed on my end."
                print(f"[tools] {c['name']}({args}) → {str(result)[:80]}")
                results.append(result)
                if c["name"] not in tool_runtime.action_tools:
                    all_actions = False
                else:
                    # The hook decides whether this reads as a failure worth
                    # surfacing on the HUD — that check lives in the tool layer.
                    tool_runtime.on_failure(result)
                msgs.append({"role": "tool",
                             "tool_call_id": c["id"] or f"call_{n}",
                             "content": str(result)})

            if results:
                last_results = results

            # ACTION tools report ground truth. Their result IS the reply — say it
            # verbatim and STOP. Never let the model take another round to
            # re-narrate; that is where "Spotify isn't open" becomes a cheerful
            # fake "Playing your music!".
            if all_actions and results:
                final = " ".join(str(r) for r in results)
                full_reply += final
                yield final
                break

            if rounds >= MAX_TOOL_ROUNDS:
                print(f"[tools] hit MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS})")
                break

            try:
                resp = _do_groq_call(msgs)
            except Exception as e:
                print(f"[groq] follow-up call failed: {e}")
                break
    except Exception as e:
        print(f"[groq] stream error mid-response: {e}")
        error_log.error(f"Groq stream error: {e}\n{traceback.format_exc()}")
    finally:
        # One exit for every path above. A turn that ran tools but produced no
        # sentence says what the tools returned; a turn that produced nothing at
        # all says so honestly. Never silence.
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
